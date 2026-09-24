"""install.sh — standing up a fresh private node.

The install itself needs a node (apt, systemd, a GPU, Ollama), so it is proven
on one, not here. What the suite can hold is the script's promises: it parses
what it documents, it refuses bad flags before touching anything, it never
enables a unit, and without a terminal it stops at the first sudo and says
what to run instead of hanging on a password prompt.
"""
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "install.sh"


def run(*args, env=None, stdin=subprocess.DEVNULL):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True,
                          text=True, env=env, stdin=stdin, timeout=60)


class InstallShTextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SCRIPT.read_text(encoding="utf-8")
        # Everything before the closing message: the part that runs commands.
        cls.body = cls.text[:cls.text.index("# --- Next ---")]

    def test_it_parses(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_it_is_executable(self):
        self.assertTrue(SCRIPT.stat().st_mode & stat.S_IXUSR)

    def test_every_flag_it_parses_is_documented(self):
        header = self.text[:self.text.index("set -euo pipefail")]
        parsed = set(re.findall(r"^\s+(--[a-z-]+)\)", self.text, re.M))
        self.assertTrue(parsed)
        for flag in parsed:
            self.assertIn(f"#   {flag} ", header, f"{flag} is parsed but not in --help")

    def test_it_never_enables_a_unit(self):
        """Enabling is a decision about what the node does (as in update.sh)."""
        self.assertNotRegex(self.body, r"systemctl --user (enable|start|restart)")

    def test_the_sync_timer_is_never_offered(self):
        """A private node has no write-path Worker for it to sync with."""
        enable_line = next(l for l in self.text.splitlines() if "enable --now" in l)
        self.assertNotIn("sketchgen-sync", enable_line)

    def test_an_existing_app_or_gallery_is_left_alone(self):
        self.assertIn('if [[ -d "$APP/.git" ]]', self.body)
        self.assertIn('if [[ -e "$GALLERY" ]]', self.body)
        self.assertNotRegex(self.body, r"git -C \"\$APP\" (pull|reset|fetch)")

    def test_node_conf_is_never_overwritten(self):
        self.assertIn('if [[ ! -e "$dir/node.conf" ]]', self.body)
        self.assertEqual(self.body.count('> "$dir/node.conf"'), 1)

    def test_only_a_database_it_created_is_paused(self):
        pause = self.body.index("control pause")
        self.assertIn("if [[ $fresh_db == yes ]]", self.body[pause - 200:pause])

    def test_it_carries_no_credential(self):
        for needle in ("PRIVATE KEY", "API_KEY", "TOKEN=", "PASSWORD", "Bearer"):
            self.assertNotIn(needle, self.text)


class InstallShRunTests(unittest.TestCase):
    """Runs the script with a scratch HOME and stub system tools on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.log = Path(self.tmp.name) / "calls"
        self.stub("uname", 'echo Linux')
        self.stub("apt-get", f'echo "apt-get $*" >> {self.log}')
        self.stub("dpkg", 'exit 1')                 # every package is missing
        self.stub("sudo", f'echo "sudo $*" >> {self.log}; exit 1')  # sudo -n fails
        self.env = {"HOME": str(self.home), "USER": "tester",
                    "PATH": f"{self.bin}:/usr/bin:/bin"}

    def stub(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)

    def test_bad_flags_stop_before_anything_runs(self):
        for args in (["--rate", "two"], ["--operator", "a@b.c"], ["--ref"], ["--nope"]):
            with self.subTest(args=args):
                done = run(*args, env=self.env)
                self.assertEqual(done.returncode, 1)
                self.assertFalse(self.log.exists(), "a tool ran before the flags were checked")
                self.assertEqual(list(self.home.iterdir()), [])

    def test_help_prints_the_header(self):
        done = run("--help", env=self.env)
        self.assertEqual(done.returncode, 0)
        self.assertIn("--ollama-url URL", done.stdout)
        self.assertNotIn("set -euo", done.stdout)

    def test_without_a_terminal_it_stops_at_sudo_and_says_what_to_run(self):
        done = run("--ref", "a72f076", "--shape", "D12 box 2", env=self.env)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn("sudo apt-get update -q", done.stderr)
        self.assertIn("ssh -t <this node> 'bash install.sh --ref a72f076 --shape D12\\ box\\ 2'",
                      done.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(calls, ["sudo -n true"], "it must not try a sudo that would prompt")
        self.assertFalse((self.home / "sketchgen").exists())

    def test_it_refuses_a_node_without_apt(self):
        (self.bin / "apt-get").unlink()
        env = dict(self.env, PATH=str(self.bin))    # no /usr/bin: no real apt-get
        done = subprocess.run(["/bin/bash", str(SCRIPT)], capture_output=True, text=True,
                              env=env, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(done.returncode, 1)
        self.assertIn("no apt-get", done.stderr)


if __name__ == "__main__":
    unittest.main()
