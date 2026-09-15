"""Unit tests for sketchgen.lineage — packet 5.3, the self-prompting half.

Run:  python3 -m unittest discover -s tests -v

No model and no browser: the critique is replayed from ``tests/fixtures/
lineage/*.txt`` and the one worker run here stubs the executor and the gate the
way tests/test_worker.py does. The two saved responses are the control this
packet needs — a one-sentence critique that must pass the validator and a
two-sentence one that must not — and they are checked before anything asks a
model for a third.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen import worker  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "lineage"
CLI = REPO_ROOT / "bin" / "sketchgen"


# ---------------------------------------------------------------------------
# Stubs, kept to the minimum this packet needs
# ---------------------------------------------------------------------------


def stub_executor(out_root):
    def run(*, brief, assertions, rules_file, out_dir, model, host):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "sketch.js").write_text("function setup(){}\n", encoding="utf-8")
        return worker.Execution(
            ok=True,
            source_dir=str(out),
            model=model,
            prompt_version="executor-v1",
            statement="a stub statement\n",
            wall_s=1.0,
        )

    return run


def stub_gate():
    def run(*, source_dir, assertions, out_dir):
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        report = {"seed": 1, "checks": {}, "assertions": {}, "artefacts": {}, "exit": 0}
        path = out / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return worker.GateOutcome(exit_code=0, report=report, report_path=str(path))

    return run


class LineageTestCase(unittest.TestCase):
    """One temp database with one published root entry to grow a line from."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-lineage-")
        self.addCleanup(self._tmp.cleanup)
        self.root_dir = Path(self._tmp.name)
        self.path = str(self.root_dir / "test.db")
        self.jobs = self.root_dir / "jobs"
        db.init(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.root_entry = self.publish(
            "sixty circles drifting on their own noise paths over a dark ground"
        )

    # -- helpers ---------------------------------------------------------

    def publish(self, prompt, by="astudent", state="published", **opts):
        """A job walked to a terminal state with its entry row, as the worker
        leaves it. Returns the entry id."""
        job_id = db.enqueue(self.conn, prompt, by, rules_file="treatment", **opts)
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        if state == "published":
            db.transition(self.conn, job_id, "published")
        job = db.get_job(self.conn, job_id)
        entry_id = db.create_entry(
            self.conn,
            job_id,
            state="held" if state == "held" else state,
            prompt=prompt,
            brief="a brief that arrived with the job",
            statement="the model's own account",
            assertions_json=json.dumps(["motion(idle)"]),
            rules_file="treatment",
            planner="gemma4:e4b",
            parent_entry_id=job.parent_entry_id,
            submitted_by=by,
        )
        lineage.record_child(self.conn, entry_id, job)
        return entry_id

    def entry_for(self, job_id, state="published"):
        """Finish a spawned job the way the worker would, and return the entry."""
        job = db.get_job(self.conn, job_id)
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        if state == "published":
            db.transition(self.conn, job_id, "published")
        entry_id = db.create_entry(
            self.conn,
            job_id,
            state=state,
            prompt=job.prompt,
            parent_entry_id=job.parent_entry_id,
            submitted_by=job.submitted_by,
        )
        lineage.record_child(self.conn, entry_id, job)
        return entry_id

    def build_line(self, depth=3):
        parent = self.root_entry
        made = [parent]
        for n in range(1, depth + 1):
            job_id = lineage.spawn(
                self.conn,
                parent_entry_id=parent,
                critique=f"critique number {n}",
                critique_by="gemma4:e4b",
                submitted_by="profcarroll",
            )
            parent = self.entry_for(job_id, state="published" if n < depth else "held")
            made.append(parent)
        return made


# ---------------------------------------------------------------------------


class TestSplitPrompt(unittest.TestCase):
    """The prompt comes apart again, because compose_prompt is its only writer."""

    def test_a_root_with_no_revisions_is_the_whole_prompt(self):
        root, revisions = lineage.split_prompt(
            "sixty circles drifting on their own noise paths"
        )
        self.assertEqual("sixty circles drifting on their own noise paths", root)
        self.assertEqual([], revisions)

    def test_the_empty_prompt_splits_into_nothing(self):
        self.assertEqual(("", []), lineage.split_prompt(""))
        self.assertEqual(("", []), lineage.split_prompt("   \n\n "))

    def test_one_generation_round_trips(self):
        prompt = lineage.compose_prompt(
            "sixty circles drifting", "let one fall out of phase with the rest"
        )
        root, revisions = lineage.split_prompt(prompt)
        self.assertEqual("sixty circles drifting", root)
        self.assertEqual(["let one fall out of phase with the rest"], revisions)
        self.assertEqual(prompt, lineage.compose_prompt(root, revisions[0]))

    def test_ten_generations_come_back_in_order(self):
        prompt = "a cityscape from sunrise to sunset"
        critiques = [f"revision number {n}" for n in range(1, 11)]
        for text in critiques:
            prompt = lineage.compose_prompt(prompt, text)
        root, revisions = lineage.split_prompt(prompt)
        self.assertEqual("a cityscape from sunrise to sunset", root)
        self.assertEqual(critiques, revisions)
        # and the whole prompt rebuilds from the pieces, in order
        rebuilt = root
        for text in revisions:
            rebuilt = lineage.compose_prompt(rebuilt, text)
        self.assertEqual(prompt, rebuilt)

    def test_the_heading_mid_sentence_is_a_sentence_not_a_generation(self):
        prompt = lineage.compose_prompt(
            "a poster that says Revise: nothing is ever finished",
            "make the type larger",
        )
        root, revisions = lineage.split_prompt(prompt)
        self.assertEqual("a poster that says Revise: nothing is ever finished", root)
        self.assertEqual(["make the type larger"], revisions)

    def test_a_multiline_root_keeps_its_own_line_breaks(self):
        prompt = lineage.compose_prompt(
            "a first line\nand a second line", "one more thing"
        )
        root, revisions = lineage.split_prompt(prompt)
        self.assertEqual("a first line\nand a second line", root)
        self.assertEqual(["one more thing"], revisions)

    def test_a_revision_is_collapsed_the_way_compose_prompt_wrote_it(self):
        prompt = "the root\n\nRevise:   spread    over\ntwo lines"
        root, revisions = lineage.split_prompt(prompt)
        self.assertEqual("the root", root)
        self.assertEqual(["spread over two lines"], revisions)


class TestSpawn(LineageTestCase):
    def test_spawn_from_published_composes_the_prompt_and_keeps_the_parent(self):
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=self.root_entry,
            critique="let one circle fall out of phase with the rest",
            critique_by="gemma4:e4b",
            submitted_by="profcarroll",
        )
        self.assertIsNotNone(job_id)
        job = db.get_job(self.conn, job_id)
        self.assertEqual("queued", job.state)
        self.assertEqual(self.root_entry, job.parent_entry_id)
        self.assertEqual("profcarroll", job.submitted_by)
        self.assertEqual("gemma4:e4b", job.critique_by)
        self.assertEqual(
            "let one circle fall out of phase with the rest", job.critique
        )
        # the parent's prompt, then the critique under the fixed heading
        self.assertTrue(
            job.prompt.startswith(
                "sixty circles drifting on their own noise paths over a dark ground"
            )
        )
        self.assertIn(
            "Revise: let one circle fall out of phase with the rest", job.prompt
        )
        # inherited, so the line stays a fair comparison with itself
        self.assertEqual("treatment", job.rules_file)
        self.assertEqual("gemma4:e4b", job.planner)
        # and no lineage row yet: there is no child entry to key one on
        self.assertEqual(
            0, self.conn.execute("SELECT COUNT(*) AS c FROM lineage").fetchone()["c"]
        )

    def test_spawn_from_a_held_parent_is_allowed(self):
        # Asking for a revision is how a person decides whether the parent is
        # worth publishing (instructor, 2026-09-14); the child holds as usual.
        held = self.publish("a sketch that has not been through the gate",
                            state="held")
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=held,
            critique="try it with one warm hue",
            critique_by="profcarroll",
            submitted_by="profcarroll",
        )
        self.assertIsNotNone(job_id)
        job = db.get_job(self.conn, job_id)
        self.assertEqual(held, job.parent_entry_id)
        self.assertEqual("hold", job.publication)

    def test_spawn_from_a_rejected_parent_returns_none_and_records_nothing(self):
        rejected = self.publish("a sketch a person closed", state="rejected")
        jobs_before = len(db.list_jobs(self.conn))
        self.assertIsNone(
            lineage.spawn(
                self.conn,
                parent_entry_id=rejected,
                critique="try it with one warm hue",
                critique_by="profcarroll",
                submitted_by="profcarroll",
            )
        )
        self.assertEqual(jobs_before, len(db.list_jobs(self.conn)))

    def test_spawn_from_a_failed_kept_parent_is_allowed(self):
        kept = self.publish("a sketch that never worked", state="failed-kept")
        self.assertIsNotNone(
            lineage.spawn(
                self.conn,
                parent_entry_id=kept,
                critique="drop the microphone and let the mouse drive it",
                critique_by="profcarroll",
                submitted_by="profcarroll",
            )
        )

    def test_spawn_refuses_a_critique_by_that_could_be_a_person(self):
        for bad in ("someone@example.invalid", "Ada Lovelace", ""):
            with self.subTest(critique_by=bad):
                with self.assertRaises(ValueError):
                    lineage.spawn(
                        self.conn,
                        parent_entry_id=self.root_entry,
                        critique="try it slower",
                        critique_by=bad,
                        submitted_by="profcarroll",
                    )

    def test_three_generations_then_the_fourth_waits_for_a_person(self):
        parent = self.root_entry
        seen = []
        for depth in (1, 2, 3):
            job_id = lineage.spawn(
                self.conn,
                parent_entry_id=parent,
                critique=f"change the {depth} thing about it",
                critique_by="gemma4:e4b",
                submitted_by="profcarroll",
            )
            job = db.get_job(self.conn, job_id)
            seen.append(job)
            parent = self.entry_for(job_id)
            self.assertEqual(depth, lineage.generation_of(self.conn, parent))

        # generations 1 and 2 are ordinary queued jobs
        for job in seen[:2]:
            self.assertEqual("queued", job.state)
            self.assertIsNone(job.needs)
            self.assertEqual("hold", job.publication)
        # generation 3 is at DECIDE[lineage-depth] and stops for a person
        self.assertEqual("review", seen[2].needs)
        self.assertEqual("hold", seen[2].publication)

    def test_at_the_limit_publication_auto_is_overridden(self):
        parent = self.root_entry
        for _ in range(3):
            job_id = lineage.spawn(
                self.conn,
                parent_entry_id=parent,
                critique="change one thing about it",
                critique_by="gemma4:e4b",
                submitted_by="profcarroll",
                publication="auto",
            )
            job = db.get_job(self.conn, job_id)
            parent = self.entry_for(job_id)
        self.assertEqual("hold", job.publication)
        self.assertEqual("review", job.needs)

    def test_max_depth_is_a_setting_not_a_constant(self):
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=self.root_entry,
            critique="change one thing about it",
            critique_by="gemma4:e4b",
            submitted_by="profcarroll",
            max_depth=1,
        )
        self.assertEqual("review", db.get_job(self.conn, job_id).needs)


class TestWorkerRecordsTheLink(LineageTestCase):
    def test_the_worker_copies_the_critique_into_lineage_with_the_entry(self):
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=self.root_entry,
            critique="let one circle fall out of phase with the rest",
            critique_by="gemma4:e4b",
            submitted_by="profcarroll",
        )
        self.conn.execute(
            "UPDATE jobs SET brief = ?, assertions_json = ? WHERE id = ?",
            ("a brief that arrived with the job", json.dumps(["motion(idle)"]), job_id),
        )
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            run = worker.Worker(
                self.conn,
                jobs_dir=self.jobs,
                gate_path="/nonexistent/sketch_gate.py",
                executor_fn=stub_executor(self.jobs),
                gate_fn=stub_gate(),
                probe=lambda: {"processes": [], "models": [], "ollama_error": None},
                log_stream=devnull,
            )
            self.assertEqual(0, run.run_once())

        entry = self.conn.execute(
            "SELECT * FROM entries WHERE job_id = ?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(entry)
        row = self.conn.execute(
            "SELECT * FROM lineage WHERE child_entry_id = ?", (entry["id"],)
        ).fetchone()
        self.assertIsNotNone(row, "the worker wrote no lineage row")
        self.assertEqual(self.root_entry, row["parent_entry_id"])
        self.assertEqual(1, row["generation"])
        self.assertEqual("gemma4:e4b", row["critique_by"])
        self.assertEqual(
            "let one circle fall out of phase with the rest", row["critique"]
        )
        self.assertTrue(row["created_utc"].endswith("Z"))

    def test_a_job_with_no_parent_records_nothing(self):
        job_id = db.enqueue(
            self.conn, "a root prompt", "astudent",
            brief="a brief", assertions=["motion(idle)"],
        )
        before = self.conn.execute("SELECT COUNT(*) AS c FROM lineage").fetchone()["c"]
        with open(os.devnull, "w", encoding="utf-8") as devnull:
            worker.Worker(
                self.conn,
                jobs_dir=self.jobs,
                gate_path="/nonexistent/sketch_gate.py",
                executor_fn=stub_executor(self.jobs),
                gate_fn=stub_gate(),
                probe=lambda: {"processes": [], "models": [], "ollama_error": None},
                log_stream=devnull,
            ).run_once()
        after = self.conn.execute("SELECT COUNT(*) AS c FROM lineage").fetchone()["c"]
        self.assertEqual(before, after)


class TestLine(LineageTestCase):
    def test_line_returns_the_generations_in_order(self):
        made = self.build_line()
        items = lineage.line(self.conn, self.root_entry)
        self.assertEqual(made, [item["entry_id"] for item in items])
        self.assertEqual([0, 1, 2, 3], [item["generation"] for item in items])
        self.assertIsNone(items[0]["critique"])
        self.assertEqual("critique number 1", items[1]["critique"])
        self.assertEqual("gemma4:e4b", items[1]["critique_by"])
        self.assertEqual("published", items[1]["state"])
        self.assertEqual("held", items[-1]["state"])

    def test_the_last_generation_is_the_one_at_the_limit(self):
        self.build_line()
        items = lineage.line(self.conn, self.root_entry)
        self.assertEqual([False, False, False, True],
                         [item["at_limit"] for item in items])

    def test_scores_are_none_while_packet_51_is_not_installed(self):
        self.build_line(depth=1)
        for item in lineage.line(self.conn, self.root_entry):
            self.assertIsNone(item["scores"])

    def test_a_root_on_its_own_is_a_one_generation_line(self):
        items = lineage.line(self.conn, self.root_entry)
        self.assertEqual(1, len(items))
        self.assertEqual(self.root_entry, items[0]["entry_id"])
        self.assertEqual(0, items[0]["generation"])

    def test_an_unknown_root_is_an_empty_line(self):
        self.assertEqual([], lineage.line(self.conn, 9999))

    def test_roots_are_the_parentless_entries_that_have_children(self):
        self.build_line(depth=2)
        lonely = self.publish("a root nobody critiqued")
        found = lineage.roots(self.conn)
        self.assertEqual([self.root_entry], [item["entry_id"] for item in found])
        self.assertNotIn(lonely, [item["entry_id"] for item in found])
        self.assertEqual(3, found[0]["generations"])


class TestTheLinePage(LineageTestCase):
    """gallery.py renders line() — packet 5.3 item 4, checked on a real line."""

    def page(self):
        from sketchgen import gallery

        by_id = {
            int(row["id"]): row
            for row in self.conn.execute(
                "SELECT * FROM entries WHERE state IN ('published', 'failed-kept')"
            )
        }
        return gallery._line_page(self.conn, self.root_entry, {}, by_id)

    def test_every_generation_gets_a_card_with_its_critique_above_it(self):
        self.build_line()
        page = self.page()
        self.assertIn(f'href="../e/{self.root_entry}/"', page)
        self.assertIn("Revise: critique number 1", page)
        self.assertIn("Revise: critique number 3", page)
        self.assertIn("critique by gemma4:e4b", page)
        self.assertIn("generation 3", page)

    def test_the_line_at_the_limit_closes_with_the_card_that_waits(self):
        self.build_line()
        page = self.page()
        self.assertIn("waits for a person", page)
        self.assertIn("DECIDE[lineage-depth]", page)

    def test_a_generation_that_is_not_public_shows_no_prompt_of_its_own(self):
        made = self.build_line()
        page = self.page()
        # the last generation is held, so it is on no public page
        self.assertNotIn(f'href="../e/{made[-1]}/"', page)
        self.assertIn("not published", page)
        self.assertIn("has not been through the publication gate", page)

    def test_a_short_line_has_no_card_that_waits(self):
        self.build_line(depth=1)
        self.assertNotIn("waits for a person", self.page())


class TestCritique(LineageTestCase):
    def test_prompts_critic_md_is_version_1_and_leaves_no_placeholders(self):
        self.assertEqual("critic-v2", lineage.prompt_version())
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (self.root_entry,)
        ).fetchone()
        rendered = lineage.critique_prompt(row, row["statement"], row["brief"])
        self.assertNotIn("{prompt}", rendered)
        self.assertNotIn("{brief}", rendered)
        self.assertNotIn("{statement}", rendered)
        self.assertNotIn("{assertions}", rendered)
        self.assertNotIn("prompt_version:", rendered)
        self.assertIn("sixty circles drifting", rendered)
        self.assertIn("motion(idle)", rendered)

    def test_the_prompt_never_shows_the_judge_who_made_it_or_who_liked_it(self):
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (self.root_entry,)
        ).fetchone()
        rendered = lineage.critique_prompt(row, row["statement"], row["brief"])
        self.assertNotIn("astudent", rendered)
        self.assertNotIn("gemma4:e4b", rendered)

    def test_a_saved_one_sentence_output_comes_back_trimmed(self):
        result = lineage.critique(
            self.conn,
            self.root_entry,
            model="gemma4:e4b",
            stub=FIXTURES / "one-sentence.txt",
        )
        self.assertEqual(
            "the same field, and this time let one circle fall out of phase "
            "with the rest.",
            result.text,
        )
        self.assertEqual("gemma4:e4b", result.model)
        self.assertEqual("critic-v2", result.prompt_version)

    def test_a_saved_two_sentence_output_is_refused(self):
        with self.assertRaises(lineage.CritiqueFailed) as caught:
            lineage.critique(
                self.conn,
                self.root_entry,
                model="gemma4:e4b",
                stub=FIXTURES / "two-sentences.txt",
            )
        self.assertIn("2 sentences", str(caught.exception))
        self.assertIn("The motion is doing the work", caught.exception.raw)

    def test_a_critique_carrying_code_is_refused(self):
        with self.assertRaises(lineage.CritiqueFailed):
            lineage.critique(
                self.conn,
                self.root_entry,
                model="gemma4:e4b",
                stub=FIXTURES / "has-code.txt",
            )

    def test_a_long_sentence_is_refused(self):
        with self.assertRaises(lineage.CritiqueFailed):
            lineage.validate(" ".join(["drift"] * 40))

    def test_a_missing_stub_is_a_refusal_not_a_failure(self):
        with self.assertRaises(lineage.CritiqueRefused):
            lineage.critique(
                self.conn, self.root_entry, stub=self.root_dir / "nothing.txt"
            )


class TestCli(LineageTestCase):
    """The exit codes, run the way the ACCEPT runs them."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "lineage", *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_help_works(self):
        done = self.run_cli("--help")
        self.assertEqual(0, done.returncode)
        self.assertIn("spawn", done.stdout)

    def test_spawn_then_show_over_the_cli(self):
        done = self.run_cli(
            "spawn", "--parent", str(self.root_entry),
            "--critique", "let one circle fall out of phase",
            "--by", "profcarroll", "--db", self.path, "--json",
        )
        self.assertEqual(0, done.returncode, done.stderr)
        document = json.loads(done.stdout)
        self.assertEqual(1, document["generation"])
        self.assertEqual("profcarroll", document["critique_by"])

        done = self.run_cli("show", "--root", str(self.root_entry),
                            "--db", self.path, "--json")
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(1, len(json.loads(done.stdout)["generations"]))

    def test_spawn_from_a_rejected_parent_exits_1(self):
        held = self.publish("closed by a person", state="rejected")
        done = self.run_cli(
            "spawn", "--parent", str(held), "--critique", "slower",
            "--by", "profcarroll", "--db", self.path,
        )
        self.assertEqual(1, done.returncode)
        self.assertIn("nothing spawned", done.stderr)

    def test_critique_on_a_two_sentence_output_exits_1_and_keeps_the_raw(self):
        out = self.root_dir / "out"
        done = self.run_cli(
            "critique", "--entry", str(self.root_entry), "--model", "gemma4:e4b",
            "--out", str(out), "--stub", str(FIXTURES / "two-sentences.txt"),
            "--db", self.path,
        )
        self.assertEqual(1, done.returncode)
        raw = out / f"entry-{self.root_entry}-critique-raw.txt"
        self.assertTrue(raw.is_file(), done.stderr)
        self.assertIn("The motion is doing the work", raw.read_text(encoding="utf-8"))

    def test_critique_on_a_one_sentence_output_exits_0_and_writes_json(self):
        out = self.root_dir / "out-ok"
        done = self.run_cli(
            "critique", "--entry", str(self.root_entry), "--model", "gemma4:e4b",
            "--out", str(out), "--stub", str(FIXTURES / "one-sentence.txt"),
            "--db", self.path, "--json",
        )
        self.assertEqual(0, done.returncode, done.stderr)
        document = json.loads(done.stdout)
        self.assertEqual("critic-v2", document["prompt_version"])
        self.assertTrue(document["critique"].startswith("the same field"))
        self.assertTrue(
            (out / f"entry-{self.root_entry}-critique.json").is_file()
        )

    def test_an_unknown_database_is_a_refusal(self):
        done = self.run_cli(
            "show", "--root", "1", "--db", str(self.root_dir / "nope.db")
        )
        self.assertEqual(3, done.returncode)


if __name__ == "__main__":
    unittest.main()
