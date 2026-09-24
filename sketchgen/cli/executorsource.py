"""``sketchgen executor-source`` — what the executor is shown, and how much.

A drop-in subcommand (see sketchgen/cli/__init__.py). Two `meta` rows govern
the mechanism packet 17 added (docs/plans/child-source.md): `executor_source`,
which chooses whether an attempt is given the sketch it is revising, and
`source_max_chars`, the length over which a sketch is named rather than shown.
A third, `revision_min_lines` (HARNESS_VERSION 6, 2026-09-24), is what came of
giving it: the lines of code a child's attempt must change from its parent
before the gate calls it a revision, 0 for no floor at all.

This verb exists because AGENTS.md rule 4 has no exception for a one-row
setting: `MEASURE[source-follow]` is two batches of the same ten parents with
`executor_source` at `both` and at `none`, and an operator running that
comparison with `sqlite3` by hand is the hand edit the rule is about. The New
job page's own switch is packet 18; this is the node's.

Nothing here touches a running job: the worker reads both rows at the top of
every attempt, so a change takes effect on the next attempt claimed and needs
no restart and no deploy.

Exit codes are the project's: 0 success, 3 a refusal — no database, or a value
outside the closed set.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import worker

EXIT_OK = 0
EXIT_REFUSED = 3


def _open(args: argparse.Namespace) -> sqlite3.Connection | None:
    path = Path(args.db).expanduser()
    if not path.is_file():
        print(f"refused: no database at {path} (run: sketchgen db init)",
              file=sys.stderr)
        return None
    return db.connect(path)


def _state(conn: sqlite3.Connection) -> dict:
    raw = db.get_meta(conn, worker.SOURCE_SWITCH_KEY)
    value = (raw or worker.SOURCE_SWITCH_DEFAULT).strip()
    return {
        "executor_source": value if value in worker.SOURCE_SWITCH_VALUES
        else worker.SOURCE_SWITCH_DEFAULT,
        # What it reads as against what is written: an unset row and an
        # unreadable one both behave as the default, and the operator
        # comparing two batches needs to know which of the three it is.
        "executor_source_set": raw,
        "source_max_chars": worker.source_max_chars(conn),
        "source_max_chars_set": db.get_meta(conn, worker.SOURCE_MAX_CHARS_KEY),
        "revision_min_lines": worker.revision_min_lines(conn),
        "revision_min_lines_set": db.get_meta(conn, worker.REVISION_MIN_LINES_KEY),
    }


def cmd_executor_source(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    if args.set is not None:
        db.set_meta(conn, worker.SOURCE_SWITCH_KEY, args.set)
    if args.max_chars is not None:
        if args.max_chars <= 0:
            print("refused: --max-chars must be positive", file=sys.stderr)
            return EXIT_REFUSED
        db.set_meta(conn, worker.SOURCE_MAX_CHARS_KEY, str(args.max_chars))
    if args.revision_min_lines is not None:
        if args.revision_min_lines < 0:
            print("refused: --revision-min-lines must be 0 (no floor) or more",
                  file=sys.stderr)
            return EXIT_REFUSED
        db.set_meta(conn, worker.REVISION_MIN_LINES_KEY, str(args.revision_min_lines))
    state = _state(conn)
    if args.json:
        print(json.dumps(state, indent=2, sort_keys=True))
        return EXIT_OK
    print(f"executor_source: {state['executor_source']}"
          + ("" if state["executor_source_set"] else "  (unset; the default)"))
    print(f"source_max_chars: {state['source_max_chars']}"
          + ("" if state["source_max_chars_set"] else "  (unset; the default)"))
    print(f"revision_min_lines: {state['revision_min_lines']}"
          + ("  (off: a child is gated like any job)"
             if state["revision_min_lines"] == 0 else "")
          + ("" if state["revision_min_lines_set"] else "  (unset; the default)"))
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "executor-source",
        help="whether the executor is shown the sketch it is revising",
        description=(
            "Read or set the two meta rows behind packet 17 "
            "(docs/plans/child-source.md). `executor_source` is one of both "
            "(the default: a child's first attempt is shown the parent "
            "entry's kept sketch, and every repair its own previous "
            "attempt), parent, previous, or none — and none is byte for byte "
            "the prompt this node sent before 2026-09-21, which is the "
            "control batch MEASURE[source-follow] compares against. "
            "`source_max_chars` is the length over which a sketch is named in "
            "one line instead of shown, because a model handed half a sketch "
            "rewrites the half it cannot see. `revision_min_lines` is the "
            "lines of code a child's attempt must change from its parent "
            "before the gate's `revised` passes; 0 turns it off. With no "
            "options, prints all three. The worker reads them at the top of "
            "every attempt: no restart, no deploy, and no job in flight is "
            "changed."
        ),
    )
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.add_argument(
        "--set",
        choices=list(worker.SOURCE_SWITCH_VALUES),
        help="set executor_source",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        metavar="N",
        help=f"set source_max_chars (default {worker.SOURCE_MAX_CHARS_DEFAULT})",
    )
    parser.add_argument(
        "--revision-min-lines",
        type=int,
        metavar="N",
        help=("set revision_min_lines: lines of code a child must change from "
              f"its parent (default {worker.REVISION_MIN_LINES_DEFAULT}; 0 is off)"),
    )
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.set_defaults(func=cmd_executor_source, _parser=parser)
