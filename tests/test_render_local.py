"""Unit tests for ``sketchgen render-local`` (docs/plans/local-gallery.md, Packet 1).

Run:  python3 -m unittest tests.test_render_local -v

The same three-entry database test_gallery.py builds, plus a held child: the
held entry is the case the site never renders and a local render exists for.
No network, no browser, no push. The encoder is never started (--no-web).
"""

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_gallery import add_child, build_db  # noqa: E402

from sketchgen import db  # noqa: E402
from sketchgen import gallery  # noqa: E402
from sketchgen.cli import renderlocal  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


class LocalTestCase(unittest.TestCase):
    """Entries 1 and 2 published, 3 a published rejection, 4 held."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-local-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.conn, (self.one, self.two, self.three) = build_db(self.tmp)
        self.held = add_child(self.conn, self.tmp, self.two, state="held",
                              prompt="a held one, never published")
        self.conn.close()
        self.db_path = self.tmp / "sketchgen.db"
        self.jobs = self.tmp / "jobs"
        self.out = self.tmp / "local"

    def run_cli(self, *extra: str, out: Path | None = None, jobs: Path | None = None):
        parser = argparse.ArgumentParser()
        renderlocal.register(parser.add_subparsers())
        args = parser.parse_args([
            "render-local", "--db", str(self.db_path), "--jobs", str(jobs or self.jobs),
            "--out", str(out or self.out), "--origin", "sld-test", "--no-web", *extra,
        ])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = args.func(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def local_conn(self):
        conn = renderlocal.open_readonly(self.db_path, self.jobs)
        self.addCleanup(conn.close)
        return conn


class HeldEntryTests(LocalTestCase):
    """A held entry is on a local render's pages as held, with no public address."""

    def setUp(self):
        super().setUp()
        code, self.stdout, self.stderr = self.run_cli("--include", "published", "--include", "held")
        self.assertEqual(0, code, self.stderr)
        self.kiosk = {row["id"]: row for row in
                      json.loads((self.out / "kiosk.json").read_text())["entries"]}

    def test_the_held_entry_has_a_page_and_a_kiosk_row(self):
        self.assertTrue((self.out / "e" / str(self.held) / "index.html").is_file())
        self.assertTrue((self.out / "e" / str(self.held) / "sketch" / "sketch.js").is_file())
        self.assertIn(self.held, self.kiosk)
        self.assertIn(self.one, self.kiosk)

    def test_the_held_entry_has_no_code_and_no_url(self):
        row = self.kiosk[self.held]
        self.assertNotIn("url", row)
        self.assertNotIn("qr", row)
        self.assertFalse((self.out / "e" / str(self.held) / "qr.svg").exists())
        page = (self.out / "e" / str(self.held) / "index.html").read_text()
        self.assertNotIn('class="qr"', page)
        self.assertIn("held · not published", page)

    def test_a_published_entry_keeps_its_code_and_public_url(self):
        row = self.kiosk[self.one]
        self.assertEqual("https://profcarroll.github.io/sketchgen-gallery/e/%d/" % self.one, row["url"])
        self.assertTrue((self.out / "e" / str(self.one) / "qr.svg").is_file())
        self.assertIn('class="qr"', (self.out / "e" / str(self.one) / "index.html").read_text())

    def test_the_held_entry_meta_cites_no_address(self):
        meta = json.loads((self.out / "e" / str(self.held) / "meta.json").read_text())
        self.assertEqual("held", meta["state"])
        self.assertIsNone(meta["source"]["entry"])
        self.assertIsNone(meta["source"]["repository"])
        self.assertNotIn("http", meta["attribution"])

    def test_swipe_links_the_held_entry_locally(self):
        swipe = {row["id"]: row for row in
                 json.loads((self.out / "swipe.json").read_text())["entries"]}
        self.assertEqual("e/%d/" % self.held, swipe[self.held]["url"])

    def test_config_says_local_and_counts_nothing(self):
        config = json.loads((self.out / "config.json").read_text())
        self.assertEqual("sld-test", config["local"])
        self.assertEqual("", config["write_path"])
        self.assertFalse(config["kiosk_views"])

    def test_no_composer_no_critique_form_no_pairs(self):
        self.assertNotIn("data-compose", (self.out / "index.html").read_text())
        for entry in (self.one, self.held):
            self.assertNotIn("data-critique", (self.out / "e" / str(entry) / "index.html").read_text())
        self.assertEqual([], json.loads((self.out / "pairs.json").read_text())["pairs"])

    def test_the_stamp_marks_the_directory(self):
        stamp = json.loads((self.out / renderlocal.STAMP).read_text())
        self.assertEqual("sld-test", stamp["origin"])


class SelectionTests(LocalTestCase):

    def test_published_only_is_the_default_and_leaves_the_held_entry_out(self):
        code, _, err = self.run_cli()
        self.assertEqual(0, code, err)
        self.assertFalse((self.out / "e" / str(self.held)).exists())
        self.assertTrue((self.out / "e" / str(self.one)).is_dir())

    def test_held_only_leaves_the_published_entries_out(self):
        code, _, err = self.run_cli("--include", "held")
        self.assertEqual(0, code, err)
        self.assertEqual([str(self.held)], sorted(p.name for p in (self.out / "e").iterdir()))

    def test_ids_narrow_and_a_rerender_removes_what_left(self):
        code, _, err = self.run_cli("--include", "published", "--include", "held")
        self.assertEqual(0, code, err)
        ids = self.tmp / "ids.txt"
        ids.write_text(f"# picked\n{self.held}\n\n{self.one}\n")
        code, _, err = self.run_cli("--include", "published", "--include", "held", "--ids", str(ids))
        self.assertEqual(0, code, err)
        self.assertEqual(sorted([str(self.one), str(self.held)]),
                         sorted(p.name for p in (self.out / "e").iterdir()))

    def test_a_shared_render_is_the_same_bytes_as_one_by_one(self):
        conn = self.local_conn()
        config = gallery.Config(local=gallery.Local("sld-test", held=True))
        shared = gallery._shared(conn, config)
        one_by_one, together = self.tmp / "a", self.tmp / "b"
        for entry in (self.one, self.held):
            gallery.render_entry(conn, entry, one_by_one, config)
            gallery.render_entry(conn, entry, together, config, shared=shared)
        self.assertEqual(tree(one_by_one), tree(together))

    def test_the_site_still_refuses_a_held_entry(self):
        conn = self.local_conn()
        with self.assertRaises(gallery.UnknownEntry):
            gallery.render_entry(conn, self.held, self.tmp / "site", gallery.Config())


class ScanTests(LocalTestCase):

    def test_an_entry_the_guard_refuses_is_skipped_and_named(self):
        sketch = Path(self.local_conn().execute(
            "SELECT source_dir FROM entries WHERE id = ?", (self.held,)).fetchone()[0])
        with open(sketch / "sketch.js", "a", encoding="utf-8") as fh:
            fh.write("// mail somebody@example.com\n")
        code, out, err = self.run_cli("--include", "published", "--include", "held")
        self.assertEqual(0, code, err)
        self.assertIn(f"skipped e/{self.held}", out)
        self.assertIn("email-shaped", out)
        self.assertFalse((self.out / "e" / str(self.held)).exists())
        ids = [row["id"] for row in json.loads((self.out / "kiosk.json").read_text())["entries"]]
        self.assertNotIn(self.held, ids)
        self.assertIn(self.one, ids)


class ReadOnlyTests(LocalTestCase):

    def test_the_database_is_not_written(self):
        before = {p.name: sha256(p) for p in self.tmp.glob("sketchgen.db*")}
        code, _, err = self.run_cli("--include", "published", "--include", "held")
        self.assertEqual(0, code, err)
        after = {p.name: sha256(p) for p in self.tmp.glob("sketchgen.db*")}
        self.assertEqual(before, after)

    def test_the_connection_refuses_a_write(self):
        conn = self.local_conn()
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("UPDATE main.entries SET state = 'published'")


class ReadonlyUriTests(LocalTestCase):
    """A snapshot gains no sidecar files; a live database is read, not frozen."""

    def test_a_closed_wal_database_is_opened_immutable(self):
        self.assertFalse((self.tmp / "sketchgen.db-wal").exists())
        self.assertTrue(renderlocal._readonly_uri(self.db_path).endswith("?mode=ro&immutable=1"))

    def test_a_database_somebody_has_open_is_opened_read_only(self):
        live = db.connect(self.db_path)
        self.addCleanup(live.close)
        live.execute("SELECT 1 FROM entries").fetchall()
        self.assertTrue((self.tmp / "sketchgen.db-wal").exists())
        self.assertTrue(renderlocal._readonly_uri(self.db_path).endswith("?mode=ro"))

    def test_a_rollback_journal_database_is_opened_read_only(self):
        snapshot = self.tmp / "snapshot.db"
        source = sqlite3.connect(self.db_path)
        target = sqlite3.connect(snapshot)
        source.backup(target)
        target.execute("PRAGMA journal_mode = DELETE")
        source.close()
        target.close()
        self.assertTrue(renderlocal._readonly_uri(snapshot).endswith("?mode=ro"))


class RemapTests(LocalTestCase):
    """The rows name the node they were written on; --jobs is where they are now."""

    def setUp(self):
        super().setUp()
        self.moved = self.tmp / "archive" / "jobs"
        self.moved.parent.mkdir()
        shutil.move(str(self.jobs), str(self.moved))

    def test_the_sketch_comes_from_jobs(self):
        code, _, err = self.run_cli("--include", "held", jobs=self.moved)
        self.assertEqual(0, code, err)
        sketch = self.out / "e" / str(self.held) / "sketch" / "sketch.js"
        attempt = self.moved / str(self.held) / "attempt-1" / "sketch.js"
        # The held entry is job 4's, and its attempt is where --jobs says.
        self.assertTrue(sketch.is_file())
        self.assertEqual(sketch.read_bytes(), attempt.read_bytes())

    def test_every_path_reads_under_jobs(self):
        conn = renderlocal.open_readonly(self.db_path, self.moved)
        self.addCleanup(conn.close)
        for table, columns in renderlocal.PATH_COLUMNS.items():
            for column in columns:
                for (value,) in conn.execute(f"SELECT {column} FROM {table}"):
                    if value is not None:
                        self.assertTrue(value.startswith(str(self.moved.resolve()) + "/"), value)

    def test_a_path_outside_any_jobs_directory_reads_null(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute("UPDATE entries SET png_path = '/etc/passwd' WHERE id = ?", (self.held,))
        raw.commit()
        raw.close()
        conn = renderlocal.open_readonly(self.db_path, self.moved)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT png_path FROM entries WHERE id = ?", (self.held,)).fetchone()
        self.assertIsNone(row[0])

    def test_a_jobs_directory_holding_none_of_them_is_refused(self):
        empty = self.tmp / "empty-jobs"
        empty.mkdir()
        code, _, err = self.run_cli("--include", "held", jobs=empty)
        self.assertEqual(3, code)
        self.assertIn("none of the", err)
        self.assertFalse(self.out.exists())


class RefusalTests(LocalTestCase):

    def assertRefused(self, code, err, text):
        self.assertEqual(3, code, err)
        self.assertIn(text, err)

    def test_inside_a_git_work_tree(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        code, _, err = self.run_cli(out=repo / "local")
        self.assertRefused(code, err, "git work tree")
        self.assertFalse((repo / "local").exists())

    def test_the_gallery_checkout(self):
        gallery_dir = self.tmp / "gallery"
        with mock.patch.object(renderlocal.publish, "DEFAULT_GALLERY_DIR", str(gallery_dir)):
            code, _, err = self.run_cli(out=gallery_dir)
        self.assertRefused(code, err, "gallery checkout")

    def test_a_directory_that_is_not_a_local_render(self):
        self.out.mkdir()
        (self.out / "notes.txt").write_text("mine")
        code, _, err = self.run_cli()
        self.assertRefused(code, err, "not a local render")
        self.assertEqual(["notes.txt"], [p.name for p in self.out.iterdir()])

    def test_a_schema_newer_than_this_code(self):
        raw = sqlite3.connect(self.db_path)
        raw.execute("INSERT INTO schema_version (version, name, applied_utc) "
                    "VALUES (999, '999_future.sql', '2027-01-01T00:00:00Z')")
        raw.commit()
        raw.close()
        code, _, err = self.run_cli()
        self.assertRefused(code, err, "schema 999")

    def test_nothing_to_render(self):
        ids = self.tmp / "ids.txt"
        ids.write_text("9999\n")
        code, _, err = self.run_cli("--ids", str(ids))
        self.assertRefused(code, err, "nothing to render")

    def test_an_ids_file_with_something_else_in_it(self):
        ids = self.tmp / "ids.txt"
        ids.write_text("12\ne/13\n")
        code, _, err = self.run_cli("--ids", str(ids))
        self.assertRefused(code, err, "not an entry id")


class PublicConfigTests(unittest.TestCase):

    def test_the_site_config_has_no_local_key(self):
        self.assertNotIn("local", json.loads(gallery.Config().to_json()))

    def test_a_checkout_cannot_become_local_by_its_config(self):
        tmp = Path(tempfile.mkdtemp(prefix="sketchgen-local-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "config.json").write_text(json.dumps({"local": "sneaky"}))
        self.assertIsNone(gallery.Config.load(tmp).local)


if __name__ == "__main__":
    unittest.main()
