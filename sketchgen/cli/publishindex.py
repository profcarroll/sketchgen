"""``sketchgen publish-index``: re-render the whole gallery and push it.

For template or asset changes. It changes no entry's state; the per-entry
``publish`` remains the only path that makes an entry public.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import IO

from sketchgen import db
from sketchgen import publish as publication

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: Characters of bar between the brackets.
BAR_WIDTH = 30


def bar_line(done: int, total: int, label: str, elapsed: float, width: int = BAR_WIDTH) -> str:
    """One line of progress: a bar, the count, the time so far, and the step.

    The estimate is the remaining steps at the pace so far. It is shown only
    once a few steps are in, because one page's timing says nothing yet, and
    the last steps (staging, commit, push) are not pages, so it is an
    estimate and says so with the tilde.
    """
    total = max(total, 1)
    filled = round(width * min(done, total) / total)
    bar = "█" * filled + "░" * (width - filled)
    pct = 100 * min(done, total) // total
    eta = ""
    if 3 <= done < total:
        eta = f" ~{_clock((total - done) * elapsed / done)} left"
    return f"[{bar}] {pct:3d}% {done}/{total} {_clock(elapsed)}{eta} · {label}"


def _clock(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 60}:{seconds % 60:02d}"


def progress_bar(out: IO[str]):
    """A callback for ``publish_index`` that redraws one line on ``out``.

    A carriage return, not a newline, so the line is redrawn in place; the
    deploy's ssh has no pty, so this cannot ask the terminal anything, and
    a plain ``\r`` is what works there. The last call ends the line.
    """
    start = time.monotonic()
    longest = 0

    def draw(done: int, total: int, label: str) -> None:
        nonlocal longest
        line = bar_line(done, total, label, time.monotonic() - start)
        longest = max(longest, len(line))
        out.write("\r" + line.ljust(longest))
        if done >= total or label == "unchanged":
            out.write("\n")
        out.flush()

    return draw


def cmd(args: argparse.Namespace) -> int:
    try:
        conn = db.connect(os.path.expanduser(args.db))
        sha, why = publication.publish_index(
            conn,
            os.path.expanduser(args.gallery_dir),
            key=os.path.expanduser(args.key) if args.key else None,
            remote=args.remote,
            write_path=args.write_path,
            on_step=progress_bar(sys.stderr) if args.progress else None,
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
    p.add_argument(
        "--progress",
        action="store_true",
        help="draw a progress bar on stderr, one line redrawn per step",
    )
    p.set_defaults(func=cmd, _parser=p)
