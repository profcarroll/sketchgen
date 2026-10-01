"""Unit tests for ``sketchgen import`` and migration 019 (docs/plans/gpu-fold-in.md, Packet 1).

Run:  python3 -m unittest tests.test_import -v

Two scratch databases and a scratch archive, built in setUp: the archive is
another node's ``~/sketchgen`` at schema 17, as the rented A10's is, with a
verified snapshot under ``backups/`` and a ``jobs/`` whose rows name the other
node's paths (``/home/ubuntu/sketchgen/jobs/…``); the node is this code's schema,
paused, with one published entry of its own. No network, no browser, no push.
"""

import argparse
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen.cli import backup  # noqa: E402
from sketchgen.cli import importentries  # noqa: E402

#: Where the other node kept its jobs: what every path in its rows says.
THEIR_JOBS = "/home/ubuntu/sketchgen/jobs"
SHAPE = "VM.GPU.A10.1 15/240 + A10 24G"
STAMP = datetime(2026, 9, 27, 13, 50, 15, tzinfo=timezone.utc)
NOTE = "rented OCI A10, terminated 2026-09-27"

#: (prompt, entry state, off-plan, calls createGraphics, attempts, created_utc)
SPECS = (
    ("a tide of slow lines", "held", False, False, 1, "2026-09-23T15:00:00Z"),
    ("a tide of slow lines", "held", False, False, 1, "2026-09-24T09:00:00Z"),
    ("hot air balloons over a salt flat", "held", True, True, 2, "2026-09-24T10:00:00Z"),
    ("a gate that never passed", "failed-kept", False, False, 3, "2026-09-25T10:00:00Z"),
    ("a quiet grid", "held", False, False, 2, "2026-09-26T10:00:00Z"),
)


def migrate_to(conn: sqlite3.Connection, version: int, base: Path) -> None:
    """Apply migrations up to ``version`` only: a database as an older node left it."""
    directory = base / f"migrations-{version}"
    directory.mkdir(exist_ok=True)
    for number, path in db._migration_files():
        if number <= version:
            shutil.copy(path, directory / path.name)
    db.migrate(conn, directory)


def make_archive(base: Path, *, schema: int = 17, stray_path_in: int | None = None) -> Path:
    """Another node's ~/sketchgen: jobs/ and one verified snapshot. Returns its root."""
    root = base / "archive"
    jobs = root / "jobs"
    jobs.mkdir(parents=True)
    live = base / "archive-live.db"
    conn = db.connect(live)
    migrate_to(conn, schema, base)
    for prompt, state, offplan, buffers, attempts, created in SPECS:
        job = db.enqueue(conn, prompt, "profcarroll", planner="gemma4:e4b",
                         executor="qwen3-coder:30b", rules_file="treatment",
                         brief=f"the brief of {prompt}")
        conn.execute("UPDATE jobs SET state = ?, created_utc = ?, updated_utc = ? WHERE id = ?",
                     ("held" if state == "held" else "failed", created, created, job))
        for n in range(1, attempts + 1):
            here = jobs / str(job) / f"attempt-{n}"
            (here / ".gate").mkdir(parents=True)
            sketch = "function setup() { createCanvas(400, 400); }\n"
            if buffers:
                sketch += "let g = createGraphics(100, 100);\n"
            (here / "sketch.js").write_text(sketch)
            (here / ".gate" / "strip.png").write_bytes(b"\x89PNG strip " + str(job).encode())
            (here / ".gate" / "gate.png").write_bytes(b"\x89PNG gate")
            (here / ".gate" / "report.json").write_text(json.dumps({"job": job, "n": n}))
            db.add_attempt(conn, job, n=n, model="qwen3-coder:30b", rules_file="treatment",
                           prompt_version="executor-v3", wall_s=12.5 * n, prompt_tokens=1000,
                           completion_tokens=500, gate_exit=0 if n == attempts else 1,
                           source_dir=f"{THEIR_JOBS}/{job}/attempt-{n}",
                           gate_report_path=f"{THEIR_JOBS}/{job}/attempt-{n}/.gate/report.json")
        (jobs / str(job) / "job.log").write_text(f"job {job}\n")
        kept = f"{THEIR_JOBS}/{job}/attempt-{attempts}"
        strip = f"{kept}/.gate/strip.png"
        if stray_path_in == job:
            strip = f"{THEIR_JOBS}/1/attempt-1/.gate/strip.png"  # another job's frames
        entry = db.create_entry(
            conn, job, state, prompt=prompt, brief=f"the brief of {prompt}",
            statement="a statement", planner="gemma4:e4b", planner_prompt_version="planner-v2",
            executor="qwen3-coder:30b", executor_prompt_version="executor-v3",
            rules_file="treatment", assertions_json='["motion(idle)"]',
            offplan_json='["motion(idle)"]' if offplan else None, harness_version=4,
            attempts=attempts, prompt_tokens=1000 * attempts, completion_tokens=500 * attempts,
            wall_s=45.952, shape=SHAPE, seed=job + 1, submitted_by="profcarroll",
            source_dir=kept, strip_path=strip, png_path=f"{kept}/.gate/gate.png",
        )
        conn.execute("UPDATE entries SET created_utc = ? WHERE id = ?", (created, entry))
    conn.close()
    backup.snapshot(to=root / "backups", source_db=live, jobs_dir=jobs,
                    gallery_dir=base / "no-gallery", gate_dir=base / "no-gate", now=STAMP)
    return root


class ImportTestCase(unittest.TestCase):
    """A node at this code's schema, paused, with entry 1 published; an archive of five."""

    archive_options: dict = {}

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="sketchgen-import-"))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.archive = make_archive(self.base, **self.archive_options)
        self.node_db = self.base / "node.db"
        self.jobs = self.base / "node" / "jobs"
        self.jobs.mkdir(parents=True)
        db.init(self.node_db)
        self.conn = db.connect(self.node_db)
        self.addCleanup(self.conn.close)
        own = db.enqueue(self.conn, "a tide of slow lines", "octocat")
        self.conn.execute("UPDATE jobs SET state = 'published' WHERE id = ?", (own,))
        (self.jobs / str(own) / "attempt-1").mkdir(parents=True)
        self.own = db.create_entry(self.conn, own, "published", prompt="a tide of slow lines",
                                   published_utc="2026-09-20T12:00:00Z")
        db.set_control(self.conn, "paused", "import")
        self.ids_file = self.base / "picks.txt"

    # -- helpers --------------------------------------------------------------

    def cli(self, *argv: str, stdin: str | None = None):
        parser = argparse.ArgumentParser()
        importentries.register(parser.add_subparsers())
        args = parser.parse_args(["import", *argv])
        out, err = io.StringIO(), io.StringIO()
        patches = [redirect_stdout(out), redirect_stderr(err)]
        if stdin is not None:
            patches.append(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
        with patches[0], patches[1], (patches[2] if len(patches) > 2 else _null()):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def run_import(self, ids, *extra: str, note: bool = True):
        self.ids_file.write_text("".join(f"{i}\n" for i in ids))
        argv = ["run", "--from", str(self.archive), "--db", str(self.node_db),
                "--jobs-dir", str(self.jobs), "--node", "sld-gpu", "--by", "octocat",
                "--ids", str(self.ids_file), *extra]
        if note:
            argv += ["--note", NOTE]
        return self.cli(*argv)

    def list_archive(self, *extra: str):
        return self.cli("list", "--from", str(self.archive), "--db", str(self.node_db), *extra)

    def state(self):
        """Everything an import may write: row counts and the jobs directory."""
        counts = {table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("jobs", "attempts", "entries", "origins")}
        return counts, sorted(p.name for p in self.jobs.iterdir())

    def imported(self, origin_entry_id: int) -> sqlite3.Row:
        return self.conn.execute(
            "SELECT * FROM entries WHERE origin_node = 'sld-gpu' AND origin_entry_id = ?",
            (origin_entry_id,)).fetchone()


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Migration019(unittest.TestCase):

    def test_up_from_018_leaves_every_existing_row_made_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            conn = db.connect(base / "t.db")
            migrate_to(conn, 18, base)
            job = db.enqueue(conn, "made here", "octocat")
            entry = db.create_entry(conn, job, "held", prompt="made here")
            self.assertEqual([19], [v for v in db.migrate(conn) if v == 19])
            row = db.get_entry(conn, entry)
            for column in ("origin_node", "origin_entry_id", "origin_job_id", "imported_utc"):
                self.assertIsNone(row[column])
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM origins").fetchone()[0])
            self.assertEqual([], db.migrate(conn))
            conn.close()

    def test_the_same_origin_entry_cannot_be_imported_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db.init(path)
            conn = db.connect(path)
            db.register_origin(conn, "sld-gpu", "octocat", note=NOTE)
            for _ in range(2):
                job = db.import_job(conn, prompt="p", submitted_by="octocat",
                                    created_utc="2026-09-24T00:00:00Z",
                                    updated_utc="2026-09-24T00:00:00Z")
                if _ == 0:
                    db.import_entry(conn, job, origin_node="sld-gpu", origin_entry_id=412,
                                    origin_job_id=412, created_utc="2026-09-24T00:00:00Z")
                else:
                    with self.assertRaises(sqlite3.IntegrityError):
                        db.import_entry(conn, job, origin_node="sld-gpu", origin_entry_id=412,
                                        origin_job_id=412, created_utc="2026-09-24T00:00:00Z")
            conn.close()

    def test_an_import_never_takes_the_ids_or_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            db.init(path)
            conn = db.connect(path)
            for column in ("id", "state", "plan_build"):
                with self.assertRaises(ValueError):
                    db.import_job(conn, **{column: 1, "prompt": "p", "submitted_by": "o",
                                           "created_utc": "x", "updated_utc": "x"})
            conn.close()


class ListTests(ImportTestCase):

    def test_every_held_entry_with_its_twin_and_its_buffers(self):
        code, out, _ = self.list_archive()
        self.assertEqual(0, code)
        lines = out.splitlines()
        self.assertTrue(lines[0].split()[:2] == ["origin", "prompt"])
        rows = {int(line.split()[0]): line for line in lines[1:-1]}
        self.assertEqual({1, 2, 3, 5}, set(rows))
        self.assertTrue(rows[1].endswith(f"e/{self.own}"))
        self.assertIn(" yes ", rows[3])
        self.assertTrue(rows[5].rstrip().endswith("-"))
        self.assertEqual(
            "4 listed of 4 held (newest snapshot 2026-09-27T135015Z, verified)", lines[-1])

    def test_filters(self):
        cases = {
            ("--clean",): [1, 2, 5],
            ("--clean", "--one-per-prompt"): [1, 5],
            ("--no-buffers",): [1, 2, 5],
            ("--prompt-of", str(self.own)): [1, 2],
            ("--executor", "nobody:1b"): [],
            ("--rules", "control"): [],
        }
        for argv, want in cases.items():
            with self.subTest(argv=argv):
                code, out, err = self.list_archive(*argv, "--ids")
                self.assertEqual(0, code, err)
                self.assertEqual(want, [int(x) for x in out.split()])

    def test_json_and_never_a_write(self):
        before = (self.archive / "backups").stat().st_mtime_ns, self.state()
        snap = next((self.archive / "backups").iterdir())
        names = sorted(p.name for p in snap.iterdir())
        code, out, _ = self.list_archive("--json")
        self.assertEqual(0, code)
        doc = json.loads(out)
        self.assertTrue(doc["verified"])
        self.assertEqual(4, doc["held"])
        self.assertEqual([1, 2, 3, 5], [e["origin_entry_id"] for e in doc["entries"]])
        self.assertEqual(sorted(p.name for p in snap.iterdir()), names)
        self.assertEqual(before, ((self.archive / "backups").stat().st_mtime_ns, self.state()))

    def test_not_imported_leaves_out_what_came(self):
        self.assertEqual(0, self.run_import([1])[0])
        code, out, _ = self.list_archive("--not-imported", "--node", "sld-gpu", "--ids")
        self.assertEqual(0, code)
        self.assertEqual([2, 3, 5], [int(x) for x in out.split()])
        self.assertEqual(3, self.list_archive("--not-imported", "--ids")[0])

    def test_with_no_database_here_the_twin_is_blank(self):
        code, out, _ = self.cli("list", "--from", str(self.archive),
                                "--db", str(self.base / "nowhere.db"))
        self.assertEqual(0, code)
        self.assertIn("so `here` is blank", out.splitlines()[-1])


class RunTests(ImportTestCase):

    def test_ids_are_renumbered_and_the_origin_kept(self):
        code, out, err = self.run_import([1, 3, 5])
        self.assertEqual(0, code, err)
        self.assertRegex(out.splitlines()[-1],
                         r"^imported 3 of 3 → entries \d+–\d+, held; 0 skipped, 0 failed; "
                         r"\d+ MB copied$")
        # New ids from this node's own sequence, after its own entry, in order.
        self.assertEqual([self.own + 1, self.own + 2, self.own + 3],
                         [self.imported(origin)["id"] for origin in (1, 3, 5)])
        for origin in (1, 3, 5):
            row = self.imported(origin)
            self.assertEqual("held", row["state"])
            self.assertEqual(origin, row["origin_entry_id"])
            self.assertEqual(origin, row["origin_job_id"])  # the archive's job ids are its entry ids
            self.assertTrue(row["imported_utc"].endswith("Z"))
            job = db.get_job(self.conn, row["job_id"])
            self.assertEqual("held", job.state)

    def test_what_is_copied_as_written(self):
        self.assertEqual(0, self.run_import([3])[0])
        row = self.imported(3)
        created = SPECS[2][5]
        self.assertEqual(created, row["created_utc"])
        self.assertEqual(SHAPE, row["shape"])
        self.assertEqual("qwen3-coder:30b", row["executor"])
        self.assertEqual("gemma4:e4b", row["planner"])
        self.assertEqual(45.952, row["wall_s"])
        self.assertEqual(4, row["seed"])
        self.assertEqual(4, row["harness_version"])
        self.assertEqual('["motion(idle)"]', row["offplan_json"])
        self.assertEqual("profcarroll", row["submitted_by"])
        job = db.get_job(self.conn, row["job_id"])
        self.assertEqual((created, created), (job.created_utc, job.updated_utc))
        self.assertEqual("the brief of hot air balloons over a salt flat", job.brief)
        attempts = db.list_attempts(self.conn, row["job_id"])
        self.assertEqual([1, 2], [a.n for a in attempts])
        self.assertEqual([12.5, 25.0], [a.wall_s for a in attempts])
        self.assertEqual([None, None], [a.build for a in attempts])
        self.assertIsNone(job.plan_build)

    def test_all_five_paths_are_rewritten_and_resolve(self):
        self.assertEqual(0, self.run_import([5])[0])
        row = self.imported(5)
        directory = self.jobs / str(row["job_id"])
        self.assertTrue(directory.is_dir())
        for column in ("source_dir", "strip_path", "png_path"):
            self.assertTrue(row[column].startswith(f"{directory}/"), row[column])
            self.assertTrue(Path(row[column]).exists(), row[column])
        for attempt in db.list_attempts(self.conn, row["job_id"]):
            self.assertEqual(str(directory / f"attempt-{attempt.n}"), attempt.source_dir)
            self.assertTrue(Path(attempt.gate_report_path).is_file())
        # Byte for byte, and the report still names the rental's job.
        self.assertEqual(b"\x89PNG strip 5", Path(row["strip_path"]).read_bytes())
        report = json.loads((directory / "attempt-2" / ".gate" / "report.json").read_text())
        self.assertEqual(5, report["job"])

    def test_the_origin_is_registered_once_from_the_manifest(self):
        code, _, err = self.run_import([1])
        self.assertEqual(0, code)
        self.assertIn("registered origin sld-gpu", err)
        origin = db.get_origin(self.conn, "sld-gpu")
        manifest = json.loads(next((self.archive / "backups").iterdir())
                              .joinpath("manifest.json").read_text())
        self.assertEqual(SHAPE, origin["shape"])
        self.assertEqual(NOTE, origin["note"])
        self.assertEqual("octocat", origin["registered_by"])
        self.assertEqual("2026-09-27T135015Z", origin["snapshot"])
        self.assertEqual(manifest["files"]["sketchgen.db"]["sha256"], origin["snapshot_sha256"])
        self.assertEqual("2026-09-23T15:00:00Z", origin["first_utc"])
        self.assertEqual("2026-09-26T10:00:00Z", origin["last_utc"])
        # A second run needs no --note: the name is known.
        code, _, err = self.run_import([2], note=False)
        self.assertEqual(0, code, err)
        self.assertNotIn("registered", err)

    def test_a_second_run_imports_nothing(self):
        self.assertEqual(0, self.run_import([1, 5])[0])
        before = self.state()
        code, out, _ = self.run_import([1, 5])
        self.assertEqual(0, code)
        self.assertEqual(before, self.state())
        self.assertIn(f"     1  already e/{self.imported(1)['id']}", out)
        self.assertEqual("imported 0 of 2; 2 skipped, 0 failed; 0 MB copied",
                         out.splitlines()[-1])

    def test_ids_from_stdin(self):
        argv = ["run", "--from", str(self.archive), "--db", str(self.node_db),
                "--jobs-dir", str(self.jobs), "--node", "sld-gpu", "--by", "octocat",
                "--note", NOTE, "--ids", "-"]
        code, _, err = self.cli(*argv, stdin="1\n# a comment\n5\n")
        self.assertEqual(0, code, err)
        self.assertIsNotNone(self.imported(1))
        self.assertIsNotNone(self.imported(5))

    def test_dry_run_writes_nothing_and_says_the_megabytes(self):
        before = self.state()
        code, out, err = self.run_import([1, 3, 5], "--dry-run")
        self.assertEqual(0, code, err)
        self.assertEqual(before, self.state())
        self.assertRegex(out, r"would import 3 of 3 from sld-gpu \(snapshot 2026-09-27T135015Z, "
                              r"verified\); 0 already here; \d+ MB to copy; registers sld-gpu")

    def test_json_is_one_object(self):
        self.assertEqual(0, self.run_import([1])[0])
        code, out, _ = self.run_import([1, 5], "--json")
        self.assertEqual(0, code)
        doc = json.loads(out)
        self.assertEqual({"5"}, set(doc["imported"]))
        self.assertEqual({"1"}, set(doc["already"]))
        self.assertEqual({}, doc["failed"])

    def test_a_copy_that_fails_half_way_leaves_neither(self):
        real = importentries.copy_job_dir
        calls = []

        def half(src, dst):
            calls.append(dst)
            if len(calls) == 1:
                Path(dst).mkdir()
                (Path(dst) / "half.png").write_bytes(b"half")
                raise OSError("disk went away")
            return real(src, dst)

        before_counts, before_dirs = self.state()
        with mock.patch.object(importentries, "copy_job_dir", side_effect=half):
            code, out, _ = self.run_import([1, 5])
        self.assertEqual(1, code)
        self.assertIn("     1  failed: disk went away", out)
        self.assertIsNone(self.imported(1))
        self.assertIsNotNone(self.imported(5))
        self.assertEqual([], [p for p in self.jobs.iterdir() if p.name.startswith(".import-")])
        counts, dirs = self.state()
        self.assertEqual(before_counts["entries"] + 1, counts["entries"])
        self.assertEqual(len(before_dirs) + 1, len(dirs))
        self.assertRegex(out.splitlines()[-1], r"^imported 1 of 2 → .*; 0 skipped, 1 failed;")


class OccupiedTests(ImportTestCase):

    def test_a_directory_in_the_way_stops_the_run(self):
        """The next entry would be handed the same id again: stop, don't fail them all."""
        real = importentries.copy_job_dir

        def squatter(src, dst):
            real(src, dst)
            taken = int(Path(dst).name.removeprefix(".import-"))
            (self.jobs / str(taken + 1)).mkdir(exist_ok=True)

        with mock.patch.object(importentries, "copy_job_dir", side_effect=squatter):
            code, out, _ = self.run_import([1, 3, 5])
        self.assertEqual(1, code)
        self.assertIn("     3  failed:", out)
        self.assertIn("already exists", out)
        self.assertNotIn("     5  failed", out)
        self.assertIsNotNone(self.imported(1))
        self.assertIsNone(self.imported(3))
        self.assertIsNone(self.imported(5))
        self.assertRegex(out.splitlines()[-1],
                         r"^imported 1 of 3 → .*; 0 skipped, 1 failed, 1 not attempted;")


class StrayPathTests(ImportTestCase):
    """Entry 5's strip names job 1's directory: it fails, and nothing of it stays."""

    archive_options = {"stray_path_in": 5}

    def test_a_path_outside_its_own_job_fails_the_entry(self):
        before_counts, before_dirs = self.state()
        code, out, _ = self.run_import([5, 1])
        self.assertEqual(1, code)
        self.assertIn("     5  failed: path", out)
        self.assertIsNone(self.imported(5))
        self.assertIsNotNone(self.imported(1))
        counts, dirs = self.state()
        self.assertEqual(before_counts["jobs"] + 1, counts["jobs"])
        self.assertEqual(len(before_dirs) + 1, len(dirs))


class RefusalTests(ImportTestCase):
    """Every refusal is exit 3 before any row or directory exists."""

    def assertRefused(self, ids, *extra, says: str, note: bool = True):
        before = self.state()
        code, out, err = self.run_import(ids, *extra, note=note)
        self.assertEqual(3, code, out + err)
        self.assertIn(says, err)
        self.assertEqual(before, self.state())
        self.assertEqual("", out)

    def test_the_generator_running(self):
        db.set_control(self.conn, "running", None)
        self.assertRefused([1], says="the generator is running")

    def test_a_snapshot_that_does_not_verify(self):
        snap = next((self.archive / "backups").iterdir())
        manifest = json.loads((snap / "manifest.json").read_text())
        manifest["files"]["sketchgen.db"]["sha256"] = "0" * 64
        (snap / "manifest.json").write_text(json.dumps(manifest))
        self.assertRefused([1], says="does not verify")

    def test_a_node_before_019(self):
        self.conn.execute("DELETE FROM schema_version WHERE version = 19")
        self.assertRefused([1], says="needs migration 019")

    def test_a_snapshot_newer_than_this_node(self):
        with mock.patch.object(importentries.db, "schema_version",
                               side_effect=lambda c: 99 if c is not self.conn else 19):
            self.assertRefused([1], says="is at schema 99")

    def test_an_id_not_held_there(self):
        self.assertRefused([1, 4, 77], says="2 of the ids are not held entries")
        self.assertRefused([4], says="4 (failed-kept)")
        self.assertRefused([77], says="77 (not in the snapshot)")

    def test_no_ids(self):
        self.assertRefused([], says="no ids in it")

    def test_an_unregistered_node_and_no_note(self):
        self.assertRefused([1], says="not a registered origin", note=False)

    def test_an_origin_registered_from_another_snapshot(self):
        db.register_origin(self.conn, "sld-gpu", "octocat", note=NOTE,
                           snapshot="2026-09-26T041003Z", snapshot_sha256="f" * 64)
        self.assertRefused([1], says="one origin is one snapshot")

    def test_a_disk_without_room(self):
        usage = shutil.disk_usage(self.jobs)._replace(free=1024)
        with mock.patch.object(importentries.shutil, "disk_usage", return_value=usage):
            self.assertRefused([1, 3], says="MB free")

    def test_a_job_directory_with_no_job_row(self):
        (self.jobs / "40").mkdir()
        self.assertRefused([1], says="(40)")

    def test_a_name_that_is_a_hostname(self):
        self.ids_file.write_text("1\n")
        before = self.state()
        code, _, err = self.cli("run", "--from", str(self.archive), "--db", str(self.node_db),
                                "--jobs-dir", str(self.jobs), "--node", "instance-20260918",
                                "--by", "octocat", "--note", NOTE, "--ids", str(self.ids_file))
        self.assertEqual(3, code)
        self.assertIn("cloud hostname", err)
        self.assertEqual(before, self.state())

    def test_no_archive_there(self):
        code, _, err = self.cli("list", "--from", str(self.base / "nowhere"))
        self.assertEqual(3, code)
        self.assertIn("no archive at", err)


class SchemaNineteenArchiveTests(ImportTestCase):
    """An archive at this code's schema copies the same, columns and all."""

    archive_options = {"schema": 19}

    def test_it_imports(self):
        code, _, err = self.run_import([1, 2, 3, 5])
        self.assertEqual(0, code, err)
        self.assertEqual(4, self.conn.execute(
            "SELECT COUNT(*) FROM entries WHERE origin_node = 'sld-gpu'").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
