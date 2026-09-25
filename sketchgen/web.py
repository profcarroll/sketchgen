"""web.py — the operator's five screens, on 127.0.0.1 and nowhere else.

Packet 4.2 of the sketchgen build. One ``ThreadingHTTPServer``, server-rendered
HTML from ``sketchgen/templates/op_*.html``, no framework, no JS build, no CDN:
every script here is a few lines at the bottom of one page, hand-written, and
every one of them only makes live something the server has already rendered
once — the worker pill, the console's two-second refetch, the transcript
stream, the click-to-run preview, and the Held page's batch tray. The
whole thing is reached over the SSH tunnel that already carries ``preview`` on
8080, so this one takes 8081.

The five screens are the wireframe's:

  ``/``            Console — node vitals, the slot, the odometer, the funnel
  ``/queue``       the tiles and the jobs table
  ``/new``         the form that writes a queued row
  ``/job/<id>``    transcript, per-attempt gate report, artefacts, provenance
  ``/held``        what is waiting for a person to publish or reject, marked
                   up as one batch and run by one press of Process

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

**The Held page is one press, not thirty.** Each card's four verbs used to be
four submit buttons, so every decision was a round trip that waited on a render,
two commits and two pushes — and a second press during any of them was a second
publisher queuing on the gallery checkout lock behind the first. The verbs are
**toggles that mark** now: pressed, a verb fills and nothing else happens. The
whole page is one ``<form>`` whose submit button is **Process** in the sticky
header's second row, and a press runs every mark as one batch — critiques while
their parents are still held, then archives, then every rejection and
publication in **one push and one index re-render** (§1.6, §1.7). One batch at a
time per web process, and while one runs every control on the page is disabled
and the single-entry routes refuse: "busy" is :attr:`App.batch`, on the server,
so it outlives the page that pressed. The single-entry routes stay in
:data:`ROUTES` for scripts and ``/entry/<id>`` habits, but no button here points
at them. :data:`HELD_SCRIPT` makes the tray live; without it the POST still
starts the batch and the tray, rendered server-side, refreshes itself.

**Spawning a child belongs here and not on the public site** (packet 5.3).
``POST /entry/<id>/spawn`` hands one critique to :func:`sketchgen.lineage.spawn`,
which composes the parent's prompt with it and queues the child. The form sits on
the job page, and on the Held page a card's Critique mark queues one through the
batch; ``critique-by`` defaults to the operator's username, which is
``$SKETCHGEN_OPERATOR`` when it is set, whatever ``gh`` on this node is signed
in as when it is not (:func:`github_login`), and the entry's own submitter
otherwise. The New job page signs a job the same way: the login is a pill,
not a box, and the box only appears for a job queued on somebody's behalf.
The gallery is generated, static, and has no way to write to this database —
asking it to would mean a public form on a queue, and the publication gate
exists precisely so a person stands between the queue and the site.

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
import shutil
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, NamedTuple

from sketchgen import build
from sketchgen import db
from sketchgen import executor
from sketchgen import lineage
from sketchgen import models
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
#: This is model-written code running in the operator's browser. Seeing it run
#: is the whole point of the screen — so it runs, scripts and all. The header
#: keeps it *here*: ``frame-ancestors 'self'`` lets the operator UI frame the
#: preview and nothing else, which is what X-Frame-Options was reaching for and
#: cannot say as precisely.
#:
#: Until 2026-09-15 there was deliberately no ``sandbox`` either, on the grounds
#: that an opaque origin is refused the microphone and ``responds(audio)`` is in
#: the gate's own vocabulary. Entry 165 and entry 269 changed the price of that
#: reasoning: a sketch the gate passed took the operator's laptop down. The
#: frames this page builds now carry ``sandbox="allow-scripts"``, which is
#: exactly what the gallery's own entry and compare pages have always used, and
#: an audio sketch is previewed in its own tab — the "open in a tab" link beside
#: every poster — where the origin is real and the microphone works.
PREVIEW_HEADERS = {"Content-Security-Policy": "frame-ancestors 'self'"}

#: What a frame in this UI is allowed to do: run its own scripts, and nothing
#: else. No same-origin, no forms, no top-level navigation. The same string the
#: gallery uses (sketchgen/assets/gallery.js, startSketch).
PREVIEW_SANDBOX = "allow-scripts"

#: A gate run longer than this many seconds is worth a mark on the card even
#: when the report says every check passed — every one of the reports over it on
#: the node is a sketch that costs hundreds of milliseconds a frame. The gate's
#: own median run is 2.4 s.
SLOW_GATE_S = 30.0

#: The preview frame, in CSS pixels. The sketches size themselves to the
#: window, so this is a window rather than a crop.
PREVIEW_W, PREVIEW_H = 640, 400

#: GitHub usernames and nothing else ever goes in ``submitted_by`` (course policy).
USERNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")

TERMINAL_STATES = frozenset({"published", "rejected", "failed"})

# What a state is CALLED on screen. The database keeps its names; the UI says
# who rejected the work. Anything not listed is shown as its state name.
#
#: Both job states and entry states are in
#: here — the two machines share three names and the label is the same either
#: way — so the queue's pill, the job page and the entry page all say the same
#: words. ``gallery.STATE_CHIPS`` carries the two rejection labels onto the
#: public site, deliberately the same strings.
STATE_LABELS = {
    "failed": "rejected · gate",
    "failed-kept": "rejected · gate",
    "rejected": "rejected · operator",
}


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

    #: The one batch this web process will run at a time, and the lock that
    #: every reader and writer of it takes (§1.5, §1.9). It lives here rather
    #: than in a table because a batch is a report on work in flight and not a
    #: record: every step it takes is atomic on its own, so a process that dies
    #: mid-batch loses the report and nothing else. ``batch_lock`` is the
    #: state that says "busy", and it is on the server rather than in the page
    #: that was open when the press happened, which is what stops the second
    #: press queuing a second publisher behind the first.
    batch: Batch | None = None
    batch_lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

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
    if kind == "yn":
        return "yes" if value else "no"
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


def _free_tier_note(doc: Any) -> str:
    """The storage meter's tail: what dropping to the Always Free allowance
    would cost in models. A list of names can't go through :func:`field`, so
    this is rendered server-side and refreshed with the page, not per poll."""
    cost = _dig(doc, "node.disk_gb.cost") or {}
    over = cost.get("over_free_tier_gb")
    budget = cost.get("free_tier_model_gb")
    if over is None or budget is None:
        return "free-tier impact unknown"
    if not over:
        return f"models fit the {budget:.0f} GB free-tier budget"
    drops = cost.get("would_drop") or []
    names = ", ".join(
        f"{esc(str(d.get('name')))} ({float(d.get('size_gb') or 0):.1f} GB)"
        for d in drops
    )
    tail = f" — would drop {names}" if names else ""
    return (
        f"drop to free tier caps models at {budget:.0f} GB, "
        f"{over:.1f} GB over{tail}"
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
    ("/submissions", "Submissions"),
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


# ---------------------------------------------------------------------------
# The build chip and /build (docs/plans/fleet.md §1.6)
# ---------------------------------------------------------------------------

#: How often this process asks GitHub whether main has moved. Every node is on
#: the tunnel's schedule rather than anyone's attention: sld-cloud reported
#: itself up to date four PRs behind main on 2026-09-24 because nothing on it
#: had fetched since its last deploy.
BUILD_FETCH_S = float(os.environ.get("SKETCHGEN_BUILD_FETCH_S") or 900)
#: How long one reading of the checkout is reused. A reading is a dozen git
#: calls and a systemctl; every page draws the chip, and the operator clicking
#: through five pages should not pay for it five times.
BUILD_TTL_S = 30.0


class BuildWatch:
    """The web process's view of its own build, and the fetch that keeps it
    current. One per server; none under ``--once-for-test``, so the suite never
    runs git against the checkout it is testing or touches the network."""

    def __init__(self, root: Path, db_path: str) -> None:
        self.root = root
        self.db_path = db_path
        self.lock = threading.Lock()
        self.fetch_error: str | None = None
        self._doc: dict[str, Any] | None = None
        self._at = 0.0

    def reading(self, fresh: bool = False) -> dict[str, Any] | None:
        with self.lock:
            stale = time.monotonic() - self._at > BUILD_TTL_S
            if fresh or self._doc is None or stale:
                try:
                    doc = build.reading(self.root, self.db_path)
                except Exception as exc:  # noqa: BLE001 - never take the page down
                    sys.stderr.write(f"{db.utc_now()} build reading failed: {exc}\n")
                    return self._doc
                doc["upstream"]["fetch_error"] = self.fetch_error
                self._doc, self._at = doc, time.monotonic()
            return self._doc

    def check(self) -> str | None:
        """Fetch main now; None on success, else why not."""
        self.fetch_error = build.fetch(self.root)
        self.reading(fresh=True)
        return self.fetch_error

    def start(self, every_s: float = BUILD_FETCH_S) -> None:
        def loop() -> None:
            while True:
                try:
                    self.check()
                except Exception as exc:  # noqa: BLE001 - a daemon thread must not die
                    sys.stderr.write(f"{db.utc_now()} build fetch failed: {exc}\n")
                time.sleep(every_s)

        threading.Thread(target=loop, name="build-fetch", daemon=True).start()


#: Set by :func:`serve`; None under test and wherever nothing started it, and
#: then the header simply has no chip.
BUILD_WATCH: BuildWatch | None = None


def build_chip(doc: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """The header's build chip: (text, css class, tooltip), or None.

    One chip, the most important thing first. Red is a node that cannot be
    trusted to be what it says (hand edits, a foreign branch, a process on
    older code than its checkout); amber is an update waiting; quiet is on
    target, which includes a pin — a pinned node is behind main on purpose.
    """
    if not doc or not doc["checkout"].get("sha"):
        return None
    here, target, up, rel = doc["checkout"], doc["target"], doc["upstream"], doc["main"]
    sha = build.short(here["build"])
    looked = up.get("fetched_utc") or "never"
    if up.get("fetch_error"):
        looked += f"; the last check failed: {up['fetch_error']}"
    problems = doc.get("problems") or []

    def first(fragment: str) -> str | None:
        return next((p for p in problems if fragment in p), None)

    if here.get("dirty"):
        return "dirty", "bad", f"{sha}: tracked files changed on this node; update.sh refuses"
    if here.get("branch") and here["branch"] != "main":
        return (f"on {here['branch']}", "bad",
                f"{sha} is on branch {here['branch']}; update.sh refuses anything but main")
    if target["kind"] == "main" and rel.get("on_main") is False:
        return "not on main", "bad", f"{sha} is not a commit on main"
    if target["kind"] == "pin" and here["sha"] != target["sha"]:
        return ("off pin", "bad",
                f"pinned to {build.short(target['sha'])} but the checkout is {sha}")
    restart = first("restart pending")
    if restart:
        return "restart pending", "bad", restart
    if first("an update is running"):
        return "updating", "warn", "sketchgen-update is running on this node"
    if target["kind"] == "main" and rel.get("behind"):
        n = rel["behind"]
        return (f"{n} behind", "warn",
                f"update available: main is {n} PR{'s' if n != 1 else ''} ahead of "
                f"{sha} (checked {looked})")
    if target["kind"] == "pin":
        why = f' — {target["reason"]}' if target.get("reason") else ""
        ahead = f"; main is {rel['behind']} PRs ahead" if rel.get("behind") else ""
        return f"pinned {sha}", "quiet", f"pinned{why}{ahead} (checked {looked})"
    unrecorded = first("unrecorded")
    if unrecorded:
        return f"{sha} ?", "warn", unrecorded
    return sha, "quiet", f"on main (checked {looked})"


def build_chip_html() -> str:
    chip = build_chip(BUILD_WATCH.reading()) if BUILD_WATCH is not None else None
    if chip is None:
        return ""
    text, css, tooltip = chip
    return (f'<a class="pill build {esc(css)}" href="/build" title="{esc(tooltip)}">'
            f'{esc(text)}</a>')


def build_page(doc: dict[str, Any] | None, root: Path | None) -> str:
    """/build: the reading, the PRs between here and main, and Check now."""
    if doc is None:
        return ('<section class="card"><h2>Build</h2><p class="dim">This process '
                'is not watching its build (it was started for a test).</p></section>')
    here, target, up = doc["checkout"], doc["target"], doc["upstream"]
    parts = [
        '<section class="card"><h2>Build</h2>',
        f'<pre class="build">{esc(build.render(doc))}</pre>',
        '<form method="post" action="/build/check">'
        '<button type="submit" title="git fetch origin main — nothing on this node '
        'moves">Check now</button></form>',
        '</section>',
    ]
    main = up.get("sha")
    if root is not None and here.get("sha") and main and doc["main"].get("behind"):
        pending = build.commits(root, here["sha"], main)
        label = ("On main since this node's pin" if target["kind"] == "pin"
                 else "Waiting to be deployed here")
        items = "".join(
            f'<li><code>{esc(c["sha"][:7])}</code> '
            + (f'#{esc(c["pr"])} ' if c["pr"] else "")
            + f'{esc(c["title"])}</li>'
            for c in pending
        )
        parts.append(f'<section class="card"><h2>{esc(label)}</h2><ul>{items}</ul>'
                     '<p class="dim">Deploy from the laptop: <code>bin/fleet update '
                     'NODE</code>, which runs <code>update.sh</code> here as the '
                     '<code>sketchgen-update</code> unit.</p></section>')
    return "".join(parts)


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
    subheader: str = "",
) -> str:
    """Every page: the header, the flash, the body, the scripts.

    ``subheader`` is a second header row, sticky with the first one because they
    are one box in the template — /held passes the batch tray and every other
    page passes nothing and looks exactly as it did.
    """
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
        build_chip=build_chip_html(),
        back=esc(back),
        pause_dis=" disabled" if state != "running" else "",
        stop_dis=" disabled" if (state == "paused" or stop_now) else "",
        resume_dis=" disabled" if state == "running" else "",
        flash=flash_html,
        body=body,
        page_script=page_script,
        subheader=subheader,
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
    if (kind === "yn") return value ? "yes" : "no";
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


# ---------------------------------------------------------------------------
# The process status card (packet 5)
#
# One renderer for both pages, so the Console's card and the Queue's one-line
# copy cannot drift apart, and one reader behind both — console.activity() —
# so the page load and the two-second poll cannot disagree about the sentence
# on screen. Every value carries data-act, the way the console's numbers carry
# data-k, and the poll at the bottom of op_layout.html repaints those and
# nothing else.
# ---------------------------------------------------------------------------

#: The card's state as the pill says it: (text, css class). The poll does not
#: mirror this table — /api/control.json sends the pill it should wear, already
#: decided here — so there is one mapping from state to colour and it is in
#: Python. Idle work is deliberately not green: an idle worker is not a working
#: one, and a green pill over "Nothing to do" tells the operator the opposite
#: of what is true.
ACTIVITY_PILLS: dict[str, tuple[str, str]] = {
    "running": ("running", "ok"),
    "idle": ("idle", "quiet"),
    "paused": ("paused", "bad"),
    "gone": ("not running", "bad"),
    "stalled": ("stalled", "warn"),
    "fenced": ("fenced", "warn"),
    "unknown": ("unknown", "quiet"),
}

#: Steps that are not work: the nap, in both its shapes. The worker opens an
#: ``idle`` row when it goes to sleep so that an open row exists for as long as
#: the process does, and a ``fenced`` row when the sleep follows a pass the
#: fence refused (2026-09-23). The bar reads this, not the state, because
#: console.activity() puts judging and critiquing under the ``idle`` state too,
#: and those two are work — long, model-shaped work, with medians of their own.
ACTIVITY_RESTING_STEPS = frozenset({"idle", "fenced"})

#: Card states where nothing is running whatever the newest row says: a paused
#: worker, one whose pid is gone (its last step stays open on purpose), and a
#: database with no rows in it.
ACTIVITY_RESTING_STATES = frozenset({"paused", "gone", "unknown"})

#: What the card shows when the document has no activity block at all: an old
#: cached document, or a console module that predates packet 5. The same shape
#: console.activity() answers for an empty table, so nothing downstream has to
#: ask which of the two it is holding.
ACTIVITY_NONE: dict[str, Any] = {
    "step": None,
    "headline": "Nothing recorded yet",
    "detail": "the worker writes this card as it works",
    "elapsed_s": None,
    "median_s": None,
    "state": "unknown",
    "recent": [],
}


def activity_of(doc: dict[str, Any]) -> dict[str, Any]:
    """The document's activity block, or the empty card."""
    block = doc.get("activity") if isinstance(doc, dict) else None
    return block if isinstance(block, dict) else ACTIVITY_NONE


def activity_pill(act: dict[str, Any]) -> tuple[str, str]:
    return ACTIVITY_PILLS.get(str(act.get("state")), ACTIVITY_PILLS["unknown"])


def activity_elapsed(act: dict[str, Any]) -> str:
    """The time under the bar: how long this step has run, against the median.

    "of about" because the median is a measurement of the last fifty of this
    step on this node, not a promise. With fewer than five of them there is no
    median, and then this is just the elapsed time — which is still the useful
    half.
    """
    elapsed = act.get("elapsed_s")
    if elapsed is None:
        return "—"
    median = act.get("median_s")
    if median:
        return f"{human_seconds(elapsed)} of about {human_seconds(median)}"
    return human_seconds(elapsed)


def activity_working(act: dict[str, Any]) -> bool:
    """Is the worker doing something, as opposed to merely being alive?

    "There is an open row" is not the same question. The worker opens an
    ``idle`` row when it naps, deliberately, because an open row for as long as
    the process lives is what makes the "worker not running" card readable —
    which means the nap is a row like any other and the bar has to know it is
    not work. Neither is a paused worker, a dead one, or a database with
    nothing in it.
    """
    if str(act.get("state") or "unknown") in ACTIVITY_RESTING_STATES:
        return False
    return str(act.get("step") or "") not in ACTIVITY_RESTING_STEPS


def activity_bar_pct(act: dict[str, Any]) -> float | None:
    """Elapsed as a percentage of the median, capped, or None for no fill.

    Capped rather than overflowing: a step that is past its median keeps a full
    track and turns amber, so a stuck step looks stuck instead of looking like
    a progress bar somebody drew too long.

    None also when nothing is running: a nap that has lasted longer than the
    median nap is not progress towards anything.
    """
    if not activity_working(act):
        return None
    elapsed, median = act.get("elapsed_s"), act.get("median_s")
    if elapsed is None or not median:
        return None
    return min(100.0, max(0.0, float(elapsed) / float(median) * 100.0))


def activity_bar_class(act: dict[str, Any]) -> str:
    """The track's own class, which is how the bar says which of three it is.

    The track is always drawn. Until 2026-09-16 it was hidden when there was no
    median, which drew an empty grey track anyway — ``.bar { display: flex }``
    is an author rule and beats the browser's own ``[hidden]`` — and said
    nothing when it was working, which is most of the time on a step the node
    has run fewer than five of. So:

    * **measured** — a median exists: no class, the fill inside says it.
    * **working** — running and unmeasurable: ``working``, a moving ribbon.
      Some steps are never measurable (``claiming`` is instant, ``sweeping``
      may happen twice a week), so this is the answer rather than a stopgap.
    * **idle** — nothing running: no class and no fill, an empty track.

    ``working late`` is the ribbon in amber, for a step past
    :data:`console.ACTIVITY_STALLED_S`, so "this is taking too long" stays
    legible in the unmeasured case too.
    """
    if not activity_working(act) or activity_bar_pct(act) is not None:
        return ""
    return "working late" if str(act.get("state")) == "stalled" else "working"


def activity_foot(doc: dict[str, Any]) -> str:
    """The three Worker tiles this card replaced, on one line.

    Control, up since and slot were the Console's Worker tiles until packet 5;
    nothing is lost by taking the tiles out, because the card is the thing an
    operator reads first and these three belong under it rather than beside it.
    They move on the scale of a session rather than of a step, so the poll
    leaves this line alone — /api/control.json deliberately does not call the
    collector, and up-since and slot are the collector's to know.
    """
    control = _dig(doc, "worker.control", "?")
    # One job is not "1 jobs". The worker says it this way too (sweep_stuck).
    jobs = _dig(doc, "funnel.generated.session", 0)
    started = _parse_utc(_dig(doc, "worker.started_utc"))
    up = (
        human_seconds((datetime.now(timezone.utc) - started).total_seconds())
        if started is not None
        else "—"
    )
    parts = [
        str(control),
        f"up {up}",
        f"slot {_dig(doc, 'model.slot.state', '—')}",
        f"{jobs} job{'' if jobs == 1 else 's'} this session",
    ]
    return " · ".join(parts)


def activity_trail(act: dict[str, Any]) -> str:
    """The three steps just before this one, newest first."""
    rows = []
    for row in (act.get("recent") or [])[:3]:
        if not isinstance(row, dict):  # pragma: no cover - a malformed document
            continue
        rows.append(
            f'<li><span class="h">{esc(row.get("headline"))}</span>'
            f'<span class="s">{esc(human_seconds(row.get("seconds")))}</span></li>'
        )
    if not rows:
        rows.append('<li><span class="h">nothing before this</span></li>')
    return "".join(rows)


def activity_card(doc: dict[str, Any], *, compact: bool = False) -> str:
    """The card, full on the Console and one line above the Queue's table.

    Everything here goes through :func:`esc`: a detail line can carry a
    sentence a model wrote about a sketch a student asked for, and neither of
    those is trusted markup.
    """
    act = activity_of(doc)
    text, css = activity_pill(act)
    pill = f'<span class="pill {esc(css)}" data-act="pill">{esc(text)}</span>'
    headline = esc(act.get("headline") or "—")
    detail = esc(act.get("detail") or "")

    if compact:
        return (
            '<section class="panel status compact">'
            f"{pill}"
            f'<span class="now" data-act="headline">{headline}</span>'
            f'<span class="who" data-act="detail">{detail}</span>'
            '<a class="more" href="/">Console ↗</a>'
            "</section>"
        )

    pct = activity_bar_pct(act)
    over = pct is not None and float(act.get("elapsed_s") or 0) > float(
        act.get("median_s") or 0
    )
    ribbon = activity_bar_class(act)
    track = (
        f'<div class="bar{" " + ribbon if ribbon else ""}" data-act="track">'
        f'<span data-act="bar" class="{"over" if over else ""}" '
        f'style="width:{_width(pct if pct is not None else 0):.1f}%"></span></div>'
    )
    return (
        '<section class="panel status">'
        f'<div class="head"><h2>Worker</h2>{pill}</div>'
        f'<p class="now" data-act="headline">{headline}</p>'
        f'<p class="who" data-act="detail">{detail}</p>'
        f'<div class="prog">{track}'
        f'<span class="t" data-act="elapsed">{esc(activity_elapsed(act))}</span></div>'
        f'<div class="trail"><ol data-act="recent">{activity_trail(act)}</ol></div>'
        f'<p class="foot" data-act="foot">{esc(activity_foot(doc))}</p>'
        "</section>"
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
    ("archived", "archived"),
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


#: How old a recorded figure may be before the card says so.
BILLING_STALE_DAYS = 7

#: The Always Free ARM allowance: four OCPUs and 24 GB, held concurrently, at
#: any duty cycle. It is the line the card measures the node against, because
#: below it the bill is zero by policy and above it the bill is zero only
#: until somebody at Oracle notices.
ALWAYS_FREE_OCPUS = 4.0
ALWAYS_FREE_GB = 24.0

#: How many days of meter readings the card draws.
BILLING_BAR_DAYS = 30


def _billing_since(days: int = BILLING_BAR_DAYS) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def billing_values(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything the card needs, read while the request still holds a connection.

    The four ``billing_*`` keys are the headline figure as somebody last
    recorded it. The rest is migration 013's daily rows, which is what makes
    this a trend rather than a snapshot — and the node's own account of itself,
    which is what makes it checkable.
    """
    values: dict[str, Any] = {
        "amount": db.get_meta(conn, "billing_amount") or "",
        "currency": db.get_meta(conn, "billing_currency") or "USD",
        "through": db.get_meta(conn, "billing_through") or "",
        "checked": db.get_meta(conn, "billing_checked_utc") or "",
        "tenancy": db.get_meta(conn, "billing_tenancy") or "",
        "node_tenancy": db.get_meta(conn, "node_tenancy") or "",
        "node_shape": db.get_meta(conn, "node_shape") or "",
        "node_ocpus": db.get_meta(conn, "node_ocpus") or "",
        "node_memory_gb": db.get_meta(conn, "node_memory_gb") or "",
        "daily": [],
        "services": [],
        "tenancies": [],
    }
    tenancy = values["tenancy"] or values["node_tenancy"]
    try:
        values["tenancies"] = db.billing_tenancies(conn)
        if tenancy:
            since = _billing_since()
            values["daily"] = db.billing_daily(conn, tenancy, since)
            values["services"] = db.billing_services(conn, tenancy, since)
    except sqlite3.OperationalError:
        # A database still on migration 012 has no billing_usage. The headline
        # figure is still readable, so the card degrades to what it always was.
        pass
    return values


def _days_since(stamp: str) -> float | None:
    when = _parse_utc(stamp)
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


def billing_bars(daily: list[dict[str, Any]]) -> str:
    """One bar per day, scaled to the tallest.

    The height is always the metered OCPU-hours, never the amount. Money is
    the wrong axis for this: the day the node was resized from 4 OCPU to 16 is
    the day worth seeing, and it was charged nothing, so a chart drawn in
    dollars flattens the sixteen days that explain the bill into hairlines
    beside the one day that has it. Amber marks a day that was charged, which
    puts the money back without spending the axis on it.
    """
    if not daily:
        return ""
    peak = max(float(row.get("ocpu_hours") or 0.0) for row in daily)
    key = "ocpu_hours" if peak > 0 else "amount"
    if key == "amount":
        peak = max(float(row.get("amount") or 0.0) for row in daily)
    peak = peak or 1.0
    bars = []
    for index, row in enumerate(daily):
        value = float(row.get(key) or 0.0)
        amount = float(row.get("amount") or 0.0)
        classes = []
        if amount > 0:
            classes.append("charged")
        if index == len(daily) - 1:
            classes.append("partial")
        title = (f"{row.get('day')}: "
                 f"{float(row.get('ocpu_hours') or 0.0) / 24.0:.2f} OCPU held, "
                 f"{amount:.2f} charged")
        bars.append(
            f'<i class="{" ".join(classes)}" '
            f'style="height:{max(1.0, value / peak * 100.0):.1f}%" '
            f'title="{esc(title)}"></i>'
        )
    label = ("OCPU held a day · amber is charged" if key == "ocpu_hours"
             else "charged a day")
    return (
        f'<div class="billbars">{"".join(bars)}</div>'
        f'<div class="billrow"><span>{esc(daily[0]["day"])}</span>'
        f'<span>{esc(label)}</span>'
        f'<span>{esc(daily[-1]["day"])}</span></div>'
    )


def _last_full_day(daily: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent complete day, which is the only one worth averaging.

    Today is partial and always reads low. And a window average is no better
    once the shape has changed inside it: four days at 16 OCPU after twelve at
    4 averages to something the node has never been.
    """
    full = daily[:-1] or daily
    return full[-1] if full else None


def billing_held(daily: list[dict[str, Any]]) -> str:
    """What the meter says the node is, against what is free.

    The last full day, not the window: this is a status line, and the status
    is what Oracle metered yesterday, not what it averaged over a month in
    which the machine was resized.
    """
    row = _last_full_day(daily)
    if row is None:
        return ""
    ocpu = float(row.get("ocpu_hours") or 0.0) / 24.0
    gigabytes = float(row.get("gb_hours") or 0.0) / 24.0
    if ocpu <= 0:
        return ""
    over = ocpu > ALWAYS_FREE_OCPUS + 0.05 or gigabytes > ALWAYS_FREE_GB + 1.0
    note = (
        '<span class="pill rejected">over the free allowance</span>'
        if over else
        '<span class="pill">inside the free allowance</span>'
    )
    return (
        f'<p class="dim" style="font-size:12px;margin:8px 0 0">On '
        f'{esc(str(row.get("day")))}, the last full day, Oracle metered '
        f'<b>{ocpu:.2f} OCPU</b> and <b>{gigabytes:.0f} GB</b> held. '
        f'Always Free is {ALWAYS_FREE_OCPUS:.0f} OCPU / '
        f'{ALWAYS_FREE_GB:.0f} GB. {note}</p>'
    )


def billing_shape_warning(values: dict[str, Any]) -> str:
    """Said out loud when the meter and the machine disagree about its size.

    A metered figure well under the provisioned shape means the tenancy is
    being charged for less than it is running. That is good news exactly until
    it is corrected, and it is not something to find out from an invoice. The
    comparison is the last full day, because a window average spanning a
    resize disagrees with the machine by arithmetic rather than by fault.
    """
    row = _last_full_day(values.get("daily") or [])
    try:
        provisioned = float(values.get("node_ocpus") or 0)
    except (TypeError, ValueError):
        return ""
    if row is None or provisioned <= 0:
        return ""
    metered = float(row.get("ocpu_hours") or 0.0) / 24.0
    if metered <= 0 or metered >= provisioned - 0.1:
        return ""
    return (
        '<p class="billwarn">'
        f'On {esc(str(row.get("day")))} the meter says {metered:.2f} OCPU; '
        f'this node is {provisioned:.0f} OCPU '
        f'({esc(values.get("node_shape", "") or "shape unknown")}). '
        'Oracle is billing for less than is running — treat the total above as '
        'a floor, not the answer.</p>'
    )


def billing_tenancy_warning(values: dict[str, Any]) -> str:
    """Said out loud when the figure came from an account that is not this one.

    On 2026-09-18 it had been for weeks, and nothing said so. The node knows
    its own tenancy without any credentials; the figure carries the tenancy it
    was read from; a card that shows one while implying the other is worse
    than a card showing nothing.
    """
    node = str(values.get("node_tenancy") or "")
    read = str(values.get("tenancy") or "")
    if not node:
        return (
            '<p class="billwarn soft">'
            'This node has not said which tenancy it is in, so the figure '
            'cannot be checked against it. Run <code>sketchgen billing '
            '--identify</code> on the node.</p>'
        )
    if read and read != node:
        return (
            '<p class="billwarn">'
            'This figure was read from a different tenancy than the one this '
            'node runs in. It is some other account\'s bill. Point '
            '<code>~/.oci/config</code> at ' + esc(node[-20:]) + ' and read it '
            'again.</p>'
        )
    return ""


def billing_services_table(values: dict[str, Any]) -> str:
    """What each service came to over the window, dearest first."""
    services = values.get("services") or []
    if not services:
        return ""
    currency = esc(values.get("currency") or "USD")
    rows = "".join(
        f'<tr><td>{esc(row["service"])}</td>'
        f'<td class="n">{float(row["amount"] or 0.0):,.2f}</td></tr>'
        for row in services
    )
    return (
        f'<table class="billtab"><tbody>{rows}</tbody></table>'
        f'<p class="dim" style="font-size:12px;margin:4px 0 0">'
        f'last {BILLING_BAR_DAYS} days, in {currency}</p>'
    )


def billing_card(values: dict[str, Any] | None) -> str:
    """What the tenancy has actually cost, as somebody last checked.

    Never a live figure. Reading it needs an OCI key that can create and
    destroy infrastructure, and that key is not on this node and is not going
    to be: the node serves a public gallery and runs code a model wrote.
    ``sketchgen billing`` asks, on the operator's machine; ``--sync`` ships the
    answer here for this card to read.

    So the card shows the number AND how old it is, because a stale figure
    presented as current is worse than none. Oracle's own usage data lags a day
    or more, so even a fresh reading is behind; the card names the window it
    covers rather than implying "now".

    And it shows the two things a single number cannot: the shape of the last
    thirty days, and whether the meter agrees with the machine. A tenancy
    inside its free allowance reads 0.00 every day until the day it doesn't,
    so the dollar figure is the last place the news arrives.
    """
    values = values or {}
    amount = values.get("amount") or ""
    if not amount:
        return (
            '<p class="dim">Nothing recorded yet. On the operator\'s machine, '
            'where <code>~/.oci</code> lives:</p>'
            '<pre style="font-size:12px">python3 bin/sketchgen billing --sync '
            '&lt;host&gt;</pre>'
            '<p class="dim" style="font-size:12px">then record it here — '
            'docs/OPERATIONS.md, &ldquo;The billing card&rdquo;.</p>'
        )
    try:
        big = f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        big = str(amount)
    age = _days_since(values.get("checked", ""))
    stale = ""
    if age is not None and age > BILLING_STALE_DAYS:
        stale = f' <span class="pill rejected">{int(age)} days old</span>'
    line = "charged by Oracle for this tenancy"
    if values.get("through"):
        line += f", through {esc(values['through'])}"
    if values.get("checked"):
        line += f" · last checked {esc(values['checked'])}"
    return (
        f'<p style="font-size:34px;margin:0 0 4px">{esc(big)} '
        f'<span class="dim" style="font-size:16px">{esc(values.get("currency", "USD"))}'
        f'</span>{stale}</p>'
        f'<p class="dim" style="font-size:12px">{line}</p>'
        + billing_tenancy_warning(values)
        + billing_shape_warning(values)
        + billing_bars(values.get("daily") or [])
        + billing_held(values.get("daily") or [])
        + billing_services_table(values)
        + '<p class="dim" style="font-size:12px;margin-top:10px">Not live. '
        'Reading it needs an OCI key that can build and destroy infrastructure, '
        'which is deliberately not on this node; and Oracle\'s usage data lags '
        'a day or more, so this is behind even when freshly checked.</p>'
    )


# ---------------------------------------------------------------------------
# The D12 pair (docs/plans/pooled-pair.md, Packet 1)
#
# Pooled, the executor runs on this node's card and the partner's at once, and
# neither the Node panel (one local card) nor the Model panel (one Ollama)
# can show it. The panel is rendered only on a node that is in a pair, the way
# the bill is only rendered on a cloud node, and every number in it carries
# data-k so the page's two-second poll moves it like any other.
# ---------------------------------------------------------------------------

#: What each role on a card is called on the page. ``runner`` is Ollama's own
#: llama-server, which on the head holds the planner, judge and critic.
PAIR_APP_LABELS = {
    "pool": "the pool (llama-server)",
    "runner": "Ollama (planner, judge, critic)",
    "rpc": "ggml-rpc-server (lent to the pool)",
}


def _pair_warn(text: str) -> str:
    return f'<p class="billwarn soft">{text}</p>'


def pair_panel(doc: dict[str, Any]) -> str:
    """The Pair panel: both cards, the pool, the link. Empty when not in a pair."""
    role = _dig(doc, "pair.role")
    if role == "partner":
        return _pair_partner_panel(doc)
    if role != "head":
        return ""

    apps = _dig(doc, "node.gpu.apps", []) or []
    app_rows = []
    for index, app in enumerate(apps):
        base = f"node.gpu.apps.{index}"
        label = PAIR_APP_LABELS.get(app.get("role") or "", app.get("name") or "?")
        app_rows.append(
            f"<tr><td>{esc(label)}</td>"
            f"<td class=\"n mono\">{field(doc, base + '.pid', 'int')}</td>"
            f"<td class=\"n\">{field(doc, base + '.used_mb', 'gb')}</td></tr>"
        )
    if not app_rows:
        app_rows.append('<tr><td colspan="3" class="dim">nothing on the card</td></tr>')

    here = (
        '<div><h3>This card <span class="dim" style="font-weight:400">— '
        + field(doc, "node.gpu.name") + "</span></h3>"
        + _meter(
            "vram",
            f"{field(doc, 'node.gpu.vram_mb.used', 'gb')} of "
            f"{field(doc, 'node.gpu.vram_mb.total', 'gb')} GB, "
            f"{field(doc, 'node.gpu.util_pct', 'pct')} busy",
            ratio_bar(doc, "node.gpu.vram_mb.used", "node.gpu.vram_mb.total"),
        )
        + '<table style="margin-top:8px"><thead><tr><th>holds it</th>'
        '<th class="n">pid</th><th class="n">GB</th></tr></thead><tbody>'
        + "".join(app_rows)
        + "</tbody></table></div>"
    )

    host = _dig(doc, "pair.partner.host")
    if host:
        read = (
            f"read {field(doc, 'pair.partner.age_s', 'f1')} s ago over ssh"
            if _dig(doc, "pair.partner.age_s") is not None
            else "not read yet"
        )
        there = (
            '<div><h3>Partner\'s card <span class="dim" style="font-weight:400">— '
            + field(doc, "pair.partner.gpu.name") + " on " + esc(host) + "</span></h3>"
            + _meter(
                "vram",
                f"{field(doc, 'pair.partner.gpu.vram_mb.used', 'gb')} of "
                f"{field(doc, 'pair.partner.gpu.vram_mb.total', 'gb')} GB, "
                f"{field(doc, 'pair.partner.gpu.util_pct', 'pct')} busy",
                ratio_bar(doc, "pair.partner.gpu.vram_mb.used",
                          "pair.partner.gpu.vram_mb.total"),
            )
            + '<table style="margin-top:8px"><thead><tr><th>holds it</th>'
            '<th class="n">pid</th><th class="n">cpu</th><th class="n">GB</th></tr></thead><tbody>'
            + f"<tr><td>ggml-rpc-server <span class=\"dim\">serving "
            f"{field(doc, 'pair.partner.rpc.serving', 'yn')}</span></td><td class=\"n mono\">"
            f"{field(doc, 'pair.partner.rpc.pid', 'int')}</td>"
            f"<td class=\"n\">{field(doc, 'pair.partner.rpc.cpu_pct', 'pct')}</td>"
            f"<td class=\"n\">{field(doc, 'pair.partner.rpc.vram_mb', 'gb')}</td></tr>"
            + f"<tr><td>generator</td><td colspan=\"3\">"
            f"{field(doc, 'pair.partner.control')} "
            f"<span class=\"dim\">{field(doc, 'pair.partner.reason')}</span></td></tr>"
            + "</tbody></table>"
            + f'<p class="dim" style="font-size:12px;margin:6px 0 0">{read} · '
            + field(doc, "pair.partner.node") + "</p></div>"
        )
    else:
        there = (
            '<div><h3>Partner\'s card</h3><p class="dim">No partner is configured '
            "(<code>SKETCHGEN_PAIR_PARTNER</code>), so only this side of the pool "
            "is visible.</p></div>"
        )

    tiles = "".join([
        _tile("pool", field(doc, "pair.pool.state"),
              field(doc, "pair.pool.model") + " · " + field(doc, "pair.pool.build")
              + " · ctx " + field(doc, "pair.pool.n_ctx", "int")),
        _tile("decoding now",
              field(doc, "pair.pool.live_decode_tok_s", "f1")
              + ' <span class="dim" style="font-size:12px">tok/s</span>',
              field(doc, "pair.pool.n_decoded", "int") + " tokens · task "
              + field(doc, "pair.pool.task", "int")),
        _tile("decode, served",
              field(doc, "pair.pool.decode_tok_s", "f1")
              + ' <span class="dim" style="font-size:12px">tok/s</span>',
              field(doc, "pair.pool.tokens_out", "int") + " tokens out"),
        _tile("prefill, served",
              field(doc, "pair.pool.prefill_tok_s", "f1")
              + ' <span class="dim" style="font-size:12px">tok/s</span>',
              field(doc, "pair.pool.tokens_in", "int") + " tokens in"),
        _tile("link in",
              field(doc, "pair.link.rx_mb_s", "f2")
              + ' <span class="dim" style="font-size:12px">MB/s</span>',
              field(doc, "pair.link.iface") + " · "
              + field(doc, "pair.link.speed_mbps", "int") + " Mb/s"),
        _tile("link out",
              field(doc, "pair.link.tx_mb_s", "f2")
              + ' <span class="dim" style="font-size:12px">MB/s</span>',
              "rpc port " + field(doc, "pair.link.rpc_port_open", "yn") + " · "
              + field(doc, "pair.link.rpc_connections", "int") + " connected"),
    ])

    # What is wrong now, said once at render: these are states to act on, not
    # numbers to watch, so they are not live-patched.
    warnings = []
    state = _dig(doc, "pair.pool.state")
    if state == "down":
        warnings.append(_pair_warn(
            "The pool is not answering at " + esc(_dig(doc, "pair.pool.url") or "?")
            + ". Nothing can be written on the pair until it is back."))
    if _dig(doc, "pair.link.rpc_port_open") is False:
        warnings.append(_pair_warn(
            "Nothing is listening on the RPC port here: the tunnel to the partner "
            "is down, and the pool cannot reach the partner's card."))
    if host and _dig(doc, "pair.partner.reachable") is False:
        warnings.append(_pair_warn(
            "The last read of the partner failed: "
            + esc(_dig(doc, "pair.partner.error") or "no reason given")
            + ". What is shown for its card is from the last read that worked."))
    if _dig(doc, "pair.partner.control") == "running":
        warnings.append(_pair_warn(
            "The partner's generator is running. While its card is lent it should "
            "be paused: its worker can load models onto the card the pool is using."))
    resident = _dig(doc, "pair.partner.resident", []) or []
    if resident:
        warnings.append(_pair_warn(
            "The partner's Ollama has " + esc(", ".join(resident))
            + " resident, on the card it lends the pool."))

    return (
        '<section class="panel" id="pair">'
        '<h2>Pair <span class="dim" style="font-weight:400;text-transform:none;'
        'letter-spacing:0">— the executor on two cards; this node is the head</span></h2>'
        f'<div class="grid">{here}{there}</div>'
        f'<div class="tiles" style="margin-top:12px">{tiles}</div>'
        + "".join(warnings)
        + "</section>"
    )


def _pair_partner_panel(doc: dict[str, Any]) -> str:
    """This node's card is lent: say so, and what holds it."""
    warning = ""
    if _dig(doc, "worker.control") == "running":
        warning = _pair_warn(
            "This node's generator is running while its card is lent. Pause it: its "
            "worker can load models onto the card the pool on the head is using.")
    tiles = "".join([
        _tile("lent", field(doc, "pair.lent.vram_mb", "gb")
              + ' <span class="dim" style="font-size:12px">GB</span>',
              "held by ggml-rpc-server pid " + field(doc, "pair.lent.rpc_pid", "int")),
        _tile("rpc cpu", field(doc, "pair.lent.cpu_pct", "pct"), "of one core"),
        _tile("serving", field(doc, "pair.lent.serving", "yn"),
              field(doc, "pair.link.rpc_connections", "int") + " connected"),
        _tile("link in", field(doc, "pair.link.rx_mb_s", "f2")
              + ' <span class="dim" style="font-size:12px">MB/s</span>',
              field(doc, "pair.link.iface") + " · "
              + field(doc, "pair.link.speed_mbps", "int") + " Mb/s"),
        _tile("link out", field(doc, "pair.link.tx_mb_s", "f2")
              + ' <span class="dim" style="font-size:12px">MB/s</span>', ""),
    ])
    return (
        '<section class="panel" id="pair">'
        '<h2>Pair <span class="dim" style="font-weight:400;text-transform:none;'
        'letter-spacing:0">— this card is lent to the pool on the head</span></h2>'
        f'<div class="tiles">{tiles}</div>'
        + warning
        + "</section>"
    )


def console_page(doc: dict[str, Any], tokens: dict[str, Any] | None = None,
                 billing: dict[str, Any] | None = None) -> str:
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

    # What the card shows depends on what the node is. A cloud VM is billed by
    # the hour and by the provisioned gigabyte, so its storage costs money and
    # belongs on the page; a desk with a GPU in it is not, and the meter that
    # priced this node's 1 TB at $20.58/mo was quoting Oracle's block-volume
    # rate for a disk that was bought once. The GPU is the mirror image: it is
    # the only thing that explains a local node's timings, and no OCI A1 shape
    # has one. Neither is a default to be overridden -- each is shown where it
    # means something and left out where it does not.
    kind = _dig(doc, "node.kind", "local")
    is_cloud = kind == "cloud"

    meter_list: list[str] = []

    if _dig(doc, "node.gpu.vram_mb.total") is not None:
        meter_list.append(
            _meter(
                "gpu",
                f"{field(doc, 'node.gpu.vram_mb.used', 'gb')} of "
                f"{field(doc, 'node.gpu.vram_mb.total', 'gb')} GB VRAM, "
                f"{field(doc, 'node.gpu.util_pct', 'pct')} busy — "
                f"{field(doc, 'node.gpu.name')}",
                ratio_bar(doc, "node.gpu.vram_mb.used", "node.gpu.vram_mb.total"),
            )
        )

    meter_list.append(
        _meter(
            "memory",
            f"{field(doc, 'node.mem_mb.used', 'gb')} used + "
            f"{field(doc, 'node.mem_mb.cache', 'gb')} cache of "
            f"{field(doc, 'node.mem_mb.total', 'gb')} GB, "
            f"{field(doc, 'node.mem_mb.available', 'gb')} available",
            ratio_bar(doc, "node.mem_mb.used", "node.mem_mb.total")
            + ratio_bar(doc, "node.mem_mb.cache", "node.mem_mb.total", "cache"),
        )
    )
    meter_list.append(
        _meter(
            "swap",
            f"{field(doc, 'node.swap_mb.used', 'gb')} of "
            f"{field(doc, 'node.swap_mb.total', 'gb')} GB",
            ratio_bar(doc, "node.swap_mb.used", "node.swap_mb.total"),
        )
    )
    meter_list.append(
        _meter(
            "boot disk",
            f"{field(doc, 'node.disk_gb.used', 'f1')} used, "
            f"{field(doc, 'node.disk_gb.free', 'f1')} free of "
            f"{field(doc, 'node.disk_gb.total', 'f1')} GB — "
            f"{field(doc, 'node.disk_gb.chromium', 'f1')} chromium",
            ratio_bar(doc, "node.disk_gb.used", "node.disk_gb.total"),
        )
    )

    # Only a real second filesystem. The collector already decides this; when
    # the blobs sit on the boot disk the meter was four dashes and a bar that
    # could not move.
    if _dig(doc, "node.disk_gb.model_volume.separate") is True:
        meter_list.append(
            _meter(
                "model volume",
                f"{field(doc, 'node.disk_gb.models', 'f1')} of models — "
                f"{field(doc, 'node.disk_gb.model_volume.used', 'f1')} used, "
                f"{field(doc, 'node.disk_gb.model_volume.free', 'f1')} free of "
                f"{field(doc, 'node.disk_gb.model_volume.total', 'f1')} GB",
                ratio_bar(
                    doc,
                    "node.disk_gb.model_volume.used",
                    "node.disk_gb.model_volume.total",
                ),
            )
        )
    elif _dig(doc, "node.disk_gb.models") is not None:
        meter_list.append(
            _meter(
                "models on disk",
                f"{field(doc, 'node.disk_gb.models', 'f1')} GB of models, on the "
                "boot disk",
                ratio_bar(doc, "node.disk_gb.models", "node.disk_gb.total"),
            )
        )

    if is_cloud:
        meter_list.append(
            _meter(
                "storage $",
                f"{field(doc, 'node.disk_gb.cost.block_gb', 'f1')} GB block · "
                f"${field(doc, 'node.disk_gb.cost.usd_month', 'f2')}/mo "
                f"({field(doc, 'node.disk_gb.cost.billable_gb', 'f1')} GB over "
                f"{field(doc, 'node.disk_gb.cost.free_tier_gb', 'f1')} free) — "
                + esc(_free_tier_note(doc)),
                ratio_bar(
                    doc,
                    "node.disk_gb.cost.billable_gb",
                    "node.disk_gb.cost.block_gb",
                ),
            )
        )

    meter_list.append(
        _meter(
            "load",
            f"{field(doc, 'node.load.0', 'f2')} / "
            f"{field(doc, 'node.load.1', 'f2')} / "
            f"{field(doc, 'node.load.2', 'f2')} on "
            f"{field(doc, 'node.cores', 'int')} "
            + ("OCPU" if is_cloud else "threads"),
            ratio_bar(doc, "node.load.0", "node.cores"),
        )
    )

    meters = "".join(meter_list)

    # A cloud VM counts OCPUs, which are whole cores; this box counts SMT
    # threads, and calling 12 of those "cores" overstates the machine by two.
    cores_unit = "OCPU" if is_cloud else "threads"

    node_note = ""
    if _dig(doc, "node.wsl") is True:
        node_note = (
            '<p class="dim" style="font-size:12px;margin-top:10px">'
            "WSL: memory, swap and boot disk are this distro's share of the "
            "Windows host, not the whole machine. The GPU figures come from the "
            "host driver, so those are the whole card.</p>"
        )

    # The bill is Oracle's. A local node has no tenancy, no meter and no
    # invoice to be behind on, so the panel is not rendered rather than
    # rendered empty.
    bill_panel = ""
    if is_cloud:
        bill_panel = (
            '<section class="panel"><h2>The bill</h2>'
            + billing_card(billing)
            + "</section>"
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

    # The public's queue, as one number with a link on it. The anchor carries
    # the data-k, not the tile, so the two-second poll patches the count and
    # leaves the link it is written on alone.
    waiting = (
        '<a href="/submissions" data-k="submissions.pending" data-fmt="int">'
        f'{esc(_fmt(_dig(doc, "submissions.pending"), "int"))}</a>'
    )
    submissions_tile = _tile(
        "waiting",
        waiting,
        field(doc, "submissions.released", "int")
        + " released, "
        + field(doc, "submissions.declined", "int")
        + " declined",
    )

    return render(
        "op_console",
        token_spark=console_spark(tokens),
        submissions=submissions_tile,
        shape=esc(_dig(doc, "node.shape", "shape unknown")),
        cores_n=field(doc, "node.cores", "int"),
        cores_unit=cores_unit,
        node_note=node_note,
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
        activity=activity_card(doc),
        pair_panel=pair_panel(doc),
        odometer=odometer,
        funnel_rows="\n".join(funnel_rows),
        per_sketch_rows="\n".join(per_sketch_rows),
        cost_a="cost 16/96",
        cost_b="cost 4/24",
        bill_panel=bill_panel,
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
    elif state in ("published", "failed-kept", "rejected"):
        href = f"{GALLERY_URL}e/{entry_id}/"
    else:
        # Archived, and anything else with no public page: the id still links
        # somewhere, because /entry/<id> is the only way back to an archived
        # entry and a dead number in this column helps nobody.
        href = f"/entry/{entry_id}"
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

    return render(
        "op_queue",
        tiles=tiles,
        activity=activity_card(doc, compact=True),
        rows="\n".join(rows),
    )


# ---------------------------------------------------------------------------
# The new-job form
# ---------------------------------------------------------------------------

#: The Ollama this page asks what models exist. Same default and same
#: environment variable as the worker's, because it is the same Ollama: a UI
#: offering a model the worker cannot reach would be a menu of lies.
OLLAMA_HOST = models.DEFAULT_HOST

#: The planner choice that means "whatever the worker is configured with". It
#: was the only local choice until 2026-09-19 and is still what a saved default,
#: a ``?parent=`` preset or an old bookmark carries, so it is accepted on the
#: way in and resolved to a real tag before it reaches the database. It is
#: rendered as an option only when the model host cannot be asked for the
#: real ones.
LOCAL = "local"
PAID = "paid"

#: An Ollama model tag, for the case where the host cannot be reached to check
#: the name against the real list. Deliberately narrow: this is the last thing
#: between a form field and a column the worker will hand to a model host.
MODEL_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,110}$")


def paid_choices(*, sentinel: bool = False) -> tuple[tuple[str, str], ...]:
    """What the menus offer off the node besides Ollama's proxied tags: nothing.

    Until 2026-09-21 this listed ``paid`` and every registered paid model id,
    so an operator could queue a job for the laptop from this page. Two jobs
    queued that way that day (one from here, one inherited by a critique
    child) parked at ``needs-laptop`` with no agent at the other end, and the
    console counted them as broken for hours. A paid job only makes sense
    with an agent present for it, and the agent is at a shell, not at this
    page: `sketchgen paid start` is the one way such a job is made, and it
    leases the job to the agent that made it. This page offers what this node
    runs. ``sentinel`` is kept for callers and means nothing.
    """
    return ()


class ModelMenu(NamedTuple):
    """One "which model" select on the New job page.

    The planner's and the executor's are the same menu over different
    questions, so they are the same code over a different descriptor: which
    capability a model needs to be offered, which model a job gets when nobody
    chooses, what is worth saying on each row, and what sits under *off this
    node* besides the proxied models.
    """

    #: The form field and the ``jobs`` column, which are the same word.
    field: str
    #: What a model must be able to do to appear. See :mod:`sketchgen.models`.
    capability: str
    #: Reads ``worker.DEFAULT_PLANNER_MODEL`` or ``executor.DEFAULT_MODEL`` when
    #: it is asked rather than when this module is imported, so the menu follows
    #: a worker configured with something else.
    default: Callable[[], str]
    #: Capabilities worth naming on a row. A menu filtered to vision does not
    #: name vision on every row; one filtered to completion does.
    mention: tuple[str, ...]
    #: Choices under *off this node* that Ollama does not list — ``paid`` and
    #: the models named in ``SKETCHGEN_PAID_MODELS`` (:func:`paid_choices`).
    #: Asked when the menu is drawn, so a unit file's list is read, not frozen.
    off_node: Callable[[], tuple[tuple[str, str], ...]]
    #: How the refusal sentence describes what the menu wanted.
    wanted: str


PLANNER_MENU = ModelMenu(
    field="planner",
    capability=models.PLANNER_CAPABILITY,
    default=lambda: worker.DEFAULT_PLANNER_MODEL,
    # Every row is vision-capable by construction, so saying so on every row is
    # width spent on nothing. ``audio`` is worth the space: one model has it.
    mention=("audio",),
    off_node=lambda: paid_choices(sentinel=True),
    wanted="vision-capable models, or paid",
)

EXECUTOR_MENU = ModelMenu(
    field="executor",
    capability=models.EXECUTOR_CAPABILITY,
    default=lambda: executor.DEFAULT_MODEL,
    # Here vision IS news: it says which model could be shown the gate's
    # screenshot rather than the text of build_evidence().
    mention=("vision", "audio"),
    off_node=lambda: paid_choices(sentinel=True),
    wanted="models that can complete",
)


def menu_models(menu: ModelMenu, host: str | None = None) -> list[models.Model]:
    """The models this node could run this step with.

    Empty when the model host does not answer, which is not an error — see
    :func:`menu_groups` for what the page shows then.
    """
    return models.with_capability(
        models.catalogue(host or OLLAMA_HOST), menu.capability
    )


def menu_groups(
    menu: ModelMenu, host: str | None = None
) -> list[tuple[str, list[tuple[str, str]]]]:
    """The menu, as ``(group label, [(value, label), …])``.

    Two groups, because the difference between them is the one an operator has
    to see before they press Queue: a model on this node costs electricity and
    nothing else, and a model *proxied* by this node — a ``-cloud`` tag, or the
    planner's ``paid`` route — sends the prompt off the box. The gallery's
    headline numbers (0 API calls, $0.006 a sketch) are true of the first group
    only.

    With no answer from the model host the first group holds one entry, the
    worker's configured default, which is exactly the choice this page offered
    before it could ask.
    """
    default = menu.default()
    found = menu_models(menu, host)
    # The worker's default is always offered, even if it fails the capability
    # filter: it is the model a job gets when nobody chooses, so a menu that
    # cannot express it is a menu that cannot say what the page is about to do.
    if found and not any(model.name == default for model in found):
        fallback = next(
            (model for model in models.catalogue(host or OLLAMA_HOST)
             if model.name == default),
            None,
        )
        if fallback is not None:
            found = [fallback] + found
    here = [
        (model.name,
         models.label(model, default=default, mention=menu.mention))
        for model in found
        if not model.remote
    ]
    if not here:
        here = [(
            LOCAL,
            f"local — {default} (the model host did not answer; this is the "
            "worker's default)",
        )]
    away = [
        (model.name,
         models.label(model, mention=menu.mention) + " — ollama.com, leaves this node")
        for model in found
        if model.remote
    ]
    return [
        ("on this node", here),
        ("off this node", away + list(menu.off_node())),
    ]


def menu_selected(menu: ModelMenu, value: str, host: str | None = None) -> str:
    """Which option the select should open on, given what the form carries.

    The form may carry ``local`` — from the built-in defaults, a saved default
    or an old bookmark — where the menu now lists real tags. Resolving it here
    rather than leaving it unmatched matters: a ``<select>`` with nothing
    selected opens on its *first* option, so a page that could not match the
    saved choice would quietly offer a different model than the one the job
    would have used.
    """
    offered = [
        option for _, options in menu_groups(menu, host) for option, _ in options
    ]
    resolved = menu_column(menu, value)
    for candidate in (resolved, value):
        if candidate in offered:
            return candidate
    return offered[0] if offered else LOCAL


def menu_values(menu: ModelMenu, host: str | None = None) -> set[str]:
    """Every value this select can carry, for the validator."""
    values = {LOCAL}
    for _, options in menu_groups(menu, host):
        values.update(option for option, _ in options)
    return values


def check_menu(menu: ModelMenu, value: str, host: str | None = None) -> str:
    """The form value, or raise ValueError with the sentence the page shows.

    A tag that is not in the catalogue is refused — unless there is no
    catalogue, because the host blinked between the render and the press. Then
    a well-formed tag is taken at its word: the worker hands it to the real
    Ollama and fails the job with a sentence naming the model, which is a
    better place for that news than a refused form.
    """
    if not value:
        return LOCAL
    if value in menu_values(menu, host):
        return value
    if not menu_models(menu, host) and MODEL_TAG_RE.match(value):
        return value
    raise ValueError(
        f"{value} is not a model this node offers as {menu.field} — pick one "
        f"from the menu ({menu.wanted})"
    )


def menu_column(menu: ModelMenu, value: str) -> str:
    """The form value as the ``jobs`` column wants it: the model tag itself.
    ``local`` is resolved here and nowhere else. ``paid`` — a saved default or
    a bookmark from before 2026-09-21 — is the worker's default now: this page
    no longer makes paid jobs (see :func:`paid_choices`)."""
    if value in ("", LOCAL, PAID):
        return menu.default()
    return value


# The two menus, by the name the rest of the module calls them.

def planner_groups(host: str | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    return menu_groups(PLANNER_MENU, host)


def planner_values(host: str | None = None) -> set[str]:
    return menu_values(PLANNER_MENU, host)


def planner_selected(value: str, host: str | None = None) -> str:
    return menu_selected(PLANNER_MENU, value, host)


def check_planner(value: str, host: str | None = None) -> str:
    return check_menu(PLANNER_MENU, value, host)


def planner_column(value: str) -> str:
    return menu_column(PLANNER_MENU, value)


def executor_groups(host: str | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    return menu_groups(EXECUTOR_MENU, host)


def executor_values(host: str | None = None) -> set[str]:
    return menu_values(EXECUTOR_MENU, host)


def executor_selected(value: str, host: str | None = None) -> str:
    return menu_selected(EXECUTOR_MENU, value, host)


def check_executor(value: str, host: str | None = None) -> str:
    return check_menu(EXECUTOR_MENU, value, host)


def executor_column(value: str) -> str:
    return menu_column(EXECUTOR_MENU, value)


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


def _grouped_options(
    groups: Iterable[tuple[str, Iterable[tuple[str, str]]]], selected: str
) -> str:
    """The same as :func:`_options`, under ``<optgroup>`` labels. Empty groups
    are dropped rather than rendered as an empty heading."""
    out = []
    for group, options in groups:
        rendered = _options(options, selected)
        if rendered:
            out.append(f'<optgroup label="{esc(group)}">{rendered}</optgroup>')
    return "".join(out)


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


#: Where the New job page keeps the operator's own defaults — one row in the
#: meta scratchpad (migration 003), JSON, under this key. The built-in ones are
#: what the page showed before anyone pressed "Save as defaults", and what it
#: shows again after "Forget them".
DEFAULTS_KEY = "new_job_defaults"
BUILTIN_DEFAULTS: dict[str, Any] = {
    "planner": LOCAL,
    "executor": LOCAL,
    "rules": "treatment",
    "publication": "hold",
    "max_attempts": "3",
    "assert": [],
    "size_w": "400",
    "size_h": "400",
}

#: How many recent spawnable entries the parent picker offers, and how many
#: recent root prompts the prompt panel offers to reuse.
PARENT_PICK_LIMIT = 12
RECENT_PROMPTS_LIMIT = 8


def load_defaults(conn: sqlite3.Connection) -> tuple[dict[str, Any], str | None]:
    """The saved defaults on top of the built-in ones, and when they were saved.

    A missing row, an unreadable one, or one from a database still on
    migration 001 all mean "the built-in ones": the page renders either way.
    """
    merged = {key: list(value) if isinstance(value, list) else value
              for key, value in BUILTIN_DEFAULTS.items()}
    # The assignment (`sketchgen paid assign`, agentic-cli §3.6) sits between
    # the built-in defaults and the saved ones: it is the node's answer to
    # "which model runs this step", and a person's saved defaults for this
    # page are still theirs to keep.
    assignment = db.get_assignment(conn)
    for step, field in (("plan", "planner"), ("execute", "executor")):
        if assignment.get(step):
            merged[field] = assignment[step]
    raw = db.get_meta(conn, DEFAULTS_KEY)
    if not raw:
        return merged, None
    try:
        doc = json.loads(raw)
    except ValueError:
        return merged, None
    if not isinstance(doc, dict):
        return merged, None
    for key in BUILTIN_DEFAULTS:
        if key in doc:
            merged[key] = doc[key]
    saved = doc.get("saved_utc")
    return merged, str(saved) if saved else None


def defaults_from(form: dict[str, list[str]]) -> dict[str, Any]:
    """What the form says the defaults should be, checked the way a job is.

    Raises ValueError with the sentence the page should show. The assertions
    are kept as the ticked chip values (``size`` rather than ``size(400,400)``)
    because that is what re-ticks them; the size box keeps its own two numbers.
    """

    def one(name: str, default: str) -> str:
        values = form.get(name) or []
        return (values[0] if values else default).strip()

    planner_choice = check_planner(one("planner", LOCAL))
    executor_choice = check_executor(one("executor", LOCAL))
    rules = one("rules", "treatment")
    if rules not in {value for value, _ in RULES_CHOICES}:
        raise ValueError("rules must be control, treatment or random")
    publication = one("publication", "hold")
    if publication not in {value for value, _ in PUBLICATION_CHOICES}:
        raise ValueError("publication must be hold or auto")
    raw_attempts = one("max_attempts", "3")
    if not raw_attempts.isdigit() or not 1 <= int(raw_attempts) <= 10:
        raise ValueError("max attempts must be a whole number from 1 to 10")
    chip_values = {"size" if word == "size(w,h)" else word for word in planner.VOCAB}
    ticked = [value for value in (form.get("assert") or []) if value in chip_values]
    size_w, size_h = one("size_w", "400"), one("size_h", "400")
    if not (size_w.isdigit() and size_h.isdigit()):
        raise ValueError("size(w,h) needs two whole numbers")
    return {
        "planner": planner_choice,
        "executor": executor_choice,
        "rules": rules,
        "publication": publication,
        "max_attempts": raw_attempts,
        "assert": ticked,
        "size_w": size_w,
        "size_h": size_h,
    }


def save_defaults(conn: sqlite3.Connection, form: dict[str, list[str]]) -> str:
    """Write the form's options as the page's defaults. Returns the flash."""
    doc = defaults_from(form)
    doc["saved_utc"] = db.utc_now()
    db.set_meta(conn, DEFAULTS_KEY, json.dumps(doc))
    words = ", ".join(doc["assert"]) or "none"
    return (
        f"Saved as defaults — {doc['planner']} · {doc['executor']} · "
        f"{doc['rules']} · {doc['publication']} · {doc['max_attempts']} "
        f"attempts · assertions: {words}"
    )


def clear_defaults(conn: sqlite3.Connection) -> str:
    """Back to the built-in defaults. Returns the flash."""
    db.set_meta(conn, DEFAULTS_KEY, None)
    return "Forgot the saved defaults — the page shows the built-in ones again"


def _model_word(value: str | None) -> str:
    """An entry's or job's ``planner``/``executor`` column as a value this form
    can carry.

    Both columns hold a model tag, and ``planner`` may hold ``paid``
    (migration 001). Until the menus listed real tags this collapsed everything
    that was not ``paid`` into ``local``; now the tag is the answer, so picking
    a parent preselects the models that parent was made with and a line stays a
    comparison with itself. A row from before any model was recorded still says
    ``local``.
    """
    text = (value or "").strip()
    if text == PAID or models.is_paid(text):
        # A parent made off the node: its child is made here, as spawn() does
        # it since 2026-09-21, so the picker preselects the worker's default.
        return LOCAL
    return text or LOCAL


#: The old name, kept because the parent card and picker read it as one.
_planner_word = _model_word


def _spawnable_rows(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    marks = ",".join("?" for _ in lineage.SPAWNABLE)
    return conn.execute(
        f"SELECT * FROM entries WHERE state IN ({marks}) "
        "ORDER BY created_utc DESC, id DESC LIMIT ?",
        (*sorted(lineage.SPAWNABLE), limit),
    ).fetchall()


#: The tick box under the parent card, and the hidden field that tells
#: :func:`create_job` the box was on the page at all. A checkbox posts nothing
#: when it is clear, so without the marker a form that never carried one and a
#: form whose box was unticked arrive identical — and the difference between
#: them is the difference between *the node decides* and *this job is the
#: control arm*. The block is always in the DOM and hidden until a parent is
#: picked, so that the script that fills the card in place (op_layout.html,
#: ``pickParent``) has something to unhide instead of something to rebuild.
_SOURCE_TICK = (
    '<div class="opts" id="source-tick"{hidden}>'
    '<input type="hidden" name="source_form" value="1">'
    '<label><input type="checkbox" id="give_source" name="give_source" '
    'value="1"{checked}> give the executor the parent\'s sketch</label>'
    '</div>'
    '<p class="help" id="source-tick-help"{hidden}>Ticked, the first attempt is '
    "shown the parent's kept <code>sketch.js</code> under a heading in its "
    "brief, and a repair is shown its own previous attempt. Untick it and this "
    "job alone is written the way every job was before 2026-09-21 — from the "
    "prompt and the critique and nothing else. That is the control arm: one "
    "line with the source and one without, without moving "
    "<code>executor_source</code> for the whole node and sweeping up every "
    "job the worker claims in between.</p>"
)


def _source_tick(parent_id: int | None, ticked: bool = True) -> str:
    """The *give the executor the parent's sketch* box, hidden with no parent.

    ``ticked`` is False only when a refused POST is being drawn again and the
    operator had cleared it: a form that comes back with a choice silently
    undone is a form that queues the job the person did not ask for.
    """
    hidden = "" if parent_id else " hidden"
    return _SOURCE_TICK.format(hidden=hidden, checked=" checked" if ticked else "")


def _parent_card(
    app: App,
    conn: sqlite3.Connection,
    parent_id: int | None,
    ticked: bool = True,
) -> str:
    """The entry the new job descends from, drawn so the operator can see it.

    A bare id was the whole of this field until 2026-09-18, and a number is
    not something a person can check. This is the strip, the state, the
    generation, the root prompt and the newest revision — enough to know
    whether it is the entry you meant — and a refusal in red when the line
    cannot grow from it.

    Under it since 2026-09-22, and only with a parent picked, the one thing
    about a child the operator now has a say in: whether its executor is shown
    the parent's code (packet 18). The box is outside ``#parent-card`` on
    purpose — the page's script replaces that element's innerHTML when a
    parent is picked without a reload, and a field inside it would be thrown
    away mid-form.
    """
    if not parent_id:
        return (
            '<p class="help" id="parent-card">none — a fresh root. Pick one below '
            "and the job is a child of it: the queue and the job page say so, and "
            "its models and rules are preset from it so the line stays a fair "
            "comparison with itself.</p>" + _source_tick(None, ticked)
        )
    row = db.get_entry(conn, parent_id)
    if row is None:
        return (f'<p class="err" id="parent-card">there is no entry {parent_id}</p>'
                + _source_tick(None, ticked))
    state = str(row["state"])
    root, revisions = lineage.split_prompt(row["prompt"] or "")
    generation = lineage.generation_of(conn, parent_id)
    revision = (
        f'<p class="rev"><span class="k">revise:</span> {esc(revisions[-1])}</p>'
        if revisions
        else ""
    )
    refusal = (
        ""
        if state in lineage.SPAWNABLE
        else f'<p class="err">entry {parent_id} is {esc(state_label(state))}: a line '
        "grows from a held, published or kept entry, so this will be refused</p>"
    )
    return (
        f'<div class="parent-card" id="parent-card" data-prompt="{esc(root)}">'
        f"{_entry_image(app, row)}"
        f'<div><p class="meta"><a href="/entry/{parent_id}">entry {parent_id}</a> '
        f'<span class="pill {esc(state)}">{esc(state_label(state))}</span> '
        f'<span class="dim">generation {generation} · '
        f'{esc(_model_word(row["planner"]))} · {esc(_model_word(row["executor"]))} · '
        f'{esc(row["rules_file"] or "—")} · {esc(row["submitted_by"] or "—")}</span></p>'
        f'<p class="prompt">{esc(root or "—")}</p>{revision}'
        '<p class="help"><button type="button" class="link" id="use-parent-prompt">'
        "use its prompt</button> as the starting point, or write a fresh one.</p>"
        f"</div></div>{refusal}{_source_tick(parent_id, ticked)}"
    )


def _parent_picker(app: App, conn: sqlite3.Connection, chosen: int | None) -> str:
    """The last few entries a line can grow from, as one-line rows.

    Each row is a link to ``/new?parent=<id>`` so it works with no script; with
    the page's script a click fills the id box and the card instead, and the
    prompt already typed stays where it is.
    """
    rows = _spawnable_rows(conn, PARENT_PICK_LIMIT)
    if not rows:
        return ""
    items = []
    for row in rows:
        entry_id = int(row["id"])
        state = str(row["state"])
        root, _ = lineage.split_prompt(row["prompt"] or "")
        image = _entry_image(app, row)
        thumb = image if image.startswith("<img") else '<span class="no-img"></span>'
        items.append(
            f'<a class="pick{" on" if entry_id == chosen else ""}" '
            f'href="/new?parent={entry_id}" data-parent="{entry_id}" '
            f'data-state="{esc(state_label(state))}" data-state-class="{esc(state)}" '
            f'data-planner="{esc(_model_word(row["planner"]))}" '
            f'data-executor="{esc(_model_word(row["executor"]))}" '
            f'data-rules="{esc(row["rules_file"] or "")}" data-prompt="{esc(root)}">'
            f'{thumb}<span class="n">{entry_id}</span>'
            f'<span class="pill {esc(state)}">{esc(state_label(state))}</span>'
            f'<span class="p">{esc(truncate(root, 80))}</span></a>'
        )
    return (
        '<details class="pick-fold"><summary>recent entries a line can grow from</summary>'
        f'<div class="picks">{"".join(items)}</div></details>'
    )


def _recent_prompts(conn: sqlite3.Connection) -> str:
    """The last few root prompts, to run again — under other rules, say."""
    rows = conn.execute(
        "SELECT id, prompt, state, rules_file FROM jobs WHERE critique IS NULL "
        "ORDER BY created_utc DESC, id DESC LIMIT 60"
    ).fetchall()
    seen: set[str] = set()
    items = []
    for row in rows:
        root, _ = lineage.split_prompt(row["prompt"] or "")
        key = " ".join(root.lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        href = "/new?prompt=" + urllib.parse.quote(root)
        items.append(
            f'<li><a href="{esc(href)}" data-prompt="{esc(root)}">{esc(truncate(root, 110))}</a>'
            f' <span class="dim">— job {int(row["id"])} · {esc(row["rules_file"] or "—")} · '
            f'{esc(state_label(str(row["state"])))}</span></li>'
        )
        if len(items) >= RECENT_PROMPTS_LIMIT:
            break
    if not items:
        return ""
    return (
        '<details class="recent-fold"><summary>recent prompts, to run again</summary>'
        f'<ul class="recent">{"".join(items)}</ul></details>'
    )


def _submitter_block(typed: str) -> str:
    """Who the job is signed as: a pill when the node knows, a box when not.

    ``typed`` is what a re-rendered form carried. A name that is not the
    login opens the box with it, so a refused job queued for a student does
    not lose the student's name.
    """
    login, source = operator_login()
    if login is None:
        return (
            '<label for="submitted_by">GitHub username</label>'
            '<input type="text" id="submitted_by" name="submitted_by" required '
            f'pattern="[A-Za-z0-9-]{{1,39}}" value="{esc(typed)}" '
            'placeholder="whose job this is">'
            '<p class="help">No GitHub login on this node, so this is typed. '
            "<code>gh auth login</code> as the operator, or set "
            "<code>SKETCHGEN_OPERATOR</code> in the web unit, and it becomes a pill.</p>"
        )
    other = bool(typed) and typed != login
    return (
        f'<p class="signed"><span class="pill ok" title="via {esc(source)}">{esc(login)}</span>'
        f'<span class="dim">via {esc(source)}</span>'
        '<button type="button" class="link" id="other-submitter"'
        f'{" hidden" if other else ""}>queue it for somebody else</button></p>'
        f'<div id="other-box"{"" if other else " hidden"}>'
        '<label for="submitted_by">Their GitHub username</label>'
        '<input type="text" id="submitted_by" name="submitted_by" '
        f'pattern="[A-Za-z0-9-]{{1,39}}" value="{esc(typed if other else "")}" '
        f'placeholder="blank means {esc(login)}">'
        "</div>"
        '<p class="help">Only a GitHub username ever goes on a job (course policy: '
        "no personal data). It is what the queue, the job page and the gallery "
        "show as the author.</p>"
    )


def _queue_note(conn: sqlite3.Connection, control: db.Control | None) -> str:
    queued = _count(conn, "SELECT COUNT(*) FROM jobs WHERE state = 'queued'")
    waiting = (
        "the queue is empty"
        if queued == 0
        else f"behind {queued} queued job{'' if queued == 1 else 's'}"
    )
    text, _, _ = pill_for(control)
    if text == "RUNNING":
        return waiting
    return f"{waiting} · worker {text.lower()}, so it waits"


def _defaults_note(saved_utc: str | None) -> str:
    if saved_utc:
        return (
            f"Defaults saved {esc(saved_utc[:16].replace('T', ' '))} UTC: the "
            "assertions and the run options open this way. Save again to move them."
        )
    return (
        "Built-in defaults. Set the assertions and the run options the way you "
        "usually want them, then Save as defaults and the page opens that way."
    )


def new_page(
    app: App,
    conn: sqlite3.Connection,
    form: dict[str, list[str]] | None = None,
    error: str | None = None,
    control: db.Control | None = None,
) -> str:
    """The form. ``form`` is what a refused POST carried, or what a link asked
    for (``?parent=``, ``?prompt=``); every field it does not carry comes from
    the saved defaults."""
    given = form or {}
    defaults, saved_utc = load_defaults(conn)
    base: dict[str, list[str]] = {
        key: (list(value) if isinstance(value, list) else [str(value)])
        for key, value in defaults.items()
    }
    base.update({key: list(values) for key, values in given.items()})
    form = base

    def one(name: str, default: str = "") -> str:
        values = form.get(name) or []
        return values[0] if values else default

    parent_raw = one("parent_entry_id").strip()
    parent_id = int(parent_raw) if parent_raw.isdigit() else None
    picked = set(form.get("assert") or [])
    # The tick box is per job and is never a saved default (`defaults_from`
    # keeps only BUILTIN_DEFAULTS), so it opens ticked every time — except on
    # a POST this page refused, where the operator's own choice is still in
    # the form and has to survive the redraw.
    ticked = not (one("source_form") and not one("give_source"))
    return render(
        "op_new",
        error=f'<p class="err">{esc(error)}</p>' if error else "",
        prompt=esc(one("prompt")),
        many_checked=" checked" if one("many") else "",
        recent=_recent_prompts(conn),
        submitter=_submitter_block(one("submitted_by").strip()),
        parent_entry_id=esc(parent_raw),
        parent_card=_parent_card(app, conn, parent_id, ticked),
        parent_picker=_parent_picker(app, conn, parent_id),
        planner_options=_grouped_options(
            planner_groups(), planner_selected(one("planner", LOCAL))
        ),
        executor_options=_grouped_options(
            executor_groups(), executor_selected(one("executor", LOCAL))
        ),
        chips=_chips(picked, one("size_w", "400"), one("size_h", "400")),
        rules_options=_options(RULES_CHOICES, one("rules", "treatment")),
        publication_options=_options(PUBLICATION_CHOICES, one("publication", "hold")),
        max_attempts=esc(one("max_attempts", "3")),
        queue_note=esc(_queue_note(conn, control)),
        assignment_note=_assignment_note(conn),
        defaults_note=_defaults_note(saved_utc),
    )


def _assignment_note(conn: sqlite3.Connection) -> str:
    """Which model each step is assigned to, and the warning where it matters.

    agentic-cli §3.5: a paid executor is a much larger second variable in the
    A/B than two local models are, and the place to say so is where the choice
    is made, not in a document.
    """
    assignment = db.get_assignment(conn)
    parts = [
        f"{step} {esc(assignment.get(step) or 'this node')}"
        for step in db.ASSIGNABLE_STEPS
    ]
    note = (
        '<p class="help">Assigned: ' + " · ".join(parts)
        + " — set with <code>sketchgen paid assign</code>.</p>"
    )
    executor_model = assignment.get("execute")
    if executor_model and models.is_paid(executor_model, conn):
        note += (
            f'<p class="err">Every job that names no executor is written by '
            f"{esc(executor_model)}, off this node: one round trip per attempt "
            "through <code>sketchgen paid</code>, and each entry badged off-node. "
            "That is a far larger difference than two local models, inside an "
            "experiment measuring the rules file.</p>"
        )
    return note


def form_from_query(
    conn: sqlite3.Connection, query: dict[str, list[str]]
) -> dict[str, list[str]]:
    """What a link into /new asks the form to start with.

    ``?parent=<id>`` fills the parent and — as :func:`sketchgen.lineage.spawn`
    does — presets planner, executor and rules from that entry, so a line stays
    a fair comparison with itself unless the operator changes them on purpose.
    ``?prompt=`` fills the prompt: the recent-prompts list and anything else
    that wants to hand a sentence to this page.
    """
    form: dict[str, list[str]] = {}
    parent = (query.get("parent") or [""])[0].strip()
    if parent.isdigit():
        form["parent_entry_id"] = [parent]
        row = db.get_entry(conn, int(parent))
        if row is not None and row["state"] in lineage.SPAWNABLE:
            # Only preset a model the menu still offers: a parent made with a
            # tag since removed from the node would otherwise select nothing.
            planned = _model_word(row["planner"])
            if planned in planner_values():
                form["planner"] = [planned]
            written = _model_word(row["executor"])
            if written in executor_values():
                form["executor"] = [written]
            if row["rules_file"] in {value for value, _ in RULES_CHOICES}:
                form["rules"] = [str(row["rules_file"])]
    prompt = (query.get("prompt") or [""])[0]
    if prompt.strip():
        form["prompt"] = [prompt]
    return form


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
    submitted_by = one("submitted_by") or (github_login() or "")
    if not submitted_by:
        raise ValueError(
            "no GitHub login on this node to sign the job with — type the "
            "submitter's GitHub username, or `gh auth login` / set "
            "SKETCHGEN_OPERATOR so the page knows who you are"
        )
    if not USERNAME_RE.match(submitted_by):
        raise ValueError(
            "submitted by must be a GitHub username — letters, digits and "
            "hyphens, nothing else (course policy: no personal data)"
        )
    planner_choice = check_planner(one("planner", LOCAL))
    executor_choice = check_executor(one("executor", LOCAL))
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
        parent_row = db.get_entry(conn, parent)
        if parent_row is None:
            raise ValueError(f"there is no entry {parent} to descend from")
        if parent_row["state"] not in lineage.SPAWNABLE:
            raise ValueError(
                f"entry {parent} is {state_label(str(parent_row['state']))}: a "
                "line grows from a held, published or kept entry, not a "
                "rejected or archived one"
            )

    # Migration 017. `executor_source` is what a caller that means it names
    # outright; the New job page's tick box says the same thing in the form a
    # person reads, and `source_form` is how an unticked box is told from a
    # form that never had one. Ticked, or absent, is NULL: the job follows
    # `meta.executor_source` at the moment each attempt runs, as every job did
    # before 2026-09-22.
    executor_source = one("executor_source")
    if not executor_source and one("source_form") and not one("give_source"):
        executor_source = "none"
    if executor_source and executor_source not in worker.SOURCE_SWITCH_VALUES:
        raise ValueError(
            "executor source must be one of "
            + ", ".join(worker.SOURCE_SWITCH_VALUES)
        )

    words, problem = _assertions_from(form)
    if problem:
        raise ValueError(problem)

    options: dict[str, Any] = {
        "planner": planner_column(planner_choice),
        "executor": executor_column(executor_choice),
        "rules_file": rules,
        "publication": publication,
        "max_attempts": int(raw_attempts),
        "parent_entry_id": parent,
        "executor_source": executor_source or None,
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


def create_jobs(
    conn: sqlite3.Connection, form: dict[str, list[str]]
) -> list[tuple[int, int]]:
    """One job, or with ``many`` ticked one per non-empty line of the prompt.

    Every line gets the same submitter, parent, assertions and options — the
    point of the tick is a batch of prompts under one setting, which is what
    MEASURE[agents-md-ab] wants fed to ``rules=random``. The lines are all
    checked before any is queued, so a bad line refuses the lot rather than
    half of it.
    """
    if not (form.get("many") or [""])[0].strip():
        return [create_job(conn, form)]
    raw = (form.get("prompt") or [""])[0]
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        raise ValueError("a job needs a prompt")
    # A dry pass first: create_job validates and then writes, so give it a
    # savepoint to unwind if a later line is the one that is wrong.
    conn.execute("SAVEPOINT many")
    try:
        queued = [create_job(conn, {**form, "prompt": [line]}) for line in lines]
    except Exception:
        conn.execute("ROLLBACK TO many")
        conn.execute("RELEASE many")
        raise
    conn.execute("RELEASE many")
    return queued


def queued_flash(queued: list[tuple[int, int]]) -> str:
    if len(queued) == 1:
        job_id, position = queued[0]
        return f"Queued as #{job_id} · position {position}"
    first, _ = queued[0]
    last, position = queued[-1]
    return f"Queued {len(queued)} jobs, #{first} to #{last} · last at position {position}"


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


#: Microphone *input* — the one thing a sandboxed, opaque-origin preview iframe
#: cannot do. `getUserMedia` is refused without a real origin and an
#: `allow="microphone"` grant, and `sandbox="allow-scripts"` gives neither, so a
#: listening sketch reads a dead mic in every embedded frame and only works in
#: its own top-level tab (http://localhost and 127.0.0.1 are secure contexts, so
#: the operator's own preview URL opens there with a real mic). Producing sound —
#: `p5.Oscillator`, `loadSound` — is deliberately NOT here: it plays in place
#: after an in-canvas click, so it keeps the ordinary run-in-page button.
_MIC_RE = re.compile(r"p5\.AudioIn|getUserMedia|mediaDevices")


def needs_microphone(app: App, job_id: int, attempt_n: int) -> bool:
    """True when this attempt's source opens the microphone (see :data:`_MIC_RE`)."""
    directory = attempt_dir(app, job_id, attempt_n)
    text = ""
    for name in ("sketch.js", "index.html"):
        try:
            text += (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return bool(_MIC_RE.search(text))


def _poster_url(app: App, job_id: int, attempt_n: int) -> str | None:
    """The still the play button is drawn on: the strip, else the last frame.

    The strip is four frames of the run and says more about a sketch than one
    does, so it is preferred; ``gate.png`` is the fallback for an attempt whose
    strip could not be produced. A run the budget stopped part way through still
    has both, of the frames it did step.
    """
    gate_dir = app.jobs_root / str(job_id) / f"attempt-{attempt_n}" / ".gate"
    for name in ("strip.png", "gate.png"):
        if (gate_dir / name).is_file():
            return f"/jobs/{job_id}/attempt-{attempt_n}/.gate/{name}"
    return None


def cost_chip(report: dict[str, Any] | None) -> str:
    """What the gate's run cost, as a chip, and a warning when it is news.

    Two numbers and one judgement. ``total_s`` is how long the gate took, which
    is the tell that found job 45 and job 166 in the first place; the frame
    budget is what the gate now measures directly. The chip is a warning when
    either says so, and a warning on this page means: do not press play on a
    machine you cannot afford to lose.
    """
    if not report:
        return '<span class="chip cost dim">no gate report</span>'
    timings = report.get("timings") or {}
    checks = report.get("checks") or {}
    total = timings.get("total_s")
    rate = timings.get("ms_per_frame")
    budget_failed = checks.get("frame_budget") is False
    slow = isinstance(total, (int, float)) and total > SLOW_GATE_S
    parts = []
    parts.append("gate " + (human_seconds(total) if total is not None else "—"))
    if rate is not None:
        parts.append(f"{rate:g} ms/frame")
    else:
        parts.append("ms/frame not recorded")
    text = " · ".join(parts)
    if budget_failed or slow:
        why = ("the frame budget failed" if budget_failed
               else f"the gate took longer than {human_seconds(SLOW_GATE_S)}")
        return (f'<span class="chip cost warn" title="{esc(why)} — running this '
                f'costs the same here as it did there">⚠ {esc(text)}</span>')
    return f'<span class="chip cost">{esc(text)}</span>'


def preview_frame(
    app: App,
    job_id: int,
    attempt_n: int,
    *,
    summary: str | None = None,
    report: dict[str, Any] | None = None,
    tab_link: bool = True,
) -> str:
    """The attempt, as a still with a play button. Empty when there is nothing.

    It does NOT run. Until 2026-09-15 this returned a live ``<iframe>``, and the
    Held page embedded one per held entry, so opening that page started every
    held sketch at once in the operator's browser — which is how entry 165 and
    entry 269 each ran a laptop out of memory. Nothing here autoplays now: the
    poster is a button, the script below builds the frame on a click, and it
    builds at most one on the page at a time.

    ``summary`` still wraps the poster in a collapsed ``<details>``; it is now
    tidiness rather than safety, because a folded poster and an open one cost
    the same.

    ``tab_link=False`` drops the "open in a tab" anchor. The decision card says
    everything about an entry on one meta line and carries the link there; the
    job page, where a poster stands on its own, keeps it. The cost chip is not
    optional either way — it is the warning beside the button that starts the
    run, and the run costs this machine what it cost the gate.
    """
    if not has_preview(app, job_id, attempt_n):
        return ""
    url = f"/preview/{job_id}/{attempt_n}/"
    poster = _poster_url(app, job_id, attempt_n)
    label = f"job {job_id}, attempt {attempt_n}"
    still = (
        f'<img src="{esc(poster)}" alt="four frames the gate saw of {esc(label)}" '
        f'loading="lazy">'
        if poster else
        '<span class="no-still">no frame on disk</span>'
    )
    # A microphone sketch cannot run in the sandboxed frame this page builds:
    # the mic is refused an opaque origin, so an embedded run shows a dead canvas
    # and reads as a broken sketch. For those the tab IS the run — the poster is
    # a link, not a play button, and the mic note stands where the tab anchor
    # otherwise would. The tab link is unconditional here (tab_link is about the
    # convenience anchor beside an ordinary run; this is the only way to run it).
    if needs_microphone(app, job_id, attempt_n):
        frame = (
            f'<div class="preview-run preview-mic">'
            f'<a class="play" href="{esc(url)}" target="_blank" rel="noopener" '
            f'aria-label="run {esc(label)} in a tab">{still}'
            f'<span class="play-label">run in a tab ▸</span></a>'
            f"</div>"
            f'<p class="dim" style="font-size:12px">'
            f"{cost_chip(report)}"
            f' · <span class="mic-note">microphone sketch — the embedded '
            f"frame can't reach the mic, so it runs only in its own tab</span>"
            f"</p>"
        )
    else:
        tab = (
            f' <a href="{esc(url)}" target="_blank" rel="noopener">open in a tab ↗</a>'
            if tab_link
            else ""
        )
        frame = (
            f'<div class="preview-run" data-preview data-src="{esc(url)}" '
            f'data-label="{esc(label)}" data-w="{PREVIEW_W}" data-h="{PREVIEW_H}">'
            f'<button type="button" class="play" data-play '
            f'aria-label="run {esc(label)}">{still}'
            f'<span class="play-label" data-play-label>run ▸</span></button>'
            f"</div>"
            f'<p class="dim" style="font-size:12px">'
            f"{cost_chip(report)}{tab}"
            f"</p>"
        )
    if summary is None:
        return frame
    return (
        f'<details class="preview-fold"><summary>{esc(summary)}</summary>'
        f"{frame}</details>"
    )


PREVIEW_SCRIPT = """<script>
// Click to run, one at a time, and gone when you stop.
//
// The Held page used to embed a live iframe per held entry, so opening it ran
// every held sketch at once; two of those sketches allocated a GPU buffer per
// line() per frame and the machine went down. So: the server renders a still
// with a play button and no frame at all, this builds the frame on a click, and
// starting one stops whichever was already running. Stopping REMOVES the
// element rather than hiding it — a hidden iframe is still a running sketch
// still holding its buffers.
(function () {
  var running = null;

  function stop() {
    if (!running) { return; }
    var host = running;
    running = null;
    var frame = host.querySelector("iframe.preview");
    if (frame) { frame.remove(); }
    host.classList.remove("running");
    var label = host.querySelector("[data-play-label]");
    if (label) { label.textContent = "run \u25b8"; }
    var button = host.querySelector("[data-play]");
    if (button) {
      button.setAttribute("aria-label", "run " + (host.getAttribute("data-label") || "sketch"));
    }
  }

  function start(host) {
    stop();
    var button = host.querySelector("[data-play]");
    if (!button) { return; }
    var frame = document.createElement("iframe");
    frame.className = "preview";
    frame.src = host.getAttribute("data-src");
    frame.width = host.getAttribute("data-w") || "640";
    frame.height = host.getAttribute("data-h") || "400";
    frame.title = (host.getAttribute("data-label") || "sketch") + ", running";
    // Scripts and nothing else: no same-origin, no forms, no top navigation.
    // The same sandbox the gallery's own pages use.
    frame.setAttribute("sandbox", "%s");
    host.insertBefore(frame, button);
    host.classList.add("running");
    var label = host.querySelector("[data-play-label]");
    if (label) { label.textContent = "stop \u25a0"; }
    button.setAttribute("aria-label", "stop " + (host.getAttribute("data-label") || "sketch"));
    running = host;
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest("[data-play]") : null;
    if (!button) { return; }
    var host = button.closest("[data-preview]");
    if (!host) { return; }
    event.preventDefault();
    if (running === host) { stop(); } else { start(host); }
  });

  // Leaving the page, or folding the attempt away, should not leave a sketch
  // running behind it.
  window.addEventListener("pagehide", stop);
  document.addEventListener("toggle", function (event) {
    if (event.target && event.target.tagName === "DETAILS" && !event.target.open
        && running && event.target.contains(running)) { stop(); }
  }, true);
})();
</script>""" % (PREVIEW_SANDBOX,)


def _artefacts(app: App, job_id: int, attempt_n: int) -> str:
    shots = []
    gate_dir = app.jobs_root / str(job_id) / f"attempt-{attempt_n}" / ".gate"
    # ghost.png is third because it is youngest and because it is the one an
    # attempt need not have: only runs gated since 2026-09-21 wrote one, and a
    # window the budget stopped wrote none (auto-mouse.md §5.3).
    for name, caption in (("strip.png", "four frames"), ("gate.png", "last frame"),
                          ("ghost.png", "with the ghost pointer")):
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


def process_line(attempt: db.Attempt) -> str:
    """What the agent said this attempt's work cost it, for the footer line.

    Migration 015, beside the reply's own numbers and never added to them:
    the seconds and tokens to the left of it are the round trip and the reply,
    and these are the session around it. Empty for every local attempt, which
    has no agent to have reported anything.
    """
    try:
        found = json.loads(attempt.process_json or "")
    except ValueError:
        return ""
    if not isinstance(found, dict):
        return ""
    parts = []
    if isinstance(found.get("session_s"), (int, float)):
        parts.append(f"{human_seconds(found['session_s'])} session")
    for key, word in (("output_tokens", "generated"),
                      ("thinking_tokens", "thinking"),
                      ("tool_calls", "tool calls"),
                      ("screenshots", "screenshots"),
                      ("tries", "tries")):
        if isinstance(found.get(key), int) and not isinstance(found[key], bool):
            parts.append(f"{found[key]:,} {word}")
    if found.get("effort"):
        parts.append(f"effort {esc(found['effort'])}")
    if not parts:
        return ""
    return " · process: " + " · ".join(parts) + " (as reported by the agent)"


def given_line(job: db.Job, attempt: db.Attempt) -> str:
    """What this attempt was shown before it wrote, in one line.

    `given: parent entry 1103 · 180 lines · ctx 16384` on a child's first
    attempt, `given: attempt 1 · 143 lines · ctx 16384` on a repair,
    `given: nothing` on everything else — which is every attempt this node ran
    before 2026-09-21 and every first attempt on a job with no parent
    (migration 016). Job 1327 on 2026-09-22 is the one this is drawn for: the
    first attempt this node wrote with a previous sketch under the heading,
    134 lines of it, at ctx 16384, and nothing on the page said so.

    ``kind`` parent carries no entry id of its own, because for a parent
    source there is only ever one candidate — the job's own
    ``parent_entry_id`` — and repeating it in the record would be a second
    place for it to be wrong. ``ctx —`` is an attempt written off the node: no
    local context window applied to it, and an em dash is true where a number
    would be invented.
    """
    given = _given_source(attempt)
    if given is None:
        return "given: nothing"
    kind = str(given.get("kind") or "")
    if kind == "parent":
        what = f"parent entry {job.parent_entry_id}" if job.parent_entry_id else "the parent"
    elif kind == "previous":
        what = f"attempt {attempt.n - 1}"
    else:  # pragma: no cover - the record's kind is one of the two
        what = kind or "something"
    parts = [what]
    lines = given.get("lines")
    if isinstance(lines, int) and not isinstance(lines, bool):
        parts.append(f"{lines} lines")
    if not given.get("shown"):
        # Found, read, and over `meta.source_max_chars`: the heading carried
        # one line naming it instead of the code, and that is a different fact
        # about this attempt from having been given nothing at all
        # (MEASURE[source-shown-rate] counts these).
        parts.append("not shown (over the cap)")
    parts.append(f"ctx {attempt.num_ctx}" if attempt.num_ctx else "ctx —")
    return "given: " + " · ".join(parts)


def human_bytes(size: Any) -> str:
    """``61.4 kB``, ``1.2 MB``, or empty when the gate recorded no number.

    Decimal thousands, because the unit is written kB and not KiB. The
    gallery's ``_size_word`` spells a size the same way and this is a second
    copy on purpose: the operator UI does not import the static generator, and
    a size on an operator's screen is not worth making it start to.
    """
    if isinstance(size, bool) or not isinstance(size, (int, float)) or size < 0:
        return ""
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{size / 1000:.1f} kB"


def loads_items(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """``report.resources_loaded``, or an empty list. Never raises.

    The gate began recording what arrived over the network with
    `loads(image)` (media-assertion.md §3.1); every report written before
    that has no such key, which is every attempt on this node until the
    harness that has it is deployed.
    """
    found = (report or {}).get("resources_loaded")
    if not isinstance(found, list):
        return []
    return [item for item in found if isinstance(item, dict)]


def loads_hosts(report: dict[str, Any] | None) -> list[str]:
    """The hosts one attempt fetched a picture from, once each, in order."""
    hosts: list[str] = []
    for item in loads_items(report):
        host = str(item.get("host") or "").strip()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def loads_line(report: dict[str, Any] | None) -> str:
    """What this attempt fetched from the web, in one line, or nothing.

    `loads: picsum.photos (image/jpeg, 61.4 kB)`, several joined with the
    page's own middle dot. Nothing at all when the attempt fetched nothing,
    unlike :func:`given_line`, which always says something: `given: nothing`
    is a fact about how an attempt was written, and every attempt has one,
    whereas a `loads: nothing` under all 900 jobs on this node would be a line
    saying that a sketch drew what sketches draw.

    Host, type and size, which is what the published record carries too
    (``gallery.LOADS_KEYS``, DECIDE[image-hosts]). The whole URL is in the
    attempt's own ``report.json``, which this page serves under
    ``/jobs/…/.gate/``, for the operator who needs to open the picture.
    """
    said = []
    for item in loads_items(report):
        host = str(item.get("host") or "").strip() or "an unrecorded host"
        facts = [
            fact
            for fact in (str(item.get("type") or "").strip(),
                         human_bytes(item.get("bytes")))
            if fact
        ]
        said.append(host + (f" ({', '.join(facts)})" if facts else ""))
    if not said:
        return ""
    return "loads: " + " · ".join(said)


def _given_source(attempt: db.Attempt) -> dict[str, Any] | None:
    """``attempts.given_source_json``, parsed, or None. Never raises."""
    try:
        found = json.loads(attempt.given_source_json) if attempt.given_source_json else None
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


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
        loads = loads_line(report)
        blocks.append(
            '<section class="panel">'
            f"<h2>Attempt {attempt.n} — <span style='text-transform:none'>"
            f"gate {esc(verdict)}"
            + (f" (exit {attempt.gate_exit})" if attempt.gate_exit is not None else "")
            + "</span></h2>"
            f'<div class="grid"><div>{_check_list(report)}</div>'
            f"<div>{_artefacts(app, job.id, attempt.n)}</div></div>"
            + preview_frame(
                app, job.id, attempt.n, summary=f"Run attempt {attempt.n}",
                report=report,
            )
            + f"{evidence}"
            '<p class="dim" style="font-size:12px">'
            f"model {esc(attempt.model or '—')} · rules {esc(attempt.rules_file or '—')} · "
            f"prompt {esc(attempt.prompt_version or '—')} · "
            f"{esc(attempt.prompt_tokens or 0)} in / {esc(attempt.completion_tokens or 0)} out · "
            f"{esc(human_seconds(attempt.wall_s))} wall"
            + process_line(attempt)
            + f"<br>{esc(given_line(job, attempt))}"
            # And, on the attempts that fetched something, what arrived:
            # entry 1103's five attempts each pulled a photograph off
            # picsum.photos and no screen on this node said so
            # (media-assertion.md §4).
            + (f"<br>{esc(loads)}" if loads else "")
            + "</p></section>"
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
        ("parent entry", f"entry {job.parent_entry_id}" if job.parent_entry_id else "—"),
        ("critique", job.critique or "—"),
        ("critique by", job.critique_by or "—"),
        # Migration 015: the two things about a job only the agent that drove
        # it could say. `since` is declared, not measured — the node's clock
        # never saw the work before `paid start` (job 1286, entry 1279).
        ("since (UTC, declared)", job.since_utc or "—"),
        ("note", job.note or "—"),
        ("submitted by", job.submitted_by),
        ("created (UTC)", job.created_utc),
        ("updated (UTC)", job.updated_utc),
        ("last error", job.last_error or "—"),
    ]
    # Migration 017, and only when it is set: a job that left the column NULL
    # follows `meta.executor_source` like every job before 2026-09-22, and a
    # row saying so on all of them would bury the handful that do not. The
    # value is named as the override it is, because a reader comparing two
    # lines needs to know which of them was told something different from the
    # node (MEASURE[source-follow]).
    if job.executor_source:
        rows.insert(
            [key for key, _ in rows].index("executor") + 1,
            ("executor source",
             f"{job.executor_source} — set on this job, over the node's setting"),
        )
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
        f'<p class="dim" style="font-size:12px;margin:10px 0 0">Or write a fresh prompt '
        f'<a href="/new?parent={int(entry["id"])}">descended from entry {int(entry["id"])}</a>.</p>'
        "</section>"
        if entry is not None
        else ""
    )
    # The latest attempt, running, above the fold: the instructor's reason for
    # this screen is to look at the sketch before publishing it, and a frame
    # strip is not a sketch.
    latest_report = _report_for(app, job.id, attempts[-1]) if attempts else None
    latest = (
        preview_frame(app, job.id, attempt_n, report=latest_report)
        if attempt_n else ""
    )
    preview = (
        '<section class="panel"><h2>The sketch — '
        f"<span style='text-transform:none'>attempt {attempt_n}, "
        f"click the still to run it</span></h2>"
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
    """The strip, but only when it really is inside the jobs directory.

    The fallback image, and only that. A card's picture is the run button's
    poster, which is this same file (:func:`_poster_url` prefers ``strip.png``),
    so until 2026-09-15 every held card drew the four-frame strip twice — once
    under the play button and once again beneath it. The poster won; this is
    what an entry with no attempt left to run gets instead.
    """
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


def _offplan_names(row: sqlite3.Row) -> list[str]:
    """The assertions this entry's kept attempt missed, if it missed any."""
    try:
        raw = row["offplan_json"]
    except (IndexError, KeyError):
        return []
    if not raw:
        return []
    try:
        names = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(n) for n in names] if isinstance(names, list) else []


def _held_summary(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """What this card is asking a person to judge.

    Two different things arrive in Held now. One passed the gate outright. The
    other spent every attempt without ever failing a QA check and diverged from
    the plan the planner wrote — it runs, it is just not what was predicted, and
    whether that is a mistake or the interesting part is exactly the judgement a
    person is here to make.

    :func:`_gate_summary` reads the LAST attempt, which for an off-plan entry is
    not the attempt the card shows: the entry keeps its best one. So say which
    attempt is on screen and what it diverged on, rather than quoting a verdict
    on a different attempt entirely.
    """
    missed = _offplan_names(row)
    if not missed:
        return _gate_summary(conn, int(row["job_id"]))
    kept = ""
    source = str(row["source_dir"] or "")
    if source:
        tail = Path(source).name
        if tail.startswith("attempt-"):
            kept = f" (attempt {tail.split('-', 1)[1]} kept)"
    return f"off-plan{kept}: runs clean, missed " + ", ".join(missed)


def _runnable_attempt(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    """Which attempt of an entry the card's run button runs, if any.

    The entry's own ``source_dir`` is the attempt the gate passed, so that is
    the one to run; when it is missing or outside the jobs directory the
    attempts table is walked backwards instead. None when no attempt has an
    index.html left on disk, which is the card's cue to fall back to
    :func:`_entry_image`.
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
        if has_preview(app, job_id, n):
            return n
    return None


def _report_for_n(app: App, job_id: int, attempt_n: int) -> dict[str, Any] | None:
    """report.json for one attempt, by its number rather than its row."""
    path = app.jobs_root / str(job_id) / f"attempt-{attempt_n}" / ".gate" / "report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lineage_row(conn: sqlite3.Connection, entry_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT parent_entry_id, generation, critique_by FROM lineage "
        "WHERE child_entry_id = ?",
        (entry_id,),
    ).fetchone()


def _lineage_note(
    conn: sqlite3.Connection, row: sqlite3.Row, *, with_generation: bool = True
) -> str:
    """Where this entry came from, in words.

    ``with_generation=False`` drops the leading "generation N, ": the card's
    header already carries the generation beside the state, and this packet is
    about the Held page saying a thing once. Nothing else changed here.
    """
    lineage = _lineage_row(conn, int(row["id"]))
    if lineage is not None:
        head = f"generation {lineage['generation']}, " if with_generation else ""
        return (
            f"{head}from entry {lineage['parent_entry_id']}"
            + (f", critiqued by {lineage['critique_by']}" if lineage["critique_by"] else "")
        )
    if row["parent_entry_id"]:
        return f"child of entry {row['parent_entry_id']}"
    return "a root prompt, no lineage"


def _kept_attempt_n(conn: sqlite3.Connection, row: sqlite3.Row) -> int | None:
    """Which attempt an entry is, by number: the one copied onto the row.

    ``entries.source_dir`` is the attempt ``_create_entry`` chose, which is
    not always the last (``best_attempt``), and the gallery resolves the same
    thing the same way (``gallery._kept_attempt``). The last attempt stands in
    when the column names nothing, which is what the gallery falls back to
    as well — one answer to "which sketch is this entry", on both screens.
    """
    name = Path(str(row["source_dir"] or "")).name
    if name.startswith("attempt-") and name[8:].isdigit():
        return int(name[8:])
    found = conn.execute(
        "SELECT MAX(n) AS n FROM attempts WHERE job_id = ?", (int(row["job_id"]),)
    ).fetchone()
    return int(found["n"]) if found is not None and found["n"] is not None else None


def _card_loads(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Where this entry's picture comes from, for the card's meta line.

    The host and nothing else, because the decision this card is for is
    whether to publish, and publishing a sketch that fetches a photograph
    publishes a page that sends every viewer to a third party under that
    party's terms (DECIDE[image-hosts], DECIDE[image-licence]: the picture is
    recorded, not resolved, and the entry's own CC BY covers the code). Seeing
    that took opening the attempt until now.

    The kept attempt's report, not the runnable one's: the entry *is* that
    attempt, and the gallery page this decision produces reads the same file.

    Two hosts are joined with "and" rather than the line's own middle dot,
    which separates the facts on it: *image from a · b · job 5* reads as
    three things and is two.
    """
    attempt_n = _kept_attempt_n(conn, row)
    if attempt_n is None:
        return ""
    hosts = loads_hosts(_report_for_n(app, int(row["job_id"]), attempt_n))
    if not hosts:
        return ""
    what = "image" if len(hosts) == 1 else "images"
    return f" · {what} from {esc(' and '.join(hosts))}"


def _card_face(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Everything above the buttons: the number, the sketch, the prompt, the line.

    One number, one image, one meta line. The Held page used to say "entry 271"
    in six places and draw the four-frame strip twice — the poster the run
    button sits on *is* the strip — so the operator read the same card six
    times to make one decision. The id is the card's heading and nowhere else
    in words; the buttons keep it in their ``aria-label`` so a screen reader
    still hears which entry it is about.

    Shared by the Held card and the read-only /entry/<id> page, which differ
    only in what is under this.
    """
    entry_id = int(row["id"])
    job_id = int(row["job_id"])
    state = str(row["state"])
    lineage_row = _lineage_row(conn, entry_id)
    generation = (
        f'<span class="dim">generation {esc(lineage_row["generation"])}</span>'
        if lineage_row is not None
        else ""
    )
    # The sketch, running on a click. Publication is a person's decision
    # (DECIDE[publication-gate]) and this is the part of it a still frame
    # cannot carry — so the still is the button, and the strip is drawn once.
    attempt_n = _runnable_attempt(app, conn, row)
    if attempt_n is None:
        stage = _entry_image(app, row)
        tab = ""
    else:
        stage = preview_frame(
            app,
            job_id,
            attempt_n,
            report=_report_for_n(app, job_id, attempt_n),
            tab_link=False,
        )
        url = f"/preview/{job_id}/{attempt_n}/"
        tab = (
            f' · <a href="{esc(url)}" target="_blank" rel="noopener">'
            "open in a tab ↗</a>"
        )
    # The root sentence and the newest revision, split the way the gallery's
    # entry page splits them: a generation-10 prompt is 1,700 characters of
    # stapled amendments and no operator reads it on a card. Neither half is
    # clamped — the critique is what the decision turns on.
    root, revisions = lineage.split_prompt(row["prompt"] or "")
    revision = (
        f'<p class="rev"><span class="k">revise:</span> {esc(revisions[-1])}</p>'
        if revisions
        else ""
    )
    return (
        '<div class="id">'
        f'<span class="n">{entry_id}</span>'
        f'<span class="pill {esc(state)}">{esc(state_label(state))}</span>'
        f"{generation}</div>"
        f"{stage}"
        f'<p class="prompt">{esc(root or "—")}</p>'
        f"{revision}"
        f'<p class="meta">{esc(_held_summary(conn, row))}'
        f" · {esc(row['rules_file'] or '—')} · {esc(row['executor'] or '—')}"
        f"{_card_loads(app, conn, row)}"
        f' · job <a href="/job/{job_id}">{job_id}</a>'
        f" · {esc(_lineage_note(conn, row, with_generation=False))}"
        f"{tab}</p>"
    )


#: The static hint under a card nobody has marked yet: what the four toggles
#: are for, and the promise that none of them is a request (§4.1).
CARD_HINT = (
    "Mark one outcome, and Critique if it should have a child. "
    "Nothing happens until Process."
)

#: How a batch item's state is coloured on its card. Quiet is "not yet", amber
#: is "now", green is "done", red is either kind of bad news — the same
#: vocabulary the pills use everywhere else in this UI.
CARD_PILLS = {
    "queued": "quiet",
    "working": "warn",
    "done": "ok",
    "refused": "bad",
    "failed": "bad",
}

#: Which item a card wears when it carries two (Publish *and* Critique are two
#: items), worst news first: what the operator has to do something about is
#: what the card should say.
_PILL_ORDER = ("failed", "refused", "working", "queued", "done")


@dataclass
class CardState:
    """What a batch has to say about one entry, on that entry's own card.

    Built from the batch's items for this entry, which is one of them usually
    and two when the card was marked Publish *and* Critique. ``do``, ``cri`` and
    ``text`` are only filled for a finished batch's refusals and failures: those
    cards come back **pre-marked**, with the sentence still in the box and the
    reason in the hint, so that a fix-and-retry is one press (§1.10).
    """

    pill: str = ""          # queued | working | done | refused | failed
    message: str = ""
    do: str = ""            # publish | reject | archive, already checked
    cri: bool = False
    text: str = ""
    marked: bool = False


def _card_states(batch: Batch | None) -> dict[int, CardState]:
    """One :class:`CardState` per entry the batch has an item for."""
    if batch is None:
        return {}
    states: dict[int, CardState] = {}
    done = batch.state == "done"
    for item in batch.items:
        state = states.setdefault(item.entry_id, CardState())
        if _PILL_ORDER.index(item.state) < _PILL_ORDER.index(state.pill or "done"):
            state.pill = item.state
        if item.message:
            state.message = (
                f"{state.message}; {item.message}" if state.message else item.message
            )
        if done and item.state in ("refused", "failed"):
            # Still held, still marked. The mark it comes back with is the mark
            # it went out with, because nothing about it has been done.
            state.marked = True
            if item.verb == "critique":
                state.cri = True
            else:
                state.do = item.verb
            if item.text:
                state.text = item.text
    return states


def _card_hint(state: CardState | None) -> str:
    """The one sentence under a card's toggles.

    Server-rendered it is the static line, or the item's own news once a batch
    has something to report about this entry; the script rewrites it on every
    press with what Process will do to this card in particular.
    """
    if state is None or not state.message:
        return f'<p class="hint decide-hint">{esc(CARD_HINT)}</p>'
    if state.pill in ("refused", "failed"):
        # ``data-reason`` is what tells the script to leave this sentence alone:
        # why the last press did not work is more use than what the next one
        # would do, right up until the operator touches the card.
        return (
            f'<p class="hint decide-hint bad" data-reason="1">{esc(state.message)}'
            " — still held, still marked.</p>"
        )
    return f'<p class="hint decide-hint">{esc(state.message)}</p>'


def _toggle(
    entry_id: int,
    css: str,
    label: str,
    aria: str,
    *,
    name: str,
    value: str = "",
    checked: bool = False,
    disabled: bool = False,
) -> str:
    """One verb, as a mark rather than a request.

    A radio for each of the three outcomes and a checkbox for Critique, all
    bound to the page's one form by ``form="held-batch"``. Radios are what make
    one-outcome-per-card true with no script at all; the script only adds
    press-again-to-clear and the Reject/Critique exclusion (§1.2).
    """
    kind = "checkbox" if not value else "radio"
    return (
        f'<label class="tog {css}"><input type="{kind}" form="held-batch" '
        f'name="{name}"'
        + (f' value="{esc(value)}"' if value else ' value="on"')
        + f' aria-label="{esc(aria)} entry {entry_id}"'
        + (" checked" if checked else "")
        + (" disabled" if disabled else "")
        + f"><span>{label}</span></label>"
    )


def _decision_card(
    app: App,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    kept: bool,
    state: CardState | None = None,
    disabled: bool = False,
) -> str:
    """One entry waiting for a person: one box, four marks, and no request.

    The four verbs used to be four submit buttons with four ``formaction``s, so
    a decision was a round trip and thirty decisions were thirty of them — and
    a second press during any one of them was a second publisher queuing on the
    gallery checkout lock behind the first. They are toggles now. Pressed, a
    verb fills with its own colour and nothing else happens; the only request on
    the page is Process in the tray, which runs every mark as one batch (§1.1).

    The controls are not in a form of their own: they join the page-wide
    ``held-batch`` form by attribute, so the cards stay siblings in the grid
    rather than nesting a form inside each one. Publish, Reject and Archive are
    radios and therefore one choice; Critique is a checkbox and stacks with
    Publish or Archive, or stands alone — a child is queued and the entry stays
    held. A kept rejection — the gate refused every attempt and the worker kept
    it (spec §9) — has no Reject, because it is a rejection already; publishing
    it puts it on the gallery's rejections page rather than the grid.

    The box is read at Process time by whichever marked verb reads it: the
    reason for Reject, where an empty box has always meant "the operator gave
    none", and the revision sentence for Critique, where it is not allowed to
    be empty. Archiving deletes nothing — not the row, not the attempt
    directories, not the strip — and the archived entry is still readable at
    /entry/<id>.

    ``state`` is what a running or finished batch has to say about this entry,
    and ``disabled`` shuts every control while one runs: the page that pressed
    is not the only page that might be open, and "busy" lives on the server.
    """
    entry_id = int(row["id"])
    placeholder = (
        "one sentence for a child"
        if kept
        else "why you are rejecting, or one sentence for a child"
    )
    marks = state or CardState()
    classes = "panel card decide"
    if marks.marked:
        classes += " marked"
    if marks.pill == "working":
        classes += " is-working"
    elif marks.pill == "done":
        classes += " is-done"
    pill = ""
    if marks.pill:
        classes += " has-state"
        pill = (
            f'<span class="pill st {CARD_PILLS.get(marks.pill, "quiet")}">'
            f"{esc(marks.pill)}</span>"
        )
    need = " need" if marks.cri and not marks.text.strip() else ""
    reject = (
        ""
        if kept
        else _toggle(
            entry_id, "rej", "× Reject", "Mark to reject",
            name=f"do-{entry_id}", value="reject",
            checked=marks.do == "reject", disabled=disabled,
        )
    )
    return (
        f'<section class="{classes}" id="entry-{entry_id}" '
        f'aria-label="Entry {entry_id}">'
        f"{pill}"
        f"{_card_face(app, conn, row)}"
        '<div class="say">'
        f'<input type="text" form="held-batch" name="text-{entry_id}" '
        f'id="say-{entry_id}" maxlength="{BATCH_TEXT_MAX}"'
        + (f' class="{need.strip()}"' if need else "")
        + f' value="{esc(marks.text)}" '
        f'aria-label="{esc(placeholder)}" placeholder="{esc(placeholder)}"'
        + (" disabled" if disabled else "")
        + ">"
        f'<div class="acts" data-entry="{entry_id}"'
        + (' data-kept="1"' if kept else "")
        + ">"
        + _toggle(
            entry_id, "pub", "+ Publish", "Mark to publish",
            name=f"do-{entry_id}", value="publish",
            checked=marks.do == "publish", disabled=disabled,
        )
        + reject
        + _toggle(
            entry_id, "cri", "› Critique", "Mark for a critique child of",
            name=f"cri-{entry_id}", checked=marks.cri, disabled=disabled,
        )
        + _toggle(
            entry_id, "arc", "− Archive", "Mark to archive",
            name=f"do-{entry_id}", value="archive",
            checked=marks.do == "archive", disabled=disabled,
        )
        + "</div>"
        + _card_hint(state)
        + "</div>"
        "</section>"
    )


def held_page(app: App, conn: sqlite3.Connection) -> tuple[str, str]:
    """The Held page: ``(the tray for the header, the body)``.

    Two pieces because they belong in two slots of the layout — the tray is the
    sticky header's second row and the cards are the page — and one function
    because they are one snapshot of one batch. Reading ``app.batch`` twice
    could show a tray from one moment and a card from the next.
    """
    batch = _batch_snapshot(app)
    running = batch is not None and batch.state == "running"
    states = _card_states(batch)
    rows = _entry_rows(conn)
    cards = [
        _decision_card(
            app, conn, row, kept=False,
            state=states.get(int(row["id"])), disabled=running,
        )
        for row in rows
    ]
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
    kept_cards = [
        _decision_card(
            app, conn, row, kept=True,
            state=states.get(int(row["id"])), disabled=running,
        )
        for row in kept
    ]
    if not kept_cards:
        kept_cards.append(
            '<section class="panel"><p class="dim">no kept rejection is waiting.'
            "</p></section>"
        )
    body = render(
        "op_held",
        cards="\n".join(cards),
        kept_count=len(kept),
        kept_cards="\n".join(kept_cards),
    )
    return batch_tray(batch, waiting=len(rows), kept=len(kept)), body


def entry_page(app: App, conn: sqlite3.Connection, entry_id: int) -> str:
    """One entry by id, read-only. The only way back to an archived one.

    No page lists archived entries — that is what archiving them was for — so
    this is where they are still readable, along with every other entry whose
    id somebody has written down. It renders and it does nothing: no publish,
    no reject, no archive, no spawn. The decisions live on /held, where the
    entries waiting for one are.
    """
    row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return (
            f'<section class="panel"><h2>Entry {entry_id}</h2>'
            '<p class="dim">there is no such entry.</p></section>'
        )
    state = str(row["state"])
    if state == "archived":
        where = (
            "Archived by the operator: off the Held page and the kept list, "
            "and nowhere else. Nothing was deleted — this row, its attempts "
            "under jobs/ and its strip are all still on disk."
        )
    elif state == "held":
        where = f'Waiting for a decision on <a href="/held#entry-{entry_id}">Held</a>.'
    elif row["published_utc"]:
        where = f'On the site: <a href="{esc(GALLERY_URL)}e/{entry_id}/">e/{entry_id}/</a>.'
    else:
        where = "Not on the site: nobody has published it."
    # The same card as /held — header, poster, prompt, meta — and no form.
    return (
        f'<section class="panel card" id="entry-{entry_id}" '
        f'aria-label="Entry {entry_id}">'
        f"{_card_face(app, conn, row)}"
        f'<p class="meta">{where}</p>'
        "</section>"
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


def said(form: dict, *legacy: str) -> str:
    """What the operator typed in the card's one text box.

    The decision card posts a single field called ``text`` to four routes and
    lets the button say which one reads it. Everything written before that
    posted ``reason`` to /reject and ``critique`` to /spawn — the job page's
    spawn form still does, and so do scripts and bookmarks nobody here can see
    — so each route still answers to its own old name. ``text`` first, the
    legacy name after, empty when neither carries anything.
    """
    for name in ("text", *legacy):
        value = (form.get(name) or [""])[0].strip()
        if value:
            return value
    return ""


def _reject_reason(reason: str) -> str:
    """What the entry's ``reject_reason`` column gets.

    An empty box is allowed and has always meant "the operator gave none"
    (§1.3), so the words are the same whether the reason came from a single
    press or from a batch item, and both callers spell it the same way.
    """
    return reason.strip() or "rejected by operator"


def _reject_state(
    conn: sqlite3.Connection, entry_id: int, reason: str
) -> str | None:
    """The first half of a rejection: the state flip, and no git at all.

    Split out of :func:`reject_entry` because a batch does the two halves in
    different places — every rejection's state moves first, and then one
    :func:`publish.publish_many` carries the whole batch's commits to the
    gallery in one push (§1.6). Returns ``None`` when the flip happened and the
    refusal message when it did not, so the single route and the batch refuse
    an entry that is no longer ``held`` in exactly the same words.
    """
    row = conn.execute(
        "SELECT id, job_id, state FROM entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        return f"there is no entry {entry_id}"
    if row["state"] != "held":
        return f"entry {entry_id} is {row['state']}, not held — nothing changed"
    reason = _reject_reason(reason)
    try:
        db.entry_transition(conn, entry_id, "rejected", reject_reason=reason)
    except (db.IllegalTransition, db.UnknownEntry) as exc:
        return f"refused: {exc}"
    job = db.get_job(conn, int(row["job_id"]))
    if job is not None and job.state == "held":
        db.transition(conn, job.id, "rejected", last_error=reason)
    return None


def reject_entry(
    app: App, conn: sqlite3.Connection, entry_id: int, reason: str
) -> str:
    """Reject one held entry and publish it to the rejections catalog.

    Rejecting used to be a state flip and nothing else, which is how 32
    sketches came to be invisible with every one of their files still on the
    node, and how eleven published entries came to descend from a parent the
    site had never heard of. So a rejection now goes out the same way a
    publication does: the reason is stored on the entry, and the entry is
    handed to the same render-and-push path :func:`publish_entry` uses (§5.2).
    A person pressed the button, which is what spec §9 asks of anything that
    reaches the public repository.

    **No file is deleted or moved.** The attempt directories under ``jobs/``,
    the strip and the gate report are exactly where they were; this question
    has been asked once already and the answer is in the code now.

    A push that fails leaves the entry ``rejected`` with a null
    ``published_utc``, which is how the console already shows a kept failure
    that is waiting: pending, and publishable again later.
    """
    refusal = _reject_state(conn, entry_id, reason)
    if refusal is not None:
        return refusal
    published = publish_entry(app, conn, entry_id)
    return f"Entry {entry_id} rejected — {_reject_reason(reason)}. {published}"


def archive_entry(conn: sqlite3.Connection, entry_id: int) -> str:
    """Take one entry off the operator's lists without deleting anything.

    **No file is deleted or moved**, and nor is the row: archiving is about
    which lists an entry appears on, and about nothing else. Legal from
    ``held``, and from ``failed-kept`` while nobody has published it; refused
    with a message on anything else, which is the whole point of doing it
    through the entry state machine rather than an UPDATE here (§5.3).
    """
    try:
        db.archive_entry(conn, entry_id)
    except db.UnknownEntry:
        return f"there is no entry {entry_id}"
    except db.IllegalTransition as exc:
        return f"refused: {exc} — nothing changed, and no file was touched"
    return (
        f"Entry {entry_id} archived — off the lists, nothing deleted; "
        f"it is still at /entry/{entry_id}"
    )


# ---------------------------------------------------------------------------
# Held, as a batch (packet 11)
#
# The four verbs on a card stop being four requests and become four marks; one
# press of Process runs the lot. Why: a publish is a render, two commits and
# two pushes, and the page waits on all of it before it redirects. Thirty
# entries waiting is thirty waits, and a second press during any of them is a
# second publisher queuing on the gallery checkout lock behind the first. A
# batch removes the waiting and the second press together — the "busy" state is
# :attr:`App.batch`, on the server, so it outlives the page that pressed.
#
# Everything here is memory. The batch is a report on work in flight, not a
# record of it: the gallery's ``git log`` and the entries table are the record,
# and each step this runner takes is atomic on its own. So there is no table
# and no migration, and a web process that dies mid-batch loses the report and
# nothing else (§1.9).
# ---------------------------------------------------------------------------

#: The three mutually exclusive outcomes a card's ``do-<id>`` field may carry.
#: Critique is not one of them: it is its own field, because it stacks with
#: Publish or Archive and stands alone (§1.2).
BATCH_OUTCOMES = frozenset({"publish", "reject", "archive"})

#: The card's box, as long as packet 12's ``maxlength`` lets it be.
BATCH_TEXT_MAX = 400

#: The five phases, in the order a batch runs them (§1.6), with the labels the
#: tray prints. Order is about what is legal and not about what was marked
#: first: a line may not grow from a ``rejected`` parent and an archived entry
#: is off the lists, so critiques are queued while their parents are still
#: held; archives are a state flip with no git in them; and the rejections and
#: publications go to the gallery together so that eight of them are one push
#: and one index re-render instead of sixteen pushes (§1.7).
BATCH_PHASES: tuple[tuple[str, str], ...] = (
    ("critique", "Critiques"),
    ("archive", "Archives"),
    ("commit", "Render & commit"),
    ("push", "Push"),
    ("index", "Index"),
)

#: The two refusals that are about the batch rather than about an entry. The
#: first answers the press that starts a second batch, the second answers the
#: single-entry routes while one is running (§3.5).
BATCH_BUSY = "a batch is already running — nothing changed"
BATCH_GUARD = "a batch is running — nothing changed; it will finish first"

#: ``do-431``, ``cri-431``, ``text-431``: the form-field contract packet 12's
#: one page-wide form sends, and the only fields read here.
BATCH_FIELD_RE = re.compile(r"^(do|cri|text)-(.*)$")


@dataclass
class BatchItem:
    """One thing the batch will do to one entry.

    A card marked Publish *and* Critique is two of these, because they are two
    pieces of work with two outcomes to report and they happen in different
    phases.
    """

    entry_id: int
    verb: str                 # "critique" | "archive" | "reject" | "publish"
    text: str = ""
    state: str = "queued"     # queued | working | done | refused | failed
    message: str = ""


@dataclass
class Batch:
    """One press of Process, and everything the tray needs to describe it.

    ``step`` counts *finished* steps and ``now`` is the sentence for the step in
    flight, so the bar is counted in real work and never in elapsed time: there
    is no median here to measure a batch against and the bar does not pretend
    to one (§1.8).

    ``phase_of`` and ``phase_done`` are not in the plan's sketch of this class
    and are here because the counts have to survive the run: a push and an
    index are one step each and only exist when something was committed, and
    ``publish_many``'s ``on_step`` says which phase it has reached but not how
    far through the whole batch that is.
    """

    id: str                   # utc_now() at the start; the tray's identity
    items: list[BatchItem]    # already in run order (§1.6)
    state: str = "running"    # running | done
    phase: str = ""           # critique | archive | commit | push | index
    now: str = ""             # the one present-tense sentence
    step: int = 0
    steps: int = 0
    started_utc: str = ""
    ended_utc: str | None = None
    index_note: str | None = None
    phase_of: dict[str, int] = dataclass_field(default_factory=dict)
    phase_done: dict[str, int] = dataclass_field(default_factory=dict)


def _entry_states(
    conn: sqlite3.Connection, entry_ids: Iterable[int]
) -> dict[int, str]:
    """The current state of each of these entries; missing ids are absent."""
    ids = sorted(set(int(entry_id) for entry_id in entry_ids))
    if not ids:
        return {}
    marks = ", ".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT id, state FROM entries WHERE id IN ({marks})", tuple(ids)
    ).fetchall()
    return {int(row["id"]): str(row["state"]) for row in rows}


def batch_plan(
    conn: sqlite3.Connection, form: dict[str, list[str]]
) -> list[BatchItem]:
    """Read one press of Process into run order, or refuse the whole press.

    Every refusal here is raised before anything runs and means nothing
    changed, which is the promise the tally beside the button makes: what it
    says the press will do is what the press does, all of it or none of it
    (§1.12). The box is read at Process time by whichever verb reads it — the
    reason for Reject, where empty is allowed, and the revision sentence for
    Critique, where it is not (§1.3).

    An entry whose state moved between the page load and the press is a
    different thing and is **not** refused here: it is that one item's own
    refusal at run time, reported on that one item, because the other
    twenty-nine marks are still good. The exception is Reject, which is only
    legal from ``held`` and which a kept rejection is never offered.
    """
    outcome: dict[int, str] = {}
    critique: set[int] = set()
    text: dict[int, str] = {}
    for key, values in form.items():
        match = BATCH_FIELD_RE.match(key)
        if match is None:
            continue  # `back`, and anything else the form happens to carry
        kind, tail = match.group(1), match.group(2)
        if not tail.isdigit():
            raise Refused(f"{key}: that is not an entry id — nothing changed")
        entry_id = int(tail)
        value = (values or [""])[0].strip()
        if kind == "do":
            if value:  # a radio nobody pressed sends nothing at all
                outcome[entry_id] = value
        elif kind == "cri":
            if value:  # a checkbox sends "on" when it is ticked and nothing when not
                critique.add(entry_id)
        else:
            text[entry_id] = value[:BATCH_TEXT_MAX]
    marked = sorted(set(outcome) | critique)
    if not marked:
        raise Refused("nothing is marked — nothing changed")
    for entry_id, verb in sorted(outcome.items()):
        if verb not in BATCH_OUTCOMES:
            raise Refused(
                f"entry {entry_id}: {verb} is not publish, reject or archive "
                "— nothing changed"
            )
    states = _entry_states(conn, marked)
    for entry_id in marked:
        if entry_id not in states:
            raise Refused(f"there is no entry {entry_id} — nothing changed")
    for entry_id in sorted(critique):
        if not text.get(entry_id):
            raise Refused(
                f"entry {entry_id}: a critique needs a sentence — nothing changed"
            )
        if outcome.get(entry_id) == "reject":
            raise Refused(
                f"entry {entry_id}: Reject and Critique read the same box, and "
                "one sentence cannot be a reason and a revision at once "
                "— nothing changed"
            )
    for entry_id, verb in sorted(outcome.items()):
        if verb == "reject" and states[entry_id] != "held":
            raise Refused(
                f"entry {entry_id} is {states[entry_id]}, not held — a kept "
                "rejection is a rejection already — nothing changed"
            )
    items = [
        BatchItem(entry_id, "critique", text.get(entry_id, ""))
        for entry_id in sorted(critique)
    ]
    for verb in ("archive", "reject", "publish"):
        items.extend(
            BatchItem(entry_id, verb, text.get(entry_id, ""))
            for entry_id in sorted(
                key for key, value in outcome.items() if value == verb
            )
        )
    return items


def _publisher() -> Any | None:
    """``sketchgen.publish``, or ``None`` when packet 3.2 is not installed."""
    try:
        from sketchgen import publish  # type: ignore[attr-defined]
    except ImportError:
        return None
    return publish


def _publish_many() -> Callable[..., Any] | None:
    """:func:`publish.publish_many` if it is there, else ``None`` (§3.3).

    Looked up by name at run time, and never imported at the top, so this
    packet lands and works before packet 10 does: without it the runner loops
    the single-entry publish and reports each as its own commit step, which is
    today's cost wearing the batch's interface. A test patches the name in and
    out to exercise both paths.
    """
    module = _publisher()
    found = getattr(module, "publish_many", None) if module is not None else None
    return found if callable(found) else None


def _phase_totals(items: Iterable[BatchItem], *, many: bool) -> dict[str, int]:
    """How many steps each phase is going to take.

    One per critique, one per archive, one per entry to be committed, one for
    the push and one for the index (§1.8). Nothing to commit means no push and
    no index; no ``publish_many`` means the same, because the fallback carries
    a push and an index inside every one of its commit steps.
    """
    counts = {key: 0 for key, _label in BATCH_PHASES}
    for item in items:
        key = "commit" if item.verb in ("reject", "publish") else item.verb
        counts[key] = counts.get(key, 0) + 1
    together = 1 if (counts["commit"] and many) else 0
    counts["push"] = together
    counts["index"] = together
    return counts


def start_batch(app: App, items: list[BatchItem]) -> Batch:
    """Put one batch on ``app`` and start the thread that runs it.

    One batch at a time, per web process (§1.5): the lock is taken to look, a
    running batch is refused, a finished one is replaced, and the lock is
    released before the thread starts so that the first thing the runner does
    is not to wait on the request that made it. The thread is a daemon because
    ``update.sh`` restarting this process is allowed to abandon a batch where it
    stands — deploy with the tray empty.
    """
    batch = Batch(
        id=db.utc_now(),
        items=list(items),
        started_utc=db.utc_now(),
        phase_of=_phase_totals(items, many=_publish_many() is not None),
    )
    batch.steps = sum(batch.phase_of.values())
    with app.batch_lock:
        if app.batch is not None and app.batch.state == "running":
            raise Refused(BATCH_BUSY)
        app.batch = batch
    threading.Thread(
        target=_BatchRun(app, batch).run, name="held-batch", daemon=True
    ).start()
    return batch


class _BatchRun:
    """The runner: one thread, its own connection, every write under the lock.

    Its own connection because the request that started it has already closed
    its own by the time the thread gets going, and because a sqlite3 connection
    belongs to the thread that opened it. Its own *lock discipline* because
    ``/api/batch.json`` is read every second from every open tab while this is
    writing: readers take a snapshot under :attr:`App.batch_lock`, and every
    mutation of the batch here takes it too.
    """

    def __init__(self, app: App, batch: Batch) -> None:
        self.app = app
        self.batch = batch

    # -- the run -----------------------------------------------------------

    def run(self) -> None:
        """Never raises. An exception is the batch's report, not a traceback.

        Whatever goes wrong, the items that had not finished become ``failed``
        with the exception's own words and the batch still ends ``done`` with
        an ``ended_utc``, because a tray stuck on ``Processing…`` would lock the
        page for as long as the process lives (§1.5) and there would be nothing
        on the screen saying why.
        """
        conn = None
        try:
            conn = self.app.connect()
            self._critiques(conn)
            self._archives(conn)
            self._publications(conn)
        except Exception as exc:  # noqa: BLE001 - the thread never raises out
            self._blame(exc)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._finish()

    def _critiques(self, conn: sqlite3.Connection) -> None:
        """Queue every marked child while its parent is still ``held``.

        First, and for that reason: :func:`spawn_child` grows a line from a
        held, published or kept entry and refuses a rejected one, so a card
        marked Publish and Critique together has to spawn before the publish
        stamps it, and a card marked Archive and Critique before the archive
        takes it off the lists.
        """
        for item in self._of("critique"):
            self._step(
                "critique",
                f"Entry {item.entry_id} — queuing a child from the critique",
                item,
            )
            self._settle(item, spawn_child(conn, item.entry_id, {"text": [item.text]}))

    def _archives(self, conn: sqlite3.Connection) -> None:
        for item in self._of("archive"):
            self._step("archive", f"Entry {item.entry_id} — archiving", item)
            self._settle(item, archive_entry(conn, item.entry_id))

    def _publications(self, conn: sqlite3.Connection) -> None:
        """Every rejection's state flip, then one trip to the gallery.

        The flips happen first and all together: a rejection is a state flip
        plus a publish to the rejections catalog (see :func:`reject_entry`), and
        the publish half is the same render-scan-commit the publications get, so
        both verbs go to the gallery in the same call with the rejections
        leading. A flip that is refused drops that entry before anything is
        rendered, and the phase it was counted in shrinks by the step it will
        not take.
        """
        commits = self._of("reject") + self._of("publish")
        if not commits:
            return
        entry_ids: list[int] = []
        for item in commits:
            if item.verb == "reject":
                refusal = _reject_state(conn, item.entry_id, item.text)
                if refusal is not None:
                    self._close(item, "refused", refusal)
                    self._shrink("commit")
                    continue
            entry_ids.append(item.entry_id)
        if not entry_ids:
            self._shrink("push")
            self._shrink("index")
            return
        many = _publish_many()
        if many is None:
            self._one_at_a_time(conn, commits)
        else:
            self._all_at_once(conn, many, commits, entry_ids)

    def _one_at_a_time(
        self, conn: sqlite3.Connection, commits: list[BatchItem]
    ) -> None:
        """Without packet 10: today's publish, once per entry (§3.3).

        Each entry is its own commit step and carries its own push and index
        inside it, so there is no push step and no index step to show. The batch
        is exactly as slow as thirty single presses were; what it is not is
        thirty waits in front of a person, and the interface the tray reads is
        the same one ``publish_many`` will fill.
        """
        for item in commits:
            if item.state != "queued":
                continue  # a rejection whose flip was refused above
            self._step(
                "commit",
                f"Entry {item.entry_id} — rendering, scanning, "
                f"committing e/{item.entry_id}/",
                item,
            )
            message = publish_entry(self.app, conn, item.entry_id)
            if is_refusal(message):
                self._close(item, "refused", message)
            else:
                self._close(item, "done", self._won(item, message))

    def _all_at_once(
        self,
        conn: sqlite3.Connection,
        many: Callable[..., Any],
        commits: list[BatchItem],
        entry_ids: list[int],
    ) -> None:
        """One ``publish_many``: n commits, one push, one index (§1.7).

        The invariant that comes with it is ``publish.py``'s own: a row becomes
        ``published`` only after the push that carried its commit has
        succeeded. So a push that fails is not this batch's individual failures
        — it is every publication in it reported failed and still held, with
        the checkout back where the batch found it — while a per-entry refusal
        (the personal-data scan, the generator, bytes already in the gallery)
        drops that one entry and the batch goes on.
        """
        module = _publisher()
        refused_type = getattr(module, "PublishRefused", None) if module else None
        try:
            result = many(conn, entry_ids, on_step=self._publisher_step)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            # Nothing had happened yet when a checkout refusal was raised (not
            # a repo, dirty tree, wrong branch, no remote), so it is every
            # entry's refusal and not one entry's; a failure is a failure on
            # all of them for the same reason.
            kind = (
                "refused"
                if refused_type is not None and isinstance(exc, refused_type)
                else "failed"
            )
            note = f"refused: {exc}" if kind == "refused" else f"publish failed: {exc}"
            for item in commits:
                if item.state in ("queued", "working"):
                    self._close(item, kind, note)
            return
        published = {
            int(getattr(entry, "entry_id", -1))
            for entry in (getattr(result, "published", None) or [])
        }
        refused = dict(getattr(result, "refused", None) or {})
        failed = dict(getattr(result, "failed", None) or {})
        for item in commits:
            if item.state not in ("queued", "working"):
                continue
            entry_id = item.entry_id
            if entry_id in published:
                self._close(item, "done", self._won(item, ""))
            elif entry_id in refused:
                self._close(item, "refused", str(refused[entry_id]))
            elif entry_id in failed:
                self._close(item, "failed", str(failed[entry_id]))
            else:  # pragma: no cover - a publisher that forgot an entry
                self._close(
                    item, "failed", "the publisher did not report on this entry"
                )
        with self.app.batch_lock:
            self.batch.index_note = getattr(result, "index_note", None)

    def _publisher_step(
        self, phase: str, entry_id: int | None = None, sentence: str = ""
    ) -> None:
        """``publish_many``'s ``on_step``, turned into this batch's now line.

        The sentence it hands over is ignored on purpose: the three packets of
        this plan have to agree on the words, so the words are written here and
        the publisher only has to say which phase it has reached and whose
        entry it is.
        """
        if phase == "commit" and entry_id is not None:
            self._step(
                "commit",
                f"Entry {entry_id} — rendering, scanning, committing e/{entry_id}/",
                self._commit_item(int(entry_id)),
            )
        elif phase == "push":
            # Committed, and not published: the row moves after the push, so
            # until then these items are back in the queue rather than done.
            self._parked()
            commits = self.batch.phase_of.get("commit", 0)
            self._step("push", f"Pushing {commits} commits to the gallery")
        elif phase == "index":
            self._step("index", "Re-rendering the index and pushing it")

    # -- bookkeeping, all of it under the lock ------------------------------

    def _of(self, verb: str) -> list[BatchItem]:
        return [item for item in self.batch.items if item.verb == verb]

    def _commit_item(self, entry_id: int) -> BatchItem | None:
        """The publish-or-reject item for this entry; they are exclusive."""
        for item in self.batch.items:
            if item.entry_id == entry_id and item.verb in ("reject", "publish"):
                return item
        return None

    def _step(self, phase: str, sentence: str, item: BatchItem | None = None) -> None:
        """One step begins: move the phase, the now line and the counters.

        ``step`` is what has *finished*, so entering a phase finishes every
        phase before it, and the k-th step of a phase finishes that phase's
        k-1st. The phases run once each, in :data:`BATCH_PHASES` order, which
        is what makes that arithmetic safe to do from one side.
        """
        with self.app.batch_lock:
            batch = self.batch
            same = 1 if batch.phase == phase else 0
            already = batch.phase_done.get(phase, 0) + same
            for key, _label in BATCH_PHASES:
                if key == phase:
                    break
                batch.phase_done[key] = batch.phase_of.get(key, 0)
            batch.phase_done[phase] = already
            batch.phase = phase
            batch.now = sentence
            batch.step = sum(batch.phase_done.values())
            if item is not None:
                item.state = "working"

    def _shrink(self, phase: str, count: int = 1) -> None:
        """A step this batch is not going to take after all."""
        with self.app.batch_lock:
            batch = self.batch
            batch.phase_of[phase] = max(0, batch.phase_of.get(phase, 0) - count)
            batch.steps = sum(batch.phase_of.values())

    def _parked(self) -> None:
        with self.app.batch_lock:
            for item in self.batch.items:
                if item.verb in ("reject", "publish") and item.state == "working":
                    item.state = "queued"
                    item.message = "committed, waiting for the push"

    def _close(self, item: BatchItem, state: str, message: str) -> None:
        with self.app.batch_lock:
            item.state = state
            item.message = message

    def _settle(self, item: BatchItem, message: str) -> None:
        """Classify what a flash-message function just told us.

        The functions this runner calls were written for a page that prints one
        sentence, so their report is a sentence. :func:`is_refusal` is what
        already decides whether such a sentence means nothing happened, and it
        decides it here too rather than a second rule drifting away from the
        first.
        """
        self._close(item, "refused" if is_refusal(message) else "done", message)

    def _won(self, item: BatchItem, detail: str) -> str:
        """What a finished commit says on its card."""
        if item.verb == "reject":
            return (
                f"Rejected — {_reject_reason(item.text)}"
                f"{'. ' + detail if detail else ', and on the rejections page'}"
            )
        return detail or f"Published as e/{item.entry_id}/"

    def _blame(self, exc: BaseException) -> None:
        note = str(exc) or exc.__class__.__name__
        sys.stderr.write(f"{db.utc_now()} batch {self.batch.id} failed: {note}\n")
        with self.app.batch_lock:
            for item in self.batch.items:
                if item.state in ("queued", "working"):
                    item.state = "failed"
                    item.message = note

    def _finish(self) -> None:
        with self.app.batch_lock:
            batch = self.batch
            # The batch is over, so every step it was ever going to take is
            # accounted for and the bar is full: what is left to read is the
            # summary and the rows under it, not a bar stopped at four fifths.
            for key, _label in BATCH_PHASES:
                batch.phase_done[key] = batch.phase_of.get(key, 0)
            batch.steps = sum(batch.phase_of.values())
            batch.step = batch.steps
            batch.state = "done"
            batch.ended_utc = db.utc_now()
            batch.now = batch_summary(batch)


def batch_elapsed(batch: Batch) -> float:
    """Seconds from the press to now, or to the end if it has ended."""
    started = _parse_utc(batch.started_utc)
    ended = _parse_utc(batch.ended_utc) or datetime.now(timezone.utc)
    if started is None:
        return 0.0
    return max(0.0, (ended - started).total_seconds())


def batch_summary(batch: Batch) -> str:
    """``6 done · 1 refused in 48s`` — what stays in the tray afterwards.

    ``failed`` is counted with its own word when there is one, because a
    rejected personal-data scan and a push that broke are not the same news and
    the operator's next press depends on which it was (§1.10).
    """
    counts = {state: 0 for state in ("done", "refused", "failed")}
    for item in batch.items:
        if item.state in counts:
            counts[item.state] += 1
    parts = [f"{counts['done']} done"]
    parts.extend(
        f"{counts[state]} {state}" for state in ("refused", "failed") if counts[state]
    )
    return f"{' · '.join(parts)} in {human_seconds(batch_elapsed(batch))}"


def batch_document(app: App) -> dict[str, Any] | None:
    """The batch as the tray and the poller read it, or ``None``.

    One snapshot, taken under the lock, so a document can never show a step
    from one moment and an item from the next. ``index_note`` is deliberately
    not in here: a finished batch is re-rendered from ``app.batch`` server-side,
    which is where that note belongs, and the poller stops at ``done``.
    """
    with app.batch_lock:
        batch = app.batch
        if batch is None:
            return None
        steps = batch.steps or 0
        return {
            "id": batch.id,
            "state": batch.state,
            "phase": batch.phase,
            "now": batch.now,
            "step": batch.step,
            "steps": steps,
            "bar_pct": round(100.0 * batch.step / steps, 1) if steps else 0.0,
            "elapsed_s": int(batch_elapsed(batch)),
            "phases": [
                {
                    "key": key,
                    "label": label,
                    "done": batch.phase_done.get(key, 0),
                    "of": batch.phase_of.get(key, 0),
                }
                for key, label in BATCH_PHASES
                if batch.phase_of.get(key, 0)
            ],
            "items": [
                {
                    "entry_id": item.entry_id,
                    "verb": item.verb,
                    "state": item.state,
                    "message": item.message,
                }
                for item in batch.items
            ],
            "summary": batch.now if batch.state == "done" else None,
        }


def batch_running(app: App) -> bool:
    """Whether a batch is in flight right now (§1.5, §3.5)."""
    with app.batch_lock:
        return app.batch is not None and app.batch.state == "running"


def dismiss_batch(app: App) -> str | None:
    """Clear a finished batch from the tray. A running one is refused."""
    with app.batch_lock:
        batch = app.batch
        if batch is not None and batch.state == "running":
            return BATCH_GUARD
        app.batch = None
    return None


# ---------------------------------------------------------------------------
# The tray, and the page's own script (packet 12)
#
# The tray is the sticky header's second row on /held: the title and counts, a
# tally of what is marked, Clear marks, and Process. It is also the <form> every
# toggle and box on the page belongs to, so the page has exactly one request in
# it and nothing else on it can start one.
#
# Everything below renders from a snapshot of `app.batch`, server-side, in three
# states: no batch, one running, one finished and not yet dismissed. That is
# what makes the page work with no JavaScript at all — the POST starts the batch
# and redirects here, and the tray says where it has got to, refreshing itself
# while it runs (§1.11). HELD_SCRIPT makes the same three states live.
# ---------------------------------------------------------------------------


def _batch_snapshot(app: App) -> Batch | None:
    """A copy of the batch, taken once under the lock, for the page to read.

    The runner mutates the batch from its own thread, and rendering a page is a
    hundred reads: a copy means the tray, the bar and every card on the page are
    all describing the same instant, which is the same reason
    :func:`batch_document` takes its snapshot under the lock.
    """
    with app.batch_lock:
        if app.batch is None:
            return None
        return replace(app.batch, items=[replace(item) for item in app.batch.items])


#: The phases whose steps belong to entries, and which therefore count them.
#: The push and the index are one step each and say only their own name.
_COUNTED_PHASES = ("critique", "archive", "commit")


def _tray_phases(batch: Batch) -> str:
    """The five phases with their counts; a phase with nothing in it is absent."""
    rows = []
    for key, label in BATCH_PHASES:
        of = batch.phase_of.get(key, 0)
        if not of:
            continue
        done = batch.phase_done.get(key, 0)
        css = "fin" if done >= of else ("on" if batch.phase == key else "")
        text = f"{label} {done}/{of}" if key in _COUNTED_PHASES else label
        mark = f' class="{css}"' if css else ""
        rows.append(f"<li{mark}>{esc(text)}</li>")
    return "".join(rows)


def _tray_results(batch: Batch) -> str:
    """What the batch did, refusals and failures first (§1.10).

    The bad news is at the top because it is the news the operator has to do
    something about: those cards are still on the page, still marked, with the
    reason on them, so fixing one and pressing again is the whole of the retry.
    """
    rank = {"refused": 0, "failed": 0, "done": 1}
    rows = sorted(
        (item for item in batch.items if item.state in rank),
        key=lambda item: (rank[item.state], batch.items.index(item)),
    )
    out = []
    for item in rows:
        css = "bad" if item.state in ("refused", "failed") else "dim"
        out.append(
            f'<li><span class="n">{item.entry_id}</span>'
            f'<span class="{css}">{esc(item.message or item.state)}</span></li>'
        )
    if batch.index_note:
        # The entries are already public; a failure re-rendering the index
        # after them is a note and never an exception (§2.1 step 6).
        out.append(
            '<li><span class="n">index</span>'
            f'<span class="bad">{esc(batch.index_note)}</span></li>'
        )
    out.append(
        '<li><button type="submit" form="held-dismiss">Dismiss</button></li>'
    )
    return "".join(out)


def batch_tray(batch: Batch | None, *, waiting: int, kept: int) -> str:
    """The header's second row on /held, in whichever of its three states.

    Nothing here is a judgement the server makes twice: the tally and the
    ``Process N`` label are the script's, because they change on every press and
    the press is not a request; the progress, the phases and the results are the
    server's, because they are the batch's own state and a page with no script
    has to be able to read them.
    """
    running = batch is not None and batch.state == "running"
    done = batch is not None and batch.state == "done"
    state = batch.state if batch is not None else ""
    bar_done = " done" if done else ""
    process = (
        '<button type="submit" class="process" id="process" disabled '
        'title="a batch is running; it finishes before another can start">'
        "Processing…</button>"
        if running
        else '<button type="submit" class="process" id="process">Process</button>'
    )
    clear = (
        '<button type="reset" id="clear"'
        + (" disabled" if running else "")
        + ">Clear marks</button>"
    )
    phases = results = ""
    now_text = step_text = ""
    pct = 0.0
    if batch is not None:
        now_text = batch_summary(batch) if done else batch.now
        pct = round(100.0 * batch.step / batch.steps, 1) if batch.steps else 0.0
        step_text = (
            f"{batch.step} of {batch.steps} steps · "
            f"{human_seconds(batch_elapsed(batch))}"
        )
        phases = _tray_phases(batch)
    # The skeleton is always rendered, empty when nothing runs: the script only
    # fills these nodes, it never builds them, and the press that starts a
    # batch happens on a page that has no batch yet.
    progress = (
        f'<span class="now" id="now" aria-live="polite">{esc(now_text)}</span>'
        f'<div class="bar{bar_done}" id="bar">'
        f'<span id="bar-fill" style="width:{pct}%"></span></div>'
        f'<span class="prog-t" id="prog-t">{esc(step_text)}</span>'
    )
    if done:
        results = _tray_results(batch)
    # A scriptless page follows the batch by reloading itself; a scripted one
    # polls /api/batch.json instead and is never reloaded under the operator.
    refresh = (
        '<noscript><meta http-equiv="refresh" content="2"></noscript>'
        if running
        else ""
    )
    # The entries a finished batch got through: the script drops their saved
    # marks, because those cards have left the page and the marks are spent.
    finished = " ".join(
        str(item.entry_id)
        for item in (batch.items if done else [])
        if item.state == "done"
    )
    hide_prog = "" if batch is not None else " hidden"
    hide_phases = "" if phases else " hidden"
    hide_results = "" if results else " hidden"
    return (
        f'<section class="tray" aria-label="Batch" data-state="{esc(state)}"'
        f' data-done="{esc(finished)}">'
        f"{refresh}"
        '<form id="held-dismiss" method="post" action="/held/batch/dismiss"></form>'
        '<form id="held-batch" method="post" action="/held/batch">'
        '<div class="tray-in">'
        '<div class="tray-row">'
        f'<h1>Held <span class="dim">— {waiting} waiting · {kept} kept</span></h1>'
        '<div class="tally" id="tally" aria-live="polite"></div>'
        f'<div class="tray-btns">{clear}{process}</div>'
        "</div>"
        f'<div class="tray-row" id="prog-row"{hide_prog}>{progress}</div>'
        f'<ol class="phases" id="phases"{hide_phases}>{phases}</ol>'
        f'<ul class="results" id="results"{hide_results}>{results}</ul>'
        "</div></form></section>"
    )


HELD_SCRIPT = """<script>
// The Held page, live. Everything here is something the server has already
// rendered once — the marks are real form controls, Process is the form's submit
// button, and a POST with no script starts the batch and redirects to a tray
// that refreshes itself. This adds the four things a page cannot do on its own:
// press-again-to-clear on the radios, the tally beside the button, the batch
// painted as it runs instead of on a two-second reload, and marks that survive
// one.
//
// House rules, the same as the layout's own script: ES5-plain, textContent and
// never innerHTML (a refusal quotes a prompt a model wrote), no library.
(function () {
  var form = document.getElementById("held-batch");
  var tray = document.querySelector(".tray");
  if (!form || !tray) { return; }

  var KEY = "held-marks";
  var POLL_MS = 1000;
  var BUSY = "a batch is running; it finishes before another can start";
  var IDLE = "Mark one outcome, and Critique if it should have a child. " +
             "Nothing happens until Process.";
  var CHIPS = [["publish", "pub", "publish"], ["reject", "rej", "reject"],
               ["cri", "cri", "critique"], ["archive", "arc", "archive"]];
  var COUNTED = {critique: 1, archive: 1, commit: 1};
  var running = tray.getAttribute("data-state") === "running";
  var timer = null;
  var leaving = false;

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function trim(text) { return String(text || "").replace(/^\\s+|\\s+$/g, ""); }

  function rows() { return document.querySelectorAll(".acts"); }
  function idOf(row) { return row.getAttribute("data-entry"); }
  function box(id) { return $("say-" + id); }
  function radio(row, value) {
    return row.querySelector('input[value="' + value + '"]');
  }
  function critique(row) { return row.querySelector('input[type="checkbox"]'); }

  // The card this row of toggles belongs to. Walked rather than selected
  // because the toggles are the only thing on the page that knows its entry id.
  function card(row) {
    var walk = row.parentNode;
    while (walk && String(walk.className || "").indexOf("card") === -1) {
      walk = walk.parentNode;
    }
    return walk;
  }

  function mark(cls, name, on) {
    var out = String(cls || "").replace(new RegExp(" ?" + name, "g"), "");
    return on ? out + " " + name : out;
  }

  function marksOf(row) {
    var id = idOf(row);
    var pub = radio(row, "publish");
    var rej = radio(row, "reject");
    var arc = radio(row, "archive");
    var cri = critique(row);
    var text = box(id);
    return {
      id: id,
      row: row,
      out: pub && pub.checked ? "publish"
         : rej && rej.checked ? "reject"
         : arc && arc.checked ? "archive" : "",
      cri: !!(cri && cri.checked),
      text: text ? text.value : "",
      kept: row.getAttribute("data-kept") === "1"
    };
  }

  // What Process will do to this one card, in one sentence. The wording is the
  // mockup's: an operator reading a card should not have to hold the whole
  // batch in their head to know what they have just asked for.
  function hintFor(m) {
    var host = card(m.row);
    var hint = host ? host.querySelector(".decide-hint") : null;
    if (!hint) { return; }
    // A card a batch refused keeps the reason it came back with until the
    // operator touches it: why the last press failed is what a retry needs.
    if (hint.getAttribute("data-reason")) { return; }
    if (m.cri && !trim(m.text)) {
      hint.className = "hint decide-hint warn";
      hint.textContent =
        "Critique needs a sentence in the box before Process will run.";
      return;
    }
    var parts = [];
    if (m.cri) { parts.push("queue a child from the box"); }
    if (m.out === "publish") {
      parts.push(m.kept ? "publish this to the rejections page"
                        : "publish this to the grid");
    }
    if (m.out === "reject") {
      parts.push(trim(m.text) ? "reject this with the box as the reason"
                              : "reject this as \\u201crejected by operator\\u201d");
    }
    if (m.out === "archive") {
      parts.push("take it off this page; nothing is deleted");
    }
    if (m.cri && !m.out) { parts.push("leave it held"); }
    hint.className = "hint decide-hint";
    hint.textContent = parts.length
      ? "Process will " + parts.join(", then ") + "." : IDLE;
  }

  function tally() {
    var counts = {publish: 0, reject: 0, cri: 0, archive: 0};
    var all = rows(), marked = 0, need = 0, i, m, host, text;
    for (i = 0; i < all.length; i++) {
      m = marksOf(all[i]);
      host = card(m.row);
      if (host) { host.className = mark(host.className, "marked", m.out || m.cri); }
      text = box(m.id);
      if (text) { text.className = m.cri && !trim(m.text) ? "need" : ""; }
      hintFor(m);
      if (!m.out && !m.cri) { continue; }
      marked += 1;
      if (m.out) { counts[m.out] += 1; }
      if (m.cri) {
        counts.cri += 1;
        if (!trim(m.text)) { need += 1; }
      }
    }
    var chips = $("tally");
    if (chips) {
      chips.textContent = "";
      for (i = 0; i < CHIPS.length; i++) {
        if (counts[CHIPS[i][0]]) {
          chips.appendChild(el("span", "t " + CHIPS[i][1],
                               counts[CHIPS[i][0]] + " " + CHIPS[i][2]));
        }
      }
      // A marked Critique with an empty box is the one mark this page will not
      // let through: one sentence cannot be a reason and a revision at once.
      if (need) {
        chips.appendChild(el("span", "t need", need + " needs a sentence"));
      }
      if (!marked && !running) {
        chips.appendChild(
          el("span", "none", "nothing marked — press a verb on any card"));
      }
    }
    var go = $("process");
    if (go) {
      go.disabled = running || !marked || need > 0;
      go.textContent = running ? "Processing…"
                     : marked ? "Process " + marked : "Process";
      go.title = running ? BUSY
               : need ? "a marked Critique has an empty box" : "";
    }
    var clear = $("clear");
    if (clear) { clear.disabled = running || !marked; }
    return {marked: marked, need: need};
  }

  // Reject and Critique read the same box, so marking either clears the other.
  function exclusive(row, input) {
    var rej = radio(row, "reject");
    var cri = critique(row);
    if (!rej || !cri) { return; }
    if (input === cri && cri.checked && rej.checked) { rej.checked = false; }
    if (input === rej && rej.checked && cri.checked) { cri.checked = false; }
  }

  function wireInput(row, input) {
    var was = false;
    function remember() { was = input.checked === true; }
    input.addEventListener("pointerdown", remember);
    input.addEventListener("keydown", remember);
    input.addEventListener("click", function () {
      // A radio has no off of its own. An outcome pressed by mistake has to be
      // undoable where it was pressed, so the second press on the filled one
      // clears it — which is what the fill looks like it should do.
      if (input.type === "radio" && was) { input.checked = false; }
      was = input.checked === true;
      settle(row, input);
    });
    input.addEventListener("change", function () { settle(row, input); });
  }

  // The card has been touched, so the reason it was carrying is history and
  // the hint goes back to saying what the next press will do.
  function forget(row) {
    var host = card(row);
    var hint = host ? host.querySelector(".decide-hint") : null;
    if (hint) { hint.removeAttribute("data-reason"); }
  }

  function settle(row, input) {
    exclusive(row, input);
    forget(row);
    tally();
    save();
  }

  function typing(row) {
    return function () {
      forget(row);
      tally();
      save();
    };
  }

  function wire() {
    var all = rows(), i, j, inputs, text;
    for (i = 0; i < all.length; i++) {
      inputs = all[i].querySelectorAll("input");
      for (j = 0; j < inputs.length; j++) { wireInput(all[i], inputs[j]); }
      text = box(idOf(all[i]));
      if (text) {
        text.addEventListener("input", typing(all[i]));
      }
    }
    var clear = $("clear");
    if (clear) {
      // type=reset puts the controls back where the server drew them, which is
      // not the same as clearing them on a page a finished batch pre-marked.
      clear.addEventListener("click", function (event) {
        if (event && event.preventDefault) { event.preventDefault(); }
        var each = rows(), k, kk, ins, field;
        for (k = 0; k < each.length; k++) {
          ins = each[k].querySelectorAll("input");
          for (kk = 0; kk < ins.length; kk++) { ins[kk].checked = false; }
          field = box(idOf(each[k]));
          if (field) { field.value = ""; }
          forget(each[k]);
        }
        tally();
        save();
      });
    }
  }

  // Marks live in sessionStorage, so a reload — or the reload a finished batch
  // does — does not lose forty decisions. Wrapped because a browser with
  // storage refused is a browser this page still has to work in.
  function save() {
    var out = {}, all = rows(), i, m;
    for (i = 0; i < all.length; i++) {
      m = marksOf(all[i]);
      if (m.out || m.cri || trim(m.text)) {
        out[m.id] = {out: m.out, cri: m.cri, text: m.text};
      }
    }
    try { sessionStorage.setItem(KEY, JSON.stringify(out)); } catch (e) {}
  }

  function restore() {
    var saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) {}
    if (!saved) { return; }
    // What a finished batch got through is spent: those cards have left the
    // page and their marks must not come back on the next one.
    var spent = String(tray.getAttribute("data-done") || "").split(" "), i;
    for (i = 0; i < spent.length; i++) {
      if (spent[i]) { delete saved[spent[i]]; }
    }
    var all = rows(), row, id, want, hit, cri, text;
    for (i = 0; i < all.length; i++) {
      row = all[i];
      id = idOf(row);
      want = saved[id];
      if (!want) { continue; }
      hit = want.out ? radio(row, want.out) : null;
      if (hit) { hit.checked = true; }
      cri = critique(row);
      if (cri && want.cri) { cri.checked = true; }
      text = box(id);
      if (text && want.text && !text.value) { text.value = want.text; }
    }
    save();
  }

  function lock(on) {
    var all = rows(), i, j, inputs, text;
    for (i = 0; i < all.length; i++) {
      inputs = all[i].querySelectorAll("input");
      for (j = 0; j < inputs.length; j++) { inputs[j].disabled = on; }
      text = box(idOf(all[i]));
      if (text) { text.disabled = on; }
    }
  }

  // web.py:human_seconds, for the one duration this tray prints.
  function dur(seconds) {
    var s = Math.max(0, Math.round(Number(seconds) || 0));
    if (s < 60) { return s + "s"; }
    if (s < 3600) {
      return (s / 60 | 0) + "m " + String(s % 60).padStart(2, "0") + "s";
    }
    if (s < 86400) {
      return (s / 3600 | 0) + "h " +
             String((s % 3600) / 60 | 0).padStart(2, "0") + "m";
    }
    return (s / 86400 | 0) + "d " + ((s % 86400) / 3600 | 0) + "h";
  }

  function phases(doc) {
    var list = $("phases");
    if (!list) { return; }
    var rowsIn = doc.phases || [], i, row, css;
    list.textContent = "";
    list.hidden = !rowsIn.length;
    for (i = 0; i < rowsIn.length; i++) {
      row = rowsIn[i];
      css = row.done >= row.of ? "fin" : doc.phase === row.key ? "on" : "";
      list.appendChild(el("li", css, COUNTED[row.key]
        ? row.label + " " + row.done + "/" + row.of : row.label));
    }
  }

  var PILLS = {queued: "quiet", working: "warn", done: "ok",
               refused: "bad", failed: "bad"};
  var WORST = ["failed", "refused", "working", "queued", "done"];

  function pills(doc) {
    var items = doc.items || [], seen = {}, i, item, host, pill, hint;
    for (i = 0; i < items.length; i++) {
      item = items[i];
      // A card marked Publish and Critique is two items; it wears the worse of
      // them, because what needs doing something about is what it should say.
      if (seen[item.entry_id] !== undefined &&
          WORST.indexOf(items[seen[item.entry_id]].state) <=
          WORST.indexOf(item.state)) { continue; }
      seen[item.entry_id] = i;
    }
    for (var id in seen) {
      if (!Object.prototype.hasOwnProperty.call(seen, id)) { continue; }
      item = items[seen[id]];
      host = $("entry-" + item.entry_id);
      if (!host) { continue; }
      pill = host.querySelector(".pill.st");
      if (!pill) {
        pill = el("span", "", "");
        host.insertBefore(pill, host.childNodes[0]);
      }
      pill.className = "pill st " + (PILLS[item.state] || "quiet");
      pill.textContent = item.state;
      host.className = mark(mark(mark(host.className, "has-state", true),
                                 "is-working", item.state === "working"),
                            "is-done", item.state === "done");
      hint = host.querySelector(".decide-hint");
      if (hint && item.message) {
        var bad = item.state === "refused" || item.state === "failed";
        hint.className = bad ? "hint decide-hint bad" : "hint decide-hint";
        hint.textContent = bad
          ? item.message + " — still held, still marked." : item.message;
        if (bad) { hint.setAttribute("data-reason", "1"); }
      }
    }
  }

  function paint(doc) {
    if (!doc) { return; }
    var prog = $("prog-row"), now = $("now"), bar = $("bar");
    var fill = $("bar-fill"), text = $("prog-t");
    if (prog) { prog.hidden = false; }
    if (now) { now.textContent = doc.now || ""; }
    if (bar) { bar.className = doc.state === "done" ? "bar done" : "bar"; }
    if (fill && fill.style) {
      fill.style.width = (Number(doc.bar_pct) || 0) + "%";
    }
    if (text) {
      text.textContent = doc.step + " of " + doc.steps + " steps · " +
                         dur(doc.elapsed_s);
    }
    phases(doc);
    pills(doc);
  }

  function refuse(message) {
    running = false;
    lock(false);
    var list = $("results");
    if (list) {
      list.textContent = "";
      list.hidden = false;
      var li = el("li");
      li.appendChild(el("span", "n", ""));
      li.appendChild(el("span", "bad", message || "nothing changed"));
      list.appendChild(li);
    }
    tally();
  }

  function poll() {
    if (timer) { return; }
    timer = setInterval(function () {
      window.fetch("/api/batch.json", {cache: "no-store"}).then(function (r) {
        return r.json();
      }).then(function (answer) {
        var doc = answer && answer.batch;
        if (!doc) { return; }
        paint(doc);
        // Done: the server renders the result and the cards that survived it,
        // so the page asks for itself once rather than trying to be the report.
        if (doc.state === "done" && !leaving) {
          leaving = true;
          clearInterval(timer);
          window.location.href = "/held";
        }
      }).catch(function () {});
    }, POLL_MS);
  }

  function body() {
    var pairs = [], all = rows(), i, m;
    for (i = 0; i < all.length; i++) {
      m = marksOf(all[i]);
      if (m.out) { pairs.push("do-" + m.id + "=" + encodeURIComponent(m.out)); }
      if (m.cri) { pairs.push("cri-" + m.id + "=on"); }
      if (m.text) {
        pairs.push("text-" + m.id + "=" + encodeURIComponent(m.text));
      }
    }
    return pairs.join("&");
  }

  form.addEventListener("submit", function (event) {
    if (!window.fetch) { return; }   // no fetch: the browser posts it instead
    event.preventDefault();
    var sent = body();
    running = true;
    lock(true);
    tally();
    window.fetch(form.action, {
      method: "POST",
      headers: {"Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"},
      body: sent
    }).then(function (response) {
      return response.json().then(function (doc) {
        return {status: response.status, doc: doc};
      });
    }).then(function (answer) {
      if (answer.status === 202 && answer.doc && answer.doc.batch) {
        paint(answer.doc.batch);
        poll();
      } else {
        refuse(answer.doc && answer.doc.error);
      }
    }).catch(function () {
      refuse("the press did not reach the server — nothing changed");
    });
  });

  restore();
  wire();
  tally();
  // A reload, a second tab, or a batch somebody else started: the tray the
  // server drew says whether one is running, and the poll starts from there.
  if (running) { poll(); }
})();
</script>"""


# ---------------------------------------------------------------------------
# The New job page's script: everything on it already works without this
# ---------------------------------------------------------------------------

#: Five conveniences, each one a few lines, none of them load-bearing: the
#: picker's rows are links and the recent prompts are links, so with no script
#: the same click lands on the same page by a round trip — and loses the
#: prompt typed so far, which is what the script is for.
NEW_JOB_SCRIPT = r"""<script>
(function () {
  var form = document.getElementById("newjob");
  if (!form) { return; }
  var prompt = document.getElementById("prompt");
  var parentBox = document.getElementById("parent_entry_id");
  var queue = document.getElementById("queue-it");
  var many = document.getElementById("many");

  // Ctrl+Enter (Cmd+Enter) queues, from anywhere on the form.
  form.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      queue.click();
    }
  });

  // "One job per line" tells the button how many that is.
  function relabel() {
    if (!many || !many.checked) { queue.textContent = "Queue it"; return; }
    var n = prompt.value.split("\n").filter(function (l) { return l.trim(); }).length;
    queue.textContent = n > 1 ? "Queue " + n + " jobs" : "Queue it";
  }
  if (many) { many.addEventListener("change", relabel); prompt.addEventListener("input", relabel); relabel(); }

  // The submitter pill, and the box under it for a job queued on somebody's behalf.
  var other = document.getElementById("other-submitter");
  var otherBox = document.getElementById("other-box");
  if (other && otherBox) {
    other.addEventListener("click", function () {
      other.hidden = true; otherBox.hidden = false;
      var input = otherBox.querySelector("input"); if (input) { input.focus(); }
    });
  }

  // Picking a parent fills the id, the card and — as spawn() would — planner and rules.
  var card = document.getElementById("parent-card");
  // The source tick box only means something with a parent, so it is hidden
  // until there is one. It is unhidden rather than built: it carries the
  // hidden marker create_job reads, and a box rebuilt by this script would be
  // a box whose clear state the server could not tell from no box at all.
  function showSourceTick(on) {
    ["source-tick", "source-tick-help"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.hidden = !on; }
    });
  }
  if (parentBox) {
    parentBox.addEventListener("input", function () {
      showSourceTick(!!parentBox.value.trim());
    });
  }
  function pickParent(row) {
    parentBox.value = row.dataset.parent;
    showSourceTick(true);
    document.querySelectorAll(".pick.on").forEach(function (el) { el.classList.remove("on"); });
    row.classList.add("on");
    var rules = form.querySelector("[name=rules]");
    // Only if the menu still has it: the parent may have been made with a model
    // since removed from the node, and setting an absent value on a <select>
    // selects nothing at all — a blank box where a model should be.
    function preset(name, want) {
      var box = form.querySelector("[name=" + name + "]");
      if (!box || !want) { return; }
      if (box.querySelector("option[value=\"" + want + "\"]")) { box.value = want; }
    }
    preset("planner", row.dataset.planner);
    preset("executor", row.dataset.executor);
    if (rules && row.dataset.rules) { rules.value = row.dataset.rules; }
    if (card) {
      var img = row.querySelector("img");
      card.className = "parent-card"; card.dataset.prompt = row.dataset.prompt;
      card.innerHTML = (img ? "<img src=\"" + img.getAttribute("src") + "\" alt=\"frame strip\">" : "") +
        "<div><p class=\"meta\"><a href=\"/entry/" + row.dataset.parent + "\">entry " + row.dataset.parent +
        "</a> <span class=\"pill " + row.dataset.stateClass + "\">" + row.dataset.state + "</span>" +
        " <span class=\"dim\">" + row.dataset.planner + " · " + (row.dataset.executor || "—") +
        " · " + (row.dataset.rules || "—") + "</span></p>" +
        "<p class=\"prompt\"></p><p class=\"help\"><button type=\"button\" class=\"link\" id=\"use-parent-prompt\">use its prompt</button>" +
        " as the starting point, or write a fresh one.</p></div>";
      card.querySelector(".prompt").textContent = row.dataset.prompt;
      wireUsePrompt();
    }
  }
  document.querySelectorAll(".pick").forEach(function (row) {
    row.addEventListener("click", function (event) { event.preventDefault(); pickParent(row); });
  });
  function wireUsePrompt() {
    var use = document.getElementById("use-parent-prompt");
    if (!use) { return; }
    use.addEventListener("click", function () {
      var current = document.getElementById("parent-card");
      if (current && current.dataset.prompt) { prompt.value = current.dataset.prompt; prompt.focus(); relabel(); }
    });
  }
  wireUsePrompt();

  // A recent prompt goes into the box rather than around through the server.
  document.querySelectorAll(".recent a[data-prompt]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      event.preventDefault(); prompt.value = link.dataset.prompt; prompt.focus(); relabel();
    });
  });
})();
</script>"""


# ---------------------------------------------------------------------------
# Spawning a child from a critique (packet 5.3)
# ---------------------------------------------------------------------------


#: A ``gh auth status`` that failed is asked again after this long, so a node
#: that signs in after the service started is noticed without a restart. A
#: login that was found is kept for the life of the process.
GH_RETRY_S = 600.0
_gh_cache: dict[str, Any] = {}
_gh_lock = threading.Lock()
_GH_LOGIN_RE = re.compile(r"Logged in to github\.com (?:account|as) ([A-Za-z0-9-]+)")


#: Where ``gh`` might be, in the order worth trying, for when it is not on
#: PATH. The web process is a systemd ``--user`` unit, and that manager's PATH
#: is a minimal one that does not include ``~/.local/bin`` -- which is where gh
#: goes when it is installed from the release tarball, as it must be on a node
#: with no root. The symptom is a gh that answers perfectly in the operator's
#: shell and is invisible to the service, so the New job page offers a typed
#: box and says nobody is signed in.
GH_PATHS = (
    "~/.local/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
    "/snap/bin/gh",
    "/home/linuxbrew/.linuxbrew/bin/gh",
)


def _gh_binary() -> str | None:
    """The gh to run: PATH first, then the usual places. None when there is none."""
    found = shutil.which("gh")
    if found:
        return found
    for candidate in GH_PATHS:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None


def _ask_gh() -> str | None:
    """One ``gh auth status``, parsed. None for no gh, no login, or no answer."""
    binary = _gh_binary()
    if binary is None:
        return None
    try:
        done = subprocess.run(
            [binary, "auth", "status", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = _GH_LOGIN_RE.search(done.stdout + done.stderr)
    if match is None or not USERNAME_RE.match(match.group(1)):
        return None
    return match.group(1)


def gh_login() -> str | None:
    """Whatever ``gh`` on this node is signed in as, or None; cached.

    The web process runs as the operator's own user (a ``--user`` unit), so
    ``gh``'s keyring or hosts file is the operator's, and the login it reports
    is the operator's GitHub username — which is the one thing course policy
    allows on a job. Asked once, in a subprocess with a timeout, never on a
    hot path: the answer is remembered.
    """
    now = time.monotonic()
    with _gh_lock:
        asked = _gh_cache.get("asked")
        if asked is not None and (_gh_cache.get("login") or now - asked < GH_RETRY_S):
            return _gh_cache.get("login")
    login = _ask_gh()
    with _gh_lock:
        _gh_cache.update(login=login, asked=now)
    return login


def operator_login() -> tuple[str | None, str]:
    """The operator's GitHub login and where it came from.

    ``$SKETCHGEN_OPERATOR`` first, when it is a GitHub username — the explicit
    setting wins over the ambient one — then :func:`gh_login`. ``(None, "")``
    when neither knows.
    """
    name = (os.environ.get("SKETCHGEN_OPERATOR") or "").strip()
    if name and USERNAME_RE.match(name):
        return name, "$SKETCHGEN_OPERATOR"
    login = gh_login()
    if login:
        return login, "gh on this node"
    return None, ""


def github_login() -> str | None:
    """The operator's GitHub login, or None. See :func:`operator_login`."""
    return operator_login()[0]


def operator_username(row: sqlite3.Row | None = None) -> str:
    """Who the UI signs a critique as, by default.

    The operator's GitHub login when this node knows it
    (:func:`operator_login`), the entry's own submitter when it does not, and
    the literal ``operator`` when there is neither. The field is editable on
    the form, so this is a default and never a claim: ``critique_by`` is a
    model id when a model wrote the sentence.
    """
    name = github_login()
    if name:
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
    """Hand one critique to lineage.spawn(). Returns the flash message.

    ``critique_by`` is optional, and the decision card does not offer it: a
    sentence typed into that card's box was typed by the person reading it, so
    the default here — :func:`operator_username` — is already the true answer
    and a field asking for it again is a field to get wrong. The job page's
    form still sends one, and the CLI still writes a model id there when a
    model wrote the critique.
    """
    critique = said(form, "critique")
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
# Submissions — the public's queue, and the person in front of it (packet 8)
#
# A submission is not a job (plan §1.2). It is a sentence a signed-in visitor
# typed on the gallery, pulled down by sync.py into its own table, and it
# becomes a job here and nowhere else — when the operator presses Release. The
# job state machine is untouched by all of this on purpose: an unreleased
# submission is not in `jobs`, so there is no state the worker could claim it
# from however this page is used or misused.
# ---------------------------------------------------------------------------

#: How many pending submissions the page draws at once. The same order the
#: table's index is in, and a cap for the same reason /queue has one: a page
#: that renders two hundred strips is not a page anybody reviews.
SUBMISSIONS_PAGE_LIMIT = 50


#: What one of the two verbs below did, for a caller that has to act on it
#: rather than print it. The page shows the sentence and nothing else; the CLI
#: turns this into an exit code, and a word is what it should be reading —
#: sniffing the sentence for "refused" would make the wording load-bearing.
RELEASED, DECLINED, REFUSED = "released", "declined", "refused"


def human_prompt_version(username: str) -> str:
    """What a released human critique is recorded under (decision §1.4).

    ``human:<login>`` in ``critiques.prompt_version``, which is the whole of the
    collision fix: migration 006's UNIQUE ``(entry_id, prompt_version)`` then
    admits one critique per *person* per entry, while the critic model's rows
    keep the one-per-version rule that is right for a model. Nothing else reads
    this string as a version — ``db.entries_to_critique`` matches the model's
    current one and so never sees a human row, and ``gallery._critic_chip``
    draws a person from ``critique_by``.
    """
    return f"human:{username}"


def _submission_parent(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """The entry a critique is about: its strip and its prompt, above the sentence.

    A critique is a sentence about a picture, and the operator is being asked
    whether to spend a run on it. Reading it without the parent in front of you
    is reading half of it, so the parent's strip and its prompt sit above the
    ask on the card. A prompt has no parent and gets nothing here.
    """
    entry_id = row["entry_id"]
    if row["kind"] != "critique" or entry_id is None:
        return ""
    parent = db.get_entry(conn, int(entry_id))
    if parent is None:  # pragma: no cover - sync.py refuses an unknown parent
        return f'<p class="meta">entry {esc(entry_id)} is not on this node</p>'
    root, revisions = lineage.split_prompt(parent["prompt"] or "")
    revision = (
        f'<p class="rev"><span class="k">revise:</span> {esc(revisions[-1])}</p>'
        if revisions
        else ""
    )
    state = str(parent["state"])
    return (
        f'<p class="meta">of entry <a href="/entry/{int(entry_id)}">{int(entry_id)}</a>'
        f' · <span class="pill {esc(state)}">{esc(state_label(state))}</span></p>'
        f"{_entry_image(app, parent)}"
        f'<p class="prompt">{esc(root or "—")}</p>'
        f"{revision}"
    )


def _submission_card(app: App, conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """One submission waiting for a person: one input, two verbs, one form.

    Built from the decision card packet 6 established and deliberately the same
    shape: the id is the heading and is nowhere else in words, the one text box
    is read by whichever button presses it, and there is no JavaScript on the
    page at all — two ``formaction``s are plain HTML5 and the routes already
    exist. The difference is that the sentence is the subject here rather than
    the sketch, so it is the largest thing on the card.
    """
    submission_id = int(row["id"])
    kind = str(row["kind"])
    username = str(row["username"])
    return (
        f'<section class="panel card" id="submission-{submission_id}" '
        f'aria-label="Submission {submission_id}">'
        '<div class="id">'
        f'<span class="n">{submission_id}</span>'
        f'<span class="pill queued">{esc(kind)}</span>'
        f'<span class="chip person">@{esc(username)}</span>'
        f'<span class="dim">{esc(row["created_utc"])}</span>'
        "</div>"
        f"{_submission_parent(app, conn, row)}"
        f'<p class="ask">{esc(row["text"])}</p>'
        f'<form method="post" action="/submissions/{submission_id}/release" class="say">'
        '<input type="hidden" name="back" value="/submissions">'
        f'<input type="text" name="text" id="say-submission-{submission_id}" '
        'maxlength="400" aria-label="why you are declining, or nothing" '
        'placeholder="why you are declining, or nothing">'
        '<div class="acts">'
        f'<button type="submit" class="pub" '
        f'aria-label="Release submission {submission_id}">+ Release</button>'
        f'<button type="submit" class="rej" '
        f'formaction="/submissions/{submission_id}/decline" '
        f'aria-label="Decline submission {submission_id}">× Decline</button>'
        "</div>"
        '<p class="hint">Release queues it as a job, held for review like every '
        "other. Decline reads the box; the row stays as the record of what was "
        "asked.</p>"
        "</form>"
        "</section>"
    )


def submissions_page(app: App, conn: sqlite3.Connection) -> str:
    rows = db.pending_submissions(conn, SUBMISSIONS_PAGE_LIMIT)
    cards = [_submission_card(app, conn, row) for row in rows]
    if not cards:
        cards.append(
            '<section class="panel"><p class="dim">nothing is waiting. '
            "A row reaches this page when <code>sketchgen sync</code> pulls a "
            "prompt or a critique somebody submitted on the gallery.</p></section>"
        )
    return render("op_submissions", count=len(rows), cards="\n".join(cards))


def release_submission(
    conn: sqlite3.Connection, submission_id: int
) -> tuple[str, int | None, str]:
    """Turn one submission into a job. Returns ``(outcome, job_id, message)``.

    A prompt is enqueued with ``rules_file='random'`` (decision §1.6, so public
    work cannot skew the treatment/control split) and ``publication='hold'``,
    signed with the visitor's own login. A critique goes through
    :func:`sketchgen.lineage.spawn` with every default it already has —
    including the depth limit, which needs no exception here because a line at
    the limit is held and marked ``needs='review'``, which is what a public
    submission gets anyway (decision §1.5) — and is then recorded in
    ``critiques`` under :func:`human_prompt_version`.

    ``spawn()`` answering None means the parent was rejected in the time
    between the submission and this review. That is not an error and not
    something to raise at the operator: the row is declined with that as the
    reason, which is the true account of what happened to it.

    ``outcome`` is :data:`RELEASED`, :data:`DECLINED` or :data:`REFUSED`, so a
    caller can tell those three apart without reading the sentence; ``job_id``
    is None for the two that queued nothing.
    """
    row = db.submission(conn, submission_id)
    if row is None:
        return REFUSED, None, f"there is no submission {submission_id}"
    state = str(row["state"])
    if state != "pending":
        return REFUSED, None, (
            f"refused: submission {submission_id} is already {state} — "
            "nothing changed"
        )
    username = str(row["username"])
    text = str(row["text"])
    if row["kind"] == "prompt":
        job_id = db.enqueue(
            conn,
            text,
            submitted_by=username,
            rules_file="random",
            publication="hold",
        )
        db.release_submission(conn, submission_id, job_id)
        return RELEASED, job_id, (
            f"Submission {submission_id} released as job #{job_id} — "
            f"queued for {username}, held for review"
        )

    entry_id = int(row["entry_id"])
    version = human_prompt_version(username)
    # One critique per person per entry, asked before anything is queued: the
    # UNIQUE constraint would refuse the row after spawn() had already made the
    # job, and a child whose critique is not on record is a line with a hole in
    # it. The submission stays pending, so it can still be declined.
    if db.get_critique(conn, entry_id, version) is not None:
        return REFUSED, None, (
            f"refused: {username} already has a critique of entry {entry_id} — "
            "one per person per entry, and nothing changed"
        )
    try:
        job_id = lineage.spawn(
            conn,
            parent_entry_id=entry_id,
            critique=text,
            critique_by=username,
            submitted_by=username,
        )
    except ValueError as exc:
        return REFUSED, None, f"refused: {exc}"
    except sqlite3.Error as exc:  # pragma: no cover - defensive
        return REFUSED, None, f"spawn failed: {exc}"
    if job_id is None:
        reason = (
            f"the parent, entry {entry_id}, was rejected before this was reviewed"
        )
        db.decline_submission(conn, submission_id, reason)
        return DECLINED, None, f"Submission {submission_id} declined — {reason}"
    db.record_critique(
        conn,
        entry_id,
        critique=text,
        critique_by=username,
        prompt_version=version,
        spawned_job_id=job_id,
    )
    db.release_submission(conn, submission_id, job_id)
    generation = lineage.generation_of(conn, entry_id) + 1
    job = db.get_job(conn, job_id)
    tail = (
        f" — generation {generation} is at the depth limit "
        f"({lineage.DEFAULT_MAX_DEPTH}), so it is held and needs a person "
        "before it goes any further"
        if job is not None and job.needs == "review"
        else f" — generation {generation} of entry {entry_id}"
    )
    return RELEASED, job_id, (
        f"Submission {submission_id} released as job #{job_id}, "
        f"critique by {username}{tail}"
    )


def decline_submission(
    conn: sqlite3.Connection, submission_id: int, reason: str
) -> tuple[str, str]:
    """A person said no. Returns ``(outcome, message)``.

    Nothing is deleted: the sentence stays in the table with the reason beside
    it, because a declined submission is the record of what somebody asked for
    (plan §7). No job is created and no critique is recorded.
    """
    row = db.submission(conn, submission_id)
    if row is None:
        return REFUSED, f"there is no submission {submission_id}"
    state = str(row["state"])
    if state != "pending":
        return REFUSED, (
            f"refused: submission {submission_id} is already {state} — "
            "nothing changed"
        )
    reason = reason.strip() or "declined by operator"
    db.decline_submission(conn, submission_id, reason)
    return DECLINED, f"Submission {submission_id} declined — {reason}"


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
    ("POST", re.compile(r"^/new/defaults$"), "post_new_defaults"),
    ("GET", re.compile(r"^/api/job/(?P<job_id>\d+)\.json$"), "api_job"),
    ("GET", re.compile(r"^/events/job/(?P<job_id>\d+)$"), "events_job"),
    ("GET", re.compile(r"^/job/(?P<job_id>\d+)$"), "page_job"),
    ("POST", re.compile(r"^/job/(?P<job_id>\d+)/cancel$"), "post_cancel"),
    ("POST", re.compile(r"^/job/(?P<job_id>\d+)/laptop$"), "post_laptop"),
    ("GET", re.compile(r"^/held$"), "page_held"),
    ("GET", re.compile(r"^/api/batch\.json$"), "api_batch"),
    ("POST", re.compile(r"^/held/batch$"), "post_batch"),
    ("POST", re.compile(r"^/held/batch/dismiss$"), "post_batch_dismiss"),
    ("POST", re.compile(r"^/held/(?P<entry_id>\d+)/publish$"), "post_publish"),
    ("POST", re.compile(r"^/held/(?P<entry_id>\d+)/reject$"), "post_reject"),
    ("POST", re.compile(r"^/held/(?P<entry_id>\d+)/archive$"), "post_archive"),
    ("GET", re.compile(r"^/entry/(?P<entry_id>\d+)$"), "page_entry"),
    ("POST", re.compile(r"^/entry/(?P<entry_id>\d+)/spawn$"), "post_spawn"),
    ("GET", re.compile(r"^/submissions$"), "page_submissions"),
    (
        "POST",
        re.compile(r"^/submissions/(?P<submission_id>\d+)/release$"),
        "post_submission_release",
    ),
    (
        "POST",
        re.compile(r"^/submissions/(?P<submission_id>\d+)/decline$"),
        "post_submission_decline",
    ),
    ("POST", re.compile(r"^/control$"), "post_control"),
    ("GET", re.compile(r"^/build$"), "page_build"),
    ("POST", re.compile(r"^/build/check$"), "post_build_check"),
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
        # The menus are drawn without a connection, so the paid model names
        # registered in the database are handed to this request's thread here
        # (sketchgen.models.remember_registered). A database that cannot be
        # read means no registered names, not a page that fails.
        try:
            conn = self.app.connect()
            try:
                models.remember_registered(db.get_paid_models(conn))
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            models.remember_registered([])
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
            billing = billing_values(conn)
        finally:
            conn.close()
        doc = console_document(self.app)
        self.html(
            layout(
                title="Console",
                here="/",
                body=console_page(doc, marks.get("tokens"), billing),
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
            # Three small queries and one stat, not console.collect(): this
            # route runs every two seconds on every open page, and the
            # collector sleeps 100 ms for its second /proc/stat reading. As
            # with the collector itself, a card that cannot be read is an
            # empty card and never a 500 on the page that shows the pill.
            now = ACTIVITY_NONE
            reader = getattr(console, "activity", None) if console else None
            if callable(reader):
                try:
                    now = reader(conn)
                except Exception as exc:  # noqa: BLE001 - never take the UI down
                    sys.stderr.write(f"{db.utc_now()} activity failed: {exc}\n")
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
                # …and so does the status card, with the pill it should wear,
                # so the page script never has to know what a state means.
                "activity": dict(now, pill_text=activity_pill(now)[0],
                                 pill_class=activity_pill(now)[1],
                                 elapsed_text=activity_elapsed(now),
                                 bar_pct=activity_bar_pct(now),
                                 bar_class=activity_bar_class(now)),
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
            body = new_page(
                self.app, conn, form_from_query(conn, self.query()), control=control
            )
        finally:
            conn.close()
        self.html(
            layout(
                title="New job",
                here="/new",
                body=body,
                control=control,
                back="/new",
                flash=self.flash(),
                nav_marks=marks,
                page_script=NEW_JOB_SCRIPT,
            )
        )

    def _new_page_with(
        self, form: dict[str, list[str]], *, error: str | None = None,
        flash: str | None = None, status: int = 200,
    ) -> None:
        """The form again, with what was typed still in it."""
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            self.html(
                layout(
                    title="New job",
                    here="/new",
                    body=new_page(self.app, conn, form, error, control=control),
                    control=control,
                    back="/new",
                    flash=flash,
                    nav_marks=nav_summary(conn),
                    page_script=NEW_JOB_SCRIPT,
                ),
                status=status,
            )
        finally:
            conn.close()

    def post_new(self) -> None:
        form = self.form()
        error: str | None = None
        conn = self.app.connect()
        try:
            try:
                queued = create_jobs(conn, form)
            except (ValueError, sqlite3.Error) as exc:
                error = str(exc)
                queued = []
        finally:
            conn.close()
        if not queued:
            self._new_page_with(form, error=error, status=400)
            return
        self.redirect("/queue", queued_flash(queued))

    def post_new_defaults(self) -> None:
        """Save as defaults, or forget them. The page comes back with the
        prompt still typed — a redirect would have thrown it away."""
        form = self.form()
        action = (form.get("action") or ["save"])[0]
        conn = self.app.connect()
        try:
            try:
                if action == "clear":
                    flash = clear_defaults(conn)
                    for key in BUILTIN_DEFAULTS:
                        form.pop(key, None)
                else:
                    flash = save_defaults(conn, form)
            except (ValueError, sqlite3.Error) as exc:
                self._new_page_with(form, error=str(exc), status=400)
                return
        finally:
            conn.close()
        self._new_page_with(form, flash=flash)

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
                page_script=JOB_SCRIPT + PREVIEW_SCRIPT,
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
            tray, body = held_page(self.app, conn)
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
                page_script=PREVIEW_SCRIPT + HELD_SCRIPT,
                nav_marks=marks,
                subheader=tray,
            )
        )

    # -- the batch (packet 11) ---------------------------------------------

    def wants_json(self) -> bool:
        """Whether this request asked for the document rather than a page.

        Packet 12's script posts the form with ``Accept: application/json`` so
        that it can switch the tray to its running state in place; a browser
        with no script sends the same body and gets the 303 it expects.
        """
        return "application/json" in (self.headers.get("Accept") or "")

    def api_batch(self) -> None:
        self.json_out({"batch": batch_document(self.app)})

    def post_batch(self) -> None:
        form = self.form()
        json_wanted = self.wants_json()
        if batch_running(self.app):
            # Checked first and checked again inside start_batch: this is the
            # double-press bug, and a press that lost the race must not get as
            # far as reading the marks, let alone running them.
            self._batch_refusal(BATCH_BUSY, 409, json_wanted)
            return
        conn = self.app.connect()
        try:
            items = batch_plan(conn, form)
        except Refused as exc:
            self._batch_refusal(str(exc), 400, json_wanted)
            return
        finally:
            conn.close()
        try:
            start_batch(self.app, items)
        except Refused as exc:
            self._batch_refusal(str(exc), 409, json_wanted)
            return
        if json_wanted:
            self.json_out({"batch": batch_document(self.app)}, 202)
        else:
            # The tray renders the progress server-side and refreshes itself,
            # so there is nothing to say in a flash that the page will not say
            # better a moment later.
            self.redirect("/held")

    def post_batch_dismiss(self) -> None:
        self.form()
        refusal = dismiss_batch(self.app)
        if refusal is not None and self.wants_json():
            self.json_out({"error": refusal}, 409)
            return
        self.redirect("/held", refusal)

    def _batch_refusal(self, message: str, status: int, json_wanted: bool) -> None:
        """One refusal, in whichever dialect the request asked for."""
        if json_wanted:
            self.json_out({"error": message}, status)
        else:
            self.redirect("/held", message)

    def post_publish(self, entry_id: str) -> None:
        self.form()
        if self._batch_guard():
            return
        conn = self.app.connect()
        try:
            message = publish_entry(self.app, conn, int(entry_id))
        finally:
            conn.close()
        self.redirect("/held", message)

    def post_reject(self, entry_id: str) -> None:
        form = self.form()
        if self._batch_guard():
            return
        reason = said(form, "reason")
        conn = self.app.connect()
        try:
            message = reject_entry(self.app, conn, int(entry_id), reason)
        finally:
            conn.close()
        self.redirect("/held", message)

    def post_archive(self, entry_id: str) -> None:
        form = self.form()
        if self._batch_guard():
            return
        back = (form.get("back") or ["/held"])[0]
        if not back.startswith("/") or back.startswith("//"):
            back = "/held"
        conn = self.app.connect()
        try:
            message = archive_entry(conn, int(entry_id))
        finally:
            conn.close()
        self.redirect(back, message)

    def _batch_guard(self) -> bool:
        """Do nothing and say so, while a batch is running (§3.5).

        The three routes that reach the gallery are shut for the duration, and
        the reason is the one in §1.5: a second publisher would queue on the
        checkout lock behind the batch's, holding a request open for as long as
        the batch takes and then doing work the operator has forgotten asking
        for. ``post_spawn`` is not guarded — it is the job page's form too, it
        touches no git, and SQLite serialises the write.

        Call it after :meth:`form`, so the request body is read either way and
        the connection stays usable for the next request on it.
        """
        if not batch_running(self.app):
            return False
        self.redirect("/held", BATCH_GUARD)
        return True

    def page_entry(self, entry_id: str) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            body = entry_page(self.app, conn, int(entry_id))
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title=f"Entry {entry_id}",
                here="/held",
                body=body,
                control=control,
                back=f"/entry/{entry_id}",
                flash=self.flash(),
                page_script=PREVIEW_SCRIPT,
                nav_marks=marks,
            )
        )

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

    def page_submissions(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            body = submissions_page(self.app, conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        self.html(
            layout(
                title="Submissions",
                here="/submissions",
                body=body,
                control=control,
                back="/submissions",
                flash=self.flash(),
                nav_marks=marks,
            )
        )

    def post_submission_release(self, submission_id: str) -> None:
        self.form()
        conn = self.app.connect()
        try:
            _, _, message = release_submission(conn, int(submission_id))
        finally:
            conn.close()
        self.redirect("/submissions", message)

    def post_submission_decline(self, submission_id: str) -> None:
        form = self.form()
        conn = self.app.connect()
        try:
            _, message = decline_submission(
                conn, int(submission_id), said(form, "reason")
            )
        finally:
            conn.close()
        self.redirect("/submissions", message)

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

    def page_build(self) -> None:
        conn = self.app.connect()
        try:
            control = db.get_control(conn)
            marks = nav_summary(conn)
        finally:
            conn.close()
        watch = BUILD_WATCH
        body = build_page(watch.reading() if watch else None,
                          watch.root if watch else None)
        self.html(
            layout(
                title="Build",
                here="/build",
                body=body,
                control=control,
                back="/build",
                flash=self.flash(),
                nav_marks=marks,
            )
        )

    def post_build_check(self) -> None:
        self.form()
        watch = BUILD_WATCH
        if watch is None:
            self.redirect("/build", "refused: this process is not watching its build")
            return
        error = watch.check()
        doc = watch.reading() or {}
        chip = build_chip(doc)
        if error:
            message = f"refused: could not reach GitHub — {error}"
        else:
            message = f"Checked: {chip[2] if chip else 'no checkout here'}"
        self.redirect("/build", message)

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
    global BUILD_WATCH
    server = make_server(
        bind=bind,
        port=port,
        db_path=db_path,
        jobs_dir=jobs_dir,
        once_for_test=once_for_test,
    )
    app: App = server.app  # type: ignore[attr-defined]
    if not once_for_test and build.ROOT is not None:
        # fleet.md §1.2 and §1.6: say which build this process is, and start
        # looking for a newer main. Never under test: see BuildWatch.
        try:
            conn = app.connect()
            try:
                build.stamp_running(conn, "web")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            sys.stderr.write(f"{db.utc_now()} could not stamp the web build: {exc}\n")
        BUILD_WATCH = BuildWatch(build.ROOT, app.db_path)
        BUILD_WATCH.start()
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
