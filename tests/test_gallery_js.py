"""gallery.js, actually run (plan §6.7, §6.9).

The rest of the suite reads the script as text. Run-in-place cannot be checked
that way — the thing to prove is that one click starts one iframe, a second
click somewhere else does not leave two running, and stopping removes the
frame rather than hiding it — so this runs the real file in node against a stub
DOM (``tests/js/dom.js``) and clicks it.

node is not a dependency of sketchgen and is not on the node's venv path, so a
machine without it skips this file instead of failing it.
"""

import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HARNESS = Path(__file__).resolve().parent / "js" / "run_in_place.js"


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


if __name__ == "__main__":
    unittest.main()
