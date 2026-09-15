"""`sketchgen backup` — a verified copy of the record, made on a node that can vanish.

Packet 0.3. The database and the attempt archive exist on one Oracle free-tier
instance, and the free tier may reclaim it. Everything else is already off the
box — the code is pushed, the gallery is pushed, votes and likes are in D1 — but
`sketchgen.db` holds 1,114 agent judgments, 158 critiques and 191 lineage rows
that exist nowhere else, and `jobs/` holds every attempt's code and evidence.
Nothing new ships until a verified nightly copy leaves the instance.

Three subcommands:

    sketchgen backup snapshot --to DIR    one dated snapshot, then prune to 14
    sketchgen backup verify PATH          integrity_check + counts against the manifest
    sketchgen backup push --bucket NAME   optional second copy, through the oci CLI

**Why `Connection.backup()` and not `cp`.** The database runs in WAL mode. A file
copy of a WAL database taken while the worker is writing can be torn: the copier
sees a `.db` from one moment and misses the `-wal` that completes it, and the
result opens fine and is silently short. SQLite's online backup API copies pages
under the same locks the writer uses and retries the ones that moved, so the
snapshot is a consistent database as of a single point in time, taken without
stopping the worker.

**What is not in here.** `jobs/` is 300 MB and only grows; a nightly tarball of
it is the wrong shape, so it is rsynced by `bin/pull-backup.sh` instead and the
manifest records only how many job directories there were, so a pull can be
checked against it. The models, the venv and the `dev-*` scratch directories are
rebuilt or abandoned, not backed up.

**What must never be in here.** `~/.ssh` and `~/sketchgen/writepath.token` are
the two credentials on the node. A backup that carries them turns every copy of
the backup into a copy of the credentials, and the backup is the artefact most
likely to be handed around. Nothing here reads either path; `_forbidden` below
refuses the whole snapshot if one ever appears inside the gate tree, which is
the only directory this command walks.

Exit codes are the project's: 0 success, 1 failure, 3 refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from sketchgen import db

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3

#: How many dated snapshot directories stay in the destination. Fourteen is two
#: weeks: long enough that a corruption noticed on a Monday can be walked back
#: past the weekend that hid it, short enough that 14 × 6 MB stays trivial.
KEEP = 14

#: The snapshot directory's name. The project's timestamps are ISO-8601 UTC with
#: a Z and colons (`db.utc_now`), and the manifest carries one of those verbatim.
#: The *directory* drops the colons: a local path containing one is a `host:path`
#: to scp, and these directories exist to be copied between machines.
STAMP_FORMAT = "%Y-%m-%dT%H%M%SZ"
STAMP_GLOB = "????-??-??T??????Z"

DB_NAME = "sketchgen.db"
GATE_NAME = "gate.tar.gz"
MANIFEST_NAME = "manifest.json"

#: Never copied, never walked into, and grounds for refusing a snapshot outright.
FORBIDDEN_NAMES = (".ssh", "writepath.token", "id_rsa", "id_ed25519")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Refusal(Exception):
    """Something the backup will not do; exit 3, nothing written."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def sha256_of(path: Path) -> str:
    """The digest of a file, read in chunks so a 300 MB tarball does not
    have to be held in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _forbidden(path: Path) -> bool:
    """True if any part of this path is a credential we refuse to carry."""
    return any(part in FORBIDDEN_NAMES for part in path.parts)


def _git(repo: Path) -> dict[str, object] | None:
    """The commit a checkout is on, or None when it is not a checkout.

    Recorded so that a restored snapshot can be paired with the code that wrote
    it: a database is only readable by a schema it has migrations for.
    """
    if not (repo / ".git").exists():
        return None
    git = shutil.which("git")
    if git is None:
        return None

    def ask(*args: str) -> str | None:
        result = subprocess.run(
            [git, "-C", str(repo), *args],
            capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    commit = ask("rev-parse", "HEAD")
    if commit is None:
        return None
    status = ask("status", "--porcelain")
    return {
        "path": str(repo),
        "commit": commit,
        "branch": ask("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(status),
    }


def _job_count(jobs_dir: Path) -> int | None:
    """How many job directories there were, so a pull of `jobs/` can be checked.

    None when the directory is not there at all, which is a fact worth keeping
    in the manifest rather than a zero that reads like an empty archive.
    """
    if not jobs_dir.is_dir():
        return None
    return sum(1 for child in jobs_dir.iterdir() if child.is_dir())


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


def copy_database(source: Path, dest: Path) -> None:
    """Copy a live WAL-mode database through SQLite's online backup API."""
    if not source.is_file():
        raise Refusal(f"no database at {source}")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            # The copy inherits WAL from the source, which would leave a -wal
            # and a -shm beside it and make the snapshot three files that have
            # to travel together. A snapshot is an artefact, not a working
            # database: checkpoint it into one file that rsync, scp and a
            # read-only open can each handle alone.
            dst.execute("PRAGMA journal_mode = DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    for leftover in (Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        leftover.unlink(missing_ok=True)


def archive_gate(gate_dir: Path, dest: Path) -> bool:
    """Tar the gate directory beside the database. False when there is none.

    Since packet 0.2 the gate is tracked in the repo, so this tarball is no
    longer the only copy of it — it is here so that one snapshot restores a
    working pipeline without also needing the right commit of the repo to hand.
    """
    if not gate_dir.is_dir():
        return False
    for path in sorted(gate_dir.rglob("*")):
        if _forbidden(path):
            raise Refusal(f"refusing to archive a credential: {path}")
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(
            gate_dir,
            arcname="gate",
            filter=lambda info: None if _forbidden(Path(info.name)) else info,
        )
    return True


def row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Every table and its row count, name order — `db.table_counts` as a dict."""
    return {name: count for name, count in db.table_counts(conn)}


def write_manifest(
    directory: Path,
    *,
    stamp: datetime,
    source_db: Path,
    jobs_dir: Path,
    gallery_dir: Path,
    gate_dir: Path,
    gate_archived: bool,
) -> dict:
    """Describe the snapshot well enough that `verify` can call it a lie."""
    snapshot_db = directory / DB_NAME
    conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        counts = row_counts(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()

    files: dict[str, dict] = {
        DB_NAME: {
            "bytes": snapshot_db.stat().st_size,
            "sha256": sha256_of(snapshot_db),
        }
    }
    if gate_archived:
        archive = directory / GATE_NAME
        files[GATE_NAME] = {
            "bytes": archive.stat().st_size,
            "sha256": sha256_of(archive),
        }

    manifest = {
        "snapshot_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": os.uname().nodename,
        "source": {
            "db": str(source_db),
            "jobs": str(jobs_dir),
            "gate": str(gate_dir),
        },
        "files": files,
        "schema_version": version,
        "row_counts": counts,
        # jobs/ is rsynced, not tarred; this is the number a pull is checked
        # against. null means the directory was not on this machine at all.
        "jobs_directories": _job_count(jobs_dir),
        "git": {"app": _git(REPO_ROOT), "gallery": _git(gallery_dir)},
        "excluded": [
            "jobs/ — rsynced by bin/pull-backup.sh, not tarred nightly",
            "~/.ssh — deploy keys, never copied",
            "writepath.token — the write-path bearer, never copied",
            "models, .venv, dev-* — rebuilt or abandoned, not the record",
        ],
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return manifest


def prune(root: Path, keep: int = KEEP) -> list[Path]:
    """Remove all but the newest `keep` snapshot directories. Returns what went.

    Only directories whose names are this command's own timestamps are counted
    or removed, so a note, a lock file or somebody's `scratch/` in the backup
    directory is never deleted by a retention pass.
    """
    snapshots = sorted(
        (p for p in root.glob(STAMP_GLOB) if p.is_dir() and (p / MANIFEST_NAME).is_file()),
        key=lambda p: p.name,
    )
    removed: list[Path] = []
    for path in snapshots[: max(0, len(snapshots) - keep)]:
        shutil.rmtree(path)
        removed.append(path)
    return removed


def snapshot(
    *,
    to: Path,
    source_db: Path,
    jobs_dir: Path,
    gallery_dir: Path,
    gate_dir: Path,
    keep: int = KEEP,
    now: datetime | None = None,
) -> tuple[Path, dict, list[Path]]:
    """One dated snapshot in `to`, then a retention pass. The whole of 0.3."""
    stamp = now or _utc_now()
    to.mkdir(parents=True, exist_ok=True)
    directory = to / stamp.strftime(STAMP_FORMAT)
    if directory.exists():
        raise Refusal(f"a snapshot already exists at {directory}")
    directory.mkdir()
    try:
        copy_database(source_db, directory / DB_NAME)
        gate_archived = archive_gate(gate_dir, directory / GATE_NAME)
        manifest = write_manifest(
            directory,
            stamp=stamp,
            source_db=source_db,
            jobs_dir=jobs_dir,
            gallery_dir=gallery_dir,
            gate_dir=gate_dir,
            gate_archived=gate_archived,
        )
    except Exception:
        # A half-written snapshot is worse than none: it would be the newest,
        # and the newest is the one recovery reaches for.
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return directory, manifest, prune(to, keep)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _resolve_snapshot(path: Path) -> Path:
    """Accept the snapshot directory, its manifest, or its database."""
    if path.is_dir():
        return path
    if path.is_file() and path.name in (MANIFEST_NAME, DB_NAME):
        return path.parent
    raise Refusal(f"no snapshot at {path}")


def verify(path: Path) -> tuple[Path, list[str]]:
    """Open a snapshot read-only and try to prove it wrong. Returns its problems.

    An empty list is a verified snapshot. This is the only claim the nightly
    timer makes that is worth anything: a backup nobody has opened is a belief,
    not a backup.
    """
    directory = _resolve_snapshot(path)
    manifest_path = directory / MANIFEST_NAME
    if not manifest_path.is_file():
        raise Refusal(f"no {MANIFEST_NAME} in {directory}")
    snapshot_db = directory / DB_NAME
    if not snapshot_db.is_file():
        raise Refusal(f"no {DB_NAME} in {directory}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refusal(f"{manifest_path} is not readable JSON: {exc}") from exc

    problems: list[str] = []

    for name, recorded in manifest.get("files", {}).items():
        target = directory / name
        if not target.is_file():
            problems.append(f"{name}: in the manifest, not in the directory")
            continue
        size = target.stat().st_size
        if size != recorded.get("bytes"):
            problems.append(f"{name}: {size} bytes, manifest says {recorded.get('bytes')}")
        digest = sha256_of(target)
        if digest != recorded.get("sha256"):
            problems.append(f"{name}: sha256 {digest[:12]}…, manifest says "
                            f"{str(recorded.get('sha256'))[:12]}…")

    conn = sqlite3.connect(f"file:{snapshot_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        verdict = [str(row[0]) for row in rows]
        if verdict != ["ok"]:
            problems.append("integrity_check: " + "; ".join(verdict[:5]))
        version = db.schema_version(conn)
        if version != manifest.get("schema_version"):
            problems.append(
                f"schema version {version}, manifest says {manifest.get('schema_version')}"
            )
        counts = row_counts(conn)
    except sqlite3.DatabaseError as exc:
        conn.close()
        problems.append(f"the database will not open: {exc}")
        return directory, problems
    finally:
        conn.close()

    recorded_counts = manifest.get("row_counts", {})
    for table, expected in sorted(recorded_counts.items()):
        if table not in counts:
            problems.append(f"table {table}: in the manifest, not in the snapshot")
        elif counts[table] != expected:
            problems.append(f"{table}: {counts[table]} rows, manifest says {expected}")
    for table in sorted(set(counts) - set(recorded_counts)):
        problems.append(f"table {table}: in the snapshot, not in the manifest")

    return directory, problems


# ---------------------------------------------------------------------------
# push (optional second copy)
# ---------------------------------------------------------------------------


def push(directory: Path, bucket: str, *, dry_run: bool = False) -> tuple[int, str]:
    """Upload one snapshot to Oracle Object Storage through the `oci` CLI.

    Deliberately a thin wrapper and deliberately not required: the copy that
    matters is the pull to the operator's machine (`bin/pull-backup.sh`), which
    needs no credential on the node. This is the copy that survives the
    operator's machine too, for whoever wants it, and it is the only thing here
    that would put an Oracle credential back on the instance — so it stays
    behind a flag and says so plainly when the CLI is absent.
    """
    snapshot_dir = _resolve_snapshot(directory)
    oci = shutil.which("oci")
    if oci is None:
        raise Refusal(
            "the `oci` CLI is not installed, so there is nothing to push with. "
            "This second copy is optional: bin/pull-backup.sh is the copy that "
            "leaves the instance, and it needs no credential here. To enable "
            "this one: install the CLI (https://docs.oracle.com/iaas/), run "
            "`oci setup config`, and re-run with --bucket."
        )
    prefix = f"backups/{snapshot_dir.name}"
    command = [
        oci, "os", "object", "bulk-upload",
        "--bucket-name", bucket,
        "--src-dir", str(snapshot_dir),
        "--object-prefix", prefix + "/",
        "--no-multipart",
    ]
    if dry_run:
        return EXIT_OK, "would run: " + " ".join(command)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return EXIT_FAIL, (result.stderr.strip() or "oci bulk-upload failed")
    return EXIT_OK, f"pushed {snapshot_dir} to {bucket}/{prefix}/"


# ---------------------------------------------------------------------------
# The CLI surface
# ---------------------------------------------------------------------------


def _default_node_home() -> Path:
    return Path(os.environ.get("SKETCHGEN_HOME", str(Path.home() / "sketchgen")))


def cmd_snapshot(args: argparse.Namespace) -> int:
    try:
        directory, manifest, removed = snapshot(
            to=Path(args.to).expanduser(),
            source_db=Path(args.db).expanduser(),
            jobs_dir=Path(args.jobs).expanduser(),
            gallery_dir=Path(args.gallery_dir).expanduser(),
            gate_dir=Path(args.gate).expanduser(),
            keep=args.keep,
        )
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    size = manifest["files"][DB_NAME]["bytes"]
    jobs = manifest["jobs_directories"]
    print(
        f"{directory}  {size} bytes  schema {manifest['schema_version']}  "
        f"{sum(manifest['row_counts'].values())} rows  "
        f"{'?' if jobs is None else jobs} job directories"
    )
    for path in removed:
        print(f"pruned {path}")

    # Verify what was just written, here, rather than trusting the nightly timer
    # to have been right. A snapshot that cannot be opened must fail the unit.
    _, problems = verify(directory)
    if problems:
        for problem in problems:
            print(f"UNVERIFIED {problem}", file=sys.stderr)
        return EXIT_FAIL
    print(f"verified {directory}")
    return EXIT_OK


def _newest(root: Path) -> Path | None:
    snapshots = sorted(
        (p for p in root.glob(STAMP_GLOB) if p.is_dir()), key=lambda p: p.name
    )
    return snapshots[-1] if snapshots else None


def cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser()
    if path.is_dir() and not (path / MANIFEST_NAME).is_file():
        newest = _newest(path)
        if newest is None:
            print(f"refused: no snapshot in {path}", file=sys.stderr)
            return EXIT_REFUSED
        path = newest
    try:
        directory, problems = verify(path)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OSError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if problems:
        print(f"{directory}: NOT verified", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_FAIL
    print(f"{directory}: verified")
    return EXIT_OK


def cmd_push(args: argparse.Namespace) -> int:
    path = Path(args.path or args.to).expanduser()
    if path.is_dir() and not (path / MANIFEST_NAME).is_file():
        newest = _newest(path)
        if newest is None:
            print(f"refused: no snapshot in {path}", file=sys.stderr)
            return EXIT_REFUSED
        path = newest
    try:
        code, message = push(path, args.bucket, dry_run=args.dry_run)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(message, file=sys.stderr if code else sys.stdout)
    return code


def register(top: argparse._SubParsersAction) -> None:
    backup = top.add_parser(
        "backup",
        help="snapshot the database and the gate, verify a snapshot, push a copy",
        description=(
            "Retention and recovery. The database and the attempt archive live "
            "on one instance that its provider may reclaim; these are the "
            "commands that get a verified copy off it."
        ),
    )
    inner = backup.add_subparsers(dest="backup_command", metavar="{snapshot,verify,push}")
    home = _default_node_home()

    snap = inner.add_parser(
        "snapshot",
        help="write one dated snapshot and prune to the newest 14",
        description=(
            "Copy the database through SQLite's online backup API (a file copy "
            "of a WAL database can be torn), tar the gate beside it, write a "
            "manifest of sizes, digests, schema version, row counts, job "
            "directory count and both git commits, then verify what was "
            "written and remove all but the newest snapshots. Credentials are "
            "never copied."
        ),
    )
    snap.add_argument(
        "--to", required=True, metavar="D",
        help="the backup root; each run makes one dated directory under it",
    )
    snap.add_argument(
        "--db", default=db.DEFAULT_DB_PATH, metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    snap.add_argument(
        "--jobs", default=os.environ.get("SKETCHGEN_JOBS", str(home / "jobs")),
        metavar="D",
        help="the attempt archive, counted but never tarred (default: $SKETCHGEN_JOBS)",
    )
    snap.add_argument(
        "--gallery-dir",
        default=os.environ.get("SKETCHGEN_GALLERY", str(home / "gallery")),
        metavar="D",
        help="the gallery checkout, for its commit (default: $SKETCHGEN_GALLERY)",
    )
    snap.add_argument(
        "--gate", default=str(REPO_ROOT / "gate"), metavar="D",
        help="the gate directory to tar (default: the repo's own gate/)",
    )
    snap.add_argument(
        "--keep", type=int, default=KEEP, metavar="N",
        help=f"how many snapshots to keep (default {KEEP})",
    )
    snap.set_defaults(func=cmd_snapshot, _parser=snap)

    check = inner.add_parser(
        "verify",
        help="integrity_check a snapshot and compare its counts with the manifest",
        description=(
            "Open a snapshot read-only, run PRAGMA integrity_check, re-digest "
            "every file the manifest names and compare every row count against "
            "it. Given a backup root rather than a snapshot, verifies the "
            "newest one. Exit 1 on any disagreement."
        ),
    )
    check.add_argument(
        "path", help="a snapshot directory, its manifest.json, or the backup root"
    )
    check.set_defaults(func=cmd_verify, _parser=check)

    away = inner.add_parser(
        "push",
        help="optional second copy to Oracle Object Storage through the oci CLI",
        description=(
            "Upload one snapshot to an object storage bucket. Optional and not "
            "wired into any timer: the copy that matters is the pull to the "
            "operator's machine (bin/pull-backup.sh), which needs no credential "
            "on the node. Refuses with instructions when the oci CLI is absent."
        ),
    )
    away.add_argument("--bucket", required=True, metavar="NAME", help="the bucket")
    away.add_argument(
        "path", nargs="?", default=None,
        help="a snapshot directory, or a backup root whose newest is pushed",
    )
    away.add_argument(
        "--to", default=str(home / "backups"), metavar="D",
        help="the backup root, when no path is given",
    )
    away.add_argument(
        "--dry-run", action="store_true", help="print the oci command, upload nothing"
    )
    away.set_defaults(func=cmd_push, _parser=away)

    def _needs_subcommand(args: argparse.Namespace) -> int:
        backup.print_help()
        return EXIT_FAIL

    backup.set_defaults(func=_needs_subcommand, _parser=backup)
