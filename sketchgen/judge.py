"""The agent judges: the local one that looks, and the paid one that claims.

Packet 5.2. Spec §5 is the reason this file exists, and it is worth restating
before the code, because most of the code is enforcement of it:

  Humans and agents get the identical artefacts and the identical two
  questions. **Blind the agent**: it never sees a human vote, a Bradley-Terry
  score, an engagement tally, the producing model, the submitter, or the rules
  file. Models prefer their own output, so the producing model is *recorded as
  a variable* (``entries.executor``, beside every verdict's ``judge_id``) and
  self-preference is expected rather than discovered. A verdict without
  provenance is unusable a month later, so every row carries the model id, the
  prompt version, and the hash of the artefacts the judge actually saw.

Two judges, because "agents" is not one opinion:

* **the local judge** — ``gemma4:e4b``, over Ollama's ``/api/chat`` with the
  two frame strips as ``images``. It is the local judge because it is the model
  on the box that can *see*; ``qwen3-coder`` has no vision, and a judge that
  reads source instead of looking at the sketch is answering a different
  question from the humans. MEASURE[e4b-as-critic].
* **the paid judge** — branch B of DECIDE[credential-model] (spec §6). No paid
  credential ever reaches the node. :func:`export_claims` writes the pairs that
  judge still owes as a JSON packet; the laptop session reads the strips over
  the tunnel, answers the same two questions, fills the packet in, and
  :func:`import_verdicts` records the answers here. The node never holds a key
  and never makes the call.

What this module deliberately cannot reach: ``/counts`` on the write path
(``writepath/worker.js`` says so at the top), the ``likes`` and ``engagement``
tables, and :func:`sketchgen.pairs.scores`. Engagement is not judgment.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import base64
import json
import random
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import db
from . import pairs
from . import worker

__all__ = [
    "BLIND_FIELDS",
    "DEFAULT_HOST",
    "DEFAULT_MODEL",
    "PROMPT_PATH",
    "QUESTIONS",
    "BlindError",
    "JudgeFailed",
    "JudgeRefused",
    "Request",
    "Verdict",
    "assert_blind",
    "build_prompt",
    "chat_payload",
    "claims_packet",
    "export_claims",
    "import_verdicts",
    "import_verdicts_detailed",
    "judge_local",
    "parse_reply",
    "prompt_version",
    "record_verdict",
    "run_local",
]

#: The model that can see. Spec §5: the reviewing agent needs to look at the
#: sketch, not read about it.
DEFAULT_MODEL = "gemma4:e4b"
DEFAULT_HOST = "http://127.0.0.1:11434"

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "judge.md"

#: The same two questions the compare page asks, in the same order.
QUESTIONS = pairs.QUESTIONS
CHOICES = pairs.CHOICES

#: Everything that would break the blind if it reached the prompt. Each name is
#: a column that carries a human's opinion, an engagement tally, or the identity
#: of whoever (or whatever) made the sketch.
BLIND_FIELDS = (
    "score",
    "likes",
    "views",
    "executor",
    "submitted_by",
    "rules_file",
    "statement",
    "planner",
    "username",
)

#: A blind field is matched at the start of a word, with any suffix: 'score'
#: catches 'scores' and 'score:', and 'views' does not catch 'reviews'. The
#: strict end of that trade is deliberate — a false positive costs one edit to a
#: brief, a false negative costs the whole comparison.
_BLIND_RES = tuple(
    (name, re.compile(r"(?<![A-Za-z])" + re.escape(name), re.IGNORECASE))
    for name in BLIND_FIELDS
)

#: Any run of three or more digits, checked against the entry ids in the
#: database. Two-digit runs are not checked: a brief that says "40 circles" is
#: not leaking entry 40, and pretending otherwise would make the check useless.
_LONG_DIGITS_RE = re.compile(r"\d{3,}")

_PROMPT_VERSION_RE = re.compile(r"^prompt_version:\s*(\S+)\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

#: ``brief: A``, ``**look:** tie``, ``- brief — B``. Tolerant of the decoration
#: a small model adds, strict about the three words it is allowed to end with.
_ANSWER_RE = re.compile(
    r"^[\s>*_`-]*(brief|look)\s*[*_`]*\s*[:\-–—]\s*[*_`\s]*(A|B|tie)\b",
    re.IGNORECASE,
)
_REASONS_RE = re.compile(r"^\s*#{0,6}\s*\**\s*reasons\s*\**\s*:?\s*\**\s*$", re.IGNORECASE)
_REASON_LINE_RE = re.compile(r"^[\s>*_`-]*(brief|look)\s*[*_`]*\s*[:\-]\s*(.+)$", re.IGNORECASE)

#: A model id, for a judge id. Same shape sketchgen/cli/pairs.py checks.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")

#: What a claims packet says it is, so an unrelated JSON file is refused rather
#: than half-read.
PACKET_KIND = "sketchgen-judge-claims"


class JudgeRefused(Exception):
    """The judge will not start: no prompt file, no strip, a bad judge id."""


class JudgeFailed(Exception):
    """The judge ran and the reply was unusable. ``raw`` is kept, always."""

    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class BlindError(JudgeRefused):
    """The rendered prompt carries something the judge must not see.

    A subclass of :class:`JudgeRefused` because it is a refusal, not a failure:
    nothing was sent, nothing was written, and the fix is to the template or to
    the brief, never to the judge.
    """


# ---------------------------------------------------------------------------
# What one judging run is handed
# ---------------------------------------------------------------------------


@dataclass
class Request:
    """One judgement request: two briefs, two strips, and the rendered prompt.

    ``strip_a`` and ``strip_b`` are the PNG bytes in the order the judge sees
    them; ``images`` is the same two, base64, A first, which is exactly what
    goes into Ollama's ``images`` field.
    """

    entry_a: int
    entry_b: int
    brief_a: str
    brief_b: str
    strip_a: bytes
    strip_b: bytes
    strip_path_a: str
    strip_path_b: str
    text: str
    prompt_version: str
    artefact_hash: str

    @property
    def images(self) -> list[str]:
        """Both strips, base64, **A first**. Order is the whole of the labelling."""
        return [
            base64.b64encode(self.strip_a).decode("ascii"),
            base64.b64encode(self.strip_b).decode("ascii"),
        ]


@dataclass
class Verdict:
    """One judge's answers to both questions about one pair.

    ``payload`` is what was sent to the model, or what would have been sent
    under a stub. It is kept so a test (and a reader of the record) can see the
    images that went with the words, in order.
    """

    brief: str
    look: str
    reasons: dict[str, str]
    model: str
    prompt_version: str
    artefact_hash: str
    tokens: dict[str, int]
    raw: str
    entry_a: int = 0
    entry_b: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def answers(self) -> dict[str, str]:
        return {"brief": self.brief, "look": self.look}


# ---------------------------------------------------------------------------
# The template
# ---------------------------------------------------------------------------


def _read_prompt_file(path: str | Path | None = None) -> str:
    p = Path(path) if path is not None else PROMPT_PATH
    try:
        return p.read_text(encoding="utf-8")
    except OSError as exc:
        raise JudgeRefused(f"cannot read the prompt file {p}: {exc}") from exc


def prompt_version(path: str | Path | None = None) -> str:
    """The ``prompt_version:`` line at the top of ``prompts/judge.md``.

    Read from the file, never written in code, for the reason planner.py gives:
    an edited prompt under an unchanged version makes every verdict's
    provenance a lie, and provenance is the only thing that makes an old
    verdict readable.
    """
    text = _read_prompt_file(path)
    match = _PROMPT_VERSION_RE.search(text)
    if match is None:
        raise JudgeRefused(f"no 'prompt_version:' line in {path or PROMPT_PATH}")
    return match.group(1)


def _render(brief_a: str, brief_b: str, path: str | Path | None = None) -> str:
    """Fill the template, minus the version line and the rule comment.

    The comment block at the top of the template names the fields that must not
    appear, which means the comment itself contains them. It is stripped here,
    so the model reads the result of the rule and never the rule.
    """
    text = _read_prompt_file(path)
    text = _PROMPT_VERSION_RE.sub("", text, count=1)
    text = _COMMENT_RE.sub("", text)
    text = text.strip() + "\n"
    return (
        text.replace("{brief_a}", (brief_a or "").strip() or "(no brief on record)")
        .replace("{brief_b}", (brief_b or "").strip() or "(no brief on record)")
    )


# ---------------------------------------------------------------------------
# The blind
# ---------------------------------------------------------------------------


def _entry_ids(conn: sqlite3.Connection | None) -> set[str]:
    if conn is None:
        return set()
    try:
        rows = conn.execute("SELECT id FROM entries")
    except sqlite3.Error:
        return set()
    return {str(int(row["id"])) for row in rows}


def _submitters(conn: sqlite3.Connection | None) -> set[str]:
    """Every username the queue has seen, from jobs and from entries.

    Both tables, because a job can be deleted and its entry kept, and because
    the point is the name, not where it is stored.
    """
    if conn is None:
        return set()
    names: set[str] = set()
    for sql in (
        "SELECT DISTINCT submitted_by FROM jobs",
        "SELECT DISTINCT submitted_by FROM entries",
    ):
        try:
            rows = conn.execute(sql)
        except sqlite3.Error:
            continue
        for row in rows:
            value = (row[0] or "").strip()
            if value:
                names.add(value)
    return names


def assert_blind(text: str, *, conn: sqlite3.Connection | None = None) -> None:
    """Raise :class:`BlindError` if anything the judge must not see is in ``text``.

    Three checks, in the order they are cheap:

    1. every name in :data:`BLIND_FIELDS`, matched at the start of a word;
    2. any run of three or more digits that is an entry id in this database —
       the pair is "A" and "B" to the judge and nothing else, and an id in the
       prompt is an id the judge could look up against anything else it has
       been told;
    3. any username in ``jobs.submitted_by`` or ``entries.submitted_by``.

    ``conn`` is optional only so the template itself can be checked without a
    database; every call on a real prompt passes one.

    This is called on every rendered prompt before it is sent, on every claims
    packet before it is written, and by the test suite on a deliberately
    poisoned brief. It is the whole of the enforcement of spec §5's first design
    consequence, so it fails loudly and writes nothing.
    """
    haystack = text or ""
    for name, expression in _BLIND_RES:
        match = expression.search(haystack)
        if match is not None:
            raise BlindError(
                f"the rendered prompt contains {name!r} at character "
                f"{match.start()}: the agent judge is blind to it (spec §5)"
            )
    ids = _entry_ids(conn)
    if ids:
        for match in _LONG_DIGITS_RE.finditer(haystack):
            if match.group(0) in ids:
                raise BlindError(
                    f"the rendered prompt contains {match.group(0)!r}, which is an "
                    "entry id: the judge sees 'A' and 'B' and no identifiers"
                )
    for name in _submitters(conn):
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])",
                     haystack, re.IGNORECASE):
            raise BlindError(
                "the rendered prompt names someone who submitted a job: the "
                "agent judge never sees the submitter (spec §5)"
            )


# ---------------------------------------------------------------------------
# Building the request
# ---------------------------------------------------------------------------


def _entry(conn: sqlite3.Connection, entry_id: int) -> Mapping[str, Any]:
    row = conn.execute(
        "SELECT * FROM entries WHERE id = ?", (int(entry_id),)
    ).fetchone()
    if row is None:
        raise JudgeRefused(f"there is no entry {entry_id}")
    return {key: row[key] for key in row.keys()}


def _strip_bytes(row: Mapping[str, Any], label: str) -> tuple[bytes, str]:
    """The frame strip, or a refusal. The judging model must SEE (spec §5)."""
    path = str(row.get("strip_path") or "").strip()
    if not path:
        raise JudgeRefused(
            f"entry shown as {label} has no strip.png on record; the judge has to "
            "look at the sketch, so there is nothing to ask it"
        )
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise JudgeRefused(f"cannot read the strip for {label} at {path}: {exc}") from exc
    if not data:
        raise JudgeRefused(f"the strip for {label} at {path} is empty")
    return data, path


def build_prompt(
    conn: sqlite3.Connection,
    entry_a: int,
    entry_b: int,
    *,
    prompt_path: str | Path | None = None,
) -> Request:
    """The judgement request for one pair, blind-checked before it is returned.

    ``entry_a`` is shown first and is "A"; the caller (:func:`run_local`, via
    :func:`sketchgen.pairs.pick_pair`) has already decided which way round that
    is, and the rng decided it, so position carries no signal.

    The ``artefact_hash`` is :func:`sketchgen.pairs.artefact_hash` over the same
    two entries in the same order: regenerate a strip and the hash changes, and
    every verdict recorded under the old one is visibly about a different
    artefact.
    """
    row_a = _entry(conn, entry_a)
    row_b = _entry(conn, entry_b)
    strip_a, path_a = _strip_bytes(row_a, "A")
    strip_b, path_b = _strip_bytes(row_b, "B")
    text = _render(
        str(row_a.get("brief") or ""), str(row_b.get("brief") or ""), prompt_path
    )
    assert_blind(text, conn=conn)
    return Request(
        entry_a=int(entry_a),
        entry_b=int(entry_b),
        brief_a=str(row_a.get("brief") or ""),
        brief_b=str(row_b.get("brief") or ""),
        strip_a=strip_a,
        strip_b=strip_b,
        strip_path_a=path_a,
        strip_path_b=path_b,
        text=text,
        prompt_version=prompt_version(prompt_path),
        artefact_hash=pairs.artefact_hash(row_a, row_b),
    )


# ---------------------------------------------------------------------------
# The call, and the reply
# ---------------------------------------------------------------------------


def chat_payload(
    request: Request,
    model: str,
    *,
    num_ctx: int = 8192,
    seed: int = 1,
) -> dict[str, Any]:
    """The ``/api/chat`` body: one user turn, two images, A first.

    ``"think": false`` goes only to the qwen tags. Ollama rejects an option a
    model does not declare, and ``gemma4:e4b`` — the model this judge is for —
    has no thinking mode to turn off; planner.py carries the same rule and the
    same comment, and the two must move together.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": request.text, "images": request.images}
        ],
        "stream": False,
        "options": {"num_ctx": num_ctx, "seed": seed},
    }
    if model.startswith("qwen"):
        payload["think"] = False
    return payload


def _post(host: str, payload: dict, timeout: float) -> dict:
    url = host.rstrip("/") + "/api/chat"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise JudgeFailed(f"the model host {url} did not answer usably: {exc}") from exc


def parse_reply(raw: str) -> tuple[str, str, dict[str, str]]:
    """``(brief, look, reasons)`` from a reply, or :class:`JudgeFailed`.

    The contract is two lines. This reads them anywhere in the text and ignores
    decoration around them, because a small model puts a heading above its
    answer about half the time. It does not guess: a reply missing either line,
    or ending a line in anything but A, B or tie, is a failure with the raw text
    kept, and the caller exits 1. A guessed verdict is worse than no verdict —
    it would go into a Bradley-Terry fit and never be distinguishable again.
    """
    answers: dict[str, str] = {}
    reasons: dict[str, str] = {}
    lines = (raw or "").splitlines()
    in_reasons = False
    pending = [q for q in QUESTIONS]
    for line in lines:
        if _REASONS_RE.match(line):
            in_reasons = True
            continue
        match = _ANSWER_RE.match(line)
        if match is not None and not in_reasons:
            question = match.group(1).lower()
            answers.setdefault(question, _normalise_choice(match.group(2)))
            continue
        if in_reasons:
            labelled = _REASON_LINE_RE.match(line)
            if labelled is not None:
                reasons.setdefault(
                    labelled.group(1).lower(), " ".join(labelled.group(2).split())
                )
                continue
            text = " ".join(line.split()).strip("*_`-> ")
            if text and pending:
                question = pending.pop(0)
                reasons.setdefault(question, text)
    missing = [q for q in QUESTIONS if q not in answers]
    if missing:
        raise JudgeFailed(
            "the reply has no " + " or ".join(f"'{q}:' line" for q in missing)
            + "; the contract is two lines, each ending in A, B or tie",
            raw or "",
        )
    return answers["brief"], answers["look"], reasons


def _normalise_choice(value: str) -> str:
    word = (value or "").strip()
    if word.lower() == "tie":
        return "tie"
    return word.upper()


def record_verdict(
    conn: sqlite3.Connection,
    verdict: Verdict,
    *,
    entry_a: int | None = None,
    entry_b: int | None = None,
    judge_id: str | None = None,
) -> list[int]:
    """Both answers, through :func:`sketchgen.pairs.record`, as one agent judge.

    ``judge_kind`` is 'agent' and ``judge_id`` is the model id — for the local
    judge the tag it was called with, for the paid judge the model id the laptop
    reported. Never a person: this column is a model id or a GitHub username and
    nothing else, everywhere in this schema.
    """
    left = int(entry_a if entry_a is not None else verdict.entry_a)
    right = int(entry_b if entry_b is not None else verdict.entry_b)
    who = str(judge_id or verdict.model).strip()
    if not MODEL_RE.match(who):
        raise JudgeRefused(f"{who!r} is not a usable model id for an agent judge")
    return [
        pairs.record(
            conn,
            entry_a=left,
            entry_b=right,
            judge_kind="agent",
            judge_id=who,
            question=question,
            choice=choice,
            prompt_version=verdict.prompt_version,
            artefact_hash=verdict.artefact_hash,
        )
        for question, choice in verdict.answers().items()
    ]


def judge_local(
    conn: sqlite3.Connection,
    entry_a: int,
    entry_b: int,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    stub: str | Path | None = None,
    num_ctx: int = 8192,
    seed: int = 1,
    timeout: float = 600.0,
    prompt_path: str | Path | None = None,
    record: bool = True,
) -> Verdict:
    """Ask the local model both questions about one pair, and record the answers.

    With ``stub`` set, the saved reply in that file is replayed and no call is
    made; the payload that *would* have gone out is still built and carried on
    the verdict, so a test can see that the images went A first.

    A malformed reply raises :class:`JudgeFailed` with the raw text attached —
    exit-1 semantics at the CLI, and the raw text saved beside the failure.
    """
    request = build_prompt(conn, entry_a, entry_b, prompt_path=prompt_path)
    payload = chat_payload(request, model, num_ctx=num_ctx, seed=seed)
    tokens: dict[str, int] = {}

    if stub is not None:
        path = Path(stub)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JudgeRefused(f"cannot read the stub {path}: {exc}") from exc
    else:
        body = _post(host, payload, timeout)
        message = body.get("message") or {}
        raw = message.get("content") if isinstance(message, dict) else None
        if not isinstance(raw, str):
            raise JudgeFailed(
                "the model host returned no 'message.content' field",
                json.dumps(body, indent=2),
            )
        tokens = {
            "prompt": int(body.get("prompt_eval_count") or 0),
            "response": int(body.get("eval_count") or 0),
        }

    brief, look, reasons = parse_reply(raw)
    verdict = Verdict(
        brief=brief,
        look=look,
        reasons=reasons,
        model=model,
        prompt_version=request.prompt_version,
        artefact_hash=request.artefact_hash,
        tokens=tokens,
        raw=raw,
        entry_a=request.entry_a,
        entry_b=request.entry_b,
        payload=payload,
    )
    if record:
        record_verdict(conn, verdict)
    return verdict


# ---------------------------------------------------------------------------
# run_local — the local judge, over as many pairs as it is given
# ---------------------------------------------------------------------------


def run_local(
    conn: sqlite3.Connection,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    limit: int = 1,
    rng: random.Random,
    stub: str | Path | None = None,
    probe: Callable[[], dict[str, Any]] | None = None,
    log: Callable[[str], None] | None = None,
    **call_kwargs: Any,
) -> dict[str, int]:
    """Judge up to ``limit`` pairs, fencing the inference slot first.

    The fence is ``sketchgen.worker.fence`` — the same one the worker uses, for
    the same reason (one inference slot; a second client makes both nine times
    slower and the numbers worthless). It runs **before the first model call**
    and raises :class:`JudgeRefused` when another client holds the slot, which
    the CLI turns into exit 3.

    A stubbed run calls no model, so it cannot contend for anything: it gets
    ``worker.observing_probe``, which looks, reports and never refuses — the
    precedent worker.py set and the comment there explains. An explicit
    ``probe`` always wins, which is how the test trips the fence without
    starting a process.

    Returns ``{"judged", "recorded", "pairs_offered"}``. ``None`` from
    ``pick_pair`` ends the run early and is not an error: fewer than two
    published entries, or this judge has already answered every pair.
    """
    say = log or (lambda _message: None)
    chosen = probe or (
        worker.default_probe if stub is None else worker.observing_probe(host)
    )
    result = worker.fence(chosen)
    if not result.ok:
        raise JudgeRefused(f"the inference slot is busy: {result.reason}")
    for name in result.observed:
        say(f"fence: NOT ENFORCED in stub mode — the slot is held by: {name}")
    if result.models:
        say(f"fence: ollama has {', '.join(result.models)} resident (not a refusal)")

    counts = {"judged": 0, "recorded": 0, "pairs_offered": 0}
    for _ in range(max(0, int(limit))):
        pair = pairs.pick_pair(
            conn, judge_id=model, judge_kind="agent", exclude_seen=True, rng=rng
        )
        if pair is None:
            break
        counts["pairs_offered"] += 1
        entry_a, entry_b = pair
        verdict = judge_local(
            conn, entry_a, entry_b, model=model, host=host, stub=stub, **call_kwargs
        )
        counts["judged"] += 1
        counts["recorded"] += len(QUESTIONS)
        say(
            f"judged {entry_a} vs {entry_b}: brief={verdict.brief} "
            f"look={verdict.look}"
        )
    return counts


# ---------------------------------------------------------------------------
# The paid judge, branch B: claims out, verdicts in
# ---------------------------------------------------------------------------


def export_claims(
    conn: sqlite3.Connection,
    *,
    judge_id: str,
    limit: int = 20,
    prompt_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """The pairs ``judge_id`` still owes a verdict on, as claim dictionaries.

    Branch B of DECIDE[credential-model], spec §6: the paid model never runs on
    the node, so the node cannot ask it anything. It writes down what it would
    have asked. Each claim carries the two briefs, the two **strip paths** (the
    laptop reads the images itself, over scp or the tunnel — the bytes are not
    copied into the packet, which keeps a packet small and keeps one copy of the
    artefact), the ``artefact_hash`` of what those paths held when the claim was
    written, the ``prompt_version``, and the rendered prompt, blind-checked like
    any other.

    ``answers`` is present and empty: the laptop fills in 'A', 'B' or 'tie' for
    each question and hands the file back to :func:`import_verdicts`.
    """
    judge_id = str(judge_id).strip()
    if not MODEL_RE.match(judge_id):
        raise JudgeRefused(f"{judge_id!r} is not a usable model id for an agent judge")
    wanted = pairs.candidate_pairs(
        conn, judge_id=judge_id, judge_kind="agent", exclude_seen=True
    )
    claims: list[dict[str, Any]] = []
    for entry_a, entry_b in wanted[: max(0, int(limit))]:
        request = build_prompt(conn, entry_a, entry_b, prompt_path=prompt_path)
        claims.append(
            {
                "entry_a": request.entry_a,
                "entry_b": request.entry_b,
                "brief_a": request.brief_a,
                "brief_b": request.brief_b,
                "strip_a": request.strip_path_a,
                "strip_b": request.strip_path_b,
                "artefact_hash": request.artefact_hash,
                "prompt_version": request.prompt_version,
                "prompt": request.text,
                "answers": {question: "" for question in QUESTIONS},
                "reasons": {question: "" for question in QUESTIONS},
            }
        )
    return claims


def claims_packet(
    conn: sqlite3.Connection,
    *,
    judge_id: str,
    limit: int = 20,
    prompt_path: str | Path | None = None,
) -> dict[str, Any]:
    """A claims packet: the envelope :func:`export_claims`'s list travels in.

    The envelope names the judge the claims were cut for and the prompt version
    they were cut under, so a packet that comes back a week later is still
    readable. The laptop may overwrite ``judge_id`` with the model id it
    actually used — that is the id the verdicts are recorded under, because a
    verdict's provenance is the model that gave it and not the model somebody
    hoped for.
    """
    claims = export_claims(
        conn, judge_id=judge_id, limit=limit, prompt_path=prompt_path
    )
    return {
        "packet": PACKET_KIND,
        "judge_id": str(judge_id).strip(),
        "judge_kind": "agent",
        "prompt_version": prompt_version(prompt_path),
        "questions": {
            "brief": "Which is closer to its brief?",
            "look": "Which would you rather look at?",
        },
        "created_utc": db.utc_now(),
        "claims": claims,
    }


def _packet_claims(packet: Any) -> tuple[list[dict[str, Any]], str]:
    """``(claims, judge_id)`` from an envelope, a bare list, or a refusal."""
    if isinstance(packet, list):
        return [c for c in packet if isinstance(c, dict)], ""
    if not isinstance(packet, dict):
        raise JudgeRefused("a claims packet is a JSON object or a list of claims")
    kind = str(packet.get("packet") or "").strip()
    if kind and kind != PACKET_KIND:
        raise JudgeRefused(f"{kind!r} is not a {PACKET_KIND} packet")
    claims = packet.get("claims")
    if not isinstance(claims, list):
        raise JudgeRefused("the packet has no 'claims' list")
    return [c for c in claims if isinstance(c, dict)], str(packet.get("judge_id") or "")


def import_verdicts_detailed(
    conn: sqlite3.Connection, packet: Any
) -> tuple[int, list[dict[str, str]]]:
    """``(recorded, rejected)``. :func:`import_verdicts` is the count alone."""
    claims, envelope_judge = _packet_claims(packet)
    recorded = 0
    rejected: list[dict[str, str]] = []

    def refuse(claim: Mapping[str, Any], why: str) -> None:
        rejected.append(
            {
                "pair": f"{claim.get('entry_a')} vs {claim.get('entry_b')}",
                "reason": why,
            }
        )

    for claim in claims:
        answers = claim.get("answers")
        if not isinstance(answers, dict):
            refuse(claim, "no 'answers' object")
            continue
        given = {
            question: str(answers.get(question) or "").strip()
            for question in QUESTIONS
        }
        if not any(given.values()):
            continue  # unanswered: the laptop did not get to this one. Not an error.
        bad = [q for q, choice in given.items() if choice not in CHOICES]
        if bad:
            refuse(claim, "not A, B or tie: " + ", ".join(sorted(bad)))
            continue
        try:
            entry_a = int(claim["entry_a"])
            entry_b = int(claim["entry_b"])
        except (KeyError, TypeError, ValueError):
            refuse(claim, "the claim names no pair of entries")
            continue
        who = str(claim.get("judge_id") or envelope_judge or "").strip()
        if not MODEL_RE.match(who):
            refuse(claim, f"{who!r} is not a usable model id for an agent judge")
            continue
        try:
            current = pairs.artefact_hash(entry_a, entry_b, conn=conn)
        except ValueError as exc:
            refuse(claim, str(exc))
            continue
        claimed = str(claim.get("artefact_hash") or "")
        if claimed != current:
            # Spec §5: a verdict is only interpretable against what the judge
            # saw. The strip or the brief moved under this claim, so the answer
            # is about an artefact that no longer exists.
            refuse(claim, "artefact_hash no longer matches; the artefacts changed")
            continue
        version = str(claim.get("prompt_version") or "") or None
        for question, choice in given.items():
            pairs.record(
                conn,
                entry_a=entry_a,
                entry_b=entry_b,
                judge_kind="agent",
                judge_id=who,
                question=question,
                choice=choice,
                prompt_version=version,
                artefact_hash=current,
            )
        recorded += 1
    return recorded, rejected


def import_verdicts(conn: sqlite3.Connection, packet: Any) -> int:
    """Record the answered claims in ``packet``; return how many pairs landed.

    A claim whose ``artefact_hash`` no longer matches the entries as they stand
    is rejected and nothing is written for it. An unanswered claim is skipped
    in silence — the laptop is allowed to hand back a packet it only got
    halfway through. Use :func:`import_verdicts_detailed` when the caller wants
    to print the rejections, as the CLI does.
    """
    recorded, _rejected = import_verdicts_detailed(conn, packet)
    return recorded
