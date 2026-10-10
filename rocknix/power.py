#!/usr/bin/env python3
"""rocknix/power.py — the handheld's sketchgen is off unless the operator has it on.

A Flip 2 on a battery is not a node. Its operator has a gamepad and no terminal, so the
only switch is the console's own Pause / Resume, opened in Firefox from the Ports entry
(rocknix/ports/Sketchgen Console.sh). That entry starts ``sketchgen.target``: the console
and this keeper, nothing else. Every few seconds the keeper reads the console's switch
(the control row) and makes the units match it:

- generator **on** (running): the worker, which pulls in the shim and llama-server;
- generator **off** (paused, or pausing with no worker left to finish anything): the
  worker, the shim and llama-server stopped, which gives back the ~8 GB the model holds;
- generator off **and** the console closed: ``sketchgen.target`` stopped, this keeper
  with it. Nothing is left running and nothing comes back at boot: no unit in the kit
  but the gallery has an ``[Install]`` section.

A generator left on keeps running after Firefox closes; that is the operator's choice,
and the console shows it the next time the entry is opened. ``pausing`` with a worker up
is left alone: the worker finishes its attempt and writes ``paused`` itself. ``pausing``
with no worker (Pause pressed while the generator was already off) has nobody to finish
it, so the keeper writes ``paused`` the way the worker would, or the console's pill would
say pausing until the next Resume (car12, 2026-10-09).

The console counts as open while /run/sketchgen/console names a live process (the Ports
entry writes its own pid there and removes the file on the way out; the pid check covers
an entry that was killed before it could).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketchgen import db  # noqa: E402

CONSOLE_FLAG = Path("/run/sketchgen/console")
TARGET = "sketchgen.target"
WORKER = "sketchgen-worker.service"
# The worker first: stopped while the model still answers, it re-queues what it held.
MODEL_UNITS = (WORKER, "sketchgen-shim.service", "sketchgen-llama.service")
# What `systemctl is-active` prints for a unit that is, or is about to be, holding memory.
UP_STATES = frozenset({"active", "activating", "reloading", "deactivating"})

START, STOP_MODEL, STOP_ALL, HOLD = "start", "stop-model", "stop-all", "hold"


def decide(state: str, up: set[str], console_open: bool) -> str:
    """What to do about the units, given the switch, the units that are up and the console."""
    if state == "running":
        return HOLD if WORKER in up else START
    if state == "pausing" and WORKER in up:
        return HOLD
    if not console_open:
        return STOP_ALL
    return STOP_MODEL if up & set(MODEL_UNITS) else HOLD


def generator_state(path: Path) -> str:
    """The console's switch. No control row reads as running, as it does to the worker."""
    conn = db.connect(path)
    try:
        control = db.get_control(conn)
    finally:
        conn.close()
    return control.state if control is not None else "running"


def settle_pause(path: Path) -> bool:
    """``pausing`` with no worker to finish it becomes ``paused``, as worker.run_once
    writes it, reason kept. True when it wrote."""
    conn = db.connect(path)
    try:
        control = db.get_control(conn)
        if control is None or control.state != "pausing":
            return False
        db.set_control(conn, "paused", control.reason)
        return True
    finally:
        conn.close()


def console_open(flag: Path = CONSOLE_FLAG) -> bool:
    try:
        pid = int(flag.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # alive, and not ours to signal
        return True
    return True


def units_up() -> set[str]:
    done = subprocess.run(["systemctl", "is-active", *MODEL_UNITS],
                          capture_output=True, text=True)
    return {unit for unit, state in zip(MODEL_UNITS, done.stdout.split()) if state in UP_STATES}


def act(action: str) -> None:
    # --no-block: llama-server takes a minute to load from the SD card, and the
    # keeper has to go on reading the switch meanwhile.
    if action == START:
        subprocess.run(["systemctl", "start", "--no-block", WORKER], check=False)
    elif action == STOP_MODEL:
        subprocess.run(["systemctl", "stop", "--no-block", *MODEL_UNITS], check=False)
    elif action == STOP_ALL:
        subprocess.run(["systemctl", "stop", "--no-block", TARGET], check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--every", type=float, default=5.0, metavar="S",
                        help="seconds between looks at the switch (default: 5)")
    args = parser.parse_args(argv)
    last = None
    while True:
        try:
            state = generator_state(args.db)
        except Exception as exc:  # a locked or missing database: look again, change nothing
            print(f"power: could not read the switch: {exc}", flush=True)
            time.sleep(args.every)
            continue
        is_open = console_open()
        up = units_up()
        if state == "pausing" and WORKER not in up and settle_pause(args.db):
            state = "paused"
        action = decide(state, up, is_open)
        seen = (state, is_open, action)
        if seen != last:
            print(f"power: generator {state}, console {'open' if is_open else 'closed'}"
                  f" -> {action}", flush=True)
            last = seen
        act(action)
        if action == STOP_ALL:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main())
