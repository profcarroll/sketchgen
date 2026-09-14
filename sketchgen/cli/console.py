"""`sketchgen console` — print the console document.

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls register(top). Packet 4.1's collector is sketchgen/console.py;
this file is only the shell around it.

Exit codes: 0 printed, 1 could not (no database, or it has no schema), 3 not
used here — the console refuses nothing, it reads.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sketchgen import console as collector  # noqa: E402
from sketchgen import db  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1


def cmd_console(args: argparse.Namespace) -> int:
    if not os.path.exists(args.db):
        print(
            f"sketchgen: no database at {args.db} (run: sketchgen db init)",
            file=sys.stderr,
        )
        return EXIT_FAIL
    try:
        conn = db.connect(args.db)
    except sqlite3.Error as exc:
        print(f"sketchgen: cannot open {args.db}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    try:
        if db.schema_version(conn) == 0:
            print(
                f"sketchgen: {args.db} has no schema (run: sketchgen db init)",
                file=sys.stderr,
            )
            return EXIT_FAIL
        previous = None
        while True:
            try:
                document = collector.collect(
                    conn, args.jobs_dir, args.host, prev=previous
                )
            except sqlite3.Error as exc:
                print(f"sketchgen: console failed: {exc}", file=sys.stderr)
                return EXIT_FAIL
            if args.json:
                print(json.dumps(document, sort_keys=True), flush=True)
            else:
                print(collector.render_text(document), flush=True)
            if not args.watch:
                return EXIT_OK
            previous = document
            time.sleep(max(0.2, float(args.watch)))
    except KeyboardInterrupt:  # pragma: no cover - the operator's Ctrl-C
        print("sketchgen: interrupted", file=sys.stderr)
        return EXIT_OK
    finally:
        conn.close()


def register(top) -> None:
    parser = top.add_parser(
        "console",
        help="node vitals, the slot, the odometer and the funnel, as one document",
        description=(
            "Collect one console document — per-core CPU, memory, swap, disk, "
            "load and the top three processes; the resident model and who holds "
            "the inference slot; the worker's control state; the token odometer, "
            "the production funnel and the per-sketch averages with their cost at "
            "both node shapes. Reads /proc, the filesystem, Ollama's /api/ps and "
            "the app database; calls no model, writes nothing, needs no sudo. The "
            "--json form is the contract the web UI reads."
        ),
    )
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.add_argument(
        "--jobs-dir",
        dest="jobs_dir",
        default=collector.DEFAULT_JOBS_DIR,
        metavar="D",
        help="job work directories (default: $SKETCHGEN_JOBS, else ~/sketchgen/jobs)",
    )
    parser.add_argument(
        "--host",
        default=collector.DEFAULT_HOST,
        metavar="URL",
        help=f"Ollama base URL (default: {collector.DEFAULT_HOST})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the document as JSON instead of as a terminal rendering",
    )
    parser.add_argument(
        "--watch",
        type=float,
        default=None,
        metavar="S",
        help="repeat every S seconds, one document per interval (default: once)",
    )
    parser.set_defaults(func=cmd_console, _parser=parser)
