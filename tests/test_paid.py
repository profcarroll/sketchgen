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
# The CLI
# ---------------------------------------------------------------------------


class CliTests(PaidTestCase):

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("export", "import", "status", "release"):
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
