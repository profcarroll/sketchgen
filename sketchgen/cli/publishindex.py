"""``sketchgen publish-index``: re-render the whole gallery and push it.

For template or asset changes. It changes no entry's state; the per-entry
``publish`` remains the only path that makes an entry public.
"""
from __future__ import annotations

import argparse
import os
import sys

from sketchgen import db
from sketchgen import publish as publication

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3


def cmd(args: argparse.Namespace) -> int:
    try:
        conn = db.connect(os.path.expanduser(args.db))
        sha, why = publication.publish_index(
            conn,
            os.path.expanduser(args.gallery_dir),
            key=os.path.expanduser(args.key) if args.key else None,
            remote=args.remote,
            write_path=args.write_path,
        )
    except publication.PublishRefused as exc:
        print(f"sketchgen: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if sha:
        print(sha)
        return EXIT_OK
    print(f"sketchgen: {why}", file=sys.stderr)
    return EXIT_OK if why == "site unchanged" else EXIT_FAIL


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "publish-index",
        help="re-render every published entry and the index, then commit and push",
        description="For template or asset changes. Changes no entry's state.",
    )
    p.add_argument(
        "--db",
        default=os.environ.get("SKETCHGEN_DB", "~/sketchgen/sketchgen.db"),
        metavar="P",
    )
    p.add_argument(
        "--gallery-dir",
        default=os.environ.get("SKETCHGEN_GALLERY", "~/sketchgen/gallery"),
        metavar="D",
    )
    p.add_argument(
        "--key",
        default=os.environ.get("SKETCHGEN_GALLERY_KEY", "~/.ssh/sketchgen-gallery"),
        metavar="F",
        help="deploy key for an ssh remote (default: ~/.ssh/sketchgen-gallery)",
    )
    p.add_argument("--remote", default=None, metavar="URL")
    p.add_argument(
        "--write-path",
        dest="write_path",
        default=os.environ.get("SKETCHGEN_WRITEPATH_URL") or None,
        metavar="URL",
        help="the gallery write-path base URL to record in config.json and every page",
    )
    p.set_defaults(func=cmd, _parser=p)
