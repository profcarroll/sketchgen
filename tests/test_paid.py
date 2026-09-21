"""Unit tests for sketchgen.paid — any step, answered off the node.

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser. Every "paid" answer below is a string typed
into a packet, which is exactly what the agent on the laptop hands back; the
node's half is the whole of what is tested. The test that earns each adapter is
its round trip: a packet cut for the step, answered with the reply the local
path would have got, lands the same rows the local path lands (plan §5.1).
"""

import json
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
from sketchgen import paid  # noqa: E402

import test_judge  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"

GOOD_JUDGE_REPLY = test_judge.GOOD_REPLY
PNG_BYTES = test_judge.PNG_BYTES


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
# The CLI
# ---------------------------------------------------------------------------


class CliTests(PaidTestCase):

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("export", "import", "status"):
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
        self.assertIn("judge", json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
