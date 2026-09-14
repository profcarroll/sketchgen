"""web.py — the operator's five screens, on 127.0.0.1 and nowhere else.

Packet 4.2 of the sketchgen build. One ``ThreadingHTTPServer``, server-rendered
HTML from ``sketchgen/templates/op_*.html``, no framework, no JS build, no CDN:
the only script on any page is the few lines at the bottom of the layout that
keep the worker pill honest, plus the console's own two-second refetch. The
whole thing is reached over the SSH tunnel that already carries ``preview`` on
8080, so this one takes 8081.

The five screens are the wireframe's:

  ``/``            Console — node vitals, the slot, the odometer, the funnel
  ``/queue``       the tiles and the jobs table
  ``/new``         the form that writes a queued row
  ``/job/<id>``    transcript, per-attempt gate report, artefacts, provenance
  ``/held``        what is waiting for a person to publish or reject

and every one of them carries the worker-state pill and the Pause / Stop now /
Resume control in its header, because the operator needs that switch wherever
they happen to be standing (spec §4).

Four decisions worth writing down rather than leaving in the code:

**No auth, and therefore no other bind.** Single user, over a tunnel, so there is
no login and no CSRF token. That is only safe while the socket is on the
loopback interface, so ``--bind`` accepts ``127.0.0.1`` and refuses anything else
with exit 3 rather than trusting the operator's typing. Open WebUI on 3000 and
the agent's own server both bound ``0.0.0.0``; the spec says do not copy them.

**Stop-now is the worker's own representation, not a new one.** The button posts
``action=stop``; this module writes ``set_control('pausing', 'stop')``, which is
exactly what ``bin/sketchgen control stop`` writes and exactly what
:func:`sketchgen.worker.is_stop_now` reads. The UI does not get its own dialect.

**The console document comes from packet 4.1 if it is installed, and from
``tests/fixtures/console/sample.json`` if it is not.** The import is lazy and the
JSON says which one you are looking at (``"source": "live"`` or ``"sample"``), so
a console with no collector behind it is obvious at a glance instead of quietly
plausible. Every field is read through :func:`_dig`, which returns ``None`` for
anything missing, so a collector whose document has grown a field or lost one
renders a dash rather than a traceback.

**The transcript is a stream, and the page still reads without it** (packet
4.3). ``GET /events/job/<id>`` is server-sent events: the last 200 lines of
``job.log`` at once, then each line as the worker appends it, a ``state`` event
carrying the job's state and attempt count, and ``done`` when the job reaches a
terminal state. The server-rendered tail stays in the HTML and the script
replaces it on the first line event, so a reader with no JS — or with the
stream refused — sees the log as of page load rather than an empty box. One
stream is one thread for as long as the tab is open, so :data:`MAX_STREAMS`
caps them and the next one gets 503 rather than a thread.

**Publishing is somebody else's code.** ``POST /held/<id>/publish`` imports
``sketchgen.publish`` lazily (packet 3.2); when it is not there the page says
"publisher not installed" and nothing changes — the entry stays held, which is
the safe direction for a gate whose whole point is that a person decides
(spec §9). Its failures are flash messages too: a missing gallery checkout or
deploy key is something to read on the page, not a traceback in the log.

**Spawning a child belongs here and not on the public site** (packet 5.3).
``POST /entry/<id>/spawn`` hands one critique to :func:`sketchgen.lineage.spawn`,
which composes the parent's prompt with it and queues the child. The form sits on
the job page and on each held card; ``critique-by`` defaults to the operator's
username, which is ``$SKETCHGEN_OPERATOR`` when it is set and the entry's own
submitter otherwise. The gallery is generated, static, and has no way to write to
this database — asking it to would mean a public form on a queue, and the
publication gate exists precisely so a person stands between the queue and the
site.

**The sketch runs on the screens where it is judged.** The first use of this UI
in anger found the hole: gate.png and strip.png are what the gate saw, and a
person deciding whether to publish is being asked to judge a moving sketch from
four still frames. ``GET /preview/<job>/<n>/`` serves the attempt directory as
the small static site it already is — ``index.html`` at the directory URL,
``sketch.js`` and any sibling the page asks for by a relative path, fenced to
that one directory the way ``/jobs/`` is fenced to the jobs directory. The job
page frames the latest attempt and folds the earlier ones away; each held card
frames the attempt its entry came from, above the strip. p5 comes from cdnjs,
which the browser fetches for itself: nothing here proxies the internet. See
:data:`PREVIEW_HEADERS` for what the frame is and is not allowed to do.
"""

from __future__ import annotations

import html
import inspect
import json
import math
import os
import re
import sqlite3
import string
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable

from sketchgen import db
from sketchgen import lineage
from sketchgen import planner
from sketchgen import worker

# Packet 4.1, built on another branch at the same time as this one. Lazy by
# construction: when it is absent the console serves the sample document.
try:  # pragma: no cover - exercised both ways, but only one way per checkout
    from sketchgen import console  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover
    console = None  # type: ignore[assignment]

__all__ = [
    "DEFAULT_BIND",
    "DEFAULT_PORT",
    "GALLERY_URL",
    "LogTailer",
    "MAX_STREAMS",
    "Refused",
    "console_document",
    "job_document",
    "live_streams",
    "make_server",
    "nav_summary",
    "node_cpu_pct",
    "serve",
    "token_bins",
]

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 3

#: The only address this server will bind. See the module docstring.
DEFAULT_BIND = "127.0.0.1"
#: 8080 is `preview` on the tunnel already.
DEFAULT_PORT = 8081
DEFAULT_JOBS_DIR = worker.DEFAULT_JOBS_DIR

GALLERY_URL = "https://profcarroll.github.io/sketchgen-gallery/"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
SAMPLE_CONSOLE = REPO_ROOT / "tests" / "fixtures" / "console" / "sample.json"

#: How long ``--once-for-test`` serves before it gives up and exits by itself.
ONCE_TIMEOUT_S = 60.0

LOG_TAIL_LINES = 200

#: One SSE stream is one thread for as long as the browser tab is open, so the
#: number of them is capped rather than left to the client. Nine tabs on a
#: single-user UI is a bug somewhere; the ninth gets 503, not a thread.
MAX_STREAMS = 8
#: How often the stream looks at the log file and at the job row.
STREAM_POLL_S = 0.5
#: How often a ``state`` event is sent even when nothing has changed.
STATE_EVERY_S = 2.0
#: How often a ``: keepalive`` comment is sent so an idle stream stays alive.
KEEPALIVE_EVERY_S = 15.0
#: How many lines the page keeps in ``#log`` before dropping from the top.
BROWSER_LOG_LINES = 1000

#: What ``GET /jobs/…`` will serve out of the jobs directory, and as what.
SERVABLE = {
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

#: What ``GET /preview/<job>/<n>/…`` will serve out of one attempt directory.
#: An attempt holds index.html and sketch.js (executor.py), and a sketch may
#: reference a stylesheet or an image beside them. p5 itself comes from cdnjs,
#: which the browser fetches for itself — nothing here is a proxy.
PREVIEW_SERVABLE = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".json": "application/json; charset=utf-8",
}

#: The header on every preview response.
#:
#: This is model-written code running in the operator's browser. That is the
#: same trust anyone viewing the gallery extends to the same sketch, and seeing
#: it run is the whole point of the screen — so it runs, scripts and all. The
#: header keeps it *here*: ``frame-ancestors 'self'`` lets the operator UI
#: frame the preview and nothing else, which is what X-Frame-Options was
#: reaching for and cannot say as precisely. There is deliberately no
#: ``sandbox``: an opaque origin is refused the microphone, and
#: ``responds(audio)`` is in the gate's own vocabulary — a sketch the gate
#: passed would be one the preview could not show.
PREVIEW_HEADERS = {"Content-Security-Policy": "frame-ancestors 'self'"}

#: The preview frame, in CSS pixels. The sketches size themselves to the
#: window, so this is a window rather than a crop.
PREVIEW_W, PREVIEW_H = 640, 400

#: GitHub usernames and nothing else ever goes in ``submitted_by`` (course policy).
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

TERMINAL_STATES = frozenset({"published", "rejected", "failed"})

# What a state is CALLED on screen. The database keeps its names; the UI says
# who rejected the work. Anything not listed is shown as its state name.
STATE_LABELS = {"failed": "rejected · gate", "rejected": "rejected · operator"}


def state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


class Refused(Exception):
    """A refusal, in delegate.py's sense: exit 3, one line, nothing done."""


# ---------------------------------------------------------------------------
# Configuration carried by the server object
# ---------------------------------------------------------------------------


@dataclass
class App:
    """Everything a request handler needs that is not in the request."""

    db_path: str
    jobs_dir: str
    once_for_test: bool = False
    quit_event: threading.Event | None = None

    def connect(self) -> sqlite3.Connection:
        return db.connect(self.db_path)

    @property
    def jobs_root(self) -> Path:
        return Path(self.jobs_dir).expanduser()


def check_bind(bind: str) -> str:
    """The loopback check. Anything but 127.0.0.1 is a refusal, not a warning."""
    if bind != DEFAULT_BIND:
        raise Refused(
            f"--bind {bind}: this UI has no authentication and binds "
            f"{DEFAULT_BIND} only"
        )
    return bind


# ---------------------------------------------------------------------------
# Small formatting helpers. Everything from the database goes through esc().
# ---------------------------------------------------------------------------


def esc(value: Any) -> str:
    """HTML-escape anything, including None, quotes included."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _dig(doc: Any, path: str, default: Any = None) -> Any:
    """Walk a dotted path through dicts and lists; missing is ``default``.

    The same path string is written into ``data-k``/``data-bar`` attributes, so
    the refresh script walks the identical route through the identical document.
    """
    current = doc
    for part in path.split("."):
        if current is None:
            return default
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(current, dict):
            if part not in current:
                return default
            current = current[part]
        else:
            return default
    return default if current is None else current


def _fmt(value: Any, kind: str = "str") -> str:
    """Format one value. Mirrored, kind for kind, by ``fmt()`` in the page script.

    The collector's document is the unit of record and nothing here rewrites
    it: ``gb`` prints the megabytes it reports as gigabytes, ``pct01`` prints a
    rate in 0..1 as a percentage, ``pct`` one that is already 0..100.
    """
    if value is None:
        return "—"
    try:
        if kind == "int":
            return f"{int(round(float(value))):,}"
        if kind == "f1":
            return f"{float(value):.1f}"
        if kind == "f2":
            return f"{float(value):.2f}"
        if kind == "f4":
            return f"{float(value):.4f}"
        if kind == "pct":
            return f"{float(value):.1f}%"
        if kind == "pct01":
            return f"{float(value) * 100.0:.1f}%"
        if kind == "hours":
            return f"{float(value) / 3600.0:.1f}"
        if kind == "gb":
            return f"{float(value) / 1024.0:.1f}"
    except (TypeError, ValueError):
        return esc(value)
    return str(value)


def field(doc: Any, path: str, kind: str = "str") -> str:
    """One live-patched span: the value now, and where to find it next time."""
    return (
        f'<span data-k="{esc(path)}" data-fmt="{esc(kind)}">'
        f"{esc(_fmt(_dig(doc, path), kind))}</span>"
    )


def _width(value: Any) -> float:
    try:
        return min(100.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def bar(doc: Any, path: str, css_class: str = "") -> str:
    """One live-patched bar segment, from a value that is already a percentage."""
    klass = f' class="{esc(css_class)}"' if css_class else ""
    return (
        f'<span{klass} data-bar="{esc(path)}" '
        f'style="width:{_width(_dig(doc, path)):.1f}%"></span>'
    )


def ratio_bar(doc: Any, num: str, den: str, css_class: str = "") -> str:
    """One live-patched bar segment from two paths: numerator over denominator.

    The collector reports memory in megabytes and load as a number of runnable
    processes, not as percentages of anything — the bar is the page's idea, so
    the page does the division, here and identically in the refresh script.
    """
    top, bottom = _dig(doc, num), _dig(doc, den)
    try:
        width = _width(float(top) / float(bottom) * 100.0)
    except (TypeError, ValueError, ZeroDivisionError):
        width = 0.0
    klass = f' class="{esc(css_class)}"' if css_class else ""
    return (
        f'<span{klass} data-bar-num="{esc(num)}" data-bar-den="{esc(den)}" '
        f'style="width:{width:.1f}%"></span>'
    )


def _parse_utc(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def human_seconds(seconds: float | None) -> str:
    """A duration a person reads at a glance: 42s, 7m 12s, 2h 04m, 3d 5h."""
    if seconds is None:
        return "—"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def elapsed_for(job: db.Job) -> str:
    """Wall time so far for a job in flight; total wall time for a finished one."""
    start = _parse_utc(job.created_utc)
    if start is None:
        return "—"
    end = (
        _parse_utc(job.updated_utc)
        if job.state in TERMINAL_STATES or job.state == "held"
        else datetime.now(timezone.utc)
    )
    if end is None:
        return "—"
    return human_seconds((end - start).total_seconds())


def truncate(text: str | None, limit: int = 90) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# The console document: packet 4.1 if it is installed, the sample if it is not
# ---------------------------------------------------------------------------


#: ``console.collect()`` keeps one /proc/stat sample in a module global and
#: wants the previous document back so it can skip the 100 ms second reading.
#: One page and its two-second refresh are several threads, so both the call
#: and the cache it feeds are serialised here.
_COLLECT_LOCK = threading.Lock()
_LAST_DOCUMENT: dict[str, dict[str, Any]] = {}


def _call_matching(fn: Callable[..., Any], **available: Any) -> Any:
    """Call ``fn`` with whichever of ``available`` its signature actually names.

    Three modules in this build land on three branches and are merged later;
    this is how one of them calls another without pinning a signature it cannot
    see yet. A required parameter we cannot supply is a TypeError, which the
    caller turns into a flash message rather than a traceback in the browser.
    """
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return fn()
    kwargs: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        if name in available:
            kwargs[name] = available[name]
        elif parameter.default is parameter.empty:
            raise TypeError(f"{getattr(fn, '__name__', fn)} needs {name!r}")
    return fn(**kwargs)


def console_document(app: App | None = None) -> dict[str, Any]:
    """One console document, with ``source`` saying where it came from.

    ``live`` means packet 4.1's collector answered. ``sample`` means it is not
    installed, or it raised, or the database it needs is not there yet — and
    you are looking at ``tests/fixtures/console/sample.json``, which is the
    contract's own document.
    """
    collect = getattr(console, "collect", None) if console is not None else None
    if callable(collect):
        db_path = app.db_path if app else db.DEFAULT_DB_PATH
        jobs_dir = app.jobs_dir if app else DEFAULT_JOBS_DIR
        conn = None
        try:
            conn = db.connect(db_path)
            with _COLLECT_LOCK:
                document = _call_matching(
                    collect,
                    conn=conn,
                    jobs_dir=jobs_dir,
                    prev=_LAST_DOCUMENT.get(db_path),
                )
                if isinstance(document, dict):
                    _LAST_DOCUMENT[db_path] = document
            if isinstance(document, dict):
                document = dict(document)
                document["source"] = "live"
                return document
        except Exception as exc:  # the console must never take the UI down
            sys.stderr.write(f"{db.utc_now()} console collector failed: {exc}\n")
        finally:
            if conn is not None:
                conn.close()
    try:
        document = json.loads(SAMPLE_CONSOLE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"source": "sample", "error": f"no console document: {exc}"}
    document["source"] = "sample"
    return document


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, string.Template] = {}


def template(name: str) -> string.Template:
    if name not in _TEMPLATES:
        path = TEMPLATE_DIR / f"{name}.html"
        _TEMPLATES[name] = string.Template(path.read_text(encoding="utf-8"))
    return _TEMPLATES[name]


REFUSAL_MARKS = ("refused", "failed", "nothing changed", "nothing spawned", "not installed")


def is_refusal(flash: str | None) -> bool:
    """Whether a flash message reports that nothing happened, or something broke."""
    text = (flash or "").lower()
    return any(mark in text for mark in REFUSAL_MARKS)


def render(name: str, **fields: Any) -> str:
    """Fill one template. ``safe_substitute``: a stray ``$`` is not an error."""
    return template(name).safe_substitute(**fields)


NAV = (
    ("/", "Console"),
    ("/queue", "Queue"),
    ("/new", "New job"),
    ("/held", "Held"),
)


def pill_for(control: db.Control | None) -> tuple[str, str, str]:
    """The worker-state pill: (text, css class, tooltip).

    Read from the same control row the worker reads, through the worker's own
    :func:`sketchgen.worker.is_stop_now`, so the pill cannot drift from the
    behaviour it is describing.
    """
    if control is None:
        return ("UNKNOWN", "paused", "no control row in this database")
    if control.state == "running":
        return ("RUNNING", "running", "the worker claims jobs as they arrive")
    if control.state == "paused":
        return ("PAUSED", "paused", control.reason or "the worker is stopped")
    if worker.is_stop_now(control):
        return (
            "STOPPING",
            "stopping",
            "abort the attempt in flight and re-queue the job",
        )
    return ("PAUSING", "pausing", control.reason or "finishing the attempt in flight")


# ---------------------------------------------------------------------------
# The header's live marks
#
# Each nav item carries a small summary of its own page, so the node is never
# out of sight while the operator is standing somewhere else: a five-segment
# level meter on Console, a coloured triple on Queue, a superscript on Held and
# on Gallery. Every one of them is rendered here, server-side, on every page
# load — the header is right with JavaScript off — and repainted from
# /api/control.json by the two-second poll at the bottom of op_layout.html.
# ---------------------------------------------------------------------------

#: Where :func:`node_cpu_pct` reads the load from. A module constant so a test
#: on a machine with no /proc can point it at nothing and see the meter go dark.
LOADAVG_PATH = "/proc/loadavg"

#: How many segments the Console meter has. Lit segments are the CPU
#: percentage in fifths, rounded.
METER_SEGMENTS = 5

#: The tokens gauge: twenty-four five-minute bins, so the sparkline beside
#: the meter is the last two hours of finished work. Long enough to show the
#: shape of a session, short enough that one job is still a visible spike.
TOKEN_BINS = 24
TOKEN_BIN_MINUTES = 5

#: The gauge at its two sizes, (width, height) in SVG units: inline in the
#: header, and again in the Console's model panel with room to read it.
NAV_SPARK_SIZE = (72, 14)
CONSOLE_SPARK_SIZE = (240, 40)

#: What "in flight" means in the header's green number: a job the worker is
#: carrying, planning included. ``console.IN_FLIGHT_STATES`` leaves planning
#: out because that tuple is about who holds the inference slot at the moment
#: of the reading; this one is about work that is moving.
IN_FLIGHT_STATES = ("planning", "executing", "gating", "repairing")

#: The gauge with nothing in it: a flat line on the baseline and a dash
#: where the rate goes. Read, never written — nothing mutates it in place.
EMPTY_TOKENS: dict[str, Any] = {
    "bins": [0] * TOKEN_BINS,
    "bin_minutes": TOKEN_BIN_MINUTES,
    "decode_tok_s": None,
    "prefill_tok_s": None,
    "session_in": 0,
    "session_out": 0,
}

#: A summary with nothing in it: what the header draws when the database
#: cannot be counted. Zeros and an unlit meter, never a traceback.
EMPTY_NAV: dict[str, Any] = {
    "cpu_pct": 0.0,
    "slot_state": "free",
    "slot_holder": None,
    "queued": 0,
    "in_flight": 0,
    "failed_session": 0,
    "held": 0,
    "kept": 0,
    "public": 0,
    "published": 0,
    "tokens": EMPTY_TOKENS,
}


def node_cpu_pct(path: str | os.PathLike[str] = LOADAVG_PATH) -> float:
    """Node CPU for the header meter: one-minute load per core, as a percent.

    This is deliberately not the console's ``node.cpu_total_pct``. That number
    is the honest one — busy jiffies between two readings of /proc/stat — and
    the second reading costs a sleep, which is tens of milliseconds on a header
    that renders on every click of every page. /proc/loadavg is one line the
    kernel has already averaged, and it answers the question a five-segment
    meter asks: how much of this machine is spoken for right now. Divided by
    the core count and capped at 100, so a load of 32 on 16 cores lights every
    segment and stops there.

    No /proc — a test on another OS, a container without it — is 0.0 and an
    unlit meter. This never raises.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            load = float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0
    try:
        cores = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        cores = os.cpu_count() or 1
    return round(min(100.0, max(0.0, 100.0 * load / max(cores, 1))), 1)


def _nav_slot(in_flight: int) -> tuple[str, str | None]:
    """Who holds the inference slot, cheaply: (state, holder).

    :func:`sketchgen.console._slot` is the full rule and it is not free — it
    needs Ollama's resident list over HTTP and a CPU percentage per process.
    The header keeps the two branches that cost one walk of /proc or nothing at
    all: a live worker with a job in flight is **ours**, an ``opencode`` session
    is **busy** (the fence's own refusal), everything else is **free**. The
    branch left out is Ollama's "a model is resident and something is burning
    it", so the header can say free where the console says busy; the console is
    the page to check when that matters, and it is one click away.
    """
    found = {"worker": None, "opencode": None}
    finder = getattr(console, "_find_processes", None) if console else None
    if finder is not None:
        try:
            found = finder(
                {"worker": ("sketchgen", "worker"), "opencode": ("opencode",)}
            )
        except Exception:  # pragma: no cover - the header must not take the UI down
            found = {"worker": None, "opencode": None}
    if found.get("worker") is not None and in_flight:
        return ("ours", "sketchgen worker")
    if found.get("opencode") is not None:
        return ("busy", f"opencode pid {found['opencode']}")
    return ("free", None)


def token_bins(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
    bins: int = TOKEN_BINS,
    minutes: int = TOKEN_BIN_MINUTES,
) -> list[int]:
    """Tokens completed per bin over the last ``bins * minutes`` minutes.

    One number per bin, oldest first, the partial bin the clock is standing in
    rightmost: every attempt whose ``finished_utc`` falls in the window adds
    its ``prompt_tokens + completion_tokens`` to the bin it finished in. An
    attempt still running has no ``finished_utc`` and counts nowhere until it
    does — this is completed work, not work in progress, and an attempt lands
    in one bin rather than being spread over the minutes it actually ran.

    The bins roll back from ``now`` instead of being aligned to the clock, so
    each one is exactly ``minutes`` older than the one on its right and a test
    can inject ``now`` and know where a stamp belongs. Idle minutes are zeros,
    which is the point: the line lies on the floor while the node is quiet and
    spikes when it works. A database that cannot be read is all zeros — the
    header must never take the UI down.
    """
    width = max(1, int(minutes)) * 60
    count = max(1, int(bins))
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(seconds=width * count)
    out = [0] * count
    try:
        rows = conn.execute(
            "SELECT finished_utc, COALESCE(prompt_tokens, 0) "
            "+ COALESCE(completion_tokens, 0) AS tokens FROM attempts "
            "WHERE finished_utc IS NOT NULL AND finished_utc >= ?",
            (start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchall()
    except sqlite3.Error as exc:
        sys.stderr.write(f"{db.utc_now()} token bins failed: {exc}\n")
        return out
    for row in rows:
        stamp = _parse_utc(row["finished_utc"])
        if stamp is None:  # a stamp nobody here wrote: skipped, not guessed
            continue
        age = (end - stamp).total_seconds()
        index = count - 1 - int(age // width)
        if index >= count:  # a clock that has stepped: the bin we are in
            index = count - 1
        if 0 <= index < count:
            out[index] += int(row["tokens"] or 0)
    return out


def _session_tokens(conn: sqlite3.Connection) -> tuple[int, int]:
    """Tokens in and out this session: the Console odometer's own numbers.

    :func:`sketchgen.console._odometer` is the source — it sums the attempts
    table since ``meta.worker_started_utc``, and sums everything on a database
    the worker never stamped. Reused rather than re-derived so the header and
    the odometer cannot disagree; when packet 4.1 is not installed the same
    query stands in, mirroring its WHERE clause.
    """
    odometer = getattr(console, "_odometer", None) if console else None
    session_start = getattr(console, "_session_start", None) if console else None
    if callable(odometer) and callable(session_start):
        try:
            session = odometer(conn, session_start(conn))["session"]
            return int(session.get("in") or 0), int(session.get("out") or 0)
        except (sqlite3.Error, KeyError, TypeError, ValueError):
            return (0, 0)
    since = db.get_meta(conn, "worker_started_utc")
    sql = (
        "SELECT COALESCE(SUM(prompt_tokens), 0) AS tin, "
        "COALESCE(SUM(completion_tokens), 0) AS tout FROM attempts"
    )
    args: tuple = ()
    if since:
        sql += " WHERE COALESCE(started_utc, finished_utc) >= ?"
        args = (since,)
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return (0, 0)
    return int(row["tin"] or 0), int(row["tout"] or 0)


def _nav_tokens(conn: sqlite3.Connection) -> dict[str, Any]:
    """The gauge's numbers: the line, and the rates that label it.

    The line is throughput — tokens a bin — because that is what changes from
    minute to minute on this node. The decode rate is not: it sits at about 25
    tok/s for every attempt the model runs, so a sparkline of it would be a
    straight line pretending to be news. It is the label instead.
    """
    instant = {}
    rates = getattr(console, "_instant", None) if console else None
    if rates is not None:
        try:
            instant = rates(conn) or {}
        except Exception:  # pragma: no cover - the header must not fall over
            instant = {}
    session_in, session_out = _session_tokens(conn)
    return {
        "bins": token_bins(conn),
        "bin_minutes": TOKEN_BIN_MINUTES,
        "decode_tok_s": instant.get("decode_tok_s"),
        "prefill_tok_s": instant.get("prefill_tok_s"),
        "session_in": session_in,
        "session_out": session_out,
    }


def nav_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """The numbers behind the header's marks, in one pass of small counts.

    The red Queue number is failures **since the worker started**, not the
    all-time count: the worker stamps ``meta.worker_started_utc`` when it comes
    up, and a number that only ever grows is a number nobody reads. A database
    with no stamp has had no session, so it is zero rather than everything.
    """
    try:
        marks = ",".join("?" for _ in IN_FLIGHT_STATES)
        in_flight = _count(
            conn,
            f"SELECT COUNT(*) FROM jobs WHERE state IN ({marks})",
            IN_FLIGHT_STATES,
        )
        since = db.get_meta(conn, "worker_started_utc")
        summary = {
            "cpu_pct": node_cpu_pct(),
            "queued": _count(conn, "SELECT COUNT(*) FROM jobs WHERE state = 'queued'"),
            "in_flight": in_flight,
            "failed_session": (
                _count(
                    conn,
                    "SELECT COUNT(*) FROM jobs WHERE state = 'failed' "
                    "AND updated_utc >= ?",
                    (since,),
                )
                if since
                else 0
            ),
            "held": _count(conn, "SELECT COUNT(*) FROM entries WHERE state = 'held'"),
            # The kept rejections still waiting below the held cards: the ones
            # already published have left the Held page.
            "kept": _count(
                conn,
                "SELECT COUNT(*) FROM entries WHERE state = 'failed-kept' "
                "AND published_utc IS NULL",
            ),
            "public": _count(
                conn, "SELECT COUNT(*) FROM entries WHERE published_utc IS NOT NULL"
            ),
            "published": _count(
                conn,
                "SELECT COUNT(*) FROM entries WHERE state = 'published' "
                "AND published_utc IS NOT NULL",
            ),
            "tokens": _nav_tokens(conn),
        }
    except sqlite3.Error as exc:  # the header must never take the UI down
        sys.stderr.write(f"{db.utc_now()} nav summary failed: {exc}\n")
        return dict(EMPTY_NAV)
    state, holder = _nav_slot(summary["in_flight"])
    summary["slot_state"] = state
    summary["slot_holder"] = holder
    return summary


def _nav_meter(summary: dict[str, Any]) -> str:
    """The Console meter: ``[▮▮▯▯▯]``, lit segments coloured by position."""
    cpu = max(0.0, min(100.0, float(summary.get("cpu_pct") or 0.0)))
    lit = round(cpu / 100.0 * METER_SEGMENTS)
    holder = summary.get("slot_holder")
    title = "node CPU %d%% · slot %s%s" % (
        round(cpu),
        summary.get("slot_state") or "free",
        f" ({holder})" if holder else "",
    )
    segments = "".join(
        '<i class="on"></i>' if index < lit else "<i></i>"
        for index in range(METER_SEGMENTS)
    )
    return (
        f'<span class="throttle" data-nav="cpu" title="{esc(title)}" '
        f'aria-label="{esc(title)}">{segments}</span>'
    )


def _spark_points(bins: list[int], width: int, height: int) -> list[str]:
    """The sparkline's points, ``x,y`` strings, oldest first.

    THE SHARED FORMULA. ``sparkPoints()`` in op_layout.html is this function
    line for line, so the poll repaints a bin exactly where the server drew
    it:

        x(i) = 1 + i * (w - 2) / (n - 1)
        y(i) = (h - 1) - v(i) / peak * (h - 4)     — peak 0 gives y = h - 1

    One unit of padding either side, the tallest bin four units below the top
    edge, and a window with nothing in it flat on the baseline rather than
    divided by zero. Both sides round with ``floor(v * 10 + 0.5) / 10`` and
    print one decimal — JavaScript's ``toFixed`` and Python's ``%.1f`` break
    ties in opposite directions, so the tie is rounded away before either of
    them sees it and the two sides agree as strings, not merely as geometry.
    """
    count = max(1, len(bins))
    peak = max(bins) if bins else 0
    step = (width - 2) / (count - 1) if count > 1 else 0.0
    points = []
    for index in range(count):
        value = bins[index] if index < len(bins) else 0
        x = 1 + index * step
        y = (height - 1) - (value / peak * (height - 4) if peak > 0 else 0.0)
        points.append(
            f"{math.floor(x * 10 + 0.5) / 10:.1f},"
            f"{math.floor(y * 10 + 0.5) / 10:.1f}"
        )
    return points


def _spark_window(tokens: dict[str, Any]) -> tuple[str, int]:
    """How much time the gauge covers, as text, and the width of one bin."""
    minutes = int(tokens.get("bin_minutes") or TOKEN_BIN_MINUTES)
    count = len(tokens.get("bins") or []) or TOKEN_BINS
    total = minutes * count
    return (f"{total / 60:g} h" if total >= 60 else f"{total:g} min"), minutes


def _spark_title(tokens: dict[str, Any]) -> str:
    """What the gauge says on hover: the window, the peak, the rate."""
    bins = list(tokens.get("bins") or [])
    span, minutes = _spark_window(tokens)
    return (
        f"tokens completed, last {span} in {minutes}-min bins · "
        f"peak {max(bins) if bins else 0:,} · "
        f"decode {_fmt(tokens.get('decode_tok_s'), 'f1')} tok/s"
    )


def _spark_svg(
    tokens: dict[str, Any], width: int, height: int, css_class: str = "spark"
) -> str:
    """One sparkline: a filled area under a line, scaled to the window's peak.

    ``data-nav="tokens"`` is the hook the poll repaints by, and it repaints
    every match — the header's gauge and the Console's wider one are the same
    line at two sizes, and one poll keeps both of them honest.
    """
    bins = list(tokens.get("bins") or []) or [0] * TOKEN_BINS
    points = _spark_points(bins, width, height)
    line = "M" + " L".join(points)
    area = f"M1,{height - 1} L" + " L".join(points) + f" L{width - 1},{height - 1} Z"
    now_x, now_y = points[-1].split(",")
    title = _spark_title(tokens)
    return (
        f'<svg class="{esc(css_class)}" data-nav="tokens" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{esc(title)}"><title>{esc(title)}</title>'
        f'<path class="spark-area" d="{esc(area)}"/>'
        f'<path class="spark-line" d="{esc(line)}"/>'
        f'<circle class="spark-now" cx="{esc(now_x)}" cy="{esc(now_y)}" r="1.4"/>'
        "</svg>"
    )


def _nav_spark(summary: dict[str, Any]) -> str:
    """The header's gauge: two hours of throughput, then the decode rate.

    The meter beside it is now — how much of the node is spoken for at this
    instant. This is the two hours behind it, so the shape of the session is
    readable from any page: flat while the worker idles, a spike per job.
    """
    tokens = summary.get("tokens") or EMPTY_TOKENS
    width, height = NAV_SPARK_SIZE
    return (
        _spark_svg(tokens, width, height)
        + ' <span class="tok"><b class="n" data-nav="decode">'
        + esc(_fmt(tokens.get("decode_tok_s"), "f1"))
        + "</b> tok/s</span>"
    )


def console_spark(tokens: dict[str, Any] | None) -> str:
    """The Console's copy of the gauge, wide enough to read bin by bin."""
    tokens = tokens or EMPTY_TOKENS
    width, height = CONSOLE_SPARK_SIZE
    span, minutes = _spark_window(tokens)
    bins = list(tokens.get("bins") or [])
    return (
        '<div class="meter spark-wide"><div class="lab">'
        f"<span>tokens completed</span><span>last {esc(span)}, "
        f"{minutes}-min bins</span></div>"
        + _spark_svg(tokens, width, height)
        + '<div class="lab dim"><span>peak <b data-nav="peak">'
        f'{max(bins) if bins else 0:,}</b> a bin</span>'
        '<span><b data-nav="session-in">'
        f'{tokens.get("session_in") or 0:,}</b> in · <b data-nav="session-out">'
        f'{tokens.get("session_out") or 0:,}</b> out this session</span>'
        "</div></div>"
    )


def nav_html(here: str, summary: dict[str, Any]) -> str:
    """The whole nav, links and marks, with the current page underlined.

    The marks sit inside the anchors, so the number is part of the link to the
    page it counts; New job counts nothing and carries nothing.
    """
    extras = {
        "/": " " + _nav_meter(summary) + " " + _nav_spark(summary),
        "/queue": (
            ' <span class="counts" data-nav="counts">'
            f'<b class="n warn" data-nav="queued" title="jobs queued">'
            f'{summary["queued"]}</b>·'
            f'<b class="n ok" data-nav="in-flight" title="jobs in flight">'
            f'{summary["in_flight"]}</b>·'
            f'<b class="n bad" data-nav="failed" '
            f'title="jobs failed since the worker started">'
            f'{summary["failed_session"]}</b></span>'
        ),
        "/held": (
            f'<sup class="sup warn" data-nav="held" title="{summary["held"]} waiting'
            f' · {summary["kept"]} kept rejections below them">{summary["held"]}</sup>'
        ),
    }
    nav = "".join(
        '<a href="%s"%s>%s%s</a>'
        % (
            esc(path),
            ' class="here"' if path == here else "",
            esc(label),
            extras.get(path, ""),
        )
        for path, label in NAV
    )
    kept_public = summary["public"] - summary["published"]
    return nav + (
        f'<a href="{esc(GALLERY_URL)}" target="_blank" rel="noopener">Gallery '
        f'<sup class="sup" data-nav="public" title="{summary["public"]} public · '
        f'{summary["published"]} published, {kept_public} kept">'
        f'{summary["public"]}</sup> ↗</a>'
    )


def layout(
    *,
    title: str,
    here: str,
    body: str,
    control: db.Control | None,
    back: str,
    flash: str | None = None,
    page_script: str = "",
    nav_marks: dict[str, Any] | None = None,
) -> str:
    text, css, tooltip = pill_for(control)
    state = control.state if control else "unknown"
    stop_now = worker.is_stop_now(control)
    nav = nav_html(here, nav_marks or EMPTY_NAV)
    # A refusal is not a success in the same quiet box: the operator has
    # already clicked, and "nothing changed" has to be the first thing seen.
    flash_html = (
        f'<div class="{"flash warn" if is_refusal(flash) else "flash"}">{esc(flash)}</div>'
        if flash else ""
    )
    return render(
        "op_layout",
        title=esc(title),
        nav=nav,
        pill_text=esc(text),
        pill_class=esc(css),
        pill_title=esc(tooltip),
        back=esc(back),
        pause_dis=" disabled" if state != "running" else "",
        stop_dis=" disabled" if (state == "paused" or stop_now) else "",
        resume_dis=" disabled" if state == "running" else "",
        flash=flash_html,
        body=body,
        page_script=page_script,
    )


# ---------------------------------------------------------------------------
# The console page
# ---------------------------------------------------------------------------

CONSOLE_SCRIPT = """<script>
// Two seconds, one GET, no reload: every element that carries data-k gets its
// text patched and every data-bar gets its width. The paths are the ones the
// server wrote, so this stays in step with the page without knowing the page.
(function () {
  function dig(doc, path) {
    var current = doc, parts = path.split(".");
    for (var i = 0; i < parts.length; i++) {
      if (current === null || current === undefined) return null;
      current = current[parts[i]];
    }
    return current === undefined ? null : current;
  }
  function fmt(value, kind) {
    if (value === null || value === undefined) return "\\u2014";
    var n = Number(value);
    if (kind === "int") return isNaN(n) ? String(value) : Math.round(n).toLocaleString("en-US");
    if (kind === "f1") return n.toFixed(1);
    if (kind === "f2") return n.toFixed(2);
    if (kind === "f4") return n.toFixed(4);
    if (kind === "pct") return n.toFixed(1) + "%";
    if (kind === "pct01") return (n * 100).toFixed(1) + "%";
    if (kind === "hours") return (n / 3600).toFixed(1);
    if (kind === "gb") return (n / 1024).toFixed(1);
    return String(value);
  }
  function width(el, value) {
    if (value === null || isNaN(value)) return;
    el.style.width = Math.max(0, Math.min(100, value)).toFixed(1) + "%";
  }
  function tick() {
    fetch("/api/console.json", {cache: "no-store"}).then(function (r) {
      return r.json();
    }).then(function (doc) {
      var src = document.getElementById("console-source");
      if (src && doc.source) src.textContent = doc.source;
      document.querySelectorAll("[data-k]").forEach(function (el) {
        var text = fmt(dig(doc, el.getAttribute("data-k")), el.getAttribute("data-fmt") || "str");
        if (el.textContent !== text) el.textContent = text;
      });
      document.querySelectorAll("[data-bar]").forEach(function (el) {
        width(el, Number(dig(doc, el.getAttribute("data-bar"))));
      });
      // Memory is megabytes and load is a count of processes: the ratio is the
      // page's arithmetic, done here exactly as ratio_bar() does it server-side.
      document.querySelectorAll("[data-bar-num]").forEach(function (el) {
        var top = Number(dig(doc, el.getAttribute("data-bar-num")));
        var bottom = Number(dig(doc, el.getAttribute("data-bar-den")));
        width(el, bottom ? (top / bottom) * 100 : 0);
      });
    }).catch(function () {});
  }
  setInterval(tick, 2000);
})();
</script>"""


def _meter(label: str, detail: str, segments: str) -> str:
    return (
        f'<div class="meter"><div class="lab"><span>{label}</span>'
        f"<span>{detail}</span></div>"
        f'<div class="bar">{segments}</div></div>'
    )


def _tile(key: str, value: str, sub: str = "") -> str:
    return (
        f'<div class="tile"><div class="k">{key}</div>'
        f'<div class="v">{value}</div><div class="sub">{sub}</div></div>'
    )


#: The funnel's stages, in the order the pipeline walks them (spec §2). Any
#: stage the collector adds later is rendered after these, under its own key.
FUNNEL_ORDER = (
    ("generated", "generated"),
    ("revised", "revised"),
    ("passed_gate", "passed gate"),
    ("published", "published"),
    ("held", "held"),
    ("rejected", "rejected"),
    ("failed_kept", "rejections (gate), kept"),
    ("children", "children"),
)

#: The per-sketch table: one row per scope, one column per measurement.
PER_SKETCH_SCOPES = (("last", "last"), ("session", "session"), ("all", "all"))
PER_SKETCH_COLUMNS = (
    ("attempts_to_pass", "f2"),
    ("wall_s", "f1"),
    ("gate_s", "f2"),
    ("tokens_in", "int"),
    ("tokens_out", "int"),
    ("sketch_lines", "int"),
    ("first_attempt_pass_rate", "pct01"),
    ("cost_usd_16_96", "f4"),
    ("cost_usd_4_24", "f4"),
)


def console_page(doc: dict[str, Any], tokens: dict[str, Any] | None = None) -> str:
    """The wireframe's Console, rendered from packet 4.1's document.

    Every value is read by its path through :func:`_dig`, and every path is
    written into the element as ``data-k`` / ``data-bar`` / ``data-bar-num`` so
    the two-second refresh patches the same places without re-rendering. A key
    the collector does not have renders as a dash and patches to a dash.

    ``tokens`` is the header's gauge, from :func:`nav_summary` — the console
    document does not carry the bins, and neither does /api/console.json, so
    the wider copy of the sparkline in the model panel is drawn from the same
    dict the header uses and repainted by the layout's poll, not this page's.
    """
    per_core = _dig(doc, "node.cpu_pct", []) or []
    cores: list[str] = []
    for index in range(len(per_core)):
        path = f"node.cpu_pct.{index}"
        cores.append(
            f'<div class="core" data-core="{index}">'
            f"<span>c{index}</span>"
            f'<span class="bar">{bar(doc, path)}</span>'
            f"{field(doc, path, 'pct')}</div>"
        )

    meters = "".join(
        [
            _meter(
                "memory",
                f"{field(doc, 'node.mem_mb.used', 'gb')} used + "
                f"{field(doc, 'node.mem_mb.cache', 'gb')} cache of "
                f"{field(doc, 'node.mem_mb.total', 'gb')} GB, "
                f"{field(doc, 'node.mem_mb.available', 'gb')} available",
                ratio_bar(doc, "node.mem_mb.used", "node.mem_mb.total")
                + ratio_bar(doc, "node.mem_mb.cache", "node.mem_mb.total", "cache"),
            ),
            _meter(
                "swap",
                f"{field(doc, 'node.swap_mb.used', 'gb')} of "
                f"{field(doc, 'node.swap_mb.total', 'gb')} GB",
                ratio_bar(doc, "node.swap_mb.used", "node.swap_mb.total"),
            ),
            _meter(
                "disk",
                f"{field(doc, 'node.disk_gb.used', 'f1')} used, "
                f"{field(doc, 'node.disk_gb.free', 'f1')} free of "
                f"{field(doc, 'node.disk_gb.total', 'f1')} GB — "
                f"{field(doc, 'node.disk_gb.models', 'f1')} models, "
                f"{field(doc, 'node.disk_gb.chromium', 'f1')} chromium",
                ratio_bar(doc, "node.disk_gb.used", "node.disk_gb.total"),
            ),
            _meter(
                "load",
                f"{field(doc, 'node.load.0', 'f2')} / "
                f"{field(doc, 'node.load.1', 'f2')} / "
                f"{field(doc, 'node.load.2', 'f2')} on "
                f"{field(doc, 'node.cores', 'int')} cores",
                ratio_bar(doc, "node.load.0", "node.cores"),
            ),
        ]
    )

    top = _dig(doc, "node.top", []) or []
    top_rows = []
    for index in range(min(3, len(top))):
        base = f"node.top.{index}"
        top_rows.append(
            "<tr>"
            f"<td class=\"n mono\">{field(doc, base + '.pid', 'int')}</td>"
            f"<td class=\"mono\">{field(doc, base + '.cmd')}</td>"
            f"<td class=\"n\">{field(doc, base + '.cpu_pct', 'f1')}</td>"
            f"<td class=\"n\">{field(doc, base + '.mem_pct', 'f1')}</td>"
            "</tr>"
        )
    if not top_rows:
        top_rows.append('<tr><td colspan="4" class="dim">nothing running</td></tr>')

    model_tiles = "".join(
        [
            _tile(
                "slot",
                field(doc, "model.slot.state"),
                field(doc, "model.slot.holder"),
            ),
            _tile(
                "prefill",
                field(doc, "model.instant.prefill_tok_s", "f1")
                + ' <span class="dim" style="font-size:12px">tok/s</span>',
                "from attempt " + field(doc, "model.instant.from_attempt_id", "int"),
            ),
            _tile(
                "decode",
                field(doc, "model.instant.decode_tok_s", "f1")
                + ' <span class="dim" style="font-size:12px">tok/s</span>',
                field(doc, "model.instant.at_utc"),
            ),
            _tile(
                "gate launch",
                field(doc, "model.gate_launch_s", "f2")
                + ' <span class="dim" style="font-size:12px">s</span>',
                "chromium, from the last report",
            ),
        ]
    )

    resident = _dig(doc, "model.resident", []) or []
    resident_rows = []
    for index in range(len(resident)):
        base = f"model.resident.{index}"
        resident_rows.append(
            "<tr>"
            f"<td class=\"mono\">{field(doc, base + '.name')}</td>"
            f"<td class=\"n\">{field(doc, base + '.size_gb', 'f2')}</td>"
            f"<td>{field(doc, base + '.processor')}</td>"
            f"<td class=\"n\">{field(doc, base + '.context', 'int')}</td>"
            f"<td class=\"mono dim\">{field(doc, base + '.until_utc')}</td>"
            "</tr>"
        )
    if not resident_rows:
        resident_rows.append(
            '<tr><td colspan="5" class="dim">no model resident</td></tr>'
        )

    worker_tiles = "".join(
        [
            _tile(
                "control",
                field(doc, "worker.control"),
                field(doc, "worker.reason"),
            ),
            _tile(
                "job in flight",
                field(doc, "worker.job_in_flight", "int"),
                "since " + field(doc, "worker.updated_utc"),
            ),
            _tile(
                "worker up since",
                field(doc, "worker.started_utc"),
                "a systemd --user unit, not a tool call",
            ),
        ]
    )

    odometer = "".join(
        [
            _tile(
                "session tokens in",
                field(doc, "odometer.session.in", "int"),
                "since " + field(doc, "odometer.session.since_utc"),
            ),
            _tile(
                "session tokens out",
                field(doc, "odometer.session.out", "int"),
                "kept apart from in, on purpose",
            ),
            _tile(
                "session wall",
                field(doc, "odometer.session.wall_s", "hours")
                + ' <span class="dim">h</span>',
                "worker time this session",
            ),
            _tile(
                "slot ours",
                field(doc, "odometer.session.slot_ours_pct", "pct"),
                "the rest of the session was somebody else's",
            ),
            _tile(
                "total tokens in",
                field(doc, "odometer.total.in", "int"),
                "every job, all time",
            ),
            _tile(
                "total tokens out",
                field(doc, "odometer.total.out", "int"),
                "every job, all time",
            ),
            _tile(
                "total wall",
                field(doc, "odometer.total.wall_s", "hours")
                + ' <span class="dim">h</span>',
                "MEASURE[job-wall-time]",
            ),
        ]
    )

    funnel = _dig(doc, "funnel", {}) or {}
    names = [key for key, _ in FUNNEL_ORDER if key in funnel]
    names += [key for key in funnel if key not in dict(FUNNEL_ORDER)]
    labels = dict(FUNNEL_ORDER)
    funnel_rows = []
    for name in names:
        base = f"funnel.{name}"
        funnel_rows.append(
            "<tr>"
            f"<td>{esc(labels.get(name, name.replace('_', ' ')))}</td>"
            f"<td class=\"n\">{field(doc, base + '.now', 'int')}</td>"
            f"<td class=\"n\">{field(doc, base + '.session', 'int')}</td>"
            f"<td class=\"n\">{field(doc, base + '.per_day', 'f1')}</td>"
            f"<td class=\"n\">{field(doc, base + '.total', 'int')}</td>"
            "</tr>"
        )
    if not funnel_rows:
        funnel_rows.append('<tr><td colspan="5" class="dim">no jobs yet</td></tr>')

    per_sketch_rows = []
    for scope, label in PER_SKETCH_SCOPES:
        cells = "".join(
            f'<td class="n">{field(doc, f"per_sketch.{scope}.{column}", kind)}</td>'
            for column, kind in PER_SKETCH_COLUMNS
        )
        per_sketch_rows.append(f"<tr><td>{esc(label)}</td>{cells}</tr>")

    return render(
        "op_console",
        token_spark=console_spark(tokens),
        shape=esc(_dig(doc, "node.shape", "shape unknown")),
        cores_n=field(doc, "node.cores", "int"),
        source=esc(doc.get("source", "sample")),
        generated=field(doc, "utc"),
        collector_ms=field(doc, "collector_ms", "f1"),
        cpu_total=field(doc, "node.cpu_total_pct", "pct"),
        cpu_total_bar=bar(doc, "node.cpu_total_pct"),
        cores="\n".join(cores),
        meters=meters,
        top_rows="\n".join(top_rows),
        model_tiles=model_tiles,
        ollama_version=field(doc, "model.ollama_version"),
        resident_rows="\n".join(resident_rows),
        worker_tiles=worker_tiles,
        odometer=odometer,
        funnel_rows="\n".join(funnel_rows),
        per_sketch_rows="\n".join(per_sketch_rows),
        cost_a="cost 16/96",
        cost_b="cost 4/24",
    )


# ---------------------------------------------------------------------------
# The queue page
# ---------------------------------------------------------------------------


def _attempt_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {
        int(row["job_id"]): int(row["n"])
        for row in conn.execute(
            "SELECT job_id, COUNT(*) AS n FROM attempts GROUP BY job_id"
        )
    }


def _count(conn: sqlite3.Connection, sql: str, args: Iterable[Any] = ()) -> int:
    row = conn.execute(sql, tuple(args)).fetchone()
    return int(row[0]) if row else 0


def _entries_by_job(conn: sqlite3.Connection) -> dict[int, tuple[int, str]]:
    """``{job_id: (entry_id, entry_state)}`` for every job that has an entry."""
    return {
        int(row["job_id"]): (int(row["id"]), str(row["state"]))
        for row in conn.execute("SELECT id, job_id, state FROM entries")
    }


def entry_cell(entry: tuple[int, str] | None) -> str:
    """The queue's 'entry' cell: the entry id, linked to where it is now.

    A job and its entry have different numbers (job 49 made entry 48), and
    the queue speaks in job ids while the Held page and the gallery speak in
    entry ids. This column is the crosswalk, so nobody has to carry one number
    over to the other page and get it wrong.
    """
    if entry is None:
        return '<td class="n dim">—</td>'
    entry_id, state = entry
    if state == "held":
        href = f"/held#entry-{entry_id}"
    elif state in ("published", "failed-kept"):
        href = f"{GALLERY_URL}e/{entry_id}/"
    else:
        return f'<td class="n" title="entry {entry_id}, {esc(state)}">{entry_id}</td>'
    return (
        f'<td class="n"><a href="{esc(href)}" title="entry {entry_id}, {esc(state)}" '
        f'onclick="event.stopPropagation()">{entry_id}</a></td>'
    )


def queue_page(conn: sqlite3.Connection, doc: dict[str, Any], control) -> str:
    jobs = list(reversed(db.list_jobs(conn)))
    counts = _attempt_counts(conn)
    entries = _entries_by_job(conn)
    today = db.utc_now()[:10]
    pill_text, pill_class, _ = pill_for(control)

    def tile(key: str, value: str, sub: str) -> str:
        return (
            f'<div class="tile"><div class="k">{key}</div>'
            f'<div class="v">{value}</div><div class="sub">{sub}</div></div>'
        )

    tiles = "".join(
        [
            tile(
                "slot",
                esc(_dig(doc, "model.slot.state", "—")),
                esc(truncate(_dig(doc, "model.slot.holder", "nobody"), 40)),
            ),
            tile(
                "worker",
                f'<span class="pill {esc(pill_class)}">{esc(pill_text)}</span>',
                esc((control.reason if control else None) or "—"),
            ),
            tile(
                "depth",
                str(_count(conn, "SELECT COUNT(*) FROM jobs WHERE state = 'queued'")),
                "queued, waiting for the slot",
            ),
            tile(
                "today",
                "%d / %d"
                % (
                    _count(
                        conn,
                        "SELECT COUNT(*) FROM jobs WHERE state = 'published' "
                        "AND updated_utc LIKE ?",
                        (today + "%",),
                    ),
                    _count(
                        conn,
                        "SELECT COUNT(*) FROM jobs WHERE state = 'failed' "
                        "AND updated_utc LIKE ?",
                        (today + "%",),
                    ),
                ),
                "published / failed",
            ),
            tile("shape", esc(_dig(doc, "node.shape", "—")), "the machine underneath"),
            tile(
                "needs laptop",
                str(
                    _count(
                        conn, "SELECT COUNT(*) FROM jobs WHERE state = 'needs-laptop'"
                    )
                ),
                "waiting on the paid path",
            ),
        ]
    )

    rows = []
    for job in jobs:
        attempts = counts.get(job.id, 0)
        rows.append(
            f'<tr class="job" onclick="location=\'/job/{job.id}\'">'
            f'<td class="n"><a href="/job/{job.id}">{job.id}</a></td>'
            f"{entry_cell(entries.get(job.id))}"
            f"<td>{esc(truncate(job.prompt))}</td>"
            f'<td><span class="pill {esc(job.state)}">{esc(state_label(job.state))}</span></td>'
            f'<td class="n">{attempts}/{esc(job.max_attempts)}</td>'
            f"<td class=\"mono\">{esc(job.executor or '—')}</td>"
            f"<td>{esc(job.rules_file or '—')}</td>"
            f'<td class="n">{esc(elapsed_for(job))}</td>'
            f"<td>{esc(job.submitted_by)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td colspan="9" class="dim">nothing queued yet — '
            '<a href="/new">write the first job</a></td></tr>'
        )

    return render("op_queue", tiles=tiles, rows="\n".join(rows))


# ---------------------------------------------------------------------------
# The new-job form
# ---------------------------------------------------------------------------

PLANNER_CHOICES = (
    ("local", f"local — {worker.DEFAULT_PLANNER_MODEL}"),
    ("paid", "paid — the laptop claims it (needs-laptop)"),
)
RULES_CHOICES = (
    ("treatment", "treatment — the rules the course teaches"),
    ("control", "control — the A/B control file"),
    ("random", "random — resolved per attempt, for MEASURE[agents-md-ab]"),
)
PUBLICATION_CHOICES = (
    ("hold", "hold — a person publishes it"),
    ("auto", "auto — publish on a green gate"),
)


def _options(choices: Iterable[tuple[str, str]], selected: str) -> str:
    return "".join(
        f'<option value="{esc(value)}"'
        f'{" selected" if value == selected else ""}>{esc(label)}</option>'
        for value, label in choices
    )


def _chips(picked: set[str], size_w: str, size_h: str) -> str:
    chips = []
    for word in planner.VOCAB:
        if word == "size(w,h)":
            checked = " checked" if "size" in picked else ""
            chips.append(
                '<label class="chip"><input type="checkbox" name="assert" '
                f'value="size"{checked}>size('
                f'<input type="number" name="size_w" min="1" max="4096" '
                f'value="{esc(size_w)}"> , '
                f'<input type="number" name="size_h" min="1" max="4096" '
                f'value="{esc(size_h)}">)</label>'
            )
            continue
        checked = " checked" if word in picked else ""
        chips.append(
            '<label class="chip"><input type="checkbox" name="assert" '
            f'value="{esc(word)}"{checked}>{esc(word)}</label>'
        )
    return "\n".join(chips)


def new_page(form: dict[str, list[str]] | None = None, error: str | None = None) -> str:
    form = form or {}

    def one(name: str, default: str = "") -> str:
        values = form.get(name) or []
        return values[0] if values else default

    picked = set(form.get("assert") or [])
    return render(
        "op_new",
        error=f'<p class="err">{esc(error)}</p>' if error else "",
        prompt=esc(one("prompt")),
        submitted_by=esc(one("submitted_by")),
        planner_options=_options(PLANNER_CHOICES, one("planner", "local")),
        parent_entry_id=esc(one("parent_entry_id")),
        chips=_chips(picked, one("size_w", "400"), one("size_h", "400")),
        rules_options=_options(RULES_CHOICES, one("rules", "treatment")),
        publication_options=_options(PUBLICATION_CHOICES, one("publication", "hold")),
        max_attempts=esc(one("max_attempts", "3")),
    )


def _assertions_from(form: dict[str, list[str]]) -> tuple[list[str], str | None]:
    """The ticked chips, as vocabulary words. Returns (words, error)."""
    ticked = form.get("assert") or []
    if not ticked:
        # Nothing ticked means no override: the planner proposes the list later.
        # (planner.validate() would helpfully add motion(idle) here, which would
        # turn "no opinion" into an assertion the operator never made.)
        return [], None
    words: list[str] = []
    for value in ticked:
        if value == "size":
            width = (form.get("size_w") or ["400"])[0]
            height = (form.get("size_h") or ["400"])[0]
            if not (width.isdigit() and height.isdigit()):
                return [], "size(w,h) needs two whole numbers"
            words.append(f"size({int(width)},{int(height)})")
        else:
            words.append(value)
    ok, rejected = planner.validate(words)
    if rejected:
        names = ", ".join(str(item.get("word", item)) for item in rejected)
        return [], f"not in the gate's vocabulary: {names}"
    return ok, None


def create_job(conn: sqlite3.Connection, form: dict[str, list[str]]) -> tuple[int, int]:
    """Validate the form and enqueue. Returns (job id, queue position).

    Raises ValueError with the message the form should show.
    """

    def one(name: str, default: str = "") -> str:
        values = form.get(name) or []
        return (values[0] if values else default).strip()

    prompt = one("prompt")
    if not prompt:
        raise ValueError("a job needs a prompt")
    submitted_by = one("submitted_by")
    if not USERNAME_RE.match(submitted_by):
        raise ValueError(
            "submitted by must be a GitHub username — letters, digits and "
            "hyphens, nothing else (course policy: no personal data)"
        )
    planner_choice = one("planner", "local")
    if planner_choice not in {value for value, _ in PLANNER_CHOICES}:
        raise ValueError("planner must be local or paid")
    rules = one("rules", "treatment")
    if rules not in {value for value, _ in RULES_CHOICES}:
        raise ValueError("rules must be control, treatment or random")
    publication = one("publication", "hold")
    if publication not in {value for value, _ in PUBLICATION_CHOICES}:
        raise ValueError("publication must be hold or auto")
    raw_attempts = one("max_attempts", "3")
    if not raw_attempts.isdigit() or not 1 <= int(raw_attempts) <= 10:
        raise ValueError("max attempts must be a whole number from 1 to 10")

    parent_raw = one("parent_entry_id")
    parent: int | None = None
    if parent_raw:
        if not parent_raw.isdigit():
            raise ValueError("parent entry id must be a number")
        parent = int(parent_raw)
        if not conn.execute(
            "SELECT 1 FROM entries WHERE id = ?", (parent,)
        ).fetchone():
            raise ValueError(f"there is no entry {parent} to descend from")

    words, problem = _assertions_from(form)
    if problem:
        raise ValueError(problem)

    options: dict[str, Any] = {
        "planner": (
            "paid" if planner_choice == "paid" else worker.DEFAULT_PLANNER_MODEL
        ),
        "rules_file": rules,
        "publication": publication,
        "max_attempts": int(raw_attempts),
        "parent_entry_id": parent,
    }
    if words:
        options["assertions_json"] = json.dumps(words)
    job_id = db.enqueue(conn, prompt, submitted_by, **options)
    position = _count(
        conn,
        "SELECT COUNT(*) FROM jobs WHERE state = 'queued' AND "
        "(created_utc, id) <= (SELECT created_utc, id FROM jobs WHERE id = ?)",
        (job_id,),
    )
    return job_id, position


# ---------------------------------------------------------------------------
# The job page
# ---------------------------------------------------------------------------


def log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def _report_for(app: App, job_id: int, attempt: db.Attempt) -> dict[str, Any] | None:
    candidates = []
    if attempt.gate_report_path:
        candidates.append(Path(attempt.gate_report_path))
    candidates.append(
        app.jobs_root / str(job_id) / f"attempt-{attempt.n}" / ".gate" / "report.json"
    )
    for candidate in candidates:
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def _check_list(report: dict[str, Any] | None) -> str:
    if not report:
        return '<p class="dim">no report.json for this attempt</p>'
    items = []
    for name, value in (report.get("checks") or {}).items():
        mark, css = ("✓", "yes") if value is True else (
            ("✗", "no") if value is False else ("–", "na")
        )
        items.append(
            f'<li><span class="{css}">{mark}</span> <span class="mono">{esc(name)}</span></li>'
        )
    for name, value in (report.get("assertions") or {}).items():
        passed = bool((value or {}).get("pass")) if isinstance(value, dict) else bool(value)
        mark, css = ("✓", "yes") if passed else ("✗", "no")
        detail = (value or {}).get("detail") if isinstance(value, dict) else None
        items.append(
            f'<li><span class="{css}">{mark}</span> <span class="mono">{esc(name)}</span>'
            + (f' <span class="dim">— {esc(detail)}</span>' if detail else "")
            + "</li>"
        )
    if not items:
        return '<p class="dim">the report named no checks</p>'
    return '<ul class="checks">' + "".join(items) + "</ul>"


def attempt_dir(app: App, job_id: int, attempt_n: int) -> Path:
    """Where the executor wrote one attempt. The preview's root, and its fence."""
    return app.jobs_root / str(job_id) / f"attempt-{attempt_n}"


def has_preview(app: App, job_id: int, attempt_n: int) -> bool:
    """True when this attempt has an index.html to run."""
    return (attempt_dir(app, job_id, attempt_n) / "index.html").is_file()


PREVIEW_NOTE = (
    "The preview runs unseeded, on the real clock, in your browser — the gate "
    "ran it seeded and frozen, so this will not match gate.png frame for frame."
)


def preview_frame(
    app: App, job_id: int, attempt_n: int, *, summary: str | None = None
) -> str:
    """The attempt, running. Empty string when there is nothing to run.

    ``summary`` wraps the frame in a collapsed ``<details>`` — the job page
    shows the latest attempt open and every earlier attempt folded away, so a
    five-attempt job is not five sketches running at once.
    """
    if not has_preview(app, job_id, attempt_n):
        return ""
    url = f"/preview/{job_id}/{attempt_n}/"
    frame = (
        f'<iframe class="preview" src="{esc(url)}" '
        f'width="{PREVIEW_W}" height="{PREVIEW_H}" loading="lazy" '
        f'title="job {job_id}, attempt {attempt_n}, running"></iframe>'
        f'<p class="dim" style="font-size:12px">'
        f'<a href="{esc(url)}" target="_blank" rel="noopener">open in a tab ↗</a>'
        f" — {esc(PREVIEW_NOTE)}</p>"
    )
    if summary is None:
        return frame
    return (
        f'<details class="preview-fold"><summary>{esc(summary)}</summary>'
        f"{frame}</details>"
    )


def _artefacts(app: App, job_id: int, attempt_n: int) -> str:
    shots = []
    gate_dir = app.jobs_root / str(job_id) / f"attempt-{attempt_n}" / ".gate"
    for name, caption in (("strip.png", "four frames"), ("gate.png", "last frame")):
        if (gate_dir / name).is_file():
            url = f"/jobs/{job_id}/attempt-{attempt_n}/.gate/{name}"
            shots.append(
                f'<figure class="shot" style="margin:0"><img src="{esc(url)}" '
                f'alt="{esc(caption)}" loading="lazy">'
                f'<figcaption class="dim" style="font-size:12px">'
                f'<a href="{esc(url)}">{esc(name)}</a> — {esc(caption)}</figcaption></figure>'
            )
    if not shots:
        return '<p class="dim">no artefacts on disk for this attempt</p>'
    return '<div class="cards">' + "".join(shots) + "</div>"


def job_page(app: App, conn: sqlite3.Connection, job: db.Job) -> str:
    attempts = db.list_attempts(conn, job.id)
    attempt_n = attempts[-1].n if attempts else 0
    entry = conn.execute(
        "SELECT * FROM entries WHERE job_id = ?", (job.id,)
    ).fetchone()

    def tile(key: str, value: str) -> str:
        return (
            f'<div class="tile"><div class="k">{key}</div>'
            f'<div class="v" style="font-size:14px">{value}</div></div>'
        )

    tiles = "".join(
        [
            tile("submitted by", esc(job.submitted_by)),
            tile("executor", esc(job.executor or "—")),
            tile("rules", esc(job.rules_file or "—")),
            tile("publication", esc(job.publication)),
            tile("elapsed", esc(elapsed_for(job))),
            tile("needs", esc(job.needs or "—")),
        ]
    )

    blocks = []
    for attempt in attempts:
        report = _report_for(app, job.id, attempt)
        verdict = (
            "passed"
            if attempt.gate_exit == 0
            else ("refused" if attempt.gate_exit == 3 else "failed")
        )
        if attempt.gate_exit is None:
            verdict = "no gate run"
        evidence = (
            f"<h3>Evidence fed to the next attempt</h3><pre>{esc(attempt.evidence)}</pre>"
            if attempt.evidence
            else ""
        )
        blocks.append(
            '<section class="panel">'
            f"<h2>Attempt {attempt.n} — <span style='text-transform:none'>"
            f"gate {esc(verdict)}"
            + (f" (exit {attempt.gate_exit})" if attempt.gate_exit is not None else "")
            + "</span></h2>"
            f'<div class="grid"><div>{_check_list(report)}</div>'
            f"<div>{_artefacts(app, job.id, attempt.n)}</div></div>"
            + preview_frame(
                app, job.id, attempt.n, summary=f"Run attempt {attempt.n}"
            )
            + f"{evidence}"
            '<p class="dim" style="font-size:12px">'
            f"model {esc(attempt.model or '—')} · rules {esc(attempt.rules_file or '—')} · "
            f"prompt {esc(attempt.prompt_version or '—')} · "
            f"{esc(attempt.prompt_tokens or 0)} in / {esc(attempt.completion_tokens or 0)} out · "
            f"{esc(human_seconds(attempt.wall_s))} wall"
            "</p></section>"
        )
    if not blocks:
        blocks.append(
            '<section class="panel"><h2>Attempts</h2>'
            '<p class="dim">no attempt has run yet</p></section>'
        )

    own = conn.execute(
        "SELECT id, state FROM entries WHERE job_id = ?", (job.id,)
    ).fetchone()
    rows = [
        ("state", job.state),
        ("entry", f"entry {own['id']} ({own['state']})" if own is not None else "— (no entry yet)"),
        ("prompt", job.prompt),
        ("brief", job.brief or "— (the planner has not run)"),
        ("assertions", ", ".join(job.assertions) or "—"),
        ("planner", job.planner or "—"),
        ("executor", job.executor or "—"),
        ("rules file", job.rules_file or "—"),
        ("parent entry", job.parent_entry_id if job.parent_entry_id else "—"),
        ("critique", job.critique or "—"),
        ("critique by", job.critique_by or "—"),
        ("submitted by", job.submitted_by),
        ("created (UTC)", job.created_utc),
        ("updated (UTC)", job.updated_utc),
        ("last error", job.last_error or "—"),
    ]
    provenance = "".join(
        f'<tr><th style="width:10em">{esc(key)}</th><td>{esc(value)}</td></tr>'
        for key, value in rows
    )

    terminal = job.state in TERMINAL_STATES
    # Packet 5.3: the spawn form needs an entry to spawn from, and a job that
    # has not produced one yet has nothing to critique.
    spawn = (
        '<section class="panel"><h2>Lineage</h2>'
        f'<div class="actions">{spawn_form(entry, f"/job/{job.id}")}</div>'
        "</section>"
        if entry is not None
        else ""
    )
    # The latest attempt, running, above the fold: the instructor's reason for
    # this screen is to look at the sketch before publishing it, and a frame
    # strip is not a sketch.
    latest = preview_frame(app, job.id, attempt_n) if attempt_n else ""
    preview = (
        '<section class="panel"><h2>Running sketch — '
        f"<span style='text-transform:none'>attempt {attempt_n}</span></h2>"
        f"{latest}</section>"
        if latest
        else ""
    )
    return render(
        "op_job",
        id=job.id,
        preview=preview,
        spawn=spawn,
        state=esc(state_label(job.state)),
        state_class=esc(job.state),
        attempt_n=attempt_n,
        max_attempts=esc(job.max_attempts),
        prompt=esc(job.prompt),
        tiles=tiles,
        log_lines=LOG_TAIL_LINES,
        log=esc(log_tail(app.jobs_root / str(job.id) / "job.log")),
        attempts="\n".join(blocks),
        provenance=provenance,
        cancel_dis=" disabled" if terminal else "",
        laptop_dis=(
            "" if "needs-laptop" in db.TRANSITIONS.get(job.state, frozenset()) else " disabled"
        ),
    )


# ---------------------------------------------------------------------------
# The live transcript: one SSE stream per open job page (packet 4.3)
# ---------------------------------------------------------------------------

#: Streams in flight, and the lock that counts them. ThreadingHTTPServer gives
#: each stream its own thread and a stream lives as long as its reader does, so
#: this is the only thing standing between an open tab per job and a thread per
#: tab. Released in the handler's ``finally``, which runs when the client goes
#: away because the next write raises.
_STREAM_LOCK = threading.Lock()
_STREAMS = 0


def live_streams() -> int:
    """How many transcript streams are open right now."""
    with _STREAM_LOCK:
        return _STREAMS


def _take_stream() -> bool:
    global _STREAMS
    with _STREAM_LOCK:
        if _STREAMS >= MAX_STREAMS:
            return False
        _STREAMS += 1
        return True


def _drop_stream() -> None:
    global _STREAMS
    with _STREAM_LOCK:
        _STREAMS = max(0, _STREAMS - 1)


def sse(data: str, event: str | None = None) -> bytes:
    """One SSE frame. A multi-line payload becomes several ``data:`` lines."""
    head = f"event: {event}\n" if event else ""
    body = "".join(f"data: {line}\n" for line in data.split("\n"))
    return (head + body + "\n").encode("utf-8")


class LogTailer:
    """Follow a growing file by byte offset and hand back whole lines only.

    ``tail -f`` in twenty lines, with the two things a naive version gets
    wrong: a write that lands mid-line is held in ``buffer`` until its newline
    arrives, and a file that has become *shorter* than the offset (truncated,
    or replaced by a new job.log) restarts from zero instead of reading from
    the middle of a line forever.
    """

    def __init__(self, path: Path | str, tail_lines: int = LOG_TAIL_LINES) -> None:
        self.path = Path(path)
        self.tail_lines = tail_lines
        self.offset = 0
        self.buffer = b""

    def initial(self) -> list[str]:
        """The last ``tail_lines`` lines on disk now; the offset lands at EOF."""
        return self._absorb(self._read_from(0))[-self.tail_lines :]

    def poll(self) -> list[str]:
        """Whatever whole lines have been appended since the last call."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self.offset:  # truncated or replaced: start again from 0
            self.offset = 0
            self.buffer = b""
            return self._absorb(self._read_from(0))
        if size == self.offset:
            return []
        return self._absorb(self._read_from(self.offset))

    def _read_from(self, start: int) -> bytes:
        try:
            with open(self.path, "rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:  # not written yet, or gone: try again next poll
            return b""
        self.offset = start + len(data)
        return data

    def _absorb(self, data: bytes) -> list[str]:
        if not data:
            return []
        self.buffer += data
        if b"\n" not in self.buffer:
            return []
        whole, _, self.buffer = self.buffer.rpartition(b"\n")
        return whole.decode("utf-8", "replace").split("\n")


def job_state(conn: sqlite3.Connection, job: db.Job) -> dict[str, Any]:
    """The small document a ``state`` event carries, and the head of the JSON."""
    attempts = db.list_attempts(conn, job.id)
    return {
        "id": job.id,
        "state": job.state,
        "attempt_n": attempts[-1].n if attempts else 0,
        "attempts": len(attempts),
        "max_attempts": job.max_attempts,
        "needs": job.needs,
        "elapsed": elapsed_for(job),
        "terminal": job.state in TERMINAL_STATES,
        "updated_utc": job.updated_utc,
        "utc": db.utc_now(),
    }


def job_document(
    app: App, conn: sqlite3.Connection, job: db.Job, lines: int = LOG_TAIL_LINES
) -> dict[str, Any]:
    """``GET /api/job/<id>.json``: the row, its attempts and the tail.

    The state event's builder reads the first half of this and the operator
    reads all of it; one shape, so the page and a shell both see the same job.
    """
    document = job_state(conn, job)
    document["job"] = asdict(job) | {"assertions": job.assertions}
    document["attempt_rows"] = [asdict(attempt) for attempt in db.list_attempts(conn, job.id)]
    document["log_lines"] = lines
    document["log"] = log_tail(app.jobs_root / str(job.id) / "job.log", lines)
    return document


JOB_SCRIPT = """<script>
// The transcript, live. The server-rendered tail above is what a reader with
// no JS gets; when EventSource connects, the first `line` event clears it and
// the stream becomes the page — the same last-200 lines, then every line as
// the worker writes it. `state` events move the pill and the attempt counter,
// `done` means the job is finished and there is nothing left to stream.
(function () {
  var log = document.getElementById("log");
  if (!log || !window.EventSource) return;
  var id = log.getAttribute("data-job");
  if (!id) return;
  var MAX = %d;
  var cleared = false;
  var source = new EventSource("/events/job/" + encodeURIComponent(id));
  source.onopen = function () {
    var live = document.getElementById("log-live");
    if (live) live.textContent = "live";
  };
  function at_bottom() {
    // Within a line and a half of the end counts as "following": scrolling up
    // to read something is a decision, and the next line should not undo it.
    return log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  }
  source.addEventListener("line", function (event) {
    var follow = at_bottom();
    if (!cleared) { log.textContent = ""; cleared = true; follow = true; }
    log.appendChild(document.createTextNode(event.data + "\\n"));
    while (log.childNodes.length > MAX) log.removeChild(log.firstChild);
    if (follow) log.scrollTop = log.scrollHeight;
  });
  source.addEventListener("state", function (event) {
    var d;
    try { d = JSON.parse(event.data); } catch (err) { return; }
    var pill = document.getElementById("job-state");
    if (pill && d.state) { pill.textContent = d.state; pill.className = "pill " + d.state; }
    var n = document.getElementById("job-attempt");
    if (n && d.attempt_n !== undefined && d.attempt_n !== null) n.textContent = d.attempt_n;
    var max = document.getElementById("job-max-attempts");
    if (max && d.max_attempts) max.textContent = d.max_attempts;
  });
  source.addEventListener("done", function () {
    // Terminal state: the server has closed its end. Do not reconnect, do not
    // refetch — the page already shows everything there will ever be.
    source.close();
    var live = document.getElementById("log-live");
    if (live) live.textContent = "finished";
  });
  source.onerror = function () {
    var live = document.getElementById("log-live");
    if (live && source.readyState === EventSource.CLOSED) live.textContent = "disconnected";
  };
})();
</script>""" % (
    BROWSER_LOG_LINES,
)


# ---------------------------------------------------------------------------
# The held page
# ---------------------------------------------------------------------------


def _entry_rows(conn: sqlite3.Connection, state: str = "held") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entries WHERE state = ? ORDER BY created_utc DESC, id DESC",
        (state,),
    ).fetchall()


def _entry_image(app: App, row: sqlite3.Row) -> str:
    """The strip, but only when it really is inside the jobs directory."""
    raw = row["strip_path"] or row["png_path"]
    if not raw:
        return '<p class="dim">no strip on disk</p>'
    try:
        resolved = Path(raw).resolve()
        root = app.jobs_root.resolve()
    except OSError:
        return '<p class="dim">no strip on disk</p>'
    if not resolved.is_file() or not resolved.is_relative_to(root):
        return '<p class="dim">no strip on disk</p>'
    url = "/jobs/" + "/".join(resolved.relative_to(root).parts)
    return f'<img src="{esc(url)}" alt="frame strip" loading="lazy">'


def _gate_summary(conn: sqlite3.Connection, job_id: int) -> str:
    row = conn.execute(
        "SELECT n, gate_exit, evidence FROM attempts WHERE job_id = ? "
        "ORDER BY n DESC LIMIT 1",
        (job_id,),
    ).fetchone()
    if row is None:
        return "no attempt recorded"
    if row["gate_exit"] == 0:
        return f"gate passed on attempt {row['n']}"
    first = (row["evidence"] or "").strip().splitlines()
    return f"attempt {row['n']}: " + (first[0] if first else f"gate exit {row['gate_exit']}")


def _held_preview(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The running sketch for a held entry: its last attempt with an index.html.

    The entry's own ``source_dir`` is the attempt the gate passed, so that is
    the one to run; when it is missing or outside the jobs directory the
    attempts table is walked backwards instead.
    """
    job_id = int(row["job_id"])
    numbers = [
        int(item["n"])
        for item in conn.execute(
            "SELECT n FROM attempts WHERE job_id = ? ORDER BY n DESC", (job_id,)
        )
    ]
    source = row["source_dir"]
    if source:
        name = Path(str(source)).name
        if name.startswith("attempt-") and name[8:].isdigit():
            wanted = int(name[8:])
            numbers = [wanted] + [n for n in numbers if n != wanted]
    for n in numbers:
        frame = preview_frame(app, job_id, n)
        if frame:
            return frame
    return ""


def _lineage_note(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    lineage = conn.execute(
        "SELECT parent_entry_id, generation, critique_by FROM lineage "
        "WHERE child_entry_id = ?",
        (row["id"],),
    ).fetchone()
    if lineage is not None:
        return (
            f"generation {lineage['generation']}, from entry "
            f"{lineage['parent_entry_id']}"
            + (f", critiqued by {lineage['critique_by']}" if lineage["critique_by"] else "")
        )
    if row["parent_entry_id"]:
        return f"child of entry {row['parent_entry_id']}"
    return "a root prompt, no lineage"


def _decision_card(
    app: App, conn: sqlite3.Connection, row: sqlite3.Row, *, kept: bool
) -> str:
    """One entry waiting for a person: the sketch, the strip, the buttons.

    A held entry can be published or rejected. A kept rejection — the gate
    refused every attempt and the worker kept it (spec §9) — can only be
    published, onto the rejections page rather than the grid, because it is
    a rejection already.
    """
    reject = (
        ""
        if kept
        else (
            f'<form method="post" action="/held/{row["id"]}/reject">'
            '<input type="text" name="reason" placeholder="reason" '
            'style="font:inherit;padding:4px 8px;border:1px solid var(--line);'
            'border-radius:5px;background:var(--bg);color:var(--fg)">'
            f'<button type="submit" class="danger">Reject entry {row["id"]}</button></form>'
        )
    )
    return (
        f'<section class="panel card" id="entry-{row["id"]}">'
        # One number per card, and it is the entry's: every button here acts
        # on the entry. The job that made it is provenance, said in words,
        # because "Entry 48 — job 49" was read as two ids for one thing twice
        # on 2026-09-14 and the wrong one was typed into the next request.
        f"<h2>Entry {row['id']}</h2>"
        f'<p class="dim" style="font-size:12px;margin:-6px 0 8px">made by job '
        f'<a href="/job/{row["job_id"]}">{row["job_id"]}</a></p>'
        # The sketch running, then the strip the gate saw. Publication is a
        # person's decision (DECIDE[publication-gate]) and this is the part
        # of it a still frame cannot carry.
        f"{_held_preview(app, conn, row)}"
        f"{_entry_image(app, row)}"
        f"<p>{esc(truncate(row['prompt'], 200))}</p>"
        f"<p class=\"dim\" style=\"font-size:12px\">{esc(_gate_summary(conn, row['job_id']))}"
        f" · {esc(_lineage_note(conn, row))} · {esc(row['executor'] or '—')}"
        f" · rules {esc(row['rules_file'] or '—')}</p>"
        '<div class="actions">'
        f'<form method="post" action="/held/{row["id"]}/publish">'
        f'<button type="submit">Publish entry {row["id"]}</button></form>'
        f"{reject}"
        "</div>"
        f'<div class="actions">{spawn_form(row, "/held")}</div>'
        "</section>"
    )


def held_page(app: App, conn: sqlite3.Connection) -> str:
    rows = _entry_rows(conn)
    cards = [_decision_card(app, conn, row, kept=False) for row in rows]
    if not cards:
        cards.append(
            '<section class="panel"><p class="dim">nothing is waiting. '
            "A job reaches this page when its gate goes green and its "
            "publication is <em>hold</em>.</p></section>"
        )
    # Kept rejections wait for the same person. The worker marks them
    # failed-kept the moment the gate gives up; they reach the gallery's
    # rejections page only when somebody here publishes them, and the ones
    # already published (published_utc set) have left this page.
    kept = [row for row in _entry_rows(conn, "failed-kept") if not row["published_utc"]]
    kept_cards = [_decision_card(app, conn, row, kept=True) for row in kept]
    if not kept_cards:
        kept_cards.append(
            '<section class="panel"><p class="dim">no kept rejection is waiting.'
            "</p></section>"
        )
    return render(
        "op_held",
        count=len(rows),
        cards="\n".join(cards),
        kept_count=len(kept),
        kept_cards="\n".join(kept_cards),
    )


def publish_entry(app: App, conn: sqlite3.Connection, entry_id: int) -> str:
    """Hand one held entry to packet 3.2. Returns the flash message."""
    try:
        from sketchgen import publish  # type: ignore[attr-defined]
    except ImportError:
        return "publisher not installed — nothing changed, the entry is still held"
    # The packet guessed the name `publish_entry` before packet 3.2 existed;
    # what 3.2 actually exports is `publish(conn, entry_id, ...)`. Both are
    # accepted so this page does not have to be right about a name it could
    # not see, and neither being present is still "not installed".
    fn = next(
        (
            candidate
            for candidate in (
                getattr(publish, "publish_entry", None),
                getattr(publish, "publish", None),
            )
            if callable(candidate)
        ),
        None,
    )
    if fn is None:
        return "publisher not installed — nothing changed, the entry is still held"
    try:
        _call_matching(
            fn,
            conn=conn,
            entry_id=entry_id,
            id=entry_id,
            db_path=app.db_path,
            path=app.db_path,
            jobs_dir=app.jobs_dir,
        )
    except Exception as exc:
        return f"publish failed: {exc}"
    return f"Entry {entry_id} handed to the publisher"


def reject_entry(conn: sqlite3.Connection, entry_id: int, reason: str) -> str:
    """Move one held entry to rejected, with its job. Returns the flash message."""
    row = conn.execute(
        "SELECT id, job_id, state FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        return f"there is no entry {entry_id}"
    if row["state"] != "held":
        return f"entry {entry_id} is {row['state']}, not held — nothing changed"
    reason = reason.strip() or "rejected by operator"
    for name in ("reject_entry", "set_entry_state", "entry_transition"):
        helper = getattr(db, name, None)
        if callable(helper):
            try:
                _call_matching(
                    helper,
                    conn=conn,
                    entry_id=entry_id,
                    id=entry_id,
                    state="rejected",
                    new_state="rejected",
                    reason=reason,
                )
                break
            except Exception:
                continue
    else:
        conn.execute(
            "UPDATE entries SET state = 'rejected' WHERE id = ?", (entry_id,)
        )
    job = db.get_job(conn, int(row["job_id"]))
    if job is not None and job.state == "held":
        db.transition(conn, job.id, "rejected", last_error=reason)
    return f"Entry {entry_id} rejected — {reason}"


# ---------------------------------------------------------------------------
# Spawning a child from a critique (packet 5.3)
# ---------------------------------------------------------------------------


def operator_username(row: sqlite3.Row | None = None) -> str:
    """Who the UI signs a critique as, by default.

    ``$SKETCHGEN_OPERATOR`` when it is set to something that is a GitHub
    username, the entry's own submitter when it is not, and the literal
    ``operator`` when there is neither. The field is editable on the form, so
    this is a default and never a claim: ``critique_by`` is a model id when a
    model wrote the sentence.
    """
    name = (os.environ.get("SKETCHGEN_OPERATOR") or "").strip()
    if name and USERNAME_RE.match(name):
        return name
    if row is not None:
        submitter = (row["submitted_by"] or "").strip()
        if USERNAME_RE.match(submitter):
            return submitter
    return "operator"


def spawn_form(row: sqlite3.Row, back: str) -> str:
    """The 'Spawn a child' form for one entry. Posts to /entry/<id>/spawn."""
    entry_id = int(row["id"])
    spawnable = row["state"] in lineage.SPAWNABLE
    note = (
        "the critique becomes the next prompt, under 'Revise:'"
        if spawnable
        else f"entry {entry_id} is {row['state']}: a line grows from a published "
        "or failed-kept entry, so this will be refused until it is one"
    )
    return (
        f'<form method="post" action="/entry/{entry_id}/spawn" class="spawn">'
        f'<input type="hidden" name="back" value="{esc(back)}">'
        f'<label for="critique-{entry_id}">Spawn a child of entry {entry_id} from a critique</label>'
        f'<input type="text" id="critique-{entry_id}" name="critique" required '
        'maxlength="400" placeholder="one sentence: what the child should do '
        'differently" style="font:inherit;padding:4px 8px;border:1px solid '
        'var(--line);border-radius:5px;background:var(--bg);color:var(--fg);'
        'min-width:22em">'
        f'<input type="text" name="critique_by" value="{esc(operator_username(row))}" '
        'title="model id or GitHub username — who wrote the critique" '
        'style="font:inherit;padding:4px 8px;border:1px solid var(--line);'
        'border-radius:5px;background:var(--bg);color:var(--fg);max-width:12em">'
        f'<button type="submit">Spawn a child of entry {entry_id}</button>'
        f'<span class="dim" style="font-size:12px">{esc(note)}</span>'
        "</form>"
    )


def spawn_child(conn: sqlite3.Connection, entry_id: int, form: dict) -> str:
    """Hand one critique to lineage.spawn(). Returns the flash message."""
    critique = (form.get("critique") or [""])[0].strip()
    by = (form.get("critique_by") or [""])[0].strip()
    row = conn.execute(
        "SELECT * FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        return f"there is no entry {entry_id}"
    if not critique:
        return "a critique with no text spawns nothing — nothing changed"
    who = by or operator_username(row)
    try:
        job_id = lineage.spawn(
            conn,
            parent_entry_id=entry_id,
            critique=critique,
            critique_by=who,
            submitted_by=operator_username(row),
        )
    except ValueError as exc:
        return f"refused: {exc}"
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        return f"spawn failed: {exc}"
    if job_id is None:
        return (
            f"refused: entry {entry_id} is {row['state']} — nothing spawned; "
            "a line grows from a held, published or kept entry, not a rejected one"
        )
    job = db.get_job(conn, job_id)
    generation = lineage.generation_of(conn, entry_id) + 1
    if job is not None and job.needs == "review":
        return (
            f"Queued as #{job_id} — generation {generation} is at the depth "
            f"limit ({lineage.DEFAULT_MAX_DEPTH}), so it is held and needs a "
            "person before it goes any further"
        )
    return f"Queued as #{job_id} — generation {generation} of entry {entry_id}"


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern[str], str]] = [
    ("GET", re.compile(r"^/$"), "page_console"),
    ("GET", re.compile(r"^/api/console\.json$"), "api_console"),
    ("GET", re.compile(r"^/api/control\.json$"), "api_control"),
    ("GET", re.compile(r"^/queue$"), "page_queue"),
    ("GET", re.compile(r"^/new$"), "page_new"),
    ("POST", re.compile(r"^/new$"), "post_new"),
    ("GET", re.compile(r"^/api/job/(?P<job_id>\d+)\.json$"), "api_job"),
    ("GET", re.compile(r"^/events/job/(?P<job_id>\d+)$"), "events_job"),
    ("GET", re.compile(r"^/job/(?P<job_id>\d+)$"), "page_job"),
    ("POST", re.compile(r"^/job/(?P<job_id>\d+)/cancel$"), "post_cancel"),
    ("POST", re.compile(r"^/job/(?P<job_id>\d+)/laptop$"), "post_laptop"),
    ("GET", re.compile(r"^/held$"), "page_held"),
    ("POST", re.compile(r"^/held/(?P<entry_id>\d+)/publish$"), "post_publish"),
    ("POST", re.compile(r"^/held/(?P<entry_id>\d+)/reject$"), "post_reject"),
    ("POST", re.compile(r"^/entry/(?P<entry_id>\d+)/spawn$"), "post_spawn"),
    ("POST", re.compile(r"^/control$"), "post_control"),
    ("GET", re.compile(r"^/preview/(?P<job_id>\d+)/(?P<n>\d+)$"), "preview_slash"),
    (
        "GET",
        re.compile(r"^/preview/(?P<job_id>\d+)/(?P<n>\d+)/(?P<rest>.*)$"),
        "serve_preview",
    ),
    ("GET", re.compile(r"^/jobs/(?P<rest>.*)$"), "serve_job_file"),
    ("POST", re.compile(r"^/_quit$"), "post_quit"),
]


class OpHandler(BaseHTTPRequestHandler):
    """One request, one thread, one line on stderr."""

    server_version = "sketchgen-web"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ----------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silenced: log_request below is the one line per request."""

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        status = getattr(code, "value", code)
        sys.stderr.write(f"{db.utc_now()} {self.command} {self.path} {status}\n")
        sys.stderr.flush()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("GET", body=False)

    def _dispatch(self, method: str, body: bool = True) -> None:
        path = urllib.parse.urlsplit(self.path).path
        matched_path = False
        for route_method, pattern, handler_name in ROUTES:
            if handler_name == "post_quit" and not self.app.once_for_test:
                # Not a route at all outside --once-for-test: 404, not 405.
                continue
            match = pattern.match(path)
            if not match:
                continue
            matched_path = True
            if route_method != method:
                continue
            try:
                getattr(self, handler_name)(**match.groupdict())
            except BrokenPipeError:  # pragma: no cover - client went away
                pass
            except Exception as exc:  # pragma: no cover - defensive
                sys.stderr.write(f"{db.utc_now()} 500 {path}: {exc}\n")
                self.send_error(500, "server error")
            return
        if matched_path:
            self.send_error(405, "method not allowed")
        else:
            self.send_error(404, "no such page")

    # -- replies -----------------------------------------------------------

    def _send(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def html(self, text: str, status: int = 200) -> None:
        self._send(text.encode("utf-8"), "text/html; charset=utf-8", status)

    def json_out(self, document: Any, status: int = 200) -> None:
        body = json.dumps(document, indent=2, sort_keys=False).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    def redirect(self, location: str, flash: str | None = None) -> None:
        if flash:
            joiner = "&" if "?" in location else "?"
            location = f"{location}{joiner}flash={urllib.parse.quote(flash)}"
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- request data ------------------------------------------------------

    def query(self) -> dict[str, list[str]]:
        return urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)

    def flash(self) -> str | None:
        values = self.query().get("flash")
        return values[0] if values else None

    def form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(min(length, 1 << 20)).decode("utf-8", "replace")
        return urllib.parse.parse_qs(raw, keep_blank_values=True)

    def back(self) -> str:
        """Where a POST returns to: the form's own hint, or the queue."""
        return "/queue"

    # -- pages -------------------------------------------------------------

    def page_console(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        doc = console_document(self.app)
        self.html(
            layout(
                title="Console",
                here="/",
                body=console_page(doc, marks.get("tokens")),
                control=control,
                back="/",
                flash=self.flash(),
                page_script=CONSOLE_SCRIPT,
                nav_marks=marks,
            )
        )

    def api_console(self) -> None:
        self.json_out(console_document(self.app))

    def api_control(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        text, css, tooltip = pill_for(control)
        self.json_out(
            {
                "state": control.state if control else None,
                "reason": control.reason if control else None,
                "updated_utc": control.updated_utc if control else None,
                "stop_now": worker.is_stop_now(control),
                "pill": text,
                "pill_class": css,
                "title": tooltip,
                # The header's marks ride along with the pill: one poll keeps
                # the whole header honest, and nothing else has to be fetched.
                "nav": marks,
            }
        )

    def page_queue(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            body = queue_page(conn, console_document(self.app), control)
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title="Queue",
                here="/queue",
                body=body,
                control=control,
                back="/queue",
                flash=self.flash(),
                nav_marks=marks,
            )
        )

    def page_new(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title="New job",
                here="/new",
                body=new_page(),
                control=control,
                back="/new",
                flash=self.flash(),
                nav_marks=marks,
            )
        )

    def post_new(self) -> None:
        form = self.form()
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            try:
                job_id, position = create_job(conn, form)
            except (ValueError, sqlite3.Error) as exc:
                self.html(
                    layout(
                        title="New job",
                        here="/new",
                        body=new_page(form, str(exc)),
                        control=control,
                        back="/new",
                        nav_marks=nav_summary(conn),
                    ),
                    status=400,
                )
                return
        finally:
            conn.close()
        self.redirect("/queue", f"Queued as #{job_id} · position {position}")

    def page_job(self, job_id: str) -> None:
        conn = self.app.connect()
        try:
            job = db.get_job(conn, int(job_id))
            if job is None:
                self.send_error(404, "no such job")
                return
            control = db.get_control(conn)
            body = job_page(self.app, conn, job)
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title=f"Job {job.id}",
                here="/queue",
                body=body,
                control=control,
                back=f"/job/{job.id}",
                flash=self.flash(),
                page_script=JOB_SCRIPT,
                nav_marks=marks,
            )
        )

    def api_job(self, job_id: str) -> None:
        conn = self.app.connect()
        try:
            job = db.get_job(conn, int(job_id))
            if job is None:
                self.send_error(404, "no such job")
                return
            document = job_document(self.app, conn, job)
        finally:
            conn.close()
        self.json_out(document)

    # -- the live transcript -----------------------------------------------

    def events_job(self, job_id: str) -> None:
        """``GET /events/job/<id>`` — the job log as it is written.

        404 for an id that is not a job, 503 when :data:`MAX_STREAMS` are
        already open. Everything after the headers is best-effort: a client
        that goes away raises on the next write and the stream ends there,
        quietly, because a closed browser tab is not an error.
        """
        conn = self.app.connect()
        try:
            job = db.get_job(conn, int(job_id))
        finally:
            conn.close()
        if job is None:
            self.send_error(404, "no such job")
            return
        if self.command == "HEAD":
            # A HEAD would otherwise open a stream nobody is reading.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        if not _take_stream():
            # ASCII only: this goes in the status line, which is latin-1.
            self.send_error(
                503, f"too many live transcripts (limit {MAX_STREAMS}); close a tab"
            )
            return
        try:
            self._stream_job(job.id)
        finally:
            _drop_stream()

    def _write_chunk(self, chunk: bytes) -> bool:
        """Write one frame. False means the reader has gone; stop streaming."""
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return False
        return True

    def _stream_job(self, job_id: int) -> None:
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        # No Content-Length: the body ends when the connection does.
        self.end_headers()

        tailer = LogTailer(self.app.jobs_root / str(job_id) / "job.log")
        quit_event = self.app.quit_event
        conn = self.app.connect()
        try:
            for line in tailer.initial():
                if not self._write_chunk(sse(line, "line")):
                    return
            snapshot = self._state_snapshot(conn, job_id)
            if snapshot is None:  # deleted between the 404 check and here
                self._write_chunk(sse(json.dumps({"id": job_id, "state": None}), "done"))
                return
            if not self._write_chunk(sse(json.dumps(snapshot), "state")):
                return
            last_state = time.monotonic()
            last_keepalive = last_state
            while snapshot is None or not snapshot.get("terminal"):
                if quit_event is not None and quit_event.is_set():
                    return
                time.sleep(STREAM_POLL_S)
                for line in tailer.poll():
                    if not self._write_chunk(sse(line, "line")):
                        return
                now = time.monotonic()
                fresh = self._state_snapshot(conn, job_id)
                if fresh is None:  # the job row went away under us
                    break
                changed = snapshot is None or any(
                    fresh.get(key) != snapshot.get(key)
                    for key in ("state", "attempt_n", "needs", "max_attempts")
                )
                if changed or now - last_state >= STATE_EVERY_S:
                    snapshot = fresh
                    last_state = now
                    if not self._write_chunk(sse(json.dumps(fresh), "state")):
                        return
                else:
                    snapshot = fresh
                if now - last_keepalive >= KEEPALIVE_EVERY_S:
                    last_keepalive = now
                    if not self._write_chunk(b": keepalive\n\n"):
                        return
            # Terminal: one last sweep of the log, then say so and close.
            for line in tailer.poll():
                if not self._write_chunk(sse(line, "line")):
                    return
            self._write_chunk(
                sse(json.dumps({"id": job_id, "state": (snapshot or {}).get("state")}), "done")
            )
        finally:
            conn.close()

    def _state_snapshot(self, conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
        try:
            job = db.get_job(conn, job_id)
        except sqlite3.Error:  # pragma: no cover - the database went away
            return None
        return None if job is None else job_state(conn, job)

    def post_cancel(self, job_id: str) -> None:
        self.form()
        conn = self.app.connect()
        try:
            try:
                db.transition(
                    conn, int(job_id), "failed", last_error="cancelled by operator"
                )
                message = f"Job {job_id} cancelled"
            except db.IllegalTransition:
                message = f"Job {job_id} has already finished — nothing changed"
            except db.UnknownJob:
                message = f"there is no job {job_id}"
        finally:
            conn.close()
        self.redirect(f"/job/{job_id}", message)

    def post_laptop(self, job_id: str) -> None:
        self.form()
        conn = self.app.connect()
        try:
            try:
                db.transition(conn, int(job_id), "needs-laptop", needs="review")
                message = f"Job {job_id} is waiting for the laptop (needs: review)"
            except db.IllegalTransition:
                message = f"Job {job_id} cannot go to needs-laptop from here"
            except db.UnknownJob:
                message = f"there is no job {job_id}"
        finally:
            conn.close()
        self.redirect(f"/job/{job_id}", message)

    def page_held(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            body = held_page(self.app, conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title="Held for publication",
                here="/held",
                body=body,
                control=control,
                back="/held",
                flash=self.flash(),
                nav_marks=marks,
            )
        )

    def post_publish(self, entry_id: str) -> None:
        self.form()
        conn = self.app.connect()
        try:
            message = publish_entry(self.app, conn, int(entry_id))
        finally:
            conn.close()
        self.redirect("/held", message)

    def post_reject(self, entry_id: str) -> None:
        form = self.form()
        reason = (form.get("reason") or [""])[0]
        conn = self.app.connect()
        try:
            message = reject_entry(conn, int(entry_id), reason)
        finally:
            conn.close()
        self.redirect("/held", message)

    def post_spawn(self, entry_id: str) -> None:
        form = self.form()
        back = (form.get("back") or ["/held"])[0]
        if not back.startswith("/") or back.startswith("//"):
            back = "/held"
        conn = self.app.connect()
        try:
            message = spawn_child(conn, int(entry_id), form)
        finally:
            conn.close()
        self.redirect(back, message)

    def post_control(self) -> None:
        form = self.form()
        action = (form.get("action") or [""])[0]
        back = (form.get("back") or ["/"])[0]
        if not back.startswith("/") or back.startswith("//"):
            back = "/"
        conn = self.app.connect()
        try:
            if action == "pause":
                db.set_control(conn, "pausing", "pause")
                message = "Pausing — the worker finishes the attempt in flight"
            elif action == "stop":
                # The worker's own representation of stop-now; see worker.py.
                db.set_control(conn, "pausing", "stop")
                message = "Stopping now — the attempt is aborted and the job re-queued"
            elif action == "resume":
                db.set_control(conn, "running", None)
                message = "Running — the worker claims jobs again"
            else:
                message = f"unknown control action {action!r} — nothing changed"
        finally:
            conn.close()
        self.redirect(back, message)

    def serve_job_file(self, rest: str) -> None:
        root = self.app.jobs_root
        try:
            root = root.resolve()
        except OSError:
            self.send_error(404, "no such file")
            return
        raw = urllib.parse.unquote(rest)
        if "\x00" in raw:
            self.send_error(404, "no such file")
            return
        try:
            target = (root / raw).resolve()
        except (OSError, ValueError):
            self.send_error(404, "no such file")
            return
        if target != root and not target.is_relative_to(root):
            self.send_error(404, "no such file")
            return
        content_type = SERVABLE.get(target.suffix.lower())
        if content_type is None or not target.is_file():
            self.send_error(404, "no such file")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(404, "no such file")
            return
        self._send(body, content_type)

    def preview_slash(self, job_id: str, n: str) -> None:
        """``/preview/4/1`` → ``/preview/4/1/``, or the sketch's own
        ``<script src="sketch.js">`` would resolve one directory too high."""
        self.send_response(301)
        self.send_header("Location", f"/preview/{int(job_id)}/{int(n)}/")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def serve_preview(self, job_id: str, n: str, rest: str) -> None:
        """One attempt directory, served as the small static site it is.

        The fence is the attempt directory itself, resolved before the prefix
        check exactly as ``/jobs/…`` does it: a sketch can only reach files its
        own attempt wrote. p5 comes from cdnjs, fetched by the browser.
        """
        root = attempt_dir(self.app, int(job_id), int(n))
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            self.send_error(404, "no such attempt")
            return
        if not root.is_dir():
            self.send_error(404, "no such attempt")
            return
        raw = urllib.parse.unquote(rest)
        if "\x00" in raw:
            self.send_error(404, "no such file")
            return
        if raw in ("", "/"):
            raw = "index.html"
        try:
            target = (root / raw).resolve()
        except (OSError, ValueError):
            self.send_error(404, "no such file")
            return
        if not target.is_relative_to(root):
            self.send_error(404, "no such file")
            return
        content_type = PREVIEW_SERVABLE.get(target.suffix.lower())
        if content_type is None or not target.is_file():
            self.send_error(404, "no such file")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(404, "no such file")
            return
        self._send(body, content_type, headers=PREVIEW_HEADERS)

    def post_quit(self) -> None:
        self.form()
        self._send(b"stopping\n", "text/plain; charset=utf-8")
        if self.app.quit_event is not None:
            self.app.quit_event.set()


def make_server(
    *,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    db_path: str = db.DEFAULT_DB_PATH,
    jobs_dir: str = DEFAULT_JOBS_DIR,
    once_for_test: bool = False,
) -> ThreadingHTTPServer:
    """Build the server. Refuses any bind but the loopback one (exit 3)."""
    check_bind(bind)
    server = ThreadingHTTPServer((bind, port), OpHandler)
    server.daemon_threads = True
    server.app = App(  # type: ignore[attr-defined]
        db_path=str(db_path),
        jobs_dir=str(jobs_dir),
        once_for_test=once_for_test,
        quit_event=threading.Event(),
    )
    return server


def serve(
    *,
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    db_path: str = db.DEFAULT_DB_PATH,
    jobs_dir: str = DEFAULT_JOBS_DIR,
    once_for_test: bool = False,
    timeout_s: float = ONCE_TIMEOUT_S,
) -> int:
    """Run the UI. ``once_for_test`` serves until POST /_quit or ``timeout_s``."""
    server = make_server(
        bind=bind,
        port=port,
        db_path=db_path,
        jobs_dir=jobs_dir,
        once_for_test=once_for_test,
    )
    app: App = server.app  # type: ignore[attr-defined]
    where = f"http://{bind}:{server.server_port}/"
    sys.stderr.write(
        f"{db.utc_now()} sketchgen web listening on {where} "
        f"db={app.db_path} jobs={app.jobs_dir}"
        + (f" once-for-test={timeout_s:.0f}s\n" if once_for_test else "\n")
    )
    sys.stderr.flush()
    if not once_for_test:
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            sys.stderr.write(f"{db.utc_now()} sketchgen web stopped\n")
        finally:
            server.server_close()
        return EXIT_OK

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2})
    thread.daemon = True
    thread.start()
    reason = "POST /_quit" if app.quit_event.wait(timeout_s) else "timeout"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()
    sys.stderr.write(f"{db.utc_now()} sketchgen web exiting ({reason})\n")
    sys.stderr.flush()
    return EXIT_OK
