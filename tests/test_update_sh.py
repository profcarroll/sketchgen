"""update.sh step 1: the node goes to its target — main, or its pin — and never
past it (docs/plans/fleet.md §1.5).

Until 2026-09-24 step 1 was `git pull origin main`, which fast-forwards a
detached HEAD without a word; three nodes were held on a72f076 for the hardware
A/B by nothing else. These run the real script against a scratch node: HOME is
a temporary directory with ~/sketchgen/app cloned from a scratch origin,
`systemctl` and bin/sketchgen are stubs that write down what they were asked,
and git is real. The app in the origin is not sketchgen — it is this update.sh,
a stub bin/sketchgen, and the marker file that says a checkout knows pins — so
nothing here imports the package or touches a model, the network or a gallery.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATE_SH = REPO_ROOT / "update.sh"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

#: bin/sketchgen, as far as update.sh can tell. `build --needs-render` answers
#: with $FAKE_VERDICT; everything else succeeds. Every call is logged.
STUB_SKETCHGEN = """#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("sketchgen " + " ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["build", "--needs-render"]:
    print(os.environ.get("FAKE_VERDICT", "skip nothing on the render path"))
"""

STUB_SYSTEMCTL = """#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
exit 0
"""


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


@unittest.skipUnless(shutil.which("bash") and shutil.which("flock"),
                     "update.sh needs bash and flock")
class UpdateShStep1(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.node_home = self.home / "sketchgen"
        self.app = self.node_home / "app"
        self.log = base / "calls"
        self.bin = base / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(STUB_SYSTEMCTL)
        (self.bin / "systemctl").chmod(0o755)

        # The origin: three commits of an app that is update.sh and a stub.
        self.origin = base / "origin.git"
        work = base / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env={**os.environ, **GIT_ENV})
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True,
                       env={**os.environ, **GIT_ENV})
        git(work, "remote", "add", "origin", str(self.origin))
        shutil.copy(UPDATE_SH, work / "update.sh")
        (work / "bin").mkdir()
        (work / "bin" / "sketchgen").write_text(STUB_SKETCHGEN)
        (work / "sketchgen" / "cli").mkdir(parents=True)
        (work / "sketchgen" / "cli" / "build.py").write_text("# marker\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "one")
        self.one = git(work, "rev-parse", "HEAD")
        (work / "README.md").write_text("two\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "two")
        self.two = git(work, "rev-parse", "HEAD")
        (work / "README.md").write_text("three\n")
        git(work, "add", ".")
        git(work, "commit", "-q", "-m", "three")
        self.three = git(work, "rev-parse", "HEAD")
        git(work, "push", "-q", "origin", "main")
        self.work = work

        # The node: a clone, a "venv" that is this interpreter, a database with
        # a meta table and nothing else (so control_state reads "unknown" and
        # nothing is paused — the pause is not what these tests are about).
        self.node_home.mkdir(parents=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.app)], check=True,
                       env={**os.environ, **GIT_ENV})
        (self.node_home / ".venv" / "bin").mkdir(parents=True)
        (self.node_home / ".venv" / "bin" / "python3").symlink_to(sys.executable)
        conn = sqlite3.connect(self.node_home / "sketchgen.db")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

    def pin(self, sha, reason="hardware A/B"):
        conn = sqlite3.connect(self.node_home / "sketchgen.db")
        conn.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                         [("pin.sha", sha), ("pin.reason", reason)])
        conn.commit()
        conn.close()

    def run_update(self, *args, env=None):
        environment = {
            **os.environ, **GIT_ENV,
            "HOME": str(self.home), "FAKE_LOG": str(self.log),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        for key in ("SKETCHGEN_RENDER", "SKETCHGEN_SKIP_RENDER", "SKETCHGEN_UPDATE_REEXEC",
                    "SKETCHGEN_UPDATE_FROM"):
            environment.pop(key, None)
        environment.update(env or {})
        return subprocess.run(["bash", str(self.app / "update.sh"), *args],
                              capture_output=True, text=True, env=environment,
                              timeout=120, check=False)

    def head(self):
        return git(self.app, "rev-parse", "HEAD")

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_node_following_main_fast_forwards_to_it(self):
        git(self.app, "reset", "-q", "--hard", self.one)
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.head(), self.three)
        self.assertEqual(git(self.app, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertIn("all done", done.stdout)

    def test_a_detached_node_with_no_pin_goes_to_main(self):
        git(self.app, "checkout", "-q", "--detach", self.one)
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.head(), self.three)

    def test_a_pinned_node_stays_on_its_pin_however_far_main_goes(self):
        git(self.app, "checkout", "-q", "--detach", self.one)
        self.pin(self.one)
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.head(), self.one)
        self.assertIn("pinned at", done.stdout)
        self.assertIn("hardware A/B", done.stdout)
        # it still did the rest of a deploy: units and the migration
        self.assertIn("sketchgen install-unit", self.calls())

    def test_a_node_pinned_elsewhere_is_moved_to_the_pin(self):
        self.pin(self.two)
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.head(), self.two)

    def test_a_pinned_node_that_cannot_reach_its_origin_still_deploys(self):
        git(self.app, "checkout", "-q", "--detach", self.one)
        self.pin(self.one)
        git(self.app, "remote", "set-url", "origin", str(self.origin) + "-gone")
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("going on, because this node is pinned", done.stdout)
        self.assertEqual(self.head(), self.one)

    def test_a_node_following_main_that_cannot_fetch_is_refused(self):
        git(self.app, "remote", "set-url", "origin", str(self.origin) + "-gone")
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 1)
        self.assertIn("git fetch failed", done.stderr)

    def test_a_dirty_tree_is_refused_before_anything_moves_or_pauses(self):
        git(self.app, "reset", "-q", "--hard", self.one)
        (self.app / "update.sh").write_text((self.app / "update.sh").read_text() + "# edit\n")
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 1)
        self.assertIn("nothing was moved or paused", done.stderr)
        self.assertEqual(self.head(), self.one)
        self.assertEqual(self.calls(), [])

    def test_a_foreign_branch_is_refused(self):
        git(self.app, "switch", "-q", "-c", "fix/on-the-node")
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not main", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_local_main_that_cannot_fast_forward_is_refused_unmoved(self):
        git(self.app, "reset", "-q", "--hard", self.one)
        (self.app / "local.txt").write_text("committed here\n")
        git(self.app, "add", "local.txt")
        git(self.app, "commit", "-q", "-m", "local")
        local = self.head()
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 1)
        self.assertIn("PR them first", done.stderr)
        self.assertEqual(self.head(), local)

    def test_a_second_update_is_refused_while_one_holds_the_lock(self):
        with open(self.node_home / "update.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 1)
        self.assertIn("another update.sh is running", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_render_auto_asks_about_the_whole_deploy_and_skips_on_skip(self):
        git(self.app, "reset", "-q", "--hard", self.one)
        done = self.run_update("--render=auto")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn(f"sketchgen build --needs-render {self.one} {self.three}", self.calls())
        self.assertIn("Skipping the gallery", done.stdout)
        self.assertFalse(any("publish-index" in c for c in self.calls()))

    def test_a_handed_over_copy_is_told_where_the_deploy_started(self):
        # main's newest commit changes update.sh itself: the first copy moves
        # the checkout, sees that, and execs the new one, which must diff from
        # where the FIRST copy started, not from where it finds itself.
        (self.work / "update.sh").write_text(UPDATE_SH.read_text() + "# a newer copy\n")
        git(self.work, "add", "update.sh")
        git(self.work, "commit", "-q", "-m", "four")
        four = git(self.work, "rev-parse", "HEAD")
        git(self.work, "push", "-q", "origin", "main")
        git(self.app, "reset", "-q", "--hard", self.one)
        done = self.run_update(env={"SKETCHGEN_RENDER": "auto"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("handing over to the new copy", done.stdout)
        self.assertIn(f"sketchgen build --needs-render {self.one} {four}", self.calls())

    def test_a_copy_handed_over_by_one_that_predates_auto_renders(self):
        git(self.app, "reset", "-q", "--hard", self.three)
        done = self.run_update(env={"SKETCHGEN_RENDER": "auto",
                                    "SKETCHGEN_UPDATE_REEXEC": "yes"})
        # it gets as far as the gallery pull, which is the render path, and
        # there is no gallery here: that failure is the proof it chose to render
        self.assertIn("did not say where it started", done.stdout)
        self.assertIn("Pulling gallery checkout", done.stdout)

    def test_skip_render_from_an_old_copy_does_not_override_auto(self):
        git(self.app, "reset", "-q", "--hard", self.one)
        done = self.run_update(env={"SKETCHGEN_RENDER": "auto", "SKETCHGEN_SKIP_RENDER": "no"})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(any("--needs-render" in c for c in self.calls()))

    def test_a_pending_migration_is_snapshotted_first_and_the_snapshot_kept(self):
        (self.work / "migrations").mkdir()
        (self.work / "migrations" / "007_something.sql").write_text("SELECT 1;\n")
        git(self.work, "add", "migrations")
        git(self.work, "commit", "-q", "-m", "a migration")
        git(self.work, "push", "-q", "origin", "main")
        conn = sqlite3.connect(self.node_home / "sketchgen.db")
        conn.execute("CREATE TABLE schema_version (version INTEGER, name TEXT, applied_utc TEXT)")
        conn.execute("INSERT INTO schema_version VALUES (6, 'six', 'x')")
        conn.execute("INSERT INTO meta VALUES ('marker', 'before')")
        conn.commit()
        conn.close()
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        snapshot = self.node_home / "sketchgen.db.pre-007"
        self.assertIn(f"snapshot before migrating: {snapshot}", done.stdout)
        copy = sqlite3.connect(snapshot)
        self.assertEqual(copy.execute("SELECT value FROM meta WHERE key = 'marker'").fetchone(),
                         ("before",))
        copy.close()
        # a rerun (the stub db init migrated nothing) keeps the first copy
        done = self.run_update("--no-render")
        self.assertIn("already here, and kept", done.stdout)

    def test_no_pending_migration_no_snapshot(self):
        done = self.run_update("--no-render")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(list(self.node_home.glob("sketchgen.db.pre-*")), [])

    def test_an_unknown_flag_is_refused_before_the_lock(self):
        done = self.run_update("--render=sometimes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("unknown argument", done.stderr)
        self.assertFalse((self.node_home / "update.lock").exists())


if __name__ == "__main__":
    unittest.main()
