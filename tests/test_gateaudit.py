"""Unit tests for `sketchgen gate-audit` — the sort that found the bad sketches.

Run:  python3 -m unittest tests.test_gateaudit

A temporary database and a temporary jobs directory holding three attempts with
real report.json shapes: a cheap one, a slow one from before the frame budget
existed (no ms_per_frame), and one the budget failed. The command has to put
them in cost order and say which is which, from those files and the database
alone — it never runs the gate, which is the point of it being safe to point at
a working node.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"


def report(total_s, ms_per_frame=None, frame_budget=None, exit_code=0):
    timings = {"launch_s": 0.06, "load_s": 0.26, "total_s": total_s}
    if ms_per_frame is not None:
        timings["ms_per_frame"] = ms_per_frame
    checks = {"console_clean": True, "is_looping": True, "frame_advancing": True,
              "sound_lib_ok": None, "audio_context_running": None}
    if frame_budget is not None:
        checks["frame_budget"] = frame_budget
    return {"seed": 1, "timings": timings, "checks": checks, "assertions": {},
            "notes": [], "console": [], "exit": exit_code}


class GateAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-gateaudit-")
        root = Path(cls._tmp.name)
        cls.db_path = root / "sketchgen.db"
        cls.jobs = root / "jobs"
        cls.jobs.mkdir()
        db.init(cls.db_path)
        conn = db.connect(cls.db_path)
        try:
            cls.ids = {}
            plan = [
                ("cheap", report(2.4, ms_per_frame=9.2, frame_budget=True), "published"),
                ("old-and-slow", report(208.8), "published"),
                ("over-budget", report(31.4, ms_per_frame=1093.0,
                                       frame_budget=False, exit_code=1), "failed-kept"),
            ]
            for name, payload, state in plan:
                job_id = db.enqueue(conn, name, "student-one")
                db.transition(conn, job_id, "executing", executor="qwen3-coder:30b")
                db.transition(conn, job_id, "gating")
                gate_dir = cls.jobs / str(job_id) / "attempt-1" / ".gate"
                gate_dir.mkdir(parents=True)
                (gate_dir / "report.json").write_text(
                    json.dumps(payload, indent=2), encoding="utf-8"
                )
                db.add_attempt(
                    conn, job_id, 1,
                    started_utc="2026-09-15T12:00:00Z",
                    finished_utc="2026-09-15T12:05:00Z",
                    model="qwen3-coder:30b",
                    source_dir=str(gate_dir.parent),
                    gate_exit=payload["exit"],
                    gate_report_path=str(gate_dir / "report.json"),
                )
                db.transition(conn, job_id, "held")
                entry_id = db.create_entry(
                    conn, job_id, "held", prompt=name, submitted_by="student-one"
                )
                conn.execute("UPDATE entries SET state = ? WHERE id = ?",
                             (state, entry_id))
                conn.commit()
                cls.ids[name] = (job_id, entry_id)
            # An attempt whose gate never wrote a report: skipped, not guessed at.
            orphan = db.enqueue(conn, "no report at all", "student-one")
            db.transition(conn, orphan, "executing", executor="qwen3-coder:30b")
            db.transition(conn, orphan, "gating")
            db.add_attempt(conn, orphan, 1, started_utc="2026-09-15T12:00:00Z",
                           finished_utc="2026-09-15T12:10:00Z", gate_exit=None)
            cls.orphan = orphan
        finally:
            conn.close()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "gate-audit",
             "--db", str(self.db_path), "--jobs", str(self.jobs), *args],
            capture_output=True, text=True, check=False,
        )

    def audit(self, *args):
        result = self.run_cli("--json", *args)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout)

    def test_it_lists_slowest_first(self):
        rows = self.audit()
        self.assertEqual([208.8, 31.4, 2.4], [r["total_s"] for r in rows])

    def test_each_row_names_its_entry_and_state(self):
        rows = {r["job_id"]: r for r in self.audit()}
        job_id, entry_id = self.ids["over-budget"]
        self.assertEqual(entry_id, rows[job_id]["entry_id"])
        self.assertEqual("failed-kept", rows[job_id]["state"])

    def test_ms_per_frame_is_carried_when_the_report_has_one(self):
        rows = {r["job_id"]: r for r in self.audit()}
        self.assertEqual(1093.0, rows[self.ids["over-budget"][0]]["ms_per_frame"])
        self.assertEqual(9.2, rows[self.ids["cheap"][0]]["ms_per_frame"])

    def test_a_report_from_before_the_budget_says_nothing_rather_than_zero(self):
        rows = {r["job_id"]: r for r in self.audit()}
        old = rows[self.ids["old-and-slow"][0]]
        self.assertIsNone(old["ms_per_frame"])
        self.assertIsNone(old["frame_budget"])
        # and is still listed, because total_s is what found job 166
        self.assertEqual(208.8, old["total_s"])

    def test_an_attempt_with_no_report_is_skipped_not_invented(self):
        self.assertNotIn(self.orphan, [r["job_id"] for r in self.audit()])

    def test_over_filters(self):
        rows = self.audit("--over", "30")
        self.assertEqual([208.8, 31.4], [r["total_s"] for r in rows])
        self.assertEqual([], self.audit("--over", "1000"))

    def test_the_table_is_readable_and_marks_the_failure(self):
        result = self.run_cli("--over", "30")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("ms/frame", result.stdout)
        self.assertIn("FAILED", result.stdout)
        self.assertIn("1093", result.stdout)
        # the slowest run is the first row under the header
        body = result.stdout.strip().splitlines()
        self.assertIn(str(self.ids["old-and-slow"][0]), body[1])

    def test_an_empty_answer_is_not_an_error(self):
        result = self.run_cli("--over", "100000")
        self.assertEqual(0, result.returncode)
        self.assertIn("no attempt has a gate report", result.stdout)

    def test_it_refuses_rather_than_guessing_when_there_is_no_database(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "gate-audit",
             "--db", str(self.jobs / "nope.db"), "--jobs", str(self.jobs)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("refused", result.stderr)

    def test_it_refuses_when_there_is_no_jobs_directory(self):
        result = subprocess.run(
            [sys.executable, str(CLI), "gate-audit",
             "--db", str(self.db_path), "--jobs", str(self.jobs / "nope")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("refused", result.stderr)
