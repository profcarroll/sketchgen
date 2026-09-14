"""`sketchgen render`, `render-index` and `render-all` — the static gallery.

Packet 3.1. Registered by bin/sketchgen through sketchgen/cli/__init__.py, so
this file is the only one the packet adds to the CLI surface.

Exit codes follow the delegate.py convention the whole project uses: 0 success,
1 failure, 3 refusal. A refusal is the interesting one here — the generator
refuses rather than publishes when an entry would put an email-shaped string or
the node's hostname into a public repository, and when asked for an entry that
is not public (held and rejected entries stay off the site: publication holds
for a person, spec §9).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import gallery

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--gallery-dir",
        required=True,
        metavar="D",
        help="the gallery checkout root — the directory Pages serves",
    )
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.add_argument(
        "--write-path",
        default=None,
        metavar="URL",
        help=(
            "base URL of the gallery write path (/counts, /like, /vote, /login). "
            "Empty until packet 3.3 is deployed; pages then render '—' and a "
            "disabled like button"
        ),
    )
    parser.add_argument(
        "--gallery-url",
        default=None,
        metavar="URL",
        help=f"public URL of the gallery (default: {gallery.DEFAULT_GALLERY_URL})",
    )


def _config(args: argparse.Namespace, dest: Path) -> gallery.Config:
    """The checkout's config.json, with the flags given on top of it."""
    config = gallery.Config.load(dest)
    if args.write_path is not None:
        config = gallery.Config(
            write_path=args.write_path,
            gallery_url=config.gallery_url,
            repository=config.repository,
        )
    if args.gallery_url is not None:
        config = gallery.Config(
            write_path=config.write_path,
            gallery_url=args.gallery_url,
            repository=config.repository,
        )
    return config


def _prepare(args: argparse.Namespace) -> tuple[sqlite3.Connection, Path, gallery.Config]:
    dest = Path(args.gallery_dir).expanduser()
    if not dest.is_dir():
        raise Refusal(f"no gallery checkout at {dest}: create or clone it first")
    database = Path(args.db).expanduser()
    if not database.is_file():
        raise Refusal(f"no database at {database}: run `sketchgen db init` first")
    return db.connect(database), dest, _config(args, dest)


class Refusal(Exception):
    """Something the generator will not do; exit 3, nothing written."""


def _run(args: argparse.Namespace, work) -> int:
    try:
        conn, dest, config = _prepare(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        written = work(conn, dest, config)
    except (gallery.Unsafe, gallery.UnknownEntry) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()
    for path in written:
        print(path)
    return EXIT_OK


def cmd_render(args: argparse.Namespace) -> int:
    return _run(
        args,
        lambda conn, dest, config: [
            gallery.render_entry(conn, args.entry_id, dest, config)
        ],
    )


def cmd_render_index(args: argparse.Namespace) -> int:
    return _run(args, lambda conn, dest, config: gallery.render_index(conn, dest, config))


def cmd_render_all(args: argparse.Namespace) -> int:
    return _run(args, lambda conn, dest, config: gallery.render_all(conn, dest, config))


def register(top: argparse._SubParsersAction) -> None:
    render = top.add_parser(
        "render",
        help="render one entry into <gallery>/e/<id>/",
        description=(
            "Render one published or failed-kept entry: the entry page, the "
            "sketch verbatim in its own directory, the gate's frames, "
            "statement.md and meta.json. Refuses (exit 3) if the entry is not "
            "public or if any file would carry personal data."
        ),
    )
    render.add_argument("entry_id", type=int, help="the entry id, as in e/<id>/")
    _add_common(render)
    render.set_defaults(func=cmd_render, _parser=render)

    index = top.add_parser(
        "render-index",
        help="render index.html, failed.html, compare.html, lines/, assets/, config.json",
        description=(
            "Render everything that is not an entry directory: the gallery grid, "
            "the kept failures, the compare shell, one page per lineage line, the "
            "stylesheet and script, and config.json with the write-path and "
            "gallery URLs."
        ),
    )
    _add_common(index)
    index.set_defaults(func=cmd_render_index, _parser=index)

    every = top.add_parser(
        "render-all",
        help="render every public entry and then the pages that index them",
        description=(
            "render-index, then render for every published and failed-kept entry. "
            "Deterministic: the same database gives byte-identical output, which "
            "is what makes the publisher's commit in packet 3.2 meaningful."
        ),
    )
    _add_common(every)
    every.set_defaults(func=cmd_render_all, _parser=every)
