"""Unit tests for the publisher.

Nothing here touches GitHub, ~/.ssh, or any real gallery. Every test builds a
bare repository with ``git init --bare`` in a temp directory, clones it as the
gallery checkout, and gives that clone a throwaway ``user.name``/``user.email``
locally — the identity never leaves the temp directory and no global git config
is read or written. No key is generated: the remotes here are local paths, so
the publisher takes its no-key path.

Run:  python3 -m unittest discover -s tests -v
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import db  # noqa: E402
from sketchgen import publish  # noqa: E402

CLI = REPO_ROOT / "bin" / "sketchgen"

#: Packet 3.1 has not landed yet; when it does, the "no generator" path below
#: stops being reachable and that one test skips itself rather than failing.
GENERATOR_PRESENT = importlib.util.find_spec("sketchgen.gallery") is not None

PROMPT = "a slow field of drifting particles that answer the mouse"
EXECUTOR = "qwen3-coder:30b-a3b-q4_K_M"

FILES = {
    "index.html": "<!doctype html>\n<html><body><main></main></body></html>\n",
    "sketch.js": "function setup(){createCanvas(600,600);}\nfunction draw(){}\n",
    "meta.json": '{"executor": "%s", "submitted_by": "profcarroll"}\n' % EXECUTOR,
    "statement.md": "# what it does\n\nIt drifts.\n",
}
STRIP_PNG = b"\x89PNG\r\n\x1a\n" + b"strip-bytes-not-a-real-png" * 4


def git(cwd, *args, check=True):
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result


def run_cli(*args):
    """Run the CLI in a subprocess so the exit codes are the real ones."""
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ),
    )


class PublishTestCase(unittest.TestCase):
    """A bare remote, a gallery clone, an app database with one held entry."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:  # pragma: no cover
            raise unittest.SkipTest("git is not on PATH")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-publish-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        self.bare = self.tmp / "remote.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.bare))

        # Seed the remote with one commit, so the clone below gets an
        # origin/HEAD and the publisher can tell what the default branch is.
        seed = self.tmp / "seed"
        git(self.tmp, "clone", str(self.bare), str(seed))
        self._identify(seed)
        (seed / "README.md").write_text("sketchgen gallery\n", encoding="utf-8")
        git(seed, "add", "README.md")
        git(seed, "commit", "-m", "seed the gallery")
        git(seed, "push", "-u", "origin", "main")

        self.gallery = self.tmp / "gallery"
        git(self.tmp, "clone", str(self.bare), str(self.gallery))
        self._identify(self.gallery)

        self.db_path = self.tmp / "app.db"
        db.init(self.db_path)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.job_id = db.enqueue(self.conn, PROMPT, "profcarroll")
        self.entry_id = db.create_entry(
            self.conn,
            self.job_id,
            state="held",
            prompt=PROMPT,
            executor=EXECUTOR,
            submitted_by="profcarroll",
        )
        self.source = self.tmp / "source"
        self.source.mkdir()
        for name, text in FILES.items():
            (self.source / name).write_text(text, encoding="utf-8")
        (self.source / "strip.png").write_bytes(STRIP_PNG)

    @staticmethod
    def _identify(checkout):
        """A throwaway identity, local to this checkout only."""
        git(checkout, "config", "user.name", "sketchgen test")
        git(checkout, "config", "user.email", "sketchgen-test@example.invalid")

    # -- helpers ----------------------------------------------------------

    def publish_cli(self, *extra, entry_id=None):
        return run_cli(
            "publish",
            str(self.entry_id if entry_id is None else entry_id),
            "--db",
            str(self.db_path),
            "--gallery-dir",
            str(self.gallery),
            "--from",
            str(self.source),
            *extra,
        )

    def entry_row(self):
        return self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (self.entry_id,)
        ).fetchone()

    def head(self, repo):
        return git(repo, "rev-parse", "HEAD").stdout.strip()


class DryRunTests(PublishTestCase):
    def test_dry_run_prints_the_plan_and_changes_nothing(self):
        before_local = self.head(self.gallery)
        before_remote = self.head(self.bare)
        result = self.publish_cli("--dry-run", "--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("index.html", "sketch.js", "strip.png", "meta.json",
                     "statement.md"):
            self.assertIn(name, result.stdout)
        self.assertIn(f"entry {self.entry_id}: {PROMPT[:60]}", result.stdout)
        self.assertIn(f"Co-Authored-By: {EXECUTOR} <noreply@localhost>",
                      result.stdout)
        self.assertIn("Published-By: profcarroll", result.stdout)
        self.assertIn("would run: git push", result.stdout)
        self.assertIn(f"/e/{self.entry_id}/", result.stdout)
        self.assertEqual(self.head(self.gallery), before_local)
        self.assertEqual(self.head(self.bare), before_remote)
        self.assertFalse((self.gallery / "e").exists())
        self.assertEqual(self.entry_row()["state"], "held")

    def test_dry_run_masks_the_key_path_for_an_ssh_remote(self):
        key = self.tmp / "fake-key"
        key.write_text("not a key\n", encoding="utf-8")
        result = self.publish_cli(
            "--dry-run",
            "--remote",
            "git@github.com:profcarroll/sketchgen-gallery.git",
            "--key",
            str(key),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ssh -i <key> -o IdentitiesOnly=yes", result.stdout)
        self.assertIn("StrictHostKeyChecking=accept-new", result.stdout)
        self.assertNotIn(str(key), result.stdout)


class PublishTests(PublishTestCase):
    def test_publish_commits_pushes_and_records_the_sha(self):
        before = self.head(self.bare)
        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertNotEqual(self.head(self.bare), before)
        self.assertEqual(self.head(self.bare), self.head(self.gallery))
        # First stdout line is the entry commit; when the generator is present a
        # second "gallery index" commit follows it, so the entry commit is an
        # ancestor of HEAD rather than HEAD itself.
        entry_sha = result.stdout.splitlines()[0]
        self.assertEqual(
            0, git(self.gallery, "merge-base", "--is-ancestor", entry_sha, "HEAD").returncode
        )
        self.assertIn(f"/e/{self.entry_id}/", result.stdout)

        listing = git(self.bare, "ls-tree", "-r", "--name-only", "HEAD").stdout
        for name in ("index.html", "sketch.js", "strip.png", "meta.json",
                     "statement.md"):
            self.assertIn(f"e/{self.entry_id}/{name}", listing)

        message = git(self.gallery, "log", "-1", "--pretty=%B", entry_sha).stdout
        self.assertIn(f"entry {self.entry_id}: {PROMPT[:60]}", message)
        self.assertIn(f"Co-Authored-By: {EXECUTOR} <noreply@localhost>", message)
        self.assertIn("Published-By: profcarroll", message)

        author = git(self.gallery, "log", "-1", "--pretty=%an", entry_sha).stdout.strip()
        self.assertEqual(author, "sketchgen test")

        row = self.entry_row()
        self.assertEqual(row["state"], "published")
        self.assertEqual(row["publish_commit"], entry_sha)
        self.assertTrue(row["published_utc"].endswith("Z"), row["published_utc"])

    def test_publishing_twice_refuses(self):
        self.assertEqual(self.publish_cli().returncode, 0)
        again = self.publish_cli()
        self.assertEqual(again.returncode, 3, again.stdout)
        self.assertIn("published", again.stderr)
        self.assertEqual(len(again.stderr.strip().splitlines()), 1)

    def test_failed_kept_entries_publish_too(self):
        self.conn.execute(
            "UPDATE entries SET state = 'failed-kept' WHERE id = ?", (self.entry_id,)
        )
        result = self.publish_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entry_row()["state"], "published")

    def test_rejected_entries_do_not_publish(self):
        self.conn.execute(
            "UPDATE entries SET state = 'rejected' WHERE id = ?", (self.entry_id,)
        )
        result = self.publish_cli()
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(self.head(self.gallery), self.head(self.bare))


class PushFailureTests(PublishTestCase):
    def test_a_failed_push_leaves_the_checkout_and_the_database_alone(self):
        before = self.head(self.gallery)
        git(self.gallery, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        result = self.publish_cli()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("publish failed", result.stderr)
        self.assertEqual(self.head(self.gallery), before)
        self.assertFalse((self.gallery / "e" / str(self.entry_id)).exists())
        self.assertEqual(git(self.gallery, "status", "--porcelain").stdout, "")
        row = self.entry_row()
        self.assertEqual(row["state"], "held")
        self.assertIsNone(row["publish_commit"])
        self.assertIsNone(row["published_utc"])


class GuardTests(PublishTestCase):
    def test_an_email_in_a_source_file_refuses(self):
        (self.source / "statement.md").write_text(
            "made by someone@example.com\n", encoding="utf-8"
        )
        result = self.publish_cli()
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("statement.md", result.stderr)
        self.assertEqual(self.entry_row()["state"], "held")
        self.assertFalse((self.gallery / "e").exists())

    def test_the_node_hostname_token_refuses(self):
        (self.source / "meta.json").write_text(
            '{"host": "instance-20250901-1200"}\n', encoding="utf-8"
        )
        result = self.publish_cli()
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("meta.json", result.stderr)
        self.assertEqual(self.entry_row()["state"], "held")

    def test_a_dirty_checkout_refuses(self):
        (self.gallery / "stray.txt").write_text("left lying around\n", encoding="utf-8")
        result = self.publish_cli()
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertEqual(self.entry_row()["state"], "held")

    def test_a_checkout_off_its_default_branch_refuses(self):
        git(self.gallery, "checkout", "-b", "somewhere-else")
        result = self.publish_cli()
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("default branch", result.stderr)

    def test_a_directory_that_is_not_a_git_repository_refuses(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        result = run_cli(
            "publish", str(self.entry_id), "--db", str(self.db_path),
            "--gallery-dir", str(plain), "--from", str(self.source),
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("not a git repository", result.stderr)

    @unittest.skipIf(
        GENERATOR_PRESENT,
        "packet 3.1's generator is present, so this path no longer refuses",
    )
    def test_without_from_and_without_the_generator_it_refuses(self):
        result = run_cli(
            "publish", str(self.entry_id), "--db", str(self.db_path),
            "--gallery-dir", str(self.gallery),
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn("generator not present; pass --from", result.stderr)

    def test_a_missing_database_refuses_in_one_line(self):
        result = run_cli(
            "publish", "999", "--db", str(self.tmp / "nothing.db"),
            "--gallery-dir", str(self.gallery),
        )
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertFalse((self.tmp / "nothing.db").exists())


class RejectTests(PublishTestCase):
    def test_reject_moves_held_to_rejected_and_touches_no_git(self):
        before = self.head(self.gallery)
        result = run_cli(
            "reject", str(self.entry_id), "--db", str(self.db_path),
            "--reason", "off brief",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entry_row()["state"], "rejected")
        self.assertEqual(self.head(self.gallery), before)
        self.assertFalse((self.gallery / "e").exists())
        job = db.get_job(self.conn, self.job_id)
        self.assertIn("off brief", job.last_error)

    def test_rejecting_a_published_entry_refuses(self):
        self.assertEqual(self.publish_cli().returncode, 0)
        result = run_cli("reject", str(self.entry_id), "--db", str(self.db_path))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(self.entry_row()["state"], "published")


if __name__ == "__main__":
    unittest.main()
