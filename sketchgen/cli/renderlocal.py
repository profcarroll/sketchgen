"""``sketchgen render-local``: a gallery of any database, in a directory nobody pushes.

docs/plans/local-gallery.md, Packet 1. The renderer has always been a function
of the database into a directory; ``publish`` is what makes the directory a
git checkout and pushes it. This is the other target: the same pages, of the
published entries, the held ones, or both, into a directory that this verb
refuses to let be a checkout — for the operator to sift an archive on a
laptop, and for a kiosk to play from its own loopback (§1.9).

The database is opened read-only and the job paths in it are read through
temporary views that point them at ``--jobs``. That is not a convenience: the
sld-gpu snapshot's rows say ``/home/ubuntu/sketchgen/jobs/1/…``, which on
sld-cloud is sld-cloud's own job 1, and a render that followed them would put
one node's sketches under the other's prompts without an error anywhere. A
path that does not lie under the snapshot's jobs directory reads as NULL, so a
local render can open nothing outside ``--jobs``.

Exit codes as everywhere: 0 rendered (skipped entries are named, and are not a
failure), 1 failed, 3 refused with nothing written.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path

from sketchgen import db
from sketchgen import gallery
from sketchgen import publish
from sketchgen import webimg

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: The file that says a directory is a local render's and may be rewritten.
STAMP = ".sketchgen-local"

#: Files per encoder child, as web-frames uses.
BATCH = 100

#: The columns that hold a path into a job's directory, per table. Every file
#: the renderer opens is reached through one of these (gallery._source_dir,
#: _artefact, _strip_png, _gate_report).
PATH_COLUMNS = {
    "entries": ("source_dir", "strip_path", "png_path"),
    "attempts": ("source_dir", "gate_report_path"),
}

#: ``<root>/jobs/<n>`` at the start of a path: the root is what is rewritten.
JOBS_RE = re.compile(r"^(.*/jobs)/\d+(?:/|$)")


class Refused(Exception):
    """Exit 3, nothing written."""


class _Once(argparse.Action):
    """A --db given twice is refused, not resolved to the last one.

    bin/sg appends the node's own `--db ~/sketchgen/sketchgen.db` after every
    command. argparse keeps the last value, so `bin/sg render-local --db
    <archive> --jobs <archive jobs>` would render the *live* database with its
    paths read under the archive's jobs directory: this node's prompts over
    another node's sketches, with no error anywhere (found writing the
    tutorial, 2026-09-28, in the kit README's own example).
    """

    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest, None) is not None:
            parser.exit(EXIT_REFUSED, f"sketchgen: refused: {option_string} given twice; "
                        "bin/sg adds the node's own --db last, so to render another "
                        "database run render-local on the node itself\n")
        setattr(namespace, self.dest, values)


def _quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _jobs_roots(conn: sqlite3.Connection) -> set[str]:
    """Every ``…/jobs`` directory the database's paths hang off."""
    roots: set[str] = set()
    for table, columns in PATH_COLUMNS.items():
        for column in columns:
            for (value,) in conn.execute(
                f"SELECT DISTINCT {column} FROM main.{table} WHERE {column} IS NOT NULL"
            ):
                match = JOBS_RE.match(str(value))
                if match:
                    roots.add(match.group(1))
    return roots


def _remap(conn: sqlite3.Connection, jobs: Path) -> None:
    """Shadow ``entries`` and ``attempts`` with views whose paths are under ``jobs``.

    SQLite resolves an unqualified name in ``temp`` before ``main``, so every
    query the renderer makes reads these. Same columns in the same order; a
    path under one of the database's jobs roots is rewritten onto ``jobs``,
    and any other path is NULL.
    """
    new = str(jobs)
    roots = sorted(_jobs_roots(conn))
    for table, columns in PATH_COLUMNS.items():
        names = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
        select = []
        for name in names:
            if name not in columns:
                select.append(name)
                continue
            cases = " ".join(
                f"WHEN substr({name}, 1, {len(root) + 1}) = {_quote(root + '/')} "
                f"THEN {_quote(new)} || substr({name}, {len(root) + 1})"
                for root in roots
            )
            select.append(f"CASE {cases} ELSE NULL END AS {name}" if cases else f"NULL AS {name}")
        conn.execute(
            f"CREATE TEMP VIEW {table} AS SELECT {', '.join(select)} FROM main.{table}"
        )


def _readonly_uri(path: Path) -> str:
    """How to open ``path`` so that nothing beside it is written either.

    ``mode=ro`` alone still creates ``-wal`` and ``-shm`` next to a WAL-mode
    database, and a snapshot directory is not the place for them. A WAL
    database with no ``-wal`` beside it has no connection open on it, so it is
    opened ``immutable``, which takes no locks and makes no files. One with a
    ``-wal`` is being written — a node's live database, whose resident worker
    always holds one — and gets ``mode=ro``, which reads it safely and adds
    nothing that was not already there. A rollback-journal database, as every
    backup snapshot is, needs neither and gets ``mode=ro``.
    """
    with path.open("rb") as fh:
        header = fh.read(20)
    wal_mode = len(header) == 20 and header[18] == 2
    quoted = urllib.parse.quote(str(path.resolve()))
    if wal_mode and not path.with_name(path.name + "-wal").exists():
        return f"file:{quoted}?mode=ro&immutable=1"
    return f"file:{quoted}?mode=ro"


def open_readonly(path: Path, jobs: Path) -> sqlite3.Connection:
    """The database, read-only, its job paths pointed at ``jobs``."""
    if not path.is_file():
        raise Refused(f"no database at {path}")
    if not jobs.is_dir():
        raise Refused(f"no jobs directory at {jobs}")
    conn = sqlite3.connect(_readonly_uri(path), uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        have = db.schema_version(conn)
        known = max((n for n, _ in db._migration_files()), default=0)
        if have > known:
            raise Refused(
                f"{path} is at schema {have} and this code knows {known}: "
                "render it with a checkout that has its migrations"
            )
        _remap(conn, jobs.resolve())
    except Exception:
        conn.close()
        raise
    return conn


def _inside_git(path: Path) -> Path | None:
    for walk in (path, *path.parents):
        if (walk / ".git").exists():
            return walk
    return None


def check_out(out: Path) -> None:
    """Refuse any destination but an empty directory or a local render's own."""
    resolved = out.resolve()
    checkout = _inside_git(resolved)
    if checkout is not None:
        raise Refused(
            f"{out} is inside the git work tree at {checkout}: a local render is "
            "never pushed, so it is never written into a checkout"
        )
    gallery_dir = Path(publish.DEFAULT_GALLERY_DIR).expanduser().resolve()
    if resolved == gallery_dir or gallery_dir in resolved.parents:
        raise Refused(f"{out} is the gallery checkout ({gallery_dir}), which publish pushes")
    if resolved.exists():
        if not resolved.is_dir():
            raise Refused(f"{out} is not a directory")
        if any(resolved.iterdir()) and not (resolved / STAMP).is_file():
            raise Refused(
                f"{out} holds files that are not a local render's (no {STAMP}): "
                "give an empty or new directory"
            )


def _read_ids(path: Path) -> frozenset[int]:
    """One id per line, the shape ``import list --ids`` and Copy picked ids write."""
    ids: set[int] = set()
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not text.isdigit():
            raise Refused(f"{path}:{line_no}: not an entry id: {text!r}")
        ids.add(int(text))
    return frozenset(ids)


def _web_frames(conn: sqlite3.Connection, ids: list[int]) -> dict[str, int]:
    """Make the WebP copies the pages will use, beside the PNGs, as web-frames does.

    The PNGs are not touched, and neither is the database. What an archive
    gains is a ``strip.webp`` beside each ``strip.png``, which is what every
    node's own jobs already carry, and what an import copies along with them.
    """
    tally = {"made": 0, "cached": 0, "failed": 0}
    pngs = [png for entry_id in ids for png in gallery.frame_pngs(conn, entry_id)]
    missing = [png for png in pngs if webimg.cached(png) is None]
    tally["cached"] = len(pngs) - len(missing)
    for start in range(0, len(missing), BATCH):
        for outcome in webimg.ensure(missing[start:start + BATCH]).values():
            key = outcome if outcome in ("made", "cached") else "failed"
            tally[key] += 1
        print(f"  web frames {min(start + BATCH, len(missing))}/{len(missing)}",
              file=sys.stderr, flush=True)
    return tally


def cmd(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    include = set(args.include or ["published"])
    try:
        check_out(out)
        only = _read_ids(Path(args.ids).expanduser()) if args.ids else None
        conn = open_readonly(Path(args.db).expanduser(), Path(args.jobs).expanduser())
    except (Refused, OSError) as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        base = (gallery.Config.load(Path(args.config_from).expanduser())
                if args.config_from else gallery.Config())
        local = gallery.Local(
            label=args.origin,
            published="published" in include,
            held="held" in include,
            only=only,
        )
        # Counting only with a write path, and then only what the Worker lets a
        # kiosk origin do (local-gallery.md §1.6): views and counts.
        config = gallery.Config(
            write_path=args.write_path or "",
            gallery_url=base.gallery_url,
            repository=base.repository,
            kiosk_views=bool(args.write_path),
            kiosk_ghost=base.kiosk_ghost,
            kiosk_ghost_loop_s=base.kiosk_ghost_loop_s,
            kiosk_sites=base.kiosk_sites,
            kiosk_buildings=base.kiosk_buildings,
            local=local,
        )
        rows = gallery._public_rows(conn, config)
        if not rows:
            print("sketchgen: refused: nothing to render: no entry in "
                  f"{args.db} matches --include {' '.join(sorted(include))}"
                  + (" and --ids" if only is not None else ""), file=sys.stderr)
            return EXIT_REFUSED
        found = sum(1 for row in rows if row["source_dir"] and Path(row["source_dir"]).is_dir())
        if not found:
            print(f"sketchgen: refused: none of the {len(rows)} entries has a job "
                  f"directory under {args.jobs}: is it this database's jobs directory?",
                  file=sys.stderr)
            return EXIT_REFUSED
        ids = [int(row["id"]) for row in rows]
        print(f"rendering {len(ids)} entries from {args.db} (read-only, schema "
              f"{db.schema_version(conn)}); {len(ids) - found} without a job directory",
              file=sys.stderr, flush=True)
        if args.no_web or not webimg.available():
            frames = None
        else:
            frames = _web_frames(conn, ids)
        out.mkdir(parents=True, exist_ok=True)
        (out / STAMP).write_text(
            json.dumps({"origin": args.origin, "db": str(Path(args.db).expanduser()),
                        "jobs": str(Path(args.jobs).expanduser())},
                       indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = gallery.render_local(
            conn, out, config,
            progress=lambda done, total: print(
                f"  entries {done}/{total}", file=sys.stderr, flush=True),
        )
    except gallery.Unsafe as exc:
        # The index's own guard: every entry passed its own, so this is text
        # only the index carries. Nothing of the index was left behind.
        print(f"sketchgen: failed: the index was refused:\n{exc}", file=sys.stderr)
        return EXIT_FAIL
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"sketchgen: failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()

    summary = {
        "out": str(out),
        "origin": args.origin,
        "rendered": len(result.rendered),
        "skipped": {str(k): v for k, v in sorted(result.skipped.items())},
        "missing_jobs": len(ids) - found,
        "web_frames": frames,
    }
    if args.json:
        print(json.dumps(summary, sort_keys=True))
        return EXIT_OK
    for entry_id, why in sorted(result.skipped.items()):
        print(f"skipped e/{entry_id}: {why}")
    frames_line = ("web frames: none made (--no-web, or no encoder here)" if frames is None
                   else f"web frames: {frames['made']} made, {frames['cached']} cached, "
                        f"{frames['failed']} failed")
    print(f"rendered {len(result.rendered)} of {len(ids)} → {out}; "
          f"{len(result.skipped)} skipped by the scan; {frames_line}")
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "render-local",
        help="render a gallery of any database into a directory that is never pushed",
        description=(
            "The gallery's pages, of the published entries, the held ones or both, "
            "into a directory that is not a git checkout: for sifting an archive on a "
            "laptop and for a kiosk served from its own loopback "
            "(docs/plans/local-gallery.md). The database is opened read-only and "
            "its job paths are read under --jobs. An entry the personal-data guard "
            "refuses is left out and named. Refuses (exit 3) a checkout, a directory "
            "that is not empty and not a previous local render, a schema newer than "
            "this code, and a --jobs that holds none of the entries."
        ),
    )
    p.add_argument("--db", required=True, metavar="P", action=_Once,
                   help="the database to render: a node's own, or an archive's snapshot")
    p.add_argument("--jobs", required=True, metavar="DIR",
                   help="the jobs directory that database's paths mean; its rows' own "
                        "paths name the node they were written on")
    p.add_argument("--out", required=True, metavar="DIR",
                   help="where the pages go: new, empty, or a previous local render")
    p.add_argument("--origin", required=True, metavar="NAME",
                   help="the name on the banner and the key for picks, e.g. sld-gpu; "
                        "no table records which node a database is, so it is not guessed")
    p.add_argument("--include", action="append", choices=("published", "held"),
                   help="which entries (repeatable; default: published)")
    p.add_argument("--ids", metavar="FILE",
                   help="only these entry ids, one per line (import list --ids, "
                        "or the page's Copy picked ids)")
    p.add_argument("--config-from", dest="config_from", metavar="DIR",
                   help="a gallery checkout whose config.json supplies the public URL, "
                        "the kiosk's sites and buildings and its ghost settings")
    p.add_argument("--write-path", dest="write_path", metavar="URL",
                   help="the Worker, for counts and kiosk views; absent, nothing is "
                        "counted (the Worker must list this render's origin)")
    p.add_argument("--no-web", dest="no_web", action="store_true",
                   help="make no WebP copies; the pages use what exists, else the PNGs")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p.set_defaults(func=cmd, _parser=p)
