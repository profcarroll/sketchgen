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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sketchgen import db  # noqa: E402
from sketchgen import judge  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen import paid  # noqa: E402
from sketchgen import worker  # noqa: E402

import test_judge  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"

GOOD_JUDGE_REPLY = test_judge.GOOD_REPLY
PNG_BYTES = test_judge.PNG_BYTES

GOOD_CRITIQUE = "Let one of the drifting dots fall out of step with the others.\n"
TWO_SENTENCES = "It is fine. Make the dots red.\n"


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
        self.assertEqual(waiting["critique"]["count"], 3)

    def test_release_needs_ids_or_all(self):
        self.assertEqual(self.run_cli("release", "--step", "critique").returncode, 3)
        result = self.run_cli("release", "--step", "critique", "--all", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
