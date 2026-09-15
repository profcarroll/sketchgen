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
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_DIR = REPO_ROOT / "gate"
GATE = GATE_DIR / "sketch_gate.py"
ACCEPT = GATE_DIR / "accept.sh"
FIXTURES = GATE_DIR / "fixtures"

#: sha256 of the gate on the node on 2026-09-15, which is also the sha256 of the
#: copy in the operator's course repo. Three copies, one hash: that is the whole
#: claim spec §3.2 makes, and it stops a well-meaning reformat of the referee
#: from landing without somebody deciding to change this line.
GATE_SHA256 = "426e8e7429984b1f5377c068886bebeaab8795a2a102a687c06938bb6a66b658"

#: How long the six fixtures are allowed to take together. On the node a single
#: gate run is about four seconds and the harness does eleven of them.
ACCEPT_TIMEOUT_S = 600


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
