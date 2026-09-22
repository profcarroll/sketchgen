"""Unit tests for sketchgen.paid — any step, answered off the node.

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser. Every "paid" answer below is a string typed
into a packet, which is exactly what the agent on the laptop hands back; the
node's half is the whole of what is tested. The test that earns each adapter is
its round trip: a packet cut for the step, answered with the reply the local
path would have got, lands the same rows the local path lands (plan §5.1).
"""

import argparse
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sketchgen import db  # noqa: E402
from sketchgen import executor  # noqa: E402
from sketchgen import judge  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen import models  # noqa: E402
from sketchgen import paid  # noqa: E402
from sketchgen import planner  # noqa: E402
from sketchgen import web  # noqa: E402
from sketchgen import worker  # noqa: E402

import test_judge  # noqa: E402
import test_planner  # noqa: E402
import test_worker  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"

GOOD_JUDGE_REPLY = test_judge.GOOD_REPLY
PNG_BYTES = test_judge.PNG_BYTES

GOOD_CRITIQUE = "Let one of the drifting dots fall out of step with the others.\n"


def gate_report(**kwargs):
    """A stub gate report carrying the number only the node can produce.

    9.8 ms a frame is what the gate measured for entry 1279, where the
    agent's laptop proxy read 6.4 (docs/plans/agent-rig.md §0): the frame time
    is the reason a try runs on the node rather than beside the agent.
    """
    report = test_worker.make_report(kwargs.pop("sketch_dir", "/attempt"), **kwargs)
    report["timings"]["ms_per_frame"] = 9.8
    return report

TWO_SENTENCES = "It is fine. Make the dots red.\n"

CLEAN_PLAN = test_planner.load("clean")

GOOD_SKETCH = """Here it is.

```js
function setup() { createCanvas(400, 400); }
function draw() { background(frameCount % 255); circle(200, 200, 80); }
```

## Statement

A grey field that breathes, with one circle held still in the middle of it.
"""
#: The same reply with the optional pointer script on it (auto-mouse.md §4.1).
#: Nothing in paid.py reads it; the worker's replay through executor.run is
#: what writes it, and these two tests are there to say so out loud.
GHOST_EVENTS = [{"t": 300, "type": "move", "x": 0.5, "y": 0.5},
                {"t": 800, "type": "click", "x": 0.5, "y": 0.5}]
GHOST_SKETCH = GOOD_SKETCH.replace(
    "## Statement",
    "```ghost\n" + json.dumps(GHOST_EVENTS) + "\n```\n\n## Statement",
)

PAID_ENV = {models.PAID_MODELS_ENV: "claude-opus-5, claude-sonnet-5"}


def migrations_upto(version, into):
    """A migrations directory holding everything up to ``version``.

    How a test builds a database as it stood before some migration and then
    applies that one migration to it. The directory is the argument
    :func:`db.migrate` takes, so nothing is mocked.
    """
    into.mkdir(parents=True, exist_ok=True)
    for sql in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
        if int(sql.name[:3]) <= version:
            shutil.copy(sql, into / sql.name)
    return into


class PaidTestCase(unittest.TestCase):
    """One temp directory, one temp database, three published entries."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-paid-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = test_judge.build_entries(self.tmp)
        self.addCleanup(self.conn.close)
        self.jobs_dir = self.tmp / "jobs"
        self.ctx = paid.Context(jobs_dir=self.jobs_dir)

    @property
    def dbpath(self):
        return str(self.tmp / "sketchgen.db")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "paid", *args,
             "--db", self.dbpath, "--jobs-dir", str(self.jobs_dir)],
            capture_output=True, text=True, check=False,
        )

    def agent_verdicts(self):
        return [
            dict(row) for row in self.conn.execute(
                "SELECT entry_a, entry_b, judge_id, question, choice, "
                "prompt_version, artefact_hash FROM judgments "
                "WHERE judge_kind = 'agent' ORDER BY entry_a, entry_b, question"
            )
        ]


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


class EnvelopeTests(PaidTestCase):

    def test_the_envelope_names_its_step_model_version_and_how_to_answer(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=1, ctx=self.ctx)
        self.assertEqual(packet["packet"], paid.PACKET_KIND)
        self.assertEqual(packet["step"], "judge")
        self.assertEqual(packet["model"], "claude-opus-5")
        self.assertEqual(packet["prompt_version"], "judge-v1")
        self.assertIn("answer", packet["how_to_answer"])
        item = packet["items"][0]
        for name in ("key", "prompt", "images", "guard", "inputs", "answer"):
            self.assertIn(name, item)
        self.assertEqual(item["answer"], "")

    def test_images_travel_as_paths_never_bytes(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=3, ctx=self.ctx)
        text = json.dumps(packet)
        for item in packet["items"]:
            for image in item["images"]:
                self.assertTrue(Path(image).is_file())
        self.assertNotIn("iVBORw0KGgo", text)  # base64 PNG header

    def test_a_setting_is_not_a_model(self):
        for word in ("paid", "local", "", "two words"):
            with self.subTest(model=word):
                with self.assertRaises(paid.PaidRefused):
                    paid.export_packet(self.conn, "judge", model=word, ctx=self.ctx)

    def test_something_that_is_not_a_packet_is_refused(self):
        for packet in ({"packet": "something-else"}, "a string",
                       {"packet": paid.PACKET_KIND, "step": "judge"},
                       {"packet": paid.PACKET_KIND, "step": "dream", "items": []}):
            with self.subTest(packet=packet):
                with self.assertRaises(paid.PaidRefused):
                    paid.import_packet(self.conn, packet, ctx=self.ctx)


# ---------------------------------------------------------------------------
# The judge, ported
# ---------------------------------------------------------------------------


class JudgeTests(PaidTestCase):

    def test_a_raw_reply_lands_the_rows_the_local_judge_lands(self):
        """Plan §5.1 for the judge: same reply, same rows, whoever answered."""
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=1, ctx=self.ctx)
        item = packet["items"][0]
        item["answer"] = GOOD_JUDGE_REPLY
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual((len(report.recorded), report.rejected), (1, []))
        landed = self.agent_verdicts()

        self.conn.execute("DELETE FROM judgments WHERE judge_kind = 'agent'")
        reply = self.tmp / "reply.txt"
        reply.write_text(GOOD_JUDGE_REPLY, encoding="utf-8")
        judge.judge_local(self.conn, item["inputs"]["entry_a"],
                          item["inputs"]["entry_b"], model="claude-opus-5",
                          stub=reply)
        self.assertEqual(landed, self.agent_verdicts())

    def test_structured_answers_still_land(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=1, ctx=self.ctx)
        packet["items"][0]["answers"] = {"brief": "B", "look": "tie"}
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1)
        self.assertEqual([r["choice"] for r in self.agent_verdicts()], ["B", "tie"])

    def test_the_model_that_answered_is_the_one_recorded(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=2, ctx=self.ctx)
        packet["model"] = "claude-sonnet-5"
        packet["items"][0]["answer"] = GOOD_JUDGE_REPLY
        packet["items"][1]["answer"] = GOOD_JUDGE_REPLY
        packet["items"][1]["model"] = "claude-fable-5-1"
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual({r["judge_id"] for r in self.agent_verdicts()},
                         {"claude-sonnet-5", "claude-fable-5-1"})

    def test_a_stale_guard_is_rejected_and_writes_nothing(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=1, ctx=self.ctx)
        item = packet["items"][0]
        item["answer"] = GOOD_JUDGE_REPLY
        Path(item["images"][0]).write_bytes(PNG_BYTES + b"\x99")
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.recorded, [])
        self.assertIn("artefact_hash", report.rejected[0]["reason"])
        self.assertEqual(self.agent_verdicts(), [])

    def test_a_partial_packet_lands_what_was_answered_and_skips_the_rest(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=3, ctx=self.ctx)
        packet["items"][1]["answer"] = GOOD_JUDGE_REPLY
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual((len(report.recorded), report.skipped, report.rejected),
                         (1, 2, []))

    def test_an_unparseable_answer_writes_nothing_and_keeps_the_text(self):
        packet = paid.export_packet(self.conn, "judge", model="claude-opus-5",
                                    limit=1, ctx=self.ctx)
        packet["items"][0]["answer"] = test_judge.MALFORMED_REPLY
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.recorded, [])
        self.assertEqual(report.rejected[0]["answer"], test_judge.MALFORMED_REPLY)
        self.assertEqual(self.agent_verdicts(), [])

    def test_a_packet_judge_export_wrote_before_this_module_still_imports(self):
        """Plan §5.6: `sketchgen-judge-claims` files are already on disk."""
        packet = judge.claims_packet(self.conn, judge_id="claude-laptop", limit=2)
        for claim in packet["claims"]:
            claim["answers"] = {"brief": "A", "look": "B"}
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 2)
        self.assertEqual({r["judge_id"] for r in self.agent_verdicts()},
                         {"claude-laptop"})


# ---------------------------------------------------------------------------
# The planner
# ---------------------------------------------------------------------------


class PaidModelListTests(unittest.TestCase):

    def test_names_come_from_the_environment_in_order_without_duplicates(self):
        env = {models.PAID_MODELS_ENV:
               "claude-opus-5 claude-sonnet-5,claude-opus-5, paid local bad;name"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(models.paid_models(), ["claude-opus-5", "claude-sonnet-5"])
            self.assertTrue(models.is_paid("claude-sonnet-5"))
            self.assertTrue(models.is_paid("paid"))
            self.assertFalse(models.is_paid("gemma4:e4b"))
            self.assertFalse(models.is_paid(None))

    def test_off_node_is_read_from_the_id_alone(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            for name, expected in (
                ("claude-sonnet-5", True),        # entry 1223: no tag, no list
                ("paid", True),
                ("gpt-oss:120b-cloud", True),     # Ollama forwards it
                ("gemma4:e4b", False),
                ("qwen3-coder:30b-a3b-q4_K_M", False),
                ("local", False), ("stub", False), ("", False), (None, False),
            ):
                with self.subTest(name=name):
                    self.assertIs(models.ran_off_node(name), expected)

    def test_unset_means_only_the_word_paid(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(models.paid_models(), [])
            self.assertTrue(models.is_paid("paid"))

    def test_the_planner_menu_offers_no_paid_model(self):
        """Since 2026-09-21 a paid job is made only by `paid start`, by the
        agent that answers it. The page lists what this node runs; a saved
        `paid` from before resolves to the worker's default."""
        with mock.patch.dict(os.environ, PAID_ENV), \
                mock.patch.object(web.models, "catalogue", return_value=[]):
            groups = dict(web.planner_groups())
            self.assertEqual([value for value, _ in groups["off this node"]], [])
            self.assertEqual(web.planner_column("paid"), worker.DEFAULT_PLANNER_MODEL)
            self.assertNotIn("paid", web.planner_values())


class PlanTests(PaidTestCase):

    def setUp(self):
        super().setUp()
        # build_entries leaves its three jobs queued; the worker would take
        # those first. They stand for finished work, so finish them.
        self.conn.execute("UPDATE jobs SET state = 'published'")

    def park(self, prompt="a grid of pale squares", by="octocat", planner="paid"):
        """A job the worker parked, exactly as the worker parks it."""
        job_id = db.enqueue(self.conn, prompt, by, planner=planner)
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            run = worker.Worker(self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
                                probe=lambda: dict(test_worker.FREE_SLOT))
            self.assertEqual(run.run_once(), 0)
        self.assertEqual(db.get_job(self.conn, job_id).state, "needs-laptop")
        return job_id

    def export(self, limit=5):
        return paid.export_packet(self.conn, "plan", model="claude-opus-5",
                                  limit=limit, ctx=self.ctx)

    def test_a_named_paid_model_parks_the_job_like_the_word_paid(self):
        with mock.patch.dict(os.environ, PAID_ENV):
            job_id = self.park(planner="claude-sonnet-5")
        self.assertEqual(db.get_job(self.conn, job_id).needs, "plan")

    def test_an_item_carries_the_prompt_the_local_planner_renders(self):
        job_id = self.park(prompt="sixty drifting circles", by="mona")
        item = self.export()["items"][0]
        self.assertEqual(item["inputs"]["job"], job_id)
        self.assertEqual(item["prompt"],
                         planner.build_prompt("sixty drifting circles", "mona"))
        self.assertEqual(item["guard"], "")  # plan §3.3: none needed, and why

    def test_a_reply_lands_what_plan_job_lands(self):
        """Plan §5.1 for the planner: the job's columns and plan.json."""
        first, second = self.park(), self.park()
        packet = self.export()
        packet["items"][0]["answer"] = CLEAN_PLAN
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.rejected, [])

        stub = self.tmp / "clean.txt"
        stub.write_text(CLEAN_PLAN, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(CLI), "plan", "--job", str(second),
             "--model", "claude-opus-5", "--stub", str(stub),
             "--jobs-dir", str(self.jobs_dir), "--db", self.dbpath],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        def shape(job_id):
            job = db.get_job(self.conn, job_id)
            document = json.loads(
                (self.jobs_dir / str(job_id) / "plan.json").read_text(encoding="utf-8"))
            document.pop("started_utc")
            # The paid path times its own round trip; `plan --job` has none.
            document.pop("durations")
            raw = (self.jobs_dir / str(job_id) / "response.txt").read_text(encoding="utf-8")
            return (job.state, job.brief, job.assertions_json, job.planner,
                    job.needs, document, raw)

        self.assertEqual(shape(first), shape(second))
        self.assertEqual(db.get_job(self.conn, first).state, "queued")

    def test_the_worker_takes_it_from_the_queue_and_the_entry_knows_its_plan(self):
        job_id = self.park()
        packet = self.export()
        packet["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            run = worker.Worker(
                self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
                executor_fn=test_worker.StubExecutor(), gate_fn=test_worker.StubGate([0]),
                probe=lambda: dict(test_worker.FREE_SLOT),
            )
            self.assertEqual(run.run_once(), 0)
        self.assertEqual(db.get_job(self.conn, job_id).state, "held")
        entry = self.conn.execute(
            "SELECT planner, planner_prompt_version FROM entries WHERE job_id = ?",
            (job_id,)).fetchone()
        self.assertEqual(entry["planner"], "claude-opus-5")
        self.assertEqual(entry["planner_prompt_version"], planner.prompt_version())

    def test_an_unparseable_answer_leaves_the_job_parked(self):
        job_id = self.park()
        packet = self.export()
        packet["items"][0]["answer"] = "motion(idle)\n"
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("still needs-laptop", report.rejected[0]["reason"])
        job = db.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.needs, job.brief), ("needs-laptop", "plan", None))

    def test_a_job_that_moved_on_is_rejected(self):
        job_id = self.park()
        packet = self.export()
        packet["items"][0]["answer"] = CLEAN_PLAN
        db.transition(self.conn, job_id, "failed", last_error="given up on")
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("not needs-laptop", report.rejected[0]["reason"])

    def test_the_same_answer_cannot_land_twice(self):
        self.park()
        packet = self.export()
        packet["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        again = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(again.recorded, [])
        self.assertEqual(len(again.rejected), 1)


# ---------------------------------------------------------------------------
# The executor
# ---------------------------------------------------------------------------


class ExecuteTests(PaidTestCase):

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE jobs SET state = 'published'")
        self.gate = test_worker.StubGate([0])

    def worker(self, gate=None, executor_fn=None):
        devnull = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(devnull.close)
        return worker.Worker(
            self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
            gate_fn=gate or self.gate, executor_fn=executor_fn,
            probe=lambda: dict(test_worker.FREE_SLOT),
        )

    def queue(self, executor_model="claude-opus-5", max_attempts=3, rules="random"):
        # 'random' by default, so the export has to resolve the coin the way
        # the worker will; the like-for-like test below pins one side.
        return db.enqueue(self.conn, "a breathing field", "octocat",
                          brief="A grey field that brightens and dims, forever.",
                          assertions=["motion(idle)"], executor=executor_model,
                          rules_file=rules, max_attempts=max_attempts)

    def park(self, **kwargs):
        job_id = self.queue(**kwargs)
        with mock.patch.dict(os.environ, PAID_ENV):
            self.assertEqual(self.worker().run_once(), 0)
        return job_id

    def export(self):
        return paid.export_packet(self.conn, "execute", model="claude-opus-5",
                                  limit=5, ctx=self.ctx)

    def answer(self, reply=GOOD_SKETCH, model=None):
        packet = self.export()
        packet["items"][0]["answer"] = reply
        if model:
            packet["model"] = model
        return packet, paid.import_packet(self.conn, packet, ctx=self.ctx)

    def test_a_paid_executor_parks_the_job_before_attempt_one(self):
        job_id = self.park()
        job = db.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.needs), ("needs-laptop", "execute"))
        self.assertEqual(db.list_attempts(self.conn, job_id), [])
        self.assertEqual(self.gate.calls, [])

    def test_the_needs_column_takes_execute_after_migration_014(self):
        job_id = self.queue()
        self.conn.execute("UPDATE jobs SET state = 'needs-laptop', needs = 'execute' "
                          "WHERE id = ?", (job_id,))
        with self.assertRaises(Exception):
            self.conn.execute("UPDATE jobs SET needs = 'dream' WHERE id = ?", (job_id,))

    def test_the_node_gates_the_reply_and_holds_the_entry(self):
        job_id = self.park()
        packet, report = self.answer(model="claude-sonnet-5")
        self.assertEqual(report.rejected, [])
        self.assertEqual(db.get_job(self.conn, job_id).state, "queued")
        with mock.patch.dict(os.environ, PAID_ENV):
            self.assertEqual(self.worker().run_once(), 0)
        job = db.get_job(self.conn, job_id)
        self.assertEqual(job.state, "held")
        self.assertEqual(len(self.gate.calls), 1)
        attempt = db.list_attempts(self.conn, job_id)[0]
        self.assertEqual(attempt.model, "claude-sonnet-5")  # who answered
        self.assertEqual(attempt.gate_exit, 0)
        attempt_dir = self.jobs_dir / str(job_id) / "attempt-1"
        # the prompt the worker rendered is the one the laptop was shown
        self.assertEqual((attempt_dir / "prompt.txt").read_text(encoding="utf-8"),
                         packet["items"][0]["prompt"])
        entry = self.conn.execute("SELECT executor FROM entries WHERE job_id = ?",
                                  (job_id,)).fetchone()
        self.assertEqual(entry["executor"], "claude-sonnet-5")

    def test_it_lands_what_the_local_path_lands_for_the_same_reply(self):
        """Plan §5.1 for the executor: the attempt row and the sketch."""
        paid_job = self.park(rules="control")
        self.answer()
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()

        reply = self.tmp / "reply.txt"
        reply.write_text(GOOD_SKETCH, encoding="utf-8")

        def replay(*, brief, assertions, rules_file, out_dir, model, host):
            return worker.Execution.from_result(executor.run(
                brief=brief, assertions=assertions, rules_file=rules_file,
                model="claude-opus-5", host=host, out_dir=out_dir, stub=reply))

        local_job = self.queue(executor_model="qwen3-coder:30b", rules="control")
        self.worker(executor_fn=replay).run_once()

        keep = ("n", "model", "rules_file", "prompt_version", "gate_exit",
                "evidence", "statement")
        rows = [
            {k: getattr(a, k) for k in keep}
            for job in (paid_job, local_job)
            for a in db.list_attempts(self.conn, job)
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], rows[1])
        for name in ("sketch.js", "index.html", "statement.md", "prompt.txt"):
            with self.subTest(file=name):
                self.assertEqual(
                    (self.jobs_dir / str(paid_job) / "attempt-1" / name).read_bytes(),
                    (self.jobs_dir / str(local_job) / "attempt-1" / name).read_bytes(),
                )

    def test_a_failed_gate_reparks_with_the_evidence_and_numbering_goes_on(self):
        """Plan §5.5: one round trip per attempt."""
        self.gate = test_worker.StubGate([1, 0])
        job_id = self.park()
        self.answer()
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()
        job = db.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.needs), ("needs-laptop", "execute"))
        first = db.list_attempts(self.conn, job_id)
        self.assertEqual([a.n for a in first], [1])

        item = self.export()["items"][0]
        self.assertEqual(item["inputs"]["attempt"], 2)
        self.assertIn(worker.EVIDENCE_HEADING, item["prompt"])
        self.assertIn(first[0].evidence.splitlines()[0], item["prompt"])

        self.answer()
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()
        self.assertEqual(db.get_job(self.conn, job_id).state, "held")
        self.assertEqual([a.n for a in db.list_attempts(self.conn, job_id)], [1, 2])

    def test_evidence_that_moved_under_the_packet_is_rejected(self):
        self.gate = test_worker.StubGate([1])
        job_id = self.park()
        self.answer()
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()
        packet = self.export()
        packet["items"][0]["answer"] = GOOD_SKETCH
        self.conn.execute("UPDATE attempts SET evidence = 'something else' "
                          "WHERE job_id = ?", (job_id,))
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("evidence", report.rejected[0]["reason"])
        self.assertEqual(db.get_job(self.conn, job_id).state, "needs-laptop")

    def test_a_reply_with_no_sketch_leaves_the_job_parked(self):
        job_id = self.park()
        _, report = self.answer(reply="I would love to help!\n")
        self.assertIn("no fenced js block", report.rejected[0]["reason"])
        job = db.get_job(self.conn, job_id)
        self.assertEqual((job.state, job.needs), ("needs-laptop", "execute"))
        self.assertFalse((self.jobs_dir / str(job_id) / "attempt-1" /
                          worker.PAID_REPLY).exists())

    def test_a_ghost_block_in_the_answer_travels_with_no_extra_code(self):
        """auto-mouse.md §4.2: `paid import` carries it for free.

        Nothing in `paid.py` knows about the block. The worker replays the
        reply through `executor.run(stub=…)`, which is the same parser and the
        same writer a local attempt uses, so the script lands beside the
        sketch the gate is about to run.
        """
        job_id = self.park()
        self.answer(reply=GHOST_SKETCH)
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()
        attempt = self.jobs_dir / str(job_id) / "attempt-1"
        self.assertEqual(GHOST_EVENTS,
                         json.loads((attempt / "ghost.json").read_text()))
        self.assertEqual({"events": 2},
                         json.loads((attempt / "result.json").read_text())["ghost"])
        self.assertEqual(db.get_job(self.conn, job_id).state, "held")

    def test_an_invalid_ghost_block_is_still_a_good_attempt(self):
        # DECIDE[ghost-dataset]: the sketch is the answer. Rejecting it over
        # an optional extra would spend one of three attempts for nothing.
        job_id = self.park()
        _, report = self.answer(reply=GHOST_SKETCH.replace('"x": 0.5', '"x": 5'))
        self.assertEqual(report.rejected, [])
        with mock.patch.dict(os.environ, PAID_ENV):
            self.worker().run_once()
        attempt = self.jobs_dir / str(job_id) / "attempt-1"
        self.assertFalse((attempt / "ghost.json").exists())
        self.assertIn("outside [0, 1]",
                      json.loads((attempt / "result.json").read_text())["ghost"]["rejected"])
        self.assertEqual(db.get_job(self.conn, job_id).state, "held")

    def test_the_same_answer_cannot_land_twice(self):
        self.park()
        packet, _ = self.answer()
        again = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(again.recorded, [])
        self.assertIn("not waiting", again.rejected[0]["reason"])

    def test_the_executor_menu_offers_no_paid_model(self):
        with mock.patch.dict(os.environ, PAID_ENV), \
                mock.patch.object(web.models, "catalogue", return_value=[]):
            offered = [v for v, _ in dict(web.executor_groups())["off this node"]]
            self.assertEqual(offered, [])
            self.assertNotIn("paid", web.executor_values())


class Migration014Tests(unittest.TestCase):
    """The jobs rebuild keeps every row, column and reference it found."""

    def test_a_database_at_013_keeps_its_jobs_and_learns_execute(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-014-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        before = migrations_upto(13, tmp / "migrations")
        conn = db.connect(tmp / "old.db")
        self.addCleanup(conn.close)
        db.migrate(conn, before)
        self.assertEqual(db.schema_version(conn), 13)
        parent_job = db.enqueue(conn, "a root", "octocat", brief="b",
                                assertions=["motion(idle)"])
        parent = db.create_entry(conn, parent_job, state="published", prompt="a root")
        job_id = db.enqueue(conn, "a child", "octocat", planner="paid",
                            executor="qwen3-coder:30b", rules_file="random",
                            parent_entry_id=parent, critique="slower",
                            critique_by="gemma4:e4b", needs="review")
        db.add_attempt(conn, job_id, 1, model="qwen3-coder:30b", evidence="e")
        was = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

        # 014 alone: 015 adds two columns to this very table, and the row this
        # test compares before and after is the rebuild's, not theirs.
        self.assertEqual(db.migrate(conn, migrations_upto(14, tmp / "through14")),
                         [14])
        now = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        self.assertEqual(was, now)
        self.assertEqual(list(was), list(now), "column order changed")
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.execute("UPDATE jobs SET state = 'needs-laptop', needs = 'execute' "
                     "WHERE id = ?", (job_id,))
        self.assertEqual(db.get_job(conn, job_id).needs, "execute")


# ---------------------------------------------------------------------------
# The critic
# ---------------------------------------------------------------------------


class CritiqueTests(PaidTestCase):

    def critique_rows(self):
        return [
            {k: row[k] for k in row.keys() if k not in ("id", "created_utc")}
            for row in self.conn.execute("SELECT * FROM critiques ORDER BY entry_id")
        ]

    def child_jobs(self):
        keep = ("prompt", "critique", "critique_by", "planner", "executor",
                "rules_file", "publication", "needs", "submitted_by",
                "parent_entry_id", "state")
        return [
            {k: row[k] for k in keep}
            for row in self.conn.execute(
                "SELECT * FROM jobs WHERE parent_entry_id IS NOT NULL ORDER BY id"
            )
        ]

    def export(self, limit=1, model="claude-opus-5"):
        return paid.export_packet(self.conn, "critique", model=model,
                                  limit=limit, ctx=self.ctx)

    def test_an_item_carries_the_prompt_the_local_critic_renders_and_its_strip(self):
        item = self.export()["items"][0]
        row = db.get_entry(self.conn, item["inputs"]["entry"])
        self.assertEqual(item["prompt"],
                         lineage.critique_prompt(row, row["statement"], row["brief"]))
        self.assertEqual(item["images"], [row["strip_path"]])
        self.assertEqual(item["guard"], paid.sha256_file(row["strip_path"]))
        self.assertEqual(item["prompt_version"], "critic-v3")

    def test_a_reply_lands_the_rows_the_idle_critic_lands(self):
        """Plan §5.1 for the critic: the critique row and the child job."""
        packet = self.export()
        entry_id = packet["items"][0]["inputs"]["entry"]
        packet["items"][0]["answer"] = GOOD_CRITIQUE
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.rejected, [])
        landed = (self.critique_rows(), self.child_jobs())
        self.assertEqual(len(landed[1]), 1)

        self.conn.execute("DELETE FROM critiques")
        self.conn.execute("DELETE FROM jobs WHERE parent_entry_id IS NOT NULL")
        reply = self.tmp / "critique.txt"
        reply.write_text(GOOD_CRITIQUE, encoding="utf-8")
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            run = worker.Worker(
                self.conn,
                jobs_dir=self.jobs_dir,
                critic_model="claude-opus-5",
                lineage_depth=self.ctx.lineage_depth,
                critic_fn=lambda conn, eid, **kw: lineage.critique(
                    conn, eid, model="claude-opus-5", stub=reply),
                log_stream=devnull,
            )
            self.assertTrue(run._critique_one(entry_id, "critic-v3"))
        self.assertEqual(landed, (self.critique_rows(), self.child_jobs()))

    def test_an_exported_entry_is_the_paid_critics_and_the_idle_loop_skips_it(self):
        """Plan §3.4: an assignment, not a race for migration 006's one row."""
        claimed = self.export(limit=1)["items"][0]["inputs"]["entry"]
        offered = db.entries_to_critique(self.conn, "critic-v3", 1,
                                         exclude=db.paid_claims(self.conn, "critique"))
        self.assertEqual(len(offered), 1)
        self.assertNotEqual(offered, [claimed])
        seen = []
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            run = worker.Worker(
                self.conn, jobs_dir=self.jobs_dir, idle_critique=3,
                critic_fn=lambda conn, eid, **kw: seen.append(eid) or (_ for _ in ()).throw(
                    lineage.CritiqueRefused("stop here")),
                log_stream=devnull,
            )
            run._idle_critique()
        self.assertNotIn(claimed, seen)
        self.assertEqual(len(seen), 2)

    def test_another_model_is_not_offered_what_one_model_holds(self):
        first = {i["inputs"]["entry"] for i in self.export(limit=2)["items"]}
        second = {i["inputs"]["entry"]
                  for i in self.export(limit=3, model="claude-sonnet-5")["items"]}
        self.assertFalse(first & second)
        again = {i["inputs"]["entry"] for i in self.export(limit=2)["items"]}
        self.assertEqual(first, again, "a model's own claims are offered again")

    def test_landing_releases_the_claim_and_a_rejection_keeps_it(self):
        packet = self.export(limit=2)
        packet["items"][0]["answer"] = GOOD_CRITIQUE
        packet["items"][1]["answer"] = TWO_SENTENCES
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1)
        self.assertIn("sentences", report.rejected[0]["reason"])
        self.assertEqual(set(db.paid_claims(self.conn, "critique")),
                         {packet["items"][1]["inputs"]["entry"]})

    def test_an_unparseable_answer_leaves_the_entry_uncritiqued(self):
        """Plan §5.4. The idle loop would record a rejected row; this must not."""
        packet = self.export()
        packet["items"][0]["answer"] = "```js\nfill(255);\n```"
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.rejected), 1)
        self.assertEqual(self.critique_rows(), [])
        self.assertEqual(self.child_jobs(), [])

    def test_a_changed_strip_is_rejected_and_writes_nothing(self):
        packet = self.export()
        packet["items"][0]["answer"] = GOOD_CRITIQUE
        Path(packet["items"][0]["images"][0]).write_bytes(PNG_BYTES + b"\x42")
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("strip", report.rejected[0]["reason"])
        self.assertEqual((self.critique_rows(), self.child_jobs()), ([], []))

    def test_a_model_id_that_cannot_sign_a_critique_is_rejected(self):
        packet = self.export()
        packet["items"][0]["answer"] = GOOD_CRITIQUE
        packet["items"][0]["model"] = "vendor/model+tag"
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("cannot sign", report.rejected[0]["reason"])
        self.assertEqual(self.critique_rows(), [])

    def test_release_hands_entries_back(self):
        self.export(limit=3)
        self.assertEqual(len(paid.release(self.conn, "critique", None)), 3)
        self.assertEqual(db.paid_claims(self.conn, "critique"), {})


# ---------------------------------------------------------------------------
# The assignment
# ---------------------------------------------------------------------------


class AssignmentTests(PaidTestCase):

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE jobs SET state = 'published'")
        self.env = mock.patch.dict(os.environ, PAID_ENV)
        self.env.start()
        self.addCleanup(self.env.stop)

    def worker(self, **kwargs):
        devnull = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(devnull.close)
        kwargs.setdefault("probe", lambda: dict(test_worker.FREE_SLOT))
        return worker.Worker(self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
                             **kwargs)

    def test_it_round_trips_and_unsets(self):
        self.assertEqual(db.get_assignment(self.conn), {})
        db.set_assignment(self.conn, {"plan": "claude-opus-5", "critique": "claude-opus-5"})
        db.set_assignment(self.conn, {"plan": None})
        self.assertEqual(db.get_assignment(self.conn), {"critique": "claude-opus-5"})
        with self.assertRaises(ValueError):
            db.set_assignment(self.conn, {"dream": "x"})

    def test_a_job_that_names_no_planner_takes_the_assignment(self):
        db.set_assignment(self.conn, {"plan": "claude-opus-5"})
        blank = db.enqueue(self.conn, "a blank planner", "octocat")
        self.worker().run_once()
        self.assertEqual(db.get_job(self.conn, blank).state, "needs-laptop")

    def test_a_job_that_says_local_does_not(self):
        db.set_assignment(self.conn, {"plan": "claude-opus-5"})
        local = db.enqueue(self.conn, "a local planner", "octocat", planner="local")
        self.worker(planner_fn=test_worker.stub_plan(),
                    executor_fn=test_worker.StubExecutor(),
                    gate_fn=test_worker.StubGate([0])).run_once()
        self.assertEqual(db.get_job(self.conn, local).state, "held")

    def test_a_job_that_names_no_executor_takes_the_assignment(self):
        db.set_assignment(self.conn, {"execute": "claude-opus-5"})
        job = db.enqueue(self.conn, "p", "octocat", brief="b", assertions=["motion(idle)"])
        self.worker(gate_fn=test_worker.StubGate([0])).run_once()
        self.assertEqual(db.get_job(self.conn, job).needs, "execute")

    def test_a_paid_critic_or_judge_assignment_keeps_the_idle_loop_off_it(self):
        db.set_assignment(self.conn, {"judge": "claude-opus-5",
                                      "critique": "claude-opus-5"})
        judge_calls, critic_calls = [], []
        run = self.worker(
            idle_judge=1, idle_critique=1,
            judge_fn=lambda conn, **kw: judge_calls.append(kw) or {"judged": 0},
            critic_fn=lambda conn, eid, **kw: critic_calls.append(eid),
        )
        run._idle_round()
        self.assertEqual((judge_calls, critic_calls), ([], []))

    def test_a_local_model_assigned_to_the_judge_is_the_one_it_runs(self):
        db.set_assignment(self.conn, {"judge": "gemma4:26b"})
        calls = []
        run = self.worker(idle_judge=1,
                          judge_fn=lambda conn, **kw: calls.append(kw) or {"judged": 1})
        run._idle_judge()
        self.assertEqual(calls[0]["model"], "gemma4:26b")

    def test_the_new_job_page_preselects_it_and_warns_about_the_executor(self):
        db.set_assignment(self.conn, {"plan": "claude-sonnet-5",
                                      "execute": "claude-opus-5"})
        defaults, _ = web.load_defaults(self.conn)
        self.assertEqual((defaults["planner"], defaults["executor"]),
                         ("claude-sonnet-5", "claude-opus-5"))
        note = web._assignment_note(self.conn)
        self.assertIn("execute claude-opus-5", note)
        self.assertIn("far larger difference", note)
        db.set_assignment(self.conn, {"execute": None})
        self.assertNotIn("far larger difference", web._assignment_note(self.conn))

    def test_a_paid_default_for_plan_or_execute_is_refused_at_the_cli(self):
        """A default paid planner makes paid jobs with no agent attached — from
        the page and from every child the idle critic spawns (2026-09-21)."""
        for flags in (("--all", "claude-opus-5"), ("--plan", "claude-opus-5"),
                      ("--execute", "paid")):
            result = self.run_cli("assign", *flags, "--json")
            self.assertEqual(result.returncode, 3, result.stdout)
            self.assertIn("paid start", result.stderr)
        self.assertEqual(db.get_assignment(self.conn), {})
        result = self.run_cli("assign", "--all", "gemma4:e4b", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)["assignment"].values()),
                         {"gemma4:e4b"})

    def test_assign_the_idle_steps_at_the_cli_and_export_without_as(self):
        result = self.run_cli("assign", "--judge", "claude-opus-5",
                              "--critique", "claude-opus-5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["assignment"].values()), {"claude-opus-5"})
        out = self.tmp / "p.json"
        result = self.run_cli("export", "--step", "critique", "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(packet["model"], "claude-opus-5")
        result = self.run_cli("assign", "--critique", "local", "--json")
        self.assertNotIn("critique", json.loads(result.stdout)["assignment"])

    def test_export_with_neither_as_nor_an_assignment_is_refused(self):
        result = self.run_cli("export", "--step", "plan", "--out",
                              str(self.tmp / "x.json"))
        self.assertEqual(result.returncode, 3)
        self.assertIn("paid assign", result.stderr)


# ---------------------------------------------------------------------------
# The harness: registration, preflight, per-job models, wait, stdio packets
# ---------------------------------------------------------------------------


class HarnessTests(PaidTestCase):
    """What a Sonnet 5 session lacked on 2026-09-21 and had to guess at."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE jobs SET state = 'published'")
        # No paid names in the environment: registration alone must route.
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(models.PAID_MODELS_ENV, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(models.remember_registered, [])

    def worker(self, **kwargs):
        devnull = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(devnull.close)
        kwargs.setdefault("probe", lambda: dict(test_worker.FREE_SLOT))
        kwargs.setdefault("gate_fn", test_worker.StubGate([0]))
        return worker.Worker(self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
                             **kwargs)

    def cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args, "--db", self.dbpath],
            capture_output=True, text=True, check=False, input=stdin,
        )

    # -- registration --------------------------------------------------------

    def test_a_registered_name_routes_with_no_environment_at_all(self):
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        self.assertTrue(models.is_paid("claude-sonnet-5", self.conn))
        job = db.enqueue(self.conn, "p", "octocat", planner="claude-sonnet-5")
        self.worker().run_once()
        self.assertEqual(db.get_job(self.conn, job).needs, "plan")

    def test_the_page_sees_names_the_request_thread_was_handed(self):
        """Not for the menus any more, but the parent picker still needs to
        know a paid parent when it sees one: its child is made here."""
        models.remember_registered(["claude-sonnet-5"])
        self.assertEqual(web._model_word("claude-sonnet-5"), "local")
        self.assertEqual(web._model_word("gemma4:e4b"), "gemma4:e4b")

    def test_paid_models_add_list_remove(self):
        result = self.cli("paid", "models", "add", "claude-sonnet-5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["registered"], ["claude-sonnet-5"])
        self.assertEqual(self.cli("paid", "models", "add", "gemma4:e4b").returncode, 3)
        result = self.cli("paid", "models", "remove", "claude-sonnet-5", "--json")
        self.assertEqual(json.loads(result.stdout)["registered"], [])

    # -- preflight -----------------------------------------------------------

    def preflight(self, workers=("worker",), drip=False):
        return paid.preflight(self.conn, "claude-sonnet-5",
                              workers=lambda: list(workers), drip=lambda: drip)

    def failed(self, result):
        return {row["check"]: row for row in result["checks"] if not row["ok"]}

    def test_preflight_names_the_missing_registration_and_whose_fix_it_is(self):
        result = self.preflight()
        self.assertFalse(result["ready"])
        row = self.failed(result)["registered"]
        self.assertEqual(row["who"], "you")
        self.assertEqual(row["fix"], "sketchgen paid models add claude-sonnet-5")

    def test_preflight_is_ready_when_everything_is(self):
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        self.assertTrue(self.preflight()["ready"])
        self.assertTrue(self.preflight(workers=(), drip=True)["ready"])

    def test_preflight_leaves_the_operators_problems_to_the_operator(self):
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        db.set_control(self.conn, "paused", "a person paused it")
        for workers, name in (((), "worker"), (("a", "b"), "worker"),
                              (("a",), "generator")):
            with self.subTest(workers=workers):
                row = self.failed(self.preflight(workers=workers))[name]
                self.assertEqual(row["who"], "operator")

    def test_preflight_at_the_cli_exits_3_and_says_stop(self):
        result = self.cli("paid", "preflight", "--as", "claude-sonnet-5")
        self.assertEqual(result.returncode, 3)
        self.assertIn("NOT READY", result.stdout)
        self.assertIn("paid models add claude-sonnet-5", result.stdout)

    # -- per-job models ----------------------------------------------------------

    def test_enqueue_names_the_models_for_one_job(self):
        refused = self.cli("enqueue", "--prompt", "x", "--by", "octocat",
                           "--planner", "claude-sonnet-5")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("paid models add", refused.stderr)
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        result = self.cli("enqueue", "--prompt", "x", "--by", "octocat", "--json",
                          "--planner", "claude-sonnet-5", "--executor", "claude-sonnet-5")
        self.assertEqual(result.returncode, 0, result.stderr)
        job = db.get_job(self.conn, json.loads(result.stdout)["job"])
        self.assertEqual((job.planner, job.executor), ("claude-sonnet-5",) * 2)
        self.assertEqual(db.get_assignment(self.conn), {})  # nobody else's jobs

    # -- wait ------------------------------------------------------------------

    def fake_time(self):
        now = [0.0]
        return (lambda seconds: now.__setitem__(0, now[0] + seconds)), (lambda: now[0])

    def test_wait_returns_the_next_command_when_the_job_is_parked(self):
        job = db.enqueue(self.conn, "p", "octocat", planner="paid")
        db.transition(self.conn, job, "needs-laptop", needs="plan")
        result = paid.wait_for(self.conn, job, timeout=10)
        self.assertEqual(result["do"], "answer")
        self.assertEqual(result["command"],
                         f"sketchgen paid export --step plan --job {job} --out -")

    def test_wait_times_out_while_the_worker_has_it(self):
        job = db.enqueue(self.conn, "p", "octocat")
        sleep, clock = self.fake_time()
        result = paid.wait_for(self.conn, job, timeout=30, interval=5,
                               sleep=sleep, clock=clock)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["state"], "queued")

    def test_wait_stops_rather_than_waiting_on_a_paused_generator(self):
        job = db.enqueue(self.conn, "p", "octocat")
        db.set_control(self.conn, "paused", "deploy")
        result = paid.wait_for(self.conn, job, timeout=30)
        self.assertEqual(result["do"], "stop")
        self.assertIn("paused", result["say"])

    # -- the whole recipe, as AGENTS.md writes it ------------------------------

    def test_the_agents_md_recipe_runs_end_to_end(self):
        """preflight → enqueue → wait → export - → answer → import - → … → held.

        Only the worker is run in-process, standing in for the daemon.
        """
        self.cli("paid", "models", "add", "claude-sonnet-5")
        queued = self.cli("enqueue", "--prompt", "a breathing field", "--by",
                          "octocat", "--planner", "claude-sonnet-5",
                          "--executor", "claude-sonnet-5", "--json")
        job = json.loads(queued.stdout)["job"]
        jobs = ["--jobs-dir", str(self.jobs_dir)]

        for step, reply in (("plan", CLEAN_PLAN), ("execute", GOOD_SKETCH)):
            self.worker().run_once()  # the daemon's pass
            waited = self.cli("paid", "wait", "--job", str(job), "--json", *jobs)
            self.assertEqual(json.loads(waited.stdout)["step"], step, waited.stdout)
            exported = self.cli("paid", "export", "--step", step, "--job", str(job),
                                "--as", "claude-sonnet-5", "--out", "-", *jobs)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            packet = json.loads(exported.stdout)
            self.assertEqual([i["inputs"]["job"] for i in packet["items"]], [job])
            packet["items"][0]["answer"] = reply
            imported = self.cli("paid", "import", "-", *jobs, stdin=json.dumps(packet))
            self.assertEqual(json.loads(imported.stdout)["recorded"], 1, imported.stdout)

        self.worker().run_once()  # the gate
        done = json.loads(self.cli("paid", "wait", "--job", str(job), "--json",
                                   *jobs).stdout)
        self.assertEqual((done["do"], done["state"]), ("done", "held"))
        entry = self.conn.execute("SELECT planner, executor FROM entries "
                                  "WHERE job_id = ?", (job,)).fetchone()
        self.assertEqual(tuple(entry), ("claude-sonnet-5", "claude-sonnet-5"))

    def test_import_from_stdin_returns_rejected_answers_verbatim(self):
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        job = db.enqueue(self.conn, "p", "octocat", planner="claude-sonnet-5")
        self.worker().run_once()
        packet = paid.export_packet(self.conn, "plan", model="claude-sonnet-5",
                                    ctx=paid.Context(jobs_dir=self.jobs_dir, job=job))
        packet["items"][0]["answer"] = "no headings here\n"
        out = self.cli("paid", "import", "-", "--jobs-dir", str(self.jobs_dir),
                       stdin=json.dumps(packet))
        report = json.loads(out.stdout)
        self.assertEqual(report["rejected"][0]["answer"], "no headings here\n")

    def test_job_narrows_only_plan_and_execute(self):
        with self.assertRaises(paid.PaidRefused):
            paid.export_packet(self.conn, "critique", model="claude-sonnet-5",
                               ctx=paid.Context(job=1))


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


class CliTests(PaidTestCase):

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("export", "import", "status", "release", "assign", "models",
                     "preflight", "wait", "start", "next", "try"):
            with self.subTest(subcommand=name):
                result = subprocess.run(
                    [sys.executable, str(CLI), "paid", name, "--help"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_answer_import_through_files(self):
        out = self.tmp / "packet.json"
        result = self.run_cli("export", "--step", "judge", "--as", "claude-opus-5",
                              "--out", str(out), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["items"], 3)
        packet = json.loads(out.read_text(encoding="utf-8"))
        packet["items"][0]["answer"] = GOOD_JUDGE_REPLY
        packet["items"][1]["answer"] = test_judge.MALFORMED_REPLY
        out.write_text(json.dumps(packet), encoding="utf-8")

        result = self.run_cli("import", str(out), "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["recorded"], report["skipped"]), (1, 1))
        self.assertEqual(len(report["rejected"]), 1)
        saved = json.loads(Path(report["rejected_saved"]).read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["answer"], test_judge.MALFORMED_REPLY)

    def test_export_with_nothing_to_offer_writes_nothing(self):
        self.conn.execute("UPDATE entries SET state = 'held' WHERE id != ?",
                          (self.ids[0],))
        out = self.tmp / "none.json"
        result = self.run_cli("export", "--step", "judge", "--as", "claude-opus-5",
                              "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to judge", result.stdout)
        self.assertFalse(out.exists())

    def test_import_of_something_that_is_not_json_is_refused(self):
        bad = self.tmp / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        result = self.run_cli("import", str(bad))
        self.assertEqual(result.returncode, 3)

    def test_status_names_every_step_with_a_route(self):
        result = self.run_cli("status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        waiting = json.loads(result.stdout)
        self.assertIn("judge", waiting)
        self.assertEqual(waiting["plan"]["count"], 0)
        self.assertEqual(waiting["critique"]["count"], 3)

    def test_release_needs_ids_or_all(self):
        self.assertEqual(self.run_cli("release", "--step", "critique").returncode, 3)
        result = self.run_cli("release", "--step", "critique", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# The agent's loop: start, next, import, release — and the lease under it
# ---------------------------------------------------------------------------


class AgentLoopTests(PaidTestCase):
    """What the second Sonnet 5 session of 2026-09-21 needed and did not have.

    Its job sat queued behind an idle-spawned child for ten minutes while
    `wait` blocked longer than a tool call allows, and nothing told the worker
    an agent was at the other end. `start` leases the job, the worker takes
    it first and does no idle work, and `next` returns within a tool call
    with either the packet or the reason there is none yet.
    """

    MODEL = "claude-sonnet-5"

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE jobs SET state = 'published'")
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(models.PAID_MODELS_ENV, None)
        self.addCleanup(self.env.stop)
        self.addCleanup(models.remember_registered, [])
        self.one_worker = dict(workers=lambda: ["4242 sketchgen worker"],
                               drip=lambda: False)

    def worker(self, **kwargs):
        devnull = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(devnull.close)
        kwargs.setdefault("probe", lambda: dict(test_worker.FREE_SLOT))
        kwargs.setdefault("gate_fn", test_worker.StubGate([0]))
        return worker.Worker(self.conn, jobs_dir=self.jobs_dir, log_stream=devnull,
                             **kwargs)

    def cli(self, *args, stdin=None):
        return subprocess.run(
            [sys.executable, str(CLI), *args, "--db", self.dbpath],
            capture_output=True, text=True, check=False, input=stdin,
        )

    def fake_time(self):
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds

        return sleep, lambda: now[0]

    def start(self, **kwargs):
        options = dict(model=self.MODEL, prompt="a breathing field", by="octocat",
                       **self.one_worker)
        options.update(kwargs)
        return paid.start(self.conn, **options)

    # -- start -----------------------------------------------------------------

    def test_start_registers_preflights_queues_and_leases(self):
        result = self.start()
        self.assertTrue(result["started"], result)
        self.assertTrue(result["registered_now"])
        self.assertIn(self.MODEL, db.get_paid_models(self.conn))
        job = db.get_job(self.conn, result["job"])
        self.assertEqual((job.state, job.planner, job.executor),
                         ("queued", self.MODEL, self.MODEL))
        self.assertEqual(db.paid_leases(self.conn)[job.id]["model"], self.MODEL)
        self.assertEqual(result["then"], f"sketchgen paid next --job {job.id} --as {self.MODEL}")

    def test_start_queues_nothing_when_the_preflight_is_not_ready(self):
        before = len(db.list_jobs(self.conn, "queued"))
        result = self.start(workers=lambda: [])
        self.assertFalse(result["started"])
        self.assertFalse(result["preflight"]["ready"])
        self.assertEqual(before, len(db.list_jobs(self.conn, "queued")))
        self.assertEqual({}, db.paid_leases(self.conn))

    def test_start_takes_local_for_a_step_and_refuses_an_unregistered_paid_model(self):
        result = self.start(executor="local")
        self.assertEqual(db.get_job(self.conn, result["job"]).executor, "local")
        self.assertIsNone(result["partner"])
        with self.assertRaises(paid.PaidRefused) as caught:
            self.start(planner="claude-opus-5")
        self.assertIn("sketchgen paid models add claude-opus-5", str(caught.exception))
        with self.assertRaises(paid.PaidRefused):
            self.start(executor="paid")
        with self.assertRaises(paid.PaidRefused):
            self.start(planner="local", executor="qwen3-coder:30b-a3b-q4_K_M")
        with self.assertRaises(paid.PaidRefused):
            self.start(prompt="   ")
        with self.assertRaises(paid.PaidRefused):
            self.start(by="not a username")

    def test_a_started_job_is_claimed_ahead_of_the_queue(self):
        older = db.enqueue(self.conn, "an idle child", "octocat", planner="stub")
        mine = self.start()["job"]
        self.worker().run_once()
        self.assertEqual(db.get_job(self.conn, mine).needs, "plan")
        self.assertEqual(db.get_job(self.conn, older).state, "queued")

    # -- next --------------------------------------------------------------------

    def test_next_hands_over_the_packet_and_import_says_what_follows(self):
        job = self.start()["job"]
        self.worker().run_once()
        result = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertEqual((result["do"], result["step"]), ("answer", "plan"))
        self.assertEqual([i["inputs"]["job"] for i in result["items"]], [job])
        self.assertEqual(result["then"], "sketchgen paid import -")
        result["items"][0]["answer"] = CLEAN_PLAN
        db.lease_paid(self.conn, job, self.MODEL, -1)  # let it lapse: import renews it
        report = paid.import_packet(self.conn, result, ctx=self.ctx)
        self.assertEqual(report.jobs, [job])
        self.assertEqual(report.then(), f"sketchgen paid next --job {job} --as {self.MODEL}")
        self.assertIn(job, db.paid_leases(self.conn))
        self.assertEqual(db.get_job(self.conn, job).state, "queued")

    def test_next_says_wait_and_what_the_worker_is_doing_on_timeout(self):
        job = self.start()["job"]
        db.begin_step(self.conn, step="planning", headline="Turning the prompt into a brief",
                      job_id=job - 1, pid=os.getpid())
        sleep, clock = self.fake_time()
        said = []
        result = paid.next_for(self.conn, job, self.MODEL, timeout=90, interval=5,
                               sleep=sleep, clock=clock, progress=said.append)
        self.assertEqual((result["do"], result["state"]), ("wait", "queued"))
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["worker"]["job"], job - 1)
        self.assertIn(f"the worker is on job {job - 1}", result["say"])
        self.assertEqual(result["then"], f"sketchgen paid next --job {job} --as {self.MODEL}")
        self.assertTrue(said and all("is queued" in line for line in said), said)

    def test_next_stops_on_a_paused_generator_and_on_another_agents_job(self):
        job = self.start()["job"]
        db.set_control(self.conn, "paused", "deploy")
        result = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual(result["do"], "stop")
        self.assertIn("paused", result["say"])
        db.set_control(self.conn, "running", None)
        db.lease_paid(self.conn, job, "claude-opus-5", 20)
        result = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual((result["do"], result["leased_to"]), ("stop", "claude-opus-5"))

    def test_next_is_done_when_held_and_drops_the_lease(self):
        job = db.enqueue(self.conn, "p", "octocat", brief="b", assertions=["motion(idle)"])
        db.lease_paid(self.conn, job, self.MODEL, 20)
        gate = test_worker.StubGate([0], reports=[gate_report(
            assertions={"motion(idle)": {"pass": True, "detail": "idle 0->120 frames"}})])
        self.worker(executor_fn=test_worker.StubExecutor(), gate_fn=gate).run_once()
        result = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual((result["do"], result["state"]), ("done", "held"))
        self.assertIsNotNone(result["entry"])
        self.assertEqual({}, db.paid_leases(self.conn))
        # Dossier 01 §7.2: `held` is reached by a clean pass and by a sketch
        # that ran and missed an assertion once the attempts were spent, so
        # `done` says which of the two this was and what the gate measured.
        verdict = result["verdict"]
        self.assertEqual((verdict["exit"], verdict["offplan"], verdict["attempt"]),
                         (0, [], 1))
        self.assertTrue(verdict["assertions"]["motion(idle)"]["pass"])
        self.assertEqual(verdict["timings"]["ms_per_frame"], 9.8)
        self.assertIn("clean", verdict["say"])
        self.assertIn("the gate: clean", result["say"])

    # -- release -----------------------------------------------------------------

    def test_release_hands_a_parked_job_to_this_node(self):
        job = self.start()["job"]
        self.worker().run_once()
        result = paid.release_job(self.conn, job, by=self.MODEL, reason="out of budget")
        after = db.get_job(self.conn, job)
        self.assertEqual((after.state, after.needs, after.planner, after.executor),
                         ("queued", None, None, None))
        self.assertIn("handed to the local path by claude-sonnet-5: out of budget",
                      after.last_error)
        self.assertEqual({}, db.paid_leases(self.conn))
        self.assertEqual(result["was"]["planner"], self.MODEL)
        with self.assertRaises(paid.PaidRefused):
            paid.release_job(self.conn, job)  # queued, not parked

    def test_release_keeps_a_local_executor_as_it_was(self):
        job = self.start(executor="qwen3-coder:30b-a3b-q4_K_M")["job"]
        self.worker().run_once()
        paid.release_job(self.conn, job)
        self.assertEqual(db.get_job(self.conn, job).executor, "qwen3-coder:30b-a3b-q4_K_M")

    def test_preflight_lists_parked_jobs_and_whose_they_are(self):
        mine = self.start()["job"]
        self.worker().run_once()
        db.set_paid_models(self.conn, [self.MODEL, "claude-opus-5"])
        theirs = db.enqueue(self.conn, "a child nobody comes for", "octocat",
                            planner="claude-opus-5")
        self.worker().run_once()
        report = paid.preflight(self.conn, self.MODEL, **self.one_worker)
        parked = {row["job"]: row for row in report["info"]["parked"]}
        self.assertTrue(parked[mine]["yours"])
        self.assertEqual(parked[mine]["command"],
                         f"sketchgen paid next --job {mine} --as {self.MODEL}")
        self.assertFalse(parked[theirs]["yours"])
        # Parked for another paid model: the command is that agent's `next`,
        # so the operator knows whom to bring; release stays one line away.
        self.assertEqual(parked[theirs]["command"],
                         f"sketchgen paid next --job {theirs} --as claude-opus-5")
        self.assertEqual(parked[theirs]["release"], f"sketchgen paid release --job {theirs}")
        self.assertEqual(report["info"]["leases"][str(mine)]["model"], self.MODEL)

    # -- an agent that never comes back (job 1263, 2026-09-21) ---------------------

    def later(self, minutes):
        """A timestamp ``minutes`` from now, in the database's format."""
        return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(
            db.UTC_FORMAT)

    def test_the_worker_hands_back_a_job_whose_lease_lapsed(self):
        """gemini-3.8-flash ran out of weekly quota mid-plan: its lease lapsed
        and the job would have sat parked until the quota reset."""
        job = self.start()["job"]
        run = self.worker()
        run.run_once()
        self.assertEqual(db.get_job(self.conn, job).state, "needs-laptop")
        self.assertEqual([], run.sweep_unattended())  # the agent is still here
        self.assertEqual([job], run.sweep_unattended(now=self.later(25)))
        after = db.get_job(self.conn, job)
        self.assertEqual((after.state, after.needs, after.planner, after.executor),
                         ("queued", None, None, None))
        self.assertIn("handed to the local path by the worker", after.last_error)
        self.assertIn(f"for {self.MODEL} with no agent", after.last_error)

    def test_an_agent_back_after_the_hand_back_is_told_to_stop(self):
        job = self.start()["job"]
        run = self.worker()
        run.run_once()
        run.sweep_unattended(now=self.later(25))
        result = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual(result["do"], "stop")
        self.assertIn("not yours any more", result["say"])
        self.assertIn("handed to the local path by the worker", result["say"])
        self.assertEqual({}, db.paid_leases(self.conn))  # and took no lease

    def test_a_live_lease_keeps_the_job_however_long_it_has_been_parked(self):
        job = self.start()["job"]
        run = self.worker()
        run.run_once()
        db.lease_paid(self.conn, job, self.MODEL, 60)
        self.assertEqual([], run.sweep_unattended(now=self.later(25)))
        self.assertEqual(db.get_job(self.conn, job).state, "needs-laptop")

    def test_a_job_parked_a_moment_ago_waits_a_lease_for_its_agent(self):
        job = self.start()["job"]
        run = self.worker()
        run.run_once()
        db.release_lease(self.conn, job)
        self.assertEqual([], run.sweep_unattended(now=self.later(5)))
        self.assertEqual([job], run.sweep_unattended(now=self.later(21)))

    def test_preflight_does_not_offer_release_of_a_job_another_agent_holds(self):
        db.set_paid_models(self.conn, [self.MODEL, "gemini-3.8-flash"])
        theirs = paid.start(self.conn, model="gemini-3.8-flash", prompt="tide",
                            by="octocat", **self.one_worker)["job"]
        self.worker().run_once()
        report = paid.preflight(self.conn, self.MODEL, **self.one_worker)
        row = {r["job"]: r for r in report["info"]["parked"]}[theirs]
        self.assertEqual((row["yours"], row["leased_to"], row["command"]),
                         (False, "gemini-3.8-flash", None))
        shown = self.cli("paid", "preflight", "--as", self.MODEL).stdout
        line = next(l for l in shown.splitlines() if f"parked: job {theirs}" in l)
        self.assertIn("leased to gemini-3.8-flash until", line)
        self.assertNotIn("no agent", line)
        self.assertNotIn("release", line)

    # -- a job split between two agents -------------------------------------------

    OTHER = "claude-opus-5"

    def test_start_takes_a_registered_paid_executor_and_names_the_handoff(self):
        """2026-09-21: an operator asked Sonnet 5 to queue a job with Opus as
        executor, the split the New job page gives two local models, and the
        paid path had no way to say it. Now `start` takes the other agent's
        registered id and says up front who runs what."""
        db.set_paid_models(self.conn, [self.OTHER])
        result = self.start(executor=self.OTHER)
        self.assertTrue(result["started"], result)
        self.assertEqual((result["planner"], result["executor"]), (self.MODEL, self.OTHER))
        self.assertEqual((result["partner"], result["partner_step"]), (self.OTHER, "execute"))
        self.assertIn(f"sketchgen paid next --job {result['job']} --as {self.OTHER}",
                      result["say"])
        self.assertEqual(db.paid_leases(self.conn)[result["job"]]["model"], self.MODEL)

    def test_a_split_job_is_handed_from_the_planner_to_the_executor(self):
        """start (Sonnet) → next → plan → import → next: handoff → next (Opus)
        → attempt → import → gate → done. Two agents, one job, one lease at a
        time, and provenance per step."""
        db.set_paid_models(self.conn, [self.OTHER])
        job = self.start(executor=self.OTHER)["job"]
        self.worker().run_once()  # parks for the plan
        asked = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertEqual((asked["do"], asked["step"]), ("answer", "plan"))
        asked["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, asked, ctx=self.ctx)

        # The plan is in: the planner's part is over, before the worker even
        # claims the job again, and its lease is dropped — nobody is present.
        handed = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual((handed["do"], handed["to"], handed["step"]),
                         ("handoff", self.OTHER, "execute"))
        self.assertEqual(handed["then"], f"sketchgen paid next --job {job} --as {self.OTHER}")
        self.assertEqual(handed["release"], f"sketchgen paid release --job {job}")
        self.assertIn("your plan for job", handed["say"])
        self.assertEqual({}, db.paid_leases(self.conn))

        self.worker().run_once()  # parks for the attempt
        parked = {row["job"]: row for row in paid.parked_jobs(self.conn, self.MODEL)}
        self.assertFalse(parked[job]["yours"])
        self.assertEqual(parked[job]["command"], handed["then"])
        # A stale lease of the planner's, had it been kept, is taken over: the
        # step is the executor's now.
        db.lease_paid(self.conn, job, self.MODEL, 20)
        theirs = paid.next_for(self.conn, job, self.OTHER, ctx=self.ctx)
        self.assertEqual((theirs["do"], theirs["step"]), ("answer", "execute"), theirs)
        self.assertEqual(db.paid_leases(self.conn)[job]["model"], self.OTHER)
        theirs["items"][0]["answer"] = GOOD_SKETCH
        paid.import_packet(self.conn, theirs, ctx=self.ctx)
        self.worker().run_once()  # the gate
        done = paid.next_for(self.conn, job, self.OTHER, timeout=10)
        self.assertEqual((done["do"], done["state"]), ("done", "held"), done)
        entry = self.conn.execute("SELECT planner, executor FROM entries "
                                  "WHERE job_id = ?", (job,)).fetchone()
        self.assertEqual(tuple(entry), (self.MODEL, self.OTHER))
        self.assertEqual({}, db.paid_leases(self.conn))

    def test_the_executor_of_a_split_job_waits_for_the_other_agents_plan(self):
        """Opus starts the job with Sonnet as planner: Opus's `next` waits
        (without touching the planner's lease) until the attempt is its own,
        and Sonnet's `next` takes the plan over the starter's lease."""
        db.set_paid_models(self.conn, [self.MODEL, self.OTHER])
        started = self.start(model=self.OTHER, planner=self.MODEL)
        job = started["job"]
        self.assertEqual((started["partner"], started["partner_step"]), (self.MODEL, "plan"))
        self.worker().run_once()  # parks for the plan
        sleep, clock = self.fake_time()
        waiting = paid.next_for(self.conn, job, self.OTHER, timeout=60, interval=5,
                                sleep=sleep, clock=clock)
        self.assertEqual((waiting["do"], waiting["waiting_on"]), ("wait", self.MODEL))
        self.assertIn(f"waiting for {self.MODEL} to plan", waiting["say"])
        self.assertEqual(db.paid_leases(self.conn)[job]["model"], self.OTHER)
        mine = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertEqual((mine["do"], mine["step"]), ("answer", "plan"), mine)
        self.assertEqual(db.paid_leases(self.conn)[job]["model"], self.MODEL)
        mine["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, mine, ctx=self.ctx)
        handed = paid.next_for(self.conn, job, self.MODEL, timeout=10)
        self.assertEqual((handed["do"], handed["to"]), ("handoff", self.OTHER))
        self.worker().run_once()  # parks for the attempt
        theirs = paid.next_for(self.conn, job, self.OTHER, ctx=self.ctx)
        self.assertEqual((theirs["do"], theirs["step"]), ("answer", "execute"), theirs)

    def test_a_split_job_over_the_cli_exits_0_on_handoff(self):
        db.begin_step(self.conn, step="idle", headline="Nothing to do", pid=os.getpid())
        db.set_paid_models(self.conn, [self.OTHER])
        jobs = ["--jobs-dir", str(self.jobs_dir)]
        started = self.cli("paid", "start", "--as", self.MODEL, "--by", "octocat",
                           "--executor", self.OTHER, "--prompt", "hot air balloons", *jobs)
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        self.assertIn(f"execute is {self.OTHER}'s", started.stdout)
        job = db.list_jobs(self.conn, "queued")[-1].id
        self.worker().run_once()
        packet = json.loads(self.cli("paid", "next", "--job", str(job), "--as", self.MODEL,
                                     "--timeout", "1", *jobs).stdout)
        packet["items"][0]["answer"] = CLEAN_PLAN
        self.cli("paid", "import", "-", *jobs, stdin=json.dumps(packet))
        handed = self.cli("paid", "next", "--job", str(job), "--as", self.MODEL,
                          "--timeout", "1", *jobs)
        self.assertEqual(handed.returncode, 0, handed.stderr)
        self.assertEqual(json.loads(handed.stdout)["do"], "handoff")
        # And the preflight, run by anyone, names the agent to bring.
        self.worker().run_once()
        pre = self.cli("paid", "preflight", "--as", self.MODEL, *jobs)
        self.assertIn(f"sketchgen paid next --job {job} --as {self.OTHER}", pre.stdout)
        self.assertIn(f"sketchgen paid release --job {job}", pre.stdout)

    # -- the whole recipe, as AGENTS.md writes it now ------------------------------

    def test_the_agents_md_recipe_runs_end_to_end_over_the_cli(self):
        """start → next → answer → import → next → … → done, held.

        Only the worker is run in-process, standing in for the daemon; its
        status card is what the preflight in `start` sees.
        """
        db.begin_step(self.conn, step="idle", headline="Nothing to do", pid=os.getpid())
        jobs = ["--jobs-dir", str(self.jobs_dir)]
        started = self.cli("paid", "start", "--as", self.MODEL, "--by", "octocat",
                           "--json", *jobs, stdin="a breathing field\n")
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        job = json.loads(started.stdout)["job"]
        self.assertEqual(db.get_job(self.conn, job).prompt, "a breathing field")

        for step, reply in (("plan", CLEAN_PLAN), ("execute", GOOD_SKETCH)):
            self.worker().run_once()  # the daemon's pass
            asked = self.cli("paid", "next", "--job", str(job), "--as", self.MODEL,
                             "--timeout", "1", *jobs)
            self.assertEqual(asked.returncode, 0, asked.stderr)
            packet = json.loads(asked.stdout)
            self.assertEqual((packet["do"], packet["step"]), ("answer", step), packet)
            packet["items"][0]["answer"] = reply
            imported = self.cli("paid", "import", "-", *jobs, stdin=json.dumps(packet))
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(json.loads(imported.stdout)["recorded"], 1, imported.stdout)

        self.worker().run_once()  # the gate
        done = json.loads(self.cli("paid", "next", "--job", str(job), "--as",
                                   self.MODEL, "--timeout", "1", *jobs).stdout)
        self.assertEqual((done["do"], done["state"]), ("done", "held"), done)
        entry = self.conn.execute("SELECT planner, executor FROM entries "
                                  "WHERE job_id = ?", (job,)).fetchone()
        self.assertEqual(tuple(entry), (self.MODEL, self.MODEL))
        self.assertEqual({}, db.paid_leases(self.conn))

    def test_start_at_the_cli_prints_the_checks_and_exits_3_when_not_ready(self):
        result = self.cli("paid", "start", "--as", self.MODEL, "--by", "octocat",
                          "--prompt", "x", "--jobs-dir", str(self.jobs_dir))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("NOT READY", result.stdout)
        self.assertIn("worker", result.stdout)
        self.assertEqual([], db.list_jobs(self.conn, "queued"))

    def test_release_at_the_cli_takes_jobs(self):
        job = self.start()["job"]
        self.worker().run_once()
        result = self.cli("paid", "release", "--job", str(job), "--by", self.MODEL,
                          "--reason", "stopping", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["released"][0]["state"], "queued")
        result = self.cli("paid", "release", "--json")
        self.assertEqual(result.returncode, 3)


class TryTests(AgentLoopTests):
    """`paid try`: the node's own gate over a candidate that is not an attempt.

    Job 1286 (entry 1279, 2026-09-21) passed the gate first time after the
    agent spent thirty-seven minutes standing up a browser rig on the laptop,
    which still read a third under the node's frame time. The gate is here;
    this is the verb that lends it, between claims, without spending an
    attempt or touching a row (docs/plans/agent-rig.md §4).
    """

    def leased(self, assertions=("motion(idle)",), **kwargs):
        """A job of this agent's, parked for its attempt, with a live lease.

        The lease is deliberately short, so that the renewal every `try` does
        is visible in ``until_utc`` rather than hidden by a second's rounding.
        """
        job = db.enqueue(self.conn, "a breathing field", "octocat",
                         brief="A grey field that brightens and dims, forever.",
                         assertions=list(assertions), planner=self.MODEL,
                         executor=self.MODEL, rules_file="treatment", **kwargs)
        db.set_paid_models(self.conn, [self.MODEL])
        db.transition(self.conn, job, "needs-laptop", needs="execute")
        db.lease_paid(self.conn, job, self.MODEL, 5)
        return job

    def gate(self, verdicts=(0,), **report):
        return test_worker.StubGate(list(verdicts), reports=[gate_report(**report)])

    def drive(self, run, job, answer=GOOD_SKETCH, timeout=60, **kwargs):
        """One `paid try`, with the worker's nap standing in for the daemon.

        The daemon serves tries from inside its own loop, so the agent's poll
        is exactly where the worker gets its turn: this sleep is that turn.
        """
        now = [0.0]

        def sleep(seconds):
            now[0] += seconds
            run._nap(0.2)

        return paid.try_for(self.conn, job, self.MODEL, answer, ctx=self.ctx,
                            sleep=sleep, clock=lambda: now[0], timeout=timeout,
                            **kwargs)

    def try_dir(self, job, k=1):
        return self.jobs_dir / str(job) / f"try-{k}"

    def job_row(self, job):
        return tuple(self.conn.execute("SELECT * FROM jobs WHERE id = ?",
                                       (job,)).fetchone())

    def steps(self):
        """Every activity row the worker wrote, oldest first, by step name."""
        return [row["step"] for row in
                self.conn.execute("SELECT step FROM activity ORDER BY id")]

    # -- 1: the plumbing ---------------------------------------------------

    def test_the_nap_serves_a_try_and_the_job_is_untouched_by_it(self):
        job = self.leased()
        before = self.job_row(job)
        was = db.paid_leases(self.conn)[job]["until_utc"]
        gate = self.gate(assertions={"motion(idle)": {"pass": True,
                                                      "detail": "idle 0->120 frames"}})
        run = self.worker(gate_fn=gate)
        result = self.drive(run, job)

        self.assertEqual(result["do"], "verdict", result)
        self.assertEqual((result["exit"], result["offplan"]), (0, []))
        self.assertEqual(result["timings"]["ms_per_frame"], 9.8)
        self.assertTrue(result["assertions"]["motion(idle)"]["pass"])
        self.assertTrue(result["artefacts"]["strip"].endswith("strip.png"))
        self.assertEqual((result["tries_used"], result["tries_cap"]), (1, 8))
        self.assertEqual(result["then"], "sketchgen paid import -")
        self.assertEqual(len(gate.calls), 1)

        # The record is the directory and the meta row, and nothing else.
        written = self.try_dir(job)
        self.assertEqual(
            json.loads((written / "result.json").read_text())["kind"], "sketchgen-try")
        for name in ("reply.txt", "sketch.js", "index.html", "execution.json"):
            self.assertTrue((written / name).is_file(), name)
        self.assertEqual(before, self.job_row(job))
        self.assertEqual([], db.list_attempts(self.conn, job))
        self.assertEqual([], list(self.conn.execute(
            "SELECT id FROM entries WHERE job_id = ?", (job,))))
        self.assertEqual([], db.paid_tries(self.conn)[job]["pending"])
        self.assertGreater(db.paid_leases(self.conn)[job]["until_utc"], was)

    def test_a_try_writes_the_ghost_script_the_candidate_carried(self):
        # The same free ride the import gets: a try is executor.run(stub=…)
        # too, so an agent can see its own script land before it commits to
        # the attempt (auto-mouse.md §4.2).
        job = self.leased()
        run = self.worker(gate_fn=self.gate())
        self.assertEqual("verdict", self.drive(run, job, answer=GHOST_SKETCH)["do"])
        written = self.try_dir(job)
        self.assertEqual(GHOST_EVENTS,
                         json.loads((written / "ghost.json").read_text()))
        # and the replay's own result.json is filed as execution.json, which
        # is where the count of what it kept goes with it
        self.assertEqual(
            {"events": 2},
            json.loads((written / "execution.json").read_text())["ghost"])

    def test_the_verdict_names_the_ghost_frames_beside_the_strip(self):
        """auto-mouse.md §5.3: an agent can look at what its own script did.

        The whole point of a `try` is that the node's gate runs and the agent
        reads the result; since 2026-09-21 that result includes four frames of
        the sketch under the pointer script, at a node path to `scp`. The
        ghost window fails nothing, so a verdict that did not name it would be
        an agent writing a `ghost` block and never seeing it played.
        """
        job = self.leased()
        summary = {"source": "executor", "script": None, "events": 2,
                   "played": 2, "ms": 800}
        run = self.worker(gate_fn=self.gate(ghost=summary))
        result = self.drive(run, job, answer=GHOST_SKETCH)
        self.assertEqual("verdict", result["do"], result)
        self.assertTrue(result["artefacts"]["ghost"].endswith("ghost.png"))
        self.assertTrue(result["artefacts"]["strip"].endswith("strip.png"))

    def test_a_verdict_from_a_gate_with_no_ghost_window_names_none(self):
        job = self.leased()
        run = self.worker(gate_fn=self.gate())
        result = self.drive(run, job)
        self.assertNotIn("ghost", result["artefacts"])

    def test_a_second_try_is_its_own_directory_and_counts_up(self):
        job = self.leased()
        run = self.worker(gate_fn=test_worker.StubGate([1, 0]))
        first = self.drive(run, job)
        self.assertEqual((first["do"], first["exit"]), ("verdict", 1))
        self.assertEqual(first["then"], paid.try_command(job, self.MODEL))
        self.assertIn("fix", first["say"])
        second = self.drive(run, job)
        self.assertEqual((second["tries_used"], second["exit"]), (2, 0))
        self.assertTrue(self.try_dir(job, 2).is_dir())
        self.assertEqual(2, db.paid_tries(self.conn)[job]["count"])

    def test_a_try_before_the_plan_says_it_checked_no_assertions(self):
        job = self.leased(assertions=())
        run = self.worker(gate_fn=self.gate())
        result = self.drive(run, job)
        self.assertEqual(result["assertions_checked"], [])
        self.assertTrue(any("not planned" in note for note in result["notes"]),
                        result["notes"])

    # -- 2: it never overlaps a real gate run ------------------------------

    def test_a_try_asked_for_mid_attempt_waits_for_the_attempt(self):
        """Acceptance 2: the worker is one thread, so ordering needs no lock."""
        mine = self.leased()
        theirs = db.enqueue(self.conn, "the node's own job", "octocat",
                            brief="b", assertions=["motion(idle)"])

        def midway(n):
            self.try_dir(mine).mkdir(parents=True, exist_ok=True)
            (self.try_dir(mine) / "reply.txt").write_text(GOOD_SKETCH, encoding="utf-8")
            db.add_paid_try(self.conn, mine, self.MODEL, 1, str(self.try_dir(mine)))

        run = self.worker(executor_fn=test_worker.StubExecutor(on_call=midway),
                          gate_fn=test_worker.StubGate([0, 0]))
        run.run_once()          # the attempt the worker was already in
        self.assertEqual("held", db.get_job(self.conn, theirs).state)
        self.assertNotIn("trying", self.steps())
        run.run_once()          # the next pass, before it claims anything
        steps = self.steps()
        self.assertIn("trying", steps)
        self.assertLess(steps.index("evaluating"), steps.index("trying"))
        self.assertEqual(
            json.loads((self.try_dir(mine) / "result.json").read_text())["exit"], 0)

    # -- 3: the lease is the permission ------------------------------------

    def test_without_a_lease_a_try_stops_and_writes_nothing(self):
        job = self.leased()
        db.release_lease(self.conn, job)
        result = paid.try_for(self.conn, job, self.MODEL, GOOD_SKETCH, ctx=self.ctx)
        self.assertEqual(result["do"], "stop")
        self.assertIn("no lease", result["say"])
        db.lease_paid(self.conn, job, "claude-opus-5", 20)
        result = paid.try_for(self.conn, job, self.MODEL, GOOD_SKETCH, ctx=self.ctx)
        self.assertEqual((result["do"], result["leased_to"]), ("stop", "claude-opus-5"))
        self.assertFalse(self.try_dir(job).exists())
        self.assertEqual({}, db.paid_tries(self.conn))

    def test_a_request_whose_lease_lapsed_is_dropped_rather_than_gated(self):
        job = self.leased()
        self.try_dir(job).mkdir(parents=True)
        (self.try_dir(job) / "reply.txt").write_text(GOOD_SKETCH, encoding="utf-8")
        db.add_paid_try(self.conn, job, self.MODEL, 1, str(self.try_dir(job)))
        db.lease_paid(self.conn, job, self.MODEL, -1)   # the agent left
        gate = test_worker.StubGate([0])
        run = self.worker(gate_fn=gate)
        self.assertEqual(0, run._serve_tries())
        self.assertEqual([], gate.calls)
        self.assertFalse((self.try_dir(job) / "result.json").exists())
        self.assertEqual([], db.paid_tries(self.conn)[job]["pending"])

    def test_an_agent_polling_after_its_request_was_dropped_is_told_to_stop(self):
        job = self.leased()
        run = self.worker(gate_fn=test_worker.StubGate([0]))

        def sleep(seconds):
            db.drop_paid_try(self.conn, job, 1)   # the worker found no lease

        result = paid.try_for(self.conn, job, self.MODEL, GOOD_SKETCH, ctx=self.ctx,
                              sleep=sleep, clock=lambda: 0.0, timeout=60)
        self.assertEqual(result["do"], "stop")
        self.assertIn("dropped by the worker", result["say"])

    # -- 4: the cap --------------------------------------------------------

    def test_the_ninth_try_is_refused_and_the_cap_comes_from_meta(self):
        job = self.leased()
        db.add_paid_try(self.conn, job, self.MODEL, 8, str(self.try_dir(job, 8)))
        db.drop_paid_try(self.conn, job, 8)
        result = paid.try_for(self.conn, job, self.MODEL, GOOD_SKETCH, ctx=self.ctx)
        self.assertEqual(result["do"], "stop")
        self.assertIn("8 of 8 tries used", result["say"])
        self.assertFalse(self.try_dir(job, 9).exists())

        db.set_meta(self.conn, "paid_try_cap", "2")
        other = self.leased()
        db.add_paid_try(self.conn, other, self.MODEL, 2, str(self.try_dir(other, 2)))
        db.drop_paid_try(self.conn, other, 2)
        result = paid.try_for(self.conn, other, self.MODEL, GOOD_SKETCH, ctx=self.ctx)
        self.assertEqual((result["do"], result["tries_cap"]), ("stop", 2))
        self.assertIn("2 of 2 tries used", result["say"])

    # -- 5: a bad reply costs nothing --------------------------------------

    def test_a_reply_with_no_sketch_is_rejected_and_spends_no_try(self):
        job = self.leased()
        result = paid.try_for(self.conn, job, self.MODEL, "I would love to help!\n",
                              ctx=self.ctx)
        self.assertEqual((result["do"], result["reason"]),
                         ("rejected", "no fenced js block"))
        self.assertEqual(result["tries_used"], 0)
        self.assertEqual({}, db.paid_tries(self.conn))
        self.assertFalse(self.try_dir(job).exists())

    # -- the verb, over the CLI --------------------------------------------

    def test_try_at_the_cli_prints_one_object_and_exits_3_on_a_stop(self):
        job = self.leased()
        jobs = ["--jobs-dir", str(self.jobs_dir)]
        rejected = self.cli("paid", "try", "--job", str(job), "--as", self.MODEL,
                            *jobs, stdin="nothing fenced here\n")
        self.assertEqual(rejected.returncode, 0, rejected.stderr)
        self.assertEqual(json.loads(rejected.stdout)["do"], "rejected")

        db.release_lease(self.conn, job)
        stopped = self.cli("paid", "try", "--job", str(job), "--as", self.MODEL,
                           *jobs, stdin=GOOD_SKETCH)
        self.assertEqual(stopped.returncode, 3, stopped.stdout)
        self.assertEqual(json.loads(stopped.stdout)["do"], "stop")

    def test_a_try_that_the_worker_has_not_reached_says_wait(self):
        job = self.leased()
        result = paid.try_for(self.conn, job, self.MODEL, GOOD_SKETCH, ctx=self.ctx,
                              sleep=lambda s: None, clock=iter_clock([0, 0, 300]),
                              timeout=240)
        self.assertEqual((result["do"], result["timed_out"]), ("wait", True))
        self.assertEqual(result["then"], paid.try_command(job, self.MODEL))

    # -- 6: the same referee, on the node only -----------------------------

    @unittest.skip(
        "needs the node: the real gate is Playwright and headless Chromium, "
        "which the laptop does not have. The operator runs it once on "
        "sld-cloud after the deploy and pastes the lines into the PR."
    )
    def test_the_gate_fixtures_through_try_return_their_expected_verdict(self):
        """Acceptance 6 of docs/plans/agent-rig.md §4.5: same gate, same answer.

        Run on the node, against a temporary database in a temporary
        directory, with:

            cd ~/sketchgen/app && ~/sketchgen/.venv/bin/python3 -m unittest \\
                tests.test_paid.TryTests.test_the_gate_fixtures_through_try_\\
                return_their_expected_verdict

        This is **not** a second worker (AGENTS.md rule 1): it claims nothing,
        calls no model and touches no real database. It calls the gate — the
        one part of a job that is safe to run beside the daemon, because it is
        CPU and a browser rather than the inference slot.
        """
        expected = json.loads((REPO_ROOT / "gate" / "fixtures" /
                               "expected.json").read_text(encoding="utf-8"))
        asserted = expected.get("assertions_expected") or {}
        for name, want in sorted(expected.items()):
            if name == "assertions_expected":
                continue
            with self.subTest(fixture=name):
                fixture = REPO_ROOT / "gate" / "fixtures" / name
                reply = (
                    "```js\n" + (fixture / "sketch.js").read_text(encoding="utf-8")
                    + "\n```\n\n```html\n"
                    + (fixture / "index.html").read_text(encoding="utf-8")
                    + "\n```\n\n## Statement\n\nA gate fixture.\n"
                )
                job = self.leased(assertions=tuple(asserted.get(name, {})))
                run = self.worker(gate_fn=None,
                                  gate_path=str(REPO_ROOT / "gate" / "sketch_gate.py"))
                result = self.drive(run, job, answer=reply, timeout=600)
                self.assertEqual(result["exit"], want["exit"], result)
                for check, value in (want.get("checks") or {}).items():
                    self.assertEqual(result["checks"].get(check), value, check)
                for word, passed in (asserted.get(name) or {}).items():
                    self.assertEqual(result["assertions"][word]["pass"], passed, word)


def iter_clock(values):
    """A clock that reads each of ``values`` in turn, then stays at the last."""
    remaining = list(values)

    def clock():
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return clock


class UsageTests(AgentLoopTests):
    """What an off-node step costs: the node times the round trip itself and
    records the token counts the agent reports, on the rows a local attempt
    uses, so the entry page stops saying "—" for a Sonnet sketch."""

    def test_every_item_offers_an_empty_usage_and_says_what_goes_in_it(self):
        job = self.start()["job"]
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertEqual(packet["items"][0]["usage"],
                         {"prompt_tokens": None, "completion_tokens": None})
        self.assertIn("never an estimate", packet["how_to_answer"])

    def test_usage_is_read_as_integers_or_not_at_all(self):
        self.assertEqual(paid.usage_of({"usage": {"prompt_tokens": "812",
                                                  "completion_tokens": None}}),
                         {"prompt_tokens": 812, "completion_tokens": None})
        self.assertEqual(paid.usage_of({"usage": {"prompt_tokens": -1,
                                                  "completion_tokens": "lots"}}),
                         {"prompt_tokens": None, "completion_tokens": None})
        self.assertEqual(paid.usage_of({}), {"prompt_tokens": None, "completion_tokens": None})

    def test_the_round_trip_and_the_counts_land_on_the_attempt_and_the_entry(self):
        job = self.start()["job"]
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = CLEAN_PLAN
        packet["items"][0]["usage"] = {"prompt_tokens": 900, "completion_tokens": 150}
        packet["created_utc"] = "2026-09-21T13:48:00Z"  # cut a while ago
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        plan = json.loads((self.jobs_dir / str(job) / "plan.json").read_text())
        self.assertEqual(plan["tokens"], {"prompt_tokens": 900, "completion_tokens": 150})
        self.assertGreater(plan["durations"]["round_trip_s"], 60)

        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = GOOD_SKETCH
        packet["items"][0]["usage"] = {"prompt_tokens": 2100, "completion_tokens": 3300}
        packet["created_utc"] = "2026-09-21T13:50:00Z"
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        meta = json.loads((self.jobs_dir / str(job) / "attempt-1" / worker.PAID_META).read_text())
        self.assertEqual(meta["usage"], {"prompt_tokens": 2100, "completion_tokens": 3300})
        self.assertGreater(meta["round_trip_s"], 60)

        self.worker().run_once()  # the gate
        attempt = db.list_attempts(self.conn, job)[0]
        self.assertEqual((attempt.prompt_tokens, attempt.completion_tokens), (2100, 3300))
        self.assertEqual(attempt.wall_s, meta["round_trip_s"])
        entry = self.conn.execute(
            "SELECT prompt_tokens, completion_tokens, wall_s, shape FROM entries "
            "WHERE job_id = ?", (job,)).fetchone()
        self.assertEqual(tuple(entry)[:3], (2100, 3300, meta["round_trip_s"]))
        self.assertTrue(entry["shape"].startswith("off-node (claude-sonnet-5) · gated on "),
                        entry["shape"])

    def test_unreported_counts_stay_null_rather_than_zero(self):
        job = self.start()["job"]
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = GOOD_SKETCH
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.worker().run_once()
        attempt = db.list_attempts(self.conn, job)[0]
        self.assertEqual((attempt.prompt_tokens, attempt.completion_tokens), (None, None))
        self.assertIsNotNone(attempt.wall_s)


# ---------------------------------------------------------------------------
# The process cost (packet 8)
# ---------------------------------------------------------------------------


class Migration015Tests(unittest.TestCase):
    """Five nullable columns, and nothing already written moves.

    015 is `ALTER TABLE … ADD COLUMN` five times, so this is less about the
    SQL than about the promise attached to it: a node mid-deploy, with rows
    written under 014, reads them all back afterwards.
    """

    def test_a_database_at_014_keeps_every_row_and_gains_the_columns(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-015-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn = db.connect(tmp / "old.db")
        self.addCleanup(conn.close)
        db.migrate(conn, migrations_upto(14, tmp / "migrations"))
        self.assertEqual(db.schema_version(conn), 14)

        job_id = db.enqueue(conn, "a tide of slow lines", "octocat", brief="a brief",
                            planner="claude-sonnet-5", executor="claude-sonnet-5")
        db.add_attempt(conn, job_id, 1, model="claude-sonnet-5", wall_s=111.0,
                       prompt_tokens=2100, completion_tokens=3300, gate_exit=0)
        entry_id = db.create_entry(conn, job_id, prompt="a tide of slow lines",
                                   wall_s=111.0, attempts=1)
        jobs = dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())
        entries = dict(conn.execute("SELECT * FROM entries WHERE id = ?",
                                    (entry_id,)).fetchone())

        self.assertEqual(db.migrate(conn), [15])
        self.assertEqual(db.schema_version(conn), 15)

        job = db.get_job(conn, job_id)
        self.assertEqual(jobs, {k: v for k, v in dict(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        ).items() if k in jobs})
        self.assertEqual(entries, {k: v for k, v in dict(
            conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        ).items() if k in entries})
        # The new columns are null on every row written before them, which is
        # the true thing to say about a job nobody reported a cost for.
        self.assertEqual((job.since_utc, job.note), (None, None))
        self.assertIsNone(db.list_attempts(conn, job_id)[0].process_json)
        after = dict(conn.execute("SELECT * FROM entries WHERE id = ?",
                                  (entry_id,)).fetchone())
        self.assertEqual((after["note"], after["process_json"]), (None, None))
        # and they are writable through the verbs, not by hand
        db.transition(conn, job_id, "planning")
        self.assertEqual(db.get_job(conn, job_id).state, "planning")

    def test_db_init_applies_it(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-015-init-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.assertIn(15, db.init(tmp / "fresh.db"))
        conn = db.connect(tmp / "fresh.db")
        self.addCleanup(conn.close)
        self.assertEqual(db.schema_version(conn), 15)
        self.assertGreaterEqual(db.schema_version(self.fixture()), 15)

    def fixture(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-015-fixture-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn, _ids = test_judge.build_entries(tmp)
        self.addCleanup(conn.close)
        return conn


class ProcessCostTests(AgentLoopTests):
    """What the work around the reply cost, recorded beside it and kept apart.

    Job 1286 became entry 1279 in 4 min 26 s of node time. The 37 minutes and
    193,000 generated tokens before `paid start` — a browser rig, a local
    prototype, a skill — were nowhere, and the entry reads as a sketch written
    in under two minutes. These are the two costs (dossier 01 §6.4): the reply's,
    which the node measures, and the process's, which only the agent knows.
    """

    PROCESS = {"session_s": 2520, "output_tokens": 207537, "thinking_tokens": 42000,
               "tool_calls": 41, "screenshots": 9, "effort": "max"}

    def drive(self, *, process=None, usage=None, since=None, note=None, tries=0):
        """One whole job, off the node, through the worker's stub gate."""
        job = self.start(since=since, note=note)["job"]
        self.worker().run_once()                       # parked for the plan
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.worker().run_once()                       # parked for the attempt
        for k in range(1, tries + 1):
            # What packet 7 leaves behind: the count outlives the request, and
            # it is the node's own, never the agent's word.
            db.add_paid_try(self.conn, job, self.MODEL, k,
                            str(self.jobs_dir / str(job) / f"try-{k}"))
            db.drop_paid_try(self.conn, job, k)
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = GOOD_SKETCH
        if usage is not None:
            packet["items"][0]["usage"] = usage
        if process is not None:
            packet["items"][0]["process"] = process
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.worker().run_once()                       # the gate, and the entry
        return job, report

    def entry_of(self, job):
        return self.conn.execute(
            "SELECT * FROM entries WHERE job_id = ?", (job,)).fetchone()

    # -- the object ------------------------------------------------------------

    def test_the_item_offers_a_process_slot_only_where_there_is_a_row_for_it(self):
        job = self.start()["job"]
        self.worker().run_once()
        plan = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertNotIn("process", plan["items"][0])
        plan["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, plan, ctx=self.ctx)
        self.worker().run_once()
        attempt = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        self.assertEqual(attempt["items"][0]["process"], paid.EMPTY_PROCESS)
        self.assertIn("tokens generated", attempt["how_to_answer"])

    def test_process_is_read_as_numbers_or_not_at_all(self):
        read = paid.process_of({"process": {"session_s": "2520.4", "output_tokens": "9",
                                            "tool_calls": -3, "screenshots": "lots",
                                            "effort": "  max  "}})
        self.assertEqual(read["session_s"], 2520.4)
        self.assertEqual(read["output_tokens"], 9)
        self.assertIsNone(read["tool_calls"])
        self.assertIsNone(read["screenshots"])
        self.assertEqual(read["effort"], "max")
        self.assertEqual(paid.process_of({}), paid.EMPTY_PROCESS)
        self.assertFalse(paid.process_reported(paid.process_of({})))

    # -- where it lands --------------------------------------------------------

    def test_a_reported_process_lands_on_the_attempt_then_on_the_entry(self):
        job, _report = self.drive(process=self.PROCESS, tries=4,
                                  note="skill=algorithmic-art; local prototype")
        attempt = db.list_attempts(self.conn, job)[0]
        found = json.loads(attempt.process_json)
        self.assertEqual(found["session_s"], 2520.0)
        self.assertEqual(found["output_tokens"], 207537)
        self.assertEqual(found["effort"], "max")
        # the one field the node fills in itself
        self.assertEqual(found["tries"], 4)
        entry = self.entry_of(job)
        self.assertEqual(json.loads(entry["process_json"]), found)
        self.assertEqual(entry["note"], "skill=algorithmic-art; local prototype")

    def test_an_unreported_process_is_null_on_both_rows_and_never_zero(self):
        job, _report = self.drive()
        self.assertIsNone(db.list_attempts(self.conn, job)[0].process_json)
        self.assertIsNone(self.entry_of(job)["process_json"])
        self.assertIsNone(self.entry_of(job)["note"])

    def test_a_half_reported_process_keeps_the_rest_null(self):
        job, _report = self.drive(process={"output_tokens": 12000})
        found = json.loads(db.list_attempts(self.conn, job)[0].process_json)
        self.assertEqual(found["output_tokens"], 12000)
        self.assertIsNone(found["session_s"])
        self.assertIsNone(found["tool_calls"])
        self.assertNotIn("tries", found)

    # -- an empty process on a --since job is declared or it is rejected --------

    SINCE = "2026-09-21T20:00:00Z"

    def attempt_packet(self, *, since):
        """A job driven to its attempt packet, the sketch written in."""
        job = self.start(since=since)["job"]
        self.worker().run_once()
        plan = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        plan["items"][0]["answer"] = CLEAN_PLAN
        paid.import_packet(self.conn, plan, ctx=self.ctx)
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = GOOD_SKETCH
        return job, packet

    def test_an_empty_process_on_a_since_job_is_rejected_and_nothing_is_written(self):
        # Job 1308 (entry 1300, 2026-09-21): started with --since, never ran
        # cost.py, landed a process of nulls in silence.
        job, packet = self.attempt_packet(since=self.SINCE)
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.recorded, [])
        self.assertEqual(len(report.rejected), 1)
        reason = report.rejected[0]["reason"]
        self.assertIn(f"python3 rig/cost.py --since {self.SINCE}", reason)
        self.assertIn("--no-process", reason)
        self.assertIn("Nothing was written", reason)
        # kept, verbatim, and the job is exactly where it was
        self.assertEqual(report.rejected[0]["answer"], GOOD_SKETCH)
        self.assertEqual(db.get_job(self.conn, job).state, "needs-laptop")
        self.assertEqual(db.list_attempts(self.conn, job), [])
        # the export wrote the prompt into attempt-1; the landing wrote nothing
        attempt_dir = self.jobs_dir / str(job) / "attempt-1"
        self.assertFalse((attempt_dir / worker.PAID_REPLY).exists())
        self.assertFalse((attempt_dir / worker.PAID_META).exists())
        # and the same packet lands once the costs are in it: a rejection is a
        # re-import, not a lost attempt
        packet["items"][0]["process"] = self.PROCESS
        packet["items"][0]["usage"] = self.USAGE
        again = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(again.recorded), 1, again.rejected)
        self.worker().run_once()
        found = json.loads(db.list_attempts(self.conn, job)[0].process_json)
        self.assertEqual(found["output_tokens"], 207537)

    def test_a_half_reported_process_is_reported_enough(self):
        job, packet = self.attempt_packet(since=self.SINCE)
        packet["items"][0]["process"] = {"output_tokens": 12000}
        packet["items"][0]["usage"] = self.USAGE
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1, report.rejected)

    def test_no_process_declares_the_empty_cost_instead(self):
        job, packet = self.attempt_packet(since=self.SINCE)
        packet["items"][0]["usage"] = self.USAGE
        declared = dataclasses.replace(self.ctx, process_unreported=True)
        report = paid.import_packet(self.conn, packet, ctx=declared)
        self.assertEqual(len(report.recorded), 1, report.rejected)
        meta = json.loads((self.jobs_dir / str(job) / "attempt-1" / worker.PAID_META)
                          .read_text(encoding="utf-8"))
        self.assertTrue(meta["process_unreported"])
        self.assertEqual(meta["process"], paid.EMPTY_PROCESS)
        # declared is still not reported: the rows stay null, never zero
        self.worker().run_once()
        self.assertIsNone(db.list_attempts(self.conn, job)[0].process_json)

    def test_a_job_without_since_made_no_promise(self):
        job, packet = self.attempt_packet(since=None)
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1, report.rejected)
        meta = json.loads((self.jobs_dir / str(job) / "attempt-1" / worker.PAID_META)
                          .read_text(encoding="utf-8"))
        self.assertFalse(meta["process_unreported"])

    # -- and the reply's own counts, for the same reason ------------------------

    USAGE = {"prompt_tokens": 68935, "completion_tokens": 2493}

    def test_an_empty_usage_on_a_since_job_is_rejected_and_nothing_is_written(self):
        # Job 1319 (entry 1312, 2026-09-22): #145 got its process recorded and
        # its usage stayed null, so the page said "—" for both counts while the
        # transcript held them all along.
        job, packet = self.attempt_packet(since=self.SINCE)
        packet["items"][0]["process"] = self.PROCESS
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(report.recorded, [])
        self.assertEqual(len(report.rejected), 1)
        reason = report.rejected[0]["reason"]
        self.assertIn("usage is empty", reason)
        self.assertIn(f"python3 rig/cost.py --since {self.SINCE} --reply", reason)
        self.assertIn("--no-usage", reason)
        self.assertIn("Nothing was written", reason)
        self.assertEqual(report.rejected[0]["answer"], GOOD_SKETCH)
        self.assertEqual(db.get_job(self.conn, job).state, "needs-laptop")
        self.assertEqual(db.list_attempts(self.conn, job), [])
        attempt_dir = self.jobs_dir / str(job) / "attempt-1"
        self.assertFalse((attempt_dir / worker.PAID_META).exists())
        # the same packet lands once the counts are in it, and they reach the
        # rows the entry page reads
        packet["items"][0]["usage"] = self.USAGE
        again = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(again.recorded), 1, again.rejected)
        self.worker().run_once()
        attempt = db.list_attempts(self.conn, job)[0]
        self.assertEqual((attempt.prompt_tokens, attempt.completion_tokens), (68935, 2493))
        entry = self.entry_of(job)
        self.assertEqual((entry["prompt_tokens"], entry["completion_tokens"]), (68935, 2493))

    def test_the_process_is_asked_for_before_the_usage(self):
        # One command fills both; the reason names the first thing missing.
        job, packet = self.attempt_packet(since=self.SINCE)
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertIn("process is empty", report.rejected[0]["reason"])
        self.assertIn("--reply", report.rejected[0]["reason"])

    def test_a_half_reported_usage_is_reported_enough(self):
        job, packet = self.attempt_packet(since=self.SINCE)
        packet["items"][0]["process"] = self.PROCESS
        packet["items"][0]["usage"] = {"completion_tokens": 2493}
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1, report.rejected)

    def test_no_usage_declares_the_blank_counts_instead(self):
        job, packet = self.attempt_packet(since=self.SINCE)
        packet["items"][0]["process"] = self.PROCESS
        declared = dataclasses.replace(self.ctx, usage_unreported=True)
        report = paid.import_packet(self.conn, packet, ctx=declared)
        self.assertEqual(len(report.recorded), 1, report.rejected)
        meta = json.loads((self.jobs_dir / str(job) / "attempt-1" / worker.PAID_META)
                          .read_text(encoding="utf-8"))
        self.assertTrue(meta["usage_unreported"])
        self.assertFalse(meta["process_unreported"])
        self.assertEqual(meta["usage"], paid.EMPTY_USAGE)
        # declared is still not reported: the rows stay null, never zero
        self.worker().run_once()
        attempt = db.list_attempts(self.conn, job)[0]
        self.assertEqual((attempt.prompt_tokens, attempt.completion_tokens), (None, None))

    def test_a_job_without_since_made_no_promise_about_usage_either(self):
        job, packet = self.attempt_packet(since=None)
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1, report.rejected)
        meta = json.loads((self.jobs_dir / str(job) / "attempt-1" / worker.PAID_META)
                          .read_text(encoding="utf-8"))
        self.assertFalse(meta["usage_unreported"])

    def test_the_import_flag_reaches_the_context(self):
        from sketchgen.cli import paid as cli
        parser = argparse.ArgumentParser()
        cli.register(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["paid", "import", "--no-process", "-"])
        self.assertTrue(cli._ctx(args).process_unreported)
        self.assertFalse(cli._ctx(args).usage_unreported)
        args = parser.parse_args(["paid", "import", "--no-usage", "-"])
        self.assertTrue(cli._ctx(args).usage_unreported)
        args = parser.parse_args(["paid", "import", "-"])
        self.assertFalse(cli._ctx(args).process_unreported)
        self.assertFalse(cli._ctx(args).usage_unreported)

    def test_the_tries_alone_are_recorded_when_the_agent_reports_nothing(self):
        job, _report = self.drive(tries=2)
        found = json.loads(db.list_attempts(self.conn, job)[0].process_json)
        self.assertEqual(found["tries"], 2)
        self.assertIsNone(found["output_tokens"])

    def test_the_process_cost_changes_none_of_the_reply_s_own_numbers(self):
        """The two costs stay two numbers (plan §5.3).

        Nothing in pairs.py, the judge or a batch total reads `process_json`;
        the columns they do read are the same with a process object and
        without one, on two jobs answered identically.
        """
        usage = {"prompt_tokens": 2100, "completion_tokens": 3300}
        with_cost, _ = self.drive(usage=usage, process=self.PROCESS, tries=4)
        without, _ = self.drive(usage=usage)
        columns = "prompt_tokens, completion_tokens, attempts"
        rows = [dict(self.conn.execute(
            f"SELECT {columns} FROM entries WHERE job_id = ?", (job,)).fetchone())
            for job in (with_cost, without)]
        self.assertEqual(rows[0], rows[1])
        attempts = [db.list_attempts(self.conn, job)[0] for job in (with_cost, without)]
        self.assertEqual(attempts[0].prompt_tokens, attempts[1].prompt_tokens)
        self.assertEqual(attempts[0].completion_tokens, attempts[1].completion_tokens)
        # wall_s is the round trip the node timed, not the session the agent
        # reported: 2,520 s of process time never reaches it.
        for attempt in attempts:
            self.assertLess(attempt.wall_s, 600)
        self.assertEqual(self.entry_of(with_cost)["wall_s"], attempts[0].wall_s)

    # -- --since, and the budget line -----------------------------------------

    def test_start_records_since_and_note_and_next_echoes_the_elapsed(self):
        since = (datetime.now(timezone.utc) - timedelta(minutes=42)).strftime(db.UTC_FORMAT)
        result = self.start(since=since, note="skill=algorithmic-art")
        job = db.get_job(self.conn, result["job"])
        self.assertEqual((job.since_utc, job.note), (since, "skill=algorithmic-art"))
        self.assertEqual(result["budget_say"],
                         "budget: held within 10 min of --since, under 30,000 tokens "
                         "generated, 8 tries · advisory")
        sleep, clock = self.fake_time()
        now = paid.next_for(self.conn, job.id, self.MODEL, timeout=0, sleep=sleep,
                            clock=clock, ctx=self.ctx)
        self.assertEqual(now["elapsed"]["from"], "--since")
        self.assertEqual(now["elapsed"]["minutes"], 42.0)
        self.assertIn("42 min", now["elapsed"]["say"])
        self.assertIn("over budget by 32 min", now["elapsed"]["over_budget"])

    def test_without_since_the_elapsed_counts_from_the_lease_and_says_so(self):
        job = self.start()["job"]
        sleep, clock = self.fake_time()
        now = paid.next_for(self.conn, job, self.MODEL, timeout=0, sleep=sleep,
                            clock=clock, ctx=self.ctx)
        self.assertEqual(now["elapsed"]["from"], "the lease")
        self.assertIsNone(now["elapsed"]["over_budget"])
        self.assertIn("from the lease", now["elapsed"]["say"])

    def test_an_over_budget_import_says_so_and_records_anyway(self):
        since = (datetime.now(timezone.utc) - timedelta(minutes=42)).strftime(db.UTC_FORMAT)
        job = self.start(since=since)["job"]
        self.worker().run_once()
        packet = paid.next_for(self.conn, job, self.MODEL, ctx=self.ctx)
        packet["items"][0]["answer"] = CLEAN_PLAN
        report = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(len(report.recorded), 1)
        self.assertEqual(db.get_job(self.conn, job).state, "queued")
        self.assertIn("over budget by 32 min", report.elapsed["over_budget"])
        self.assertIn("finish, and report it", report.elapsed["over_budget"])
        self.assertEqual(report.as_dict()["elapsed"]["from"], "--since")

    def test_the_tokens_an_agent_reports_are_measured_against_the_budget(self):
        job, report = self.drive(process={"output_tokens": 207537})
        self.assertIn("177,537 tokens generated", report.elapsed["over_budget"])
        self.assertEqual(paid.tokens_generated(self.conn, job), 207537)

    def test_a_since_that_cannot_be_true_is_refused_and_nothing_is_queued(self):
        queued = len(db.list_jobs(self.conn, "queued"))
        ahead = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(db.UTC_FORMAT)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(db.UTC_FORMAT)
        for bad, word in ((ahead, "future"), (old, "last session"), ("yesterday", "UTC")):
            with self.subTest(since=bad):
                with self.assertRaises(paid.PaidRefused) as caught:
                    self.start(since=bad)
                self.assertIn(word, str(caught.exception))
        with self.assertRaises(paid.PaidRefused):
            self.start(note="x" * (paid.NOTE_MAX + 1))
        self.assertEqual(queued, len(db.list_jobs(self.conn, "queued")))

    def test_since_takes_what_date_u_prints_and_an_offset_too(self):
        now = "2026-09-21T14:03:22Z"
        self.assertEqual(paid.parse_since("2026-09-21T14:00:00Z", now=now),
                         "2026-09-21T14:00:00Z")
        self.assertEqual(paid.parse_since("2026-09-21T10:00:00-04:00", now=now),
                         "2026-09-21T14:00:00Z")
        self.assertEqual(paid.parse_since("2026-09-21 13:55:00", now=now),
                         "2026-09-21T13:55:00Z")
        self.assertIsNone(paid.parse_since(None))

    # -- the budget verb, and the freshness pair --------------------------------

    def test_the_budget_verb_writes_the_rows_start_reads(self):
        self.assertEqual(paid.budget(self.conn),
                         {"minutes": 10.0, "tokens": 30000, "tries": 8})
        written = paid.set_budget(self.conn, tokens=20000, tries=2)
        self.assertEqual(written, {"minutes": 10.0, "tokens": 20000, "tries": 2})
        self.assertEqual(paid.try_cap(self.conn), 2)
        self.assertIn("under 20,000 tokens generated", self.start()["budget_say"])
        with self.assertRaises(paid.PaidRefused):
            paid.set_budget(self.conn, minutes=0)

    def test_the_budget_verb_prints_and_writes_from_the_command_line(self):
        printed = self.cli("paid", "budget")
        self.assertEqual(printed.returncode, 0, printed.stderr)
        self.assertIn("under 30,000 tokens generated", printed.stdout)
        written = self.cli("paid", "budget", "--tokens", "20000", "--json")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(json.loads(written.stdout)["tokens"], 20000)
        self.assertIn("under 20,000 tokens generated", self.cli("paid", "budget").stdout)

    def test_the_preflight_says_which_checkout_the_node_is_on(self):
        report = paid.preflight(self.conn, self.MODEL, **self.one_worker)
        self.assertIn("node_commit", report["info"])
        self.assertEqual(report["info"]["agents_md_sha256"],
                         paid.sha256_file(REPO_ROOT / "AGENTS.md"))
        self.assertEqual(report["info"]["budget"], paid.budget(self.conn))
        started = self.start()
        self.assertEqual(started["node_commit"], report["info"]["node_commit"])
        self.assertEqual(started["agents_md_sha256"],
                         report["info"]["agents_md_sha256"])

    def test_a_node_with_no_git_reports_a_null_commit_and_no_failure(self):
        db.set_paid_models(self.conn, [self.MODEL])
        with mock.patch.dict(os.environ, {"PATH": ""}, clear=False):
            self.assertIsNone(paid.node_commit())
            report = paid.preflight(self.conn, self.MODEL, **self.one_worker)
        self.assertTrue(report["ready"], report["checks"])
        self.assertIsNone(report["info"]["node_commit"])
