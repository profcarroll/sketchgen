"""`sketchgen judge run|one|export|import|status` — the agent judges (packet 5.2).

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls ``register(top)``, so the packet adds one file and edits
nothing shared. The work is in sketchgen/judge.py; this is the shell around it.

  run     the local judge (``gemma4:e4b``) over up to N pairs, slot fenced
  one     one pair, named on the command line, for looking at a single verdict
  export  the claims packet the paid judge answers on the laptop (spec §6)
  import  the answers that packet comes back with
  status  verdicts per model, and the agreement with the humans

Exit codes, as everywhere in this project: 0 success, 1 failure (a malformed
reply, with the raw text printed so it is not lost), 3 refused (no database, an
unpublished entry, a busy inference slot, a prompt that would break the blind).

**The first real call is ASK-FIRST.** Without ``--stub`` and without an
explicit ``--host``, ``run`` and ``one`` talk to the model on the node. The
fence is checked first either way.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import judge as judge_mod  # noqa: E402
from sketchgen import pairs as pairs_mod  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3


class Refusal(Exception):
    """Something this command will not do; exit 3, nothing written."""


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def _add_db(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )


def _add_model(parser: argparse.ArgumentParser, *, required: bool = False) -> None:
    parser.add_argument(
        "--model",
        default=None if required else judge_mod.DEFAULT_MODEL,
        required=required,
        metavar="M",
        help=(
            "the local judge's model tag (default: %s). It has to be a model "
            "that can see; a text-only model is answering a different question "
            "from the humans." % judge_mod.DEFAULT_MODEL
        ),
    )
    parser.add_argument(
        "--host",
        default=judge_mod.DEFAULT_HOST,
        metavar="URL",
        help="Ollama host (default: %(default)s)",
    )
    parser.add_argument(
        "--stub",
        default=None,
        metavar="FILE",
        help="replay a saved reply from FILE and call no model",
    )


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    path = Path(os.path.expanduser(args.db))
    if not path.is_file():
        raise Refusal(f"no database at {path}: run `sketchgen db init` first")
    conn = db.connect(path)
    if db.schema_version(conn) == 0:
        conn.close()
        raise Refusal(f"{path} has no schema: run `sketchgen db init` first")
    return conn


def _run(args: argparse.Namespace, work) -> int:
    try:
        conn = _open(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return work(conn)
    except (Refusal, judge_mod.JudgeRefused) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except judge_mod.JudgeFailed as exc:
        # The raw reply is the evidence; it is never swallowed.
        print(f"failed: {exc}", file=sys.stderr)
        if exc.raw:
            print("--- the model said ---", file=sys.stderr)
            print(exc.raw, file=sys.stderr)
        return EXIT_FAIL
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()


def _published_or_refuse(conn: sqlite3.Connection, *entry_ids: int) -> None:
    known = {
        int(row["id"]): str(row["state"])
        for row in conn.execute("SELECT id, state FROM entries")
    }
    for entry_id in entry_ids:
        if entry_id not in known:
            raise Refusal(f"no entry {entry_id}")
        if known[entry_id] != "published":
            raise Refusal(
                f"entry {entry_id} is {known[entry_id]}, not published; only "
                "published entries are judged"
            )


# ---------------------------------------------------------------------------
# judge run
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        seed = args.seed if args.seed is not None else 0
        counts = judge_mod.run_local(
            conn,
            model=args.model,
            host=args.host,
            limit=args.limit,
            rng=random.Random(seed),
            stub=args.stub,
            log=None if args.json else (lambda line: print(line)),
        )
        if args.json:
            print(json.dumps({**counts, "model": args.model, "seed": seed},
                             sort_keys=True))
        elif not counts["judged"]:
            print("no pairs to judge: two published entries are the minimum, and "
                  f"{args.model} may already have answered every pair")
        else:
            print(
                f"{counts['judged']} pair(s) judged, {counts['recorded']} answers "
                f"recorded as {args.model}"
            )
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# judge one
# ---------------------------------------------------------------------------


def cmd_one(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        if args.a == args.b:
            raise Refusal("a pair needs two different entries")
        _published_or_refuse(conn, args.a, args.b)
        verdict = judge_mod.judge_local(
            conn,
            args.a,
            args.b,
            model=args.model,
            host=args.host,
            stub=args.stub,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "entry_a": verdict.entry_a,
                        "entry_b": verdict.entry_b,
                        "brief": verdict.brief,
                        "look": verdict.look,
                        "reasons": verdict.reasons,
                        "judge_id": verdict.model,
                        "prompt_version": verdict.prompt_version,
                        "artefact_hash": verdict.artefact_hash,
                        "tokens": verdict.tokens,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"A: entry {verdict.entry_a}   B: entry {verdict.entry_b}")
            print(f"brief: {verdict.brief}")
            print(f"look:  {verdict.look}")
            for question in judge_mod.QUESTIONS:
                if verdict.reasons.get(question):
                    print(f"  {question}: {verdict.reasons[question]}")
            print(f"judge: {verdict.model} ({verdict.prompt_version})")
            print(f"artefact_hash: {verdict.artefact_hash}")
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# judge export / import — the paid judge, branch B
# ---------------------------------------------------------------------------


def cmd_export(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        packet = judge_mod.claims_packet(
            conn, judge_id=args.judge_id, limit=args.limit
        )
        if not packet["claims"]:
            print(f"no pairs: {args.judge_id} owes a verdict on nothing right now")
            return EXIT_OK
        out = Path(os.path.expanduser(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        print(
            f"{len(packet['claims'])} claim(s) for {args.judge_id} written to {out}"
        )
        print(
            "The images are not in the packet: each claim names the strip paths, "
            "and the laptop reads them over the tunnel."
        )
        return EXIT_OK

    return _run(args, work)


def cmd_import(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        path = Path(os.path.expanduser(args.file))
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise Refusal(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise Refusal(f"{path} is not JSON: {exc}") from exc
        recorded, rejected = judge_mod.import_verdicts_detailed(conn, packet)
        if args.json:
            print(json.dumps({"recorded": recorded, "rejected": rejected},
                             sort_keys=True))
            return EXIT_OK
        print(f"{recorded} pair(s) recorded from {path}")
        for row in rejected:
            print(f"  rejected {row['pair']}: {row['reason']}")
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# judge status
# ---------------------------------------------------------------------------


def _verdict_counts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT judge_id, prompt_version, question, COUNT(*) AS n "
        "FROM judgments WHERE judge_kind = 'agent' "
        "GROUP BY judge_id, prompt_version, question "
        "ORDER BY judge_id, prompt_version, question"
    )
    by_judge: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (str(row["judge_id"]), str(row["prompt_version"] or ""))
        slot = by_judge.setdefault(
            key,
            {
                "judge_id": key[0],
                "prompt_version": key[1],
                **{question: 0 for question in pairs_mod.QUESTIONS},
                "total": 0,
            },
        )
        question = str(row["question"])
        if question in slot:
            slot[question] = int(row["n"])
        slot["total"] += int(row["n"])
    return list(by_judge.values())


def cmd_status(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        verdicts = _verdict_counts(conn)
        agreement = pairs_mod.agreement(conn)
        if args.json:
            print(json.dumps({"verdicts": verdicts, "agreement": agreement},
                             sort_keys=True))
            return EXIT_OK
        if not verdicts:
            print("no agent verdicts yet")
        for row in verdicts:
            questions = "  ".join(
                f"{question}={row[question]}" for question in pairs_mod.QUESTIONS
            )
            version = row["prompt_version"] or "(no prompt version)"
            print(f"{row['judge_id']}  {version}  {questions}  total={row['total']}")
        print()
        for name, row in agreement.items():
            fraction = (
                "—" if row["agreement"] is None else f"{float(row['agreement']):.2f}"
            )
            print(f"agreement {name:<8} {fraction}  ({row['agree']}/{row['n']} pairs)")
        print(
            "Pairs answered by both populations only. Two populations, two "
            "scores, never one aggregate (spec §5)."
        )
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "judge",
        help="the agent judges: the local one that looks, the paid one that claims",
        description=(
            "Agent verdicts on the same pairs, under the same two questions, "
            "the humans answer (spec §5). The judge is blind: no human vote, no "
            "engagement tally, no producing model, no submitter, no rules file, "
            "no entry ids — enforced on every rendered prompt before it is sent. "
            "The local judge is a model that can see, because a judge reading "
            "source is answering a different question."
        ),
    )
    sub = parser.add_subparsers(dest="judge_command", metavar="SUBCOMMAND")
    parser.set_defaults(func=None, _parser=parser)

    run = sub.add_parser(
        "run",
        help="judge up to N balanced pairs with the local model",
        description=(
            "Fence the inference slot, then judge up to --limit pairs picked by "
            "the same balance rule the humans' pairs use. Refuses (exit 3) while "
            "another client holds the slot. THE FIRST REAL RUN IS ASK-FIRST: "
            "with --stub nothing is called."
        ),
    )
    _add_model(run)
    run.add_argument("--limit", type=int, default=1, metavar="N",
                     help="how many pairs to judge (default: %(default)s)")
    run.add_argument("--seed", type=int, default=None, metavar="S",
                     help="rng seed for pair selection; same seed, same pairs")
    run.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(run)
    run.set_defaults(func=cmd_run, _parser=run)

    one = sub.add_parser(
        "one",
        help="judge one named pair",
        description=(
            "Ask the local judge about one pair, named on the command line. "
            "Refuses (exit 3) an unknown or unpublished entry; exits 1 on a "
            "reply that is not the two-line contract, with the raw text printed."
        ),
    )
    one.add_argument("--a", type=int, required=True, metavar="ID",
                     help="entry shown as A (the first image)")
    one.add_argument("--b", type=int, required=True, metavar="ID",
                     help="entry shown as B (the second image)")
    _add_model(one, required=True)
    one.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(one)
    one.set_defaults(func=cmd_one, _parser=one)

    exp = sub.add_parser(
        "export",
        help="write the claims packet the paid judge answers on the laptop",
        description=(
            "Branch B of DECIDE[credential-model] (spec §6): no paid credential "
            "on the node. This writes the pairs that judge still owes — the two "
            "briefs, the two strip paths, the artefact hash, the prompt — as "
            "JSON with the answers left blank. Prints one line and exits 0 when "
            "there is nothing to claim."
        ),
    )
    exp.add_argument("--as", dest="judge_id", required=True, metavar="MODEL_ID",
                     help="the judge the claims are cut for: a model id")
    exp.add_argument("--out", required=True, metavar="FILE",
                     help="where to write the packet")
    exp.add_argument("--limit", type=int, default=20, metavar="N",
                     help="at most this many claims (default: %(default)s)")
    _add_db(exp)
    exp.set_defaults(func=cmd_export, _parser=exp)

    imp = sub.add_parser(
        "import",
        help="record the answers a claims packet came back with",
        description=(
            "Read a packet the laptop filled in and record each answered claim "
            "as an agent verdict. A claim whose artefact_hash no longer matches "
            "the entries as they stand is rejected and nothing is written for "
            "it: a verdict is only readable against what the judge saw."
        ),
    )
    imp.add_argument("file", metavar="FILE", help="the filled-in packet")
    imp.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(imp)
    imp.set_defaults(func=cmd_import, _parser=imp)

    sta = sub.add_parser(
        "status",
        help="agent verdict counts per model, and agreement with the humans",
        description=(
            "How many verdicts each agent judge has given, under which prompt "
            "version, and how often the two populations reached the same verdict "
            "on pairs both answered (MEASURE[agent-human-correlation])."
        ),
    )
    sta.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(sta)
    sta.set_defaults(func=cmd_status, _parser=sta)
