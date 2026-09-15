"""worker.py — the loop that turns one queued job into a held entry or a kept failure.

Packet 2.3 of the sketchgen build. One job at a time, fenced against every other
client of the node's single inference slot, pausable from the database, and
recorded step by step: every transition goes through :mod:`sketchgen.db`, and
every step is logged to stderr and to ``<jobs>/<id>/job.log`` with a UTC stamp.
The job page's SSE in phase 4 tails that file; the Job detail screen in the
wireframe shows nothing this module does not write.

The order of one job, as the packet states it:

  1. read the control row   (paused / pausing / stop-now — see below)
  2. FENCE the inference slot
  3. db.claim_next()
  4. PLAN, unless the job arrived with a brief and assertions
  5. ATTEMPT n: resolve the rules file, run the executor into
     ``<jobs>/<id>/attempt-<n>/``
  6. GATE that directory as a subprocess, parse its report.json
  7. EVIDENCE from the report on failure; held on success; repair while
     ``n < max_attempts``; failed at the last one
  8. stop-now, re-read between every step and between attempts
  9. record model, rules file, prompt version, tokens, seconds, source dir,
     gate exit, gate report path, evidence and the executor's statement on
     every attempt row

Three decisions this module had to make, written down rather than left in the
code:

**Stop-now is ``pausing`` with a ``stop`` reason.** The packet allowed either a
new control value or a reason on the existing one. This takes the second: the
control row stays ``('running', 'pausing', 'paused')``, migration 001's CHECK
constraint is untouched, ``db.set_control``'s signature is untouched, and there
is no migration 002. ``pausing`` with a reason whose first word is ``stop``
means *abort the attempt in flight and re-queue the job*; ``pausing`` with any
other reason means *finish the attempt in flight, then stop*. See
:func:`is_stop_now`.

**What the fence refuses on.** ``pgrep -af opencode`` finding any process other
than this one is a refusal: an interactive session holds the slot for an hour at
a time, and a second client makes both parties about 9x slower
(``node-16x96-first-day.md`` §6). A model *listed by* ``/api/ps`` is **not** a
refusal on its own. The node runs ``KEEP_ALIVE=30m``, so a model stays resident
for half an hour after the last request with no client attached, and refusing on
residency would refuse almost always while telling us nothing about contention.
The resident models are logged on every run, so a contended run can be recognised
after the fact. If ``/api/ps`` cannot be reached at all, that is logged and is
not a refusal either: a job that needs the model then fails on its own
``model call failed`` error, with the evidence kept, rather than being refused.

**Pause with nothing in flight pauses at once.** Step 1 of the packet is taken
literally: ``pausing`` seen by a process that owns no attempt sets ``paused``
immediately and claims nothing. ``pausing`` that arrives while an attempt is
running lets that attempt finish — gate, transition, attempt row — and only then
sets ``paused``; if the job is not finished by then it is re-queued, so that a
paused job is claimable again on resume rather than stranded in ``repairing``.
An attempt that has already been recorded is never re-run: the attempt number
resumes from the highest row in the table, and the evidence from that row is
what the next attempt is given.

**Idle work, packet 5.4.** When the queue is empty and control is ``running``,
the worker does one bounded round of idle work before it sleeps: it judges up to
``SKETCHGEN_IDLE_JUDGE`` pairs with the local judge (packet 5.2) and critiques up
to ``SKETCHGEN_IDLE_CRITIQUE`` published entries, spawning the child each
critique asks for (packet 5.3). The instructor's decision of 2026-09-14 is that
this is scheduled *inside this loop* rather than by a second timer, and the
reason is the one fact the whole system is built around: there is one inference
slot. A second unit would need its own fence, and two fences racing each other
is exactly the contention the fence exists to prevent. One process owns the slot;
when it has nothing to make, it looks and it critiques.

Two consequences, both deliberate. The idle round runs *after* the worker's own
fence has cleared, and the judge is handed the worker's own probe, so it never
refuses the process that already owns the slot. And an idle round cannot take the
worker down: every step is bounded by a limit, and an exception inside one is
logged and dropped, because a queue that stops draining because a critique failed
would be a worse system than one that occasionally skips a critique.

**A step that fails is a job outcome, never an escaped exception.** Written after
the first unattended night: on 2026-09-14 job 5 was claimed into ``planning``,
``gemma4:e4b`` answered with no ``Brief`` heading, ``PlannerFailed`` came out
through the job loop — ``job.log`` ends at "worker: unhandled PlannerFailed" —
and the job sat in ``planning`` for good while the same process went on to serve
job 6. Two rules came out of it:

  * PLAN saves the raw reply to ``<jobs>/<id>/plan-response-<n>.txt`` and
    retries once **with a different seed** (:func:`plan_seed`) — the requeued
    job 5 failed identically on its second run because the seed was fixed, so a
    retry that samples the same way is not a retry. If both tries fail, the last
    reply is read leniently (:func:`sketchgen.planner.recover`): prose with no
    ``Brief`` heading becomes the brief, and the plan is stamped
    ``planner-v1+lenient`` so the entry says so. Only a reply with no prose at
    all fails the job, with ``last_error`` beginning ``planner:``. EXECUTE and
    GATE turn any exception into a recorded attempt with evidence and a
    transition. A last-resort guard around the whole job fails it rather than
    leaving it in a running state.
  * :meth:`Worker.sweep_stuck` re-queues any job in ``planning``, ``executing``,
    ``gating`` or ``repairing`` that has not moved for ``SKETCHGEN_STUCK_MINUTES``
    and that this process does not own — at startup and once per idle cycle. A
    worker that is killed mid-job, or a bug nobody predicted, costs a delay now
    instead of a job.

Python 3.12, stdlib only. Timestamps are UTC, ISO 8601 with a trailing Z.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import db, executor, lineage, planner, preflight

__all__ = [
    "DEFAULT_CRITIC_MODEL",
    "DEFAULT_GATE_PATH",
    "DEFAULT_HOST",
    "DEFAULT_IDLE_CRITIQUE",
    "DEFAULT_IDLE_JUDGE",
    "DEFAULT_JOBS_DIR",
    "DEFAULT_JUDGE_MODEL",
    "DEFAULT_LINEAGE_DEPTH",
    "DEFAULT_PLANNER_MODEL",
    "DEFAULT_RULES",
    "DEFAULT_SLEEP_S",
    "DEFAULT_STUCK_MINUTES",
    "EVIDENCE_HEADING",
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "Execution",
    "FenceResult",
    "GateOutcome",
    "PREFLIGHT_HEADING",
    "StopNow",
    "Worker",
    "build_evidence",
    "default_judge",
    "default_probe",
    "evidence_with_preflight",
    "fence",
    "idle_summary",
    "is_stop_now",
    "minutes_between",
    "node_shape",
    "observing_probe",
    "plan_seed",
    "resolve_rules",
    "stub_executor",
    "stub_judge",
    "stub_planner",
]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3

#: Paths and the model host come from the environment, with the same names and
#: the same defaults as systemd/sketchgen-worker.service (packet 2.4).
DEFAULT_JOBS_DIR = os.environ.get(
    "SKETCHGEN_JOBS", str(Path.home() / "sketchgen" / "jobs")
)
DEFAULT_GATE_PATH = os.environ.get(
    "SKETCHGEN_GATE", str(Path.home() / "sketchgen" / "gate" / "sketch_gate.py")
)
DEFAULT_HOST = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")

DEFAULT_SLEEP_S = 30.0
DEFAULT_PLANNER_MODEL = "gemma4:e4b"
DEFAULT_GATE_TIMEOUT_S = 600.0


def _int_env(name: str, default: int) -> int:
    """An integer from the environment, or the default if it is not one.

    Junk in an ``Environment=`` line must not stop the worker starting: a
    misspelled limit becomes the default, and the unit file documents the names.
    """
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


#: Idle work (packet 5.4). The two limits are also the off switches: 0 disables
#: that half of the round. Both models default to the local judge's model —
#: ``gemma4:e4b`` is the model on this box that can see (spec §5), and the critic
#: is that judge with a pen.
DEFAULT_JUDGE_MODEL = os.environ.get("SKETCHGEN_JUDGE_MODEL", "gemma4:e4b")
DEFAULT_CRITIC_MODEL = os.environ.get("SKETCHGEN_CRITIC_MODEL", "gemma4:e4b")
DEFAULT_IDLE_JUDGE = _int_env("SKETCHGEN_IDLE_JUDGE", 1)
DEFAULT_IDLE_CRITIQUE = _int_env("SKETCHGEN_IDLE_CRITIQUE", 1)
DEFAULT_LINEAGE_DEPTH = _int_env("SKETCHGEN_LINEAGE_DEPTH", lineage.DEFAULT_MAX_DEPTH)

#: How long a job may sit in a running state with nobody attending it before the
#: sweep puts it back on the queue. 0 switches the sweep off. Thirty minutes is
#: comfortably longer than any real step: the slowest measured job is minutes,
#: and the gate's slowest assertion is about five seconds.
DEFAULT_STUCK_MINUTES = _int_env("SKETCHGEN_STUCK_MINUTES", 30)

#: One plan, then one retry. A small model that drops a heading usually does not
#: drop it twice, and a second failure is a job outcome rather than a third try.
PLAN_TRIES = 2

#: The shape of every timestamp this system writes (db.utc_now).
UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
#: The rules file a job gets when it names none. The A/B (spec §9) is opt-in per
#: job; a job that says nothing is executed under the rules the course teaches.
DEFAULT_RULES = "treatment"

#: The heading the previous attempt's evidence is filed under in the next
#: attempt's brief. The executor template needs no new slot for this and its
#: prompt_version therefore does not change: the evidence is part of the brief,
#: which is where a person would put it too.
EVIDENCE_HEADING = "## What the gate found on the previous attempt"

#: The heading the pre-flight scan's findings are filed under, above everything
#: the gate itself said. The gate remains the authority on the verdict; this is
#: the sentence that explains the console error it reported (sketchgen/preflight.py).
PREFLIGHT_HEADING = "Names this sketch shadows (checked before the gate ran):"

#: Which report notes belong to which fixed check, when the note does not name
#: the check itself. sketch_gate.py writes prose notes; the evidence has to put
#: each one beside the check it explains (spec §3.3).
CHECK_NOTE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "console_clean": ("console",),
    "frame_advancing": ("frame_advancing", "framecount"),
    "is_looping": ("is_looping", "noloop"),
    "sound_lib_ok": ("sound_lib_ok", "p5.sound", "addon"),
    "audio_context_running": ("audio_context_running", "audiocontext"),
    # The gate writes every frame-budget note with "frame_budget:" in front of
    # it, so the name alone finds them; "ms per frame" is here for the day
    # somebody writes a note that forgets to.
    "frame_budget": ("frame_budget", "ms per frame"),
}

#: Notes worth carrying even when nothing failed on them: the two runtime facts
#: the executor cannot get by reading its own source (spec §3.3).
RUNTIME_NOTE_KEYWORDS = ("framecount", "audiocontext")

MAX_CONSOLE_LINES = 10


class StopNow(Exception):
    """The operator asked for stop-now while this process held an attempt."""


# ---------------------------------------------------------------------------
# The control row
# ---------------------------------------------------------------------------


def is_stop_now(control: db.Control | None) -> bool:
    """True when the control row means *stop now*, not *pause after this*.

    Stop-now is ``state='pausing'`` with a reason whose first word is ``stop``
    (``bin/sketchgen control stop`` writes exactly that). Keeping it in the
    reason rather than in a new state value leaves migration 001's CHECK
    constraint and ``db.set_control``'s signature alone.
    """
    if control is None or control.state != "pausing":
        return False
    reason = (control.reason or "").strip().lower()
    return reason == "stop" or reason.startswith("stop:") or reason.startswith("stop ")


# ---------------------------------------------------------------------------
# The fence
# ---------------------------------------------------------------------------


@dataclass
class FenceResult:
    """What the fence saw. ``ok`` false means refuse (exit 3) and back off."""

    ok: bool
    reason: str | None = None
    processes: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    ollama_error: str | None = None
    #: Clients the probe saw but chose not to refuse on (test mode only).
    observed: list[str] = field(default_factory=list)


def opencode_processes() -> list[str]:
    """``pgrep -af opencode``, minus this process, its parent and pgrep itself."""
    try:
        proc = subprocess.run(
            ["pgrep", "-af", "opencode"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return []
    mine = {os.getpid(), os.getppid()}
    found: list[str] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid, _, command = line.partition(" ")
        if pid.isdigit() and int(pid) in mine:
            continue
        if "pgrep" in command:
            continue
        found.append(line)
    return found


def resident_models(host: str = DEFAULT_HOST, timeout: float = 5.0) -> tuple[list[str], str | None]:
    """``GET /api/ps``: the model names Ollama has resident, and any error."""
    url = host.rstrip("/") + "/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return [], f"{url} did not answer: {exc}"
    models = body.get("models") or []
    names = [str(entry.get("name") or entry.get("model") or "?") for entry in models]
    return names, None


def default_probe(host: str = DEFAULT_HOST) -> dict[str, Any]:
    """The real probe: other clients, and what Ollama has loaded."""
    names, error = resident_models(host)
    return {
        "processes": opencode_processes(),
        "models": names,
        "ollama_error": error,
    }


def observing_probe(host: str = DEFAULT_HOST) -> Callable[[], dict[str, Any]]:
    """TEST ONLY: look at the slot, report it, and never refuse on it.

    The CLI injects this when the executor is stubbed. A stubbed run calls no
    model, so it cannot contend for the inference slot and has nothing to fence
    against; refusing it would only mean the worker path could never be
    exercised from a shell while somebody else is using the node. What the real
    probe saw is still carried through as ``observed_processes`` and logged, so
    the run says out loud that the slot was busy. The fence itself is unchanged
    and is what a real run gets.
    """

    def probe() -> dict[str, Any]:
        seen = default_probe(host)
        return {
            "processes": [],
            "observed_processes": seen.get("processes") or [],
            "models": seen.get("models") or [],
            "ollama_error": seen.get("ollama_error"),
        }

    return probe


def fence(probe: Callable[[], dict[str, Any]] = default_probe) -> FenceResult:
    """Decide whether this worker may use the inference slot.

    Refuses only when another client process is alive. A model listed by
    ``/api/ps`` is recorded and allowed: ``KEEP_ALIVE=30m`` leaves models
    resident with nobody attached, so residency is not contention. The probe is
    injectable so a test can trip the fence without starting anything.
    """
    seen = probe() or {}
    processes = list(seen.get("processes") or [])
    models = list(seen.get("models") or [])
    observed = list(seen.get("observed_processes") or [])
    error = seen.get("ollama_error")
    if processes:
        return FenceResult(
            ok=False,
            reason="another client holds the inference slot: " + "; ".join(processes),
            processes=processes,
            models=models,
            ollama_error=error,
            observed=observed,
        )
    return FenceResult(ok=True, processes=[], models=models, ollama_error=error,
                       observed=observed)


# ---------------------------------------------------------------------------
# What one attempt produced
# ---------------------------------------------------------------------------


@dataclass
class Execution:
    """One executor run, in the shape the attempt row wants.

    The default executor builds this from ``executor.Result``; an injected stub
    builds it directly. ``ok`` false means a malformed response: the attempt is
    still recorded, with ``gate_exit`` null and ``executor: <error>`` as its
    evidence.
    """

    ok: bool
    error: str | None = None
    source_dir: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    prefill_s: float | None = None
    decode_s: float | None = None
    wall_s: float | None = None
    statement: str | None = None

    @classmethod
    def from_result(cls, result: executor.Result) -> "Execution":
        tokens = result.tokens or {}
        seconds = result.durations_s or {}
        return cls(
            ok=result.ok,
            error=result.error,
            source_dir=result.out_dir,
            model=result.model,
            prompt_version=result.prompt_version,
            prompt_tokens=tokens.get("prompt_eval_count"),
            completion_tokens=tokens.get("eval_count"),
            prefill_s=seconds.get("prompt_eval_duration"),
            decode_s=seconds.get("eval_duration"),
            wall_s=result.wall_s,
            statement=read_statement(result.out_dir),
        )


@dataclass
class GateOutcome:
    """One gate run: its exit code, its report.json, and where that landed."""

    exit_code: int | None
    report: dict[str, Any] | None = None
    report_path: str | None = None
    stderr: str = ""


def read_statement(source_dir: str | os.PathLike[str] | None) -> str | None:
    """The executor's ``statement.md``, verbatim, or None if it wrote none."""
    if source_dir is None:
        return None
    path = Path(source_dir) / "statement.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# The rules file
# ---------------------------------------------------------------------------


def resolve_rules(rules_file: str | None, job_id: int) -> str:
    """``random`` becomes control or treatment by a seeded coin on the job id.

    Deterministic and stable: the same job always lands on the same side of the
    A/B, whatever order the queue is worked in, and the resolved value is what
    the attempt row records (spec §9).
    """
    if rules_file in ("control", "treatment"):
        return rules_file
    if rules_file == "random":
        digest = hashlib.sha256(f"sketchgen-rules-{job_id}".encode("utf-8")).digest()
        return "treatment" if digest[0] % 2 else "control"
    return DEFAULT_RULES


# ---------------------------------------------------------------------------
# Evidence (spec §3.3)
# ---------------------------------------------------------------------------


def _notes_for_check(name: str, notes: list[str]) -> list[str]:
    keywords = CHECK_NOTE_KEYWORDS.get(name, ())
    hits = []
    for note in notes:
        low = note.lower()
        if name in low or any(word in low for word in keywords):
            hits.append(note.strip())
    return hits


def build_evidence(report: dict[str, Any] | None, gate_exit: int | None,
                   stderr: str = "") -> str:
    """The text fed back to the executor after a failed gate run.

    Built from report.json and nothing else: the failed fixed checks with the
    note that explains each one, the failed assertions with the gate's own
    detail line, the console lines that are errors, and the frameCount and
    AudioContext notes. ``qwen3-coder`` has no vision and cannot read a
    screenshot; this is the eye on the canvas it lacked, in the only form it can
    use (spec §3.3). The first line is a summary and is what lands in
    ``jobs.last_error`` when the last attempt fails.
    """
    if not report:
        if gate_exit == 3:
            head = "gate refused (exit 3)"
        elif gate_exit is None:
            head = "gate did not finish and wrote no report.json"
        else:
            head = f"gate exit {gate_exit}: no report.json was written"
        first_error = (stderr or "").strip().splitlines()
        if first_error:
            return f"{head}: {first_error[0]}"
        return head

    checks = report.get("checks") or {}
    assertions = report.get("assertions") or {}
    notes = [str(note) for note in (report.get("notes") or [])]
    console = report.get("console") or []

    failed_checks = [name for name, value in checks.items() if value is False]
    failed_assertions = [
        name for name, value in assertions.items() if not (value or {}).get("pass")
    ]

    parts: list[str] = []
    summary = []
    if failed_checks:
        summary.append("checks failed: " + ", ".join(failed_checks))
    if failed_assertions:
        summary.append("assertions failed: " + ", ".join(failed_assertions))
    if not summary:
        summary.append("nothing in the report named a failure; read the notes below")
    parts.append(f"gate exit {gate_exit}: " + "; ".join(summary))

    if failed_checks:
        parts.append("")
        parts.append("Fixed checks that failed:")
        for name in failed_checks:
            parts.append(f"- {name} = false")
            for note in _notes_for_check(name, notes):
                parts.append(f"    {note}")

    if failed_assertions:
        parts.append("")
        parts.append("Assertions that failed:")
        for name in failed_assertions:
            detail = (assertions.get(name) or {}).get("detail") or "(no detail)"
            parts.append(f"- {name}: {detail}")

    error_lines = [
        entry for entry in console
        if str(entry.get("type")) in ("error", "pageerror")
    ]
    if error_lines:
        parts.append("")
        shown = error_lines[:MAX_CONSOLE_LINES]
        parts.append(
            "Console (%d error line%s, first %d shown):"
            % (len(error_lines), "" if len(error_lines) == 1 else "s", len(shown))
        )
        for entry in shown:
            text = str(entry.get("text", "")).replace("\n", " ")
            parts.append(f"- [{entry.get('type')}] {text}")

    runtime = [
        note for note in notes
        if any(word in note.lower() for word in RUNTIME_NOTE_KEYWORDS)
    ]
    if runtime:
        parts.append("")
        parts.append("Runtime state the gate read:")
        for note in runtime:
            parts.append(f"- {note.strip()}")

    return "\n".join(parts).rstrip() + "\n"


def evidence_with_preflight(evidence: str, lines: list[str]) -> str:
    """The gate's evidence with the pre-flight findings above it.

    Above, because the model reads the top of what it is given and the gate's
    own first line is ``line is not a function`` — the symptom. The finding is
    the cause, and putting the cause second is how three attempts got spent on
    job 16. No findings changes nothing at all.
    """
    if not lines:
        return evidence
    return "\n".join([PREFLIGHT_HEADING, *lines, "", evidence.lstrip("\n")])


def minutes_between(earlier: str | None, later: str | None) -> float | None:
    """Minutes between two of this system's UTC stamps, or None if unreadable.

    Unreadable is not an error and not zero: a row whose timestamp cannot be
    parsed is one the sweep leaves alone rather than re-queues on a guess.
    """
    try:
        start = datetime.strptime(str(earlier), UTC_FORMAT).replace(tzinfo=timezone.utc)
        end = datetime.strptime(str(later), UTC_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return (end - start).total_seconds() / 60.0


def node_shape() -> str:
    """The machine this entry was made on, as one string for the gallery.

    ``SKETCHGEN_SHAPE`` wins when it is set; otherwise the core count and
    MemTotal are read and formatted the way the dossiers name the node. The
    tenancy's trial ends about 10/1 and 16/96 becomes 4/24 (spec §9) — the
    entry has to say which one made it, or the timings in it mean nothing.
    """
    override = os.environ.get("SKETCHGEN_SHAPE")
    if override:
        return override
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        cores = os.cpu_count() or 0
    gib = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    gib = round(int(line.split()[1]) / (1024 * 1024))
                    break
    except (OSError, ValueError, IndexError):  # pragma: no cover - non-Linux
        gib = 0
    return f"VM.Standard.A1.Flex {cores}/{gib}"


def brief_with_evidence(brief: str, evidence: str | None) -> str:
    """The brief the next attempt is given: the original, then the evidence."""
    if not evidence:
        return brief
    return f"{brief.rstrip()}\n\n{EVIDENCE_HEADING}\n\n{evidence.rstrip()}\n"


# ---------------------------------------------------------------------------
# The default injectables
# ---------------------------------------------------------------------------


def plan_seed(job_id: int, n: int) -> int:
    """The sampling seed for plan try ``n`` of a job.

    Not a constant, and that is the point: planner.plan defaults to seed 1, so
    the retry of job 5 on 2026-09-14 asked the same model the same question with
    the same seed and got back the same malformed reply, exactly. A seed that
    moves with the try number makes the retry a different roll; keying it to the
    job id keeps it reproducible for anyone re-running that job.
    """
    return int(job_id) + int(n)


def default_planner(*, job: db.Job, host: str, model: str,
                    seed: int = 1) -> planner.Plan:
    """The local planner (packet 2.5). Calls the model; never used in tests."""
    return planner.plan(
        job.prompt, model, host=host, by=job.submitted_by, seed=seed
    )


def default_executor(
    *,
    brief: str,
    assertions: list[str],
    rules_file: str,
    out_dir: str,
    model: str,
    host: str,
) -> Execution:
    """The single-shot executor (packet 2.2). Calls the model."""
    result = executor.run(
        brief=brief,
        assertions=assertions,
        rules_file=rules_file,
        model=model,
        host=host,
        out_dir=out_dir,
    )
    return Execution.from_result(result)


def default_gate(
    *,
    source_dir: str,
    assertions: list[str],
    out_dir: str,
    gate_path: str = DEFAULT_GATE_PATH,
    timeout: float = DEFAULT_GATE_TIMEOUT_S,
) -> GateOutcome:
    """Run sketch_gate.py over one attempt directory as a subprocess.

    ``python3 <gate> <attempt dir> --assert … --json --out <attempt dir>/.gate``,
    exactly as the packet states it. The gate calls no model, so this is the one
    part of a job that is safe to run while the slot is busy.
    """
    command = [sys.executable, str(gate_path), str(source_dir)]
    for word in assertions:
        command += ["--assert", word]
    command += ["--json", "--out", str(out_dir)]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
        code: int | None = proc.returncode
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        return GateOutcome(
            exit_code=None,
            report=None,
            report_path=None,
            stderr=f"the gate did not finish within {timeout:.0f}s",
        )
    except OSError as exc:
        return GateOutcome(exit_code=None, report=None, report_path=None,
                           stderr=f"could not run the gate: {exc}")

    report_path = Path(out_dir) / "report.json"
    report: dict[str, Any] | None = None
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            stderr = f"{stderr}\nreport.json is unreadable: {exc}"
    return GateOutcome(
        exit_code=code,
        report=report,
        report_path=str(report_path) if report is not None else None,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# The test-only stubs behind `sketchgen worker --stub-executor/--stub-planner-assert`
# ---------------------------------------------------------------------------


def stub_executor(source: str | os.PathLike[str]) -> Callable[..., Execution]:
    """An executor that copies a directory instead of calling a model.

    TEST ONLY, and the CLI says so in its help. It exists so that the whole
    worker path — claim, plan, execute, the REAL gate, evidence, held — can be
    exercised from a shell with no model call at all.
    """
    origin = Path(source).expanduser()

    def run_stub(*, brief, assertions, rules_file, out_dir, model, host) -> Execution:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        if not origin.is_dir():
            return Execution(ok=False, error=f"no stub directory at {origin}",
                             source_dir=str(out))
        started = time.monotonic()
        for entry in sorted(origin.iterdir()):
            if entry.is_file():
                shutil.copyfile(entry, out / entry.name)
        (out / "brief.txt").write_text(brief, encoding="utf-8")
        statement_path = out / "statement.md"
        if not statement_path.is_file():
            statement_path.write_text(
                "Stubbed executor: the files in this attempt were copied from a "
                "fixture directory and no model was called. The statement a real "
                "run would publish is the model's own, verbatim; this one is the "
                "worker's, and says so.\n",
                encoding="utf-8",
            )
        return Execution(
            ok=True,
            source_dir=str(out),
            model="stub",
            prompt_version="stub-executor",
            wall_s=round(time.monotonic() - started, 3),
            statement=read_statement(out),
        )

    return run_stub


def default_judge(conn, **kwargs: Any) -> dict[str, int]:
    """:func:`sketchgen.judge.run_local`, imported here rather than at the top.

    ``judge.py`` imports this module for the fence, so importing it at module
    level would be a cycle. It is imported at the moment it is called instead,
    which also means a worker that never goes idle never loads the judge.
    """
    from . import judge  # local import: judge.py imports worker for the fence

    return judge.run_local(conn, **kwargs)


def default_critic(conn, entry_id: int, **kwargs: Any):
    """:func:`sketchgen.lineage.critique` — one sentence about one entry."""
    return lineage.critique(conn, entry_id, **kwargs)


def stub_judge(reply: str | os.PathLike[str] | None = None) -> Callable[..., dict[str, int]]:
    """A judge that replays a saved reply, or judges nothing at all. TEST ONLY.

    With no ``reply`` it calls nothing and reports no pairs, which is what
    ``sketchgen worker --stub-judge`` gives the phase gate: an idle round whose
    critique half can be watched without the judge touching the model.
    """

    def judge_stub(conn, **kwargs: Any) -> dict[str, int]:
        if reply is None:
            log = kwargs.get("log")
            if log is not None:
                log("the judge is stubbed and was given no reply to replay")
            return {"judged": 0, "recorded": 0, "pairs_offered": 0}
        from . import judge  # local import, as in default_judge

        kwargs["stub"] = str(reply)
        return judge.run_local(conn, **kwargs)

    return judge_stub


def idle_summary(conn) -> dict[str, Any]:
    """What the idle round has done, read from the database, not from memory.

    ``sketchgen db status`` prints this. A database that predates migration 006
    (or 001's judgments table) answers zeros rather than raising: an older file
    should still render.
    """
    summary: dict[str, Any] = {
        "agent_verdicts": 0,
        "critiques": 0,
        "spawned": 0,
        "rejected": 0,
        "last_action_utc": None,
    }
    stamps: list[str] = []
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, MAX(created_utc) AS last FROM judgments "
            "WHERE judge_kind = 'agent'"
        ).fetchone()
        summary["agent_verdicts"] = int(row["c"] or 0)
        if row["last"]:
            stamps.append(str(row["last"]))
    except sqlite3.OperationalError:
        pass
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c, "
            "SUM(CASE WHEN spawned_job_id IS NOT NULL THEN 1 ELSE 0 END) AS spawned, "
            "SUM(CASE WHEN rejected_reason IS NOT NULL THEN 1 ELSE 0 END) AS rejected, "
            "MAX(created_utc) AS last FROM critiques"
        ).fetchone()
        summary["critiques"] = int(row["c"] or 0)
        summary["spawned"] = int(row["spawned"] or 0)
        summary["rejected"] = int(row["rejected"] or 0)
        if row["last"]:
            stamps.append(str(row["last"]))
    except sqlite3.OperationalError:
        pass
    summary["last_action_utc"] = max(stamps) if stamps else None
    return summary


def stub_planner(assertions: list[str]) -> Callable[..., planner.Plan]:
    """A planner that returns the job's own prompt as the brief. TEST ONLY."""

    def plan_stub(*, job, host, model, seed: int = 1) -> planner.Plan:
        return planner.Plan(
            brief=job.prompt,
            assertions=list(assertions),
            prompt_version="stub-planner",
            tokens={},
            durations={},
            raw="",
        )

    return plan_stub


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class Worker:
    """One job at a time, from the queue to held or failed.

    Every external thing it touches is injectable: ``planner_fn``,
    ``executor_fn``, ``gate_fn`` and the fence ``probe``. With all four stubbed
    the worker runs with no model, no browser and no other process, which is how
    ``tests/test_worker.py`` exercises it.
    """

    def __init__(
        self,
        conn,
        *,
        jobs_dir: str | os.PathLike[str] = DEFAULT_JOBS_DIR,
        gate_path: str | os.PathLike[str] = DEFAULT_GATE_PATH,
        host: str = DEFAULT_HOST,
        executor_model: str = executor.DEFAULT_MODEL,
        planner_model: str = DEFAULT_PLANNER_MODEL,
        judge_model: str = DEFAULT_JUDGE_MODEL,
        critic_model: str = DEFAULT_CRITIC_MODEL,
        idle_judge: int = DEFAULT_IDLE_JUDGE,
        idle_critique: int = DEFAULT_IDLE_CRITIQUE,
        lineage_depth: int = DEFAULT_LINEAGE_DEPTH,
        stuck_minutes: int = DEFAULT_STUCK_MINUTES,
        planner_fn: Callable[..., Any] | None = None,
        executor_fn: Callable[..., Execution] | None = None,
        gate_fn: Callable[..., GateOutcome] | None = None,
        judge_fn: Callable[..., dict[str, int]] | None = None,
        critic_fn: Callable[..., Any] | None = None,
        spawn_fn: Callable[..., int | None] | None = None,
        probe: Callable[[], dict[str, Any]] | None = None,
        log_stream=None,
    ) -> None:
        self.conn = conn
        self.jobs_dir = Path(jobs_dir).expanduser()
        self.gate_path = str(gate_path)
        self.host = host
        self.executor_model = executor_model
        self.planner_model = planner_model
        self.planner_fn = planner_fn or default_planner
        self.executor_fn = executor_fn or default_executor
        self.gate_fn = gate_fn or (
            lambda **kwargs: default_gate(gate_path=self.gate_path, **kwargs)
        )
        self.judge_model = judge_model
        self.critic_model = critic_model
        self.idle_judge = int(idle_judge)
        self.idle_critique = int(idle_critique)
        self.lineage_depth = int(lineage_depth)
        self.stuck_minutes = int(stuck_minutes)
        self.judge_fn = judge_fn or default_judge
        self.critic_fn = critic_fn or default_critic
        self.spawn_fn = spawn_fn or lineage.spawn
        self.probe = probe or (lambda: default_probe(self.host))
        self.log_stream = sys.stderr if log_stream is None else log_stream
        self._log_path: Path | None = None
        self._planner_prompt_version: str | None = None
        #: Jobs this process has in flight. The sweep never touches these.
        self._owned: set[int] = set()
        self._swept = False
        self._terminating = False

    # -- logging ---------------------------------------------------------

    def log(self, message: str) -> None:
        """One line, UTC-stamped, to stderr and to the job log if one is open."""
        line = f"{db.utc_now()}  {message}"
        try:
            print(line, file=self.log_stream, flush=True)
        except (OSError, ValueError):  # pragma: no cover - closed stream
            pass
        if self._log_path is not None:
            try:
                with open(self._log_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:  # pragma: no cover - disk failure
                pass

    def _open_job_log(self, job_id: int) -> Path:
        directory = self.jobs_dir / str(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        self._log_path = directory / "job.log"
        return directory

    # -- control ---------------------------------------------------------

    def _control(self) -> db.Control | None:
        return db.get_control(self.conn)

    def _check_stop(self) -> None:
        """Raise StopNow if the operator asked for it since the last check."""
        if is_stop_now(self._control()):
            raise StopNow()

    def _pause_requested(self) -> bool:
        """``pausing`` that is not stop-now: finish this attempt, then stop."""
        control = self._control()
        return control is not None and control.state == "pausing" and not is_stop_now(control)

    # -- one pass --------------------------------------------------------

    def run_once(self) -> int:
        """One pass: at most one job. 0 ok / 1 failed / 3 refused by the fence."""
        self._log_path = None
        self._planner_prompt_version = None
        control = self._control()
        if control is not None and control.state == "paused":
            self.log(f"control: paused ({control.reason or 'no reason given'}); "
                     "claiming nothing")
            return EXIT_OK
        if control is not None and control.state == "pausing":
            reason = control.reason or "no reason given"
            db.set_control(self.conn, "paused", control.reason)
            self.log(f"control: {reason} and no attempt is in flight; now paused")
            return EXIT_OK

        # Before anything else this process does with the queue: put back the
        # jobs a dead worker left in flight. It is pure database work, no model
        # and no browser, so it runs ahead of the fence — a worker that is about
        # to refuse can still clean up after the one that came before it.
        if not self._swept:
            self._swept = True
            self.sweep_stuck()

        result = fence(self.probe)
        for name in result.models:
            self.log(f"fence: ollama has {name} resident (not a refusal)")
        for line in result.observed:
            self.log(f"fence: NOT ENFORCED in test mode — the slot is held by: {line}")
        if result.ollama_error:
            self.log(f"fence: {result.ollama_error} (not a refusal)")
        if not result.ok:
            self.log(f"fence: REFUSED — {result.reason}")
            return EXIT_REFUSED
        self.log(
            "fence: proceeding; this run calls no model (test mode)"
            if result.observed
            else "fence: the inference slot is free"
        )

        job = db.claim_next(self.conn)
        if job is None:
            self.log("queue: nothing queued")
            self._idle_round()
            return EXIT_OK

        directory = self._open_job_log(job.id)
        self.log(f"job {job.id}: claimed, state {job.state}, by {job.submitted_by}, "
                 f"work directory {directory}")
        self._owned.add(job.id)
        try:
            return self._run_job(job)
        except StopNow:
            if self._terminating:
                self._abandon_for_restart(job.id)
            else:
                self._stop_now(job.id)
            return EXIT_OK
        except Exception as exc:
            # Last resort. Every step above already turns its own failures into
            # a transition; this is here so that a bug nobody predicted still
            # cannot leave a job in a running state with no worker attending it,
            # which is exactly what happened to job 5 on 2026-09-14.
            reason = f"worker: {type(exc).__name__}: {' '.join(f'{exc}'.split())}"
            self.log(f"job {job.id}: unhandled {reason}")
            try:
                current = db.get_job(self.conn, job.id)
                if current is not None and current.state in db.REQUEUABLE:
                    db.transition(self.conn, job.id, "failed", last_error=reason)
                    self.log(f"job {job.id}: failed rather than left in "
                             f"{current.state}")
            except Exception as inner:  # pragma: no cover - the DB is the problem
                self.log(f"job {job.id}: could not record the failure: {inner}")
            return EXIT_FAIL
        finally:
            self._owned.discard(job.id)

    def run_forever(self, sleep_s: float = DEFAULT_SLEEP_S) -> int:
        """The resident mode systemd runs. Never start this from a tool call."""
        # Packet 4.1: the console's "session" column is everything since here.
        db.set_meta(self.conn, "worker_started_utc", db.utc_now())
        self._install_signal_handlers()
        self.log(f"worker: resident, polling every {sleep_s:.0f}s")
        # There is one worker. Anything in a running state at this moment was
        # left there by the previous one and nobody is attending it, however
        # recent its timestamp: back on the queue now, not in thirty minutes.
        self.sweep_stuck(everything=True)
        while not self._terminating:
            try:
                code = self.run_once()
            except Exception as exc:  # keep the daemon alive; systemd logs it
                self.log(f"worker: unhandled {type(exc).__name__}: {exc}")
                code = EXIT_FAIL
            if self._terminating:
                break
            if code == EXIT_REFUSED:
                self.log(f"worker: fenced; backing off {sleep_s:.0f}s")
            self._nap(sleep_s)
        self.log("worker: stopped (SIGTERM); nothing left in flight")
        return EXIT_OK

    # -- the sweep -------------------------------------------------------

    def sweep_stuck(self, now: str | None = None, *, everything: bool = False) -> list[int]:
        """Re-queue jobs left in a running state by a worker that is not coming
        back. Returns the job ids re-queued.

        A job in ``planning``, ``executing``, ``gating`` or ``repairing`` is a
        job somebody is supposed to be attending. If its ``updated_utc`` is
        older than ``stuck_minutes`` and this process is not the one attending
        it, nobody is: the worker was killed, the node rebooted, or an
        exception escaped before this packet existed. It goes back on the queue,
        with the attempts it already has, and the next pass picks it up.

        Owned means in flight *here*: a job this process claimed in this pass is
        never swept, however long its own plan or gate takes.

        ``everything=True`` is the start-up sweep: age is not consulted, because
        at start nothing is owned and there is no other worker, so every job in
        a running state is an orphan of the previous process.
        """
        if self.stuck_minutes <= 0 and not everything:
            return []
        stamp = now or db.utc_now()
        try:
            rows = self.conn.execute(
                "SELECT id, state, updated_utc FROM jobs WHERE state IN "
                "('planning','executing','gating','repairing') ORDER BY id"
            ).fetchall()
        except sqlite3.Error as exc:  # pragma: no cover - the DB is the problem
            self.log(f"sweep: cannot read the queue: {exc}")
            return []

        swept: list[int] = []
        for row in rows:
            job_id = int(row["id"])
            if job_id in self._owned:
                continue
            age = minutes_between(row["updated_utc"], stamp)
            if everything:
                reason = f"swept at start: left in {row['state']} by the previous worker"
                said = f"sweep: job {job_id} left in {row['state']} by the previous worker; re-queued"
            elif age is None or age < self.stuck_minutes:
                continue
            else:
                reason = f"swept: left in {row['state']} for {age:.0f} minutes"
                said = (f"sweep: job {job_id} sat in {row['state']} for {age:.0f} minutes "
                        "with no worker attending it; re-queued")
            try:
                db.requeue(self.conn, job_id, reason=reason)
            except (db.IllegalTransition, db.UnknownJob, sqlite3.Error) as exc:
                self.log(f"sweep: job {job_id} could not be re-queued: {exc}")
                continue
            swept.append(job_id)
            self.log(said)
        return swept

    # -- idle work (packet 5.4) ------------------------------------------

    def _idle_say(self, message: str) -> None:
        """One line from inside an idle step, prefixed so a log reader can tell.

        ``judge.run_local`` says ``judged 1 vs 2: brief=A look=B``; the model
        that said it belongs in the same line, because two judges will share
        this log before the semester is out.
        """
        if message.startswith("judged "):
            head, _, tail = message.partition(":")
            detail = f" ({tail.strip()})" if tail.strip() else ""
            self.log(f"idle: {head.strip()} as {self.judge_model}{detail}")
        else:
            self.log(f"idle: {message}")

    def _idle_seed(self) -> int:
        """A seed from the UTC minute: stable within a round, different between.

        The judge's pair choice is deterministic given its rng (packet 5.1), and
        a seed that changes every minute keeps a worker that idles all night
        from offering the same tied pair over and over.
        """
        return int("".join(ch for ch in db.utc_now()[:16] if ch.isdigit()))

    def _idle_round(self) -> None:
        """One bounded round of idle work: judge a pair, critique an entry.

        Called only when the queue is empty and control is ``running``. Nothing
        here may raise: a failure in idle work is logged and dropped, because a
        worker that stopped draining the queue over a failed critique would be a
        worse machine than one that skips a critique.
        """
        # Once per idle cycle, because this is the moment the worker has time:
        # a job stuck in a running state is a job nobody is coming back for.
        self.sweep_stuck()
        judged = self._idle_judge() if self.idle_judge > 0 else None
        critiqued = self._idle_critique() if self.idle_critique > 0 else None
        if judged or critiqued:
            return
        parts = [
            "judging is switched off (SKETCHGEN_IDLE_JUDGE=0)"
            if self.idle_judge <= 0 else "nothing to judge",
            "critiquing is switched off (SKETCHGEN_IDLE_CRITIQUE=0)"
            if self.idle_critique <= 0 else "nothing to critique",
        ]
        self.log("idle: " + "; ".join(parts))

    def _idle_judge(self) -> int:
        """Judge up to ``idle_judge`` pairs with the local judge (packet 5.2).

        The judge is handed *this worker's* probe. Its own fence has already
        cleared this pass, and the worker is the process that owns the slot: a
        second probe could only refuse the worker its own resident model, or
        undo the test-mode observing probe the CLI injected.
        """
        try:
            counts = self.judge_fn(
                self.conn,
                model=self.judge_model,
                host=self.host,
                limit=self.idle_judge,
                rng=random.Random(self._idle_seed()),
                probe=self.probe,
                log=self._idle_say,
            )
        except Exception as exc:  # never take the worker down over idle work
            self.log(f"idle: the judge stopped: {type(exc).__name__}: {exc}")
            return 0
        return int((counts or {}).get("judged") or 0)

    def _idle_critique(self) -> int:
        """Critique up to ``idle_critique`` published entries and spawn children.

        Returns how many critiques were *recorded*, rejections included: a
        rejected critique is an action, and it is what stops that entry being
        offered again under the same prompt version.
        """
        try:
            version = lineage.prompt_version()
        except Exception as exc:
            self.log(f"idle: no critique prompt to work from: {exc}")
            return 0
        try:
            candidates = db.entries_to_critique(
                self.conn, version, self.idle_critique
            )
        except sqlite3.Error as exc:
            self.log(f"idle: cannot look for entries to critique: {exc}")
            return 0
        recorded = 0
        for entry_id in candidates:
            try:
                if self._critique_one(entry_id, version):
                    recorded += 1
            except Exception as exc:  # as above: logged, dropped
                self.log(
                    f"idle: critique of entry {entry_id} stopped: "
                    f"{type(exc).__name__}: {exc}"
                )
        return recorded

    def _parent_rules_file(self, entry_row) -> str | None:
        """The rules file the parent's JOB named, not the one it resolved to.

        ``entries.rules_file`` holds the resolved side of the A/B ('control' or
        'treatment'); the job may have said 'random'. A line inherits the
        *setting*, so a random line stays random and the coin is tossed again
        per child (spec §9).
        """
        job_id = entry_row["job_id"] if "job_id" in entry_row.keys() else None
        if job_id is None:
            return None
        row = self.conn.execute(
            "SELECT rules_file FROM jobs WHERE id = ?", (int(job_id),)
        ).fetchone()
        return row["rules_file"] if row is not None else None

    def _critique_one(self, entry_id: int, version: str) -> bool:
        """One entry: critique it, spawn its child, record what happened."""
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (int(entry_id),)
        ).fetchone()
        if row is None:  # pragma: no cover - it was there a moment ago
            return False

        try:
            critique = self.critic_fn(
                self.conn, int(entry_id), model=self.critic_model, host=self.host
            )
        except lineage.CritiqueRefused as exc:
            # It could not run at all (no prompt file, no stub). The entry is
            # not consumed: nothing was asked of the model.
            self.log(f"idle: critique of entry {entry_id} refused: {exc}")
            return False
        except lineage.CritiqueFailed as exc:
            raw = " ".join(str(getattr(exc, "raw", "") or "").split())[:1000]
            db.record_critique(
                self.conn,
                entry_id,
                critique=raw or None,
                critique_by=self.critic_model,
                prompt_version=version,
                spawned_job_id=None,
                rejected_reason=str(exc),
            )
            self.log(f"idle: critiqued entry {entry_id} but it was rejected: {exc}")
            return True

        job_id: int | None = None
        reason: str | None = None
        try:
            job_id = self.spawn_fn(
                self.conn,
                parent_entry_id=int(entry_id),
                critique=critique.text,
                critique_by=critique.model,
                submitted_by=row["submitted_by"] or "",
                max_depth=self.lineage_depth,
                rules_file=self._parent_rules_file(row),
                publication="hold",
            )
        except ValueError as exc:
            reason = f"spawn refused: {exc}"
        if job_id is None and reason is None:
            reason = "the parent is a rejected entry, and a line does not grow from one"

        db.record_critique(
            self.conn,
            entry_id,
            critique=critique.text,
            critique_by=critique.model,
            prompt_version=version,
            spawned_job_id=job_id,
            rejected_reason=reason,
        )
        if job_id is None:
            self.log(f"idle: critiqued entry {entry_id}, spawned nothing: {reason}")
        else:
            generation = lineage.generation_of(self.conn, int(entry_id)) + 1
            self.log(
                f"idle: critiqued entry {entry_id} -> job {job_id} "
                f"(generation {generation})"
            )
        return True

    # -- the job ---------------------------------------------------------

    def _stop_now(self, job_id: int) -> None:
        control = self._control()
        reason = (control.reason if control else None) or "stop"
        job = db.get_job(self.conn, job_id)
        if job is not None and job.state in db.REQUEUABLE:
            db.requeue(self.conn, job_id, reason=f"stop-now: {reason}")
            self.log(f"job {job_id}: stop-now — attempt aborted, job re-queued")
        else:
            self.log(f"job {job_id}: stop-now — nothing in flight to abort")
        db.set_control(self.conn, "paused", reason)
        self.log("control: paused")

    def _abandon_for_restart(self, job_id: int) -> None:
        """SIGTERM with a job in flight: re-queue it and leave control alone.

        systemd's restart is not the operator's stop-now: the control row must
        still say ``running`` when the next worker starts, or a routine deploy
        would leave the queue paused until somebody noticed.
        """
        job = db.get_job(self.conn, job_id)
        if job is not None and job.state in db.REQUEUABLE:
            db.requeue(self.conn, job_id, reason="worker stopped: re-queued for the next worker")
            self.log(f"job {job_id}: worker stopping — attempt abandoned, job re-queued")

    def _on_terminate(self, signum: int, frame: Any) -> None:
        """SIGTERM: finish nothing. A job in flight is re-queued on the way out.

        Raising inside the model call or the gate is deliberate — an attempt
        that took a minute is cheaper than a job row left in ``executing`` with
        no worker, which is what a restart mid-attempt did on 2026-09-14 (job
        58 sat orphaned while the new worker ran job 59 beside it).
        """
        self._terminating = True
        if self._owned:
            raise StopNow()

    def _install_signal_handlers(self) -> bool:
        try:
            signal.signal(signal.SIGTERM, self._on_terminate)
        except ValueError:  # not the main thread: tests, embedding
            self.log("worker: SIGTERM handler not installed (not the main thread)")
            return False
        return True

    def _nap(self, seconds: float) -> None:
        """Sleep, in slices, so a SIGTERM while idle ends the loop promptly."""
        deadline = time.monotonic() + seconds
        while not self._terminating:
            left = deadline - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(1.0, left))

    def _pause_after_attempt(self, job_id: int) -> None:
        """The attempt is finished; settle into paused, leaving nothing stranded."""
        control = self._control()
        reason = (control.reason if control else None) or "paused"
        job = db.get_job(self.conn, job_id)
        if job is not None and job.state in db.REQUEUABLE:
            db.requeue(self.conn, job_id, reason=f"paused: {reason}")
            self.log(f"job {job_id}: attempt finished, job re-queued for resume")
        db.set_control(self.conn, "paused", reason)
        self.log("control: paused")

    def _create_entry(self, job_id: int, state: str, rules: str) -> int | None:
        """The gallery-visible row, written when the job stops moving.

        Spec §2: failures are entries too, so a job that exhausts its attempts
        gets one in state ``failed-kept``. Every column the schema has is filled
        from the job and its attempts here, while the worker still knows which
        attempt was the one that counted; packet 3.2 publishes from this row and
        never has to re-derive it.
        """
        job = db.get_job(self.conn, job_id)
        if job is None:  # pragma: no cover - the job was just transitioned
            return None
        attempts = db.list_attempts(self.conn, job_id)
        last = attempts[-1] if attempts else None

        report: dict[str, Any] = {}
        if last is not None and last.gate_report_path:
            try:
                report = json.loads(
                    Path(last.gate_report_path).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                report = {}
        artefacts = report.get("artefacts") or {}

        def total(name: str) -> float | int | None:
            values = [getattr(row, name) for row in attempts]
            values = [value for value in values if value is not None]
            if not values:
                return None
            return round(sum(values), 3) if name == "wall_s" else sum(values)

        entry_id = db.create_entry(
            self.conn,
            job_id,
            state=state,
            prompt=job.prompt,
            brief=job.brief,
            statement=last.statement if last else None,
            planner=job.planner,
            planner_prompt_version=self._planner_prompt_version,
            executor=last.model if last else self.executor_model,
            executor_prompt_version=last.prompt_version if last else None,
            rules_file=(last.rules_file if last and last.rules_file else rules),
            assertions_json=job.assertions_json,
            attempts=len(attempts),
            prompt_tokens=total("prompt_tokens"),
            completion_tokens=total("completion_tokens"),
            wall_s=total("wall_s"),
            shape=node_shape(),
            seed=report.get("seed", executor.DEFAULT_SEED),
            parent_entry_id=job.parent_entry_id,
            submitted_by=job.submitted_by,
            source_dir=last.source_dir if last else None,
            strip_path=artefacts.get("strip"),
            png_path=artefacts.get("png"),
        )
        self.log(f"job {job_id}: entry {entry_id} created, state {state}")
        # Packet 5.3. A spawned job has carried its critique since
        # lineage.spawn() queued it, because the `lineage` table keys on the
        # child ENTRY and the entry is only now a thing that exists. This is
        # the one place that link is recorded; a job with no parent records
        # nothing and record_child says so by returning None.
        if job.parent_entry_id:
            generation = lineage.record_child(self.conn, entry_id, job)
            self.log(
                f"job {job_id}: entry {entry_id} is generation {generation} of "
                f"entry {job.parent_entry_id}"
                + (f", critiqued by {job.critique_by}" if job.critique_by else "")
            )
        return entry_id

    def _save_plan_response(self, job_id: int, n: int, raw: str) -> str | None:
        """Keep what the planner actually said, beside the job it was about.

        The raw text is the only evidence of a format slip, and it is what a
        person reads when a job fails at PLAN. Named by try number, so the retry
        does not overwrite the answer that caused it.
        """
        if not raw:
            return None
        directory = self.jobs_dir / str(job_id)
        path = directory / f"plan-response-{n}.txt"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path.write_text(raw, encoding="utf-8")
        except OSError as exc:  # pragma: no cover - disk failure
            self.log(f"job {job_id}: could not save the planner's reply: {exc}")
            return None
        return str(path)

    def _plan(self, job: db.Job) -> db.Job | None:
        """Step 4. Returns the updated job, or None when it stops here.

        A planner failure is a job outcome, not a crash. On the first unattended
        night ``gemma4:e4b`` answered job 5 with no ``Brief`` heading, the
        ``PlannerFailed`` came out through the job loop, and the job sat in
        ``planning`` for good while the process went on to the next job. So the
        raw reply is saved, the plan is retried **once** — a format slip from a
        small model is usually transient — and a second failure fails the job
        with ``last_error`` beginning ``planner:``. No entry is created: nothing
        was made, so there is nothing to keep.
        """
        if job.brief and job.assertions:
            return job
        if (job.planner or "").strip() == "paid":
            db.transition(self.conn, job.id, "needs-laptop", needs="plan")
            self.log(f"job {job.id}: planner is 'paid' — needs-laptop, needs=plan "
                     "(DECIDE[credential-model] B: no paid key on the node)")
            return None

        plan = None
        reason = "the planner said nothing"
        last_raw = ""
        for n in range(1, PLAN_TRIES + 1):
            seed = plan_seed(job.id, n)
            self.log(f"job {job.id}: planning with {self.planner_model}, seed "
                     f"{seed} (try {n}/{PLAN_TRIES})")
            try:
                plan = self.planner_fn(
                    job=job, host=self.host, model=self.planner_model, seed=seed
                )
                break
            except StopNow:
                raise
            except Exception as exc:
                reason = " ".join(f"{exc}".split()) or type(exc).__name__
                raw = str(getattr(exc, "raw", "") or "")
                last_raw = raw or last_raw
                saved = self._save_plan_response(job.id, n, raw)
                where = f"; reply saved to {saved}" if saved else ""
                self.log(f"job {job.id}: plan try {n} (seed {seed}) failed "
                         f"({type(exc).__name__}: {reason}){where}")

        if plan is None:
            # Every strict try failed. Before giving up, read the last reply
            # leniently: on 2026-09-14 the same seed gave the same headingless
            # answer twice, and a missing heading over a good paragraph is a
            # format slip, not a job that cannot be planned.
            try:
                plan = planner.recover(last_raw)
            except Exception as exc:
                db.transition(self.conn, job.id, "failed",
                              last_error=f"planner: {reason}")
                self.log(f"job {job.id}: failed at PLAN after {PLAN_TRIES} tries — "
                         f"planner: {reason} (lenient parse: {exc})")
                return None
            self.log(f"job {job.id}: recovered the plan from the last reply with a "
                     f"lenient parse ({plan.prompt_version})")

        assertions = list(plan.assertions)
        self._planner_prompt_version = plan.prompt_version
        job = db.transition(
            self.conn,
            job.id,
            "executing",
            brief=plan.brief,
            assertions_json=json.dumps(assertions),
            planner=self.planner_model,
        )
        self.log(f"job {job.id}: planned, {plan.prompt_version}, assertions: "
                 f"{', '.join(assertions) or '(none)'}")
        return job

    def _run_job(self, job: db.Job) -> int:
        if job.state == "planning":
            job = self._plan(job)
            if job is None:
                return EXIT_OK
        self._check_stop()

        assertions = job.assertions
        rules = resolve_rules(job.rules_file, job.id)
        self.log(f"job {job.id}: rules_file {job.rules_file or 'unset'} -> {rules}")

        done = db.list_attempts(self.conn, job.id)
        evidence = done[-1].evidence if done else None
        first = len(done) + 1
        if done:
            self.log(f"job {job.id}: resuming at attempt {first} "
                     f"({len(done)} already recorded)")

        for n in range(first, job.max_attempts + 1):
            outcome = self._attempt(job, n, assertions, rules, evidence)
            if outcome is None:  # held or failed: the job is finished
                break
            evidence = outcome
            self._check_stop()
            if self._pause_requested():
                break
        if self._pause_requested():
            # The attempt in flight is finished, which is what `pause` promises.
            self._pause_after_attempt(job.id)
        return EXIT_OK

    def _preflight_lines(self, job_id: int, attempt_dir: Path) -> list[str]:
        """The pre-flight scan over one attempt directory, as evidence lines.

        Never raises into the job: a scan that fails is logged and dropped, the
        same bargain the idle round makes. The pre-flight is an explanation of a
        failure that has already happened, and no explanation is worth losing
        the evidence the gate did produce.
        """
        try:
            lines = preflight.evidence_lines(preflight.scan_dir(attempt_dir))
        except Exception as exc:  # noqa: BLE001 - deliberately everything
            self.log(f"job {job_id}: preflight scan failed: "
                     f"{type(exc).__name__}: {' '.join(f'{exc}'.split())}")
            return []
        for line in lines:
            self.log(f"job {job_id}: {line}")
        return lines

    def _attempt(
        self,
        job: db.Job,
        n: int,
        assertions: list[str],
        rules: str,
        evidence: str | None,
    ) -> str | None:
        """One execute+gate attempt. Returns the evidence for the next attempt,
        or None when the job is finished (held, failed)."""
        attempt_dir = self.jobs_dir / str(job.id) / f"attempt-{n}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        started = db.utc_now()
        last = n >= job.max_attempts
        brief = brief_with_evidence(job.brief or job.prompt, evidence)

        self.log(f"job {job.id}: attempt {n}/{job.max_attempts} executing into "
                 f"{attempt_dir}"
                 + (" with the previous attempt's evidence" if evidence else ""))
        try:
            execution = self.executor_fn(
                brief=brief,
                assertions=assertions,
                rules_file=rules,
                out_dir=str(attempt_dir),
                model=self.executor_model,
                host=self.host,
            )
        except StopNow:
            raise
        except Exception as exc:
            # executor.run() answers a bad response with ok=False rather than by
            # raising, but an injected executor, a disk failure or a bug can
            # still raise. It becomes the same thing: a recorded attempt with
            # evidence, and a repair — never an exception out of the job loop.
            execution = Execution(
                ok=False,
                error=f"{type(exc).__name__}: {' '.join(f'{exc}'.split())}",
                source_dir=str(attempt_dir),
                model=self.executor_model,
            )
        self._check_stop()

        if not execution.ok:
            failure = f"executor: {execution.error or 'malformed response'}"
            db.add_attempt(
                self.conn,
                job.id,
                n,
                started_utc=started,
                finished_utc=db.utc_now(),
                model=execution.model or self.executor_model,
                rules_file=rules,
                prompt_version=execution.prompt_version,
                prompt_tokens=execution.prompt_tokens,
                completion_tokens=execution.completion_tokens,
                prefill_s=execution.prefill_s,
                decode_s=execution.decode_s,
                wall_s=execution.wall_s,
                source_dir=execution.source_dir or str(attempt_dir),
                gate_exit=None,
                gate_report_path=None,
                evidence=failure,
                statement=execution.statement,
            )
            self.log(f"job {job.id}: attempt {n} — {failure} (no gate run)")
            if last:
                db.transition(self.conn, job.id, "failed",
                              last_error=failure.splitlines()[0])
                self.log(f"job {job.id}: failed after {n} attempt(s)")
                self._create_entry(job.id, "failed-kept", rules)
                return None
            # A malformed response never reaches the gate, so the job goes
            # straight from executing to repairing; the attempt row carries a
            # null gate_exit and says why.
            db.transition(self.conn, job.id, "repairing")
            db.transition(self.conn, job.id, "executing")
            return failure

        db.transition(self.conn, job.id, "gating")
        gate_out = attempt_dir / ".gate"
        asked = " ".join(f"--assert {word}" for word in assertions) or "(no assertions)"
        self.log(f"job {job.id}: attempt {n} gating {attempt_dir} {asked}")
        try:
            outcome = self.gate_fn(
                source_dir=str(attempt_dir),
                assertions=assertions,
                out_dir=str(gate_out),
            )
        except StopNow:
            raise
        except Exception as exc:
            # As with the executor: a gate that blows up is an attempt that
            # failed with no verdict, not a dead worker.
            outcome = GateOutcome(
                exit_code=None,
                report=None,
                report_path=None,
                stderr=f"{type(exc).__name__}: {' '.join(f'{exc}'.split())}",
            )
        passed = outcome.exit_code == 0
        gate_evidence = None if passed else build_evidence(
            outcome.report, outcome.exit_code, outcome.stderr
        )
        # The pre-flight runs only on a failure, and only ever adds to what the
        # gate said. `gate_evidence` is kept separate because its first line is
        # the gate's summary, and that line is what `jobs.last_error` shows.
        new_evidence = gate_evidence
        if gate_evidence is not None:
            new_evidence = evidence_with_preflight(
                gate_evidence, self._preflight_lines(job.id, attempt_dir)
            )
        db.add_attempt(
            self.conn,
            job.id,
            n,
            started_utc=started,
            finished_utc=db.utc_now(),
            model=execution.model or self.executor_model,
            rules_file=rules,
            prompt_version=execution.prompt_version,
            prompt_tokens=execution.prompt_tokens,
            completion_tokens=execution.completion_tokens,
            prefill_s=execution.prefill_s,
            decode_s=execution.decode_s,
            wall_s=execution.wall_s,
            source_dir=execution.source_dir or str(attempt_dir),
            gate_exit=outcome.exit_code,
            gate_report_path=outcome.report_path,
            evidence=new_evidence,
            statement=execution.statement,
        )
        self.log(f"job {job.id}: attempt {n} gate exit {outcome.exit_code}")

        if passed:
            db.transition(self.conn, job.id, "held")
            note = "" if job.publication != "auto" else (
                " (publication 'auto' still lands in held; publishing is packet 3.2)"
            )
            self.log(f"job {job.id}: held after {n} attempt(s){note}")
            self._create_entry(job.id, "held", rules)
            return None

        first_line = (gate_evidence or "gate failed").splitlines()[0]
        if last:
            db.transition(self.conn, job.id, "failed", last_error=first_line)
            self.log(f"job {job.id}: failed after {n} attempt(s): {first_line}")
            self._create_entry(job.id, "failed-kept", rules)
            return None

        db.transition(self.conn, job.id, "repairing")
        db.transition(self.conn, job.id, "executing")
        self.log(f"job {job.id}: repairing — {first_line}")
        return new_evidence
