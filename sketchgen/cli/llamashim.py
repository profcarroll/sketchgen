"""``sketchgen llama-shim``: Ollama's API on loopback, in front of a llama-server.

The verb is a thin wrapper: everything, including the argument parsing, is in
:mod:`sketchgen.llamashim`, so the module runs on its own (``python3 -m
sketchgen.llamashim``) on a box that has the package and nothing else.
"""

from __future__ import annotations

import argparse

from sketchgen import llamashim


def _run(args: argparse.Namespace) -> int:
    argv = ["--upstream", args.upstream, "--name", args.name,
            "--bind", args.bind, "--port", str(args.port), "--timeout", str(args.timeout)]
    return llamashim.main(argv)


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "llama-shim",
        help="serve Ollama's API on loopback in front of one llama-server",
        description=(
            "The five Ollama endpoints the pipeline uses (/api/generate, /api/chat with "
            "images, /api/ps, /api/tags, /api/show) over one llama-server's OpenAI "
            "endpoint, for a node whose model runs under llama.cpp rather than Ollama "
            "(the Retroid Flip 2). One upstream, one model name; any other name is "
            "404, as Ollama would answer. Loopback only."
        ),
    )
    p.add_argument("--upstream", required=True, metavar="URL",
                   help="the llama-server, e.g. http://127.0.0.1:8092")
    p.add_argument("--name", required=True, metavar="TAG",
                   help="the model name the worker is configured with, e.g. "
                        "gemma4:e4b-qat-q4_0; it is what every record carries")
    p.add_argument("--bind", default="127.0.0.1",
                   help="address to bind (127.0.0.1 only; anything else exits 3)")
    p.add_argument("--port", type=int, default=llamashim.DEFAULT_PORT,
                   help=f"port (default {llamashim.DEFAULT_PORT}, Ollama's own)")
    p.add_argument("--timeout", type=float, default=llamashim.DEFAULT_TIMEOUT_S,
                   metavar="S", help="ceiling on one completion, seconds")
    p.set_defaults(func=_run, _parser=p)
