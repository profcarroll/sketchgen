"""pair.py — what a node can see of the D12 pair, for the Console.

docs/plans/pooled-pair.md, Packet 1. In pooled mode the executor is neither in
this node's Ollama nor on one card: flux (the *head*) runs ``llama-server`` with
the model split across its own GPU and d12's, and d12 (the *partner*) runs only
``ggml-rpc-server``, its card lent to flux. :mod:`sketchgen.console` reads one
card, one Ollama and one worker, so without this the operator on flux sees half
the pool and the operator on d12 sees a stranger holding ten gigabytes.

:func:`block` answers one ``pair`` block for the console document. It always
has the same keys, null where the node is not in a pair (the GPU block does the
same for a node without a GPU, and the fixture test holds both to it):

- ``role``: ``head`` when ``SKETCHGEN_POOL_URL`` is set, ``partner`` when a
  ``ggml-rpc-server`` process is running here, otherwise null. The partner
  needs no configuration: a lent card says so even if nobody told the node.
- ``pool`` (head): the pool as llama-server reports it — ``/health``,
  ``/props``, ``/slots`` and ``/metrics`` (the pool runs with ``--metrics``).
- ``partner`` (head): the partner as it reports itself. The head runs ``ssh``
  to the partner's restricted key, whose forced command prints the partner's
  own console document (0.24 s, 3.5 KB, measured 2026-09-25), so no new code has
  to be deployed on the partner for the head to see it. A daemon thread
  refreshes it every :data:`PARTNER_TTL_S`, so the page's two-second poll never
  waits on SSH; a single-shot ``sketchgen console`` fetches it inline.
- ``link``: the interface the pair uses, its speed, bytes a second each way,
  and the RPC port (on the head, the tunnel's local end; on the partner, the
  server itself): whether it is listening and how many connections it holds.
- ``lent`` (partner): the ``ggml-rpc-server`` process, what it burns and holds,
  and whether a pool is connected to it.

Everything is read-only, needs no sudo, calls no model and raises nothing: a
source that does not answer is null, the way the rest of the console works.
Python 3.12, stdlib only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

#: The pool this node is the head of: llama-server's base URL. Setting it is
#: what makes a node a head; nothing else does.
POOL_URL_ENV = "SKETCHGEN_POOL_URL"
#: The ssh destination of the partner — an alias in this user's ~/.ssh/config
#: carrying the pair key (pooled-pair.md §0.1).
PARTNER_ENV = "SKETCHGEN_PAIR_PARTNER"
#: What to ask the partner for. With the pair key's forced command in place
#: the partner runs its own command and ignores this; it matters only for a
#: key without one.
PARTNER_COMMAND_ENV = "SKETCHGEN_PAIR_PARTNER_COMMAND"
DEFAULT_PARTNER_COMMAND = (
    "~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen console --json "
    "--db ~/sketchgen/sketchgen.db"
)
#: The interface the pair's traffic crosses: enp7s0 on the Linksys today,
#: thunderbolt0 once the cable is in (d12-pair.md §3). Unset, the default
#: route's interface, which is right for the LAN and wrong for the cable.
LINK_ENV = "SKETCHGEN_PAIR_LINK"
RPC_PORT_ENV = "SKETCHGEN_PAIR_RPC_PORT"
DEFAULT_RPC_PORT = 50052

#: How old the partner's document may get before the next read starts, and
#: how long one read may take. The read is ~0.3 s on the LAN; the timeout is
#: for a partner that is off, where ssh waits out its ConnectTimeout.
PARTNER_TTL_S = 5.0
PARTNER_TIMEOUT_S = 8.0
PARTNER_CONNECT_TIMEOUT_S = 3
#: The pool is on loopback; anything slower than this is a pool in trouble,
#: and the console has a 200 ms budget to keep.
POOL_TIMEOUT_S = 0.5
#: The second link reading a single-shot call takes, when there is no earlier
#: one to difference against.
LINK_SETTLE_S = 0.2

RPC_PROCESS = "ggml-rpc-server"
POOL_PROCESS = "llama-server"
#: Ollama 0.34 bundles llama.cpp and runs its model as ``llama-server`` from
#: its own library directory (``/usr/local/lib/ollama/llama-server`` on both
#: D12 boxes). Same basename as the pool, so the path is what tells them apart.
OLLAMA_PATH_MARK = "/ollama/"

_MB = 1_000_000.0


# ---------------------------------------------------------------------------
# Processes and sockets
# ---------------------------------------------------------------------------


def classify(argv0: str) -> str | None:
    """What a process is to the pair, from its ``argv[0]``: ``rpc``, ``pool``,
    ``runner`` (Ollama's own llama-server), or None."""
    base = os.path.basename(argv0)
    if base == RPC_PROCESS:
        return "rpc"
    if base == POOL_PROCESS:
        return "runner" if OLLAMA_PATH_MARK in argv0 else "pool"
    return None


def processes(proc: str = "/proc") -> dict[str, list[int]]:
    """Every pid, by what it is to the pair. One walk of /proc.

    Matched on ``argv[0]`` exactly, never on a word anywhere in the command
    line: a shell whose command mentions ``ggml-rpc-server`` is not one (the
    pkill self-match that bit twice on 2026-09-25).
    """
    found: dict[str, list[int]] = {"rpc": [], "pool": [], "runner": []}
    try:
        names = os.listdir(proc)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"{proc}/{name}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        argv0 = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
        kind = classify(argv0) if argv0 else None
        if kind is not None:
            found[kind].append(int(name))
    for pids in found.values():
        pids.sort()
    return found


def parse_tcp(text: str, port: int) -> tuple[bool, int]:
    """(listening, established) for one local port, from /proc/net/tcp{,6}."""
    listening, established = False, 0
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            local_port = int(parts[1].rsplit(":", 1)[1], 16)
        except (IndexError, ValueError):
            continue
        if local_port != port:
            continue
        if parts[3] == "0A":
            listening = True
        elif parts[3] == "01":
            established += 1
    return listening, established


def rpc_socket(port: int, proc: str = "/proc") -> tuple[bool | None, int | None]:
    """Whether something listens on ``port`` here, and how many connections it
    has accepted. On the head that is the tunnel's local end with the pool
    connected to it; on the partner, ggml-rpc-server with sshd connected."""
    texts = []
    for name in ("tcp", "tcp6"):
        try:
            with open(f"{proc}/net/{name}", encoding="ascii", errors="replace") as handle:
                texts.append(handle.read())
        except OSError:
            continue
    if not texts:
        return None, None
    listening, established = False, 0
    for text in texts:
        one, two = parse_tcp(text, port)
        listening = listening or one
        established += two
    return listening, established


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------


def default_iface(route_text: str | None = None) -> str | None:
    """The default route's interface, from /proc/net/route."""
    if route_text is None:
        try:
            with open("/proc/net/route", encoding="ascii") as handle:
                route_text = handle.read()
        except OSError:
            return None
    for line in route_text.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "00000000":
            return parts[0]
    return None


def iface_bytes(iface: str, dev_text: str | None = None) -> tuple[int, int] | None:
    """(rx, tx) bytes for one interface, from /proc/net/dev."""
    if dev_text is None:
        try:
            with open("/proc/net/dev", encoding="ascii") as handle:
                dev_text = handle.read()
        except OSError:
            return None
    for line in dev_text.splitlines():
        name, sep, rest = line.partition(":")
        if sep and name.strip() == iface:
            fields = rest.split()
            try:
                return int(fields[0]), int(fields[8])
            except (IndexError, ValueError):
                return None
    return None


def iface_speed(iface: str) -> int | None:
    """Negotiated Mb/s. A tunnel, a down link or a virtual NIC reads -1 or
    refuses; both are None, not a number that looks like a speed."""
    try:
        with open(f"/sys/class/net/{iface}/speed", encoding="ascii") as handle:
            value = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return value if value > 0 else None


#: iface -> (monotonic, rx, tx) from the previous call, for rates.
_LINK_PREV: dict[str, tuple[float, int, int]] = {}


def link_rates(iface: str, *, settle: bool, now: Callable[[], float] = time.monotonic,
               read: Callable[[str], tuple[int, int] | None] = iface_bytes,
               sleep: Callable[[float], None] = time.sleep) -> tuple[float | None, float | None]:
    """Megabytes a second in and out since the last call (or over a short
    settle, for a single shot with nothing to difference against)."""
    current = read(iface)
    if current is None:
        return None, None
    at = now()
    previous = _LINK_PREV.get(iface)
    if previous is None and settle:
        _LINK_PREV[iface] = (at, *current)
        sleep(LINK_SETTLE_S)
        return link_rates(iface, settle=False, now=now, read=read, sleep=sleep)
    _LINK_PREV[iface] = (at, *current)
    if previous is None:
        return None, None
    span = at - previous[0]
    if span <= 0:
        return None, None
    rx = max(0, current[0] - previous[1]) / span / _MB
    tx = max(0, current[1] - previous[2]) / span / _MB
    return round(rx, 2), round(tx, 2)


# ---------------------------------------------------------------------------
# The pool
# ---------------------------------------------------------------------------


def _get(url: str, timeout: float) -> tuple[int, str]:
    """GET; (status, body). An HTTP error status is an answer, not an exception:
    llama-server says 503 on /health while it is loading."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


def parse_metrics(text: str) -> dict[str, float]:
    """``llamacpp:name value`` lines, prefix dropped; comments ignored."""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        try:
            values[name.split(":", 1)[-1]] = float(value)
        except ValueError:
            continue
    return values


def _rate(count: float | None, seconds: float | None) -> float | None:
    if not count or not seconds:
        return None
    return round(count / seconds, 1)


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def blob_tag(model_path: str | None, model_dirs: tuple[str, ...] | None = None) -> str | None:
    """The Ollama tag whose model layer is this blob, if there is one.

    The pool serves the executor's GGUF straight out of Ollama's blob store
    (pooled-pair.md §0.2), so the path is ``…/blobs/sha256-<digest>`` and the
    manifest that names it says which tag the operator knows it as.
    """
    if not model_path:
        return None
    base = os.path.basename(model_path)
    if not base.startswith("sha256-"):
        return None
    digest = "sha256:" + base[len("sha256-"):]
    if model_dirs is None:
        model_dirs = (os.path.dirname(os.path.dirname(model_path)),)
    for root in model_dirs:
        manifests = os.path.join(root, "manifests")
        for dirpath, dirs, files in os.walk(manifests):
            dirs.sort()
            for name in sorted(files):
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8") as handle:
                        layers = json.load(handle).get("layers") or []
                except (OSError, ValueError, AttributeError):
                    continue
                if any(layer.get("digest") == digest for layer in layers
                       if isinstance(layer, dict)):
                    rel = os.path.relpath(path, manifests).split(os.sep)
                    # registry.ollama.ai/library/qwen3-coder/30b -> qwen3-coder:30b,
                    # hf.co/unsloth/…-GGUF/Q5_K_S -> hf.co/unsloth/…-GGUF:Q5_K_S,
                    # which is how `ollama list` names each.
                    if len(rel) == 4 and rel[0] == "registry.ollama.ai" and rel[1] == "library":
                        return f"{rel[2]}:{rel[3]}"
                    return "/".join(rel[:-1]) + ":" + rel[-1]
    return None


_TAG_CACHE: dict[str, str | None] = {}

#: url -> (monotonic, task, n_decoded) from the previous read, for the live rate.
_SLOT_PREV: dict[str, tuple[float, int, int]] = {}


def pool_block(url: str, *, pids: list[int], apps: list[dict[str, Any]],
               get: Callable[[str, float], tuple[int, str]] = _get,
               now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The pool as llama-server reports it. Never raises."""
    out = skeleton()["pool"]
    out["url"] = url
    base = url.rstrip("/")
    try:
        status, _ = get(base + "/health", POOL_TIMEOUT_S)
    except (OSError, ValueError):
        status = None
    if status is None:
        out["state"] = "down"
    elif status == 503:
        out["state"] = "loading"
    elif status != 200:
        out["state"] = "down"
    pid = pids[0] if pids else None
    out["pid"] = pid
    for app in apps:
        if pid is not None and app.get("pid") == pid:
            out["vram_mb"] = app.get("used_mb")
    if out["state"] is not None:
        return out

    try:
        props = _json(get(base + "/props", POOL_TIMEOUT_S)[1]) or {}
    except (OSError, ValueError):
        props = {}
    if isinstance(props, dict):
        build = str(props.get("build_info") or "") or None
        out["build"] = build.split("-", 1)[0] if build else None
        path = props.get("model_path")
        if path:
            if path not in _TAG_CACHE:
                _TAG_CACHE[path] = blob_tag(path)
            out["model"] = _TAG_CACHE[path] or os.path.basename(str(path))[:19]
        settings = props.get("default_generation_settings") or {}
        if isinstance(settings, dict) and isinstance(settings.get("n_ctx"), int):
            out["n_ctx"] = settings["n_ctx"]

    try:
        slots = _json(get(base + "/slots", POOL_TIMEOUT_S)[1])
    except (OSError, ValueError):
        slots = None
    busy = None
    if isinstance(slots, list):
        busy = next((s for s in slots if isinstance(s, dict) and s.get("is_processing")), None)
    out["state"] = "decoding" if busy else "idle"
    if busy:
        task = busy.get("id_task")
        tokens = (busy.get("next_token") or [{}])
        decoded = tokens[0].get("n_decoded") if tokens and isinstance(tokens[0], dict) else None
        out["task"] = task if isinstance(task, int) else None
        out["n_decoded"] = decoded if isinstance(decoded, int) else None
        out["prompt_tokens"] = busy.get("n_prompt_tokens") if isinstance(
            busy.get("n_prompt_tokens"), int) else None
        at = now()
        previous = _SLOT_PREV.get(url)
        if (previous is not None and out["task"] is not None and out["n_decoded"] is not None
                and previous[1] == out["task"] and out["n_decoded"] > previous[2]
                and at > previous[0]):
            out["live_decode_tok_s"] = round((out["n_decoded"] - previous[2]) / (at - previous[0]), 1)
        if out["task"] is not None and out["n_decoded"] is not None:
            _SLOT_PREV[url] = (at, out["task"], out["n_decoded"])
    else:
        _SLOT_PREV.pop(url, None)

    try:
        status, text = get(base + "/metrics", POOL_TIMEOUT_S)
    except (OSError, ValueError):
        status, text = None, ""
    if status == 200:
        metrics = parse_metrics(text)
        out["decode_tok_s"] = _rate(metrics.get("tokens_predicted_total"),
                                    metrics.get("tokens_predicted_seconds_total"))
        out["prefill_tok_s"] = _rate(metrics.get("prompt_tokens_total"),
                                     metrics.get("prompt_seconds_total"))
        if "tokens_predicted_total" in metrics:
            out["tokens_out"] = int(metrics["tokens_predicted_total"])
        if "prompt_tokens_total" in metrics:
            out["tokens_in"] = int(metrics["prompt_tokens_total"])
    return out


# ---------------------------------------------------------------------------
# The partner, over ssh
# ---------------------------------------------------------------------------


def fetch_partner(host: str, command: str, timeout: float = PARTNER_TIMEOUT_S) -> dict[str, Any]:
    """The partner's own console document. Raises OSError on any failure."""
    argv = [
        "ssh", "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={PARTNER_CONNECT_TIMEOUT_S}",
        "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=2",
        host, command,
    ]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"ssh {host}: {exc}") from exc
    if done.returncode != 0:
        tail = (done.stderr or "").strip().splitlines()
        raise OSError(tail[-1] if tail else f"ssh {host} exited {done.returncode}")
    document = _json(done.stdout)
    if not isinstance(document, dict) or not isinstance(document.get("node"), dict):
        raise OSError(f"ssh {host}: the reply was not a console document")
    return document


class PartnerCache:
    """The last partner document, who read it and when; refreshed off-thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.document: dict[str, Any] | None = None
        self.read_at: float | None = None
        self.tried_at: float | None = None
        self.error: str | None = None
        self.refreshing = False

    def _refresh(self, host: str, command: str,
                 fetch: Callable[[str, str], dict[str, Any]],
                 now: Callable[[], float]) -> None:
        try:
            document = fetch(host, command)
        except Exception as exc:  # noqa: BLE001 - a failed read is shown, never raised
            with self.lock:
                self.error = str(exc) or type(exc).__name__
                self.tried_at = now()
                self.refreshing = False
            return
        with self.lock:
            self.document = document
            self.error = None
            self.read_at = self.tried_at = now()
            self.refreshing = False

    def snapshot(self, host: str, command: str, *, sync: bool,
                 fetch: Callable[[str, str], dict[str, Any]] = fetch_partner,
                 now: Callable[[], float] = time.monotonic,
                 spawn: Callable[[Callable[[], None]], None] | None = None,
                 ) -> tuple[dict[str, Any] | None, float | None, str | None]:
        """(document, age in seconds, error). Starts a refresh when stale."""
        with self.lock:
            stale = self.tried_at is None or now() - self.tried_at >= PARTNER_TTL_S
            start = stale and not self.refreshing
            if start:
                self.refreshing = True
        if start:
            job = lambda: self._refresh(host, command, fetch, now)  # noqa: E731
            if sync:
                job()
            elif spawn is not None:
                spawn(job)
            else:
                threading.Thread(target=job, name="pair-partner", daemon=True).start()
        with self.lock:
            age = round(now() - self.read_at, 1) if self.read_at is not None else None
            return self.document, age, self.error


_PARTNER = PartnerCache()


def partner_view(document: dict[str, Any] | None) -> dict[str, Any]:
    """The partner's own document, cut to what the head's panel shows.

    A partner deployed with Packet 1 reports ``pair.lent`` itself. One still on
    an older build (d12 on a72f076, the day this was written) does not, so the
    rpc process is found in its top three by CPU and its VRAM in ``gpu.apps``
    when that exists — which, on that build, it does not: the VRAM it holds is
    then unknown, and the card's total is still true.
    """
    view = skeleton()["partner"]
    if not isinstance(document, dict):
        return view
    node = document.get("node") or {}
    view["node"] = node.get("shape")
    gpu = node.get("gpu") or {}
    view["gpu"] = {
        "name": gpu.get("name"),
        "vram_mb": {
            "total": (gpu.get("vram_mb") or {}).get("total"),
            "used": (gpu.get("vram_mb") or {}).get("used"),
        },
        "util_pct": gpu.get("util_pct"),
    }
    lent = (document.get("pair") or {}).get("lent") or {}
    if lent.get("rpc_pid") is not None:
        view["rpc"] = {
            "pid": lent.get("rpc_pid"),
            "cpu_pct": lent.get("cpu_pct"),
            "vram_mb": lent.get("vram_mb"),
            "serving": lent.get("serving"),
        }
    else:
        for row in node.get("top") or []:
            if str(row.get("cmd") or "").split(" ")[0] == RPC_PROCESS:
                view["rpc"]["pid"] = row.get("pid")
                view["rpc"]["cpu_pct"] = row.get("cpu_pct")
                break
        for app in gpu.get("apps") or []:
            if app.get("role") == "rpc":
                view["rpc"]["pid"] = view["rpc"]["pid"] or app.get("pid")
                view["rpc"]["vram_mb"] = app.get("used_mb")
    worker = document.get("worker") or {}
    view["control"] = worker.get("control")
    view["reason"] = worker.get("reason")
    view["resident"] = [str(entry.get("name")) for entry in
                        ((document.get("model") or {}).get("resident") or [])
                        if isinstance(entry, dict) and entry.get("name")]
    return view


# ---------------------------------------------------------------------------
# The block
# ---------------------------------------------------------------------------


def skeleton() -> dict[str, Any]:
    """Every key the block ever has, all null: the shape of a node not in a pair."""
    return {
        "role": None,
        "pool": {
            "url": None, "state": None, "build": None, "model": None, "n_ctx": None,
            "pid": None, "vram_mb": None, "task": None, "prompt_tokens": None,
            "n_decoded": None, "live_decode_tok_s": None, "decode_tok_s": None,
            "prefill_tok_s": None, "tokens_in": None, "tokens_out": None,
        },
        "partner": {
            "host": None, "reachable": None, "age_s": None, "error": None, "node": None,
            "gpu": {"name": None, "vram_mb": {"total": None, "used": None}, "util_pct": None},
            "rpc": {"pid": None, "cpu_pct": None, "vram_mb": None, "serving": None},
            "control": None, "reason": None, "resident": [],
        },
        "link": {
            "iface": None, "speed_mbps": None, "rx_mb_s": None, "tx_mb_s": None,
            "rpc_port_open": None, "rpc_connections": None,
        },
        "lent": {"rpc_pid": None, "cpu_pct": None, "vram_mb": None, "serving": None},
    }


def block(*, apps: list[dict[str, Any]] | None = None,
          procs: Mapping[str, list[int]] | None = None,
          proc_cpu: Callable[[int], float | None] = lambda _pid: None,
          sync: bool = False,
          env: Mapping[str, str] | None = None,
          fetch: Callable[[str, str], dict[str, Any]] = fetch_partner,
          cache: PartnerCache | None = None) -> dict[str, Any]:
    """The console document's ``pair`` block. See the module docstring.

    ``apps`` is ``node.gpu.apps`` (the VRAM each process holds), ``procs``
    :func:`processes` (the collector walks /proc once for both), ``proc_cpu``
    a pid's CPU over the collector's own sample window, and ``sync`` says a
    single shot: read the partner and the link now rather than off-thread.
    """
    env = os.environ if env is None else env
    apps = apps or []
    procs = procs if procs is not None else processes()
    out = skeleton()
    url = (env.get(POOL_URL_ENV) or "").strip()
    if url:
        out["role"] = "head"
    elif procs.get("rpc"):
        out["role"] = "partner"
    else:
        return out

    try:
        port = int(env.get(RPC_PORT_ENV) or DEFAULT_RPC_PORT)
    except ValueError:
        port = DEFAULT_RPC_PORT
    if out["role"] == "head" and sync:
        # A single shot reads the pool on both sides of the link's settle, so
        # the request in flight has a live rate: the rate is two reads apart.
        pool_block(url, pids=list(procs.get("pool") or []), apps=apps)
    iface = (env.get(LINK_ENV) or "").strip() or default_iface()
    link = out["link"]
    link["iface"] = iface
    if iface:
        link["speed_mbps"] = iface_speed(iface)
        link["rx_mb_s"], link["tx_mb_s"] = link_rates(iface, settle=sync)
    link["rpc_port_open"], link["rpc_connections"] = rpc_socket(port)

    if out["role"] == "partner":
        pid = procs["rpc"][0]
        lent = out["lent"]
        lent["rpc_pid"] = pid
        lent["cpu_pct"] = proc_cpu(pid)
        lent["vram_mb"] = next((app.get("used_mb") for app in apps
                                if app.get("pid") == pid), None)
        connections = link["rpc_connections"]
        lent["serving"] = None if connections is None else connections > 0
        return out

    out["pool"] = pool_block(url, pids=list(procs.get("pool") or []), apps=apps)
    host = (env.get(PARTNER_ENV) or "").strip()
    if host:
        command = (env.get(PARTNER_COMMAND_ENV) or "").strip() or DEFAULT_PARTNER_COMMAND
        document, age, error = (cache or _PARTNER).snapshot(host, command, sync=sync,
                                                            fetch=fetch)
        view = partner_view(document)
        view["host"] = host
        view["age_s"] = age
        view["error"] = error
        view["reachable"] = None if (document is None and error is None) else error is None
        out["partner"] = view
    return out
