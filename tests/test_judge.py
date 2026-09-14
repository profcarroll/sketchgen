"""Unit tests for sketchgen.judge — the blinded agent judges (packet 5.2).

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser, no push. Every reply below is a stub file on
disk, every rng is seeded, and the fence is an injected probe, so a failure here
is a change in the code and never a change in the weather or in the node.

The test that earns the packet is
:meth:`BlindTests.test_a_poisoned_brief_is_refused`: the blind is the whole of
spec §5's first design consequence, and a blind that is only a comment in a
template is not a blind. The one beside it — that the two images go A first —
is the other half: the two populations have to be looking at the same two
things in the same order, or the divergence between them measures the page
layout instead of the judgment.
"""

import json
import random
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
from sketchgen import pairs  # noqa: E402

import test_gallery  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"

PNG_BYTES = test_gallery.PNG_BYTES

#: The three published entries the suite judges. Everything in `secret` is a
#: field the agent judge must never see; the briefs are the only part of a row
#: that is allowed into the prompt.
FIXTURES = [
    {
        "brief": "A wide field of slow blue dots that drift left and wrap around.",
        "executor": "qwen3-coder:30b-a3b-q4_K_M",
        "statement": "I drew the dots on a dark ground and let them wrap.",
        "rules_file": "control",
        "submitted_by": "profcarroll",
    },
    {
        "brief": "Three concentric rings that breathe in and out, warm on cool.",
        "executor": "qwen3.5:4b",
        "statement": "The rings use a sine on the radius so they breathe.",
        "rules_file": "treatment",
        "submitted_by": "octocat",
    },
    {
        "brief": "A single white line that redraws itself as a slow spiral.",
        "executor": "gemma4:26b",
        "statement": "One line, one spiral, nothing else on the canvas.",
        "rules_file": "control",
        "submitted_by": "mona",
    },
]

GOOD_REPLY = """brief: A
look: tie

Reasons
brief: A has the drift and the wrap the brief asks for, B stops short.
look: Neither one holds the eye longer than the other.
"""

MALFORMED_REPLY = "I liked the first one more, honestly. Hard to say about the other.\n"


def build_entries(tmp: Path):
    """Three published entries with real strips, plus a job row each."""
    database = tmp / "sketchgen.db"
    db.init(database)
    conn = db.connect(database)
    ids = []
    for n, fixture in enumerate(FIXTURES):
        job_id = db.enqueue(
            conn,
            f"prompt number {n}",
            fixture["submitted_by"],
            brief=fixture["brief"],
            rules_file=fixture["rules_file"],
        )
        strip = tmp / "art" / str(n) / "strip.png"
        strip.parent.mkdir(parents=True, exist_ok=True)
        strip.write_bytes(PNG_BYTES + bytes([n, n, n]))
        ids.append(
            db.create_entry(
                conn,
                job_id,
                state="published",
                prompt=f"prompt number {n}",
                brief=fixture["brief"],
                statement=fixture["statement"],
                executor=fixture["executor"],
                planner="gemma4:e4b",
                rules_file=fixture["rules_file"],
                seed=1,
                submitted_by=fixture["submitted_by"],
                strip_path=str(strip),
            )
        )
    return conn, ids


class JudgeTestCase(unittest.TestCase):
    """One temp directory, one temp database, three published entries."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-judge-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = build_entries(self.tmp)
        self.addCleanup(self.conn.close)
        # Two humans, so the agent is joining a conversation rather than
        # starting one, and so `agreement` has something to compare against.
        for judge_id, choice in (("profcarroll", "A"), ("octocat", "B")):
            for question in pairs.QUESTIONS:
                pairs.record(
                    self.conn,
                    entry_a=self.ids[0],
                    entry_b=self.ids[1],
                    judge_kind="human",
                    judge_id=judge_id,
                    question=question,
                    choice=choice,
                )

    @property
    def dbpath(self):
        return str(self.tmp / "sketchgen.db")

    def stub(self, text, name="reply.txt"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def free_slot(self):
        """A probe that sees nothing: the slot is free, no process started."""
        return lambda: {"processes": [], "models": [], "ollama_error": None}


# ---------------------------------------------------------------------------
# (a) the blind — the reason the packet exists
# ---------------------------------------------------------------------------


class BlindTests(JudgeTestCase):

    def test_the_prompt_carries_both_briefs_and_nothing_else_from_the_row(self):
        request = judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIn(FIXTURES[0]["brief"], request.text)
        self.assertIn(FIXTURES[1]["brief"], request.text)
        for fixture in FIXTURES:
            self.assertNotIn(fixture["executor"], request.text)
            self.assertNotIn(fixture["statement"], request.text)
            self.assertNotIn(fixture["submitted_by"], request.text)
            self.assertNotIn(fixture["rules_file"], request.text)
        for word in ("score", "likes", "views", "planner", "username"):
            self.assertNotIn(word, request.text.lower(), f"{word!r} reached the judge")
        # And the enforcement itself passes on the real rendered prompt.
        judge.assert_blind(request.text, conn=self.conn)

    def test_the_rule_comment_never_reaches_the_model(self):
        """The template's rule block names the forbidden fields, so it is stripped."""
        raw = judge.PROMPT_PATH.read_text(encoding="utf-8")
        self.assertIn("submitted_by", raw)  # the rule is written down…
        with self.assertRaises(judge.BlindError):
            judge.assert_blind(raw)  # …and the raw file would fail its own check
        request = judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertNotIn("<!--", request.text)
        self.assertNotIn("prompt_version:", request.text)

    def test_a_poisoned_brief_is_refused(self):
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("Slow blue dots, and it already has 40 likes from the gallery.",
             self.ids[0]),
        )
        with self.assertRaises(judge.BlindError) as caught:
            judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIn("likes", str(caught.exception))

    def test_an_entry_id_in_the_prompt_is_refused(self):
        """Three-digit runs are checked against the entry ids in this database."""
        self.conn.execute("UPDATE entries SET id = 314 WHERE id = ?", (self.ids[2],))
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("A field of dots, the way entry 314 did it.", self.ids[0]),
        )
        with self.assertRaises(judge.BlindError) as caught:
            judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIn("314", str(caught.exception))

    def test_a_submitter_in_the_prompt_is_refused(self):
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("Concentric rings, in the manner octocat asked for.", self.ids[1]),
        )
        with self.assertRaises(judge.BlindError) as caught:
            judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIn("submitter", str(caught.exception))

    def test_a_number_that_is_not_an_entry_id_is_fine(self):
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("Eight hundred by six hundred: 800 by 600, slow drift.", self.ids[0]),
        )
        request = judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIn("800 by 600", request.text)


# ---------------------------------------------------------------------------
# (b) what is actually sent
# ---------------------------------------------------------------------------


class PayloadTests(JudgeTestCase):

    def test_the_images_are_the_strips_and_A_is_first(self):
        import base64

        verdict = judge.judge_local(
            self.conn,
            self.ids[1],
            self.ids[0],
            model="gemma4:e4b",
            stub=self.stub(GOOD_REPLY),
        )
        message = verdict.payload["messages"][0]
        self.assertEqual(len(message["images"]), 2)
        strips = [
            (self.tmp / "art" / str(n) / "strip.png").read_bytes() for n in range(3)
        ]
        self.assertEqual(base64.b64decode(message["images"][0]), strips[1])
        self.assertEqual(base64.b64decode(message["images"][1]), strips[0])
        self.assertFalse(verdict.payload["stream"])
        # planner.py's rule: `think` goes only to the tags that support it, and
        # gemma4:e4b — this judge's model — is not one of them.
        self.assertNotIn("think", verdict.payload)
        self.assertEqual(verdict.payload["model"], "gemma4:e4b")

    def test_thinking_is_turned_off_only_for_the_tags_that_have_it(self):
        request = judge.build_prompt(self.conn, self.ids[0], self.ids[1])
        self.assertIs(judge.chat_payload(request, "qwen3:8b")["think"], False)
        self.assertNotIn("think", judge.chat_payload(request, "gemma4:e4b"))

    def test_a_missing_strip_is_a_refusal_because_the_judge_must_see(self):
        self.conn.execute(
            "UPDATE entries SET strip_path = NULL WHERE id = ?", (self.ids[1],)
        )
        with self.assertRaises(judge.JudgeRefused):
            judge.build_prompt(self.conn, self.ids[0], self.ids[1])


# ---------------------------------------------------------------------------
# (c) a verdict, recorded
# ---------------------------------------------------------------------------


class LocalVerdictTests(JudgeTestCase):

    def test_a_stubbed_verdict_is_recorded_for_both_questions(self):
        expected_hash = pairs.artefact_hash(self.ids[0], self.ids[2], conn=self.conn)
        verdict = judge.judge_local(
            self.conn,
            self.ids[0],
            self.ids[2],
            model="gemma4:e4b",
            stub=self.stub(GOOD_REPLY),
        )
        self.assertEqual(verdict.brief, "A")
        self.assertEqual(verdict.look, "tie")
        self.assertEqual(verdict.prompt_version, "judge-v1")
        self.assertEqual(verdict.artefact_hash, expected_hash)
        self.assertIn("wrap", verdict.reasons["brief"])

        rows = list(
            self.conn.execute(
                "SELECT question, choice, judge_kind, judge_id, prompt_version, "
                "artefact_hash FROM judgments WHERE judge_kind = 'agent' "
                "ORDER BY question"
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["question"] for r in rows], ["brief", "look"])
        self.assertEqual([r["choice"] for r in rows], ["A", "tie"])
        for row in rows:
            self.assertEqual(row["judge_id"], "gemma4:e4b")
            self.assertEqual(row["prompt_version"], "judge-v1")
            self.assertEqual(row["artefact_hash"], expected_hash)

    def test_a_malformed_reply_fails_with_the_raw_text_kept(self):
        with self.assertRaises(judge.JudgeFailed) as caught:
            judge.judge_local(
                self.conn,
                self.ids[0],
                self.ids[1],
                model="gemma4:e4b",
                stub=self.stub(MALFORMED_REPLY, "bad.txt"),
            )
        self.assertEqual(caught.exception.raw, MALFORMED_REPLY)
        self.assertEqual(
            list(self.conn.execute(
                "SELECT id FROM judgments WHERE judge_kind = 'agent'"
            )),
            [],
        )

    def test_a_malformed_reply_takes_the_exit_1_path_at_the_cli(self):
        bad = self.stub(MALFORMED_REPLY, "bad.txt")
        result = subprocess.run(
            [sys.executable, str(CLI), "judge", "one", "--a", str(self.ids[0]),
             "--b", str(self.ids[1]), "--model", "gemma4:e4b", "--stub", str(bad),
             "--db", self.dbpath],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("the model said", result.stderr)
        self.assertIn("honestly", result.stderr)

    def test_a_decorated_reply_still_parses(self):
        verdict = judge.judge_local(
            self.conn,
            self.ids[0],
            self.ids[1],
            model="gemma4:e4b",
            stub=self.stub("**brief:** B\n- look — tie\n", "decorated.txt"),
        )
        self.assertEqual((verdict.brief, verdict.look), ("B", "tie"))


# ---------------------------------------------------------------------------
# (d) the fence
# ---------------------------------------------------------------------------


class FenceTests(JudgeTestCase):

    def test_run_local_refuses_while_another_client_holds_the_slot(self):
        def busy():
            return {"processes": ["4242 opencode serve"], "models": [],
                    "ollama_error": None}

        with self.assertRaises(judge.JudgeRefused) as caught:
            judge.run_local(
                self.conn,
                model="gemma4:e4b",
                host="http://127.0.0.1:11434",
                limit=1,
                rng=random.Random(0),
                stub=self.stub(GOOD_REPLY),
                probe=busy,
            )
        self.assertIn("opencode", str(caught.exception))
        self.assertEqual(
            list(self.conn.execute(
                "SELECT id FROM judgments WHERE judge_kind = 'agent'"
            )),
            [],
        )

    def test_run_local_judges_its_limit_when_the_slot_is_free(self):
        counts = judge.run_local(
            self.conn,
            model="gemma4:e4b",
            host="http://127.0.0.1:11434",
            limit=2,
            rng=random.Random(7),
            stub=self.stub(GOOD_REPLY),
            probe=self.free_slot(),
        )
        self.assertEqual(counts["judged"], 2)
        self.assertEqual(counts["recorded"], 4)
        seen = {
            (row["entry_a"], row["entry_b"])
            for row in self.conn.execute(
                "SELECT entry_a, entry_b FROM judgments WHERE judge_kind = 'agent'"
            )
        }
        self.assertEqual(len(seen), 2, "the same pair was judged twice")

    def test_run_local_stops_when_there_is_no_pair_left(self):
        counts = judge.run_local(
            self.conn,
            model="gemma4:e4b",
            host="http://127.0.0.1:11434",
            limit=99,
            rng=random.Random(3),
            stub=self.stub(GOOD_REPLY),
            probe=self.free_slot(),
        )
        # Three published entries make three pairs, and no more.
        self.assertEqual(counts["judged"], 3)


# ---------------------------------------------------------------------------
# (e) the paid judge, branch B
# ---------------------------------------------------------------------------


class ClaimTests(JudgeTestCase):

    def test_export_writes_hashes_and_no_blind_fields(self):
        packet = judge.claims_packet(self.conn, judge_id="claude-laptop", limit=10)
        self.assertEqual(len(packet["claims"]), 3)
        text = json.dumps(packet)
        judge.assert_blind(text)
        for fixture in FIXTURES:
            self.assertNotIn(fixture["executor"], text)
            self.assertNotIn(fixture["statement"], text)
            self.assertNotIn(fixture["submitted_by"], text)
        for claim in packet["claims"]:
            self.assertEqual(
                claim["artefact_hash"],
                pairs.artefact_hash(claim["entry_a"], claim["entry_b"],
                                    conn=self.conn),
            )
            self.assertEqual(claim["prompt_version"], "judge-v1")
            self.assertEqual(claim["answers"], {"brief": "", "look": ""})
            # The strips stay where they are; the packet names them.
            self.assertTrue(Path(claim["strip_a"]).is_file())
            self.assertTrue(Path(claim["strip_b"]).is_file())

    def test_export_skips_pairs_that_judge_has_already_answered(self):
        judge.judge_local(
            self.conn, self.ids[0], self.ids[1],
            model="claude-laptop", stub=self.stub(GOOD_REPLY),
        )
        claims = judge.export_claims(self.conn, judge_id="claude-laptop", limit=10)
        answered = {(self.ids[0], self.ids[1]), (self.ids[1], self.ids[0])}
        self.assertEqual(len(claims), 2)
        for claim in claims:
            self.assertNotIn((claim["entry_a"], claim["entry_b"]), answered)

    def test_import_records_a_filled_in_packet(self):
        packet = judge.claims_packet(self.conn, judge_id="claude-laptop", limit=1)
        packet["claims"][0]["answers"] = {"brief": "B", "look": "tie"}
        packet["judge_id"] = "claude-opus-5"  # the model id the laptop reports
        self.assertEqual(judge.import_verdicts(self.conn, packet), 1)
        rows = list(
            self.conn.execute(
                "SELECT judge_id, question, choice, prompt_version, artefact_hash "
                "FROM judgments WHERE judge_kind = 'agent' ORDER BY question"
            )
        )
        self.assertEqual([r["choice"] for r in rows], ["B", "tie"])
        self.assertEqual({r["judge_id"] for r in rows}, {"claude-opus-5"})
        self.assertEqual({r["prompt_version"] for r in rows}, {"judge-v1"})
        self.assertEqual(
            rows[0]["artefact_hash"],
            packet["claims"][0]["artefact_hash"],
        )

    def test_import_rejects_a_stale_hash(self):
        packet = judge.claims_packet(self.conn, judge_id="claude-laptop", limit=1)
        claim = packet["claims"][0]
        claim["answers"] = {"brief": "A", "look": "A"}
        # The sketch was run again and its strip regenerated: the judge answered
        # about an artefact that is no longer there.
        Path(claim["strip_a"]).write_bytes(PNG_BYTES + b"\x99\x99\x99")
        recorded, rejected = judge.import_verdicts_detailed(self.conn, packet)
        self.assertEqual(recorded, 0)
        self.assertEqual(len(rejected), 1)
        self.assertIn("artefact_hash", rejected[0]["reason"])
        self.assertEqual(
            list(self.conn.execute(
                "SELECT id FROM judgments WHERE judge_kind = 'agent'"
            )),
            [],
        )

    def test_import_skips_an_unanswered_claim_without_complaining(self):
        packet = judge.claims_packet(self.conn, judge_id="claude-laptop", limit=2)
        recorded, rejected = judge.import_verdicts_detailed(self.conn, packet)
        self.assertEqual((recorded, rejected), (0, []))

    def test_import_refuses_a_packet_that_is_not_one(self):
        with self.assertRaises(judge.JudgeRefused):
            judge.import_verdicts(self.conn, {"packet": "something-else"})


# ---------------------------------------------------------------------------
# (f) the number the packet exists to produce
# ---------------------------------------------------------------------------


class AgreementTests(JudgeTestCase):

    def test_agreement_is_half_after_one_agreeing_and_one_disagreeing_pair(self):
        # setUp already gave pair (0, 1) two human votes, A and B — a dead heat,
        # which `_majority` reports as a tie. Start from a clean slate instead so
        # the two pairs below are the whole of the arithmetic.
        self.conn.execute("DELETE FROM judgments")
        agreeing = (self.ids[0], self.ids[1])
        disagreeing = (self.ids[0], self.ids[2])
        for question in pairs.QUESTIONS:
            for entry_a, entry_b, human, agent in (
                (*agreeing, "A", "A"),
                (*disagreeing, "A", "B"),
            ):
                pairs.record(
                    self.conn, entry_a=entry_a, entry_b=entry_b,
                    judge_kind="human", judge_id="profcarroll",
                    question=question, choice=human,
                )
                pairs.record(
                    self.conn, entry_a=entry_a, entry_b=entry_b,
                    judge_kind="agent", judge_id="gemma4:e4b",
                    question=question, choice=agent,
                )
        result = pairs.agreement(self.conn)
        self.assertEqual(result["brief"]["agreement"], 0.5)
        self.assertEqual(result["look"]["agreement"], 0.5)
        self.assertEqual(result["overall"]["agreement"], 0.5)
        self.assertEqual(result["overall"]["n"], 4)


# ---------------------------------------------------------------------------
# (g) the CLI's contract
# ---------------------------------------------------------------------------


class CliTests(JudgeTestCase):

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "judge", *args],
            capture_output=True, text=True, check=False,
        )

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("run", "one", "export", "import", "status"):
            with self.subTest(subcommand=name):
                result = self.run_cli(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--db", result.stdout)

    def test_status_on_an_unjudged_database(self):
        result = self.run_cli("status", "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["verdicts"], [])
        self.assertIsNone(payload["agreement"]["overall"]["agreement"])

    def test_status_counts_verdicts_per_model(self):
        judge.judge_local(
            self.conn, self.ids[0], self.ids[1],
            model="gemma4:e4b", stub=self.stub(GOOD_REPLY),
        )
        self.conn.commit()
        result = self.run_cli("status", "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        verdicts = json.loads(result.stdout)["verdicts"]
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["judge_id"], "gemma4:e4b")
        self.assertEqual(verdicts[0]["total"], 2)
        self.assertEqual(verdicts[0]["prompt_version"], "judge-v1")

    def test_one_refuses_an_unpublished_entry(self):
        self.conn.execute(
            "UPDATE entries SET state = 'held' WHERE id = ?", (self.ids[1],)
        )
        self.conn.commit()
        result = self.run_cli(
            "one", "--a", str(self.ids[0]), "--b", str(self.ids[1]),
            "--model", "gemma4:e4b", "--stub", str(self.stub(GOOD_REPLY)),
            "--db", self.dbpath,
        )
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("not published", result.stderr)

    def test_export_and_import_round_trip_through_files(self):
        out = self.tmp / "claims.json"
        result = self.run_cli(
            "export", "--as", "claude-laptop", "--out", str(out),
            "--db", self.dbpath,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        packet = json.loads(out.read_text(encoding="utf-8"))
        for claim in packet["claims"]:
            claim["answers"] = {"brief": "A", "look": "B"}
        out.write_text(json.dumps(packet), encoding="utf-8")

        result = self.run_cli("import", str(out), "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["recorded"], 3)

    def test_export_says_so_and_exits_zero_when_there_is_nothing_to_claim(self):
        """One published entry is no pair at all, and that is not a failure."""
        self.conn.execute(
            "UPDATE entries SET state = 'held' WHERE id != ?", (self.ids[0],)
        )
        self.conn.commit()
        out = self.tmp / "none.json"
        result = self.run_cli(
            "export", "--as", "claude-laptop", "--out", str(out), "--db", self.dbpath
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no pairs", result.stdout)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
