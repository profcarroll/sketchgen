"""Unit tests for sketchgen.worker — the loop, with everything external stubbed.

Run:  python3 -m unittest discover -s tests -v

No model, no browser, no other process: the planner, the executor, the gate and
the fence probe are all injected callables. The stub gate writes a report.json in
sketch_gate.py's real schema, so the evidence builder is tested against the shape
the gate actually produces rather than against a convenient fiction.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import worker  # noqa: E402


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def make_report(sketch_dir, *, checks=None, assertions=None, notes=None,
                console=None, exit_code=0):
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

    def __init__(self, statement="a stub statement\n", ok=None, on_call=None):
        self.calls = []
        self.statement = statement
        self.ok = list(ok) if ok is not None else None
        self.on_call = on_call

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
        (out / "sketch.js").write_text("function setup(){}\n", encoding="utf-8")
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
    def plan(*, job, host, model):
        from sketchgen import planner

        return planner.Plan(brief=brief, assertions=list(assertions),
                            prompt_version="planner-v1", tokens={}, durations={},
                            raw="")

    return plan


FREE_SLOT = {"processes": [], "models": [], "ollama_error": None}
BUSY_SLOT = {"processes": ["4242 node /home/ubuntu/.local/bin/opencode"],
             "models": ["qwen3-coder:30b-a3b-q4_K_M"], "ollama_error": None}


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
                    probe=None):
        return worker.Worker(
            self.conn,
            jobs_dir=self.jobs,
            gate_path="/nonexistent/sketch_gate.py",
            executor_fn=executor_fn or StubExecutor(),
            gate_fn=gate_fn or StubGate([0]),
            planner_fn=planner_fn or stub_plan(),
            probe=probe or (lambda: dict(FREE_SLOT)),
            log_stream=self.log,
        )

    def attempts(self, job_id):
        return db.list_attempts(self.conn, job_id)

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

    def test_the_planner_prompt_version_lands_on_the_entry(self):
        job_id = db.enqueue(self.conn, "sixty drifting circles", "octocat")
        self.make_worker().run_once()
        self.assertEqual("planner-v1", self.entries(job_id)[0]["planner_prompt_version"])


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
