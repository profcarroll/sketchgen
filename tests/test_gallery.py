"""Unit tests for sketchgen.gallery — the static generator (packet 3.1).

Run:  python3 -m unittest discover -s tests -v

No network, no model, no browser, no push. Three entries are built in a temp
database exactly the way the worker builds them — two published with different
rules files and executors, one failed-kept — each with a real attempt directory
on disk: the sketch copied from the gate's own fixtures, a statement, and a
``.gate/`` holding PNG bytes and a ``report.json`` in sketch_gate.py's schema.

The tests that matter most are the two the gallery's public-by-construction
problem forces: every page must parse, and nothing that could carry personal
data may survive a render.
"""

import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import gallery  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"
SKETCHES = Path(__file__).resolve().parent / "fixtures" / "sketches"

#: One valid 1x1 PNG. The packet allows any PNG bytes; these are the smallest
#: that a browser and `file(1)` both accept, and they are checked into no repo.
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

STATEMENT_ONE = (
    "I built a field of circles & let each one drift along its own noise path.\n"
    "The <canvas> fills the window; a click scatters them from the cursor and "
    "they drift back."
)
STATEMENT_TWO = (
    "This one holds still on purpose. It draws once, calls noLoop(), and says so "
    "in its own API rather than asking you to take my word for it."
)

#: Every key spec §7 asks for, plus the four the packet adds. Written out here
#: rather than imported so that the test is a check and not a restatement.
SPEC_7_KEYS = {
    "prompt",
    "brief",
    "statement",
    "submitted_by",
    "planner",
    "planner_prompt_version",
    "executor",
    "executor_prompt_version",
    "rules_file",
    "assertions",
    "gate",
    "attempts",
    "prompt_tokens",
    "completion_tokens",
    "wall_s",
    "shape",
    "seed",
    "lineage",
    "source",
    "job_id",
    "created_utc",
    "last_error",
    # the four the packet names
    "entry_id",
    "state",
    "published_utc",
    "publish_commit",
    "licence",
    "attribution",
}


# ---------------------------------------------------------------------------
# A parser strict about the tags the packet names
# ---------------------------------------------------------------------------


class Balance(HTMLParser):
    """Balanced-tag check over a fixed tag list, LIFO, with positions."""

    TAGS = ("div", "section", "table", "tr", "td", "a", "p", "iframe", "script")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.TAGS:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag):
        if tag not in self.TAGS:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> at {self.getpos()} closes nothing")
            return
        open_tag, where = self.stack[-1]
        if open_tag != tag:
            self.errors.append(
                f"</{tag}> at {self.getpos()} closes <{open_tag}> opened at {where}"
            )
            return
        self.stack.pop()

    def finish(self) -> list[str]:
        self.close()
        return self.errors + [
            f"<{tag}> opened at {where} is never closed" for tag, where in self.stack
        ]


def balance_errors(path: Path) -> list[str]:
    parser = Balance()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser.finish()


class Elements(HTMLParser):
    """Every start tag in a page as (name, attributes)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, {name: (value or "") for name, value in attrs}))


def elements(text: str) -> list[tuple[str, dict[str, str]]]:
    """The page's tags, with attribute values unescaped the way a browser
    unescapes them before any script ever sees them."""
    parser = Elements()
    parser.feed(text)
    parser.close()
    return parser.tags


def search_boxes(text: str) -> list[dict[str, str]]:
    return [
        attrs
        for tag, attrs in elements(text)
        if tag == "input" and attrs.get("type") == "search" and "data-search" in attrs
    ]


def card_search(text: str) -> dict[str, str]:
    """Each card's data-search attribute, by entry id."""
    return {
        attrs["data-entry"]: attrs.get("data-search", "")
        for tag, attrs in elements(text)
        if tag == "div" and "card" in attrs.get("class", "").split()
    }


# ---------------------------------------------------------------------------
# The fixture database
# ---------------------------------------------------------------------------


def gate_report(sketch_dir: Path, gate_dir: Path, *, exit_code: int, assertions):
    """One report.json in sketch_gate.py's real schema."""
    return {
        "sketch_dir": str(sketch_dir),
        "seed": 1,
        "started_utc": "2026-09-14T03:34:52Z",
        "chromium": "chromium-1234/chrome-linux/headless_shell",
        "timings": {"launch_s": 0.057, "load_s": 0.213, "total_s": 1.42},
        "checks": {
            "console_clean": exit_code == 0,
            "is_looping": True,
            "frame_advancing": exit_code == 0,
            "sound_lib_ok": None,
            "audio_context_running": None,
        },
        "assertions": {
            word: {
                "pass": exit_code == 0,
                "detail": "3184 pixels changed between t0 and t20",
            }
            for word in assertions
        },
        "notes": [
            "frameCount at load 1, at frame 84 84, at end 120",
            "isLooping() read at the end of the idle window",
        ],
        "console": [],
        "artefacts": {
            "png": str(gate_dir / "gate.png"),
            "strip": str(gate_dir / "strip.png"),
            "log": str(gate_dir / "console.log"),
        },
        "exit": exit_code,
    }


def write_attempt(jobs: Path, job_id: int, n: int, sketch: str, statement: str,
                  *, exit_code: int, assertions) -> Path:
    """One attempt directory in the worker's layout, gate output included."""
    source = SKETCHES / sketch
    attempt = jobs / str(job_id) / f"attempt-{n}"
    (attempt / ".gate").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / "sketch.js", attempt / "sketch.js")
    shutil.copyfile(source / "index.html", attempt / "index.html")
    (attempt / "statement.md").write_text(statement + "\n", encoding="utf-8")
    (attempt / "result.json").write_text(
        json.dumps({"ok": exit_code == 0, "model": "stub", "seed": 1}, indent=2) + "\n",
        encoding="utf-8",
    )
    gate = attempt / ".gate"
    (gate / "gate.png").write_bytes(PNG_BYTES)
    (gate / "strip.png").write_bytes(PNG_BYTES)
    (gate / "console.log").write_text("", encoding="utf-8")
    (gate / "report.json").write_text(
        json.dumps(gate_report(attempt, gate, exit_code=exit_code,
                               assertions=assertions), indent=2) + "\n",
        encoding="utf-8",
    )
    return attempt


def build_db(tmp: Path):
    """Three entries, the way the worker leaves them. Returns (conn, ids)."""
    database = tmp / "sketchgen.db"
    db.init(database)
    conn = db.connect(database)
    jobs = tmp / "jobs"

    # 1 — published, control rules, the 30B, a root prompt.
    job1 = db.enqueue(
        conn,
        "a field of sixty circles drifting on their own noise paths over a dark ground",
        "profcarroll",
        brief="Sixty circles, one warm hue, each on its own noise path; a click "
              "scatters them from the cursor and they drift back.",
        assertions=["motion(idle)", "responds(click)"],
        planner="gemma4:e4b",
        executor="qwen3-coder:30b-a3b-q4_K_M",
        rules_file="control",
    )
    attempt1 = write_attempt(jobs, job1, 1, "good-motion", STATEMENT_ONE,
                             exit_code=0, assertions=["motion(idle)", "responds(click)"])
    db.add_attempt(
        conn, job1, 1, model="qwen3-coder:30b-a3b-q4_K_M", rules_file="control",
        prompt_version="executor.md v1", prompt_tokens=1066, completion_tokens=628,
        wall_s=67.9, source_dir=str(attempt1), gate_exit=0,
        gate_report_path=str(attempt1 / ".gate" / "report.json"),
        statement=STATEMENT_ONE,
    )
    db.transition(conn, job1, "executing")
    db.transition(conn, job1, "gating")
    db.transition(conn, job1, "held")
    db.transition(conn, job1, "published")
    entry1 = db.create_entry(
        conn, job1, state="published",
        prompt="a field of sixty circles drifting on their own noise paths over a dark ground",
        brief="Sixty circles, one warm hue, each on its own noise path; a click "
              "scatters them from the cursor and they drift back.",
        statement=STATEMENT_ONE, planner="gemma4:e4b",
        planner_prompt_version="planner.md v1",
        executor="qwen3-coder:30b-a3b-q4_K_M",
        executor_prompt_version="executor.md v1", rules_file="control",
        assertions_json=json.dumps(["motion(idle)", "responds(click)"]), attempts=1,
        prompt_tokens=1066, completion_tokens=628, wall_s=67.9,
        shape="VM.Standard.A1.Flex 16/96", seed=1, submitted_by="profcarroll",
        source_dir=str(attempt1),
        strip_path=str(attempt1 / ".gate" / "strip.png"),
        png_path=str(attempt1 / ".gate" / "gate.png"),
        published_utc="2026-09-14T04:02:11Z", publish_commit=None,
    )

    # 2 — published, treatment rules, a different executor, child of entry 1.
    job2 = db.enqueue(
        conn, "the same field, but it holds still and earns it", "astudent",
        brief="One frame, drawn once, then noLoop(). Stillness declared in the API.",
        assertions=["no_motion"], planner="gemma4:e4b", executor="qwen3.5:4b",
        rules_file="treatment", parent_entry_id=entry1,
    )
    attempt2 = write_attempt(jobs, job2, 1, "good-static-noloop", STATEMENT_TWO,
                             exit_code=0, assertions=["no_motion"])
    db.add_attempt(
        conn, job2, 1, model="qwen3.5:4b", rules_file="treatment",
        prompt_version="executor.md v1", prompt_tokens=1095, completion_tokens=1522,
        wall_s=130.9, source_dir=str(attempt2), gate_exit=0,
        gate_report_path=str(attempt2 / ".gate" / "report.json"),
        statement=STATEMENT_TWO,
    )
    db.transition(conn, job2, "executing")
    db.transition(conn, job2, "gating")
    db.transition(conn, job2, "held")
    db.transition(conn, job2, "published")
    entry2 = db.create_entry(
        conn, job2, state="published",
        prompt="the same field, but it holds still and earns it",
        brief="One frame, drawn once, then noLoop(). Stillness declared in the API.",
        statement=STATEMENT_TWO, planner="gemma4:e4b",
        planner_prompt_version="planner.md v1", executor="qwen3.5:4b",
        executor_prompt_version="executor.md v1", rules_file="treatment",
        assertions_json=json.dumps(["no_motion"]), attempts=1, prompt_tokens=1095,
        completion_tokens=1522, wall_s=130.9, shape="VM.Standard.A1.Flex 16/96",
        seed=1, submitted_by="astudent", parent_entry_id=entry1,
        source_dir=str(attempt2),
        strip_path=str(attempt2 / ".gate" / "strip.png"),
        png_path=str(attempt2 / ".gate" / "gate.png"),
        published_utc="2026-09-14T04:11:47Z", publish_commit=None,
    )
    db.add_lineage(
        conn, entry2, entry1, generation=2, critique_by="gemma4:e4b",
        critique="The motion is doing the work the colour should do; try it still.",
    )

    # 3 — failed, kept, two attempts.
    job3 = db.enqueue(
        conn, "a sketch whose microphone drives the whole composition", "astudent",
        brief="Bass drives the ground, treble the foreground.",
        assertions=["responds(audio)"], planner="gemma4:e4b",
        executor="qwen3-coder:30b-a3b-q4_K_M", rules_file="control", max_attempts=2,
    )
    failed_dirs = []
    for n in (1, 2):
        attempt = write_attempt(jobs, job3, n, "good-motion", STATEMENT_ONE,
                                exit_code=1, assertions=["responds(audio)"])
        failed_dirs.append(attempt)
        db.add_attempt(
            conn, job3, n, model="qwen3-coder:30b-a3b-q4_K_M", rules_file="control",
            prompt_version="executor.md v1", prompt_tokens=1100, completion_tokens=700,
            wall_s=71.2, source_dir=str(attempt), gate_exit=1,
            gate_report_path=str(attempt / ".gate" / "report.json"),
            evidence="assertion responds(audio) failed: the AudioContext is suspended",
            statement=STATEMENT_ONE,
        )
    db.transition(conn, job3, "executing")
    db.transition(conn, job3, "gating")
    db.transition(
        conn, job3, "failed",
        last_error="assertion responds(audio) failed: the AudioContext is suspended",
    )
    entry3 = db.create_entry(
        conn, job3, state="failed-kept",
        prompt="a sketch whose microphone drives the whole composition",
        brief="Bass drives the ground, treble the foreground.",
        statement=STATEMENT_ONE, planner="gemma4:e4b",
        planner_prompt_version="planner.md v1",
        executor="qwen3-coder:30b-a3b-q4_K_M",
        executor_prompt_version="executor.md v1", rules_file="control",
        assertions_json=json.dumps(["responds(audio)"]), attempts=2,
        prompt_tokens=2200, completion_tokens=1400, wall_s=142.4,
        shape="VM.Standard.A1.Flex 16/96", seed=1, submitted_by="astudent",
        # kept by the worker, then published by a person: that stamp is what
        # puts it on the rejections page
        published_utc="2026-09-14T04:20:33Z", publish_commit=None,
        source_dir=str(failed_dirs[-1]),
        strip_path=str(failed_dirs[-1] / ".gate" / "strip.png"),
        png_path=str(failed_dirs[-1] / ".gate" / "gate.png"),
    )
    return conn, (entry1, entry2, entry3)


class GalleryTestCase(unittest.TestCase):
    """One temp database, one temp gallery checkout, per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-gallery-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = build_db(self.tmp)
        self.addCleanup(self.conn.close)
        self.dest = self.tmp / "site"
        self.dest.mkdir()
        self.config = gallery.Config(
            write_path="https://write.example.invalid/api",
            gallery_url="https://profcarroll.github.io/sketchgen-gallery/",
        )

    def render(self):
        return gallery.render_all(self.conn, self.dest, self.config)

    def pages(self):
        return sorted(self.dest.rglob("*.html"))


# ---------------------------------------------------------------------------
# The tests the packet names
# ---------------------------------------------------------------------------


class TreeTests(GalleryTestCase):

    def test_render_all_writes_the_expected_tree(self):
        self.render()
        one, two, three = self.ids
        expected = [
            "assets/gallery.css",
            "assets/gallery.js",
            "compare.html",
            "config.json",
            f"e/{one}/gate.png",
            f"e/{one}/index.html",
            f"e/{one}/meta.json",
            f"e/{one}/sketch/index.html",
            f"e/{one}/sketch/sketch.js",
            f"e/{one}/statement.md",
            f"e/{one}/strip.png",
            f"e/{two}/index.html",
            f"e/{three}/index.html",
            "rejections.html",
            "index.html",
            f"lines/{one}.html",
        ]
        for relative in expected:
            with self.subTest(path=relative):
                self.assertTrue((self.dest / relative).is_file(), relative)

    def test_the_sketch_is_copied_verbatim(self):
        self.render()
        one = self.ids[0]
        for name in ("sketch.js", "index.html"):
            self.assertEqual(
                (self.dest / "e" / str(one) / "sketch" / name).read_bytes(),
                (SKETCHES / "good-motion" / name).read_bytes(),
            )

    def test_held_entries_are_not_public(self):
        job = db.enqueue(self.conn, "a held prompt", "profcarroll")
        held = db.create_entry(self.conn, job, state="held", prompt="a held prompt")
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(self.conn, held, self.dest, self.config)
        self.assertFalse((self.dest / "e" / str(held)).exists())

    def test_a_kept_rejection_nobody_published_is_on_no_page(self):
        # The worker keeps it the moment the gate gives up; a person has not
        # published it (spec §9). Until then it is as private as a held entry:
        # no directory, no card, no 404 behind a card.
        job = db.enqueue(self.conn, "a rejection nobody has published", "profcarroll")
        kept = db.create_entry(
            self.conn, job, state="failed-kept",
            prompt="a rejection nobody has published",
        )
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(self.conn, kept, self.dest, self.config)
        self.render()
        self.assertFalse((self.dest / "e" / str(kept)).exists())
        failed = (self.dest / "rejections.html").read_text(encoding="utf-8")
        self.assertNotIn(f'data-entry="{kept}"', failed)
        self.assertNotIn(f"e/{kept}/", failed)
        # the publisher, rendering ahead of its push, is still admitted
        out = gallery.render_entry(self.conn, kept, self.dest, self.config, publishing=True)
        self.assertTrue((out / "index.html").exists())

    def test_publisher_may_render_a_held_entry_as_published(self):
        # The publisher renders BEFORE the push that flips the row, so with
        # publishing=True a held entry is admitted and written as published.
        one = self.ids[0] if hasattr(self, "ids") else 1
        self.conn.execute(
            "UPDATE entries SET state='held', published_utc=NULL, publish_commit=NULL "
            "WHERE id=?", (one,))
        self.conn.commit()
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(self.conn, one, self.dest, self.config)
        out = gallery.render_entry(self.conn, one, self.dest, self.config, publishing=True)
        meta = json.loads((out / "meta.json").read_text())
        self.assertEqual(meta["state"], "published")
        self.assertIsNone(meta["publish_commit"])
        self.assertTrue((out / "index.html").exists())


class ParseTests(GalleryTestCase):

    def test_every_page_parses_with_balanced_tags(self):
        self.render()
        pages = self.pages()
        self.assertGreaterEqual(len(pages), 6)
        for page in pages:
            with self.subTest(page=str(page.relative_to(self.dest))):
                self.assertEqual(balance_errors(page), [])


class EntryPageTests(GalleryTestCase):

    def setUp(self):
        super().setUp()
        self.render()
        self.entry_id = self.ids[0]
        self.page = (self.dest / "e" / str(self.entry_id) / "index.html").read_text(
            encoding="utf-8"
        )

    def test_statement_appears_verbatim_and_escaped(self):
        for line in STATEMENT_ONE.split("\n"):
            self.assertIn(html.escape(line, quote=True), self.page)
        self.assertNotIn("<canvas>", self.page)
        self.assertIn("qwen3-coder:30b-a3b-q4_K_M, unedited", self.page)

    def test_the_provenance_rows_are_there(self):
        for label in (
            "Prompt", "Brief", "Statement", "Submitted by", "Planner", "Executor",
            "Rules file", "Assertions", "Gate", "Attempts", "Prompt tokens",
            "Completion tokens", "Wall seconds", "Node shape", "Seed", "Lineage",
            "Source", "Created (UTC)", "Published (UTC)", "Publish commit",
            "Licence", "Attribution",
        ):
            with self.subTest(row=label):
                self.assertIn(f'<th scope="row">{label}</th>', self.page)

    def test_the_page_is_the_entry_not_the_sketch(self):
        self.assertIn('<iframe class="sketch" src="sketch/"', self.page)
        self.assertIn("seed 1", self.page)
        self.assertIn(
            "Prompt by profcarroll. Planned by gemma4:e4b, written by "
            "qwen3-coder:30b-a3b-q4_K_M, passed the gate on attempt 1.",
            self.page,
        )
        self.assertIn(
            "https://github.com/profcarroll/sketchgen-gallery/tree/main/e/"
            f"{self.entry_id}",
            self.page,
        )
        self.assertIn("CC BY 4.0", self.page)
        self.assertIn(f"compare.html?a={self.entry_id}", self.page)
        self.assertIn("ask the operator", self.page.lower())

    def test_no_score_is_invented(self):
        self.assertEqual(self.page.count("no pairs yet"), 2)

    def test_counts_are_left_to_the_write_path(self):
        self.assertIn('<span class="count" data-count="views">—</span>', self.page)
        self.assertIn('data-like=', self.page)

    def test_lineage_links_parent_and_children(self):
        child = (self.dest / "e" / str(self.ids[1]) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'href="../{self.ids[0]}/"', child)
        self.assertIn(f'href="../{self.ids[1]}/"', self.page)
        self.assertIn(f"lines/{self.ids[0]}.html", self.page)


class MetaTests(GalleryTestCase):

    def test_meta_has_every_spec_seven_key(self):
        self.render()
        for entry_id in self.ids:
            with self.subTest(entry=entry_id):
                meta = json.loads(
                    (self.dest / "e" / str(entry_id) / "meta.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(set(meta), SPEC_7_KEYS)
                self.assertEqual(meta["entry_id"], entry_id)
                self.assertEqual(meta["licence"], "CC BY 4.0")
                self.assertEqual(meta["shape"], "VM.Standard.A1.Flex 16/96")
                self.assertIsNone(meta["publish_commit"])
                self.assertTrue(meta["gate"])
                self.assertEqual(
                    meta["source"]["repository"],
                    "https://github.com/profcarroll/sketchgen-gallery/tree/main/e/"
                    f"{entry_id}",
                )

    def test_the_failed_entry_keeps_its_reason(self):
        self.render()
        meta = json.loads(
            (self.dest / "e" / str(self.ids[2]) / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["state"], "failed-kept")
        self.assertEqual(meta["attempts"], 2)
        self.assertIn("AudioContext is suspended", meta["last_error"])
        self.assertEqual(len(meta["gate"]), 2)


class IndexTests(GalleryTestCase):

    def setUp(self):
        super().setUp()
        self.render()
        self.index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.failed = (self.dest / "rejections.html").read_text(encoding="utf-8")

    def test_the_failed_entry_is_in_failed_html_only(self):
        one, two, three = self.ids
        self.assertIn(f'data-entry="{one}"', self.index)
        self.assertIn(f'data-entry="{two}"', self.index)
        self.assertNotIn(f'data-entry="{three}"', self.index)
        self.assertIn(f'data-entry="{three}"', self.failed)
        self.assertNotIn(f'data-entry="{one}"', self.failed)
        self.assertIn("REJECTED", self.failed)
        self.assertIn("AudioContext is suspended", self.failed)

    def test_the_cards_carry_what_the_grid_shows(self):
        one = self.ids[0]
        self.assertIn(f'src="e/{one}/strip.png"', self.index)
        self.assertIn('data-rules="control"', self.index)
        self.assertIn('data-executor="qwen3.5:4b"', self.index)
        self.assertIn("profcarroll", self.index)
        self.assertIn('data-count="views"', self.index)

    def test_the_filters_are_plain_links(self):
        for href in (
            'href="index.html?rules=control"',
            'href="index.html?rules=treatment"',
            'href="index.html?executor=qwen3.5:4b"',
        ):
            with self.subTest(href=href):
                self.assertIn(href, self.index)

    def test_compare_is_a_shell_with_both_questions(self):
        compare = (self.dest / "compare.html").read_text(encoding="utf-8")
        self.assertIn("Which is closer to its brief?", compare)
        self.assertIn("Which would you rather look at?", compare)
        self.assertIn('data-vote="tie"', compare)
        self.assertIn('data-reveal hidden', compare)
        embedded = compare.split('type="application/json">')[1].split("</script>")[0]
        entries = json.loads(embedded.replace("<\\/", "</"))
        # every public entry, the kept rejection included: its own page links
        # here with ?a=<itself> and the page has to know the id to honour it
        self.assertEqual([entry["id"] for entry in entries], list(self.ids))

    def test_compare_can_run_either_sketch_in_place(self):
        """The strip is clickable and the JSON says where the sketch lives.

        gallery.js turns each thumbnail into a play button and swaps in an
        iframe of ``<href>sketch/`` — one at a time. Two things have to hold in
        the page the generator writes for that to be possible at all: the
        caption the script speaks through is in the shell, and every entry in
        the embedded JSON carries the ``href`` the iframe URL is built from.
        """
        compare = (self.dest / "compare.html").read_text(encoding="utf-8")
        self.assertEqual(2, compare.count("data-run-note"))
        # one caption per side, next to that side's thumbnail
        for side in ("A", "B"):
            block = compare.split(f'data-side="{side}"')[1].split("</section>")[0]
            self.assertIn("data-thumb", block)
            self.assertIn("data-run-note", block)
        embedded = compare.split('type="application/json">')[1].split("</script>")[0]
        entries = json.loads(embedded.replace("<\\/", "</"))
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(entry=entry["id"]):
                self.assertEqual(f"e/{entry['id']}/", entry["href"])
                self.assertTrue(
                    (self.dest / "e" / str(entry["id"]) / "sketch" / "index.html").is_file()
                )

    def test_config_json_carries_the_urls(self):
        config = json.loads((self.dest / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["write_path"], "https://write.example.invalid/api")
        self.assertEqual(
            config["gallery_url"], "https://profcarroll.github.io/sketchgen-gallery/"
        )
        self.assertEqual(
            config["repository"], "https://github.com/profcarroll/sketchgen-gallery"
        )

    def test_the_line_page_shows_the_whole_line(self):
        line = (self.dest / "lines" / f"{self.ids[0]}.html").read_text(encoding="utf-8")
        self.assertIn(f'href="../e/{self.ids[0]}/"', line)
        self.assertIn(f'href="../e/{self.ids[1]}/"', line)
        self.assertNotIn(f'href="../e/{self.ids[2]}/"', line)

    def test_the_line_page_carries_the_critique_and_who_wrote_it(self):
        line = (self.dest / "lines" / f"{self.ids[0]}.html").read_text(encoding="utf-8")
        # packet 5.3: the critique that produced a generation sits above it
        self.assertIn("Revise: The motion is doing the work", line)
        self.assertIn("critique by gemma4:e4b", line)
        # and the root, which nobody critiqued into being, has no such line
        self.assertEqual(1, line.count('class="critique"'))


class GridOrderTests(GalleryTestCase):
    """The grid's order, its sort control and its folded-away filters.

    The order is in the HTML and not only in the script: the site is static,
    someone may read it with JavaScript off, and the first paint should not
    have to be rearranged before it is right.
    """

    def setUp(self):
        super().setUp()
        self.render()
        self.index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.failed = (self.dest / "rejections.html").read_text(encoding="utf-8")

    def test_the_index_is_newest_first(self):
        one, two, _ = self.ids
        # entry two was published nine minutes after entry one
        self.assertLess(
            self.index.index(f'data-entry="{two}"'),
            self.index.index(f'data-entry="{one}"'),
        )

    def test_the_rejections_page_is_newest_first(self):
        # one card is no order at all, so this page needs a second rejection
        job = db.enqueue(self.conn, "a rejection published later", "profcarroll")
        later = db.create_entry(
            self.conn, job, state="failed-kept",
            prompt="a rejection published later",
            published_utc="2026-09-14T05:31:02Z",
        )
        dest = self.tmp / "again"
        dest.mkdir()
        gallery.render_index(self.conn, dest, self.config)
        page = (dest / "rejections.html").read_text(encoding="utf-8")
        self.assertLess(
            page.index(f'data-entry="{later}"'),
            page.index(f'data-entry="{self.ids[2]}"'),
        )

    def test_every_card_carries_the_stamp_a_sort_needs(self):
        self.assertIn('data-published="2026-09-14T04:11:47Z"', self.index)
        self.assertIn('data-published="2026-09-14T04:02:11Z"', self.index)
        self.assertIn('data-published="2026-09-14T04:20:33Z"', self.failed)
        for name, page in (("index", self.index), ("rejections", self.failed)):
            with self.subTest(page=name):
                self.assertEqual(page.count('class="card"'), page.count("data-published="))

    def test_the_grid_offers_the_four_sorts(self):
        for name, page in (("index", self.index), ("rejections", self.failed)):
            with self.subTest(page=name):
                for sort in ("newest", "oldest", "random", "liked"):
                    self.assertIn(
                        f'<button type="button" class="sort" data-sort="{sort}"', page
                    )
                self.assertEqual(page.count('class="sort"'), 4)
                # the order the HTML is already in is the one marked current
                self.assertIn('data-sort="newest" aria-current="true"', page)

    def test_the_filters_are_a_details_that_starts_closed(self):
        for name, page in (("index", self.index), ("rejections", self.failed)):
            with self.subTest(page=name):
                self.assertIn('<details class="filters">', page)
                self.assertIn('<summary class="filter-label">Filter</summary>', page)
                self.assertNotIn('<div class="filters">', page)
                self.assertNotIn("open", page.split("<details")[1].split(">")[0])

    def test_the_line_page_is_still_oldest_first(self):
        # the grid reversed; the line pages did not, because a critique has to
        # be read before the generation it produced
        one, two, _ = self.ids
        line = (self.dest / "lines" / f"{one}.html").read_text(encoding="utf-8")
        self.assertLess(line.index(f'href="../e/{one}/"'), line.index(f'href="../e/{two}/"'))

    def test_compare_still_embeds_the_entries_oldest_first(self):
        compare = (self.dest / "compare.html").read_text(encoding="utf-8")
        embedded = compare.split('type="application/json">')[1].split("</script>")[0]
        entries = json.loads(embedded.replace("<\\/", "</"))
        # the grid reversed and this did not: published oldest first, then the
        # kept rejections, which is the order _public_rows walks
        self.assertEqual([entry["id"] for entry in entries], list(self.ids))


class CompareRejectionTests(GalleryTestCase):
    """A kept rejection is comparable, and only where a person asks for it.

    Every entry page — the rejections included — links to
    ``compare.html?a=<itself>``. While that page was built from the published
    rows only, a rejection's link arrived at a page that had never heard of the
    id and quietly showed some other pair: the symptom was "judgment pairs
    loads wrong pair". The fix widens the page's own JSON to the public set,
    and stops there: the balanced spec §9 offer is still published-only, so a
    rejection is compared when somebody asks and never by rotation.
    """

    def setUp(self):
        super().setUp()
        self.render()
        self.compare = (self.dest / "compare.html").read_text(encoding="utf-8")

    def entries(self, page=None):
        block = (page or self.compare).split('type="application/json">')[1]
        return json.loads(block.split("</script>")[0].replace("<\\/", "</"))

    def test_the_compare_page_carries_the_kept_rejection_and_its_state(self):
        three = self.ids[2]
        by_id = {entry["id"]: entry for entry in self.entries()}
        self.assertIn(three, by_id, "the rejection its own link names is missing")
        self.assertEqual(by_id[three]["state"], "failed-kept")
        self.assertEqual(by_id[three]["href"], f"e/{three}/")
        # and the state is on every entry, not only the interesting one
        for entry_id in self.ids[:2]:
            with self.subTest(entry=entry_id):
                self.assertEqual(by_id[entry_id]["state"], "published")

    def test_a_kept_rejection_nobody_published_is_not_comparable(self):
        # Same rule as every other page: the worker keeping it is not a person
        # publishing it, and until someone does there is no e/<id>/ behind it.
        job = db.enqueue(self.conn, "a rejection nobody has published", "profcarroll")
        kept = db.create_entry(
            self.conn, job, state="failed-kept",
            prompt="a rejection nobody has published",
        )
        dest = self.tmp / "again"
        dest.mkdir()
        gallery.render_index(self.conn, dest, self.config)
        page = (dest / "compare.html").read_text(encoding="utf-8")
        self.assertNotIn(kept, [entry["id"] for entry in self.entries(page)])
        self.assertIn(self.ids[2], [entry["id"] for entry in self.entries(page)])

    def test_the_balanced_offer_stays_published_only(self):
        """spec §9 is control against treatment; a rejection is on neither arm.

        The offer is what the page hands a visitor who arrives with no
        parameters, so a rejection in it would be a rejection put in front of
        people who never asked for one.
        """
        three = self.ids[2]
        offered = gallery._offered_pairs(self.conn)
        self.assertTrue(offered, "two published entries should offer a pair")
        for pair in offered:
            with self.subTest(pair=pair):
                self.assertNotIn(three, (pair["a"], pair["b"]))
        baked = json.loads((self.dest / "pairs.json").read_text(encoding="utf-8"))
        for pair in baked["pairs"]:
            with self.subTest(pair=pair):
                self.assertNotIn(three, (pair["a"], pair["b"]))

    def test_the_reveal_holds_the_hook_the_rejection_note_is_written_into(self):
        """Inside [data-reveal], above the verdicts, and empty in the HTML.

        Spec §5 blinds the human until both answers are in, so the page the
        generator writes must not say anywhere that a side was rejected; the
        one line per rejected side is written by gallery.js into this hook,
        which is hidden with the rest of the reveal until the second vote.
        """
        reveal = self.compare.split('<section class="reveal"')[1]
        reveal = reveal.split("</section>")[0]
        self.assertIn("data-rejected-note", reveal)
        self.assertLess(
            reveal.index("data-rejected-note"), reveal.index("data-agent-verdicts")
        )
        self.assertIn('data-reveal hidden', self.compare)
        # nothing outside the reveal, and no verdict baked into the HTML
        self.assertEqual(1, self.compare.count("data-rejected-note"))
        self.assertNotIn("rejected by the gate", self.compare)


class GridSearchTests(GalleryTestCase):
    """The grid's search box, and the one attribute it matches cards on.

    The box is gallery.js's; what the generator owes it is data-search on
    every card — and that attribute is the only place a brief reaches a grid
    page, which is the point of having one.
    """

    #: a word from each fixture entry's brief that its own prompt does not use
    BRIEF_ONLY = ("warm", "noloop", "treble")

    def setUp(self):
        super().setUp()
        self.render()
        self.index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.failed = (self.dest / "rejections.html").read_text(encoding="utf-8")

    def test_both_grid_pages_offer_one_search_box(self):
        for name, page in (("index.html", self.index), ("rejections.html", self.failed)):
            with self.subTest(page=name):
                boxes = search_boxes(page)
                self.assertEqual(len(boxes), 1)
                self.assertEqual(boxes[0]["name"], "q")
                self.assertEqual(boxes[0].get("placeholder"), "search prompts")
                counts = [
                    attrs for _, attrs in elements(page) if "data-search-count" in attrs
                ]
                self.assertEqual(len(counts), 1)
                self.assertIn("hidden", counts[0])
                # with no script, Enter reloads the page it is already on and
                # that page shows everything: harmless, which is the whole ask
                forms = [
                    attrs for tag, attrs in elements(page)
                    if tag == "form" and attrs.get("role") == "search"
                ]
                self.assertEqual(len(forms), 1)
                self.assertEqual(forms[0]["action"], name)

    def test_every_card_carries_what_the_search_reads(self):
        pages = {self.ids[0]: self.index, self.ids[1]: self.index, self.ids[2]: self.failed}
        executors = {
            self.ids[0]: "qwen3-coder:30b-a3b-q4_K_M",
            self.ids[1]: "qwen3.5:4b",
            self.ids[2]: "qwen3-coder:30b-a3b-q4_K_M",
        }
        for entry_id, word in zip(self.ids, self.BRIEF_ONLY):
            with self.subTest(entry=entry_id):
                value = card_search(pages[entry_id])[str(entry_id)]
                # lowercased once by the generator, so the script only has to
                # lowercase the query
                self.assertEqual(value, value.lower())
                self.assertEqual(value, " ".join(value.split()))
                # the brief is in there and the card does not otherwise show it
                self.assertIn(word, value)
                prompt = str(
                    self.conn.execute(
                        "SELECT prompt FROM entries WHERE id = ?", (entry_id,)
                    ).fetchone()[0]
                ).lower()
                self.assertNotIn(word, prompt)
                self.assertIn(executors[entry_id].lower(), value)
                self.assertIn(f"entry {entry_id}", value)
                self.assertIn(f"#{entry_id}", value)
        # the statement is not in it: it is paragraphs, and every card would
        # have to carry them
        self.assertNotIn("fills the window", card_search(self.index)[str(self.ids[0])])

    def test_a_quote_in_a_prompt_does_not_break_the_attribute(self):
        rough = 'a "quoted" prompt with <canvas> & one ampersand'
        self.conn.execute(
            "UPDATE entries SET prompt = ? WHERE id = ?", (rough, self.ids[0])
        )
        self.conn.commit()
        dest = self.tmp / "escaped"
        dest.mkdir()
        gallery.render_index(self.conn, dest, self.config)
        page = (dest / "index.html").read_text(encoding="utf-8")
        # in the file the dangerous characters are entities, so the tag still
        # ends where it says it ends
        self.assertIn("&quot;quoted&quot;", page)
        self.assertIn("&lt;canvas&gt;", page)
        self.assertEqual(balance_errors(dest / "index.html"), [])
        # and what a browser hands the script is the text itself
        self.assertIn(rough.lower(), card_search(page)[str(self.ids[0])])


class GuardTests(GalleryTestCase):

    def test_an_email_in_a_statement_is_refused_and_nothing_is_left(self):
        self.conn.execute(
            "UPDATE entries SET statement = ? WHERE id = ?",
            ("Write to nobody@example.invalid with notes.", self.ids[0]),
        )
        with self.assertRaises(gallery.Unsafe) as caught:
            gallery.render_entry(self.conn, self.ids[0], self.dest, self.config)
        self.assertIn("email-shaped string", str(caught.exception))
        self.assertFalse((self.dest / "e" / str(self.ids[0])).exists())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_a_hostname_in_a_brief_is_refused(self):
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("Rendered on instance-0000.example.invalid for now.", self.ids[1]),
        )
        with self.assertRaises(gallery.Unsafe):
            gallery.render_entry(self.conn, self.ids[1], self.dest, self.config)
        self.assertFalse((self.dest / "e" / str(self.ids[1])).exists())

    def test_a_clean_render_passes_the_read_only_guard(self):
        self.render()
        gallery.guard(self.dest)


class DeterminismTests(GalleryTestCase):

    def test_a_second_render_all_is_byte_identical(self):
        self.render()
        first = {
            path.relative_to(self.dest): path.read_bytes()
            for path in sorted(self.dest.rglob("*"))
            if path.is_file()
        }
        self.render()
        second = {
            path.relative_to(self.dest): path.read_bytes()
            for path in sorted(self.dest.rglob("*"))
            if path.is_file()
        }
        self.assertEqual(sorted(first), sorted(second))
        for name, data in first.items():
            with self.subTest(path=str(name)):
                self.assertEqual(data, second[name])


class CommandLineTests(GalleryTestCase):
    """The three subcommands, as the shell sees them: --help and the codes."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )

    def test_help_exits_zero(self):
        for name in ("render", "render-index", "render-all"):
            with self.subTest(subcommand=name):
                result = self.run_cli(name, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("--gallery-dir", result.stdout)

    def test_render_all_writes_the_site(self):
        result = self.run_cli(
            "render-all", "--gallery-dir", str(self.dest),
            "--db", str(self.tmp / "sketchgen.db"),
            "--write-path", "https://write.example.invalid/api",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dest / "index.html").is_file())
        self.assertIn(str(self.dest / "index.html"), result.stdout)

    def test_a_missing_checkout_is_a_refusal(self):
        result = self.run_cli(
            "render-all", "--gallery-dir", str(self.tmp / "nowhere"),
            "--db", str(self.tmp / "sketchgen.db"),
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("refused", result.stderr)


if __name__ == "__main__":
    unittest.main()
