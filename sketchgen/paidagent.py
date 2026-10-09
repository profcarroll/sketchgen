"""A box with a model as a paid planner on a node's queue.

The handheld can run the whole pipeline (``rocknix/``), and the first batch
there showed the cost of its one remote step: the executor's call to
sld-cloud's Ollama competed with that node's own idle judge for sixteen cores
and crawled at 1.1 tokens a second (job 1, 2026-10-09 04:31–04:41Z). The
operator named the better shape the same hour: use the *paid* path. A paid
job's lease stops the node's idle work while the agent is driving, and the
executor runs inside the node's own worker and slot fence rather than as a
stranger's HTTP call; the node gates and holds the entry itself; nothing in
``sketchgen`` changes. What the handheld needs is an agent, and until now the
only agents were people and Claude sessions reading AGENTS.md.

This is that agent. It runs the paid verbs on the node over ssh, the way
``bin/sg`` does from the laptop, and answers each plan packet with a model
behind Ollama's API on this box (the shim, or an Ollama), filling ``usage``
from the reply's own counts and ``process`` with what it can honestly say
(the wall time, the tokens, that no tools and no screenshots were used). The
loop is the one AGENTS.md prescribes, verbatim: ``start`` once, ``next`` →
answer → ``import`` until ``next`` says ``done``, and on ``stop`` it reports
what the node said and stops. A rejected answer (the plan did not parse) is
retried with a new seed up to :data:`MAX_REJECTIONS` times, then the job is
released to the node's own planner rather than left parked.

The model id given with ``--as`` is the provenance every record carries and
must be the name the local Ollama API serves; the shim answers any other name
with a 404, which comes back here as a failed plan, loudly.

Python 3.12, stdlib only. Every timestamp is UTC, ISO 8601, with a Z.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

__all__ = [
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "MAX_REJECTIONS",
    "MAX_TURNS",
    "Model",
    "Node",
    "Outcome",
    "drive",
    "main",
    "process_of",
    "utc_now",
]

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: A plan the node cannot parse is answered again with a new seed this many
#: times before the job goes back to the node's own planner.
MAX_REJECTIONS = 3
#: ``next`` blocks up to four minutes per call; this many calls is hours, and
#: a job that has not resolved by then is somebody's to look at.
MAX_TURNS = 90
#: One plan's ceiling. The Flip's Gemma took 2 m 44 s on job 1's long prompt
#: through the shim; the planner's own ceiling on a node is 300 s.
MODEL_TIMEOUT_S = 900.0


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# The node, over ssh
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """One sketchgen verb at a time on the node, as ``bin/sg`` runs them.

    ``runner`` is the seam the tests use: it takes the argv list and stdin
    text and returns ``(code, stdout, stderr)``. The default runs ssh.
    """

    host: str = "sld-cloud"
    remote: str = "$HOME/sketchgen"
    ssh: list[str] = field(default_factory=lambda: ["ssh"])
    runner: Callable[[list[str], str | None], tuple[int, str, str]] | None = None

    def argv(self, args: list[str]) -> list[str]:
        quoted = " ".join(shlex.quote(a) for a in args)
        return [*self.ssh, "-o", "BatchMode=yes", self.host,
                f"{self.remote}/.venv/bin/python3 {self.remote}/app/bin/sketchgen "
                f"{quoted} --db {self.remote}/sketchgen.db"]

    def run(self, args: list[str], stdin: str | None = None) -> tuple[int, str, str]:
        if self.runner is not None:
            return self.runner(args, stdin)
        try:
            done = subprocess.run(self.argv(args), input=stdin, capture_output=True,
                                  text=True, timeout=600, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 255, "", f"ssh {self.host}: {exc}"
        return done.returncode, done.stdout, done.stderr

    def json(self, args: list[str], stdin: str | None = None) -> tuple[int, dict[str, Any], str]:
        """A verb that prints one JSON object; ``{}`` with the stderr when it did not."""
        code, out, err = self.run(args, stdin)
        try:
            loaded = json.loads(out) if out.strip() else {}
        except ValueError:
            loaded = {}
        return code, loaded if isinstance(loaded, dict) else {}, err


# ---------------------------------------------------------------------------
# The model, over Ollama's API
# ---------------------------------------------------------------------------


@dataclass
class Model:
    """The answering model: ``/api/generate`` on this box, one prompt in,
    the reply and its counts out."""

    name: str
    host: str = "http://127.0.0.1:11434"
    timeout: float = MODEL_TIMEOUT_S

    def answer(self, prompt: str, *, seed: int = 1) -> tuple[str, dict[str, Any], float]:
        payload = {
            "model": self.name,
            "prompt": prompt,
            "stream": False,
            "options": {"seed": seed},
        }
        if self.name.startswith("qwen"):
            # planner.py's rule: Ollama rejects an option a model does not declare.
            payload["think"] = False
        request = urllib.request.Request(
            self.host.rstrip("/") + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                text = exc.read().decode("utf-8", "replace")[:300]
            raise OSError(f"{self.host} answered {exc.code} for {self.name}: {text}") from exc
        except (OSError, urllib.error.URLError, ValueError) as exc:
            raise OSError(f"{self.host} did not answer usably: {exc}") from exc
        wall = time.monotonic() - started
        text = body.get("response")
        if not isinstance(text, str):
            raise OSError("the model host returned no 'response' field")
        return text, body, wall


def process_of(body: dict[str, Any], wall_s: float) -> dict[str, Any]:
    """The item's ``process``: what this agent can say truthfully about the
    work around the reply. It used no tools and took no screenshots; the
    session was the one call; thinking is the model's affair and unknown
    here unless the host counted none (a model asked not to think)."""
    out = {
        "session_s": round(wall_s, 1),
        "output_tokens": body.get("eval_count") if isinstance(body.get("eval_count"), int) else None,
        "thinking_tokens": None,
        "tool_calls": 0,
        "screenshots": 0,
        "effort": None,
    }
    return out


def usage_of(body: dict[str, Any]) -> dict[str, int | None]:
    def count(key: str) -> int | None:
        value = body.get(key)
        return value if isinstance(value, int) and value >= 0 else None
    return {"prompt_tokens": count("prompt_eval_count"),
            "completion_tokens": count("eval_count")}


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    do: str
    job: int | None = None
    say: str = ""
    entry: int | None = None
    plans: int = 0
    rejected: list[str] = field(default_factory=list)
    exit: int = EXIT_OK

    def as_dict(self) -> dict[str, Any]:
        return {"do": self.do, "job": self.job, "say": self.say, "entry": self.entry,
                "plans": self.plans, "rejected": list(self.rejected), "exit": self.exit}


def _say(log: Callable[[str], None], text: str) -> None:
    log(f"{utc_now()}  {text}")


def drive(
    node: Node,
    model: Model,
    *,
    prompt: str,
    by: str,
    executor: str = "local",
    note: str | None = None,
    log: Callable[[str], None] = lambda line: print(line, file=sys.stderr),
    max_rejections: int = MAX_REJECTIONS,
    max_turns: int = MAX_TURNS,
) -> Outcome:
    """One prompt, start to ``done``: the AGENTS.md loop with this model answering."""
    since = utc_now()
    args = ["paid", "start", "--as", model.name, "--by", by, "--executor", executor,
            "--since", since, "--prompt", prompt, "--json"]
    if note:
        args += ["--note", note]
    code, started, err = node.json(args)
    if not started.get("started"):
        checks = started.get("preflight", {}).get("checks") or []
        lines = [f"{'ok  ' if row.get('ok') else 'FAIL'} {row.get('check')}: {row.get('detail')}"
                 for row in checks]
        say = "NOT READY — nothing queued\n" + "\n".join(lines) if lines else (
            f"paid start did not start a job (exit {code}): {err.strip() or json.dumps(started)}")
        _say(log, say)
        return Outcome(do="stop", say=say, exit=EXIT_REFUSED)
    job = int(started["job"])
    _say(log, f"started job {job} on {node.host}: planner {model.name}, executor "
              f"{started.get('executor')}, {started.get('budget_say') or 'no budget line'}")

    outcome = Outcome(do="stop", job=job)
    seed = 1
    for _ in range(max_turns):
        code, step, err = node.json(["paid", "next", "--job", str(job), "--as", model.name])
        do = step.get("do")
        if elapsed := step.get("elapsed"):
            if isinstance(elapsed, dict) and elapsed.get("over_budget"):
                _say(log, str(elapsed["over_budget"]))
        if do == "answer":
            items = [i for i in (step.get("items") or []) if isinstance(i, dict)]
            if not items:
                outcome.say = "the packet carried no items"
                outcome.exit = EXIT_FAIL
                return outcome
            for item in items:
                try:
                    text, body, wall = model.answer(str(item.get("prompt") or ""), seed=seed)
                except OSError as exc:
                    outcome.say = f"the model could not answer: {exc}"
                    outcome.exit = EXIT_FAIL
                    _say(log, outcome.say)
                    _release(node, job, model.name, outcome.say)
                    return outcome
                item["answer"] = text
                item["usage"] = usage_of(body)
                item["process"] = process_of(body, wall)
                _say(log, f"job {job}: {step.get('step', 'plan')} answered by {model.name} in "
                          f"{wall:.1f} s, {item['usage']['prompt_tokens']}→"
                          f"{item['usage']['completion_tokens']} tok")
            step["model"] = model.name
            code, report, err = node.json(["paid", "import", "-"], stdin=json.dumps(step))
            rejected = report.get("rejected") or []
            if rejected:
                reasons = [str(r.get("reason")) for r in rejected if isinstance(r, dict)]
                outcome.rejected.extend(reasons)
                _say(log, f"job {job}: rejected: {'; '.join(reasons)}")
                if len(outcome.rejected) >= max_rejections:
                    outcome.say = (f"{len(outcome.rejected)} answers rejected; "
                                   "releasing the job to the node's own planner")
                    outcome.exit = EXIT_FAIL
                    _say(log, outcome.say)
                    _release(node, job, model.name, outcome.say)
                    return outcome
                seed += 1
                continue
            if not report.get("recorded"):
                outcome.say = f"import recorded nothing (exit {code}): {err.strip() or json.dumps(report)}"
                outcome.exit = EXIT_FAIL
                _say(log, outcome.say)
                return outcome
            outcome.plans += int(report.get("recorded") or 0)
            _say(log, f"job {job}: plan landed; {report.get('then') or 'next'}")
            continue
        if do == "wait":
            worker = step.get("worker") if isinstance(step.get("worker"), dict) else {}
            _say(log, f"job {job}: waiting — {step.get('say') or worker.get('headline') or 'the worker has it'}")
            continue
        if do == "done":
            outcome.do = "done"
            outcome.entry = step.get("entry") if isinstance(step.get("entry"), int) else None
            outcome.say = (f"job {job} is {step.get('state')}"
                           + (f", entry {outcome.entry}" if outcome.entry else "")
                           + (f": {step['last_error']}" if step.get("last_error") else ""))
            outcome.exit = EXIT_OK if step.get("state") in ("held", "published") else EXIT_FAIL
            _say(log, outcome.say)
            return outcome
        if do == "handoff":
            outcome.do = "handoff"
            outcome.say = f"job {job}: the rest is {step.get('to')}'s — {step.get('then')}"
            _say(log, outcome.say)
            return outcome
        outcome.do = "stop"
        outcome.say = str(step.get("say") or err.strip() or f"paid next exit {code}: {json.dumps(step)[:300]}")
        outcome.exit = EXIT_REFUSED
        _say(log, f"job {job}: stop — {outcome.say}")
        return outcome
    outcome.say = f"job {job}: still not done after {max_turns} turns of `next`"
    outcome.exit = EXIT_FAIL
    _say(log, outcome.say)
    return outcome


def _release(node: Node, job: int, model: str, reason: str) -> None:
    node.run(["paid", "release", "--job", str(job), "--by", model, "--reason", reason])


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sketchgen paid-agent",
        description="Plan jobs on a node's queue as a paid model, answered by the model "
                    "behind Ollama's API on this box.",
    )
    parser.add_argument("--as", dest="model", required=True, metavar="MODEL_ID",
                        help="your model id: the name the local Ollama API serves, and the "
                             "provenance every record carries")
    parser.add_argument("--by", required=True, metavar="USERNAME", help="the operator's GitHub username")
    parser.add_argument("--prompt", action="append", default=[], metavar="TEXT",
                        help="a job's prompt (repeatable; jobs run one after another)")
    parser.add_argument("--prompts", metavar="FILE", help="one prompt per line, blank lines skipped")
    parser.add_argument("--node", default="sld-cloud", help="ssh host of the node (default sld-cloud)")
    parser.add_argument("--remote", default="$HOME/sketchgen",
                        help="the node's ~/sketchgen (default $HOME/sketchgen there)")
    parser.add_argument("--ssh", default="ssh", help="the ssh command (default ssh)")
    parser.add_argument("--host", default="http://127.0.0.1:11434",
                        help="the local Ollama API (default http://127.0.0.1:11434, the shim)")
    parser.add_argument("--executor", default="local",
                        help="who writes the sketch: local (the node's own), an Ollama tag "
                             "there, or a registered paid model (default local)")
    parser.add_argument("--note", default=None, metavar="TEXT",
                        help="what the node cannot see about how this is made")
    parser.add_argument("--json", action="store_true", help="one JSON object per job on stdout")
    args = parser.parse_args(argv)

    prompts = list(args.prompt)
    if args.prompts:
        try:
            with open(args.prompts, encoding="utf-8") as handle:
                prompts += [line.strip() for line in handle if line.strip()]
        except OSError as exc:
            print(f"sketchgen: refused: cannot read {args.prompts}: {exc}", file=sys.stderr)
            return EXIT_REFUSED
    if not prompts:
        print("sketchgen: refused: no prompt (--prompt or --prompts)", file=sys.stderr)
        return EXIT_REFUSED
    if ":" not in args.model:
        print(f"sketchgen: refused: --as {args.model!r} is not name:tag", file=sys.stderr)
        return EXIT_REFUSED

    node = Node(host=args.node, remote=args.remote, ssh=shlex.split(args.ssh))
    model = Model(name=args.model, host=args.host)
    worst = EXIT_OK
    for prompt in prompts:
        outcome = drive(node, model, prompt=prompt, by=args.by, executor=args.executor,
                        note=args.note)
        if args.json:
            print(json.dumps({"prompt": prompt, **outcome.as_dict()}, sort_keys=True))
        else:
            print(f"{outcome.do}: {outcome.say}")
        worst = max(worst, outcome.exit)
        if outcome.do == "stop":
            # The stop rule: report what the node said, verbatim, and stop.
            break
    return worst


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
