"""Preview a critic prompt on real entries without recording or spawning.

Run on the node, from a copy of a branch (never from ~/sketchgen/app): it opens
the database read-only, calls lineage.critique() with each prompt file named,
and prints JSON lines. Nothing is written to `critiques`, no child is queued.

    python3 rig/critic_dryrun.py --db ~/sketchgen/sketchgen.db \
        --prompt v3=/tmp/critic-v3.md --prompt v4=prompts/critic.md \
        --model gemma4:e4b --model gemma4:26b --sample 20

Written for critic-v4 (2026-10-02): a new critic version re-opens every
published entry to critique, so it is worth reading twenty of its sentences
before it writes a thousand.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sketchgen import lineage  # noqa: E402


def sample(conn: sqlite3.Connection, n: int, since: str) -> list[int]:
    """Line tips the idle critic could reach, one per root, spread evenly over
    four bands of generation (0, 1-2, 3-4, 5+), newest first in each band.

    The first dry run (2026-10-02) took the deepest tips only, all generation
    5 or 6, so critic-v4's "no refine after two revisions" meant it never had
    the chance to refine. The bands are so every move is on the table."""
    parent = {r["child_entry_id"]: r["parent_entry_id"]
              for r in conn.execute("SELECT * FROM lineage")}
    has_child = set(parent.values())
    def root_of(e):
        for _ in range(200):
            if e not in parent:
                return e
            e = parent[e]
        return e
    def gen_of(e):
        g = 0
        while e in parent and g < 200:
            e, g = parent[e], g + 1
        return g
    rows = conn.execute(
        "SELECT id FROM entries WHERE state IN ('published','held') "
        "AND strip_path IS NOT NULL AND created_utc >= ? ORDER BY id", (since,)
    ).fetchall()
    tips = [r["id"] for r in rows if r["id"] not in has_child]
    by_root: dict[int, int] = {}
    for e in sorted(tips, key=lambda e: (-gen_of(e), e)):
        by_root.setdefault(root_of(e), e)
    bands = ((0, 0), (1, 2), (3, 4), (5, 999))
    per_band = max(1, n // len(bands))
    seen_roots: set[int] = set()
    picked = []
    for low, high in bands:
        band = [e for e in sorted(tips, reverse=True)
                if low <= gen_of(e) <= high and root_of(e) not in seen_roots]
        for e in band[:per_band]:
            seen_roots.add(root_of(e))
            picked.append((e, gen_of(e)))
    return picked


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True)
    ap.add_argument("--prompt", action="append", required=True, metavar="NAME=PATH")
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--sample", type=int, default=20)
    ap.add_argument("--since", default="2026-09-26")
    ap.add_argument("--entry", type=int, action="append", default=[])
    ap.add_argument("--host", default=lineage.DEFAULT_HOST)
    ap.add_argument("--timeout", type=float, default=300.0,
                    help="seconds per call; gemma4:26b over two images on "
                         "sld-cloud's CPU needs more than the critic's 300")
    args = ap.parse_args()
    uri = "file:" + os.path.expanduser(args.db) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    prompts = [p.split("=", 1) for p in args.prompt]
    entries = [(e, None) for e in args.entry] or sample(conn, args.sample, args.since)
    for entry_id, gen in entries:
        for name, path in prompts:
            for model in args.model:
                if name == "v3" and model != args.model[0]:
                    continue  # the baseline is today's critic, on today's model
                started = time.monotonic()
                out = {"entry": entry_id, "gen": gen, "prompt": name, "model": model}
                try:
                    result = lineage.critique(conn, entry_id, model=model,
                                              host=args.host, prompt_path=path,
                                              timeout=args.timeout)
                    out.update(text=result.text, ghost=bool(result.ghost_sha256))
                    if name != "v3":
                        out["lens"] = lineage.critic_lens(
                            entry_id, lineage.prompt_version(path))
                except (lineage.CritiqueFailed, lineage.CritiqueRefused) as exc:
                    out.update(error=str(exc), raw=getattr(exc, "raw", None))
                out["s"] = round(time.monotonic() - started, 1)
                print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
