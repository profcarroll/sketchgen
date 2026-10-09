"""``sketchgen paid-agent``: this box's model as a paid planner on a node's queue.

A thin wrapper: the loop, the ssh plumbing and the argument parsing are in
:mod:`sketchgen.paidagent`, so it also runs as ``python3 -m sketchgen.paidagent``
on a box with the package alone (the Flip, where the kit installs it).
"""

from __future__ import annotations

import argparse
import sys

from sketchgen import paidagent


def _run(args: argparse.Namespace) -> int:
    # `sketchgen paid-agent -- --as …`: REMAINDER keeps the `--` that separates
    # the verb from the module's own options, and the module's parser would
    # read everything after it as positional. Drop it.
    rest = list(args.rest)
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return paidagent.main(rest)


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "paid-agent",
        help="plan jobs on a node's queue as a paid model answered by this box's model",
        description=(
            "Runs AGENTS.md's paid loop (start, next, answer, import, until done) on a node "
            "over ssh, answering each plan packet with the model behind Ollama's API here "
            "(the shim on a handheld). The job's lease stops the node's idle work and the "
            "node's own worker writes, gates and holds the sketch. Every argument after "
            "`paid-agent` is the module's own: see `sketchgen paid-agent -- --help`."
        ),
        add_help=False,
    )
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=_run, _parser=p)
