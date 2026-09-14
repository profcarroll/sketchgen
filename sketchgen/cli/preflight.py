"""``sketchgen preflight`` — name the p5 globals a sketch declares over.

A drop-in subcommand (see sketchgen/cli/__init__.py): bin/sketchgen imports this
module and calls :func:`register`. The scan itself is sketchgen/preflight.py and
the worker runs it on every failed gate; this is the same scan by hand, for
looking at an attempt directory after the fact.

Exit codes here are the scan's answer rather than this build's usual triple:
0 nothing shadowed, 1 something is, 2 the directory holds no sketch.js.
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
        help="name the p5 globals a sketch declares over, before the gate runs",
        description=(
            "Scan <sketch-dir>/sketch.js for variables, parameters and "
            "functions that hide a p5.js global — the collision behind seven "
            "of the eleven crashing attempts the gate has recorded. Prints one "
            "line per finding, the same lines the worker puts at the top of a "
            "failed attempt's evidence. Exit 0 when nothing is shadowed, 1 "
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
