"""The gate, run over its own fixtures — when there is a browser to run it in.

`gate/accept.sh` is the real harness and has been since packet 1.2: it runs
`sketch_gate.py` twice per fixture, once plain and once with that fixture's
assertions, and compares both against `gate/fixtures/expected.json`. This file
does not reimplement any of that. It runs `accept.sh` and asserts it exits 0,
so that moving the gate into the repo (packet 0, spec §3.2) cannot quietly break
it and nobody notices until a job fails on the node.

It needs three things the ordinary developer machine does not have: the
Playwright package, the Chromium it downloads, and the network (the fixtures
load p5.js from a CDN). Each missing one is a **skip**, not a failure — a laptop
with no venv must still be able to run `python3 -m unittest discover -s tests`
and get a clean result.

A skip here is not a pass. After changing anything under `gate/`, run the
harness where it can actually run:

    ssh sld-cloud '. ~/sketchgen/.venv/bin/activate && ~/sketchgen/app/gate/accept.sh'
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = REPO_ROOT / "gate"
GATE = GATE_DIR / "sketch_gate.py"
ACCEPT = GATE_DIR / "accept.sh"
FIXTURES = GATE_DIR / "fixtures"

sys.path.insert(0, str(REPO_ROOT))
from sketchgen import executor, ghostshim  # noqa: E402

#: sha256 of the gate in this repo. Three copies — here, the node, the
#: operator's course repo — one hash: that is the whole claim spec §3.2 makes,
#: and it stops a well-meaning reformat of the referee from landing without
#: somebody deciding to change this line.
#:
#: Changed 2026-09-16 by ResourceLog (entry 429): the gate now records every
#: resource the sketch asked for from outside SKETCH_ORIGIN and did not get, and
#: reports them as notes and under `resources`. It adds no check and fails no
#: run — reaching outside the sketch stays allowed. It exists because a failed
#: image is not a page error, so `console_clean` stayed true while the canvas
#: stayed blank, and the only evidence the executor got was three assertions
#: reading zero pixels changed. The previous value, which is the one the node
#: and the course repo carry until this is deployed, was
#: 2cd5f5156f3669ee558f051d69939655a4a7bc82707b1469ffcb48cb54d1a85e.
#:
#: Changed 2026-09-15 by the frame budget (job 166, job 270): the gate now times
#: its own idle window and fails `frame_budget` above --frame-budget-ms, or
#: above the --budget-s wall ceiling.
#:
#: Changed 2026-09-21 by the ghost window (entry 1103, auto-mouse.md §5): after
#: every check and every assertion has been decided, the gate plays a pointer
#: script through page.mouse and writes ghost.png beside strip.png. It adds no
#: check and fails no run — strip.png and gate.png are byte-for-byte what they
#: were, the assertions are evaluated before it opens, and console_clean is
#: read at the moment it opens. The previous value, which is the one the node
#: and the course repo carry until this is deployed, was
#: 88bedeb8b32eadb5522984e3ef481375e904b94a9affdd2f0a76e38ccf436d99.
GATE_SHA256 = "f64ac446bac40ebce61bfc2811684087b23c824f70220de1dd373b56928bfcbc"

#: How long the eight fixtures are allowed to take together. On the node a
#: single gate run is about four seconds and the harness does fifteen of them —
#: except bad-frame-budget, which is a third of a second a frame by design and
#: costs tens of seconds before the budget stops it. That fixture is why this
#: number is 900 and not 600.
ACCEPT_TIMEOUT_S = 900


def _have_playwright() -> bool:
    """True when the package imports, without importing it."""
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, ValueError):
        return False


class GateFilesTests(unittest.TestCase):
    """What must be in the repo, checked everywhere, browser or no browser."""

    def test_the_gate_and_its_harness_are_tracked(self):
        for path in (GATE, ACCEPT, FIXTURES / "expected.json", GATE_DIR / "README.md"):
            self.assertTrue(path.is_file(), f"missing from the repo: {path}")

    def test_the_gate_is_executable_and_unmodified(self):
        self.assertTrue(os.access(GATE, os.X_OK), f"{GATE} is not executable")
        self.assertTrue(os.access(ACCEPT, os.X_OK), f"{ACCEPT} is not executable")
        digest = hashlib.sha256(GATE.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            GATE_SHA256,
            "sketch_gate.py differs from the copy on the node and in the course "
            "repo; if that is deliberate, change GATE_SHA256 here in the same "
            "commit and say why",
        )

    def test_every_fixture_has_an_expectation(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        directories = sorted(
            p.name for p in FIXTURES.iterdir()
            if p.is_dir() and (p / "index.html").is_file()
        )
        self.assertTrue(directories, "no fixture directories were copied")
        for name in directories:
            self.assertIn(name, expected, f"fixture {name} has no entry in expected.json")
            self.assertIn("exit", expected[name])
            self.assertIn("checks", expected[name])
            self.assertTrue(
                (FIXTURES / name / "sketch.js").is_file(),
                f"fixture {name} has no sketch.js",
            )


class AcceptHarnessTests(unittest.TestCase):
    """The harness itself, which only runs where Playwright does."""

    @classmethod
    def setUpClass(cls):
        if not _have_playwright():
            raise unittest.SkipTest(
                "playwright is not importable — build the venv from "
                "requirements.txt and run `playwright install chromium` to run "
                "the gate here; gate/accept.sh on the node is the real check"
            )

    def test_accept_passes_every_fixture(self):
        with tempfile.TemporaryDirectory() as artefacts:
            try:
                result = subprocess.run(
                    [str(ACCEPT), str(FIXTURES), artefacts],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=ACCEPT_TIMEOUT_S,
                    cwd=str(REPO_ROOT),
                )
            except subprocess.TimeoutExpired:
                self.fail(
                    f"gate/accept.sh did not finish within {ACCEPT_TIMEOUT_S}s"
                )
        output = result.stdout + result.stderr
        # Exit 3 is the gate's refusal, and the only refusals reachable from
        # here are environmental: no browser downloaded yet. Skip rather than
        # fail — the same rule as a missing package.
        if result.returncode == 3 or "Executable doesn't exist" in output:
            raise unittest.SkipTest(
                "the gate refused for want of a browser — run "
                "`playwright install chromium`:\n" + output[-800:]
            )
        self.assertEqual(
            result.returncode,
            0,
            "gate/accept.sh reported a mismatch:\n" + output[-4000:],
        )
        self.assertIn("0 mismatch(es)", output)


def _gate_module():
    """Import sketch_gate.py by path. It is a script, not a package member."""
    spec = importlib.util.spec_from_file_location("sketch_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FrameBudgetTests(unittest.TestCase):
    """The budget's arithmetic and its sentences, with no browser in sight.

    `accept.sh` is what proves the budget fails a real sketch; these are the
    parts that can be checked on a laptop, and the sentence is one of them —
    the worker feeds it to REPAIR verbatim, so what it says is the interface.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def budget(self, *, ms=100.0, seconds=90.0, age=0.0):
        b = self.gate.Budget(ms, seconds, time.time() - age)
        b.counting = True
        return b

    def test_frame_budget_is_failable(self):
        self.assertIn("frame_budget", self.gate.FAILABLE_CHECKS)

    def test_the_defaults_are_the_calibrated_ones(self):
        self.assertEqual(self.gate.DEFAULT_FRAME_BUDGET_MS, 100.0)
        self.assertEqual(self.gate.DEFAULT_BUDGET_S, 90.0)

    def test_an_ordinary_sketch_passes_the_whole_window(self):
        b = self.budget()
        for frames in (42, 42, 36):
            b.record(frames, frames * 0.012)
        passed, note = b.verdict()
        self.assertTrue(passed)
        self.assertEqual(b.ms_per_frame, 12.0)
        self.assertIn("120-frame idle window", note)

    def test_a_sketch_just_over_the_budget_finishes_the_window_and_fails(self):
        # 150 ms a frame is over the budget but under twice it, so the run is
        # not cut short: the check is about the mean over the window.
        b = self.budget()
        for frames in (42, 42, 36):
            b.record(frames, frames * 0.150)
        passed, note = b.verdict()
        self.assertFalse(passed)
        self.assertIn("150 ms per frame over the 120-frame idle window", note)
        self.assertIn("budget 100 ms", note)

    def test_job_166_attempt_3_trips_early_and_says_so(self):
        b = self.budget()
        with self.assertRaises(self.gate.BudgetExceeded) as caught:
            b.record(30, 30 * 1.093)
        sentence = str(caught.exception)
        self.assertTrue(sentence.startswith("frame_budget: 1093 ms per frame"))
        self.assertIn("of the 120-frame idle window", sentence)
        self.assertIn("budget 100 ms", sentence)
        self.assertIn("batch points and lines into one shape", sentence)

    def test_the_wall_ceiling_stops_a_run_that_is_cheap_per_frame(self):
        # 13 ms a frame would pass forever; what it cannot do is keep running
        # past the ceiling, which is what a 284 s pass looked like.
        b = self.budget(age=91.0)
        with self.assertRaises(self.gate.BudgetExceeded) as caught:
            b.record(30, 0.4)
        sentence = str(caught.exception)
        self.assertIn("90 s wall ceiling", sentence)
        self.assertIn("budget 100 ms", sentence)

    def test_the_deadline_applies_outside_the_counted_window(self):
        b = self.budget(age=91.0)
        b.counting = False
        with self.assertRaises(self.gate.BudgetExceeded):
            b.record(30, 0.4)

    def test_nothing_stepped_is_not_a_verdict(self):
        b = self.budget()
        self.assertIsNone(b.ms_per_frame)
        self.assertEqual(b.verdict(), (None, None))

    def test_the_fixture_that_must_fail_the_budget_is_in_the_repo(self):
        fixture = FIXTURES / "bad-frame-budget"
        self.assertTrue((fixture / "sketch.js").is_file())
        self.assertTrue((fixture / "index.html").is_file())
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(expected["bad-frame-budget"]["exit"], 1)
        self.assertIs(expected["bad-frame-budget"]["checks"]["frame_budget"], False)

    def test_every_other_fixture_expects_to_clear_the_budget(self):
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        for name, want in expected.items():
            if name in ("assertions_expected", "bad-frame-budget"):
                continue
            self.assertIs(
                want["checks"].get("frame_budget"), True,
                f"{name} should clear the frame budget",
            )


#: Scripts both validators are run over, and what each one is. The gate's copy
#: of the rules and `executor.validate_ghost` have to agree on every one of
#: them: the executor is what lets a script onto disk and the gate is what
#: plays it, so a gate that refused what the executor accepted would hand the
#: entry the built-in instead — which looks exactly like a working entry.
GHOST_SAMPLES = [
    ("one move", '[{"t": 0, "type": "move", "x": 0, "y": 0}]'),
    ("a click and a drag",
     (FIXTURES / "ghost-echo" / "ghost.json").read_text(encoding="utf-8")),
    ("out of order",
     '[{"t": 9, "type": "up", "x": 1, "y": 1},'
     ' {"t": 1, "type": "down", "x": 0, "y": 0}]'),
    ("not JSON", "the pointer goes left a bit"),
    ("not a list", '{"t": 0, "type": "move", "x": 0, "y": 0}'),
    ("empty", "[]"),
    ("too many", json.dumps([{"t": 1, "type": "move", "x": 0.5, "y": 0.5}] * 65)),
    ("not an object", '["move"]'),
    ("a fifth key",
     '[{"t": 1, "type": "move", "x": 0.5, "y": 0.5, "button": 0}]'),
    ("a missing key", '[{"t": 1, "type": "move", "x": 0.5}]'),
    ("t as a word", '[{"t": "soon", "type": "move", "x": 0.5, "y": 0.5}]'),
    ("t past the cap", '[{"t": 8001, "type": "move", "x": 0.5, "y": 0.5}]'),
    ("t negative", '[{"t": -1, "type": "move", "x": 0.5, "y": 0.5}]'),
    ("a type nobody plays",
     '[{"t": 1, "type": "wheel", "x": 0.5, "y": 0.5}]'),
    ("x off the canvas", '[{"t": 1, "type": "move", "x": 4, "y": 0.5}]'),
    ("y as true", '[{"t": 1, "type": "move", "x": 0.5, "y": true}]'),
]


class GhostWindowTests(unittest.TestCase):
    """The ghost window's pure-Python half (auto-mouse.md §5, 2026-09-21).

    Everything here is a thing two copies could disagree about. The gate is a
    standalone script that imports nothing from the package — it has its own
    copy on the node and a third in the course repo — so the caps, the four
    event types, the built-in scripts and the rule for what a script may be are
    written twice on purpose, and this is the file that says they are the same
    twice. `tests/test_executor.py`'s SOUND_RE parity test is the pattern.
    """

    @classmethod
    def setUpClass(cls):
        cls.gate = _gate_module()

    def test_the_built_in_scripts_are_the_shim_s(self):
        self.assertEqual(ghostshim.BUILTINS, self.gate.GHOST_BUILTINS)

    def test_the_caps_and_the_types_are_the_shim_s(self):
        self.assertEqual(ghostshim.MAX_EVENTS, self.gate.GHOST_MAX_EVENTS)
        self.assertEqual(ghostshim.MAX_MS, self.gate.GHOST_MAX_MS)
        self.assertEqual(ghostshim.TYPES, self.gate.GHOST_TYPES)
        self.assertEqual(executor.GHOST_KEYS, self.gate.GHOST_KEYS)

    def test_the_gap_between_two_built_ins_is_the_shim_s(self):
        # The one number that lives only in the shim's JavaScript: the shim
        # offsets a second built-in by this much, and a gate that used another
        # would play the same two scripts at different times.
        found = re.search(r"\bvar GAP_MS = (\d+);", ghostshim.script_js())
        self.assertIsNotNone(found, "the shim declares no GAP_MS")
        self.assertEqual(int(found.group(1)), self.gate.GHOST_GAP_MS)

    def test_the_two_validators_agree_on_every_sample(self):
        for what, text in GHOST_SAMPLES:
            with self.subTest(what):
                mine, why_mine = self.gate.validate_ghost(text)
                theirs, why_theirs = executor.validate_ghost(text)
                self.assertEqual(theirs, mine)
                self.assertEqual(why_theirs, why_mine)

    def test_a_list_out_of_order_is_played_in_order_not_refused(self):
        events, why = self.gate.validate_ghost(
            '[{"t": 9, "type": "up", "x": 1, "y": 1},'
            ' {"t": 1, "type": "down", "x": 0, "y": 0}]')
        self.assertIsNone(why)
        self.assertEqual([1, 9], [event["t"] for event in events])

    def wanted(self, *words):
        return [(word, word, None) for word in words]

    def test_the_default_script_follows_the_assertions(self):
        self.assertEqual("click",
                         self.gate.ghost_default_name(self.wanted("responds(click)")))
        self.assertEqual("drag",
                         self.gate.ghost_default_name(self.wanted("responds(drag)")))
        # Both, in that order: a sketch is clicked before it is dragged.
        self.assertEqual(
            "click,drag",
            self.gate.ghost_default_name(
                self.wanted("responds(drag)", "responds(click)")))
        # Every run gets a window, so a sketch that asked for no interaction
        # at all is crossed rather than left alone (DECIDE[ghost-who]).
        self.assertEqual("wander", self.gate.ghost_default_name([]))
        self.assertEqual("wander",
                         self.gate.ghost_default_name(self.wanted("motion(idle)")))

    def test_two_built_ins_are_joined_the_way_the_shim_joins_them(self):
        both = self.gate.ghost_builtin("click,drag")
        click = ghostshim.BUILTINS["click"]
        drag = ghostshim.BUILTINS["drag"]
        self.assertEqual(len(click) + len(drag), len(both))
        shift = max(event["t"] for event in click) + self.gate.GHOST_GAP_MS
        self.assertEqual([event["t"] + shift for event in drag],
                         [event["t"] for event in both[len(click):]])
        self.assertLessEqual(max(event["t"] for event in both),
                             self.gate.GHOST_MAX_MS)

    def test_a_joined_script_is_still_held_to_the_caps(self):
        # Nothing the gate plays may be longer than what the shim would play,
        # or the two players show the sketch different things.
        long = self.gate.ghost_builtin("wander,wander,wander")
        self.assertLessEqual(len(long), self.gate.GHOST_MAX_EVENTS)
        self.assertLessEqual(max(event["t"] for event in long),
                             self.gate.GHOST_MAX_MS)

    def test_a_sketch_with_no_script_of_its_own_gets_the_default(self):
        with tempfile.TemporaryDirectory() as empty:
            notes = []
            events, source, name = self.gate.ghost_script(
                empty, self.wanted("responds(click)"), notes)
            self.assertEqual(("default", "click"), (source, name))
            self.assertEqual(ghostshim.BUILTINS["click"], events)
            self.assertEqual([], notes)

    def test_an_invalid_script_is_a_note_and_the_default_not_a_refusal(self):
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / "ghost.json").write_text("[]", encoding="utf-8")
            notes = []
            events, source, name = self.gate.ghost_script(home, [], notes)
            self.assertEqual(("default", "wander"), (source, name))
            self.assertEqual(ghostshim.BUILTINS["wander"], events)
            self.assertEqual(1, len(notes))
            self.assertIn("nothing to play", notes[0])

    def test_the_echo_fixture_carries_the_script_the_expectation_counts(self):
        fixture = FIXTURES / "ghost-echo"
        events, why = self.gate.validate_ghost(
            (fixture / "ghost.json").read_text(encoding="utf-8"))
        self.assertIsNone(why)
        expected = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))
        want = expected["ghost-echo"]["ghost"]
        self.assertEqual({"source": "executor", "events": len(events),
                          "played": len(events)}, want)
        # And the gate reads it from the directory rather than being told.
        notes = []
        read, source, name = self.gate.ghost_script(fixture, [], notes)
        self.assertEqual((events, "executor", None, []), (read, source, name, notes))

    def test_the_console_is_read_up_to_the_window_and_not_through_it(self):
        # The promise HARNESS_VERSION 3 makes: nothing the gate fails changed.
        # A pointer clicking where the probe did not may not turn a sketch that
        # passed into a console_clean failure.
        rec = self.gate.Recorder()
        rec.entries = [{"t": "x", "type": "log", "text": "hello"},
                       {"t": "x", "type": "pageerror", "text": "boom"}]
        self.assertTrue(rec.clean_through(1))
        self.assertFalse(rec.clean_through())
        self.assertFalse(rec.clean)

    def test_the_ghost_window_can_be_turned_off(self):
        self.assertTrue(self.gate.parse_args(["dir"]).ghost)
        self.assertFalse(self.gate.parse_args(["dir", "--no-ghost"]).ghost)
