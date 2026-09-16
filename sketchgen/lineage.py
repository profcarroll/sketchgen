"""lineage.py — a critique of one entry becomes the prompt for the next.

This is what the spec means by a gallery that generates itself (§8.1). Two
things happen here and nothing else does:

**A critique becomes a child job.** :func:`spawn` takes a parent entry and one
sentence of critique and queues a job whose prompt is the parent's prompt with
that sentence under a fixed heading::

    <the parent's prompt>

    Revise: <the critique>

The child job carries ``parent_entry_id``, and it carries the critique itself in
``jobs.critique`` / ``jobs.critique_by`` (migration 005), because the `lineage`
table keys on the child ENTRY and that entry does not exist yet. When the worker
finally creates the entry it calls :func:`record_child`, which is the only place
a `lineage` row is written for a spawned job.

**A line runs three generations and then waits.** DECIDE[lineage-depth] is three
(plan §4). A child at generation ``max_depth`` or deeper is still created — the
prompt is not thrown away — but with ``publication='hold'`` whatever was asked
for and ``needs='review'`` on the job, so a person has to touch it before the
line goes any further. Spec §9 is the reason: a pipeline that publishes
unattended will eventually publish something unintended, and self-prompted lines
make that more urgent, not less.

Generations count from the root, which is generation 0. An entry with no
`lineage` row is a root; its first child is generation 1.

**The critique itself is a model call, and this module never makes one by
accident.** :func:`critique` calls Ollama's ``/api/generate`` only when it is
given a ``host`` and no ``stub``; ``--stub FILE`` replays a saved response and is
how the tests run. The validator is deliberately strict — one sentence, under
forty words, no code — because the text becomes the next brief's revision line
and a paragraph of review would be a paragraph of prompt.

**The critic looks at the sketch.** Until critic-v3 it saw only words: the
prompt, the brief, the gate's assertions, and the executor's own statement about
what it built. The statement is unverified self-description, and on 2026-09-14 it
was false twice in one line — entry 20 claimed interlocking planes of colour and
is a blue blob on black; its child, job 33, claimed a gradient behind a head and
neck and is a red rectangle. The critic revised the statement instead of the
sketch. So :func:`critique` now sends ``entries.strip_path`` — the gate's
four-frame strip — base64 in ``/api/generate``'s ``images`` field, the way
:func:`sketchgen.judge._strip_bytes` does for the local judge, and **refuses**
rather than critiquing blind when there is no strip to send. Every
:class:`Critique` carries the path and the sha256 of exactly the bytes that went
out, so a sentence is tied to the pixels that produced it.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from sketchgen import db

__all__ = [
    "CRITIQUE_BY_RE",
    "DEFAULT_HOST",
    "DEFAULT_MAX_DEPTH",
    "MAX_CRITIQUE_WORDS",
    "PROMPT_PATH",
    "REVISE_HEADING",
    "SPAWNABLE",
    "Critique",
    "CritiqueFailed",
    "CritiqueRefused",
    "compose_prompt",
    "critique",
    "critique_prompt",
    "generation_of",
    "line",
    "prompt_version",
    "record_child",
    "roots",
    "spawn",
    "split_prompt",
]

#: DECIDE[lineage-depth], plan §4: three generations unattended, then a person.
DEFAULT_MAX_DEPTH = 3

#: The states a parent may be spawned from. A held entry has not been through
#: the publication gate and a rejected one was refused at it; neither is a thing
#: the gallery should be building a line on top of (spec §9).
#: A line grows from a held entry too (instructor, 2026-09-14): "you need to
#: see if a revision will make it worth publishing" is a primary use of the
#: gate, not an edge case. Only a rejected entry is closed.
SPAWNABLE = frozenset({"held", "published", "failed-kept"})

#: The fixed heading. Fixed so that a child's prompt can always be split back
#: into the prompt it inherited and the critique that changed it.
REVISE_HEADING = "Revise:"

#: A model id (``gemma4:e4b``) or a GitHub username, and nothing that could be a
#: person: no ``@``, no spaces, no punctuation an email needs. Course policy.
CRITIQUE_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: GitHub usernames and nothing else go in ``jobs.submitted_by``.
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

#: A critique longer than this is a review, not a revision line.
MAX_CRITIQUE_WORDS = 40

DEFAULT_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma4:e4b"
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "critic.md"

#: The heading as :func:`compose_prompt` writes it and only as it writes it: at
#: the start of a line, with the revision after it. A prompt that happens to say
#: "Revise: " in the middle of a sentence is a sentence, not a generation.
_REVISE_SPLIT_RE = re.compile(rf"^{re.escape(REVISE_HEADING)}[ \t]*", re.MULTILINE)

_PROMPT_VERSION_RE = re.compile(r"^prompt_version:\s*(\S+)\s*$", re.MULTILINE)
#: Where one sentence ends and the next begins: a terminator, then whitespace,
#: then something that is not the rest of an abbreviation.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
#: Things a sentence of critique has no business containing.
_CODE_MARKS = ("```", "{", "}", ";", "()", "=>", "function ", "<script", "//", "$")


class CritiqueRefused(Exception):
    """The critique will not start: an unreadable prompt file, a missing stub."""


class CritiqueFailed(Exception):
    """The model answered and the answer is unusable. ``raw`` is kept.

    ``strip_path`` and ``strip_sha256`` are filled in by :func:`critique` when
    the failure happened after the strip went out, so the row the worker keeps
    for a rejected critique is tied to the same pixels a good one would be.
    They are empty when nothing was ever shown to a model.
    """

    def __init__(
        self,
        message: str,
        raw: str = "",
        strip_path: str = "",
        strip_sha256: str = "",
    ) -> None:
        super().__init__(message)
        self.raw = raw
        self.strip_path = strip_path
        self.strip_sha256 = strip_sha256


@dataclass
class Critique:
    """One sentence, and the provenance a verdict needs to be readable later.

    ``strip_path`` is the frame strip the critic was shown and ``strip_sha256``
    is the sha256 of exactly the bytes that were sent. Together they are what
    :func:`sketchgen.pairs.artefact_hash` is for a verdict: re-run the sketch,
    regenerate the strip, and the hash no longer matches, so an old critique is
    visibly about a picture that no longer exists.
    """

    text: str
    model: str
    prompt_version: str
    strip_path: str
    strip_sha256: str
    tokens: dict[str, int] = field(default_factory=dict)
    raw: str = ""


# ---------------------------------------------------------------------------
# Rows, without caring whether they are sqlite3.Row or a plain dict
# ---------------------------------------------------------------------------


def _get(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        value = row[key]
    except (IndexError, KeyError, TypeError):
        return getattr(row, key, default)
    return default if value is None else value


def _entry(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def _strip_bytes(row: Any) -> tuple[bytes, str]:
    """The entry's four-frame strip, or a refusal. The critic must SEE.

    The same rule, the same three failure modes and deliberately the same
    wording as :func:`sketchgen.judge._strip_bytes`, which enforces it for the
    local judge (spec §5: the model reviewing a sketch has to look at it, not
    read about it). It is a sibling here rather than a shared helper because
    ``judge.py`` imports ``worker.py``, which imports this module; hoisting one
    function into either of them would close that import loop.

    There is no blind fallback on purpose. A critique written without the
    picture is what critic-v2 was, and what it produced was a revision of the
    executor's statement rather than of the sketch.
    """
    label = _get(row, "id", "?")
    path = str(_get(row, "strip_path", "") or "").strip()
    if not path:
        raise CritiqueRefused(
            f"entry {label} has no strip.png on record; the critic has to look "
            "at the sketch, so there is nothing to ask it"
        )
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CritiqueRefused(
            f"cannot read the strip for entry {label} at {path}: {exc}"
        ) from exc
    if not data:
        raise CritiqueRefused(f"the strip for entry {label} at {path} is empty")
    return data, path


def _lineage_row(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM lineage WHERE child_entry_id = ?", (entry_id,)
    ).fetchone()


def _parent_of(conn: sqlite3.Connection, entry_id: int) -> int | None:
    """The parent from the lineage table, falling back to the entry column.

    gallery.py resolves it the same way and for the same reason: the entry
    column is written by the worker from the job, the lineage row is written
    when the link is recorded, and a database where only one of them exists
    still has a readable line.
    """
    row = _lineage_row(conn, entry_id)
    if row is not None and row["parent_entry_id"] is not None:
        return int(row["parent_entry_id"])
    entry = _entry(conn, entry_id)
    if entry is None or entry["parent_entry_id"] is None:
        return None
    return int(entry["parent_entry_id"])


def generation_of(conn: sqlite3.Connection, entry_id: int) -> int:
    """How many critiques stand between this entry and its root. Roots are 0."""
    row = _lineage_row(conn, entry_id)
    if row is None or row["generation"] is None:
        return 0
    return int(row["generation"])


# ---------------------------------------------------------------------------
# Spawning
# ---------------------------------------------------------------------------


def compose_prompt(parent_prompt: str, critique_text: str) -> str:
    """The child's prompt: the parent's, then the critique under the heading."""
    base = (parent_prompt or "").strip()
    revision = " ".join((critique_text or "").split())
    return f"{base}\n\n{REVISE_HEADING} {revision}".strip()


def split_prompt(prompt: str) -> tuple[str, list[str]]:
    """Undo :func:`compose_prompt`: the root sentence, then the revisions.

    The accumulated prompt of a generation-10 entry is 1,700 characters and
    reads as one sentence with nine amendments stapled to it. Everything that
    wants to show a line — the entry title, the ledger panel, ``lineage.json``
    — wants the two halves apart, and the prompt has always been splittable:
    :data:`REVISE_HEADING` is fixed, :func:`compose_prompt` is the only writer,
    and it writes one heading per ancestor at the start of its own line.

    Round-trips: ``split_prompt(compose_prompt(root, critique))`` is
    ``(root.strip(), [collapsed critique])``, and composing those again gives
    the same prompt back. A root with no revisions returns an empty list.
    """
    text = (prompt or "").strip()
    if not text:
        return "", []
    parts = _REVISE_SPLIT_RE.split(text)
    root = parts[0].strip()
    # Each revision was written as one collapsed line, so collapsing here is
    # what makes the round trip exact rather than merely close.
    revisions = [" ".join(part.split()) for part in parts[1:]]
    return root, revisions


def spawn(
    conn: sqlite3.Connection,
    *,
    parent_entry_id: int,
    critique: str,
    critique_by: str,
    submitted_by: str,
    max_depth: int = DEFAULT_MAX_DEPTH,
    planner: str | None = None,
    rules_file: str | None = None,
    publication: str = "hold",
) -> int | None:
    """Queue the child job one critique asks for. Returns its job id, or None.

    None, and nothing written at all, when the parent is a rejected entry:
    a person closed it. A held parent is allowed — asking for a revision is
    how a person finds out whether the parent is worth publishing — and its
    children wait at the gate like any other.

    At ``generation >= max_depth`` the job is still created — the critique is
    not thrown away — but always with ``publication='hold'`` and with
    ``needs='review'``, so the line stops until a person moves it
    (DECIDE[lineage-depth]).

    ``planner`` and ``rules_file`` default to the parent's, so a line stays a
    fair comparison with itself: the rules file is a variable the gallery
    measures (spec §9) and changing it silently mid-line would spoil it.
    """
    text = " ".join((critique or "").split())
    if not text:
        raise ValueError("a critique with no text spawns nothing")
    by = (critique_by or "").strip()
    if not CRITIQUE_BY_RE.match(by):
        raise ValueError(
            f"critique_by {by!r}: a model id or a GitHub username, nothing else"
        )
    who = (submitted_by or "").strip()
    if not USERNAME_RE.match(who):
        raise ValueError(f"submitted_by {who!r}: a GitHub username, nothing else")

    parent = _entry(conn, int(parent_entry_id))
    if parent is None or parent["state"] not in SPAWNABLE:
        return None

    generation = generation_of(conn, int(parent_entry_id)) + 1
    at_limit = generation >= int(max_depth)

    options: dict[str, Any] = {
        "parent_entry_id": int(parent_entry_id),
        "critique": text,
        "critique_by": by,
        "publication": "hold" if at_limit else publication,
        "planner": planner if planner is not None else parent["planner"],
        "rules_file": rules_file if rules_file is not None else parent["rules_file"],
    }
    if at_limit:
        options["needs"] = "review"
    return db.enqueue(
        conn, compose_prompt(parent["prompt"], text), who, **options
    )


def record_child(conn: sqlite3.Connection, child_entry_id: int, job: Any) -> int | None:
    """Copy a job's pending critique into `lineage`. Called by the worker.

    Returns the generation recorded, or None when the job has no parent and
    there is therefore no link to record. Idempotent: ``db.add_lineage`` is an
    INSERT OR REPLACE keyed on the child entry.
    """
    parent_entry_id = _get(job, "parent_entry_id")
    if not parent_entry_id:
        return None
    generation = generation_of(conn, int(parent_entry_id)) + 1
    db.add_lineage(
        conn,
        int(child_entry_id),
        int(parent_entry_id),
        generation=generation,
        critique_by=_get(job, "critique_by"),
        critique=_get(job, "critique"),
    )
    return generation


# ---------------------------------------------------------------------------
# Reading a line
# ---------------------------------------------------------------------------


def _children(conn: sqlite3.Connection) -> dict[int, list[int]]:
    ids = [
        int(row["id"])
        for row in conn.execute("SELECT id FROM entries ORDER BY id")
    ]
    kids: dict[int, list[int]] = {entry_id: [] for entry_id in ids}
    for entry_id in ids:
        mother = _parent_of(conn, entry_id)
        if mother is not None and mother in kids and mother != entry_id:
            kids[mother].append(entry_id)
    return kids


def _scores(conn: sqlite3.Connection) -> dict[int, Any]:
    """Bradley–Terry scores from packet 5.1, if packet 5.1 is installed.

    Imported lazily and defensively on purpose: `pairs.py` is another packet's
    file and may not exist in this checkout at all. Absent, or present and
    unhappy, means every entry's ``scores`` is None — which is the honest answer
    and not an error, because a line is readable without scores.
    """
    try:
        from sketchgen import pairs  # type: ignore
    except Exception:
        return {}
    for name in ("scores", "entry_scores", "score_table", "all_scores"):
        fn = getattr(pairs, name, None)
        if not callable(fn):
            continue
        try:
            table = fn(conn)
        except Exception:
            continue
        if isinstance(table, Mapping):
            out: dict[int, Any] = {}
            for key, value in table.items():
                try:
                    out[int(key)] = value
                except (TypeError, ValueError):
                    continue
            return out
    return {}


def line(
    conn: sqlite3.Connection,
    root_entry_id: int,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> list[dict[str, Any]]:
    """The whole line from one root, generation order, shallowest first.

    One dict per entry: ``entry_id``, ``generation``, ``state``, ``prompt``, the
    ``critique`` that produced it and its ``critique_by`` (both None for the
    root, which nobody critiqued into being), ``parent_entry_id``, ``children``,
    ``submitted_by``, ``scores`` (None unless ``sketchgen.pairs`` is importable)
    and ``at_limit`` — the generation where DECIDE[lineage-depth] says the line
    waits for a person.
    """
    root = _entry(conn, int(root_entry_id))
    if root is None:
        return []
    kids = _children(conn)
    scores = _scores(conn)

    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    queue: list[tuple[int, int]] = [(int(root_entry_id), generation_of(conn, int(root_entry_id)))]
    while queue:
        entry_id, walked = queue.pop(0)
        if entry_id in seen:
            continue
        seen.add(entry_id)
        row = _entry(conn, entry_id)
        if row is None:
            continue
        link = _lineage_row(conn, entry_id)
        # The recorded generation is the one record_child wrote and is what the
        # provenance says; the walked depth is the fallback for an entry whose
        # link predates this table having a number in it.
        generation = (
            int(link["generation"])
            if link is not None and link["generation"] is not None
            else walked
        )
        out.append(
            {
                "entry_id": entry_id,
                "generation": generation,
                "state": row["state"],
                "prompt": row["prompt"],
                "brief": row["brief"],
                "submitted_by": row["submitted_by"],
                "parent_entry_id": _parent_of(conn, entry_id),
                "children": list(kids.get(entry_id, [])),
                "critique": link["critique"] if link is not None else None,
                "critique_by": link["critique_by"] if link is not None else None,
                "scores": scores.get(entry_id),
                "at_limit": generation >= int(max_depth),
            }
        )
        for kid in kids.get(entry_id, []):
            queue.append((kid, generation + 1))
    out.sort(key=lambda item: (item["generation"], item["entry_id"]))
    return out


def roots(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Entries with no parent that have children — one per line, id order."""
    kids = _children(conn)
    found: list[dict[str, Any]] = []
    for entry_id in sorted(kids):
        if _parent_of(conn, entry_id) is not None or not kids[entry_id]:
            continue
        row = _entry(conn, entry_id)
        if row is None:  # pragma: no cover - the id came from this table
            continue
        found.append(
            {
                "entry_id": entry_id,
                "state": row["state"],
                "prompt": row["prompt"],
                "children": list(kids[entry_id]),
                "generations": 1 + max(
                    (item["generation"] for item in line(conn, entry_id)), default=0
                ),
                "created_utc": row["created_utc"],
            }
        )
    return found


# ---------------------------------------------------------------------------
# The critique itself — one model call, and never by accident
# ---------------------------------------------------------------------------


def _read_prompt_file(path: str | Path | None = None) -> str:
    target = Path(path) if path is not None else PROMPT_PATH
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        raise CritiqueRefused(f"cannot read the prompt file {target}: {exc}") from exc


def prompt_version(path: str | Path | None = None) -> str:
    """The ``prompt_version:`` line at the top of ``prompts/critic.md``.

    Read from the file, never written in code: an edited prompt with an
    unchanged version would make every verdict's provenance a lie (spec §5).
    """
    text = _read_prompt_file(path)
    match = _PROMPT_VERSION_RE.search(text)
    if match is None:
        raise CritiqueRefused(f"no 'prompt_version:' line in {path or PROMPT_PATH}")
    return match.group(1)


def critique_prompt(
    entry_row: Any,
    statement: str | None,
    brief: str | None,
    path: str | Path | None = None,
) -> str:
    """Fill ``prompts/critic.md`` for one entry — the words half of the request.

    The critic sees the prompt, the brief, the executor's own statement and the
    assertions the gate ran. It does not see who submitted it, what any human
    thought of it, or which model made it: spec §5 blinds the agent, and a
    critic that becomes the next prompt is an agent judge with a pen.

    The picture is not in here. :func:`critique` attaches the frame strip as an
    image beside this text, and critic-v3's first section tells the model that
    the image is the only evidence of what the sketch shows and that it beats
    the statement wherever the two disagree.
    """
    text = _read_prompt_file(path)
    text = _PROMPT_VERSION_RE.sub("", text, count=1).lstrip("\n")
    raw_assertions = _get(entry_row, "assertions_json")
    try:
        words = json.loads(raw_assertions) if raw_assertions else []
    except (TypeError, ValueError):
        words = []
    return (
        text.replace("{prompt}", str(_get(entry_row, "prompt", "") or "").strip())
        .replace("{brief}", str(brief or "").strip() or "(no brief on record)")
        .replace(
            "{statement}",
            str(statement or "").strip() or "(the model said nothing about it)",
        )
        .replace("{assertions}", ", ".join(str(w) for w in words) or "(none)")
    )


def validate(raw: str) -> str:
    """One sentence, under forty words, no code. Returns it trimmed.

    Raises :class:`CritiqueFailed` with the raw text attached otherwise, so the
    caller can save what the model actually said before it exits 1.
    """
    text = " ".join((raw or "").split())
    if not text:
        raise CritiqueFailed("the critique is empty", raw)
    for mark in _CODE_MARKS:
        if mark in text:
            raise CritiqueFailed(
                f"the critique contains code ({mark!r}); it becomes a prompt, "
                "not a patch",
                raw,
            )
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(text) if part]
    if len(sentences) > 1:
        raise CritiqueFailed(
            f"the critique is {len(sentences)} sentences; one is the contract",
            raw,
        )
    words = text.split()
    if len(words) >= MAX_CRITIQUE_WORDS:
        raise CritiqueFailed(
            f"the critique is {len(words)} words; under {MAX_CRITIQUE_WORDS} "
            "is the contract",
            raw,
        )
    return text


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
        raise CritiqueFailed(
            f"the model host {url} did not answer usably: {exc}"
        ) from exc


def critique(
    conn: sqlite3.Connection,
    entry_id: int,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    stub: str | Path | None = None,
    *,
    num_ctx: int = 8192,
    seed: int = 1,
    prompt_path: str | Path | None = None,
    timeout: float = 300.0,
) -> Critique:
    """One sentence of critique for one entry. With ``stub``, call nothing.

    The entry's frame strip goes with the words, base64, in ``/api/generate``'s
    ``images`` field — the same field the local judge fills over ``/api/chat``,
    and the reason the call can stay on ``/api/generate``: ollama accepts
    ``images`` beside ``prompt`` there, and nothing in this call needs a
    multi-turn conversation. An entry with no readable strip is
    :class:`CritiqueRefused` and never a blind critique (see :func:`_strip_bytes`).

    The strip is read before the ``stub`` branch, so a replayed run refuses on
    the same evidence a real one would: the stub replaces the model, not the
    requirement to have looked.

    ``"think": false`` goes only to the qwen tags, as in planner.py: ollama
    refuses an option a model does not declare, and ``gemma4:e4b`` — the critic,
    as it is the local judge in spec §5 — has no thinking mode to turn off.
    """
    row = _entry(conn, int(entry_id))
    if row is None:
        raise CritiqueRefused(f"there is no entry {entry_id}")
    strip, strip_path = _strip_bytes(row)
    strip_sha256 = hashlib.sha256(strip).hexdigest()
    version = prompt_version(prompt_path)
    rendered = critique_prompt(row, row["statement"], row["brief"], prompt_path)
    tokens: dict[str, int] = {}

    if stub is not None:
        path = Path(stub)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CritiqueRefused(f"cannot read the stub {path}: {exc}") from exc
    else:
        payload: dict = {
            "model": model,
            "prompt": rendered,
            "images": [base64.b64encode(strip).decode("ascii")],
            "stream": False,
            "options": {"num_ctx": num_ctx, "seed": seed},
        }
        if model.startswith("qwen"):
            payload["think"] = False
        body = _post(host, payload, timeout)
        raw = body.get("response")
        if not isinstance(raw, str):
            raise CritiqueFailed(
                "the model host returned no 'response' field",
                json.dumps(body, indent=2),
                strip_path,
                strip_sha256,
            )
        tokens = {
            "prompt": int(body.get("prompt_eval_count") or 0),
            "response": int(body.get("eval_count") or 0),
        }

    try:
        text = validate(raw)
    except CritiqueFailed as exc:
        # The words failed, but the picture was still the evidence. The worker
        # keeps a rejected critique as a row, and that row should say which
        # pixels it is a rejection of.
        exc.strip_path = strip_path
        exc.strip_sha256 = strip_sha256
        raise

    return Critique(
        text=text,
        model=model,
        prompt_version=version,
        strip_path=strip_path,
        strip_sha256=strip_sha256,
        tokens=tokens,
        raw=raw,
    )
