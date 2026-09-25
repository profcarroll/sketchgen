"""Unit tests for `sketchgen backup` — spec §3.6.

The four claims packet 0 makes, each one a test below:

  1. a snapshot of a **WAL-mode** database verifies (the whole point of using the
     online backup API instead of `cp`);
  2. retention keeps fourteen;
  3. the manifest's counts match the snapshot's actual counts;
  4. no secret is anywhere in the snapshot directory.

The fixture database is put into WAL mode explicitly and a writer is left open
across the snapshot, because the failure this command exists to avoid — a torn
copy of a database being written — only happens when there is something in the
WAL that a `cp` would miss.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen.cli import backup  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "bin" / "sketchgen"
UNIT_DIR = REPO_ROOT / "systemd"

#: What a snapshot directory is allowed to contain, and nothing else.
EXPECTED_FILES = {"sketchgen.db", "gate.tar.gz", "manifest.json"}

TOKEN = "a-secret-bearer-nobody-should-ever-find-in-a-backup"


def make_database(path: Path) -> None:
    """A migrated database in WAL mode with rows in it and an open writer's
    work still sitting in the -wal file."""
    db.init(path)
    conn = db.connect(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"the fixture is not in WAL mode: {mode}"
    for index in range(7):
        db.enqueue(conn, f"a sketch about {index}", "profcarroll")
    return conn  # left OPEN: its pages are in the -wal, which is the point


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-backup-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "sketchgen.db"
        self.writer = make_database(self.source)
        self.addCleanup(self.writer.close)
        self.backups = self.tmp / "backups"
        self.jobs = self.tmp / "jobs"
        for name in ("1", "2", "3"):
            (self.jobs / name).mkdir(parents=True)
        (self.jobs / "not-a-job.log").write_text("", encoding="utf-8")

    def snapshot(self, **kwargs):
        options = dict(
            to=self.backups,
            source_db=self.source,
            jobs_dir=self.jobs,
            gallery_dir=self.tmp / "gallery",
            gate_dir=REPO_ROOT / "gate",
        )
        options.update(kwargs)
        return backup.snapshot(**options)

    # --- 1. a WAL-mode snapshot verifies -----------------------------------

    def test_a_snapshot_of_a_wal_database_verifies(self):
        directory, manifest, _ = self.snapshot()
        _, problems = backup.verify(directory)
        self.assertEqual(problems, [], f"a fresh snapshot did not verify: {problems}")
        self.assertEqual(manifest["schema_version"], db.schema_version(self.writer))

    def test_the_snapshot_carries_the_rows_still_in_the_wal(self):
        """The reason this is `Connection.backup()` and not `cp`."""
        self.assertTrue(
            Path(f"{self.source}-wal").is_file(),
            "the fixture has no -wal, so this test would prove nothing",
        )
        directory, _, _ = self.snapshot()
        conn = sqlite3.connect(f"file:{directory / 'sketchgen.db'}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 7)
        finally:
            conn.close()

    def test_the_snapshot_is_one_self_contained_file(self):
        directory, _, _ = self.snapshot()
        self.assertEqual({p.name for p in directory.iterdir()}, EXPECTED_FILES)

    def test_a_snapshot_survives_the_source_being_written_after_it(self):
        directory, manifest, _ = self.snapshot()
        db.enqueue(self.writer, "written after the snapshot", "profcarroll")
        _, problems = backup.verify(directory)
        self.assertEqual(problems, [])
        self.assertEqual(manifest["row_counts"]["jobs"], 7)

    def test_the_gate_travels_with_the_database(self):
        directory, manifest, _ = self.snapshot()
        self.assertIn("gate.tar.gz", manifest["files"])
        with tarfile.open(directory / "gate.tar.gz") as tar:
            names = tar.getnames()
        self.assertIn("gate/sketch_gate.py", names)
        self.assertIn("gate/accept.sh", names)
        self.assertTrue(any(n.startswith("gate/fixtures/") for n in names))

    def test_a_missing_database_is_a_refusal_and_leaves_nothing_behind(self):
        with self.assertRaises(backup.Refusal):
            self.snapshot(source_db=self.tmp / "nowhere.db")
        self.assertEqual(list(self.backups.glob("*")), [])

    # --- 2. retention keeps fourteen ---------------------------------------

    def _fake_snapshots(self, count: int, keep: int = backup.KEEP):
        start = datetime(2026, 1, 1, 4, 10, tzinfo=timezone.utc)
        made = []
        for index in range(count):
            directory, _, removed = self.snapshot(
                now=start + timedelta(days=index), keep=keep
            )
            made.append((directory, removed))
        return made

    def test_retention_keeps_the_newest_fourteen(self):
        self.assertEqual(backup.KEEP, 14)
        made = self._fake_snapshots(17)
        kept = sorted(p.name for p in self.backups.iterdir() if p.is_dir())
        self.assertEqual(len(kept), 14)
        self.assertEqual(kept, sorted(d.name for d, _ in made[-14:]))
        # the three that went are named, so the journal says what was pruned
        self.assertEqual([len(removed) for _, removed in made[:14]], [0] * 14)
        self.assertEqual([len(removed) for _, removed in made[14:]], [1, 1, 1])

    def test_retention_removes_nothing_that_is_not_a_snapshot(self):
        self._fake_snapshots(16)
        note = self.backups / "READ-ME-FIRST.txt"
        note.write_text("the pull runs from the laptop", encoding="utf-8")
        scratch = self.backups / "scratch"
        scratch.mkdir()
        backup.prune(self.backups, keep=1)
        self.assertTrue(note.is_file())
        self.assertTrue(scratch.is_dir())
        self.assertEqual(
            len([p for p in self.backups.glob(backup.STAMP_GLOB) if p.is_dir()]), 1
        )

    def test_two_snapshots_in_the_same_second_do_not_overwrite_each_other(self):
        when = datetime(2026, 1, 1, 4, 10, tzinfo=timezone.utc)
        self.snapshot(now=when)
        with self.assertRaises(backup.Refusal):
            self.snapshot(now=when)

    # --- 3. the manifest's counts match ------------------------------------

    def test_the_manifest_counts_every_table(self):
        directory, manifest, _ = self.snapshot()
        recorded = manifest["row_counts"]
        conn = sqlite3.connect(f"file:{directory / 'sketchgen.db'}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            actual = backup.row_counts(conn)
        finally:
            conn.close()
        self.assertEqual(recorded, actual)
        self.assertEqual(recorded["jobs"], 7)

    def test_the_manifest_counts_job_directories_and_not_stray_files(self):
        _, manifest, _ = self.snapshot()
        self.assertEqual(manifest["jobs_directories"], 3)

    def test_a_missing_jobs_directory_is_null_not_zero(self):
        _, manifest, _ = self.snapshot(jobs_dir=self.tmp / "gone")
        self.assertIsNone(manifest["jobs_directories"])

    def test_the_manifest_records_the_app_commit(self):
        _, manifest, _ = self.snapshot()
        app = manifest["git"]["app"]
        if app is None:
            self.skipTest("this checkout has no git; nothing to record")
        self.assertRegex(app["commit"], r"^[0-9a-f]{40}$")

    def test_the_manifest_timestamp_is_utc_with_a_z(self):
        _, manifest, _ = self.snapshot(
            now=datetime(2026, 9, 15, 4, 10, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(manifest["snapshot_utc"], "2026-09-15T04:10:00Z")

    # --- verify actually looks -------------------------------------------

    def test_verify_catches_a_count_the_manifest_disagrees_with(self):
        directory, _, _ = self.snapshot()
        conn = sqlite3.connect(directory / "sketchgen.db")
        try:
            conn.execute("DELETE FROM jobs WHERE id = (SELECT MIN(id) FROM jobs)")
            conn.commit()
        finally:
            conn.close()
        _, problems = backup.verify(directory)
        self.assertTrue(any("jobs: 6 rows" in p for p in problems), problems)

    def test_verify_catches_a_truncated_database(self):
        directory, _, _ = self.snapshot()
        target = directory / "sketchgen.db"
        data = target.read_bytes()
        target.write_bytes(data[: len(data) // 2])
        _, problems = backup.verify(directory)
        self.assertTrue(problems, "a half a database verified, which it must not")

    def test_verify_refuses_a_directory_that_is_not_a_snapshot(self):
        with self.assertRaises(backup.Refusal):
            backup.verify(self.tmp)

    def test_verify_given_the_backup_root_checks_the_newest(self):
        made = self._fake_snapshots(3)
        result = subprocess.run(
            [sys.executable, str(CLI), "backup", "verify", str(self.backups)],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(made[-1][0].name, result.stdout)

    # --- 4. no secret is in the snapshot -----------------------------------

    def test_no_secret_reaches_the_snapshot_directory(self):
        """The two credentials on the node, planted where a careless walk
        would pick them up, then looked for byte by byte in everything the
        snapshot wrote."""
        ssh = self.tmp / ".ssh"
        ssh.mkdir()
        (ssh / "sketchgen-gallery").write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8"
        )
        (self.tmp / "writepath.token").write_text(TOKEN, encoding="utf-8")

        directory, manifest, _ = self.snapshot()

        self.assertEqual({p.name for p in directory.iterdir()}, EXPECTED_FILES)
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            self.assertNotIn(TOKEN.encode(), blob, f"the bearer is in {path.name}")
            self.assertNotIn(b"PRIVATE KEY", blob, f"a private key is in {path.name}")
        with tarfile.open(directory / "gate.tar.gz") as tar:
            for name in tar.getnames():
                self.assertNotIn(".ssh", name)
                self.assertNotIn("writepath.token", name)
        # and the manifest says out loud what it left behind
        self.assertTrue(any("writepath.token" in line for line in manifest["excluded"]))

    def test_a_credential_inside_the_gate_tree_refuses_the_whole_snapshot(self):
        gate = self.tmp / "gate"
        (gate / "fixtures").mkdir(parents=True)
        (gate / "sketch_gate.py").write_text("# a gate\n", encoding="utf-8")
        (gate / "writepath.token").write_text(TOKEN, encoding="utf-8")
        with self.assertRaises(backup.Refusal):
            self.snapshot(gate_dir=gate)
        self.assertEqual(list(self.backups.glob(backup.STAMP_GLOB)), [])


class CommandLineTests(unittest.TestCase):
    """The subcommand as the timer runs it: exit codes and one summary line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sketchgen-backup-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.source = self.tmp / "sketchgen.db"
        make_database(self.source).close()

    def run_cli(self, *args: str):
        return subprocess.run(
            [sys.executable, str(CLI), *args],
            capture_output=True, text=True, check=False,
            env={**os.environ, "SKETCHGEN_DB": str(self.source)},
        )

    def test_snapshot_then_verify(self):
        to = self.tmp / "backups"
        made = self.run_cli(
            "backup", "snapshot", "--to", str(to),
            "--db", str(self.source),
            "--jobs", str(self.tmp / "jobs"),
            "--gallery-dir", str(self.tmp / "gallery"),
        )
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("verified", made.stdout)
        directories = [p for p in to.glob(backup.STAMP_GLOB) if p.is_dir()]
        self.assertEqual(len(directories), 1)

        checked = self.run_cli("backup", "verify", str(directories[0]))
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertIn("verified", checked.stdout)

    def test_snapshot_of_a_missing_database_refuses_with_3(self):
        result = self.run_cli(
            "backup", "snapshot", "--to", str(self.tmp / "backups"),
            "--db", str(self.tmp / "nowhere.db"),
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("refused", result.stderr)

    def test_verify_of_a_broken_snapshot_exits_1(self):
        to = self.tmp / "backups"
        self.run_cli(
            "backup", "snapshot", "--to", str(to), "--db", str(self.source),
            "--jobs", str(self.tmp / "jobs"), "--gallery-dir", str(self.tmp / "g"),
        )
        directory = next(p for p in to.glob(backup.STAMP_GLOB) if p.is_dir())
        (directory / "sketchgen.db").write_bytes(b"not a database at all")
        result = self.run_cli("backup", "verify", str(directory))
        self.assertEqual(result.returncode, 1)
        self.assertIn("NOT verified", result.stderr)

    def test_push_without_the_oci_cli_refuses_with_instructions(self):
        if shutil.which("oci") is not None:
            self.skipTest("the oci CLI is installed here, so it would try to upload")
        to = self.tmp / "backups"
        self.run_cli(
            "backup", "snapshot", "--to", str(to), "--db", str(self.source),
            "--jobs", str(self.tmp / "jobs"), "--gallery-dir", str(self.tmp / "g"),
        )
        result = self.run_cli("backup", "push", "--bucket", "sketchgen", str(to))
        self.assertEqual(result.returncode, 3)
        self.assertIn("oci", result.stderr)
        self.assertIn("bin/pull-backup.sh", result.stderr)


class BackupUnitTests(unittest.TestCase):
    """The units that run it: a oneshot, a daily timer, 04:10 UTC, no secret."""

    def setUp(self):
        self.service = (UNIT_DIR / "sketchgen-backup.service").read_text(encoding="utf-8")
        self.timer = (UNIT_DIR / "sketchgen-backup.timer").read_text(encoding="utf-8")

    def test_the_service_is_a_oneshot_that_snapshots_into_the_node_backups(self):
        self.assertIn("Type=oneshot", self.service)
        self.assertIn("backup snapshot --to %h/sketchgen/backups", self.service)
        self.assertNotIn("[Install]", self.service)  # the timer starts it, not enable

    def test_the_timer_runs_daily_at_0410_utc_and_catches_up(self):
        self.assertIn("OnCalendar=*-*-* 04:10:00 UTC", self.timer)
        self.assertIn("Persistent=true", self.timer)
        self.assertIn("Unit=sketchgen-backup.service", self.timer)
        self.assertIn("WantedBy=timers.target", self.timer)

    def test_neither_unit_carries_a_credential(self):
        for name, text in (("service", self.service), ("timer", self.timer)):
            for needle in ("PRIVATE KEY", "API_KEY", "TOKEN=", "PASSWORD", "Bearer"):
                self.assertNotIn(needle, text, f"the backup {name} looks like a secret")

    def test_update_sh_installs_the_units(self):
        text = (REPO_ROOT / "update.sh").read_text(encoding="utf-8")
        self.assertIn("bin/sketchgen install-unit", text)

    def test_update_sh_hands_over_to_the_copy_it_pulled(self):
        """A pull that changes update.sh must not finish as the old script.

        Twice (2026-09-16, 2026-09-18) a deploy pulled a new step into this
        file and then ran on without it, because bash keeps the file it
        opened. The hash is taken before the pull and checked after; a
        change execs the pulled copy under a guard so it cannot loop. Since
        2026-09-24 the pull is a move to the node's target — a fast-forward to
        main or a switch to its pin — and tests/test_update_sh.py runs it.
        """
        text = (REPO_ROOT / "update.sh").read_text(encoding="utf-8")
        pull = text.index("git merge --quiet --ff-only origin/main")
        self.assertIn("self_before=$(script_hash)", text[:pull])
        after = text[pull:]
        self.assertIn('exec bash "$SELF" "$@"', after)
        self.assertIn("SKETCHGEN_UPDATE_REEXEC", after)
        self.assertLess(after.index('exec bash "$SELF"'), after.index("control pause"))

    def test_install_unit_knows_about_them(self):
        text = (REPO_ROOT / "bin" / "sketchgen").read_text(encoding="utf-8")
        self.assertIn("sketchgen-backup.service", text)
        self.assertIn("sketchgen-backup.timer", text)


class RunbookTests(unittest.TestCase):
    """docs/OPERATIONS.md's Recovery section — spec §3.5.

    The rehearsal itself needs a throwaway instance that an operator has to
    provision, so it cannot happen in a test. What a test can do is refuse to
    let the section pretend: the placeholder line has to be there, word for
    word, until somebody replaces it with a date.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = (REPO_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")

    def test_there_is_a_recovery_section(self):
        self.assertIn("\n## Recovery\n", self.text)

    def test_the_rehearsal_line_is_the_first_thing_in_it(self):
        after = self.text.split("\n## Recovery\n", 1)[1].lstrip("\n")
        first = after.splitlines()[0]
        self.assertTrue(
            first == "Last rehearsed: not yet" or first.startswith("Last rehearsed: 20"),
            "the Recovery section must open with `Last rehearsed: not yet` until "
            f"a real rehearsal date replaces it; it opens with {first!r}",
        )

    def test_every_step_the_runbook_names_is_a_command(self):
        section = self.text.split("\n## Recovery\n", 1)[1].split("\n## ", 1)[0]
        for needed in (
            "python3 -m venv",
            "playwright install chromium",
            "ollama pull qwen3-coder:30b-a3b-q4_K_M",
            "ollama pull gemma4:e4b",
            "backup verify",
            "rsync -a",
            "sketchgen keygen app",
            "sketchgen keygen gallery",
            "writepath.token",
            "install-unit",
            "systemctl --user enable --now sketchgen-backup.timer",
            "accept.sh",
            "sketchgen db status",
        ):
            self.assertIn(needed, section, f"the runbook has no command for: {needed}")


if __name__ == "__main__":
    unittest.main()
