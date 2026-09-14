"""Drop-in subcommands for bin/sketchgen.

Each module here defines ``register(top)`` and adds one parser to the top-level
subparsers with ``set_defaults(func=..., _parser=...)``. bin/sketchgen imports
every module in this package in name order, so a new packet adds a file and
touches nothing shared.
"""
