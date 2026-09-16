"""`sketchgen submissions list | release | decline` — the public's queue, over SSH.

Packet 8. The review that matters is the one on /submissions, which shows the
parent's strip beside a critique and is the right place to make the decision.
This is the same three verbs without the tunnel: the node is reachable over SSH
long before anybody has forwarded 8081, and a queue nobody can see is a queue
nobody empties.

Both write verbs go through :mod:`sketchgen.web`'s own
:func:`~sketchgen.web.release_submission` and
:func:`~sketchgen.web.decline_submission`, so the page and the terminal cannot
drift apart about what releasing means — one of them queues the job with
``rules_file='random'`` and the other must not quietly do something else.
Importing that module starts no server; it is where this project keeps the
operator's decisions.

Exit codes are the project's: 0 success, 1 the submission was declined by the
release itself (its parent had been rejected), 3 a refusal — no database, no
such submission, or one a person has already decided.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import web

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )


def _open(args: argparse.Namespace) -> sqlite3.Connection | None:
    path = Path(args.db).expanduser()
    if not path.is_file():
        print(
            f"refused: no database at {path} (run: sketchgen db init)", file=sys.stderr
        )
        return None
    return db.connect(path)


def _document(row: sqlite3.Row) -> dict:
    """One submission as JSON. Every column, because there are eleven of them
    and guessing which the next script wants is how a format grows a version."""
    return {key: row[key] for key in row.keys()}


# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    try:
        rows = db.pending_submissions(conn, args.limit)
        counts = db.submission_counts(conn)
    finally:
        conn.close()
    if args.json:
        print(
            json.dumps(
                {"counts": counts, "pending": [_document(row) for row in rows]},
                indent=2,
            )
        )
        return EXIT_OK
    if not rows:
        print(
            f"nothing waiting ({counts['released']} released, "
            f"{counts['declined']} declined)"
        )
        return EXIT_OK
    for row in rows:
        parent = f" of entry {row['entry_id']}" if row["entry_id"] else ""
        print(
            f"{row['id']:>5}  {row['kind']:<8} @{row['username']}{parent}"
            f"  {row['created_utc']}"
        )
        print(f"       {row['text']}")
    print(
        f"{len(rows)} waiting · {counts['released']} released, "
        f"{counts['declined']} declined"
    )
    return EXIT_OK


def cmd_release(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    try:
        outcome, job_id, message = web.release_submission(conn, args.id)
    finally:
        conn.close()
    if args.json:
        print(json.dumps({"submission_id": args.id, "job_id": job_id,
                          "outcome": outcome, "message": message}, indent=2))
    elif outcome == web.RELEASED:
        print(message)
    else:
        print(message, file=sys.stderr)
    if outcome == web.RELEASED:
        return EXIT_OK
    # The one outcome that is neither: spawn() refused a parent somebody
    # rejected while this waited, so the release declined the row instead.
    return EXIT_FAIL if outcome == web.DECLINED else EXIT_REFUSED


def cmd_decline(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    try:
        outcome, message = web.decline_submission(conn, args.id, args.reason or "")
    finally:
        conn.close()
    declined = outcome == web.DECLINED
    if args.json:
        print(json.dumps({"submission_id": args.id, "outcome": outcome,
                          "message": message}, indent=2))
    elif declined:
        print(message)
    else:
        print(message, file=sys.stderr)
    return EXIT_OK if declined else EXIT_REFUSED


# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "submissions",
        help="review what the public asked for: list, release, decline",
        description=(
            "A submission is not a job (plan §1.2): a prompt or a critique a "
            "signed-in visitor typed on the gallery, pulled down by "
            "`sketchgen sync` into its own table. It becomes a job only when a "
            "person releases it here or on /submissions. With no subcommand "
            "this lists what is waiting."
        ),
    )
    sub = parser.add_subparsers(
        dest="submissions_command", metavar="{list,release,decline}"
    )
    # Bare `sketchgen submissions` lists: the read is the common case and the
    # one that changes nothing.
    parser.add_argument("--limit", type=int, default=50, metavar="N",
                        help="how many pending rows to list (default: 50)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(parser)
    parser.set_defaults(func=cmd_list, _parser=parser)

    listing = sub.add_parser(
        "list",
        help="what is waiting for a person, oldest first",
        description="Pending submissions, oldest first, with the day's totals.",
    )
    listing.add_argument("--limit", type=int, default=50, metavar="N")
    listing.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(listing)
    listing.set_defaults(func=cmd_list, _parser=listing)

    release = sub.add_parser(
        "release",
        help="queue one submission as a job, held for review",
        description=(
            "A prompt is queued with rules_file='random' so public work cannot "
            "skew the treatment/control split, and publication='hold' like "
            "every other job. A critique spawns the child through "
            "lineage.spawn() with its existing defaults and is recorded in "
            "`critiques` as human:<login>. Exits 3 on a submission somebody "
            "has already decided; exits 1 when the parent had been rejected, "
            "which declines the row with that as the reason."
        ),
    )
    release.add_argument("--id", type=int, required=True, metavar="ID",
                         help="the SUBMISSION id (not an entry or a job id)")
    release.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(release)
    release.set_defaults(func=cmd_release, _parser=release)

    decline = sub.add_parser(
        "decline",
        help="say no to one submission; the row stays as the record",
        description=(
            "Nothing is deleted and no job is created: the sentence stays in "
            "the table with the reason beside it, because a declined "
            "submission is the record of what somebody asked for."
        ),
    )
    decline.add_argument("--id", type=int, required=True, metavar="ID")
    decline.add_argument("--reason", default=None, metavar="TEXT",
                         help="why (default: 'declined by operator')")
    decline.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(decline)
    decline.set_defaults(func=cmd_decline, _parser=decline)
