"""Single-shot executor: one prompt in, one p5 sketch out.

Packet 2.2 of the sketchgen build. This is deliberately **not** an agent loop.
Spec section 8: a sketch is one file, so one prompt of roughly 2k tokens goes in,
one ``sketch.js`` of roughly 3k tokens comes out, and a repair turn sends the
evidence again rather than re-sending a transcript. Everything here is one call
to Ollama's ``/api/generate`` with ``stream`` false and thinking off, plus a
parser for the output contract stated in ``prompts/executor.md``.

What the model is asked for, and what this module keeps of it:

  - a fenced block tagged ``js``   -> ``sketch.js``   (required; its absence is
    a malformed response and the caller exits 1)
  - a fenced block tagged ``html`` -> ``index.html``  (optional; only when an
    addon library is needed, otherwise DEFAULT_INDEX_HTML is written instead)
  - a fenced block tagged ``ghost`` -> ``ghost.json`` (optional; the pointer
    script the kiosk and the gate play when nobody is at the keyboard, and the
    one block here whose contents are checked rather than stored verbatim —
    see :func:`validate_ghost`, and ``DECIDE[ghost-dataset]``)
  - a paragraph under a ``Statement`` heading -> ``statement.md``, stored
    verbatim, because spec section 7 publishes it unedited as the executor's own
    claim about its work.

The raw response is always written to ``response.txt`` before anything is parsed,
so a response that cannot be parsed is still evidence. ``result.json`` records
the model, the prompt version, the rules file, the seed, Ollama's own token
counts and nanosecond durations, the prefill and decode rates derived from them,
the wall time and the UTC start.

``--stub FILE`` replays a saved response through the same parser and writes the
same outputs, with the rates left null: nothing external is involved, so a stub
run is deterministic and is the control the build plan's section 0 rule requires
before any model is asked.

Python 3.12, stdlib only. Timestamps are UTC, ISO 8601, trailing Z.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from typing import Any

from . import ghostshim
from . import soundshim

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "DEFAULT_NUM_CTX",
    "DEFAULT_INDEX_HTML",
    "VOCAB",
    "ExecutorRefused",
    "Result",
    "normalise_assertions",
    "parse_response",
    "prompt_version",
    "render_prompt",
    "resolve_rules",
    "run",
    "validate_ghost",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = REPO_ROOT / "prompts"
RULES_DIR = PROMPTS_DIR / "rules"
TEMPLATE_PATH = PROMPTS_DIR / "executor.md"

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = os.environ.get(
    "SKETCHGEN_EXECUTOR_MODEL", "qwen3-coder:30b-a3b-q4_K_M"
)
DEFAULT_NUM_CTX = 8192
DEFAULT_SEED = 1

#: The page written when the model emits no ``html`` block: the gate's
#: fixtures/good-motion index, p5 pinned at 1.11.3, nothing else in it.
#: :func:`index_html_for` is what actually goes to disk, and it adds the two
#: shims — this is the page they are added to.
DEFAULT_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>sketch</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js"></script>
    <style>
        body { margin: 0; overflow: hidden; background: #111; }
        canvas { display: block; }
    </style>
</head>
<body>
    <script src="sketch.js"></script>
</body>
</html>
"""

#: The p5.sound API, character for character as gate/sketch_gate.py's SOUND_RE
#: (its line 133) and as the four names prompts/rules/treatment.md warns about.
#: Copied rather than imported because the gate is a standalone script with its
#: own copy on the node; if one list moves the other must, and a test says so.
SOUND_RE = re.compile(r"p5\.AudioIn|p5\.FFT|p5\.Oscillator|loadSound")

#: The p5 script line in DEFAULT_INDEX_HTML, and the addon that goes under it.
_P5_TAG = ('    <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/'
           '1.11.3/p5.min.js"></script>\n')
_P5_SOUND_TAG = ('    <script src="https://cdnjs.cloudflare.com/ajax/libs/p5.js/'
                 '1.11.3/addons/p5.sound.min.js"></script>\n')


def index_html_for(js: str) -> tuple[str, str]:
    """The index.html for a sketch whose model emitted no ``html`` block.

    p5.sound is a separate library. A sketch that calls ``p5.AudioIn`` without it
    loaded does not fail loudly: ``sound_lib_ok`` goes false, the microphone
    never opens, and ``responds(audio)`` reads as "the canvas did not react to
    the tone" — which is true, and is not the sketch's fault.

    prompts/rules/treatment.md warns about exactly this, in its loudest section,
    under the heading "The library trap". Of the 27 treatment-arm sketches that
    reached for p5.sound, 25 did not get it, and every run that ever satisfied
    responds(audio) came from a sketch that never touched the library. A prose
    warning has been tried and it does not work, so the addon is loaded for a
    sketch that plainly needs it.

    The model can still write its own ``html`` block and this never runs. Only
    the fallback got smarter about what the sketch it is wrapping actually asked
    for. A model-supplied page is written verbatim here and picks up both shims
    in the gallery's copy instead (``gallery._write_entry``).
    """
    if not SOUND_RE.search(js):
        # The ghost pointer goes on every page, sound or not: it is inert
        # without ?ghost= in the URL, and putting it here rather than only in
        # the gallery's copy is what makes the page the gate runs the page
        # that gets published (ghostshim.py, auto-mouse.md §3.1).
        return ghostshim.with_shim(DEFAULT_INDEX_HTML), "default"
    # And the shim that keeps the addon from holding the sketch at
    # "Loading..." inside a sandboxed frame on WebKit (soundshim.py).
    return ghostshim.with_shim(soundshim.with_shim(
        DEFAULT_INDEX_HTML.replace(_P5_TAG, _P5_TAG + _P5_SOUND_TAG)
    )), "default+p5.sound"


# ---------------------------------------------------------------------------
# The assertion vocabulary
# ---------------------------------------------------------------------------
# Mirrors sketch_gate.py's vocabulary exactly. The executor does not get to
# invent words any more than the planner does: a word the gate cannot evaluate
# would be a promise to the model that nothing checks.

SIMPLE_ASSERTIONS = {
    "motion(idle)",
    "no_motion",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "loads(image)",
}
SIZE_RE = re.compile(r"^size\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
VOCAB = [
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "size(w,h)",
    "no_motion",
    "loads(image)",
]

#: One line per word, written for the model: what the gate will actually do.
MEANINGS = {
    "motion(idle)": (
        "the canvas must change on its own, with no input, across the watch window"
    ),
    "no_motion": (
        "the canvas must NOT change on its own; the work is deliberately still"
    ),
    "responds(click)": (
        "the canvas must look different after a single click at its centre"
    ),
    "responds(drag)": (
        "the canvas must look different after a drag across it"
    ),
    "responds(audio)": (
        "the canvas must look different under a 440 Hz test tone played into a "
        "fake microphone than it does under silence"
    ),
    "uses(webgl)": (
        "the sketch must be drawing in the WEBGL renderer, not the 2D one"
    ),
    # The longest line here, and every clause of it is a failure that has
    # happened. Entry 429 fetched a picture eight times and drew nothing;
    # entry 1103 fetched one and missed on five attempts; p5 1.11.3 sets
    # crossOrigin on the image it builds, so a host without CORS headers does
    # not taint the canvas, it fails to load at all and the console says
    # nothing (2026-09-22, media-assertion.md section 3.2).
    "loads(image)": (
        "the gate watches the network: at least one image must arrive from a "
        "host outside the sketch and be drawn before the idle window ends. "
        "Load it in preload() from a host that serves CORS headers and a "
        "stable URL, such as https://picsum.photos/seed/<word>/800/600 or a "
        "file on upload.wikimedia.org; a data: URI is not the web and does not "
        "pass; a host that does not serve CORS fails to load at all"
    ),
    "size": (
        "p5's width and height must be exactly %d by %d pixels"
    ),
}


class ExecutorRefused(Exception):
    """Refusal, not failure: exit 3. Unknown assertion word, missing rules file."""


def normalise_assertions(words: list[str]) -> list[tuple[str, str, Any]]:
    """Return ``[(literal, kind, params)]`` or raise :class:`ExecutorRefused`."""
    out: list[tuple[str, str, Any]] = []
    for raw in words:
        word = raw.strip()
        if word in SIMPLE_ASSERTIONS:
            out.append((word, word, None))
            continue
        match = SIZE_RE.match(word)
        if match:
            out.append((word, "size", (int(match.group(1)), int(match.group(2)))))
            continue
        raise ExecutorRefused(
            "unknown assertion %r; vocabulary: %s" % (raw, ", ".join(VOCAB))
        )
    return out


def resolve_rules(rules_file: str) -> Path:
    """``treatment`` / ``control`` name the vendored copies; anything else is a path."""
    if rules_file in ("treatment", "control"):
        path = RULES_DIR / f"{rules_file}.md"
    else:
        path = Path(rules_file).expanduser()
    if not path.is_file():
        raise ExecutorRefused(f"no rules file at {path}")
    return path


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"^prompt_version:\s*(\S+)\s*$")


def _read_template() -> tuple[str, str]:
    """Return ``(prompt_version, body)`` from ``prompts/executor.md``."""
    if not TEMPLATE_PATH.is_file():
        raise ExecutorRefused(f"no prompt template at {TEMPLATE_PATH}")
    lines = TEMPLATE_PATH.read_text(encoding="utf-8").splitlines()
    match = _VERSION_RE.match(lines[0]) if lines else None
    if match is None:
        raise ExecutorRefused(
            f"{TEMPLATE_PATH} does not begin with a prompt_version: line"
        )
    body = lines[1:]
    if body and body[0].strip() == "---":
        body = body[1:]
    return match.group(1), "\n".join(body).lstrip("\n")


def prompt_version() -> str:
    return _read_template()[0]


def render_prompt(
    brief: str,
    assertions: list[tuple[str, str, Any]],
    rules_text: str,
    seed: int,
) -> str:
    """Fill the template. Deterministic: same inputs, same bytes."""
    _, body = _read_template()
    lines = []
    for literal, kind, params in assertions:
        if kind == "size":
            meaning = MEANINGS["size"] % params
        else:
            meaning = MEANINGS[kind]
        lines.append(f"- `{literal}` — {meaning}")
    rendered = Template(body).substitute(
        rules=rules_text.strip(),
        brief=brief.strip(),
        assertions="\n".join(lines) if lines else "- (no assertions were requested)",
        seed=str(seed),
    )
    if not rendered.endswith("\n"):
        rendered += "\n"
    return rendered


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------

_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(```+|~~~+)[ \t]*([A-Za-z0-9_.+-]*)[ \t]*$")
_HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(.*?)[ \t]*$")
_STATEMENT_RE = re.compile(r"^statement\b[ \t]*:?[ \t]*$", re.IGNORECASE)
_TAG_ALIASES = {
    "js": "js",
    "javascript": "js",
    "jsx": "js",
    "html": "html",
    "ghost": "ghost",
}


@dataclass
class Parsed:
    """What the contract in ``prompts/executor.md`` asked for, as found."""

    js: str | None = None
    html: str | None = None
    #: The optional pointer script (``DECIDE[ghost-dataset]``), as text. Kept
    #: unvalidated: this dataclass is what the reply *said*, and whether it
    #: parses is :func:`validate_ghost`'s question.
    ghost: str | None = None
    statement: str | None = None
    blocks: list[str] = field(default_factory=list)
    js_block_count: int = 0
    html_block_count: int = 0
    ghost_block_count: int = 0


def parse_response(text: str) -> Parsed:
    """Pull the js block, the optional html and ghost blocks and the Statement.

    Order does not matter and chatter around the blocks is ignored: the model is
    asked for a fixed order but is not trusted to keep it. Only the first block
    of each tag is kept; extra ones are counted so ``result.json`` can say so.
    A ``Statement`` heading inside a fenced block is not a statement.

    The statement is the first paragraph under the heading — it ends at the
    first blank line, heading or fence after it. The contract asks for one
    paragraph, and a model that goes on to explain itself afterwards ("and here
    is the code…") must not have that explanation published as its statement.
    Whatever is dropped is still in ``response.txt``.
    """
    parsed = Parsed()
    lines = text.splitlines()
    statement_lines: list[str] | None = None
    fence: str | None = None
    tag: str | None = None
    buffer: list[str] = []

    for line in lines:
        open_match = _FENCE_OPEN_RE.match(line)
        if fence is None:
            if open_match:
                if statement_lines is not None:
                    statement_lines = _close_statement(parsed, statement_lines)
                marker, raw_tag = open_match.groups()
                fence = marker[0] * 3
                tag = _TAG_ALIASES.get(raw_tag.strip().lower())
                buffer = []
                continue
            heading = _HEADING_RE.match(line) if line.lstrip().startswith("#") else None
            if heading and _STATEMENT_RE.match(heading.group(1)):
                statement_lines = []
                continue
            if statement_lines is not None:
                if heading:
                    statement_lines = _close_statement(parsed, statement_lines)
                elif not line.strip():
                    # A blank line ends the paragraph. Blank lines before the
                    # paragraph begins are just the space under the heading.
                    if any(l.strip() for l in statement_lines):
                        statement_lines = _close_statement(parsed, statement_lines)
                else:
                    statement_lines.append(line)
            continue

        # Inside a fence. A bare marker line of the same kind closes it.
        if open_match and not open_match.group(2) and open_match.group(1)[0] == fence[0]:
            body = "\n".join(buffer)
            if tag == "js":
                parsed.js_block_count += 1
                if parsed.js is None:
                    parsed.js = body
            elif tag == "html":
                parsed.html_block_count += 1
                if parsed.html is None:
                    parsed.html = body
            elif tag == "ghost":
                parsed.ghost_block_count += 1
                if parsed.ghost is None:
                    parsed.ghost = body
            fence = None
            tag = None
            buffer = []
            continue
        buffer.append(line)

    if statement_lines is not None:
        _close_statement(parsed, statement_lines)

    if parsed.js is not None:
        parsed.blocks.append("js")
    if parsed.html is not None:
        parsed.blocks.append("html")
    if parsed.ghost is not None:
        parsed.blocks.append("ghost")
    if parsed.statement is not None:
        parsed.blocks.append("statement")
    return parsed


def _close_statement(parsed: Parsed, collected: list[str]) -> None:
    """Store the first Statement paragraph verbatim, trimming blank edges only."""
    body = "\n".join(collected).strip("\n")
    while body.endswith((" ", "\t")):
        body = body[:-1]
    if body and parsed.statement is None:
        parsed.statement = body
    return None


#: The four keys an event carries, and nothing else. Exactly, not at least: a
#: fifth key is a model describing something this system does not play —
#: a duration, a button, a key — and accepting it silently would publish a
#: script that does less than its author thinks it does.
GHOST_KEYS = ("t", "type", "x", "y")


def _number(value: Any) -> float | None:
    """*value* as a float, or None if it is not a number.

    ``True`` is an ``int`` in Python and would otherwise be an x of 1.0 at the
    right-hand edge of the canvas. It is not a coordinate; it is a typo.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate_ghost(text: str) -> tuple[list[dict] | None, str | None]:
    """The ghost block as events the players can read, or why it is not.

    ``(events, None)`` or ``(None, reason)``, where *reason* is one line
    written for the model that wrote the block: it goes back in the next
    attempt's evidence (``worker.evidence_with_ghost``), and "invalid" would
    tell it nothing it could act on.

    The shape is ``DECIDE[ghost-script]`` in docs/plans/auto-mouse.md §1 and
    the caps are :mod:`sketchgen.ghostshim`'s own, read from there rather than
    restated: the shim holds every script to them again in the browser, and
    two numbers that could drift apart would mean a script this accepted and
    the page silently truncated.

    Events come back sorted by ``t`` — stably, so two events at the same
    millisecond stay in the order they were written; the shim schedules one
    ``setTimeout`` per event and a list that ran backwards would play
    backwards with nothing to say so. Positions in a reason are 1-based: the
    author is reading their own list, not indexing it.
    """
    try:
        events = json.loads(text)
    except ValueError:
        return None, "not a JSON list"
    if not isinstance(events, list):
        return None, "not a JSON list"
    if not events:
        return None, "an empty list, so there is nothing to play"
    if len(events) > ghostshim.MAX_EVENTS:
        return None, "%d events, more than %d" % (len(events), ghostshim.MAX_EVENTS)

    out: list[dict] = []
    for at, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return None, "event %d: not an object" % at
        if tuple(sorted(event)) != tuple(sorted(GHOST_KEYS)):
            return None, "event %d: its keys are %s, not %s" % (
                at, ", ".join(sorted(str(key) for key in event)) or "(none)",
                "/".join(GHOST_KEYS),
            )
        when = _number(event["t"])
        if when is None:
            return None, "event %d: t is %s, not a number" % (at, json.dumps(event["t"]))
        if when < 0 or when > ghostshim.MAX_MS:
            return None, "event %d: t is %s, outside [0, %d] ms" % (
                at, json.dumps(event["t"]), ghostshim.MAX_MS)
        if event["type"] not in ghostshim.TYPES:
            return None, "event %d: type is %s, not one of %s" % (
                at, json.dumps(event["type"]), "/".join(ghostshim.TYPES))
        for axis in ("x", "y"):
            where = _number(event[axis])
            if where is None:
                return None, "event %d: %s is %s, not a number" % (
                    at, axis, json.dumps(event[axis]))
            if where < 0 or where > 1:
                return None, "event %d: %s is %s, outside [0, 1]" % (
                    at, axis, json.dumps(event[axis]))
        out.append({key: event[key] for key in GHOST_KEYS})

    out.sort(key=lambda event: _number(event["t"]))
    return out, None


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@dataclass
class Result:
    """Everything one execute attempt produced, and what ``result.json`` holds."""

    ok: bool
    error: str | None
    started_utc: str
    wall_s: float
    model: str
    host: str
    prompt_version: str
    rules_file: str
    seed: int
    num_ctx: int
    brief: str
    assertions: list[str]
    blocks: list[str]
    index_source: str | None
    sketch_js_lines: int | None
    #: What became of a ``ghost`` block: ``{"events": N}`` when one was
    #: accepted and written, ``{"rejected": "<reason>"}`` when one was
    #: dropped, None when the reply carried none. Never a reason to fail the
    #: attempt — see :func:`run`.
    ghost: dict[str, Any] | None
    extra_blocks: dict[str, int]
    tokens: dict[str, int | None]
    durations_ns: dict[str, int | None]
    durations_s: dict[str, float | None]
    rates: dict[str, float | None]
    out_dir: str
    stub: str | None
    files: list[str]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ns_to_s(value: int | None) -> float | None:
    if value is None:
        return None
    return round(value / 1e9, 3)


def _rate(count: int | None, duration_ns: int | None) -> float | None:
    if not count or not duration_ns:
        return None
    return round(count / (duration_ns / 1e9), 2)


def _call_ollama(
    host: str, model: str, prompt: str, seed: int, num_ctx: int, timeout: float
) -> dict[str, Any]:
    """POST /api/generate, stream off, thinking off. Raises OSError on failure."""
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"num_ctx": num_ctx, "seed": seed},
    }
    request = urllib.request.Request(
        host.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run(
    brief: str,
    assertions: list[str],
    rules_file: str,
    seed: int = DEFAULT_SEED,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    num_ctx: int = DEFAULT_NUM_CTX,
    out_dir: str | Path = ".",
    stub: str | Path | None = None,
    timeout: float = 1800.0,
) -> Result:
    """Run one single-shot execute attempt and write its artefacts into *out_dir*.

    Raises :class:`ExecutorRefused` (exit 3) for an unknown assertion word, a
    missing rules file, a missing template or an unreadable stub. Every other
    failure — the model unreachable, a response with no js block — comes back as
    a ``Result`` with ``ok`` false (exit 1), never as a traceback.
    """
    normalised = normalise_assertions(assertions)
    rules_path = resolve_rules(rules_file)
    rules_text = rules_path.read_text(encoding="utf-8")
    version, _ = _read_template()
    prompt = render_prompt(brief, normalised, rules_text, seed)

    out = Path(out_dir).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    started = _utc_now()
    started_monotonic = time.monotonic()
    error: str | None = None
    raw = ""
    response: dict[str, Any] = {}

    if stub is not None:
        stub_path = Path(stub).expanduser()
        if not stub_path.is_file():
            raise ExecutorRefused(f"no stub response at {stub_path}")
        raw = stub_path.read_text(encoding="utf-8")
    else:
        try:
            response = _call_ollama(host, model, prompt, seed, num_ctx, timeout)
            raw = response.get("response") or ""
        except (OSError, urllib.error.URLError, ValueError) as exc:
            error = f"model call failed: {exc}"

    wall_s = round(time.monotonic() - started_monotonic, 3)

    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    (out / "response.txt").write_text(raw, encoding="utf-8")
    files = ["prompt.txt", "response.txt"]

    parsed = parse_response(raw) if raw else Parsed()
    index_source: str | None = None
    sketch_lines: int | None = None
    ghost: dict[str, Any] | None = None

    if error is None and (parsed.js is None or not parsed.js.strip()):
        error = "malformed response: no fenced js block"

    if error is None:
        js = parsed.js if parsed.js.endswith("\n") else parsed.js + "\n"
        (out / "sketch.js").write_text(js, encoding="utf-8")
        sketch_lines = len(js.splitlines())
        files.append("sketch.js")
        if parsed.html is not None:
            html = parsed.html if parsed.html.endswith("\n") else parsed.html + "\n"
            index_source = "model"
        else:
            html, index_source = index_html_for(js)
        (out / "index.html").write_text(html, encoding="utf-8")
        files.append("index.html")
        if parsed.statement is not None:
            (out / "statement.md").write_text(parsed.statement + "\n", encoding="utf-8")
            files.append("statement.md")
        if parsed.ghost is not None:
            # An invalid ghost block is NOT a rejected reply. The sketch is
            # the answer; the pointer script is an extra the entry does not
            # need, and there is a built-in waiting for it either way
            # (ghostshim.BUILTINS). Spending one of three attempts on a
            # malformed optional block would cost the sketch to save nothing,
            # so the block is dropped, the reason is recorded, and the next
            # attempt — if the gate asks for one — is told in a sentence.
            events, why = validate_ghost(parsed.ghost)
            if events is None:
                ghost = {"rejected": why}
            else:
                (out / "ghost.json").write_text(
                    json.dumps(events, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                files.append("ghost.json")
                ghost = {"events": len(events)}

    tokens = {
        "prompt_eval_count": response.get("prompt_eval_count"),
        "eval_count": response.get("eval_count"),
    }
    durations_ns = {
        key: response.get(key)
        for key in (
            "load_duration",
            "prompt_eval_duration",
            "eval_duration",
            "total_duration",
        )
    }
    result = Result(
        ok=error is None,
        error=error,
        started_utc=started,
        wall_s=wall_s,
        model=model,
        host=host if stub is None else "stub",
        prompt_version=version,
        rules_file=str(rules_path),
        seed=seed,
        num_ctx=num_ctx,
        brief=brief,
        assertions=[literal for literal, _, _ in normalised],
        blocks=parsed.blocks,
        index_source=index_source,
        sketch_js_lines=sketch_lines,
        ghost=ghost,
        extra_blocks={
            "js": max(0, parsed.js_block_count - 1),
            "html": max(0, parsed.html_block_count - 1),
            "ghost": max(0, parsed.ghost_block_count - 1),
        },
        tokens=tokens,
        durations_ns=durations_ns,
        durations_s={key: _ns_to_s(value) for key, value in durations_ns.items()},
        rates={
            "prefill_tok_s": _rate(
                tokens["prompt_eval_count"], durations_ns["prompt_eval_duration"]
            ),
            "decode_tok_s": _rate(tokens["eval_count"], durations_ns["eval_duration"]),
        },
        out_dir=str(out),
        stub=str(stub) if stub is not None else None,
        files=sorted(files + ["result.json"]),
    )
    (out / "result.json").write_text(result.to_json(), encoding="utf-8")
    return result
