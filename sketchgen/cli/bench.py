"""``sketchgen bench``: one inference measurement, identical on every node.

Written 2026-09-23, the day three machines had to be compared: sld-cloud (16
ARM OCPUs, no GPU), d12 (RTX 5070 Ti 16 GB) and a rented OCI ``VM.GPU.A10.1``
(A10 24 GB) with about 95 hours of trial credit on it. Before this the only
speed figures were the ones jobs leave behind — ``decode_tok_s`` on attempt
rows — and those are shaped by whatever the job asked: prompt length varies by
an order of magnitude between a fresh prompt and a repair that carries its
parent's source, loads land inside some jobs and not others, and d12 reloads
its executor every job because planner and executor do not fit in VRAM
together. A number that is supposed to be about the machine has to come from a
request that is the same everywhere.

So this sends fixed prompts, at the pipeline's own context sizes, and reads
Ollama's own counters back: cold load time, prefill and decode tokens per
second at three prompt lengths, and what ended up resident where (``size_vram``
against ``size``, and ``nvidia-smi`` when there is one). Three things keep the
numbers honest:

- **The worker is paused, or this refuses.** A judge or an attempt running
  underneath would halve both parties' rates and neither would say so. The
  control row must read ``paused`` (``control pause`` and wait); ``pausing``
  is still finishing an attempt.
- **Every model is unloaded first**, so the load is a cold one and Ollama's
  prompt cache is empty. Each request also starts with its own line (run and
  case), because Ollama reuses a cached prefix within a runner and an identical
  second request would report a prefill of a handful of tokens.
- **Models are matched by digest, not tag.** d12 calls the executor
  ``qwen3-coder:30b`` and sld-cloud ``qwen3-coder:30b-a3b-q4_K_M``; the
  weights are the same blob (06c1097efce0). ``--compare`` lines nodes up on
  the digest and says when two runs did not send the same prompts.

What it cannot see: the Ollama server's own environment (flash attention, KV
cache type, ``NUM_PARALLEL``) decides a great deal and is not visible through
the API. It is read from ``systemctl show ollama`` where that answers, and
recorded as blank where it does not — never guessed.

Stdlib only. Writes nothing on the node but ``--out``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from sketchgen import db

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: Bumped whenever the prompts or the request change, so ``--compare`` never
#: lines up two runs that asked different questions.
BENCH_VERSION = 1

#: None means "where this node's worker looks" — see :func:`default_host`.
DEFAULT_HOST = os.environ.get("OLLAMA_HOST_URL")

#: (name, target prompt tokens). ``short`` is a fresh prompt with the rules
#: file, ``rules`` a first repair with its evidence, ``source`` a critique
#: child that is shown its parent's sketch — the three sizes the executor
#: actually sees. Targets are approximate: every model tokenises differently,
#: and the counts that are reported are Ollama's, not these.
CASES: tuple[tuple[str, int], ...] = (("short", 1000), ("rules", 4000), ("source", 12000))

#: Tokens asked for per request. Long enough that decode dominates its own
#: timer, short enough that a CPU node finishes the whole run in under half an
#: hour. A model may stop early; the rate is over what it actually produced.
NUM_PREDICT = 512
REPEAT = 3
SEED = 1

#: Room kept between a case's prompt plus its reply and the context window.
#: A case that does not fit is skipped and says so, rather than being
#: silently truncated from the head by Ollama.
CTX_MARGIN = 256

#: The planner runs at 8k in the pipeline (planner.py) and everything else at
#: the executor's 16k. The runner is keyed on num_ctx, so benching at another
#: size would measure a configuration the pipeline never loads.
PLANNER_NUM_CTX = 8192
EXECUTOR_NUM_CTX = 16384

REQUEST_TIMEOUT_S = 1800.0
UNLOAD_WAIT_S = 60.0

#: A fixed paragraph repeated to length. Its content is the kind of text the
#: executor reads (a rules file and a sketch), so tokenisation behaves as it
#: does on real prompts; its exact wording matters only in that it never
#: changes without BENCH_VERSION changing too.
FILLER = (
    "Rules for a p5.js sketch in global mode. Call createCanvas once in setup and "
    "draw every frame in draw. Keep state in top-level variables declared with let. "
    "Use random and noise only after randomSeed(1) and noiseSeed(1) so the gate sees "
    "the same frames every run. Respond to mousePressed and keyPressed where the "
    "brief asks for interaction. Avoid loops that allocate objects every frame; "
    "reuse arrays. A particle is an object with x, y, vx, vy and a life counter; "
    "update it, draw it with ellipse or line, and remove it when life reaches zero. "
    "function setup() { createCanvas(800, 600); colorMode(HSB, 360, 100, 100, 1); "
    "for (let i = 0; i < 200; i++) particles.push(makeParticle(width / 2, height / 2)); } "
    "function draw() { background(220, 30, 12, 0.2); for (const p of particles) { "
    "p.x += p.vx; p.y += p.vy; p.vy += 0.05; stroke((frameCount + p.x) % 360, 80, 95); "
    "point(p.x, p.y); } }\n"
)

TASK = (
    "\nUsing the rules above, write a complete p5.js sketch of a flock of paper "
    "lanterns drifting upward over a dark river, responding to the mouse. "
    "Reply with one fenced js block and nothing else."
)

Post = Callable[[str, str, "dict | None", float], dict]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def http(host: str, path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    """GET (no body) or POST JSON; the decoded reply. Raises OSError on failure."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        host.rstrip("/") + path,
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode() or "{}")
    except (urllib.error.URLError, ValueError) as exc:
        raise OSError(f"{path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def prompt_for(case: str, target_tokens: int, run: int, slot: int = 0) -> str:
    """The prompt for one request: a unique first line, then filler, then the task.

    About four characters a token, which is close for English and code under
    the tokenisers in use; the reported ``prompt_eval_count`` is the truth.
    """
    head = f"Benchmark {case}, run {run}, slot {slot}.\n"
    body_chars = max(0, target_tokens * 4 - len(head) - len(TASK))
    body = (FILLER * (body_chars // len(FILLER) + 1))[:body_chars]
    return head + body + TASK


def prompts_digest() -> str:
    """One hash over every prompt a default run sends, for ``--compare``."""
    h = hashlib.sha256(f"v{BENCH_VERSION}/{NUM_PREDICT}/{SEED}".encode())
    for case, target in CASES:
        h.update(prompt_for(case, target, 1).encode())
    return h.hexdigest()[:12]


def num_ctx_for(model: str, planner: str) -> int:
    return PLANNER_NUM_CTX if model == planner else EXECUTOR_NUM_CTX


#: What the filler actually tokenises to, measured on d12 on 2026-09-23:
#: qwen3-coder 3.19 characters a token and gemma4 3.09, not the four the
#: prompts were sized by — the 12k case is 15,029 tokens. The prompts stay
#: as they are (changing them would orphan that run); the fit check uses the
#: measured ratio. At 3.0 it would skip the 12k case at 16k, which fits with
#: 800 tokens to spare; gemma4's slightly denser count is inside CTX_MARGIN,
#: and a run that overflows anyway is flagged from Ollama's own counts.
CHARS_PER_TOKEN = 3.1


def fits(target_tokens: int, num_ctx: int) -> bool:
    estimated = target_tokens * 4 / CHARS_PER_TOKEN
    return estimated + NUM_PREDICT + CTX_MARGIN <= num_ctx


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key.strip() in ("model name", "Model name"):
                return value.strip()
    except OSError:
        pass
    # ARM kernels do not put a model name in /proc/cpuinfo; lscpu does.
    for line in _run(["lscpu"]).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "Model name":
            return value.strip()
    return None


def _mem_total_gb() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return round(int(line.split()[1]) / 1024 / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def gpu() -> dict[str, Any]:
    """Name, driver and VRAM of the first GPU; every field None without nvidia-smi."""
    out = _run([
        "nvidia-smi",
        "--query-gpu=name,driver_version,memory.total,memory.used",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return {"name": None, "driver": None, "vram_mb": None, "used_mb": None}
    name, driver, total, used = [part.strip() for part in out.splitlines()[0].split(",")]
    return {"name": name, "driver": driver, "vram_mb": float(total), "used_mb": float(used)}


def ollama_env() -> dict[str, str]:
    """The OLLAMA_* settings of the ollama unit, including drop-ins; {} if unreadable."""
    return {k: v for k, v in _unit_env("ollama", user=False).items() if k.startswith("OLLAMA_")}


def _unit_env(unit: str, user: bool = True) -> dict[str, str]:
    """A systemd unit's Environment, drop-ins included; {} if unreadable."""
    argv = ["systemctl"] + (["--user"] if user else []) + [
        "show", unit, "--property=Environment", "--value"]
    found: dict[str, str] = {}
    for word in _run(argv).split():
        key, sep, value = word.partition("=")
        if sep:
            found[key] = value
    return found


def default_host() -> str:
    """Where the worker reaches Ollama: its unit's OLLAMA_HOST_URL, else loopback.

    d12's Ollama listens on its Tailscale address only (OLLAMA_HOST in the
    ollama unit, read 2026-09-23), and the worker is pointed there by a
    drop-in a shell does not inherit; a bench defaulting to 127.0.0.1 there
    reports no model host on a machine that has one.
    """
    return (_unit_env("sketchgen-worker").get("OLLAMA_HOST_URL")
            or "http://127.0.0.1:11434")


def default_models(conn=None) -> tuple[str, str]:
    """(executor, planner) as this node's worker would pick them.

    The assignment wins, then the worker unit's environment, then the code's
    defaults — the worker's own order. Read from here because a shell does not
    inherit the unit's drop-ins: d12's executor tag is set in one, and a bench
    that fell back to the code default would ask for a tag d12 does not have.
    """
    from sketchgen import executor, worker

    assigned = db.get_assignment(conn) if conn is not None else {}
    unit = _unit_env("sketchgen-worker")
    execute = (assigned.get("execute") or unit.get("SKETCHGEN_EXECUTOR_MODEL")
               or executor.DEFAULT_MODEL)
    plan = (assigned.get("plan") or unit.get("SKETCHGEN_PLANNER_MODEL")
            or worker.DEFAULT_PLANNER_MODEL)
    return execute, plan


def _commit() -> str | None:
    root = Path(__file__).resolve().parents[2]
    return _run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"]) or None


def environment(host: str, post: Post, conn=None) -> dict[str, Any]:
    from sketchgen import worker  # heavy import, only for the one string

    try:
        version = post(host, "/api/version", None, 5.0).get("version")
    except OSError:
        version = None
    return {
        "utc": db.utc_now(),
        "hostname": socket.gethostname(),
        "arch": platform.machine(),
        "shape": worker.node_shape(conn),
        "cpu": _cpu_model(),
        "threads": os.cpu_count(),
        "mem_gb": _mem_total_gb(),
        "gpu": gpu(),
        "ollama": version,
        "ollama_env": ollama_env(),
        "commit": _commit(),
    }


# ---------------------------------------------------------------------------
# One model
# ---------------------------------------------------------------------------


def model_info(host: str, post: Post, name: str) -> dict[str, Any] | None:
    """Digest, size, quantisation and capabilities; None if it is not installed."""
    tags = post(host, "/api/tags", None, 10.0).get("models") or []
    entry = next((m for m in tags if m.get("name") == name or m.get("model") == name), None)
    if entry is None:
        return None
    try:
        show = post(host, "/api/show", {"model": name}, 10.0)
    except OSError:
        show = {}
    details = entry.get("details") or {}
    return {
        "name": name,
        "digest": (entry.get("digest") or "")[:12],
        "size_gb": round((entry.get("size") or 0) / 1e9, 2),
        "parameter_size": details.get("parameter_size"),
        "quantization": details.get("quantization_level"),
        "capabilities": sorted(show.get("capabilities") or []),
    }


def resident(host: str, post: Post, name: str) -> dict[str, Any] | None:
    for m in post(host, "/api/ps", None, 5.0).get("models") or []:
        if m.get("name") == name or m.get("model") == name:
            return m
    return None


def unload_all(host: str, post: Post, sleep: Callable[[float], None] = time.sleep) -> None:
    """Ask Ollama to drop every resident model, and wait until it has."""
    for m in post(host, "/api/ps", None, 5.0).get("models") or []:
        post(host, "/api/generate", {"model": m.get("name"), "keep_alive": 0}, 60.0)
    deadline = time.monotonic() + UNLOAD_WAIT_S
    while post(host, "/api/ps", None, 5.0).get("models"):
        if time.monotonic() > deadline:
            raise OSError("models still resident after unload")
        sleep(1.0)


def _s(ns: Any) -> float:
    return round((ns or 0) / 1e9, 3)


def _rate(count: Any, ns: Any) -> float | None:
    return round(count / (ns / 1e9), 2) if count and ns else None


def load(host: str, post: Post, name: str, num_ctx: int) -> dict[str, Any]:
    """Cold-load ``name`` at ``num_ctx``; the load time and where it landed."""
    started = time.monotonic()
    reply = post(host, "/api/generate",
                 {"model": name, "options": {"num_ctx": num_ctx}, "keep_alive": "30m"},
                 REQUEST_TIMEOUT_S)
    wall = round(time.monotonic() - started, 2)
    # Ollama 0.34.3 answers an empty-prompt load with no load_duration (d12,
    # 2026-09-23: 0.0 for both models, 3.5 s and 2.1 s by the clock), so the
    # wall clock is the load time when the counter is missing. "Cold" means
    # not resident in Ollama; the weights may still be in the OS page cache,
    # which only a root `drop_caches` would rule out, so a first load after
    # boot reads slower than this.
    counted = _s(reply.get("load_duration"))
    ps = resident(host, post, name) or {}
    size, size_vram = ps.get("size") or 0, ps.get("size_vram") or 0
    return {
        "num_ctx": num_ctx,
        "load_s": counted if counted else wall,
        "load_counted": bool(counted),
        "wall_s": wall,
        "resident_gb": round(size / 1e9, 2),
        "vram_gb": round(size_vram / 1e9, 2),
        "on_gpu_pct": round(100.0 * size_vram / size) if size else None,
        "gpu_used_mb": gpu()["used_mb"],
    }


def request(host: str, post: Post, name: str, num_ctx: int, prompt: str,
            think: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": name,
        "prompt": prompt,
        "stream": False,
        "keep_alive": "30m",
        "options": {"num_ctx": num_ctx, "seed": SEED, "temperature": 0,
                    "num_predict": NUM_PREDICT},
    }
    # Thinking off wherever the model has it, as the executor sends it: a
    # thinking model left on would spend the budget on tokens the pipeline
    # never asks for. Sent only where it is a capability, since a model
    # without one may refuse the field.
    if think:
        body["think"] = False
    started = time.monotonic()
    reply = post(host, "/api/generate", body, REQUEST_TIMEOUT_S)
    wall = time.monotonic() - started
    return {
        "prompt_tokens": reply.get("prompt_eval_count"),
        "tokens": reply.get("eval_count"),
        "prefill_tok_s": _rate(reply.get("prompt_eval_count"), reply.get("prompt_eval_duration")),
        "decode_tok_s": _rate(reply.get("eval_count"), reply.get("eval_duration")),
        "load_s": _s(reply.get("load_duration")),
        "wall_s": round(wall, 2),
    }


def _median(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r.get(key) is not None]
    return round(statistics.median(values), 2) if values else None


def bench_model(host: str, post: Post, info: dict, num_ctx: int, *, repeat: int,
                parallel: int, cases: tuple = CASES,
                say: Callable[[str], None] = lambda _l: None) -> dict[str, Any]:
    name = info["name"]
    think = "thinking" in info["capabilities"]
    unload_all(host, post)
    say(f"{name}: cold load at ctx {num_ctx}")
    loaded = load(host, post, name, num_ctx)
    results: dict[str, Any] = {}
    for case, target in cases:
        if not fits(target, num_ctx):
            results[case] = {"skipped": f"{target} + {NUM_PREDICT} tokens do not fit ctx {num_ctx}"}
            continue
        runs = []
        for run in range(1, repeat + 1):
            if parallel <= 1:
                row = request(host, post, name, num_ctx, prompt_for(case, target, run), think)
                say(f"{name}: {case} run {run}: prefill {row['prefill_tok_s']} "
                    f"decode {row['decode_tok_s']} tok/s")
            else:
                row = _parallel(host, post, name, num_ctx, case, target, run, think, parallel)
                say(f"{name}: {case} run {run} x{parallel}: "
                    f"aggregate {row['aggregate_decode_tok_s']} tok/s")
            runs.append(row)
        summary = {
            "prompt_tokens": _median(runs, "prompt_tokens"),
            "prefill_tok_s": _median(runs, "prefill_tok_s"),
            "decode_tok_s": _median(runs, "decode_tok_s"),
            "runs": runs,
        }
        if parallel > 1:
            summary["aggregate_decode_tok_s"] = _median(runs, "aggregate_decode_tok_s")
        # A prompt far shorter than asked means the prefix cache answered and
        # the prefill rate is fiction; a load inside a run means the runner
        # was torn down mid-bench (ctx flip, eviction). Either is reported.
        flags = []
        if summary["prompt_tokens"] and summary["prompt_tokens"] < target / 2:
            flags.append("prompt cached")
        if any((r.get("load_s") or 0) > 1.0 for r in runs):
            flags.append("reloaded during run")
        if any((r.get("prompt_tokens") or 0) + (r.get("tokens") or 0) > num_ctx for r in runs):
            flags.append("overflowed ctx")
        if flags:
            summary["flags"] = flags
        results[case] = summary
    return {"model": info, "load": loaded, "cases": results}


def _parallel(host, post, name, num_ctx, case, target, run, think, n) -> dict[str, Any]:
    """``n`` requests at once. Only meaningful if Ollama's NUM_PARALLEL >= n."""
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n) as pool:
        rows = list(pool.map(
            lambda slot: request(host, post, name, num_ctx,
                                 prompt_for(case, target, run, slot), think),
            range(n),
        ))
    wall = time.monotonic() - started
    tokens = sum(r.get("tokens") or 0 for r in rows)
    return {
        "parallel": n,
        "prompt_tokens": _median(rows, "prompt_tokens"),
        "prefill_tok_s": _median(rows, "prefill_tok_s"),
        "decode_tok_s": _median(rows, "decode_tok_s"),
        "aggregate_decode_tok_s": round(tokens / wall, 2) if wall else None,
        "load_s": max((r.get("load_s") or 0) for r in rows),
        "wall_s": round(wall, 2),
    }


# ---------------------------------------------------------------------------
# The verb
# ---------------------------------------------------------------------------


def _control(db_path: str) -> str | None:
    """The control row's state, or None when there is no database here."""
    if not Path(os.path.expanduser(db_path)).exists():
        return None
    conn = db.connect(os.path.expanduser(db_path))
    try:
        row = db.get_control(conn)
    finally:
        conn.close()
    return row.state if row else "running"


def cmd(args: argparse.Namespace, post: Post = http) -> int:
    if args.compare:
        return compare(args.compare, as_json=args.json)

    state = _control(args.db)
    if state not in (None, "paused"):
        print(
            f"sketchgen: refused: the generator is {state}. A job or an idle judge "
            "running underneath would share the model host and both rates would be "
            "wrong. Run `control pause --reason bench`, wait for `db status` to say "
            "paused, then bench, then `control resume`.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    host = args.host or default_host()
    try:
        post(host, "/api/version", None, 5.0)
    except OSError as exc:
        print(f"sketchgen: no model host at {host}: {exc}", file=sys.stderr)
        return EXIT_FAIL

    conn = db.connect(os.path.expanduser(args.db)) if state is not None else None
    try:
        executor_model, planner = default_models(conn)
        env = environment(host, post, conn)
    finally:
        if conn is not None:
            conn.close()
    names = args.model or [executor_model, planner]
    infos = []
    for name in names:
        info = model_info(host, post, name)
        if info is None:
            print(f"sketchgen: refused: {name} is not installed here "
                  "(`ollama list`; tags differ between nodes, digests do not)",
                  file=sys.stderr)
            return EXIT_REFUSED
        infos.append(info)

    say = (lambda line: None) if args.json else (
        lambda line: print(line, file=sys.stderr, flush=True))
    result = {
        "bench_version": BENCH_VERSION,
        "prompts": prompts_digest(),
        "num_predict": NUM_PREDICT,
        "repeat": args.repeat,
        "parallel": args.parallel,
        "node": env,
        "models": [],
    }
    try:
        for info in infos:
            ctx = args.num_ctx or num_ctx_for(info["name"], planner)
            result["models"].append(bench_model(
                host, post, info, ctx, repeat=args.repeat, parallel=args.parallel, say=say))
    except OSError as exc:
        print(f"sketchgen: bench failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(table([result]))
        print("\nleft paused: `control resume` when you are done", file=sys.stderr)
    return EXIT_OK


def table(results: list[dict]) -> str:
    """One line per node x model x case, matched on the model's digest."""
    lines = [f"{'node':<36} {'model':<30} {'digest':<12} {'load s':>7} {'gpu%':>5} "
             f"{'case':<7} {'prompt':>6} {'prefill':>8} {'decode':>7} {'agg':>7}"]
    rows = []
    for result in results:
        node = result["node"]
        label = node.get("hostname") or "?"
        if node["gpu"].get("name"):
            card = node["gpu"]["name"].replace("NVIDIA ", "").replace("GeForce ", "")
            label += f" ({card})"
        for entry in result["models"]:
            m, load_ = entry["model"], entry["load"]
            for case, row in entry["cases"].items():
                rows.append((m["digest"], case, label, m["name"], load_, row))
    order = {case: i for i, (case, _t) in enumerate(CASES)}
    for digest, case, label, name, load_, row in sorted(
            rows, key=lambda r: (r[0], order.get(r[1], 99), r[2])):
        if "skipped" in row:
            lines.append(f"{label[:36]:<36} {name[:30]:<30} {digest:<12} {'':>7} {'':>5} "
                         f"{case:<7} skipped")
            continue
        flags = f"  [{', '.join(row['flags'])}]" if row.get("flags") else ""
        lines.append(
            f"{label[:36]:<36} {name[:30]:<30} {digest:<12} {load_['load_s']:>7.1f} "
            f"{_fmt(load_.get('on_gpu_pct')):>5} {case:<7} {_fmt(row['prompt_tokens']):>6} "
            f"{_fmt(row['prefill_tok_s']):>8} {_fmt(row['decode_tok_s']):>7} "
            f"{_fmt(row.get('aggregate_decode_tok_s')):>7}{flags}"
        )
    return "\n".join(lines)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}" if isinstance(value, (int, float)) and value >= 100 else f"{value}"


def compare(paths: list[str], as_json: bool = False) -> int:
    results = []
    for path in paths:
        try:
            results.append(json.loads(Path(path).read_text()))
        except (OSError, ValueError) as exc:
            print(f"sketchgen: cannot read {path}: {exc}", file=sys.stderr)
            return EXIT_FAIL
    asked = {(r.get("bench_version"), r.get("prompts"), r.get("num_predict")) for r in results}
    if len(asked) > 1:
        print("sketchgen: warning: these runs did not send the same prompts "
              f"(version, prompts, num_predict: {sorted(asked, key=str)}); "
              "the rates are not comparable", file=sys.stderr)
    if as_json:
        print(json.dumps(results, sort_keys=True))
    else:
        print(table(results))
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "bench",
        help="measure load, prefill and decode with fixed prompts, the same on every node",
        description=(
            "Cold-load each model at the pipeline's context size and send fixed "
            f"prompts of about {', '.join(str(t) for _c, t in CASES)} tokens, "
            f"{NUM_PREDICT} tokens out, {REPEAT} runs each; report Ollama's own "
            "counters and where the model ended up resident. Refuses (exit 3) "
            "unless the generator is paused, and leaves it paused. --compare "
            "lines up saved runs from several nodes by model digest; it runs "
            "anywhere and touches no model host."
        ),
    )
    p.add_argument("--model", action="append", metavar="TAG",
                   help="a model to bench (repeatable; default: this node's "
                        "executor and planner)")
    p.add_argument("--repeat", type=int, default=REPEAT, metavar="N",
                   help=f"runs per case (default {REPEAT}); the median is reported")
    p.add_argument("--parallel", type=int, default=1, metavar="N",
                   help="requests in flight at once; set OLLAMA_NUM_PARALLEL >= N "
                        "first or they queue and the aggregate means nothing")
    p.add_argument("--num-ctx", dest="num_ctx", type=int, metavar="N",
                   help=f"override the context size (default {PLANNER_NUM_CTX} for "
                        f"the planner, {EXECUTOR_NUM_CTX} for everything else)")
    p.add_argument("--host", default=DEFAULT_HOST, metavar="URL",
                   help="Ollama (default $OLLAMA_HOST_URL, else the worker unit's, "
                        "else 127.0.0.1:11434)")
    p.add_argument("--out", metavar="FILE", help="also write the full result as JSON here")
    p.add_argument("--compare", nargs="+", metavar="FILE",
                   help="print saved results side by side instead of running")
    p.add_argument("--json", action="store_true", help="one JSON object on stdout")
    p.add_argument(
        "--db",
        default=db.DEFAULT_DB_PATH,
        metavar="P",
        help="database file (default: $SKETCHGEN_DB, else ~/sketchgen/sketchgen.db)",
    )
    p.set_defaults(func=cmd, _parser=p)
