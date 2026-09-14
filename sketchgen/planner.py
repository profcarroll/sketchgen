"""The local planner: one prompt in, a brief and a closed list of assertions out.

`gemma4:e4b` (the vision-capable local model, used text-only here) expands a
submitted prompt into

  - a **brief**: the version of the prompt the executor writes from and the
    judges read, under 120 words and concrete about what a viewer sees and does;
  - an **assertion list**: words the gate runs against the finished sketch.

The assertion vocabulary is closed (spec section 3.2). The model does not get to
invent assertions: anything outside ``VOCAB`` is dropped by :func:`validate` and
recorded in ``rejected``, so a hallucinated word costs a line in the record and
never reaches the runner. ``examples/week11-self-hosted-ai/sketch-gate/
sketch_gate.py`` in the course repo is authoritative on the vocabulary; the copy
below must be edited in the same change as that file, never on its own.

No model is called unless :func:`plan` is given a ``host`` and no ``stub``. The
``--stub FILE`` path replays a saved response and is how the tests run.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

__all__ = [
    "DEFAULT_HOST",
    "PROMPT_PATH",
    "VOCAB",
    "Plan",
    "PlannerFailed",
    "PlannerRefused",
    "build_prompt",
    "parse_response",
    "plan",
    "prompt_version",
    "utc_now",
    "validate",
    "validate_detailed",
]

#: The gate's vocabulary, copied from ``sketch_gate.py``. ``size(w,h)`` is a
#: shape, not a literal: the model writes the numbers in.
VOCAB = [
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "size(w,h)",
    "no_motion",
]

#: Words that stand for themselves, with no parameters to read.
SIMPLE = {
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "no_motion",
}

#: Same expression the gate uses, so a word this validator passes is a word the
#: gate accepts.
SIZE_RE = re.compile(r"^size\(\s*(\d+)\s*,\s*(\d+)\s*\)$")

#: Pairs that cannot both be true. The first one seen wins; the second is
#: rejected with its reason recorded.
EXCLUSIVE = [("motion(idle)", "no_motion")]

#: One of these must be present: the spec's liveness check is the one that
#: matters, so a plan that says nothing about motion gets ``motion(idle)``.
LIVENESS = ("motion(idle)", "no_motion")
LIVENESS_DEFAULT = "motion(idle)"

DEFAULT_HOST = "http://127.0.0.1:11434"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "planner.md"

_PROMPT_VERSION_RE = re.compile(r"^prompt_version:\s*(\S+)\s*$", re.MULTILINE)
_HEADING_RE_CACHE: dict[str, re.Pattern[str]] = {}
#: A candidate assertion line: one token, no internal spaces, optionally with a
#: parenthesised argument. Every vocabulary word has this shape, and no sentence
#: of commentary does -- which is what stops trailing chatter being read as
#: assertions.
_CANDIDATE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(\([^()]*\))?$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")


class PlannerRefused(Exception):
    """The planner will not start: a missing stub, an unreadable prompt file."""


class PlannerFailed(Exception):
    """The planner ran and the response was unusable. ``raw`` is kept."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class Plan:
    """What one planning run produced, including what it threw away."""

    brief: str
    assertions: list[str]
    prompt_version: str
    tokens: dict[str, int]
    durations: dict[str, float]
    raw: str
    rejected: list[dict[str, str]] = field(default_factory=list)
    defaulted: list[str] = field(default_factory=list)


def utc_now() -> str:
    """Now, UTC, ISO 8601 with a Z. The only clock this module reads."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

def _read_prompt_file(path: str | Path | None = None) -> str:
    p = Path(path) if path is not None else PROMPT_PATH
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlannerRefused(f"cannot read the prompt file {p}: {exc}") from exc


def prompt_version(path: str | Path | None = None) -> str:
    """The ``prompt_version:`` line at the top of ``prompts/planner.md``.

    The version is read from the file rather than written in code so that the
    two cannot drift: an edited prompt with an unchanged version would make
    every entry's provenance a lie.
    """
    text = _read_prompt_file(path)
    match = _PROMPT_VERSION_RE.search(text)
    if match is None:
        raise PlannerRefused(f"no 'prompt_version:' line in {path or PROMPT_PATH}")
    return match.group(1)


def build_prompt(prompt: str, by: str, path: str | Path | None = None) -> str:
    """Fill the template. ``by`` is recorded, never addressed (the template says so)."""
    text = _read_prompt_file(path)
    text = _PROMPT_VERSION_RE.sub("", text, count=1).lstrip("\n")
    return text.replace("{prompt}", prompt.strip()).replace("{by}", by.strip() or "-")


# ---------------------------------------------------------------------------
# Parsing the response
# ---------------------------------------------------------------------------

def _heading_re(word: str) -> re.Pattern[str]:
    """A line that is just a heading: ``Brief``, ``## Brief``, ``**Brief:**``."""
    if word not in _HEADING_RE_CACHE:
        _HEADING_RE_CACHE[word] = re.compile(
            r"^\s*#{0,6}\s*\**\s*" + word + r"\s*\**\s*:?\s*\**\s*$",
            re.IGNORECASE,
        )
    return _HEADING_RE_CACHE[word]


def parse_response(raw: str) -> tuple[str, list[str]]:
    """Return ``(brief, words)`` from a model response.

    Tolerant of a preamble and of trailing commentary, because a small model
    adds both: the two headings are found anywhere in the text, the brief is
    everything between them, and the assertion block stops at the first line
    that is not a single bare token. Strict about one thing only -- a response
    with no ``Brief`` heading raises :class:`PlannerFailed`, because the brief
    is the artefact the executor and the judges both read and a guess at it
    would be silently wrong for the life of the entry.
    """
    lines = raw.splitlines()
    brief_at = assertions_at = -1
    brief_re, assertions_re = _heading_re("Brief"), _heading_re("Assertions")
    for i, line in enumerate(lines):
        if brief_at < 0 and brief_re.match(line):
            brief_at = i
        elif brief_at >= 0 and assertions_at < 0 and assertions_re.match(line):
            assertions_at = i
    if brief_at < 0:
        raise PlannerFailed("no 'Brief' heading in the response", raw)

    end = assertions_at if assertions_at >= 0 else len(lines)
    brief = " ".join(part.strip() for part in lines[brief_at + 1:end] if part.strip())

    words: list[str] = []
    if assertions_at >= 0:
        for line in lines[assertions_at + 1:]:
            stripped = _BULLET_RE.sub("", line).strip().strip("`").rstrip(".,;")
            if not stripped:
                continue
            candidate = re.sub(r"\s+", "", stripped)
            if not _CANDIDATE_RE.match(candidate):
                break  # trailing commentary; the assertion block is over
            words.append(candidate)
    return brief, words


# ---------------------------------------------------------------------------
# The validator -- the reason the vocabulary is closed
# ---------------------------------------------------------------------------

def validate_detailed(
    assertions: list[str],
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """``(ok, rejected, defaulted)``. :func:`validate` is the two-value form."""
    ok: list[str] = []
    rejected: list[dict[str, str]] = []
    defaulted: list[str] = []

    for raw in assertions:
        word = re.sub(r"\s+", "", str(raw).strip())
        if not word:
            continue
        if word not in SIMPLE:
            match = SIZE_RE.match(word)
            if match is None:
                rejected.append({
                    "word": str(raw).strip(),
                    "reason": "not in the vocabulary: " + ", ".join(VOCAB),
                })
                continue
            word = "size(%d,%d)" % (int(match.group(1)), int(match.group(2)))
        if word in ok:
            rejected.append({"word": word, "reason": "duplicate of an earlier line"})
            continue
        conflict = next(
            (
                other
                for a, b in EXCLUSIVE
                for this, other in ((a, b), (b, a))
                if word == this and other in ok
            ),
            None,
        )
        if conflict is not None:
            rejected.append({
                "word": word,
                "reason": f"mutually exclusive with {conflict}, which came first",
            })
            continue
        ok.append(word)

    if not any(w in ok for w in LIVENESS):
        ok.append(LIVENESS_DEFAULT)
        defaulted.append(LIVENESS_DEFAULT)
    return ok, rejected, defaulted


def validate(assertions: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """Keep only vocabulary words; return ``(ok, rejected)``.

    Drops duplicates, refuses the second half of a mutually exclusive pair, and
    adds ``motion(idle)`` when the model said nothing about motion at all. Use
    :func:`validate_detailed` when you need to record which words were added.
    """
    ok, rejected, _ = validate_detailed(assertions)
    return ok, rejected


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _post(host: str, payload: dict, timeout: float) -> dict:
    url = host.rstrip("/") + "/api/generate"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise PlannerFailed(f"the model host {url} did not answer usably: {exc}") from exc


def plan(
    prompt: str,
    model: str,
    host: str = DEFAULT_HOST,
    num_ctx: int = 8192,
    seed: int = 1,
    stub: str | Path | None = None,
    *,
    by: str = "",
    prompt_path: str | Path | None = None,
    timeout: float = 300.0,
) -> Plan:
    """Plan one job. With ``stub`` set, replay a saved response and call nothing.

    ``"think": false`` is sent only when the model tag starts with ``qwen``.
    Ollama rejects options a model does not declare, and ``gemma4:e4b`` -- the
    planner's model -- has no thinking mode to turn off; the qwen tags in this
    build do. Sending it unconditionally would fail the call for the one model
    the planner actually uses.
    """
    version = prompt_version(prompt_path)
    rendered = build_prompt(prompt, by, prompt_path)
    tokens: dict[str, int] = {}
    durations: dict[str, float] = {}

    if stub is not None:
        path = Path(stub)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PlannerRefused(f"cannot read the stub {path}: {exc}") from exc
    else:
        payload: dict = {
            "model": model,
            "prompt": rendered,
            "stream": False,
            "options": {"num_ctx": num_ctx, "seed": seed},
        }
        if model.startswith("qwen"):
            payload["think"] = False
        body = _post(host, payload, timeout)
        raw = body.get("response")
        if not isinstance(raw, str):
            raise PlannerFailed("the model host returned no 'response' field",
                                json.dumps(body, indent=2))
        tokens = {
            "prompt": int(body.get("prompt_eval_count") or 0),
            "response": int(body.get("eval_count") or 0),
        }
        durations = {
            name: round((body.get(key) or 0) / 1e9, 3)
            for name, key in (
                ("total_s", "total_duration"),
                ("load_s", "load_duration"),
                ("prompt_eval_s", "prompt_eval_duration"),
                ("eval_s", "eval_duration"),
            )
        }

    brief, words = parse_response(raw)
    ok, rejected, defaulted = validate_detailed(words)
    return Plan(
        brief=brief,
        assertions=ok,
        prompt_version=version,
        tokens=tokens,
        durations=durations,
        raw=raw,
        rejected=rejected,
        defaulted=defaulted,
    )
