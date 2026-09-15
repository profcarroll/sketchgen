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
  the app database  the control row, the attempts, the entries, the jobs

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

Python 3.12, stdlib only. Timestamps are UTC, ISO 8601 with a trailing Z.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sketchgen import db
from sketchgen.worker import DEFAULT_HOST, DEFAULT_JOBS_DIR, node_shape

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_JOBS_DIR",
    "FUNNEL_NAMES",
    "RATE_PER_HOUR_16_96",
    "RATE_PER_HOUR_4_24",
    "collect",
    "redact_command",
    "render_text",
    "resident_entry",
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

#: Directories the disk row names, in the order they are tried.
MODEL_DIRS = (
    os.environ.get("OLLAMA_MODELS", ""),
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


def _disk_block() -> dict[str, float | None]:
    try:
        stats = os.statvfs("/")
        block = float(stats.f_frsize)
        total = stats.f_blocks * block / _GIB
        free = stats.f_bavail * block / _GIB
        used = (stats.f_blocks - stats.f_bfree) * block / _GIB
    except OSError:  # pragma: no cover - "/" always statvfs's on Linux
        total = free = used = None
    models = None
    for candidate in MODEL_DIRS:
        models = _du_gb(candidate)
        if models is not None:
            break
    return {
        "total": _round(total, 1),
        "used": _round(used, 1),
        "free": _round(free, 1),
        "models": models,
        "chromium": _du_gb(CHROMIUM_DIR),
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
          job_in_flight: int | None) -> dict[str, Any]:
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
    * **free** — everything else.

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
    if resident:
        mine = os.getpid()
        for row in top:
            if row["pid"] == mine or (row["cpu_pct"] or 0) < BUSY_CPU_PCT:
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
# The document
# ---------------------------------------------------------------------------


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
        "cores": cores,
        "uptime_s": _uptime_s(),
        "load": _loadavg(),
        "cpu_pct": per_core,
        "cpu_total_pct": cpu_total,
        "mem_mb": mem,
        "swap_mb": swap,
        "disk_gb": _disk_block(),
        "top": _top(before, after, info.get("MemTotal")),
    }
    # A core that vanished between the two samples (offline, or /proc unread)
    # would leave the list short; the contract says one entry per core.
    if len(node["cpu_pct"]) < cores:
        node["cpu_pct"] = node["cpu_pct"] + [0.0] * (cores - len(node["cpu_pct"]))
    node["cpu_pct"] = node["cpu_pct"][:cores]

    base = host_url.rstrip("/")
    version = _http_json(base + "/api/version")
    ps_body = _http_json(base + "/api/ps") or {}
    resident = [
        resident_entry(entry)
        for entry in (ps_body.get("models") or [])
        if isinstance(entry, dict)
    ]

    since = _session_start(conn)
    worker = _worker_block(conn, since)
    model = {
        "ollama_version": (version or {}).get("version"),
        "resident": resident,
        "slot": _slot(resident, node["top"], worker["job_in_flight"]),
        "instant": _instant(conn),
        "gate_launch_s": _gate_launch_s(conn, jobs_path),
    }

    document = {
        "utc": db.utc_now(),
        "collector_ms": 0.0,
        "node": node,
        "model": model,
        "worker": worker,
        "odometer": _odometer(conn, since),
        "funnel": _funnel(conn, since),
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
    mem, swap, disk = node["mem_mb"], node["swap_mb"], node["disk_gb"]
    lines += [
        f"  mem   used {mem['used']} MB   cache {mem['cache']} MB   "
        f"available {mem['available']} MB of {mem['total']} MB",
        f"  swap  {swap['used']} MB of {swap['total']} MB",
        f"  disk  {disk['used']} of {disk['total']} GiB used; "
        f"models {disk['models']} GiB, chromium {disk['chromium']} GiB",
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
    worker = doc["worker"]
    odo = doc["odometer"]
    lines += [
        "",
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
