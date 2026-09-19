"""gallery.js and kiosk.js, actually run (plan §6.7, §6.9; plan §5.4).

The rest of the suite reads the script as text. Three things cannot be checked
that way:

* **Run-in-place.** One click starts one iframe, a second click somewhere else
  does not leave two running, and stopping removes the frame rather than
  hiding it — so this runs the real file in node against a stub DOM
  (``tests/js/dom.js``) and clicks it.
* **The critique form's validator.** It restates ``lineage.validate`` in
  JavaScript, and a restatement is only worth anything if it refuses the same
  text. So the same cases go through both: node types them into the form and
  reports what the page says, Python runs ``lineage.validate`` over them, and
  the two must agree case for case. The *sentences* differ on purpose — the
  page's are short enough to read while typing and the Worker answers with
  Python's — so the page's wording is asserted here verbatim.
* **The kiosk.** Its seven orders, its keys, its one sandboxed frame and its
  settings are behaviour, not text, and the page it drives does not exist
  until the generator writes it — so ``tests/js/kiosk.js`` builds that page
  from the DOM contract ``kiosk.html`` is written to, loads the real script
  onto it, and presses keys at it with a clock it steps by hand. What the
  script must *not* do (kiosk spec §4.5) is still read as text, below.

node is not a dependency of sketchgen and is not on the node's venv path, so a
machine without it skips this file instead of failing it.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import lineage  # noqa: E402

HARNESS = Path(__file__).resolve().parent / "js" / "run_in_place.js"
KIOSK_HARNESS = Path(__file__).resolve().parent / "js" / "kiosk.js"
DOM = Path(__file__).resolve().parent / "js" / "dom.js"
SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "gallery.js"
KIOSK_SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "kiosk.js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RunInPlaceTests(unittest.TestCase):

    def test_one_sketch_runs_at_a_time_on_both_pages(self):
        done = subprocess.run(
            [shutil.which("node"), str(HARNESS)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(
            0, done.returncode, done.stdout + done.stderr
        )
        self.assertIn("ok ", done.stdout)


#: The harness the validator tests drive. It lives here rather than in
#: ``tests/js/`` because packet 9's file scope is this file and the six it
#: names; it loads the real gallery.js into the same stub DOM run_in_place.js
#: uses, builds the critique form the generator writes, and types each case
#: into it.
TYPIST = """
"use strict";

const fs = require("fs");
const vm = require("vm");
const { makeWindow } = require(process.argv[2]);

const SCRIPT = process.argv[3];
const CASES = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

// config.json is the only request that answers: a write path has to be
// configured or the generator would not have written the form at all.
// Everything else — /me, /counts — is offline, as it is in run_in_place.js.
function stubFetch(url) {
  if (String(url).indexOf("config.json") !== -1) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: function () {
        return Promise.resolve({ write_path: "https://write.example.invalid/api" });
      }
    });
  }
  return Promise.reject(new Error("offline"));
}

function hook(document, parent, tag, attribute) {
  const el = document.createElement(tag);
  el.setAttribute(attribute, "");
  parent.appendChild(el);
  return el;
}

function entryPage(document) {
  const block = document.createElement("section");
  block.className = "panel critique-form";
  block.setAttribute("data-critique", "412");
  block.hidden = true;
  document.appendChild(block);
  hook(document, block, "p", "data-critique-out");
  const form = hook(document, block, "div", "data-critique-in");
  form.hidden = true;
  document.field = hook(document, form, "textarea", "data-critique-text");
  document.rule = hook(document, form, "p", "data-critique-rule");
  document.echo = hook(document, form, "em", "data-critique-echo");
  document.button = hook(document, form, "button", "data-critique-send");
  const sent = hook(document, block, "div", "data-critique-sent");
  sent.hidden = true;
  hook(document, sent, "em", "data-critique-echo-sent");
}

const window = makeWindow();
window.fetch = stubFetch;
const document = window.document;
entryPage(document);
window.SKETCHGEN_ROOT = "../../";

vm.runInContext(fs.readFileSync(SCRIPT, "utf8"), vm.createContext({
  window: window,
  document: document,
  fetch: window.fetch,
  URLSearchParams: URLSearchParams,
  Promise: Promise,
  console: console,
  Math: Math,
  Number: Number,
  String: String,
  Object: Object,
  Array: Array,
  JSON: JSON,
  parseInt: parseInt,
  setTimeout: setTimeout
}), { filename: "gallery.js" });

function typed(text) {
  document.field.value = text;
  (document.field.listeners.input || []).forEach(function (fn) { fn({}); });
  return {
    says: document.rule.textContent,
    ok: document.rule.className === "rule good",
    echo: document.echo.textContent,
    disabled: document.button.disabled === true
  };
}

// loadConfig() resolves a tick after the script runs; nothing is wired before
// it, because nothing should be wired before the page knows the write path.
setTimeout(function () {
  console.log(JSON.stringify(CASES.map(typed)));
}, 0);
"""


#: One sentence of critique per case, and the sentence the page must print for
#: it. Whether each one *passes* is not written down here: it is taken from
#: ``lineage.validate`` at run time, which is the whole point of the check.
CASES = [
    ("", "say something"),
    ("   \n  ", "say something"),
    ("let the lines thin ``` at the edge",
     "holds code (```) — a critique becomes a prompt, not a patch"),
    ("let the lines thin { at the edge",
     "holds code ({) — a critique becomes a prompt, not a patch"),
    ("let the lines thin } at the edge",
     "holds code (}) — a critique becomes a prompt, not a patch"),
    ("let the lines thin; and then stop",
     "holds code (;) — a critique becomes a prompt, not a patch"),
    ("let it call noise() nearer the edge",
     "holds code (()) — a critique becomes a prompt, not a patch"),
    ("let the edge => the centre feel deeper",
     "holds code (=>) — a critique becomes a prompt, not a patch"),
    ("let the function that draws the edge thin it",
     "holds code (function ) — a critique becomes a prompt, not a patch"),
    ("let it add <script to the page",
     "holds code (<script) — a critique becomes a prompt, not a patch"),
    ("let the lines thin // at the edge",
     "holds code (//) — a critique becomes a prompt, not a patch"),
    ("let the whole thing cost $5 less attention",
     "holds code ($) — a critique becomes a prompt, not a patch"),
    ("Let the lines thin. Let them stop at the edge.",
     "2 sentences — one is the contract"),
    ("Thin them. Then stop. Then thin again.",
     "3 sentences — one is the contract"),
    # thirty-nine words passes and forty does not, which is where
    # lineage.validate draws it: `len(words) >= MAX_CRITIQUE_WORDS`.
    (" ".join(["thin"] * 39), "one sentence · 39 words · no code"),
    (" ".join(["thin"] * 40), "40 words — under 40 is the contract"),
    ("let the lines thin as they near the edge of the canvas so the field "
     "reads as depth",
     "one sentence · 18 words · no code"),
    ("  let   the lines\nthin at the edge  ", "one sentence · 7 words · no code"),
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CritiqueValidatorTests(unittest.TestCase):
    """The page's validator against Python's, case for case (plan §5.4)."""

    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-js-"))
        cls.tmp = tmp
        harness = tmp / "typist.js"
        harness.write_text(TYPIST, encoding="utf-8")
        cases = tmp / "cases.json"
        cases.write_text(json.dumps([text for text, _ in CASES]), encoding="utf-8")
        done = subprocess.run(
            [shutil.which("node"), str(harness), str(DOM), str(SCRIPT), str(cases)],
            capture_output=True, text=True, timeout=60,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.verdicts = json.loads(done.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def python_accepts(self, text):
        try:
            lineage.validate(text)
        except lineage.CritiqueFailed:
            return False
        return True

    def test_the_page_refuses_exactly_what_lineage_refuses(self):
        for (text, _), verdict in zip(CASES, self.verdicts):
            with self.subTest(text=text[:40]):
                self.assertEqual(self.python_accepts(text), verdict["ok"])

    def test_the_page_says_the_mockup_s_sentence(self):
        for (text, says), verdict in zip(CASES, self.verdicts):
            with self.subTest(text=text[:40]):
                self.assertEqual(says, verdict["says"])

    def test_the_button_is_dead_until_the_sentence_is_a_critique(self):
        for (text, _), verdict in zip(CASES, self.verdicts):
            with self.subTest(text=text[:40]):
                self.assertEqual(not verdict["ok"], verdict["disabled"])

    def test_the_child_s_prompt_echoes_what_was_typed_collapsed(self):
        by_text = dict(zip([text for text, _ in CASES], self.verdicts))
        self.assertEqual(
            "let the lines thin at the edge",
            by_text["  let   the lines\nthin at the edge  "]["echo"],
        )
        # nothing typed is an ellipsis and not an empty line, so the preview
        # keeps its shape while someone thinks
        self.assertEqual("…", by_text[""]["echo"])

    def test_the_code_marks_are_lineage_s_own_list(self):
        source = SCRIPT.read_text(encoding="utf-8")
        found = re.search(r"var CODE_MARKS = \[(.*?)\];", source, re.S)
        self.assertIsNotNone(found, "gallery.js no longer declares CODE_MARKS")
        marks = json.loads("[" + found.group(1) + "]")
        self.assertEqual(list(lineage._CODE_MARKS), marks)

    def test_the_word_limit_is_lineage_s_own_number(self):
        source = SCRIPT.read_text(encoding="utf-8")
        found = re.search(r"var MAX_CRITIQUE_WORDS = (\d+);", source)
        self.assertIsNotNone(found, "gallery.js no longer declares the word limit")
        self.assertEqual(lineage.MAX_CRITIQUE_WORDS, int(found.group(1)))

    def test_the_validator_parses_in_a_browser_without_lookbehind(self):
        # A regex lookbehind is a syntax error in a browser too old for it, and
        # a syntax error anywhere in this file takes the counts, the like
        # button and the sort down with the form.
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("(?<=", source)
        self.assertNotIn("(?<!", source)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class KioskTests(unittest.TestCase):
    """kiosk.js, run for real against the DOM contract (spec §5).

    ``tests/js/kiosk.js`` loads the real script into the stub DOM on a page
    built from the ids ``kiosk.html`` promises, clicks Start, presses keys and
    steps a fake clock, then prints one JSON report. Everything below reads
    that report, so a failure names the behaviour rather than the harness.
    """

    @classmethod
    def setUpClass(cls):
        done = subprocess.run(
            [shutil.which("node"), str(KIOSK_HARNESS)],
            capture_output=True, text=True, timeout=120,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.report = json.loads(done.stdout.strip().splitlines()[-1])

    # ---- the seven orders (§4.2) ----------------------------------------

    def test_each_order_is_the_order_the_grid_s_comparator_would_give(self):
        # The fixture's three entries are pulled as far apart as three things
        # can be: all six deterministic orders are different permutations, so
        # no order can pass by accident.
        self.assertEqual(
            {
                "newest": [33, 22, 11],
                "oldest": [11, 22, 33],
                "liked": [22, 11, 33],
                "reviewed": [33, 11, 22],
                "controversial": [11, 33, 22],
                "consensus": [22, 33, 11],
            },
            {
                name: seen for name, seen in self.report["orders"].items()
                if name != "random"
            },
        )

    def test_random_is_a_permutation_of_the_manifest(self):
        self.assertEqual([11, 22, 33], sorted(self.report["orders"]["random"]))

    def test_liked_falls_back_to_newest_with_no_write_path(self):
        # No write path, no likes: every entry tied at zero is not an order.
        fell_back = self.report["noWritePath"]
        self.assertEqual(33, fell_back["entry"])
        self.assertEqual(
            ["generation 1 · a root", "— views", "— likes"], fell_back["facts"]
        )

    def test_the_fallback_is_what_every_label_says_too(self):
        # A projector that plays newest while saying "liked" is lying about
        # what the room is looking at, so the fallback lands in the state and
        # not only in the sequence.
        fell_back = self.report["noWritePath"]
        self.assertTrue(fell_back["status"].startswith("1 of 3 · newest"))
        self.assertEqual(
            "kiosk.html?order=newest&every=60&show=prompt,authors,generation,views,likes",
            fell_back["launch"],
        )
        self.assertIn("newest first", fell_back["current"])
        self.assertEqual("newest", fell_back["stored"]["order"])

    # ---- the playback timer (§4.2) ---------------------------------------

    def test_the_progress_line_is_elapsed_over_every(self):
        timer = self.report["timer"]
        self.assertEqual("25%", timer["quarter"]["width"])
        self.assertEqual("50%", timer["half"]["width"])

    def test_it_advances_by_itself_after_every_seconds(self):
        timer = self.report["timer"]
        # Sixty seconds in, the fade has started but the sketch has not changed.
        self.assertEqual(33, timer["atTheEnd"]["entry"])
        self.assertEqual(1, timer["atTheEnd"]["frames"])
        # 420 ms later it is the next one, and still only one frame.
        self.assertEqual(22, timer["afterTheFade"]["entry"])
        self.assertEqual(1, timer["afterTheFade"]["frames"])
        self.assertEqual("./e/22/sketch/", timer["afterTheFade"]["src"])

    def test_a_paused_run_does_not_advance(self):
        paused = self.report["paused"]
        # Four minutes of frames at sixty seconds each, and nothing moves.
        self.assertEqual(paused["before"], paused["after"])
        self.assertEqual(1, paused["frames"])
        self.assertTrue(paused["widthHeld"], "the progress line stops too")
        self.assertEqual("progress paused", paused["pausedClass"])
        self.assertEqual("paused", paused["pauseState"])
        self.assertTrue(paused["status"].startswith("paused · 1 of 3"))

    def test_random_reshuffles_on_the_wrap_and_never_repeats_across_it(self):
        random = self.report["random"]
        self.assertEqual(41, len(random["seen"]))
        # A wrap that seats the sketch already on the stage fades out of it and
        # back into it, which reads as a stall rather than as a shuffle.
        self.assertEqual(0, random["repeats"])
        # Twelve wraps that all dealt the same lap would not be a shuffle.
        self.assertGreater(random["laps"], 1)

    def test_a_fixed_canvas_is_scaled_to_fit_and_a_window_sized_one_is_not(self):
        fitting = self.report["fitting"]
        # 800x600 on a 1600x900 stage: limited by height, so 1.5.
        self.assertEqual(11, fitting["fixed"]["entry"])
        self.assertEqual("1200px", fitting["fixed"]["w"])
        self.assertEqual("900px", fitting["fixed"]["h"])
        # No canvas in the manifest, nothing said about the frame: the CSS
        # default fills the stage, which is what a window-sized sketch wants.
        self.assertEqual(22, fitting["windowSized"]["entry"])
        self.assertEqual("", fitting["windowSized"]["w"])
        self.assertEqual("", fitting["windowSized"]["h"])
        self.assertIsNone(fitting["windowSized"]["style"])

    # ---- the start card (§1.7) -------------------------------------------

    def test_the_button_waits_for_the_manifest(self):
        starting = self.report["starting"]
        self.assertTrue(starting["beforeAnything"]["disabled"])
        self.assertEqual("loading…", starting["beforeAnything"]["label"])
        self.assertTrue(starting["loaded"]["welcomeUp"], "the card waits for the click")
        self.assertFalse(starting["loaded"]["disabled"])
        self.assertEqual("Start", starting["loaded"]["label"])

    def test_a_manifest_that_never_arrives_says_so_on_the_card(self):
        # Taking the click and fetching afterwards is how a projector ends up
        # black and deaf: card gone, nothing playing, and because nothing is
        # playing the key handler returns without even opening the menu.
        starting = self.report["starting"]
        self.assertTrue(starting["failed"]["welcomeUp"])
        self.assertTrue(starting["failed"]["disabled"])
        self.assertEqual(
            "Could not load the gallery's list of sketches.",
            starting["failed"]["note"],
        )
        self.assertTrue(starting["afterAClick"]["welcomeUp"], "the click does nothing")
        self.assertFalse(starting["afterAClick"]["playing"])
        self.assertEqual(0, starting["afterAClick"]["frames"])
        self.assertTrue(starting["menuStillShut"])

    def test_an_empty_gallery_is_nothing_to_play(self):
        self.assertTrue(self.report["starting"]["empty"]["disabled"])
        self.assertEqual(
            "Could not load the gallery's list of sketches.",
            self.report["starting"]["empty"]["note"],
        )

    # ---- the keys (§4.1) -------------------------------------------------

    def test_the_first_press_only_opens_the_menu(self):
        keys = self.report["keys"]
        self.assertTrue(keys["menuShutBefore"], "the menu starts shut")
        self.assertTrue(keys["firstPressOpens"])
        self.assertTrue(
            keys["firstPressChangesNothing"],
            "a bumped keyboard must not skip a sketch or pause one",
        )

    def test_the_second_press_acts(self):
        self.assertEqual(33, self.report["keys"]["entryBefore"])
        self.assertEqual(22, self.report["keys"]["advancedTo"])

    def test_escape_closes_the_menu_and_the_hint_comes_back(self):
        keys = self.report["keys"]
        self.assertTrue(keys["escapeCloses"])
        self.assertTrue(keys["statusAfterEscape"].startswith("2 of 3 · newest"))
        self.assertTrue(keys["statusAfterEscape"].endswith("any key controls"))

    def test_the_menu_closes_itself_after_eight_seconds(self):
        self.assertTrue(self.report["keys"]["timedOut"])

    def test_every_clamps_at_600_and_at_15(self):
        self.assertEqual("600 s", self.report["keys"]["longer"])
        self.assertEqual("15 s", self.report["keys"]["shorter"])

    # ---- the frame (§4.3, §4.5) ------------------------------------------

    def test_one_frame_at_a_time_sandboxed_to_scripts_and_nothing_else(self):
        frame = self.report["frame"]
        self.assertEqual(1, frame["framesAtStart"])
        self.assertEqual("allow-scripts", frame["sandbox"])
        self.assertEqual(["class", "sandbox", "src", "title"], frame["attributes"])
        self.assertEqual("./e/33/sketch/", frame["src"])

    def test_opening_the_code_column_adds_no_second_frame(self):
        frame = self.report["frame"]
        self.assertTrue(frame["codeColumnShown"])
        self.assertEqual(1, frame["framesWithCode"])

    def test_advancing_leaves_exactly_one_frame(self):
        frame = self.report["frame"]
        self.assertEqual(1, frame["framesAfterAdvance"])
        self.assertEqual("./e/22/sketch/", frame["srcAfterAdvance"])

    def test_the_code_column_names_the_file_its_size_and_that_it_is_unedited(self):
        frame = self.report["frame"]
        self.assertEqual("e/33/sketch/sketch.js · 78 bytes, unedited", frame["codeHeading"])
        self.assertEqual(4, frame["codeLines"])

    def test_the_heading_counts_bytes_and_not_characters(self):
        # The fixture's source has an em dash in a comment, so the two differ.
        source = self.report["source"]
        self.assertNotEqual(source["chars"], source["bytes"])
        self.assertIn(f"{source['bytes']} bytes", self.report["frame"]["codeHeading"])

    def test_it_asks_for_nothing_but_the_five_things_it_may_ask_for(self):
        asked = self.report["frame"]["asked"]
        self.assertEqual("./kiosk.json", asked[0])
        self.assertEqual("./config.json", asked[1])
        self.assertEqual(
            "https://write.example.invalid/api/counts?entries=11%2C22%2C33", asked[2]
        )
        # One source per entry whose code was shown, and nothing else at all.
        self.assertEqual(
            ["./e/33/sketch/sketch.js", "./e/22/sketch/sketch.js"], asked[3:]
        )

    def test_no_request_carries_a_method_credentials_or_headers(self):
        # cache: "no-store" on the two files the generator rewrites is the only
        # option any call passes. Nothing names a method, credentials or a
        # header, because every one of those would be a request this page has
        # no business making.
        self.assertEqual(
            [["cache"], ["cache"], [], [], []], self.report["frame"]["inits"]
        )

    # ---- settings (§1.8) -------------------------------------------------

    def test_a_url_sets_the_state_and_the_launch_link_prints_it_back(self):
        settings = self.report["settings"]
        self.assertEqual(
            "kiosk.html?order=liked&every=45&show=prompt,code", settings["launch"]
        )
        self.assertEqual("45 s", settings["every"])
        self.assertIn("most liked", settings["order"])
        self.assertEqual(2, settings["overlaysOn"])
        self.assertEqual("2 of 13 on", settings["count"])

    def test_an_acting_key_writes_both_the_address_bar_and_storage(self):
        settings = self.report["settings"]
        self.assertEqual(settings["launch"], settings["launchAfterKeys"])
        self.assertEqual(
            "/kiosk.html?order=liked&every=45&show=prompt,code", settings["address"]
        )
        self.assertEqual(
            {"every": 45, "order": "liked", "show": {"prompt": True, "code": True}},
            settings["stored"],
        )

    def test_a_url_parameter_beats_a_stored_one_which_beats_the_default(self):
        precedence = self.report["precedence"]
        self.assertEqual(
            "kiosk.html?order=oldest&every=120&show=brief", precedence["fromStorage"]
        )
        # ?every=30 overrides the stored 120; the stored order and overlays,
        # which the URL says nothing about, are left alone.
        self.assertEqual(
            "kiosk.html?order=oldest&every=30&show=brief", precedence["urlWins"]
        )

    # ---- the words (§4.4) ------------------------------------------------

    def test_the_caption_says_what_the_mockup_says(self):
        words = self.report["words"]
        self.assertEqual("#33A square of quiet colour.", words["prompt"])
        self.assertIsNone(words["revisions"], "a root has no revisions line")
        self.assertEqual(
            "Prompted by profcarroll · planned by gemma4:e4b · written by "
            "qwen3-coder:30b-a3b-q4_K_M under the control rules",
            words["authors"],
        )
        self.assertEqual(
            [
                "generation 1 · a root",
                "900 views",
                "2 likes",
                # The date's wording is the viewer's locale; the clock is UTC.
                words["facts"][3],
                "700 prompt + 300 completion tokens",
                "written in 12.3 s",
                "CC BY 4.0",
            ],
            words["facts"],
        )
        self.assertTrue(words["facts"][3].endswith("10:00 UTC"))

    def test_a_revised_entry_says_how_many_times_and_what_the_latest_was(self):
        revised = self.report["revised"]
        self.assertEqual(11, revised["entry"])
        self.assertEqual("#11A field of slow lines.", revised["prompt"])
        self.assertEqual(
            "revised once, latest: let the lines thin as they near the edge.",
            revised["revisions"],
        )
        self.assertTrue(revised["authors"].endswith(", gate passed on attempt 2"))

    def test_judgment_prints_the_score_the_pairs_and_the_quadrant(self):
        words = self.report["words"]
        self.assertEqual("1.50 over 3 pairs", words["human"])
        self.assertEqual("looks good, misses the brief", words["humanQuad"])
        self.assertEqual("0.60 over 3 pairs", words["agent"])
        self.assertEqual("neither", words["agentQuad"])

    def test_a_population_that_has_not_judged_says_no_pairs_yet(self):
        unjudged = self.report["unjudged"]
        self.assertEqual("no pairs yet", unjudged["human"])
        self.assertEqual("no pairs yet", unjudged["agent"])
        # No score, no square: one coordinate is not a point.
        self.assertEqual("", unjudged["humanQuad"])

    def test_a_missing_wall_time_is_an_em_dash_and_not_zero_seconds(self):
        # "written in 0.0 s" would be a claim about how long the executor took.
        self.assertEqual(["written in —"], self.report["unjudged"]["facts"])


    # ---- the one write (docs/plans/kiosk-views.md §3) --------------------

    def test_a_sketch_becomes_a_view_after_ten_seconds_and_not_before(self):
        run = self.report["views"]
        self.assertEqual(0, run["atNine"])
        self.assertEqual(
            [
                {
                    "url": "https://write.example.invalid/api/view",
                    "method": "POST",
                    "body": {"entry_id": run["seat"], "source": "kiosk"},
                    # No credentials and no headers but the content type: the
                    # projector posts a view and names nobody, not even
                    # itself. This is the same assertion the text test makes,
                    # made again against what actually reached the network.
                    "keys": ["body", "headers", "method"],
                }
            ],
            run["atTen"],
        )

    def test_one_seat_is_one_view_however_many_frames_it_takes(self):
        # The test that matters most. /view de-duplicates a signed-in viewer
        # and the kiosk signs nobody in, so a view posted per frame would be
        # sixty a second into a counter with nothing downstream to catch it.
        self.assertEqual(1, self.report["views"]["afterSixHundredFrames"])

    def test_a_sketch_somebody_skipped_past_is_not_a_view(self):
        run = self.report["views"]["afterASkip"]
        self.assertNotEqual(run["entry"], run["now"])
        self.assertEqual(0, run["posts"])

    def test_the_ten_seconds_are_playing_seconds_not_wall_clock(self):
        # Paused at nine seconds, then ten minutes of a paused room: still
        # nothing. One second after it plays again, the view it had earned.
        self.assertEqual(0, self.report["views"]["whilePaused"])
        self.assertEqual(1, self.report["views"]["afterResuming"])

    def test_a_slot_shorter_than_the_threshold_still_counts(self):
        # Fifteen seconds is the shortest slot the kiosk allows, and a
        # projector set to it shows real sketches to a real room.
        self.assertEqual(1, self.report["views"]["shortSlot"])

    def test_it_stops_counting_an_empty_room_and_starts_again_when_asked(self):
        run = self.report["views"]
        # Eight hours of playing seconds at ten minutes a sketch is forty-eight
        # sketches; the forty-eighth is past the limit and is not counted.
        self.assertEqual(47, run["unattended"])
        # The sketches never stopped — only the counting did.
        self.assertEqual(1, run["stillPlaying"])
        # One key, and the room is an audience again.
        self.assertEqual(48, run["afterSomebodyArrives"])

    def test_views_0_turns_it_off_and_survives_the_next_key(self):
        run = self.report["views"]["off"]
        self.assertEqual(0, run["posts"])
        # persist() rebuilds the address bar from query(), so a parameter
        # missing from it is a parameter the first acting key throws away —
        # and the launch link would then hand somebody a projector that counts
        # when the one it was copied from did not.
        self.assertIn("&views=0", run["link"])
        self.assertIn("&views=0", run["bar"])

    def test_the_gallery_can_turn_it_off_without_a_deploy(self):
        # kiosk_views in config.json: one line in the gallery checkout, which
        # is the only switch there is — the write path has none.
        self.assertEqual(0, self.report["views"]["configOff"])

    def test_a_gallery_with_no_write_path_posts_nothing(self):
        self.assertEqual(0, self.report["views"]["noWritePathPosts"])


def _without_comments(source: str) -> str:
    """kiosk.js with its comments taken out, so the bans below hold on code.

    The file's own header names the things it must not do, which is exactly
    why these checks cannot be run over the raw text: a comment saying *this
    never POSTs* would fail a test for ``method:``. Block and line comments
    only, which is all this file has — no string in it holds ``//`` or ``/*``.
    """
    source = re.sub(r"/\*[\s\S]*?\*/", "", source)
    return re.sub(r"(?m)^\s*//.*$", "", source)


class KioskScriptTextTests(unittest.TestCase):
    """What kiosk.js must not contain (spec §4.5). Read as text, no node."""

    @classmethod
    def setUpClass(cls):
        cls.code = _without_comments(KIOSK_SCRIPT.read_text(encoding="utf-8"))

    def test_it_writes_one_thing_and_only_one(self):
        # A play on a projector is a view now (docs/plans/kiosk-views.md), but
        # it is the only write on this page and /view is the only endpoint it
        # may reach. Every method other than GET is a write to this write
        # path, so the count of them is the count of writes: one.
        methods = re.findall(r"method\s*:\s*\"([A-Z]+)\"", self.code)
        self.assertEqual(["POST"], methods)
        # And it goes where it says it goes. The fetch and its options are one
        # expression in the source, so the endpoint and the method that reach
        # the network together are read together here.
        posts = re.findall(
            r"fetch\(([^\n]*?),\s*\{\s*\n\s*method", self.code
        )
        self.assertEqual(['base() + "/view"'], [one.strip() for one in posts])

    def test_it_presents_no_identity(self):
        # /counts is a public read and /view takes an anonymous write; the
        # kiosk signs nobody in and so has nothing to present. This matters
        # more now that it writes, not less: a projector that presented one
        # would file every sketch it played under whoever last signed in on
        # that machine.
        self.assertNotIn("document.cookie", self.code)
        self.assertNotIn("credentials", self.code)
        self.assertNotIn("Authorization", self.code)

    def test_it_never_evaluates_a_sketch_or_talks_to_one(self):
        self.assertNotIn("new Function", self.code)
        self.assertNotIn("eval(", self.code)
        # allow-scripts without allow-same-origin is opaque by design.
        self.assertNotIn("postMessage", self.code)

    def test_the_only_storage_key_it_touches_is_its_own(self):
        calls = re.findall(
            r"localStorage\.(?:getItem|setItem|removeItem)\(\s*([^,)]+)", self.code
        )
        self.assertTrue(calls, "kiosk.js no longer touches localStorage at all")
        self.assertEqual({"STORAGE_KEY"}, {call.strip() for call in calls})
        found = re.search(r'var STORAGE_KEY = "([^"]+)";', self.code)
        self.assertIsNotNone(found, "kiosk.js no longer declares STORAGE_KEY")
        self.assertEqual("sketchgen-kiosk", found.group(1))
        self.assertEqual(
            ["sketchgen-kiosk"], re.findall(r'"(sketchgen[-_][a-z]+)"', self.code)
        )

    def test_it_parses_in_a_browser_without_lookbehind(self):
        # The same line gallery.js holds: a lookbehind is a syntax error in a
        # browser too old for it, and a syntax error here is a black screen.
        self.assertNotIn("(?<", self.code)

    def test_it_is_es5_like_the_rest_of_the_gallery(self):
        # No build step means the file is the file the browser gets.
        self.assertNotIn("=>", self.code)
        self.assertNotIn("`", self.code)
        self.assertIsNone(re.search(r"\b(?:let|const)\s+\w+\s*=", self.code))


if __name__ == "__main__":
    unittest.main()
