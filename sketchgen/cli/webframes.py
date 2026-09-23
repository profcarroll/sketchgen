"""``sketchgen web-frames``: make the WebP copies the gallery publishes.

The publisher makes an entry's web copies as it publishes it. This is for the
entries published before that existed — all 1,194 on 2026-09-22, whose PNGs
had put the gallery over GitHub Pages' 1 GB cap — and for any a publish could
not encode. It writes only beside the gate's PNGs on the node; the gallery
picks the copies up on the next render, which for a backfill is
``publish-index`` (docs/plans/gallery-hub.md, Step 0).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from sketchgen import db
from sketchgen import gallery
from sketchgen import webimg

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: Files per child process: a bounded timeout each, and a progress line
#: between them on a backfill that takes minutes.
BATCH = 100


def _public_ids(conn) -> list[int]:
    return [
        int(row[0])
        for row in conn.execute(
            "SELECT id FROM entries WHERE published_utc IS NOT NULL ORDER BY id"
        )
    ]


def cmd(args: argparse.Namespace) -> int:
    if not args.all and not args.entry_id:
        print("sketchgen: name entry ids, or pass --all", file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(os.path.expanduser(args.db))
    ids = _public_ids(conn) if args.all else list(args.entry_id)
    pngs = [png for entry_id in ids for png in gallery.frame_pngs(conn, entry_id)]
    missing = [png for png in pngs if webimg.cached(png) is None]

    if args.dry_run or not missing:
        summary = {"entries": len(ids), "frames": len(pngs), "missing": len(missing),
                   "made": 0, "failed": {}}
        _report(summary, args.json)
        return EXIT_OK
    if not webimg.available():
        print("sketchgen: refused: no encoder — Playwright is not importable by "
              f"{sys.executable}; run this with the node's venv", file=sys.stderr)
        return EXIT_REFUSED

    made = 0
    failed: dict[str, str] = {}
    for start in range(0, len(missing), BATCH):
        chunk = missing[start:start + BATCH]
        for png, outcome in webimg.ensure(chunk).items():
            if outcome in ("made", "cached"):
                made += outcome == "made"
            else:
                failed[png] = outcome
        if not args.json:
            print(f"  {min(start + BATCH, len(missing))}/{len(missing)} frames",
                  file=sys.stderr, flush=True)
    _report({"entries": len(ids), "frames": len(pngs), "missing": len(missing),
             "made": made, "failed": failed}, args.json)
    return EXIT_FAIL if failed else EXIT_OK


def _report(summary: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, sort_keys=True))
        return
    print(f"{summary['entries']} entries, {summary['frames']} frames, "
          f"{summary['missing']} without a web copy, {summary['made']} made, "
          f"{len(summary['failed'])} failed")
    for png, why in sorted(summary["failed"].items()):
        print(f"  {png}: {why}")


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "web-frames",
        help="make the WebP copies of the gate's frames that the gallery publishes",
        description=(
            "Write a WebP beside each published entry's strip.png and ghost.png "
            "on the node, with the gate's own Chromium. The PNGs are untouched. "
            "The gallery uses the copies from its next render (publish-index). "
            "Refuses (exit 3) if this interpreter cannot import Playwright."
        ),
    )
    p.add_argument("entry_id", type=int, nargs="*", metavar="ID")
    p.add_argument("--all", action="store_true", help="every published entry")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="count what is missing and write nothing")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    p.set_defaults(func=cmd, _parser=p)
