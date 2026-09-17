"""`sketchgen render`, `render-index`, `render-all`, `publish-rejected` and
`repoint-kept`.

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
import json
import os
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import gallery
from sketchgen import publish as publication
from sketchgen import worker

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


# ---------------------------------------------------------------------------
# publish-rejected — the one-time backfill of §5.2
# ---------------------------------------------------------------------------

#: What a rejection with nothing written down says on the page. Two of the
#: three placeholders the old operator UI wrote are indistinguishable from a
#: real reason by shape alone, so they are named here rather than guessed at.
NO_REASON = "rejected by operator (reason not recorded)"

#: The placeholders the old ``web.reject_entry`` wrote when the operator left
#: the reason box empty. They are not reasons and this backfill does not
#: pretend they are.
PLACEHOLDERS = frozenset({"rejected by operator", "entry rejected", ""})

#: Fragments that mean a machine wrote the line: the gate, the executor or a
#: traceback, reaching ``last_error`` by some path other than a person typing
#: into the reject box. None of these is an operator's reason.
MACHINE_MARKERS = (
    "traceback",
    "gate exit",
    "executor:",
    "planner:",
    "exit code",
    "console_clean",
    "frame_advancing",
    "archived by operator",
)


def backfill_reason(last_error: str | None) -> str:
    """The reason to record for an existing rejection, from the job's last_error.

    Before migration 007 the operator UI wrote the reason a person typed into
    the originating job's ``last_error`` and nowhere else, so that column is
    the only record of it and this is where it is read back. It is read
    carefully: ``last_error`` is also where the executor and the planner put
    their failures, and an empty reject box wrote a placeholder rather than
    nothing. Anything that is not plainly a person's sentence becomes
    :data:`NO_REASON`, which is honest, rather than a machine's error message
    dressed up as a verdict.
    """
    text = " ".join((last_error or "").split())
    if text.lower().startswith("entry rejected:"):
        # publish.reject's own spelling; what follows the colon is the reason.
        text = text.split(":", 1)[1].strip()
    if text.lower() in PLACEHOLDERS:
        return NO_REASON
    if len(text) > 200:
        return NO_REASON
    lowered = text.lower()
    if any(marker in lowered for marker in MACHINE_MARKERS):
        return NO_REASON
    return text


def _rejected_backlog(conn: sqlite3.Connection, ids: list[int] | None) -> list:
    """Rejected entries nobody has published, id order, with their reasons."""
    rows = conn.execute(
        "SELECT e.id AS id, e.reject_reason AS reject_reason, "
        "j.last_error AS last_error FROM entries e "
        "LEFT JOIN jobs j ON j.id = e.job_id "
        "WHERE e.state = 'rejected' AND e.published_utc IS NULL ORDER BY e.id"
    ).fetchall()
    if ids is not None:
        wanted = set(ids)
        rows = [row for row in rows if int(row["id"]) in wanted]
    return rows


def cmd_publish_rejected(args: argparse.Namespace) -> int:
    """Render and push the rejections that were only ever a state flip.

    One entry at a time, in id order, each through :func:`publish.publish` —
    the same path and the same one-commit-per-entry shape the operator's
    Publish button uses. A failure stops the run rather than carrying on: the
    entries already pushed stay pushed, and the next run picks up where this
    one stopped, because a published entry is no longer in the backlog.
    """
    ids = sorted(set(args.entry_id)) if args.entry_id else None
    if ids is None and not args.all:
        print(
            "refused: say which — `--all`, or one or more entry ids",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    database = Path(args.db).expanduser()
    if not database.is_file():
        print(f"refused: no database at {database}", file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(database)
    try:
        rows = _rejected_backlog(conn, ids)
        if ids is not None:
            missing = sorted(set(ids) - {int(row["id"]) for row in rows})
            if missing:
                print(
                    "refused: not a rejected entry awaiting publication: "
                    + ", ".join(str(i) for i in missing),
                    file=sys.stderr,
                )
                return EXIT_REFUSED
        if not rows:
            print("nothing to publish: no rejected entry is waiting")
            return EXIT_OK
        if args.dry_run:
            for row in rows:
                reason = row["reject_reason"] or backfill_reason(row["last_error"])
                print(f"entry {int(row['id'])}: {reason}")
            print(f"{len(rows)} rejected entries would be published")
            return EXIT_OK
        for row in rows:
            entry_id = int(row["id"])
            reason = row["reject_reason"] or backfill_reason(row["last_error"])
            if not row["reject_reason"]:
                conn.execute(
                    "UPDATE entries SET reject_reason = ? WHERE id = ?",
                    (reason, entry_id),
                )
                conn.commit()
            try:
                result = publication.publish(
                    conn,
                    entry_id,
                    gallery_dir=args.gallery_dir,
                    key=os.path.expanduser(args.key) if args.key else None,
                    remote=args.remote,
                    by=args.by,
                )
            except publication.PublishRefused as exc:
                print(f"refused at entry {entry_id}: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            except publication.PublishFailed as exc:
                print(f"failed at entry {entry_id}: {exc}", file=sys.stderr)
                return EXIT_FAIL
            print(f"entry {entry_id}: {result.commit} — {reason}")
    finally:
        conn.close()
    return EXIT_OK


# ---------------------------------------------------------------------------
# repoint-kept — the one-time repair of what a kept failure shows
# ---------------------------------------------------------------------------


def _best_of(conn: sqlite3.Connection, job_id: int):
    """The attempt a kept entry should be showing, and what it missed.

    Same ranking as worker._best_attempt and for the same reason: runs at all,
    then how much of the plan it managed, then recency. Returns
    ``(attempt_row, report, missed)`` or ``(None, {}, [])``.
    """
    rows = list(conn.execute(
        "SELECT * FROM attempts WHERE job_id = ? ORDER BY n", (job_id,)
    ))
    if not rows:
        return None, {}, []

    def report_of(row):
        path = row["gate_report_path"]
        if not path or not Path(path).is_file():
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def rank(row):
        report = report_of(row)
        assertions = report.get("assertions") or {}
        passed = sum(1 for v in assertions.values() if (v or {}).get("pass"))
        return (1 if worker.qa_clean(report) else 0, passed, int(row["n"]))

    best = max(rows, key=rank)
    report = report_of(best)
    # Only a sketch that ran can be off-plan, the same rule worker._create_entry
    # applies: one that threw missed its assertions too, and recording that as a
    # divergence would put "this sketch runs" on the page of one that does not.
    if not worker.qa_clean(report):
        return best, report, []
    return best, report, worker.missed_assertions(report)


def cmd_repoint_kept(args: argparse.Namespace) -> int:
    """Point every kept failure at its best attempt instead of its last.

    An entry has always taken its files from the attempt that ENDED the job.
    Usually that is also the best one and this changes nothing for it; on the 45
    kept entries it moves 7. Entry 429 is why those 7 matter: it publishes a
    blank canvas from a tenth attempt whose image never arrived, while its
    second drew a working puzzle from an image it built itself.

    This rewrites nothing but which attempt an entry points at: ``source_dir``,
    ``strip_path``, ``png_path``, the statement and the executor that wrote it,
    plus ``offplan_json`` so the pages can tell a working divergence from a
    failure. No file in any attempt directory is touched and **no state
    changes**. It had a ``--reclassify`` flag that moved the QA-clean ones into
    ``held``; it did so with a raw UPDATE, and ENTRY_TRANSITIONS does not allow
    failed-kept -> held at all — for a published entry that would have taken it
    off the public record, which is the deletion this project does not do. See
    ``reopen-offplan`` for the version that goes through the state machine and
    can only touch a kept failure nobody has published.
    """
    database = Path(args.db).expanduser()
    if not database.is_file():
        print(f"refused: no database at {database}", file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(database)
    repointed = 0
    try:
        rows = list(conn.execute(
            "SELECT * FROM entries WHERE state = 'failed-kept' ORDER BY id"
        ))
        if not rows:
            print("nothing to repoint: no kept failure is recorded")
            return EXIT_OK
        for row in rows:
            entry_id = int(row["id"])
            best, report, missed = _best_of(conn, int(row["job_id"]))
            if best is None:
                print(f"entry {entry_id}: no attempts recorded, left alone")
                continue
            current = str(row["source_dir"] or "")
            target = str(best["source_dir"] or "")
            clean = worker.qa_clean(report)
            change = "same attempt" if current == target else (
                f"{Path(current).name or '—'} -> {Path(target).name}")
            print(f"entry {entry_id}: {change}"
                  f" · {'runs clean' if clean else 'no clean attempt'}"
                  f" · missed {', '.join(missed) or 'nothing'}")
            if args.dry_run:
                continue
            artefacts = report.get("artefacts") or {}
            conn.execute(
                "UPDATE entries SET source_dir = ?, strip_path = ?, png_path = ?, "
                "statement = ?, executor = ?, executor_prompt_version = ?, "
                "offplan_json = ? WHERE id = ?",
                (target or None, artefacts.get("strip"), artefacts.get("png"),
                 best["statement"], best["model"], best["prompt_version"],
                 json.dumps(missed) if missed else None, entry_id),
            )
            if current != target:
                repointed += 1
        if args.dry_run:
            print(f"{len(rows)} kept entries would be examined; nothing written")
            return EXIT_OK
        conn.commit()
        print(f"{len(rows)} kept entries examined, {repointed} repointed")
    finally:
        conn.close()
    print("now re-render and push: sketchgen publish-index")
    return EXIT_OK


# ---------------------------------------------------------------------------
# reopen-offplan — the door back for a kept failure that was never a failure
# ---------------------------------------------------------------------------


def cmd_reopen_offplan(args: argparse.Namespace) -> int:
    """Move a kept failure that RAN back into held, for a person to judge.

    Publishing decides which page an entry lands on, and it decides it from the
    state: a ``held`` entry becomes ``published`` and joins the grid, while a
    ``failed-kept`` one keeps its state and joins the rejections page
    (publish.py, "keeps its state, which is what puts it on the rejections page
    rather than the grid"). So an off-plan sketch recorded under the old rule
    can currently only ever be published as a failure, however good it is. This
    is the door back.

    Narrow on purpose:

    * ``failed-kept`` only, and only where ``offplan_json`` says it ran clean
      and merely diverged from the plan. A sketch that threw is still a failure.
    * ``published_utc IS NULL`` only. A kept failure already on the site is part
      of the public record; ``held`` is not a public state, so reopening one
      would take it off the gallery, and that is the deletion this project does
      not do. :func:`db.entry_transition` refuses it anyway — this only declines
      to ask.

    The move goes through ``entry_transition``, so the state machine is the
    thing that decides, not this command. The earlier ``repoint-kept
    --reclassify`` wrote the state with a raw UPDATE and would have taken 18
    published entries off the site; it is gone.

    The job row is left alone. It is ``failed`` and terminal, and a sixth
    terminal job state to mean "its entry got a second look" would mean touching
    every count and funnel in the console to say nothing new (db.py says the
    same thing about archiving).
    """
    ids = sorted(set(args.entry_id)) if args.entry_id else None
    if ids is None and not args.all:
        print("refused: say which — `--all`, or one or more entry ids",
              file=sys.stderr)
        return EXIT_REFUSED
    database = Path(args.db).expanduser()
    if not database.is_file():
        print(f"refused: no database at {database}", file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(database)
    moved = 0
    try:
        rows = list(conn.execute(
            "SELECT * FROM entries WHERE state = 'failed-kept' "
            "AND published_utc IS NULL AND offplan_json IS NOT NULL ORDER BY id"
        ))
        if ids is not None:
            wanted = set(ids)
            found = {int(r["id"]) for r in rows}
            missing = sorted(wanted - found)
            if missing:
                print("refused: not an unpublished off-plan kept failure: "
                      + ", ".join(str(i) for i in missing), file=sys.stderr)
                return EXIT_REFUSED
            rows = [r for r in rows if int(r["id"]) in wanted]
        if not rows:
            print("nothing to reopen: no unpublished kept failure ran clean")
            return EXIT_OK
        for row in rows:
            entry_id = int(row["id"])
            try:
                missed = ", ".join(json.loads(row["offplan_json"] or "[]"))
            except (TypeError, ValueError):
                missed = "?"
            print(f"entry {entry_id}: failed-kept -> held · ran clean · missed {missed}")
            if args.dry_run:
                continue
            try:
                db.entry_transition(conn, entry_id, "held")
            except db.IllegalTransition as exc:
                print(f"refused at entry {entry_id}: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            moved += 1
        if args.dry_run:
            print(f"{len(rows)} would be reopened; nothing written")
            return EXIT_OK
        conn.commit()
        print(f"{moved} reopened; they are on the Held page for a person to judge")
    finally:
        conn.close()
    return EXIT_OK


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


    backfill = top.add_parser(
        "publish-rejected",
        help="publish the operator's existing rejections to the rejections page",
        description=(
            "The one-time backfill for the lineage ledger's §5.2. Rejecting an "
            "entry used to be a state flip and nothing else, so every rejection "
            "made before that change is invisible with all of its files still "
            "on disk. This renders and pushes each of them, in id order, one "
            "commit per entry, exactly as the operator's Publish button does. "
            "The reason comes from the entry's reject_reason, or from the "
            "originating job's last_error where that reads as something a "
            "person typed; where it does not, the page says the reason was "
            "not recorded rather than inventing one. --dry-run reads the "
            "database and nothing else: no checkout, no push, no write."
        ),
    )
    backfill.add_argument(
        "entry_id", type=int, nargs="*", metavar="ID",
        help="the entries to publish; omit and pass --all for every one waiting",
    )
    backfill.add_argument(
        "--all", action="store_true",
        help="every rejected entry whose published_utc is null",
    )
    backfill.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="print what would be published, and the reason each would carry",
    )
    backfill.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    backfill.add_argument(
        "--gallery-dir",
        default=publication.DEFAULT_GALLERY_DIR,
        metavar="D",
        help="the gallery checkout to commit into",
    )
    backfill.add_argument(
        "--key",
        default=publication.DEFAULT_KEY_PATH,
        metavar="F",
        help="deploy key for an ssh remote (default: ~/.ssh/sketchgen-gallery)",
    )
    backfill.add_argument("--remote", default=None, metavar="URL")
    backfill.add_argument(
        "--by", default=None, metavar="USER",
        help="the GitHub username for the commit's Published-By: trailer",
    )
    backfill.set_defaults(func=cmd_publish_rejected, _parser=backfill)

    repoint = top.add_parser(
        "repoint-kept",
        help="point every kept failure at its best attempt instead of its last",
        description=(
            "A kept failure has always shown the attempt that ended the job, "
            "which is usually but not always its best one: on the first 45 it "
            "moves 7, entry 429 among them. This points each one at its best "
            "attempt instead — the same "
            "ranking the worker now uses: runs at all, then how much of the plan "
            "it managed, then recency — and records which assertions that "
            "attempt missed. It touches no file in any attempt directory and "
            "changes no entry's state — see reopen-offplan for that. "
            "--dry-run reads the database and writes nothing."
        ),
    )
    repoint.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="print what would change and write nothing",
    )
    repoint.add_argument(
        "--db", default=db.DEFAULT_DB_PATH, metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    repoint.set_defaults(func=cmd_repoint_kept, _parser=repoint)

    reopen = top.add_parser(
        "reopen-offplan",
        help="move an unpublished kept failure that RAN back into held",
        description=(
            "Publishing reads the state to decide the page: a held entry joins "
            "the grid, a failed-kept one joins the rejections page. So a sketch "
            "that ran clean and only diverged from the plan can currently only "
            "ever be published as a failure. This moves it back to held, for a "
            "person to judge, through the entry state machine. Only kept "
            "failures whose offplan_json says they ran, and only ones nobody "
            "has published: an entry already on the site stays on it."
        ),
    )
    reopen.add_argument(
        "entry_id", type=int, nargs="*", metavar="ID",
        help="the entries to reopen; omit and pass --all for every one eligible",
    )
    reopen.add_argument("--all", action="store_true",
                        help="every unpublished kept failure that ran clean")
    reopen.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="print what would move and write nothing")
    reopen.add_argument(
        "--db", default=db.DEFAULT_DB_PATH, metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    reopen.set_defaults(func=cmd_reopen_offplan, _parser=reopen)
