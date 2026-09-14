"""`sketchgen lineage spawn | critique | show` — the self-prompting half.

Packet 5.3. Registered by bin/sketchgen through sketchgen/cli/__init__.py, so
this file is the whole CLI surface the packet adds.

Exit codes are the project's: 0 success, 1 failure, 3 refusal. The split that
matters here is between a critique the model got wrong (1, and the raw response
is written to ``--out`` so the next person can read what it actually said) and
something we will not do at all (3: no database, no such entry, an unreadable
prompt file or stub).

``lineage critique`` is the only command in this file that can reach a model,
and only when it is given no ``--stub``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import lineage

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


# ---------------------------------------------------------------------------


def cmd_spawn(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    try:
        try:
            job_id = lineage.spawn(
                conn,
                parent_entry_id=args.parent,
                critique=args.critique,
                critique_by=args.critique_by or args.by,
                submitted_by=args.by,
                max_depth=args.max_depth,
            )
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        except sqlite3.Error as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return EXIT_FAIL

        if job_id is None:
            row = conn.execute(
                "SELECT state FROM entries WHERE id = ?", (args.parent,)
            ).fetchone()
            state = row["state"] if row else "not an entry"
            message = (
                f"nothing spawned: entry {args.parent} is {state}; a line grows "
                "from a published or failed-kept entry only"
            )
            if args.json:
                print(json.dumps({"job_id": None, "parent_entry_id": args.parent,
                                  "state": state, "reason": message}, indent=2))
            else:
                print(message, file=sys.stderr)
            return EXIT_FAIL
        job = db.get_job(conn, job_id)
        generation = lineage.generation_of(conn, args.parent) + 1
        document = {
            "job_id": job_id,
            "parent_entry_id": args.parent,
            "generation": generation,
            "max_depth": args.max_depth,
            "state": job.state if job else None,
            "publication": job.publication if job else None,
            "needs": job.needs if job else None,
            "critique": job.critique if job else None,
            "critique_by": job.critique_by if job else None,
            "prompt": job.prompt if job else None,
        }
        if args.json:
            print(json.dumps(document, indent=2))
        else:
            print(f"job {job_id} queued: generation {generation} of entry {args.parent}")
            if job is not None and job.needs == "review":
                print(
                    f"  generation {generation} is at the depth limit "
                    f"({args.max_depth}): held, needs review, waiting for a person"
                )
        return EXIT_OK
    finally:
        conn.close()


def cmd_critique(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    out = Path(args.out).expanduser()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        conn.close()
        print(f"refused: cannot write to {out}: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        result = lineage.critique(
            conn, args.entry, model=args.model, host=args.host, stub=args.stub
        )
    except lineage.CritiqueRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except lineage.CritiqueFailed as exc:
        raw_path = out / f"entry-{args.entry}-critique-raw.txt"
        try:
            raw_path.write_text(exc.raw or "", encoding="utf-8")
        except OSError:  # pragma: no cover - disk failure
            raw_path = None
        print(f"failed: {exc}", file=sys.stderr)
        if raw_path is not None:
            print(f"raw response kept at {raw_path}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()

    document = {
        "entry_id": args.entry,
        "critique": result.text,
        "critique_by": result.model,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "tokens": result.tokens,
        "stub": str(args.stub) if args.stub else None,
        "created_utc": db.utc_now(),
    }
    path = out / f"entry-{args.entry}-critique.json"
    try:
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        (out / f"entry-{args.entry}-critique-raw.txt").write_text(
            result.raw, encoding="utf-8"
        )
    except OSError as exc:
        print(f"failed: cannot write {path}: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if args.json:
        print(json.dumps(document, indent=2))
    else:
        print(result.text)
        print(f"({result.model}, {result.prompt_version}) -> {path}")
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    conn = _open(args)
    if conn is None:
        return EXIT_REFUSED
    try:
        generations = lineage.line(conn, args.root)
    except sqlite3.Error as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()
    if not generations:
        print(f"refused: there is no entry {args.root}", file=sys.stderr)
        return EXIT_REFUSED
    if args.json:
        print(json.dumps({"root_entry_id": args.root, "generations": generations},
                         indent=2))
        return EXIT_OK
    for item in generations:
        head = f"gen {item['generation']}  entry {item['entry_id']}  {item['state']}"
        print(head)
        if item["critique"]:
            print(f"    revise: {item['critique']}  — {item['critique_by'] or '?'}")
        print(f"    {' '.join(str(item['prompt'] or '').split())}")
        if item["at_limit"]:
            print("    at the depth limit: this generation waits for a person")
    return EXIT_OK


# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "lineage",
        help="spawn a child from a critique, write a critique, show a line",
        description=(
            "The gallery's second source of prompts (spec §8.1): the critique "
            "of one entry becomes the prompt for its child. A line runs "
            "DECIDE[lineage-depth] generations — three — and then waits for a "
            "person."
        ),
    )
    sub = parser.add_subparsers(dest="lineage_command", metavar="{spawn,critique,show}")
    parser.set_defaults(
        func=lambda args: (parser.print_help(), EXIT_REFUSED)[1], _parser=parser
    )

    spawn = sub.add_parser(
        "spawn",
        help="queue the child job one critique asks for",
        description=(
            "Compose the parent's prompt with the critique under 'Revise:' and "
            "queue it as a child job with the parent recorded. Exits 1 and "
            "writes nothing when the parent is not published or failed-kept."
        ),
    )
    spawn.add_argument("--parent", type=int, required=True, metavar="ID",
                       help="the parent ENTRY id (not a job id)")
    spawn.add_argument("--critique", required=True, metavar="TEXT",
                       help="one sentence: what the child should do differently")
    spawn.add_argument("--by", required=True, metavar="WHO",
                       help="GitHub username submitting the child job")
    spawn.add_argument("--critique-by", default=None, metavar="WHO",
                       help="model id or GitHub username that wrote the critique "
                            "(default: --by)")
    spawn.add_argument("--max-depth", type=int, default=lineage.DEFAULT_MAX_DEPTH,
                       metavar="N",
                       help=f"generations before a person must touch the line "
                            f"(default: {lineage.DEFAULT_MAX_DEPTH})")
    spawn.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(spawn)
    spawn.set_defaults(func=cmd_spawn, _parser=spawn)

    critique = sub.add_parser(
        "critique",
        help="ask a local model for the one sentence that becomes the next prompt",
        description=(
            "One sentence, under forty words, no code. Anything else exits 1 "
            "with the raw response saved under --out. With --stub it replays a "
            "saved response and calls no model at all."
        ),
    )
    critique.add_argument("--entry", type=int, required=True, metavar="ID")
    critique.add_argument("--model", default=lineage.DEFAULT_MODEL, metavar="M",
                          help=f"model tag (default: {lineage.DEFAULT_MODEL})")
    critique.add_argument("--out", required=True, metavar="DIR",
                          help="directory for the critique and the raw response")
    critique.add_argument("--stub", default=None, metavar="FILE",
                          help="replay a saved response instead of calling a model")
    critique.add_argument("--host", default=lineage.DEFAULT_HOST, metavar="URL",
                          help=f"ollama host (default: {lineage.DEFAULT_HOST})")
    critique.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(critique)
    critique.set_defaults(func=cmd_critique, _parser=critique)

    show = sub.add_parser(
        "show",
        help="print one line, generation by generation",
        description=(
            "Every generation from one root, in order: the entry, the critique "
            "that produced it and who wrote it, the state, and the scores when "
            "packet 5.1's pairs module is installed."
        ),
    )
    show.add_argument("--root", type=int, required=True, metavar="ID",
                      help="the root ENTRY id")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db_option(show)
    show.set_defaults(func=cmd_show, _parser=show)
