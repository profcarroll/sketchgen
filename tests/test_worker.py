"""Unit tests for sketchgen.worker — the loop, with everything external stubbed.

Run:  python3 -m unittest discover -s tests -v

No model, no browser, no other process: the planner, the executor, the gate and
the fence probe are all injected callables. The stub gate writes a report.json in
sketch_gate.py's real schema, so the evidence builder is tested against the shape
the gate actually produces rather than against a convenient fiction.
"""

import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent

from sketchgen import db  # noqa: E402
from sketchgen import executor  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen import planner  # noqa: E402
from sketchgen import worker  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def make_report(sketch_dir, *, checks=None, assertions=None, notes=None,
                console=None, resources=None, exit_code=0):
    """One report.json in sketch_gate.py's schema (see its report construction)."""
    base_checks = {
        "console_clean": True,
        "is_looping": True,
        "frame_advancing": True,
        "sound_lib_ok": None,
        "audio_context_running": None,
    }
    base_checks.update(checks or {})
    return {
        "sketch_dir": str(sketch_dir),
        "seed": 1,
        "started_utc": "1970-01-01T00:00:00.000Z",
        "chromium": "/nonexistent/chromium-for-the-stub",
        "timings": {"launch_s": 0.0, "load_s": 0.0, "total_s": 0.0},
        "checks": base_checks,
        "assertions": assertions or {},
        "notes": notes or [],
        "resources": resources or [],
        "console": console or [],
        "artefacts": {
            "png": str(Path(sketch_dir) / ".gate" / "gate.png"),
            "strip": str(Path(sketch_dir) / ".gate" / "strip.png"),
            "log": str(Path(sketch_dir) / ".gate" / "console.log"),
        },
        "exit": exit_code,
    }


class StubExecutor:
    """Writes the files a real executor would and records how it was called."""

    def __init__(self, statement="a stub statement\n", ok=None, on_call=None,
                 sketch_js="function setup(){}\n"):
        self.calls = []
        self.statement = statement
        self.ok = list(ok) if ok is not None else None
        self.on_call = on_call
        # The sketch the stub "writes". Default: the smallest thing that is a
        # sketch. tests/test_preflight.py hands it one that shadows a p5 name.
        self.sketch_js = sketch_js

    def __call__(self, *, brief, assertions, rules_file, out_dir, model, host):
        n = len(self.calls) + 1
        self.calls.append(
            {"brief": brief, "assertions": list(assertions),
             "rules_file": rules_file, "out_dir": out_dir, "model": model}
        )
        if self.on_call is not None:
            self.on_call(n)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        ok = True if self.ok is None else self.ok[min(n, len(self.ok)) - 1]
        if not ok:
            (out / "response.txt").write_text("I would love to help!\n", encoding="utf-8")
            return worker.Execution(
                ok=False,
                error="malformed response: no fenced js block",
                source_dir=str(out),
                model=model,
                prompt_version="executor-v1",
                wall_s=0.5,
            )
        (out / "sketch.js").write_text(self.sketch_js, encoding="utf-8")
        (out / "index.html").write_text("<!DOCTYPE html>\n", encoding="utf-8")
        (out / "statement.md").write_text(self.statement, encoding="utf-8")
        return worker.Execution(
            ok=True,
            source_dir=str(out),
            model=model,
            prompt_version="executor-v1",
            prompt_tokens=2000 + n,
            completion_tokens=3000 + n,
            prefill_s=28.0,
            decode_s=90.0,
            wall_s=120.0,
            statement=self.statement,
        )


class StubGate:
    """Writes a real-schema report.json and returns the verdict it was given."""

    def __init__(self, verdicts, reports=None, on_call=None):
        self.verdicts = list(verdicts)
        self.reports = reports
        self.on_call = on_call
        self.calls = []

    def __call__(self, *, source_dir, assertions, out_dir):
        n = len(self.calls) + 1
        self.calls.append({"source_dir": source_dir, "assertions": list(assertions),
                           "out_dir": out_dir})
        code = self.verdicts[min(n, len(self.verdicts)) - 1]
        if self.reports is not None:
            report = self.reports[min(n, len(self.reports)) - 1]
        elif code == 0:
            report = make_report(source_dir, exit_code=0)
        else:
            report = make_report(
                source_dir,
                checks={"frame_advancing": False},
                assertions={"motion(idle)": {
                    "pass": False,
                    "detail": "idle 0->120 frames: mean abs diff 0.0000/255, "
                              "0 of 160000 pixels changed"}},
                notes=["frame_advancing compares frameCount at frame 84 of the "
                       "120-frame idle window (11) with the end of it (11); at "
                       "load it was 0",
                       "AudioContext state after the click probe: suspended"],
                console=[{"t": "1970-01-01T00:00:00.000Z", "type": "pageerror",
                          "text": "TypeError: x is not a function"}],
                exit_code=code,
            )
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "report.json"
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if self.on_call is not None:
            self.on_call(n)
        return worker.GateOutcome(exit_code=code, report=report, report_path=str(path))


def stub_plan(assertions=("motion(idle)",), brief="a brief the stub planner wrote"):
    def plan(*, job, host, model, seed=1):
        from sketchgen import planner

        return planner.Plan(brief=brief, assertions=list(assertions),
                            prompt_version="planner-v1", tokens={}, durations={},
                            raw="")

    return plan


class StubJudge:
    """Stands in for judge.run_local: records the call, records no verdict."""

    def __init__(self, judged=1):
        self.judged = judged
        self.calls = []

    def __call__(self, conn, *, model, host, limit, rng, probe=None, log=None,
                 **kwargs):
        self.calls.append({"model": model, "host": host, "limit": limit,
                           "probe": probe, "rng": rng})
        for index in range(self.judged):
            if log is not None:
                log(f"judged {index + 1} vs {index + 2}: brief=A look=B")
        return {"judged": self.judged, "recorded": 2 * self.judged,
                "pairs_offered": self.judged}


class StubCritic:
    """Stands in for lineage.critique: one sentence, or a validation failure."""

    def __init__(self, text="the same field, and this time let one circle fall "
                            "out of phase with the rest.", fails=None):
        self.text = text
        self.fails = fails
        self.calls = []
        # critic-v3: the real lineage.critique never returns without having
        # looked, so neither does the stub that stands in for it.
        self.strip_path = "/tmp/strip.png"
        self.strip_sha256 = "a" * 64

    def __call__(self, conn, entry_id, *, model, host, **kwargs):
        self.calls.append(entry_id)
        if self.fails:
            raise lineage.CritiqueFailed(self.fails, "Sure! Here is my review. "
                                                     "It has two sentences.")
        return lineage.Critique(
            text=self.text,
            model=model,
            prompt_version=lineage.prompt_version(),
            strip_path=self.strip_path,
            strip_sha256=self.strip_sha256,
            raw=self.text,
        )


FREE_SLOT = {"processes": [], "models": [], "ollama_error": None}
BUSY_SLOT = {"processes": ["4242 node /home/ubuntu/.local/bin/opencode"],
             "models": ["qwen3-coder:30b-a3b-q4_K_M"], "ollama_error": None}
SECOND_WORKER = {
    "processes": [],
    "workers": ["3077718 /home/ubuntu/sketchgen/.venv/bin/python3 "
                "/home/ubuntu/sketchgen/app/bin/sketchgen worker --once"],
    "models": [], "ollama_error": None,
}


# ---------------------------------------------------------------------------


class WorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.path = str(root / "test.db")
        self.jobs = root / "jobs"
        db.init(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.log = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(self.log.close)

        # Every transition the worker makes, in order, whichever helper made it.
        self.states = []
        real_transition = db.transition

        def recording(conn, job_id, new_state, **fields):
            self.states.append(new_state)
            return real_transition(conn, job_id, new_state, **fields)

        patcher = mock.patch.object(db, "transition", recording)
        patcher.start()
        self.addCleanup(patcher.stop)

    def enqueue(self, prompt="sixty drifting circles", by="octocat", **opts):
        opts.setdefault("brief", "a brief that arrived with the job")
        opts.setdefault("assertions", ["motion(idle)"])
        return db.enqueue(self.conn, prompt, by, **opts)

    def make_worker(self, *, executor_fn=None, gate_fn=None, planner_fn=None,
                    probe=None, judge_fn=None, critic_fn=None, spawn_fn=None,
                    idle_judge=0, idle_critique=0, lineage_depth=3,
                    log_stream=None):
        """A worker with everything external stubbed.

        Idle work is off by default, so a test that does not ask for it cannot
        reach the judge or the critic; the idle tests below turn the limits up.
        """
        return worker.Worker(
            self.conn,
            jobs_dir=self.jobs,
            gate_path="/nonexistent/sketch_gate.py",
            executor_fn=executor_fn or StubExecutor(),
            gate_fn=gate_fn or StubGate([0]),
            planner_fn=planner_fn or stub_plan(),
            judge_fn=judge_fn or StubJudge(judged=0),
            critic_fn=critic_fn or StubCritic(),
            spawn_fn=spawn_fn,
            idle_judge=idle_judge,
            idle_critique=idle_critique,
            lineage_depth=lineage_depth,
            probe=probe or (lambda: dict(FREE_SLOT)),
            log_stream=log_stream or self.log,
        )

    def attempts(self, job_id):
        return db.list_attempts(self.conn, job_id)

    def steps(self):
        """Every activity row this worker wrote, oldest first, by step name."""
        return [
            row["step"]
            for row in self.conn.execute("SELECT step FROM activity ORDER BY id")
        ]

    def activity_rows(self):
        return self.conn.execute("SELECT * FROM activity ORDER BY id").fetchall()

    def entries(self, job_id):
        return self.conn.execute(
            "SELECT * FROM entries WHERE job_id = ?", (job_id,)
        ).fetchall()


class TestHappyPath(WorkerTestCase):
    def test_queued_to_held_with_one_attempt(self):
        job_id = self.enqueue()
        run = self.make_worker()
        self.assertEqual(0, run.run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("held", job.state)
        self.assertEqual(["gating", "held"], self.states)

        rows = self.attempts(job_id)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(1, row.n)
        self.assertEqual(0, row.gate_exit)
        self.assertEqual("a stub statement\n", row.statement)
        self.assertIsNone(row.evidence)
        self.assertEqual("treatment", row.rules_file)
        self.assertEqual("executor-v1", row.prompt_version)
        self.assertEqual(2001, row.prompt_tokens)
        self.assertEqual(3001, row.completion_tokens)
        self.assertTrue(row.started_utc.endswith("Z"))
        self.assertTrue(row.gate_report_path.endswith("attempt-1/.gate/report.json"))

    def test_the_job_log_is_written_with_utc_stamps(self):
        job_id = self.enqueue()
        self.make_worker().run_once()
        text = (self.jobs / str(job_id) / "job.log").read_text(encoding="utf-8")
        self.assertIn("claimed", text)
        self.assertIn("gate exit 0", text)
        for line in text.splitlines():
            self.assertRegex(line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z  ")

    def test_publication_auto_still_lands_in_held(self):
        job_id = self.enqueue(publication="auto")
        self.make_worker().run_once()
        self.assertEqual("held", db.get_job(self.conn, job_id).state)


class TestRepair(WorkerTestCase):
    def test_a_failed_assertion_repairs_and_the_evidence_is_fed_back(self):
        job_id = self.enqueue()
        executor_fn = StubExecutor()
        run = self.make_worker(executor_fn=executor_fn, gate_fn=StubGate([1, 0]))
        self.assertEqual(0, run.run_once())

        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertIn("repairing", self.states)
        self.assertEqual(["gating", "repairing", "executing", "gating", "held"],
                         self.states)

        rows = self.attempts(job_id)
        self.assertEqual([1, 2], [row.n for row in rows])
        self.assertEqual([1, 0], [row.gate_exit for row in rows])
        self.assertIn("motion(idle)", rows[0].evidence)
        self.assertIsNone(rows[1].evidence)

        second = executor_fn.calls[1]["brief"]
        self.assertIn(worker.EVIDENCE_HEADING, second)
        self.assertIn("frame_advancing", second)
        self.assertIn("TypeError: x is not a function", second)
        self.assertIn("AudioContext state after the click probe: suspended", second)
        self.assertNotIn(worker.EVIDENCE_HEADING, executor_fn.calls[0]["brief"])


class TestMaxAttempts(WorkerTestCase):
    def test_exhausted_attempts_fail_the_job_with_the_last_evidence(self):
        job_id = self.enqueue(max_attempts=2)
        run = self.make_worker(gate_fn=StubGate([1, 1]))
        self.assertEqual(0, run.run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("failed", job.state)
        rows = self.attempts(job_id)
        self.assertEqual(2, len(rows))
        self.assertEqual(rows[-1].evidence.splitlines()[0], job.last_error)
        self.assertTrue(job.last_error.startswith("gate exit 1:"))


class TestFence(WorkerTestCase):
    def test_a_busy_slot_refuses_and_touches_nothing(self):
        job_id = self.enqueue()
        run = self.make_worker(probe=lambda: dict(BUSY_SLOT))
        self.assertEqual(3, run.run_once())

        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.attempts(job_id))
        self.assertEqual([], self.states)
        self.assertFalse(self.jobs.exists())

    def test_a_resident_model_alone_is_not_a_refusal(self):
        probe = {"processes": [], "models": ["qwen3-coder:30b-a3b-q4_K_M"],
                 "ollama_error": None}
        result = worker.fence(lambda: probe)
        self.assertTrue(result.ok)
        self.assertEqual(["qwen3-coder:30b-a3b-q4_K_M"], result.models)

    def test_an_unreachable_ollama_is_not_a_refusal(self):
        probe = {"processes": [], "models": [], "ollama_error": "connection refused"}
        self.assertTrue(worker.fence(lambda: probe).ok)

    def test_an_observed_client_is_reported_and_not_refused(self):
        """What `--stub-executor` injects: a run that calls no model says the
        slot was busy instead of refusing on it."""
        probe = {"processes": [], "observed_processes": BUSY_SLOT["processes"],
                 "models": [], "ollama_error": None}
        result = worker.fence(lambda: probe)
        self.assertTrue(result.ok)
        self.assertEqual(BUSY_SLOT["processes"], result.observed)

    def test_a_second_worker_is_refused(self):
        result = worker.fence(lambda: dict(SECOND_WORKER))
        self.assertFalse(result.ok)
        self.assertIn("another sketchgen worker is running", result.reason)
        self.assertEqual(SECOND_WORKER["workers"], result.workers)

    def test_a_second_worker_is_refused_even_in_test_mode(self):
        """The one refusal `observing_probe` must not soften.

        A stubbed run calls no model, so it cannot contend for inference — but
        it still claims jobs, which is the whole of what a second worker gets
        wrong. On 2026-09-21 that race wrote `IllegalTransition: failed ->
        gating` into the journal.
        """
        probe = dict(SECOND_WORKER, observed_processes=BUSY_SLOT["processes"])
        self.assertFalse(worker.fence(lambda: probe).ok)

    def test_a_second_worker_outranks_a_busy_slot_in_the_reason(self):
        probe = dict(SECOND_WORKER, processes=BUSY_SLOT["processes"])
        self.assertIn("another sketchgen worker", worker.fence(lambda: probe).reason)

    def test_observing_probe_softens_opencode_and_not_another_worker(self):
        seen = dict(SECOND_WORKER, processes=BUSY_SLOT["processes"])
        with mock.patch.object(worker, "default_probe", lambda host: dict(seen)):
            got = worker.observing_probe()()
        self.assertEqual([], got["processes"])
        self.assertEqual(BUSY_SLOT["processes"], got["observed_processes"])
        self.assertEqual(SECOND_WORKER["workers"], got["workers"])

    def test_a_run_beside_another_worker_refuses_and_touches_nothing(self):
        job_id = self.enqueue()
        log = io.StringIO()
        run = self.make_worker(probe=lambda: dict(SECOND_WORKER), log_stream=log)
        self.assertEqual(3, run.run_once())

        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.attempts(job_id))
        self.assertIn("another sketchgen worker is running", log.getvalue())

    def test_both_documented_invocations_read_as_a_worker(self):
        systemd = ["/home/ubuntu/sketchgen/.venv/bin/python3",
                   "/home/ubuntu/sketchgen/app/bin/sketchgen", "worker"]
        by_hand = ["python3", "bin/sketchgen", "worker", "--once",
                   "--db", "/home/ubuntu/sketchgen/sketchgen.db"]
        self.assertTrue(worker.is_worker_argv(systemd))
        self.assertTrue(worker.is_worker_argv(by_hand))

    def test_a_shell_that_merely_talks_about_the_worker_is_not_one(self):
        """Why this reads argv and not `pgrep -f`.

        `pgrep -f` matches its pattern anywhere in the command line, so every
        one of these would have refused the daemon for as long as it ran.
        """
        for argv in (
            ["/bin/bash", "-c", "python3 bin/sketchgen worker --once"],
            ["grep", "bin/sketchgen worker", "/home/ubuntu/sketchgen/jobs/1/job.log"],
            ["pgrep", "-af", "bin/sketchgen worker"],
            ["systemctl", "--user", "restart", "sketchgen-worker.service"],
            ["journalctl", "--user", "-u", "sketchgen-worker", "-n", "50"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(worker.is_worker_argv(argv))

    def test_the_other_subcommands_are_not_the_worker(self):
        for sub in ("web", "console", "db", "control", "gallery", "billing"):
            with self.subTest(sub=sub):
                self.assertFalse(
                    worker.is_worker_argv(["python3", "bin/sketchgen", sub])
                )

    def test_the_scan_does_not_count_this_process(self):
        """The daemon must never fence itself out."""
        self.assertEqual([], [
            line for line in worker.worker_processes()
            if line.startswith(f"{os.getpid()} ")
        ])


class TestControlPaused(WorkerTestCase):
    def test_paused_claims_nothing(self):
        job_id = self.enqueue()
        db.set_control(self.conn, "paused", "the node is wanted for something else")
        run = self.make_worker()
        self.assertEqual(0, run.run_once())
        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.attempts(job_id))
        self.assertEqual("paused", db.get_control(self.conn).state)


class TestControlPausing(WorkerTestCase):
    def test_pausing_with_nothing_in_flight_pauses_at_once(self):
        job_id = self.enqueue()
        db.set_control(self.conn, "pausing", "maintenance")
        run = self.make_worker()
        self.assertEqual(0, run.run_once())
        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.attempts(job_id))
        self.assertEqual("paused", db.get_control(self.conn).state)

    def test_pausing_during_an_attempt_finishes_that_attempt(self):
        job_id = self.enqueue()
        executor_fn = StubExecutor(
            on_call=lambda n: db.set_control(self.conn, "pausing", "maintenance")
        )
        run = self.make_worker(executor_fn=executor_fn)
        self.assertEqual(0, run.run_once())

        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual(1, len(self.attempts(job_id)))
        self.assertEqual(0, self.attempts(job_id)[0].gate_exit)
        self.assertEqual("paused", db.get_control(self.conn).state)

    def test_pausing_mid_job_requeues_so_the_job_resumes(self):
        job_id = self.enqueue(max_attempts=3)
        gate_fn = StubGate(
            [1, 0],
            on_call=lambda n: db.set_control(self.conn, "pausing", "maintenance")
            if n == 1 else None,
        )
        run = self.make_worker(gate_fn=gate_fn)
        self.assertEqual(0, run.run_once())

        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual(1, len(self.attempts(job_id)))
        self.assertEqual("paused", db.get_control(self.conn).state)

        # resume: the job is claimable again and attempt 2 carries on
        db.set_control(self.conn, "running")
        self.assertEqual(0, self.make_worker(gate_fn=gate_fn).run_once())
        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual([1, 2], [row.n for row in self.attempts(job_id)])


class TestStopNow(WorkerTestCase):
    def test_stop_between_attempts_requeues_and_keeps_the_attempt_rows(self):
        job_id = self.enqueue(max_attempts=3)
        gate_fn = StubGate(
            [1, 0],
            on_call=lambda n: db.set_control(self.conn, "pausing", "stop")
            if n == 1 else None,
        )
        run = self.make_worker(gate_fn=gate_fn)
        self.assertEqual(0, run.run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("queued", job.state)
        self.assertIn("stop-now", job.last_error)
        self.assertEqual(1, len(self.attempts(job_id)))
        self.assertEqual(1, self.attempts(job_id)[0].gate_exit)
        self.assertEqual("paused", db.get_control(self.conn).state)
        self.assertEqual("stop", db.get_control(self.conn).reason)
        self.assertEqual(1, len(gate_fn.calls))

    def test_is_stop_now_reads_the_reason_not_a_new_state(self):
        self.assertTrue(worker.is_stop_now(db.set_control(self.conn, "pausing", "stop")))
        self.assertTrue(
            worker.is_stop_now(db.set_control(self.conn, "pausing", "stop: swap models"))
        )
        self.assertFalse(
            worker.is_stop_now(db.set_control(self.conn, "pausing", "maintenance"))
        )
        self.assertFalse(worker.is_stop_now(db.set_control(self.conn, "paused", "stop")))


class TestPaidPlanner(WorkerTestCase):
    def test_a_paid_planner_stops_at_needs_laptop(self):
        job_id = db.enqueue(self.conn, "a paid plan, please", "octocat", planner="paid")
        run = self.make_worker()
        self.assertEqual(0, run.run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("needs-laptop", job.state)
        self.assertEqual("plan", job.needs)
        self.assertEqual([], self.attempts(job_id))

    def test_a_local_planner_fills_the_brief_and_the_assertions(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        run = self.make_worker(
            planner_fn=stub_plan(assertions=("motion(idle)", "responds(click)"))
        )
        self.assertEqual(0, run.run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("held", job.state)
        self.assertEqual("a brief the stub planner wrote", job.brief)
        self.assertEqual(["motion(idle)", "responds(click)"], job.assertions)
        self.assertEqual(["executing", "gating", "held"], self.states)


class TestTheJobsOwnPlannerModel(WorkerTestCase):
    """``jobs.planner`` has said "a model id, or 'paid'" since migration 001.
    Until 2026-09-19 only the 'paid' half was read; the New job page can now
    name a model, so the model id half has to reach the call."""

    def recording_planner(self):
        seen = []

        def plan(*, job, host, model, seed=1):
            seen.append(model)
            return planner.Plan(brief="a brief the stub planner wrote",
                                assertions=["motion(idle)"],
                                prompt_version="planner-v1", tokens={},
                                durations={}, raw="")

        plan.seen = seen
        return plan

    def test_the_model_the_job_names_is_the_model_that_is_called(self):
        job_id = db.enqueue(self.conn, "planned by a model of its own", "octocat",
                            planner="qwen3.5:9b")
        plan = self.recording_planner()
        self.assertEqual(0, self.make_worker(planner_fn=plan).run_once())

        self.assertEqual(["qwen3.5:9b"], plan.seen)
        self.assertEqual("qwen3.5:9b", db.get_job(self.conn, job_id).planner)

    def test_the_model_the_job_names_is_the_model_the_entry_records(self):
        job_id = db.enqueue(self.conn, "provenance, not a guess", "octocat",
                            planner="qwen3.5:9b")
        self.assertEqual(0, self.make_worker(
            planner_fn=self.recording_planner()).run_once())
        row = self.entries(job_id)[0]
        self.assertEqual("qwen3.5:9b", row["planner"])

    def test_the_activity_row_says_which_model_is_planning(self):
        db.enqueue(self.conn, "say which one", "octocat", planner="qwen3.5:9b")
        self.make_worker(planner_fn=self.recording_planner()).run_once()
        row = next(r for r in self.activity_rows() if r["step"] == "planning")
        self.assertEqual("qwen3.5:9b", row["model"])
        self.assertIn("qwen3.5:9b", row["detail"])

    def test_an_empty_column_still_means_the_workers_default(self):
        db.enqueue(self.conn, "nobody chose", "octocat")
        plan = self.recording_planner()
        self.make_worker(planner_fn=plan).run_once()
        self.assertEqual([worker.DEFAULT_PLANNER_MODEL], plan.seen)

    def test_the_word_local_from_an_older_page_means_the_default_too(self):
        db.enqueue(self.conn, "an old row", "octocat", planner="local")
        plan = self.recording_planner()
        self.make_worker(planner_fn=plan).run_once()
        self.assertEqual([worker.DEFAULT_PLANNER_MODEL], plan.seen)

    def test_paid_is_still_read_before_any_model_is_chosen(self):
        job_id = db.enqueue(self.conn, "a paid plan", "octocat", planner="paid")
        plan = self.recording_planner()
        self.make_worker(planner_fn=plan).run_once()
        self.assertEqual([], plan.seen)
        self.assertEqual("needs-laptop", db.get_job(self.conn, job_id).state)


class TestTheJobsOwnExecutorModel(WorkerTestCase):
    """`jobs.executor` is a column migration 001 created and nothing ever wrote.
    The New job page writes it now, so the execute step has to read it."""

    def test_the_model_the_job_names_is_the_model_that_writes(self):
        job_id = self.enqueue("written by a model of its own",
                              executor="qwen3.5:9b")
        run = self.make_worker(executor_fn=(stub := StubExecutor()))
        self.assertEqual(0, run.run_once())
        self.assertEqual("qwen3.5:9b", stub.calls[0]["model"])
        self.assertEqual("qwen3.5:9b", self.attempts(job_id)[0].model)

    def test_the_model_the_job_names_is_the_model_the_entry_records(self):
        job_id = self.enqueue("provenance, not a guess", executor="qwen3.5:9b")
        self.make_worker().run_once()
        self.assertEqual("qwen3.5:9b", self.entries(job_id)[0]["executor"])

    def test_the_activity_row_says_which_model_is_writing(self):
        self.enqueue("say which one", executor="qwen3.5:9b")
        self.make_worker().run_once()
        row = next(r for r in self.activity_rows() if r["step"] == "writing")
        self.assertEqual("qwen3.5:9b", row["model"])
        self.assertIn("qwen3.5:9b", row["detail"])

    def test_every_attempt_of_a_repair_uses_it_too(self):
        job_id = self.enqueue(max_attempts=3, executor="qwen3.5:9b")
        stub = StubExecutor(ok=[False, True])
        self.make_worker(executor_fn=stub).run_once()
        self.assertEqual(["qwen3.5:9b", "qwen3.5:9b"],
                         [call["model"] for call in stub.calls])
        self.assertEqual(["qwen3.5:9b", "qwen3.5:9b"],
                         [row.model for row in self.attempts(job_id)])

    def test_an_empty_column_still_means_the_workers_default(self):
        self.enqueue("nobody chose")
        run = self.make_worker(executor_fn=(stub := StubExecutor()))
        run.run_once()
        self.assertEqual(executor.DEFAULT_MODEL, stub.calls[0]["model"])

    def test_the_word_local_from_an_older_page_means_the_default_too(self):
        self.enqueue("an old row", executor="local")
        run = self.make_worker(executor_fn=(stub := StubExecutor()))
        run.run_once()
        self.assertEqual(executor.DEFAULT_MODEL, stub.calls[0]["model"])

    def test_the_two_models_are_read_from_their_own_columns(self):
        """One job, two different models, neither borrowing the other's."""
        # brief unset and assertions empty, so the planner really runs
        job_id = self.enqueue("a job with both named", executor="qwen3.5:9b",
                              brief=None, assertions=[])
        seen = []

        def plan(*, job, host, model, seed=1):
            seen.append(model)
            return planner.Plan(brief="a brief", assertions=["motion(idle)"],
                                prompt_version="planner-v1", tokens={},
                                durations={}, raw="")

        stub = StubExecutor()
        self.make_worker(planner_fn=plan, executor_fn=stub).run_once()
        self.assertEqual([worker.DEFAULT_PLANNER_MODEL], seen)
        self.assertEqual("qwen3.5:9b", stub.calls[0]["model"])
        row = self.entries(job_id)[0]
        self.assertEqual(worker.DEFAULT_PLANNER_MODEL, row["planner"])
        self.assertEqual("qwen3.5:9b", row["executor"])


class TestMalformedExecutorResponse(WorkerTestCase):
    def test_the_attempt_is_recorded_and_the_job_repairs(self):
        job_id = self.enqueue(max_attempts=3)
        executor_fn = StubExecutor(ok=[False, True])
        gate_fn = StubGate([0])
        run = self.make_worker(executor_fn=executor_fn, gate_fn=gate_fn)
        self.assertEqual(0, run.run_once())

        rows = self.attempts(job_id)
        self.assertEqual([1, 2], [row.n for row in rows])
        self.assertIsNone(rows[0].gate_exit)
        self.assertTrue(rows[0].evidence.startswith("executor:"))
        self.assertEqual(1, len(gate_fn.calls))  # the malformed attempt never gated
        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertIn("repairing", self.states)
        self.assertIn("executor: malformed response", executor_fn.calls[1]["brief"])

    def test_a_malformed_last_attempt_fails_the_job(self):
        job_id = self.enqueue(max_attempts=1)
        run = self.make_worker(executor_fn=StubExecutor(ok=[False]))
        self.assertEqual(0, run.run_once())
        job = db.get_job(self.conn, job_id)
        self.assertEqual("failed", job.state)
        self.assertTrue(job.last_error.startswith("executor:"))


class TestRandomRules(WorkerTestCase):
    def test_random_resolves_deterministically_per_job_id(self):
        seen = {job_id: worker.resolve_rules("random", job_id) for job_id in range(1, 21)}
        for job_id, choice in seen.items():
            self.assertIn(choice, ("control", "treatment"))
            self.assertEqual(choice, worker.resolve_rules("random", job_id))
        self.assertEqual({"control", "treatment"}, set(seen.values()))

    def test_the_resolved_side_is_what_the_attempt_records(self):
        job_id = self.enqueue(rules_file="random")
        expected = worker.resolve_rules("random", job_id)
        executor_fn = StubExecutor()
        self.assertEqual(0, self.make_worker(executor_fn=executor_fn).run_once())
        self.assertEqual(expected, self.attempts(job_id)[0].rules_file)
        self.assertEqual(expected, executor_fn.calls[0]["rules_file"])

    def test_a_job_that_names_no_rules_file_gets_the_default(self):
        job_id = self.enqueue()
        self.make_worker().run_once()
        self.assertEqual(worker.DEFAULT_RULES, self.attempts(job_id)[0].rules_file)


class TestEntries(WorkerTestCase):
    def test_the_happy_path_creates_one_held_entry(self):
        job_id = self.enqueue(rules_file="treatment")
        self.make_worker().run_once()

        rows = self.entries(job_id)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("held", row["state"])
        self.assertEqual(self.attempts(job_id)[0].statement, row["statement"])
        self.assertEqual("sixty drifting circles", row["prompt"])
        self.assertEqual("octocat", row["submitted_by"])
        self.assertEqual("treatment", row["rules_file"])
        self.assertEqual(1, row["attempts"])
        self.assertEqual(2001, row["prompt_tokens"])
        self.assertEqual(3001, row["completion_tokens"])
        self.assertEqual(120.0, row["wall_s"])
        self.assertEqual("executor-v1", row["executor_prompt_version"])
        self.assertEqual(json.dumps(["motion(idle)"]), row["assertions_json"])
        self.assertTrue(row["source_dir"].endswith("attempt-1"))
        self.assertTrue(row["png_path"].endswith(".gate/gate.png"))
        self.assertTrue(row["strip_path"].endswith(".gate/strip.png"))
        self.assertTrue(row["shape"])
        self.assertEqual(1, row["seed"])
        self.assertTrue(row["created_utc"].endswith("Z"))

    def test_an_exhausted_job_is_kept_as_an_entry_too(self):
        job_id = self.enqueue(max_attempts=2)
        self.make_worker(gate_fn=StubGate([1, 1])).run_once()

        rows = self.entries(job_id)
        self.assertEqual(1, len(rows))
        self.assertEqual("failed-kept", rows[0]["state"])
        self.assertEqual(2, rows[0]["attempts"])
        self.assertEqual(self.attempts(job_id)[-1].statement, rows[0]["statement"])

    def test_a_job_that_only_missed_the_plan_goes_to_a_person(self):
        """39 of the first 65 failed jobs had an attempt like this one.

        Nothing that protects a visitor went wrong — no throw, no freeze, inside
        the frame budget. It simply is not what the planner predicted, and the
        planner is a model. That is a judgement for a person, so it lands in the
        same queue a clean pass lands in.
        """
        offplan = make_report(
            "/tmp/x",
            assertions={"responds(drag)": {"pass": False, "detail": "0 of 9 changed"}},
            exit_code=1,
        )
        job_id = self.enqueue(max_attempts=2)
        self.make_worker(
            gate_fn=StubGate([1, 1], reports=[offplan, offplan])
        ).run_once()

        row = self.entries(job_id)[0]
        self.assertEqual("held", row["state"])
        self.assertEqual(["responds(drag)"], json.loads(row["offplan_json"]))
        self.assertEqual("held", db.get_job(self.conn, job_id).state)

    def test_a_job_that_broke_the_browser_is_still_a_failure(self):
        """QA keeps its teeth. A sketch that throws is not off-plan, it is broken."""
        job_id = self.enqueue(max_attempts=2)
        self.make_worker(gate_fn=StubGate([1, 1])).run_once()   # frame_advancing false
        row = self.entries(job_id)[0]
        self.assertEqual("failed-kept", row["state"])
        self.assertIsNone(row["offplan_json"])
        self.assertEqual("failed", db.get_job(self.conn, job_id).state)

    def test_the_entry_keeps_the_best_attempt_not_the_last(self):
        """Entry 429 publishes a blank tenth attempt; its second drew a puzzle.

        The last attempt is usually the best one and the ranking agrees with it;
        on the first 45 kept entries this moves 7. Entry 429 is one of the 7,
        which is the whole reason the ranking exists.
        """
        good = make_report(
            "/tmp/x",
            assertions={"motion(idle)": {"pass": True, "detail": "ok"}},
            exit_code=1,
        )
        broken = make_report(
            "/tmp/x",
            checks={"console_clean": False},
            assertions={"motion(idle)": {"pass": False, "detail": "0 of 9 changed"}},
            exit_code=1,
        )
        job_id = self.enqueue(max_attempts=3)
        self.make_worker(
            gate_fn=StubGate([1, 1, 1], reports=[broken, good, broken])
        ).run_once()

        row = self.entries(job_id)[0]
        self.assertTrue(row["source_dir"].endswith("attempt-2"), row["source_dir"])
        self.assertEqual(3, row["attempts"])      # the record still counts all three
        self.assertEqual(self.attempts(job_id)[1].statement, row["statement"])
        # attempt 2 ran clean and satisfied its one assertion, so it is a pass
        # in everything but name and goes to a person, with nothing missed.
        self.assertEqual("held", row["state"])
        self.assertIsNone(row["offplan_json"])

    def test_a_passing_job_still_keeps_its_passing_attempt(self):
        """The ranking must not disturb the ordinary case."""
        job_id = self.enqueue(max_attempts=3)
        self.make_worker(gate_fn=StubGate([1, 0])).run_once()
        row = self.entries(job_id)[0]
        self.assertEqual("held", row["state"])
        self.assertTrue(row["source_dir"].endswith("attempt-2"), row["source_dir"])

    def test_the_planner_prompt_version_lands_on_the_entry(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        self.make_worker().run_once()
        self.assertEqual("planner-v1", self.entries(job_id)[0]["planner_prompt_version"])


class IdleTestCase(WorkerTestCase):
    """Packet 5.4: what the worker does with an empty queue."""

    def publish(self, prompt="a field of dots", by="octocat", rules="random"):
        """One published entry, its job walked out of the queue first.

        A job left in `queued` would mean the queue is not empty and there
        would be no idle round to test.
        """
        job_id = db.enqueue(self.conn, prompt, by, rules_file=rules,
                            brief="a brief", assertions=["motion(idle)"])
        for state in ("executing", "gating", "held", "published"):
            db.transition(self.conn, job_id, state)
        entry_id = db.create_entry(
            self.conn, job_id, state="published", prompt=prompt, brief="a brief",
            statement="the model's own words", submitted_by=by,
            rules_file="treatment", assertions_json=json.dumps(["motion(idle)"]),
            strip_path="/tmp/strip.png",  # critic-v3 only offers entries it can see
        )
        self.states.clear()
        return job_id, entry_id

    def publish_without_a_strip(self, prompt="a blind field", by="octocat"):
        """A published entry the gate left no strip for: the critic is blind to it."""
        job_id = db.enqueue(self.conn, prompt, by, rules_file="random",
                            brief="a brief", assertions=["motion(idle)"])
        for state in ("executing", "gating", "held", "published"):
            db.transition(self.conn, job_id, state)
        entry_id = db.create_entry(
            self.conn, job_id, state="published", prompt=prompt, brief="a brief",
            statement="the model's own words", submitted_by=by,
            rules_file="treatment", assertions_json=json.dumps(["motion(idle)"]),
        )
        self.states.clear()
        return job_id, entry_id

    def critiques(self):
        return self.conn.execute(
            "SELECT * FROM critiques ORDER BY id"
        ).fetchall()

    def child_jobs(self):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE parent_entry_id IS NOT NULL ORDER BY id"
        ).fetchall()


class TestIdleRound(IdleTestCase):
    def test_an_empty_queue_judges_a_pair_and_critiques_an_entry(self):
        _, first = self.publish(prompt="a field of dots")
        self.publish(prompt="a second field")
        judge_fn = StubJudge(judged=1)
        critic_fn = StubCritic()
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())

        self.assertEqual(1, len(judge_fn.calls))
        self.assertEqual(1, judge_fn.calls[0]["limit"])
        self.assertEqual(worker.DEFAULT_JUDGE_MODEL, judge_fn.calls[0]["model"])
        # the judge is handed the worker's own probe: it must not refuse the
        # process that already owns the slot
        self.assertIs(run.probe, judge_fn.calls[0]["probe"])

        self.assertEqual([first], critic_fn.calls)  # the oldest one lacking a critique
        rows = self.critiques()
        self.assertEqual(1, len(rows))
        self.assertEqual(first, rows[0]["entry_id"])
        self.assertEqual(critic_fn.text, rows[0]["critique"])
        self.assertEqual(worker.DEFAULT_CRITIC_MODEL, rows[0]["critique_by"])
        self.assertEqual(lineage.prompt_version(), rows[0]["prompt_version"])
        self.assertIsNone(rows[0]["rejected_reason"])
        self.assertTrue(rows[0]["created_utc"].endswith("Z"))

        children = self.child_jobs()
        self.assertEqual(1, len(children))
        child = children[0]
        self.assertEqual(rows[0]["spawned_job_id"], child["id"])
        self.assertEqual(first, child["parent_entry_id"])
        self.assertEqual("queued", child["state"])
        self.assertEqual("octocat", child["submitted_by"])  # the parent's, not a model
        self.assertEqual("gemma4:e4b", child["critique_by"])
        self.assertEqual("hold", child["publication"])
        # the line inherits the SETTING, so a random line stays random
        self.assertEqual("random", child["rules_file"])
        self.assertIn(lineage.REVISE_HEADING, child["prompt"])

    def test_a_blind_entry_does_not_stall_the_idle_critic(self):
        """critic-v3 regression: a strip-less entry must not hold the line.

        It is published first, so oldest-first would offer it on every round;
        a refusal writes no critique row, so nothing would ever mark it tried
        and the entry behind it would never be critiqued.
        """
        _, blind = self.publish_without_a_strip()
        _, sighted = self.publish(prompt="a field with a strip")
        critic_fn = StubCritic()
        run = self.make_worker(judge_fn=StubJudge(judged=0), critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())

        self.assertEqual([sighted], critic_fn.calls)  # stepped over the blind one
        rows = self.critiques()
        self.assertEqual(1, len(rows))
        self.assertEqual(sighted, rows[0]["entry_id"])
        self.assertEqual(
            [], [r["entry_id"] for r in rows if r["entry_id"] == blind]
        )

    def test_a_second_round_judges_nothing_and_critiques_nothing(self):
        self.publish()  # one entry, so the first round leaves nothing to do
        critic_fn = StubCritic()
        self.make_worker(judge_fn=StubJudge(judged=1), critic_fn=critic_fn,
                         idle_judge=1, idle_critique=1).run_once()
        # the child the first round spawned would otherwise be claimed
        db.transition(self.conn, self.child_jobs()[0]["id"], "failed")

        log = io.StringIO()
        judge_fn = StubJudge(judged=0)
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1, log_stream=log)
        self.assertEqual(0, run.run_once())

        self.assertEqual(1, len(judge_fn.calls))  # asked, and told there were none
        self.assertEqual(1, len(critic_fn.calls))  # not asked a second time
        self.assertEqual(1, len(self.critiques()))
        self.assertIn("idle: nothing to judge; nothing to critique", log.getvalue())

    def test_both_switches_off_call_nothing(self):
        self.publish()
        self.publish(prompt="a second field")
        judge_fn, critic_fn = StubJudge(), StubCritic()
        log = io.StringIO()
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=0, idle_critique=0, log_stream=log)
        self.assertEqual(0, run.run_once())

        self.assertEqual([], judge_fn.calls)
        self.assertEqual([], critic_fn.calls)
        self.assertEqual([], self.critiques())
        self.assertIn("SKETCHGEN_IDLE_JUDGE=0", log.getvalue())
        self.assertIn("SKETCHGEN_IDLE_CRITIQUE=0", log.getvalue())

    def test_paused_does_no_idle_work(self):
        self.publish()
        db.set_control(self.conn, "paused", "maintenance")
        judge_fn, critic_fn = StubJudge(), StubCritic()
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())
        self.assertEqual([], judge_fn.calls)
        self.assertEqual([], critic_fn.calls)

    def test_a_queued_job_takes_precedence_over_idle_work(self):
        self.publish()
        job_id = self.enqueue()
        judge_fn, critic_fn = StubJudge(), StubCritic()
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())

        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual([], judge_fn.calls)
        self.assertEqual([], critic_fn.calls)


class TestIdlePause(IdleTestCase):
    """A pause asked for *during* idle work is taken between its steps.

    Regression for 2026-09-21: the control row was read once per pass and not
    again, so a stop that arrived while the judge sat on the model host was
    invisible until the whole round was over — and the round started a fresh
    critique first. On the node the stop was asked for at 02:30:56 and a new
    critique began at 02:33:46, after the judge's ten-minute timeout.
    """

    def stopping_judge(self, reason):
        """A judge that asks for the pause while it is running, as the operator does."""

        class StopsMidJudge(StubJudge):
            def __call__(inner, conn, **kwargs):
                db.set_control(conn, "pausing", reason)
                return StubJudge.__call__(inner, conn, **kwargs)

        return StopsMidJudge(judged=1)

    def test_a_stop_during_the_judge_is_taken_before_the_critic(self):
        self.publish()
        self.publish(prompt="a second field")
        critic_fn = StubCritic()
        log = io.StringIO()
        run = self.make_worker(judge_fn=self.stopping_judge("stop"),
                               critic_fn=critic_fn, idle_judge=1,
                               idle_critique=1, log_stream=log)
        self.assertEqual(0, run.run_once())

        self.assertEqual([], critic_fn.calls)
        self.assertEqual([], self.critiques())
        self.assertNotIn("critiquing", self.steps())
        self.assertEqual("paused", db.get_control(self.conn).state)
        self.assertIn("the judge has finished", log.getvalue())
        self.assertIn("now paused", log.getvalue())

    def test_a_plain_pause_during_the_judge_is_taken_too(self):
        """Idle work holds no attempt, so both flavours settle at once."""
        self.publish()
        critic_fn = StubCritic()
        run = self.make_worker(
            judge_fn=self.stopping_judge("someone else wants the slot"),
            critic_fn=critic_fn, idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())

        self.assertEqual([], critic_fn.calls)
        self.assertEqual("paused", db.get_control(self.conn).state)

    def test_a_stop_during_the_critic_ends_the_round_after_it(self):
        """The critique it was in the middle of is kept — it is finished work."""
        _, entry_id = self.publish()

        class StopsMidCritique(StubCritic):
            def __call__(inner, conn, eid, **kwargs):
                db.set_control(conn, "pausing", "stop")
                return StubCritic.__call__(inner, conn, eid, **kwargs)

        run = self.make_worker(critic_fn=StopsMidCritique(), idle_critique=1)
        self.assertEqual(0, run.run_once())

        rows = self.critiques()
        self.assertEqual(1, len(rows))
        self.assertEqual(entry_id, rows[0]["entry_id"])
        self.assertEqual("paused", db.get_control(self.conn).state)

    def test_a_round_nobody_interrupts_still_judges_and_critiques(self):
        """The check costs the round nothing when no pause was asked for."""
        _, entry_id = self.publish()
        self.publish(prompt="a second field")
        critic_fn = StubCritic()
        run = self.make_worker(judge_fn=StubJudge(judged=1), critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())

        self.assertEqual([entry_id], critic_fn.calls)
        self.assertEqual("running", db.get_control(self.conn).state)

    def test_the_pause_still_holds_on_the_next_pass(self):
        """The round settles the row, so the pass after it claims nothing."""
        self.publish()
        run = self.make_worker(judge_fn=self.stopping_judge("stop"),
                               critic_fn=StubCritic(), idle_judge=1, idle_critique=1)
        # an empty queue, so the pass reaches the idle round and pauses inside it
        self.assertEqual(0, run.run_once())
        self.assertEqual("paused", db.get_control(self.conn).state)

        job_id = self.enqueue()  # work arriving after the pause waits for a resume
        self.assertEqual(0, run.run_once())
        self.assertEqual("queued", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.attempts(job_id))


class TestIdleCritiqueRejected(IdleTestCase):
    def test_an_invalid_critique_is_kept_and_spawns_nothing(self):
        _, entry_id = self.publish()
        critic_fn = StubCritic(fails="the critique is 2 sentences; one is the contract")
        run = self.make_worker(critic_fn=critic_fn, idle_critique=1)
        self.assertEqual(0, run.run_once())

        rows = self.critiques()
        self.assertEqual(1, len(rows))
        self.assertEqual(entry_id, rows[0]["entry_id"])
        self.assertIsNone(rows[0]["spawned_job_id"])
        self.assertIn("one is the contract", rows[0]["rejected_reason"])
        self.assertIn("two sentences", rows[0]["critique"])  # what it actually said
        self.assertEqual([], self.child_jobs())

    def test_the_rejected_entry_is_not_offered_again(self):
        self.publish()
        critic_fn = StubCritic(fails="the critique contains code")
        self.make_worker(critic_fn=critic_fn, idle_critique=1).run_once()
        self.make_worker(critic_fn=critic_fn, idle_critique=1).run_once()
        self.assertEqual(1, len(critic_fn.calls))
        self.assertEqual(1, len(self.critiques()))


class TestIdleLineageDepth(IdleTestCase):
    def test_a_child_at_the_depth_limit_waits_for_a_person(self):
        _, root = self.publish(prompt="generation zero")
        _, second = self.publish(prompt="generation one")
        _, third = self.publish(prompt="generation two")
        db.add_lineage(self.conn, second, root, generation=1)
        db.add_lineage(self.conn, third, second, generation=2)
        # root and second already have children, so `third` is the candidate
        run = self.make_worker(critic_fn=StubCritic(), idle_critique=1,
                               lineage_depth=3)
        self.assertEqual(0, run.run_once())

        children = self.child_jobs()
        self.assertEqual(1, len(children))
        self.assertEqual(third, children[0]["parent_entry_id"])
        # generation 3 == DECIDE[lineage-depth]: created, held, and waiting
        self.assertEqual("review", children[0]["needs"])
        self.assertEqual("hold", children[0]["publication"])


class TestIdleSummary(IdleTestCase):
    def test_the_summary_is_read_from_the_database(self):
        _, entry_id = self.publish()
        self.make_worker(critic_fn=StubCritic(), idle_critique=1).run_once()
        summary = worker.idle_summary(self.conn)
        self.assertEqual(1, summary["critiques"])
        self.assertEqual(1, summary["spawned"])
        self.assertEqual(0, summary["rejected"])
        self.assertEqual(0, summary["agent_verdicts"])
        self.assertEqual(
            self.critiques()[0]["created_utc"], summary["last_action_utc"]
        )

    def test_an_untouched_database_summarises_as_zeros(self):
        summary = worker.idle_summary(self.conn)
        self.assertEqual(
            {"agent_verdicts": 0, "critiques": 0, "spawned": 0, "rejected": 0,
             "last_action_utc": None},
            summary,
        )


class TestIdleEnv(unittest.TestCase):
    def test_the_limits_come_from_the_environment_and_tolerate_junk(self):
        with mock.patch.dict(os.environ, {"SKETCHGEN_IDLE_JUDGE": "4"}):
            self.assertEqual(4, worker._int_env("SKETCHGEN_IDLE_JUDGE", 1))
        with mock.patch.dict(os.environ, {"SKETCHGEN_IDLE_JUDGE": "many"}):
            self.assertEqual(1, worker._int_env("SKETCHGEN_IDLE_JUDGE", 1))
        with mock.patch.dict(os.environ, {}, clear=False):
            self.assertEqual(3, worker._int_env("SKETCHGEN_NOT_SET_ANYWHERE", 3))


class FlakyPlanner:
    """A planner that fails its first ``failures`` calls, then succeeds.

    The default failure is the real one from the node on 2026-09-14: the model
    answered with no Brief heading and planner.py raised PlannerFailed with the
    reply attached.
    """

    RAW = "Sure! Here are some ideas for the sketch:\n- drifting circles\n"
    #: A reply with nothing a lenient parse could take a brief from.
    UNUSABLE = "Assertions:\n- motion(idle)\n"

    def __init__(self, failures=1, error=None, raw=None):
        self.failures = failures
        self.error = error
        self.raw = self.RAW if raw is None else raw
        self.calls = 0
        self.seeds = []

    def __call__(self, *, job, host, model, seed=1):
        self.calls += 1
        self.seeds.append(seed)
        if self.calls <= self.failures:
            if self.error is not None:
                raise self.error
            raise planner.PlannerFailed("no 'Brief' heading in the response",
                                        self.raw)
        return planner.Plan(
            brief="a brief the planner wrote on the retry",
            assertions=["motion(idle)"],
            prompt_version="planner-v1",
            tokens={},
            durations={},
            raw="",
        )


class ExplodingExecutor:
    """An executor that raises on its first calls and then behaves."""

    def __init__(self, raises=1, exc=None):
        self.raises = raises
        self.exc = exc or RuntimeError("the response was bytes, not text")
        self.inner = StubExecutor()
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        if self.calls <= self.raises:
            raise self.exc
        return self.inner(**kwargs)


class TestPlannerFailures(WorkerTestCase):
    """2026-09-14: a planner failure has to be a job outcome, not a crash."""

    def plan_files(self, job_id):
        return sorted(p.name for p in
                      (self.jobs / str(job_id)).glob("plan-response-*.txt"))

    def test_one_slip_is_retried_and_the_reply_is_saved(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        planner_fn = FlakyPlanner(failures=1)
        self.assertEqual(0, self.make_worker(planner_fn=planner_fn).run_once())

        self.assertEqual(2, planner_fn.calls)
        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual(["plan-response-1.txt"], self.plan_files(job_id))
        self.assertEqual(
            FlakyPlanner.RAW,
            (self.jobs / str(job_id) / "plan-response-1.txt").read_text(),
        )

    def test_two_failures_fail_the_job_and_keep_no_entry(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        planner_fn = FlakyPlanner(failures=2, raw=FlakyPlanner.UNUSABLE)
        # a job outcome, not an error exit: the worker carries on
        self.assertEqual(0, self.make_worker(planner_fn=planner_fn).run_once())

        job = db.get_job(self.conn, job_id)
        self.assertEqual("failed", job.state)
        self.assertTrue(job.last_error.startswith("planner:"), job.last_error)
        self.assertIn("Brief", job.last_error)
        self.assertEqual([], self.attempts(job_id))
        self.assertEqual([], self.entries(job_id))  # nothing was made to keep
        self.assertEqual(["plan-response-1.txt", "plan-response-2.txt"],
                         self.plan_files(job_id))

    def test_a_refusal_fails_the_job_the_same_way(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        error = planner.PlannerRefused("cannot read prompts/planner.md")
        run = self.make_worker(planner_fn=FlakyPlanner(failures=2, error=error))
        self.assertEqual(0, run.run_once())
        job = db.get_job(self.conn, job_id)
        self.assertEqual("failed", job.state)
        self.assertTrue(job.last_error.startswith("planner:"), job.last_error)

    def test_an_unexpected_exception_from_the_planner_is_caught_too(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        run = self.make_worker(
            planner_fn=FlakyPlanner(failures=2, error=ZeroDivisionError("division"))
        )
        self.assertEqual(0, run.run_once())
        self.assertEqual("failed", db.get_job(self.conn, job_id).state)


class SeededPlanner:
    """Malformed on one seed, fine on any other — the 2026-09-14 failure mode.

    The planner is sampled with a fixed seed, so the retry of job 5 asked the
    same question the same way and got the same broken answer back. This stub is
    that model: deterministic per seed.
    """

    BAD = "Sure! Some ideas:\n- circles\n"

    def __init__(self, bad_seed):
        self.bad_seed = bad_seed
        self.seeds = []

    def __call__(self, *, job, host, model, seed=1):
        self.seeds.append(seed)
        if seed == self.bad_seed:
            raise planner.PlannerFailed("no 'Brief' heading in the response",
                                        self.BAD)
        return planner.Plan(brief="a brief from a different roll",
                            assertions=["motion(idle)"],
                            prompt_version="planner-v1", tokens={},
                            durations={}, raw="")


class ProsePlanner:
    """Always headingless, always prose: the lenient parse is the only way out."""

    RAW = ("Sure! Here is the plan:\n\n"
           "Sixty circles drift across a dark field, each on its own slow noise "
           "path.\n\nAssertions:\n- motion(idle)\n- responds(click)\n")

    def __init__(self):
        self.calls = 0

    def __call__(self, *, job, host, model, seed=1):
        self.calls += 1
        raise planner.PlannerFailed("no 'Brief' heading in the response", self.RAW)


class TestPlannerSeedAndRecovery(WorkerTestCase):
    def test_the_retry_uses_a_different_seed(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        first_seed = worker.plan_seed(job_id, 1)
        planner_fn = SeededPlanner(bad_seed=first_seed)
        log = io.StringIO()
        run = self.make_worker(planner_fn=planner_fn, log_stream=log)
        self.assertEqual(0, run.run_once())

        self.assertEqual([first_seed, worker.plan_seed(job_id, 2)], planner_fn.seeds)
        self.assertNotEqual(planner_fn.seeds[0], planner_fn.seeds[1])
        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual("a brief from a different roll",
                         db.get_job(self.conn, job_id).brief)
        for seed in planner_fn.seeds:  # every try says which seed it used
            self.assertIn(f"seed {seed}", log.getvalue())

    def test_prose_without_a_heading_is_recovered_and_marked_lenient(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        planner_fn = ProsePlanner()
        log = io.StringIO()
        run = self.make_worker(planner_fn=planner_fn, log_stream=log)
        self.assertEqual(0, run.run_once())

        self.assertEqual(worker.PLAN_TRIES, planner_fn.calls)  # strict first, both times
        job = db.get_job(self.conn, job_id)
        self.assertEqual("held", job.state)
        self.assertEqual(
            "Sixty circles drift across a dark field, each on its own slow "
            "noise path.",
            job.brief,
        )
        self.assertEqual(["motion(idle)", "responds(click)"], job.assertions)
        self.assertIn("lenient parse", log.getvalue())
        # the entry says the plan was recovered rather than parsed
        self.assertEqual(
            f"planner-v1{planner.LENIENT_MARK}",
            self.entries(job_id)[0]["planner_prompt_version"],
        )

    def test_a_reply_with_no_prose_at_all_still_fails_the_job(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        run = self.make_worker(
            planner_fn=FlakyPlanner(failures=2, raw=FlakyPlanner.UNUSABLE)
        )
        self.assertEqual(0, run.run_once())
        job = db.get_job(self.conn, job_id)
        self.assertEqual("failed", job.state)
        self.assertTrue(job.last_error.startswith("planner:"), job.last_error)


class TestStepExceptions(WorkerTestCase):
    def test_an_executor_that_raises_is_a_failed_attempt_and_the_job_repairs(self):
        job_id = self.enqueue(max_attempts=3)
        run = self.make_worker(executor_fn=ExplodingExecutor(raises=1),
                               gate_fn=StubGate([0]))
        self.assertEqual(0, run.run_once())

        rows = self.attempts(job_id)
        self.assertEqual([1, 2], [row.n for row in rows])
        self.assertTrue(rows[0].evidence.startswith("executor: RuntimeError"),
                        rows[0].evidence)
        self.assertIsNone(rows[0].gate_exit)
        self.assertIn("repairing", self.states)
        self.assertEqual("held", db.get_job(self.conn, job_id).state)

    def test_a_gate_that_raises_is_a_failed_attempt_with_no_verdict(self):
        job_id = self.enqueue(max_attempts=1)

        def gate_fn(**kwargs):
            raise OSError("chromium is not where it was")

        self.assertEqual(0, self.make_worker(gate_fn=gate_fn).run_once())
        rows = self.attempts(job_id)
        self.assertEqual(1, len(rows))
        self.assertIsNone(rows[0].gate_exit)
        self.assertIn("chromium is not where it was", rows[0].evidence)
        self.assertEqual("failed", db.get_job(self.conn, job_id).state)

    def test_a_job_is_never_left_owned_after_a_pass(self):
        """Even an exception no guard expects releases the job to the sweep."""
        job_id = self.enqueue()

        def gate_fn(**kwargs):
            raise KeyboardInterrupt  # not an Exception: it escapes every guard

        run = self.make_worker(gate_fn=gate_fn)
        with self.assertRaises(KeyboardInterrupt):
            run.run_once()
        self.assertEqual("gating", db.get_job(self.conn, job_id).state)
        self.assertEqual(set(), run._owned)  # so the next sweep can recover it


class TestSweep(WorkerTestCase):
    def age(self, job_id, minutes):
        """Backdate one job's updated_utc."""
        then = datetime.strptime(db.utc_now(), worker.UTC_FORMAT) - timedelta(
            minutes=minutes)
        self.conn.execute("UPDATE jobs SET updated_utc = ? WHERE id = ?",
                          (then.strftime(worker.UTC_FORMAT), job_id))

    #: How a job legally reaches each running state from `queued`.
    PATHS = {
        "planning": ("planning",),
        "executing": ("executing",),
        "gating": ("executing", "gating"),
        "repairing": ("executing", "gating", "repairing"),
    }

    def stuck(self, state="planning", minutes=90):
        """A job parked in one running state with an old updated_utc."""
        job_id = self.enqueue()
        for step in self.PATHS[state]:
            db.transition(self.conn, job_id, step)
        self.age(job_id, minutes)
        self.states.clear()
        return job_id

    def test_a_live_second_worker_stops_the_sweep(self):
        """Its jobs in flight are its own, not a dead worker's leavings.

        The sweep runs ahead of the fence's refusal on purpose, so that a node
        fenced by an `opencode` session still tidies up. A second *worker* is
        the one refusal where that is wrong.
        """
        job_id = self.stuck("executing", minutes=90)
        run = self.make_worker(probe=lambda: dict(SECOND_WORKER))
        self.assertEqual(3, run.run_once())
        self.assertEqual("executing", db.get_job(self.conn, job_id).state)

    def test_a_busy_slot_still_sweeps(self):
        job_id = self.stuck("executing", minutes=90)
        run = self.make_worker(probe=lambda: dict(BUSY_SLOT))
        self.assertEqual(3, run.run_once())
        self.assertEqual("queued", db.get_job(self.conn, job_id).state)

    def test_a_job_left_in_planning_is_requeued(self):
        job_id = self.stuck("planning", minutes=90)
        self.assertEqual([job_id], self.make_worker().sweep_stuck())
        job = db.get_job(self.conn, job_id)
        self.assertEqual("queued", job.state)
        self.assertIn("swept", job.last_error)
        self.assertIn("90 minutes", job.last_error)

    def test_every_running_state_is_swept(self):
        ids = [self.stuck(state) for state in
               ("planning", "executing", "gating", "repairing")]
        self.assertEqual(ids, self.make_worker().sweep_stuck())
        for job_id in ids:
            self.assertEqual("queued", db.get_job(self.conn, job_id).state)

    def test_a_fresh_job_is_left_alone(self):
        job_id = self.stuck("executing", minutes=5)
        self.assertEqual([], self.make_worker().sweep_stuck())
        self.assertEqual("executing", db.get_job(self.conn, job_id).state)

    def test_at_start_a_fresh_orphan_is_swept_too(self):
        # A restart killed the previous worker mid-attempt (job 58, 2026-09-14):
        # the row is a minute old and nobody is attending it.
        job_id = self.stuck("executing", minutes=1)
        self.assertEqual([job_id], self.make_worker().sweep_stuck(everything=True))
        job = db.get_job(self.conn, job_id)
        self.assertEqual("queued", job.state)
        self.assertIn("swept at start", job.last_error)

    def test_sigterm_mid_attempt_requeues_the_job_and_leaves_control_running(self):
        import os
        import signal
        signal_seen = []

        def executor_fn(**kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            signal_seen.append(True)  # pragma: no cover - the handler raises first
            raise AssertionError("the SIGTERM handler should have raised StopNow")

        job_id = self.enqueue()
        run = self.make_worker(executor_fn=executor_fn)
        self.assertTrue(run._install_signal_handlers())
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        self.assertEqual(0, run.run_once())
        self.assertTrue(run._terminating)
        job = db.get_job(self.conn, job_id)
        self.assertEqual("queued", job.state)
        self.assertIn("worker stopped", job.last_error)
        self.assertEqual("running", db.get_control(self.conn).state)
        self.assertEqual([], signal_seen)

    def test_run_forever_returns_on_sigterm_while_idle(self):
        import os
        import signal
        import threading
        run = self.make_worker()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.SIG_DFL)
        threading.Timer(0.3, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
        started = time.monotonic()
        self.assertEqual(0, run.run_forever(sleep_s=5.0))
        self.assertLess(time.monotonic() - started, 4.0)

    def test_a_queued_or_finished_job_is_never_swept(self):
        queued = self.enqueue()
        self.age(queued, 600)
        done = self.enqueue()
        for state in ("executing", "gating", "held"):
            db.transition(self.conn, done, state)
        self.age(done, 600)
        self.assertEqual([], self.make_worker().sweep_stuck())
        self.assertEqual("queued", db.get_job(self.conn, queued).state)
        self.assertEqual("held", db.get_job(self.conn, done).state)

    def test_the_job_this_worker_owns_is_never_swept(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        seen = {}

        def slow_planner(*, job, host, model, seed=1):
            # the worker owns this job right now; pretend its plan took two hours
            self.age(job.id, 120)
            seen["swept"] = run.sweep_stuck()
            return planner.Plan(brief="a brief", assertions=["motion(idle)"],
                                prompt_version="planner-v1", tokens={},
                                durations={}, raw="")

        run = self.make_worker(planner_fn=slow_planner)
        self.assertEqual(0, run.run_once())
        self.assertEqual([], seen["swept"])
        self.assertEqual("held", db.get_job(self.conn, job_id).state)

    def test_the_sweep_runs_at_the_start_of_a_pass(self):
        job_id = self.stuck("gating", minutes=45)
        log = io.StringIO()
        self.assertEqual(0, self.make_worker(log_stream=log).run_once())
        self.assertIn(f"sweep: job {job_id} sat in gating", log.getvalue())
        # swept before claim_next, so the same pass picks it back up
        self.assertNotEqual("gating", db.get_job(self.conn, job_id).state)

    def test_zero_minutes_switches_the_sweep_off(self):
        job_id = self.stuck("planning", minutes=999)
        run = self.make_worker()
        run.stuck_minutes = 0
        self.assertEqual([], run.sweep_stuck())
        self.assertEqual("planning", db.get_job(self.conn, job_id).state)

    def test_an_unreadable_timestamp_is_left_alone(self):
        job_id = self.stuck("planning", minutes=90)
        self.conn.execute("UPDATE jobs SET updated_utc = 'yesterday' WHERE id = ?",
                          (job_id,))
        self.assertEqual([], self.make_worker().sweep_stuck())
        self.assertIsNone(worker.minutes_between("yesterday", db.utc_now()))
        self.assertEqual(60.0, worker.minutes_between("2026-09-14T00:00:00Z",
                                                      "2026-09-14T01:00:00Z"))


class TestStatusCard(IdleTestCase):
    """Packet 5: the steps the worker records for the operator's card.

    The card is the only way "what is the worker doing right now" reaches the
    web process — a different process, sharing one database and nothing else —
    so these tests are about the row, not about the log line beside it.
    """

    def test_one_job_records_its_steps_in_order(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        self.assertEqual(0, self.make_worker().run_once())
        self.assertEqual(
            ["claiming", "planning", "writing", "evaluating", "submitting"],
            self.steps(),
        )
        rows = self.activity_rows()
        # the claiming row names the job it got, once it has it
        self.assertIn(f"job {job_id}", rows[0]["detail"])
        self.assertIn("by octocat", rows[0]["detail"])
        # the sentence is in plain language; the job state machine is not
        writing = rows[2]
        self.assertEqual("Writing the sketch", writing["headline"])
        self.assertEqual(job_id, writing["job_id"])
        self.assertIn("attempt 1 of 3", writing["detail"])
        # two vocabularies on purpose: the card says writing, the database says
        # executing, and neither is renamed for the other
        self.assertNotIn("executing", self.steps())
        # the last step of a finished job stays open; nothing closes it but the
        # next step, and the nap is the next step
        self.assertIsNone(rows[-1]["ended_utc"])
        self.assertEqual("Submitting the sketch for review", rows[-1]["headline"])

    def test_a_failed_gate_records_the_feedback_and_the_correction(self):
        self.enqueue()
        run = self.make_worker(gate_fn=StubGate([1, 0]))
        self.assertEqual(0, run.run_once())
        steps = self.steps()
        self.assertEqual(
            ["claiming", "writing", "evaluating", "feedback", "correcting",
             "writing", "evaluating", "submitting"],
            steps,
        )
        correcting = [row for row in self.activity_rows()
                      if row["step"] == "correcting"][0]
        self.assertEqual("Correcting the sketch from the feedback",
                         correcting["headline"])
        self.assertIn("attempt 2 of 3 next", correcting["detail"])
        second = [row for row in self.activity_rows() if row["step"] == "writing"][1]
        self.assertIn("correcting from the last evaluation", second["detail"])

    def test_an_idle_round_records_judging_critiquing_and_the_nap(self):
        _, entry_id = self.publish()
        self.publish(prompt="a second field")
        run = self.make_worker(judge_fn=StubJudge(judged=1),
                               critic_fn=StubCritic(), idle_judge=1,
                               idle_critique=1)
        self.assertEqual(0, run.run_once())
        run._nap(0.0)  # what run_forever does next, with no sleep to wait for
        self.assertEqual(
            ["claiming", "judging", "critiquing", "spawning", "idle"], self.steps()
        )
        rows = {row["step"]: row for row in self.activity_rows()}
        # the judge names its pair only after it has judged it
        self.assertIn("entry 1 against entry 2", rows["judging"]["detail"])
        self.assertEqual(f"Critiquing entry {entry_id}", rows["critiquing"]["headline"])
        self.assertIn("next wake in 0 s", rows["idle"]["detail"])
        # the nap is the step that is still open, so a living worker always has
        # one: an open row with a dead pid is how the card says it stopped
        self.assertIsNone(rows["idle"]["ended_utc"])

    def test_the_nap_carries_what_the_idle_round_found(self):
        run = self.make_worker(idle_judge=0, idle_critique=0)
        self.assertEqual(0, run.run_once())
        # The step is opened before the sleeping starts, so a worker on its way
        # out records the nap it was about to take — and this test does not
        # have to wait three minutes and forty seconds to read it.
        run._terminating = True
        run._nap(220.0)
        row = db.current_activity(self.conn)
        self.assertEqual("idle", row["step"])
        self.assertIn("queue empty", row["detail"])
        self.assertIn("judging is switched off", row["detail"])
        self.assertIn("next wake in 3 min 40 s", row["detail"])

    def test_a_sweep_that_re_queues_nothing_says_nothing(self):
        # The sweep runs on every pass. A step per pass would push whatever the
        # worker is really doing off the card.
        self.make_worker().run_once()
        self.assertNotIn("sweeping", self.steps())

    def test_a_sweep_that_re_queues_something_is_a_step(self):
        job_id = self.enqueue()
        db.transition(self.conn, job_id, "executing")
        self.conn.execute(
            "UPDATE jobs SET updated_utc = ? WHERE id = ?",
            ("2026-09-14T00:00:00Z", job_id),
        )
        run = self.make_worker()
        run.sweep_stuck(now="2026-09-14T09:00:00Z")
        row = db.current_activity(self.conn)
        self.assertEqual("sweeping", row["step"])
        self.assertEqual("1 job left running by a worker that stopped",
                         row["detail"])

    def test_a_card_that_cannot_be_written_does_not_cost_the_job(self):
        # The same bargain the idle round makes: the display of the work is
        # never worth the work.
        job_id = self.enqueue()
        log = io.StringIO()
        run = self.make_worker(log_stream=log)

        def refuse(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with mock.patch.object(db, "begin_step", refuse):
            self.assertEqual(0, run.run_once())
        self.assertEqual("held", db.get_job(self.conn, job_id).state)
        self.assertEqual([], self.steps())
        self.assertIn("status: the card could not record", log.getvalue())


class TestHumanGap(unittest.TestCase):
    """The card's own duration voice: read inside a sentence, not in a cell."""

    def test_the_shapes_the_card_uses(self):
        for seconds, text in (
            (0, "0 s"),
            (40, "40 s"),
            (220, "3 min 40 s"),
            (840, "14 min"),
            (3600, "1 h"),
            (15120, "4 h 12 min"),
            (90000, "1 d 1 h"),
            (None, "—"),
        ):
            with self.subTest(seconds=seconds):
                self.assertEqual(text, worker.human_gap(seconds))


class TestQaSplit(unittest.TestCase):
    """QA is what may fail a job; the plan is what a person judges."""

    def test_qa_clean_is_false_when_a_failable_check_failed(self):
        for name in worker.QA_CHECKS:
            with self.subTest(check=name):
                self.assertFalse(worker.qa_clean(make_report("/tmp/x", checks={name: False})))

    def test_an_advisory_check_does_not_make_a_sketch_unclean(self):
        # noLoop() is a declaration, not a defect. A puzzle that redraws on
        # input has not failed quality assurance by being still.
        self.assertTrue(worker.qa_clean(make_report("/tmp/x", checks={"is_looping": False})))

    def test_a_missing_report_is_never_assumed_clean(self):
        for report in (None, {}):
            with self.subTest(report=report):
                self.assertFalse(worker.qa_clean(report))

    def test_qa_checks_are_exactly_the_gate_s_failable_ones(self):
        """The gate decides what may fail a run; this module only reads it.

        QA_CHECKS is a copy, because the gate is a standalone script with its
        own copy on the node and cannot be imported. If the gate's list moves
        and this one does not, sketches start being failed for things the gate
        forgave, or forgiven for things it did not — so the two are compared
        here rather than trusted to stay in step.
        """
        source = (REPO_ROOT / "gate" / "sketch_gate.py").read_text(encoding="utf-8")
        body = source.split("FAILABLE_CHECKS = (", 1)[1].split(")", 1)[0]
        gate_checks = set(re.findall(r'"([a-z_]+)"', body))
        self.assertEqual(gate_checks, set(worker.QA_CHECKS))

    def test_missed_assertions_lists_only_the_ones_that_failed(self):
        report = make_report("/tmp/x", assertions={
            "motion(idle)": {"pass": False, "detail": ""},
            "responds(click)": {"pass": True, "detail": ""},
            "responds(drag)": {"pass": False, "detail": ""},
        })
        self.assertEqual(["motion(idle)", "responds(drag)"],
                         worker.missed_assertions(report))


class TestEvidence(unittest.TestCase):
    """The text spec §3.3 sends back, built from a real-schema report."""

    def test_every_section_of_the_report_reaches_the_evidence(self):
        report = make_report(
            "/tmp/attempt-1",
            checks={"console_clean": False, "frame_advancing": False},
            assertions={
                "motion(idle)": {"pass": False, "detail": "0 of 160000 pixels changed"},
                "responds(click)": {"pass": True, "detail": "fine"},
            },
            notes=["frame_advancing compares frameCount at frame 84 (11) with the "
                   "end of it (11)",
                   "AudioContext state after the click probe: suspended"],
            console=[{"t": "x", "type": "pageerror", "text": "TypeError: nope"},
                     {"t": "x", "type": "log", "text": "hello"}],
            exit_code=1,
        )
        text = worker.build_evidence(report, 1)
        first = text.splitlines()[0]
        self.assertIn("checks failed: console_clean, frame_advancing", first)
        self.assertIn("assertions failed: motion(idle)", first)
        self.assertIn("0 of 160000 pixels changed", text)
        self.assertNotIn("responds(click)", text)
        self.assertIn("[pageerror] TypeError: nope", text)
        self.assertNotIn("hello", text)
        self.assertIn("AudioContext state after the click probe: suspended", text)

    def test_a_suspended_audio_context_is_given_the_fix_not_just_the_symptom(self):
        """0 of the first 1,392 attempts ever reached a running AudioContext.

        The gate names the symptom ("AudioContext state ... suspended"); the
        evidence has to name the fix, the way it does for frame_budget, or the
        executor rewrites everything but the one line that matters — the gesture
        the gate gives is a canvas-centre click, and audio has to start from it.
        """
        report = make_report(
            "/tmp/x",
            checks={"audio_context_running": False},
            notes=["AudioContext state after the click probe: suspended"],
            exit_code=1,
        )
        text = worker.build_evidence(report, 1)
        self.assertIn("audio_context_running = false", text)
        # the symptom the gate wrote is still there
        self.assertIn("AudioContext state after the click probe: suspended", text)
        # and now the fix is too, beneath the check it belongs to
        self.assertIn("fix:", text)
        self.assertIn("userStartAudio()", text)
        self.assertIn("mousePressed()", text)

    def test_a_sketch_that_declares_itself_static_is_not_told_it_failed(self):
        """Entry 429: nine attempts told to fix a check the gate never failed.

        is_looping is absent from the gate's FAILABLE_CHECKS on purpose — a
        sketch that calls noLoop() is declaring itself static and the gate
        believes it. Reporting it as a failed check told the executor to stop
        writing the event-driven sketches an interactive brief actually needs.
        """
        report = make_report(
            "/tmp/x",
            checks={"is_looping": False},
            assertions={"motion(idle)": {"pass": False, "detail": "0 of 9 changed"}},
            exit_code=1,
        )
        text = worker.build_evidence(report, 1)
        self.assertNotIn("checks failed", text)
        self.assertNotIn("Fixed checks that failed", text)
        self.assertNotIn("is_looping", text)
        # the real reason the run failed is still the first thing it reads
        self.assertIn("assertions failed: motion(idle)", text.splitlines()[0])

    def test_a_real_failed_check_beside_an_advisory_one_still_reports(self):
        report = make_report(
            "/tmp/x",
            checks={"is_looping": False, "console_clean": False},
            exit_code=1,
        )
        text = worker.build_evidence(report, 1)
        self.assertIn("checks failed: console_clean", text.splitlines()[0])
        self.assertNotIn("is_looping", text)

    def test_a_resource_that_did_not_arrive_is_named(self):
        """Entry 429 again: eight attempts, a blank canvas, and no reason given.

        A failed image is not a page error, so console_clean stayed true and the
        only evidence was three assertions reading zero pixels changed — which
        reads as a broken click handler when the sketch never started at all.
        """
        report = make_report(
            "/tmp/x",
            assertions={
                "responds(click)": {"pass": False, "detail": "0 of 160000 pixels changed"},
            },
            resources=[{"url": "https://picsum.photos/400/400", "type": "image",
                        "why": "net::ERR_FAILED"}],
            exit_code=1,
        )
        text = worker.build_evidence(report, 1)
        self.assertIn("Resources the sketch asked for and did not get:", text)
        self.assertIn("https://picsum.photos/400/400 — net::ERR_FAILED", text)
        # named as a cause, never as a prohibition
        self.assertIn("Reaching outside the sketch is allowed", text)
        self.assertNotIn("checks failed", text)
        # and it is above the zeroed assertion it explains
        self.assertLess(text.index("picsum"), text.index("0 of 160000"))

    def test_no_resource_section_when_nothing_failed_to_load(self):
        report = make_report("/tmp/x", checks={"console_clean": False}, exit_code=1)
        text = worker.build_evidence(report, 1)
        self.assertNotIn("Resources the sketch", text)

    def test_only_the_first_ten_console_errors_are_carried(self):
        console = [{"t": "x", "type": "error", "text": f"error {i}"} for i in range(25)]
        report = make_report("/tmp/a", console=console, exit_code=1,
                             checks={"console_clean": False})
        text = worker.build_evidence(report, 1)
        self.assertIn("error 9", text)
        self.assertNotIn("error 10", text)
        self.assertIn("25 error lines, first 10 shown", text)

    def test_a_refused_gate_says_so_without_a_report(self):
        text = worker.build_evidence(None, 3, "sketch_gate: refused: no such directory\n")
        self.assertTrue(text.startswith("gate refused (exit 3)"))
        self.assertIn("no such directory", text)


if __name__ == "__main__":
    unittest.main()


class TestPaidLease(IdleTestCase):
    """A live paid lease (db.paid_leases) means an agent is driving a job:
    the worker takes that job first and does no idle work meanwhile."""

    def parked(self, model="claude-sonnet-5"):
        job_id = self.enqueue("a paid plan", planner=model)
        db.transition(self.conn, job_id, "needs-laptop", needs="plan")
        self.states.clear()
        return job_id

    def test_idle_work_stands_by_while_a_lease_is_live_and_resumes_after(self):
        self.publish(prompt="a field of dots")
        self.publish(prompt="a second field")
        job_id = self.parked()
        db.lease_paid(self.conn, job_id, "claude-sonnet-5", 20)
        judge_fn = StubJudge(judged=1)
        critic_fn = StubCritic()
        run = self.make_worker(judge_fn=judge_fn, critic_fn=critic_fn,
                               idle_judge=1, idle_critique=1)
        self.assertEqual(0, run.run_once())
        self.assertEqual(([], []), (judge_fn.calls, critic_fn.calls))
        self.assertIn("standing by", run._idle_note)
        self.assertEqual([], self.child_jobs())
        # the lease ran out: the agent is gone, and the night goes on
        db.lease_paid(self.conn, job_id, "claude-sonnet-5", -1)
        self.assertEqual({}, db.paid_leases(self.conn))
        self.assertEqual(0, run.run_once())
        self.assertEqual(1, len(judge_fn.calls))
        self.assertEqual(1, len(critic_fn.calls))

    def test_a_lease_on_a_finished_job_means_nothing(self):
        self.publish(prompt="a field of dots")
        job_id = self.parked()
        db.lease_paid(self.conn, job_id, "claude-sonnet-5", 20)
        db.transition(self.conn, job_id, "failed", last_error="cancelled by operator")
        judge_fn = StubJudge(judged=1)
        run = self.make_worker(judge_fn=judge_fn, critic_fn=StubCritic(),
                               idle_judge=1, idle_critique=0)
        run.run_once()
        self.assertEqual(1, len(judge_fn.calls))

    def test_a_leased_job_is_claimed_ahead_of_older_queued_work(self):
        older = self.enqueue("an idle-spawned child", planner="stub")
        # no brief: the worker would plan it, and its planner is paid
        mine = db.enqueue(self.conn, "the agent's job", "octocat",
                          planner="claude-sonnet-5")
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        db.lease_paid(self.conn, mine, "claude-sonnet-5", 20)
        run = self.make_worker()
        self.assertEqual(0, run.run_once())
        self.assertEqual("needs-laptop", db.get_job(self.conn, mine).state)
        self.assertEqual("queued", db.get_job(self.conn, older).state)
