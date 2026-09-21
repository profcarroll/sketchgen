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
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import db
from . import judge
from . import lineage
from . import planner
from . import worker

__all__ = [
    "PACKET_KIND",
    "STEPS",
    "Context",
    "PaidRefused",
    "Rejected",
    "export_packet",
    "import_packet",
    "release",
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
    lineage_depth: int = worker.DEFAULT_LINEAGE_DEPTH


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


# ---------------------------------------------------------------------------
# The planner's write-back, shared with `sketchgen plan --job`
# ---------------------------------------------------------------------------


def plan_document(result: planner.Plan, model: str, started_utc: str) -> dict[str, Any]:
    """What ``plan.json`` holds: the plan, what it threw away, who wrote it."""
    return {
        "brief": result.brief,
        "assertions": result.assertions,
        "rejected": result.rejected,
        "defaulted": result.defaulted,
        "prompt_version": result.prompt_version,
        "model": model,
        "tokens": result.tokens,
        "durations": result.durations,
        "started_utc": started_utc,
    }


def save_plan(out_dir: Path, result: planner.Plan, document: Mapping[str, Any]) -> None:
    """``response.txt`` (the raw reply, the only evidence) and ``plan.json``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "response.txt").write_text(result.raw, encoding="utf-8")
    (out_dir / "plan.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8"
    )


def requeue_planned(
    conn: sqlite3.Connection, job_id: int, result: planner.Plan, model: str
) -> db.Job:
    """The worker's own write, made from off the node, and back on the queue.

    The brief, the assertions and the model that wrote them, in the one
    transition that also puts the job back on the queue — the columns the
    worker writes on the local path, so a job planned off the node is
    indistinguishable afterwards from one planned on it. ``planner`` takes the
    model that answered rather than the word it was queued under: provenance
    is who answered, not who was asked. ``needs`` is cleared by hand because
    ``transition`` does not clear it.

    The state is ``queued``, not ``executing``. ``db.claim_next`` selects on
    ``state = 'queued'`` alone, so a job moved straight to ``executing`` by
    something that is not the worker sits in a running state with nobody in it
    until the stuck-sweep notices, half an hour later by default. Queued, it is
    claimed on the next pass, and ``claim_next`` sends a job that has a brief
    to ``executing`` itself.
    """
    return db.transition(
        conn,
        job_id,
        "queued",
        brief=result.brief,
        assertions_json=json.dumps(result.assertions),
        planner=model,
        needs=None,
    )


def plannable(job: db.Job | None) -> str | None:
    """Why this job cannot take a plan from off the node, or None if it can.

    Only a job parked at ``needs-laptop`` for a plan. Anything else is finished
    or in flight with the worker attending it, and a second writer of
    ``jobs.brief`` is the same class of mistake as a second worker.
    """
    if job is None:
        return "there is no such job"
    if job.state != "needs-laptop":
        return (f"job {job.id} is {job.state}, not needs-laptop — only a job "
                "waiting for the laptop can be planned here")
    if job.needs not in (None, "plan"):
        return f"job {job.id} is waiting for {job.needs!r}, not a plan"
    return None


class PlanAdapter(Adapter):
    """The planner: one prompt in, a brief and its assertions out.

    Offering is the jobs parked at ``needs-laptop`` waiting for a plan — the
    worker parks a job there when its planner is ``paid`` or a model named in
    ``SKETCHGEN_PAID_MODELS``. Rendering is :func:`planner.build_prompt`, and
    landing is :func:`planner.parse_response` and
    :func:`planner.validate_detailed`, the strict parser the local path uses
    first, then :func:`requeue_planned`. A reply that will not parse leaves the
    job parked (plan §5.4): it is a reply to try again, not a job to fail.

    **No guard**, and on purpose (plan §3.3). ``jobs.prompt`` is written once
    by ``enqueue`` and no code in this repository updates it, so what the
    laptop was shown is what the job still holds. The prompt *version* is
    checked instead: a reply to an older ``planner.md`` is not a reply to the
    prompt the node would now ask.
    """

    step = "plan"
    how_to_answer = (
        "Send the model the item's prompt. Put its reply, verbatim, in "
        "'answer': a 'Brief' heading and paragraph, then an 'Assertions' "
        "heading and one vocabulary word per line."
    )

    def prompt_version(self) -> str:
        try:
            return planner.prompt_version()
        except planner.PlannerRefused as exc:
            raise PaidRefused(str(exc)) from exc

    def _parked(self, conn: sqlite3.Connection) -> list[db.Job]:
        return [
            job for job in db.list_jobs(conn, "needs-laptop")
            if job.needs in (None, "plan")
        ]

    def offer(self, conn, *, model, limit, ctx):
        version = self.prompt_version()
        items = []
        for job in self._parked(conn)[:limit]:
            items.append(
                {
                    "key": f"job {job.id}",
                    "prompt": planner.build_prompt(job.prompt, job.submitted_by),
                    "images": [],
                    "guard": "",
                    "prompt_version": version,
                    "inputs": {
                        "job": job.id,
                        "prompt": job.prompt,
                        "queued_for": job.planner,
                    },
                    "answer": "",
                }
            )
        return items

    def waiting(self, conn):
        parked = self._parked(conn)
        return {
            "count": len(parked),
            "jobs": [job.id for job in parked],
            "summary": f"{len(parked)} job(s) parked for a plan",
        }

    def land(self, conn, item, *, model, ctx):
        inputs = item.get("inputs") or {}
        try:
            job_id = int(inputs["job"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("the item names no job") from None
        why = plannable(db.get_job(conn, job_id))
        if why:
            raise Rejected(why)
        version = str(item.get("prompt_version") or "")
        if version != self.prompt_version():
            raise Rejected(
                f"cut under {version or 'no prompt version'}, and the planner is "
                f"now {self.prompt_version()}: export again"
            )
        raw = str(item.get("answer") or "")
        try:
            brief, words = planner.parse_response(raw)
        except planner.PlannerFailed as exc:
            raise Rejected(f"{exc}; job {job_id} is unchanged, still needs-laptop") from exc
        ok, rejected, defaulted = planner.validate_detailed(words)
        result = planner.Plan(
            brief=brief, assertions=ok, prompt_version=version, tokens={},
            durations={}, raw=raw, rejected=rejected, defaulted=defaulted,
        )
        try:
            save_plan(ctx.jobs_dir / str(job_id), result,
                      plan_document(result, model, db.utc_now()))
        except OSError as exc:
            raise Rejected(f"cannot write the plan for job {job_id}: {exc}") from exc
        requeue_planned(conn, job_id, result, model)
        return (f"job {job_id}: planned by {model}, assertions "
                f"{', '.join(ok) or '-'}; back on the queue")


class CritiqueAdapter(Adapter):
    """The critic: one sentence about one published entry, which becomes a child.

    Offering is :func:`db.entries_to_critique` — the entries the idle loop
    would reach for — and every entry offered is **claimed** for the model the
    packet is cut for (:func:`db.paid_claims`), which the idle loop then skips.
    That is plan §3.4's answer: `critiques` holds one row per entry per prompt
    version, so the paid critic is the only critic for the entries it has been
    handed rather than a racer for them. An export re-offers this model's own
    outstanding claims first, so a packet lost on the laptop costs nothing.

    Landing is the idle loop's two writes — :func:`lineage.spawn` and
    :func:`db.record_critique`, with the same arguments — after
    :func:`lineage.validate`, unchanged. One difference, on purpose: the idle
    loop records a critique that fails the validator as a rejected row, which
    uses up the entry for that prompt version. Here it writes nothing and the
    entry stays claimed, because the reply is the laptop's to try again (plan
    §5.4), and the raw text is saved beside the packet.

    The guard is the sha256 of the strip (plan §3.3). The strip is the evidence
    and :func:`lineage.critique` refuses to critique without it; a strip that
    changed since export means the sentence is about a picture that is gone.
    """

    step = "critique"
    how_to_answer = (
        "Show the model the item's prompt with the one image in 'images' (the "
        "sketch's frame strip: the only evidence of what it shows). Put its "
        "reply, verbatim, in 'answer': one sentence, under 40 words, no code."
    )

    def prompt_version(self) -> str:
        try:
            return lineage.prompt_version()
        except lineage.CritiqueRefused as exc:
            raise PaidRefused(str(exc)) from exc

    def _others(self, conn: sqlite3.Connection, model: str) -> list[int]:
        return [
            entry_id
            for entry_id, claim in db.paid_claims(conn, self.step).items()
            if claim.get("model") != model
        ]

    def offer(self, conn, *, model, limit, ctx):
        version = self.prompt_version()
        wanted = db.entries_to_critique(
            conn, version, limit, exclude=self._others(conn, model)
        )
        items = []
        for entry_id in wanted:
            row = db.get_entry(conn, entry_id)
            strip = str(row["strip_path"])
            try:
                guard = sha256_file(strip)
            except Rejected:
                continue  # an unreadable strip: the local critic refuses it too
            items.append(
                {
                    "key": f"entry {entry_id}",
                    "prompt": lineage.critique_prompt(row, row["statement"], row["brief"]),
                    "images": [strip],
                    "guard": guard,
                    "prompt_version": version,
                    "inputs": {
                        "entry": entry_id,
                        "generation": lineage.generation_of(conn, entry_id),
                    },
                    "answer": "",
                }
            )
        db.claim_paid(conn, self.step, [item["inputs"]["entry"] for item in items], model)
        return items

    def waiting(self, conn):
        claims = db.paid_claims(conn, self.step)
        try:
            open_ = db.entries_to_critique(
                conn, self.prompt_version(), 10_000, exclude=claims
            )
        except PaidRefused:
            open_ = []
        return {
            "count": len(open_),
            "claimed": {str(k): v for k, v in sorted(claims.items())},
            "summary": f"{len(open_)} entr{'y' if len(open_) == 1 else 'ies'} "
                       f"to critique, {len(claims)} claimed off the node",
        }

    def land(self, conn, item, *, model, ctx):
        inputs = item.get("inputs") or {}
        try:
            entry_id = int(inputs["entry"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("the item names no entry") from None
        row = db.get_entry(conn, entry_id)
        if row is None:
            raise Rejected(f"there is no entry {entry_id}")
        if not lineage.CRITIQUE_BY_RE.match(model):
            # It becomes `critique_by` on the child, which is narrower than a
            # model id; better refused here than recorded as a spawn refusal.
            raise Rejected(f"{model!r} cannot sign a critique: letters, digits, "
                           "'.', '_', ':' and '-' only")
        version = str(item.get("prompt_version") or "")
        if version != self.prompt_version():
            raise Rejected(
                f"cut under {version or 'no prompt version'}, and the critic is "
                f"now {self.prompt_version()}: export again"
            )
        if db.get_critique(conn, entry_id, version) is not None:
            raise Rejected(f"entry {entry_id} already has a {version} critique")
        strip = str(row["strip_path"] or "")
        if not strip or sha256_file(strip) != str(item.get("guard") or ""):
            raise Rejected("the strip no longer matches; the sketch changed")
        try:
            text = lineage.validate(str(item.get("answer") or ""))
        except lineage.CritiqueFailed as exc:
            raise Rejected(str(exc)) from exc

        job_id: int | None = None
        reason: str | None = None
        try:
            job_id = lineage.spawn(
                conn,
                parent_entry_id=entry_id,
                critique=text,
                critique_by=model,
                submitted_by=row["submitted_by"] or "",
                max_depth=ctx.lineage_depth,
                rules_file=lineage.parent_rules_file(conn, row),
                publication="hold",
            )
        except ValueError as exc:
            reason = f"spawn refused: {exc}"
        if job_id is None and reason is None:
            reason = "the parent is a rejected entry, and a line does not grow from one"
        db.record_critique(
            conn,
            entry_id,
            critique=text,
            critique_by=model,
            prompt_version=version,
            spawned_job_id=job_id,
            rejected_reason=reason,
            strip_path=strip,
            strip_sha256=item.get("guard"),
        )
        db.release_paid(conn, self.step, [entry_id])
        if job_id is None:
            return f"entry {entry_id}: critiqued by {model}, spawned nothing: {reason}"
        return f"entry {entry_id}: critiqued by {model} -> job {job_id}"


#: One adapter per step. A step not listed here refuses at the CLI.
ADAPTERS: dict[str, Adapter] = {
    "plan": PlanAdapter(),
    "judge": JudgeAdapter(),
    "critique": CritiqueAdapter(),
}


def release(conn: sqlite3.Connection, step: str,
            subject_ids: list[int] | None = None) -> list[int]:
    """Hand claimed subjects back to the local path. Only the critic claims."""
    adapter_for(step)
    return db.release_paid(conn, step, subject_ids)


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
