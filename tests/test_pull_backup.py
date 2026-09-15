"""Unit tests for bin/pull-backup.sh and the operator's pull timer — spec §3.4.

The script runs on the operator's machine and talks to the node over ssh, so
nothing here runs it against a real host: what is checked is the shape of it,
and one property that matters more than any of the rest — that the `jobs/`
rsync has no `--delete` on it, ever. The snapshots are pruned on the node and
the mirror follows them down; the attempt archive only grows, and a `--delete`
on that side would replay one bad night on the node over here at machine speed.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIT_DIR = REPO_ROOT / "systemd"


class PullScriptTests(unittest.TestCase):
    """bin/pull-backup.sh: the copy that actually leaves the instance."""

    SCRIPT = REPO_ROOT / "bin" / "pull-backup.sh"

    def test_it_is_tracked_and_executable(self):
        self.assertTrue(self.SCRIPT.is_file())
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))

    def test_it_parses_as_bash(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("no bash here")
        result = subprocess.run(
            [bash, "-n", str(self.SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_it_refuses_without_a_host(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("no bash here")
        result = subprocess.run(
            [bash, str(self.SCRIPT)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("HOST", result.stderr + result.stdout)

    def test_it_deletes_from_backups_and_never_from_jobs(self):
        """The asymmetry is the whole design: snapshots are pruned on the node
        and the mirror must follow, attempts are only ever added and a --delete
        on that side would turn one bad night into data loss."""
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--delete-excluded", text)
        jobs_lines = [
            line for line in text.splitlines()
            if "rsync" in line and "jobs" in line and not line.strip().startswith("#")
        ]
        self.assertTrue(jobs_lines, "no rsync of jobs/ in the script")
        for line in jobs_lines:
            self.assertNotIn("--delete", line, "the jobs/ pull must never delete")

    def test_the_operator_timer_ships_with_install_instructions(self):
        unit_dir = UNIT_DIR / "operator"
        service = unit_dir / "sketchgen-pull.service"
        timer = unit_dir / "sketchgen-pull.timer"
        self.assertTrue(service.is_file())
        self.assertTrue(timer.is_file())
        text = service.read_text(encoding="utf-8") + timer.read_text(encoding="utf-8")
        self.assertIn("systemctl --user", text)
        self.assertIn("OnCalendar", timer.read_text(encoding="utf-8"))



if __name__ == "__main__":
    unittest.main()
