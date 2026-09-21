"""Unit tests for sketchgen.paid — any step, answered off the node.

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser. Every "paid" answer below is a string typed
into a packet, which is exactly what the agent on the laptop hands back; the
node's half is the whole of what is tested. The test that earns each adapter is
its round trip: a packet cut for the step, answered with the reply the local
path would have got, lands the same rows the local path lands (plan §5.1).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
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
PAID_ENV = {models.PAID_MODELS_ENV: "claude-opus-5, claude-sonnet-5"}


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

    def test_the_planner_menu_offers_each_named_model_off_this_node(self):
        with mock.patch.dict(os.environ, PAID_ENV), \
                mock.patch.object(web.models, "catalogue", return_value=[]):
            groups = dict(web.planner_groups())
            offered = [value for value, _ in groups["off this node"]]
            self.assertEqual(offered, ["paid", "claude-opus-5", "claude-sonnet-5"])
            self.assertEqual(web.check_planner("claude-opus-5"), "claude-opus-5")
            self.assertEqual(web.planner_column("claude-opus-5"), "claude-opus-5")
            self.assertEqual(web.planner_column("paid"), "paid")


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

    def test_the_same_answer_cannot_land_twice(self):
        self.park()
        packet, _ = self.answer()
        again = paid.import_packet(self.conn, packet, ctx=self.ctx)
        self.assertEqual(again.recorded, [])
        self.assertIn("not waiting", again.rejected[0]["reason"])

    def test_the_executor_menu_offers_paid_models(self):
        with mock.patch.dict(os.environ, PAID_ENV), \
                mock.patch.object(web.models, "catalogue", return_value=[]):
            offered = [v for v, _ in dict(web.executor_groups())["off this node"]]
            self.assertIn("claude-opus-5", offered)
            self.assertEqual(web.check_executor("claude-opus-5"), "claude-opus-5")


class Migration014Tests(unittest.TestCase):
    """The jobs rebuild keeps every row, column and reference it found."""

    def test_a_database_at_013_keeps_its_jobs_and_learns_execute(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-014-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        before = tmp / "migrations"
        before.mkdir()
        for sql in sorted(db.MIGRATIONS_DIR.glob("*.sql")):
            if not sql.name.startswith("014"):
                shutil.copy(sql, before / sql.name)
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

        self.assertEqual(db.migrate(conn), [14])
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

    def test_assign_all_at_the_cli_and_export_without_as(self):
        result = self.run_cli("assign", "--all", "claude-opus-5", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["assignment"].values()), {"claude-opus-5"})
        self.assertTrue(any("second variable" in n for n in payload["notes"]))
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
# The CLI
# ---------------------------------------------------------------------------


class CliTests(PaidTestCase):

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("export", "import", "status", "release", "assign"):
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
