"""Any of the four model steps, answered off the node (docs/plans/agentic-cli.md).

The planner, the executor, the judge and the critic each ask a model something.
DECIDE[credential-model] branch B, spec §6, says the paid model never runs on
the node: the node has no key and is not getting one. So a paid step is always
two legs — the node writes down what it would have asked, something holding the
credential answers, the node reads the answer back — and this module is those
two legs, once, for all four steps.

It is small because every step is already split down the middle. Each renders
its own prompt, and each parses a reply without caring who produced it, since
the ``--stub`` path that the tests replay is exactly the seam a paid model
needs. So an adapter here knows three things — what to offer, how to render it,
where the answer is written — and borrows the rest from the local path:

    export  →  the prompt the local path would have rendered, its inputs, a guard
    (laptop →  an agent answers each item with the model that holds the credential)
    import  →  parsed by the local path's parser, written where the local path writes

**The envelope** is the judge's claims packet (``judge.claims_packet``)
generalised with a ``step`` field. Each item carries the rendered prompt, the
inputs it was rendered from, **paths** to any images rather than their bytes
(the laptop reads them over the tunnel, and a packet stays small enough to
paste), a ``guard``, and an empty ``answer`` for the model's reply, verbatim.

**The guard** is whatever can move under a request between export and import,
and it differs per step (plan §3.3). An item whose guard no longer matches is
rejected with a reason and writes nothing: an answer is only interpretable
against what the model was actually shown (spec §5).

**Provenance is who answered.** The envelope names the model it was cut for;
the laptop may overwrite ``model`` — on the envelope or on one item — with the
model that actually answered, and that is the id written to the database.

Judge packets written before this module (``sketchgen-judge-claims``) are on
disk and keep importing: :func:`import_packet` hands them to
:func:`sketchgen.judge.import_verdicts_detailed` unchanged.

Nothing here calls a model, and nothing here runs a worker. An import writes
where the worker would have written and puts a job back on the queue; the
node's own daemon does the gating and publishing as it always has.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import db
from . import judge

__all__ = [
    "PACKET_KIND",
    "STEPS",
    "Context",
    "PaidRefused",
    "Rejected",
    "export_packet",
    "import_packet",
    "sha256_file",
    "waiting",
]

PACKET_KIND = "sketchgen-paid"
PACKET_VERSION = 1

#: The four steps, in pipeline order. The CLI's ``--step`` choices.
STEPS = ("plan", "execute", "judge", "critique")

#: The same shape the judge accepts for a model id: a model id, not a sentence.
MODEL_RE = judge.MODEL_RE


class PaidRefused(Exception):
    """This packet will not be cut or read at all; nothing is written. Exit 3."""


class Rejected(Exception):
    """One item's answer cannot land. Nothing is written for it.

    ``reason`` is printed; the raw answer stays in the packet and the CLI saves
    the rejected items beside it, so a reply that would not parse is never lost.
    """


@dataclass
class Context:
    """What an adapter needs beyond the connection: where the node keeps things."""

    jobs_dir: Path = field(default_factory=lambda: Path("~/sketchgen/jobs").expanduser())
    lineage_depth: int = 3


def sha256_file(path: str | Path) -> str:
    """The sha256 of a file's bytes, or a :class:`Rejected` naming the file."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise Rejected(f"cannot read {path}: {exc}") from exc


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class Adapter:
    """One step's three answers: what to offer, how to render it, where it lands.

    ``offer`` returns items with the envelope's fields filled except ``answer``.
    ``land`` writes one answered item, or raises :class:`Rejected` having
    written nothing. ``answered`` says whether the laptop got to an item at all:
    an unanswered item is skipped in silence, because a packet handed back half
    done is allowed (the judge's rule).
    """

    step = ""
    #: What the agent on the laptop is told, in the envelope, about this step.
    how_to_answer = ""

    def prompt_version(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def offer(
        self, conn: sqlite3.Connection, *, model: str, limit: int, ctx: Context
    ) -> list[dict[str, Any]]:  # pragma: no cover - abstract
        raise NotImplementedError

    def answered(self, item: Mapping[str, Any]) -> bool:
        return bool(str(item.get("answer") or "").strip())

    def waiting(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """``{"count", "summary"}``: what an export would offer, for `paid status`."""
        return {"count": None, "summary": ""}  # pragma: no cover - abstract

    def land(
        self,
        conn: sqlite3.Connection,
        item: Mapping[str, Any],
        *,
        model: str,
        ctx: Context,
    ) -> str:  # pragma: no cover - abstract
        """Write one item. Returns one line for the operator saying what landed."""
        raise NotImplementedError


class JudgeAdapter(Adapter):
    """The judge, the pattern the other three follow — ported, not changed.

    Offering is :func:`judge.export_claims` and landing is
    :func:`judge.import_verdicts_detailed`, so the rules the judge already
    enforces — the blind, the ``artefact_hash`` guard, 'A', 'B' or 'tie' — are
    the rules here. An item may be answered the old way, with ``answers``
    filled in per question, or the new way, with the model's reply verbatim in
    ``answer``, which :func:`judge.parse_reply` reads exactly as it reads the
    local judge's.
    """

    step = "judge"
    how_to_answer = (
        "Show the model the item's prompt with the two images in 'images', A "
        "first. Put its reply, verbatim, in 'answer' — two lines, 'brief: A|B|tie' "
        "and 'look: A|B|tie', then any reasons. (Filling 'answers' per question "
        "instead also works.)"
    )

    def prompt_version(self) -> str:
        return judge.prompt_version()

    def offer(self, conn, *, model, limit, ctx):
        items = []
        for claim in judge.export_claims(conn, judge_id=model, limit=limit):
            items.append(
                {
                    "key": f"pair {claim['entry_a']} vs {claim['entry_b']}",
                    "prompt": claim["prompt"],
                    "images": [claim["strip_a"], claim["strip_b"]],
                    "guard": claim["artefact_hash"],
                    "prompt_version": claim["prompt_version"],
                    "inputs": {
                        "entry_a": claim["entry_a"],
                        "entry_b": claim["entry_b"],
                        "brief_a": claim["brief_a"],
                        "brief_b": claim["brief_b"],
                    },
                    "answers": claim["answers"],
                    "answer": "",
                }
            )
        return items

    def waiting(self, conn):
        published = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE state = 'published'"
        ).fetchone()[0]
        return {
            "count": None,
            "summary": f"per model: pairs of {published} published entries that "
                       "model has not judged (`paid export --step judge --as M`)",
        }

    def answered(self, item):
        answers = item.get("answers")
        structured = isinstance(answers, dict) and any(
            str(value or "").strip() for value in answers.values()
        )
        return structured or super().answered(item)

    def land(self, conn, item, *, model, ctx):
        inputs = item.get("inputs") or {}
        answers = item.get("answers") if isinstance(item.get("answers"), dict) else {}
        reasons: dict[str, str] = {}
        if not any(str(v or "").strip() for v in answers.values()):
            try:
                brief, look, reasons = judge.parse_reply(str(item.get("answer") or ""))
            except judge.JudgeFailed as exc:
                raise Rejected(str(exc)) from exc
            answers = {"brief": brief, "look": look}
        claim = {
            "entry_a": inputs.get("entry_a"),
            "entry_b": inputs.get("entry_b"),
            "artefact_hash": item.get("guard"),
            "prompt_version": item.get("prompt_version"),
            "judge_id": model,
            "answers": answers,
            "reasons": reasons,
        }
        recorded, rejected = judge.import_verdicts_detailed(conn, [claim])
        if rejected:
            raise Rejected(rejected[0]["reason"])
        if not recorded:
            raise Rejected("nothing to record: no answer to either question")
        return (f"{item.get('key')}: brief={answers['brief']} "
                f"look={answers['look']} by {model}")


#: One adapter per step. A step not listed here refuses at the CLI.
ADAPTERS: dict[str, Adapter] = {
    "judge": JudgeAdapter(),
}


def adapter_for(step: str) -> Adapter:
    try:
        return ADAPTERS[step]
    except KeyError:
        raise PaidRefused(
            f"no paid route for {step!r} yet; there is one for: "
            + ", ".join(s for s in STEPS if s in ADAPTERS)
        ) from None


def waiting(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Per step with a paid route: what is waiting for an answer off the node."""
    return {step: ADAPTERS[step].waiting(conn) for step in STEPS if step in ADAPTERS}


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _check_model(model: str) -> str:
    model = str(model or "").strip()
    if not MODEL_RE.match(model):
        raise PaidRefused(f"{model!r} is not a usable model id")
    if model in ("local", "paid"):
        raise PaidRefused(
            f"{model!r} is a setting, not a model: name the model that will answer"
        )
    return model


def export_packet(
    conn: sqlite3.Connection,
    step: str,
    *,
    model: str,
    limit: int = 20,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """The envelope for ``step``: what ``model`` owes an answer on, right now.

    ``model`` is the model the packet is cut for. For the judge that decides
    which pairs are offered (the ones that judge has not answered); for the
    others it is recorded in the envelope and is the default provenance when
    the laptop does not name the model that answered.
    """
    adapter = adapter_for(step)
    model = _check_model(model)
    items = adapter.offer(conn, model=model, limit=max(0, int(limit)), ctx=ctx or Context())
    return {
        "packet": PACKET_KIND,
        "version": PACKET_VERSION,
        "step": step,
        "model": model,
        "prompt_version": adapter.prompt_version(),
        "created_utc": db.utc_now(),
        "how_to_answer": (
            adapter.how_to_answer
            + " Leave an item's 'answer' empty to skip it. Set 'model' (here, or "
            "on one item) to the model that actually answered: that is what the "
            "node records. Change nothing else — 'guard' is how the node knows "
            "the answer is still about what it asked."
        ),
        "items": items,
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    step: str
    recorded: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    skipped: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "recorded": len(self.recorded),
            "landed": list(self.recorded),
            "rejected": [
                {"item": row["item"], "reason": row["reason"]} for row in self.rejected
            ],
            "skipped": self.skipped,
        }


def _legacy_judge(conn: sqlite3.Connection, packet: Any) -> ImportReport:
    """A packet ``judge export`` wrote before this module existed."""
    recorded, rejected = judge.import_verdicts_detailed(conn, packet)
    report = ImportReport(step="judge")
    report.recorded = [f"pair {n + 1}" for n in range(recorded)]
    report.rejected = [
        {"item": row["pair"], "reason": row["reason"], "answer": ""} for row in rejected
    ]
    return report


def import_packet(
    conn: sqlite3.Connection,
    packet: Any,
    *,
    ctx: Context | None = None,
    log: Callable[[str], None] | None = None,
) -> ImportReport:
    """Land every answered item in ``packet``; reject, with a reason, what cannot.

    An unanswered item is skipped in silence. A rejected item writes nothing,
    and its raw answer is carried in the report's ``rejected`` rows under
    ``answer`` so the caller can keep it. Raises :class:`PaidRefused` only for a
    file that is not a packet at all.
    """
    ctx = ctx or Context()
    say = log or (lambda _message: None)
    if isinstance(packet, list) or (
        isinstance(packet, dict)
        and (packet.get("packet") == judge.PACKET_KIND or "claims" in packet)
    ):
        try:
            return _legacy_judge(conn, packet)
        except judge.JudgeRefused as exc:
            raise PaidRefused(str(exc)) from exc
    if not isinstance(packet, dict):
        raise PaidRefused("a packet is a JSON object")
    kind = str(packet.get("packet") or "").strip()
    if kind != PACKET_KIND:
        raise PaidRefused(f"{kind or 'this'!r} is not a {PACKET_KIND} packet")
    step = str(packet.get("step") or "").strip()
    adapter = adapter_for(step)
    items = packet.get("items")
    if not isinstance(items, list):
        raise PaidRefused("the packet has no 'items' list")

    report = ImportReport(step=step)
    envelope_model = str(packet.get("model") or "").strip()
    for item in items:
        if not isinstance(item, dict):
            continue
        if not adapter.answered(item):
            report.skipped += 1
            continue
        key = str(item.get("key") or "?")
        who = str(item.get("model") or envelope_model).strip()
        try:
            _check_model(who)
            line = adapter.land(conn, item, model=who, ctx=ctx)
        except PaidRefused as exc:
            reason = str(exc)
        except Rejected as exc:
            reason = str(exc)
        else:
            report.recorded.append(line)
            say(f"landed {line}")
            continue
        report.rejected.append(
            {"item": key, "reason": reason, "answer": str(item.get("answer") or "")}
        )
        say(f"rejected {key}: {reason}")
    return report
