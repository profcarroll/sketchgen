"""``sketchgen import``: entries another node made, brought into this one's tables.

docs/plans/gpu-fold-in.md, Packet 1. The rented A10 (`sld-gpu`) made 3,518
sketches and published none; it was terminated on 2026-09-27 and its whole
``~/sketchgen`` survives as an archive: a ``backups/<stamp>/`` of verified
snapshots and a ``jobs/`` of every attempt, frames included. Bringing a finished
archive into one node is an import, not a hub: each entry becomes an ordinary
``held`` entry here, under a new id from this node's sequence, with the node and
its ids there kept beside it (migration 019). Everything downstream — Held, the
personal-data scan, publish, the judge — treats it as one.

    sketchgen import list --from DIR [filters] [--ids | --json]
    sketchgen import run  --from DIR --node NAME --ids FILE --by LOGIN [--note …] [--dry-run]

``list`` only reads. ``run`` takes the ids it is given and nothing else: which
sketches are worth bringing is a person's choice (§1.3), made on a local render
(local-gallery.md §4) whose *Copy picked ids* writes the file ``--ids`` reads.

The archive's database is its newest snapshot, opened read-only. Its rows name
the node they were written on (``/home/ubuntu/sketchgen/jobs/412/…``, which on
sld-cloud is sld-cloud's own job 412), so nothing here follows a stored path into
the archive: a job's directory is ``<from>/jobs/<its id>``, and a path is copied
only after its ``…/jobs/<old id>`` prefix has been replaced by this node's
``jobs/<new id>``. A path that does not lie under its own job's directory fails
that entry rather than travel unrewritten.

Exit codes as everywhere: 0 ok, 1 failed (``run``: any entry failed), 3 refused
with nothing written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sketchgen import db
from sketchgen import gallery
from sketchgen.cli import backup
from sketchgen.cli import renderlocal

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: The worker's job directories, as ``paid`` reads them.
DEFAULT_JOBS_DIR = os.environ.get(
    "SKETCHGEN_JOBS", os.path.join(os.path.expanduser("~"), "sketchgen", "jobs")
)

#: Free space ``run`` wants beyond the selection itself. The jobs directory
#: shares its disk with the database, its WAL, the logs and the next job.
MARGIN_BYTES = 2 * 1024 ** 3

#: One progress line on stderr per this many entries. An import of the whole
#: archive is 3,255 entries; a line each is a scroll nobody reads.
PROGRESS_EVERY = 100

#: A fleet name: what the page will print as *Made on* (Packet 2), so public by
#: construction. Lower case, digits and dashes, as every node's name is.
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

#: Harness 4 read a buffer as the sketch: a kept sketch that calls
#: createGraphics() has the buffer's is_looping and size on its report
#: (AGENTS.md, "Working on the code"). About 317 of the rental's jobs.
BUFFER_RE = re.compile(rb"\bcreateGraphics\s*\(")

#: The widths of the prompt and executor columns in ``list``'s table. The
#: unsloth quantisations' names are 50 characters; ``--json`` has them whole.
PROMPT_WIDTH = 36
EXECUTOR_WIDTH = 28


class Refused(Exception):
    """Exit 3, nothing written."""


class Failed(Exception):
    """This entry did not come; the run goes on."""


class Occupied(Failed):
    """This node's next job directory already exists: every entry after would meet it."""


# ---------------------------------------------------------------------------
# The archive
# ---------------------------------------------------------------------------


@dataclass
class Archive:
    root: Path
    jobs: Path
    snapshot: Path
    manifest: dict[str, Any]
    problems: list[str]
    conn: sqlite3.Connection
    columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def stamp(self) -> str:
        return self.snapshot.name

    @property
    def sha256(self) -> str | None:
        return (self.manifest.get("files", {}).get(backup.DB_NAME) or {}).get("sha256")

    def close(self) -> None:
        self.conn.close()


def newest_snapshot(root: Path) -> Path:
    found = sorted(p for p in (root / "backups").glob(backup.STAMP_GLOB)
                   if (p / backup.DB_NAME).is_file())
    if not found:
        raise Refused(f"no snapshot under {root / 'backups'}: --from is the archive's "
                      "~/sketchgen, which holds backups/ and jobs/")
    return found[-1]


def open_archive(root: Path) -> Archive:
    """The newest snapshot under ``root``, verified, opened read-only."""
    root = root.expanduser()
    if not root.is_dir():
        raise Refused(f"no archive at {root}")
    jobs = root / "jobs"
    if not jobs.is_dir():
        raise Refused(f"no jobs directory at {jobs}")
    snapshot = newest_snapshot(root)
    try:
        _, problems = backup.verify(snapshot)
        manifest = json.loads((snapshot / backup.MANIFEST_NAME).read_text(encoding="utf-8"))
        conn = renderlocal.open_readonly(snapshot / backup.DB_NAME, jobs)
    except (backup.Refusal, renderlocal.Refused, ValueError) as exc:
        raise Refused(str(exc)) from exc
    archive = Archive(root=root, jobs=jobs, snapshot=snapshot, manifest=manifest,
                      problems=problems, conn=conn)
    for table in db.IMPORT_NEVER:
        archive.columns[table] = db.table_columns(conn, table)
    return archive


def held_rows(archive: Archive) -> list[sqlite3.Row]:
    # main.: render-local's views shadow these names with remapped paths, and
    # an import copies what the archive wrote, then rewrites it itself.
    return archive.conn.execute(
        "SELECT * FROM main.entries WHERE state = 'held' ORDER BY id"
    ).fetchall()


def rewrite(value: str | None, old_job: int, new_dir: Path) -> str | None:
    """``…/jobs/<old_job>/rest`` as ``<new_dir>/rest``; anything else fails the entry."""
    if value is None:
        return None
    match = re.match(rf"^.*?/jobs/{int(old_job)}(?=/|$)", str(value))
    if not match:
        raise Failed(f"path {value!r} is not under its own job's directory, jobs/{old_job}")
    return str(new_dir) + str(value)[match.end():]


def calls_buffer(archive: Archive, row: sqlite3.Row) -> bool | None:
    """Whether the kept sketch calls createGraphics(); None when it cannot be read."""
    try:
        source = rewrite(row["source_dir"], int(row["job_id"]),
                         archive.jobs / str(int(row["job_id"])))
    except Failed:
        return None
    if source is None:
        return None
    try:
        return bool(BUFFER_RE.search((Path(source) / "sketch.js").read_bytes()))
    except OSError:
        return None


def copy_job_dir(source: Path, staging: Path) -> None:
    """The job directory byte for byte, frames included: the judge and the critic
    read the PNGs. A link is copied as a link, never followed out of the archive."""
    shutil.copytree(source, staging, symlinks=True)


def tree_bytes(path: Path) -> int:
    total = 0
    for top, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------
# This node
# ---------------------------------------------------------------------------


def open_node_readonly(path: Path) -> sqlite3.Connection | None:
    """This node's database for reading only, or None when there is none here."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def published_twins(conn: sqlite3.Connection | None) -> dict[str, int]:
    """Prompt → the earliest published entry here with exactly that prompt."""
    if conn is None:
        return {}
    return {row["prompt"]: int(row["id"]) for row in conn.execute(
        "SELECT prompt, MIN(id) AS id FROM entries WHERE state = 'published' "
        "AND prompt IS NOT NULL GROUP BY prompt")}


def imported_ids(conn: sqlite3.Connection | None, node: str) -> dict[int, int]:
    """Origin entry id → this node's id, for everything already imported from ``node``."""
    if conn is None or db.schema_version(conn) < 19:
        return {}
    return {int(row["origin_entry_id"]): int(row["id"]) for row in conn.execute(
        "SELECT id, origin_entry_id FROM entries WHERE origin_node = ?", (node,))}


def check_node_name(node: str) -> None:
    if not NODE_RE.match(node):
        raise Refused(f"--node {node!r}: a fleet name is lower case letters, digits and "
                      "dashes, at most 32")
    # The name goes on public pages (Packet 2); the publish scan refuses
    # Oracle's hostnames, and an origin called one would be refused there.
    if gallery.HOSTNAME_MARK in node:
        raise Refused(f"--node {node!r} looks like a cloud hostname "
                      f"({gallery.HOSTNAME_MARK!r}); use the node's fleet name")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[:width - 1] + "…"


def _first_line(text: str | None, width: int = PROMPT_WIDTH) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    return _clip(line, width)


def select(archive: Archive, args: argparse.Namespace,
           node_conn: sqlite3.Connection | None) -> tuple[list[dict[str, Any]], int]:
    """The held entries the filters keep, each as a dict, and how many were held."""
    rows = held_rows(archive)
    twins = published_twins(node_conn)
    done = imported_ids(node_conn, args.node) if args.node else {}
    prompt_of = None
    if args.prompt_of is not None:
        found = node_conn.execute("SELECT prompt FROM entries WHERE id = ?",
                                  (args.prompt_of,)).fetchone() if node_conn else None
        if found is None:
            raise Refused(f"--prompt-of {args.prompt_of}: no such entry on this node")
        prompt_of = found["prompt"]

    kept: list[dict[str, Any]] = []
    for row in rows:
        if args.clean and row["offplan_json"]:
            continue
        if args.executor and row["executor"] != args.executor:
            continue
        if args.rules and row["rules_file"] != args.rules:
            continue
        if prompt_of is not None and row["prompt"] != prompt_of:
            continue
        if args.not_imported and int(row["id"]) in done:
            continue
        buffers = calls_buffer(archive, row)
        if args.no_buffers and buffers:
            continue
        kept.append({
            "origin_entry_id": int(row["id"]),
            "origin_job_id": int(row["job_id"]),
            "prompt": row["prompt"],
            "executor": row["executor"],
            "planner": row["planner"],
            "rules_file": row["rules_file"],
            "attempts": row["attempts"],
            "offplan": bool(row["offplan_json"]),
            "harness_version": row["harness_version"],
            "created_utc": row["created_utc"],
            "buffers": buffers,
            "here": twins.get(row["prompt"]),
            "imported_as": done.get(int(row["id"])),
        })
    if args.one_per_prompt:
        # The earliest of each prompt among what the other filters kept:
        # with --clean, the earliest clean one (§2.2).
        seen: set[str] = set()
        first: list[dict[str, Any]] = []
        for item in sorted(kept, key=lambda i: (i["created_utc"] or "", i["origin_entry_id"])):
            if item["prompt"] in seen:
                continue
            seen.add(item["prompt"])
            first.append(item)
        kept = sorted(first, key=lambda i: i["origin_entry_id"])
    return kept, len(rows)


def cmd_list(args: argparse.Namespace) -> int:
    node_conn = None
    try:
        if args.not_imported and not args.node:
            raise Refused("--not-imported needs --node: which node's imports to leave out")
        archive = open_archive(Path(args.source))
        node_conn = open_node_readonly(Path(args.db).expanduser())
        try:
            items, held = select(archive, args, node_conn)
        finally:
            archive.close()
    except (Refused, sqlite3.Error, OSError) as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        if node_conn is not None:
            node_conn.close()

    verified = ("verified" if not archive.problems
                else f"NOT verified: {'; '.join(archive.problems[:3])}")
    footer = f"{len(items)} listed of {held} held (newest snapshot {archive.stamp}, {verified})"
    if node_conn is None:
        footer += f"; no database at {args.db}, so `here` is blank"
    if args.json:
        print(json.dumps({"snapshot": archive.stamp, "verified": not archive.problems,
                          "problems": archive.problems, "held": held, "entries": items},
                         sort_keys=True))
        print(footer, file=sys.stderr)
        return EXIT_OK
    if args.ids:
        for item in items:
            print(item["origin_entry_id"])
        print(footer, file=sys.stderr)
        return EXIT_OK
    executor_width = min(EXECUTOR_WIDTH,
                         max([len("executor")] + [len(i["executor"] or "") for i in items]))
    print(f"{'origin':>6}  {'prompt':<{PROMPT_WIDTH}}  {'executor':<{executor_width}}  "
          f"{'rules':<9}  {'att':>3}  {'buffers':<7}  here")
    for item in items:
        buffers = {True: "yes", False: "-", None: "?"}[item["buffers"]]
        here = f"e/{item['here']}" if item["here"] else "-"
        print(f"{item['origin_entry_id']:>6}  {_first_line(item['prompt']):<{PROMPT_WIDTH}}  "
              f"{_clip(item['executor'] or '-', executor_width):<{executor_width}}  "
              f"{item['rules_file'] or '-':<9}  "
              f"{item['attempts'] if item['attempts'] is not None else '-':>3}  "
              f"{buffers:<7}  {here}")
    print(footer)
    return EXIT_OK


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _read_ids(spec: str) -> frozenset[int]:
    if spec == "-":
        return renderlocal.parse_ids(sys.stdin.read(), "stdin")
    path = Path(spec).expanduser()
    try:
        return renderlocal.parse_ids(path.read_text(encoding="utf-8"), str(path))
    except OSError as exc:
        raise Refused(f"--ids {spec}: {exc}") from exc


def _stray_job_dirs(jobs: Path, last_job: int) -> list[str]:
    """Job directories numbered past the last job row: an id the import would take."""
    return sorted((p.name for p in jobs.iterdir()
                   if p.is_dir() and p.name.isdigit() and int(p.name) > last_job),
                  key=int)


def _origin_fields(archive: Archive) -> dict[str, Any]:
    conn = archive.conn
    shapes = [row[0] for row in conn.execute(
        "SELECT shape FROM main.entries WHERE shape IS NOT NULL "
        "GROUP BY shape ORDER BY COUNT(*) DESC")]
    span = conn.execute(
        "SELECT MIN(created_utc), MAX(updated_utc) FROM main.jobs").fetchone()
    app = (archive.manifest.get("git") or {}).get("app") or {}
    return {
        "shape": " / ".join(shapes) or None,
        "first_utc": span[0],
        "last_utc": span[1],
        "build": app.get("commit"),
        "snapshot": archive.stamp,
        "snapshot_sha256": archive.sha256,
    }


@dataclass
class Plan:
    """What ``run`` will do, worked out before anything is written."""

    rows: list[sqlite3.Row]
    already: dict[int, int]
    sizes: dict[int, int]
    origin: sqlite3.Row | None
    register: dict[str, Any] | None


def plan_run(args: argparse.Namespace, archive: Archive, conn: sqlite3.Connection,
             jobs: Path, ids: frozenset[int]) -> Plan:
    """Every refusal ``run`` makes, in the order the plan lists them (§2.3)."""
    control = db.get_control(conn)
    if control is None or control.state != "paused":
        state = control.state if control else "unknown"
        raise Refused(
            f"the generator is {state}. The worker must not issue job ids or write "
            "jobs/ underneath an import: `control pause --reason import`, wait for "
            "`db status` to say paused, then import, then `control resume`")
    if archive.problems:
        raise Refused(f"snapshot {archive.stamp} does not verify: "
                      + "; ".join(archive.problems[:5]))
    have, theirs = db.schema_version(conn), db.schema_version(archive.conn)
    if have < 19:
        raise Refused(f"this node's database is at schema {have}; import needs migration "
                      "019 (`db init`, or deploy with bin/fleet update)")
    if theirs > have:
        raise Refused(f"snapshot {archive.stamp} is at schema {theirs}, this node at "
                      f"{have}: migrate this node first")
    if not ids:
        raise Refused(f"--ids {args.ids}: no ids in it")
    by_id = {int(row["id"]): row for row in held_rows(archive)}
    missing = sorted(i for i in ids if i not in by_id)
    if missing:
        states = dict(archive.conn.execute(
            f"SELECT id, state FROM main.entries WHERE id IN ({','.join('?' * len(missing))})",
            missing).fetchall())
        named = ", ".join(f"{i} ({states.get(i, 'not in the snapshot')})" for i in missing[:10])
        raise Refused(f"{len(missing)} of the ids are not held entries of snapshot "
                      f"{archive.stamp}: {named}")

    origin = db.get_origin(conn, args.node)
    register = None
    if origin is None:
        if not args.note:
            raise Refused(f"{args.node} is not a registered origin here; give --note "
                          "(what the node was, e.g. 'rented OCI A10, terminated "
                          "2026-09-27') to register it")
        register = _origin_fields(archive)
    elif origin["snapshot_sha256"] and origin["snapshot_sha256"] != archive.sha256:
        raise Refused(
            f"{args.node} was registered from snapshot {origin['snapshot']}, and this is "
            f"{archive.stamp}: every entry's page will say which snapshot it came from, so "
            "one origin is one snapshot")

    if not jobs.is_absolute() or not jobs.is_dir():
        raise Refused(f"no jobs directory at {jobs} (--jobs-dir)")
    last_job = conn.execute("SELECT COALESCE(MAX(id), 0) FROM jobs").fetchone()[0]
    stray = _stray_job_dirs(jobs, int(last_job))
    if stray:
        raise Refused(
            f"{jobs} has job directories with no job row past job {last_job} "
            f"({', '.join(stray[:5])}): an import would take those ids and find them "
            "occupied. Find out whose they are before importing")

    already = {i: here for i in sorted(ids)
               if (here := db.imported_entry(conn, args.node, i)) is not None}
    rows = [by_id[i] for i in sorted(ids) if i not in already]
    sizes = {int(row["id"]): tree_bytes(archive.jobs / str(int(row["job_id"])))
             for row in rows}
    need = sum(sizes.values()) + MARGIN_BYTES
    free = shutil.disk_usage(jobs).free
    if free < need:
        raise Refused(f"{jobs} has {free / 1e6:.0f} MB free; the selection is "
                      f"{sum(sizes.values()) / 1e6:.0f} MB and the margin "
                      f"{MARGIN_BYTES / 1e6:.0f} MB")
    return Plan(rows=rows, already=already, sizes=sizes, origin=origin, register=register)


def import_one(conn: sqlite3.Connection, archive: Archive, node: str, row: sqlite3.Row,
               jobs: Path) -> int:
    """One entry, its job and its attempts, and their directory: all of it or none."""
    old_entry, old_job = int(row["id"]), int(row["job_id"])
    source = archive.jobs / str(old_job)
    if not source.is_dir():
        raise Failed(f"no job directory at {source}")
    job = archive.conn.execute("SELECT * FROM main.jobs WHERE id = ?", (old_job,)).fetchone()
    if job is None:
        raise Failed(f"no job {old_job} in the snapshot")
    if job["parent_entry_id"] is not None or row["parent_entry_id"] is not None:
        raise Failed("it has a parent, which names an entry of the other node's sequence")
    if "origin_node" in archive.columns["entries"] and row["origin_node"]:
        raise Failed(f"it was itself imported, from {row['origin_node']}: import it from "
                     "the node that made it")
    attempts = archive.conn.execute(
        "SELECT * FROM main.attempts WHERE job_id = ? ORDER BY n", (old_job,)).fetchall()

    def copied(table: str, record: sqlite3.Row) -> dict[str, Any]:
        wanted = set(db.importable_columns(conn, table)) & set(archive.columns[table])
        return {name: record[name] for name in wanted}

    staging: Path | None = None
    final: Path | None = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        new_job = db.import_job(conn, **copied("jobs", job))
        target = jobs / str(new_job)
        if target.exists():
            raise Occupied(f"{target} already exists")
        staging = jobs / f".import-{new_job}"
        if staging.exists():
            # Left by a run that was killed mid-copy: its job row was rolled
            # back, so the name is this run's to reuse.
            shutil.rmtree(staging)
        copy_job_dir(source, staging)
        os.rename(staging, target)
        staging, final = None, target
        for attempt in attempts:
            fields = copied("attempts", attempt)
            for column in renderlocal.PATH_COLUMNS["attempts"]:
                if column in fields:
                    fields[column] = rewrite(fields[column], old_job, target)
            db.import_attempt(conn, new_job, **fields)
        fields = copied("entries", row)
        for column in renderlocal.PATH_COLUMNS["entries"]:
            if column in fields:
                fields[column] = rewrite(fields[column], old_job, target)
        new_entry = db.import_entry(conn, new_job, origin_node=node, origin_entry_id=old_entry,
                                    origin_job_id=old_job, **fields)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        for leftover in (staging, final):
            if leftover is not None and leftover.exists():
                shutil.rmtree(leftover, ignore_errors=True)
        raise
    return new_entry


def cmd_run(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    jobs = Path(args.jobs_dir).expanduser()
    archive = None
    conn = None
    try:
        check_node_name(args.node)
        ids = _read_ids(args.ids)
        if not db_path.is_file():
            raise Refused(f"no database at {db_path}")
        archive = open_archive(Path(args.source))
        conn = db.connect(db_path)
        plan = plan_run(args, archive, conn, jobs, ids)
    except (Refused, sqlite3.Error, OSError) as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        for handle in (archive, conn):
            if handle is not None:
                handle.close()
        return EXIT_REFUSED

    total_mb = sum(plan.sizes.values()) / 1e6
    try:
        if args.dry_run:
            return _report(args, plan, archive, imported={}, failed={}, copied=0,
                           dry_run=True)
        if plan.register is not None:
            db.register_origin(conn, args.node, args.by, note=args.note, **plan.register)
            print(f"registered origin {args.node}: {plan.register['shape']}, "
                  f"{plan.register['first_utc']} – {plan.register['last_utc']}, "
                  f"build {(plan.register['build'] or '?')[:7]}, snapshot {archive.stamp}",
                  file=sys.stderr)
        # Per-entry lines go where the summary does not, so --json stays one object.
        lines = sys.stderr if args.json else sys.stdout
        for origin_id, here in plan.already.items():
            print(f"{origin_id:>6}  already e/{here}", file=lines)
        imported: dict[int, int] = {}
        failed: dict[int, str] = {}
        copied = 0
        if plan.rows:
            print(f"importing {len(plan.rows)} entries from {args.node}, {total_mb:.0f} MB",
                  file=sys.stderr, flush=True)
        for done, row in enumerate(plan.rows, start=1):
            origin_id = int(row["id"])
            try:
                imported[origin_id] = import_one(conn, archive, args.node, row, jobs)
                copied += plan.sizes[origin_id]
            except (Failed, OSError, sqlite3.Error, shutil.Error) as exc:
                failed[origin_id] = str(exc)
                print(f"{origin_id:>6}  failed: {exc}", file=lines, flush=True)
                if isinstance(exc, Occupied):
                    # The next entry would be handed the same id and meet the
                    # same directory: stop rather than fail every one after.
                    break
            if done % PROGRESS_EVERY == 0:
                print(f"  {done}/{len(plan.rows)}", file=sys.stderr, flush=True)
        return _report(args, plan, archive, imported=imported, failed=failed, copied=copied,
                       dry_run=False)
    finally:
        archive.close()
        conn.close()


def _report(args: argparse.Namespace, plan: Plan, archive: Archive, *,
            imported: dict[int, int], failed: dict[int, str], copied: int,
            dry_run: bool) -> int:
    selected = len(plan.rows) + len(plan.already)
    if dry_run:
        summary = {
            "dry_run": True, "node": args.node, "snapshot": archive.stamp,
            "selected": selected, "would_import": len(plan.rows),
            "already": {str(k): v for k, v in plan.already.items()},
            "megabytes": round(sum(plan.sizes.values()) / 1e6, 1),
            "registers_origin": plan.register is not None,
        }
        line = (f"would import {len(plan.rows)} of {selected} from {args.node} "
                f"(snapshot {archive.stamp}, verified); {len(plan.already)} already here; "
                f"{summary['megabytes']:.0f} MB to copy"
                + (f"; registers {args.node}" if plan.register is not None else ""))
    else:
        new_ids = sorted(imported.values())
        span = (f" → entries {new_ids[0]}–{new_ids[-1]}, held" if new_ids else "")
        summary = {
            "dry_run": False, "node": args.node, "snapshot": archive.stamp,
            "selected": selected,
            "imported": {str(k): v for k, v in sorted(imported.items())},
            "already": {str(k): v for k, v in plan.already.items()},
            "failed": {str(k): v for k, v in sorted(failed.items())},
            "megabytes": round(copied / 1e6, 1),
        }
        untried = len(plan.rows) - len(imported) - len(failed)
        summary["not_attempted"] = untried
        line = (f"imported {len(imported)} of {selected}{span}; {len(plan.already)} skipped, "
                f"{len(failed)} failed"
                + (f", {untried} not attempted" if untried else "")
                + f"; {copied / 1e6:.0f} MB copied")
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        print(line, file=sys.stderr)
    else:
        print(line)
    return EXIT_FAIL if failed else EXIT_OK


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "import",
        help="bring entries another node made into this one (gpu-fold-in.md)",
        description=(
            "Entries of another node's archive, as held entries here: `list` shows what "
            "an archive holds, `run` brings the ids it is given, each under a new id with "
            "its job directory copied and the node and its old ids kept "
            "(docs/plans/gpu-fold-in.md)."
        ),
    )
    sub = p.add_subparsers(dest="import_command", required=True)

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--from", dest="source", required=True, metavar="DIR",
                       help="the archive's ~/sketchgen: backups/<stamp>/ and jobs/; the "
                            "newest snapshot is the one read")
        q.add_argument("--db", default=db.DEFAULT_DB_PATH, metavar="P",
                       help="this node's database (default: $SKETCHGEN_DB, else "
                            "~/sketchgen/sketchgen.db)")
        q.add_argument("--json", action="store_true", help="one JSON object on stdout")

    q = sub.add_parser(
        "list",
        help="what an archive holds; reads only",
        description=(
            "The archive's held entries, one line each: its id there, the prompt, the "
            "executor, the rules file, attempts, whether the kept sketch calls "
            "createGraphics() (harness 4 read the buffer as the sketch), and the entry "
            "published here with the same prompt. Opens everything read-only."
        ),
    )
    common(q)
    q.add_argument("--clean", action="store_true", help="only entries with no off-plan "
                   "assertion (no offplan_json)")
    q.add_argument("--one-per-prompt", dest="one_per_prompt", action="store_true",
                   help="the earliest of each prompt, among what the other filters keep")
    q.add_argument("--executor", metavar="MODEL", help="only this executor")
    q.add_argument("--rules", choices=("control", "treatment"), help="only this rules file")
    q.add_argument("--prompt-of", dest="prompt_of", type=int, metavar="ENTRY",
                   help="only the prompt of this node's entry ENTRY")
    q.add_argument("--no-buffers", dest="no_buffers", action="store_true",
                   help="leave out kept sketches that call createGraphics()")
    q.add_argument("--not-imported", dest="not_imported", action="store_true",
                   help="leave out what was already imported from --node")
    q.add_argument("--node", metavar="NAME", help="the archive's fleet name, for "
                   "--not-imported")
    q.add_argument("--ids", action="store_true",
                   help="print the ids alone, one per line: the file `run --ids` reads")
    q.set_defaults(func=cmd_list, _parser=q)

    r = sub.add_parser(
        "run",
        help="bring these entries in, as held",
        description=(
            "Each id is an entry of the archive's newest snapshot that is held there. "
            "Refuses (exit 3, nothing written) unless the generator is paused, the "
            "snapshot verifies, its schema is not newer than this node's, every id is "
            "held there, the node is registered or --note registers it, and the disk "
            "has the selection's size and a margin free. Then one transaction per entry; "
            "one that fails is undone and named, and the run goes on (exit 1). An entry "
            "already imported is skipped."
        ),
    )
    common(r)
    r.add_argument("--node", required=True, metavar="NAME",
                   help="the archive's fleet name, e.g. sld-gpu; public on the entry page")
    r.add_argument("--ids", required=True, metavar="FILE",
                   help="entry ids of the archive, one per line, or - for stdin "
                        "(import list --ids, or a local render's Copy picked ids)")
    r.add_argument("--by", required=True, metavar="LOGIN", help="GitHub username")
    r.add_argument("--note", help="what the node was, to register it the first time")
    r.add_argument("--jobs-dir", dest="jobs_dir", default=DEFAULT_JOBS_DIR,
                   metavar="D", help="this node's job directories (default: "
                                     "$SKETCHGEN_JOBS, else ~/sketchgen/jobs)")
    r.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="every check and the megabytes; nothing written")
    r.set_defaults(func=cmd_run, _parser=r)
