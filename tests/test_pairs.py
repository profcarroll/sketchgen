"""Unit tests for sketchgen.pairs — paired comparison and Bradley–Terry (5.1).

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser, no push, and no clock beyond ``db.utc_now``.
Every judgment below is synthetic and every rng is seeded, so a failure here is
a change in the code and never a change in the weather.

The test that earns the packet is :meth:`ScoreTests.test_the_fit_recovers_a_known_ordering`:
six entries are given true strengths, two hundred judgments are drawn from the
Bradley–Terry model itself, and the fit has to put them back in order. If that
holds, the number the gallery prints beside an entry means something; if it does
not, nothing else in this file matters.
"""

import json
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sketchgen import db  # noqa: E402
from sketchgen import gallery  # noqa: E402
from sketchgen import pairs  # noqa: E402

import test_gallery  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"

#: One valid 1x1 PNG, borrowed from the gallery tests so that both suites are
#: hashing and copying the same bytes.
PNG_BYTES = test_gallery.PNG_BYTES


# ---------------------------------------------------------------------------
# A small fixture: N published entries, alternating rules files
# ---------------------------------------------------------------------------


def build_entries(tmp: Path, count: int, *, rules=("control", "treatment")):
    """``count`` published entries with real strip.png files. Returns (conn, ids)."""
    database = tmp / "sketchgen.db"
    db.init(database)
    conn = db.connect(database)
    ids = []
    for n in range(count):
        rules_file = rules[n % len(rules)]
        job_id = db.enqueue(
            conn,
            f"prompt number {n}",
            "profcarroll",
            brief=f"brief number {n}",
            rules_file=rules_file,
        )
        strip = tmp / "art" / str(n) / "strip.png"
        strip.parent.mkdir(parents=True, exist_ok=True)
        strip.write_bytes(PNG_BYTES + bytes([n]))
        ids.append(
            db.create_entry(
                conn,
                job_id,
                state="published",
                prompt=f"prompt number {n}",
                brief=f"brief number {n}",
                rules_file=rules_file,
                seed=1,
                submitted_by="profcarroll",
                strip_path=str(strip),
            )
        )
    return conn, ids


class PairsTestCase(unittest.TestCase):
    """One temp directory and one temp database per test."""

    entries = 4

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-pairs-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = build_entries(self.tmp, self.entries)
        self.addCleanup(self.conn.close)

    def vote(self, a, b, choice, *, judge, kind="human", question="look"):
        return pairs.record(
            self.conn,
            entry_a=a,
            entry_b=b,
            judge_kind=kind,
            judge_id=judge,
            question=question,
            choice=choice,
            prompt_version="test-v1",
        )


# ---------------------------------------------------------------------------
# (a) the fit
# ---------------------------------------------------------------------------


class ScoreTests(PairsTestCase):

    entries = 6

    def test_the_fit_recovers_a_known_ordering(self):
        true = dict(zip(self.ids, (16.0, 8.0, 4.0, 2.0, 1.0, 0.5)))
        rng = random.Random(20260914)
        for n in range(200):
            a, b = rng.sample(self.ids, 2)
            winner_is_a = rng.random() < true[a] / (true[a] + true[b])
            self.vote(a, b, "A" if winner_is_a else "B", judge=f"voter{n:03d}")

        table = pairs.scores(self.conn, population="human", question="look")
        self.assertEqual(set(table), set(self.ids))
        recovered = sorted(table, key=lambda entry: -float(table[entry]["score"]))
        self.assertEqual(recovered, self.ids, f"fitted: {table}")

        top = float(table[self.ids[0]]["score"])
        bottom = float(table[self.ids[-1]]["score"])
        # The prior shrinks every score toward the reference at 1.00, so the
        # recovered margin is smaller than the true 32x. It must still be a
        # margin and not a rounding artefact.
        self.assertGreater(top - bottom, 1.0, f"fitted: {table}")
        self.assertGreater(top / bottom, 4.0, f"fitted: {table}")

    def test_every_judgment_is_counted_once_per_entry(self):
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertEqual(table, {})
        self.vote(self.ids[0], self.ids[1], "A", judge="alice")
        self.vote(self.ids[0], self.ids[1], "A", judge="bob")
        table = pairs.scores(self.conn, population="human", question="look")
        for entry in (self.ids[0], self.ids[1]):
            self.assertEqual(table[entry]["n"], 2)
        self.assertEqual(table[self.ids[0]]["wins"], 2)
        self.assertEqual(table[self.ids[1]]["losses"], 2)

    def test_orientation_is_a_fact_about_the_page_not_the_score(self):
        """The same verdict recorded both ways round is the same verdict."""
        one, two = self.ids[0], self.ids[1]
        self.vote(one, two, "A", judge="alice")     # one beats two
        self.vote(two, one, "B", judge="bob")       # one beats two, shown flipped
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertEqual(table[one]["wins"], 2)
        self.assertEqual(table[two]["losses"], 2)
        self.assertGreater(table[one]["score"], table[two]["score"])

    # (b) ties
    def test_a_tie_is_half_a_win_each_way(self):
        one, two, three = self.ids[0], self.ids[1], self.ids[2]
        self.vote(one, two, "tie", judge="alice")
        table = pairs.scores(self.conn, population="human", question="look")
        for entry in (one, two):
            self.assertEqual(table[entry]["ties"], 1)
            self.assertEqual(table[entry]["wins"], 0)
            self.assertEqual(table[entry]["losses"], 0)
            self.assertEqual(table[entry]["n"], 1)
        self.assertAlmostEqual(table[one]["score"], table[two]["score"], places=9)
        self.assertNotIn(three, table)

    def test_an_all_tie_pool_scores_level_and_finite(self):
        for index, judge in enumerate(("alice", "bob", "carol")):
            self.vote(self.ids[0], self.ids[1], "tie", judge=judge)
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertAlmostEqual(table[self.ids[0]]["score"], table[self.ids[1]]["score"])
        for row in table.values():
            self.assertTrue(0.0 < float(row["score"]) < 1e6)

    def test_an_undefeated_entry_stays_finite(self):
        """Without the prior this is where Bradley–Terry runs off to infinity."""
        for n, other in enumerate(self.ids[1:]):
            self.vote(self.ids[0], other, "A", judge=f"voter{n:03d}")
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertTrue(0.0 < float(table[self.ids[0]]["score"]) < 1e6)
        self.assertGreater(table[self.ids[0]]["score"], pairs.REFERENCE_STRENGTH)

    # (c) absence
    def test_an_entry_with_no_judgments_is_absent_not_zero(self):
        self.vote(self.ids[0], self.ids[1], "A", judge="alice")
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertEqual(set(table), {self.ids[0], self.ids[1]})
        for missing in self.ids[2:]:
            self.assertNotIn(missing, table)

    def test_the_two_populations_are_kept_apart(self):
        self.vote(self.ids[0], self.ids[1], "A", judge="alice")
        self.vote(self.ids[0], self.ids[1], "B", judge="gemma4:e4b", kind="agent")
        humans = pairs.scores(self.conn, population="human", question="look")
        agents = pairs.scores(self.conn, population="agent", question="look")
        self.assertGreater(humans[self.ids[0]]["score"], humans[self.ids[1]]["score"])
        self.assertLess(agents[self.ids[0]]["score"], agents[self.ids[1]]["score"])

    def test_the_two_questions_are_kept_apart(self):
        self.vote(self.ids[0], self.ids[1], "A", judge="alice", question="look")
        look = pairs.scores(self.conn, population="human", question="look")
        brief = pairs.scores(self.conn, population="human", question="brief")
        self.assertEqual(set(look), {self.ids[0], self.ids[1]})
        self.assertEqual(brief, {})

    def test_an_unknown_population_or_question_is_a_valueerror(self):
        with self.assertRaises(ValueError):
            pairs.scores(self.conn, population="everyone", question="look")
        with self.assertRaises(ValueError):
            pairs.scores(self.conn, population="human", question="vibes")


# ---------------------------------------------------------------------------
# (d) picking
# ---------------------------------------------------------------------------


class PickPairTests(PairsTestCase):

    entries = 4

    def pick(self, judge="alice", kind="human", seed=0, exclude_seen=True):
        return pairs.pick_pair(
            self.conn,
            judge_id=judge,
            judge_kind=kind,
            exclude_seen=exclude_seen,
            rng=random.Random(seed),
        )

    def test_fewer_than_two_published_entries_gives_none(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-pairs-one-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn, _ids = build_entries(tmp, 1)
        self.addCleanup(conn.close)
        self.assertIsNone(
            pairs.pick_pair(
                conn, judge_id="alice", judge_kind="human", rng=random.Random(0)
            )
        )

    def test_the_same_seed_gives_the_same_pair(self):
        first = self.pick(seed=7)
        second = self.pick(seed=7)
        self.assertIsNotNone(first)
        self.assertEqual(first, second)

    def test_a_pair_is_never_one_entry_twice(self):
        for seed in range(40):
            pair = self.pick(seed=seed)
            self.assertIsNotNone(pair)
            self.assertNotEqual(pair[0], pair[1])
            self.assertIn(pair[0], self.ids)
            self.assertIn(pair[1], self.ids)

    def test_it_mixes_the_rules_files_when_both_arms_exist(self):
        rules = {
            int(row["id"]): row["rules_file"]
            for row in self.conn.execute("SELECT id, rules_file FROM entries")
        }
        self.assertEqual(set(rules.values()), {"control", "treatment"})
        for seed in range(40):
            pair = self.pick(seed=seed)
            with self.subTest(seed=seed):
                self.assertNotEqual(
                    rules[pair[0]], rules[pair[1]],
                    "an untouched pool should offer control against treatment",
                )

    def test_it_prefers_the_pairs_nobody_has_judged(self):
        loaded = (self.ids[0], self.ids[1])
        for n, judge in enumerate(("alice", "bob", "carol")):
            for question in ("brief", "look"):
                self.vote(*loaded, "A", judge=judge, question=question)
        for seed in range(40):
            pair = self.pick(judge="dave", seed=seed)
            with self.subTest(seed=seed):
                self.assertNotEqual(
                    tuple(sorted(pair)), loaded,
                    "a pair with six judgments should not be offered while "
                    "unjudged pairs remain",
                )

    def test_a_pair_this_judge_has_finished_is_never_offered_again(self):
        first = self.pick(judge="alice", seed=3)
        for question in ("brief", "look"):
            self.vote(*first, "A", judge="alice", question=question)
        for seed in range(40):
            pair = self.pick(judge="alice", seed=seed)
            with self.subTest(seed=seed):
                self.assertIsNotNone(pair)
                self.assertNotEqual(tuple(sorted(pair)), tuple(sorted(first)))

    def test_half_answered_is_not_finished(self):
        """One question answered is not both; the pair comes back."""
        first = self.pick(judge="alice", seed=3)
        self.vote(*first, "A", judge="alice", question="brief")
        remaining = {
            tuple(sorted(pair))
            for pair in pairs.candidate_pairs(
                self.conn, judge_id="alice", judge_kind="human"
            )
        }
        self.assertIn(tuple(sorted(first)), remaining)

    def test_a_judge_who_has_seen_everything_gets_none(self):
        for a, b in pairs.candidate_pairs(
            self.conn, judge_id="alice", judge_kind="human"
        ):
            for question in ("brief", "look"):
                self.vote(a, b, "tie", judge="alice", question=question)
        self.assertIsNone(self.pick(judge="alice", seed=0))
        # …but exclude_seen=False still offers one.
        self.assertIsNotNone(self.pick(judge="alice", seed=0, exclude_seen=False))

    def test_one_judges_answers_do_not_exclude_another_judge(self):
        first = self.pick(judge="alice", seed=3)
        for question in ("brief", "look"):
            self.vote(*first, "A", judge="alice", question=question)
        self.assertIn(
            tuple(sorted(first)),
            {
                tuple(sorted(pair))
                for pair in pairs.candidate_pairs(
                    self.conn, judge_id="bob", judge_kind="human"
                )
            },
        )

    def test_position_is_not_systematic(self):
        """A is not always the lower entry id: the rng decides which side."""
        firsts = {self.pick(seed=seed)[0] for seed in range(40)}
        self.assertGreater(len(firsts), 1)


# ---------------------------------------------------------------------------
# (e) recording
# ---------------------------------------------------------------------------


class RecordTests(PairsTestCase):

    def test_a_later_answer_replaces_the_earlier_one(self):
        one, two = self.ids[0], self.ids[1]
        first = self.vote(one, two, "A", judge="alice")
        second = self.vote(one, two, "B", judge="alice")
        self.assertEqual(first, second, "the upsert should reuse the same row")
        rows = list(
            self.conn.execute(
                "SELECT choice FROM judgments WHERE judge_id = 'alice'"
            )
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["choice"], "B")
        table = pairs.scores(self.conn, population="human", question="look")
        self.assertEqual(table[two]["wins"], 1)
        self.assertEqual(table[one]["losses"], 1)

    def test_the_unique_constraint_is_per_judge_pair_and_question(self):
        one, two = self.ids[0], self.ids[1]
        self.vote(one, two, "A", judge="alice", question="look")
        self.vote(one, two, "A", judge="alice", question="brief")
        self.vote(one, two, "A", judge="bob", question="look")
        self.vote(one, two, "A", judge="gemma4:e4b", kind="agent", question="look")
        count = self.conn.execute("SELECT COUNT(*) AS c FROM judgments").fetchone()["c"]
        self.assertEqual(count, 4)

    def test_it_refuses_nonsense(self):
        one, two = self.ids[0], self.ids[1]
        for kwargs in (
            {"judge_kind": "robot"},
            {"question": "vibes"},
            {"choice": "maybe"},
            {"entry_b": self.ids[0]},
            {"judge_id": "  "},
        ):
            with self.subTest(**kwargs):
                call = dict(
                    entry_a=one, entry_b=two, judge_kind="human", judge_id="alice",
                    question="look", choice="A",
                )
                call.update(kwargs)
                with self.assertRaises(ValueError):
                    pairs.record(self.conn, **call)

    def test_it_stores_the_provenance_a_verdict_needs(self):
        one, two = self.ids[0], self.ids[1]
        digest = pairs.artefact_hash(one, two, conn=self.conn)
        pairs.record(
            self.conn, entry_a=one, entry_b=two, judge_kind="agent",
            judge_id="gemma4:e4b", question="look", choice="A",
            prompt_version="judge.md v1", artefact_hash=digest,
        )
        row = self.conn.execute("SELECT * FROM judgments").fetchone()
        self.assertEqual(row["prompt_version"], "judge.md v1")
        self.assertEqual(row["artefact_hash"], digest)
        self.assertTrue(row["created_utc"].endswith("Z"))


# ---------------------------------------------------------------------------
# (f) agreement
# ---------------------------------------------------------------------------


class AgreementTests(PairsTestCase):

    entries = 8

    def test_agents_agreeing_on_three_of_four_pairs_is_three_quarters(self):
        # Four disjoint pairs. Humans say A on all four; the agent judge says A
        # on three of them and B on the fourth.
        quads = [
            (self.ids[0], self.ids[1], "A"),
            (self.ids[2], self.ids[3], "A"),
            (self.ids[4], self.ids[5], "A"),
            (self.ids[6], self.ids[7], "B"),
        ]
        for a, b, agent_choice in quads:
            self.vote(a, b, "A", judge="alice", question="brief")
            self.vote(a, b, agent_choice, judge="gemma4:e4b", kind="agent",
                      question="brief")
        result = pairs.agreement(self.conn)
        self.assertEqual(result["brief"]["n"], 4)
        self.assertEqual(result["brief"]["agree"], 3)
        self.assertEqual(result["brief"]["agreement"], 0.75)
        # 'look' was never asked, so it has no fraction at all — not a zero.
        self.assertEqual(result["look"]["n"], 0)
        self.assertIsNone(result["look"]["agreement"])
        self.assertEqual(result["overall"]["n"], 4)
        self.assertEqual(result["overall"]["agreement"], 0.75)

    def test_a_pair_only_one_population_answered_does_not_count(self):
        self.vote(self.ids[0], self.ids[1], "A", judge="alice", question="look")
        result = pairs.agreement(self.conn)
        self.assertEqual(result["look"]["n"], 0)
        self.assertIsNone(result["look"]["agreement"])

    def test_orientation_does_not_create_a_disagreement(self):
        one, two = self.ids[0], self.ids[1]
        self.vote(one, two, "A", judge="alice", question="look")
        self.vote(two, one, "B", judge="gemma4:e4b", kind="agent", question="look")
        result = pairs.agreement(self.conn)
        self.assertEqual(result["look"]["n"], 1)
        self.assertEqual(result["look"]["agreement"], 1.0)

    def test_a_populations_verdict_is_its_majority(self):
        one, two = self.ids[0], self.ids[1]
        for judge, choice in (("alice", "A"), ("bob", "A"), ("carol", "B")):
            self.vote(one, two, choice, judge=judge, question="look")
        self.vote(one, two, "A", judge="gemma4:e4b", kind="agent", question="look")
        result = pairs.agreement(self.conn)
        self.assertEqual(result["look"]["agreement"], 1.0)

    def test_no_pairs_at_all_is_no_fraction(self):
        result = pairs.agreement(self.conn)
        for name in ("brief", "look", "overall"):
            self.assertEqual(result[name]["n"], 0)
            self.assertIsNone(result[name]["agreement"])


# ---------------------------------------------------------------------------
# (g) artefact hash
# ---------------------------------------------------------------------------


class ArtefactHashTests(PairsTestCase):

    def test_a_changed_strip_changes_the_hash(self):
        one, two = self.ids[0], self.ids[1]
        before = pairs.artefact_hash(one, two, conn=self.conn)
        strip = Path(
            self.conn.execute(
                "SELECT strip_path FROM entries WHERE id = ?", (one,)
            ).fetchone()["strip_path"]
        )
        strip.write_bytes(strip.read_bytes() + b"one more frame")
        after = pairs.artefact_hash(one, two, conn=self.conn)
        self.assertNotEqual(before, after)

    def test_a_changed_brief_changes_the_hash(self):
        one, two = self.ids[0], self.ids[1]
        before = pairs.artefact_hash(one, two, conn=self.conn)
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?", ("a different brief", one)
        )
        self.assertNotEqual(before, pairs.artefact_hash(one, two, conn=self.conn))

    def test_the_order_the_pair_was_shown_in_is_part_of_the_hash(self):
        one, two = self.ids[0], self.ids[1]
        self.assertNotEqual(
            pairs.artefact_hash(one, two, conn=self.conn),
            pairs.artefact_hash(two, one, conn=self.conn),
        )

    def test_it_is_stable_across_calls_and_takes_rows_too(self):
        one, two = self.ids[0], self.ids[1]
        digest = pairs.artefact_hash(one, two, conn=self.conn)
        self.assertEqual(digest, pairs.artefact_hash(one, two, conn=self.conn))
        rows = [
            self.conn.execute("SELECT * FROM entries WHERE id = ?", (i,)).fetchone()
            for i in (one, two)
        ]
        self.assertEqual(digest, pairs.artefact_hash(rows[0], rows[1]))

    def test_a_missing_strip_is_hashed_honestly_not_raised(self):
        one, two = self.ids[0], self.ids[1]
        self.conn.execute("UPDATE entries SET strip_path = NULL WHERE id = ?", (one,))
        self.assertEqual(len(pairs.artefact_hash(one, two, conn=self.conn)), 64)


# ---------------------------------------------------------------------------
# (h) and (i) — what the generated pages show
# ---------------------------------------------------------------------------


class RenderedPageTests(unittest.TestCase):
    """The gallery tests' own fixture, with pairs judged on top of it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-pairs-gallery-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = test_gallery.build_db(self.tmp)
        self.addCleanup(self.conn.close)
        self.dest = self.tmp / "gallery"
        self.dest.mkdir()
        self.config = gallery.Config(write_path="https://write.example.invalid/api")
        one, two = self.ids[0], self.ids[1]
        for judge, choice in (("alice", "A"), ("bob", "A"), ("carol", "B")):
            for question in ("brief", "look"):
                pairs.record(
                    self.conn, entry_a=one, entry_b=two, judge_kind="human",
                    judge_id=judge, question=question, choice=choice,
                    prompt_version="gallery-v1",
                )
        pairs.record(
            self.conn, entry_a=one, entry_b=two, judge_kind="agent",
            judge_id="gemma4:e4b", question="look", choice="B",
            prompt_version="judge.md v1",
        )
        gallery.render_all(self.conn, self.dest, self.config)

    def read(self, *parts):
        return (self.dest.joinpath(*parts)).read_text(encoding="utf-8")

    # (h)
    def test_the_entry_page_shows_both_populations_scores(self):
        page = self.read("e", str(self.ids[0]), "index.html")
        table = pairs.scores(self.conn, population="human", question="look")
        briefs = pairs.scores(self.conn, population="human", question="brief")
        self.assertIn(f"{float(table[self.ids[0]]['score']):.2f} over 3 pairs", page)
        self.assertIn(
            f"closer to its brief: {float(briefs[self.ids[0]]['score']):.2f} "
            "over 3 pairs",
            page,
        )
        agents = pairs.scores(self.conn, population="agent", question="look")
        self.assertIn(f"{float(agents[self.ids[0]]['score']):.2f} over 1 pair", page)
        # The agent judged 'look' only, so its 'brief' cell — and only that
        # cell — still says so. A missing question is not a zero.
        self.assertEqual(page.count("no pairs yet"), 1)
        self.assertIn("closer to its brief: no pairs yet", page)
        self.assertEqual(balance(self.dest / "e" / str(self.ids[0]) / "index.html"), [])

    def test_an_unjudged_entry_still_says_no_pairs_yet(self):
        page = self.read("e", str(self.ids[2]), "index.html")
        self.assertEqual(page.count("no pairs yet"), 2)

    def test_the_grid_cards_show_the_scores(self):
        index = self.read("index.html")
        table = pairs.scores(self.conn, population="human", question="look")
        for entry_id in self.ids[:2]:
            with self.subTest(entry=entry_id):
                self.assertIn(f"{float(table[entry_id]['score']):.2f} over 3 pairs", index)
        self.assertIn("card-briefs", index)
        self.assertEqual(balance(self.dest / "index.html"), [])

    def test_the_agent_score_is_its_own_number(self):
        """Two populations, two scores, never one aggregate."""
        humans = pairs.scores(self.conn, population="human", question="look")
        agents = pairs.scores(self.conn, population="agent", question="look")
        # Humans put entry one ahead 2-1; the agent put entry two ahead.
        self.assertGreater(humans[self.ids[0]]["score"], humans[self.ids[1]]["score"])
        self.assertLess(agents[self.ids[0]]["score"], agents[self.ids[1]]["score"])
        page = self.read("e", str(self.ids[0]), "index.html")
        self.assertIn(f"{float(humans[self.ids[0]]['score']):.2f}", page)
        self.assertIn(f"{float(agents[self.ids[0]]['score']):.2f}", page)

    # (i)
    def test_compare_carries_the_pairs_json_data(self):
        compare = self.read("compare.html")
        self.assertIn('id="sketchgen-pairs"', compare)
        block = compare.split('<script id="sketchgen-pairs" type="application/json">')[1]
        offered = json.loads(block.split("</script>")[0].replace("<\\/", "</"))
        self.assertTrue(offered)
        for pair in offered:
            self.assertIn(pair["a"], self.ids[:2])
            self.assertIn(pair["b"], self.ids[:2])
            self.assertNotEqual(pair["a"], pair["b"])
        on_disk = json.loads(self.read("pairs.json"))
        self.assertEqual(on_disk["pairs"], offered)

    def test_the_agent_block_is_present_but_hidden(self):
        compare = self.read("compare.html")
        self.assertIn("data-reveal hidden", compare)
        self.assertIn("data-agent-verdicts", compare)
        reveal = compare.split('data-reveal hidden')[1].split("</section>")[0]
        self.assertIn("data-agent-verdicts", reveal)
        block = compare.split('<script id="sketchgen-agents" type="application/json">')[1]
        verdicts = json.loads(block.split("</script>")[0].replace("<\\/", "</"))
        key = f"{min(self.ids[:2])}-{max(self.ids[:2])}"
        self.assertEqual(len(verdicts[key]), 1)
        self.assertEqual(verdicts[key][0]["judge"], "gemma4:e4b")

    def test_the_vote_payload_matches_the_write_path(self):
        """gallery.js must send exactly what worker.js:routeVote destructures."""
        script = (REPO_ROOT / "sketchgen" / "assets" / "gallery.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("entry_a: sides.A.id", script)
        self.assertIn("entry_b: sides.B.id", script)
        worker = (REPO_ROOT / "writepath" / "worker.js").read_text(encoding="utf-8")
        self.assertIn("entry_a: a, entry_b: b, question, choice", worker)

    def test_a_second_render_is_byte_identical(self):
        first = self.read("compare.html"), self.read("pairs.json")
        gallery.render_all(self.conn, self.dest, self.config)
        self.assertEqual(first, (self.read("compare.html"), self.read("pairs.json")))

    def test_nothing_personal_survives_the_render(self):
        gallery.guard(self.dest)


def balance(path: Path):
    return test_gallery.balance_errors(path)


# ---------------------------------------------------------------------------
# The command line, as the shell sees it
# ---------------------------------------------------------------------------


class CommandLineTests(PairsTestCase):

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "pairs", *args],
            capture_output=True, text=True, check=False,
        )

    @property
    def dbpath(self):
        return str(self.tmp / "sketchgen.db")

    def test_help_exits_zero_for_every_subcommand(self):
        for name in ("next", "record", "scores", "agreement"):
            with self.subTest(subcommand=name):
                result = self.run_cli(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--db", result.stdout)

    def test_next_then_record_then_scores(self):
        result = self.run_cli("next", "--for", "alice", "--seed", "1", "--json",
                              "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        offered = json.loads(result.stdout)
        self.assertEqual(len(offered["artefact_hash"]), 64)

        result = self.run_cli(
            "record", "--a", str(offered["entry_a"]), "--b", str(offered["entry_b"]),
            "--kind", "human", "--judge", "alice", "--question", "look",
            "--choice", "A", "--db", self.dbpath,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_cli("scores", "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        table = json.loads(result.stdout)
        self.assertEqual(table["agent"]["look"], {})
        self.assertEqual(
            set(table["human"]["look"]),
            {str(offered["entry_a"]), str(offered["entry_b"])},
        )

    def test_one_published_entry_is_a_null_and_an_explanation(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-pairs-one-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        conn, _ids = build_entries(tmp, 1)
        conn.close()
        result = subprocess.run(
            [sys.executable, str(CLI), "pairs", "next", "--for", "someone",
             "--json", "--db", str(tmp / "sketchgen.db")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(json.loads(result.stdout))
        self.assertIn("a pair needs two", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_scores_on_an_unjudged_database_is_empty(self):
        result = self.run_cli("scores", "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {p: {q: {} for q in pairs.QUESTIONS} for p in pairs.POPULATIONS},
        )

    def test_agreement_json_is_well_formed_with_no_pairs(self):
        result = self.run_cli("agreement", "--json", "--db", self.dbpath)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["overall"], {"n": 0, "agree": 0, "agreement": None})

    def test_a_human_judge_id_that_is_not_a_github_username_is_refused(self):
        result = self.run_cli(
            "record", "--a", str(self.ids[0]), "--b", str(self.ids[1]),
            "--kind", "human", "--judge", "someone@example.invalid",
            "--question", "look", "--choice", "A", "--db", self.dbpath,
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("GitHub username", result.stderr)

    def test_an_unknown_entry_is_refused(self):
        result = self.run_cli(
            "record", "--a", str(self.ids[0]), "--b", "99999",
            "--kind", "human", "--judge", "alice", "--question", "look",
            "--choice", "A", "--db", self.dbpath,
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("no entry 99999", result.stderr)

    def test_a_missing_database_is_refused(self):
        result = self.run_cli("scores", "--db", str(self.tmp / "nowhere.db"))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("refused", result.stderr)


if __name__ == "__main__":
    unittest.main()


class WritePathContractTests(unittest.TestCase):
    """gallery.js and writepath/worker.js must agree on every payload."""

    def setUp(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1]
        self.js = (root / "sketchgen" / "assets" / "gallery.js").read_text()
        self.worker = (root / "writepath" / "worker.js").read_text()

    def test_counts_query_parameter(self):
        self.assertIn('"/counts?entries="', self.js)
        self.assertIn('searchParams.get("entries")', self.worker)

    def test_like_payload(self):
        self.assertIn("entry_id:", self.js)
        self.assertIn("on:", self.js)
        self.assertIn("body.entry_id", self.worker)
        self.assertIn("body.on", self.worker)
