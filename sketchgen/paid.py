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
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import db
from . import executor
from . import judge
from . import lineage
from . import models
from . import planner
from . import worker

__all__ = [
    "DEFAULT_LEASE_MINUTES",
    "PACKET_KIND",
    "STEPS",
    "Context",
    "PaidRefused",
    "Rejected",
    "budget",
    "elapsed_of",
    "export_packet",
    "import_packet",
    "next_for",
    "next_step",
    "parse_since",
    "partner_of",
    "preflight",
    "process_of",
    "release",
    "release_job",
    "set_budget",
    "spend_of",
    "start",
    "try_for",
    "verdict_summary",
    "wait_for",
    "sha256_file",
    "waiting",
    "whose_turn",
    "worker_now",
]

PACKET_KIND = "sketchgen-paid"
PACKET_VERSION = 1

#: The four steps, in pipeline order. The CLI's ``--step`` choices.
STEPS = ("plan", "execute", "judge", "critique")

#: The same shape the judge accepts for a model id: a model id, not a sentence.
MODEL_RE = judge.MODEL_RE

#: How long a paid verb's touch on a job keeps the worker standing by for it
#: (db.paid_leases). Long enough to write a sketch between `next` and
#: `import`; short enough that an agent that vanished costs the idle loop
#: minutes. Every verb the agent runs on the job renews it.
DEFAULT_LEASE_MINUTES = float(os.environ.get("SKETCHGEN_PAID_LEASE_MINUTES", "") or 20)

#: The steps an agent answers per job. The judge and the critic are per entry.
JOB_STEPS = ("plan", "execute")


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
    #: Offer only this job (plan and execute): an agent running its own job
    #: should not be handed everybody else's parked ones.
    job: int | None = None


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
    #: Whether this step's items offer a `process` slot: only where there is a
    #: row to record it on (the attempt), so a cost an agent writes into a
    #: packet is never silently dropped.
    records_process = False

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

    def _parked(self, conn: sqlite3.Connection, only: int | None = None) -> list[db.Job]:
        return [
            job for job in db.list_jobs(conn, "needs-laptop")
            if job.needs in (None, "plan") and (only is None or job.id == only)
        ]

    def offer(self, conn, *, model, limit, ctx):
        version = self.prompt_version()
        items = []
        for job in self._parked(conn, ctx.job)[:limit]:
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
        usage = usage_of(item)
        trip = round_trip_s(item.get("_exported_utc"))
        result = planner.Plan(
            brief=brief, assertions=ok, prompt_version=version,
            tokens={k: v for k, v in usage.items() if v is not None},
            durations={"round_trip_s": trip} if trip is not None else {},
            raw=raw, rejected=rejected, defaulted=defaulted,
        )
        try:
            save_plan(ctx.jobs_dir / str(job_id), result,
                      plan_document(result, model, db.utc_now()))
        except OSError as exc:
            raise Rejected(f"cannot write the plan for job {job_id}: {exc}") from exc
        requeue_planned(conn, job_id, result, model)
        return (f"job {job_id}: planned by {model}, assertions "
                f"{', '.join(ok) or '-'}; back on the queue")


class ExecuteAdapter(Adapter):
    """The executor: one attempt at the sketch, gated on the node as always.

    Offering is the jobs parked at ``needs-laptop`` with ``needs='execute'`` —
    the worker parks one there at the top of every attempt whose executor is
    ``paid`` or a named paid model. Rendering is :func:`executor.render_prompt`
    over the brief the worker would have sent this attempt, which for attempt
    *n* > 1 is the brief plus the gate's evidence from attempt *n*−1
    (:func:`worker.brief_with_evidence`), and the rules file the worker would
    have resolved for it.

    Landing is **not** the gate. It checks the reply has a fenced js block —
    :func:`executor.parse_response`, the parser the worker uses — writes the
    reply into ``attempt-N/`` beside the job and puts the job back on the
    queue. The resident worker claims it, finds the reply where the model's
    answer would have been, and runs the rest of the attempt exactly as for a
    local model: the parse into ``sketch.js``, the real gate, the evidence, the
    repair or the hold. The gate is the referee and it does not move off the
    node. A failed gate parks the job again for attempt *n*+1, with the new
    evidence in the next export: one round trip per attempt.

    The guard is a digest of the attempt number and the previous attempt's
    evidence (plan §3.3): attempt *n* is a reply to attempt *n*−1's gate
    output, and an answer written against evidence the job no longer holds is
    an answer to a different question.
    """

    step = "execute"
    #: The one step whose answer has an attempt row to carry a process cost
    #: (migration 015), so the one step whose items offer `process`.
    records_process = True
    how_to_answer = (
        "Send the model the item's prompt. Put its reply, verbatim, in "
        "'answer': it must contain a fenced ```js block (and may contain an "
        "```html block and a statement), as the prompt asks. The node gates "
        "it; a failed gate comes back as the next attempt, evidence included."
    )

    def prompt_version(self) -> str:
        try:
            return executor.prompt_version()
        except executor.ExecutorRefused as exc:
            raise PaidRefused(str(exc)) from exc

    def _parked(self, conn: sqlite3.Connection, only: int | None = None) -> list[db.Job]:
        return [
            job for job in db.list_jobs(conn, "needs-laptop")
            if job.needs == "execute" and (only is None or job.id == only)
        ]

    @staticmethod
    def guard(n: int, evidence: str | None) -> str:
        return sha256_text(f"attempt {int(n)}\n{evidence or ''}")

    def offer(self, conn, *, model, limit, ctx):
        version = self.prompt_version()
        items = []
        for job in self._parked(conn, ctx.job)[:limit]:
            done = db.list_attempts(conn, job.id)
            n = len(done) + 1
            evidence = done[-1].evidence if done else None
            rules = worker.resolve_rules(job.rules_file, job.id)
            brief = worker.brief_with_evidence(job.brief or job.prompt, evidence)
            try:
                rules_text = executor.resolve_rules(rules).read_text(encoding="utf-8")
                prompt = executor.render_prompt(
                    brief, executor.normalise_assertions(job.assertions),
                    rules_text, executor.DEFAULT_SEED,
                )
            except (executor.ExecutorRefused, OSError) as exc:
                raise PaidRefused(f"job {job.id}: {exc}") from exc
            previous = (
                str(Path(done[-1].source_dir) / "sketch.js")
                if done and done[-1].source_dir else None
            )
            items.append(
                {
                    "key": f"job {job.id} attempt {n}",
                    "prompt": prompt,
                    "images": [],
                    "guard": self.guard(n, evidence),
                    "prompt_version": version,
                    "inputs": {
                        "job": job.id,
                        "attempt": n,
                        "max_attempts": job.max_attempts,
                        "rules_file": rules,
                        "assertions": job.assertions,
                        "previous_sketch": previous,
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
            "summary": f"{len(parked)} job(s) parked for an attempt",
        }

    def land(self, conn, item, *, model, ctx):
        inputs = item.get("inputs") or {}
        try:
            job_id = int(inputs["job"])
            n = int(inputs["attempt"])
        except (KeyError, TypeError, ValueError):
            raise Rejected("the item names no job and attempt") from None
        job = db.get_job(conn, job_id)
        if job is None:
            raise Rejected(f"there is no job {job_id}")
        if job.state != "needs-laptop" or job.needs != "execute":
            raise Rejected(f"job {job_id} is {job.state}"
                           + (f" (needs {job.needs})" if job.needs else "")
                           + ", not waiting for an attempt")
        done = db.list_attempts(conn, job_id)
        if len(done) + 1 != n:
            raise Rejected(f"job {job_id} is on attempt {len(done) + 1}, not {n}")
        evidence = done[-1].evidence if done else None
        if str(item.get("guard") or "") != self.guard(n, evidence):
            raise Rejected("the previous attempt's evidence no longer matches")
        version = str(item.get("prompt_version") or "")
        if version != self.prompt_version():
            raise Rejected(
                f"cut under {version or 'no prompt version'}, and the executor is "
                f"now {self.prompt_version()}: export again"
            )
        raw = str(item.get("answer") or "")
        parsed = executor.parse_response(raw)
        if parsed.js is None or not parsed.js.strip():
            raise Rejected(f"no fenced js block; job {job_id} is unchanged, still "
                           "needs-laptop")
        attempt_dir = ctx.jobs_dir / str(job_id) / f"attempt-{n}"
        try:
            attempt_dir.mkdir(parents=True, exist_ok=True)
            (attempt_dir / worker.PAID_REPLY).write_text(raw, encoding="utf-8")
            landed = db.utc_now()
            (attempt_dir / worker.PAID_META).write_text(
                json.dumps({"model": model, "prompt_version": version,
                            "guard": item.get("guard"),
                            "exported_utc": item.get("_exported_utc"),
                            "imported_utc": landed,
                            # The node's own clock: export to import. The
                            # worker writes it on the attempt as wall_s, where
                            # a local attempt's model time goes.
                            "round_trip_s": round_trip_s(item.get("_exported_utc"), landed),
                            "usage": usage_of(item),
                            # Beside the reply's cost, never added to it: what
                            # the agent says the work around this reply cost.
                            # The worker copies it onto the attempt and fills
                            # in `tries` from its own count.
                            "process": process_of(item)}, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise Rejected(f"cannot write attempt {n} of job {job_id}: {exc}") from exc
        # Queued, not executing, for the reason requeue_planned gives: only the
        # worker moves a job into a running state, or nobody attends it.
        db.transition(conn, job_id, "queued", needs=None)
        return (f"job {job_id} attempt {n}: written by {model}; back on the "
                "queue for the gate")


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
    "execute": ExecuteAdapter(),
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
    ctx = ctx or Context()
    if ctx.job is not None and step not in ("plan", "execute"):
        raise PaidRefused(f"--job narrows plan and execute; {step} is not per job")
    items = adapter.offer(conn, model=model, limit=max(0, int(limit)), ctx=ctx)
    if ctx.job is not None and db.get_job(conn, ctx.job) is not None:
        # The agent is here for this job: the worker serves it first and does
        # no idle work until the lease runs out or the job is finished.
        db.lease_paid(conn, ctx.job, model, DEFAULT_LEASE_MINUTES)
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
            "node records. If you know the token counts of your own reply, put "
            "them in the item's 'usage' (prompt_tokens, completion_tokens); "
            "leave what you do not know null, never an estimate. Change nothing "
            "else — 'guard' is how the node knows the answer is still about "
            "what it asked."
            + (
                " If your harness can report what the work around this reply "
                "cost — session seconds, tokens generated, thinking, tool "
                "calls, screenshots, the effort level — put those in the "
                "item's 'process'. Same rule: leave out what you do not know. "
                "It is recorded beside the entry, as reported by you, and "
                "never enters any measure."
                if adapter.records_process else ""
            )
        ),
        "items": [
            dict(item, usage=dict(EMPTY_USAGE),
                 **({"process": dict(EMPTY_PROCESS)} if adapter.records_process else {}))
            for item in items
        ],
    }


#: What an item carries for the counts only the answering side knows. The node
#: measures the round trip itself (`round_trip_s`, export to import); tokens
#: it can only be told. Null means not known, and stays null: a page that says
#: "—" is truer than one that says a guess (2026-09-21, entry 1246's blanks).
EMPTY_USAGE: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None}


def usage_of(item: Mapping[str, Any]) -> dict[str, int | None]:
    """The item's reported token counts, as integers or None."""
    raw = item.get("usage") if isinstance(item.get("usage"), Mapping) else {}
    out: dict[str, int | None] = {}
    for key in EMPTY_USAGE:
        value = raw.get(key)
        try:
            out[key] = int(value) if value is not None and str(value).strip() != "" else None
        except (TypeError, ValueError):
            out[key] = None
        if out[key] is not None and out[key] < 0:
            out[key] = None
    return out


#: What an item carries about the work *around* the reply, when the agent's
#: harness can report it (migration 015). Two costs, kept apart (dossier 01
#: §6.4): `usage` and `wall_s` above are the reply — what the node was sent
#: and how long the round trip took, the numbers that compare with a local
#: model's prompt and decode — and this is everything the agent did before and
#: between those replies. Job 1286's reply cost 111 s; its process cost was 42
#: minutes and about 207,000 generated tokens, and only the first was recorded.
#:
#: `tries` is not here: the node counts those itself (db.paid_tries) and fills
#: it in at the attempt, because it is the one field of the object the node
#: can check for itself.
EMPTY_PROCESS: dict[str, Any] = {
    "session_s": None,
    "output_tokens": None,
    "thinking_tokens": None,
    "tool_calls": None,
    "screenshots": None,
    "effort": None,
}

#: Every key a stored process object may carry: the agent's six, and the
#: node's count of the tries that went into the attempt.
PROCESS_KEYS = tuple(EMPTY_PROCESS) + ("tries",)

#: The counts, which are whole numbers and never negative. ``session_s`` is a
#: duration and may be fractional; ``effort`` is the word the harness uses for
#: itself ('max', 'high'), not a vocabulary this node owns.
PROCESS_COUNTS = ("output_tokens", "thinking_tokens", "tool_calls", "screenshots")

#: An effort longer than this is a sentence, and a sentence in a field read as
#: one word would print across the provenance table.
EFFORT_MAX = 40


def process_of(item: Mapping[str, Any]) -> dict[str, Any]:
    """The item's reported process cost, every field validated or None.

    Never defaulted to zero, for ``usage``'s reason and with more force: a
    harness that cannot count its own tool calls has not made zero of them.
    A field that is not a number, or is negative, becomes None rather than a
    refusal — the answer is what an import is about, and no sketch should be
    rejected over a cost line.
    """
    raw = item.get("process") if isinstance(item.get("process"), Mapping) else {}
    out: dict[str, Any] = dict(EMPTY_PROCESS)
    for key in PROCESS_COUNTS:
        value = raw.get(key)
        try:
            number = (int(value) if value is not None and str(value).strip() != ""
                      else None)
        except (TypeError, ValueError):
            number = None
        out[key] = number if number is None or number >= 0 else None
    seconds = raw.get("session_s")
    try:
        out["session_s"] = (round(float(seconds), 1)
                            if seconds is not None and str(seconds).strip() != ""
                            else None)
    except (TypeError, ValueError):
        out["session_s"] = None
    if out["session_s"] is not None and out["session_s"] < 0:
        out["session_s"] = None
    effort = raw.get("effort")
    if isinstance(effort, str) and effort.strip():
        out["effort"] = effort.strip()[:EFFORT_MAX]
    return out


def process_reported(process: Mapping[str, Any] | None) -> bool:
    """Whether anything at all is known about the process cost.

    An object of nothing but nulls is not recorded: the row would say the
    agent reported its cost, and it did not.
    """
    return bool(process) and any(value is not None for value in process.values())


def round_trip_s(packet_created_utc: Any, landed_utc: str | None = None) -> float | None:
    """Seconds from the packet being cut to its answer landing, or None.

    The node's own measurement of a paid step: it includes the model's
    thinking and the agent's handling, which is the honest wall time of work
    done off the node, and it needs nothing reported.
    """
    seconds = worker.minutes_between(str(packet_created_utc or ""), landed_utc or db.utc_now())
    return None if seconds is None else round(max(0.0, seconds * 60.0), 1)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


@dataclass
class ImportReport:
    step: str
    recorded: list[str] = field(default_factory=list)
    rejected: list[dict[str, str]] = field(default_factory=list)
    skipped: int = 0
    #: The jobs a plan or execute import put back on the queue.
    jobs: list[int] = field(default_factory=list)
    #: The model the packet was cut for, for the `then` line.
    model: str = ""
    #: `elapsed`, as `next` echoes it, for the job this import moved — with
    #: the over-budget line when there is one. Advisory: an import is never
    #: refused over a budget (DECIDE[agent-budget]).
    elapsed: dict[str, Any] | None = None

    def then(self) -> str | None:
        """The one command that follows this import, or None."""
        if self.step in JOB_STEPS and self.jobs:
            return next_command(self.jobs[0], self.model)
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "recorded": len(self.recorded),
            "landed": list(self.recorded),
            "rejected": [
                {"item": row["item"], "reason": row["reason"]} for row in self.rejected
            ],
            "skipped": self.skipped,
            "jobs": list(self.jobs),
            "then": self.then(),
            "elapsed": self.elapsed,
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

    envelope_model = str(packet.get("model") or "").strip()
    report = ImportReport(step=step, model=envelope_model)
    for item in items:
        if not isinstance(item, dict):
            continue
        if not adapter.answered(item):
            report.skipped += 1
            continue
        key = str(item.get("key") or "?")
        who = str(item.get("model") or envelope_model).strip()
        # The envelope's cut time travels with the item, so the adapter can
        # record how long this answer took to come back.
        item = dict(item, _exported_utc=packet.get("created_utc"))
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
            if step in JOB_STEPS:
                # Back on the queue, and the agent is waiting for the gate:
                # keep the worker on this job (db.paid_leases).
                try:
                    job_id = int((item.get("inputs") or {}).get("job"))
                except (TypeError, ValueError):
                    job_id = None
                if job_id is not None:
                    db.lease_paid(conn, job_id, who, DEFAULT_LEASE_MINUTES)
                    report.jobs.append(job_id)
                    landed_job = db.get_job(conn, job_id)
                    if landed_job is not None and report.elapsed is None:
                        # The attempt row for this answer is the worker's to
                        # write, after the gate; the count it carries is here
                        # now, so the line this import prints counts it.
                        report.elapsed = spend_of(
                            conn, landed_job,
                            extra_tokens=process_of(item).get("output_tokens"),
                        )
            continue
        report.rejected.append(
            {"item": key, "reason": reason, "answer": str(item.get("answer") or "")}
        )
        say(f"rejected {key}: {reason}")
    return report


# ---------------------------------------------------------------------------
# The agent's own cost: --since, the budget, and what checkout the node is on
# ---------------------------------------------------------------------------
#
# Packet 8 of docs/plans/agent-rig.md. The node cannot meter a session it did
# not start, so it asks for the one number the agent always has — when the
# work began — and prints an advisory budget against it. Never enforced
# (DECIDE[agent-budget]): refusing an over-budget import would reward not
# reporting, and the only thing worse than an expensive entry is an expensive
# entry whose cost is blank.


#: How far ahead of the node's clock a declared start may be. The agent runs
#: `date -u` on its own machine, and two clocks that agree to the minute are
#: as much as ssh and NTP promise; further ahead than this is a typo.
SINCE_SKEW_MINUTES = 2.0

#: And how far behind. A sketch is a few minutes' work; a `--since` from
#: yesterday is last session's stamp pasted into this one, which would make
#: every `elapsed` line nonsense and the budget unreadable.
SINCE_MAX_HOURS = 24.0

#: A `--note` is a line on the job and the entry page, not a document: what
#: the node cannot see about how this one was made. Entry 1279's would have
#: been `skill=algorithmic-art; local prototype`, 38 characters.
NOTE_MAX = 500


def parse_since(text: str | None, *, now: str | None = None) -> str | None:
    """A declared start, as this system's UTC stamp. Refuses a typo.

    Takes what `date -u +%FT%TZ` prints, and anything else
    :func:`datetime.fromisoformat` reads, converting an offset to UTC; a
    stamp with no zone is read as UTC, because the instruction that produced
    it says ``-u``.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise PaidRefused(
            f"--since {raw!r}: a UTC ISO stamp, as `date -u +%FT%TZ` prints it "
            "(2026-09-21T14:03:22Z)"
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    clock = (datetime.strptime(now, db.UTC_FORMAT).replace(tzinfo=timezone.utc)
             if now else datetime.now(timezone.utc))
    ahead = (moment - clock).total_seconds() / 60.0
    if ahead > SINCE_SKEW_MINUTES:
        raise PaidRefused(
            f"--since {raw!r} is {ahead:.0f} min ahead of this node's clock "
            f"({clock.strftime(db.UTC_FORMAT)}): a task cannot have begun in the "
            "future. Run `date -u +%FT%TZ` and pass what it prints"
        )
    if -ahead > SINCE_MAX_HOURS * 60.0:
        raise PaidRefused(
            f"--since {raw!r} is {-ahead / 60.0:.0f} h old, and a sketch is a few "
            "minutes' work: that is last session's stamp. Run `date -u +%FT%TZ` "
            "again"
        )
    return moment.strftime(db.UTC_FORMAT)


#: The `meta` row `paid budget` writes, and what it says when nobody has
#: (DECIDE[agent-budget]): job 1286's job phase alone was 4 min 26 s and
#: 14,798 tokens, so ten minutes and thirty thousand is that with headroom.
#: To be re-set when MEASURE[freenode-baseline] is read.
BUDGET_KEY = "paid_budget"
DEFAULT_BUDGET_MINUTES = 10.0
DEFAULT_BUDGET_TOKENS = 30000


def budget(conn: sqlite3.Connection) -> dict[str, Any]:
    """The advisory budget: minutes, generated tokens, and the try cap.

    Tries come from :data:`TRY_CAP_KEY`, where packet 7 put them, rather than
    being copied into this row: one number, one place, and `paid budget
    --tries N` writes it.
    """
    raw = db.get_meta(conn, BUDGET_KEY)
    try:
        found = json.loads(raw) if raw else {}
    except ValueError:
        found = {}
    if not isinstance(found, dict):
        found = {}

    def number(key: str, fallback: float | int) -> float | int:
        value = found.get(key)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return fallback
        return value if value > 0 else fallback

    return {
        "minutes": float(number("minutes", DEFAULT_BUDGET_MINUTES)),
        "tokens": int(number("tokens", DEFAULT_BUDGET_TOKENS)),
        "tries": try_cap(conn),
    }


def set_budget(
    conn: sqlite3.Connection,
    *,
    minutes: float | None = None,
    tokens: int | None = None,
    tries: int | None = None,
) -> dict[str, Any]:
    """Write the budget. Returns it as :func:`budget` reads it back.

    The verb rule 4 asks for: the two `meta` rows the paid flow reads have
    somewhere to be written from, so nobody opens sqlite3 on them.
    """
    for name, value in (("minutes", minutes), ("tokens", tokens), ("tries", tries)):
        if value is not None and float(value) <= 0:
            raise PaidRefused(f"--{name} {value}: a budget is a positive number")
    current = budget(conn)
    if minutes is not None or tokens is not None:
        db.set_meta(conn, BUDGET_KEY, json.dumps({
            "minutes": float(current["minutes"] if minutes is None else minutes),
            "tokens": int(current["tokens"] if tokens is None else tokens),
        }, sort_keys=True))
    if tries is not None:
        db.set_meta(conn, TRY_CAP_KEY, str(int(tries)))
    return budget(conn)


def budget_line(limits: Mapping[str, Any]) -> str:
    """The one line `start` prints, and every word of it is advisory."""
    return (f"budget: held within {float(limits['minutes']):.0f} min of --since, "
            f"under {int(limits['tokens']):,} tokens generated, "
            f"{int(limits['tries'])} tries · advisory")


def elapsed_of(conn: sqlite3.Connection, job: db.Job) -> dict[str, Any] | None:
    """How long the agent has been at ``job``, and which clock says so.

    ``--since`` when the agent declared one, else the lease's own start, which
    is when this job was first touched by an agent and is a floor on the real
    answer. Said which, because the two are different measurements: the first
    includes the reading and prototyping before the job existed, which is
    where job 1286's 37 minutes went, and the second cannot.
    """
    lease = db.paid_leases(conn).get(job.id) or {}
    if job.since_utc:
        started, source = job.since_utc, "--since"
    elif lease.get("since_utc"):
        started, source = str(lease["since_utc"]), "the lease"
    else:
        return None
    minutes = worker.minutes_between(started, db.utc_now())
    if minutes is None:
        return None
    minutes = max(0.0, minutes)
    return {
        "since_utc": started,
        "from": source,
        "minutes": round(minutes, 1),
        "s": round(minutes * 60.0),
        "say": f"elapsed {worker.human_gap(minutes * 60.0)}, from {source}",
    }


def tokens_generated(conn: sqlite3.Connection, job_id: int) -> int | None:
    """What the agent has reported generating on this job, or None.

    ``output_tokens`` only: a harness that reports thinking tokens reports
    them *inside* output (the transcript's ``output_tokens_details``), and
    adding the two would count the thinking twice.
    """
    total = None
    for attempt in db.list_attempts(conn, job_id):
        try:
            found = json.loads(attempt.process_json or "")
        except ValueError:
            continue
        value = found.get("output_tokens") if isinstance(found, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            total = value if total is None else total + value
    return total


def spend_of(conn: sqlite3.Connection, job: db.Job,
             *, extra_tokens: int | None = None) -> dict[str, Any] | None:
    """``elapsed``, as `next` and `import` echo it, with the budget beside it.

    ``over_budget`` is one line past either number and None otherwise. It is
    never a refusal: an agent that is late is asked to finish and say so.

    ``extra_tokens`` is the count on an answer that has just been imported and
    has no attempt row yet — the worker writes that row when it gates — so the
    line an import prints is about the reply the agent just sent, not the one
    before it.
    """
    elapsed = elapsed_of(conn, job)
    if elapsed is None:
        return None
    limits = budget(conn)
    tokens = tokens_generated(conn, job.id)
    if extra_tokens is not None:
        tokens = int(extra_tokens) + (tokens or 0)
    over: list[str] = []
    if elapsed["minutes"] > float(limits["minutes"]):
        over.append(f"{elapsed['minutes'] - float(limits['minutes']):.0f} min")
    if tokens is not None and tokens > int(limits["tokens"]):
        over.append(f"{tokens - int(limits['tokens']):,} tokens generated")
    row = dict(elapsed)
    row["tokens_generated"] = tokens
    row["budget"] = limits
    row["over_budget"] = (
        f"over budget by {' and '.join(over)}; finish, and report it" if over else None
    )
    return row


def node_commit() -> str | None:
    """The commit of the checkout this code is running from, or None.

    DECIDE[freshness]. An agent reads AGENTS.md in its own clone and drives a
    node that may be ahead of it: on 2026-09-21 a session read the file at
    #131 and answered packets the node had cut under #132, whose items carried
    a `usage` slot nothing had told it about. Null is a fine answer — a
    tarball deploy, no git, a checkout it cannot read — and it is never a
    failure: this is a line of information, not a check.
    """
    import subprocess

    root = Path(__file__).resolve().parent.parent
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = (out.stdout or "").strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return None
    return sha


def agents_md_sha256() -> str | None:
    """The digest of the AGENTS.md this node is running with, or None.

    The other half of the freshness check, for the agent whose clone has no
    common history with the node (a worktree, a tarball): the same file has
    the same digest, and a different one is a different file.
    """
    try:
        return sha256_file(Path(__file__).resolve().parent.parent / "AGENTS.md")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Preflight and wait: the harness an agent drives the flow with
# ---------------------------------------------------------------------------
#
# Both exist because of 2026-09-21. A Sonnet 5 session asked to make one
# sketch as itself spent its budget piecing together, by hand, whether the node
# could route its model at all (it could not: the only list of paid names was
# in two systemd units no shell can read) and then how long to wait for a
# worker it must not start. Neither question should need investigating.


#: The oldest schema the paid path runs on: 015 gave the job its `since_utc`
#: and `note` and the attempt its `process_json`, which `start` and `import`
#: write. Before it, a paid verb would fail on an unknown column rather than
#: say the node is behind, which is what this check is for.
MIN_SCHEMA = 15

#: States in which a job needs nothing from an agent and will move by itself.
MOVING = frozenset({"queued", "planning", "executing", "gating", "repairing"})


def _systemd_timer_active() -> bool:
    """Whether the worker runs in drip mode, where no worker is resident."""
    import subprocess

    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "sketchgen-worker.timer"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "active"


def _card_worker(conn: sqlite3.Connection) -> int | None:
    """The pid on the worker's open status-card step, if that pid is alive."""
    row = db.current_activity(conn)
    if row is None:
        return None
    try:
        pid = int(row["pid"])
        os.kill(pid, 0)
    except (TypeError, ValueError, ProcessLookupError):
        return None
    except PermissionError:
        pass  # alive, and not ours to signal: still a worker
    return pid


def preflight(
    conn: sqlite3.Connection,
    model: str,
    *,
    workers: Callable[[], list[str]] = worker.worker_processes,
    drip: Callable[[], bool] = _systemd_timer_active,
) -> dict[str, Any]:
    """Can ``model`` run a job end to end right now? Every check, and its fix.

    ``ready`` is true only when every check passes. Each failed check carries
    ``fix``: the one command that repairs it, and ``who`` may run it — ``you``
    for the one thing an agent may do for itself (register its own name, which
    is a name and not a key), ``operator`` for everything else, which the agent
    reports rather than attempts (AGENTS.md: report and stop).
    """
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, fix: str = "", who: str = "operator") -> None:
        row: dict[str, Any] = {"check": name, "ok": bool(ok), "detail": detail}
        if not ok:
            row["fix"] = fix
            row["who"] = who
        checks.append(row)

    try:
        model = _check_model(model)
        check("model id", True, model)
    except PaidRefused as exc:
        check("model id", False, str(exc), "name your own exact model id with --as",
              who="you")
        return {"ready": False, "model": model, "checks": checks, "info": {}}

    version = db.schema_version(conn)
    check("schema", version >= MIN_SCHEMA, f"schema {version}, needs {MIN_SCHEMA}",
          "deploy main to the node: bash ~/sketchgen/app/update.sh")

    registered = models.is_paid(model, conn)
    check(
        "registered",
        registered,
        f"{model} is {'' if registered else 'not '}a registered paid model — "
        + ("the worker parks its steps for you" if registered
           else "the worker would send it to Ollama, which answers 404"),
        f"sketchgen paid models add {model}",
        who="you",
    )

    running = workers()
    card = _card_worker(conn)
    if len(running) == 1:
        check("worker", True, "one resident worker; it claims a queued job within ~30 s")
    elif not running and card is not None:
        # /proc showed no worker argv but the status card has a step open under
        # a pid that is alive: the console's own liveness test, and the one
        # that holds where /proc is not this machine's (a container, a test).
        check("worker", True, f"one resident worker (status card, pid {card}); "
                              "it claims a queued job within ~30 s")
    elif not running and drip():
        check("worker", True, "drip mode: the timer starts a worker every 5 minutes")
    elif not running:
        check("worker", False, "no worker is running, so nothing will claim the job",
              "systemctl --user start sketchgen-worker.service")
    else:
        check("worker", False, f"{len(running)} workers are running; there must be one",
              "stop the extra worker (see AGENTS.md rule 1); never start another")

    control = db.get_control(conn)
    state = control.state if control is not None else "running"
    check("generator", state == "running",
          f"control is {state}"
          + (f" ({control.reason})" if control is not None and control.reason else ""),
          "sketchgen control resume")

    queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE state = 'queued'").fetchone()[0]
    info = {
        "assignment": db.get_assignment(conn),
        # DECIDE[freshness]: which checkout the node is running, so an agent
        # can tell whether the file it read is the file the node cut its
        # packets under. Both may be null and neither is a check.
        "node_commit": node_commit(),
        "agents_md_sha256": agents_md_sha256(),
        "budget": budget(conn),
        "queued_ahead": int(queued),
        "waiting": {step: row.get("count") for step, row in waiting(conn).items()},
        "parked": parked_jobs(conn, model),
        "leases": {str(k): v for k, v in sorted(db.paid_leases(conn).items())},
        "worker_now": worker_now(conn),
    }
    return {
        "ready": all(row["ok"] for row in checks),
        "model": model,
        "checks": checks,
        "info": info,
    }


def next_command(job_id: int, model: str) -> str:
    return f"sketchgen paid next --job {int(job_id)} --as {model}"


def worker_now(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """What the resident worker is doing this second, from its status card.

    The answer to "why is my job still queued": the worker is on job 1245,
    planning, since 05:41. An agent that can read this waits; one that cannot
    starts investigating, and AGENTS.md rule 1 is about where that ends.
    """
    row = db.current_activity(conn)
    if row is None:
        return None
    return {
        "step": row["step"],
        "headline": row["headline"],
        "detail": row["detail"],
        "job": row["job_id"],
        "model": row["model"],
        "since_utc": row["started_utc"],
    }


def parked_jobs(conn: sqlite3.Connection, model: str | None = None) -> list[dict[str, Any]]:
    """Every job parked for a plan or an attempt, and what to do about each.

    ``yours`` is whether the parked step is ``model``'s to answer. A job that
    is yours and has no live lease is one an earlier session of yours left
    behind: answer it (`paid next`) or hand it back (`paid release`). A job
    leased to another agent has no command: it is theirs, being driven. A job
    parked for another paid model that nobody holds names that model's `next`
    as ``command``, so an operator reading the preflight knows which agent to
    bring — the other half of a split job (#134) — and ``release`` is the way
    to hand it to this node's models instead, which the worker does itself
    once it has sat a lease's length with no lease (``Worker.sweep_unattended``).
    """
    leases = db.paid_leases(conn)
    rows = []
    for job in db.list_jobs(conn, "needs-laptop"):
        if job.needs not in JOB_STEPS:
            continue
        who = job.planner if job.needs == "plan" else job.executor
        lease = leases.get(job.id)
        yours = bool(model) and who in (model, models.PAID)
        release_cmd = f"sketchgen paid release --job {job.id}"
        if yours:
            command = next_command(job.id, model)
        elif lease and lease.get("model") != model:
            command = None
        elif who and who != models.PAID and models.is_paid(who, conn):
            command = next_command(job.id, who)
        else:
            command = release_cmd
        rows.append({
            "job": job.id,
            "needs": job.needs,
            "model": who,
            "planner": job.planner,
            "executor": job.executor,
            "since_utc": job.updated_utc,
            "yours": yours,
            "leased_to": lease.get("model") if lease else None,
            "lease_until": lease.get("until_utc") if lease else None,
            # Someone else's live lease means an agent is driving it: the
            # command is none. Until 2026-09-21 this said `paid release` for
            # any job not yours, and the preflight printed "no agent" beside
            # gemini-3.8-flash's live lease on job 1263 — an invitation to
            # take a job out from under an agent still answering it.
            "command": command,
            "release": release_cmd,
        })
    return rows


# ---------------------------------------------------------------------------
# start, next, try, import, release: the verbs an agent's whole job is made of
# ---------------------------------------------------------------------------
#
# 2026-09-21, second Sonnet 5 session. The recipe was preflight, enqueue,
# wait, export, answer, import, wait, … — six verbs and a shell function that
# quoted the prompt twice. The session got the first four right and then sat
# in `wait` for ten minutes (the tool's own timeout) behind an idle-spawned job
# whose planner was timing out, and was killed. Nothing was wrong with the
# node. What was wrong: `wait` blocked longer than an agent's tool call may;
# nothing told the worker an agent was waiting; and there were too many verbs
# between "I want a sketch" and "here is the packet". `start` and `next` are
# the same flow with the seams removed: one verb to begin, one verb to ask
# "what now", `import` to answer, and a lease so the worker knows.


def _check_by(by: str) -> str:
    text = (by or "").strip()
    if not lineage.USERNAME_RE.match(text):
        raise PaidRefused(f"--by {by!r}: a GitHub username, nothing else")
    return text


def _job_model(word: str | None, model: str, column: str,
               conn: sqlite3.Connection | None = None) -> str:
    """What ``--planner`` / ``--executor`` on `paid start` may say.

    Blank means the agent itself. ``local`` means this node's default. An
    Ollama tag (it has a colon) is that model on the node. A **registered**
    paid id is another agent: the job is handed to it when its step comes
    (:func:`next_for` answers ``handoff``), which is how an operator gets, say,
    a Sonnet plan and an Opus attempt from off the node — the same split the
    New job page gives two local models. Until 2026-09-21 this refused any
    paid id but the caller's, and a Sonnet 5 session asked to queue a job with
    Opus as executor rightly stopped rather than work around it.

    The other id must already be registered (``paid models add``), by the
    operator: it is the exact name the second agent has to answer as, and an
    agent guessing another model's id — that session did not know it, and
    said so — would park the job for a name nobody comes for.
    """
    text = (word or "").strip()
    if not text or text == model:
        return model
    if text == "local" or ":" in text:
        return text
    if text != models.PAID and models.is_paid(text, conn):
        return text
    known = [name for name in models.paid_models(conn) if name != model]
    raise PaidRefused(
        f"--{column} {text!r}: yourself ({model}), `local`, an Ollama tag "
        f"(name:tag), or a registered paid model"
        + (f" ({', '.join(known)})" if known else " (none other is registered)")
        + f". The operator registers one with: sketchgen paid models add {text}"
    )


def partner_of(model: str, planner: str | None, executor: str | None,
               conn: sqlite3.Connection | None = None) -> tuple[str, str] | None:
    """The other agent on a job split between two paid models, and its step.

    ``None`` when ``model`` answers every paid step of the job. Read by `start`
    to say up front that a handoff is coming, and by `next` to tell the two
    apart.
    """
    for column, step in ((planner, "plan"), (executor, "execute")):
        if column and column not in (model, models.PAID) and models.is_paid(column, conn):
            return column, step
    return None


def whose_turn(job: db.Job) -> tuple[str | None, str | None]:
    """The model the job is at, or heading to, and that model's step.

    A job that is queued with no brief goes to its planner next; with one, to
    its executor. Parked or moving, the state says. ``(None, None)`` for a job
    that is finished or parked for a person.
    """
    if job.state == "needs-laptop":
        if job.needs == "plan":
            return job.planner, "plan"
        if job.needs == "execute":
            return job.executor, "execute"
        return None, None
    if job.state in MOVING:
        planning = job.state == "planning" or (
            job.state == "queued" and not (job.brief and job.assertions))
        return (job.planner, "plan") if planning else (job.executor, "execute")
    return None, None


def start(
    conn: sqlite3.Connection,
    *,
    model: str,
    prompt: str,
    by: str,
    planner: str | None = None,
    executor: str | None = None,
    rules_file: str | None = None,
    max_attempts: int = 3,
    publication: str = "hold",
    since: str | None = None,
    note: str | None = None,
    workers: Callable[[], list[str]] = worker.worker_processes,
    drip: Callable[[], bool] = _systemd_timer_active,
) -> dict[str, Any]:
    """One job, made by an agent, for the agent: register, preflight, queue, lease.

    Returns ``{"started": True, "job": N, "then": …}`` or, when the preflight
    is not ready, ``{"started": False, "preflight": …}`` with nothing queued —
    the CLI exits 3 and prints the checks, and the stop rule applies.

    Registration happens here rather than being the agent's one `fix (you)`,
    because it is a name and not a key and there is no reason to make an agent
    run two commands to say who it is. It is reported, so the operator can
    `paid models remove` it.

    ``since`` is what `date -u +%FT%TZ` printed when the agent began — the
    first command AGENTS.md asks for — and ``note`` is anything about how this
    job is being made that the node cannot see (a skill, a local prototype).
    Both are refused before anything is queued, so a typo costs nothing.
    """
    model = _check_model(model)
    by = _check_by(by)
    since_utc = parse_since(since)
    note_text = str(note or "").strip() or None
    if note_text and len(note_text) > NOTE_MAX:
        raise PaidRefused(
            f"--note is {len(note_text)} characters and the limit is {NOTE_MAX}: "
            "a note is a line about how this job was made (`skill=algorithmic-art; "
            "local prototype`), not the story of it"
        )
    text = (prompt or "").strip()
    if not text:
        raise PaidRefused("a job needs a prompt: --prompt TEXT, or the prompt on stdin")
    planner_col = _job_model(planner, model, "planner", conn)
    executor_col = _job_model(executor, model, "executor", conn)
    if planner_col != model and executor_col != model:
        raise PaidRefused(
            f"neither step is yours: --planner {planner_col} --executor "
            f"{executor_col}. Use `sketchgen enqueue` for a job this node runs, "
            "or have the agent that answers one of the steps start it."
        )
    partner = partner_of(model, planner_col, executor_col, conn)
    registered_now = False
    if not models.is_paid(model, conn):
        db.set_paid_models(conn, db.get_paid_models(conn) + [model])
        registered_now = True
    report = preflight(conn, model, workers=workers, drip=drip)
    if not report["ready"]:
        return {"started": False, "model": model, "registered_now": registered_now,
                "preflight": report}
    options: dict[str, Any] = {
        "planner": planner_col,
        "executor": executor_col,
        "max_attempts": int(max_attempts),
        "publication": publication,
        # Declared, never inferred: the node's clock cannot see a session it
        # did not start (migration 015).
        "since_utc": since_utc,
        "note": note_text,
    }
    if rules_file:
        options["rules_file"] = rules_file
    job_id = db.enqueue(conn, text, by, **options)
    lease = db.lease_paid(conn, job_id, model, DEFAULT_LEASE_MINUTES)
    if partner is None:
        say = (f"job {job_id} is queued for {model}; the worker claims it on its "
               "next pass and parks it for your plan")
    elif partner[1] == "execute":
        say = (f"job {job_id} is queued; you plan, then `next` hands it to "
               f"{partner[0]} for the attempt — a session that is that model runs "
               f"`{next_command(job_id, partner[0])}`")
    else:
        say = (f"job {job_id} is queued for {partner[0]} to plan first — a session "
               f"that is that model runs `{next_command(job_id, partner[0])}`; "
               f"your `next` waits until the attempt is yours")
    limits = budget(conn)
    return {
        "started": True,
        "job": job_id,
        "model": model,
        "planner": planner_col,
        "executor": executor_col,
        "since_utc": since_utc,
        "note": note_text,
        # Advisory, and said so in the line itself. An agent with no number to
        # hold itself to spent 37 minutes on entry 1279 without noticing.
        "budget": limits,
        "budget_say": budget_line(limits),
        # DECIDE[freshness], both halves, so an agent that only ever runs
        # `start` is told which checkout it is driving.
        "node_commit": report["info"].get("node_commit"),
        "agents_md_sha256": report["info"].get("agents_md_sha256"),
        "partner": partner[0] if partner else None,
        "partner_step": partner[1] if partner else None,
        "registered_now": registered_now,
        "lease_until": lease["until_utc"],
        "worker_now": report["info"].get("worker_now"),
        "queued_ahead": report["info"].get("queued_ahead"),
        "then": next_command(job_id, model),
        "say": say,
    }


def next_for(
    conn: sqlite3.Connection,
    job_id: int,
    model: str,
    *,
    timeout: float = 240.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    ctx: Context | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """What the agent does next about its job — and, if it is a packet, the packet.

    Read-only apart from the lease, which every poll renews. Returns one object
    with ``do``:

    - ``answer``: the object *is* the export packet for the step the job is
      parked at, cut for this job only, with ``then`` naming the import.
    - ``wait``: ``timeout`` passed while the worker had it; ``worker`` says what
      the worker is doing and ``then`` is this same command. Not an error: run
      it again. The timeout is shorter than an agent's tool call on purpose.
    - ``done``: held (``entry``), published, failed or rejected. The lease is
      dropped.
    - ``handoff``: the job is split between two paid models and the rest of it
      is the other one's (``to``, ``step``); ``then`` is that agent's command
      and ``release`` the operator's if no such agent is coming. This agent is
      finished with the job. Exit 0.
    - ``stop``: the job is parked for a person, the generator is paused, or
      another agent holds the lease. Exit 3 at the CLI: report and stop.

    ``wait`` also covers the executor of a split job while the other agent
    plans (``waiting_on``); that wait leaves the planner's lease alone.

    ``progress`` gets one line every 30 s of waiting, for stderr, so a tool
    that shows partial output shows life.
    """
    import time

    model = _check_model(model)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    base_ctx = ctx or Context()
    ctx = Context(jobs_dir=base_ctx.jobs_dir, lineage_depth=base_ctx.lineage_depth,
                  job=int(job_id))
    started = clock()
    last_said = started
    while True:
        job = db.get_job(conn, job_id)
        if job is None:
            raise PaidRefused(f"there is no job {job_id}")
        entry = conn.execute("SELECT id FROM entries WHERE job_id = ?", (job_id,)).fetchone()
        step = next_step(
            job, int(entry["id"]) if entry else None,
            # Dossier §7.2: `done` carries what the gate found, so the agent
            # is told whether its sketch passed rather than inferring it.
            verdict=(kept_verdict(conn, job_id)
                     if job.state in ("held", "published") else None),
        )
        waited = round(clock() - started, 1)
        base: dict[str, Any] = {
            "job": job.id, "state": job.state, "needs": job.needs, "model": model,
            "attempts": len(db.list_attempts(conn, job_id)),
            "max_attempts": job.max_attempts, "waited_s": waited,
            # How long this has been going on, from `--since` if the agent
            # declared one and the lease if not, said which; and one line when
            # it is past the budget. Never a refusal (DECIDE[agent-budget]).
            "elapsed": spend_of(conn, job),
        }
        if step["do"] in ("done", "stop"):
            if step["do"] == "done":
                db.release_lease(conn, job_id)
            return {**base, **step}
        lease = db.paid_leases(conn).get(job.id)
        turn, turn_step = whose_turn(job)
        theirs = (turn is not None and turn not in (model, models.PAID)
                  and models.is_paid(turn, conn))
        if theirs and turn_step == "plan" and model == job.executor:
            # A job split two ways, and the other agent goes first: the plan is
            # theirs, the attempt is this agent's. Wait, and do not touch the
            # lease — it is the planner's while the job is at the plan.
            control = db.get_control(conn)
            if control is not None and control.state == "paused" and job.state == "queued":
                return {**base, "do": "stop", "waiting_on": turn,
                        "say": f"job {job.id} is queued for {turn} to plan but the "
                               "generator is paused; it will not move until the "
                               "operator resumes it"}
            if waited >= timeout:
                return {**base, "do": "wait", "timed_out": True, "waiting_on": turn,
                        "then": next_command(job_id, model),
                        "say": f"job {job.id} is waiting for {turn} to plan "
                               f"(state {job.state}); the attempt is yours after; "
                               "run the same command again"}
            if progress is not None and clock() - last_said >= 30:
                last_said = clock()
                progress(f"{waited:.0f} s: job {job.id} is {job.state}, waiting for "
                         f"{turn} to plan")
            sleep(interval)
            continue
        if theirs:
            # The job is at, or heading to, a step another agent answers, and
            # nothing of it comes back to this one: either its plan is in and
            # the attempts are the executor's, or it is not on this job at all.
            # Drop this agent's lease — nobody is present for the job until the
            # other one runs `next` — and say exactly who that is.
            if lease and lease.get("model") == model:
                db.release_lease(conn, job_id)
            mine = model in (job.planner, job.executor)
            return {
                **base, "do": "handoff", "to": turn, "step": turn_step,
                "then": next_command(job_id, turn),
                "release": f"sketchgen paid release --job {job_id}",
                "say": (
                    (f"your plan for job {job.id} is in; " if mine
                     else f"job {job.id} is not yours; ")
                    + f"the {turn_step} is {turn}'s. A session that is that model "
                    f"runs `{next_command(job_id, turn)}` — within "
                    f"{DEFAULT_LEASE_MINUTES:.0f} minutes of the job parking, or the "
                    "worker hands it to this node's models; sooner, "
                    f"`sketchgen paid release --job {job_id}` does the same. "
                    "You are finished with it."
                ),
            }
        if (lease and lease.get("model") != model and job.state != "held"
                and lease.get("model") not in (job.planner, job.executor)):
            # Leased to someone who is not one of this job's two models — an
            # earlier lease by the other agent on a split job is taken over
            # below, since the step is this agent's now.
            return {**base, "do": "stop", "leased_to": lease.get("model"),
                    "say": f"job {job.id} is leased to {lease.get('model')} until "
                           f"{lease.get('until_utc')}: it is theirs, not yours"}
        if model not in (job.planner, job.executor) and job.state != "held":
            # Handed back while the agent was away (Worker.sweep_unattended, or
            # an operator's `release`): without this, the agent that returns
            # waits on the local run, renews a lease that stands the idle loop
            # down for a job that is no longer its own, and reports an entry it
            # did not make.
            return {**base, "do": "stop",
                    "say": f"job {job.id} is not yours any more: "
                           f"{job.last_error or 'its paid steps were handed back'}"}
        if step["do"] == "answer":
            packet = export_packet(conn, step["step"], model=model, ctx=ctx)
            if packet["items"]:
                item = packet["items"][0]
                packet.update(base)
                packet["do"] = "answer"
                packet["attempt"] = (item.get("inputs") or {}).get("attempt")
                packet["then"] = "sketchgen paid import -"
                packet["say"] = (
                    f"job {job.id} is waiting for your {step['step']}"
                    + (f" (attempt {packet['attempt']} of {job.max_attempts})"
                       if packet["attempt"] else "")
                    + ": answer items[0] and import the packet"
                )
                return packet
            # Parked, but nothing to offer: the prompt file is missing or the
            # job moved under us. Say so rather than spin.
            return {**base, "do": "stop",
                    "say": f"job {job.id} is parked for {step['step']} but nothing "
                           "can be exported for it; report this"}
        # The worker has it. Say so, keep the lease warm, and look again.
        db.lease_paid(conn, job_id, model, DEFAULT_LEASE_MINUTES)
        control = db.get_control(conn)
        if control is not None and control.state == "paused" and job.state == "queued":
            return {**base, "do": "stop",
                    "say": f"job {job.id} is queued but the generator is paused"
                           f"{' (' + control.reason + ')' if control.reason else ''}; "
                           "it will not move until the operator resumes it"}
        now = worker_now(conn)
        if waited >= timeout:
            return {**base, "do": "wait", "timed_out": True, "worker": now,
                    "then": next_command(job_id, model),
                    "say": f"job {job.id} is still {job.state} after {waited:.0f} s"
                           + _worker_sentence(now, job.id)
                           + "; run the same command again"}
        if progress is not None and clock() - last_said >= 30:
            last_said = clock()
            progress(f"{waited:.0f} s: job {job.id} is {job.state}"
                     + _worker_sentence(now, job.id))
        sleep(interval)


def _worker_sentence(now: dict[str, Any] | None, job_id: int) -> str:
    if not now:
        return "; the worker has written no status"
    where = now.get("headline") or now.get("step") or "busy"
    if now.get("job") and int(now["job"]) != int(job_id):
        return (f"; the worker is on job {now['job']} ({where}, since "
                f"{now.get('since_utc')}) and takes yours next")
    if now.get("job"):
        return f"; the worker is on it ({where}, since {now.get('since_utc')})"
    return f"; the worker: {where} (since {now.get('since_utc')})"


#: Tries per job before `try` refuses (DECIDE[try-budget]). A gate run is
#: 5–15 s of the node's CPU; eight is a couple of minutes, and an agent that
#: needs more is designing on the node's clock. Read from a `meta` row so it
#: can change without a deploy; the verb that writes it is Packet 8's
#: `paid budget --tries N`, because rule 4 means no row is edited by hand.
DEFAULT_TRY_CAP = 8
TRY_CAP_KEY = "paid_try_cap"


def try_cap(conn: sqlite3.Connection) -> int:
    """How many tries one job may ask for, from ``meta`` or the default."""
    try:
        value = int(str(db.get_meta(conn, TRY_CAP_KEY, "") or "").strip())
    except (TypeError, ValueError):
        return DEFAULT_TRY_CAP
    return value if value > 0 else DEFAULT_TRY_CAP


def try_command(job_id: int, model: str) -> str:
    return f"sketchgen paid try --job {int(job_id)} --as {model}"


def try_for(
    conn: sqlite3.Connection,
    job_id: int,
    model: str,
    answer: str,
    *,
    timeout: float = 240.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    ctx: Context | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """The node's own gate, run over a candidate that is not an attempt yet.

    ``answer`` is the text the agent would have put in ``items[0].answer``:
    the fenced ``js`` block, an optional ``html`` block and the statement. Not
    a bare ``sketch.js``, on purpose — a try then exercises
    :func:`executor.parse_response` too, so a reply that would be rejected at
    ``import`` is rejected here, for free, before it reaches the worker.

    Returns one object with ``do``:

    - ``verdict``: the gate ran. :func:`verdict_summary`'s shape, plus the
      node paths of the strip and the still, ``tries_used``/``tries_cap``, and
      ``then`` — the import on a clean pass, another try otherwise.
    - ``rejected``: the reply does not parse. Exit 0, nothing written, no try
      spent; the reason is the one ``import`` would have given.
    - ``wait``: ``timeout`` passed before the worker served it. Not an error:
      run the same command again.
    - ``stop``: exit 3. No lease, someone else's lease, or the cap is spent.

    The lease is the whole permission model — no new column says who may try —
    and this renews it, since a try is the agent's own activity. The verdict
    is **advisory**: it writes no attempt, no entry and no transition, and the
    gate the worker runs on the imported attempt is the one that counts.
    Dossier 01 §5.3 is the argument; job 1286 (entry 1279, 2026-09-21) is the
    run that made it, where the agent spent 37 minutes building a browser rig
    that still read a third under the node's own number.
    """
    import time

    model = _check_model(model)
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    ctx = ctx or Context()
    job = db.get_job(conn, job_id)
    if job is None:
        raise PaidRefused(f"there is no job {job_id}")
    cap = try_cap(conn)
    used = int((db.paid_tries(conn).get(job.id) or {}).get("count") or 0)
    base: dict[str, Any] = {"job": job.id, "state": job.state, "model": model,
                            "tries_used": used, "tries_cap": cap}

    lease = db.paid_leases(conn).get(job.id)
    if lease is None or lease.get("model") != model:
        holder = lease.get("model") if lease else None
        return {**base, "do": "stop", "leased_to": holder,
                "say": (f"job {job.id} is leased to {holder} until "
                        f"{lease.get('until_utc')}: it is theirs, not yours"
                        if holder else
                        f"you hold no lease on job {job.id}: `"
                        f"{next_command(job.id, model)}` takes one if the job "
                        "is yours, and a try needs one")}

    parsed = executor.parse_response(answer or "")
    if parsed.js is None or not parsed.js.strip():
        # The words `import` uses, and the same bargain: nothing is written,
        # no try is spent, and the reply comes back to be fixed.
        return {**base, "do": "rejected",
                "reason": "no fenced js block",
                "then": try_command(job.id, model),
                "say": f"no fenced js block; nothing was written on the node "
                       f"and job {job.id} is untouched. Fix the reply and try again"}

    if used >= cap:
        return {**base, "do": "stop",
                "say": f"{used} of {cap} tries used on job {job.id}; import an "
                       "attempt (`sketchgen paid import -`), or ask the operator "
                       f"to raise `{TRY_CAP_KEY}`"}

    k = used + 1
    try_dir = ctx.jobs_dir / str(job.id) / f"try-{k}"
    try:
        try_dir.mkdir(parents=True, exist_ok=True)
        # The reply lands before the request does: the worker must never find
        # a request whose reply is not on disk yet.
        (try_dir / worker.TRY_REPLY).write_text(answer, encoding="utf-8")
    except OSError as exc:
        raise PaidRefused(f"cannot write try {k} of job {job.id}: {exc}") from exc
    db.add_paid_try(conn, job.id, model, k, str(try_dir))
    db.lease_paid(conn, job.id, model, DEFAULT_LEASE_MINUTES)
    base["tries_used"] = k
    result_path = try_dir / worker.TRY_RESULT

    started = clock()
    last_said = started
    while True:
        verdict = _read_try_result(result_path)
        waited = round(clock() - started, 1)
        if verdict is not None:
            clean = verdict.get("exit") == 0 and not verdict.get("offplan")
            failing = ", ".join(
                [name for name, value in (verdict.get("checks") or {}).items()
                 if value is False]
                + [f"{name} (assertion)" for name in verdict.get("offplan") or []]
            )
            return {
                **base, "do": "verdict", "waited_s": waited, **verdict,
                "then": ("sketchgen paid import -" if clean
                         else try_command(job.id, model)),
                "say": (f"job {job.id} try {k} of {cap}: {verdict.get('say')}"
                        + ("; this is what an import would be gated on"
                           if clean else f"; fix {failing or 'what the gate named'} "
                           "and try again")),
            }
        # Every poll renews the lease, as `next` does: the agent is here.
        db.lease_paid(conn, job.id, model, DEFAULT_LEASE_MINUTES)
        pending = [row for row in (db.paid_tries(conn).get(job.id) or {}).get("pending") or []
                   if int(row.get("k") or 0) == k]
        # Read the verdict again before concluding there is none: the worker
        # writes result.json and drops the request in that order, and this
        # poll can land between the two.
        if not pending and _read_try_result(result_path) is None:
            # Dropped, then — because the lease had lapsed by the time the
            # worker reached it. Nobody is bringing a verdict for this one.
            return {**base, "do": "stop", "waited_s": waited,
                    "then": try_command(job.id, model),
                    "say": f"job {job.id} try {k} was dropped by the worker "
                           "before it ran — your lease had lapsed. `"
                           f"{next_command(job.id, model)}` first, then try again"}
        if waited >= timeout:
            return {**base, "do": "wait", "timed_out": True, "waited_s": waited,
                    "worker": worker_now(conn),
                    "then": try_command(job.id, model),
                    "say": f"job {job.id} try {k} is still waiting for the gate "
                           f"after {waited:.0f} s"
                           + _worker_sentence(worker_now(conn), job.id)
                           + "; run the same command again"}
        if progress is not None and clock() - last_said >= 30:
            last_said = clock()
            progress(f"{waited:.0f} s: job {job.id} try {k} is with the worker"
                     + _worker_sentence(worker_now(conn), job.id))
        sleep(interval)


def _read_try_result(path: Path) -> dict[str, Any] | None:
    """The verdict in a try directory, or None until there is a whole one."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict) or found.get("kind") != worker.TRY_KIND:
        return None
    return found


def release_job(
    conn: sqlite3.Connection, job_id: int, *, by: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Hand a parked job to this node's models: the local path finishes it.

    Only a job at ``needs-laptop`` for a plan or an attempt. Whichever of its
    two model columns is paid is blanked — the worker then reads the
    assignment, else its default — and the job goes back on the queue with
    ``last_error`` saying who handed it back and why. Provenance stays honest:
    nobody had answered, and the model that does answer is the one recorded.
    The lease, if any, is dropped.

    This is how an agent leaves a job it cannot finish, and how an operator
    clears a job parked for an agent that is not coming (job 1246, whose
    session was killed on 2026-09-21; job 1252, spawned for a model that was
    never there).
    """
    job = db.get_job(conn, job_id)
    if job is None:
        raise PaidRefused(f"there is no job {job_id}")
    if job.state != "needs-laptop" or job.needs not in JOB_STEPS:
        raise PaidRefused(
            f"job {job_id} is {job.state}"
            + (f" (needs {job.needs})" if job.needs else "")
            + ": only a job parked for a plan or an attempt can be handed back"
        )
    was = {"planner": job.planner, "executor": job.executor}
    fields: dict[str, Any] = {"needs": None}
    for column in ("planner", "executor"):
        if models.is_paid(getattr(job, column), conn):
            fields[column] = None
    who = f" by {by}" if by else ""
    why = f": {reason}" if reason else ""
    fields["last_error"] = f"handed to the local path{who}{why}"
    db.transition(conn, job_id, "queued", **fields)
    db.release_lease(conn, job_id)
    after = db.get_job(conn, job_id)
    return {
        "job": job_id,
        "state": after.state,
        "was": was,
        "now": {"planner": after.planner, "executor": after.executor},
        "say": (f"job {job_id} is back on the queue for this node's models; "
                f"the worker claims it on its next pass"),
    }


#: How many console lines a verdict carries. The evidence the worker builds
#: for a failed attempt keeps ten (worker.MAX_CONSOLE_LINES); a try is read by
#: an agent that is debugging rather than repairing, so it gets twenty, and
#: ``console_total`` says how many there were.
VERDICT_CONSOLE_LINES = 20


def verdict_summary(report: Mapping[str, Any] | None,
                    lines: int = VERDICT_CONSOLE_LINES) -> dict[str, Any]:
    """One gate report, as the shape an agent reads.

    Built once and used twice (docs/plans/agent-rig.md §4.4): for the verdict
    a `paid try` returns, and for the ``verdict`` on `next`'s ``done``. An
    agent that learns to read a try has learnt to read the end of its job.

    Everything here is the gate's own: ``exit``, the QA ``checks``, each
    assertion with the detail it gave, ``timings.ms_per_frame`` — the number
    that decides the frame budget, and the one no laptop proxy can produce
    (entry 1279: 9.8 ms on the node against 6.4 ms local) — the console,
    the resources the sketch asked for and did not get, the notes, and the
    node paths of ``strip.png`` and ``gate.png``, to be fetched with the same
    ``scp`` the judge steps use.
    """
    if not report:
        return {"exit": None, "checks": {}, "assertions": {}, "timings": {},
                "console": [], "console_total": 0, "resources": [], "notes": [],
                "artefacts": {}, "offplan": [],
                "say": "the gate wrote no report"}
    assertions = {
        name: {"pass": bool((value or {}).get("pass")),
               "detail": (value or {}).get("detail")}
        for name, value in (report.get("assertions") or {}).items()
    }
    console = list(report.get("console") or [])
    # Only a sketch that RAN can be off-plan, for _create_entry's reason: one
    # that threw or froze missed its assertions of course, and calling that a
    # divergence would put "this sketch runs" beside one that does not.
    missed = worker.missed_assertions(report) if worker.qa_clean(report) else []
    failed = [name for name, value in (report.get("checks") or {}).items()
              if value is False]
    code = report.get("exit")
    if code == 0 and not missed:
        say = "clean: every check and every assertion passed"
    elif code == 0:
        say = "it runs, and missed " + ", ".join(missed)
    else:
        say = "failed on " + ", ".join(failed + [f"{n} (assertion)" for n in missed]
                                       or ["the gate's own exit"])
    return {
        "exit": code,
        "checks": dict(report.get("checks") or {}),
        "assertions": assertions,
        "timings": dict(report.get("timings") or {}),
        "console": console[:max(0, int(lines))],
        "console_total": len(console),
        "resources": list(report.get("resources") or []),
        "notes": list(report.get("notes") or []),
        "artefacts": dict(report.get("artefacts") or {}),
        "offplan": missed,
        "say": say,
    }


def kept_verdict(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    """What the gate found in the attempt the entry kept, or None.

    The kept attempt is the one :meth:`sketchgen.worker.Worker._create_entry`
    shows, which on a clean pass is the last one and on an off-plan hold need
    not be — so the ranking is the worker's own (:func:`worker.best_attempt`),
    not a re-derivation of it.
    """
    attempts = db.list_attempts(conn, job_id)
    kept = worker.best_attempt(attempts)
    report = worker.attempt_report(kept) if kept is not None else {}
    if not report:
        return None
    return {"attempt": int(kept.n), "attempts": len(attempts),
            **verdict_summary(report)}


def next_step(job: db.Job, entry_id: int | None = None, *,
              verdict: dict[str, Any] | None = None) -> dict[str, Any]:
    """What an agent does next about ``job``: a command, wait, or stop.

    ``verdict`` is what the gate found in the attempt the entry kept
    (:func:`kept_verdict`), carried on ``done``. Before 2026-09-21 ``done``
    said only *held as entry N*, and ``held`` is reached both by a clean pass
    and by a sketch that ran cleanly and missed an assertion once the attempts
    were spent (gate/README.md): the agent driving job 1286 inferred its clean
    pass from ``attempts: 1``, which is a guess that happened to be right.
    """
    if job.state in MOVING:
        return {"do": "wait", "say": f"job {job.id} is {job.state}; the worker has it"}
    if job.state == "needs-laptop" and job.needs in ("plan", "execute"):
        return {
            "do": "answer",
            "step": job.needs,
            "command": f"sketchgen paid export --step {job.needs} --job {job.id} --out -",
            "say": f"job {job.id} is waiting for your {job.needs}",
        }
    if job.state == "needs-laptop":
        return {"do": "stop", "say": f"job {job.id} needs {job.needs or 'a person'}: "
                                     "that is the operator's, not yours"}
    if job.state in ("held", "published"):
        return {"do": "done", "entry": entry_id, "verdict": verdict,
                "say": f"job {job.id} is {job.state}"
                       + (f" as entry {entry_id}" if entry_id else "")
                       + ("; a person publishes it" if job.state == "held" else "")
                       + (f"; the gate: {verdict['say']}" if verdict else "")}
    return {"do": "done", "say": f"job {job.id} {job.state}: {job.last_error or 'no reason recorded'}"}


def wait_for(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    timeout: float = 1800.0,
    interval: float = 5.0,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Block until ``job_id`` needs something, is finished, or cannot move.

    This is the whole of "wait, don't act" as a verb: it only reads. It returns
    when the job is parked for a step an agent answers, when it is held,
    published, failed or rejected, when it is parked for a person, or — so a
    wait never outlives the reason it would end — when the generator is paused
    and the job is queued behind the pause.
    """
    import time

    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    started = clock()
    while True:
        job = db.get_job(conn, job_id)
        if job is None:
            raise PaidRefused(f"there is no job {job_id}")
        entry = conn.execute("SELECT id FROM entries WHERE job_id = ?", (job_id,)).fetchone()
        step = next_step(job, int(entry["id"]) if entry else None)
        waited = round(clock() - started, 1)
        result = {"job": job.id, "state": job.state, "needs": job.needs,
                  "attempts": len(db.list_attempts(conn, job_id)),
                  "max_attempts": job.max_attempts, "waited_s": waited,
                  "timed_out": False, **step}
        if step["do"] != "wait":
            return result
        control = db.get_control(conn)
        if control is not None and control.state == "paused" and job.state == "queued":
            result.update(do="stop", say=f"job {job.id} is queued but the generator "
                          "is paused; it will not move until the operator resumes it")
            return result
        if waited >= timeout:
            result.update(timed_out=True, say=f"job {job.id} is still {job.state} "
                          f"after {waited:.0f} s; wait again, or report it")
            return result
        sleep(interval)
