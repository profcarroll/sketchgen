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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_qr import decode  # noqa: E402

from sketchgen import db  # noqa: E402
from sketchgen import gallery  # noqa: E402
from sketchgen import lineage  # noqa: E402
from sketchgen.cli import gallery as cli_gallery  # noqa: E402


def cross_a_second() -> None:
    """Wait until the wall clock's second has ticked over.

    A render is a function of the database, so two of them must agree whichever
    seconds they fall in. Waiting here is the cheapest way to ask that on
    purpose: the whole shape of the bug this guards was that it was invisible
    inside one second.
    """
    edge = int(time.time()) + 1
    while time.time() < edge:
        time.sleep(0.02)

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
    # agentic-cli §1: which of the models that made it ran off the node
    "off_node",
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
    # migration 015: the process cost the agent reported and the note it left
    "process",
    "note",
    # auto-mouse.md §4.2: which pointer script this entry's page plays when
    # nobody is at the keyboard, and who wrote it
    "ghost",
    # media-assertion.md §4: where the picture the kept attempt drew came
    # from — host, content type and size, never the URL. `[]` on every entry
    # gated before `loads(image)` existed, which is all of them so far.
    "loads",
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


def add_child(conn, tmp: Path, parent_entry: int, *, state: str = "held",
              prompt: str = "the same field, slower, in one colour",
              critique: str = "let the whole thing slow down and lose a colour",
              critique_by: str = "profcarroll",
              generation: int = 3) -> int:
    """One more entry in the line, with a real attempt directory behind it.

    ``held`` by default, because a held child of a published parent is exactly
    what the publisher renders and exactly what used to lose its parent link.
    """
    jobs = tmp / "jobs"
    job = db.enqueue(
        conn, prompt, "profcarroll",
        brief="One hue, half the speed.", assertions=["motion(idle)"],
        planner="gemma4:e4b", executor="qwen3.5:4b", rules_file="treatment",
        parent_entry_id=parent_entry, critique=critique, critique_by=critique_by,
    )
    attempt = write_attempt(jobs, job, 1, "good-motion", STATEMENT_ONE,
                            exit_code=0, assertions=["motion(idle)"])
    db.add_attempt(
        conn, job, 1, model="qwen3.5:4b", rules_file="treatment",
        prompt_version="executor.md v1", prompt_tokens=1100, completion_tokens=640,
        wall_s=70.1, source_dir=str(attempt), gate_exit=0,
        gate_report_path=str(attempt / ".gate" / "report.json"),
        statement=STATEMENT_ONE,
    )
    db.transition(conn, job, "executing")
    db.transition(conn, job, "gating")
    db.transition(conn, job, "held")
    if state == "published":
        db.transition(conn, job, "published")
    entry = db.create_entry(
        conn, job, state=state, prompt=prompt,
        brief="One hue, half the speed.", statement=STATEMENT_ONE,
        planner="gemma4:e4b", planner_prompt_version="planner.md v1",
        executor="qwen3.5:4b", executor_prompt_version="executor.md v1",
        rules_file="treatment", assertions_json=json.dumps(["motion(idle)"]),
        attempts=1, prompt_tokens=1100, completion_tokens=640, wall_s=70.1,
        shape="VM.Standard.A1.Flex 16/96", seed=1, submitted_by="profcarroll",
        parent_entry_id=parent_entry, source_dir=str(attempt),
        strip_path=str(attempt / ".gate" / "strip.png"),
        png_path=str(attempt / ".gate" / "gate.png"),
        published_utc="2026-09-14T05:00:00Z" if state == "published" else None,
        publish_commit=None,
    )
    db.add_lineage(
        conn, entry, parent_entry, generation=generation,
        critique_by=critique_by, critique=critique,
    )
    return entry


class GalleryTestCase(unittest.TestCase):
    """One temp database, one temp gallery checkout, per test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-gallery-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, self.ids = build_db(self.tmp)
        self.db_path = self.tmp / "sketchgen.db"
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
            "kiosk.html",
            "kiosk.json",
            "swipe.html",
            "swipe.json",
            "lineage.json",
            f"lines/{one}.html",
        ]
        for relative in expected:
            with self.subTest(path=relative):
                self.assertTrue((self.dest / relative).is_file(), relative)

    def test_the_sketch_is_copied_verbatim_but_for_the_two_shims(self):
        # sketch.js is byte for byte the bytes the gate ran, always. The page
        # around it has exactly two additions, both of them scripts the
        # gallery needs and the gate does not fail on: the sound shim, under
        # the p5.sound tag when there is one, and the ghost pointer, under the
        # sketch.js tag on every page (soundshim.py, ghostshim.py).
        from sketchgen import ghostshim
        self.render()
        one = self.ids[0]
        self.assertEqual(
            (self.dest / "e" / str(one) / "sketch" / "sketch.js").read_bytes(),
            (SKETCHES / "good-motion" / "sketch.js").read_bytes(),
        )
        source = (SKETCHES / "good-motion" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            (self.dest / "e" / str(one) / "sketch" / "index.html").read_text(
                encoding="utf-8"),
            ghostshim.with_shim(source),
        )
        # and taking the ghost back out leaves the page it arrived on
        out = (self.dest / "e" / str(one) / "sketch" / "index.html").read_text(
            encoding="utf-8")
        self.assertEqual(1, out.count(ghostshim.MARKER))
        self.assertLess(out.index('src="sketch.js"'), out.index(ghostshim.MARKER))

    def test_a_page_that_loads_p5_sound_gets_the_shim_and_keeps_it_once(self):
        # The one exception to verbatim: a sketch page that loads p5.sound
        # would say "Loading..." for ever inside a sandboxed frame on WebKit
        # (soundshim.py), so the copy carries the shim under the addon's tag.
        # Entries published before the executor wrote it pick it up here.
        from sketchgen import soundshim
        one = self.ids[0]
        row = self.conn.execute("SELECT * FROM entries WHERE id = ?", (one,)).fetchone()
        source = gallery._source_dir(row, gallery._attempt_rows(self.conn, int(row["job_id"])))
        page = source / "index.html"
        original = page.read_text(encoding="utf-8")
        page.write_text(original.replace("p5.min.js\"></script>\n",
                                         "p5.min.js\"></script>\n" + soundshim.P5_SOUND_TAG, 1),
                        encoding="utf-8")
        self.assertIn("p5.sound.min.js", page.read_text(encoding="utf-8"))
        self.render()
        out = (self.dest / "e" / str(one) / "sketch" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(1, out.count(soundshim.MARKER))
        self.assertLess(out.index("p5.sound.min.js"), out.index(soundshim.MARKER))
        self.assertLess(out.index(soundshim.MARKER), out.index('src="sketch.js"'))
        # sketch.js is still the bytes the gate ran
        self.assertEqual((self.dest / "e" / str(one) / "sketch" / "sketch.js").read_bytes(),
                         (source / "sketch.js").read_bytes())
        # and a source that already carries the shim is not given a second one
        page.write_text(out, encoding="utf-8")
        self.render()
        again = (self.dest / "e" / str(one) / "sketch" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(out, again)

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
            "qwen3-coder:30b-a3b-q4_K_M.",
            self.page,
        )
        self.assertIn(
            "https://github.com/profcarroll/sketchgen-gallery/tree/main/e/"
            f"{self.entry_id}",
            self.page,
        )
        self.assertIn("CC BY 4.0", self.page)
        self.assertIn(f"compare.html?a={self.entry_id}", self.page)
        # where the apology used to be — the page can start a job now, and
        # still only by asking the operator for one (plan §5.2).
        self.assertIn("Critique this sketch", self.page)

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

    # ---- the page's own QR code and the scan strip (qr.md §6) ------------

    def test_the_source_panel_shows_the_entry_s_own_clean_code(self):
        # Relative, because the file sits in this directory; the clean one,
        # never the kiosk's; and once, because there is one code on this page.
        self.assertEqual(1, self.page.count('src="qr.svg"'))
        self.assertNotIn("qr-kiosk.svg", self.page)
        url = self.config.entry_url(self.entry_id)
        self.assertIn(f'<a href="{url}">{url}</a>', self.page)
        self.assertIn("Scan to open this entry on a phone", self.page)

    def test_the_code_is_decorative_because_the_url_is_printed_beside_it(self):
        # A screen reader that announced "QR code" and stopped would be worse
        # than one that reads the link (§1.10).
        figure = self.page.split('<figure class="qr">')[1].split("</figure>")[0]
        self.assertIn('alt=""', figure)

    def test_the_scan_strip_is_written_hidden_and_links_the_page_s_controls(self):
        self.assertIn("<p class=\"scanned\" data-scanned hidden>", self.page)
        self.assertIn("You scanned this from a projection.", self.page)
        self.assertIn(f'<a href="../../compare.html?a={self.entry_id}">', self.page)
        self.assertIn('<a href="#engagement">Like it</a>', self.page)
        self.assertIn('<a href="#critique-text">Ask for a revision</a>', self.page)
        # The anchors point at things this page actually has.
        self.assertIn('<section class="engagement" id="engagement">', self.page)
        self.assertIn('id="critique-text"', self.page)

    def test_without_the_param_the_page_is_what_it_was_plus_the_figure(self):
        # Nothing about the strip is conditional in the generator: it is
        # markup, hidden, and only gallery.js ever reveals it.
        self.assertIn("hidden>You scanned", self.page)


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


class RevisedFromTests(GalleryTestCase):
    """Which entries were revisions of code, and which of a sentence.

    Entry 1103, 2026-09-20, is why the mechanism exists: five attempts at a
    jigsaw, each written from a blank page, because the only thing carried
    from a sketch to its revision was prose. Since 2026-09-21 the executor is
    shown the sketch it is revising (child-source.md packet 17) and the
    attempt row records it; this is where a reader of the gallery finds out,
    and the field the ledger reads is `lineage.inherits_source`.

    Entry 2 in the fixture is the child of entry 1, so it is the one with a
    parent to have been shown. The column is set here with an UPDATE rather
    than through `add_attempt` because `build_db` is shared with every other
    test in this file and this is a fixture building a past, not the worker
    writing a present.
    """

    def given(self, entry_id, record, *, num_ctx=16384):
        job_id = self.conn.execute(
            "SELECT job_id FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()["job_id"]
        self.conn.execute(
            "UPDATE attempts SET given_source_json = ?, num_ctx = ? "
            "WHERE job_id = ? AND n = 1",
            (json.dumps(record) if record else None, num_ctx, job_id),
        )

    def meta_for(self, entry_id):
        return json.loads(
            (self.dest / "e" / str(entry_id) / "meta.json").read_text(encoding="utf-8")
        )

    def page_for(self, entry_id):
        return (self.dest / "e" / str(entry_id) / "index.html").read_text(
            encoding="utf-8")

    PARENT = {"kind": "parent", "path": "/home/x/jobs/1/attempt-1/sketch.js",
              "sha256": "a" * 64, "lines": 180, "shown": True}

    def test_the_gate_log_carries_the_record_and_the_context(self):
        self.given(self.ids[1], self.PARENT)
        self.render()
        gate = self.meta_for(self.ids[1])["gate"]
        # the record less its node path: the public gallery names nothing on
        # the node's filesystem (gallery.GIVEN_KEYS)
        published = {k: v for k, v in self.PARENT.items() if k != "path"}
        self.assertEqual(published, gate[0]["given"])
        self.assertNotIn("path", gate[0]["given"])
        self.assertNotIn("/home/", json.dumps(gate))
        self.assertEqual(16384, gate[0]["num_ctx"])
        # and the entry whose attempts were never given anything says null,
        # which is every entry this gallery published before 2026-09-21
        first = self.meta_for(self.ids[0])["gate"]
        self.assertIsNone(first[0]["given"])
        self.assertIsNone(first[0]["num_ctx"])

    def test_inherits_source_is_true_only_where_code_was_shown(self):
        self.given(self.ids[1], self.PARENT)
        self.render()
        self.assertIs(True, self.meta_for(self.ids[1])["lineage"]["inherits_source"])
        self.assertIs(False, self.meta_for(self.ids[0])["lineage"]["inherits_source"])
        # The whole of the lineage object, written out the way SPEC_7_KEYS
        # writes out the top level: META_KEYS itself does not move, because
        # both of this packet's fields are nested, and a key added inside here
        # without a reason in this list should fail a test.
        self.assertEqual(
            {"parent_entry_id", "children", "generation", "root_entry_id",
             "critique_by", "critique",
             # child-source.md packet 18: was this sketch a revision of code,
             # or of a sentence? False on every entry before 2026-09-21.
             "inherits_source"},
            set(self.meta_for(self.ids[1])["lineage"]),
        )

    def test_a_sketch_over_the_cap_revised_nothing(self):
        """Found, named in one line, and not shown: the entry is a revision of
        the prompt like any other, and `false` is the honest answer. That the
        sketch was offered is still in the gate log and on the job page."""
        self.given(self.ids[1], {**self.PARENT, "shown": False, "lines": 412})
        self.render()
        meta = self.meta_for(self.ids[1])
        self.assertIs(False, meta["lineage"]["inherits_source"])
        self.assertEqual(412, meta["gate"][0]["given"]["lines"])
        self.assertNotIn("Revised from", self.page_for(self.ids[1]))

    def test_the_revised_from_row_names_the_parent_and_the_lines(self):
        self.given(self.ids[1], self.PARENT)
        self.render()
        page = self.page_for(self.ids[1])
        self.assertIn('<th scope="row">Revised from</th>', page)
        self.assertIn(f"entry {self.ids[0]}&#x27;s sketch, 180 lines", page)
        # one attempt, so there is nothing to say about tries of its own
        self.assertNotIn("on its own", page)
        # and it lands directly under Lineage, which says which parent
        self.assertLess(page.index("Lineage"), page.index("Revised from"))

    def test_the_tail_counts_the_attempts_it_made_on_its_own(self):
        self.conn.execute("UPDATE entries SET attempts = 3 WHERE id = ?",
                          (self.ids[1],))
        self.given(self.ids[1], self.PARENT)
        self.render()
        self.assertIn(
            f"entry {self.ids[0]}&#x27;s sketch, 180 lines, then 2 attempts on its own",
            self.page_for(self.ids[1]),
        )

    def test_the_line_itself_reads_as_the_packet_wrote_it(self):
        self.assertEqual(
            "entry 1103's sketch, 180 lines, then 2 attempts on its own",
            gallery._revised_from_line({
                "attempts": 3,
                "lineage": {"parent_entry_id": 1103, "inherits_source": True},
                "gate": [{"given": self.PARENT}],
            }),
        )
        self.assertEqual(
            "entry 1103's sketch, 180 lines, then 1 attempt on its own",
            gallery._revised_from_line({
                "attempts": 2,
                "lineage": {"parent_entry_id": 1103, "inherits_source": True},
                "gate": [{"given": self.PARENT}],
            }),
        )
        self.assertIsNone(gallery._revised_from_line({
            "attempts": 1,
            "lineage": {"parent_entry_id": 1103, "inherits_source": False},
            "gate": [{"given": None}],
        }))

    def test_an_entry_given_nothing_renders_as_it_did_before_the_packet(self):
        """The promise the packet makes to 910 published pages.

        A row of dashes would have been a change to every one of them; absent
        means the render of an entry whose attempts carry NULLs is the render
        it was, byte for byte. Checked against the same render with the row
        generation forced off, which is the state of this file before packet
        18 — no fixture of the old HTML to go stale beside it.
        """
        self.render()
        before = {entry_id: self.page_for(entry_id) for entry_id in self.ids[:2]}
        shutil.rmtree(self.dest)
        self.dest.mkdir()
        original = gallery._revised_from_line
        gallery._revised_from_line = lambda meta: None
        self.addCleanup(setattr, gallery, "_revised_from_line", original)
        self.render()
        for entry_id, page in before.items():
            with self.subTest(entry=entry_id):
                self.assertEqual(page, self.page_for(entry_id))
                self.assertNotIn("Revised from", page)


class GhostScriptTests(GalleryTestCase):
    """The executor's own pointer script, from the attempt dir onto the page.

    auto-mouse.md §4.2. No migration and no column: ``ghost.json`` sits beside
    ``sketch.js`` in the attempt directory, which the entry row already names,
    the way ``_canvas_size`` finds the source.
    """

    EVENTS = [{"t": 200, "type": "move", "x": 0.2, "y": 0.25},
              {"t": 600, "type": "down", "x": 0.2, "y": 0.25},
              {"t": 900, "type": "up", "x": 0.6, "y": 0.6}]

    def source_of(self, entry_id):
        row = gallery._entry(self.conn, int(entry_id))
        return gallery._source_dir(row, gallery._attempt_rows(self.conn, row["job_id"]))

    def give(self, entry_id, text):
        (self.source_of(entry_id) / "ghost.json").write_text(text, encoding="utf-8")

    def rendered(self, entry_id):
        gallery.render_entry(self.conn, entry_id, self.dest, self.config)
        base = self.dest / "e" / str(entry_id)
        page = base / "sketch" / "index.html"
        return (
            page.read_text(encoding="utf-8") if page.is_file() else "",
            json.loads((base / "meta.json").read_text(encoding="utf-8")),
            base,
        )

    def test_an_entry_with_a_script_carries_it_above_the_player(self):
        from sketchgen import ghostshim
        one = self.ids[0]
        self.give(one, json.dumps(self.EVENTS))
        page, meta, base = self.rendered(one)
        self.assertLess(page.index(ghostshim.SCRIPT_MARKER),
                        page.index(ghostshim.MARKER))
        self.assertEqual(1, page.count(ghostshim.SCRIPT_MARKER))
        line = [l for l in page.splitlines()
                if l.startswith(ghostshim.SCRIPT_MARKER)][0]
        self.assertEqual(
            self.EVENTS,
            json.loads(line[len(ghostshim.SCRIPT_MARKER):-len(";</script>")]),
        )
        # and the file itself goes beside the page, for a reader who wants to
        # know what the pointer was asked to do
        self.assertEqual(self.EVENTS,
                         json.loads((base / "ghost.json").read_text(encoding="utf-8")))
        self.assertEqual({"events": 3, "by": "executor"}, meta["ghost"])

    def test_rendering_the_same_entry_twice_writes_the_same_page(self):
        # A render-all rewrites every published page and the publisher commits
        # what changed; a script that stacked would be a diff every time.
        one = self.ids[0]
        self.give(one, json.dumps(self.EVENTS))
        first, _, _ = self.rendered(one)
        second, _, _ = self.rendered(one)
        self.assertEqual(first, second)

    def test_an_entry_with_none_says_which_built_in_it_gets(self):
        from sketchgen import ghostshim
        # entry 1 confirmed responds(click); entry 2 is no_motion and asked
        # for nothing it could respond to (DECIDE[ghost-who]).
        page, meta, base = self.rendered(self.ids[0])
        self.assertEqual(
            {"events": len(ghostshim.BUILTINS["click"]), "by": "default",
             "script": "click"},
            meta["ghost"],
        )
        self.assertNotIn(ghostshim.SCRIPT_MARKER, page)
        self.assertFalse((base / "ghost.json").exists())
        still = self.rendered(self.ids[1])[1]
        self.assertEqual(
            {"events": len(ghostshim.BUILTINS["wander"]), "by": "default",
             "script": "wander"},
            still["ghost"],
        )

    def test_an_entry_that_responds_to_both_gets_both_built_ins(self):
        from sketchgen import ghostshim
        entry = self.ids[0]
        self.conn.execute(
            "UPDATE entries SET assertions_json = ? WHERE id = ?",
            (json.dumps(["no_motion", "responds(click)", "responds(drag)"]), entry),
        )
        self.conn.commit()
        self.assertEqual(
            {"events": len(ghostshim.BUILTINS["click"])
                       + len(ghostshim.BUILTINS["drag"]),
             "by": "default", "script": "click,drag"},
            self.rendered(entry)[1]["ghost"],
        )

    def test_an_assertion_the_gate_missed_does_not_choose_the_script(self):
        # The subtraction _swipe_entry and kiosk.json already apply: sending a
        # click into a sketch the gate proved does not respond to one is the
        # case it exists to prevent.
        entry = self.ids[0]
        self.conn.execute(
            "UPDATE entries SET offplan_json = ? WHERE id = ?",
            (json.dumps(["responds(click)"]), entry),
        )
        self.conn.commit()
        self.assertEqual("wander", self.rendered(entry)[1]["ghost"]["script"])

    def test_a_file_that_no_longer_validates_is_skipped_with_its_reason(self):
        # A person may edit the file on disk; a render must not be the thing
        # that breaks over it.
        from sketchgen import ghostshim
        one = self.ids[0]
        self.give(one, json.dumps([{"t": 200, "type": "move", "x": 4, "y": 0}]))
        page, meta, base = self.rendered(one)
        self.assertNotIn(ghostshim.SCRIPT_MARKER, page)
        self.assertFalse((base / "ghost.json").exists())
        self.assertEqual("default", meta["ghost"]["by"])
        self.assertEqual("click", meta["ghost"]["script"])
        self.assertEqual("event 1: x is 4, outside [0, 1]", meta["ghost"]["rejected"])

    def test_a_file_that_is_not_json_is_the_same_kind_of_skip(self):
        one = self.ids[0]
        self.give(one, "{ nearly")
        page, meta, _ = self.rendered(one)
        self.assertEqual("not a JSON list", meta["ghost"]["rejected"])
        self.assertTrue(page.rstrip().endswith("</html>"))

    def test_the_script_does_not_reach_an_entry_with_no_sketch_to_play_it(self):
        # No sketch.js means no page, no shim and nothing to feed.
        job = db.enqueue(self.conn, "a sketch that never landed", "profcarroll")
        self.conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, state="failed-kept", prompt="a sketch that never landed",
            published_utc=db.utc_now(), submitted_by="profcarroll",
        )
        self.conn.commit()
        _, meta, base = self.rendered(entry)
        self.assertFalse((base / "sketch").exists())
        self.assertEqual("wander", meta["ghost"]["script"])


class GhostFramesTests(GalleryTestCase):
    """``ghost.png``, from the gate's own directory onto the entry page.

    auto-mouse.md §5.3. No migration and no column: nothing re-gates a
    published entry, so a column would be NULL on all 910 of them and the
    attempt directory the entry already names is where the file is.
    """

    def gate_dir(self, entry_id):
        row = gallery._entry(self.conn, int(entry_id))
        source = gallery._source_dir(row, gallery._attempt_rows(self.conn,
                                                                row["job_id"]))
        return source / ".gate"

    def give(self, entry_id, summary=None):
        """The two halves the gate writes: the picture, and the summary."""
        gate = self.gate_dir(entry_id)
        (gate / "ghost.png").write_bytes(PNG_BYTES)
        report = json.loads((gate / "report.json").read_text(encoding="utf-8"))
        report["artefacts"]["ghost"] = str(gate / "ghost.png")
        report["ghost"] = summary or {"source": "default", "script": "click",
                                      "events": 16, "played": 16, "ms": 3320}
        (gate / "report.json").write_text(json.dumps(report, indent=2),
                                          encoding="utf-8")

    def rendered(self, entry_id):
        gallery.render_entry(self.conn, entry_id, self.dest, self.config)
        base = self.dest / "e" / str(entry_id)
        return ((base / "index.html").read_text(encoding="utf-8"),
                json.loads((base / "meta.json").read_text(encoding="utf-8")),
                base)

    def test_the_frames_are_copied_and_captioned(self):
        one = self.ids[0]
        self.give(one)
        page, meta, base = self.rendered(one)
        self.assertEqual(PNG_BYTES, (base / "ghost.png").read_bytes())
        self.assertIn('<img src="ghost.png"', page)
        self.assertIn("with the ghost pointer", page)
        # Under the stage the sketch is in, not somewhere else on the page.
        self.assertLess(page.index("stage-meta"), page.index('src="ghost.png"'))
        self.assertEqual({"source": "default", "script": "click", "events": 16,
                          "played": 16, "ms": 3320}, meta["gate"][0]["ghost"])
        self.assertEqual(set(gallery.META_KEYS), set(meta))

    def test_an_entry_with_no_ghost_renders_exactly_as_it_did(self):
        # The 910 published entries have no ghost.png and nothing re-gates
        # them. Their pages must not move by one byte, or the next render-all
        # is a diff of the whole gallery for a file none of them has.
        one = self.ids[0]
        before, meta_before, base = self.rendered(one)
        self.assertFalse((base / "ghost.png").exists())
        self.assertNotIn("ghost.png", before)
        self.assertIsNone(meta_before["gate"][0]["ghost"])
        after, _meta, _base = self.rendered(one)
        self.assertEqual(before, after)

    def test_the_summary_travels_even_when_the_picture_did_not_arrive(self):
        # A window the budget stopped writes the summary and no file: the page
        # shows nothing, and meta.json still says what was attempted, which is
        # what MEASURE[ghost-coverage] counts.
        one = self.ids[0]
        gate = self.gate_dir(one)
        report = json.loads((gate / "report.json").read_text(encoding="utf-8"))
        report["ghost"] = {"source": "default", "script": "wander",
                           "events": 40, "played": 11, "ms": 1100}
        (gate / "report.json").write_text(json.dumps(report), encoding="utf-8")
        page, meta, base = self.rendered(one)
        self.assertFalse((base / "ghost.png").exists())
        self.assertNotIn("ghost.png", page)
        self.assertEqual(11, meta["gate"][0]["ghost"]["played"])


class PublishTimeLineageTests(GalleryTestCase):
    """The entry being published is in its own forest (packet 1, spec §4.1).

    Before this, ``_forest`` was built over the public entries only and the
    publisher renders while the row is still held, so the entry looked up its
    own parent, missed, and froze ``null`` into meta.json. The parent's own
    meta lost the child for the same reason.
    """

    def test_publishing_a_held_child_records_its_parent(self):
        parent = self.ids[1]
        child = add_child(self.conn, self.tmp, parent)
        gallery.render_entry(self.conn, child, self.dest, self.config, publishing=True)
        meta = json.loads(
            (self.dest / "e" / str(child) / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(parent, meta["lineage"]["parent_entry_id"])
        self.assertEqual(3, meta["lineage"]["generation"])
        self.assertEqual(self.ids[0], meta["lineage"]["root_entry_id"])

    def test_the_admitted_entry_is_a_child_in_the_forest(self):
        parent = self.ids[1]
        child = add_child(self.conn, self.tmp, parent)
        forest_parent, children = gallery._forest(self.conn, admit=child)
        self.assertEqual(parent, forest_parent[child])
        self.assertIn(child, children[parent])

    def test_without_admit_the_forest_is_the_public_entries_alone(self):
        # The site's tree pages render entries that are already public and must
        # not start showing held ones; only the publisher admits its own entry.
        parent = self.ids[1]
        child = add_child(self.conn, self.tmp, parent)
        _, children = gallery._forest(self.conn)
        self.assertNotIn(child, children)
        self.assertNotIn(child, children[parent])

    def test_the_parent_gains_the_child_once_the_child_is_published(self):
        parent = self.ids[1]
        child = add_child(self.conn, self.tmp, parent, state="published")
        self.render()
        meta = json.loads(
            (self.dest / "e" / str(parent) / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual([child], meta["lineage"]["children"])


class LineageJsonTests(GalleryTestCase):
    """<gallery>/lineage.json, the shape packet 3's panel reads (spec §4.2)."""

    def setUp(self):
        super().setUp()
        # A line of four: 1 -> 2 (both published from build_db), then a third
        # published generation, then a held child nobody has published.
        self.third = add_child(
            self.conn, self.tmp, self.ids[1], state="published",
            prompt="the same field, slower, in one colour", generation=3,
        )
        self.held = add_child(
            self.conn, self.tmp, self.third, state="held",
            prompt="slower still, and let the ground breathe",
            critique="slow it further and let the ground breathe",
            generation=4,
        )
        self.render()
        self.data = json.loads(
            (self.dest / "lineage.json").read_text(encoding="utf-8")
        )
        self.entries = self.data["entries"]

    def test_the_file_is_stamped_and_keyed_by_entry_id_as_a_string(self):
        self.assertRegex(
            self.data["generated_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertEqual({"generated_utc", "entries"}, set(self.data))
        self.assertEqual(
            sorted(str(i) for i in (*self.ids, self.third, self.held)),
            sorted(self.entries),
        )

    def test_the_stamp_is_the_newest_stamp_in_the_database_not_the_clock(self):
        # publish_index re-renders the site and commits only if bytes changed;
        # a stamp taken from the clock made every re-render a commit. This one
        # is the last thing the database recorded, so it moves only when the
        # database does.
        newest = self.conn.execute(
            "SELECT MAX(stamp) FROM ("
            "  SELECT created_utc AS stamp FROM entries"
            "  UNION ALL SELECT published_utc FROM entries"
            "  UNION ALL SELECT created_utc FROM lineage)"
        ).fetchone()[0]
        self.assertEqual(newest, self.data["generated_utc"])
        later = "2031-01-01T00:00:00Z"
        self.conn.execute(
            "UPDATE entries SET published_utc = ? WHERE id = ?", (later, self.ids[0])
        )
        self.conn.commit()
        self.render()
        data = json.loads((self.dest / "lineage.json").read_text(encoding="utf-8"))
        self.assertEqual(later, data["generated_utc"])

    def test_every_entry_is_here_whatever_its_state(self):
        # including the held child, which is on no page of the site at all
        self.assertIn(str(self.held), self.entries)
        self.assertEqual("held", self.entries[str(self.held)]["state"])
        self.assertFalse(self.entries[str(self.held)]["public"])

    def test_a_public_entry_carries_its_strip_prompt_and_submitter(self):
        item = self.entries[str(self.ids[1])]
        self.assertTrue(item["public"])
        self.assertEqual("published", item["state"])
        self.assertEqual(self.ids[0], item["parent"])
        self.assertEqual([self.third], item["children"])
        self.assertEqual(2, item["generation"])
        self.assertEqual(self.ids[0], item["root"])
        self.assertEqual("astudent", item["submitted_by"])
        self.assertEqual(f"e/{self.ids[1]}/strip.png", item["strip"])
        self.assertEqual(
            "the same field, but it holds still and earns it", item["root_prompt"]
        )
        self.assertIn("The motion is doing the work", item["critique"])
        self.assertEqual("gemma4:e4b", item["critique_by"])

    def test_a_non_public_entry_carries_nothing_of_its_own(self):
        item = self.entries[str(self.held)]
        self.assertEqual(
            {"state", "public", "parent", "children", "generation", "root",
             "critique", "critique_by"},
            set(item),
        )
        # its place in the line, and its critique, which came from its parent
        self.assertEqual(self.third, item["parent"])
        self.assertEqual([], item["children"])
        self.assertEqual(4, item["generation"])
        self.assertEqual(self.ids[0], item["root"])
        self.assertEqual("slow it further and let the ground breathe", item["critique"])

    def test_a_held_child_counts_as_a_child_of_its_public_parent(self):
        # _forest stops at the public entries; this file does not, which is the
        # whole reason the panel can say a generation exists but is not shown.
        self.assertEqual([self.held], self.entries[str(self.third)]["children"])

    def test_a_root_has_no_parent_and_is_its_own_root(self):
        item = self.entries[str(self.ids[0])]
        self.assertIsNone(item["parent"])
        self.assertEqual(self.ids[0], item["root"])
        self.assertIsNone(item["critique"])
        self.assertIsNone(item["critique_by"])
        self.assertEqual([self.ids[1]], item["children"])

    def test_the_root_prompt_is_the_prompt_without_its_revisions(self):
        composed = gallery.lineage.compose_prompt(
            "a cityscape from sunrise to sunset", "try a colder palette"
        )
        self.conn.execute(
            "UPDATE entries SET prompt = ? WHERE id = ?", (composed, self.ids[1])
        )
        data = gallery._lineage_index(self.conn)
        self.assertEqual(
            "a cityscape from sunrise to sunset",
            data["entries"][str(self.ids[1])]["root_prompt"],
        )

    def test_the_file_stays_under_a_hundred_kilobytes(self):
        self.assertLess(
            (self.dest / "lineage.json").stat().st_size, gallery.LINEAGE_JSON_LIMIT
        )

    def test_over_the_limit_the_root_prompts_are_capped(self):
        long_prompt = "x" * 4_000
        for entry_id in (*self.ids, self.third):
            self.conn.execute(
                "UPDATE entries SET prompt = ? WHERE id = ?", (long_prompt, entry_id)
            )
        # The real limit at three hundred entries; here, a limit these four
        # entries can cross, which is the same arithmetic.
        limit = gallery.LINEAGE_JSON_LIMIT
        gallery.LINEAGE_JSON_LIMIT = 4_000
        self.addCleanup(setattr, gallery, "LINEAGE_JSON_LIMIT", limit)
        data = gallery._lineage_index(self.conn)
        for entry_id in (*self.ids, self.third):
            self.assertEqual(
                gallery.LINEAGE_ROOT_PROMPT_CAP,
                len(data["entries"][str(entry_id)]["root_prompt"]),
            )


# ---------------------------------------------------------------------------
# Packet 4: the title is the root prompt, the subtitle is the latest revision
# ---------------------------------------------------------------------------


def heading(text: str) -> str:
    """The `h1` of a page, tags stripped, entities resolved."""
    inner = text.split("<h1>", 1)[1].split("</h1>", 1)[0]
    return html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()


def subtitle(text: str) -> str:
    """The `p.sub` under the heading, tags stripped. Empty when there is none."""
    match = re.search(r'<p class="sub">(.*?)</p>', text, re.S)
    if match is None:
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split())


def head_title(text: str) -> str:
    return html.unescape(text.split("<title>", 1)[1].split("</title>", 1)[0])


class TitleTests(GalleryTestCase):
    """A root, a first revision, and the generation-10 case that forced this.

    Entry 230 on the live site is generation 10: its prompt is the root
    sentence with ten critiques stapled under `Revise:` headings, 1,700
    characters that the heading used to print in full. The fixture here is the
    same shape, built by composing each generation's prompt the way
    `lineage.compose_prompt` does, so the split the page does is the real one.
    """

    #: The critiques, in order, that turn the root into a generation-10 prompt.
    #: One per generation: entry 230 carries ten headings and its lineage row
    #: says generation 10, and entry 82 carries six and says 6.
    REVISIONS = [
        "try again with a different colour palette and mood",
        "slow the drift until a single circle can be followed",
        "let the ground breathe instead of holding one value",
        "give the cursor less power over the whole field",
        "bring back some of the contrast the last pass lost",
        "let one circle be larger than the rest and lead",
        "hold the palette but change what the click does",
        "make the return after a scatter take longer",
        "end on stillness rather than on motion",
        "keep the stillness but let the ground hold one more hue",
    ]

    def setUp(self):
        super().setUp()
        self.root_id = self.ids[0]
        self.root_prompt = " ".join(
            self.conn.execute(
                "SELECT prompt FROM entries WHERE id = ?", (self.root_id,)
            ).fetchone()["prompt"].split()
        )
        prompt = self.root_prompt
        parent = self.root_id
        self.chain = []
        for n, critique in enumerate(self.REVISIONS, start=1):
            prompt = lineage.compose_prompt(prompt, critique)
            parent = add_child(
                self.conn, self.tmp, parent, state="published", prompt=prompt,
                critique=critique, generation=n,
                # a critic model on the odd generations, a person on the even
                critique_by="gemma4:e4b" if n % 2 else "profcarroll",
            )
            self.chain.append(parent)
        self.render()

    def page(self, entry_id: int) -> str:
        return (self.dest / "e" / str(entry_id) / "index.html").read_text(
            encoding="utf-8"
        )

    def test_a_root_is_its_own_title_and_has_no_subtitle(self):
        page = self.page(self.root_id)
        self.assertEqual(self.root_prompt, heading(page))
        self.assertEqual("", subtitle(page))
        self.assertNotIn("Revise:", page.split("<section", 1)[0])

    def test_the_first_revision_names_itself_and_counts_no_others(self):
        page = self.page(self.chain[0])
        self.assertEqual(self.root_prompt, heading(page))
        sub = subtitle(page)
        self.assertIn("Revise:", sub)
        self.assertIn(self.REVISIONS[0], sub)
        self.assertIn("generation 1", sub)
        # one revision, so there are no earlier ones to send anybody below
        self.assertNotIn("earlier revision", sub)

    def test_a_model_critic_wears_the_model_chip_without_its_size(self):
        # generation 1 was asked for by gemma4:e4b; the chip is the critic, not
        # the quantisation, so the tag after the colon is not in it
        page = self.page(self.chain[0])
        self.assertIn('<span class="chip model">gemma4</span>', page)
        self.assertNotIn("gemma4:e4b", subtitle(page))

    def test_the_generation_ten_title_is_one_sentence(self):
        entry_id = self.chain[-1]
        page = self.page(entry_id)
        self.assertEqual(self.root_prompt, heading(page))
        # the thing this packet exists to stop: nine amendments in the heading
        self.assertNotIn("Revise:", page.split("</h1>", 1)[0])
        for revision in self.REVISIONS:
            self.assertNotIn(revision, page.split("</h1>", 1)[0])

    def test_the_generation_ten_subtitle_names_the_latest_revision(self):
        page = self.page(self.chain[-1])
        sub = subtitle(page)
        self.assertIn(self.REVISIONS[-1], sub)
        self.assertNotIn(self.REVISIONS[0], sub)
        self.assertIn("generation 10", sub)
        self.assertIn("9 earlier revisions in the lineage below", sub)
        # the critique on the last generation came from a person
        self.assertIn('<span class="chip person">profcarroll</span>', page)

    def test_the_head_title_is_the_root_truncated_at_eighty(self):
        for entry_id in (self.root_id, self.chain[0], self.chain[-1]):
            with self.subTest(entry=entry_id):
                self.assertEqual(
                    self.root_prompt[:80], head_title(self.page(entry_id))
                )

    def test_provenance_keeps_the_whole_prompt(self):
        """The title is shorter; the record is not."""
        page = self.page(self.chain[-1])
        meta = json.loads(
            (self.dest / "e" / str(self.chain[-1]) / "meta.json").read_text()
        )
        for revision in self.REVISIONS:
            with self.subTest(revision=revision):
                self.assertIn(revision, meta["prompt"])
                self.assertIn(html.escape(revision), page)

    def test_a_card_shows_the_root_and_the_latest_revision(self):
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        card = index.split(f'data-entry="{self.chain[-1]}"', 1)[1].split("</div>", 1)[0]
        self.assertIn(html.escape(self.root_prompt), card)
        self.assertIn("card-revision", card)
        self.assertIn(f"g10 · {self.REVISIONS[-1]}", html.unescape(card))
        # the nine earlier revisions are in data-search and nowhere visible
        shown = html.unescape(card.split('data-search="', 1)[1].split('">', 1)[1])
        for revision in self.REVISIONS[:-1]:
            with self.subTest(revision=revision):
                self.assertNotIn(revision, shown)

    def test_the_entry_eighty_two_case_reads_g6(self):
        """Six headings, lineage generation 6, and the card says so.

        Entry 82 on the live site: `generation` in the lineage row is the
        number of `Revise:` headings in the prompt, not one more than it, and
        the card takes the number from the row either way.
        """
        entry_id = self.chain[5]
        self.assertEqual(
            6,
            int(self.conn.execute(
                "SELECT generation FROM lineage WHERE child_entry_id = ?",
                (entry_id,),
            ).fetchone()["generation"]),
        )
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        card = index.split(f'data-entry="{entry_id}"', 1)[1].split("</div>", 1)[0]
        self.assertIn(f"g6 · {self.REVISIONS[5]}", html.unescape(card))
        sub = subtitle(self.page(entry_id))
        self.assertIn("generation 6", sub)
        self.assertIn("5 earlier revisions in the lineage below", sub)

    def test_search_still_finds_an_early_revision(self):
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        haystack = card_search(index)[str(self.chain[-1])]
        for revision in self.REVISIONS:
            with self.subTest(revision=revision):
                self.assertIn(revision, html.unescape(haystack))

    def test_only_the_root_card_on_a_line_page_carries_the_prompt(self):
        line = (self.dest / "lines" / f"{self.root_id}.html").read_text(
            encoding="utf-8"
        )
        # the card that closes a line at DECIDE[lineage-depth] has one too; it
        # is not a generation, so it is not part of this count
        self.assertEqual(1, line.split('class="node depth-6 waits"')[0].count("node-prompt"))
        self.assertIn(html.escape(self.root_prompt), line)
        for revision in self.REVISIONS:
            with self.subTest(revision=revision):
                # each critique is on the line page once, as a critique
                self.assertEqual(1, line.count(html.escape(revision)))


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
        # The chip says whose rejection it is, not just that there was one.
        self.assertIn("rejected · automatic", self.failed)
        self.assertIn("AudioContext is suspended", self.failed)

    def test_the_cards_carry_what_the_grid_shows(self):
        one = self.ids[0]
        self.assertIn(f'src="e/{one}/strip.png"', self.index)
        self.assertIn('data-rules="control"', self.index)
        self.assertIn('data-executor="qwen3.5:4b"', self.index)
        self.assertIn("profcarroll", self.index)
        self.assertIn('data-count="views"', self.index)

    def test_the_url_still_filters_with_the_chips_gone(self):
        """§5.3 took the bar out; ``?rules=`` and ``?executor=`` still work.

        The chips were links and the filtering was never theirs: it is
        ``applyVisibility()``, which reads the two parameters off the URL and
        matches them against attributes the generator writes on every card. So
        this asserts the machinery a shared link needs, not the furniture.
        """
        self.assertNotIn('class="filter"', self.index)
        for attribute in (
            'data-rules="control"',
            'data-rules="treatment"',
            'data-executor="qwen3.5:4b"',
        ):
            with self.subTest(attribute=attribute):
                self.assertIn(attribute, self.index)
        script = (self.dest / "assets" / "gallery.js").read_text(encoding="utf-8")
        self.assertIn('params.get("rules")', script)
        self.assertIn('params.get("executor")', script)

    def test_the_filter_links_are_still_built_for_the_revert(self):
        # _filters() stays in gallery.py, unused, so that putting the bar back
        # is four lines of template and nothing else (plan §5.3).
        rows = [row for row in self.conn.execute("SELECT * FROM entries")]
        links = gallery._filters(rows, "index.html")
        self.assertIn('href="index.html?rules=control"', links)
        self.assertIn('href="index.html?executor=qwen3.5:4b"', links)

    def test_compare_carries_no_pre_deployment_notes(self):
        # Two sentences written before the write path and the agent judge
        # existed survived on the live page until 2026-09-14. The script
        # only rewrites the status note when no write path is configured, so
        # the template's default has to be true on its own.
        compare = (self.dest / "compare.html").read_text(encoding="utf-8")
        self.assertNotIn("Until it is deployed", compare)
        self.assertNotIn("packet 5.2", compare)
        self.assertIn("Signed in with GitHub, your answers are recorded", compare)

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
        # The kiosk counts unless somebody says otherwise, and the saying is
        # this line (docs/plans/kiosk-views.md §3.3).
        self.assertIs(True, config["kiosk_views"])
        # And it ghosts unless somebody says otherwise, on the same terms
        # (docs/plans/auto-mouse.md DECIDE[ghost-off]).
        self.assertIs(True, config["kiosk_ghost"])
        self.assertEqual(6, config["kiosk_ghost_loop_s"])

    def test_the_kiosk_switch_round_trips_a_render(self):
        # It is the only lever there is: the write path has no switch of its
        # own, so turning the projector's views off means editing this file in
        # the gallery checkout. A render that dropped the line, or read it as
        # a string and called it true, would turn them back on silently.
        dest = self.tmp / "switched"
        dest.mkdir()
        (dest / "config.json").write_text(
            json.dumps({"write_path": "https://write.example.invalid/api",
                        "kiosk_views": False}),
            encoding="utf-8",
        )
        loaded = gallery.Config.load(dest)
        self.assertIs(False, loaded.kiosk_views)
        gallery.render_index(self.conn, dest, loaded)
        after = json.loads((dest / "config.json").read_text(encoding="utf-8"))
        self.assertIs(False, after["kiosk_views"])

    def test_the_ghost_switch_round_trips_a_render(self):
        # The same lever the views have, for the same reason: one line in the
        # gallery checkout and a render-index, no deploy. A render that
        # dropped the line would turn the pointer back on silently.
        dest = self.tmp / "unghosted"
        dest.mkdir()
        (dest / "config.json").write_text(
            json.dumps({"write_path": "https://write.example.invalid/api",
                        "kiosk_ghost": False, "kiosk_ghost_loop_s": 20}),
            encoding="utf-8",
        )
        loaded = gallery.Config.load(dest)
        self.assertIs(False, loaded.kiosk_ghost)
        self.assertEqual(20, loaded.kiosk_ghost_loop_s)
        gallery.render_index(self.conn, dest, loaded)
        after = json.loads((dest / "config.json").read_text(encoding="utf-8"))
        self.assertIs(False, after["kiosk_ghost"])
        self.assertEqual(20, after["kiosk_ghost_loop_s"])

    def test_a_ghost_gap_nobody_could_have_meant_is_the_default(self):
        # Clamped rather than trusted, as clampEvery in kiosk.js is: a
        # projector should not be left ghosting once an hour, or forty times a
        # second, by a typo in a file nobody rereads.
        dest = self.tmp / "silly"
        dest.mkdir()
        for value in (0, -5, 6000, "soon", None, [6]):
            with self.subTest(value=value):
                (dest / "config.json").write_text(
                    json.dumps({"kiosk_ghost_loop_s": value}), encoding="utf-8")
                self.assertEqual(
                    gallery.DEFAULT_GHOST_LOOP_S,
                    gallery.Config.load(dest).kiosk_ghost_loop_s,
                )

    def test_a_config_written_before_the_kiosk_counts(self):
        # Absent is on. Every checkout is in this state the first time the
        # field ships, and none of them should go quiet.
        dest = self.tmp / "older"
        dest.mkdir()
        (dest / "config.json").write_text(
            json.dumps({"write_path": "https://write.example.invalid/api"}),
            encoding="utf-8",
        )
        self.assertIs(True, gallery.Config.load(dest).kiosk_views)
        # Absent is on for the ghost pointer too: a checkout that predates it
        # should get it, not go without until somebody notices.
        self.assertIs(True, gallery.Config.load(dest).kiosk_ghost)
        self.assertEqual(
            gallery.DEFAULT_GHOST_LOOP_S,
            gallery.Config.load(dest).kiosk_ghost_loop_s,
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

    def test_the_grid_offers_every_sort(self):
        sorts = (
            "newest", "oldest", "random", "liked",
            "reviewed", "controversial", "consensus",
        )
        for name, page in (("index", self.index), ("rejections", self.failed)):
            with self.subTest(page=name):
                for sort in sorts:
                    self.assertIn(
                        f'<button type="button" class="sort" data-sort="{sort}"', page
                    )
                self.assertEqual(page.count('class="sort"'), len(sorts))
                # the order the HTML is already in is the one marked current
                self.assertIn('data-sort="newest" aria-current="true"', page)

    def test_no_page_has_a_filter_bar_any_more(self):
        self.render()
        for page in self.pages():
            with self.subTest(page=str(page.relative_to(self.dest))):
                text = page.read_text(encoding="utf-8")
                self.assertNotIn('details class="filters"', text)
                self.assertNotIn('class="filter-label"', text)
                self.assertNotIn('class="filter-links"', text)

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


def flat(text: str) -> str:
    """One line, single-spaced: the page's wrapping is not its wording."""
    return " ".join(text.split())


class ComposerTests(GalleryTestCase):
    """The prompt composer (plan §5.1), in the space the filters gave up.

    Its copy is the mockup's, with one correction the plan makes: a submission
    is not a job (§1.2), so the receipt names no job number and no username —
    at submit time neither exists.
    """

    def setUp(self):
        super().setUp()
        self.render()
        self.index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.failed = (self.dest / "rejections.html").read_text(encoding="utf-8")

    def block(self):
        return self.index.split('<section class="compose"')[1].split("</section>")[0]

    def test_only_the_gallery_index_takes_a_prompt(self):
        self.assertIn('<section class="compose" data-compose hidden>', self.index)
        others = {
            "rejections.html": self.failed,
            f"lines/{self.ids[0]}.html": (
                self.dest / "lines" / f"{self.ids[0]}.html"
            ).read_text(encoding="utf-8"),
            "compare.html": (self.dest / "compare.html").read_text(encoding="utf-8"),
            f"e/{self.ids[0]}/index.html": (
                self.dest / "e" / str(self.ids[0]) / "index.html"
            ).read_text(encoding="utf-8"),
        }
        for name, page in others.items():
            with self.subTest(page=name):
                self.assertNotIn("data-compose", page)

    def test_every_line_of_it_is_the_mockup_s(self):
        page = flat(self.index)
        for line in (
            "<h2>Submit a prompt</h2>",
            "Sign in with GitHub</a> to submit a prompt. "
            "Only your GitHub username is shared and published.",
            ">One sentence describing a sketch. No code.</label>",
            "<b data-left>—</b> of 3 left today",
            ">Queue it</button>",
            '<span class="note">Held for review before it runs</span>',
        ):
            with self.subTest(line=line):
                self.assertIn(flat(line), page)

    def test_the_receipt_promises_a_review_and_names_no_job(self):
        # §1.2: the Worker answers with a submission id and nothing more, so
        # the mockup's "Job #418, submitted by profcarroll" cannot be true.
        page = flat(self.index)
        self.assertIn("<strong>Queued for review.</strong>", page)
        self.assertIn(
            "<p>After review, check back later to see if your sketch was "
            "successfully created.</p>",
            page,
        )
        self.assertNotIn("Job #", self.index)
        self.assertNotIn("submitted by profcarroll.", self.index)

    def test_nothing_shows_until_me_has_answered(self):
        block = self.block()
        # the section itself, and each of the three states inside it
        self.assertIn("data-compose hidden", self.index)
        for hook in ("data-compose-quota", "data-compose-in", "data-compose-receipt"):
            with self.subTest(hook=hook):
                self.assertIn(f"{hook} hidden", block)
        # signed out is the one state that is not hidden: it is what a page
        # with no session, and a page whose script never ran, should say.
        self.assertIn('<p class="signed-out-line" data-compose-out>', block)

    def test_the_composer_holds_no_credential(self):
        block = self.block()
        for mark in ("Bearer", "token", "Authorization", "sketchgen_session"):
            with self.subTest(mark=mark):
                self.assertNotIn(mark, block)
        # the sign-in link's href is written by gallery.js from config.json;
        # the page ships a relative link to itself, not the write path
        self.assertIn('<a class="login" data-login href="index.html">', block)
        self.assertNotIn(self.config.write_path, block)

    def test_an_undeployed_write_path_renders_no_composer(self):
        dest = self.tmp / "nowrite"
        dest.mkdir()
        gallery.render_index(self.conn, dest, gallery.Config(write_path=""))
        index = (dest / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("data-compose", index)
        self.assertNotIn("Submit a prompt", index)


class OffPlanTests(GalleryTestCase):
    """A sketch that runs and diverged is described, not condemned."""

    def page_for(self, **fields):
        job = db.enqueue(self.conn, "a jigsaw puzzle game", "profcarroll")
        self.conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, state="failed-kept", prompt="a jigsaw puzzle game",
            executor="qwen3-coder:30b", published_utc=db.utc_now(), **fields
        )
        self.conn.commit()
        gallery.render_entry(self.conn, entry, self.dest, self.config)
        return (self.dest / "e" / str(entry) / "index.html").read_text(encoding="utf-8")

    def test_an_off_plan_entry_is_not_called_a_rejection(self):
        page = self.page_for(offplan_json=json.dumps(["motion(idle)"]))
        self.assertNotIn("REJECTED —", page)
        self.assertIn("OFF-PLAN", page)
        self.assertIn("this sketch runs", page)
        # and it must not borrow the rejection colour
        self.assertIn('class="chip offplan"', page)

    def test_the_missed_assertions_are_not_named_to_a_visitor(self):
        # motion(idle) is the plan's own notation. The chip says the sketch
        # diverged; which rule it diverged on is under Provenance.
        page = self.page_for(
            offplan_json=json.dumps(["motion(idle)", "responds(click)", "responds(drag)"])
        )
        body = page.split('<h2>Provenance</h2>')[0]
        for name in ("motion(idle)", "responds(click)", "responds(drag)"):
            with self.subTest(name=name):
                self.assertNotIn(name, body)

    def test_a_real_failure_is_still_called_a_rejection(self):
        page = self.page_for()
        self.assertIn("REJECTED —", page)
        self.assertNotIn("OFF-PLAN", page)

    def test_a_row_written_before_the_column_existed_is_not_off_plan(self):
        # _offplan reads a column older rows do not carry; it must not raise.
        self.assertEqual([], gallery._offplan({"id": 1}))


class OffNodeTests(GalleryTestCase):
    """An entry any of whose models ran off the node says so (agentic-cli §1).

    The gallery-wide sentence stopped claiming every model is self-hosted; the
    truth moved onto the entry, which already knew it.
    """

    def publish(self, *, planner="gemma4:e4b", executor="qwen3-coder:30b",
                parent=None, critique_by=None, critic_row=False):
        prompt = "a slow tide of lines"
        if critique_by:
            prompt = lineage.compose_prompt(prompt, "make it slower")
        job = db.enqueue(self.conn, prompt, "profcarroll",
                         parent_entry_id=parent, critique_by=critique_by,
                         critique="make it slower" if critique_by else None)
        self.conn.execute("UPDATE jobs SET state = 'published' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, state="published", prompt=prompt,
            planner=planner, executor=executor, parent_entry_id=parent,
            published_utc=db.utc_now(), submitted_by="profcarroll",
        )
        if critique_by:
            db.add_lineage(self.conn, entry, parent, 2, critique_by, "make it slower")
            if critic_row:
                db.record_critique(self.conn, parent, critique="make it slower",
                                   critique_by=critique_by, prompt_version="critic-v3",
                                   spawned_job_id=job)
        gallery.render_entry(self.conn, entry, self.dest, self.config)
        base = self.dest / "e" / str(entry)
        return (
            (base / "index.html").read_text(encoding="utf-8"),
            json.loads((base / "meta.json").read_text(encoding="utf-8")),
        )

    def test_the_footer_no_longer_claims_every_model_is_self_hosted(self):
        self.render()
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            if "AI Disclosure" not in text:
                continue
            with self.subTest(page=page.name):
                self.assertNotIn("fully attributed self-hosted models", text)
                self.assertIn("off-node", text)

    def test_a_local_entry_wears_no_badge(self):
        page, meta = self.publish()
        self.assertNotIn('class="chip offnode"', page)
        self.assertEqual(meta["off_node"], [])

    def test_a_paid_planner_is_badged_and_named_on_its_own_page(self):
        """Entry 1223's shape: planned by claude-sonnet-5, written locally."""
        page, meta = self.publish(planner="claude-sonnet-5")
        self.assertEqual(meta["off_node"],
                         [{"step": "planner", "model": "claude-sonnet-5"}])
        self.assertIn('Planned by claude-sonnet-5 <span class="chip offnode"', page)
        self.assertIn("claude-sonnet-5 · answered off this node · prompt", page)
        self.assertNotIn("qwen3-coder:30b · answered off this node", page)

    def test_a_paid_executor_is_badged(self):
        page, meta = self.publish(executor="claude-opus-5")
        self.assertEqual([o["step"] for o in meta["off_node"]], ["executor"])
        self.assertIn('written by claude-opus-5 <span class="chip offnode"', page)
        # its numbers are not Ollama's meter, and the page says so
        self.assertIn("as reported by the model", page)
        self.assertIn("round trip, export to import", page)

    def test_a_local_executor_has_no_such_caveat(self):
        page, _ = self.publish()
        self.assertNotIn("as reported by the model", page)
        self.assertNotIn("round trip, export to import", page)

    def test_an_ollama_cloud_tag_is_off_the_node_too(self):
        _, meta = self.publish(executor="gpt-oss:120b-cloud")
        self.assertEqual([o["step"] for o in meta["off_node"]], ["executor"])

    def test_a_paid_critic_is_badged_but_a_person_is_not(self):
        parent = self.ids[0]
        page, meta = self.publish(parent=parent, critique_by="claude-opus-5",
                                  critic_row=True)
        self.assertEqual(meta["off_node"],
                         [{"step": "critic", "model": "claude-opus-5"}])
        self.assertIn('<span class="chip model">claude-opus-5</span> '
                      '<span class="chip offnode"', page)
        # a GitHub username with the same shape, and no critiques row: a person
        page, meta = self.publish(parent=parent, critique_by="octocat")
        self.assertEqual(meta["off_node"], [])
        self.assertIn('<span class="chip person">octocat</span>', page)

    def test_the_card_is_badged_on_the_grid(self):
        self.publish(planner="claude-sonnet-5")
        self.render()
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.assertEqual(index.count('class="chip offnode"'), 1)


class CritiqueFormTests(GalleryTestCase):
    """The critique form (plan §5.2), where the apology used to be."""

    def setUp(self):
        super().setUp()
        self.render()
        self.entry_id = self.ids[1]          # generation 2, a child of entry 1
        self.page = (self.dest / "e" / str(self.entry_id) / "index.html").read_text(
            encoding="utf-8"
        )

    def form(self, page=None):
        text = self.page if page is None else page
        return text.split('<section class="panel critique-form"')[1].split("</section>")[0]

    def test_the_apology_is_gone(self):
        for line in (
            "Spawn a child from a critique",
            "A static page cannot start a job",
            "Ask the operator.",
        ):
            with self.subTest(line=line):
                self.assertNotIn(line, self.page)

    def test_every_line_of_it_is_the_mockup_s(self):
        page = flat(self.page)
        for line in (
            "<h2>Critique this sketch</h2>",
            "Sign in with GitHub</a> to ask for a revision. "
            "One sentence becomes the next generation's prompt.",
            "One sentence, under 40 words, no code.",
            ">Say what should change, not how to write it.</label>",
            '<span class="label">the child\'s prompt</span>',
            ">Submit Critique</button>",
        ):
            with self.subTest(line=line):
                self.assertIn(flat(line), page)

    def test_the_receipt_records_a_critique_and_names_no_job(self):
        page = flat(self.page)
        self.assertIn(
            "<p><strong>Critique recorded.</strong> Queued for review.</p>", page
        )
        self.assertIn(
            "Your username appears on the child entry as the critic, the way the "
            "critic model's name appears on this one.",
            page,
        )
        self.assertIn('<span class="label">what you asked for</span>', page)
        self.assertNotIn("Child job #", self.page)

    def test_the_form_does_not_explain_itself_to_the_pipeline(self):
        # The depth the critique lands at is still recorded; it is simply not
        # a thing the button has to say out loud.
        meta = json.loads(
            (self.dest / "e" / str(self.entry_id) / "meta.json").read_text()
        )
        self.assertEqual(2, meta["lineage"]["generation"])
        form = flat(self.form())
        self.assertNotIn("a person's critique, so the line does not stall here", form)
        self.assertNotIn("generation 3 ·", form)

    def test_the_preview_shows_the_prompt_the_child_would_carry(self):
        form = flat(self.form())
        self.assertIn(
            "<p>the same field, but it holds still and earns it</p>", form
        )
        self.assertIn(
            '<p class="revise"><span class="label">Revise:</span> '
            '<em data-critique-echo>…</em></p>',
            form,
        )

    def test_a_prompt_that_already_holds_revisions_shows_all_of_them(self):
        # A generation-4 parent does not pretend its child inherits one
        # sentence: every Revise: line already on the prompt is in the preview.
        self.conn.execute(
            "UPDATE entries SET prompt = ? WHERE id = ?",
            ("a field of thin blue lines\nRevise: let them thin at the edge",
             self.entry_id),
        )
        self.conn.commit()
        self.render()
        form = flat(
            self.form(
                (self.dest / "e" / str(self.entry_id) / "index.html").read_text(
                    encoding="utf-8"
                )
            )
        )
        self.assertIn("<p>a field of thin blue lines</p>", form)
        self.assertIn(
            '<p class="revise"><span class="label">Revise:</span> '
            "<em>let them thin at the edge</em></p>",
            form,
        )

    def test_nothing_shows_until_me_has_answered(self):
        self.assertIn('data-critique="%d" hidden' % self.entry_id, self.page)
        form = self.form()
        for hook in ("data-critique-in", "data-critique-sent"):
            with self.subTest(hook=hook):
                self.assertIn(f"{hook} hidden", form)
        self.assertIn('<p class="signed-out-line" data-critique-out>', form)

    def test_the_form_holds_no_credential(self):
        form = self.form()
        for mark in ("Bearer", "token", "Authorization", "sketchgen_session"):
            with self.subTest(mark=mark):
                self.assertNotIn(mark, form)
        self.assertIn('<a class="login" data-login href="../../index.html">', form)
        self.assertNotIn(self.config.write_path, form)

    def test_a_rejected_entry_is_offered_no_form(self):
        # lineage.spawn refuses a rejected parent, so a box on that page would
        # be an offer the pipeline will not honour.
        self.conn.execute(
            "UPDATE entries SET state = 'rejected', reject_reason = ?, "
            "published_utc = ? WHERE id = ?",
            ("drifted from the prompt", "2026-09-14T06:00:00Z", self.entry_id),
        )
        self.conn.commit()
        self.render()
        page = (self.dest / "e" / str(self.entry_id) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("data-critique", page)
        self.assertNotIn("Critique this sketch", page)
        # a kept rejection is spawnable and keeps its form
        kept = (self.dest / "e" / str(self.ids[2]) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Critique this sketch", kept)

    def test_an_undeployed_write_path_renders_no_form(self):
        dest = self.tmp / "nowrite"
        dest.mkdir()
        gallery.render_all(self.conn, dest, gallery.Config(write_path=""))
        page = (dest / "e" / str(self.entry_id) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("data-critique", page)
        self.assertNotIn("Critique this sketch", page)


class FormSafetyTests(GalleryTestCase):
    """The guard's question, asked of the two forms: what did they publish?

    Nothing the forms add may carry an absolute path off this machine, a
    credential, or a URL that only resolves on the node. The guard itself
    refuses e-mail addresses and the node's hostname; these are the marks the
    forms could plausibly have introduced and the guard does not look for.
    """

    def test_a_render_with_both_forms_still_passes_the_guard(self):
        self.render()
        gallery.guard(self.dest)          # the read-only form: raises or nothing

    def test_the_forms_publish_no_path_no_token_and_no_local_url(self):
        self.render()
        marks = (
            str(self.tmp),                # this machine's absolute paths
            "/home/",
            "127.0.0.1",
            "localhost",
            "sslip.io",
            "Bearer ",
            "Authorization",
            "sketchgen_session",
        )
        for page in self.pages():
            text = page.read_text(encoding="utf-8")
            for mark in marks:
                with self.subTest(page=str(page.relative_to(self.dest)), mark=mark):
                    self.assertNotIn(mark, text)

    def test_an_email_in_a_prompt_is_still_refused(self):
        # The guard's own rule, re-checked through a page that now has a form
        # on it: the render is undone and nothing is left behind.
        self.conn.execute(
            "UPDATE entries SET prompt = ? WHERE id = ?",
            ("write to someone@example.com about it", self.ids[0]),
        )
        self.conn.commit()
        with self.assertRaises(gallery.Unsafe):
            self.render()


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


class OperatorRejectionTests(GalleryTestCase):
    """§5.2: a rejection a person made is public, and says so in their words."""

    def reject(self, entry_id, reason, published_utc="2026-09-14T06:00:00Z"):
        """Turn one of the fixture's entries into a published operator rejection."""
        self.conn.execute(
            "UPDATE entries SET state = 'rejected', reject_reason = ?, "
            "published_utc = ? WHERE id = ?",
            (reason, published_utc, entry_id),
        )
        self.conn.commit()
        return entry_id

    def test_a_rejected_entry_renders_with_the_operator_chip_and_the_reason(self):
        entry_id = self.reject(self.ids[1], "drifted from the prompt")
        self.render()
        page = (self.dest / "e" / str(entry_id) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("rejected · operator", page)
        self.assertIn(
            "Rejected by the operator: drifted from the prompt", page
        )
        # The automatic wording is not borrowed for a decision a person made.
        self.assertNotIn("REJECTED —", page)
        self.assertEqual(
            "rejected",
            json.loads(
                (self.dest / "e" / str(entry_id) / "meta.json").read_text("utf-8")
            )["state"],
        )

    def test_rejections_html_shows_both_kinds(self):
        gate = self.ids[2]
        operator = self.reject(self.ids[1], "second version better")
        self.render()
        page = (self.dest / "rejections.html").read_text(encoding="utf-8")
        self.assertIn(f'data-entry="{gate}"', page)
        self.assertIn(f'data-entry="{operator}"', page)
        self.assertIn("rejected · automatic", page)
        self.assertIn("rejected · operator", page)
        # Each card's own reason, from its own source: the entry's column for
        # the operator's, the job's last_error for the gate's.
        self.assertIn("second version better", page)
        self.assertIn("AudioContext is suspended", page)
        # ...and it is off the grid.
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(f'data-entry="{operator}"', index)

    def test_a_rejection_with_no_reason_recorded_says_so(self):
        entry_id = self.reject(self.ids[1], None)
        self.render()
        page = (self.dest / "e" / str(entry_id) / "index.html").read_text("utf-8")
        self.assertIn("Rejected by the operator: reason not recorded", page)

    def test_a_rejection_nobody_published_is_on_no_page(self):
        # The same rule the kept failures have had: the state flip is not the
        # decision to show it, the push is (§5.2).
        job = db.enqueue(self.conn, "a rejection nobody pushed", "profcarroll")
        entry_id = db.create_entry(
            self.conn, job, state="rejected", prompt="a rejection nobody pushed",
            reject_reason="off brief",
        )
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(self.conn, entry_id, self.dest, self.config)
        self.render()
        page = (self.dest / "rejections.html").read_text(encoding="utf-8")
        self.assertNotIn(f'data-entry="{entry_id}"', page)

    def test_an_archived_entry_is_never_public(self):
        job = db.enqueue(self.conn, "one taken off the lists", "profcarroll")
        entry_id = db.create_entry(
            self.conn, job, state="archived", prompt="one taken off the lists",
        )
        self.assertNotIn("archived", gallery.PUBLIC_STATES)
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(self.conn, entry_id, self.dest, self.config)
        for publishing in (False, True):
            with self.subTest(publishing=publishing):
                with self.assertRaises(gallery.UnknownEntry):
                    gallery.render_entry(
                        self.conn, entry_id, self.dest, self.config,
                        publishing=publishing,
                    )
        self.render()
        self.assertFalse((self.dest / "e" / str(entry_id)).exists())

    def test_a_rejected_entry_keeps_its_place_in_the_lineage_file(self):
        # The whole point of §5.2: a rejected parent is a public entry with a
        # strip, so a child's ledger has a frame to show instead of a blank.
        entry_id = self.reject(self.ids[1], "prompt drift")
        self.render()
        index = json.loads((self.dest / "lineage.json").read_text("utf-8"))
        item = index["entries"][str(entry_id)]
        self.assertEqual("rejected", item["state"])
        self.assertTrue(item["public"])
        self.assertEqual(f"e/{entry_id}/strip.png", item["strip"])


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


class StandingTests(unittest.TestCase):
    """The percentile maths behind a mark, on tables built by hand.

    _standing is pure: one scores() table in, one place in the pool out. These
    are the cases the render depends on and a browser cannot show us — the tie,
    the pool of one, and the entry nobody judged.
    """

    #: two entries tied on score, so the id has to break it
    TABLE = {
        7: {"score": 2.0, "n": 4, "wins": 3, "losses": 1, "ties": 0},
        3: {"score": 1.0, "n": 1, "wins": 0, "losses": 1, "ties": 0},
        5: {"score": 1.0, "n": 9, "wins": 4, "losses": 4, "ties": 1},
        9: {"score": 0.5, "n": 2, "wins": 0, "losses": 2, "ties": 0},
    }

    def test_the_strongest_entry_tops_the_pool(self):
        stand = gallery._standing(self.TABLE, 7)
        self.assertEqual((stand["rank"], stand["pool"]), (1, 4))
        self.assertAlmostEqual(stand["pct"], 1.0)
        self.assertEqual((stand["score"], stand["n"]), (2.0, 4))

    def test_the_weakest_entry_sits_at_zero(self):
        stand = gallery._standing(self.TABLE, 9)
        self.assertEqual((stand["rank"], stand["pool"]), (4, 4))
        self.assertAlmostEqual(stand["pct"], 0.0)

    def test_a_tie_is_broken_by_entry_id_so_the_render_is_stable(self):
        lower, higher = gallery._standing(self.TABLE, 3), gallery._standing(self.TABLE, 5)
        self.assertEqual((lower["rank"], higher["rank"]), (2, 3))
        self.assertAlmostEqual(lower["pct"], 2 / 3)
        self.assertAlmostEqual(higher["pct"], 1 / 3)

    def test_a_pool_of_one_sits_in_the_middle(self):
        stand = gallery._standing({4: {"score": 1.4, "n": 1}}, 4)
        self.assertEqual((stand["rank"], stand["pool"]), (1, 1))
        self.assertAlmostEqual(stand["pct"], 0.5)

    def test_an_entry_the_population_never_judged_has_no_standing(self):
        self.assertIsNone(gallery._standing(self.TABLE, 11))
        self.assertIsNone(gallery._standing({}, 7))

    def test_opacity_is_the_evidence_in_four_steps(self):
        for n, expected in ((0, 0.0), (1, 0.4), (2, 0.6), (3, 0.6),
                            (4, 0.8), (7, 0.8), (8, 1.0), (40, 1.0)):
            with self.subTest(n=n):
                self.assertEqual(gallery._evidence_opacity(n), expected)


class CardMarksTests(GalleryTestCase):
    """What the two tracks on a card say, and what the chip beside them says.

    The fixture database holds no judgments, so each test seeds exactly the
    ones it is about: a judgment is one row per (judge, pair, question), which
    is what db.record_judgment writes.
    """

    def judge(self, question, kind, judge_id, a, b, choice):
        db.record_judgment(self.conn, a, b, kind, judge_id, question, choice)

    def cards(self, name="index.html"):
        """{entry id: that card's HTML} from one rendered grid page."""
        page = (self.dest / name).read_text(encoding="utf-8")
        out = {}
        for chunk in page.split('<div class="card"')[1:]:
            depth, cut = 1, len(chunk)
            for at in range(len(chunk)):
                if chunk.startswith("<div", at):
                    depth += 1
                elif chunk.startswith("</div>", at):
                    depth -= 1
                    if depth == 0:
                        cut = at
                        break
            body = chunk[:cut]
            entry_id = body.split('data-entry="')[1].split('"')[0]
            out[int(entry_id)] = body
        return out

    def test_a_card_judged_by_both_populations_carries_both_marks(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
            self.judge(question, "agent", "qwen3.5:4b", one, two, "A")
        self.render()
        card = self.cards()[one]
        self.assertEqual(card.count('class="bar-row"'), 2)
        self.assertEqual(card.count('class="mark human"'), 2)
        self.assertEqual(card.count('class="mark agent"'), 2)
        self.assertEqual(card.count('class="gap"'), 2)
        self.assertNotIn(gallery.NO_PAIRS, card)
        # every mark is placed by percentage and says where it stands: this
        # entry won both questions in both populations, so all four sit at the
        # strong end of a pool of two
        self.assertEqual(card.count("left:100.0%;opacity:"), 4)
        self.assertIn('title="humans: 1st of 2 · ', card)
        self.assertIn('title="agents: 1st of 2 · ', card)
        self.assertIn("over 1 pair", card)
        # one pair behind a mark is one pair's worth of certainty
        self.assertIn("opacity:0.4", card)
        self.assertNotIn("opacity:1.0", card)

    def test_an_entry_no_population_has_judged_says_so_in_both_rows(self):
        self.render()
        card = self.cards()[self.ids[0]]
        self.assertEqual(card.count(gallery.NO_PAIRS), 2)
        self.assertNotIn('class="mark human"', card)
        self.assertNotIn('class="mark agent"', card)
        for chip in ("agree", "disagree", "humans only", "agents only"):
            self.assertNotIn(f">{chip}<", card)

    def test_the_chip_says_which_populations_looked_and_whether_they_agree(self):
        one, two, _ = self.ids
        cases = (
            # both populations, same order: agree
            ((("human", "profcarroll", "A"), ("agent", "qwen3.5:4b", "A")), "agree"),
            # both populations, opposite orders: the pool's two ends
            ((("human", "profcarroll", "A"), ("agent", "qwen3.5:4b", "B")), "disagree"),
            ((("human", "profcarroll", "A"),), "humans only"),
            ((("agent", "qwen3.5:4b", "A"),), "agents only"),
        )
        for votes, expected in cases:
            with self.subTest(chip=expected):
                self.conn.execute("DELETE FROM judgments")
                for kind, judge_id, choice in votes:
                    self.judge("look", kind, judge_id, one, two, choice)
                shutil.rmtree(self.dest, True)
                self.dest.mkdir()
                self.render()
                card = self.cards()[one]
                self.assertIn(f">{expected}</span>", card)
                for other in ("agree", "disagree", "humans only", "agents only"):
                    if other != expected and not expected.startswith(other):
                        self.assertNotIn(f">{other}</span>", card)

    def test_the_rejections_page_gets_the_bars_too(self):
        self.render()
        card = self.cards("rejections.html")[self.ids[2]]
        self.assertIn('class="standing"', card)
        self.assertEqual(card.count('class="bar-row"'), 2)

    def test_the_score_sentences_left_the_grid_but_not_the_entry_page(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
        self.render()
        index = (self.dest / "index.html").read_text(encoding="utf-8")
        entry = (self.dest / "e" / str(one) / "index.html").read_text(encoding="utf-8")
        for gone in ('class="card-scores"', 'class="card-briefs"',
                     'class="score-value"', "closer to its brief:"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, index)
        # the entry page is untouched: the numbers are still spelled out there
        self.assertIn("over 1 pair", entry)
        self.assertIn("closer to its brief:", entry)
        self.assertNotIn('class="standing"', entry)


class CompassTests(unittest.TestCase):
    """The square itself, on score tables built by hand.

    _compass is pure, so the coordinates can be checked exactly — which is the
    part a browser cannot show us. A 160 square pads by size/12, so the strong
    end of an axis is at 146.7 and the weak end at 13.3.
    """

    STRONG, WEAK = "146.7", "13.3"

    #: entry 1 above entry 2 in a pool of two, and the other way round
    WON = {1: {"score": 2.0, "n": 4}, 2: {"score": 1.0, "n": 4}}
    LOST = {1: {"score": 1.0, "n": 4}, 2: {"score": 2.0, "n": 4}}

    @staticmethod
    def scores(human=None, agent=None):
        """A scores() shape with only the questions each population answered.

        `human`/`agent` are {"look": table, "brief": table}; a question left out
        is one that population has not judged at all.
        """
        empty = {"look": {}, "brief": {}}
        return {
            "human": {**empty, **(human or {})},
            "agent": {**empty, **(agent or {})},
        }

    def test_a_population_strong_on_both_questions_sits_top_right(self):
        square = gallery._compass(
            self.scores(human={"look": self.WON, "brief": self.WON}), 1
        )
        self.assertIn(f'cx="{self.STRONG}" cy="{self.WEAK}"', square)
        self.assertIn('class="c-mark human"', square)
        self.assertIn(">humans: looks good, on brief<", square)
        # one mark, so there is no gap to draw and no second phrase
        self.assertNotIn('class="c-gap"', square)
        self.assertNotIn('class="c-mark agent"', square)
        self.assertNotIn("agents:", square)
        # the facts are in the title, both questions, not on the square
        self.assertIn(
            "<title>humans: look 1st of 2 (2.00 over 4 pairs) · "
            "brief 1st of 2 (2.00 over 4 pairs)</title>",
            square,
        )
        self.assertIn("→ rather look at it · ↑ closer to its brief", square)

    def test_a_population_weak_on_both_questions_sits_bottom_left(self):
        square = gallery._compass(
            self.scores(human={"look": self.LOST, "brief": self.LOST}), 1
        )
        self.assertIn(f'cx="{self.WEAK}" cy="{self.STRONG}"', square)
        self.assertIn(">humans: neither<", square)

    def test_two_marks_draw_the_gap_and_each_quadrant_gets_its_words(self):
        # the humans put entry 1 top of the look pool and bottom of the brief
        # pool; the agents read it the other way round, which is the whole
        # point of the square: they disagree about *which* question it answers
        square = gallery._compass(
            self.scores(
                human={"look": self.WON, "brief": self.LOST},
                agent={"look": self.LOST, "brief": self.WON},
            ),
            1,
        )
        self.assertIn(
            f'<line x1="{self.STRONG}" y1="{self.STRONG}" '
            f'x2="{self.WEAK}" y2="{self.WEAK}" class="c-gap"/>',
            square,
        )
        self.assertIn(">humans: looks good, misses the brief<", square)
        self.assertIn(">agents: on brief, not much to look at<", square)
        self.assertEqual(square.count('class="c-mark'), 2)

    def test_the_mark_carries_the_evidence_of_the_thinner_question(self):
        # eight pairs on looks, one on the brief: the point rests on the one
        many = {1: {"score": 2.0, "n": 8}, 2: {"score": 1.0, "n": 8}}
        few = {1: {"score": 2.0, "n": 1}, 2: {"score": 1.0, "n": 1}}
        thin = gallery._compass(self.scores(human={"look": many, "brief": few}), 1)
        solid = gallery._compass(self.scores(human={"look": many, "brief": many}), 1)
        self.assertIn("opacity:0.4", thin)
        self.assertIn('r="9.7"', thin)
        self.assertIn("opacity:1.0", solid)
        self.assertIn('r="11.7"', solid)

    def test_one_question_alone_places_no_mark_and_says_nothing(self):
        square = gallery._compass(self.scores(human={"look": self.WON}), 1)
        self.assertNotIn('class="c-mark', square)
        self.assertNotIn("humans:", square)
        self.assertIn(">no pairs</text>", square)
        self.assertIn(
            "neither population has scored this entry on both questions", square
        )

    def test_an_entry_nobody_judged_still_draws_its_square(self):
        square = gallery._compass(self.scores(), 1)
        self.assertIn('class="compass"', square)
        self.assertIn('class="c-ground"', square)
        self.assertEqual(square.count('class="c-axis"'), 2)
        self.assertIn('text-anchor="middle" class="c-none">no pairs</text>', square)
        # "no pairs", not "no pairs yet": the score boxes below say that part
        self.assertNotIn(gallery.NO_PAIRS, square)

    def test_the_whole_drawing_scales_with_the_size(self):
        square = gallery._compass(
            self.scores(human={"look": self.WON, "brief": self.WON}), 1, size=96
        )
        self.assertIn('viewBox="0 0 96 96" width="96" height="96"', square)
        self.assertIn('cx="88.0" cy="8.0"', square)


class EntryCompassTests(GalleryTestCase):
    """Where the compass goes: the Judgment panel of an entry page, only."""

    def judge(self, question, kind, judge_id, a, b, choice):
        db.record_judgment(self.conn, a, b, kind, judge_id, question, choice)

    def panel(self, entry_id):
        """The Judgment panel of one rendered entry page."""
        page = (self.dest / "e" / str(entry_id) / "index.html").read_text(
            encoding="utf-8"
        )
        return page.split("<h2>Judgment</h2>")[1].split("</section>")[0]

    def test_the_compass_is_in_the_judgment_panel_above_the_score_boxes(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
            self.judge(question, "agent", "qwen3.5:4b", one, two, "B")
        self.render()
        panel = self.panel(one)
        self.assertIn('<div class="compass-wrap">', panel)
        self.assertIn('class="c-mark human"', panel)
        self.assertIn('class="c-mark agent"', panel)
        self.assertIn('class="c-gap"', panel)
        self.assertIn(">humans: looks good, on brief<", panel)
        self.assertIn(">agents: neither<", panel)
        self.assertLess(panel.index("compass-wrap"), panel.index('<div class="two">'))
        # the numbers stay: the square is added to the panel, not swapped in
        self.assertIn("over 1 pair", panel)
        self.assertIn("closer to its brief:", panel)

    def test_an_unjudged_entry_keeps_the_panel_the_same_shape(self):
        self.render()
        panel = self.panel(self.ids[0])
        self.assertIn('<div class="compass-wrap">', panel)
        self.assertIn(">no pairs</text>", panel)
        self.assertEqual(panel.count(gallery.NO_PAIRS), 2)

    def test_the_grid_pages_do_not_carry_the_compass(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
        self.render()
        for name in ("index.html", "rejections.html"):
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                self.assertNotIn("compass", page)
                self.assertNotIn('class="quads"', page)


# ---------------------------------------------------------------------------
# The kiosk (docs/plans/kiosk.md §5)
# ---------------------------------------------------------------------------


class KioskManifestTests(GalleryTestCase):
    """``kiosk.json``: what a projector is handed before the first sketch.

    The manifest is a second serialisation of values meta.json and the cards
    already carry, so what these tests are really asking is whether the second
    copy can disagree with the first — by carrying an entry the grid does not,
    by inventing a zero where a population has not voted, or by coming out
    differently on a second render of the same rows.
    """

    def setUp(self):
        super().setUp()
        self.render()

    def manifest(self):
        return json.loads((self.dest / "kiosk.json").read_text(encoding="utf-8"))

    def rows(self):
        return {int(entry["id"]): entry for entry in self.manifest()["entries"]}

    def judge(self, question, kind, judge_id, a, b, choice):
        db.record_judgment(self.conn, a, b, kind, judge_id, question, choice)

    #: Every key spec §2 names, written out rather than imported so that the
    #: test is a check on the shape and not a restatement of it. ``canvas`` and
    #: ``responds`` are absent from the list on purpose: they are the two
    #: optional keys, and ``responds`` arrived with the ghost pointer
    #: (docs/plans/auto-mouse.md §3.2), which is the only thing the kiosk does
    #: with it.
    SPEC_2_KEYS = {
        "id",
        "prompt",
        "brief",
        "statement",
        "submitted_by",
        "planner",
        "executor",
        "rules_file",
        "attempts",
        "seed",
        "created_utc",
        "published_utc",
        "prompt_tokens",
        "completion_tokens",
        "wall_s",
        "licence",
        "generation",
        "parent_entry_id",
        "critique_by",
        "root_entry_id",
        "sketch",
        "source",
        "href",
        "judgment",
        # qr.md §4 adds these two: the path of the entry's kiosk code, and the
        # clean URL the kiosk prints under it.
        "qr",
        "url",
    }

    def test_render_index_writes_the_page_and_the_manifest(self):
        for name in ("kiosk.html", "kiosk.json"):
            with self.subTest(file=name):
                self.assertTrue((self.dest / name).is_file())

    def test_render_index_alone_writes_them_too(self):
        # render_all is render_index plus the entry directories; a node that
        # only re-indexes must still get the kiosk.
        shutil.rmtree(self.dest)
        self.dest.mkdir()
        gallery.render_index(self.conn, self.dest, self.config)
        for name in ("kiosk.html", "kiosk.json"):
            with self.subTest(file=name):
                self.assertTrue((self.dest / name).is_file())

    def test_both_files_pass_the_guard(self):
        # The read-only guard walks every text file in the checkout, which is
        # the form that would catch a manifest smuggling a mail address out of
        # a statement or a brief.
        gallery.guard(self.dest)

    def test_a_statement_with_an_email_in_it_refuses_the_whole_render(self):
        # The manifest carries the statement verbatim, so it is one more file
        # personal data could reach the public repository through.
        self.conn.execute(
            "UPDATE entries SET statement = ? WHERE id = ?",
            ("Ask nobody@example.invalid what it means.", self.ids[0]),
        )
        self.conn.commit()
        with self.assertRaises(gallery.Unsafe) as caught:
            gallery.render_index(self.conn, self.dest, self.config)
        self.assertIn("email-shaped string", str(caught.exception))

    def test_only_published_entries_are_in_it(self):
        one, two, kept = self.ids
        held = add_child(self.conn, self.tmp, two, state="held")
        self.render()
        ids = [int(entry["id"]) for entry in self.manifest()["entries"]]
        self.assertEqual([one, two], ids)
        self.assertNotIn(kept, ids)
        self.assertNotIn(held, ids)

    def test_the_entries_are_in_ascending_id_order(self):
        # _entries returns created_utc order; the manifest promises id order,
        # because the browser's tie-breaks are by id and a stable file is
        # easier to diff.
        third = add_child(self.conn, self.tmp, self.ids[0], state="published")
        self.render()
        ids = [int(entry["id"]) for entry in self.manifest()["entries"]]
        self.assertEqual(sorted(ids), ids)
        self.assertIn(third, ids)

    def test_every_spec_key_is_present_for_a_root_and_for_a_child(self):
        rows = self.rows()
        for name, entry_id in (("root", self.ids[0]), ("child", self.ids[1])):
            with self.subTest(entry=name):
                self.assertEqual(
                    self.SPEC_2_KEYS, set(rows[entry_id]) - {"canvas", "responds"}
                )

    def test_every_row_names_its_kiosk_code_and_its_clean_url(self):
        # The manifest is read by one page and that page is the projection, so
        # the path it carries is the -kiosk code. The url beside it is the
        # clean one, because that is what the kiosk *prints* under the code
        # and not what the code holds (qr.md §4, §5.3).
        rows = self.rows()
        for name, entry_id in (("root", self.ids[0]), ("child", self.ids[1])):
            with self.subTest(entry=name):
                row = rows[entry_id]
                self.assertEqual(f"e/{entry_id}/qr-kiosk.svg", row["qr"])
                self.assertEqual(
                    f"https://profcarroll.github.io/sketchgen-gallery/e/{entry_id}/",
                    row["url"],
                )
                self.assertNotIn("kiosk", row["url"])
                # And the file that path names is actually there.
                self.assertTrue((self.dest / row["qr"]).is_file())

    def test_the_url_is_the_same_string_meta_json_publishes(self):
        entry_id = self.ids[0]
        meta = json.loads(
            (self.dest / "e" / str(entry_id) / "meta.json").read_text(encoding="utf-8")
        )
        self.assertEqual(meta["source"]["entry"], self.rows()[entry_id]["url"])

    def test_a_root_says_so_with_nulls_and_a_child_names_its_parent(self):
        one, two, _ = self.ids
        rows = self.rows()
        self.assertIsNone(rows[one]["parent_entry_id"])
        self.assertIsNone(rows[one]["critique_by"])
        self.assertEqual(1, rows[one]["generation"])
        self.assertEqual(one, rows[one]["root_entry_id"])
        self.assertEqual(one, rows[two]["parent_entry_id"])
        self.assertEqual("gemma4:e4b", rows[two]["critique_by"])
        self.assertEqual(2, rows[two]["generation"])
        self.assertEqual(one, rows[two]["root_entry_id"])

    def test_the_provenance_is_the_same_values_meta_json_carries(self):
        one = self.ids[0]
        meta = json.loads(
            (self.dest / "e" / str(one) / "meta.json").read_text(encoding="utf-8")
        )
        row = self.rows()[one]
        for key in gallery.KIOSK_META_KEYS:
            with self.subTest(key=key):
                self.assertEqual(meta[key], row[key])

    def test_responds_names_what_the_gate_confirmed(self):
        # Entry one's assertions are motion(idle) and responds(click). The
        # kiosk reads this for one thing: whether to put ?ghost= on the
        # frame's src, so a sketch that waits to be touched moves on a wall
        # nobody is standing at (docs/plans/auto-mouse.md §3.2).
        self.assertEqual(["click"], self.rows()[self.ids[0]]["responds"])

    def test_an_off_plan_miss_is_subtracted_before_the_kiosk_sees_it(self):
        # assertions_json is what the planner asked for; offplan_json is what
        # the gate published anyway with some of them missed. A projector that
        # sent a pointer at a sketch the gate proved does not respond would be
        # spending a minute of the room's attention on nothing.
        one = self.ids[0]
        self.conn.execute(
            "UPDATE entries SET offplan_json = ? WHERE id = ?",
            (json.dumps(["responds(click)"]), one),
        )
        self.conn.commit()
        self.render()
        self.assertNotIn("responds", self.rows()[one])

    def test_a_sketch_with_nothing_to_respond_to_has_no_responds_key(self):
        # Absent, not an empty list: one less thing for the script to test for.
        two = self.ids[1]
        self.conn.execute(
            "UPDATE entries SET assertions_json = ? WHERE id = ?",
            (json.dumps(["motion(idle)"]), two),
        )
        self.conn.commit()
        self.render()
        self.assertNotIn("responds", self.rows()[two])

    def test_the_two_manifests_cannot_disagree_about_it(self):
        # Both come out of one _manifest_base per entry, which is the whole
        # reason responds moved there when the kiosk started reading it.
        swipe = {
            int(entry["id"]): entry.get("responds")
            for entry in json.loads(
                (self.dest / "swipe.json").read_text(encoding="utf-8"))["entries"]
        }
        kiosk = {
            entry_id: row.get("responds") for entry_id, row in self.rows().items()
        }
        self.assertEqual(swipe, kiosk)

    def test_where_to_run_it_and_where_to_read_it(self):
        one = self.ids[0]
        row = self.rows()[one]
        self.assertEqual(f"e/{one}/sketch/", row["sketch"])
        self.assertEqual(f"e/{one}/sketch/sketch.js", row["source"])
        self.assertEqual(f"e/{one}/", row["href"])
        # and all three are really there, relative to the gallery root
        self.assertTrue((self.dest / row["source"]).is_file())
        self.assertTrue((self.dest / row["href"] / "index.html").is_file())

    def test_an_unjudged_population_is_absent_not_zero(self):
        """A score nobody voted on is not a low score (spec §2).

        The fixture holds no judgments at all, so both populations are missing;
        seeding one human pair on one question brings ``human`` back with that
        question alone, and leaves ``agent`` away.
        """
        one, two, _ = self.ids
        self.assertEqual({}, self.rows()[one]["judgment"])
        self.judge("look", "human", "profcarroll", one, two, "A")
        self.render()
        judgment = self.rows()[one]["judgment"]
        self.assertNotIn("agent", judgment)
        self.assertEqual({"look"}, set(judgment["human"]))

    def test_a_judged_entry_carries_score_n_and_pct_and_nothing_else(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
            self.judge(question, "agent", "qwen3.5:4b", one, two, "B")
        self.render()
        rows = self.rows()
        for entry_id in (one, two):
            for population in ("human", "agent"):
                for question in ("look", "brief"):
                    with self.subTest(entry=entry_id, population=population,
                                      question=question):
                        cell = rows[entry_id]["judgment"][population][question]
                        # rank and pool place a mark on a card's track; the
                        # kiosk has no track, so they are dropped
                        self.assertEqual({"score", "n", "pct"}, set(cell))
                        self.assertEqual(1, cell["n"])
        # and the numbers are the ones the cards were drawn from
        scores = gallery._all_scores(self.conn)
        stand = gallery._standing(scores["human"]["look"], one)
        self.assertEqual(stand["score"], rows[one]["judgment"]["human"]["look"]["score"])
        self.assertEqual(stand["pct"], rows[one]["judgment"]["human"]["look"]["pct"])

    def test_a_second_render_gives_the_same_bytes(self):
        # No clock is read and the order is total, which is what lets the
        # publisher's commit mean something.
        first = (self.dest / "kiosk.json").read_bytes()
        self.render()
        self.assertEqual(first, (self.dest / "kiosk.json").read_bytes())


class KioskCanvasTests(GalleryTestCase):
    """The one thing the kiosk cannot ask the sketch itself (spec §4.3).

    The frame is ``allow-scripts`` without ``allow-same-origin`` and therefore
    opaque, so a fixed-size canvas would sit top-left in a stage-sized frame
    with nothing to centre it. The generator reads the source instead. Each
    case here rewrites the attempt directory's own ``sketch.js`` — the file
    ``_write_entry`` copies — and asks what the manifest then says.
    """

    def sketch_says(self, call):
        """The manifest row for entry one, with its sketch rewritten to ``call``."""
        one = self.ids[0]
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id = ?", (one,)
        ).fetchone()
        (Path(row["source_dir"]) / "sketch.js").write_text(
            "function setup() {\n  %s;\n}\nfunction draw() { background(0); }\n" % call,
            encoding="utf-8",
        )
        self.render()
        entries = json.loads(
            (self.dest / "kiosk.json").read_text(encoding="utf-8")
        )["entries"]
        return next(entry for entry in entries if int(entry["id"]) == one)

    def test_two_integer_literals_give_a_size(self):
        self.assertEqual([800, 600], self.sketch_says("createCanvas(800, 600)")["canvas"])

    def test_a_third_argument_does_not_take_the_size_away(self):
        # 27 of the 222 published sketches are WEBGL; they have a size like
        # any other and the regex stops before the third argument.
        self.assertEqual(
            [640, 480], self.sketch_says("createCanvas(640, 480, WEBGL)")["canvas"]
        )

    def test_a_window_sized_sketch_has_no_key_at_all(self):
        # Absent, not null and not zero: no key is what tells the browser to
        # let the frame fill the stage.
        self.assertNotIn(
            "canvas", self.sketch_says("createCanvas(windowWidth, windowHeight)")
        )

    def test_variables_are_not_a_size(self):
        self.assertNotIn("canvas", self.sketch_says("createCanvas(w, h)"))

    def test_whitespace_between_the_literals_is_allowed(self):
        self.assertEqual(
            [1024, 768], self.sketch_says("createCanvas(  1024 ,\n    768 )")["canvas"]
        )


class EntryQrCodeTests(GalleryTestCase):
    """``e/<id>/qr.svg`` and ``e/<id>/qr-kiosk.svg`` (qr.md §4, §7).

    The two files go in beside ``meta.json`` for every entry with a page, and
    ``render_index`` writes them rather than ``render_entry``: they depend on
    the entry's id and on ``config.gallery_url`` and on nothing else about the
    entry, which is what lets a deploy pick them up without re-rendering 222
    entry pages.

    Every assertion about what a code *holds* goes through the decoder in
    ``test_qr.py`` — the same walk a phone makes — rather than through the
    encoder, so a code that is written correctly and encoded wrongly fails
    here too.
    """

    def codes(self, entry_id):
        return (
            self.dest / "e" / str(entry_id) / "qr.svg",
            self.dest / "e" / str(entry_id) / "qr-kiosk.svg",
        )

    def test_render_index_alone_writes_both_for_every_public_entry(self):
        # render_index, not render_all: this is what update.sh runs on every
        # deploy, and it is the whole of what the kiosk needs.
        gallery.render_index(self.conn, self.dest, self.config)
        for entry_id in self.ids:
            for path in self.codes(entry_id):
                with self.subTest(file=str(path.relative_to(self.dest))):
                    self.assertTrue(path.is_file())

    def test_render_all_writes_them_too(self):
        self.render()
        for entry_id in self.ids:
            for path in self.codes(entry_id):
                self.assertTrue(path.is_file(), str(path))

    def test_a_kept_rejection_gets_them_and_a_held_entry_does_not(self):
        # Both kinds of public entry have a page that links the file; a held
        # entry has no directory at all, and publication holds for a person.
        _one, two, kept = self.ids
        held = add_child(self.conn, self.tmp, two, state="held")
        self.render()
        for path in self.codes(kept):
            self.assertTrue(path.is_file(), f"kept rejection {kept}")
        for path in self.codes(held):
            self.assertFalse(path.exists(), f"held entry {held}")
        self.assertFalse((self.dest / "e" / str(held)).exists())

    def test_both_files_pass_the_guard(self):
        self.render()
        gallery.guard(self.dest)

    def test_each_code_holds_its_own_entry_s_url_and_nothing_else(self):
        self.render()
        for entry_id in self.ids:
            clean, kiosk = self.codes(entry_id)
            url = self.config.entry_url(entry_id)
            with self.subTest(entry=entry_id):
                self.assertEqual(url, decode(clean.read_text(encoding="utf-8")))
                self.assertEqual(
                    url + "?kiosk", decode(kiosk.read_text(encoding="utf-8"))
                )

    def test_the_entry_page_s_code_carries_no_param(self):
        # Someone scanning a laptop on a lectern did not scan a projection,
        # and the param would be a lie in the only place the difference is
        # measurable (§6.1).
        self.render()
        for entry_id in self.ids:
            payload = decode(self.codes(entry_id)[0].read_text(encoding="utf-8"))
            self.assertNotIn("?", payload)
            self.assertNotIn("kiosk", payload)

    def test_the_two_files_differ(self):
        self.render()
        for entry_id in self.ids:
            clean, kiosk = self.codes(entry_id)
            self.assertNotEqual(clean.read_bytes(), kiosk.read_bytes())

    def test_a_different_gallery_url_gives_different_codes(self):
        self.render()
        was = {
            entry_id: self.codes(entry_id)[0].read_bytes() for entry_id in self.ids
        }
        elsewhere = gallery.Config(
            write_path=self.config.write_path,
            gallery_url="https://example.invalid/elsewhere/",
        )
        gallery.render_all(self.conn, self.dest, elsewhere)
        for entry_id in self.ids:
            clean, kiosk = self.codes(entry_id)
            with self.subTest(entry=entry_id):
                self.assertNotEqual(was[entry_id], clean.read_bytes())
                self.assertEqual(
                    f"https://example.invalid/elsewhere/e/{entry_id}/",
                    decode(clean.read_text(encoding="utf-8")),
                )
                self.assertEqual(
                    f"https://example.invalid/elsewhere/e/{entry_id}/?kiosk",
                    decode(kiosk.read_text(encoding="utf-8")),
                )

    def test_two_renders_of_one_database_write_identical_bytes(self):
        # Determinism is a property of this packet and not an aspiration: no
        # clock, no randomness, sorted iteration.
        self.render()
        first = {
            entry_id: [path.read_bytes() for path in self.codes(entry_id)]
            for entry_id in self.ids
        }
        cross_a_second()
        self.render()
        for entry_id in self.ids:
            self.assertEqual(
                first[entry_id],
                [path.read_bytes() for path in self.codes(entry_id)],
                f"entry {entry_id}",
            )

    def test_every_code_in_this_gallery_is_the_same_size(self):
        # §1.5's budget, where it actually matters: a code that changes size
        # as the slideshow advances reads as a bug, and a version bump costs
        # about a tenth of the distance a phone scans from.
        self.render()
        boxes = set()
        for entry_id in self.ids:
            for path in self.codes(entry_id):
                boxes.add(
                    re.search(
                        r'viewBox="([^"]+)"', path.read_text(encoding="utf-8")
                    ).group(1)
                )
        self.assertEqual({"0 0 41 41"}, boxes)

    def test_a_gallery_url_too_long_to_encode_refuses_the_whole_render(self):
        # TooLong is a ValueError, so it comes out of render_index the way
        # Unsafe does and _Written.undo() puts the checkout back.
        self.render()
        before = sorted(
            str(path.relative_to(self.dest)) for path in self.dest.rglob("*")
        )
        huge = gallery.Config(gallery_url="https://" + "n" * 120 + ".invalid/")
        with self.assertRaises(ValueError) as caught:
            gallery.render_index(self.conn, self.dest, huge)
        self.assertIn("version 6 at level M holds 106", str(caught.exception))
        self.assertEqual(
            before,
            sorted(str(path.relative_to(self.dest)) for path in self.dest.rglob("*")),
        )


class KioskPageTests(GalleryTestCase):
    """``kiosk.html`` is a shell, and the nav that reaches it.

    The page carries no entry data by design: ``kiosk.js`` fetches
    ``kiosk.json``, so a gallery of 222 entries does not put 222 prompts into
    every projector's first paint, and the page never goes stale between an
    entry landing and the next full render.
    """

    #: Verbatim from docs/plans/kiosk-mockup/kiosk.html. The copy is the visual
    #: spec's, and the start card is the only copy in the page a person reads
    #: before anything runs.
    START_CARD = (
        "sketchgen · kiosk",
        "Sketches from the gallery play one after another, full screen.",
        "Press any key at any time for the controls.",
        "Start",
        "This first press is the one gesture the browser needs before it will "
        "run audio or go full screen.",
    )

    def setUp(self):
        super().setUp()
        self.render()
        self.kiosk = (self.dest / "kiosk.html").read_text(encoding="utf-8")

    def test_the_start_card_says_what_the_mockup_says(self):
        for line in self.START_CARD:
            with self.subTest(line=line):
                self.assertIn(line, self.kiosk)

    def test_the_page_carries_no_entry_data(self):
        self.assertNotIn("data-entry", self.kiosk)
        self.assertNotIn("sketchgen-entries", self.kiosk)
        for row in self.conn.execute("SELECT prompt FROM entries"):
            with self.subTest(prompt=row["prompt"][:32]):
                self.assertNotIn(str(row["prompt"]), self.kiosk)

    def test_exactly_one_kiosk_script_tag(self):
        scripts = [
            attrs.get("src", "")
            for tag, attrs in elements(self.kiosk)
            if tag == "script" and attrs.get("src")
        ]
        # kiosk.js alone. gallery.js would bring the session line, but its
        # ready() also asks the write path /me with the viewer's cookie and
        # bearer token on every load, and a projector has no business sending
        # either: nothing is fetched but kiosk.json, config.json, /counts and
        # the frames (spec §1.11). kiosk.js carries its own copy of base().
        self.assertEqual(["./assets/kiosk.js"], scripts)
        # No p5 and no kiosk-data.js: the mockup loads both because an artefact
        # cannot frame the gallery, and the real page frames it (spec §1.4).
        self.assertNotIn("p5.min.js", self.kiosk)
        self.assertNotIn("kiosk-data.js", self.kiosk)

    def test_the_body_is_the_kiosk_from_load(self):
        # The CSS hangs the whole layout off body.kiosk; adding the class in
        # script would show the gallery's own page first and then repaint.
        self.assertIn('<body class="kiosk">', self.kiosk)

    def test_the_menu_note_describes_the_live_page_not_the_mockup(self):
        # The mockup's numbers are invented and its note says so. Here they
        # are real, and so is the view a sketch earns by staying on the stage
        # (docs/plans/kiosk-views.md §3.1). The note is the only place the
        # room is told that, so it is pinned here: kiosk.js can stop counting
        # without anybody noticing, but it cannot stop saying so.
        self.assertIn(
            "Views and likes are live from the write path; a sketch counts as "
            "a view once it has been on screen for ten seconds.",
            self.kiosk,
        )
        self.assertNotIn("example numbers", self.kiosk)
        self.assertNotIn("never counted as a view", self.kiosk)

    def test_the_page_parses(self):
        self.assertEqual([], balance_errors(self.dest / "kiosk.html"))

    def test_every_page_links_to_the_kiosk_after_compare(self):
        one = self.ids[0]
        pages = [
            "index.html",
            "rejections.html",
            "compare.html",
            "kiosk.html",
            f"e/{one}/index.html",
            f"lines/{one}.html",
        ]
        for name in pages:
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                nav = page.split('<p class="nav">')[1].split("</p>")[0]
                self.assertLess(nav.index("compare.html"), nav.index("kiosk.html"))
                self.assertIn(">kiosk</a>", nav)

    def test_the_kiosk_marks_itself_current_and_no_other_page_does(self):
        one = self.ids[0]
        nav = self.kiosk.split('<p class="nav">')[1].split("</p>")[0]
        self.assertIn('kiosk.html" aria-current="page"', nav)
        for name in ("index.html", "compare.html", f"e/{one}/index.html"):
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                other = page.split('<p class="nav">')[1].split("</p>")[0]
                self.assertNotIn("aria-current", other)

    def test_the_code_sits_at_the_lower_left_at_every_width(self):
        # qr.md §5.2: the tile is where the eye starts and the words run to
        # the right of it. The move is CSS and not markup — captionShell()
        # still emits .words first, because the code is a decorative tile and
        # the words are the content — so `order: -1` is the whole of it.
        #
        # These two only make sense together. `order: -1` reverses a row, and
        # it reverses a column too, so the stacked rule below 760 px has to be
        # plain `column`: with `column-reverse` the two reversals cancel and
        # the code lands under the words, off the bottom of a phone-shaped
        # screen — the one thing that rule exists to prevent. Revert either
        # one alone and the code is somewhere nobody wants it.
        css = (self.dest / "assets" / "gallery.css").read_text(encoding="utf-8")
        start = css.index("body.kiosk .caption .qr {")
        self.assertIn("order: -1", css[start:css.index("}", start)])
        narrow = css.split("@media (max-width: 760px) {")[1]
        start = narrow.index("body.kiosk .caption { flex-direction:")
        rule = narrow[start:narrow.index("}", start)]
        self.assertIn("flex-direction: column;", rule)
        self.assertNotIn("column-reverse", rule)

    def test_a_line_page_reaches_the_kiosk_from_one_level_down(self):
        # lines/<root>.html renders with root="../"; a kiosk link that forgot
        # it would 404 from there and nowhere else.
        line = (self.dest / "lines" / f"{self.ids[0]}.html").read_text(encoding="utf-8")
        self.assertIn('href="../kiosk.html"', line)


# ---------------------------------------------------------------------------
# Swipe mode (docs/plans/swipe.md §2, §3, §6)
# ---------------------------------------------------------------------------


class SwipeManifestTests(GalleryTestCase):
    """``swipe.json``: the kiosk's rows with the prose taken out.

    kiosk.json is 2.8 MB because it carries every brief and statement in the
    gallery; the phone shows one of each at a time and fetches meta.json when
    the words sheet opens (spec §1.2). So what these tests ask is whether the
    slimmer file is the same rows — same set, same order, same numbers — and
    whether the prose really left, because a manifest that quietly kept it
    would be the kiosk's file under another name.
    """

    def setUp(self):
        super().setUp()
        self.render()

    def manifest(self):
        return json.loads((self.dest / "swipe.json").read_text(encoding="utf-8"))

    def rows(self):
        return {int(entry["id"]): entry for entry in self.manifest()["entries"]}

    def judge(self, question, kind, judge_id, a, b, choice):
        db.record_judgment(self.conn, a, b, kind, judge_id, question, choice)

    def set_assertions(self, entry_id, assertions):
        self.conn.execute(
            "UPDATE entries SET assertions_json = ? WHERE id = ?",
            (json.dumps(assertions), entry_id),
        )
        self.conn.commit()
        self.render()

    #: Every key spec §2 names, written out rather than imported so that the
    #: test is a check on the shape and not a restatement of it. ``canvas``,
    #: ``responds`` and ``mic`` are absent from the list on purpose: they are
    #: the three optional keys.
    SPEC_2_KEYS = {
        "id",
        "prompt",
        "submitted_by",
        "planner",
        "executor",
        "rules_file",
        "attempts",
        "published_utc",
        "generation",
        "parent_entry_id",
        "critique_by",
        "judgment",
        "sketch",
        "meta",
        "href",
        "url",
    }

    OPTIONAL = {"canvas", "responds", "mic"}

    def test_render_index_writes_the_page_and_the_manifest(self):
        for name in ("swipe.html", "swipe.json"):
            with self.subTest(file=name):
                self.assertTrue((self.dest / name).is_file())

    def test_render_index_alone_writes_them_too(self):
        # As the kiosk's: a node that only re-indexes must still get the phone.
        shutil.rmtree(self.dest)
        self.dest.mkdir()
        gallery.render_index(self.conn, self.dest, self.config)
        for name in ("swipe.html", "swipe.json"):
            with self.subTest(file=name):
                self.assertTrue((self.dest / name).is_file())

    def test_both_files_pass_the_guard(self):
        gallery.guard(self.dest)

    def test_only_published_entries_are_in_it(self):
        one, two, kept = self.ids
        held = add_child(self.conn, self.tmp, two, state="held")
        self.render()
        ids = [int(entry["id"]) for entry in self.manifest()["entries"]]
        self.assertEqual([one, two], ids)
        self.assertNotIn(kept, ids)
        self.assertNotIn(held, ids)

    def test_the_entries_are_in_ascending_id_order(self):
        third = add_child(self.conn, self.tmp, self.ids[0], state="published")
        self.render()
        ids = [int(entry["id"]) for entry in self.manifest()["entries"]]
        self.assertEqual(sorted(ids), ids)
        self.assertIn(third, ids)

    def test_the_two_manifests_carry_the_same_entries_in_the_same_order(self):
        # One pass, one _meta() per row, two files: the shape that makes it
        # impossible for them to disagree about which entries exist.
        kiosk = json.loads((self.dest / "kiosk.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [int(entry["id"]) for entry in kiosk["entries"]],
            [int(entry["id"]) for entry in self.manifest()["entries"]],
        )

    def test_every_spec_key_is_present_for_a_root_and_for_a_child(self):
        rows = self.rows()
        for name, entry_id in (("root", self.ids[0]), ("child", self.ids[1])):
            with self.subTest(entry=name):
                self.assertEqual(
                    self.SPEC_2_KEYS, set(rows[entry_id]) - self.OPTIONAL
                )

    def test_a_root_says_so_with_nulls_and_a_child_names_its_parent(self):
        one, two, _ = self.ids
        rows = self.rows()
        self.assertIsNone(rows[one]["parent_entry_id"])
        self.assertIsNone(rows[one]["critique_by"])
        self.assertEqual(1, rows[one]["generation"])
        self.assertEqual(one, rows[two]["parent_entry_id"])
        self.assertEqual("gemma4:e4b", rows[two]["critique_by"])
        self.assertEqual(2, rows[two]["generation"])

    def test_the_prose_is_not_in_the_file_at_all(self):
        # Not merely absent from the row: absent from the bytes. The brief and
        # the statement are most of the kiosk's 2.8 MB and the whole reason
        # this file exists (spec §1.2).
        text = (self.dest / "swipe.json").read_text(encoding="utf-8")
        for row in self.rows().values():
            with self.subTest(entry=row["id"]):
                self.assertNotIn("brief", row)
                self.assertNotIn("statement", row)
        for entry_id in self.ids[:2]:
            entry = self.conn.execute(
                "SELECT brief, statement FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
            with self.subTest(entry=entry_id):
                self.assertNotIn(str(entry["brief"]), text)
                self.assertNotIn(str(entry["statement"]).split("\n")[0], text)

    def test_the_kiosk_only_values_are_gone(self):
        # The words sheet fetches meta.json for these; carrying them here
        # would be the kiosk's file with a different name (spec §2).
        row = self.rows()[self.ids[0]]
        for key in ("seed", "created_utc", "prompt_tokens", "completion_tokens",
                    "wall_s", "licence", "root_entry_id", "source", "qr"):
            with self.subTest(key=key):
                self.assertNotIn(key, row)

    def test_the_provenance_is_the_same_values_meta_json_carries(self):
        one = self.ids[0]
        meta = json.loads(
            (self.dest / "e" / str(one) / "meta.json").read_text(encoding="utf-8")
        )
        row = self.rows()[one]
        for key in gallery.SWIPE_META_KEYS:
            with self.subTest(key=key):
                self.assertEqual(meta[key], row[key])

    def test_where_to_run_it_and_where_to_read_it(self):
        one = self.ids[0]
        row = self.rows()[one]
        self.assertEqual(f"e/{one}/sketch/", row["sketch"])
        self.assertEqual(f"e/{one}/meta.json", row["meta"])
        self.assertEqual(f"e/{one}/", row["href"])
        self.assertEqual(
            f"https://profcarroll.github.io/sketchgen-gallery/e/{one}/", row["url"]
        )
        # The path from the manifest is never assembled in the script, so it
        # had better name a file that is really there.
        self.assertTrue((self.dest / row["meta"]).is_file())
        self.assertTrue((self.dest / row["href"] / "index.html").is_file())

    def test_responds_names_what_the_gate_asserted(self):
        # Entry one's assertions are motion(idle) and responds(click): the
        # caption prints *responds to touch · hold to try* off this, and the
        # shield means nobody finds out any other way (spec §4.4).
        self.assertEqual(["click"], self.rows()[self.ids[0]]["responds"])

    def test_a_sketch_with_nothing_to_give_has_no_responds_key(self):
        # Absent, not an empty list: nobody should be told to hold a sketch
        # that cannot feel it.
        self.set_assertions(self.ids[1], ["motion(idle)"])
        self.assertNotIn("responds", self.rows()[self.ids[1]])

    def test_responds_keeps_the_order_the_assertions_list(self):
        self.set_assertions(
            self.ids[1], ["responds(click)", "motion(idle)", "responds(drag)"]
        )
        self.assertEqual(["click", "drag"], self.rows()[self.ids[1]]["responds"])

    def test_mic_is_true_only_for_a_listening_sketch(self):
        one, two, _ = self.ids
        self.assertNotIn("mic", self.rows()[one])
        self.assertNotIn("mic", self.rows()[two])
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id = ?", (two,)
        ).fetchone()
        (Path(row["source_dir"]) / "sketch.js").write_text(
            "let mic;\nfunction setup() { mic = new p5.AudioIn(); mic.start(); }\n",
            encoding="utf-8",
        )
        self.render()
        # Framed like any other, and the caption says to open the entry page:
        # skipping it would make the feed lie about the size of the gallery.
        self.assertIs(True, self.rows()[two]["mic"])
        self.assertNotIn("mic", self.rows()[one])

    def test_an_unjudged_population_is_absent_not_zero(self):
        one, two, _ = self.ids
        self.assertEqual({}, self.rows()[one]["judgment"])
        self.judge("look", "human", "profcarroll", one, two, "A")
        self.render()
        judgment = self.rows()[one]["judgment"]
        self.assertNotIn("agent", judgment)
        self.assertEqual({"look"}, set(judgment["human"]))

    def test_the_judgment_block_is_the_kiosk_s_own(self):
        one, two, _ = self.ids
        for question in ("look", "brief"):
            self.judge(question, "human", "profcarroll", one, two, "A")
        self.render()
        kiosk = {
            int(entry["id"]): entry
            for entry in json.loads(
                (self.dest / "kiosk.json").read_text(encoding="utf-8")
            )["entries"]
        }
        self.assertEqual(kiosk[one]["judgment"], self.rows()[one]["judgment"])

    def test_a_window_sized_sketch_has_no_canvas_key(self):
        one = self.ids[0]
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id = ?", (one,)
        ).fetchone()
        (Path(row["source_dir"]) / "sketch.js").write_text(
            "function setup() { createCanvas(windowWidth, windowHeight); }\n"
            "function draw() { background(0); }\n",
            encoding="utf-8",
        )
        self.render()
        self.assertNotIn("canvas", self.rows()[one])
        (Path(row["source_dir"]) / "sketch.js").write_text(
            "function setup() { createCanvas(800, 600); }\n"
            "function draw() { background(0); }\n",
            encoding="utf-8",
        )
        self.render()
        self.assertEqual([800, 600], self.rows()[one]["canvas"])

    def test_a_second_render_gives_the_same_bytes(self):
        first = (self.dest / "swipe.json").read_bytes()
        self.render()
        self.assertEqual(first, (self.dest / "swipe.json").read_bytes())

    def test_the_shared_meta_call_left_the_kiosk_rows_alone(self):
        """The refactor must not have moved a byte of ``kiosk.json``.

        ``_kiosk_entry`` used to make its own ``_meta()`` call and now takes a
        base built once for both manifests; called without one it still makes
        that call, which is the pre-refactor path. The two must agree, or the
        slim manifest was bought with a change to the kiosk's.
        """
        parent, children = gallery._forest(self.conn)
        scores = gallery._all_scores(self.conn)
        rows = sorted(
            gallery._entries(self.conn, "published"), key=lambda row: int(row["id"])
        )
        alone = {
            "entries": [
                gallery._kiosk_entry(
                    self.conn, row, self.config, parent, children, scores
                )
                for row in rows
            ]
        }
        self.assertEqual(
            json.dumps(alone, indent=2, sort_keys=True) + "\n",
            (self.dest / "kiosk.json").read_text(encoding="utf-8"),
        )


class SwipePageTests(GalleryTestCase):
    """``swipe.html`` is a shell, and the nav and the offers that reach it.

    The page carries no entry data by design, as the kiosk's does not:
    ``swipe.js`` fetches ``swipe.json``. What is pinned here is the DOM
    contract of spec §3 — the ids the CSS, the script and the JavaScript
    harness all build from independently, in three parallel packets — and the
    two additions to the head a phone needs.
    """

    #: Verbatim from docs/plans/swipe-mockup/swipe.html. The copy is the
    #: visual spec's, and the start card is the only copy in the page a person
    #: reads before anything runs.
    START_CARD = (
        "sketchgen · swipe",
        "Sketches from the gallery, one at a time, full screen.",
        "swipe up",
        "the next sketch",
        "swipe right",
        "like it",
        "swipe left",
        "judge it against another",
        "tap",
        "the words, on and off",
        "hold",
        "touch the sketch",
        "Start",
        "This first tap is the one gesture the browser needs before a sketch "
        "can make sound.",
    )

    #: Spec §3's table, ids only. Three packets build against this list and
    #: nothing else, so a rename here is a rename in three repositories' worth
    #: of work.
    CONTRACT = (
        "welcome", "go", "stage", "shield", "cue-like", "cue-judge", "status",
        "heart", "caption", "toast", "touching", "dim", "sheet-info",
        "sheet-judge", "sheet-settings", "sheet-signin",
    )

    def setUp(self):
        super().setUp()
        self.render()
        self.swipe = (self.dest / "swipe.html").read_text(encoding="utf-8")

    def ids_in(self, text):
        return {attrs["id"] for _, attrs in elements(text) if attrs.get("id")}

    def test_the_start_card_says_what_the_mockup_says(self):
        for line in self.START_CARD:
            with self.subTest(line=line):
                self.assertIn(line, self.swipe)

    def test_every_id_the_contract_names_is_in_the_page(self):
        present = self.ids_in(self.swipe)
        for name in self.CONTRACT:
            with self.subTest(id=name):
                self.assertIn(name, present)

    def test_each_sheet_carries_a_close_control(self):
        for name in ("sheet-info", "sheet-judge", "sheet-settings", "sheet-signin"):
            with self.subTest(sheet=name):
                sheet = self.swipe.split(f'id="{name}"')[1].split("</section>")[0]
                self.assertIn("data-close", sheet)

    def test_the_page_carries_no_entry_data(self):
        self.assertNotIn("data-entry", self.swipe)
        self.assertNotIn("sketchgen-entries", self.swipe)
        for row in self.conn.execute("SELECT prompt FROM entries"):
            with self.subTest(prompt=row["prompt"][:32]):
                self.assertNotIn(str(row["prompt"]), self.swipe)

    def test_exactly_one_swipe_script_tag(self):
        scripts = [
            attrs.get("src", "")
            for tag, attrs in elements(self.swipe)
            if tag == "script" and attrs.get("src")
        ]
        # swipe.js alone, as the kiosk has kiosk.js alone: nothing is fetched
        # but swipe.json, config.json, the write path's own endpoints,
        # meta.json, pairs.json and the frames (spec §4.1), and swipe.js
        # carries its own copy of base() and of the session helpers.
        self.assertEqual(["./assets/swipe.js"], scripts)
        self.assertNotIn("p5.min.js", self.swipe)
        self.assertNotIn("swipe-data.js", self.swipe)

    def test_the_body_is_the_swipe_page_from_load(self):
        # The CSS hangs the whole layout off body.swipe; adding the class in
        # script would show the gallery's own page first and then repaint.
        self.assertIn('<body class="swipe">', self.swipe)

    def test_the_head_is_shaped_for_a_phone(self):
        # Two things no other shell has: the ground colour a phone paints
        # behind the notch and the home bar, and the viewport that lets the
        # page run under them at all (spec §3).
        self.assertIn('<meta name="theme-color" content="#050608">', self.swipe)
        self.assertIn("viewport-fit=cover", self.swipe)

    def test_the_settings_sheet_says_how_a_view_is_counted(self):
        # The only place a visitor is told, so it is pinned here: swipe.js can
        # stop counting without anybody noticing, but it cannot stop saying so.
        self.assertIn(
            "Likes and judgments are counted by GitHub login. A sketch counts "
            "as a view once it has been on screen for ten seconds.",
            self.swipe,
        )

    def test_the_page_parses(self):
        self.assertEqual([], balance_errors(self.dest / "swipe.html"))

    def test_every_page_links_to_the_swipe_page_after_the_kiosk(self):
        one = self.ids[0]
        pages = [
            "index.html",
            "rejections.html",
            "compare.html",
            "kiosk.html",
            "swipe.html",
            f"e/{one}/index.html",
            f"lines/{one}.html",
        ]
        for name in pages:
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                nav = page.split('<p class="nav">')[1].split("</p>")[0]
                self.assertLess(nav.index("kiosk.html"), nav.index("swipe.html"))
                self.assertIn(">swipe</a>", nav)

    def test_the_swipe_page_marks_itself_current_and_no_other_page_does(self):
        one = self.ids[0]
        nav = self.swipe.split('<p class="nav">')[1].split("</p>")[0]
        self.assertIn('swipe.html" aria-current="page"', nav)
        self.assertNotIn('kiosk.html" aria-current="page"', nav)
        for name in ("index.html", "compare.html", f"e/{one}/index.html"):
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                other = page.split('<p class="nav">')[1].split("</p>")[0]
                self.assertNotIn("swipe.html\" aria-current", other)

    def test_a_line_page_reaches_the_swipe_page_from_one_level_down(self):
        line = (self.dest / "lines" / f"{self.ids[0]}.html").read_text(encoding="utf-8")
        self.assertIn('href="../swipe.html"', line)

    def test_the_grid_offers_the_page_and_the_css_shows_it_at_phone_width(self):
        # The grid offers it and never imposes it: no redirect, one line under
        # the sorts, and the stylesheet decides who sees it (spec §7 packet A).
        for name in ("index.html", "rejections.html"):
            with self.subTest(page=name):
                page = (self.dest / name).read_text(encoding="utf-8")
                self.assertIn(
                    '<p class="swipe-offer"><a href="./swipe.html">'
                    "On a phone? Swipe through the gallery →</a></p>",
                    page,
                )
        css = (self.dest / "assets" / "gallery.css").read_text(encoding="utf-8")
        self.assertIn(".swipe-offer { display: none; }", css)
        under = css.split("@media (max-width: 40rem) {")[-1]
        self.assertIn(".swipe-offer { display: block;", under)

    def test_the_frame_rule_is_scoped_to_the_swipe_page_and_reads_the_fit(self):
        # Without this rule the frame falls through to the entry page's
        # `iframe.sketch` — a full-width 26rem grey box — and fitFrame()'s four
        # custom properties are set on the stage and read by nothing.
        css = (self.dest / "assets" / "gallery.css").read_text(encoding="utf-8")
        start = css.index("body.swipe .stage iframe.sketch {")
        rule = css[start:css.index("}", start)]
        self.assertIn("transform: scale(var(--frame-sx, 1), var(--frame-sy, 1))", rule)
        self.assertIn("width: var(--frame-w, 100%)", rule)
        self.assertIn("height: var(--frame-h, 100%)", rule)
        # And the stage is one track its own size, or the frame centres in an
        # implicit track as wide as the unscaled canvas and sits off right.
        start = css.index("body.swipe .stage {")
        stage = css[start:css.index("}", start)]
        self.assertIn("grid-template-columns: minmax(0, 1fr)", stage)

    def test_the_bar_hides_once_the_page_is_playing(self):
        css = (self.dest / "assets" / "gallery.css").read_text(encoding="utf-8")
        self.assertIn("body.swipe.playing .bar { display: none; }", css)

    def test_the_scanned_strip_offers_to_swipe_on_from_here(self):
        # qr.md §6.2's strip, one verb longer: somebody who arrived by phone
        # is exactly who the swipe page is for.
        one = self.ids[0]
        page = (self.dest / "e" / str(one) / "index.html").read_text(encoding="utf-8")
        self.assertIn(
            '<span data-scanned-swipe> · <a href="../../swipe.html?at=%d">'
            "Swipe on from here</a></span>" % one,
            page,
        )

    def test_the_swipe_block_is_scoped_to_the_page(self):
        # Every rule but the grid's offer hangs off body.swipe, which only
        # this page carries: .stage, .caption, .status and .note are spoken
        # for elsewhere in the stylesheet and scoping is what keeps them apart.
        css = (self.dest / "assets" / "gallery.css").read_text(encoding="utf-8")
        block = css.split("/* ---- swipe ----")[1]
        for line in block.splitlines():
            line = line.strip()
            if not line or "{" not in line or line.startswith(("*", "/*", "@", "}")):
                continue
            selectors = line.split("{")[0].strip()
            if not selectors or selectors.endswith(","):
                selectors = selectors.rstrip(",")
            with self.subTest(rule=selectors[:60]):
                self.assertTrue(
                    all(
                        part.strip().startswith(("body.swipe", ".swipe-offer", "0%", "100%"))
                        for part in selectors.split(",")
                    ),
                    selectors,
                )


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

    def test_a_refused_second_render_keeps_what_the_first_wrote(self):
        # The web process rendered entry 71's index half-way and died
        # (2026-09-14); the undo then unlinked assets/ and config.json it had
        # only overwritten. A refusal must leave the previous render intact.
        self.render()
        kept = {
            name: (self.dest / name).read_bytes()
            for name in ("assets/gallery.css", "assets/gallery.js", "config.json", "index.html")
        }
        self.conn.execute(
            "UPDATE entries SET brief = ? WHERE id = ?",
            ("Write to nobody@example.invalid with notes.", self.ids[0]),
        )
        self.conn.commit()
        with self.assertRaises(gallery.Unsafe):
            gallery.render_index(self.conn, self.dest, self.config)
        for name, before in kept.items():
            with self.subTest(name=name):
                self.assertTrue((self.dest / name).exists())
                self.assertEqual(before, (self.dest / name).read_bytes())

    def test_templates_are_read_once_per_process(self):
        # So a running process never fills this version's template with the
        # last version's code, or the other way round; a restart takes both.
        self.assertIs(gallery._template("card.html"), gallery._template("card.html"))

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

    def render(self):
        # Nothing is pinned: a render reads no clock, so this asks the real
        # question — does the same database give the same bytes — of the same
        # call publish_index makes, not of a render with the clock held still.
        return gallery.render_all(self.conn, self.dest, self.config)

    def files(self):
        return {
            path.relative_to(self.dest): path.read_bytes()
            for path in sorted(self.dest.rglob("*"))
            if path.is_file()
        }

    def test_a_second_render_all_is_byte_identical(self):
        self.render()
        first = self.files()
        cross_a_second()
        self.render()
        second = self.files()
        self.assertEqual(sorted(first), sorted(second))
        for name, data in first.items():
            with self.subTest(path=str(name)):
                self.assertEqual(data, second[name])

    def test_the_ledger_is_identical_across_a_second_boundary(self):
        """The one question the old stamp could fail, asked so it cannot pass
        by luck.

        Two renders only ever differed when they fell either side of a second.
        Back-to-back they almost never do, so a clock reintroduced here would
        not fail this suite — it would make it flaky, which is the same bug
        arriving in the same disguise. Crossing the boundary on purpose is
        what turns "usually passes" into an assertion.
        """
        self.render()
        first = (self.dest / "lineage.json").read_bytes()
        cross_a_second()
        self.render()
        self.assertEqual(first, (self.dest / "lineage.json").read_bytes())


class RenderAllRereadsTests(GalleryTestCase):
    """render-all rebuilds the record and touches no row (spec §4.4).

    It is run once on the node after the forest fix, over entries published
    months ago, so the question it has to answer is whether re-rendering an
    entry rewrites anything about when or how it was published. It does not:
    every stamp on the page comes from the row, and the generator only writes
    files.
    """

    def stamps(self):
        return {
            int(row["id"]): (row["state"], row["published_utc"], row["publish_commit"])
            for row in self.conn.execute(
                "SELECT id, state, published_utc, publish_commit FROM entries"
            )
        }

    def test_re_rendering_rewrites_the_files_and_no_row(self):
        self.conn.execute(
            "UPDATE entries SET publish_commit = ? WHERE id = ?",
            ("0123456789abcdef0123456789abcdef01234567", self.ids[0]),
        )
        self.render()
        before = self.stamps()
        entry_dir = self.dest / "e" / str(self.ids[0])
        meta_before = (entry_dir / "meta.json").read_text(encoding="utf-8")
        shutil.rmtree(entry_dir)

        self.render()

        self.assertEqual(before, self.stamps())
        self.assertTrue((entry_dir / "index.html").is_file())
        self.assertEqual(meta_before, (entry_dir / "meta.json").read_text(encoding="utf-8"))
        meta = json.loads(meta_before)
        self.assertEqual("2026-09-14T04:02:11Z", meta["published_utc"])
        self.assertEqual(
            "0123456789abcdef0123456789abcdef01234567", meta["publish_commit"]
        )

    def test_the_cli_render_all_leaves_the_stamps_alone(self):
        before = self.stamps()
        result = subprocess.run(
            [sys.executable, str(CLI), "render-all",
             "--gallery-dir", str(self.dest),
             "--db", str(self.tmp / "sketchgen.db"),
             "--write-path", "https://write.example.invalid/api"],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.stamps())
        self.assertIn(str(self.dest / "lineage.json"), result.stdout)


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

    def test_render_index_help_names_the_files_it_writes(self):
        # The help is the only place an operator reads the file list, and a
        # deploy that does not know kiosk.json is written cannot know to pull
        # the gallery checkout before the next publish.
        result = self.run_cli("render-index", "--help")
        for name in ("kiosk.html", "kiosk.json", "swipe.html", "swipe.json"):
            with self.subTest(file=name):
                self.assertIn(name, result.stdout)

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


class LedgerPanelTests(GalleryTestCase):
    """The lineage panel is a ledger: one row per generation (plan §6).

    The fixture is a line of eleven generations with a fork at the parent, the
    shape the mockup was drawn against and the shape entry 82 has on the live
    site: root, three folded middle generations, grandparent, parent, this
    entry, a sibling, and descendants.
    """

    def setUp(self):
        super().setUp()
        # ids[0] is the root and ids[1] its child; nine more take the line to
        # eleven. A model asks for the middle of the line, which is what makes
        # the fold read "all by gemma4".
        self.line = [self.ids[0], self.ids[1]]
        for generation in range(3, 12):
            self.line.append(add_child(
                self.conn, self.tmp, self.line[-1], state="published",
                prompt=f"the same field, generation {generation}",
                critique=f"generation {generation}: take one more colour out of it",
                critique_by="gemma4:e4b", generation=generation,
            ))
        # The chain is then seven long — root, three that fold, grandparent,
        # parent, this entry — exactly the shape entry 82 has on the site.
        self.here = self.line[6]
        self.parent = self.line[5]
        self.sibling = add_child(
            self.conn, self.tmp, self.parent, state="published",
            prompt="the same field, but the other way",
            critique="try the other way instead", critique_by="profcarroll",
            generation=7,
        )
        self.render()
        self.page = (self.dest / "e" / str(self.here) / "index.html").read_text(
            encoding="utf-8"
        )

    def panel(self, page=None):
        text = page if page is not None else self.page
        return text.split('<h2>Lineage')[1].split("</section>")[0]

    def test_the_heading_counts_this_generation_in_its_line(self):
        self.assertIn("generation 7 of <span data-ledger-deepest>11</span>", self.page)
        self.assertIn(f"in the line from <a href=\"../../lines/{self.ids[0]}.html\">"
                      f"entry {self.ids[0]}</a>", self.page)

    def test_the_root_row_is_the_prompt_and_says_root_not_a_generation(self):
        panel = self.panel()
        root = panel.split("</li>")[0]
        self.assertIn('class="ledger-prompt"', root)
        self.assertIn("a field of sixty circles", root)
        # THE NUMBERING: the root and its first child both record generation 1,
        # so the root prints the word and no number (gallery._generation_label).
        self.assertIn(f'<a href="../{self.ids[0]}/">entry {self.ids[0]}</a> · root ·', root)
        self.assertNotIn("generation 1", root)
        self.assertIn('<span class="chip person">profcarroll</span>', root)

    def test_the_middle_of_the_line_folds_into_one_disclosure(self):
        panel = self.panel()
        folded = self.line[1:4]
        summary = (
            f"3 generations folded · {folded[0]}, {folded[1]}, {folded[2]}"
        )
        self.assertIn(summary, html.unescape(panel))
        self.assertIn("all by gemma4", panel)
        # A details element, so it opens with no script at all.
        self.assertIn('<details class="fold">', panel)
        for one in folded:
            self.assertIn(f'href="../{one}/"', panel)

    def test_the_root_grandparent_parent_and_this_entry_are_never_folded(self):
        panel = self.panel()
        rows = panel.split('<li class="ledger-row')
        # the four unfolded rows plus the three inside the disclosure
        outside = self.panel().split("</details>")[1]
        for one in (self.line[4], self.parent, self.here):
            self.assertIn(f"entry {one}", outside)
        self.assertGreaterEqual(len(rows), 7)

    def test_this_entry_is_highlighted_and_is_not_a_link(self):
        panel = self.panel()
        self.assertIn('<li class="ledger-row here" aria-current="true">', panel)
        here = panel.split('<li class="ledger-row here"')[1].split("</li>")[0]
        self.assertIn(f"entry {self.here}", here)
        # Every other row's name is a link; this one is where the reader is.
        self.assertNotIn(f'<a href="../{self.here}/">entry {self.here}</a>', here)
        self.assertIn("generation 7", here)

    def test_every_public_row_carries_a_playable_first_frame(self):
        panel = self.panel()
        for one in (self.ids[0], self.parent, self.here):
            self.assertIn(f'data-run-href="../{one}/sketch/"', panel)
            self.assertIn(f'<img src="../{one}/strip.png"', panel)

    def test_the_critique_is_the_row_and_is_never_clamped(self):
        panel = self.panel()
        parent = panel.split(f'data-run-href="../{self.parent}/sketch/"')[1].split("</li>")[0]
        self.assertIn('<span class="revise">Revise:</span>', parent)
        self.assertIn("generation 6: take one more colour out of it", parent)
        self.assertIn('<span class="chip model">gemma4</span>', parent)
        self.assertNotIn("clamp", parent)

    def test_the_fork_and_descendant_containers_are_the_scripts_to_fill(self):
        panel = self.panel()
        self.assertIn(f'data-ledger="{self.here}"', panel)
        self.assertIn(f'data-ledger-parent="{self.parent}"', panel)
        self.assertIn("data-ledger-forks", panel)
        self.assertIn("data-ledger-tiles", panel)
        # The sibling itself is painted by gallery.js from lineage.json, which
        # is why it must be in that file for this entry's parent.
        index = json.loads((self.dest / "lineage.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [self.here, self.sibling],
            sorted(index["entries"][str(self.parent)]["children"]),
        )

    def test_with_no_script_the_children_are_still_named(self):
        panel = self.panel()
        child = self.line[7]
        self.assertIn(f'<a href="../{child}/">entry {child}</a>', panel)
        self.assertIn("Children:", panel)
        self.assertIn("After this entry: 1 child", panel)

    def test_a_root_with_no_line_says_so_and_links_nowhere(self):
        job = db.enqueue(self.conn, "a root nobody has revised", "profcarroll")
        alone = db.create_entry(
            self.conn, job, state="published", prompt="a root nobody has revised",
            submitted_by="profcarroll", published_utc="2026-09-14T06:00:00Z",
        )
        db.transition(self.conn, job, "executing")
        db.transition(self.conn, job, "gating")
        db.transition(self.conn, job, "held")
        db.transition(self.conn, job, "published")
        out = gallery.render_entry(self.conn, alone, self.dest, self.config)
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("a root prompt, no children yet", page)
        self.assertIn("No children yet.", page)
        self.assertNotIn(f"lines/{alone}.html", page)

    def test_a_generation_that_is_not_published_is_a_blank_tile_that_counts(self):
        """Spec §1.6: a line whose middle is missing still counts right."""
        missing = self.line[4]          # the grandparent, in the unfolded rows
        self.conn.execute(
            "UPDATE entries SET state='held', published_utc=NULL WHERE id=?",
            (missing,),
        )
        self.conn.commit()
        out = gallery.render_entry(self.conn, self.here, self.dest, self.config)
        panel = self.panel((out / "index.html").read_text(encoding="utf-8"))
        row = panel.split(f"entry {missing}")[0].rsplit('<li class="ledger-row', 1)[1]
        self.assertIn('class="ledger-tile narrow blank"', row)
        self.assertNotIn(f'<img src="../{missing}/strip.png"', panel)
        self.assertNotIn(f'<a href="../{missing}/">', panel)
        self.assertIn('<span class="chip unpublished">not published</span>', panel)
        # and the generations after it keep their numbers
        self.assertIn("generation 7", panel)
        self.assertIn("generation 6", panel)

    def test_an_ancestor_that_forked_says_so_and_sends_you_to_the_line(self):
        # A second child of the root: the ledger does not draw the other
        # branch, it says the branch is there and where to see it.
        add_child(
            self.conn, self.tmp, self.ids[0], state="published",
            prompt="the same field, a different road", generation=2,
        )
        out = gallery.render_entry(self.conn, self.here, self.dest, self.config)
        panel = self.panel((out / "index.html").read_text(encoding="utf-8"))
        root = panel.split("</li>")[0]
        self.assertIn(f'<a href="../../lines/{self.ids[0]}.html">forked</a>', root)

    def test_the_panel_says_what_clicking_a_frame_does(self):
        self.assertIn("Click a frame to run that sketch in place", self.page)
        self.assertIn("one at a time", self.page)


class HeavyStageTests(GalleryTestCase):
    """An entry the gate had to grind through does not autoplay (plan §6, the
    addition): 29 published entries cost more than 100 ms a virtual frame."""

    def reported(self, entry_id, **timings):
        """Rewrite that entry's gate report with the timings it really had."""
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        report = Path(row["source_dir"]) / ".gate" / "report.json"
        data = json.loads(report.read_text(encoding="utf-8"))
        data["timings"].update(timings)
        report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def page_of(self, entry_id):
        out = gallery.render_entry(self.conn, entry_id, self.dest, self.config)
        return (out / "index.html").read_text(encoding="utf-8")

    def test_the_timings_reach_meta_json_inside_gate(self):
        self.reported(self.ids[0], total_s=208.8, ms_per_frame=1093.0)
        out = gallery.render_entry(self.conn, self.ids[0], self.dest, self.config)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(208.8, meta["gate"][0]["total_s"])
        self.assertEqual(1093.0, meta["gate"][0]["ms_per_frame"])
        # inside gate, so the key list the spec fixes does not move
        self.assertEqual(set(gallery.META_KEYS), set(meta))

    def test_a_report_with_no_frame_rate_records_null_rather_than_a_guess(self):
        out = gallery.render_entry(self.conn, self.ids[0], self.dest, self.config)
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        self.assertIsNone(meta["gate"][0]["ms_per_frame"])
        self.assertEqual(1.42, meta["gate"][0]["total_s"])

    def test_an_ordinary_entry_still_autoplays(self):
        page = self.page_of(self.ids[0])
        self.assertIn('<iframe class="sketch" src="sketch/"', page)
        self.assertNotIn("data-stage-run", page)
        self.assertNotIn("chip heavy", page)

    def test_over_the_frame_budget_the_stage_waits_for_a_click(self):
        self.reported(self.ids[0], total_s=208.8, ms_per_frame=1093.0)
        page = self.page_of(self.ids[0])
        self.assertNotIn('<iframe class="sketch" src="sketch/"', page)
        self.assertIn("data-stage-run", page)
        self.assertIn('data-run-href="sketch/"', page)
        self.assertIn('<img src="strip.png"', page)
        self.assertIn('<span class="chip heavy">heavy · 1093 ms per frame</span>',
                      html.unescape(page))

    def test_a_slow_run_with_no_frame_rate_is_heavy_too(self):
        self.reported(self.ids[0], total_s=42.0)
        page = self.page_of(self.ids[0])
        self.assertIn("data-stage-run", page)
        self.assertIn("heavy · 42 s to run", html.unescape(page))

    def test_the_budget_is_a_ceiling_not_a_target(self):
        # Exactly at the budget is not over it.
        self.reported(self.ids[0], total_s=12.0, ms_per_frame=100.0)
        page = self.page_of(self.ids[0])
        self.assertIn('<iframe class="sketch" src="sketch/"', page)
class RepointKeptTests(GalleryTestCase):
    """The one-time repair: a kept failure shows its best attempt, not its last."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "repoint-kept", "--db", str(self.db_path), *args],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )

    def kept(self, reports):
        """A kept failure whose entry points at its LAST attempt, as they all do."""
        job = db.enqueue(self.conn, "jigsaw puzzle game", "profcarroll")
        self.conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
        last = None
        for n, report in enumerate(reports, 1):
            out = self.tmp / f"j{job}-attempt-{n}"
            (out / ".gate").mkdir(parents=True, exist_ok=True)
            report = dict(report, artefacts={"strip": str(out / ".gate/strip.png"),
                                             "png": str(out / ".gate/gate.png")})
            (out / ".gate" / "report.json").write_text(json.dumps(report), encoding="utf-8")
            db.add_attempt(
                self.conn, job, n, started_utc=db.utc_now(), finished_utc=db.utc_now(),
                model="qwen3-coder:30b", rules_file="treatment",
                prompt_version="executor-v2", source_dir=str(out), gate_exit=1,
                gate_report_path=str(out / ".gate" / "report.json"),
                evidence="x", statement=f"statement {n}",
            )
            last = out
        entry = db.create_entry(
            self.conn, job, state="failed-kept", prompt="jigsaw puzzle game",
            source_dir=str(last), statement=f"statement {len(reports)}",
        )
        self.conn.commit()
        return entry

    CLEAN = {"checks": {"console_clean": True, "frame_advancing": True},
             "assertions": {"responds(drag)": {"pass": False, "detail": "0 changed"}}}
    BROKEN = {"checks": {"console_clean": False, "frame_advancing": False},
              "assertions": {"responds(drag)": {"pass": False, "detail": "none"}}}

    def row(self, entry_id):
        return self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()

    def test_it_repoints_at_the_best_attempt_and_records_the_divergence(self):
        entry = self.kept([self.CLEAN, self.BROKEN])
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        row = self.row(entry)
        self.assertTrue(row["source_dir"].endswith("attempt-1"), row["source_dir"])
        self.assertEqual("statement 1", row["statement"])
        self.assertEqual(["responds(drag)"], json.loads(row["offplan_json"]))
        # the state is left alone without --reclassify
        self.assertEqual("failed-kept", row["state"])

    def test_dry_run_writes_nothing(self):
        entry = self.kept([self.CLEAN, self.BROKEN])
        before = dict(self.row(entry))
        result = self.run_cli("--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertRegex(result.stdout, r"attempt-2 -> \S*attempt-1")
        self.assertEqual(before["source_dir"], self.row(entry)["source_dir"])
        self.assertIsNone(self.row(entry)["offplan_json"])

    def test_it_never_changes_an_entry_s_state(self):
        """--reclassify is gone: it wrote state with a raw UPDATE, and
        failed-kept -> held is not a transition the machine allows for a
        published entry at all. reopen-offplan is the version that asks."""
        runs = self.kept([self.CLEAN, self.BROKEN])
        broken = self.kept([self.BROKEN, self.BROKEN])
        result = self.run_cli()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("failed-kept", self.row(runs)["state"])
        self.assertEqual("failed-kept", self.row(broken)["state"])
        # and a sketch that never ran is never called off-plan
        self.assertIsNone(self.row(broken)["offplan_json"])

    def test_running_it_twice_changes_nothing_the_second_time(self):
        entry = self.kept([self.CLEAN, self.BROKEN])
        self.run_cli()
        after_first = dict(self.row(entry))
        result = self.run_cli()
        self.assertIn("same attempt", result.stdout)
        self.assertEqual(after_first["source_dir"], self.row(entry)["source_dir"])


class ReopenOffPlanTests(GalleryTestCase):
    """The door back, and the 18 doors it must leave shut."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "reopen-offplan", "--db", str(self.db_path), *args],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )

    def kept(self, *, offplan, published=False):
        job = db.enqueue(self.conn, "a jigsaw puzzle game", "profcarroll")
        self.conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (job,))
        entry = db.create_entry(
            self.conn, job, state="failed-kept", prompt="a jigsaw puzzle game",
            offplan_json=json.dumps(offplan) if offplan else None,
            published_utc=db.utc_now() if published else None,
        )
        self.conn.commit()
        return entry

    def state(self, entry_id):
        return self.conn.execute(
            "SELECT state FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()["state"]

    def test_an_unpublished_off_plan_entry_is_reopened(self):
        entry = self.kept(offplan=["responds(drag)"])
        result = self.run_cli("--all")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("failed-kept -> held", result.stdout)
        self.assertIn("missed responds(drag)", result.stdout)
        self.assertEqual("held", self.state(entry))

    def test_a_published_entry_is_never_touched(self):
        """The public record stands. 18 of the 30 are in exactly this case."""
        entry = self.kept(offplan=["responds(drag)"], published=True)
        result = self.run_cli("--all")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("nothing to reopen", result.stdout)
        self.assertEqual("failed-kept", self.state(entry))

    def test_the_state_machine_refuses_it_even_if_asked_directly(self):
        """Belt and braces: the command declines to ask, and db refuses anyway."""
        entry = self.kept(offplan=["responds(drag)"], published=True)
        with self.assertRaises(db.IllegalTransition) as caught:
            db.entry_transition(self.conn, entry, "held")
        self.assertIn("already on the site", str(caught.exception))
        self.assertIn("reopened", str(caught.exception))

    def test_a_sketch_that_never_ran_is_not_reopened(self):
        entry = self.kept(offplan=None)
        result = self.run_cli("--all")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("nothing to reopen", result.stdout)
        self.assertEqual("failed-kept", self.state(entry))

    def test_dry_run_writes_nothing(self):
        entry = self.kept(offplan=["motion(idle)"])
        result = self.run_cli("--all", "--dry-run")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("would be reopened", result.stdout)
        self.assertEqual("failed-kept", self.state(entry))

    def test_it_refuses_without_all_or_ids(self):
        result = self.run_cli()
        self.assertEqual(3, result.returncode)
        self.assertIn("say which", result.stderr)

    def test_naming_an_ineligible_id_refuses_rather_than_skipping(self):
        entry = self.kept(offplan=["motion(idle)"], published=True)
        result = self.run_cli(str(entry))
        self.assertEqual(3, result.returncode)
        self.assertIn("not an unpublished off-plan kept failure", result.stderr)
        self.assertEqual("failed-kept", self.state(entry))


class PublishRejectedTests(GalleryTestCase):
    """§5.4: the one-time backfill of the rejections that were only a state flip."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(CLI), "publish-rejected", *args],
            capture_output=True, text=True, check=False, env=dict(os.environ),
        )

    def waiting(self, prompt, last_error, entry_reason=None):
        """A rejected entry nobody has published, with its job's last word."""
        job = db.enqueue(self.conn, prompt, "profcarroll")
        self.conn.execute(
            "UPDATE jobs SET state = 'rejected', last_error = ? WHERE id = ?",
            (last_error, job),
        )
        entry_id = db.create_entry(
            self.conn, job, state="rejected", prompt=prompt,
            reject_reason=entry_reason,
        )
        self.conn.commit()
        return entry_id

    def test_the_reason_is_read_from_the_job_only_when_a_person_wrote_it(self):
        for last_error, expected in (
            ("prompt drift", "prompt drift"),
            ("entry rejected: too illegible", "too illegible"),
            # the placeholder the old UI wrote for an empty reason box
            ("rejected by operator", cli_gallery.NO_REASON),
            ("", cli_gallery.NO_REASON),
            (None, cli_gallery.NO_REASON),
            # the machine's own words, which are not a person's verdict
            ("gate exit 1: checks failed", cli_gallery.NO_REASON),
            ("executor: no js block in the response", cli_gallery.NO_REASON),
            ("x" * 400, cli_gallery.NO_REASON),
        ):
            with self.subTest(last_error=last_error):
                self.assertEqual(expected, cli_gallery.backfill_reason(last_error))

    def test_dry_run_lists_every_waiting_rejection_and_its_reason(self):
        one = self.waiting("one a person refused", "prompt drift")
        two = self.waiting("one with nothing written down", "rejected by operator")
        result = self.run_cli(
            "--all", "--dry-run", "--db", str(self.tmp / "sketchgen.db")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"entry {one}: prompt drift", result.stdout)
        self.assertIn(f"entry {two}: {cli_gallery.NO_REASON}", result.stdout)
        self.assertIn("2 rejected entries would be published", result.stdout)
        # A dry run writes nothing, not even the reason it would record.
        self.assertIsNone(db.get_entry(self.conn, two)["reject_reason"])

    def test_a_rejection_already_on_the_site_is_not_in_the_backlog(self):
        done = self.waiting("one already pushed", "prompt drift")
        self.conn.execute(
            "UPDATE entries SET published_utc = ? WHERE id = ?",
            ("2026-09-14T06:00:00Z", done),
        )
        self.conn.commit()
        result = self.run_cli(
            "--all", "--dry-run", "--db", str(self.tmp / "sketchgen.db")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"entry {done}:", result.stdout)

    def test_an_entrys_own_reason_wins_over_the_jobs_last_error(self):
        entry_id = self.waiting("one with both", "prompt drift", "what a person typed")
        result = self.run_cli(
            "--all", "--dry-run", "--db", str(self.tmp / "sketchgen.db")
        )
        self.assertIn(f"entry {entry_id}: what a person typed", result.stdout)

    def test_named_ids_narrow_the_run_and_a_wrong_one_refuses(self):
        one = self.waiting("one a person refused", "prompt drift")
        self.waiting("another", "lacks cohesion")
        db_arg = str(self.tmp / "sketchgen.db")
        result = self.run_cli(str(one), "--dry-run", "--db", db_arg)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 rejected entries would be published", result.stdout)
        # An id that is not a rejection waiting to be published is a refusal,
        # not a silent no-op: the operator typed a number and meant it.
        wrong = self.run_cli(str(self.ids[0]), "--dry-run", "--db", db_arg)
        self.assertEqual(wrong.returncode, 3, wrong.stdout)
        self.assertIn("refused", wrong.stderr)

    def test_saying_nothing_at_all_is_refused(self):
        result = self.run_cli("--dry-run", "--db", str(self.tmp / "sketchgen.db"))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("refused", result.stderr)

    def test_help_exits_zero(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--dry-run", result.stdout)


class MicSketchTests(GalleryTestCase):
    """A listening sketch runs in its own tab, not the sandboxed frame.

    The published sketch iframe is sandbox="allow-scripts" — an opaque origin
    with no allow="microphone" — so getUserMedia is refused and a mic sketch
    reads a dead mic in the embed. It only works in its own top-level tab, where
    the Pages origin is a secure context, so the stage and the ledger tiles route
    it there. Making sound is unaffected: it plays in the frame after a click.
    """

    MIC_JS = (
        "function setup(){ createCanvas(windowWidth, windowHeight);\n"
        "  let mic = new p5.AudioIn(); mic.start(); }\n"
        "function draw(){ background(0); }\n"
    )

    def _make_mic(self, entry_id):
        src = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()["source_dir"]
        path = Path(src)
        (path / "sketch.js").write_text(self.MIC_JS, encoding="utf-8")
        return path

    def test_the_stage_of_a_mic_entry_opens_a_tab_not_an_embed(self):
        one = self.ids[0]
        self._make_mic(one)
        gallery.render_all(self.conn, self.dest, self.config)
        page = (self.dest / "e" / str(one) / "index.html").read_text(encoding="utf-8")
        self.assertIn("stage-mic", page)
        self.assertIn('href="sketch/" target="_blank"', page)
        self.assertIn("listens to the microphone", page)
        # never the self-starting embed for this one
        self.assertNotIn('<iframe class="sketch"', page)

    def test_lineage_json_marks_a_mic_entry_and_leaves_others_alone(self):
        one = self.ids[0]
        self._make_mic(one)
        gallery.render_all(self.conn, self.dest, self.config)
        entries = json.loads(
            (self.dest / "lineage.json").read_text(encoding="utf-8")
        )["entries"]
        self.assertTrue(entries[str(one)]["mic"])
        # a published sibling that only draws stays false
        self.assertFalse(entries[str(self.ids[1])].get("mic", False))

    def test_needs_mic_catches_listening_and_ignores_making_sound(self):
        src = self._make_mic(self.ids[0])
        self.assertTrue(gallery._needs_mic(src))
        (src / "sketch.js").write_text(
            "function setup(){ let o = new p5.Oscillator('sine'); o.start(); }\n",
            encoding="utf-8",
        )
        self.assertFalse(gallery._needs_mic(src))
        self.assertFalse(gallery._needs_mic(None))


if __name__ == "__main__":
    unittest.main()


class ProcessCostTests(GalleryTestCase):
    """Migration 015 on the page: what the agent said the work cost.

    Entry 1279 (2026-09-21) shows a 111 s round trip and nothing about the 37
    minutes before it. The row says both numbers are the agent's word, dashes
    what it did not report, and sits after Wall seconds so a reader meets the
    node's own measurements first.
    """

    PROCESS = {
        "session_s": 2520, "output_tokens": 207537, "thinking_tokens": None,
        "tool_calls": None, "screenshots": None, "effort": None, "tries": 4,
    }
    NOTE = "skill=algorithmic-art; local prototype"

    def record(self, process=None, note=None):
        # The columns the worker fills at _create_entry; written here directly
        # because this test is about the page, not about how they got there.
        self.conn.execute(
            "UPDATE entries SET process_json = ?, note = ? WHERE id = ?",
            (json.dumps(process) if process is not None else None, note, self.ids[0]),
        )
        self.render()
        where = self.dest / "e" / str(self.ids[0])
        return (where / "index.html").read_text(encoding="utf-8"), json.loads(
            (where / "meta.json").read_text(encoding="utf-8")
        )

    def test_the_process_row_follows_wall_seconds_and_says_who_reported_it(self):
        page, meta = self.record(self.PROCESS, self.NOTE)
        self.assertIn('<th scope="row">Process</th>', page)
        self.assertIn(
            "42 min · 207,537 tokens generated · 4 tries · as reported by the agent",
            html.unescape(page),
        )
        self.assertLess(page.index('>Wall seconds<'), page.index('>Process<'))
        self.assertLess(page.index('>Process<'), page.index('>Node shape<'))
        self.assertEqual(meta["process"], self.PROCESS)
        self.assertEqual(meta["note"], self.NOTE)

    def test_a_field_the_agent_left_out_is_a_dash_and_not_a_zero(self):
        page, meta = self.record({"session_s": None, "output_tokens": None, "tries": 1})
        self.assertIn("— min · — tokens generated · 1 try · as reported by the agent",
                      html.unescape(page))
        self.assertIsNone(meta["process"]["session_s"])
        self.assertNotIn("0 tokens", html.unescape(page))

    def test_the_effort_is_printed_when_the_harness_named_one(self):
        page, _meta = self.record({"session_s": 600, "effort": "max"})
        self.assertIn("10 min · — tokens generated · — tries · effort max · as "
                      "reported by the agent", html.unescape(page))

    def test_an_entry_nobody_reported_on_has_neither_row(self):
        page, meta = self.record(None, None)
        self.assertNotIn('<th scope="row">Process</th>', page)
        self.assertNotIn('<th scope="row">Note</th>', page)
        self.assertIsNone(meta["process"])
        self.assertIsNone(meta["note"])
        # and the rest of the table is where it was
        self.assertIn('<th scope="row">Wall seconds</th>', page)

    def test_the_note_stands_on_its_own_and_is_escaped(self):
        page, meta = self.record(None, "skill=<b>algorithmic-art</b>")
        self.assertIn('<th scope="row">Note</th>', page)
        self.assertNotIn("<b>algorithmic-art</b>", page)
        self.assertEqual(meta["note"], "skill=<b>algorithmic-art</b>")
        self.assertLess(page.index('>Wall seconds<'), page.index('>Note<'))

    def test_unreadable_json_is_no_row_rather_than_half_a_row(self):
        self.conn.execute("UPDATE entries SET process_json = ? WHERE id = ?",
                          ("{not json", self.ids[0]))
        self.render()
        where = self.dest / "e" / str(self.ids[0])
        page = (where / "index.html").read_text(encoding="utf-8")
        meta = json.loads((where / "meta.json").read_text(encoding="utf-8"))
        self.assertNotIn('<th scope="row">Process</th>', page)
        self.assertIsNone(meta["process"])


class LoadsImageTests(GalleryTestCase):
    """Where the picture a sketch loaded came from, shown to people.

    Entry 1103, the jigsaw prompted on 2026-09-20, loads
    `https://picsum.photos/800/600` in `preload()`: five attempts, three of
    them eleven seconds of gate time waiting for a photograph, and not one
    word about it anywhere on its published page. Entry 429 did the same eight
    times and drew nothing, which is why the gate records what a sketch asked
    for at all. Since `loads(image)` (media-assertion.md §3.1) the gate also
    records what arrived, and this is where a reader of the gallery — and the
    person deciding whether to publish — finds out.

    The report is rewritten on disk here rather than through the worker
    because `build_db` is shared with every other test in this file and this
    is a fixture building a gate run, not the worker writing one.
    """

    PICSUM = {"url": "https://picsum.photos/seed/sketchgen/800/600?token=abc123",
              "host": "picsum.photos", "type": "image/jpeg", "bytes": 61440,
              "ms": 340}
    WIKIMEDIA = {"url": "https://upload.wikimedia.org/w/x.png",
                 "host": "upload.wikimedia.org", "type": "image/png",
                 "bytes": 1_250_000, "ms": 712}

    def loaded(self, entry_id, *items, preload_s=None):
        """Rewrite that entry's kept report with the arrivals it really had."""
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id=?", (entry_id,)
        ).fetchone()
        report = Path(row["source_dir"]) / ".gate" / "report.json"
        data = json.loads(report.read_text(encoding="utf-8"))
        data["resources_loaded"] = list(items)
        if preload_s is not None:
            data["timings"]["preload_s"] = preload_s
        report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def out(self, entry_id):
        return gallery.render_entry(self.conn, entry_id, self.dest, self.config)

    def page_and_meta(self, entry_id):
        out = self.out(entry_id)
        return (
            (out / "index.html").read_text(encoding="utf-8"),
            json.loads((out / "meta.json").read_text(encoding="utf-8")),
        )

    # -- meta.json ---------------------------------------------------------

    def test_the_gate_log_carries_what_arrived_and_how_long_it_waited(self):
        self.loaded(self.ids[0], self.PICSUM, preload_s=11.4)
        _page, meta = self.page_and_meta(self.ids[0])
        self.assertEqual(
            [{"host": "picsum.photos", "type": "image/jpeg", "bytes": 61440}],
            meta["gate"][0]["resources_loaded"],
        )
        self.assertEqual(11.4, meta["gate"][0]["preload_s"])
        # and the top level is the kept attempt's own list, same shape
        self.assertEqual(meta["gate"][0]["resources_loaded"], meta["loads"])
        # inside gate for the per-attempt facts; META_KEYS moved once
        self.assertEqual(set(gallery.META_KEYS), set(meta))

    def test_a_report_written_before_the_word_existed_says_nothing(self):
        """Which is every report this gallery has published."""
        _page, meta = self.page_and_meta(self.ids[0])
        self.assertEqual([], meta["loads"])
        self.assertEqual([], meta["gate"][0]["resources_loaded"])
        self.assertIsNone(meta["gate"][0]["preload_s"])

    def test_the_kept_attempt_is_the_one_the_top_level_reads(self):
        """Entry 3 kept its last of two attempts; the first loaded nothing."""
        three = self.ids[2]
        row = self.conn.execute(
            "SELECT job_id, source_dir FROM entries WHERE id=?", (three,)
        ).fetchone()
        first = Path(str(row["source_dir"])).parent / "attempt-1"
        data = json.loads((first / ".gate" / "report.json").read_text(encoding="utf-8"))
        data["resources_loaded"] = []
        (first / ".gate" / "report.json").write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.loaded(three, self.WIKIMEDIA)
        _page, meta = self.page_and_meta(three)
        self.assertEqual([], meta["gate"][0]["resources_loaded"])
        self.assertEqual(
            [{"host": "upload.wikimedia.org", "type": "image/png",
              "bytes": 1_250_000}],
            meta["gate"][1]["resources_loaded"],
        )
        self.assertEqual(meta["gate"][1]["resources_loaded"], meta["loads"])

    def test_a_malformed_item_is_dropped_and_a_missing_key_is_null(self):
        self.loaded(self.ids[0], {"host": "picsum.photos"}, "not an object")
        _page, meta = self.page_and_meta(self.ids[0])
        self.assertEqual(
            [{"host": "picsum.photos", "type": None, "bytes": None}], meta["loads"]
        )

    def test_a_resources_loaded_that_is_not_a_list_is_read_as_empty(self):
        row = self.conn.execute(
            "SELECT source_dir FROM entries WHERE id=?", (self.ids[0],)
        ).fetchone()
        report = Path(row["source_dir"]) / ".gate" / "report.json"
        data = json.loads(report.read_text(encoding="utf-8"))
        data["resources_loaded"] = "picsum.photos"
        report.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _page, meta = self.page_and_meta(self.ids[0])
        self.assertEqual([], meta["loads"])

    # -- the URL, which is never published ---------------------------------

    def test_the_url_appears_nowhere_in_the_rendered_entry(self):
        """DECIDE[image-hosts], and the reason `guard()` scans what it writes.

        A URL can carry a query string a person did not choose to publish, so
        the gallery publishes the host and the gate keeps the URL. Asserted
        over every byte of the written directory — the page, meta.json, the
        sketch, the strip — and not just the page, because meta.json is the
        machine-readable copy of the same record.
        """
        self.loaded(self.ids[0], self.PICSUM, self.WIKIMEDIA)
        out = self.out(self.ids[0])
        written = sorted(p for p in out.rglob("*") if p.is_file())
        self.assertTrue(written)
        for path in written:
            with self.subTest(path=path.name):
                blob = path.read_bytes()
                self.assertNotIn(b"token=abc123", blob)
                self.assertNotIn(self.PICSUM["url"].encode(), blob)
                self.assertNotIn(self.WIKIMEDIA["url"].encode(), blob)
        # the host is published, and so is the fact that there was one
        self.assertIn(b"picsum.photos", (out / "meta.json").read_bytes())
        self.assertNotIn("url", json.loads(
            (out / "meta.json").read_text(encoding="utf-8"))["loads"][0])

    # -- the entry page ----------------------------------------------------

    def test_one_host_gets_a_row_and_a_line_under_the_frame(self):
        self.loaded(self.ids[0], self.PICSUM)
        page, _meta = self.page_and_meta(self.ids[0])
        self.assertIn('<th scope="row">Loads</th>', page)
        self.assertIn("a picture from picsum.photos (image/jpeg, 61.4 kB)", page)
        self.assertIn(
            '<p class="stage-loads">This sketch fetches an image from '
            "picsum.photos when it runs.</p>",
            page,
        )
        # the row lands directly under Gate, which says what each run found
        self.assertLess(page.index(">Gate<"), page.index(">Loads<"))
        # and the line is under the frame, above the seed
        self.assertLess(page.index("stage-loads"), page.index("stage-meta"))

    def test_two_hosts_are_joined_with_and(self):
        self.loaded(self.ids[0], self.PICSUM, self.WIKIMEDIA)
        page, _meta = self.page_and_meta(self.ids[0])
        self.assertIn(
            "a picture from picsum.photos (image/jpeg, 61.4 kB) and a picture "
            "from upload.wikimedia.org (image/png, 1.2 MB)",
            page,
        )
        self.assertIn(
            '<p class="stage-loads">This sketch fetches images from '
            "picsum.photos and upload.wikimedia.org when it runs.</p>",
            page,
        )

    def test_two_pictures_from_one_host_name_it_once_under_the_frame(self):
        """The row is about what arrived, the line about who gets called."""
        self.loaded(self.ids[0], self.PICSUM, {**self.PICSUM, "bytes": 2048})
        page, _meta = self.page_and_meta(self.ids[0])
        self.assertIn(
            '<p class="stage-loads">This sketch fetches an image from '
            "picsum.photos when it runs.</p>",
            page,
        )
        self.assertIn("2.0 kB", page)
        self.assertEqual(2, page.count("a picture from picsum.photos"))

    def test_the_size_is_kb_or_mb_with_one_decimal_and_never_a_guess(self):
        self.assertEqual("61.4 kB", gallery._size_word(61440))
        self.assertEqual("0.5 kB", gallery._size_word(512))
        self.assertEqual("999.9 kB", gallery._size_word(999_900))
        self.assertEqual("1.0 MB", gallery._size_word(1_000_000))
        self.assertEqual("1.2 MB", gallery._size_word(1_250_000))
        # no number is no words: a size the gate did not record is left out of
        # the parenthesis rather than printed as a zero
        for missing in (None, "61440", True, -1):
            with self.subTest(value=missing):
                self.assertEqual("", gallery._size_word(missing))
        self.assertEqual(
            "a picture from picsum.photos (image/jpeg)",
            gallery._loads_line({"loads": [{"host": "picsum.photos",
                                            "type": "image/jpeg",
                                            "bytes": None}]}),
        )

    def test_the_line_itself_reads_as_the_packet_wrote_it(self):
        self.assertEqual(
            "a picture from picsum.photos (image/jpeg, 61.4 kB)",
            gallery._loads_line({"loads": [self.PICSUM]}),
        )
        self.assertIsNone(gallery._loads_line({"loads": []}))
        self.assertIsNone(gallery._loads_line({}))
        # three, for the joiner's own sake
        self.assertEqual(
            "a, b and c",
            gallery._join_and(["a", "b", "c"]),
        )
        # something arrived from a host the gate did not name: still said,
        # because dropping it would understate what the browser is about to do
        self.assertEqual(
            "a picture from a host the gate did not record",
            gallery._loads_line({"loads": [{"host": None, "type": None,
                                            "bytes": None}]}),
        )

    def test_a_host_is_escaped_where_it_is_printed(self):
        self.loaded(self.ids[0], {**self.PICSUM, "host": "<b>evil</b>.example"})
        page, _meta = self.page_and_meta(self.ids[0])
        self.assertNotIn("<b>evil</b>", page)
        self.assertIn("&lt;b&gt;evil&lt;/b&gt;.example", page)

    def test_an_entry_that_loads_nothing_renders_as_it_did_before_the_packet(self):
        """The promise the packet makes to 910 published pages.

        A row of dashes and an empty line would have been a change to every
        one of them; absent means the render of an entry whose report has no
        `resources_loaded` is the render it was, byte for byte. Checked
        against the same render with the row and the line forced off, which is
        the state of this file before packet 20 — no fixture of the old HTML
        to go stale beside it.
        """
        pages = {}
        for entry_id in self.ids[:2]:
            pages[entry_id] = (self.out(entry_id) / "index.html").read_text(
                encoding="utf-8")
        shutil.rmtree(self.dest)
        self.dest.mkdir()
        line, note = gallery._loads_line, gallery._loads_note
        gallery._loads_line = lambda meta: None
        gallery._loads_note = lambda meta: ""
        self.addCleanup(setattr, gallery, "_loads_line", line)
        self.addCleanup(setattr, gallery, "_loads_note", note)
        for entry_id, before in pages.items():
            with self.subTest(entry=entry_id):
                after = (self.out(entry_id) / "index.html").read_text(
                    encoding="utf-8")
                self.assertEqual(before, after)
                self.assertNotIn("Loads", after)
                self.assertNotIn("stage-loads", after)
                # The template's `$loads` sits at the end of the line above
                # the seed line, so an empty one leaves no blank line and no
                # trailing spaces behind it: the stage of a page that loads
                # nothing is the bytes it was before this packet.
                self.assertIn('</iframe>\n    <p class="stage-meta">', after)
