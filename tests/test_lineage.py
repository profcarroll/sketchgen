"""Unit tests for sketchgen.lineage — packet 5.3, the self-prompting half.

Run:  python3 -m unittest discover -s tests -v

No model and no browser: the critique is replayed from ``tests/fixtures/
lineage/*.txt`` and the one worker run here stubs the executor and the gate the
way tests/test_worker.py does. The two saved responses are the control this
packet needs — a one-sentence critique that must pass the validator and a
two-sentence one that must not — and they are checked before anything asks a
model for a third.
"""

import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import ghostshim  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen import worker  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "lineage"
CLI = REPO_ROOT / "bin" / "sketchgen"


# ---------------------------------------------------------------------------
# Stubs, kept to the minimum this packet needs
# ---------------------------------------------------------------------------


def png_bytes(mark: bytes = b"\x11\x22\x33") -> bytes:
    """A tiny but genuinely well-formed PNG, 1x1, with ``mark`` as its pixel.

    Nothing in these tests decodes it — what is asserted is that the bytes on
    disk are the bytes that reach the model — but the critic is being handed a
    picture, so the fixture is a picture. ``mark`` varies it per entry so two
    entries do not share a sha256 by accident.
    """

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixel = zlib.compress(b"\x00" + mark[:3].ljust(3, b"\x00"))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixel)
        + chunk(b"IEND", b"")
    )


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
        self.strips = self.root_dir / "strips"
        db.init(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.root_entry = self.publish(
            "sixty circles drifting on their own noise paths over a dark ground"
        )

    # -- helpers ---------------------------------------------------------

    def publish(self, prompt, by="astudent", state="published", strip="png",
                ghost=None, **opts):
        """A job walked to a terminal state with its entry row, as the worker
        leaves it. Returns the entry id.

        ``strip`` is what the gate left behind: ``"png"`` for a real strip file,
        ``"empty"`` for a zero-byte one, ``"missing"`` for a path pointing at
        nothing, and ``None`` for no ``strip_path`` column at all. Since
        critic-v3 the critic refuses everything but the first, so the three
        broken shapes each have a test.

        ``ghost`` is the gate's second picture, which has no column at all and
        is found in the attempt directory (auto-mouse.md §5.3): ``"png"`` for a
        real one, ``"empty"`` for a zero-byte one, ``"dir"`` for an attempt
        directory with no ghost window in it — the shape every entry published
        before 2026-09-21 has — and ``None`` for no ``source_dir`` either.
        """
        job_id = db.enqueue(self.conn, prompt, by, rules_file="treatment", **opts)
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        if state == "published":
            db.transition(self.conn, job_id, "published")
        job = db.get_job(self.conn, job_id)
        strip_path = None
        if strip is not None:
            path = self.strips / f"job-{job_id}" / "strip.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            if strip == "png":
                path.write_bytes(png_bytes(bytes([job_id % 251, 7, 9])))
            elif strip == "empty":
                path.write_bytes(b"")
            strip_path = str(path)
        source_dir = None
        if ghost is not None:
            attempt = self.jobs / str(job_id) / "attempt-1"
            (attempt / ".gate").mkdir(parents=True, exist_ok=True)
            if ghost != "dir":
                (attempt / ".gate" / "ghost.png").write_bytes(
                    b"" if ghost == "empty" else png_bytes(bytes([9, job_id % 251, 3]))
                )
            source_dir = str(attempt)
        entry_id = db.create_entry(
            self.conn,
            job_id,
            source_dir=source_dir,
            state="held" if state == "held" else state,
            prompt=prompt,
            brief="a brief that arrived with the job",
            statement="the model's own account",
            assertions_json=json.dumps(["motion(idle)"]),
            rules_file="treatment",
            planner="gemma4:e4b",
            executor="qwen3-coder:30b-a3b-q4_K_M",
            parent_entry_id=job.parent_entry_id,
            strip_path=strip_path,
            submitted_by=by,
        )
        lineage.record_child(self.conn, entry_id, job)
        return entry_id

    def strip_of(self, entry_id):
        """The bytes on disk for one entry's strip."""
        row = self.conn.execute(
            "SELECT strip_path FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return Path(row["strip_path"]).read_bytes()

    def ghost_of(self, entry_id):
        """The bytes on disk for one entry's ghost frames."""
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return (Path(row["source_dir"]) / ".gate" / "ghost.png").read_bytes()

    def critic_file(self, images="strip ghost", version="critic-v4"):
        """The real prompt file with a header of this test's choosing.

        The body is copied rather than invented so that what these tests switch
        is one header line and nothing else — which is the claim the switch is
        making (auto-mouse.md §6).
        """
        body = lineage.PROMPT_PATH.read_text(encoding="utf-8").split("\n\n", 1)[1]
        head = f"prompt_version: {version}\n"
        if images is not None:
            head += f"images: {images}\n"
        path = self.root_dir / f"critic-{version}-{images!r}.md".replace("/", "-")
        path.write_text(head + "\n" + body, encoding="utf-8")
        return path

    def counts(self):
        """(critiques, jobs) — what a refusal must leave untouched."""
        return (
            self.conn.execute("SELECT COUNT(*) AS c FROM critiques").fetchone()["c"],
            self.conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"],
        )

    def entry_for(self, job_id, state="published"):
        """Finish a spawned job the way the worker would, and return the entry."""
        job = db.get_job(self.conn, job_id)
        db.transition(self.conn, job_id, "executing")
        db.transition(self.conn, job_id, "gating")
        db.transition(self.conn, job_id, "held")
        if state == "published":
            db.transition(self.conn, job_id, "published")
        strip_path = self.strips / f"job-{job_id}" / "strip.png"
        strip_path.parent.mkdir(parents=True, exist_ok=True)
        strip_path.write_bytes(png_bytes(bytes([job_id % 251, 13, 29])))
        entry_id = db.create_entry(
            self.conn,
            job_id,
            state=state,
            prompt=job.prompt,
            parent_entry_id=job.parent_entry_id,
            strip_path=str(strip_path),
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
        self.assertEqual("qwen3-coder:30b-a3b-q4_K_M", job.executor)

    def test_a_child_is_written_by_the_model_that_wrote_its_parent(self):
        """A revision by a different model is a revision of nothing: the two
        model columns are variables in the same way the rules file is."""
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=self.root_entry,
            critique="slow the whole field down",
            critique_by="gemma4:e4b",
            submitted_by="profcarroll",
        )
        job = db.get_job(self.conn, job_id)
        self.assertEqual("gemma4:e4b", job.planner)
        self.assertEqual("qwen3-coder:30b-a3b-q4_K_M", job.executor)

    def test_either_model_can_be_overridden_on_purpose(self):
        job_id = lineage.spawn(
            self.conn,
            parent_entry_id=self.root_entry,
            critique="try the same revision with another hand",
            critique_by="profcarroll",
            submitted_by="profcarroll",
            executor="qwen3.5:9b",
        )
        job = db.get_job(self.conn, job_id)
        self.assertEqual("qwen3.5:9b", job.executor)
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
        self.assertIn("This generation is not published", page)

    def test_a_short_line_has_no_card_that_waits(self):
        self.build_line(depth=1)
        self.assertNotIn("waits for a person", self.page())


class TestCritique(LineageTestCase):
    def test_prompts_critic_md_is_v3_and_leaves_no_placeholders(self):
        self.assertEqual("critic-v3", lineage.prompt_version())
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

    def test_the_rendered_prompt_tells_the_critic_the_image_beats_the_words(self):
        """critic-v3's whole point: the statement is a claim, the strip is not.

        Entry 20 said 'interlocking planes of colour' and is a blue blob; its
        child said 'blue-to-purple gradient' and is a red rectangle. A critic
        that cannot be told which of the two to believe revises the sentence.
        """
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (self.root_entry,)
        ).fetchone()
        rendered = lineage.critique_prompt(row, row["statement"], row["brief"])
        self.assertIn("WHAT THE SKETCH ACTUALLY SHOWS", rendered)
        self.assertIn("four frames", rendered)
        self.assertIn("the image wins", rendered)
        # the new section comes before the words it is telling the critic to
        # distrust, or it is advice arriving after the fact
        self.assertLess(
            rendered.index("WHAT THE SKETCH ACTUALLY SHOWS"),
            rendered.index("THE PROMPT IT WAS MADE FROM"),
        )
        # and the contract the version bump does not touch
        self.assertIn("One sentence. Fewer than forty words.", rendered)

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
        self.assertEqual("critic-v3", result.prompt_version)

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


class TestTheGhostImage(LineageTestCase):
    """Packet 16: the second picture, and the header line that asks for it.

    The gate has played a ghost pointer and written ``ghost.png`` since
    2026-09-21, and the critic may be shown it beside the strip. What is under
    test here is mostly the *switch*: it is the prompt file that decides, never
    the presence of the file on disk, because critiques are comparable only
    inside one prompt version (auto-mouse.md §6).
    """

    def fake_ollama(self, reply="the same field, and this time let one circle "
                                "fall out of phase with the rest."):
        """A stand-in for ``/api/generate`` that keeps the payload it was sent.

        The stub file replays a *reply*; this replaces the call, which is the
        only way to see what went out on the wire.
        """
        seen = {}

        def post(host, payload, timeout):
            seen["host"] = host
            seen["payload"] = payload
            return {"response": reply, "prompt_eval_count": 11, "eval_count": 7}

        return seen, mock.patch.object(lineage, "_post", post)

    def images_sent(self, entry_id, prompt_path=None):
        """The decoded images one critique sent, in order."""
        seen, patched = self.fake_ollama()
        with patched:
            result = lineage.critique(
                self.conn, entry_id, model="gemma4:e4b", prompt_path=prompt_path
            )
        sent = [base64.b64decode(one) for one in seen["payload"]["images"]]
        return sent, result, seen["payload"]

    # -- the reader ------------------------------------------------------

    def test_a_prompt_with_no_images_line_asks_for_the_strip_alone(self):
        self.assertEqual(
            ("strip",), lineage.critic_images(self.critic_file(images=None))
        )

    def test_the_line_is_what_adds_the_ghost(self):
        self.assertEqual(
            ("strip", "ghost"), lineage.critic_images(self.critic_file())
        )
        self.assertEqual(
            ("strip", "ghost"),
            lineage.critic_images(self.critic_file(images="strip, ghost")),
        )

    def test_the_strip_is_first_whatever_the_line_says(self):
        """A version that forgot to name it would be a blind critic again."""
        self.assertEqual(
            ("strip", "ghost"), lineage.critic_images(self.critic_file(images="ghost"))
        )

    def test_a_word_the_reader_does_not_know_is_dropped_not_refused(self):
        self.assertEqual(
            ("strip", "ghost"),
            lineage.critic_images(self.critic_file(images="ghost sonogram")),
        )
        self.assertEqual(
            ("strip",), lineage.critic_images(self.critic_file(images="sonogram"))
        )

    def test_critic_v3_on_disk_asks_for_the_strip_alone(self):
        self.assertEqual(("strip",), lineage.critic_images())

    def test_the_setting_is_not_sent_to_the_model(self):
        path = self.critic_file()
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (self.root_entry,)
        ).fetchone()
        rendered = lineage.critique_prompt(row, row["statement"], row["brief"], path)
        self.assertNotIn("images:", rendered)
        self.assertNotIn("prompt_version:", rendered)
        self.assertTrue(rendered.startswith("You are looking"), rendered[:40])
        # and it is the same prompt the version on disk renders, because the
        # body was copied: one header line is the whole difference
        self.assertEqual(
            lineage.critique_prompt(row, row["statement"], row["brief"]), rendered
        )

    # -- the payload -----------------------------------------------------

    def test_the_two_image_prompt_sends_the_strip_then_the_ghost(self):
        entry = self.publish("a jigsaw nobody touches", ghost="png")
        sent, result, payload = self.images_sent(entry, self.critic_file())
        self.assertEqual([self.strip_of(entry), self.ghost_of(entry)], sent)
        self.assertEqual(
            hashlib.sha256(self.ghost_of(entry)).hexdigest(), result.ghost_sha256
        )
        self.assertTrue(result.ghost_path.endswith("/.gate/ghost.png"), result.ghost_path)
        self.assertEqual("", result.ghost_note)
        # the first image and its provenance are untouched
        self.assertEqual(
            hashlib.sha256(self.strip_of(entry)).hexdigest(), result.strip_sha256
        )
        self.assertEqual("critic-v4", result.prompt_version)

    def test_the_file_on_disk_never_switches_it_on_by_itself(self):
        """The whole of DECIDE-by-prompt-version: comparability, not coverage.

        An entry with a ghost window, critiqued under critic-v3, sends exactly
        what critic-v3 has always sent — or the version would hold sighted and
        half-sighted critiques mixed together.
        """
        entry = self.publish("a jigsaw with a ghost", ghost="png")
        sent, result, payload = self.images_sent(entry)
        self.assertEqual([self.strip_of(entry)], sent)
        self.assertEqual("critic-v3", result.prompt_version)
        self.assertEqual("", result.ghost_path)
        self.assertEqual("", result.ghost_sha256)
        self.assertEqual("", result.ghost_note)
        self.assertEqual(
            [base64.b64encode(self.strip_of(entry)).decode("ascii")],
            payload["images"],
        )

    def test_an_entry_with_no_ghost_window_is_critiqued_over_the_strip_alone(self):
        """910 entries were published before the gate had a ghost window."""
        for shape in (None, "dir", "empty"):
            with self.subTest(shape=shape):
                entry = self.publish(f"an entry gated in {shape} times", ghost=shape)
                sent, result, _ = self.images_sent(entry, self.critic_file())
                self.assertEqual([self.strip_of(entry)], sent)
                self.assertEqual("", result.ghost_sha256)
                self.assertIn("the strip alone", result.ghost_note)

    def test_an_unreadable_strip_is_still_a_refusal_under_the_new_prompt(self):
        entry = self.publish("a sketch with no strip", strip=None, ghost="png")
        with self.assertRaises(lineage.CritiqueRefused):
            lineage.critique(
                self.conn, entry, stub=FIXTURES / "one-sentence.txt",
                prompt_path=self.critic_file(),
            )

    def test_a_replayed_run_still_records_what_it_was_shown(self):
        """The stub replaces the model, not the requirement to have looked."""
        entry = self.publish("a jigsaw replayed", ghost="png")
        result = lineage.critique(
            self.conn, entry, model="gemma4:e4b",
            stub=FIXTURES / "one-sentence.txt", prompt_path=self.critic_file(),
        )
        self.assertEqual(
            hashlib.sha256(self.ghost_of(entry)).hexdigest(), result.ghost_sha256
        )

    def test_the_ghost_is_the_file_the_entry_page_shows(self):
        """One resolver, so a critique cannot be about a frame nobody can find."""
        entry = self.publish("a jigsaw on the page", ghost="png")
        row = self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry,)
        ).fetchone()
        found = lineage.ghost_image(self.conn, row)
        self.assertIsNotNone(found)
        self.assertEqual(
            found, ghostshim.ghost_png(row["source_dir"])
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
        self.assertEqual("critic-v3", document["prompt_version"])
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


class PaidParentTests(unittest.TestCase):
    """A child never inherits a paid model (2026-09-21, job 1252)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-lineage-paid-")
        self.addCleanup(self._tmp.cleanup)
        path = str(Path(self._tmp.name) / "s.db")
        db.init(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        db.set_paid_models(self.conn, ["claude-sonnet-5"])
        job_id = db.enqueue(self.conn, "a field of compass needles", "octocat",
                            planner="claude-sonnet-5", executor="claude-sonnet-5",
                            rules_file="treatment", brief="b",
                            assertions=["motion(idle)"])
        for state in ("executing", "gating", "held", "published"):
            db.transition(self.conn, job_id, state)
        self.parent = db.create_entry(
            self.conn, job_id, state="published", prompt="a field of compass needles",
            brief="b", statement="s", submitted_by="octocat", rules_file="treatment",
            assertions_json=json.dumps(["motion(idle)"]),
            planner="claude-sonnet-5", executor="claude-sonnet-5",
        )

    def test_the_idle_critics_child_is_made_on_this_node(self):
        child = lineage.spawn(
            self.conn, parent_entry_id=self.parent, critique="slow it down",
            critique_by="gemma4:e4b", submitted_by="octocat",
        )
        job = db.get_job(self.conn, child)
        self.assertIsNone(job.planner)
        self.assertIsNone(job.executor)
        self.assertEqual("treatment", job.rules_file)  # the A/B variable is kept

    def test_a_paid_model_named_on_purpose_is_still_taken(self):
        child = lineage.spawn(
            self.conn, parent_entry_id=self.parent, critique="slow it down",
            critique_by="claude-sonnet-5", submitted_by="octocat",
            planner="claude-sonnet-5",
        )
        self.assertEqual("claude-sonnet-5", db.get_job(self.conn, child).planner)
