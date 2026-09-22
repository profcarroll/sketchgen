#!/usr/bin/env python3
"""cost.py: what this session has cost so far, from the transcript Claude Code already writes.

    python3 cost.py [--since ISO] [--until ISO] [--reply FILE] [--transcript PATH]

Every assistant message in the transcript carries its own `usage` and a timestamp, so
time, tokens generated, thinking, tool calls and screenshots can be totalled at any
moment, by the agent or by the operator, with nothing installed. It reads the newest
transcript for the current directory unless told which one. Prints a summary and, last,
one JSON line to merge into a packet's item: its `process` is the window's total and its
`usage` is the reply's own counts. Stdlib only.

`--reply FILE` names the file the reply was written to (the answer as it goes into
`items[0].answer`). The message that wrote it is found by its text — the tool call
whose input carries the reply's fenced js block, or the whole reply when there is no
block — and that message's `usage` is the reply's: `prompt_tokens` is everything the
model read to write it (`input_tokens` and both cache counts), `completion_tokens` is
everything it generated, thinking included. The API counted both; nothing here is
estimated. A reply that no single message wrote (edited in place across several tool
calls) is reported as not found and its `usage` left null, which is the truth.
"""
import argparse, collections, glob, json, os, re, sys
from datetime import datetime

def when(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))

def newest_transcript():
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/{slug}/*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None

FENCE = re.compile(r"```(?:js|javascript)\s*\n(.*?)```", re.S)

def reply_key(text):
    """The text that identifies the reply: its js block's body, else all of it."""
    text = text.replace("\r\n", "\n")
    block = FENCE.search(text)
    return (block.group(1) if block else text).strip()

def strings_in(value):
    """Every string inside a tool call's input, however it is nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings_in(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings_in(v)

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since"); ap.add_argument("--until"); ap.add_argument("--transcript")
    ap.add_argument("--reply", help="the file the reply was written to: its usage is reported")
    a = ap.parse_args()
    path = a.transcript or newest_transcript()
    if not path:
        sys.exit("no transcript found for this directory")
    lo = when(a.since) if a.since else None
    hi = when(a.until) if a.until else None
    key = None
    if a.reply:
        try:
            key = reply_key(open(a.reply, encoding="utf-8").read())
        except OSError as exc:
            sys.exit(f"cannot read the reply: {exc}")
        if not key:
            sys.exit("the reply is empty")

    msgs, tools, shots, stamps, wrote = {}, set(), 0, [], {}
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
            mid = m.get("id") or r.get("uuid")
            d = msgs.setdefault(mid, collections.Counter())
            u = m["usage"]
            for k, v in (("out", u.get("output_tokens")),
                         ("think", (u.get("output_tokens_details") or {}).get("thinking_tokens")),
                         ("in", u.get("input_tokens")),
                         ("cache_new", u.get("cache_creation_input_tokens")),
                         ("cache_read", u.get("cache_read_input_tokens"))):
                d[k] = max(d[k], v or 0)
            tools.update(b["id"] for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use")
            if key and mid not in wrote:
                # The message that wrote the reply is the one whose tool call carries its text.
                # The earliest is the one that generated it; a later copy is a copy.
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and any(
                            key in s.replace("\r\n", "\n") for s in strings_in(b.get("input"))):
                        wrote[mid] = t
                        break
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
    usage = {"prompt_tokens": None, "completion_tokens": None}
    if not key:
        print("reply: not looked for; --reply FILE names the file it was written to")
    elif not wrote:
        print("reply: not found in one message of this window; usage left null")
    else:
        mid, at = min(wrote.items(), key=lambda kv: kv[1])
        d = msgs[mid]
        usage = {"prompt_tokens": d["in"] + d["cache_new"] + d["cache_read"],
                 "completion_tokens": d["out"]}
        later = len(wrote) - 1
        copies = f", and {later} later {'copy' if later == 1 else 'copies'}" if later else ""
        print(f"reply: written at {at:%H:%M:%SZ}, {usage['prompt_tokens']:,} tokens read, "
              f"{usage['completion_tokens']:,} generated{copies}")
    print(json.dumps({"usage": usage,
                      "process": {"session_s": wall, "output_tokens": out, "thinking_tokens": think,
                                  "tool_calls": len(tools), "screenshots": shots}}))

if __name__ == "__main__":
    main()
