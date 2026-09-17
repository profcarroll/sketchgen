"""HELD_SCRIPT, actually run (plan §4.3, §4.4).

The rest of the suite reads this script as text and checks the markup it acts
on. Four of its rules cannot be checked that way, and every one of them is a
rule about what the operator's next press will do:

* **One outcome per card, and press again to clear it.** Radios give the first
  half with no script at all; the second half — a filled verb pressed again goes
  back to nothing — is this script's, and so is the Reject/Critique exclusion
  (both read the card's one box, and one sentence cannot be a reason and a
  revision at once).
* **The tally and the Process label.** What the button says the press will do is
  the whole reason there is no confirm dialog, so ``Process 3`` had better mean
  three marks.
* **Needs a sentence.** A marked Critique with an empty box holds Process dimmed
  until it is typed or unmarked, and lets go the moment it is.
* **A refused card keeps its reason.** It comes back from a batch pre-marked,
  and the hint says why the last press did not work until the operator touches
  the card, at which point it goes back to saying what the next press will do.

So the real ``HELD_SCRIPT`` runs in node against the same stub DOM
``tests/js/dom.js`` that ``tests/test_gallery_js.py`` uses for the gallery's
run-in-place, on a page this harness builds the way :func:`web._decision_card`
builds it. node is not a dependency of sketchgen and is not on the node's venv
path, so a machine without it skips this file instead of failing it.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import web  # noqa: E402

DOM = Path(__file__).resolve().parent / "js" / "dom.js"

#: The harness. It builds the tray and four cards — two held, one kept rejection
#: (which has no Reject) and one a batch handed back refused — presses the
#: toggles the way a browser does (checkedness first, then the click, then the
#: change), and prints what the page says after each step.
DRIVER = """
"use strict";

const fs = require("fs");
const vm = require("vm");
const { makeWindow } = require(process.argv[2]);

const SOURCE = fs.readFileSync(process.argv[3], "utf8");

const window = makeWindow();
const document = window.document;

function el(tag, cls) {
  const node = document.createElement(tag);
  if (cls) { node.className = cls; }
  return node;
}

function tray() {
  const sec = el("section", "tray");
  sec.setAttribute("data-state", "");
  sec.setAttribute("data-done", "");
  document.appendChild(sec);
  const form = el("form");
  form.id = "held-batch";
  sec.appendChild(form);
  const chips = el("div", "tally");
  chips.id = "tally";
  form.appendChild(chips);
  const clear = el("button");
  clear.id = "clear";
  form.appendChild(clear);
  const go = el("button", "process");
  go.id = "process";
  go.textContent = "Process";
  form.appendChild(go);
}

// The card _decision_card writes: the box, the toggles, the hint. A kept
// rejection gets two radios instead of three, because it is a rejection
// already.
function card(id, kept) {
  const sec = el("section", "panel card decide");
  sec.id = "entry-" + id;
  document.appendChild(sec);
  const say = el("div", "say");
  sec.appendChild(say);
  const box = el("input");
  box.type = "text";
  box.id = "say-" + id;
  box.value = "";
  say.appendChild(box);
  const acts = el("div", "acts");
  acts.setAttribute("data-entry", String(id));
  if (kept) { acts.setAttribute("data-kept", "1"); }
  say.appendChild(acts);
  const outcomes = kept
    ? [["pub", "publish", "+ Publish"], ["arc", "archive", "\\u2212 Archive"]]
    : [["pub", "publish", "+ Publish"], ["rej", "reject", "\\u00d7 Reject"],
       ["arc", "archive", "\\u2212 Archive"]];
  outcomes.forEach(function (verb) {
    const label = el("label", "tog " + verb[0]);
    const input = el("input");
    input.type = "radio";
    input.setAttribute("name", "do-" + id);
    input.setAttribute("value", verb[1]);
    input.checked = false;
    const span = el("span");
    span.textContent = verb[2];
    label.appendChild(input);
    label.appendChild(span);
    acts.appendChild(label);
  });
  const label = el("label", "tog cri");
  const cri = el("input");
  cri.type = "checkbox";
  cri.setAttribute("name", "cri-" + id);
  cri.setAttribute("value", "on");
  cri.checked = false;
  label.appendChild(cri);
  acts.appendChild(label);
  const hint = el("p", "hint decide-hint");
  hint.textContent = "Mark one outcome, and Critique if it should have a child."
    + " Nothing happens until Process.";
  say.appendChild(hint);
}

// The card a finished batch handed back: pre-marked, its sentence still in the
// box, and the reason in the hint (web.py:_card_hint).
function refused(id) {
  card(id, false);
  const acts = document.querySelector('[data-entry="' + id + '"]');
  acts.querySelector('input[value="publish"]').checked = true;
  document.getElementById("say-" + id).value = "one that will not go";
  const hint = document.getElementById("entry-" + id).querySelector(".decide-hint");
  hint.className = "hint decide-hint bad";
  hint.setAttribute("data-reason", "1");
  hint.textContent = "refused: sketch.js holds an email address"
    + " \u2014 still held, still marked.";
}

const HELD = [431, 432];
const KEPT = 434;
const REFUSED = 436;
tray();
HELD.forEach(function (id) { card(id, false); });
card(KEPT, true);
refused(REFUSED);

const store = {};
const sandbox = {
  window: window,
  document: document,
  sessionStorage: {
    getItem: function (key) {
      return Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null;
    },
    setItem: function (key, value) { store[key] = String(value); },
    removeItem: function (key) { delete store[key]; }
  },
  setInterval: function () { return 0; },
  clearInterval: function () {},
  setTimeout: setTimeout,
  encodeURIComponent: encodeURIComponent,
  console: console,
  JSON: JSON,
  Math: Math,
  Number: Number,
  String: String,
  Object: Object,
  Array: Array,
  RegExp: RegExp,
  Promise: Promise
};

vm.runInContext(SOURCE, vm.createContext(sandbox), { filename: "held.js" });

function fire(node, name, event) {
  (node.listeners[name] || []).forEach(function (fn) { fn(event || {}); });
}

// What a browser does to a radio or a checkbox, in a browser's order: the
// checkedness moves first, then the click listeners run, then change.
function press(id, value) {
  const acts = document.querySelector('[data-entry="' + id + '"]');
  const input = value === "cri"
    ? acts.querySelector('input[type="checkbox"]')
    : acts.querySelector('input[value="' + value + '"]');
  fire(input, "pointerdown");
  if (input.type === "radio") {
    document.querySelectorAll('input[name="do-' + id + '"]').forEach(
      function (other) { other.checked = false; });
    input.checked = true;
  } else {
    input.checked = !input.checked;
  }
  fire(input, "click", { preventDefault: function () {} });
  fire(input, "change", {});
}

function type(id, text) {
  const box = document.getElementById("say-" + id);
  box.value = text;
  fire(box, "input");
}

function clear() {
  const button = document.getElementById("clear");
  fire(button, "click", { preventDefault: function () {} });
}

function marks(id) {
  const acts = document.querySelector('[data-entry="' + id + '"]');
  const one = function (value) {
    const input = acts.querySelector('input[value="' + value + '"]');
    return !!(input && input.checked);
  };
  const host = document.getElementById("entry-" + id);
  const hint = host.querySelector(".decide-hint");
  const box = document.getElementById("say-" + id);
  return {
    publish: one("publish"),
    reject: one("reject"),
    archive: one("archive"),
    cri: acts.querySelector('input[type="checkbox"]').checked === true,
    marked: String(host.className).indexOf("marked") !== -1,
    hint: hint.textContent,
    hint_class: hint.className,
    box_class: box.className
  };
}

function report(what) {
  const go = document.getElementById("process");
  return {
    did: what,
    tally: document.getElementById("tally").textContent,
    process: go.textContent,
    process_off: go.disabled === true,
    title: go.title,
    clear_off: document.getElementById("clear").disabled === true,
    cards: { 431: marks(431), 432: marks(432), 434: marks(KEPT),
             436: marks(REFUSED) },
    saved: store["held-marks"] || ""
  };
}

// The card a batch refused comes first: it arrives marked, so the page is
// already counting it before anybody has pressed anything.
const out = [report("the refused card, untouched")];
press(REFUSED, "archive");
out.push(report("archive 436 instead"));
clear();
out.push(report("nothing"));
press(431, "publish");
out.push(report("publish 431"));
press(431, "archive");
out.push(report("archive 431 instead"));
press(431, "archive");
out.push(report("archive 431 again"));
press(432, "reject");
out.push(report("reject 432"));
press(432, "cri");
out.push(report("critique 432 as well"));
type(432, "let the gust arrive from the left edge");
out.push(report("a sentence for 432"));
press(432, "reject");
out.push(report("reject 432 again"));
press(KEPT, "publish");
press(431, "publish");
out.push(report("publish 434 and 431"));
clear();
out.push(report("clear marks"));
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class HeldScriptTests(unittest.TestCase):
    """The real script, on a real press, in node."""

    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-held-js-"))
        cls.tmp = tmp
        body = web.HELD_SCRIPT
        body = body.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        script = tmp / "held.js"
        script.write_text(body, encoding="utf-8")
        driver = tmp / "driver.js"
        driver.write_text(DRIVER, encoding="utf-8")
        done = subprocess.run(
            [shutil.which("node"), str(driver), str(DOM), str(script)],
            capture_output=True, text=True, timeout=60,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.steps = json.loads(done.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def step(self, what):
        for step in self.steps:
            if step["did"] == what:
                return step
        self.fail(f"the driver never reported {what}")

    def test_an_unmarked_page_says_so_and_process_is_dead(self):
        step = self.step("nothing")
        self.assertEqual("nothing marked — press a verb on any card", step["tally"])
        self.assertEqual("Process", step["process"])
        self.assertTrue(step["process_off"])
        self.assertTrue(step["clear_off"])
        self.assertFalse(step["cards"]["431"]["marked"])

    def test_one_mark_fills_the_tally_and_counts_the_button(self):
        step = self.step("publish 431")
        self.assertEqual("1 publish", step["tally"])
        self.assertEqual("Process 1", step["process"])
        self.assertFalse(step["process_off"])
        self.assertFalse(step["clear_off"])
        self.assertTrue(step["cards"]["431"]["publish"])
        self.assertTrue(step["cards"]["431"]["marked"])
        self.assertEqual(
            "Process will publish this to the grid.", step["cards"]["431"]["hint"]
        )

    def test_a_second_outcome_moves_the_fill_rather_than_adding_one(self):
        step = self.step("archive 431 instead")
        self.assertFalse(step["cards"]["431"]["publish"])
        self.assertTrue(step["cards"]["431"]["archive"])
        self.assertEqual("1 archive", step["tally"])
        self.assertEqual("Process 1", step["process"])

    def test_pressing_the_filled_verb_again_clears_it(self):
        step = self.step("archive 431 again")
        self.assertFalse(step["cards"]["431"]["archive"])
        self.assertFalse(step["cards"]["431"]["marked"])
        self.assertEqual("nothing marked — press a verb on any card", step["tally"])
        self.assertTrue(step["process_off"])

    def test_critique_clears_reject_and_needs_a_sentence(self):
        step = self.step("reject 432")
        self.assertTrue(step["cards"]["432"]["reject"])
        step = self.step("critique 432 as well")
        # Both read the card's one box, so marking either clears the other.
        self.assertFalse(step["cards"]["432"]["reject"])
        self.assertTrue(step["cards"]["432"]["cri"])
        # …and an empty box holds Process until it is typed
        self.assertIn("1 needs a sentence", step["tally"])
        self.assertTrue(step["process_off"])
        self.assertEqual("a marked Critique has an empty box", step["title"])
        self.assertEqual("hint decide-hint warn", step["cards"]["432"]["hint_class"])
        self.assertEqual("need", step["cards"]["432"]["box_class"])

    def test_typing_the_sentence_lets_process_go(self):
        step = self.step("a sentence for 432")
        self.assertNotIn("needs a sentence", step["tally"])
        self.assertEqual("1 critique", step["tally"])
        self.assertFalse(step["process_off"])
        self.assertEqual("", step["title"])
        self.assertEqual("", step["cards"]["432"]["box_class"])
        self.assertEqual(
            "Process will queue a child from the box, then leave it held.",
            step["cards"]["432"]["hint"],
        )

    def test_reject_clears_critique_the_other_way_round(self):
        step = self.step("reject 432 again")
        self.assertFalse(step["cards"]["432"]["cri"])
        self.assertTrue(step["cards"]["432"]["reject"])
        self.assertEqual("1 reject", step["tally"])
        # the box is the reason now, and the hint says which
        self.assertEqual(
            "Process will reject this with the box as the reason.",
            step["cards"]["432"]["hint"],
        )

    def test_the_tally_counts_every_card_and_a_kept_one_publishes_elsewhere(self):
        step = self.step("publish 434 and 431")
        self.assertEqual("2 publish1 reject", step["tally"])
        self.assertEqual("Process 3", step["process"])
        self.assertEqual(
            "Process will publish this to the rejections page.",
            step["cards"]["434"]["hint"],
        )
        # and the marks are in sessionStorage, for the reload a batch ends with
        saved = json.loads(step["saved"])
        self.assertEqual("publish", saved["431"]["out"])
        self.assertEqual("reject", saved["432"]["out"])
        self.assertEqual("publish", saved["434"]["out"])

    def test_clear_marks_empties_every_card_and_the_boxes(self):
        step = self.step("clear marks")
        for entry in ("431", "432", "434", "436"):
            self.assertFalse(step["cards"][entry]["marked"], entry)
            self.assertFalse(step["cards"][entry]["publish"], entry)
            self.assertFalse(step["cards"][entry]["cri"], entry)
        self.assertEqual("nothing marked — press a verb on any card", step["tally"])
        self.assertTrue(step["process_off"])
        self.assertEqual("{}", step["saved"])

    def test_a_refused_card_keeps_its_reason_until_it_is_touched(self):
        # The page loads with the card still marked and the reason on it: why
        # the last press did not work is what a retry needs to know.
        step = self.step("the refused card, untouched")
        card = step["cards"]["436"]
        self.assertTrue(card["publish"])
        self.assertTrue(card["marked"])
        self.assertEqual("hint decide-hint bad", card["hint_class"])
        self.assertIn("holds an email address", card["hint"])
        # …and the tally counts it, so fixing one and pressing again is one press
        self.assertIn("publish", step["tally"])
        # touch it and the hint goes back to what the next press will do
        step = self.step("archive 436 instead")
        card = step["cards"]["436"]
        self.assertFalse(card["publish"])
        self.assertTrue(card["archive"])
        self.assertEqual("hint decide-hint", card["hint_class"])
        self.assertEqual(
            "Process will take it off this page; nothing is deleted.",
            card["hint"],
        )


if __name__ == "__main__":
    unittest.main()
