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
SWIPE_HARNESS = Path(__file__).resolve().parent / "js" / "swipe.js"
DOM = Path(__file__).resolve().parent / "js" / "dom.js"
SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "gallery.js"
KIOSK_SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "kiosk.js"
SWIPE_SCRIPT = Path(__file__).resolve().parent.parent / "sketchgen" / "assets" / "swipe.js"


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


#: The §6.2 harness: gallery.js on an entry page, with and without ``?kiosk``.
#:
#: The page is the markup ``entry.html`` writes — the hidden strip, the
#: engagement section it anchors to, and the critique form when there is a
#: write path — and the run reports what the strip did, what the address bar
#: was left as, and what the view POST carried. Same stub DOM as the two
#: harnesses above.
SCANNER = """
"use strict";

const fs = require("fs");
const vm = require("vm");
const { makeWindow } = require(process.argv[2]);

const SCRIPT = process.argv[3];
const CASES = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

function el(document, parent, tag, attrs, text) {
  const node = document.createElement(tag);
  Object.keys(attrs || {}).forEach(function (name) {
    if (name === "hidden") { node.hidden = attrs[name]; return; }
    node.setAttribute(name, attrs[name]);
  });
  if (text !== undefined) { node.textContent = text; }
  parent.appendChild(node);
  return node;
}

function text(document, parent, value) {
  parent.appendChild(document.createTextNode(value));
}

// The page entry.html writes, as far as §6.2 touches it — the strip's own
// text nodes included, because the separator between the second verb and the
// third is inside the span that may be removed and that is the whole point.
function entryPage(document, withWritePath) {
  const main = el(document, document.body, "main", {
    class: "entry", "data-entry": "82"
  });
  const strip = el(document, main, "p", {
    class: "scanned", "data-scanned": "", hidden: true
  });
  text(document, strip, "You scanned this from a projection. ");
  el(document, strip, "a", { href: "../../compare.html?a=82" }, "Judge it against another");
  text(document, strip, " \\u00b7 ");
  el(document, strip, "a", { href: "#engagement" }, "Like it");
  const revision = el(document, strip, "span", { "data-scanned-critique": "" });
  text(document, revision, " \\u00b7 ");
  el(document, revision, "a", { href: "#critique-text" }, "Ask for a revision");
  text(document, strip, ".");
  const engagement = el(document, main, "section", {
    class: "engagement", id: "engagement"
  });
  // The like button's offer of a sign-in, which §6.3 has to wire without
  // taking the href paintSession() puts on it.
  el(document, engagement, "a", {
    class: "login", "data-login": "", href: "../../index.html"
  }, "sign in with GitHub to like");
  if (withWritePath) {
    const form = el(document, main, "section", {
      class: "panel critique-form", "data-critique": "82", hidden: true
    });
    // And the critique form's, so that the pair proves paintSession() is not
    // the only thing on the page that iterates all of them.
    el(document, form, "a", {
      class: "login", "data-login": "", href: "../../index.html"
    }, "Sign in with GitHub");
    el(document, form, "textarea", { id: "critique-text", "data-critique-text": "" });
  }
  return { main: main, strip: strip };
}

function run(one) {
  const window = makeWindow();
  const document = window.document;
  const asked = [];
  window.location.pathname = "/e/82/";
  window.location.search = one.search;
  const page = entryPage(document, one.writePath !== false);
  window.SKETCHGEN_ROOT = "../../";
  window.fetch = function (url, init) {
    asked.push({ url: String(url), init: init || null });
    if (String(url).indexOf("config.json") !== -1) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: function () {
          return Promise.resolve({
            write_path: one.writePath === false
              ? "" : "https://write.example.invalid/api"
          });
        }
      });
    }
    return Promise.reject(new Error("offline"));
  };

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

  // Every sign-in offer on the page, clicked one at a time with storage
  // wiped between them, so the answer says which anchor wrote what rather
  // than only that something did (§6.3). The href is read afterwards
  // because the note must not cost the sign-in it rides along with.
  function signIn(document, window) {
    const out = [];
    Array.prototype.forEach.call(
      document.querySelectorAll("[data-login]"),
      function (anchor) {
        window.localStorage.removeItem("sketchgen-return");
        anchor.click();
        out.push({
          note: window.localStorage.getItem("sketchgen-return"),
          href: anchor.getAttribute("href")
        });
      }
    );
    return out;
  }

  return new Promise(function (resolve) {
    // Two ticks: loadConfig() resolves on the first and sendView() runs on it.
    setTimeout(function () {
      const view = asked.filter(function (one) {
        return one.url.indexOf("/view") !== -1;
      })[0] || null;
      resolve({
        hidden: page.strip.hidden === true,
        text: page.strip.textContent,
        links: Array.prototype.map.call(
          page.strip.querySelectorAll("a"),
          function (a) { return a.getAttribute("href"); }
        ),
        address: window.history.lastUrl,
        viewBody: view ? view.init.body : null,
        logins: one.signIn ? signIn(document, window) : null
      });
    }, 0);
  });
}

(async function () {
  const out = [];
  for (const one of CASES) { out.push(await run(one)); }
  console.log(JSON.stringify(out));
})();
"""

#: The searches §6.2 names, and one that must leave the strip alone.
SCANS = [
    {"search": "?kiosk"},
    {"search": "?kiosk="},
    {"search": "?kiosk=1"},
    {"search": ""},
    {"search": "?q=lines"},
    # Another parameter alongside it: the strip fires and ?q survives.
    {"search": "?kiosk&q=lines&sort=liked"},
    # No write path, so the generator wrote no critique form and the third
    # verb has nothing to point at.
    {"search": "?kiosk", "writePath": False},
    # Signed out and leaving for GitHub, from a scan and from a plain visit
    # (§6.3). The note differs by one parameter and nothing else.
    {"search": "?kiosk", "signIn": True},
    {"search": "", "signIn": True},
]


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
class ArrivedByScanTests(unittest.TestCase):
    """The entry page's greeting for somebody who scanned a wall (qr.md §6.2)."""

    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-scan-"))
        cls.tmp = tmp
        harness = tmp / "scanner.js"
        harness.write_text(SCANNER, encoding="utf-8")
        cases = tmp / "scans.json"
        cases.write_text(json.dumps(SCANS), encoding="utf-8")
        done = subprocess.run(
            [shutil.which("node"), str(harness), str(DOM), str(SCRIPT), str(cases)],
            capture_output=True, text=True, timeout=60,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.runs = json.loads(done.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_for(self, search, **extra):
        for case, result in zip(SCANS, self.runs):
            if case["search"] == search and all(
                case.get(key) == value for key, value in extra.items()
            ):
                return result
        raise AssertionError(f"no run for {search!r}")

    def test_every_shape_of_the_bare_key_counts(self):
        # params.has, not params.get: a bare key parses to "", which is falsy,
        # and a version of this that silently never fired would be worse than
        # one that does not exist. A reader that normalises the key to
        # ?kiosk=1 has to count too.
        for search in ("?kiosk", "?kiosk=", "?kiosk=1"):
            with self.subTest(search=search):
                self.assertFalse(self.run_for(search)["hidden"])

    def test_no_param_leaves_the_strip_hidden(self):
        for search in ("", "?q=lines"):
            with self.subTest(search=search):
                self.assertTrue(self.run_for(search)["hidden"])
        # And nothing is rewritten: a page nobody scanned keeps its address.
        self.assertIsNone(self.run_for("?q=lines")["address"])

    def test_the_strip_says_the_three_things_the_page_can_do(self):
        run = self.run_for("?kiosk")
        self.assertEqual(
            "You scanned this from a projection. Judge it against another · "
            "Like it · Ask for a revision.",
            run["text"],
        )
        # The page's own controls, not new behaviour: a link to compare and
        # two in-page anchors.
        self.assertEqual(
            ["../../compare.html?a=82", "#engagement", "#critique-text"],
            run["links"],
        )

    def test_the_param_leaves_the_address_bar_and_the_others_survive(self):
        # An address bar people copy from should not carry a projection's
        # provenance, which is what claimTokenFromHash() says about the
        # session token for the same reason.
        self.assertEqual("/e/82/", self.run_for("?kiosk")["address"])
        both = self.run_for("?kiosk&q=lines&sort=liked")
        self.assertFalse(both["hidden"])
        self.assertEqual("/e/82/?q=lines&sort=liked", both["address"])

    def test_a_verb_the_gallery_cannot_honour_is_not_offered(self):
        # No write path, so no critique form on the page at all: the third
        # link and its separator go rather than pointing at nothing.
        run = self.run_for("?kiosk", writePath=False)
        self.assertFalse(run["hidden"])
        self.assertEqual(
            "You scanned this from a projection. Judge it against another · Like it.",
            run["text"],
        )
        self.assertEqual(["../../compare.html?a=82", "#engagement"], run["links"])

    def test_a_scan_leaves_a_note_saying_where_to_come_back_to(self):
        # Both offers of a sign-in write it, and both write the same thing:
        # the entry's own directory, built from data-entry and not from the
        # address bar, with the greeting the scanner arrived with still on it
        # so the page reads the same when they land (qr.md §6.3).
        for one in self.run_for("?kiosk", signIn=True)["logins"]:
            self.assertEqual("e/82/?kiosk", one["note"])
            # And the sign-in itself is untouched: the note rides along with
            # the navigation the anchor was already going to make.
            self.assertEqual("https://write.example.invalid/api/login", one["href"])

    def test_a_visitor_who_did_not_scan_is_carried_back_without_the_greeting(self):
        # ?kiosk is a claim about how somebody arrived, so it goes on the note
        # only when the strip that says so is actually up.
        for one in self.run_for("", signIn=True)["logins"]:
            self.assertEqual("e/82/", one["note"])
            self.assertEqual("https://write.example.invalid/api/login", one["href"])

    def test_the_note_is_written_by_every_offer_of_a_sign_in(self):
        # Two on an entry page with a write path — the like button's and the
        # critique form's; the composer's is the index's alone (_composer()).
        # A fix that wired only the first would strand whoever signed in from
        # the other, so the count is asserted and not assumed.
        self.assertEqual(2, len(self.run_for("?kiosk", signIn=True)["logins"]))

    def test_the_view_post_is_the_same_body_with_or_without_the_param(self):
        # This packet adds no field to the write path (§6.2, §9). The signal
        # is in the URL; recording it is a full-stack packet and not this one.
        self.assertEqual('{"entry_id":82}', self.run_for("?kiosk")["viewBody"])
        self.assertEqual('{"entry_id":82}', self.run_for("")["viewBody"])


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
            "kiosk.html?order=newest&every=60&size=native&show=prompt,authors,generation,views,likes,qr",
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

    def test_a_fixed_canvas_frame_is_the_canvas_and_the_scale_does_the_fitting(self):
        fitting = self.report["fitting"]
        # The frame is laid out at the sketch's own size, not at a fitted box.
        # Sizing the box was the old bug: a p5 canvas is exactly as big as
        # createCanvas() made it and does not grow with its frame, so an
        # 800x600 sketch in a fitted frame stayed 800x600 in its top-left
        # corner. The scale below is what the projector actually magnifies.
        self.assertEqual(11, fitting["fixed"]["entry"])
        self.assertEqual("800px", fitting["fixed"]["w"])
        self.assertEqual("600px", fitting["fixed"]["h"])
        # 800x600 fits inside 1600x900 as it is, and "as prompted" means it.
        self.assertEqual("1", fitting["fixed"]["sx"])
        self.assertEqual("1", fitting["fixed"]["sy"])
        # No canvas in the manifest, nothing said about the frame: the CSS
        # default fills the stage unscaled, which is what a window-sized
        # sketch wants.
        self.assertEqual(22, fitting["windowSized"]["entry"])
        self.assertEqual("", fitting["windowSized"]["w"])
        self.assertEqual("", fitting["windowSized"]["h"])
        self.assertEqual("", fitting["windowSized"]["sx"])
        self.assertEqual("", fitting["windowSized"]["sy"])
        self.assertIsNone(fitting["windowSized"]["style"])

    # ---- Z: the size a sketch is put on the stage at ----------------------

    def test_z_walks_the_four_sizes_and_comes_back(self):
        sizing = self.report["sizing"]
        walk = sizing["walk"]
        # The frame is the canvas in every mode; only the scale moves.
        for seat in walk:
            self.assertEqual("800px", seat["w"])
            self.assertEqual("600px", seat["h"])
        # as prompted -> fit -> fill -> stretch -> as prompted, on 1600x900:
        #   fit     min(1600/800, 900/600) = 1.5, all of it, bars left and right
        #   fill    max(1600/800, 900/600) = 2,   no bars, top and bottom cropped
        #   stretch the two ratios apart, which is the only mode where they are
        self.assertEqual(["1", "1.5", "2", "2", "1"], [seat["sx"] for seat in walk])
        self.assertEqual(["1", "1.5", "2", "1.5", "1"], [seat["sy"] for seat in walk])
        # The fourth press is as good as never having pressed it at all.
        self.assertEqual(walk[0], walk[4])
        self.assertEqual(
            ["fit to screen", "fill screen", "stretch to screen", "as prompted"],
            sizing["labels"],
        )
        # Each state says what it costs, because two of the four throw pixels
        # away and an operator aiming a projector should be told which.
        self.assertIn("cropped", sizing["notes"][1])
        self.assertIn("distorted", sizing["notes"][2])

    def test_a_window_sized_sketch_is_the_stage_in_every_size(self):
        sizing = self.report["sizing"]
        # Nothing the key can do to a sketch that already sized itself to the
        # room it was given: four presses, four untouched frames.
        for seat in sizing["windowSized"]:
            self.assertEqual({"entry": 22, "w": "", "h": "", "sx": "", "sy": ""}, seat)
        # And the menu says so, rather than leaving somebody pressing Z at a
        # screen that never moves.
        self.assertEqual("", sizing["windowSizedRow"])
        self.assertIn("sizes itself", sizing["windowSizedNote"])

    def test_as_prompted_still_shrinks_a_canvas_bigger_than_the_stage(self):
        # The one thing "as prompted" cannot honour. 800x600 on a 400x300
        # stage comes down by half; the alternative is cropping three quarters
        # of the sketch and calling it the artist's intent.
        down = self.report["sizingDown"]
        self.assertEqual("800px", down["w"])
        self.assertEqual("600px", down["h"])
        self.assertEqual("0.5", down["sx"])
        self.assertEqual("0.5", down["sy"])

    def test_a_size_persists_and_a_link_beats_it(self):
        sizing = self.report["sizingPersists"]
        # Stored: the projector comes back up the way it was left.
        self.assertEqual("2", sizing["fromStorage"]["sx"])
        self.assertEqual("2", sizing["fromStorage"]["sy"])
        # And the launch link names it, so the link reproduces the projector.
        self.assertIn("size=fill", sizing["fromStorageLink"])
        # A parameter beats a stored setting, as every other setting does.
        self.assertEqual("2", sizing["urlWins"]["sx"])
        self.assertEqual("1.5", sizing["urlWins"]["sy"])
        # A size nobody defined is not a size: the stored one stands.
        self.assertEqual("2", sizing["nonsense"]["sx"])
        self.assertEqual("2", sizing["nonsense"]["sy"])

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
            "kiosk.html?order=liked&every=45&size=native&show=prompt,code", settings["launch"]
        )
        self.assertEqual("45 s", settings["every"])
        self.assertIn("most liked", settings["order"])
        self.assertEqual(2, settings["overlaysOn"])
        self.assertEqual("2 of 14 on", settings["count"])

    def test_an_acting_key_writes_both_the_address_bar_and_storage(self):
        settings = self.report["settings"]
        self.assertEqual(settings["launch"], settings["launchAfterKeys"])
        self.assertEqual(
            "/kiosk.html?order=liked&every=45&size=native&show=prompt,code", settings["address"]
        )
        self.assertEqual(
            {
                "v": 2,
                "every": 45,
                "order": "liked",
                "size": "native",
                "show": {"prompt": True, "code": True},
            },
            settings["stored"],
        )

    def test_a_url_parameter_beats_a_stored_one_which_beats_the_default(self):
        precedence = self.report["precedence"]
        self.assertEqual(
            "kiosk.html?order=oldest&every=120&size=native&show=brief", precedence["fromStorage"]
        )
        # ?every=30 overrides the stored 120; the stored order and overlays,
        # which the URL says nothing about, are left alone.
        self.assertEqual(
            "kiosk.html?order=oldest&every=30&size=native&show=brief", precedence["urlWins"]
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

    # ---- the QR overlay (qr.md §5) ---------------------------------------

    def test_the_code_is_on_by_default_and_the_launch_link_says_so(self):
        # A projection nobody can act on is a screensaver (qr.md §1.7), so
        # this is the one overlay whose default is the point of the packet.
        code = self.report["qr"]
        self.assertEqual(1, code["onByDefault"]["images"])
        self.assertIn("qr", code["launch"].split("show=")[1].split(","))

    def test_the_menu_lists_fourteen_overlays_and_counts_them(self):
        code = self.report["qr"]
        self.assertEqual(14, code["menuRows"])
        self.assertEqual("6 of 14 on", code["menuCount"])

    def test_the_image_is_the_manifest_s_kiosk_code_and_is_decorative(self):
        # The path comes from the manifest and is never assembled in the
        # script, and it is the -kiosk file: the manifest is read by one page
        # and that page is the projection (§4).
        code = self.report["qr"]
        self.assertEqual("./e/33/qr-kiosk.svg", code["onByDefault"]["src"])
        # alt="" because the URL is printed as text right underneath: a screen
        # reader that announced "QR code" and stopped would be worse (§1.10).
        self.assertEqual("", code["onByDefault"]["alt"])

    def test_the_three_lines_are_the_ones_section_five_three_fixes(self):
        # The printed URL is the clean one — the code beside it holds ?kiosk
        # and the line does not, because typing is not scanning (§1.8).
        self.assertEqual(
            [
                "scan to open #33",
                "profcarroll.github.io/sketchgen-gallery/e/33/",
                "judge it · like it · ask for a revision",
            ],
            self.report["qr"]["onByDefault"]["lines"],
        )

    def test_the_words_are_still_first_in_the_markup(self):
        # The code shows at the lower left, but it is put there by CSS
        # (`order: -1`, qr.md §5.2) and not by emitting it first. The words
        # are what the caption says; the tile is aria-hidden with the URL
        # printed beside it, and meeting it first is no use to anyone reading
        # this page as text. Nothing on screen reveals which order the markup
        # is in, so this is the only thing standing between that decision and
        # the first person who "fixes" captionShell() to match the layout.
        self.assertEqual(["words", "qr"], self.report["qr"]["onByDefault"]["order"])
        # And with the code off, the words are the caption, alone.
        self.assertEqual(["words"], self.report["qr"]["toggledOff"]["order"])

    def test_it_swaps_with_the_entry_and_never_leaves_two(self):
        after = self.report["qr"]["afterAdvance"]
        self.assertEqual(1, after["images"])
        self.assertEqual("./e/22/qr-kiosk.svg", after["src"])
        self.assertEqual("scan to open #22", after["lines"][0])

    def test_q_toggles_it_with_the_menu_open(self):
        code = self.report["qr"]
        self.assertEqual(0, code["toggledOff"]["images"])
        self.assertEqual(1, code["toggledOn"]["images"])

    def test_h_takes_it_away_with_everything_else(self):
        # on() already gates on state.hideAll, and an empty caption has to
        # stay genuinely empty or the scrim never lifts off the stage.
        hidden = self.report["qr"]["hidden"]
        self.assertEqual(0, hidden["images"])
        self.assertEqual("", hidden["caption"])

    def test_an_explicit_show_list_does_not_get_the_default_thrown_in(self):
        # ?show=prompt is an explicit list (kiosk.md §1.8), so it turns the
        # code off along with everything else it does not name.
        self.assertEqual(0, self.report["qrElsewhere"]["explicit"]["images"])

    def test_with_no_write_path_the_code_shows_and_the_third_line_does_not(self):
        # All three verbs are the write path. A projection that invites a
        # stranger to do something the gallery cannot accept is worse than one
        # that only shows the URL (§5.3).
        offline = self.report["qrElsewhere"]["offline"]
        self.assertEqual(1, offline["images"])
        self.assertEqual(
            [
                "scan to open #33",
                "profcarroll.github.io/sketchgen-gallery/e/33/",
            ],
            offline["lines"],
        )

    def test_a_projector_configured_before_this_packet_gets_the_code(self):
        # The stored show map cannot mention a key that did not exist when it
        # was written, so without the migration those rooms come back with the
        # code off and nobody at the keyboard (§5.1).
        migration = self.report["migration"]
        self.assertEqual(
            "kiosk.html?order=newest&every=60&size=native&show=prompt,qr", migration["migrated"]
        )
        self.assertEqual(2, migration["stamped"]["v"])

    def test_the_stamp_stops_the_migration_running_twice(self):
        # Somebody who turned the code off after the migration keeps it off.
        self.assertEqual(
            "kiosk.html?order=newest&every=60&size=native&show=prompt",
            self.report["migration"]["stays"],
        )


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

    def test_it_never_fetches_a_qr_code(self):
        # The code beside each sketch is an <img src> the browser loads and
        # caches, pointing at a file render_index already wrote (qr.md §1.9,
        # §5.4). This file encodes nothing and asks for nothing.
        for call in re.findall(r"fetch\(([^)]*)", self.code):
            self.assertNotIn("qr", call, call)
        self.assertNotIn("encodeURIComponent(entry.url", self.code)
        # And it adds no parameter to anything: the only "?kiosk" in the
        # gallery is in the payload the generator encoded.
        self.assertNotIn("?kiosk", self.code)

    def test_it_is_es5_like_the_rest_of_the_gallery(self):
        # No build step means the file is the file the browser gets.
        self.assertNotIn("=>", self.code)
        self.assertNotIn("`", self.code)
        self.assertIsNone(re.search(r"\b(?:let|const)\s+\w+\s*=", self.code))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class SwipeTests(unittest.TestCase):
    """swipe.js, run for real against the DOM contract (swipe spec §6).

    ``tests/js/swipe.js`` loads the real script into the stub DOM on a page
    built from the ids ``swipe.html`` promises, dispatches pointer events at
    the shield, presses the six courtesy keys and steps a fake clock, then
    prints one JSON report. Everything below reads that report, so a failure
    names the gesture rather than the harness.
    """

    @classmethod
    def setUpClass(cls):
        done = subprocess.run(
            [shutil.which("node"), str(SWIPE_HARNESS)],
            capture_output=True, text=True, timeout=120,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.report = json.loads(done.stdout.strip().splitlines()[-1])

    # ---- the seven orders, the seat and the address bar (§4.2) ----------

    def test_each_order_is_the_order_the_kiosk_s_comparator_would_give(self):
        # The same three entries the kiosk fixture uses, pulled as far apart
        # as three things can be: all six deterministic orders are different
        # permutations, so no order can pass by accident.
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

    def test_at_seats_on_that_id_in_the_current_order(self):
        self.assertEqual(22, self.report["seating"]["seated"])
        # An id this gallery does not have is seat 0 rather than an error: a
        # link to a pruned entry should still open the gallery.
        self.assertEqual(11, self.report["seating"]["missingAt"])

    def test_the_address_bar_follows_every_seat_and_the_order_is_stored(self):
        seating = self.report["seating"]
        self.assertEqual("/swipe.html?order=liked&at=22", seating["addressAfterFirstSeat"])
        self.assertEqual("/swipe.html?order=liked&at=11", seating["addressAfterSecondSeat"])
        self.assertEqual(11, seating["seatAfterSecond"])
        self.assertEqual({"v": 1, "order": "liked"}, seating["storedBlob"])

    def test_a_url_order_beats_a_stored_one_and_a_stored_one_beats_the_default(self):
        seating = self.report["seating"]
        self.assertEqual(11, seating["fromStorage"])   # oldest, from storage
        self.assertEqual(33, seating["urlWins"])       # ?order=newest
        # An order nobody defined is not an order: the stored one stands.
        self.assertEqual(11, seating["nonsense"])

    # ---- the feed: up, down, and a drag that did not reach (§4.4) --------

    def test_a_vertical_drag_past_the_threshold_moves_one_seat_each_way(self):
        feed = self.report["feed"]
        self.assertEqual(33, feed["start"])
        self.assertEqual(22, feed["up"]["entry"])      # 0 to -80 in y
        self.assertEqual(33, feed["down"]["entry"])    # and back
        self.assertEqual(1, feed["up"]["frames"])
        self.assertEqual(1, feed["down"]["frames"])

    def test_a_drag_short_of_the_threshold_snaps_back_and_changes_nothing(self):
        short = self.report["feed"]["short"]
        self.assertEqual(33, short["entry"])
        self.assertEqual(1, short["frames"])
        self.assertEqual("stage snap", short["className"])
        # Snapped back means the stage is where it started, not held at 40 px.
        self.assertNotIn("transform", short["style"])

    def test_one_frame_at_a_time_sandboxed_to_scripts_and_nothing_else(self):
        feed = self.report["feed"]
        self.assertEqual(1, feed["framesAtStart"])
        self.assertEqual("allow-scripts", feed["sandbox"])
        self.assertEqual("./e/33/sketch/", feed["src"])
        # No allow=, no second sandbox token, nothing the entry page's frame
        # does not carry.
        self.assertEqual(["class", "sandbox", "src", "title"], feed["attributes"])

    def test_a_fixed_canvas_is_laid_out_at_its_own_size_and_scaled_to_fit(self):
        # 400x400 on a 390x844 phone: min(390/400, 844/400) is the short side,
        # which is the whole of §1.11 — fit, and only fit.
        fitted = self.report["feed"]["fitted"]
        self.assertEqual("400px", fitted["w"])
        self.assertEqual("400px", fitted["h"])
        self.assertEqual("0.975", fitted["sx"])
        # And a sketch that sized itself to its window leaves the stage to say
        # how big it is: no properties at all.
        self.assertIsNone(self.report["feed"]["windowSized"]["style"])

    def test_the_caption_says_what_a_sketch_has_to_give_a_finger(self):
        # Entry 22 listens and responds to nothing, so it gets the one line
        # about the microphone and not the one about holding.
        caption = self.report["feed"]["caption"]
        self.assertIn("listens to the microphone · open the entry page to let it", caption)
        self.assertNotIn("responds to touch", caption)
        # Entry 33's assertions are responds(click) and responds(audio).
        liked = self.report["liking"]["liked"]["caption"]
        self.assertIn("responds to touch · hold to try", liked)
        self.assertIn("makes sound · hold to hear it", liked)

    # ---- the tap, the hold and the pill (§4.4) ---------------------------

    def test_a_tap_toggles_the_words_and_a_second_one_brings_them_back(self):
        held = self.report["tapAndHold"]
        self.assertNotIn("quiet", held["before"])
        self.assertIn("quiet", held["quietNow"])
        self.assertNotIn("quiet", held["loudAgain"])

    def test_a_hold_hands_the_touch_over_and_is_never_read_as_a_tap(self):
        held = self.report["tapAndHold"]
        self.assertIn("touching", held["touching"]["classes"])
        # The hold timer clears the gesture before the release can read it.
        self.assertNotIn("quiet", held["touching"]["classes"])
        self.assertEqual([12], held["touching"]["buzzed"])
        # And no gesture starts while the sketch has the touch.
        self.assertEqual(33, held["heldEntry"])

    def test_the_pill_gives_the_touch_back(self):
        held = self.report["tapAndHold"]
        self.assertNotIn("touching", held["given"])
        self.assertEqual(22, held["movedAfter"])

    def test_a_tap_on_the_caption_opens_the_words_rather_than_hiding_them(self):
        # Through the shield, not on the caption: the caption sits under it so
        # a swipe that starts on the words is still a swipe (a thumb starts
        # most of them there), and the shield hit-tests the tap instead.
        self.assertEqual("sheet-info", self.report["tapAndHold"]["captionOpens"])

    def test_the_start_card_dismissed_marks_the_body_playing(self):
        # Which is what hides the header bar (body.swipe.playing .bar).
        self.assertIn("playing", self.report["tapAndHold"]["before"])

    def test_a_tap_above_the_caption_toggles_the_words_instead(self):
        self.assertIsNone(self.report["tapAndHold"]["closedAgain"])
        self.assertIn("quiet", self.report["tapAndHold"]["aboveToggles"])

    # ---- a like (§4.6) ---------------------------------------------------

    def test_a_right_swipe_signed_out_asks_and_posts_nothing(self):
        out = self.report["liking"]["signedOut"]
        self.assertEqual("sheet-signin", out["sheet"])
        self.assertEqual("Sign in to like it", out["title"])
        self.assertEqual(0, out["posts"])
        self.assertNotIn("liked", out["classes"])

    def test_a_right_swipe_signed_in_posts_one_like_and_then_unlikes_it(self):
        liking = self.report["liking"]
        self.assertEqual(
            [
                {"entry_id": 33, "on": True},
                {"entry_id": 33, "on": False},
            ],
            [one["body"] for one in liking["posts"]],
        )
        self.assertEqual(
            ["https://write.example.invalid/api/like"] * 2,
            [one["url"] for one in liking["posts"]],
        )
        self.assertIn("liked", liking["liked"]["classes"])
        self.assertEqual("liked", liking["liked"]["toast"])
        self.assertNotIn("liked", liking["unliked"]["classes"])
        self.assertEqual("unliked", liking["unliked"]["toast"])
        # The caption's count moved with it: 2 likes became 3.
        self.assertIn("3 likes", liking["liked"]["caption"])

    def test_a_refused_like_empties_the_heart_drops_the_token_and_asks(self):
        refused = self.report["liking"]["refused"]
        self.assertNotIn("liked", refused["classes"])
        self.assertEqual("sheet-signin", refused["sheet"])
        self.assertIsNone(refused["token"])
        self.assertEqual(1, refused["posts"])

    # ---- a judgment (§4.5) -----------------------------------------------

    def test_a_left_swipe_opens_the_judge_sheet_on_a_pair(self):
        judging = self.report["judging"]
        self.assertEqual("sheet-judge", judging["opened"]["sheet"])
        self.assertEqual(33, judging["a"])
        # B is one of the two offered pairs that contain A.
        self.assertIn(judging["b"], (11, 22))
        # Two tiles, two briefs, and A still playing above the sheet.
        self.assertEqual("One square, slowly changing colour.", judging["briefA"])
        self.assertTrue(judging["briefB"])
        self.assertEqual("./e/33/sketch/", judging["opened"]["src"])
        self.assertEqual(1, judging["opened"]["frames"])
        self.assertIn("quiet", judging["opened"]["classes"])

    def test_nothing_about_b_but_its_brief_is_in_the_document(self):
        # The compare page blinds the visitor until both answers are in, and
        # B's authors are the loudest anchor this sheet could hand them.
        self.assertEqual([], self.report["judging"]["leaked"])

    def test_tapping_b_s_tile_swaps_the_one_iframe_to_b(self):
        swapped = self.report["judging"]["swapped"]
        self.assertEqual(1, swapped["frames"])
        self.assertEqual(
            "./e/%d/sketch/" % self.report["judging"]["b"], swapped["src"]
        )
        self.assertEqual(["A:false", "B:true"], swapped["pressed"])

    def test_each_answer_posts_a_vote_with_the_pair_the_question_and_the_choice(self):
        judging = self.report["judging"]
        self.assertEqual(
            [
                {"entry_a": 33, "entry_b": judging["b"], "question": "brief", "choice": "A"},
                {"entry_a": 33, "entry_b": judging["b"], "question": "look", "choice": "B"},
            ],
            [one["body"] for one in judging["votes"]],
        )
        self.assertEqual("recorded: A", judging["afterOne"]["answered"])

    def test_the_reveal_waits_for_both_answers_and_then_says_what_the_agents_said(self):
        judging = self.report["judging"]
        self.assertTrue(judging["opened"]["revealHidden"])
        self.assertTrue(judging["afterOne"]["revealHidden"])
        self.assertFalse(judging["afterBoth"]["revealHidden"])
        # Stored against the pair in (low, high) order and shown the other way
        # round, so both verdicts come out flipped.
        self.assertEqual(
            [
                "gemma4:e4b · closer to its brief: B",
                "gemma4:e4b · rather look at: A",
            ],
            judging["afterBoth"]["verdicts"],
        )
        self.assertEqual("A is #33 · B is #%d" % judging["b"], judging["afterBoth"]["both"])
        self.assertEqual(
            [
                "https://profcarroll.github.io/sketchgen-gallery/e/33/",
                "https://profcarroll.github.io/sketchgen-gallery/e/%d/" % judging["b"],
            ],
            judging["afterBoth"]["links"],
        )

    def test_judge_another_pair_keeps_a_and_changes_b(self):
        judging = self.report["judging"]
        self.assertEqual("One square, slowly changing colour.", judging["another"]["briefA"])
        self.assertNotEqual(judging["b"], judging["secondB"])
        self.assertIn(judging["secondB"], (11, 22))
        # A fresh pair is a fresh pair of questions.
        self.assertTrue(judging["another"]["revealHidden"])
        self.assertEqual("", judging["another"]["answered"])
        self.assertEqual("./e/33/sketch/", judging["another"]["src"])

    def test_keep_swiping_closes_the_sheet_and_frames_a_again(self):
        back = self.report["judging"]["back"]
        self.assertIsNone(back["sheet"])
        self.assertNotIn("quiet", back["classes"])
        self.assertEqual(1, back["frames"])
        self.assertEqual("./e/33/sketch/", back["src"])
        self.assertEqual(33, back["entry"])

    def test_signed_out_a_vote_is_noted_on_the_sheet_and_the_offer_is_a_tap(self):
        judging = self.report["judging"]
        self.assertEqual("noted here only: tie", judging["signedOut"]["answered"])
        # The judge sheet stays open: the sign-in sheet is offered, not forced.
        self.assertEqual("sheet-judge", judging["signedOut"]["sheet"])
        self.assertIn("noted here only", judging["signedOut"]["status"])
        self.assertEqual("sheet-signin", judging["offered"]["sheet"])
        self.assertEqual("Sign in to record it", judging["offered"]["title"])

    # ---- a view (§4.7) ----------------------------------------------------

    def test_ten_seconds_on_one_seat_is_one_view_and_not_a_frame_before(self):
        viewing = self.report["viewing"]
        self.assertEqual(0, viewing["atNine"])
        self.assertEqual(1, len(viewing["atTen"]))
        self.assertEqual(
            "https://write.example.invalid/api/view", viewing["atTen"][0]["url"]
        )
        # The entry page's body, not the kiosk's: no source.
        self.assertEqual({"entry_id": 33}, viewing["atTen"][0]["body"])

    def test_six_hundred_more_frames_on_the_same_seat_post_nothing_more(self):
        # The flag is set before the request goes out, so no frame can post a
        # second one while the first is still in flight.
        self.assertEqual(1, self.report["viewing"]["afterSixHundredFrames"])

    def test_a_seat_left_in_under_ten_seconds_costs_nothing(self):
        skipped = self.report["viewing"]["afterASkip"]
        self.assertEqual(0, skipped["posts"])
        self.assertEqual(22, skipped["entry"])

    def test_a_hidden_tab_counts_nothing_until_somebody_looks_again(self):
        viewing = self.report["viewing"]
        self.assertEqual(0, viewing["whileHidden"])
        self.assertEqual(1, viewing["afterComingBack"])

    def test_time_on_b_counts_for_b_and_not_for_the_seat_it_was_swapped_onto(self):
        viewing = self.report["viewing"]
        # Nine seconds on A and nine on B is no view at all; the swap cleared
        # the dwell, as a seat does.
        self.assertEqual(0, viewing["beforeBEarnsIt"])
        self.assertEqual(1, len(viewing["bEarnedIt"]))
        self.assertEqual(
            {"entry_id": viewing["bOnTheStage"]}, viewing["bEarnedIt"][0]["body"]
        )
        self.assertNotEqual(33, viewing["bOnTheStage"])

    # ---- no write path (§4.1) ---------------------------------------------

    def test_with_no_write_path_nothing_is_asked_of_the_write_path_at_all(self):
        # The whole run: start, a right swipe, ten seconds, a left swipe and a
        # vote. Not one /counts, /me, /view, /like or /vote among them — the
        # four files the gallery itself serves, and nothing else. (Which of
        # the two partners the judge sheet drew is a coin toss, so its
        # meta.json is matched by shape.)
        offline = self.report["offline"]
        self.assertEqual(
            ["./swipe.json", "./config.json", "./pairs.json", "./e/33/meta.json"],
            offline["asked"][:4],
        )
        self.assertEqual(5, len(offline["asked"]))
        self.assertRegex(offline["asked"][4], r"^\./e/(11|22)/meta\.json$")
        for url in offline["asked"]:
            self.assertTrue(url.startswith("./"), url)
        self.assertEqual(0, offline["posts"])

    def test_with_no_write_path_the_counts_are_em_dashes_and_the_heart_is_gone(self):
        start = self.report["offline"]["start"]
        self.assertIn("— views", start["caption"])
        self.assertIn("— likes", start["caption"])
        self.assertTrue(start["heartHidden"])

    def test_with_no_write_path_a_like_says_so_and_a_vote_is_noted_here_only(self):
        offline = self.report["offline"]
        self.assertEqual(
            "the gallery write path is not deployed yet", offline["right"]["toast"]
        )
        # It does not ask for a sign-in it cannot use.
        self.assertIsNone(offline["right"]["sheet"])
        self.assertEqual("sheet-judge", offline["judge"]["sheet"])
        self.assertEqual("noted here only: A", offline["noted"])

    # ---- the words sheet (§4.5) -------------------------------------------

    def test_the_words_sheet_prints_meta_json_and_asks_for_it_once(self):
        shown = self.report["words"]["shown"]
        self.assertEqual("sheet-info", shown["sheet"])
        self.assertEqual("#11 · A field of slow lines.", shown["title"])
        self.assertEqual(
            "Revised once, latest: let the lines thin as they near the edge.",
            shown["sub"],
        )
        self.assertEqual("Lines drawn across the canvas, thinning at the edges.", shown["brief"])
        self.assertEqual("This sketch draws a field of lines.", shown["statement"])
        self.assertEqual("qwen3-coder:30b-a3b-q4_K_M", shown["by"])
        # Cached per id for the page's life: closed and reopened is one request.
        self.assertEqual(1, self.report["words"]["metaCalls"])

    def test_the_words_sheet_says_the_judgment_per_population(self):
        shown = self.report["words"]["shown"]
        self.assertEqual("2.10 over 2 pairs", shown["human"])
        self.assertEqual("looks good, on brief", shown["humanQuad"])
        self.assertEqual("0.40 over 2 pairs", shown["agent"])
        # The same quadrant words gallery.py draws the compass square with.
        self.assertEqual("neither", shown["agentQuad"])

    def test_the_words_sheet_prints_the_provenance_and_two_links_out(self):
        shown = self.report["words"]["shown"]
        self.assertEqual(
            [
                "prompted by", "planned by", "written by", "rules", "generation",
                "attempts", "created", "tokens", "written in", "canvas", "licence",
            ],
            list(shown["fields"]),
        )
        self.assertEqual(
            "2, revised from #7 after a critique by gemma4:e4b",
            shown["fields"]["generation"],
        )
        self.assertEqual("1,169 prompt + 561 completion", shown["fields"]["tokens"])
        self.assertEqual("800×600", shown["fields"]["canvas"])
        self.assertEqual(
            "https://profcarroll.github.io/sketchgen-gallery/e/11/", shown["entryLink"]
        )
        self.assertEqual(
            "https://profcarroll.github.io/sketchgen-gallery/e/11/sketch/sketch.js",
            shown["sourceLink"],
        )

    def test_a_sheet_closes_on_its_cross_on_escape_and_on_the_dim(self):
        words = self.report["words"]
        self.assertIsNone(words["closed"])
        self.assertIsNone(words["afterEscape"])
        # The dim goes with it, or the next gesture would land on a layer
        # that is still there and invisible.
        self.assertEqual("dim", words["dimAfterEscape"])
        self.assertIsNone(words["afterDim"])

    def test_a_sheet_dragged_down_past_ninety_pixels_closes_and_a_shorter_one_does_not(self):
        words = self.report["words"]
        # It follows the finger while the drag is live…
        self.assertIn("translateY(60px)", words["followed"])
        self.assertIsNone(words["afterDragDown"])
        # …and springs back when the finger did not go far enough.
        self.assertEqual("sheet-info", words["afterShortDrag"]["sheet"])
        self.assertNotIn("transform", words["afterShortDrag"]["style"])

    # ---- this page (§4.5) --------------------------------------------------

    def test_the_settings_sheet_names_the_seat_the_orders_and_the_session(self):
        open_ = self.report["settings"]["open"]
        self.assertEqual("sheet-settings", open_["sheet"])
        self.assertEqual("Sketch 1 of 3", open_["place"])
        self.assertEqual(
            [
                "newest:true", "oldest:false", "random:false", "liked:false",
                "reviewed:false", "controversial:false", "consensus:false",
            ],
            open_["rows"],
        )
        self.assertIn("signed in: profcarroll", open_["session"])
        self.assertEqual("swipe.html?order=newest&at=33", open_["launch"])
        self.assertEqual("1 of 3 · newest", open_["statusLine"])

    def test_changing_the_order_keeps_the_sketch_and_re_seats_it(self):
        settings = self.report["settings"]
        reordered = settings["reordered"]
        self.assertEqual(settings["before"], reordered["entry"])
        self.assertEqual(1, reordered["frames"])
        # 33 is second in consensus, and every label says so.
        self.assertEqual("2 of 3 · consensus", reordered["statusLine"])
        self.assertEqual("swipe.html?order=consensus&at=33", reordered["launch"])
        self.assertEqual("/swipe.html?order=consensus&at=33", reordered["address"])
        self.assertEqual({"v": 1, "order": "consensus"}, reordered["stored"])

    def test_signing_out_drops_the_token_and_says_so(self):
        out = self.report["settings"]["out"]
        self.assertIn("not signed in", out["session"])
        self.assertIsNone(out["token"])
        self.assertEqual("signed out", out["toast"])

    # ---- signing in, and coming back (§5) ----------------------------------

    def test_signing_in_leaves_the_note_the_front_page_reads_and_goes_to_login(self):
        signin = self.report["signin"]
        self.assertEqual("sheet-signin", signin["asked"])
        # Exactly the shape gallery.js's returnFromSignIn will accept.
        self.assertEqual("swipe.html?order=newest&at=22", signin["note"])
        self.assertEqual("https://write.example.invalid/api/login", signin["went"])

    def test_declining_a_sign_in_does_nothing_at_all(self):
        after = self.report["signin"]["after"]
        self.assertIsNone(after["sheet"])
        self.assertNotIn("liked", after["classes"])
        self.assertEqual(0, after["posts"])

    def test_a_token_in_the_fragment_is_claimed_and_stripped_here_too(self):
        signin = self.report["signin"]
        self.assertEqual("tok.9.sig", signin["claimedToken"])
        self.assertEqual("/swipe.html?order=newest", signin["claimedAddress"])

    # ---- the courtesy keys (§1.5) ------------------------------------------

    def test_the_six_keys_do_the_six_things_a_thumb_does(self):
        keys = self.report["keys"]
        self.assertEqual(33, keys["start"])
        self.assertEqual(22, keys["up"])
        self.assertEqual(33, keys["down"])
        self.assertIn("quiet", keys["quietNow"])
        self.assertNotIn("quiet", keys["loud"])
        self.assertIn("liked", keys["liked"])
        self.assertEqual("sheet-judge", keys["judging"])
        self.assertIsNone(keys["closed"])

    def test_a_modifier_chord_is_a_browser_shortcut_and_stays_one(self):
        self.assertTrue(self.report["keys"]["afterChord"])

    # ---- the start card -----------------------------------------------------

    def test_the_card_holds_the_tap_until_there_is_something_to_swipe(self):
        starting = self.report["starting"]
        self.assertTrue(starting["before"]["disabled"])
        self.assertEqual("loading…", starting["before"]["label"])
        self.assertFalse(starting["loaded"]["disabled"])
        self.assertEqual("Start", starting["loaded"]["label"])

    def test_a_manifest_that_never_arrives_says_so_and_nothing_starts(self):
        failed = self.report["starting"]["failed"]
        self.assertEqual("Could not load the gallery's list of sketches.", failed["note"])
        self.assertTrue(failed["welcomeUp"])
        self.assertEqual(0, failed["frames"])
        self.assertNotIn("playing", failed["classes"])
        # And the gestures are inert rather than half alive.
        self.assertEqual(0, self.report["starting"]["stillNothing"])

    # ---- every write the page ever makes (§4.1, §4.8) -----------------------

    def test_the_only_posts_over_the_whole_run_are_view_like_and_vote(self):
        posts = self.report["everyPost"]
        self.assertTrue(posts, "swipe.js posted nothing at all")
        self.assertEqual({"POST"}, {one["method"] for one in posts})
        self.assertEqual(
            {
                "https://write.example.invalid/api/view",
                "https://write.example.invalid/api/like",
                "https://write.example.invalid/api/vote",
            },
            {one["url"] for one in posts},
        )

    def test_every_post_carries_the_bearer_token_when_one_is_stored(self):
        # The one that does not is the vote cast while signed out, which is
        # the case the Worker answers with a 401 and the sheet notes locally.
        for one in self.report["everyPost"]:
            self.assertEqual(
                ["body", "credentials", "headers", "method"], one["keys"], one["url"]
            )
        bare = [one["url"] for one in self.report["everyPost"] if not one["hasAuth"]]
        # Exactly one, and it is the vote: a token-bearing vote that dropped
        # the header would show up here as a second entry.
        self.assertEqual(1, len(bare), bare)
        self.assertTrue(bare[0].endswith("/vote"), bare)

    def test_no_request_carries_credentials_other_than_include(self):
        self.assertTrue(self.report["everyCredentials"])
        self.assertEqual({"include"}, set(self.report["everyCredentials"]))


class SwipeScriptTextTests(unittest.TestCase):
    """What swipe.js must and must not contain (swipe spec §4.8). No node."""

    @classmethod
    def setUpClass(cls):
        cls.code = _without_comments(SWIPE_SCRIPT.read_text(encoding="utf-8"))

    def test_it_speaks_to_the_write_path_at_seven_paths_and_no_other(self):
        # Every request to base() is a literal `base() + "/..."`, so the set of
        # those literals is the whole conversation with the Worker: the six
        # reads and writes of spec §4.1, and /logout on sign-out, which is
        # gallery.js's own sign-out and clears the Worker's cookie beside the
        # token this page drops.
        paths = set(re.findall(r'base\(\) \+ "(/[a-z]+)', self.code))
        self.assertEqual(
            {"/counts", "/me", "/logout", "/login", "/view", "/like", "/vote"}, paths
        )

    def test_it_writes_three_things_and_only_those_three(self):
        # Every method other than GET is a write to the write path, so the
        # count of them is the count of writes: a view, a like and a vote.
        methods = re.findall(r"method\s*:\s*\"([A-Z]+)\"", self.code)
        self.assertEqual(["POST", "POST", "POST"], methods)
        # And they go where they say they go. The fetch and its options are
        # one expression in the source, so the endpoint and the method that
        # reach the network together are read together here.
        posts = re.findall(r"fetch\(([^\n]*?),\s*\{\s*\n\s*method", self.code)
        self.assertEqual(
            sorted(['base() + "/view"', 'base() + "/like"', 'base() + "/vote"']),
            sorted(one.strip() for one in posts),
        )

    def test_it_never_evaluates_a_sketch_or_talks_to_one(self):
        self.assertNotIn("new Function", self.code)
        self.assertNotIn("eval(", self.code)
        # allow-scripts without allow-same-origin is opaque by design.
        self.assertNotIn("postMessage", self.code)

    def test_it_never_reads_a_cookie(self):
        # The session is a bearer token in this origin's storage, and the
        # reason it is not a cookie is in the file header.
        self.assertNotIn("document.cookie", self.code)

    def test_the_only_storage_keys_it_touches_are_the_three_of_the_spec(self):
        calls = re.findall(
            r"localStorage\.(?:getItem|setItem|removeItem)\(\s*([^,)]+)", self.code
        )
        self.assertTrue(calls, "swipe.js no longer touches localStorage at all")
        self.assertEqual({"SETTINGS_KEY", "STORAGE_KEY", "RETURN_KEY"},
                         {call.strip() for call in calls})
        names = dict(re.findall(r'var ([A-Z_]+) = "(sketchgen[-_][a-z-]+)";', self.code))
        self.assertEqual(
            {
                "SETTINGS_KEY": "sketchgen-swipe",
                "STORAGE_KEY": "sketchgen_session",
                "RETURN_KEY": "sketchgen-return",
            },
            names,
        )
        # And no fourth key anywhere in the file, named or spelled out.
        self.assertEqual(
            {"sketchgen-swipe", "sketchgen_session", "sketchgen-return"},
            set(re.findall(r'"(sketchgen[-_][a-z-]+)"', self.code)),
        )

    def test_it_writes_the_shared_session_key_only_in_write_token(self):
        # It reads the gallery's session and hands it back; minting one is
        # gallery.js's job on the page /callback actually returns to.
        writes = re.findall(r"localStorage\.setItem\(\s*STORAGE_KEY", self.code)
        self.assertEqual(1, len(writes))
        body = re.search(
            r"function writeToken\(token\) \{(.*?)\n  \}", self.code, re.S
        )
        self.assertIsNotNone(body, "swipe.js no longer has writeToken")
        self.assertIn("localStorage.setItem(STORAGE_KEY", body.group(1))

    def test_the_return_note_is_only_ever_written(self):
        # gallery.js is the only reader (§5); a page that read its own note
        # back could be steered by anything that could write storage.
        self.assertNotIn("getItem(RETURN_KEY", self.code)
        self.assertNotIn("removeItem(RETURN_KEY", self.code)
        self.assertIn("setItem(RETURN_KEY", self.code)

    def test_it_parses_in_a_browser_without_lookbehind(self):
        # The same line gallery.js and kiosk.js hold: a lookbehind is a syntax
        # error in a browser too old for it, and a syntax error here is a
        # black screen with no way out of it.
        self.assertNotIn("(?<", self.code)

    def test_it_is_es5_like_the_rest_of_the_gallery(self):
        # No build step means the file is the file the browser gets.
        self.assertNotIn("=>", self.code)
        self.assertNotIn("`", self.code)
        self.assertIsNone(re.search(r"\b(?:let|const)\s+\w+\s*=", self.code))


#: The swipe spec §5 harness: gallery.js on the gallery's front page, which is
#: where <write_path>/callback lands everybody, with a fragment and a note in
#: storage. The run reports what the session key holds afterwards, whether the
#: note survived, and where the page meant to send the visitor.
RETURNER = """
"use strict";

const fs = require("fs");
const vm = require("vm");
const { makeWindow } = require(process.argv[2]);

const SCRIPT = process.argv[3];
const CASES = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));

const RETURN_KEY = "sketchgen-return";
const SESSION_KEY = "sketchgen_session";

// The front page is bare here on purpose: nothing §5 does needs a card, a
// filter or a form, and a page with none of them proves it asks for none.
// Offline, so config.json answers nothing and no request goes anywhere.
function run(one) {
  const window = makeWindow();
  const document = window.document;
  window.location.pathname = "/";
  window.location.search = "";
  window.location.hash = one.hash || "";
  if (one.note !== undefined) { window.localStorage.setItem(RETURN_KEY, one.note); }
  window.SKETCHGEN_ROOT = "./";
  window.fetch = function () { return Promise.reject(new Error("offline")); };

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

  return new Promise(function (resolve) {
    // A tick, so that a redirect the script only reached after loadConfig
    // would still be seen. It does not: §5 runs in ready(), before any fetch.
    setTimeout(function () {
      resolve({
        token: window.localStorage.getItem(SESSION_KEY),
        note: window.localStorage.getItem(RETURN_KEY),
        replaced: window.location.lastReplaced,
        address: window.history.lastUrl
      });
    }, 0);
  });
}

(async function () {
  const out = [];
  for (const one of CASES) { out.push(await run(one)); }
  console.log(JSON.stringify(out));
})();
"""

#: The note swipe.js writes, and the three arrivals §6 names: the one that
#: redirects, the one with no fragment behind it, and the ones whose note is
#: not a swipe page's address.
RETURN_NOTE = "swipe.html?order=newest&at=5"

#: The notes an entry page writes (qr.md §6.3): the sketch a scanner signed in
#: from, and the same one with the greeting they arrived with still on it.
ENTRY_NOTE = "e/93/"
ENTRY_KIOSK_NOTE = "e/93/?kiosk"

#: Every note the reader must act on. Everything else in RETURNS is a refusal.
HONOURED = (RETURN_NOTE, ENTRY_NOTE, ENTRY_KIOSK_NOTE)

RETURNS = [
    # Signed in and on the way back.
    {"hash": "#session=tok", "note": RETURN_NOTE},
    # A note left over from some earlier visit, and nobody signing in.
    {"note": RETURN_NOTE},
    # The same trip, begun on a sketch somebody scanned off a wall.
    {"hash": "#session=tok", "note": ENTRY_NOTE},
    {"hash": "#session=tok", "note": ENTRY_KIOSK_NOTE},
    # Notes that are not a page of this gallery. Each one is a way storage
    # could try to steer a visitor if the pattern were any looser.
    {"hash": "#session=tok", "note": "https://elsewhere.example/"},
    {"hash": "#session=tok", "note": "//elsewhere.example/"},
    {"hash": "#session=tok", "note": "javascript:alert(1)"},
    {"hash": "#session=tok", "note": "../../swipe.html"},
    {"hash": "#session=tok", "note": "swipe.html?at=5#session=stolen"},
    {"hash": "#session=tok", "note": "kiosk.html"},
    # And the same six shapes again wearing an entry's clothes, because the
    # directory the pattern now admits is the half of it that is new.
    {"hash": "#session=tok", "note": "https://elsewhere.example/e/93/"},
    {"hash": "#session=tok", "note": "//elsewhere.example/e/93/"},
    {"hash": "#session=tok", "note": "javascript:alert(1)//e/93/"},
    {"hash": "#session=tok", "note": "../e/93/"},
    {"hash": "#session=tok", "note": "e/93/../../"},
    {"hash": "#session=tok", "note": "e\\93\\"},
    {"hash": "#session=tok", "note": "e/93/#session=stolen"},
    # A directory, not a file under one: the pattern ends where the id's
    # slash does, so nothing chooses which file in the entry to open.
    {"hash": "#session=tok", "note": "e/93/meta.json"},
    {"hash": "#session=tok", "note": "e/93"},
]


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class ReturnFromSignInTests(unittest.TestCase):
    """Coming back to the page a sign-in started on (swipe.md §5, §6; qr.md §6.3).

    The Worker's /callback returns to the gallery's front page and only there,
    so the page the visitor left leaves a note in storage before it goes and
    gallery.js reads it once on the way back. The three rules — consumed once,
    only in the load that claimed a token, only to a page of this gallery —
    are each one case here.
    """

    @classmethod
    def setUpClass(cls):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-return-"))
        cls.tmp = tmp
        harness = tmp / "returner.js"
        harness.write_text(RETURNER, encoding="utf-8")
        cases = tmp / "returns.json"
        cases.write_text(json.dumps(RETURNS), encoding="utf-8")
        done = subprocess.run(
            [shutil.which("node"), str(harness), str(DOM), str(SCRIPT), str(cases)],
            capture_output=True, text=True, timeout=60,
        )
        if done.returncode != 0:
            raise AssertionError(done.stdout + done.stderr)
        cls.runs = json.loads(done.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_for(self, note, fragment=""):
        for case, result in zip(RETURNS, self.runs):
            if case["note"] == note and case.get("hash", "") == fragment:
                return result
        raise AssertionError(f"no run for {note!r}")

    def test_a_claimed_token_sends_the_visitor_back_and_eats_the_note(self):
        run = self.run_for(RETURN_NOTE, "#session=tok")
        self.assertEqual("tok", run["token"])
        # ROOT + the note, and ROOT on the front page is "./".
        self.assertEqual("./" + RETURN_NOTE, run["replaced"])
        # Consumed once: a second load of this page finds nothing to act on.
        self.assertIsNone(run["note"])
        # And the token still leaves the address bar on the way past.
        self.assertEqual("/", run["address"])

    def test_a_note_with_no_fragment_behind_it_redirects_nobody(self):
        # The rule that matters most: a stale note must never take somebody
        # who typed the front page's address somewhere they did not ask for.
        run = self.run_for(RETURN_NOTE)
        self.assertIsNone(run["replaced"])
        self.assertIsNone(run["token"])
        # Not read, so not consumed: the next real sign-in still honours it.
        self.assertEqual(RETURN_NOTE, run["note"])

    def test_a_scanner_comes_back_to_the_sketch_and_not_the_index(self):
        # The bug this fixes: three sign-in links on e/93/, all of them
        # returning somebody to the front page of a gallery they reached by
        # pointing a phone at a wall (qr.md §6.3).
        run = self.run_for(ENTRY_NOTE, "#session=tok")
        self.assertEqual("tok", run["token"])
        self.assertEqual("./" + ENTRY_NOTE, run["replaced"])
        self.assertIsNone(run["note"])

    def test_the_greeting_they_arrived_with_survives_the_sign_in(self):
        # ?kiosk rides on the note, so the strip is back up when they land and
        # the page reads the way it did before they left.
        run = self.run_for(ENTRY_KIOSK_NOTE, "#session=tok")
        self.assertEqual("./" + ENTRY_KIOSK_NOTE, run["replaced"])
        self.assertIsNone(run["note"])

    def test_a_note_that_is_not_a_page_of_this_gallery_is_dropped(self):
        for case in RETURNS:
            note = case["note"]
            if note in HONOURED:
                continue
            with self.subTest(note=note):
                run = self.run_for(note, "#session=tok")
                self.assertIsNone(run["replaced"], note)
                # Removed all the same: a note this file will not act on is
                # not a note worth keeping.
                self.assertIsNone(run["note"], note)
                # The sign-in itself still worked.
                self.assertEqual("tok", run["token"])

if __name__ == "__main__":
    unittest.main()
