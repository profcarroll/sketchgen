"""Unit tests for the ops surface: keygen, install-unit, and the unit files.

Nothing here installs, enables, starts or reloads anything, and nothing here
touches ~/.ssh or the real ~/.config: keygen runs with --dir into a temp
directory and install-unit runs --dry-run under a temp HOME.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"
UNIT_DIR = REPO_ROOT / "systemd"
UNIT_FILES = ("sketchgen-worker.service", "sketchgen-worker.timer", "sketchgen-web.service",
              "sketchgen-sync.service", "sketchgen-sync.timer",
              "sketchgen-backup.service", "sketchgen-backup.timer")

SECTION_RE = re.compile(r"^\[[A-Za-z][A-Za-z0-9]*\]$")
KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*=")


def run_cli(*args: str, env: dict[str, str] | None = None):
    """Run the CLI in a subprocess so exit codes are the real ones."""
    environ = dict(os.environ)
    if env:
        environ.update(env)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        check=False,
        env=environ,
    )


class KeygenTests(unittest.TestCase):
    """keygen writes a 0600 private half, prints only the public one, refuses twice."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("ssh-keygen") is None:
            raise unittest.SkipTest("ssh-keygen is not on PATH")

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sketchgen-keygen-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_dry_run_writes_nothing(self):
        result = run_cli("keygen", "app", "--dir", self.tmp, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry run", result.stdout)
        self.assertIn("ssh-keygen -t ed25519", result.stdout)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_generates_pair_then_refuses(self):
        result = run_cli("keygen", "gallery", "--dir", self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)

        private = Path(self.tmp) / "sketchgen-gallery"
        public = Path(self.tmp) / "sketchgen-gallery.pub"
        self.assertTrue(private.is_file())
        self.assertTrue(public.is_file())

        mode = stat.S_IMODE(private.stat().st_mode)
        self.assertEqual(mode, 0o600, f"private half is {mode:04o}, want 0600")

        lines = result.stdout.splitlines()
        self.assertTrue(
            any(line.startswith("ssh-ed25519") for line in lines),
            f"no public key line in: {lines}",
        )
        self.assertIn("sketchgen-gallery-deploy", result.stdout)
        self.assertIn("GIT_SSH_COMMAND", result.stdout)
        # The private half must never appear in what the command prints.
        self.assertNotIn("PRIVATE KEY", result.stdout)
        self.assertNotIn("PRIVATE KEY", result.stderr)

        again = run_cli("keygen", "gallery", "--dir", self.tmp)
        self.assertEqual(again.returncode, 3, again.stdout + again.stderr)
        self.assertIn("refusing", again.stderr)

    def test_permission_line_differs_by_role(self):
        app = run_cli("keygen", "app", "--dir", self.tmp, "--dry-run")
        gallery = run_cli("keygen", "gallery", "--dir", self.tmp, "--dry-run")
        self.assertIn("READ-ONLY", app.stdout)
        self.assertIn("WRITE", gallery.stdout)


class InstallUnitTests(unittest.TestCase):
    """install-unit --dry-run names both targets and writes nothing at all."""

    def test_dry_run_prints_targets_and_writes_nothing(self):
        tmp_home = tempfile.mkdtemp(prefix="sketchgen-home-")
        self.addCleanup(shutil.rmtree, tmp_home, True)

        result = run_cli("install-unit", "--dry-run", env={"HOME": tmp_home})
        self.assertEqual(result.returncode, 0, result.stderr)

        target_dir = Path(tmp_home) / ".config" / "systemd" / "user"
        for name in UNIT_FILES:
            self.assertIn(str(target_dir / name), result.stdout)
        self.assertIn("daemon-reload", result.stdout)
        self.assertIn("sketchgen-worker.timer", result.stdout)

        self.assertEqual(os.listdir(tmp_home), [], "dry run wrote into HOME")

    def test_dry_run_enables_nothing(self):
        tmp_home = tempfile.mkdtemp(prefix="sketchgen-home-")
        self.addCleanup(shutil.rmtree, tmp_home, True)
        result = run_cli("install-unit", "--dry-run", env={"HOME": tmp_home})
        self.assertIn("nothing has been enabled or started", result.stdout)


class UnitFileTests(unittest.TestCase):
    """A local stand-in for systemd-analyze: every line is a section, key, or comment."""

    def parse(self, path: Path) -> dict[str, list[str]]:
        sections: dict[str, list[str]] = {}
        current = None
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("["):
                self.assertRegex(line, SECTION_RE, f"{path.name}:{number}: bad section")
                current = line[1:-1]
                sections.setdefault(current, [])
                continue
            self.assertIsNotNone(current, f"{path.name}:{number}: key before any section")
            self.assertRegex(line, KEY_RE, f"{path.name}:{number}: bad key line")
            key, value = line.split("=", 1)
            self.assertNotEqual(value.strip(), "", f"{path.name}:{number}: empty value")
            sections[current].append(key)
        return sections

    def test_service_is_well_formed(self):
        sections = self.parse(UNIT_DIR / "sketchgen-worker.service")
        self.assertEqual(set(sections), {"Unit", "Service", "Install"})
        for key in ("Type", "ExecStart", "WorkingDirectory", "Restart", "RestartSec"):
            self.assertIn(key, sections["Service"])
        # four paths, and they are the four sketchgen/worker.py reads:
        # SKETCHGEN_DB, SKETCHGEN_JOBS, SKETCHGEN_GATE, OLLAMA_HOST_URL
        self.assertEqual(sections["Service"].count("Environment"), 4)

    def test_timer_is_well_formed(self):
        sections = self.parse(UNIT_DIR / "sketchgen-worker.timer")
        self.assertEqual(set(sections), {"Unit", "Timer", "Install"})
        for key in ("OnBootSec", "OnUnitActiveSec", "Persistent", "Unit"):
            self.assertIn(key, sections["Timer"])

    def test_no_secret_ever_sits_in_a_unit_file(self):
        for name in UNIT_FILES:
            text = (UNIT_DIR / name).read_text(encoding="utf-8")
            for needle in ("PRIVATE KEY", "API_KEY", "TOKEN=", "PASSWORD"):
                self.assertNotIn(needle, text, f"{name} looks like it carries a secret")

    def test_service_binds_nothing_public(self):
        text = (UNIT_DIR / "sketchgen-worker.service").read_text(encoding="utf-8")
        self.assertIn("OLLAMA_HOST_URL=http://127.0.0.1:11434", text)
        self.assertNotIn("0.0.0.0", text)


if __name__ == "__main__":
    unittest.main()


class WebUnitTests(unittest.TestCase):
    """The operator UI's unit binds loopback only and carries no secret."""

    def test_web_unit_binds_loopback_and_names_the_port(self):
        text = (UNIT_DIR / "sketchgen-web.service").read_text(encoding="utf-8")
        self.assertIn("--bind 127.0.0.1", text)
        self.assertIn("--port 8081", text)
        self.assertNotIn("0.0.0.0", text)
        for secret in ("KEY=", "TOKEN=", "SECRET=", "PASSWORD="):
            self.assertNotIn(secret, text.upper().replace("SKETCHGEN_GATE=", ""))
        self.assertIn("WantedBy=default.target", text)


class SyncUnitTests(unittest.TestCase):
    """The sync oneshot takes its token from a file, never argv, and the timer drives it."""

    def test_sync_units(self):
        service = (UNIT_DIR / "sketchgen-sync.service").read_text(encoding="utf-8")
        timer = (UNIT_DIR / "sketchgen-sync.timer").read_text(encoding="utf-8")
        self.assertIn("Type=oneshot", service)
        self.assertIn("--token-file", service)
        self.assertNotIn("Bearer", service)
        self.assertIn("SKETCHGEN_WRITEPATH_URL=https://sketchgen-writepath.sketchgen.workers.dev", service)
        self.assertIn("Unit=sketchgen-sync.service", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("WantedBy=timers.target", timer)

