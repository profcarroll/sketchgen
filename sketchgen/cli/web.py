"""``sketchgen web`` — the operator's screens on 127.0.0.1.

A drop-in subcommand: bin/sketchgen imports this module and calls
:func:`register`, so packet 4.2 adds a file and edits nothing shared.

Exit codes, as everywhere in this build: 0 success, 1 failure, 3 refused. The
refusal that matters here is ``--bind`` anything other than 127.0.0.1 — the UI
has no authentication because it is single-user over the SSH tunnel, which is
only true while the socket is on the loopback interface.
"""

from __future__ import annotations

import argparse
import sys

from sketchgen import db
from sketchgen import web


def cmd_web(args: argparse.Namespace) -> int:
    try:
        web.check_bind(args.bind)
    except web.Refused as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        return web.EXIT_REFUSED
    try:
        return web.serve(
            bind=args.bind,
            port=args.port,
            db_path=args.db,
            jobs_dir=args.jobs_dir,
            once_for_test=args.once_for_test,
        )
    except OSError as exc:
        print(f"sketchgen: web failed to start: {exc}", file=sys.stderr)
        return web.EXIT_FAIL


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "web",
        help="serve the operator's screens on 127.0.0.1 (console, queue, jobs, held)",
        description=(
            "Serve the five operator screens — Console, Queue, New job, Job "
            "detail, Held for publication — over HTTP on 127.0.0.1, with the "
            "worker's pause / stop now / resume control in every header. There "
            "is no authentication and no CSRF token, because there is one user "
            "and one loopback socket: --bind accepts 127.0.0.1 and refuses "
            "anything else with exit 3. Reach it through the SSH tunnel that "
            "already carries preview on 8080."
        ),
    )
    parser.add_argument(
        "--bind",
        default=web.DEFAULT_BIND,
        metavar="ADDR",
        help="address to bind (127.0.0.1 only; anything else exits 3)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=web.DEFAULT_PORT,
        metavar="N",
        help=f"port (default {web.DEFAULT_PORT}; 0 picks a free one)",
    )
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.add_argument(
        "--jobs-dir",
        default=web.DEFAULT_JOBS_DIR,
        metavar="D",
        help=f"where the worker writes job logs and attempts "
        f"(default: {web.DEFAULT_JOBS_DIR})",
    )
    parser.add_argument(
        "--once-for-test",
        action="store_true",
        help=(
            "serve until POST /_quit or %d seconds, then exit — the only mode "
            "this server may run in on the node" % int(web.ONCE_TIMEOUT_S)
        ),
    )
    parser.set_defaults(func=cmd_web, _parser=parser)
