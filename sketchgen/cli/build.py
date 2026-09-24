"""``sketchgen build`` and ``sketchgen pin`` — which build this node is on, and
which it is meant to be on.

Drop-in subcommands (see sketchgen/cli/__init__.py), docs/plans/fleet.md
Packet 1. `build` is a reading: checkout, target, upstream, the build each
long-lived process is running, the queue. `pin` is the one write: it declares
the commit this node stays on, which `update.sh` then moves the checkout to and
never past. Before it, the hardware A/B's pin on a72f076 was a detached HEAD on
two nodes and nothing at all on the third, and a routine deploy would have
ended the A/B without a word (2026-09-24).

Exit codes. `build`: 0 on target, 1 not — the last line says why; never 3, it
is a reading, not a check. `pin`: 0 done, 3 refused (no database, a ref the
checkout cannot find, a missing --reason or --by), nothing written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sketchgen import build
from sketchgen import db

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3


def _root(args: argparse.Namespace) -> Path:
    return Path(args.root).expanduser() if args.root else build.ROOT


def cmd_needs_render(args: argparse.Namespace) -> int:
    """One line for update.sh: ``render <why>`` or ``skip <why>``."""
    old, new = args.needs_render
    root = _root(args)
    paths = build.changed_paths(root, old, new)
    if paths is None:
        print(f"refused: cannot diff {old}..{new} in {root}", file=sys.stderr)
        return EXIT_REFUSED
    needed, why = build.render_verdict(root, paths)
    print(f"{'render' if needed else 'skip'} {why}")
    return EXIT_OK


def cmd_build(args: argparse.Namespace) -> int:
    if args.needs_render:
        return cmd_needs_render(args)
    doc = build.reading(_root(args), Path(args.db).expanduser(), do_fetch=args.fetch)
    if args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(build.render(doc))
    return EXIT_OK if doc["on_target"] else EXIT_FAIL


def _pin_line(conn) -> str:
    sha = db.get_meta(conn, build.PIN_SHA)
    if not sha:
        return "no pin: this node follows main"
    reason = db.get_meta(conn, build.PIN_REASON) or ""
    by = db.get_meta(conn, build.PIN_BY) or "?"
    utc = db.get_meta(conn, build.PIN_UTC) or "?"
    return f'pinned {sha[:7]} — "{reason}" ({by}, {utc})'


def cmd_pin(args: argparse.Namespace) -> int:
    path = Path(args.db).expanduser()
    if not path.is_file():
        print(f"refused: no database at {path} (run: sketchgen db init)", file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(path)
    root = _root(args)
    if args.clear:
        if not args.by:
            print("refused: --clear needs --by LOGIN", file=sys.stderr)
            return EXIT_REFUSED
        was = db.get_meta(conn, build.PIN_SHA)
        build.clear_pin(conn)
        print(f"pin cleared by {args.by}" + (f" (was {was[:7]})" if was else "")
              + "; this node follows main from the next update.sh")
        return EXIT_OK
    if args.ref is None:
        print(_pin_line(conn))
        return EXIT_OK
    if not (args.reason or "").strip() or not (args.by or "").strip():
        print("refused: a pin needs --reason (why this node stays behind) and "
              "--by LOGIN", file=sys.stderr)
        return EXIT_REFUSED
    sha = build.resolve(root, args.ref)
    if sha is None:
        # A pin names a commit someone else's node is on; this checkout may not
        # have fetched it yet.
        build.git(root, "fetch", "--quiet", "origin", timeout=30)
        sha = build.resolve(root, args.ref)
    if sha is None:
        print(f"refused: {args.ref!r} is not a commit this checkout has, even "
              f"after a fetch", file=sys.stderr)
        return EXIT_REFUSED
    build.set_pin(conn, sha, args.reason.strip(), args.by.strip())
    print(_pin_line(conn))
    here = build.checkout(root)["sha"]
    if here != sha:
        print(f"the checkout is on {build.short(here)}: update.sh (or bin/fleet "
              f"update) moves it to {sha[:7]}")
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "build",
        help="which build this node is on, is meant to be on, and is running",
        description=(
            "Read this node's build: the checkout (sha, branch, clean or "
            "dirty), its target (main, or the pin `sketchgen pin` set), how "
            "far behind main it is in PRs, the build the worker and the web "
            "process are running (stamped when each started), the schema, the "
            "generator switch and the queue. Exit 0 when the checkout is its "
            "target, the tree is clean and both processes run the checkout; "
            "exit 1 otherwise, with the reasons on the last line."
        ),
    )
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH, metavar="P",
                        help="database file (default: $SKETCHGEN_DB, else "
                             "~/sketchgen/sketchgen.db)")
    parser.add_argument("--fetch", action="store_true",
                        help="git fetch origin main first (10 s timeout; offline "
                             "is reported, not fatal)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--needs-render", nargs=2, metavar=("OLD", "NEW"),
                        help="instead: whether a deploy from OLD to NEW has to "
                             "re-render the gallery; prints `render <why>` or "
                             "`skip <why>` (update.sh --render=auto)")
    parser.add_argument("--root", default=None, help=argparse.SUPPRESS)
    parser.set_defaults(func=cmd_build, _parser=parser)

    pin = top.add_parser(
        "pin",
        help="keep this node on one commit (an A/B arm), or follow main again",
        description=(
            "With REF, pins this node: the ref is resolved to a full sha now, "
            "so a branch or tag that moves later does not move the pin, and "
            "update.sh moves the checkout to it and never past it. --clear "
            "goes back to following main. With neither, prints the pin. It "
            "moves nothing itself: the next update.sh does."
        ),
    )
    pin.add_argument("ref", nargs="?", help="commit, tag or branch to pin to")
    pin.add_argument("--reason", help="why this node stays behind (shown on the "
                                      "Console and by bin/fleet)")
    pin.add_argument("--by", help="GitHub login of whoever is pinning it")
    pin.add_argument("--clear", action="store_true", help="remove the pin")
    pin.add_argument("--db", default=db.DEFAULT_DB_PATH, metavar="P",
                     help="database file")
    pin.add_argument("--root", default=None, help=argparse.SUPPRESS)
    pin.set_defaults(func=cmd_pin, _parser=pin)
