"""``sketchgen preflight`` — what one sketch does to itself, in sentences.

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls :func:`register`. The scans themselves are in
sketchgen/preflight.py and the worker runs them on every failed gate; this is
the same pair by hand, for looking at an attempt directory after the fact.

Both scans run: the p5 globals the sketch declares over, and the per-frame costs
it cannot afford (2026-09-15, after job 166 and job 270).

Exit codes here are the scan's answer rather than this build's usual triple:
0 nothing found, 1 something was, 2 the directory holds no sketch.js.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sketchgen import preflight

EXIT_CLEAN = 0
EXIT_SHADOWED = 1
EXIT_NO_SKETCH = 2


def cmd_preflight(args: argparse.Namespace) -> int:
    sketch_dir = Path(os.path.expanduser(args.sketch_dir))
    if not (sketch_dir / preflight.SKETCH_FILE).is_file():
        print(
            f"sketchgen: no {preflight.SKETCH_FILE} in {sketch_dir}",
            file=sys.stderr,
        )
        return EXIT_NO_SKETCH
    lines = preflight.evidence_lines(preflight.scan_dir(sketch_dir))
    for line in lines:
        print(line)
    return EXIT_SHADOWED if lines else EXIT_CLEAN


def register(top: argparse._SubParsersAction) -> None:
    parser = top.add_parser(
        "preflight",
        help="name what a sketch does to itself, before the gate runs",
        description=(
            "Scan <sketch-dir>/sketch.js twice. First for variables, "
            "parameters and functions that hide a p5.js global — the collision "
            "behind seven of the eleven crashing attempts the gate has "
            "recorded. Then for the per-frame costs behind job 166 and job "
            "270: a 3D primitive or an immediate-mode line()/point() inside a "
            "loop inside draw() in a WEBGL sketch, an all-pairs loop in "
            "draw(), an allocation in draw(), a filter() inside a loop. Prints "
            "one line per finding, the same lines the worker puts at the top "
            "of a failed attempt's evidence. Exit 0 when nothing is found, 1 "
            "when something is, 2 when the directory holds no sketch.js. Runs "
            "no browser and calls no model."
        ),
    )
    parser.add_argument(
        "sketch_dir",
        metavar="sketch-dir",
        help="a directory holding sketch.js (an attempt directory, usually)",
    )
    parser.set_defaults(func=cmd_preflight, _parser=parser)
