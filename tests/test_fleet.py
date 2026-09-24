"""bin/fleet: every node at once, from the laptop (docs/plans/fleet.md, Packet 2).

Nothing here leaves this machine. `ssh` is a script that takes the host name,
finds a directory of that name under a scratch root, and runs the remote
command with HOME pointed at it — so the probe that `status` sends really runs,
against a real git clone and a real database, exactly as it would on a node.
`systemctl` and `systemd-run` are stubs that answer from, and write to, files
in that fake HOME. "Main" is a scratch origin (SKETCHGEN_FLEET_REPO), never
GitHub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sketchgen import db

REPO_ROOT = Path(__file__).resolve().parent.parent
FLEET = REPO_ROOT / "bin" / "fleet"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}

FAKE_SSH = """#!/usr/bin/env bash
while [ "$1" = "-o" ]; do shift 2; done
host=$1; shift
home="$FAKE_HOMES/$host"
if [ ! -d "$home" ]; then
    echo "ssh: connect to host $host port 22: Connection refused" >&2
    exit 255
fi
export HOME="$home"
exec bash -c "$1"
"""

#: is-active answers from $HOME/units ("name state" per line, default
#: inactive); everything else is written down and succeeds.
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$HOME/calls"
[ "$1" = "--user" ] && shift
if [ "$1" = "is-active" ]; then
    shift
    quiet=no; [ "$1" = "--quiet" ] && { quiet=yes; shift; }
    worst=0
    for unit in "$@"; do
        state=$(awk -v u="$unit" '$1 == u {print $2}' "$HOME/units" 2>/dev/null)
        state=${state:-inactive}
        [ "$quiet" = yes ] || echo "$state"
        [ "$state" = active ] || worst=3
    done
    exit $worst
fi
exit 0
"""

FAKE_SYSTEMD_RUN = """#!/usr/bin/env bash
for a in "$@"; do printf 'systemd-run-arg %s\\n' "$a"; done >> "$HOME/calls"
"""


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit(cwd, name, text, message) -> str:
    path = Path(cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)
    return git(cwd, "rev-parse", "HEAD")


@unittest.skipUnless(shutil.which("bash"), "the fake ssh is a bash script")
class FleetTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.base = base
        self.homes = base / "homes"
        self.homes.mkdir()
        self.bin = base / "bin"
        self.bin.mkdir()
        for name, body in (("ssh", FAKE_SSH), ("systemctl", FAKE_SYSTEMCTL),
                           ("systemd-run", FAKE_SYSTEMD_RUN)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

        # The origin: c1 predates the build verb, c2 brings it, c3 is main.
        self.origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env={**os.environ, **GIT_ENV})
        self.laptop = base / "laptop"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.laptop)], check=True,
                       env={**os.environ, **GIT_ENV})
        git(self.laptop, "remote", "add", "origin", str(self.origin))
        self.c1 = commit(self.laptop, "README.md", "one\n", "first")
        self.c2 = commit(self.laptop, "sketchgen/cli/build.py", "# marker\n", "pins")
        self.c3 = commit(self.laptop, "README.md", "three\n", "third")
        git(self.laptop, "push", "-q", "origin", "main")
        self.fleet_file = base / "fleet"
        self.nodes: list[str] = []

    def node(self, name, at=None, meta=None, jobs=None, units=None):
        home = self.homes / name
        app = home / "sketchgen" / "app"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(app)], check=True,
                       env={**os.environ, **GIT_ENV})
        if at:
            git(app, "checkout", "-q", "--detach", at)
        (home / "sketchgen" / ".venv" / "bin").mkdir(parents=True)
        (home / "sketchgen" / ".venv" / "bin" / "python3").symlink_to(sys.executable)
        path = home / "sketchgen" / "sketchgen.db"
        db.init(path)
        conn = db.connect(path)
        for key, value in (meta or {}).items():
            db.set_meta(conn, key, value)
        for state, count in (jobs or {}).items():
            for _ in range(count):
                conn.execute(
                    "INSERT INTO jobs (state, prompt, submitted_by, created_utc, "
                    "updated_utc) VALUES (?, 'p', 'octocat', 'x', 'x')", (state,))
        conn.close()
        (home / "units").write_text(
            "".join(f"{unit} {state}\n" for unit, state in (units or {}).items()))
        self.nodes.append(name)
        self.fleet_file.write_text("# name host\n" + "".join(
            f"{n} {n}\n" for n in self.nodes))
        return home

    def fleet(self, *args):
        env = {
            **os.environ, **GIT_ENV,
            "SKETCHGEN_SSH": str(self.bin / "ssh"),
            "SKETCHGEN_FLEET": str(self.fleet_file),
            "SKETCHGEN_FLEET_REPO": str(self.laptop),
            "FAKE_HOMES": str(self.homes),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        return subprocess.run([sys.executable, str(FLEET), *args], capture_output=True,
                              text=True, env=env, timeout=120, check=False)

    def stamped(self, sha):
        run = f"{sha} 2026-09-24T12:00:00Z"
        return {"meta": {"run.worker": run, "run.web": run},
                "units": {"sketchgen-worker.service": "active",
                          "sketchgen-web.service": "active"}}

    def calls(self, name):
        path = self.homes / name / "calls"
        return path.read_text().splitlines() if path.exists() else []

    # -- status --------------------------------------------------------------

    def test_every_node_on_main_and_running_it_is_exit_0(self):
        self.node("alpha", **self.stamped(self.c3))
        self.node("beta", **self.stamped(self.c3))
        done = self.fleet("status")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("1 build on 2 nodes", done.stdout)
        self.assertIn("all on target", done.stdout)

    def test_a_node_behind_main_is_named_with_how_far(self):
        self.node("alpha", **self.stamped(self.c3))
        self.node("beta", at=self.c2, **self.stamped(self.c2))
        done = self.fleet("status")
        self.assertEqual(done.returncode, 1)
        self.assertIn("2 builds on 2 nodes", done.stdout)
        self.assertRegex(done.stdout, r"beta\s+1 PR behind main")

    def test_main_is_the_laptops_not_what_a_stale_node_last_fetched(self):
        # the node's own origin/main is c2, which it is on: by its own lights
        # it is current, and that is the sld-cloud of 2026-09-24
        home = self.node("beta", at=self.c2, **self.stamped(self.c2))
        git(home / "sketchgen" / "app", "update-ref", "refs/remotes/origin/main", self.c2)
        done = self.fleet("status")
        self.assertRegex(done.stdout, r"beta\s+1 PR behind main")

    def test_a_node_that_predates_the_verb_is_read_and_flagged(self):
        self.node("old", at=self.c1)
        done = self.fleet("status", "--json")
        self.assertEqual(done.returncode, 1)
        row = json.loads(done.stdout)["nodes"][0]
        self.assertTrue(row["reachable"])
        self.assertFalse(row["doc"]["knows_pins"])
        self.assertIn("2 PRs behind main", row["problems"])
        self.assertTrue(any("predates build stamps and pins" in p for p in row["problems"]))

    def test_an_unreachable_node_is_a_row_not_a_hang(self):
        self.node("alpha", **self.stamped(self.c3))
        self.fleet_file.write_text("alpha alpha\nghost ghost-host\n")
        done = self.fleet("status")
        self.assertEqual(done.returncode, 1)
        self.assertRegex(done.stdout, r"ghost\s+unreachable: ssh: connect to host ghost-host")

    def test_same_wants_one_build_even_when_every_node_is_on_target(self):
        self.node("alpha", **self.stamped(self.c3))
        pinned = self.stamped(self.c2)
        pinned["meta"]["pin.sha"] = self.c2
        self.node("arm", at=self.c2, **pinned)
        self.assertEqual(self.fleet("status").returncode, 0)
        done = self.fleet("status", "--same")
        self.assertEqual(done.returncode, 1)
        self.assertIn("not on one build", done.stdout)

    # -- update --------------------------------------------------------------

    def test_update_refuses_a_node_with_a_batch_in_hand_and_starts_nothing(self):
        self.node("busy", jobs={"queued": 3, "executing": 1}, **self.stamped(self.c3))
        done = self.fleet("update", "busy")
        self.assertEqual(done.returncode, 3)
        self.assertIn("3 queued, 1 in flight", done.stdout)
        self.assertFalse(any("systemd-run" in c for c in self.calls("busy")))

    def test_update_refuses_a_node_that_predates_pins_unless_adopted(self):
        self.node("old", at=self.c1)
        done = self.fleet("update", "old")
        self.assertEqual(done.returncode, 3)
        self.assertIn("predates pins", done.stdout)
        self.assertIn("detached", done.stdout)
        self.assertFalse(any("systemd-run" in c for c in self.calls("old")))

        done = self.fleet("update", "old", "--adopt")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("started:", done.stdout)
        args = [c.split(" ", 1)[1] for c in self.calls("old") if c.startswith("systemd-run-arg")]
        self.assertIn("--unit=sketchgen-update", args)
        self.assertIn("--setenv=SKETCHGEN_RENDER=auto", args)
        # nothing on update.sh's own command line: an old copy dies on a flag
        # it does not know, and an adopted node runs the old copy first
        command = args[-1]
        self.assertIn('bash "$HOME/sketchgen/app/update.sh";', command)

    def test_render_no_reaches_an_old_update_sh_too(self):
        self.node("alpha", **self.stamped(self.c3))
        self.assertEqual(self.fleet("update", "alpha", "--render", "no").returncode, 0)
        args = [c.split(" ", 1)[1] for c in self.calls("alpha") if c.startswith("systemd-run-arg")]
        self.assertIn("--setenv=SKETCHGEN_SKIP_RENDER=1", args)

    def test_a_node_already_updating_is_left_alone(self):
        running = self.stamped(self.c3)
        running["units"]["sketchgen-update.service"] = "active"
        self.node("alpha", **running)
        done = self.fleet("update", "alpha")
        self.assertEqual(done.returncode, 3)
        self.assertIn("already running", done.stdout)

    def test_update_needs_names_or_all_but_not_both(self):
        self.node("alpha", **self.stamped(self.c3))
        self.assertEqual(self.fleet("update").returncode, 3)
        self.assertEqual(self.fleet("update", "alpha", "--all").returncode, 3)
        self.assertEqual(self.fleet("update", "--all", "--dry-run").returncode, 0)

    # -- watch ---------------------------------------------------------------

    def test_watch_reports_how_each_update_ended(self):
        home = self.node("alpha", **self.stamped(self.c3))
        logs = home / "sketchgen" / "logs"
        logs.mkdir()
        (logs / "update-20260924T120000Z.log").write_text(
            "\x1b[1;36m→ Resuming\x1b[0m\n✓ all done\n[fleet] update.sh exited 0\n")
        done = self.fleet("watch", "alpha")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"alpha\s+done\s+✓ all done")

        (logs / "update-20260924T130000Z.log").write_text(
            "FAILED: git fetch failed\n[fleet] update.sh exited 1\n")
        done = self.fleet("watch", "alpha")
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAILED (exit 1)", done.stdout)

    # -- pin -----------------------------------------------------------------

    def test_pin_sends_a_full_sha_and_a_node_without_the_verb_is_named(self):
        home = self.node("old", at=self.c1)
        # a stand-in bin/sketchgen that says what an old one says to `pin`
        sg = home / "sketchgen" / "app" / "bin" / "sketchgen"
        sg.parent.mkdir(parents=True)
        sg.write_text("import sys\nprint(\"sketchgen: error: argument command: invalid "
                      "choice: 'pin'\", file=sys.stderr)\nsys.exit(2)\n")
        done = self.fleet("pin", "old", "--to", self.c2[:7], "--reason", "A/B")
        self.assertEqual(done.returncode, 1)
        self.assertIn("predates pins", done.stdout)

    def test_pin_refuses_a_ref_this_checkout_does_not_have(self):
        self.node("alpha", **self.stamped(self.c3))
        done = self.fleet("pin", "alpha", "--to", "0" * 40, "--reason", "x")
        self.assertEqual(done.returncode, 3)

    def test_no_fleet_file_is_a_refusal_that_says_where(self):
        self.fleet_file = self.base / "missing"
        done = self.fleet("status")
        self.assertEqual(done.returncode, 3)
        self.assertIn("fleet.example", done.stderr)


if __name__ == "__main__":
    unittest.main()
