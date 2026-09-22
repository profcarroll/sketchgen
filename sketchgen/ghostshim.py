"""The pointer a sketch gets when nobody is in the room, and why it is inside.

The kiosk plays a published sketch on a projector for sixty seconds and moves
on. A sketch built around input sits still for the whole minute: entry 1103, a
jigsaw, is a photograph cut into pieces that never move, and on a wall with
nobody at the keyboard that reads as blank or broken. 27 entries of 910 respond
to a click and 25 to a drag (docs/plans/swipe.md §133–137), so this is a
hundredth of the gallery showing itself as a still frame.

The kiosk cannot help. Its frame is ``sandbox="allow-scripts"`` with no
``allow-same-origin``, which makes it an opaque origin: the projector page
cannot dispatch an event into it, cannot read its canvas and must not try
(docs/plans/kiosk.md §4.5). So the pointer has to be *inside* the frame, in the
sketch's own document, where the canvas and its ``getBoundingClientRect()`` are
in reach — which is exactly the shape ``soundshim.py`` already has, and this
file is modelled on it line for line. Decided 21 September 2026,
``DECIDE[ghost-where]`` in docs/plans/auto-mouse.md §1.

The one thing the kiosk does say is a query parameter on the frame's ``src``:
``sketch/?ghost=click,drag``. One way, read once, nothing after
(``DECIDE[ghost-channel]``). Without it the shim returns before it touches the
DOM, so the entry page, the swipe feed and the operator's preview load the same
bytes as the kiosk and never ghost a real viewer — that early return is the
whole guarantee, and it is the first thing in the script for that reason.

What it dispatches is a ``MouseEvent`` on the canvas with ``bubbles: true``,
its ``clientX``/``clientY`` computed from the canvas's own
``getBoundingClientRect()`` and the event's fractions. That is exactly what p5
1.11.3 reads: ``_updateNextMouseCoords`` takes ``clientX`` against
``_curElement.elt.getBoundingClientRect()``, and p5 attaches every one of its
mouse handlers to ``window``, so an event that bubbles off the canvas reaches
them. ``mouseX``, ``mouseIsPressed``, ``mousePressed``, ``mouseDragged``,
``mouseReleased``, ``mouseMoved`` and ``mouseClicked`` all behave as they do
for a hand. Read out of the pinned 1.11.3 bundle on 21 September 2026; the gate
is where it is confirmed in a browser (Packet 15).

A trusted ``pointerdown`` or ``mousemove`` ends the run for the rest of the
load (``DECIDE[ghost-yields]``): a hand always wins, and nobody wants to watch
a cursor fight them. ``isTrusted`` is false on everything dispatched here,
which is also why sound cannot be started this way — a synthetic event is not a
user gesture and will not resume an ``AudioContext``. ``kiosk.md`` §1.7 still
says so for audio, and says only that now.

:data:`BUILTINS` is the three default scripts as Python data, rendered into the
JavaScript below rather than written twice. Packet 15's gate is a standalone
script with its own copy on the node and cannot import this module, so it will
carry a copy and a test will pin the two together — the arrangement
``executor.SOUND_RE`` and the gate's ``SOUND_RE`` are already in.

An entry may carry a script of its own instead, written by the model that
wrote the sketch and inlined above the shim by :func:`with_script` as
:data:`GLOBAL` (``DECIDE[ghost-dataset]``, 21 September 2026). The built-ins
are the floor under every entry that has none, which is nearly all of them.

Two callers, as for the sound shim: ``executor.index_html_for``, so the gate
runs the bytes the gallery publishes, and ``gallery._write_entry``, so entries
published before this existed pick it up on the next ``render-all``. A page
that already carries the marker is left alone. It costs every entry page about
eight kilobytes, three of which are the built-ins — worth saying out loud,
because it goes on all 910 of them and not only on the hundredth that responds
to anything.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

__all__ = [
    "BUILTINS",
    "GATE_DIR",
    "GHOST_PNG",
    "GLOBAL",
    "MARKER",
    "MAX_EVENTS",
    "MAX_MS",
    "SCRIPT_MARKER",
    "SHIM",
    "TYPES",
    "ghost_png",
    "script_js",
    "with_script",
    "with_shim",
]

#: The caps a ghost script is held to, here and in the shim below
#: (``DECIDE[ghost-script]``). Sixty-four events over eight seconds is a
#: gesture; anything longer is a sketch of its own, and the kiosk gives each
#: entry sixty seconds in total.
MAX_EVENTS = 64
MAX_MS = 8000

#: Where the gate leaves the four ghost frames, and what it calls them
#: (auto-mouse.md §5.1). Named here rather than in either reader because there
#: are two of them and no column between them: :func:`sketchgen.gallery._artefact`
#: copies the file onto the entry page and :func:`sketchgen.lineage.ghost_image`
#: shows it to the critic, and a picture the page shows and the critic does not
#: — or the other way round — would be the kind of drift nobody notices until a
#: critique is about a frame nobody can find.
GATE_DIR = ".gate"
GHOST_PNG = "ghost.png"

#: The four event types the players know. ``click`` is the other three in
#: order, because p5 sets ``mouseIsPressed`` from ``mousedown`` and calls
#: ``mouseClicked`` from ``click``, and a sketch may read either.
TYPES = ("move", "down", "up", "click")

#: The default gap between the end of one run and the start of the next, in
#: milliseconds. The kiosk overrides it from ``config.json``.
DEFAULT_LOOP_MS = 6000

#: The golden section. The three extra clicks land on its points rather than on
#: a grid: a sketch that reacts to where it was clicked shows that better from
#: three unrelated places than from three at the same spacing.
_PHI = (math.sqrt(5.0) - 1.0) / 2.0


def _event(when: float, kind: str, x: float, y: float) -> dict:
    """One event, rounded so the rendered JavaScript is byte-stable."""
    return {"t": int(round(when)), "type": kind, "x": round(float(x), 4),
            "y": round(float(y), 4)}


def _lerp(start: float, end: float, part: float) -> float:
    return start + (end - start) * part


def _approach(start, end, count, first_t, last_t) -> list[dict]:
    """``count`` moves along a straight line, evenly spaced in time.

    The last one lands exactly on ``end``: a pointer that stopped a pixel short
    of where it is about to press would make the press and the travel disagree.
    """
    out = []
    for step in range(1, count + 1):
        out.append(_event(
            first_t + (last_t - first_t) * (step - 1) / max(1, count - 1),
            "move",
            _lerp(start[0], end[0], step / count),
            _lerp(start[1], end[1], step / count),
        ))
    return out


def _click_script() -> list[dict]:
    """In from a corner to the centre, one press, then three clicks apart."""
    out = _approach((0.08, 0.92), (0.5, 0.5), 8, 50, 400)
    out.append(_event(500, "down", 0.5, 0.5))
    out.append(_event(620, "up", 0.5, 0.5))
    for at, point in enumerate(((1 - _PHI, _PHI), (_PHI, 1 - _PHI), (_PHI, _PHI))):
        when = 1520 + 900 * at
        # The move first: a sketch whose mouseMoved draws the cursor should
        # have somewhere to draw it before the press arrives.
        out.append(_event(when - 120, "move", point[0], point[1]))
        out.append(_event(when, "click", point[0], point[1]))
    return out


def _drag_script_leg(start, end, first_t) -> list[dict]:
    """One press, twelve moves over 800 ms, one release."""
    down_at = first_t + 200
    out = [_event(first_t, "move", start[0], start[1]),
           _event(down_at, "down", start[0], start[1])]
    for step in range(1, 13):
        out.append(_event(
            down_at + 800 * step / 12, "move",
            _lerp(start[0], end[0], step / 12),
            _lerp(start[1], end[1], step / 12),
        ))
    out.append(_event(down_at + 920, "up", end[0], end[1]))
    return out


def _wander_script() -> list[dict]:
    """A Lissajous path, no button.

    Three turns across against two down, which never retraces itself and never
    stops in a corner — a sketch whose field scatters under the cursor has to
    be crossed, not visited.
    """
    out = []
    for step in range(1, 41):
        turn = step / 40
        out.append(_event(
            100 * step, "move",
            0.5 + 0.4 * math.sin(2 * math.pi * 3 * turn),
            0.5 + 0.4 * math.sin(2 * math.pi * 2 * turn + math.pi / 4),
        ))
    return out


#: The three default scripts, by name. An entry with no script of its own gets
#: the one its gate-confirmed assertions name: ``click`` for ``responds(click)``,
#: ``drag`` for ``responds(drag)``, and the kiosk asks for both where both hold.
BUILTINS: dict[str, list[dict]] = {
    "click": _click_script(),
    "drag": _drag_script_leg((0.2, 0.5), (0.8, 0.5), 200)
    + _drag_script_leg((0.5, 0.2), (0.5, 0.8), 1800),
    "wander": _wander_script(),
}


def ghost_png(*source_dirs: str | Path | None) -> Path | None:
    """The gate's ghost frames for the first of these attempt directories.

    ``None`` when none of them has the file, which is the normal answer: the
    ghost window arrived with ``HARNESS_VERSION`` 3 on 21 September 2026 and
    nothing re-gates a published entry, so every one of the 910 entries in the
    gallery today has a ``strip.png`` and no ``ghost.png``. Callers treat that
    as a fact about the entry, never as an error (auto-mouse.md §6).

    Several directories because an entry's ``source_dir`` and its last
    attempt's can disagree — ``gallery._source_dir`` has always tried both —
    and the entry page and the critic have to land on the same file.
    """
    for source in source_dirs:
        if not source:
            continue
        candidate = Path(source) / GATE_DIR / GHOST_PNG
        if candidate.is_file():
            return candidate
    return None


def _render_builtins() -> str:
    """:data:`BUILTINS` as the JavaScript literal the shim carries.

    Sorted keys and fixed rounding, so the same module renders the same bytes
    and a render-all is a no-op on a page that already has the shim.
    """
    lines = []
    for name in sorted(BUILTINS):
        events = json.dumps(BUILTINS[name], sort_keys=True, separators=(",", ":"))
        lines.append('        "%s": %s' % (name, events))
    return "{\n" + ",\n".join(lines) + "\n      }"


#: The first line of the shim, which is also how a page that has it is known.
MARKER = "    <script>/* ghost pointer */"

#: Where a page carries a script of its own, written by the model that wrote
#: the sketch (``DECIDE[ghost-dataset]``, Packet 14). One name, spelled once:
#: :func:`with_script` writes it and the player below reads it, and a typo in
#: either would be a page whose script is silently ignored in favour of the
#: built-in — which looks exactly like a working page.
GLOBAL = "window.__ghostScript"

#: How a page that already carries an inlined script is known — the assignment
#: and not the name, because the shim below *reads* the name and a page with
#: the shim on it would otherwise look like a page that already had a script.
SCRIPT_MARKER = "    <script>%s = " % GLOBAL

SHIM = MARKER + """
    (function () {
      /* Inert without ?ghost=; see sketchgen/ghostshim.py for why it is here
       * at all. Nothing below runs on the entry page, on swipe or in a
       * preview, because none of them pass the parameter. */
      var MAX_EVENTS = %(max_events)d;
      var MAX_MS = %(max_ms)d;
      var LOOP_MS = %(loop_ms)d;
      var GAP_MS = 600;          /* between two built-ins played in a row */
      var POLL_MS = 100;         /* a preload() sketch has no canvas yet */
      var WAIT_MS = 10000;       /* and after ten seconds it is not coming */
      var BUILTINS = %(builtins)s;

      function note(err) {
        /* debug, never error: the gate's console_clean must not be trippable
         * by a shim the sketch did not ask for. */
        if (window.console && window.console.debug) {
          window.console.debug("ghost pointer: " +
            ((err && err.message) ? err.message : err));
        }
      }

      function guarded(fn) {
        return function () {
          try { fn(); } catch (err) { note(err); }
        };
      }

      function param(name) {
        var found = new RegExp("[?&]" + name + "=([^&#]*)")
          .exec(window.location.search || "");
        return found ? decodeURIComponent(found[1].replace(/\\+/g, " ")) : null;
      }

      function num(value, fallback) {
        var parsed = Number(value);
        return (value !== null && value !== "" && isFinite(parsed) && parsed > 0)
          ? parsed : fallback;
      }

      /* Every script, whoever wrote it, goes through here. */
      function clean(events) {
        var out = [];
        var at;
        var one;
        var kind;
        if (!events || typeof events.length !== "number") { return out; }
        for (at = 0; at < events.length && out.length < MAX_EVENTS; at += 1) {
          one = events[at];
          if (!one) { continue; }
          kind = String(one.type);
          if (kind !== "move" && kind !== "down" &&
              kind !== "up" && kind !== "click") { continue; }
          if (!(Number(one.t) >= 0) || Number(one.t) > MAX_MS) { continue; }
          if (!(Number(one.x) >= 0) || Number(one.x) > 1) { continue; }
          if (!(Number(one.y) >= 0) || Number(one.y) > 1) { continue; }
          out.push({ t: Number(one.t), type: kind, x: Number(one.x), y: Number(one.y) });
        }
        return out;
      }

      function ends(events) {
        var last = 0;
        var at;
        for (at = 0; at < events.length; at += 1) {
          if (events[at].t > last) { last = events[at].t; }
        }
        return last;
      }

      /* %(global)s, when a page carries one, beats the built-ins
       * the parameter names; a comma list is played one after another. */
      function chosen(names) {
        var out = [];
        var parts;
        var shifted;
        var at;
        var by;
        var step;
        if (%(global)s) { return clean(%(global)s); }
        parts = String(names).split(",");
        for (at = 0; at < parts.length; at += 1) {
          shifted = BUILTINS[parts[at].replace(/^\\s+|\\s+$/g, "")];
          if (!shifted) { continue; }
          by = out.length ? ends(out) + GAP_MS : 0;
          for (step = 0; step < shifted.length; step += 1) {
            out.push({
              t: shifted[step].t + by, type: shifted[step].type,
              x: shifted[step].x, y: shifted[step].y
            });
          }
        }
        return clean(out);
      }

      var want = param("ghost");
      if (!want || want === "0") { return; }

      var script = chosen(want);
      if (!script.length) { return; }
      var loopMs = num(param("ghost_loop"), LOOP_MS);
      var stopped = false;

      /* A hand always wins. isTrusted is false on everything dispatched
       * below, so this cannot be woken by the ghost itself, and once it has
       * fired the ghost never plays again in this load. */
      function yieldToHand(event) {
        if (event && event.isTrusted) { stopped = true; }
      }
      document.addEventListener("pointerdown", yieldToHand, true);
      document.addEventListener("mousemove", yieldToHand, true);

      function send(canvas, kind, x, y, buttons) {
        canvas.dispatchEvent(new MouseEvent(kind, {
          bubbles: true, cancelable: true,
          clientX: x, clientY: y, button: 0, buttons: buttons
        }));
      }

      /* p5 1.11 reads clientX against the canvas's own rect and attaches its
       * handlers to window, so bubbles: true on the canvas is a hand. */
      function fire(one) {
        var canvas = document.querySelector("canvas");
        var box;
        var x;
        var y;
        if (stopped || !canvas) { return; }
        box = canvas.getBoundingClientRect();
        x = box.left + box.width * one.x;
        y = box.top + box.height * one.y;
        if (one.type === "move") { send(canvas, "mousemove", x, y, 0); }
        else if (one.type === "down") { send(canvas, "mousedown", x, y, 1); }
        else if (one.type === "up") { send(canvas, "mouseup", x, y, 0); }
        else {
          send(canvas, "mousedown", x, y, 1);
          send(canvas, "mouseup", x, y, 0);
          send(canvas, "click", x, y, 0);
        }
      }

      function play() {
        var at;
        if (stopped) { return; }
        for (at = 0; at < script.length; at += 1) {
          window.setTimeout(guarded(fire.bind(null, script[at])), script[at].t);
        }
        window.setTimeout(guarded(play), ends(script) + loopMs);
      }

      function waitForCanvas(waited) {
        if (stopped) { return; }
        if (document.querySelector("canvas")) { play(); return; }
        if (waited >= WAIT_MS) { note("no canvas after " + WAIT_MS + " ms"); return; }
        window.setTimeout(
          guarded(waitForCanvas.bind(null, waited + POLL_MS)), POLL_MS);
      }

      guarded(waitForCanvas.bind(null, 0))();
    })();
    </script>
""" % {
    "max_events": MAX_EVENTS,
    "max_ms": MAX_MS,
    "loop_ms": DEFAULT_LOOP_MS,
    "builtins": _render_builtins(),
    "global": GLOBAL,
}

#: The sketch's own script tag, however the page that carries it spells it.
#: The executor writes one exact line, but a model's own ``html`` block writes
#: whatever it likes and still gets the ghost (auto-mouse.md §3.1).
_SKETCH_TAG_RE = re.compile(
    r"""<script\b[^>]*\bsrc\s*=\s*["'](?:\./)?sketch\.js["'][^>]*>\s*</script>""",
    re.IGNORECASE,
)


def with_shim(html: str) -> str:
    """``html`` with the shim after its ``sketch.js`` tag; unchanged when it has
    no tag, or already carries the shim.

    *After*, not before: p5 attaches its mouse handlers when ``sketch.js``
    runs, and this script only dispatches. Putting it first would mean
    dispatching into a page that has nothing listening yet.
    """
    if MARKER in html:
        return html
    found = _SKETCH_TAG_RE.search(html)
    if found is None:
        return html
    at = found.end()
    if html[at:at + 1] == "\n":
        at += 1
    return html[:at] + SHIM + html[at:]


def with_script(html: str, events: list[dict]) -> str:
    """``html`` with *events* inlined just above the shim, as :data:`GLOBAL`.

    The script the executor wrote for its own sketch (``DECIDE[ghost-dataset]``,
    Packet 14). *Above* the shim, because the player reads the global at the
    moment the parameter sends it looking, and a page whose own script came
    second would play the built-in instead — the one failure that looks like
    success.

    Unchanged when the page already carries a script (a render-all rewrites
    every published page and must be a no-op on one it wrote last time) and
    when it has no shim to feed: a page with no ``sketch.js`` tag never got the
    marker, and a global nothing reads is litter in somebody's file.
    """
    if SCRIPT_MARKER in html:
        return html
    at = html.find(MARKER)
    if at < 0:
        return html
    return html[:at] + _script_tag(events) + html[at:]


def _script_tag(events: list[dict]) -> str:
    """The one line :func:`with_script` inserts.

    ``</`` is escaped because the HTML parser ends a ``<script>`` element at
    the first ``</script``, whatever the JavaScript around it meant, and a
    page that ends its script early is a page that throws. A validated ghost
    script cannot contain the sequence — every value in it is a number or one
    of four words (``executor.validate_ghost``) — but the escaping does not
    depend on that: this function is handed a list, not a promise, and
    ``<\\/`` is the same string to JSON and safe to the parser both.

    Compact, sorted keys, as :func:`_render_builtins` writes the built-ins, so
    the same list renders the same bytes every render.
    """
    data = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return SCRIPT_MARKER + data.replace("</", "<\\/") + ";</script>\n"


def script_js() -> str:
    """The shim's JavaScript, without the ``<script>`` tags around it.

    tests/js/ghostshim.js runs the real thing in node rather than a
    transcription of it: a second copy of this player would be a second thing
    to keep true, and the whole point of rendering :data:`BUILTINS` into the
    script was to stop that happening.
    """
    return SHIM.split(">", 1)[1].rsplit("</script>", 1)[0]
