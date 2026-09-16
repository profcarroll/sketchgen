"""gallery.js, actually run (plan §6.7, §6.9; plan §5.4).

The rest of the suite reads the script as text. Two things cannot be checked
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
DOM = Path(__file__).resolve().parent / "js" / "dom.js"
SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "gallery.js"


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


if __name__ == "__main__":
    unittest.main()
