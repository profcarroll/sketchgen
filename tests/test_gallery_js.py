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
        self.assertEqual(33, self.report["noWritePath"]["entry"])
        self.assertEqual(
            ["generation 1 · a root", "— views", "— likes"],
            self.report["noWritePath"]["facts"],
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
        self.assertEqual("e/33/sketch/sketch.js · 47 bytes, unedited", frame["codeHeading"])
        self.assertEqual(3, frame["codeLines"])

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
        # config.json's cache: "no-store" is the only option any call passes.
        self.assertEqual([[], ["cache"], [], [], []], self.report["frame"]["inits"])

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

    def test_it_never_writes_anything(self):
        # A play on a projector is not a view. No POST, and no method: at all,
        # because every method other than GET is a write to this write path.
        self.assertNotIn("method:", self.code)
        self.assertNotIn("method :", self.code)

    def test_it_presents_no_identity(self):
        # /counts is a public read; the kiosk signs nobody in and so has
        # nothing to present.
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
