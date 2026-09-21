"""`sketchgen paid start|next|import|…` — any step, answered off the node.

A drop-in subcommand (see sketchgen/cli/__init__.py). The work is in
sketchgen/paid.py; this is the shell around it, shaped to be driven by an agent
on a machine that holds a credential (docs/plans/agentic-cli.md §3.8).

The agent's own job is three verbs, repeated:

  start   register yourself, preflight, queue one job as you, lease it
  next    what now: the packet to answer, "wait" (run it again), done, or stop
  import  land the answered packet; the node gates it
  release hand a parked job (or the critic's entries) back to the local path

And the rest, for the per-entry steps and the operator:

  export  write what the node would have asked a model, for one step
  status  what is waiting for an answer from off the node, per step
  assign  which local model runs each step by default (paid: judge, critique)
  models  register the paid model ids this node routes off the node
  preflight  ready or not, and the fix for each thing that is not
  wait    block until a job needs you (superseded by `next`, kept)

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
from sketchgen import models
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
    return paid_mod.Context(jobs_dir=Path(os.path.expanduser(args.jobs_dir)),
                            job=getattr(args, "job", None))


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
        model = args.model or db.get_assignment(conn).get(args.step)
        if not model:
            raise paid_mod.PaidRefused(
                f"name the model that will answer with --as, or assign one to "
                f"{args.step} with `paid assign --{args.step} MODEL_ID`"
            )
        args.model = model
        packet = paid_mod.export_packet(
            conn, args.step, model=model, limit=args.limit, ctx=_ctx(args)
        )
        count = len(packet["items"])
        text = json.dumps(packet, indent=2, sort_keys=True) + "\n"
        if args.out == "-":
            # The packet itself is the output: over ssh it lands on the
            # laptop with no scp, and `paid import -` takes it back on stdin.
            if not count:
                print(f"nothing to {args.step}: no item is waiting for {model}",
                      file=sys.stderr)
                return EXIT_OK
            sys.stdout.write(text)
            return EXIT_OK
        out = Path(os.path.expanduser(args.out))
        if count:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
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
        stdin = args.file == "-"
        path = Path("stdin") if stdin else Path(os.path.expanduser(args.file))
        try:
            text = sys.stdin.read() if stdin else path.read_text(encoding="utf-8")
            packet = json.loads(text)
        except OSError as exc:
            raise paid_mod.PaidRefused(f"cannot read {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise paid_mod.PaidRefused(f"{path} is not JSON: {exc}") from exc
        report = paid_mod.import_packet(conn, packet, ctx=_ctx(args))
        # From stdin there is no file to save beside; the rejected answers go
        # back in the output instead, so they are still never lost.
        saved = None if stdin else _save_rejected(path, report.rejected)
        if args.json or stdin:
            payload = report.as_dict()
            payload["rejected_saved"] = str(saved) if saved else None
            if stdin:
                payload["rejected"] = report.rejected
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
        if report.then():
            print(f"next: {report.then()}")
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


def cmd_release(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        if args.jobs:
            results = [
                paid_mod.release_job(conn, job_id, by=args.by, reason=args.reason)
                for job_id in args.jobs
            ]
            if args.json:
                print(json.dumps({"released": results}, sort_keys=True))
            else:
                for row in results:
                    print(row["say"])
            return EXIT_OK
        if args.step != "critique":
            raise paid_mod.PaidRefused("name the jobs to hand back with --job N, or "
                                       "the critic's entries with --step critique")
        if not args.all and not args.ids:
            raise paid_mod.PaidRefused("name the ids to release, or --all")
        released = paid_mod.release(conn, args.step, None if args.all else args.ids)
        if args.json:
            print(json.dumps({"step": args.step, "released": released}))
        elif released:
            print(f"released {', '.join(map(str, released))}: the local "
                  f"{args.step} path may take them again")
        else:
            print("nothing was claimed")
        return EXIT_OK

    return _run(args, work)


def cmd_models(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        names = db.get_paid_models(conn)
        for name in args.names:
            if args.action == "add":
                paid_mod._check_model(name)
                if ":" in name:
                    raise paid_mod.PaidRefused(
                        f"{name!r} looks like an Ollama tag (name:tag); a paid "
                        "model id has no tag, or the node could not tell them apart"
                    )
        if args.action == "add":
            names = db.set_paid_models(conn, names + list(args.names))
        elif args.action == "remove":
            names = db.set_paid_models(conn, [n for n in names if n not in args.names])
        from_env = [n for n in models.paid_models() if n not in names]
        if args.json:
            print(json.dumps({"registered": names, "from_environment": from_env}))
            return EXIT_OK
        print("registered: " + (", ".join(names) or "(none)"))
        if from_env:
            print(f"from {models.PAID_MODELS_ENV} in this shell: " + ", ".join(from_env))
        return EXIT_OK

    return _run(args, work)


def cmd_preflight(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        result = paid_mod.preflight(conn, args.model)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print("READY" if result["ready"] else "NOT READY")
            for row in result["checks"]:
                mark = "ok  " if row["ok"] else "FAIL"
                print(f"  {mark} {row['check']:<11} {row['detail']}")
                if not row["ok"]:
                    print(f"       fix ({row['who']}): {row['fix']}")
            _print_info(result["info"])
            if not result["ready"]:
                print("Fix what is yours; report what is the operator's, and stop.")
        return EXIT_OK if result["ready"] else EXIT_REFUSED

    return _run(args, work)


def _print_info(info: dict) -> None:
    """The lines under a preflight, text mode: what the agent is walking into."""
    if not info:
        return
    print(f"  jobs queued ahead: {info.get('queued_ahead', 0)}")
    now = info.get("worker_now")
    if now:
        where = now.get("headline") or now.get("step")
        on = f" on job {now['job']}" if now.get("job") else ""
        print(f"  worker now: {where}{on} (since {now.get('since_utc')})")
    for lease_job, lease in sorted((info.get("leases") or {}).items()):
        print(f"  lease: job {lease_job} is {lease.get('model')}'s until "
              f"{lease.get('until_utc')}")
    for row in info.get("parked") or []:
        whose = "yours" if row["yours"] else f"{row['model']}'s, no agent"
        print(f"  parked: job {row['job']} needs {row['needs']} ({whose}, since "
              f"{row['since_utc']}) -> {row['command']}")
    if info.get("assignment"):
        print("  assignment: " + ", ".join(
            f"{k}={v}" for k, v in sorted(info["assignment"].items())))


def cmd_start(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        prompt = args.prompt
        if prompt is None or prompt == "-":
            prompt = sys.stdin.read()
        result = paid_mod.start(
            conn, model=args.model, prompt=prompt, by=args.by,
            planner=args.planner, executor=args.executor, rules_file=args.rules,
            max_attempts=args.max_attempts, publication=args.publication,
        )
        if args.json:
            print(json.dumps(result, sort_keys=True))
        elif not result["started"]:
            report = result["preflight"]
            print("NOT READY — nothing queued")
            for row in report["checks"]:
                mark = "ok  " if row["ok"] else "FAIL"
                print(f"  {mark} {row['check']:<11} {row['detail']}")
                if not row["ok"]:
                    print(f"       fix ({row['who']}): {row['fix']}")
            _print_info(report["info"])
            print("Report the failing checks to the operator, verbatim, and stop.")
        else:
            if result["registered_now"]:
                print(f"registered {result['model']} as a paid model (a name, not a key)")
            print(f"started job {result['job']}: planner {result['planner']}, "
                  f"executor {result['executor']}, leased to {result['model']} "
                  f"until {result['lease_until']}")
            _print_info({"queued_ahead": result["queued_ahead"],
                         "worker_now": result["worker_now"]})
            print(f"next: {result['then']}")
        return EXIT_OK if result["started"] else EXIT_REFUSED

    return _run(args, work)


def cmd_next(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        result = paid_mod.next_for(
            conn, args.job, args.model, timeout=args.timeout, interval=args.interval,
            ctx=_ctx(args), progress=lambda line: print(line, file=sys.stderr),
        )
        # Always one JSON object on stdout: when `do` is `answer` it is the
        # packet itself, to be answered and given to `paid import -`.
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return EXIT_REFUSED if result["do"] == "stop" else EXIT_OK

    return _run(args, work)


def cmd_wait(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        result = paid_mod.wait_for(conn, args.job, timeout=args.timeout,
                                   interval=args.interval)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            print(result["say"])
            if result.get("command"):
                print(f"next: {result['command']}")
        if result["timed_out"]:
            return EXIT_FAIL
        return EXIT_REFUSED if result["do"] == "stop" else EXIT_OK

    return _run(args, work)


def _assign_changes(args: argparse.Namespace) -> dict[str, str | None]:
    changes: dict[str, str | None] = {}
    if args.all:
        changes = {step: args.all for step in db.ASSIGNABLE_STEPS}
    for step in db.ASSIGNABLE_STEPS:
        value = getattr(args, step)
        if value is not None:
            changes[step] = value
    cleaned: dict[str, str | None] = {}
    for step, value in changes.items():
        value = value.strip()
        if value in ("", "local"):
            cleaned[step] = None
            continue
        if not paid_mod.MODEL_RE.match(value):
            raise paid_mod.PaidRefused(f"{value!r} is not a usable model id")
        if step in paid_mod.JOB_STEPS and (value == "paid" or ":" not in value):
            # A default paid planner or executor makes paid jobs with no agent
            # attached — from the New job page, and from every child the idle
            # critic spawns. 2026-09-21: two of those sat at needs-laptop for
            # hours. A paid job is started per job, by its agent (`paid start`).
            raise paid_mod.PaidRefused(
                f"--{step} {value}: only an Ollama tag (name:tag) or `local` can "
                f"be the default for {step}. A paid model runs a job it was "
                "started for: `sketchgen paid start --as MODEL`."
            )
        cleaned[step] = value
    return cleaned


def cmd_assign(args: argparse.Namespace) -> int:
    def work(conn: sqlite3.Connection) -> int:
        changes = _assign_changes(args)
        assignment = (db.set_assignment(conn, changes) if changes
                      else db.get_assignment(conn))
        paid_names = set(models.paid_models(conn))
        notes = []
        for step, model in sorted(assignment.items()):
            if model != "paid" and model not in paid_names and ":" not in model:
                notes.append(
                    f"{model} has no Ollama tag and is not in "
                    f"{models.PAID_MODELS_ENV} as this shell sees it: the worker "
                    "parks it for the laptop only if its unit names it, and "
                    "otherwise sends it to Ollama"
                )
        if models.is_paid(assignment.get("execute"), conn) or (
            assignment.get("execute") and ":" not in assignment["execute"]
        ):
            notes.append(
                "a paid executor is a second variable in the A/B the gallery "
                "runs on the rules file, and a much larger one than two local "
                "models; every entry it writes is badged off-node"
            )
        if args.json:
            print(json.dumps({"assignment": assignment, "notes": notes},
                             sort_keys=True))
            return EXIT_OK
        for step in db.ASSIGNABLE_STEPS:
            print(f"{step:<9} {assignment.get(step) or '(this node: the worker default)'}")
        for note in dict.fromkeys(notes):
            print(f"note: {note}")
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
    exp.add_argument("--as", dest="model", default=None, metavar="MODEL_ID",
                     help="the model that will answer, e.g. claude-opus-5 "
                          "(default: the step's assignment, `paid assign`)")
    exp.add_argument("--out", required=True, metavar="FILE",
                     help="where to write the packet; - writes it to stdout")
    exp.add_argument("--job", type=int, default=None, metavar="N",
                     help="plan and execute: offer only job N (your own)")
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
    imp.add_argument("file", metavar="FILE",
                     help="the filled-in packet; - reads it from stdin")
    _add_common(imp)
    imp.set_defaults(func=cmd_import, _parser=imp)

    sta = sub.add_parser(
        "status",
        help="what is waiting for an answer from off the node",
        description="Per step, how many items an export would offer right now.",
    )
    _add_common(sta)
    sta.set_defaults(func=cmd_status, _parser=sta)

    rel = sub.add_parser(
        "release",
        help="hand exported subjects back to the local path",
        description=(
            "An entry exported to a paid critic is assigned to it: the idle "
            "loop's local critic skips it until its answer is imported. This "
            "gives entries back without an answer — a packet that is never "
            "coming home. Jobs need no release: a parked job is failed or "
            "answered, not reclaimed."
        ),
    )
    rel.add_argument("--job", dest="jobs", action="append", type=int, default=[],
                     metavar="N",
                     help="hand this parked job (needs-laptop, plan or execute) back "
                          "to this node's models: its paid planner/executor is "
                          "blanked and it is queued again. Repeatable")
    rel.add_argument("--by", default=None, metavar="WHO",
                     help="who is handing it back (your model id, or a username)")
    rel.add_argument("--reason", default=None, metavar="TEXT",
                     help="why, recorded on the job")
    rel.add_argument("--step", default=None, choices=("critique",),
                     help="the step whose claims to release (entries)")
    rel.add_argument("ids", nargs="*", type=int, metavar="ID",
                     help="entry ids to release")
    rel.add_argument("--all", action="store_true", help="release every claim")
    _add_common(rel)
    rel.set_defaults(func=cmd_release, _parser=rel)

    mod = sub.add_parser(
        "models",
        help="the paid model ids this node routes off the node",
        description=(
            "Register the ids of models that answer off the node. Names only, "
            "never a key. Every process reads this list from the database, so "
            "a name added here routes at once — no unit file, no restart. "
            "SKETCHGEN_PAID_MODELS in a unit's environment still counts too."
        ),
    )
    mod.add_argument("action", nargs="?", choices=("list", "add", "remove"),
                     default="list")
    mod.add_argument("names", nargs="*", metavar="MODEL_ID")
    _add_common(mod)
    mod.set_defaults(func=cmd_models, _parser=mod)

    pre = sub.add_parser(
        "preflight",
        help="ready or not ready to run a job as MODEL, and why",
        description=(
            "Every check the paid flow depends on — schema, the model's "
            "registration, one running worker, the generator running — each "
            "with the command that fixes it and whose command that is. Exit 0 "
            "ready, 3 not ready. Run it first; if it is not ready, fix what is "
            "yours and report the rest."
        ),
    )
    pre.add_argument("--as", dest="model", required=True, metavar="MODEL_ID",
                     help="your own exact model id")
    _add_common(pre)
    pre.set_defaults(func=cmd_preflight, _parser=pre)

    wai = sub.add_parser(
        "wait",
        help="block until job N needs you, is finished, or cannot move",
        description=(
            "Read-only. Returns when the job is parked for a step you answer "
            "(and prints the export command), when it is held/published/failed, "
            "when it needs a person, or when the generator is paused. Exit 0, "
            "1 on timeout, 3 when there is nothing for you to do but report."
        ),
    )
    wai.add_argument("--job", type=int, required=True, metavar="N")
    wai.add_argument("--timeout", type=float, default=240.0, metavar="S",
                     help="give up after S seconds (default %(default)s; an agent's "
                          "tool call allows less than it used to)")
    wai.add_argument("--interval", type=float, default=5.0, metavar="S",
                     help="poll every S seconds (default %(default)s)")
    _add_common(wai)
    wai.set_defaults(func=cmd_wait, _parser=wai)

    sta_ = sub.add_parser(
        "start",
        help="begin one job as MODEL: register, preflight, queue, lease",
        description=(
            "The first of the agent's three verbs. Registers MODEL as a paid "
            "model if it is not (a name, never a key), runs the preflight and "
            "refuses — exit 3, nothing queued — if it is not ready, queues one "
            "job with MODEL as planner and executor (or `local` for either), "
            "and leases the job to MODEL so the worker takes it first and does "
            "no idle work while MODEL is driving it. Prints the `next` command."
        ),
    )
    sta_.add_argument("--as", dest="model", required=True, metavar="MODEL_ID",
                      help="your own exact model id")
    sta_.add_argument("--by", required=True, metavar="USERNAME",
                      help="GitHub username the job is submitted under")
    sta_.add_argument("--prompt", default=None, metavar="TEXT",
                      help="what to make; omitted or `-`: read from stdin, which "
                           "needs no quoting through ssh")
    sta_.add_argument("--planner", default=None, metavar="MODEL",
                      help="yourself (default), `local`, or an Ollama tag")
    sta_.add_argument("--executor", default=None, metavar="MODEL",
                      help="yourself (default), `local`, or an Ollama tag")
    sta_.add_argument("--rules", choices=("control", "treatment", "random"),
                      default=None, help="rules file for the executor")
    sta_.add_argument("--max-attempts", dest="max_attempts", type=int, default=3,
                      metavar="N", help="execute+gate attempts (default 3)")
    sta_.add_argument("--publication", choices=("hold", "auto"), default="hold")
    _add_common(sta_)
    sta_.set_defaults(func=cmd_start, _parser=sta_)

    nxt = sub.add_parser(
        "next",
        help="what to do next about job N: the packet, wait, done, or stop",
        description=(
            "The agent's loop, as one verb. Prints one JSON object with `do`: "
            "`answer` — the object is the packet for the step the job is parked "
            "at; write your reply into items[0].answer and `paid import -` it. "
            "`wait` — the worker has it; `worker` says what it is doing; run "
            "this again (exit 0). `done` — held, failed, published. `stop` — "
            "a person is needed, the generator is paused, or another agent "
            "holds the job (exit 3): report and stop. Renews your lease on "
            "every poll. Returns within --timeout, which is shorter than a "
            "tool call on purpose."
        ),
    )
    nxt.add_argument("--job", type=int, required=True, metavar="N")
    nxt.add_argument("--as", dest="model", required=True, metavar="MODEL_ID",
                     help="your own exact model id")
    nxt.add_argument("--timeout", type=float, default=240.0, metavar="S",
                     help="return `wait` after S seconds (default %(default)s)")
    nxt.add_argument("--interval", type=float, default=5.0, metavar="S",
                     help="poll every S seconds (default %(default)s)")
    _add_common(nxt)
    nxt.set_defaults(func=cmd_next, _parser=nxt)

    asg = sub.add_parser(
        "assign",
        help="which model runs each step by default",
        description=(
            "One setting for the four steps (agentic-cli §3.6). With no options, "
            "print it. `plan` and `execute` are what the New job page preselects "
            "and what a job that names no model gets — local tags only: a paid "
            "job is started per job by `paid start`. `judge` and `critique` are "
            "what the idle loop runs, and a paid model there means the idle loop "
            "leaves that step to `paid export`. A job's own planner and executor "
            "always win. `local` unsets a step."
        ),
    )
    asg.add_argument("--all", metavar="MODEL_ID",
                     help="assign every step to this model at once")
    for step in db.ASSIGNABLE_STEPS:
        asg.add_argument(f"--{step}", metavar="MODEL_ID", default=None,
                         help=f"the model for {step}, or `local` to unset it")
    _add_common(asg)
    asg.set_defaults(func=cmd_assign, _parser=asg)
