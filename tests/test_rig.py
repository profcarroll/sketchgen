"""The staged rig, checked against the things it is a proxy for.

`rig/` is prose, two paste-in scripts, a page and a little meter: almost nothing
to unit-test, and exactly one way to go wrong. The agent on job 1286 spent part
of its thirty-seven minutes reading `gate/sketch_gate.py` and
`sketchgen/executor.py` to find the numbers it had to design against
(2026-09-21); the rig writes those numbers down, and a written-down number drifts
from the constant it was copied from. So the facts table is tied to the gate's
constants here, and the rig's page is tied to the executor's, and a change to
either one fails this file rather than quietly teaching the next agent something
that stopped being true.

No browser, no network: `fetch-p5.sh` is exercised with a fake `curl` on PATH,
the way `tests/test_sg.py` fakes `ssh`, and `cost.py` reads a fixture transcript.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RIG = REPO_ROOT / "rig"
GATE = REPO_ROOT / "gate" / "sketch_gate.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "rig" / "transcript.jsonl"

sys.path.insert(0, str(REPO_ROOT))
from sketchgen import executor  # noqa: E402


def gate_module():
    """The gate, imported for its constants. It loads playwright lazily, inside
    the run, so reading `IDLE_FRAMES` off it costs nothing a laptop lacks."""
    spec = importlib.util.spec_from_file_location("sketch_gate", GATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table_rows(text):
    return [line for line in text.splitlines() if line.lstrip().startswith("|")]


class FactsTableTests(unittest.TestCase):
    """Acceptance 1: the README's table says what the gate says."""

    def setUp(self):
        self.gate = gate_module()
        self.readme = (RIG / "README.md").read_text(encoding="utf-8")
        self.rows = table_rows(self.readme)

    def row_naming(self, constant):
        rows = [r for r in self.rows if "`%s`" % constant in r]
        self.assertTrue(rows, "no facts-table row names %s" % constant)
        return rows

    def assert_stated(self, constant, value):
        for row in self.row_naming(constant):
            if value in row:
                return
        self.fail("rig/README.md names %s but does not state %r; the gate has "
                  "moved and the table has not" % (constant, value))

    def test_the_viewport_is_the_gate_s(self):
        v = self.gate.VIEWPORT
        self.assert_stated("VIEWPORT", "%d×%d" % (v["width"], v["height"]))

    def test_the_idle_window_and_the_probe_window_are_the_gate_s(self):
        self.assert_stated("IDLE_FRAMES", str(self.gate.IDLE_FRAMES))
        self.assert_stated("PROBE_FRAMES", str(self.gate.PROBE_FRAMES))

    def test_the_ghost_window_s_caps_are_the_gate_s(self):
        # Added 2026-09-21 with the window itself. A number in this table that
        # has drifted is worse than a missing one: the next agent designs a
        # pointer script against it. The milliseconds are written the way the
        # rest of the file writes a four-figure number, with the comma.
        self.assert_stated("GHOST_MAX_EVENTS", str(self.gate.GHOST_MAX_EVENTS))
        self.assert_stated("GHOST_MAX_MS", "{:,} ms".format(self.gate.GHOST_MAX_MS))
        # And the three scripts it can play are named where an agent looks.
        self.assert_stated("GHOST_BUILTINS", "ghost.png")

    def test_the_budgets_are_the_gate_s(self):
        self.assert_stated("DEFAULT_FRAME_BUDGET_MS",
                           "%g ms" % self.gate.DEFAULT_FRAME_BUDGET_MS)
        self.assert_stated("DEFAULT_BUDGET_S", "%g s" % self.gate.DEFAULT_BUDGET_S)

    def test_the_rule_is_the_first_thing_in_the_file(self):
        # The one sentence the rig exists to be read with. Entry 1279 read 6.4 ms
        # here and 9.8 ms on the gate; a rig that forgets to say so is worse than
        # no rig, because its number looks like a verdict.
        head = self.readme.split("\n\n", 2)[1]
        self.assertIn("advisory", head)
        self.assertIn("the gate's number is the number", head.lower())
        self.assertIn("6.4", self.readme)
        self.assertIn("9.8", self.readme)

    def test_the_bench_carries_the_same_numbers(self):
        # bench.js prints the budget beside its own reading, so the same drift
        # would make the rig say "comfortably under" about a sketch the gate
        # would refuse.
        bench = (RIG / "bench.js").read_text(encoding="utf-8")

        def declared(name):
            match = re.search(r"\bB\.%s\s*=\s*([0-9.]+)\s*;" % name, bench)
            self.assertIsNotNone(match, "bench.js declares no B.%s" % name)
            return float(match.group(1))

        self.assertEqual(declared("BUDGET_MS"), self.gate.DEFAULT_FRAME_BUDGET_MS)
        self.assertEqual(declared("EARLY_TRIP"), self.gate.EARLY_TRIP_FACTOR)
        self.assertEqual(declared("IDLE_FRAMES"), self.gate.IDLE_FRAMES)


class RigPageTests(unittest.TestCase):
    """Acceptance 2: the rig's page is the executor's page, local p5 aside."""

    def setUp(self):
        self.html = (RIG / "index.html").read_text(encoding="utf-8")
        self.local_tag = executor._P5_TAG.replace(
            "https://cdnjs.cloudflare.com/ajax/libs/p5.js/1.11.3/p5.min.js",
            "p5.min.js")

    def test_the_page_differs_from_the_executor_s_only_in_the_p5_src(self):
        self.assertEqual(self.html,
                         executor.DEFAULT_INDEX_HTML.replace(executor._P5_TAG,
                                                             self.local_tag))

    def test_p5_loads_in_the_same_place_relative_to_sketch_js(self):
        def gap(text, p5_tag):
            lines = text.splitlines(keepends=True)
            p5_at = lines.index(p5_tag)
            sketch_at = next(i for i, line in enumerate(lines)
                             if 'src="sketch.js"' in line)
            self.assertLess(p5_at, sketch_at, "p5 must load before the sketch")
            return sketch_at - p5_at

        self.assertEqual(gap(self.html, self.local_tag),
                         gap(executor.DEFAULT_INDEX_HTML, executor._P5_TAG))

    def test_the_pinned_p5_is_the_version_the_executor_writes(self):
        # One version across the two pages, or the rig is timing another library.
        version = executor._P5_TAG.split("/p5.js/")[1].split("/")[0]
        self.assertIn('P5_VERSION="%s"' % version,
                      (RIG / "fetch-p5.sh").read_text(encoding="utf-8"))


FAKE_CURL = """#!/usr/bin/env bash
# Stand-in for curl: writes %s to whatever -o names, and exits %d.
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift 2 ;;
        *) shift ;;
    esac
done
[ -n "$out" ] && printf '%%s' 'not p5 at all' > "$out"
exit %d
"""


@unittest.skipUnless(shutil.which("bash"), "fetch-p5.sh is a bash script")
class FetchP5Tests(unittest.TestCase):
    """Acceptance 3: a p5 that is not the pinned one is refused, not kept.

    The script is copied out of the repository first, because it fetches beside
    itself: run in a temporary directory it can neither read nor write the
    developer's own `rig/p5.min.js`.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-rig-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.rig = self.tmp / "rig"
        self.rig.mkdir()
        self.script = self.rig / "fetch-p5.sh"
        shutil.copy2(RIG / "fetch-p5.sh", self.script)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

    def fake_curl(self, status):
        curl = self.bin / "curl"
        curl.write_text(FAKE_CURL % ("junk", status, status), encoding="utf-8")
        curl.chmod(0o755)

    def fetch(self):
        return subprocess.run(
            ["bash", str(self.script)], capture_output=True, text=True,
            env=dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"])),
            check=False,
        )

    def test_a_sha256_mismatch_is_refused_and_writes_nothing(self):
        self.fake_curl(0)
        result = self.fetch()
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("sha256 mismatch", result.stderr)
        self.assertIn("expected", result.stderr)
        self.assertFalse((self.rig / "p5.min.js").exists(),
                         "a refused download must leave nothing behind")

    def test_a_download_that_fails_is_a_failure_not_a_refusal(self):
        self.fake_curl(22)
        result = self.fetch()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.rig / "p5.min.js").exists())


class CostTests(unittest.TestCase):
    """Acceptance 4: the meter reads a transcript the same way every time.

    The last line is one object to merge into a packet item: `process`, the
    window's total, and `usage`, the reply's own counts. The fixture's msg_d
    wrote a reply (a heredoc into answer.txt) and msg_e copied it into an
    import; job 1319 (entry 1312, 2026-09-22) is why the reply's usage is here
    at all: #145 got the process recorded and the two counts a local attempt
    always has stayed blank, because nothing read them off the transcript.
    """

    REPLY = ("```js\nfunction setup() { createCanvas(800, 600); }\n"
             "function draw() { background(frameCount % 255); }\n```\n"
             "A statement about it.\n")

    def cost(self, *args):
        return subprocess.run(
            [sys.executable, str(RIG / "cost.py"), "--transcript", str(FIXTURE), *args],
            capture_output=True, text=True, check=False,
        )

    def reply_file(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = Path(d) / "answer.txt"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_a_window_reproduces_its_line(self):
        result = self.cost("--since", "2026-09-21T17:00:00Z",
                           "--until", "2026-09-21T17:30:00Z")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "17:00:00Z -> 17:30:00Z  30 min 0 s")
        # Two assistant messages, because the two lines sharing msg_a are one
        # message; three tool calls, because tool_use ids are counted across
        # both of them; three images in the tool results.
        self.assertEqual(lines[1], "2 assistant messages, 3 tool calls, 3 screenshots")
        self.assertEqual(lines[2], "2,300 tokens generated, 1,300 of them thinking (56%)")
        self.assertEqual(lines[3], "reply: not looked for; --reply FILE names the file "
                                   "it was written to")
        self.assertEqual(json.loads(lines[4]), {
            "usage": {"prompt_tokens": None, "completion_tokens": None},
            "process": {"session_s": 1800, "output_tokens": 2300, "thinking_tokens": 1300,
                        "tool_calls": 3, "screenshots": 3},
        })

    def test_the_whole_transcript_spans_its_own_stamps(self):
        result = self.cost()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "17:02:11Z -> 18:00:00Z  57 min 49 s")
        self.assertEqual(json.loads(lines[4])["process"]["output_tokens"], 10226)

    def test_an_empty_window_exits_1(self):
        result = self.cost("--since", "2026-09-22T00:00:00Z")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nothing in that window", result.stderr)

    # -- the reply's own counts ------------------------------------------------

    def test_the_reply_s_usage_is_the_message_that_wrote_it(self):
        # msg_d wrote it at 17:40 and msg_e copied it into an import at 17:50:
        # the earliest is the one that generated it, and the copy is counted
        # as a copy. prompt_tokens is everything that message read (input and
        # both caches), completion_tokens everything it generated.
        result = self.cost("--since", "2026-09-21T17:00:00Z", "--until", "2026-09-21T18:00:00Z",
                           "--reply", self.reply_file(self.REPLY))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[3], "reply: written at 17:40:00Z, 68,935 tokens read, "
                                   "2,493 generated, and 1 later copy")
        found = json.loads(lines[4])
        self.assertEqual(found["usage"], {"prompt_tokens": 68935, "completion_tokens": 2493})
        # and the window's total is still the window's total
        self.assertEqual(found["process"]["output_tokens"], 10226)

    def test_a_reply_without_a_js_block_is_matched_whole(self):
        # A plan is Brief and Assertions with no fence: the whole text is the key.
        result = self.cost("--reply", self.reply_file("A statement about it.\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[4])["usage"],
                         {"prompt_tokens": 68935, "completion_tokens": 2493})

    def test_a_reply_no_single_message_wrote_leaves_usage_null(self):
        # Edited in place across several tool calls, or written outside the
        # window: not found is the truth, and null is what the node records.
        result = self.cost("--reply", self.reply_file("```js\nfunction draw() {}\n```\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[3], "reply: not found in one message of this window; "
                                   "usage left null")
        self.assertEqual(json.loads(lines[4])["usage"],
                         {"prompt_tokens": None, "completion_tokens": None})
        outside = self.cost("--since", "2026-09-21T17:00:00Z", "--until", "2026-09-21T17:30:00Z",
                            "--reply", self.reply_file(self.REPLY))
        self.assertIn("not found in one message", outside.stdout)

    def test_an_empty_reply_file_exits_1(self):
        result = self.cost("--reply", self.reply_file("  \n"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("the reply is empty", result.stderr)


@unittest.skipUnless(shutil.which("git"), "needs git")
class WorkingTreeTests(unittest.TestCase):
    """Acceptance 5: the recipe leaves nothing to clean up.

    The whole reason `.claude/launch.json` is tracked and the two working files
    are ignored: on 2026-09-21 the agent had to create a launch configuration
    and remember to delete it, because it was not ignored either.
    """

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, check=False)

    def test_the_launch_configuration_is_tracked(self):
        self.assertEqual(self.git("ls-files", ".claude/launch.json").stdout.strip(),
                         ".claude/launch.json")
        config = json.loads((REPO_ROOT / ".claude" / "launch.json").read_text("utf-8"))
        names = [c["name"] for c in config["configurations"]]
        self.assertIn("rig", names)
        rig = next(c for c in config["configurations"] if c["name"] == "rig")
        self.assertIn("http.server", rig["runtimeArgs"])
        self.assertIn("rig", rig["runtimeArgs"])

    def test_what_the_recipe_writes_is_ignored(self):
        for name in ("p5.min.js", "sketch.js"):
            path = RIG / name
            self.assertEqual(self.git("check-ignore", "rig/%s" % name).returncode, 0,
                             "rig/%s is not ignored" % name)
            if not path.exists():
                path.write_text("// left by the rig\n", encoding="utf-8")
                self.addCleanup(path.unlink)
        # Scoped to the two paths, so an unrelated edit elsewhere in the tree
        # cannot fail this.
        status = self.git("status", "--porcelain", "--untracked-files=all", "--",
                          "rig/p5.min.js", "rig/sketch.js")
        self.assertEqual(status.stdout, "", "the rig's working files are visible to git")


if __name__ == "__main__":
    unittest.main()
