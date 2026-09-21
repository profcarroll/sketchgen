"""`sketchgen paid export|import|status` — any step, answered off the node.

A drop-in subcommand (see sketchgen/cli/__init__.py). The work is in
sketchgen/paid.py; this is the shell around it, shaped to be driven by an agent
on a machine that holds a credential (docs/plans/agentic-cli.md §3.8):

  export  write what the node would have asked a model, for one step
  import  land the answers that packet comes back with
  status  what is waiting for an answer from off the node, per step

Every verb takes ``--json`` and prints one object. Exit codes, as everywhere in
this project: 0 success, 1 failure, 3 refused (no database, not a packet, a
step with no paid route). An import that rejects some items still exits 0: the
rejections are reported, and saved with their raw answers beside the packet.

Nothing here calls a model or runs a worker. **Never run `worker --once` beside
the daemon to make a paid job move** — an import puts the job back on the queue
and the daemon claims it on its next pass. #118 is the record of what happens
otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from sketchgen import db
from sketchgen import paid as paid_mod

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3

DEFAULT_JOBS_DIR = os.environ.get(
    "SKETCHGEN_JOBS", os.path.join(os.path.expanduser("~"), "sketchgen", "jobs")
)


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    parser.add_argument(
        "--jobs-dir",
        dest="jobs_dir",
        default=DEFAULT_JOBS_DIR,
        metavar="D",
        help="the worker's job directories (default: $SKETCHGEN_JOBS, else "
             "~/sketchgen/jobs)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def _ctx(args: argparse.Namespace) -> paid_mod.Context:
    return paid_mod.Context(jobs_dir=Path(os.path.expanduser(args.jobs_dir)))


def _run(args: argparse.Namespace, work) -> int:
    path = Path(os.path.expanduser(args.db))
    if not path.is_file():
        print(f"refused: no database at {path}: run `sketchgen db init` first",
              file=sys.stderr)
        return EXIT_REFUSED
    conn = db.connect(path)
    try:
        return work(conn)
    except paid_mod.PaidRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()


# ---------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        packet = paid_mod.export_packet(
            conn, args.step, model=args.model, limit=args.limit, ctx=_ctx(args)
        )
        count = len(packet["items"])
        out = Path(os.path.expanduser(args.out))
        if count:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")
        if args.json:
            print(json.dumps({"step": args.step, "items": count,
                              "out": str(out) if count else None}, sort_keys=True))
        elif not count:
            print(f"nothing to {args.step}: no item is waiting for {args.model}")
        else:
            print(f"{count} {args.step} item(s) for {args.model} written to {out}")
            images = sum(len(item.get("images") or []) for item in packet["items"])
            if images:
                print(f"{images} image(s) are named by path, not copied: read them "
                      "over the tunnel.")
        return EXIT_OK

    return _run(args, work)


def _save_rejected(path: Path, rejected: list[dict]) -> Path | None:
    """Beside the packet, so a reply that would not parse is never lost."""
    if not rejected:
        return None
    target = path.with_name(path.name + ".rejected.json")
    target.write_text(json.dumps(rejected, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    return target


def cmd_import(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        path = Path(os.path.expanduser(args.file))
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise paid_mod.PaidRefused(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise paid_mod.PaidRefused(f"{path} is not JSON: {exc}") from exc
        report = paid_mod.import_packet(conn, packet, ctx=_ctx(args))
        saved = _save_rejected(path, report.rejected)
        if args.json:
            payload = report.as_dict()
            payload["rejected_saved"] = str(saved) if saved else None
            print(json.dumps(payload, sort_keys=True))
            return EXIT_OK
        print(f"{len(report.recorded)} {report.step} item(s) landed from {path}"
              + (f", {report.skipped} unanswered" if report.skipped else ""))
        for line in report.recorded:
            print(f"  {line}")
        for row in report.rejected:
            print(f"  rejected {row['item']}: {row['reason']}")
        if saved:
            print(f"the rejected answers are kept, verbatim, in {saved}")
        return EXIT_OK

    return _run(args, work)


def cmd_status(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        waiting = paid_mod.waiting(conn)
        if args.json:
            print(json.dumps(waiting, sort_keys=True))
            return EXIT_OK
        for step in paid_mod.STEPS:
            row = waiting.get(step)
            if row is None:
                continue
            print(f"{step:<9} {row['summary']}")
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "paid",
        help="answer any model step off the node: export, answer, import",
        description=(
            "Branch B of DECIDE[credential-model] (spec §6): the paid model "
            "never runs on the node. `export` writes what the node would have "
            "asked, an agent on a machine with the credential answers each item "
            "with its own model, and `import` lands the answers where the local "
            "path would have. Nothing here calls a model or runs a worker."
        ),
    )
    sub = parser.add_subparsers(dest="paid_command", metavar="SUBCOMMAND")
    parser.set_defaults(func=None, _parser=parser)

    exp = sub.add_parser(
        "export",
        help="write the packet for one step",
        description=(
            "Write every item STEP owes an answer on right now — the rendered "
            "prompt, its inputs, image paths, a guard, and an empty 'answer' — "
            "as JSON. Prints one line and writes nothing when there is nothing "
            "to answer."
        ),
    )
    exp.add_argument("--step", required=True, choices=paid_mod.STEPS,
                     help="which step to answer")
    exp.add_argument("--as", dest="model", required=True, metavar="MODEL_ID",
                     help="the model that will answer, e.g. claude-opus-5")
    exp.add_argument("--out", required=True, metavar="FILE",
                     help="where to write the packet")
    exp.add_argument("--limit", type=int, default=20, metavar="N",
                     help="at most this many items (default: %(default)s)")
    _add_common(exp)
    exp.set_defaults(func=cmd_export, _parser=exp)

    imp = sub.add_parser(
        "import",
        help="land the answers a packet came back with",
        description=(
            "Read a filled-in packet (or a `judge export` claims packet) and "
            "land each answered item through the local path's own parser. An "
            "item whose guard no longer matches, or whose answer will not parse, "
            "is rejected with a reason, writes nothing, and is saved verbatim to "
            "FILE.rejected.json."
        ),
    )
    imp.add_argument("file", metavar="FILE", help="the filled-in packet")
    _add_common(imp)
    imp.set_defaults(func=cmd_import, _parser=imp)

    sta = sub.add_parser(
        "status",
        help="what is waiting for an answer from off the node",
        description="Per step, how many items an export would offer right now.",
    )
    _add_common(sta)
    sta.set_defaults(func=cmd_status, _parser=sta)
