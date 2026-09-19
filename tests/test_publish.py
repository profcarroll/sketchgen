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
import threading
import time
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
        # A real held entry belongs to a held job; the publisher moves both.
        self.conn.execute("UPDATE jobs SET state = 'held' WHERE id = ?", (self.job_id,))
        self.conn.commit()
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
        # The job's row follows its entry: the queue must not say "held".
        self.assertEqual(db.get_job(self.conn, self.job_id).state, "published")
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
        self.assertEqual(self.entry_row()["state"], "failed-kept")
        self.assertIsNotNone(self.entry_row()["publish_commit"])

    def test_rejected_entries_publish_and_keep_their_state(self):
        # The lineage ledger's §5.2 turned this test around: an operator
        # rejection used to be refused here and so stayed invisible with all
        # of its files still on disk. It now publishes, onto the rejections
        # page, and keeps the state that puts it there rather than becoming
        # 'published'.
        self.conn.execute(
            "UPDATE entries SET state = 'rejected', reject_reason = 'off brief' "
            "WHERE id = ?",
            (self.entry_id,),
        )
        result = self.publish_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.entry_row()["state"], "rejected")
        self.assertIsNotNone(self.entry_row()["publish_commit"])

    def test_archived_entries_do_not_publish(self):
        self.conn.execute(
            "UPDATE entries SET state = 'archived' WHERE id = ?", (self.entry_id,)
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


@unittest.skipUnless(
    GENERATOR_PRESENT, "this path is the generator's; without it publishing refuses"
)
class GeneratorStagingTests(PublishTestCase):
    """Publishing without --from: the generator renders into a staging directory.

    The staging directory is a fresh mkdtemp, so the generator cannot read the
    gallery's config out of the directory it is writing into the way it does
    everywhere else. The publisher hands it the checkout's config instead.
    Without that, write_path came out empty and every first-published page went
    to the site with no critique form — entry 531, 2026-09-16.
    """

    CONFIG = (
        '{\n'
        '  "gallery_url": "https://example.github.io/sketchgen-gallery/",\n'
        '  "repository": "https://github.com/example/sketchgen-gallery",\n'
        '  "write_path": "https://writepath.example"\n'
        '}\n'
    )

    def setUp(self):
        super().setUp()
        (self.gallery / "config.json").write_text(self.CONFIG, encoding="utf-8")
        git(self.gallery, "add", "config.json")
        git(self.gallery, "commit", "-m", "the checkout's config")

    def publish_generated(self, *extra):
        return run_cli(
            "publish", str(self.entry_id), "--db", str(self.db_path),
            "--gallery-dir", str(self.gallery), *extra,
        )

    def test_the_first_published_page_carries_the_critique_form(self):
        result = self.publish_generated("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        page = (self.gallery / "e" / str(self.entry_id) / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('<section class="panel critique-form"', page)
        self.assertIn(f'data-critique="{self.entry_id}"', page)

    def test_the_published_page_carries_the_stamps_the_push_left(self):
        # The page is rendered before the push, so the copy that lands in the
        # entry commit has no published date, no publish commit, and a lineage
        # ledger that calls the entry on the page "not published". The index
        # pass re-renders it once the row has both stamps.
        import json as _json

        result = self.publish_generated("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        entry_dir = self.gallery / "e" / str(self.entry_id)
        page = (entry_dir / "index.html").read_text(encoding="utf-8")
        row = self.entry_row()
        self.assertIsNotNone(row["publish_commit"])
        self.assertIn(row["publish_commit"], page)
        self.assertIn(row["published_utc"], page)
        # the entry's own ledger row no longer contradicts its state chip
        self.assertIn('<span class="chip published">published</span>', page)
        self.assertNotIn('<span class="chip unpublished">not published</span>', page)
        meta = _json.loads((entry_dir / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(row["publish_commit"], meta["publish_commit"])
        self.assertEqual(row["published_utc"], meta["published_utc"])

    def test_the_re_render_leaves_the_checkout_clean(self):
        result = self.publish_generated("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())
        self.assertEqual(self.head(self.gallery), self.head(self.bare))

    def test_the_generator_is_handed_the_checkout_s_write_path(self):
        from sketchgen import gallery

        seen = []
        original = gallery.render_entry

        def remember(conn, entry_id, dest, config=None, **kwargs):
            seen.append(config)
            return original(conn, entry_id, dest, config, **kwargs)

        gallery.render_entry = remember
        try:
            publish.publish(
                self.conn, self.entry_id, gallery_dir=self.gallery,
                remote=str(self.bare), by="profcarroll", key=None,
            )
        finally:
            gallery.render_entry = original
        self.assertTrue(seen, "the generator was never called")
        self.assertEqual("https://writepath.example", seen[0].write_path)


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
        self.assertEqual(job.state, "rejected")
        self.assertIn("off brief", job.last_error)

    def test_rejecting_a_published_entry_refuses(self):
        self.assertEqual(self.publish_cli().returncode, 0)
        result = run_cli("reject", str(self.entry_id), "--db", str(self.db_path))
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(self.entry_row()["state"], "published")


if __name__ == "__main__":
    unittest.main()


def cross_a_second() -> None:
    """Wait until the wall clock's second has ticked over. See the use below:
    the property under test is only observable either side of a boundary."""
    edge = int(time.time()) + 1
    while time.time() < edge:
        time.sleep(0.02)


class PublishIndexTests(PublishTestCase):
    def test_publish_index_records_the_write_path_and_pushes(self):
        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        before = self.head(self.bare)
        import json as _json
        from sketchgen import publish as publication
        sha, why = publication.publish_index(
            self.conn, self.gallery, remote=str(self.bare),
            write_path="https://writepath.example/",
        )
        self.assertIsNone(why, why)
        self.assertEqual(sha, self.head(self.gallery))
        self.assertEqual(self.head(self.bare), sha)
        self.assertNotEqual(before, sha)
        config = _json.loads((self.gallery / "config.json").read_text())
        self.assertEqual(config["write_path"], "https://writepath.example")
        # A second run with nothing new is a no-op, not a commit. Crossing a
        # second boundary first is the point: "site unchanged" is read off a
        # diff of the render against the checkout, so a render that is a
        # function of the clock rather than of the database makes this commit
        # and push a whole gallery for nothing. It used to, and because the
        # two renders usually fell inside one second this test only said so
        # about one run in ten.
        cross_a_second()
        sha2, why2 = publication.publish_index(
            self.conn, self.gallery, remote=str(self.bare),
            write_path="https://writepath.example/",
        )
        self.assertIsNone(sha2, "a re-render with no new data committed anyway")
        self.assertEqual(why2, "site unchanged")


    def test_publish_index_reports_every_step_to_the_bar(self):
        """update.sh draws a bar from on_step; a step it skips is a bar that lies."""
        import io
        from sketchgen import publish as publication
        from sketchgen.cli import publishindex
        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = []
        sha, why = publication.publish_index(
            self.conn, self.gallery, remote=str(self.bare),
            write_path="https://writepath.example/",
            on_step=lambda done, total, label: seen.append((done, total, label)),
        )
        self.assertIsNone(why, why)
        labels = [label for _, _, label in seen]
        self.assertEqual(
            labels,
            [f"entry {self.entry_id}", "index", "staging", "commit", "push", "pushed"],
        )
        total = 1 + 4
        self.assertTrue(all(t == total for _, t, _ in seen))
        self.assertEqual([d for d, _, _ in seen], [0, 1, 2, 3, 4, 5])
        # The bar itself: redrawn in place, ended once, and it says so.
        out = io.StringIO()
        draw = publishindex.progress_bar(out)
        for done, t, label in seen:
            draw(done, t, label)
        text = out.getvalue()
        self.assertEqual(text.count("\n"), 1)
        self.assertTrue(text.endswith("\n"))
        self.assertIn("100% 5/5", text)
        self.assertIn("· pushed", text.split("\r")[-1])
        # And when nothing changed it still ends the line rather than leaving
        # the cursor mid-bar for the next thing the deploy prints.
        seen.clear()
        publication.publish_index(
            self.conn, self.gallery, remote=str(self.bare),
            write_path="https://writepath.example/",
            on_step=lambda done, total, label: seen.append((done, total, label)),
        )
        self.assertEqual(seen[-1][2], "unchanged")
        out = io.StringIO()
        draw = publishindex.progress_bar(out)
        for done, t, label in seen:
            draw(done, t, label)
        self.assertTrue(out.getvalue().endswith("· unchanged\n"))

    def test_the_bar_line_shows_an_estimate_only_once_it_has_one(self):
        from sketchgen.cli import publishindex
        early = publishindex.bar_line(1, 500, "entry 3", 2.0)
        self.assertNotIn("left", early)
        self.assertTrue(early.startswith("[░"), early)
        mid = publishindex.bar_line(250, 500, "entry 300", 300.0)
        self.assertIn(" 50% 250/500 5:00 ~5:00 left · entry 300", mid)
        self.assertEqual(mid.count("█"), 15)
        end = publishindex.bar_line(500, 500, "pushed", 601.0)
        self.assertIn("100% 500/500 10:01 · pushed", end)
        self.assertNotIn("left", end)

    def test_publish_index_repairs_a_page_published_without_the_write_path(self):
        """The retroactive fix: 36 published entries, 518 through 563.

        They went to the site rendered against a config with no write_path, so
        every one of them has no critique form. Nothing re-rendered an entry
        page after its own publish commit, so nothing ever put one back. This
        is the command that does — the same one update.sh now runs on every
        deploy, by way of render-all. (A rejected entry has no form either,
        and should not: lineage.spawn refuses a rejected parent.)
        """
        from sketchgen import publish as publication

        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        page_path = self.gallery / "e" / str(self.entry_id) / "index.html"
        self.assertNotIn("critique-form", page_path.read_text(encoding="utf-8"))

        sha, why = publication.publish_index(
            self.conn, self.gallery, remote=str(self.bare),
            write_path="https://writepath.example",
        )
        self.assertIsNone(why, why)
        self.assertEqual(self.head(self.bare), sha)
        page = page_path.read_text(encoding="utf-8")
        self.assertIn('<section class="panel critique-form"', page)
        self.assertIn(f'data-critique="{self.entry_id}"', page)
        # and the stamps the push left, which the first render could not know
        self.assertIn(self.entry_row()["publish_commit"], page)

    def test_publish_index_leaves_an_unpublished_rejection_alone(self):
        # A gate rejection the worker kept is a person's to publish (spec §9).
        # The re-render must not put it on the site by the back door.
        kept_job = db.enqueue(self.conn, "a kept rejection", "profcarroll")
        self.conn.execute("UPDATE jobs SET state = 'failed' WHERE id = ?", (kept_job,))
        self.conn.commit()
        kept = db.create_entry(
            self.conn, kept_job, state="failed-kept", prompt="a kept rejection",
            executor=EXECUTOR, submitted_by="profcarroll",
        )
        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        from sketchgen import publish as publication
        publication.publish_index(self.conn, self.gallery, remote=str(self.bare))
        self.assertFalse((self.gallery / "e" / str(kept)).exists())
        failed = (self.gallery / "rejections.html").read_text(encoding="utf-8")
        self.assertNotIn(f'data-entry="{kept}"', failed)
        # Once a person publishes it, it is on the rejections page and nowhere else.
        result = self.publish_cli("--by", "profcarroll", entry_id=kept)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.gallery / "e" / str(kept) / "index.html").exists())
        failed = (self.gallery / "rejections.html").read_text(encoding="utf-8")
        self.assertIn(f'data-entry="{kept}"', failed)
        index = (self.gallery / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(f'data-entry="{kept}"', index)


    def test_a_failed_index_render_leaves_the_checkout_clean(self):
        # The second pass died mid-render on the node (2026-09-14, entry 71)
        # and every publish after it was refused for a dirty checkout.
        result = self.publish_cli("--by", "profcarroll")
        self.assertEqual(result.returncode, 0, result.stderr)
        head = self.head(self.gallery)
        from sketchgen import gallery, publish as publication

        def dies_half_way(conn, dest, config=None):
            (Path(dest) / "config.json").write_text("{}", encoding="utf-8")
            (Path(dest) / "stray.txt").write_text("half a render", encoding="utf-8")
            raise RuntimeError("template asked for a placeholder this code does not know")

        original = gallery.render_index
        gallery.render_index = dies_half_way
        try:
            sha, why = publication._publish_index(
                self.conn, self.gallery, "main", str(self.bare), None, self.entry_id
            )
        finally:
            gallery.render_index = original
        self.assertIsNone(sha)
        self.assertIn("index render failed", why)
        self.assertEqual(head, self.head(self.gallery))
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())


class LockTests(PublishTestCase):
    """One publisher at a time per checkout (the entry 488 race, 2026-09-16)."""

    def test_the_lock_file_is_in_the_git_dir_and_never_in_the_work_tree(self):
        """A lock file in the work tree would be staged and published."""
        lock_path = publish._lock_file(self.gallery)
        self.assertIsNotNone(lock_path)
        self.assertEqual(".git", lock_path.parent.name)
        with publish._checkout_lock(self.gallery):
            pass
        self.assertTrue(lock_path.exists())
        # the publisher stages everything in the work tree; this must not be in it
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())

    def test_a_second_holder_gives_up_rather_than_waiting_for_ever(self):
        with publish._checkout_lock(self.gallery):
            with self.assertRaises(publish.PublishFailed) as caught:
                with publish._checkout_lock(self.gallery, timeout=0.3):
                    self.fail("the second holder should not have got the lock")
        self.assertIn("another publish", str(caught.exception))

    def test_the_lock_is_released_when_the_block_exits(self):
        with publish._checkout_lock(self.gallery, timeout=0.3):
            pass
        with publish._checkout_lock(self.gallery, timeout=0.3):
            pass  # would raise if the first had not released

    def test_the_lock_is_released_when_the_block_raises(self):
        with self.assertRaises(ValueError):
            with publish._checkout_lock(self.gallery, timeout=0.3):
                raise ValueError("boom")
        with publish._checkout_lock(self.gallery, timeout=0.3):
            pass

    def test_a_directory_that_is_not_a_repository_is_not_locked(self):
        """Not a repo: no lock, and gallery_checkout gives the real refusal."""
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        self.assertIsNone(publish._lock_file(plain))
        with publish._checkout_lock(plain, timeout=0.3):
            pass  # a no-op, not an error
        self.assertIsNone(publish._lock_file(self.tmp / "does-not-exist"))

    def test_a_publish_waits_for_the_holder_instead_of_racing_it(self):
        """The whole point: the second publisher blocks, then does its work.

        Without the lock both would render and push against the same tree,
        which is how entry 488's push was refused for a ref that had moved.
        """
        held_for = 0.5
        released_at = []

        def hold():
            with publish._checkout_lock(self.gallery):
                time.sleep(held_for)
                released_at.append(time.monotonic())

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        time.sleep(0.1)  # let the thread take the lock first

        started = time.monotonic()
        result = publish.publish(
            self.conn, self.entry_id, gallery_dir=self.gallery, key=None
        )
        elapsed = time.monotonic() - started
        holder.join()

        self.assertIsInstance(result, publish.Published)
        self.assertTrue(released_at, "the holder never released the lock")
        # it cannot have started its git work before the holder let go
        self.assertGreater(elapsed, held_for - 0.2)


@unittest.skipUnless(
    GENERATOR_PRESENT, "a batch has no --from; the generator is its only source"
)
class PublishManyTests(PublishTestCase):
    """Several entries, one push, one index (the Held page's Process button).

    There is no ``--from`` here, so every entry comes out of the generator, and
    the fixture's prompt is what ends up on the page — which is how the
    personal-data test below trips the scan on one entry and not the others.
    """

    def make_entry(self, prompt, state="held", job_state="held"):
        """Another publishable entry, with a job of its own behind it."""
        job = db.enqueue(self.conn, prompt, "profcarroll")
        self.conn.execute("UPDATE jobs SET state = ? WHERE id = ?", (job_state, job))
        self.conn.commit()
        return db.create_entry(
            self.conn,
            job,
            state=state,
            prompt=prompt,
            executor=EXECUTOR,
            submitted_by="profcarroll",
        )

    def count_pushes(self):
        """Wrap ``_git`` and collect every push it is asked to make."""
        pushes = []
        original = publish._git

        def counting(cwd, *args, **kwargs):
            if args and args[0] == "push":
                pushes.append(args)
            return original(cwd, *args, **kwargs)

        publish._git = counting
        self.addCleanup(setattr, publish, "_git", original)
        return pushes

    def publish_batch(self, ids, **extra):
        options = dict(
            gallery_dir=self.gallery,
            remote=str(self.bare),
            key=None,
            by="profcarroll",
        )
        options.update(extra)
        return publish.publish_many(self.conn, ids, **options)

    def state_of(self, entry_id):
        return self.conn.execute(
            "SELECT * FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()

    def subjects_since(self, before):
        out = git(self.gallery, "log", "--pretty=%s", f"{before}..HEAD").stdout
        return [line for line in out.splitlines() if line.strip()]

    # -- the whole point: n commits, one push, one index --------------------

    def test_a_batch_is_three_commits_one_push_and_one_index(self):
        second = self.make_entry("a lattice that breathes when the mouse is still")
        third = self.make_entry("three colours arguing about the horizon")
        ids = [self.entry_id, second, third]
        before = self.head(self.gallery)
        pushes = self.count_pushes()

        result = self.publish_batch(ids)

        self.assertEqual({}, result.refused)
        self.assertEqual({}, result.failed)
        self.assertEqual(ids, [p.entry_id for p in result.published])
        self.assertIsNone(result.index_note, result.index_note)
        self.assertIsNotNone(result.index_commit)

        # One commit per entry, and one index commit naming all three of them.
        subjects = self.subjects_since(before)
        entry_commits = [s for s in subjects if s.startswith("entry ")]
        self.assertEqual(3, len(entry_commits), subjects)
        self.assertEqual(
            1,
            subjects.count(f"gallery index after entries {ids[0]}, {ids[1]}, {ids[2]}"),
            subjects,
        )

        # Two pushes for the lot: the entries' and the index's. Eight publishes
        # one at a time would have been sixteen.
        self.assertEqual(2, len(pushes), pushes)

        # The remote has every one of those commits, not just the local branch.
        self.assertEqual(self.head(self.bare), self.head(self.gallery))
        listing = git(self.bare, "ls-tree", "-r", "--name-only", "HEAD").stdout
        for entry_id in ids:
            self.assertIn(f"e/{entry_id}/index.html", listing)
        self.assertIn("index.html", listing)

        # Every row published, each stamped with its own commit.
        commits = []
        for entry_id in ids:
            row = self.state_of(entry_id)
            self.assertEqual("published", row["state"])
            self.assertTrue(row["published_utc"].endswith("Z"), row["published_utc"])
            commits.append(row["publish_commit"])
        self.assertEqual(3, len(set(commits)), commits)
        self.assertEqual([p.commit for p in result.published], commits)
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())

    # -- one entry's refusal is not the batch's -----------------------------

    def test_a_refused_entry_drops_out_and_the_rest_go_on(self):
        """The middle entry's prompt carries an address, so its render does."""
        caught = self.make_entry("a portrait of someone@example.com in motion")
        third = self.make_entry("three colours arguing about the horizon")
        ids = [self.entry_id, caught, third]

        result = self.publish_batch(ids)

        self.assertEqual([caught], list(result.refused))
        self.assertIn("email-shaped", result.refused[caught])
        self.assertEqual({}, result.failed)
        self.assertEqual([self.entry_id, third], [p.entry_id for p in result.published])

        self.assertEqual("held", self.state_of(caught)["state"])
        self.assertIsNone(self.state_of(caught)["publish_commit"])
        for entry_id in (self.entry_id, third):
            self.assertEqual("published", self.state_of(entry_id)["state"])

        listing = git(self.bare, "ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertNotIn(f"e/{caught}/", listing)
        self.assertIn(f"e/{third}/index.html", listing)

        # The dropped entry left nothing behind for the next one to trip on.
        self.assertFalse((self.gallery / "e" / str(caught)).exists())
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())

    # -- the push is the whole batch's -------------------------------------

    def test_a_failed_push_takes_the_whole_batch_back(self):
        second = self.make_entry("a lattice that breathes when the mouse is still")
        ids = [self.entry_id, second]
        before = self.head(self.gallery)

        result = self.publish_batch(ids, remote=str(self.tmp / "gone.git"))

        self.assertEqual([], result.published)
        self.assertEqual(sorted(ids), sorted(result.failed))
        for entry_id in ids:
            self.assertTrue(result.failed[entry_id])
            row = self.state_of(entry_id)
            self.assertEqual("held", row["state"])
            self.assertIsNone(row["publish_commit"])
            self.assertIsNone(row["published_utc"])
        self.assertEqual(before, self.head(self.gallery))
        self.assertEqual("", git(self.gallery, "status", "--porcelain").stdout.strip())

    # -- a rejection is a result, and keeps its state ----------------------

    def test_a_rejection_in_the_batch_keeps_its_state_and_gains_the_stamps(self):
        self.conn.execute(
            "UPDATE entries SET state = 'rejected', reject_reason = 'off brief' "
            "WHERE id = ?",
            (self.entry_id,),
        )
        self.conn.commit()
        kept = self.make_entry(
            "a sketch the gate failed", state="failed-kept", job_state="failed"
        )
        held = self.make_entry("three colours arguing about the horizon")

        result = self.publish_batch([self.entry_id, kept, held])

        self.assertEqual({}, result.refused)
        self.assertEqual({}, result.failed)
        self.assertEqual(3, len(result.published))
        self.assertEqual("rejected", self.state_of(self.entry_id)["state"])
        self.assertEqual("failed-kept", self.state_of(kept)["state"])
        self.assertEqual("published", self.state_of(held)["state"])
        for entry_id in (self.entry_id, kept, held):
            row = self.state_of(entry_id)
            self.assertIsNotNone(row["publish_commit"])
            self.assertTrue(row["published_utc"].endswith("Z"), row["published_utc"])

    # -- the progress the tray counts --------------------------------------

    def test_on_step_reports_every_commit_then_the_push_then_the_index(self):
        second = self.make_entry("a lattice that breathes when the mouse is still")
        seen = []

        result = self.publish_batch(
            [self.entry_id, second],
            on_step=lambda phase, entry_id, sentence: seen.append(
                (phase, entry_id, sentence)
            ),
        )

        self.assertEqual(2, len(result.published))
        self.assertEqual(
            ["commit", "commit", "push", "index"], [s[0] for s in seen]
        )
        self.assertEqual([self.entry_id, second, None, None], [s[1] for s in seen])
        self.assertEqual(
            f"Entry {self.entry_id} — rendering, scanning, committing "
            f"e/{self.entry_id}/",
            seen[0][2],
        )
        self.assertEqual("Pushing 2 commits to the gallery", seen[2][2])
        self.assertEqual("Re-rendering the index and pushing it", seen[3][2])

    # -- nothing marked, nothing done --------------------------------------

    def test_an_empty_batch_does_nothing_and_never_takes_the_lock(self):
        """An empty press must not queue behind a real batch to learn it is empty."""
        pushes = self.count_pushes()
        # So a regression that does take the lock fails in a moment rather
        # than waiting out the real five minutes.
        self.addCleanup(setattr, publish, "LOCK_TIMEOUT", publish.LOCK_TIMEOUT)
        publish.LOCK_TIMEOUT = 0.3
        with publish._checkout_lock(self.gallery):
            result = self.publish_batch([])
        self.assertIsInstance(result, publish.ManyResult)
        self.assertEqual([], result.published)
        self.assertEqual({}, result.refused)
        self.assertEqual({}, result.failed)
        self.assertIsNone(result.index_commit)
        self.assertIsNone(result.index_note)
        self.assertEqual([], pushes)


class ScanTests(unittest.TestCase):
    def test_binary_files_are_not_scanned_for_addresses(self):
        import tempfile, pathlib
        from sketchgen import publish as publication
        with tempfile.TemporaryDirectory() as tmp:
            d = pathlib.Path(tmp)
            # PNG bytes that happen to spell an address: a real strip did this.
            (d / "strip.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16 + b"ab@cd.ef" + b"\x00" * 8)
            (d / "sketch.js").write_text("let x = 1;\n", encoding="utf-8")
            publication.scan_for_personal_data(d)  # must not raise
            (d / "sketch.js").write_text("// mail me at someone@example.org\n", encoding="utf-8")
            with self.assertRaises(publication.PublishRefused):
                publication.scan_for_personal_data(d)
