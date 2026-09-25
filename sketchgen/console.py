"""The console collector: one JSON document per call, and nothing else.

Packet 4.1 of the sketchgen build. :func:`collect` is the only thing the web UI
(packet 4.2) calls; `sketchgen console` prints what it returns. The document's
key names are a contract between the two packets and are not to be renamed.

What it reads, all of it read-only and none of it needing sudo:

  /proc/stat        per-core and total CPU, sampled twice (see below)
  /proc/meminfo     memory and swap, split the way htop splits them
  /proc/loadavg     load, against the core count
  /proc/uptime      uptime
  /proc/<pid>/stat  per-process CPU and RSS, for the top three
  /proc/<pid>/cmdline  the command line of those three, **redacted** (see
                    :func:`redact_command`)
  os.statvfs("/")   disk, plus a walk of the two big tenants (the model blobs
                    and the Playwright browser) so the console can name them
  GET /api/ps       what Ollama has resident, and /api/version
  nvidia-smi        the card, and the VRAM each process on it holds
  the D12 pair      when this node is in one: the pool's own endpoints, the
                    partner's console document over ssh, the link's counters
                    (:mod:`sketchgen.pair`, docs/plans/pooled-pair.md)
  the app database  the control row, the attempts, the entries, the jobs, and
                    the worker's own step-by-step account of itself (the
                    ``activity`` table, migration 008)
  /proc/<pid>       whether the worker that wrote the open step is still there

Every one of those is optional. A source that is missing, unreadable or
unreachable becomes ``null`` in the document; nothing here raises because the
laptop is not the node. The collector calls no model, writes nothing, and is
safe to run while a job holds the slot.

**Per-core percentages need two samples.** ``/proc/stat`` counts jiffies since
boot, so a percentage is a difference between two readings. A single-shot call
takes them 100 ms apart, which is where most of its time goes. A caller polling
in a loop (``--watch``, or the web server) passes the previous document as
``prev``; the collector then differences against the raw sample it kept from
that call and does not sleep at all. The raw jiffy counters are deliberately not
in the document — they are not part of the contract — so ``prev`` is used as a
signal that the cached sample is the caller's, not as the sample itself.

**The process status card is read by one function, deliberately.**
:func:`activity` is called by :func:`collect` for the document *and* by the
operator UI's two-second poll for the card, so the page and the poll cannot end
up saying different things about what the worker is doing. It is also where the
card's ``state`` is decided — paused, gone, stalled, running, idle, unknown —
rather than in a template, because that reading takes the control row, a stat
of ``/proc/<pid>`` and the age of the open step, and none of those belong in
markup.

Python 3.12, stdlib only. Timestamps are UTC, ISO 8601 with a trailing Z.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sketchgen import db, pair
from sketchgen.worker import (DEFAULT_HOST, DEFAULT_JOBS_DIR, FENCED_STEP, human_gap,
                              node_shape)

__all__ = [
    "ACTIVITY_IDLE_STEPS",
    "ACTIVITY_MEDIAN_MIN",
    "ACTIVITY_MEDIAN_SAMPLE",
    "ACTIVITY_STALLED_S",
    "DEFAULT_HOST",
    "DEFAULT_JOBS_DIR",
    "FUNNEL_NAMES",
    "RATE_PER_HOUR_16_96",
    "RATE_PER_HOUR_4_24",
    "activity",
    "collect",
    "redact_command",
    "render_text",
    "resident_entry",
    "submissions",
]

# --- cost ------------------------------------------------------------------
#
# examples/week11-self-hosted-ai/cost.py in the class repo prices the demo node
# at its trial shape: VM.Standard.A1.Flex 16 OCPU / 96 GB, always on, is
# US$166.44/month there (16 x $0.01 per OCPU-hour + 96 x $0.0015 per GB-hour,
# over the 730 hours cost.py calls a month, with the 4/24 Always Free allowance
# already deducted). 166.44 / 730 = 0.228 US$/hour, which is the rate below.
# SKETCHGEN_RATE_PER_HOUR overrides it when the price list moves.
RATE_PER_HOUR_16_96 = float(os.environ.get("SKETCHGEN_RATE_PER_HOUR", "0.228"))

# The 4 OCPU / 24 GB shape is exactly the Always Free allowance: Oracle bills
# nothing for it, at any duty cycle, so a sketch made there costs zero dollars
# however long it took. That is the whole point of the second column — it is the
# shape the course survives on after the trial credit ends (spec §9), and the
# honest number for it is 0.00, not a smaller positive one.
RATE_PER_HOUR_4_24 = 0.0

# --- storage ---------------------------------------------------------------
#
# Compute is not the only meter that costs money once the trial credit lapses.
# OCI's Always Free tier includes 200 GB of *total* block-volume storage (boot
# volume plus any attached volumes); past that, storage bills at roughly
# US$0.0255 per GB-month for the balanced default. The demo node runs a ~140 GB
# boot disk plus a block volume the model blobs were moved onto (OLLAMA_MODELS,
# /mnt/models), so the operator now has two volumes to weigh and a bill that is
# only zero while the two together stay under 200 GB. SKETCHGEN_STORAGE_RATE and
# SKETCHGEN_FREE_TIER_STORAGE_GB override both when Oracle's price list moves.
FREE_TIER_STORAGE_GB = float(os.environ.get("SKETCHGEN_FREE_TIER_STORAGE_GB", "200"))

# nvidia-smi costs about 65 ms and the console refreshes every two seconds, so
# the GPU answer is cached for this long. SKETCHGEN_GPU_TTL_S to tune it.
GPU_TTL_S = float(os.environ.get("SKETCHGEN_GPU_TTL_S", "5"))
STORAGE_RATE_USD_GB_MONTH = float(os.environ.get("SKETCHGEN_STORAGE_RATE", "0.0255"))

#: The funnel's rows, in the order the console shows them.
FUNNEL_NAMES = (
    "generated",
    "revised",
    "passed_gate",
    "published",
    "held",
    "rejected",
    "failed_kept",
    # Entries the operator took off their lists without deleting anything
    # (lineage ledger §5.1). There is no archived JOB state — the job reuses
    # 'rejected' — so this row's "now" is always zero: nothing is ever in
    # flight towards being archived, a person just does it.
    "archived",
    "children",
)

#: Job states that mean the job is over. The per-sketch population.
FINISHED_STATES = ("held", "published", "rejected", "failed")

#: Job states that mean an attempt is in flight right now.
IN_FLIGHT_STATES = ("executing", "gating", "repairing")

#: An argument containing any of these is replaced wholesale, and so is the
#: argument after it: ``--api-key sk-…`` hides both halves.
SECRET_WORDS = ("key", "token", "secret", "password", "passwd", "credential")
REDACTED = "…"

#: How much of a command line survives after the executable's basename.
CMD_MAX_CHARS = 60

#: Reading gate reports and sketch files is the only part of the per-sketch
#: block that touches the disk, so it is capped at the most recent N finished
#: jobs. Averages of gate_s and sketch_lines are over that sample; the token,
#: wall and attempt numbers come from SQL and are over the whole population.
FILE_SAMPLE_LIMIT = 25

#: A resident model plus a non-Ollama process burning at least this much of one
#: core means somebody is decoding. See :func:`_slot`.
BUSY_CPU_PCT = 20.0

#: Directories the disk row names, in the order they are tried. OLLAMA_MODELS
#: wins when the service sets it; /mnt/models is where the demo node's block
#: volume mounts; the last two are Ollama's system and per-user defaults.
MODEL_DIRS = (
    os.environ.get("OLLAMA_MODELS", ""),
    "/mnt/models",
    "/usr/share/ollama/.ollama/models",
    str(Path.home() / ".ollama" / "models"),
)
CHROMIUM_DIR = str(Path.home() / ".cache" / "ms-playwright")

_CLOCK_TICKS = float(os.sysconf("SC_CLK_TCK")) if hasattr(os, "sysconf") else 100.0
_PAGE_SIZE = float(os.sysconf("SC_PAGE_SIZE")) if hasattr(os, "sysconf") else 4096.0
_KIB = 1024.0
_GIB = 1024.0 ** 3


# ---------------------------------------------------------------------------
# Small safe readers
# ---------------------------------------------------------------------------


def _text(path: str | os.PathLike[str]) -> str | None:
    """A file's text, or None if it is missing or unreadable. Never raises."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except (OSError, ValueError):
        return None


def _json_file(path: str | os.PathLike[str]) -> dict[str, Any] | None:
    raw = _text(path)
    if raw is None:
        return None
    try:
        loaded = json.loads(raw)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _http_json(url: str, timeout: float = 1.5) -> dict[str, Any] | None:
    """GET one JSON object, or None. An unreachable host is an answer, not an
    error: the console renders with the model block empty."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError, TimeoutError):
        return None
    return body if isinstance(body, dict) else None


def _round(value: Any, places: int = 2) -> float | None:
    """Round, turning anything not finite into None so the JSON stays valid."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return round(number, places)


def _utc_z(raw: str | None) -> str | None:
    """Normalise an ISO timestamp to whole seconds with a Z, or None."""
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return str(raw)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(stamp: str | None) -> float | None:
    """Seconds from a UTC stamp until now, or None if it does not parse."""
    normalised = _utc_z(stamp)
    if normalised is None:
        return None
    try:
        moment = datetime.strptime(normalised, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - moment).total_seconds()


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def redact_command(argv: list[str]) -> str:
    """One process's command line, safe to show and short enough to fit.

    Course policy (CLAUDE.md, spec §6): the console is a web page, a web page
    gets screenshotted into a slide deck, and a command line on this node can
    carry an API key. So: the executable is reduced to its basename, any
    argument containing ``key``/``token``/``secret``/``password`` is replaced by
    an ellipsis, the argument *after* such a flag is replaced too (that is where
    ``--api-key sk-live-…`` keeps the value), and what is left is truncated to
    :data:`CMD_MAX_CHARS` characters. Nothing here is reversible.
    """
    if not argv:
        return ""
    words = [str(word) for word in argv if word != ""]
    if not words:
        return ""
    name = os.path.basename(words[0]) or words[0]
    safe: list[str] = []
    hide_next = False
    for word in words[1:]:
        lowered = word.lower()
        if hide_next:
            safe.append(REDACTED)
            # The hidden word was a value, not a flag, unless it looks like one:
            # `--api-key --token v` keeps hiding, `hunter2secretvalue` does not
            # drag the argument behind it into the dark with it.
            hide_next = (
                word.startswith("-")
                and "=" not in word
                and any(part in lowered for part in SECRET_WORDS)
            )
            continue
        if any(part in lowered for part in SECRET_WORDS):
            safe.append(REDACTED)
            # A flag spelled --api-key carries its value in the next argument.
            hide_next = word.startswith("-") and "=" not in word
            continue
        safe.append(word)
    rest = " ".join(safe)
    if len(rest) > CMD_MAX_CHARS:
        rest = rest[: CMD_MAX_CHARS - 1].rstrip() + REDACTED
    return f"{name} {rest}".strip()


# ---------------------------------------------------------------------------
# /proc sampling
# ---------------------------------------------------------------------------


@dataclass
class _Sample:
    """One instant of /proc/stat and every process's CPU time."""

    at: float = 0.0
    total: tuple[int, ...] = ()
    cores: dict[int, tuple[int, ...]] = field(default_factory=dict)
    procs: dict[int, int] = field(default_factory=dict)


#: The last raw sample this process took. Not part of the document (the
#: contract has no room for jiffy counters) and not read unless the caller
#: passes ``prev``, which is how it says "I polled you a moment ago".
_LAST_SAMPLE: _Sample | None = None


def _read_stat() -> tuple[tuple[int, ...], dict[int, tuple[int, ...]]]:
    total: tuple[int, ...] = ()
    cores: dict[int, tuple[int, ...]] = {}
    raw = _text("/proc/stat")
    if raw is None:
        return total, cores
    for line in raw.splitlines():
        if not line.startswith("cpu"):
            continue
        head, _, rest = line.partition(" ")
        try:
            values = tuple(int(word) for word in rest.split())
        except ValueError:
            continue
        if head == "cpu":
            total = values
        elif head[3:].isdigit():
            cores[int(head[3:])] = values
    return total, cores


def _read_proc_times() -> dict[int, int]:
    """pid -> utime+stime in jiffies, for every process we may read."""
    times: dict[int, int] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return times
    for name in names:
        if not name.isdigit():
            continue
        raw = _text(f"/proc/{name}/stat")
        if raw is None:
            continue
        fields = _stat_fields(raw)
        if fields is None:
            continue
        try:
            times[int(name)] = int(fields[11]) + int(fields[12])
        except (IndexError, ValueError):
            continue
    return times


def _stat_fields(raw: str) -> list[str] | None:
    """The fields of /proc/<pid>/stat from ``state`` on.

    ``comm`` is in parentheses and may contain spaces and parentheses of its
    own, so the split is on the *last* ``)``. Index 0 here is field 3 of the
    proc(5) table: utime (14) is index 11, stime (15) is 12, rss (24) is 21.
    """
    close = raw.rfind(")")
    if close < 0:
        return None
    return raw[close + 2 :].split()


def _sample() -> _Sample:
    total, cores = _read_stat()
    return _Sample(at=time.monotonic(), total=total, cores=cores,
                   procs=_read_proc_times())


def _busy_and_total(values: tuple[int, ...]) -> tuple[int, int]:
    """htop's split: everything except idle and iowait counts as busy."""
    if not values:
        return 0, 0
    total = sum(values[:8]) if len(values) >= 8 else sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total - idle, total


def _cpu_percentages(before: _Sample, after: _Sample) -> tuple[list[float], float | None]:
    per_core: list[float] = []
    for index in sorted(after.cores):
        old = before.cores.get(index)
        new = after.cores[index]
        if not old:
            continue
        busy_old, total_old = _busy_and_total(old)
        busy_new, total_new = _busy_and_total(new)
        span = total_new - total_old
        per_core.append(
            round(100.0 * (busy_new - busy_old) / span, 1) if span > 0 else 0.0
        )
    busy_old, total_old = _busy_and_total(before.total)
    busy_new, total_new = _busy_and_total(after.total)
    span = total_new - total_old
    overall = round(100.0 * (busy_new - busy_old) / span, 1) if span > 0 else None
    return per_core, overall


def _meminfo() -> dict[str, float]:
    values: dict[str, float] = {}
    raw = _text("/proc/meminfo")
    if raw is None:
        return values
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            values[key] = float(parts[0])
        except ValueError:
            continue
    return values


def _mem_block(info: dict[str, float]) -> tuple[dict[str, float | None], dict[str, float | None]]:
    """Memory and swap in MiB, split the way htop splits them: page cache
    (buffers + Cached + SReclaimable) is shown apart from what is really in
    use, because 60 GB of cached model blobs is not 60 GB of pressure."""
    def mb(key: str) -> float | None:
        return _round(info[key] / _KIB, 1) if key in info else None

    total = info.get("MemTotal")
    free = info.get("MemFree")
    cache_kb = (
        info.get("Buffers", 0.0) + info.get("Cached", 0.0)
        + info.get("SReclaimable", 0.0)
    )
    used = None
    if total is not None and free is not None:
        used = _round((total - free - cache_kb) / _KIB, 1)
    mem = {
        "total": mb("MemTotal"),
        "used": used,
        "cache": _round(cache_kb / _KIB, 1) if info else None,
        "free": mb("MemFree"),
        "available": mb("MemAvailable"),
    }
    swap_total = info.get("SwapTotal")
    swap_free = info.get("SwapFree")
    swap = {
        "total": mb("SwapTotal"),
        "used": (
            _round((swap_total - swap_free) / _KIB, 1)
            if swap_total is not None and swap_free is not None
            else None
        ),
    }
    return mem, swap


def _du_gb(path: str | None, limit: int = 50000) -> float | None:
    """Apparent size of a directory tree in GiB, or None if it is not there.

    Bounded at ``limit`` files so an unexpected tree cannot blow the 200 ms
    budget; the two trees this is pointed at hold 53 and 485 files (measured on
    the node, 2026-09-14, about 2 ms each).
    """
    if not path or not os.path.isdir(path):
        return None
    total = 0
    seen = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
            seen += 1
            if seen >= limit:
                return _round(total / _GIB, 2)
    return _round(total / _GIB, 2)


def _model_dir() -> str | None:
    """The first of MODEL_DIRS that exists, or None if the store is not here."""
    for candidate in MODEL_DIRS:
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def _volume(path: str | None) -> dict[str, float | None]:
    """total/used/free GiB of the filesystem `path` sits on (Nones if unknown)."""
    total = used = free = None
    if path:
        try:
            stats = os.statvfs(path)
            block = float(stats.f_frsize)
            total = _round(stats.f_blocks * block / _GIB, 1)
            free = _round(stats.f_bavail * block / _GIB, 1)
            used = _round((stats.f_blocks - stats.f_bfree) * block / _GIB, 1)
        except OSError:  # pragma: no cover - a live mount always statvfs's
            total = used = free = None
    return {"total": total, "used": used, "free": free}


def _same_filesystem(a: str, b: str) -> bool:
    """True when two paths live on the same mount (same st_dev)."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:  # pragma: no cover - both paths exist when this is asked
        return False


def _free_tier_drops(
    catalog: list[dict[str, Any]] | None, need_gb: float
) -> list[dict[str, Any]]:
    """One way to get under a free-tier model budget: the fewest models,
    largest first, whose sizes sum to at least `need_gb`. Empty when nothing
    has to go, or when the catalogue is unavailable (a dead Ollama host)."""
    if need_gb <= 0 or not catalog:
        return []
    ordered = sorted(
        (m for m in catalog if m.get("size_gb")),
        key=lambda m: m["size_gb"],
        reverse=True,
    )
    drops: list[dict[str, Any]] = []
    freed = 0.0
    for model in ordered:
        if freed >= need_gb:
            break
        drops.append({"name": model["name"], "size_gb": model["size_gb"]})
        freed += model["size_gb"]
    return drops


def _storage_cost(
    boot_total: float | None,
    model_volume: dict[str, float | None],
    models_gb: float | None,
    catalog: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """What the block storage costs, and what dropping back to the Always Free
    allowance would force off the model volume.

    Both volumes are OCI block volumes, so the bill is on their *combined*
    provisioned size above FREE_TIER_STORAGE_GB. Dropping to free tier means the
    model volume can be no larger than what the boot disk leaves under that
    allowance; anything the models use beyond that has to go."""
    separate = model_volume.get("separate") is True
    mv_total = model_volume.get("total")
    block_gb = None
    if boot_total is not None:
        block_gb = boot_total + (mv_total if separate and mv_total else 0.0)
    billable = usd = None
    if block_gb is not None:
        billable = max(0.0, block_gb - FREE_TIER_STORAGE_GB)
        usd = billable * STORAGE_RATE_USD_GB_MONTH
    free_tier_model_gb = over = None
    would_drop: list[dict[str, Any]] = []
    if boot_total is not None:
        free_tier_model_gb = max(0.0, FREE_TIER_STORAGE_GB - boot_total)
        over = max(0.0, (models_gb or 0.0) - free_tier_model_gb)
        would_drop = _free_tier_drops(catalog, over)
    return {
        "block_gb": _round(block_gb, 1),
        "free_tier_gb": _round(FREE_TIER_STORAGE_GB, 1),
        "billable_gb": _round(billable, 1),
        "rate_usd_gb_month": STORAGE_RATE_USD_GB_MONTH,
        "usd_month": _round(usd, 2),
        "free_tier_model_gb": _round(free_tier_model_gb, 1),
        "over_free_tier_gb": _round(over, 1),
        "would_drop": would_drop,
    }


def _disk_block(catalog: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The boot disk, the volume the model blobs live on, and the money.

    ``total``/``used``/``free`` describe the boot disk (``/``) as they always
    have. ``model_volume`` is the filesystem the blobs actually sit on now that
    they can be moved off the boot disk; when it is a separate mount its
    ``separate`` flag is true and it carries its own total/used/free."""
    boot = _volume("/")
    model_dir = _model_dir()
    if model_dir is not None:
        model_volume = _volume(model_dir)
        model_volume["separate"] = _same_filesystem(model_dir, "/") is False
    else:
        model_volume = {"total": None, "used": None, "free": None, "separate": False}
    models = _du_gb(model_dir)
    return {
        "total": boot["total"],
        "used": boot["used"],
        "free": boot["free"],
        "models": models,
        "chromium": _du_gb(CHROMIUM_DIR),
        "model_volume": model_volume,
        "cost": _storage_cost(boot["total"], model_volume, models, catalog),
    }


def _top(before: _Sample, after: _Sample, mem_total_kb: float | None,
         count: int = 3) -> list[dict[str, Any]]:
    """The ``count`` processes burning the most CPU over the sampled window."""
    span = after.at - before.at
    scored: list[tuple[float, int]] = []
    for pid, ticks in after.procs.items():
        old = before.procs.get(pid)
        if old is None or span <= 0:
            continue
        pct = 100.0 * (ticks - old) / _CLOCK_TICKS / span
        if pct > 0:
            scored.append((pct, pid))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    rows: list[dict[str, Any]] = []
    for pct, pid in scored[:count]:
        rows.append(
            {
                "pid": pid,
                "cpu_pct": round(pct, 1),
                "mem_pct": _process_mem_pct(pid, mem_total_kb),
                "cmd": _process_cmd(pid),
            }
        )
    return rows


def _process_cmd(pid: int) -> str:
    raw = _text(f"/proc/{pid}/cmdline")
    argv = [word for word in (raw or "").split("\0") if word]
    if argv:
        return redact_command(argv)
    # A kernel thread has an empty cmdline; proc(5) gives its name in comm.
    stat = _text(f"/proc/{pid}/stat") or ""
    open_paren, close_paren = stat.find("("), stat.rfind(")")
    if 0 <= open_paren < close_paren:
        return f"[{stat[open_paren + 1 : close_paren]}]"
    return f"pid {pid}"


def _process_mem_pct(pid: int, mem_total_kb: float | None) -> float | None:
    stat = _text(f"/proc/{pid}/stat")
    if stat is None or not mem_total_kb:
        return None
    fields = _stat_fields(stat)
    try:
        rss_kb = int(fields[21]) * _PAGE_SIZE / _KIB  # type: ignore[index]
    except (TypeError, IndexError, ValueError):
        return None
    return round(100.0 * rss_kb / mem_total_kb, 1)


def _uptime_s() -> float | None:
    raw = _text("/proc/uptime")
    if raw is None:
        return None
    try:
        return _round(float(raw.split()[0]), 1)
    except (IndexError, ValueError):
        return None


def _loadavg() -> list[float | None]:
    raw = _text("/proc/loadavg")
    if raw is None:
        return [None, None, None]
    parts = raw.split()
    out: list[float | None] = []
    for index in range(3):
        try:
            out.append(float(parts[index]))
        except (IndexError, ValueError):
            out.append(None)
    return out


def _cores() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        return os.cpu_count() or 0


# ---------------------------------------------------------------------------
# The model block
# ---------------------------------------------------------------------------


def resident_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """One ``/api/ps`` model, in the shape the console shows it.

    ``processor`` is computed the way ``ollama ps`` computes its own column:
    from how much of the model's bytes sit in VRAM. On this node that is always
    100% CPU — there is no GPU — and saying so on the page is the point.
    """
    size = raw.get("size") or 0
    vram = raw.get("size_vram") or 0
    if not size:
        processor = None
    elif vram <= 0:
        processor = "100% CPU"
    elif vram >= size:
        processor = "100% GPU"
    else:
        gpu = round(100.0 * vram / size)
        processor = f"{100 - gpu}%/{gpu}% CPU/GPU"
    return {
        "name": str(raw.get("name") or raw.get("model") or "?"),
        "size_gb": _round(size / _GIB, 2) if size else None,
        "processor": processor,
        "context": raw.get("context_length") or raw.get("context") or None,
        "until_utc": _utc_z(raw.get("expires_at")),
    }


def _instant(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Prefill and decode rates from the most recent attempt that has them.

    The attempt row stores tokens and seconds, not rates, so the console
    divides. An attempt with a zero or missing duration is skipped rather than
    dividing by it — that is what "the most recent attempt *with rates*" means.
    """
    rows = conn.execute(
        "SELECT id, prompt_tokens, completion_tokens, prefill_s, decode_s, "
        "finished_utc, started_utc FROM attempts "
        "WHERE prefill_s IS NOT NULL AND decode_s IS NOT NULL "
        "ORDER BY COALESCE(finished_utc, started_utc) DESC, id DESC LIMIT 10"
    ).fetchall()
    for row in rows:
        prefill = row["prefill_s"] or 0
        decode = row["decode_s"] or 0
        if prefill <= 0 or decode <= 0:
            continue
        return {
            "prefill_tok_s": _round((row["prompt_tokens"] or 0) / prefill, 1),
            "decode_tok_s": _round((row["completion_tokens"] or 0) / decode, 1),
            "from_attempt_id": int(row["id"]),
            "at_utc": _utc_z(row["finished_utc"] or row["started_utc"]),
        }
    return None


def _gate_report(row: sqlite3.Row, jobs_dir: Path) -> dict[str, Any] | None:
    """One attempt's gate report, by its recorded path or where the worker
    would have put it (``<jobs>/<job>/attempt-<n>/.gate/report.json``)."""
    path = row["gate_report_path"]
    if path:
        report = _json_file(path)
        if report is not None:
            return report
    fallback = jobs_dir / str(row["job_id"]) / f"attempt-{row['n']}" / ".gate" / "report.json"
    return _json_file(fallback)


def _gate_launch_s(conn: sqlite3.Connection, jobs_dir: Path) -> float | None:
    rows = conn.execute(
        "SELECT id, job_id, n, gate_report_path FROM attempts "
        "WHERE gate_exit IS NOT NULL "
        "ORDER BY COALESCE(finished_utc, started_utc) DESC, id DESC LIMIT 5"
    ).fetchall()
    for row in rows:
        report = _gate_report(row, jobs_dir)
        if report:
            value = (report.get("timings") or {}).get("launch_s")
            if value is not None:
                return _round(value, 3)
    return None


def _slot(resident: list[dict[str, Any]], top: list[dict[str, Any]],
          job_in_flight: int | None, pair_block: dict[str, Any] | None = None,
          procs: dict[str, list[int]] | None = None) -> dict[str, Any]:
    """Who holds the one inference slot.

    The rule is worker.fence's, plus one addition the fence does not need:

    * **ours** — a ``sketchgen … worker`` process is alive and a job is in
      executing/gating/repairing. That is this system decoding.
    * **busy** — any ``opencode`` process exists (the fence's own refusal: an
      interactive session holds the slot for an hour at a time), *or* Ollama
      reports a model resident and some process that is neither ollama itself
      nor this collector is burning at least :data:`BUSY_CPU_PCT`. Residency
      alone is never busy: ``KEEP_ALIVE=30m`` leaves a model loaded with nobody
      attached, which is exactly why the fence does not refuse on it.
    * **lent** — this node is the partner in the D12 pair: ``ggml-rpc-server``
      holds its card for the pool on the head (docs/plans/pooled-pair.md).
    * **free** — everything else.

    Three processes are never the stranger in the resident rule. Ollama's own
    runner: Ollama 0.34 runs ``llama-server`` from its library directory, and
    until 2026-09-25 an idle judge on the D12 boxes read here as *busy —
    llama-server*. The pool and the RPC server: on the head the pool decoding
    with no job in flight is said separately — *the pool, not a job*, which is
    a bench or a client that is not the worker, and worth seeing.

    ``holder`` names the process, never its arguments.
    """
    found = _find_processes(
        {"worker": ("sketchgen", "worker"), "opencode": ("opencode",)}
    )
    worker_pid = found["worker"]
    if worker_pid is not None and job_in_flight is not None:
        return {
            "state": "ours",
            "holder": f"sketchgen worker job {job_in_flight}",
        }
    opencode_pid = found["opencode"]
    if opencode_pid is not None:
        return {"state": "busy", "holder": f"opencode pid {opencode_pid}"}
    role = (pair_block or {}).get("role")
    if role == "partner":
        lent = pair_block.get("lent") or {}  # type: ignore[union-attr]
        serving = lent.get("serving")
        return {
            "state": "lent",
            "holder": f"{pair.RPC_PROCESS} pid {lent.get('rpc_pid')} — "
            + ("serving the pool" if serving else "waiting for the pool"),
        }
    if role == "head":
        pool = pair_block.get("pool") or {}  # type: ignore[union-attr]
        if pool.get("state") == "decoding":
            return {
                "state": "busy",
                "holder": f"the pool ({pair.POOL_PROCESS} pid {pool.get('pid')}), not a job",
            }
    ours = set()
    for key in ("runner", "pool", "rpc"):
        ours.update((procs or {}).get(key) or [])
    if resident:
        mine = os.getpid()
        for row in top:
            if row["pid"] == mine or (row["cpu_pct"] or 0) < BUSY_CPU_PCT:
                continue
            if row["pid"] in ours:
                continue
            name = (row["cmd"] or "").split(" ")[0]
            if name.startswith("ollama"):
                continue
            return {"state": "busy", "holder": f"{name} pid {row['pid']}"}
    return {"state": "free", "holder": None}


def _find_processes(specs: dict[str, tuple[str, ...]]) -> dict[str, int | None]:
    """The lowest pid matching each spec — every word in its command line.

    ``pgrep`` would do this, but the collector is on a 200 ms budget: a
    subprocess is not free, and neither is a second walk of /proc, so every
    spec is answered from one pass. This process and its parent never match
    (``sketchgen console`` contains the word ``sketchgen`` too).
    """
    found: dict[str, int | None] = {name: None for name in specs}
    mine = {os.getpid(), os.getppid()}
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for entry in names:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid in mine:
            continue
        raw = _text(f"/proc/{pid}/cmdline")
        if not raw:
            continue
        line = raw.replace("\0", " ")
        for name, words in specs.items():
            if all(word in line for word in words):
                if found[name] is None or pid < found[name]:
                    found[name] = pid
    return found


# ---------------------------------------------------------------------------
# The database blocks
# ---------------------------------------------------------------------------


def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] is not None else 0


def _session_start(conn: sqlite3.Connection) -> str | None:
    """When this session began, or None — in which case session == total."""
    return _utc_z(db.get_meta(conn, "worker_started_utc"))


def _odometer(conn: sqlite3.Connection, since: str | None) -> dict[str, Any]:
    def totals(where: str, args: tuple) -> dict[str, Any]:
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens), 0) AS tin, "
            "COALESCE(SUM(completion_tokens), 0) AS tout, "
            "COALESCE(SUM(wall_s), 0) AS wall FROM attempts " + where,
            args,
        ).fetchone()
        return {
            "in": int(row["tin"] or 0),
            "out": int(row["tout"] or 0),
            "wall_s": _round(row["wall"] or 0.0, 1),
        }

    total = totals("", ())
    if since is None:
        session = dict(total)
    else:
        session = totals("WHERE COALESCE(started_utc, finished_utc) >= ?", (since,))
    # What share of the session the slot was ours: seconds spent inside our own
    # attempts, over seconds elapsed. The denominator for a session with no
    # start stamp is the age of the oldest attempt, so the number still means
    # something on a database the worker never stamped.
    elapsed = _seconds_since(since)
    if elapsed is None:
        row = conn.execute(
            "SELECT MIN(COALESCE(started_utc, finished_utc)) AS first FROM attempts"
        ).fetchone()
        elapsed = _seconds_since(row["first"] if row else None)
    share = None
    if elapsed and elapsed > 0:
        share = _round(100.0 * (session["wall_s"] or 0.0) / elapsed, 1)
    session["since_utc"] = since
    session["slot_ours_pct"] = share
    return {"session": session, "total": total}


def _days_since_first_job(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT MIN(created_utc) AS first FROM jobs").fetchone()
    seconds = _seconds_since(row["first"] if row else None)
    if not seconds or seconds <= 0:
        return 1.0
    return max(1.0, seconds / 86400.0)


def _funnel(conn: sqlite3.Connection, since: str | None) -> dict[str, Any]:
    """The production funnel, as now / session / per day / total.

    * ``now`` is jobs sitting in the matching state or activity right now:
      generated is executing, revised is repairing, passed_gate is gating, the
      four outcome rows are jobs in that outcome state, and children is the
      unfinished jobs that have a parent entry.
    * ``session`` counts rows created since the worker's start stamp; with no
      stamp it is the total, which is what "session == total" means below.
    * ``per_day`` is total over days since the first job, floored at one day, so
      a system half a day old does not report double its own output.
    """
    days = _days_since_first_job(conn)
    entry_since = "AND created_utc >= ?"

    def row(now: int, total: int, session: int) -> dict[str, Any]:
        return {
            "now": now,
            "session": session,
            "per_day": _round(total / days, 2),
            "total": total,
        }

    def jobs_in(state: str) -> int:
        return _count(conn, "SELECT COUNT(*) FROM jobs WHERE state = ?", (state,))

    def attempts(extra: str = "") -> tuple[int, int]:
        base = "SELECT COUNT(*) FROM attempts"
        where = f" WHERE {extra}" if extra else ""
        total = _count(conn, base + where)
        if since is None:
            return total, total
        joiner = " AND " if extra else " WHERE "
        session = _count(
            conn,
            base + where + joiner + "COALESCE(started_utc, finished_utc) >= ?",
            (since,),
        )
        return total, session

    def entries(state: str) -> tuple[int, int]:
        total = _count(conn, "SELECT COUNT(*) FROM entries WHERE state = ?", (state,))
        if since is None:
            return total, total
        session = _count(
            conn,
            "SELECT COUNT(*) FROM entries WHERE state = ? " + entry_since,
            (state, since),
        )
        return total, session

    def children() -> tuple[int, int]:
        total = _count(
            conn, "SELECT COUNT(*) FROM jobs WHERE parent_entry_id IS NOT NULL"
        )
        if since is None:
            return total, total
        session = _count(
            conn,
            "SELECT COUNT(*) FROM jobs WHERE parent_entry_id IS NOT NULL "
            "AND created_utc >= ?",
            (since,),
        )
        return total, session

    generated_total, generated_session = attempts()
    revised_total, revised_session = attempts("n > 1")
    passed_total, passed_session = attempts("gate_exit = 0")
    children_total, children_session = children()
    in_flight_children = _count(
        conn,
        "SELECT COUNT(*) FROM jobs WHERE parent_entry_id IS NOT NULL "
        "AND state NOT IN ('published', 'rejected', 'failed')",
    )

    funnel: dict[str, Any] = {
        "generated": row(jobs_in("executing"), generated_total, generated_session),
        "revised": row(jobs_in("repairing"), revised_total, revised_session),
        "passed_gate": row(jobs_in("gating"), passed_total, passed_session),
    }
    for name, state, job_state in (
        ("published", "published", "published"),
        ("held", "held", "held"),
        ("rejected", "rejected", "rejected"),
        ("failed_kept", "failed-kept", "failed"),
        ("archived", "archived", None),
    ):
        total, session = entries(state)
        funnel[name] = row(jobs_in(job_state) if job_state else 0, total, session)
    funnel["children"] = row(in_flight_children, children_total, children_session)
    assert set(funnel) == set(FUNNEL_NAMES)  # the contract, checked in place
    return funnel


def submissions(conn: sqlite3.Connection) -> dict[str, int]:
    """What the public has asked for, by the submissions table's own state.

    The console shows the ``pending`` half of this as one tile, because that
    number is the only one on the page a person is expected to do something
    about: a submission waits until the operator releases or declines it, and
    nothing else on this document waits for a human at all. The other two
    states ride along so the tile's subtitle can say what has already been
    decided.

    Zeros on a database that predates migration 010 — see
    :func:`sketchgen.db.submission_counts`, which does not raise for it.
    """
    return db.submission_counts(conn)


def _sketch_lines(row: sqlite3.Row, jobs_dir: Path) -> int | None:
    """Lines in an attempt's sketch.js, by its recorded source_dir or the
    directory the worker would have written it into."""
    candidates = []
    if row["source_dir"]:
        candidates.append(Path(row["source_dir"]) / "sketch.js")
    candidates.append(jobs_dir / str(row["job_id"]) / f"attempt-{row['n']}" / "sketch.js")
    for path in candidates:
        raw = _text(path)
        if raw is not None:
            return len(raw.splitlines())
    return None


def _per_sketch_rows(conn: sqlite3.Connection, jobs_dir: Path,
                     job_ids: list[int]) -> dict[str, Any]:
    """The averages for one population of finished jobs.

    Every field is a mean over the jobs in the population except the rates,
    which are fractions of it. gate_s and sketch_lines are read from the disk
    and so are averaged over the most recent :data:`FILE_SAMPLE_LIMIT` jobs;
    everything else is over all of them. An empty population is all zeros
    rather than nulls, so the page has something to render on day one.
    """
    empty = {
        "attempts_to_pass": 0.0,
        "wall_s": 0.0,
        "gate_s": 0.0,
        "tokens_in": 0.0,
        "tokens_out": 0.0,
        "sketch_lines": 0.0,
        "first_attempt_pass_rate": 0.0,
        "cost_usd_16_96": 0.0,
        "cost_usd_4_24": RATE_PER_HOUR_4_24,
    }
    if not job_ids:
        return empty
    marks = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"SELECT job_id, COUNT(*) AS n, COALESCE(SUM(wall_s), 0) AS wall, "
        f"COALESCE(SUM(prompt_tokens), 0) AS tin, "
        f"COALESCE(SUM(completion_tokens), 0) AS tout "
        f"FROM attempts WHERE job_id IN ({marks}) GROUP BY job_id",
        job_ids,
    ).fetchall()
    if not rows:
        return empty
    count = float(len(rows))
    attempts_mean = sum(row["n"] for row in rows) / count
    wall_mean = sum(row["wall"] or 0.0 for row in rows) / count
    tokens_in = sum(row["tin"] or 0 for row in rows) / count
    tokens_out = sum(row["tout"] or 0 for row in rows) / count

    first_pass = _count(
        conn,
        f"SELECT COUNT(*) FROM attempts WHERE n = 1 AND gate_exit = 0 "
        f"AND job_id IN ({marks})",
        tuple(job_ids),
    )

    sampled = job_ids[-FILE_SAMPLE_LIMIT:]
    sample_marks = ",".join("?" for _ in sampled)
    detail = conn.execute(
        f"SELECT id, job_id, n, source_dir, gate_report_path FROM attempts "
        f"WHERE job_id IN ({sample_marks}) ORDER BY job_id, n",
        sampled,
    ).fetchall()
    gate_by_job: dict[int, float] = {}
    last_attempt: dict[int, sqlite3.Row] = {}
    for row in detail:
        report = _gate_report(row, jobs_dir)
        total_s = (report or {}).get("timings", {}).get("total_s")
        if total_s is not None:
            gate_by_job[row["job_id"]] = gate_by_job.get(row["job_id"], 0.0) + float(total_s)
        last_attempt[row["job_id"]] = row
    gate_mean = (
        sum(gate_by_job.values()) / len(gate_by_job) if gate_by_job else 0.0
    )
    line_counts = [
        value
        for value in (_sketch_lines(row, jobs_dir) for row in last_attempt.values())
        if value is not None
    ]
    lines_mean = sum(line_counts) / len(line_counts) if line_counts else 0.0

    return {
        "attempts_to_pass": _round(attempts_mean, 2),
        "wall_s": _round(wall_mean, 1),
        "gate_s": _round(gate_mean, 2),
        "tokens_in": _round(tokens_in, 1),
        "tokens_out": _round(tokens_out, 1),
        "sketch_lines": _round(lines_mean, 1),
        "first_attempt_pass_rate": _round(first_pass / count, 3),
        "cost_usd_16_96": _round(wall_mean / 3600.0 * RATE_PER_HOUR_16_96, 4),
        "cost_usd_4_24": RATE_PER_HOUR_4_24,
    }


def _per_sketch(conn: sqlite3.Connection, jobs_dir: Path,
                since: str | None) -> dict[str, Any]:
    marks = ",".join("?" for _ in FINISHED_STATES)
    rows = conn.execute(
        f"SELECT id, updated_utc FROM jobs WHERE state IN ({marks}) "
        f"ORDER BY updated_utc, id",
        FINISHED_STATES,
    ).fetchall()
    all_ids = [int(row["id"]) for row in rows]
    if since is None:
        session_ids = list(all_ids)
    else:
        session_ids = [
            int(row["id"]) for row in rows if (row["updated_utc"] or "") >= since
        ]
    last_ids = all_ids[-1:]
    return {
        "last": _per_sketch_rows(conn, jobs_dir, last_ids),
        "session": _per_sketch_rows(conn, jobs_dir, session_ids),
        "all": _per_sketch_rows(conn, jobs_dir, all_ids),
    }


def _worker_block(conn: sqlite3.Connection, since: str | None) -> dict[str, Any]:
    control = None
    try:
        control = db.get_control(conn)
    except sqlite3.Error:
        control = None
    marks = ",".join("?" for _ in IN_FLIGHT_STATES)
    row = conn.execute(
        f"SELECT id FROM jobs WHERE state IN ({marks}) ORDER BY updated_utc DESC, "
        f"id DESC LIMIT 1",
        IN_FLIGHT_STATES,
    ).fetchone()
    return {
        "control": control.state if control else "running",
        "reason": control.reason if control else None,
        "updated_utc": _utc_z(control.updated_utc) if control else None,
        "started_utc": since,
        "job_in_flight": int(row["id"]) if row else None,
    }


# ---------------------------------------------------------------------------
# The process status card (packet 5)
# ---------------------------------------------------------------------------

#: How many closed rows of a step the median is fitted over, and how few is too
#: few to fit one. Fifty is a working day of that step on this node; under five
#: samples the median moves further between readings than the thing it is
#: measuring, and a progress bar that lies is worse than no progress bar, so it
#: is null and the card draws none.
ACTIVITY_MEDIAN_SAMPLE = 50
ACTIVITY_MEDIAN_MIN = 5

#: Past this, a step that is still open is not slow, it is stuck. Longer than
#: the executor's own 1800 s timeout on purpose: the timeout is the step's way
#: of ending itself, and this is the card's way of saying that it did not.
ACTIVITY_STALLED_S = 40 * 60.0

#: Steps that are the worker keeping itself busy rather than carrying a job.
#: The card's pill goes quiet for these: an idle worker is not a working one,
#: and a green pill over "Nothing to do" has told the operator the wrong thing.
ACTIVITY_IDLE_STEPS = ("judging", "critiquing", "sweeping", "idle")

#: What to do about a worker that is not there. The card says it in words,
#: because "gone" on its own reads like a bug in the page.
ACTIVITY_GONE_FIX = "systemctl --user status sketchgen-worker"


def _activity_seconds(row: sqlite3.Row) -> float | None:
    """How long one closed step took, or None if its stamps do not parse."""
    start = _utc_z(row["started_utc"])
    end = _utc_z(row["ended_utc"])
    if start is None or end is None:
        return None
    try:
        began = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
        ended = datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return max(0.0, (ended - began).total_seconds())


def _activity_median(conn: sqlite3.Connection, step: str) -> float | None:
    """The median duration of this step, over the newest closed rows of it.

    No per-step constants anywhere: how long writing a sketch takes is a
    measurement of this node with these models, and it moves when either
    changes. The card's bar is elapsed against this, which is the only honest
    progress a worker that cannot tick mid-step can offer.
    """
    try:
        rows = conn.execute(
            "SELECT started_utc, ended_utc FROM activity WHERE step = ? "
            "AND ended_utc IS NOT NULL ORDER BY id DESC LIMIT ?",
            (str(step), ACTIVITY_MEDIAN_SAMPLE),
        ).fetchall()
    except sqlite3.OperationalError:
        return None
    seconds = [value for value in (_activity_seconds(row) for row in rows)
               if value is not None]
    if len(seconds) < ACTIVITY_MEDIAN_MIN:
        return None
    return _round(statistics.median(seconds), 1)


def _pid_alive(pid: int | None) -> bool:
    """Whether that process still exists. One stat call, on a 200 ms budget."""
    if not pid:
        return False
    return Path(f"/proc/{int(pid)}").exists()


def _blank_activity(state: str, headline: str, detail: str | None,
                    recent: list[dict[str, Any]]) -> dict[str, Any]:
    """The card with no step under it: every key present, nothing claimed."""
    return {
        "step": None,
        "headline": headline,
        "detail": detail,
        "job_id": None,
        "entry_id": None,
        "model": None,
        "pid": None,
        "started_utc": None,
        "elapsed_s": None,
        "median_s": None,
        "live": False,
        "state": state,
        "recent": recent,
    }


def activity(conn: sqlite3.Connection) -> dict[str, Any]:
    """What the worker is doing right now, as the card shows it.

    The one reader of migration 008's table: :func:`collect` calls it for the
    Console document and the operator UI's two-second poll calls it directly,
    so the document and the poll cannot disagree about the sentence on screen.
    Three small queries and one stat — the poll must not be the reason a page
    load waits for a collector.

    ``state`` is decided here rather than in the template, because it is a
    reading of three things a template should not be re-deriving: the control
    row, whether the recorded pid still exists, and how long the open step has
    been open. It is exactly one of:

      ``paused``   the operator's switch says so, whatever a row claims
      ``gone``     the pid that opened the step is not there any more
      ``stalled``  the pid is there and the step is past ACTIVITY_STALLED_S
      ``fenced``   the nap after a refused pass: another client holds the
                   inference slot, and the worker claims nothing until it goes
      ``running``  a step that is carrying a job
      ``idle``     a step that is the worker keeping itself busy
      ``unknown``  nothing has ever been recorded

    ``paused`` comes first on purpose. A paused worker's last step stays open —
    it is stopped between steps, not mid-step — and the operator who pressed
    Pause needs the card to agree with the button they pressed.
    """
    try:
        control = db.get_control(conn)
    except sqlite3.Error:  # pragma: no cover - the database is the problem
        control = None
    paused = control is not None and control.state == "paused"

    recent = []
    for row in db.recent_activity(conn, 3):
        recent.append({
            "headline": row["headline"],
            "seconds": _round(_activity_seconds(row), 1),
        })

    row = db.current_activity(conn)
    if row is None:
        if paused:
            return _blank_activity(
                "paused",
                "The worker is paused",
                (control.reason if control else None) or "paused by the operator",
                recent,
            )
        return _blank_activity(
            "unknown",
            "Nothing recorded yet",
            "the worker writes this card as it works; this database has no "
            "activity in it",
            recent,
        )

    step = str(row["step"])
    pid = int(row["pid"]) if row["pid"] is not None else None
    live = _pid_alive(pid)
    elapsed = _round(_seconds_since(row["started_utc"]), 1)
    headline = str(row["headline"])
    detail = row["detail"]

    if paused:
        state = "paused"
        detail = (control.reason if control else None) or "paused by the operator"
    elif not live:
        state = "gone"
        ago = human_gap(elapsed) if elapsed is not None else "some time"
        headline = (
            "Worker not running — the last step was "
            + step
            + (f" job {int(row['job_id'])}" if row["job_id"] is not None else "")
            + f", {ago} ago"
        )
        detail = ACTIVITY_GONE_FIX
    elif elapsed is not None and elapsed > ACTIVITY_STALLED_S:
        state = "stalled"
        detail = " · ".join(filter(None, [
            detail, "longer than any step has taken; check the transcript"
        ]))
    elif step == FENCED_STEP:
        # Not idle: an idle worker would claim the next job, and this one will
        # not. The detail already carries the fence's reason (Worker._nap).
        state = "fenced"
    elif step in ACTIVITY_IDLE_STEPS:
        state = "idle"
    else:
        state = "running"

    return {
        "step": step,
        "headline": headline,
        "detail": detail,
        "job_id": int(row["job_id"]) if row["job_id"] is not None else None,
        "entry_id": int(row["entry_id"]) if row["entry_id"] is not None else None,
        "model": row["model"],
        "pid": pid,
        "started_utc": _utc_z(row["started_utc"]),
        "elapsed_s": elapsed,
        "median_s": _activity_median(conn, step),
        "live": live,
        "state": state,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


# Where nvidia-smi is, in the order worth trying. The systemd user manager
# runs with a minimal PATH that has none of these on it, and on WSL the driver
# shim is mounted from the Windows host rather than installed, so "it works in
# my shell" and "the service can see it" are two different questions.
NVIDIA_SMI_PATHS = (
    "/usr/lib/wsl/lib/nvidia-smi",
    "/usr/bin/nvidia-smi",
    "/usr/local/bin/nvidia-smi",
)

_GPU_CACHE: tuple[float, dict[str, Any]] | None = None


def _nvidia_smi() -> str | None:
    """The nvidia-smi to run, or None when this node has no GPU tooling."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for candidate in NVIDIA_SMI_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _is_wsl() -> bool:
    """True on WSL, whose MemTotal is a share of a Windows host, not a machine.

    Worth saying on the page: 8 GiB here is half of a 16 GB desktop, and a
    reader who takes it for the machine will misread every memory number.
    """
    try:
        with open("/proc/version", encoding="utf-8") as handle:
            return "microsoft" in handle.read().lower()
    except OSError:  # pragma: no cover - /proc always exists on Linux
        return False


def node_kind(conn: sqlite3.Connection | None = None) -> str:
    """``cloud`` or ``local`` — which kind of machine the card is describing.

    Local unless something proves otherwise, for the same reason
    :func:`sketchgen.worker.node_shape` no longer names a shape it cannot
    prove. The proof is ``meta.node_shape``, which nothing writes but
    :func:`sketchgen.cli.billing.node_identity`, and which it reads from the
    instance metadata service on the node itself. ``SKETCHGEN_NODE_KIND``
    overrides the lot, for a cloud node that has not been identified yet.
    """
    override = (os.environ.get("SKETCHGEN_NODE_KIND") or "").strip().lower()
    if override in ("cloud", "local"):
        return override
    if conn is not None:
        try:
            if (db.get_meta(conn, "node_shape") or "").strip():
                return "cloud"
        except sqlite3.Error:  # pragma: no cover - a closed or partial db
            pass
    return "local"


def _gpu_block() -> dict[str, Any]:
    """The GPU as name / VRAM / utilisation; every field None where there is not one.

    Always the same keys, never an absent block: the document's key shape is a
    contract the console fixture holds us to, and a node without a GPU has to
    produce the same skeleton as a node with one. "No GPU" is
    ``vram_mb.total`` being None, which is what the card reads.

    On the local node this is the number that explains the timings: the
    executor is only fast while it is fully resident in VRAM, and the card has
    6 GB, so "how full is it" is the first thing an operator wants. A node
    without ``nvidia-smi`` — every OCI A1 shape so far — caches the empty
    answer and stops paying for the lookup.
    """
    global _GPU_CACHE
    now = time.monotonic()
    if _GPU_CACHE is not None and now - _GPU_CACHE[0] < GPU_TTL_S:
        return _GPU_CACHE[1]
    block: dict[str, Any] = {
        "name": None,
        "vram_mb": {"total": None, "used": None},
        "util_pct": None,
        "apps": [],
    }
    binary = _nvidia_smi()
    if binary is None:
        _GPU_CACHE = (now, block)
        return block
    try:
        done = subprocess.run(
            [
                binary,
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [line for line in (done.stdout or "").splitlines() if line.strip()]
        if done.returncode == 0 and lines:
            name, total, used, util = [part.strip() for part in lines[0].split(",")]
            block = {
                "name": name,
                "vram_mb": {"total": float(total), "used": float(used)},
                "util_pct": float(util),
                "apps": _gpu_apps(binary),
            }
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    _GPU_CACHE = (now, block)
    return block


def _gpu_apps(binary: str) -> list[dict[str, Any]]:
    """The VRAM each process holds: ``nvidia-smi --query-compute-apps``.

    The one honest answer to "where does the model sit" once there is more
    than one thing on the card. In the D12 pair the pool (llama-server), Ollama's
    runner (also llama-server, from Ollama's own directory) and on the partner
    ggml-rpc-server each hold their share, and the card's total cannot say whose
    is whose. ``role`` is :func:`sketchgen.pair.classify`'s answer from the
    process path; nvidia-smi reports it for every user's process, Ollama's too.
    WSL answers ``[N/A]`` for the memory; that is None, not zero.
    """
    try:
        done = subprocess.run(
            [binary, "--query-compute-apps=pid,process_name,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    apps: list[dict[str, Any]] = []
    for line in (done.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        try:
            used: float | None = float(parts[-1])
        except ValueError:
            used = None
        path = ",".join(parts[1:-1])
        apps.append({
            "pid": int(parts[0]),
            "name": os.path.basename(path) or path,
            "role": pair.classify(path),
            "used_mb": used,
        })
    apps.sort(key=lambda app: -(app["used_mb"] or 0.0))
    return apps


def collect(
    conn: sqlite3.Connection,
    jobs_dir: str | os.PathLike[str] = DEFAULT_JOBS_DIR,
    host_url: str = DEFAULT_HOST,
    prev: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One console document. See this module's docstring for the contract.

    ``prev`` is the document from this caller's previous call; passing it says
    "differences against the sample you kept then", which skips the 100 ms
    sleep a cold call needs to have two ``/proc/stat`` readings. Passing None
    (the default, and what a single shot does) takes both readings here.
    """
    global _LAST_SAMPLE
    started = time.monotonic()
    jobs_path = Path(os.path.expanduser(str(jobs_dir)))

    before = _LAST_SAMPLE if (prev is not None and _LAST_SAMPLE is not None) else None
    if before is None:
        before = _sample()
        time.sleep(0.1)
    after = _sample()
    _LAST_SAMPLE = after

    per_core, cpu_total = _cpu_percentages(before, after)
    info = _meminfo()
    mem, swap = _mem_block(info)
    cores = _cores()
    node = {
        "shape": node_shape(),
        "kind": node_kind(conn),
        "wsl": _is_wsl(),
        "cores": cores,
        "uptime_s": _uptime_s(),
        "load": _loadavg(),
        "cpu_pct": per_core,
        "cpu_total_pct": cpu_total,
        "mem_mb": mem,
        "swap_mb": swap,
        "top": _top(before, after, info.get("MemTotal")),
    }
    # A core that vanished between the two samples (offline, or /proc unread)
    # would leave the list short; the contract says one entry per core.
    if len(node["cpu_pct"]) < cores:
        node["cpu_pct"] = node["cpu_pct"] + [0.0] * (cores - len(node["cpu_pct"]))
    node["cpu_pct"] = node["cpu_pct"][:cores]

    node["gpu"] = _gpu_block()
    procs = pair.processes()
    span = after.at - before.at

    def proc_cpu(pid: int) -> float | None:
        old, new = before.procs.get(pid), after.procs.get(pid)
        if old is None or new is None or span <= 0:
            return None
        return round(100.0 * (new - old) / _CLOCK_TICKS / span, 1)

    # A single shot (``prev`` None, the CLI) reads the partner and the link
    # now; the web server's poll never waits on ssh (sketchgen/pair.py).
    pair_block = pair.block(apps=node["gpu"].get("apps") or [], procs=procs,
                            proc_cpu=proc_cpu, sync=prev is None)

    base = host_url.rstrip("/")
    version = _http_json(base + "/api/version")
    ps_body = _http_json(base + "/api/ps") or {}
    resident = [
        resident_entry(entry)
        for entry in (ps_body.get("models") or [])
        if isinstance(entry, dict)
    ]
    # /api/tags is every model on disk (not just the resident ones), which is
    # what the free-tier drop list is reasoned over. Unreachable host -> [].
    tags_body = _http_json(base + "/api/tags") or {}
    catalog = [
        {"name": entry.get("name"), "size_gb": _round(entry.get("size", 0) / _GIB, 2)}
        for entry in (tags_body.get("models") or [])
        if isinstance(entry, dict) and entry.get("name")
    ]
    node["disk_gb"] = _disk_block(catalog)

    since = _session_start(conn)
    worker = _worker_block(conn, since)
    model = {
        "ollama_version": (version or {}).get("version"),
        "resident": resident,
        "slot": _slot(resident, node["top"], worker["job_in_flight"], pair_block, procs),
        "instant": _instant(conn),
        "gate_launch_s": _gate_launch_s(conn, jobs_path),
    }

    document = {
        "utc": db.utc_now(),
        "collector_ms": 0.0,
        "node": node,
        "model": model,
        "worker": worker,
        "activity": activity(conn),
        "pair": pair_block,
        "odometer": _odometer(conn, since),
        "funnel": _funnel(conn, since),
        "submissions": submissions(conn),
        "per_sketch": _per_sketch(conn, jobs_path, since),
    }
    document["collector_ms"] = round((time.monotonic() - started) * 1000.0, 1)
    return document


# ---------------------------------------------------------------------------
# The text rendering (the JSON is the contract; this is for a terminal)
# ---------------------------------------------------------------------------


def _bar(pct: float | None, width: int = 10) -> str:
    if pct is None:
        return "?" * width
    filled = max(0, min(width, int(round(width * pct / 100.0))))
    return "|" * filled + " " * (width - filled)


def _disk_text_lines(disk: dict[str, Any]) -> list[str]:
    """The storage rows: the boot disk, the volume the models are on, the bill,
    and what dropping back to the free tier would force off."""
    mv = disk["model_volume"]
    cost = disk["cost"]
    lines = [
        f"  disk  boot {disk['used']} of {disk['total']} GiB used, "
        f"{disk['free']} free   (chromium {disk['chromium']} GiB)"
    ]
    if mv["separate"]:
        lines.append(
            f"  vol   models {disk['models']} GiB on a separate volume: "
            f"{mv['used']} of {mv['total']} GiB used, {mv['free']} free"
        )
    else:
        lines.append(f"  vol   models {disk['models']} GiB on the boot disk")
    if cost["block_gb"] is not None:
        if cost["billable_gb"]:
            bill = (f"${cost['usd_month']}/mo ({cost['billable_gb']} GiB over the "
                    f"{cost['free_tier_gb']} GiB free tier)")
        else:
            bill = "no storage bill (under the free tier)"
        lines.append(f"  cost  {cost['block_gb']} GiB block storage — {bill}")
        if cost["over_free_tier_gb"]:
            drops = ", ".join(
                f"{d['name']} ({d['size_gb']} GiB)" for d in cost["would_drop"]
            ) or "some models"
            lines.append(
                f"  free  free tier caps models at {cost['free_tier_model_gb']} GiB; "
                f"{cost['over_free_tier_gb']} GiB over — would drop: {drops}"
            )
        else:
            lines.append(
                f"  free  models fit the {cost['free_tier_model_gb']} GiB free-tier budget"
            )
    return lines


def _gb(mb: Any) -> str:
    return "?" if mb is None else f"{float(mb) / 1024.0:.1f}"


def _pair_text_lines(doc: dict[str, Any]) -> list[str]:
    """The D12 pair, for a terminal: nothing at all when the node is not in one."""
    block = doc.get("pair") or {}
    role = block.get("role")
    if role not in ("head", "partner"):
        return []
    link = block.get("link") or {}
    port = link.get("rpc_port_open")
    link_line = (
        f"        link     {link.get('iface') or '?'} "
        f"{link.get('speed_mbps') or '?'} Mb/s · in {link.get('rx_mb_s')} MB/s · "
        f"out {link.get('tx_mb_s')} MB/s · rpc port "
        + ("open" if port else "closed" if port is False else "?")
        + f", {link.get('rpc_connections')} connection(s)"
    )
    worker = doc.get("worker") or {}
    if role == "partner":
        lent = block.get("lent") or {}
        lines = [
            "",
            f"  pair  partner — this card is lent: {pair.RPC_PROCESS} pid "
            f"{lent.get('rpc_pid')} holds {_gb(lent.get('vram_mb'))} GB, "
            f"{lent.get('cpu_pct')}% cpu, "
            + ("serving the pool" if lent.get("serving") else "waiting for the pool"),
            link_line,
        ]
        if worker.get("control") == "running":
            lines.append("  !     this node's generator is running while its card is lent: "
                         "its worker can load models onto the pool's card")
        return lines
    pool = block.get("pool") or {}
    gpu = (doc.get("node") or {}).get("gpu") or {}
    held = ", ".join(
        f"{app.get('role') or app.get('name')} {_gb(app.get('used_mb'))} GB"
        for app in gpu.get("apps") or []
    ) or "nothing"
    lines = [
        "",
        f"  pair  head — pool {pool.get('state')} · {pool.get('model')} · "
        f"{pool.get('build')} · ctx {pool.get('n_ctx')}",
    ]
    if pool.get("state") == "decoding":
        lines.append(
            f"        now      task {pool.get('task')}: {pool.get('n_decoded')} tokens"
            + (f" at {pool['live_decode_tok_s']} tok/s"
               if pool.get("live_decode_tok_s") is not None else "")
        )
    lines.append(
        f"        served   decode {pool.get('decode_tok_s')} tok/s, prefill "
        f"{pool.get('prefill_tok_s')} tok/s · {pool.get('tokens_in')} in / "
        f"{pool.get('tokens_out')} out"
    )
    vram = gpu.get("vram_mb") or {}
    lines.append(
        f"        here     {gpu.get('name')}: {_gb(vram.get('used'))} of "
        f"{_gb(vram.get('total'))} GB, {gpu.get('util_pct')}% busy — {held}"
    )
    partner = block.get("partner") or {}
    if partner.get("host"):
        pgpu = partner.get("gpu") or {}
        pvram = pgpu.get("vram_mb") or {}
        rpc = partner.get("rpc") or {}
        if partner.get("reachable") is False and partner.get("age_s") is None:
            detail = f"unreachable — {partner.get('error')}"
        else:
            detail = (
                f"{pgpu.get('name')}: {_gb(pvram.get('used'))} of {_gb(pvram.get('total'))} GB, "
                f"{pgpu.get('util_pct')}% busy · rpc pid {rpc.get('pid')} {rpc.get('cpu_pct')}% cpu"
                + (f" {_gb(rpc.get('vram_mb'))} GB" if rpc.get("vram_mb") is not None else "")
                + f" · generator {partner.get('control')}"
                + (f" ({partner.get('reason')})" if partner.get("reason") else "")
                + f" · read {partner.get('age_s')} s ago"
                + ("" if partner.get("reachable") is not False
                   else f" — last read failed: {partner.get('error')}")
            )
        lines.append(f"        partner  {partner.get('host')}: {detail}")
        if partner.get("control") == "running":
            lines.append("  !     the partner's generator is running: its worker can load "
                         "models onto the card it lends the pool")
        if partner.get("resident"):
            lines.append("  !     the partner's Ollama has resident: "
                         + ", ".join(partner["resident"]))
    lines.append(link_line)
    return lines


def render_text(doc: dict[str, Any]) -> str:
    node = doc["node"]
    model = doc["model"]
    lines = [
        f"{doc['utc']}  {node['shape']}  collected in {doc['collector_ms']} ms",
        "",
        "  cpu   " + "  ".join(
            f"{index:>2}[{_bar(pct, 6)}{(pct if pct is not None else 0):>5.1f}%]"
            for index, pct in list(enumerate(node["cpu_pct"]))[:8]
        ),
    ]
    if len(node["cpu_pct"]) > 8:
        lines.append("        " + "  ".join(
            f"{index:>2}[{_bar(pct, 6)}{(pct if pct is not None else 0):>5.1f}%]"
            for index, pct in list(enumerate(node["cpu_pct"]))[8:16]
        ))
    mem, swap = node["mem_mb"], node["swap_mb"]
    lines += [
        f"  mem   used {mem['used']} MB   cache {mem['cache']} MB   "
        f"available {mem['available']} MB of {mem['total']} MB",
        f"  swap  {swap['used']} MB of {swap['total']} MB",
    ]
    lines += _disk_text_lines(node["disk_gb"])
    lines += [
        f"  load  {node['load']} over {node['cores']} cores; "
        f"total cpu {node['cpu_total_pct']}%",
        "  top   " + "; ".join(
            f"{row['pid']} {row['cpu_pct']}% {row['cmd']}" for row in node["top"]
        ),
        "",
        f"  slot  {model['slot']['state']}"
        + (f" — {model['slot']['holder']}" if model["slot"]["holder"] else "")
        + f"   ollama {model['ollama_version'] or 'unreachable'}",
    ]
    for entry in model["resident"]:
        lines.append(
            f"  model {entry['name']}  {entry['size_gb']} GiB  {entry['processor']}"
            f"  ctx {entry['context']}  until {entry['until_utc']}"
        )
    instant = model["instant"]
    if instant:
        lines.append(
            f"  rates prefill {instant['prefill_tok_s']} tok/s, decode "
            f"{instant['decode_tok_s']} tok/s (attempt {instant['from_attempt_id']}, "
            f"{instant['at_utc']})"
        )
    lines += _pair_text_lines(doc)
    worker = doc["worker"]
    odo = doc["odometer"]
    act = doc.get("activity") or {}
    lines += [
        "",
        # The two lines this view exists for over SSH: the sentence, and the
        # line under it. Everything else here is a number; this is the only
        # part that says what the machine is doing.
        f"  now   {act.get('headline') or '—'}"
        + (f"   [{act.get('state')}"
           + (f", {act['elapsed_s']:.0f} s" if act.get("elapsed_s") is not None else "")
           + (f" of about {act['median_s']:.0f} s" if act.get("median_s") else "")
           + "]" if act.get("state") else ""),
        f"        {act.get('detail') or ''}",
        f"  worker {worker['control']}"
        + (f" ({worker['reason']})" if worker["reason"] else "")
        + f"; job in flight {worker['job_in_flight']}; session began "
        f"{worker['started_utc']}",
        f"  tokens session {odo['session']['in']} in / {odo['session']['out']} out, "
        f"{odo['session']['wall_s']} s, slot ours {odo['session']['slot_ours_pct']}%",
        f"         total   {odo['total']['in']} in / {odo['total']['out']} out, "
        f"{odo['total']['wall_s']} s",
        "",
        f"  {'funnel':<12}{'now':>6}{'session':>9}{'per day':>9}{'total':>8}",
    ]
    for name in FUNNEL_NAMES:
        cell = doc["funnel"][name]
        lines.append(
            f"  {name:<12}{cell['now']:>6}{cell['session']:>9}"
            f"{cell['per_day']:>9}{cell['total']:>8}"
        )
    lines += [
        "",
        f"  {'per sketch':<12}{'attempts':>9}{'wall s':>9}{'gate s':>8}"
        f"{'tok in':>8}{'tok out':>9}{'lines':>7}{'1st pass':>9}"
        f"{'$16/96':>9}{'$4/24':>8}",
    ]
    for which in ("last", "session", "all"):
        cell = doc["per_sketch"][which]
        lines.append(
            f"  {which:<12}{cell['attempts_to_pass']:>9}{cell['wall_s']:>9}"
            f"{cell['gate_s']:>8}{cell['tokens_in']:>8}{cell['tokens_out']:>9}"
            f"{cell['sketch_lines']:>7}{cell['first_attempt_pass_rate']:>9}"
            f"{cell['cost_usd_16_96']:>9}{cell['cost_usd_4_24']:>8}"
        )
    return "\n".join(lines)
