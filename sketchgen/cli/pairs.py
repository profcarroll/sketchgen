"""`sketchgen pairs next|record|scores|agreement` — paired comparison (packet 5.1).

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls ``register(top)``, so the packet adds one file and edits
nothing shared. The work is in sketchgen/pairs.py; this is the shell around it.

Exit codes, as everywhere in this project: 0 success, 1 failure (no database,
no schema, a SQL error), 3 refused (an argument this command will not act on —
an unknown entry, an entry that is not published, a judge id that is not a
GitHub username).

**No personal data.** A human judge id is a GitHub username and nothing else
(course policy, spec §7); `--judge` is checked against the same login pattern
the write path uses before anything is written. An agent judge id is a model id.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sketchgen import db  # noqa: E402
from sketchgen import pairs as pairs_mod  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3

#: GitHub logins: alphanumerics and single hyphens, 39 characters at most. The
#: same expression sync.py uses, for the same reason.
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")

#: A model id, for an agent judge: the shape ollama and the paid APIs use.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")


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


def _open(args: argparse.Namespace) -> sqlite3.Connection:
    path = Path(os.path.expanduser(args.db))
    if not path.is_file():
        raise Refusal(f"no database at {path}: run `sketchgen db init` first")
    conn = db.connect(path)
    if db.schema_version(conn) == 0:
        conn.close()
        raise Refusal(f"{path} has no schema: run `sketchgen db init` first")
    return conn


def _check_judge(judge_id: str, kind: str) -> str:
    judge_id = judge_id.strip()
    if kind == "human":
        if not LOGIN_RE.match(judge_id):
            raise Refusal(
                f"{judge_id!r} is not a GitHub username. A human judge id is a "
                "GitHub username and nothing else — no emails, no real names."
            )
    elif not MODEL_RE.match(judge_id):
        raise Refusal(f"{judge_id!r} is not a usable model id for an agent judge")
    return judge_id


def _run(args: argparse.Namespace, work) -> int:
    try:
        conn = _open(args)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return work(conn)
    except Refusal as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# pairs next
# ---------------------------------------------------------------------------


def cmd_next(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        judge_id = _check_judge(args.judge, args.kind)
        seed = args.seed if args.seed is not None else 0
        pair = pairs_mod.pick_pair(
            conn,
            judge_id=judge_id,
            judge_kind=args.kind,
            exclude_seen=not args.repeat,
            rng=random.Random(seed),
        )
        if pair is None:
            published = len(
                list(conn.execute("SELECT id FROM entries WHERE state = 'published'"))
            )
            if published < 2:
                why = (
                    f"only {published} published "
                    f"{'entry' if published == 1 else 'entries'}; a pair needs two."
                )
            else:
                why = f"{judge_id} has already answered both questions on every pair."
            # stdout stays parseable; the explanation goes to stderr.
            print("null" if args.json else "no pair")
            print(f"sketchgen: no pair to offer: {why}", file=sys.stderr)
            return EXIT_OK
        entry_a, entry_b = pair
        if args.json:
            print(
                json.dumps(
                    {
                        "entry_a": entry_a,
                        "entry_b": entry_b,
                        "judge_id": judge_id,
                        "judge_kind": args.kind,
                        "seed": seed,
                        "artefact_hash": pairs_mod.artefact_hash(
                            entry_a, entry_b, conn=conn
                        ),
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"A: entry {entry_a}")
            print(f"B: entry {entry_b}")
            print(f"artefact_hash: {pairs_mod.artefact_hash(entry_a, entry_b, conn=conn)}")
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# pairs record
# ---------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        judge_id = _check_judge(args.judge, args.kind)
        if args.a == args.b:
            raise Refusal("a pair needs two different entries")
        known = {
            int(row["id"]): str(row["state"])
            for row in conn.execute("SELECT id, state FROM entries")
        }
        for entry_id in (args.a, args.b):
            if entry_id not in known:
                raise Refusal(f"no entry {entry_id}")
            if known[entry_id] != "published":
                raise Refusal(
                    f"entry {entry_id} is {known[entry_id]}, not published; only "
                    "published entries are judged"
                )
        judgment_id = pairs_mod.record(
            conn,
            entry_a=args.a,
            entry_b=args.b,
            judge_kind=args.kind,
            judge_id=judge_id,
            question=args.question,
            choice=args.choice,
            prompt_version=args.prompt_version,
            artefact_hash=pairs_mod.artefact_hash(args.a, args.b, conn=conn),
        )
        print(
            f"recorded judgment {judgment_id}: {judge_id} ({args.kind}) on "
            f"{args.a} vs {args.b}, {args.question} -> {args.choice}"
        )
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# pairs scores
# ---------------------------------------------------------------------------


def _score_table(conn: sqlite3.Connection, population: str, question: str) -> dict:
    return {
        str(entry_id): row
        for entry_id, row in pairs_mod.scores(
            conn, population=population, question=question
        ).items()
    }


def cmd_scores(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        populations = (
            [args.population] if args.population else list(pairs_mod.POPULATIONS)
        )
        questions = [args.question] if args.question else list(pairs_mod.QUESTIONS)
        table = {
            population: {
                question: _score_table(conn, population, question)
                for question in questions
            }
            for population in populations
        }
        if args.json:
            print(json.dumps(table, sort_keys=True))
            return EXIT_OK
        for population in populations:
            for question in questions:
                rows = table[population][question]
                print(f"{population} · {question}")
                if not rows:
                    print("  no pairs yet")
                    continue
                for entry_id, row in sorted(
                    rows.items(), key=lambda item: -float(item[1]["score"])
                ):
                    print(
                        f"  entry {entry_id:>6}  {row['score']:8.2f}  "
                        f"n={row['n']:<4} w={row['wins']} l={row['losses']} "
                        f"t={row['ties']}"
                    )
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# pairs agreement
# ---------------------------------------------------------------------------


def cmd_agreement(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        result = pairs_mod.agreement(conn)
        if args.json:
            print(json.dumps(result, sort_keys=True))
            return EXIT_OK
        for name, row in result.items():
            fraction = (
                "—" if row["agreement"] is None else f"{float(row['agreement']):.2f}"
            )
            print(f"{name:<8} {fraction}  ({row['agree']}/{row['n']} pairs)")
        print(
            "Pairs answered by both populations only. MEASURE"
            "[agent-human-correlation]."
        )
        return EXIT_OK

    return _run(args, work)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "pairs",
        help="offer pairs, record answers, fit Bradley–Terry per population",
        description=(
            "Paired comparison (spec §5): two entries, the same two questions "
            "for humans and for agents, a Bradley–Terry score per population "
            "and never one aggregate. Views and likes are engagement, not "
            "judgment, and are not read here."
        ),
    )
    sub = parser.add_subparsers(dest="pairs_command", metavar="SUBCOMMAND")
    parser.set_defaults(func=None, _parser=parser)

    nxt = sub.add_parser(
        "next",
        help="offer one balanced pair to a judge",
        description=(
            "Pick the pair this judge should see next: fewest judgments so far, "
            "one entry from each rules file where both arms exist, never a pair "
            "this judge has already answered both questions for. Prints nothing "
            "but a pair (or null) on stdout; the reason for a null goes to "
            "stderr, and the exit code stays 0."
        ),
    )
    nxt.add_argument(
        "--for", dest="judge", required=True, metavar="USERNAME",
        help="the judge: a GitHub username for --kind human, a model id for agent",
    )
    nxt.add_argument(
        "--kind", choices=list(pairs_mod.POPULATIONS), default="human",
        help="judge population (default: human)",
    )
    nxt.add_argument(
        "--seed", type=int, default=None, metavar="N",
        help="rng seed; the same seed against the same database gives the same pair",
    )
    nxt.add_argument(
        "--repeat", action="store_true",
        help="allow a pair this judge has already answered (default: do not)",
    )
    nxt.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(nxt)
    nxt.set_defaults(func=cmd_next, _parser=nxt)

    rec = sub.add_parser(
        "record",
        help="record one answer to one question about one pair",
        description=(
            "Store one judgment, replacing this judge's earlier answer to the "
            "same question about the same pair. Refuses (exit 3) an unknown or "
            "unpublished entry, and a human judge id that is not a GitHub "
            "username."
        ),
    )
    rec.add_argument("--a", type=int, required=True, metavar="ID", help="entry shown as A")
    rec.add_argument("--b", type=int, required=True, metavar="ID", help="entry shown as B")
    rec.add_argument(
        "--kind", choices=list(pairs_mod.POPULATIONS), required=True,
        help="judge population",
    )
    rec.add_argument(
        "--judge", required=True, metavar="J",
        help="GitHub username (human) or model id (agent)",
    )
    rec.add_argument(
        "--question", choices=list(pairs_mod.QUESTIONS), required=True,
        help="brief: closer to its brief. look: which you would rather look at",
    )
    rec.add_argument(
        "--choice", choices=list(pairs_mod.CHOICES), required=True,
        help="A, B or tie, relative to the order given by --a and --b",
    )
    rec.add_argument(
        "--prompt-version", default=None, metavar="V",
        help="the version of the prompt the judge answered under",
    )
    _add_db(rec)
    rec.set_defaults(func=cmd_record, _parser=rec)

    sco = sub.add_parser(
        "scores",
        help="Bradley–Terry scores, per population and question",
        description=(
            "Fit Bradley–Terry (Hunter 2004's MM iteration, stdlib floats) over "
            "the recorded pairs. Every entry carries one virtual tie against a "
            "reference of strength 1.00, so an undefeated entry stays finite and "
            "the scale is anchored; the cost is shrinkage toward 1.00 where "
            "there are few pairs, so read the ordering before the margin. An "
            "entry with no judgments in a population is absent, not zero."
        ),
    )
    sco.add_argument(
        "--population", choices=list(pairs_mod.POPULATIONS), default=None,
        help="one population (default: both, kept apart)",
    )
    sco.add_argument(
        "--question", choices=list(pairs_mod.QUESTIONS), default=None,
        help="one question (default: both)",
    )
    sco.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(sco)
    sco.set_defaults(func=cmd_scores, _parser=sco)

    agr = sub.add_parser(
        "agreement",
        help="how often humans and agents reach the same verdict",
        description=(
            "Over pairs answered by both populations, the fraction where the two "
            "majority verdicts match, per question and overall. This is "
            "MEASURE[agent-human-correlation]'s first number; the spec expects "
            "divergence, and the divergence is the dataset."
        ),
    )
    agr.add_argument("--json", action="store_true", help="machine-readable output")
    _add_db(agr)
    agr.set_defaults(func=cmd_agreement, _parser=agr)
