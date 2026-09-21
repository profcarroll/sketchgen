#!/usr/bin/env python3
"""cost.py: what this session has cost so far, from the transcript Claude Code already writes.

    python3 cost.py [--since ISO] [--until ISO] [--transcript PATH]

Every assistant message in the transcript carries its own `usage` and a timestamp, so
time, tokens generated, thinking, tool calls and screenshots can be totalled at any
moment, by the agent or by the operator, with nothing installed. It reads the newest
transcript for the current directory unless told which one. Prints a summary and, last,
one JSON line to paste into a packet's `process`. Stdlib only.
"""
import argparse, collections, glob, json, os, re, sys
from datetime import datetime

def when(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def newest_transcript():
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/{slug}/*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--transcript")
    a = ap.parse_args()
    path = a.transcript or newest_transcript()
    if not path:
        sys.exit("no transcript found for this directory")
    lo = when(a.since) if a.since else None
    hi = when(a.until) if a.until else None

    msgs, tools, shots, stamps = {}, set(), 0, []
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ts, m = r.get("timestamp"), r.get("message")
        if not ts or not isinstance(m, dict):
            continue
        t = when(ts)
        if (lo and t < lo) or (hi and t > hi):
            continue
        stamps.append(t)
        blocks = m.get("content") if isinstance(m.get("content"), list) else []
        if r.get("type") == "assistant" and m.get("usage"):
            # A message can span several lines with the same id: keep the largest of each count.
            d = msgs.setdefault(m.get("id") or r.get("uuid"), collections.Counter())
            u = m["usage"]
            for k, v in (("out", u.get("output_tokens")), ("think", (u.get("output_tokens_details") or {}).get("thinking_tokens"))):
                d[k] = max(d[k], v or 0)
            tools.update(b["id"] for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use")
        if r.get("type") == "user":
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_result" and isinstance(b.get("content"), list):
                    shots += sum(1 for x in b["content"] if isinstance(x, dict) and x.get("type") == "image")

    if not stamps:
        sys.exit("nothing in that window")
    start = lo or min(stamps)
    end = hi or max(stamps)
    out = sum(d["out"] for d in msgs.values())
    think = sum(d["think"] for d in msgs.values())
    wall = int((end - start).total_seconds())
    print(f"{start:%H:%M:%SZ} -> {end:%H:%M:%SZ}  {wall // 60} min {wall % 60} s")
    print(f"{len(msgs)} assistant messages, {len(tools)} tool calls, {shots} screenshots")
    print(f"{out:,} tokens generated, {think:,} of them thinking ({100 * think // max(out, 1)}%)")
    print(json.dumps({"session_s": wall, "output_tokens": out, "thinking_tokens": think,
                      "tool_calls": len(tools), "screenshots": shots}))

if __name__ == "__main__":
    main()
