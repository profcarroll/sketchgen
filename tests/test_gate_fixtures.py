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
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = REPO_ROOT / "gate"
GATE = GATE_DIR / "sketch_gate.py"
ACCEPT = GATE_DIR / "accept.sh"
FIXTURES = GATE_DIR / "fixtures"

#: sha256 of the gate in this repo. Three copies — here, the node, the
#: operator's course repo — one hash: that is the whole claim spec §3.2 makes,
#: and it stops a well-meaning reformat of the referee from landing without
#: somebody deciding to change this line.
#:
#: Changed 2026-09-15 by the frame budget (job 166, job 270): the gate now times
#: its own idle window and fails `frame_budget` above --frame-budget-ms, or
#: above the --budget-s wall ceiling. The previous value, which is the one the
#: node and the course repo carry until this is deployed, was
#: 426e8e7429984b1f5377c068886bebeaab8795a2a102a687c06938bb6a66b658.
GATE_SHA256 = "6fdf97b2e6ee63d8266a7b16fdc66cb3b88cea2f4b3bb43053dc8528d6b01e00"

#: How long the seven fixtures are allowed to take together. On the node a
#: single gate run is about four seconds and the harness does thirteen of them —
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
