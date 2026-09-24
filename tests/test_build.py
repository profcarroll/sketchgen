"""sketchgen/build.py and the verbs over it: which build a node is on, is meant
to be on, and is running (docs/plans/fleet.md, Packet 1).

Every repository here is a scratch one made in setUp, with its own bare
"origin", so nothing touches GitHub or this checkout's refs. The one exception
is the render closure, which is read from this checkout's own source files —
that is the thing it has to be right about.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sketchgen import build
from sketchgen import db

REPO_ROOT = Path(__file__).resolve().parent.parent
SKETCHGEN = REPO_ROOT / "bin" / "sketchgen"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def commit(cwd, name, text="x\n", message=None) -> str:
    path = Path(cwd) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message or f"change {name}")
    return git(cwd, "rev-parse", "HEAD")


class ScratchRepo:
    """An origin (bare), a working clone that pushes to it, and helpers.

    History on main, oldest first: ``c1`` (no build verb yet), ``c2`` (adds
    sketchgen/cli/build.py, so a checkout there "knows pins"), then a PR merge
    ``pr7`` carrying two commits, then ``c4``. Behind counts are PRs: c2 is two
    behind main (pr7, c4), not four.
    """

    def __init__(self, base: Path):
        self.origin = base / "origin.git"
        self.work = base / "work"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True, env={**os.environ, **GIT_ENV})
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)],
                       check=True, env={**os.environ, **GIT_ENV})
        git(self.work, "remote", "add", "origin", str(self.origin))
        self.c1 = commit(self.work, "README.md", "one\n", "first")
        self.c2 = commit(self.work, "sketchgen/cli/build.py", "# marker\n", "pins")
        git(self.work, "switch", "-q", "-c", "feature")
        commit(self.work, "a.txt", "a\n", "a")
        commit(self.work, "b.txt", "b\n", "b")
        git(self.work, "switch", "-q", "main")
        git(self.work, "merge", "-q", "--no-ff", "feature", "-m",
            "Merge pull request #7 from someone/feature\n\nThe seventh thing")
        self.pr7 = git(self.work, "rev-parse", "HEAD")
        self.c4 = commit(self.work, "README.md", "four\n", "fourth")
        git(self.work, "push", "-q", "origin", "main")

    def clone(self, where: Path, at: str | None = None) -> Path:
        subprocess.run(["git", "clone", "-q", str(self.origin), str(where)], check=True,
                       env={**os.environ, **GIT_ENV})
        if at:
            git(where, "checkout", "-q", "--detach", at)
        return where


def make_db(path: Path, meta: dict[str, str] | None = None,
            jobs: dict[str, int] | None = None) -> Path:
    db.init(path)
    conn = db.connect(path)
    for key, value in (meta or {}).items():
        db.set_meta(conn, key, value)
    for state, count in (jobs or {}).items():
        for _ in range(count):
            conn.execute(
                "INSERT INTO jobs (state, prompt, submitted_by, created_utc, updated_utc) "
                "VALUES (?, 'p', 'octocat', '2026-09-24T00:00:00Z', '2026-09-24T00:00:00Z')",
                (state,))
    conn.close()
    return path


class GitFacts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = ScratchRepo(self.base)

    def test_checkout_says_branch_detached_and_dirty(self):
        node = self.repo.clone(self.base / "node")
        here = build.checkout(node)
        self.assertEqual(here["sha"], self.repo.c4)
        self.assertEqual(here["branch"], "main")
        self.assertFalse(here["detached"])
        self.assertFalse(here["dirty"])
        self.assertEqual(here["build"], self.repo.c4)

        git(node, "checkout", "-q", "--detach", self.repo.c2)
        here = build.checkout(node)
        self.assertIsNone(here["branch"])
        self.assertTrue(here["detached"])

        (node / "README.md").write_text("edited on the node\n", encoding="utf-8")
        here = build.checkout(node)
        self.assertTrue(here["dirty"])
        self.assertEqual(here["build"], f"{self.repo.c2}-dirty")
        self.assertEqual(build.short(here["build"]), f"{self.repo.c2[:7]}-dirty")

    def test_an_untracked_file_is_not_dirt(self):
        node = self.repo.clone(self.base / "node")
        (node / "notes.txt").write_text("left here\n", encoding="utf-8")
        self.assertFalse(build.checkout(node)["dirty"])

    def test_behind_counts_prs_on_the_first_parent_line_not_commits(self):
        work = self.repo.work
        self.assertEqual(build.relation(work, self.repo.c2, self.repo.c4),
                         {"on_main": True, "behind": 2, "ahead": 0})
        self.assertEqual(build.relation(work, self.repo.c4, self.repo.c4)["behind"], 0)
        # ahead of a stale fetch: still on main, not behind
        rel = build.relation(work, self.repo.c4, self.repo.c2)
        self.assertEqual((rel["on_main"], rel["behind"], rel["ahead"]), (True, 0, 2))

    def test_a_commit_main_never_had_is_off_main(self):
        work = self.repo.work
        git(work, "switch", "-q", "-c", "local", self.repo.c2)
        stray = commit(work, "stray.txt", "committed on the node\n", "stray")
        self.assertEqual(build.relation(work, stray, self.repo.c4)["on_main"], False)
        self.assertIsNone(build.relation(work, None, self.repo.c4)["on_main"])

    def test_commits_are_named_by_the_pr_title(self):
        found = build.commits(self.repo.work, self.repo.c2, self.repo.c4)
        self.assertEqual([(c["pr"], c["title"]) for c in found],
                         [(None, "fourth"), ("7", "The seventh thing")])

    def test_resolve_gives_a_full_sha_or_none(self):
        self.assertEqual(build.resolve(self.repo.work, self.repo.c2[:7]), self.repo.c2)
        self.assertEqual(build.resolve(self.repo.work, "main"), self.repo.c4)
        self.assertIsNone(build.resolve(self.repo.work, "no-such-ref"))

    def test_fetch_reports_failure_in_words_and_never_raises(self):
        node = self.repo.clone(self.base / "node")
        self.assertIsNone(build.fetch(node))
        git(node, "remote", "set-url", "origin", str(self.base / "gone.git"))
        error = build.fetch(node)
        self.assertTrue(error and error.startswith("git fetch"), error)


class Reading(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = ScratchRepo(self.base)
        self.node = self.repo.clone(self.base / "node")
        self.db = self.base / "sketchgen.db"

    def read(self, units=None, **meta):
        make_db(self.db, {k.replace("_", "."): v for k, v in meta.items()})
        return build.reading(self.node, self.db, units=units or {})

    def test_on_main_with_both_processes_on_it_is_on_target(self):
        run = f"{self.repo.c4} 2026-09-24T10:00:00Z"
        doc = self.read(units={"worker": "active", "web": "active"},
                        run_worker=run, run_web=run)
        self.assertTrue(doc["on_target"], doc["problems"])
        self.assertEqual(doc["target"]["kind"], "main")
        self.assertTrue(doc["knows_pins"])
        conn = db.connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(doc["schema"], db.schema_version(conn))

    def test_behind_main_says_how_many_prs(self):
        git(self.node, "checkout", "-q", "--detach", self.repo.c2)
        doc = self.read()
        self.assertFalse(doc["on_target"])
        self.assertIn("2 PRs behind main", doc["problems"])

    def test_a_pinned_node_at_its_pin_is_on_target_however_far_main_has_gone(self):
        git(self.node, "checkout", "-q", "--detach", self.repo.c2)
        doc = self.read(pin_sha=self.repo.c2, pin_reason="hardware A/B",
                        pin_by="octocat", pin_utc="2026-09-23T00:00:00Z")
        self.assertTrue(doc["on_target"], doc["problems"])
        self.assertEqual(doc["target"]["reason"], "hardware A/B")
        self.assertEqual(doc["main"]["behind"], 2)
        self.assertIn('pinned', build.render(doc))

    def test_a_pinned_node_off_its_pin_is_not(self):
        doc = self.read(pin_sha=self.repo.c2)
        self.assertIn(f"not at its pin {self.repo.c2[:7]}", doc["problems"])

    def test_a_worker_on_older_code_than_its_checkout_is_a_restart_pending(self):
        doc = self.read(units={"worker": "active", "web": "inactive"},
                        run_worker=f"{self.repo.c2} 2026-09-24T09:00:00Z")
        self.assertEqual(len(doc["problems"]), 1, doc["problems"])
        self.assertIn("restart pending", doc["problems"][0])
        self.assertIn(self.repo.c2[:7], doc["problems"][0])

    def test_a_live_worker_that_never_stamped_is_not_trusted(self):
        doc = self.read(units={"worker": "active"})
        self.assertIn("worker build unrecorded (it predates build stamps)", doc["problems"])
        # …but a stopped one is not asked about
        self.assertTrue(self.read(units={"worker": "inactive"})["on_target"])

    def test_dirt_and_a_foreign_branch_are_problems(self):
        git(self.node, "switch", "-q", "-c", "hotfix")
        (self.node / "README.md").write_text("hand edit\n", encoding="utf-8")
        problems = self.read()["problems"]
        self.assertIn("on branch hotfix, not main", problems)
        self.assertTrue(any(p.startswith("dirty tree") for p in problems))

    def test_work_in_hand_is_counted_by_kind(self):
        make_db(self.db, jobs={"queued": 2, "executing": 1, "needs-laptop": 1, "held": 5})
        work = build.reading(self.node, self.db, units={})["work"]
        self.assertEqual((work["queued"], work["in_flight"], work["parked"]), (2, 1, 1))

    def test_a_checkout_before_the_verb_does_not_know_pins(self):
        git(self.node, "checkout", "-q", "--detach", self.repo.c1)
        self.assertFalse(self.read()["knows_pins"])

    def test_no_database_is_a_reading_not_a_crash(self):
        doc = build.reading(self.node, self.base / "none.db", units={})
        self.assertFalse(doc["database"])
        self.assertIsNone(doc["control"])
        self.assertIn("generator ?", build.render(doc))

    def test_the_probe_runs_from_stdin_with_no_package_to_import(self):
        make_db(self.db)
        done = subprocess.run(
            [sys.executable, "-", "--root", str(self.node), "--db", str(self.db)],
            input=(REPO_ROOT / "sketchgen" / "build.py").read_text(encoding="utf-8"),
            capture_output=True, text=True, cwd=self.base, check=False,
            env={**os.environ, "PYTHONPATH": ""},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        doc = json.loads(done.stdout)
        self.assertEqual(doc["checkout"]["sha"], self.repo.c4)
        self.assertTrue(doc["knows_pins"])


class RunningBuild(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.multiple(build, _RUNNING=None, _RUNNING_SET=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_environment_wins_and_the_answer_is_kept(self):
        with mock.patch.dict(os.environ, {"SKETCHGEN_BUILD": "abc1234"}):
            self.assertEqual(build.running(), "abc1234")
        # asked again with the variable gone: still the first answer
        self.assertEqual(build.running(), "abc1234")

    def test_stamp_running_writes_the_build_and_when(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db.init(path)
            conn = db.connect(path)
            with mock.patch.dict(os.environ, {"SKETCHGEN_BUILD": "abc1234"}):
                self.assertEqual(build.stamp_running(conn, "worker"), "abc1234")
            value = db.get_meta(conn, "run.worker")
            conn.close()
        self.assertRegex(value, r"^abc1234 \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class RenderVerdict(unittest.TestCase):
    """Against this checkout's own render path."""

    def verdict(self, *paths):
        return build.render_verdict(REPO_ROOT, paths)[0]

    def test_the_closure_is_what_step_4_imports(self):
        closure = build.render_closure(REPO_ROOT)
        for module in ("sketchgen.gallery", "sketchgen.publish", "sketchgen.webimg",
                       "sketchgen.qr", "sketchgen.ghostshim", "sketchgen.db"):
            self.assertIn(module, closure)
        for module in ("sketchgen.web", "sketchgen.console", "sketchgen.cli.paid"):
            self.assertNotIn(module, closure)

    def test_what_renders_and_what_does_not(self):
        cases = {
            ("sketchgen/gallery.py",): True,
            ("sketchgen/templates/entry.html",): True,
            ("sketchgen/assets/kiosk.js",): True,
            ("migrations/019_something.sql",): True,
            ("somewhere/new.txt",): True,
            ("sketchgen/web.py", "sketchgen/console.py"): False,
            ("sketchgen/templates/op_layout.html",): False,
            ("gate/sketch_gate.py", "prompts/critic.md"): False,
            ("docs/plans/fleet.md", "tests/test_build.py", "AGENTS.md"): False,
            ("systemd/sketchgen-web.service", "update.sh", "bin/fleet"): False,
            (): False,
        }
        for paths, renders in cases.items():
            with self.subTest(paths=paths):
                self.assertEqual(self.verdict(*paths), renders)

    def test_one_page_path_among_many_is_enough(self):
        renders, why = build.render_verdict(
            REPO_ROOT, ["docs/a.md", "sketchgen/web.py", "sketchgen/templates/grid.html"])
        self.assertTrue(renders)
        self.assertIn("grid.html", why)


class Migration018(unittest.TestCase):

    def test_attempts_and_plans_carry_a_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db.init(path)
            conn = db.connect(path)
            job_id = db.enqueue(conn, "p", "octocat")
            db.add_attempt(conn, job_id, build="abc1234")
            self.assertEqual(db.list_attempts(conn, job_id)[0].build, "abc1234")
            conn.execute("UPDATE jobs SET state = 'planning' WHERE id = ?", (job_id,))
            job = db.transition(conn, job_id, "executing", brief="b", plan_build="def5678")
            self.assertEqual(job.plan_build, "def5678")
            conn.close()


class Verbs(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repo = ScratchRepo(self.base)
        self.node = self.repo.clone(self.base / "node")
        self.db = make_db(self.base / "sketchgen.db")

    def sg(self, *args):
        return subprocess.run(
            [sys.executable, str(SKETCHGEN), *args, "--db", str(self.db),
             "--root", str(self.node)],
            capture_output=True, text=True, check=False,
            env={**os.environ, **GIT_ENV},
        )

    def meta(self, key):
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        conn.close()
        return row[0] if row else None

    def test_build_exits_0_on_target_and_1_off_it(self):
        done = self.sg("build")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.rstrip().endswith("on target"))
        git(self.node, "checkout", "-q", "--detach", self.repo.c2)
        done = self.sg("build", "--json")
        self.assertEqual(done.returncode, 1)
        self.assertIn("2 PRs behind main", json.loads(done.stdout)["problems"])

    def test_a_pin_needs_a_reason_and_a_login(self):
        done = self.sg("pin", self.repo.c2[:7], "--by", "octocat")
        self.assertEqual(done.returncode, 3)
        self.assertIsNone(self.meta("pin.sha"))

    def test_a_pin_is_a_full_sha_and_clear_removes_it(self):
        done = self.sg("pin", self.repo.c2[:7], "--reason", "hardware A/B", "--by", "octocat")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.meta("pin.sha"), self.repo.c2)
        self.assertIn("moves it to", done.stdout)
        self.assertIn(f'pinned {self.repo.c2[:7]} — "hardware A/B"', self.sg("pin").stdout)
        done = self.sg("pin", "--clear", "--by", "octocat")
        self.assertEqual(done.returncode, 0)
        self.assertIsNone(self.meta("pin.sha"))
        self.assertIn("no pin", self.sg("pin").stdout)

    def test_a_ref_nobody_has_is_refused(self):
        done = self.sg("pin", "0" * 40, "--reason", "r", "--by", "octocat")
        self.assertEqual(done.returncode, 3)
        self.assertIsNone(self.meta("pin.sha"))

    def test_needs_render_is_one_word_and_a_reason(self):
        done = self.sg("build", "--needs-render", self.repo.c2, self.repo.c4)
        self.assertEqual(done.returncode, 0, done.stderr)
        # README.md and two top-level .txt files: the .txt ones are unknown
        self.assertTrue(done.stdout.startswith("render "), done.stdout)
        done = self.sg("build", "--needs-render", self.repo.c4, self.repo.c4)
        self.assertEqual(done.stdout.strip(), "skip nothing changed")


if __name__ == "__main__":
    unittest.main()
