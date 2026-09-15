"""``sketchgen gate-audit`` — every attempt by what its gate run cost.

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls :func:`register`.

Sorting the gate reports by ``timings.total_s`` is how both unsafe sketches were
found. Job 166's was found by hand, the morning after entry 165 took the
operator's laptop down; the same sort then turned up job 45 (504 s, twelve
``filter(BLUR)`` passes a frame) and job 43 (120 s), which nobody had noticed
because the gate said yes to all of them. That sort is this command.

It reads the database and the report files on disk and nothing else: no browser,
no model, no network, and it never runs the gate. It is safe to point at a live
node over SSH while the worker is working.

    sketchgen gate-audit                  # every attempt, slowest first
    sketchgen gate-audit --over 30        # the ones worth looking at
    sketchgen gate-audit --over 90 --json

Exit codes are the project's: 0 listed something, 0 listed nothing (an empty
gallery is not an error), 3 refused — no database, no jobs directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import worker

EXIT_OK = 0
EXIT_REFUSED = 3


def _report_path(jobs_root: Path, job_id: int, attempt_n: int,
                 recorded: str | None) -> Path:
    """Where this attempt's report.json is, preferring what the row recorded."""
    if recorded:
        path = Path(recorded).expanduser()
        if path.is_file():
            return path
    return jobs_root / str(job_id) / f"attempt-{attempt_n}" / ".gate" / "report.json"


def _rows(conn: sqlite3.Connection, jobs_root: Path) -> list[dict]:
    """One dict per attempt that has a readable report, with its entry beside it.

    An attempt whose report cannot be read is skipped rather than guessed at:
    the gate either wrote a report or it did not, and "no report.json" is
    already the worker's own name for that failure. An attempt directory with no
    row in `attempts` is not listed either: the database is the index, and a
    directory it does not know about is a question for `sketchgen db status`.
    """
    entries = {
        int(row["job_id"]): (row["id"], row["state"])
        for row in conn.execute("SELECT id, job_id, state FROM entries")
    }
    out = []
    for row in conn.execute(
        "SELECT job_id, n, gate_exit, gate_report_path FROM attempts ORDER BY job_id, n"
    ):
        path = _report_path(jobs_root, int(row["job_id"]), int(row["n"]),
                            row["gate_report_path"])
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        timings = report.get("timings") or {}
        checks = report.get("checks") or {}
        entry_id, state = entries.get(int(row["job_id"]), (None, None))
        out.append({
            "job_id": int(row["job_id"]),
            "attempt": int(row["n"]),
            "entry_id": entry_id,
            "state": state,
            "gate_exit": row["gate_exit"],
            "total_s": timings.get("total_s"),
            "ms_per_frame": timings.get("ms_per_frame"),
            "frame_budget": checks.get("frame_budget"),
            "report": str(path),
        })
    out.sort(key=lambda item: (item["total_s"] is None, -(item["total_s"] or 0.0)))
    return out


def cmd_gate_audit(args: argparse.Namespace) -> int:
    path = Path(args.db).expanduser()
    if not path.is_file():
        print(f"refused: no database at {path} (run: sketchgen db init)",
              file=sys.stderr)
        return EXIT_REFUSED
    jobs_root = Path(args.jobs).expanduser()
    if not jobs_root.is_dir():
        print(f"refused: no jobs directory at {jobs_root}", file=sys.stderr)
        return EXIT_REFUSED

    conn = db.connect(path)
    try:
        rows = _rows(conn, jobs_root)
    finally:
        conn.close()

    if args.over is not None:
        rows = [r for r in rows if (r["total_s"] or 0.0) > args.over]

    if args.json:
        print(json.dumps(rows, indent=2))
        return EXIT_OK

    if not rows:
        over = "" if args.over is None else f" over {args.over:g} s"
        print(f"no attempt has a gate report{over}")
        return EXIT_OK

    print("%-9s %-7s %-7s %-12s %8s %10s %s"
          % ("job", "attempt", "entry", "state", "gate_s", "ms/frame", "budget"))
    for r in rows:
        print("%-9s %-7s %-7s %-12s %8s %10s %s"
              % (r["job_id"],
                 r["attempt"],
                 "—" if r["entry_id"] is None else r["entry_id"],
                 r["state"] or "—",
                 "—" if r["total_s"] is None else f"{r['total_s']:.1f}",
                 "—" if r["ms_per_frame"] is None else f"{r['ms_per_frame']:g}",
                 {True: "pass", False: "FAILED"}.get(r["frame_budget"], "—")))
    print()
    print("%d attempt(s); ms/frame is blank for a run from before the frame "
          "budget landed (2026-09-15)" % len(rows))
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "gate-audit",
        help="list attempts by what their gate run cost, slowest first",
        description=(
            "Every attempt with a readable report.json, sorted by "
            "timings.total_s descending, with its entry id, the entry's state, "
            "and timings.ms_per_frame where the report has one. Sorting the "
            "reports this way is how job 166 and job 45 were both found, after "
            "the gate had passed them. Reads the database and the report files "
            "and nothing else: no browser, no model, and it never runs the "
            "gate, so it is safe to run against a working node."
        ),
    )
    parser.add_argument(
        "--over", type=float, default=None, metavar="SECONDS",
        help="only attempts whose gate run took longer than this "
             "(try 30, which is where the operator UI draws its warning)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--jobs", default=os.environ.get("SKETCHGEN_JOBS", worker.DEFAULT_JOBS_DIR),
        metavar="D",
        help="the attempt archive holding the reports "
             "(default: $SKETCHGEN_JOBS, else ~/sketchgen/jobs)",
    )
    parser.add_argument(
        "--db", default=db.DEFAULT_DB_PATH, metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.set_defaults(func=cmd_gate_audit, _parser=parser)
