#!/usr/bin/env python3
"""sketch_gate.py -- the deterministic gate for one p5.js sketch directory.

Runs a sketch in headless chromium (Playwright, sync API), applies the fixed
checks from the sketchgen spec section 3.1 to every sketch, then evaluates the
small assertion vocabulary of section 3.2 that a planner is allowed to choose
from.  Writes gate.png, strip.png, console.log and report.json into --out.

    python3 sketch_gate.py <sketch_dir> [--assert WORD ...] [--seed N]
                           [--out DIR] [--timeout S] [--json]
                           [--frame-budget-ms MS] [--budget-s S]
                           [--keep-browser-log]

Four sampling decisions the fixtures, the first repair run and job 166 forced,
stated up front:

  - is_looping is read at the END of the ~2 s idle window, not at load.
    Calling noLoop() at the end of the first draw() is a legitimate p5 idiom,
    and a sketch that declares itself static during its first frame is still
    declaring itself static.
  - frame_advancing means "still advancing in the second half of the idle
    window": frameCount is sampled partway through the window and again at the
    end, and the check is end > mid.  Comparing against load instead would call
    a sketch that throws at frame 11 and kills its own draw loop "advancing".
    report.json["notes"] carries both samples.
  - responds(audio) is a pixel-wise comparison against a silent reference run,
    not a brightness magnitude.  Brightness measured how loud the sketch's
    response was rather than whether it responded: a correctly repaired sketch
    moved the mean by 1.0 on 0-255 while a sketch whose background is the mic
    level moved it by 254.  The determinism below is what makes the strict
    comparison sound -- the two runs differ only by audio.
  - frame_budget is the WALL time this runner spends inside __gate.step() over
    the idle window, divided by the frames it stepped -- not the sketch's own
    idea of deltaTime, which the virtual clock has already made a lie.  Job 166
    attempt 3 took 1,093 ms to draw one frame, passed every other check, and ran
    the operator's laptop out of memory when the Held page previewed it.  The
    gate timed that window all along and never read the number.

Exit codes follow the delegate.py convention used elsewhere in this repo:
    0  every failable check passed and every requested assertion passed
    1  console_clean, frame_advancing, sound_lib_ok, audio_context_running or
       frame_budget reported false, or a requested assertion failed.
       is_looping is a declaration, not a verdict, and never fails a run on its
       own.
    3  refused: no browser, unknown assertion word, unusable sketch directory

The sketch directory is served to the page by a page.route() interception on a
fake, potentially-trustworthy origin (see SKETCH_ORIGIN).  Nothing listens on a
port.  file:// was tried and rejected: on an opaque origin p5.sound cannot load
its AudioWorklet and throws, so the audio-without-a-user-gesture bug would be
reported as a console error rather than as a suspended AudioContext.

--------------------------------------------------------------------------
HOW DETERMINISM IS OBTAINED, AND WHERE IT STOPS
--------------------------------------------------------------------------
Everything below is installed by page.add_init_script(), which chromium runs in
the page before any of the page's own scripts -- so before p5.js itself loads.

1.  A virtual clock.  Date.now(), new Date() with no arguments and
    performance.now() all read a counter that starts at a fixed origin and
    advances ONLY when this script steps a frame.  Wall-clock time passing
    while the browser sits idle is invisible to the sketch.

2.  Frame stepping.  window.requestAnimationFrame is replaced by a queue.
    Nothing animates on its own; __gate.step(n) advances the virtual clock by
    n * 16.667 ms and drains the queue once per step.  This is what makes two
    runs comparable: the screenshot is taken at a fixed FRAME NUMBER, not after
    a fixed number of wall seconds, so a contended CPU cannot change the image.
    Exceptions thrown inside a frame callback are re-thrown out of band with
    setTimeout so that chromium still reports them as uncaught page errors --
    catching them silently would hide exactly the bug the gate exists to find.

3.  Seeds.  randomSeed(SEED) and noiseSeed(SEED) have to run before the
    sketch's own setup() body.  In global mode that is done by wrapping
    window.setup at DOMContentLoaded: sketch.js has already declared setup() by
    then, and p5 only calls it from its own window-load handler, which was
    registered after this init script ran.  In instance mode window.p5 is put
    behind an accessor so that p5.prototype._initializeInstanceVariables can be
    wrapped, and from there an accessor for `setup` is installed on the
    instance, which is how an instance-mode sketch hands p5 its setup function.
    (p5 1.11 moved _start/_setup/_draw onto the instance as class fields, and
    they are not assigned yet when _initializeInstanceVariables runs, so there
    is nothing earlier to wrap.)  report.json["notes"] says which hook fired, or
    says plainly that neither did.  Math.random is additionally replaced with a
    seeded mulberry32 generator.

4.  preserveDrawingBuffer is forced on for webgl/webgl2 contexts, so that a
    WEBGL sketch's canvas can still be read back after the frame is done.

Limits, stated rather than hidden:
  - A sketch driven by setTimeout/setInterval instead of requestAnimationFrame
    is not stepped by us and will see real wall time; its output may vary.
  - A library that captures its own randomness from crypto.getRandomValues, or
    that is loaded in a worker or an iframe, is untouched by any of this.
  - Anything the sketch fetches from the network is as stable as the network.
  - Font rasterisation and GPU behaviour are only stable on one machine; the
    two-runs-identical guarantee is per machine, not across machines.
"""

import argparse
import base64
import binascii
import datetime
import json
import math
import os
import pathlib
import re
import struct
import sys
import time
import wave
from urllib.parse import unquote, urlsplit

VOCAB = [
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "size(w,h)",
    "no_motion",
]
SIMPLE_ASSERTIONS = {
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "no_motion",
}
SIZE_RE = re.compile(r"^size\(\s*(\d+)\s*,\s*(\d+)\s*\)$")
SOUND_RE = re.compile(r"p5\.AudioIn|p5\.FFT|p5\.Oscillator|loadSound")

# Frame budget.  60 virtual frames per virtual second.
PRIME_FRAMES = 2           # let the sketch render its first frame before t0.
                           # Without this the t0 snapshot is the blank canvas
                           # and EVERY sketch looks like it moved, which would
                           # pass motion(idle) on a sketch that draws once and
                           # then freezes -- the bad-frozen case.
IDLE_FRAMES = 120          # ~2.0 virtual seconds of "idle"
STRIP_AT = (0, 42, 84)     # 0 s, ~0.7 s, ~1.4 s
PROBE_FRAMES = 30          # frames stepped after a synthesised gesture
AUDIO_DWELL_STEPS = 30     # audio needs real time: 30 x (2 frames + 50 ms)
AUDIO_DWELL_WAIT_MS = 50
# responds(audio) is decided by a pixel-wise comparison against a silent
# reference run, not by a brightness magnitude.  Brightness measured how LOUD
# the sketch's response was: the week 7 sketch, correctly fixed, moved the mean
# by 1.0 on 0-255 and would have failed a threshold of 8, while a sketch whose
# background is the mic level moves it by 254.  The determinism this runner
# installs is what makes the strict comparison sound: with the seeds fixed and
# the clock virtual, the tone run and the silent run differ only by audio.
AUDIO_DIFF_MEAN = 1.0        # mean abs per-channel difference, 0-255
AUDIO_DIFF_FRACTION = 0.02   # or this fraction of pixels changed at all
VIEWPORT = {"width": 1280, "height": 900}

# ---------------------------------------------------------------------------
# The frame budget (job 166, 2026-09-15)
# ---------------------------------------------------------------------------
# Two numbers, both overridable, both chosen from the 371 gate reports the node
# held on 2026-09-15 rather than from taste.
#
# DEFAULT_FRAME_BUDGET_MS = 100.  Per-frame cost derived from those reports --
# (total_s - launch_s - load_s - settle) / frames stepped -- has a median of
# 12.0 ms, and about 3.3 ms of that is this runner's own fixed overhead spread
# over the window, so the median sketch really costs something like 9 ms a
# frame and clears 100 ms by more than ten times.  The tail is the point: 46 of
# 371 attempts derive above 100 ms, and the top of that list is job 45 (3,311
# ms), job 270 attempt 1 (1,867 ms) and job 166 attempt 3 (1,370 ms) -- the
# three the incident reports are about.  100 ms is also 3 frames at 30 fps: a
# sketch over it is not slow, it has stopped being animation.
#
# DEFAULT_BUDGET_S = 90.  The wall ceiling, measured from the start of the run,
# so that a 284 s pass cannot happen again whatever the per-frame number says.
# Eight of the 371 attempts ran longer than 90 s and all eight are sketches this
# check exists to stop; the median run is 2.4 s, so the ceiling is forty times
# the ordinary case.  It is deliberately far below the worker's own 600 s
# subprocess timeout: a run killed out there writes no report.json at all, and
# the evidence the next attempt got from that was "gate did not finish", a
# sentence with no cause in it.
DEFAULT_FRAME_BUDGET_MS = 100.0
DEFAULT_BUDGET_S = 90.0

#: Trip early, without finishing the window, once the running mean is this many
#: times the budget.  Twice: a sketch whose first chunk costs 2x the budget is
#: not going to be rescued by its ninety-first frame, and stepping the rest of
#: the window at a second a frame is exactly the two minutes the ceiling exists
#: to refuse.  Below 2x the window is always finished, because the check is
#: about the mean over the window and a single expensive first frame (a texture
#: upload, a geometry built late) is not the bug being looked for.
EARLY_TRIP_FACTOR = 2.0

#: What the evidence tells the executor to do about it.  One clause per thing
#: that has actually caused this, in the order they cost the most.
FRAME_BUDGET_REMEDY = ("batch points and lines into one shape, no sphere() per "
                       "particle, no all-pairs loop")

# The sketch is served from a fake origin fulfilled entirely by a page.route()
# handler reading the sketch directory from disk.  Nothing binds a port.  The
# origin has to be one Chromium treats as potentially trustworthy: under file://
# p5.sound cannot load its AudioWorklet and throws, which would turn the
# audio-without-a-gesture bug into a console error instead of a suspended
# AudioContext -- the gate would misreport the one bug it exists to catch.
# *.localhost is potentially trustworthy, and the route handler means the name
# is never resolved and no socket is ever opened.
SKETCH_ORIGIN = "http://sketch.localhost"

# is_looping is a declaration the sketch makes about itself, not a verdict, so
# it never fails a run on its own (spec 3.1: a sketch that calls noLoop() is
# declaring itself static and the gate believes it).
FAILABLE_CHECKS = ("console_clean", "frame_advancing", "sound_lib_ok",
                   "audio_context_running", "frame_budget")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def tilde(path):
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path


def refuse(msg):
    sys.stderr.write("sketch_gate: refused -- %s\n" % msg)
    sys.exit(3)


# ---------------------------------------------------------------------------
# the in-page harness
# ---------------------------------------------------------------------------

INIT_JS = r"""
(() => {
  const SEED = __SEED__;
  const FRAME_MS = 1000 / 60;
  const ORIGIN = 1600000000000;   // fixed epoch: 2020-09-13T12:26:40Z

  const g = {
    vnow: 0, frames: 0, seeded: false, inst: null,
    notes: [], queue: [], snaps: {}, _scratch: null, _nextId: 1, _dead: {}
  };
  window.__gate = g;

  // ---- 1. virtual clock --------------------------------------------------
  const RealDate = Date;
  const vms = () => ORIGIN + g.vnow;
  window.Date = new Proxy(RealDate, {
    construct(t, a) { return a.length ? new t(...a) : new t(vms()); },
    apply() { return new RealDate(vms()).toString(); },
    get(t, p, r) { return p === 'now' ? (() => vms()) : Reflect.get(t, p, r); }
  });
  try {
    Object.defineProperty(performance, 'now',
      { value: () => g.vnow, configurable: true, writable: true });
  } catch (e) { g.notes.push('performance.now not frozen: ' + e.message); }

  // ---- 2. frame stepping -------------------------------------------------
  window.requestAnimationFrame = function (cb) {
    const id = g._nextId++;
    g.queue.push([id, cb]);
    return id;
  };
  window.cancelAnimationFrame = function (id) { g._dead[id] = true; };
  window.webkitRequestAnimationFrame = window.requestAnimationFrame;
  window.webkitCancelAnimationFrame = window.cancelAnimationFrame;

  g.step = function (n) {
    n = n || 1;
    for (let i = 0; i < n; i++) {
      g.vnow += FRAME_MS;
      const q = g.queue; g.queue = [];
      for (const [id, cb] of q) {
        if (g._dead[id]) { delete g._dead[id]; continue; }
        try { cb(g.vnow); }
        catch (err) { setTimeout(() => { throw err; }, 0); }   // stay uncaught
      }
      g.frames++;
    }
    return g.frames;
  };

  // ---- 3. seeded randomness ---------------------------------------------
  let mr = SEED >>> 0;
  Math.random = function () {
    mr = (mr + 0x6D2B79F5) | 0;
    let t = Math.imul(mr ^ (mr >>> 15), 1 | mr);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  function seed(target) {
    if (g.seeded) return;
    try {
      if (typeof target.randomSeed === 'function') target.randomSeed(SEED);
      if (typeof target.noiseSeed === 'function') target.noiseSeed(SEED);
      g.seeded = true;
    } catch (e) { g.notes.push('seeding failed: ' + e.message); }
  }

  // p5 1.11 moved _start/_setup/_draw from the prototype onto the instance as
  // class fields, so there is nothing left on p5.prototype to wrap, and by the
  // time the one prototype method the constructor still calls runs
  // (_initializeInstanceVariables) those fields have not been assigned yet
  // (measured: this._setup is undefined there).  So this hook does two smaller
  // things it can do reliably: it captures the instance, which is what the
  // fixed checks read; and it installs an accessor for `setup` on the instance,
  // which is how an instance-mode sketch hands p5 its setup function -- a plain
  // assignment, so the accessor survives it.
  function patchP5(P) {
    if (!P || !P.prototype || P.__gatePatched) return;
    const init = P.prototype._initializeInstanceVariables;
    if (typeof init !== 'function') return;      // try again on a later assignment
    P.__gatePatched = true;
    P.prototype._initializeInstanceVariables = function () {
      const r = init.apply(this, arguments);
      g.inst = this;
      const self = this;
      let userSetup = null, wrapped = null;
      try {
        Object.defineProperty(this, 'setup', {
          configurable: true,
          get() { return wrapped; },
          set(fn) {
            userSetup = fn;
            wrapped = (typeof fn === 'function')
              ? function () { seed(self); g.hook = 'p5 instance setup'; return userSetup.apply(this, arguments); }
              : fn;
          }
        });
      } catch (e) { g.notes.push('instance setup accessor failed: ' + e.message); }
      return r;
    };
  }
  let _p5;
  try {
    Object.defineProperty(window, 'p5', {
      configurable: true,
      get() { return _p5; },
      set(v) { _p5 = v; try { patchP5(v); } catch (e) { g.notes.push('patch failed: ' + e.message); } }
    });
  } catch (e) { g.notes.push('could not hook window.p5: ' + e.message); }

  // Fallback hook, global mode only: sketch.js has already declared setup() by
  // DOMContentLoaded, and p5 only calls it from its own load handler, which is
  // registered after this script ran -- so wrapping it here still lands before
  // the sketch's setup() body.
  function wrapGlobalSetup() {
    if (g._wrappedSetup) return;
    const user = window.setup;
    if (typeof user !== 'function') return;
    g._wrappedSetup = true;
    window.setup = function () {
      seed(window);
      return user.apply(this, arguments);
    };
    if (!g.hook) g.hook = 'window.setup';
  }
  document.addEventListener('DOMContentLoaded', wrapGlobalSetup, true);
  window.addEventListener('load', wrapGlobalSetup, true);

  // ---- 4. readable webgl -------------------------------------------------
  const realGetContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, attrs) {
    if (/webgl/i.test(String(type))) {
      attrs = Object.assign({}, attrs || {}, { preserveDrawingBuffer: true });
    }
    return realGetContext.call(this, type, attrs);
  };

  // ---- canvas access, snapshots, in-page frame difference ---------------
  g.canvas = function () {
    const cs = document.querySelectorAll('canvas');
    for (const c of cs) if (c.id === 'defaultCanvas0') return c;
    return cs.length ? cs[0] : null;
  };
  g.imageData = function () {
    const c = g.canvas();
    if (!c || !c.width || !c.height) return null;
    if (!g._scratch) g._scratch = document.createElement('canvas');
    const s = g._scratch;
    s.width = c.width; s.height = c.height;
    const x = s.getContext('2d', { willReadFrequently: true });
    x.clearRect(0, 0, s.width, s.height);
    try { x.drawImage(c, 0, 0); } catch (e) { return null; }
    try { return x.getImageData(0, 0, s.width, s.height); } catch (e) { return null; }
  };
  g.snap = function (name) {
    const d = g.imageData();
    if (!d) return false;
    g.snaps[name] = d;
    return true;
  };
  g.diff = function (a, b) {
    const A = g.snaps[a], B = g.snaps[b];
    if (!A || !B) return null;
    if (A.data.length !== B.data.length) return { mean: 255, changed: -1, total: -1, resized: true };
    let sum = 0, changed = 0;
    const da = A.data, db = B.data, n = da.length;
    for (let i = 0; i < n; i += 4) {
      const d0 = Math.abs(da[i] - db[i]);
      const d1 = Math.abs(da[i + 1] - db[i + 1]);
      const d2 = Math.abs(da[i + 2] - db[i + 2]);
      sum += d0 + d1 + d2;
      if (d0 || d1 || d2) changed++;
    }
    const px = n / 4;
    return { mean: sum / (px * 3), changed: changed, total: px };
  };
  g.rgba = function (name) {
    const A = g.snaps[name];
    if (!A) return null;
    const d = A.data;
    let s = '';
    const CH = 0x8000;                       // chunked: apply() has an arg limit
    for (let i = 0; i < d.length; i += CH) {
      s += String.fromCharCode.apply(null, d.subarray(i, Math.min(i + CH, d.length)));
    }
    return { w: A.width, h: A.height, b64: btoa(s) };
  };
  g.diffExternal = function (name, w, h, b64) {
    const A = g.snaps[name];
    if (!A) return null;
    if (A.width !== w || A.height !== h) {
      return { mismatch: true, aw: A.width, ah: A.height, bw: w, bh: h };
    }
    const bin = atob(b64);
    const da = A.data, n = da.length;
    if (bin.length !== n) return { mismatch: true, aw: n, ah: 0, bw: bin.length, bh: 0 };
    let sum = 0, changed = 0;
    for (let i = 0; i < n; i += 4) {
      const d0 = Math.abs(da[i] - bin.charCodeAt(i));
      const d1 = Math.abs(da[i + 1] - bin.charCodeAt(i + 1));
      const d2 = Math.abs(da[i + 2] - bin.charCodeAt(i + 2));
      sum += d0 + d1 + d2;
      if (d0 || d1 || d2) changed++;
    }
    const px = n / 4;
    return { mean: sum / (px * 3), changed: changed, total: px };
  };
  g.brightness = function (name) {
    const A = g.snaps[name];
    if (!A) return null;
    let sum = 0;
    const d = A.data, n = d.length;
    for (let i = 0; i < n; i += 4) sum += (d[i] + d[i + 1] + d[i + 2]) / 3;
    return sum / (n / 4);
  };
  g.png = function () {
    const c = g.canvas();
    if (!c) return null;
    try { return c.toDataURL('image/png'); } catch (e) { return null; }
  };
  g.strip = function (names) {
    const parts = names.map(n => g.snaps[n]).filter(Boolean);
    if (!parts.length) return null;
    const h = Math.max.apply(null, parts.map(p => p.height));
    const gap = 4;
    const w = parts.reduce((a, p) => a + p.width, 0) + gap * (parts.length - 1);
    const s = document.createElement('canvas');
    s.width = w; s.height = h;
    const x = s.getContext('2d');
    x.fillStyle = '#000'; x.fillRect(0, 0, w, h);
    let ox = 0;
    for (const p of parts) {
      const t = document.createElement('canvas');
      t.width = p.width; t.height = p.height;
      t.getContext('2d').putImageData(p, 0, 0);
      x.drawImage(t, ox, 0);
      ox += p.width + gap;
    }
    try { return s.toDataURL('image/png'); } catch (e) { return null; }
  };

  // ---- runtime state the fixed checks read ------------------------------
  g.state = function () {
    const inst = g.inst || (window.p5 && window.p5.instance) || null;
    const out = { frameCount: null, isLooping: null, hasCanvas: !!g.canvas(),
                  width: null, height: null, canvasW: null, canvasH: null,
                  webgl: null, seeded: g.seeded, hook: g.hook || null,
                  notes: g.notes.slice() };
    const c = g.canvas();
    if (c) { out.canvasW = c.width; out.canvasH = c.height; }
    const src = inst || (typeof window.frameCount === 'number' ? window : null);
    if (src) {
      if (typeof src.frameCount === 'number') out.frameCount = src.frameCount;
      try { if (typeof src.isLooping === 'function') out.isLooping = !!src.isLooping(); } catch (e) {}
      if (typeof src.width === 'number') out.width = src.width;
      if (typeof src.height === 'number') out.height = src.height;
    }
    let webgl = null;
    const r = inst && inst._renderer;
    if (r) {
      const dc = r.drawingContext;
      webgl = !!(r.GL
        || (window.WebGLRenderingContext && dc instanceof WebGLRenderingContext)
        || (window.WebGL2RenderingContext && dc instanceof WebGL2RenderingContext));
    } else if (c) {
      try { webgl = !!(c.getContext('webgl2') || c.getContext('webgl')); } catch (e) { webgl = null; }
    }
    out.webgl = webgl;
    return out;
  };
  g.audio = function () {
    const out = { hasAudioIn: typeof (window.p5 && window.p5.AudioIn) === 'function',
                  hasGetAudioContext: typeof window.getAudioContext === 'function',
                  state: null };
    if (out.hasGetAudioContext) {
      try { const ac = window.getAudioContext(); out.state = ac ? ac.state : null; }
      catch (e) { out.state = 'error: ' + e.message; }
    }
    return out;
  };
})();
"""


# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        prog="sketch_gate.py",
        description="Run one p5.js sketch directory through the deterministic gate.",
        epilog="assertion vocabulary: " + ", ".join(VOCAB),
    )
    ap.add_argument("sketch_dir", help="directory holding index.html and sketch.js")
    ap.add_argument("--assert", dest="assertions", action="append", default=[],
                    metavar="WORD", help="assertion to evaluate; repeatable")
    ap.add_argument("--seed", type=int, default=1, help="seed for random()/noise() (default 1)")
    ap.add_argument("--out", default=None, help="artefact directory (default <sketch_dir>/.gate)")
    ap.add_argument("--timeout", type=float, default=60.0, help="seconds for page load (default 60)")
    ap.add_argument("--frame-budget-ms", type=float, default=DEFAULT_FRAME_BUDGET_MS,
                    metavar="MS",
                    help="wall milliseconds one stepped frame of the idle window may "
                         "cost before frame_budget fails (default %g)"
                         % DEFAULT_FRAME_BUDGET_MS)
    ap.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S, metavar="S",
                    help="wall seconds the whole run may take before it stops where it "
                         "is and fails frame_budget (default %g)" % DEFAULT_BUDGET_S)
    ap.add_argument("--json", action="store_true", help="print report.json to stdout as well")
    ap.add_argument("--keep-browser-log", action="store_true",
                    help="also write <out>/browser.log: the chromium build, the launch "
                         "arguments, and every request, response and network failure")
    return ap.parse_args(argv)


def normalise_assertions(words):
    """Return [(literal, kind, params)] or refuse on an unknown word."""
    out = []
    for raw in words:
        w = raw.strip()
        if w in SIMPLE_ASSERTIONS:
            out.append((w, w, None))
            continue
        m = SIZE_RE.match(w)
        if m:
            out.append((w, "size", (int(m.group(1)), int(m.group(2)))))
            continue
        refuse("unknown assertion %r; vocabulary: %s" % (raw, ", ".join(VOCAB)))
    return out


# ---------------------------------------------------------------------------
# WAV generation (stdlib only)
# ---------------------------------------------------------------------------

def write_wav(path, seconds=3.0, freq=440.0, rate=44100, amplitude=0.6):
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        v = 0 if freq is None else amplitude * math.sin(2.0 * math.pi * freq * i / rate)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

class BrowserLog:
    """What --keep-browser-log can actually keep.

    Chromium's own --log-file is ignored when Playwright launches it (measured:
    the file is never created, at any verbosity), so instead of pretending, this
    records the browser build, the exact launch arguments, and every request,
    response and failure the page made -- which is what a browser log is wanted
    for when a sketch's CDN fetch or audio worklet quietly does not arrive.
    """

    def __init__(self):
        self.lines = []

    def attach(self, browser, page, args):
        self.lines.append("%s  browser   chromium %s" % (utc_now(), browser.version))
        for arg in args:
            self.lines.append("%s  arg       %s" % (utc_now(), arg))
        page.on("request", lambda r: self.lines.append(
            "%s  request   %s %s (%s)" % (utc_now(), r.method, r.url, r.resource_type)))
        page.on("response", lambda r: self.lines.append(
            "%s  response  %s %s" % (utc_now(), r.status, r.url)))
        page.on("requestfailed", lambda r: self.lines.append(
            "%s  failed    %s %s" % (utc_now(), r.url, r.failure)))

    def write(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# chromium activity as seen from Playwright, timestamps UTC\n")
            for line in self.lines:
                fh.write(line + "\n")
        return path


class Recorder:
    """Collects console messages and page errors with UTC timestamps."""

    def __init__(self):
        self.entries = []

    def attach(self, page):
        page.on("console", self._console)
        page.on("pageerror", self._pageerror)

    def _console(self, msg):
        try:
            text = msg.text
        except Exception:
            text = "<unreadable console message>"
        self.entries.append({"t": utc_now(), "type": msg.type, "text": text})

    def _pageerror(self, err):
        try:
            text = str(err)
        except Exception:
            text = "<unreadable page error>"
        self.entries.append({"t": utc_now(), "type": "pageerror", "text": text})

    @property
    def clean(self):
        return not any(e["type"] in ("error", "pageerror") for e in self.entries)


def launch_args(fake_audio_wav):
    # The autoplay policy is pinned to the browser default on purpose: an
    # AudioContext must be resumed by a real user gesture.  That requirement is
    # the whole point of the audio_context_running check.
    args = [
        "--use-fake-device-for-media-stream",
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=document-user-activation-required",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
    ]
    if fake_audio_wav:
        args.append("--use-file-for-fake-audio-capture=%s" % fake_audio_wav)
    return args


def serve_from_disk(page, sketch_dir):
    """Fulfil every request under SKETCH_ORIGIN from the sketch directory.

    This is a page.route() interception, not a server: no socket is opened and
    the hostname is never resolved.  Requests to anywhere else -- the p5 CDN --
    are not matched by the glob and go to the network untouched.
    """
    root = pathlib.Path(sketch_dir).resolve()

    def handler(route, request):
        rel = unquote(urlsplit(request.url).path).lstrip("/") or "index.html"
        target = (root / rel).resolve()
        try:
            inside = target.is_relative_to(root)
        except AttributeError:                      # pragma: no cover (py<3.9)
            inside = str(target).startswith(str(root))
        if not inside or not target.is_file():
            route.fulfill(status=404, content_type="text/plain", body="not found")
            return
        route.fulfill(status=200, path=str(target))

    page.route(SKETCH_ORIGIN + "/**", handler)


def open_page(pw, init_js, fake_audio_wav, net_log, timeout_ms, sketch_dir):
    t0 = time.time()
    args = launch_args(fake_audio_wav)
    browser = pw.chromium.launch(headless=True, args=args)
    launch_s = time.time() - t0
    context = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
    try:
        context.grant_permissions(["microphone"], origin=SKETCH_ORIGIN)
    except Exception:
        pass
    context.set_default_timeout(timeout_ms)
    page = context.new_page()
    page.add_init_script(init_js)
    serve_from_disk(page, sketch_dir)
    if net_log is not None:
        net_log.attach(browser, page, args)
    return browser, context, page, launch_s


def load_sketch(page, timeout_ms):
    t0 = time.time()
    page.goto(SKETCH_ORIGIN + "/index.html", wait_until="load", timeout=timeout_ms)
    try:
        page.wait_for_function("() => !!(window.__gate && window.__gate.canvas())",
                               timeout=min(timeout_ms, 10000))
    except Exception:
        pass
    return time.time() - t0


class BudgetExceeded(Exception):
    """The run cost more than it was allowed to.  Carries its own sentence.

    Raised out of :func:`step`, caught once in :func:`main`, which stops the run
    where it stands and reports ``frame_budget`` false.  The message is the
    evidence line the worker feeds to REPAIR, so it names the measured number
    and the budget it broke.
    """


class Budget:
    """What one run is allowed to cost, and what it has cost so far.

    Two independent limits, one object, because a sketch can break either:
    ``frame_budget_ms`` is the mean wall cost of a stepped frame over the idle
    window, and ``deadline`` is the wall clock for the whole run.  Only the idle
    window is *counted* (``counting`` is turned on for it and off again); every
    step in the run is *checked* against the deadline, including the probes
    after it, so a sketch that is cheap to idle and ruinous to click cannot slip
    past either.
    """

    def __init__(self, frame_budget_ms, budget_s, started):
        self.frame_budget_ms = float(frame_budget_ms)
        self.budget_s = float(budget_s)
        self.started = started
        self.deadline = started + float(budget_s)
        self.counting = False
        self.frames = 0
        self.seconds = 0.0
        self.window_frames = IDLE_FRAMES

    @property
    def ms_per_frame(self):
        """The mean so far, or None before a single frame has been stepped."""
        if not self.frames:
            return None
        return round(self.seconds * 1000.0 / self.frames, 1)

    def _where(self):
        if self.frames >= self.window_frames:
            return "over the %d-frame idle window" % self.window_frames
        return ("over the first %d frames of the %d-frame idle window"
                % (self.frames, self.window_frames))

    def sentence(self, over_wall=False):
        """The one line that goes in the report's notes and in the evidence."""
        rate = self.ms_per_frame
        if over_wall:
            head = ("frame_budget: the run passed its %g s wall ceiling %s"
                    % (self.budget_s, self._where()))
            if rate is not None:
                head += " (%g ms per frame)" % rate
            return "%s, budget %g ms — %s" % (
                head, self.frame_budget_ms, FRAME_BUDGET_REMEDY)
        return "frame_budget: %g ms per frame %s, budget %g ms — %s" % (
            rate, self._where(), self.frame_budget_ms, FRAME_BUDGET_REMEDY)

    def record(self, frames, seconds):
        """Add one chunk's cost, then decide whether the run may go on."""
        if self.counting:
            self.frames += frames
            self.seconds += seconds
        if time.time() > self.deadline:
            raise BudgetExceeded(self.sentence(over_wall=True))
        rate = self.ms_per_frame
        if (self.counting and rate is not None
                and self.frames < self.window_frames
                and rate > self.frame_budget_ms * EARLY_TRIP_FACTOR):
            raise BudgetExceeded(self.sentence())

    def verdict(self):
        """(passed, sentence) at the end of a window that finished normally."""
        rate = self.ms_per_frame
        if rate is None:
            return None, None
        if rate > self.frame_budget_ms:
            return False, self.sentence()
        return True, ("frame_budget: %g ms per frame %s, budget %g ms"
                      % (rate, self._where(), self.frame_budget_ms))


def step(page, n, chunk=30, settle_ms=25, budget=None):
    """Advance n virtual frames, in chunks, letting real microtasks settle.

    Returns the wall seconds spent inside ``__gate.step()`` -- the settle waits
    are the runner's own and are deliberately not in the number.  With a
    ``budget``, each chunk is charged to it and may raise :class:`BudgetExceeded`
    between chunks; a chunk is the finest grain there is, because the evaluate
    that runs it cannot be interrupted from here.
    """
    done = 0
    spent = 0.0
    while done < n:
        k = min(chunk, n - done)
        t0 = time.time()
        page.evaluate("n => window.__gate.step(n)", k)
        took = time.time() - t0
        spent += took
        done += k
        if budget is not None:
            budget.record(k, took)
        if settle_ms:
            page.wait_for_timeout(settle_ms)
    return spent


def click_centre(page):
    box = page.evaluate(
        "() => { const c = window.__gate.canvas(); if (!c) return null;"
        " const r = c.getBoundingClientRect();"
        " return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height}; }")
    if not box:
        return None
    page.mouse.click(box["x"], box["y"])
    return box


def drag_across(page):
    box = page.evaluate(
        "() => { const c = window.__gate.canvas(); if (!c) return null;"
        " const r = c.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; }")
    if not box:
        return None
    x0 = box["x"] + box["w"] * 0.2
    y0 = box["y"] + box["h"] * 0.5
    x1 = box["x"] + box["w"] * 0.8
    y1 = box["y"] + box["h"] * 0.5
    page.mouse.move(x0, y0)
    page.mouse.down()
    for i in range(1, 9):
        page.mouse.move(x0 + (x1 - x0) * i / 8.0, y0 + (y1 - y0) * i / 8.0)
        page.evaluate("() => window.__gate.step(2)")
    page.mouse.up()
    return box


def audio_dwell(page):
    """Let real time pass so the fake microphone actually reaches the sketch."""
    for _ in range(AUDIO_DWELL_STEPS):
        page.evaluate("() => window.__gate.step(2)")
        page.wait_for_timeout(AUDIO_DWELL_WAIT_MS)


def save_data_url(data_url, path):
    if not data_url or "," not in data_url:
        return False
    try:
        raw = base64.b64decode(data_url.split(",", 1)[1])
    except (binascii.Error, ValueError):
        return False
    with open(path, "wb") as fh:
        fh.write(raw)
    return True


def silent_reference_run(pw, init_js, wav, timeout_ms, sketch_dir):
    """Second launch, silent fake microphone, same script.

    Returns the canvas at the same point the tone run snaps it: the raw RGBA as
    base64 plus its size and mean brightness.  The pixels are what decides
    responds(audio); the brightness is kept for the human reading the detail.
    """
    browser = context = None
    try:
        browser, context, page, _ = open_page(pw, init_js, wav, None, timeout_ms, sketch_dir)
        load_sketch(page, timeout_ms)
        step(page, IDLE_FRAMES)
        click_centre(page)
        step(page, PROBE_FRAMES)
        audio_dwell(page)
        page.evaluate("() => window.__gate.snap('audio')")
        out = page.evaluate("() => window.__gate.rgba('audio')")
        if out is not None:
            out["brightness"] = page.evaluate("() => window.__gate.brightness('audio')")
        return out
    finally:
        for obj in (context, browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass


def main(argv=None):
    a = parse_args(argv if argv is not None else sys.argv[1:])
    started = utc_now()
    t_start = time.time()

    sketch_dir = pathlib.Path(a.sketch_dir).expanduser()
    if not sketch_dir.is_dir():
        refuse("%s is not a directory" % a.sketch_dir)
    index = sketch_dir / "index.html"
    sketch_js = sketch_dir / "sketch.js"
    if not index.is_file():
        refuse("%s has no index.html" % a.sketch_dir)
    if not sketch_js.is_file():
        refuse("%s has no sketch.js" % a.sketch_dir)

    wanted = normalise_assertions(a.assertions)
    want_audio = any(kind == "responds(audio)" for _, kind, _ in wanted)
    want_drag = any(kind == "responds(drag)" for _, kind, _ in wanted)

    out_dir = pathlib.Path(a.out).expanduser() if a.out else sketch_dir / ".gate"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        refuse("cannot create --out %s (%s)" % (out_dir, e))

    timeout_ms = int(a.timeout * 1000)

    # source scan for the sound library, before any browser work
    source = ""
    for p in sorted(sketch_dir.glob("*.html")) + sorted(sketch_dir.glob("*.js")):
        try:
            source += p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    references_sound = bool(SOUND_RE.search(source))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        refuse("playwright is not importable; activate the venv that has it")

    init_js = INIT_JS.replace("__SEED__", str(int(a.seed)))
    notes = ["served from %s/ by a page.route() handler reading the sketch "
             "directory from disk; no socket was opened, and the origin is "
             "potentially trustworthy so p5.sound's AudioWorklet loads"
             % SKETCH_ORIGIN]
    checks = {
        "console_clean": None,
        "is_looping": None,
        "frame_advancing": None,
        "sound_lib_ok": None,
        "audio_context_running": None,
        "frame_budget": None,
    }
    assertions = {}
    rec = Recorder()
    timings = {"launch_s": None, "load_s": None, "total_s": None,
               "ms_per_frame": None, "idle_step_s": None}
    budget = Budget(a.frame_budget_ms, a.budget_s, t_start)
    chromium_path = None

    tone_wav = None
    if want_audio:
        tone_wav = out_dir / "tone-440.wav"
        write_wav(tone_wav, seconds=3.0, freq=440.0)
        notes.append("responds(audio): chromium launched with a fake microphone fed "
                     "from tone-440.wav (3 s, 440 Hz, mono, 16-bit, 44.1 kHz)")
    net_log = BrowserLog() if a.keep_browser_log else None

    with sync_playwright() as pw:
        try:
            chromium_path = tilde(pw.chromium.executable_path)
        except Exception:
            chromium_path = None
        browser = context = None
        try:
            try:
                browser, context, page, launch_s = open_page(
                    pw, init_js, tone_wav, net_log, timeout_ms, sketch_dir)
            except Exception as e:
                refuse("could not launch chromium (%s)" % e)
            timings["launch_s"] = round(launch_s, 3)
            rec.attach(page)

            try:
                load_s = load_sketch(page, timeout_ms)
            except Exception as e:
                notes.append("page load did not complete: %s" % e)
                load_s = time.time() - t_start
            timings["load_s"] = round(load_s, 3)

            try:
                state0 = page.evaluate("() => window.__gate.state()")
                notes.extend(state0.get("notes") or [])
                if not state0.get("hasCanvas"):
                    notes.append("no canvas was found in the page")
                if state0.get("seeded"):
                    notes.append("randomSeed(%d)/noiseSeed(%d) ran before setup(), via %s"
                                 % (a.seed, a.seed, state0.get("hook")))
                else:
                    notes.append("randomSeed/noiseSeed were NOT installed before setup(); "
                                 "p5 was not detected, so this run is not seed-deterministic")
                frame0 = state0.get("frameCount")

                step(page, PRIME_FRAMES, budget=budget)
                page.evaluate("() => window.__gate.snap('t0')")

                # ~2 virtual seconds of idle, with strip frames along the way.
                # This is the window the frame budget is measured over, and the
                # only stretch charged to it: the priming frames are the
                # sketch's first draw and the probes are a different question.
                # The snapshots between the three calls are the runner's cost,
                # not the sketch's, so they sit outside the timed stepping.
                budget.counting = True
                idle_s = step(page, STRIP_AT[1] - STRIP_AT[0], budget=budget)
                page.evaluate("() => window.__gate.snap('t07')")
                idle_s += step(page, STRIP_AT[2] - STRIP_AT[1], budget=budget)
                page.evaluate("() => window.__gate.snap('t14')")
                state_mid = page.evaluate("() => window.__gate.state()")
                frame_mid = state_mid.get("frameCount")
                idle_s += step(page, IDLE_FRAMES - STRIP_AT[2], budget=budget)
                budget.counting = False
                timings["idle_step_s"] = round(idle_s, 3)
                timings["ms_per_frame"] = budget.ms_per_frame
                passed_budget, budget_note = budget.verdict()
                checks["frame_budget"] = passed_budget
                if budget_note:
                    notes.append(budget_note)
                page.evaluate("() => window.__gate.snap('t20')")

                state1 = page.evaluate("() => window.__gate.state()")
                frame_end = state1.get("frameCount")

                # is_looping is read at the END of the idle window, not at load.
                # noLoop() at the end of the first draw() is a legitimate p5 idiom,
                # and a declaration made during the first frame still counts.
                checks["is_looping"] = state1.get("isLooping")

                if checks["is_looping"] is False:
                    checks["frame_advancing"] = None
                    notes.append("frame_advancing skipped: the sketch calls noLoop(), so it "
                                 "declares itself static (spec 3.1, closes MEASURE[gate-false-negatives])")
                elif frame_mid is None or frame_end is None:
                    checks["frame_advancing"] = None
                    notes.append("frame_advancing unknown: frameCount was not readable")
                else:
                    # "Still advancing in the second half of the idle window", not
                    # "advanced at all since load".  A sketch that throws at frame 11
                    # kills its own draw loop, and frameCount 0 -> 11 would otherwise
                    # read as advancing.
                    checks["frame_advancing"] = frame_end > frame_mid
                    notes.append("frame_advancing compares frameCount at frame %d of the "
                                 "%d-frame idle window (%s) with the end of it (%s); at load "
                                 "it was %s"
                                 % (STRIP_AT[2], IDLE_FRAMES, frame_mid, frame_end, frame0))

                gate_png = out_dir / "gate.png"
                if not save_data_url(page.evaluate("() => window.__gate.png()"), gate_png):
                    notes.append("gate.png could not be produced from the canvas")

                # the click probe: a real chromium input event, so it is a user gesture
                click_box = click_centre(page)
                if click_box is None:
                    notes.append("click probe skipped: no canvas to click")
                step(page, PROBE_FRAMES, budget=budget)
                page.evaluate("() => window.__gate.snap('click')")

                strip_png = out_dir / "strip.png"
                if not save_data_url(
                        page.evaluate("() => window.__gate.strip(['t0','t07','t14','click'])"),
                        strip_png):
                    notes.append("strip.png could not be produced from the canvas")

                # ---- sound library and audio context ---------------------------
                audio = page.evaluate("() => window.__gate.audio()")
                if not references_sound:
                    checks["sound_lib_ok"] = None
                    checks["audio_context_running"] = None
                else:
                    checks["sound_lib_ok"] = bool(audio.get("hasAudioIn"))
                    if not audio.get("hasGetAudioContext"):
                        checks["audio_context_running"] = None
                        notes.append("audio_context_running is null: the source references the "
                                     "p5.sound API but getAudioContext() does not exist, so the "
                                     "addon is not loaded")
                    else:
                        checks["audio_context_running"] = (audio.get("state") == "running")
                        notes.append("AudioContext state after the click probe: %s"
                                     % audio.get("state"))

                # ---- assertions ------------------------------------------------
                if want_drag:
                    if drag_across(page) is None:
                        notes.append("drag probe skipped: no canvas to drag across")
                    step(page, PROBE_FRAMES, budget=budget)
                    page.evaluate("() => window.__gate.snap('drag')")

                tone_brightness = None
                silent_ref = None
                if want_audio:
                    audio_dwell(page)
                    page.evaluate("() => window.__gate.snap('audio')")
                    tone_brightness = page.evaluate("() => window.__gate.brightness('audio')")
                    silent_wav = out_dir / "silence.wav"
                    write_wav(silent_wav, seconds=3.0, freq=None)
                    try:
                        silent_ref = silent_reference_run(
                            pw, init_js, silent_wav, timeout_ms, sketch_dir)
                    except Exception as e:
                        notes.append("silent reference run failed: %s" % e)
                    if silent_ref:
                        notes.append("responds(audio) compares the tone run with the silent "
                                     "reference run pixel by pixel; the two runs are identical "
                                     "except for the microphone because the seeds and the clock "
                                     "are fixed")

                for literal, kind, params in wanted:
                    assertions[literal] = evaluate_assertion(
                        page, kind, params, state1, tone_brightness, silent_ref)
            except BudgetExceeded as exc:
                # Stop where we stand.  Everything measured up to here is kept
                # -- the checks already decided, the artefacts already written,
                # the console lines already recorded -- because a report that
                # names the cost is the whole point of stopping.  An assertion
                # that never got to run stays absent rather than failing: it was
                # not evaluated, and saying it failed would be a second claim
                # this run cannot support.
                checks["frame_budget"] = False
                timings["ms_per_frame"] = budget.ms_per_frame
                timings["idle_step_s"] = round(budget.seconds, 3)
                notes.append(str(exc))
                notes.append(
                    "frame_budget: the run stopped at %d of the %d idle frames and "
                    "did not finish the probes or the assertions; the artefacts on "
                    "disk are of the frames it did step"
                    % (budget.frames, IDLE_FRAMES))

            # console cleanliness is judged over the whole run
            checks["console_clean"] = rec.clean

        finally:
            for obj in (context, browser):
                try:
                    if obj:
                        obj.close()
                except Exception:
                    pass

    timings["total_s"] = round(time.time() - t_start, 3)

    # is_looping is deliberately absent from FAILABLE_CHECKS: a sketch that
    # calls noLoop() declares itself static and the gate believes it.
    failed = any(checks[k] is False for k in FAILABLE_CHECKS) \
        or any(not v["pass"] for v in assertions.values())
    code = 1 if failed else 0

    log_path = out_dir / "console.log"
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("# sketch_gate console log, timestamps UTC\n")
        for e in rec.entries:
            fh.write("%s  %-9s %s\n" % (e["t"], e["type"], e["text"].replace("\n", "\\n")))

    artefacts = {
        "png": str(out_dir / "gate.png"),
        "strip": str(out_dir / "strip.png"),
        "log": str(log_path),
    }
    if net_log is not None:
        artefacts["browser_log"] = str(net_log.write(out_dir / "browser.log"))

    report = {
        "sketch_dir": str(sketch_dir),
        "seed": int(a.seed),
        "started_utc": started,
        "chromium": chromium_path,
        "timings": timings,
        "checks": checks,
        "assertions": assertions,
        "notes": notes,
        "console": rec.entries,
        "artefacts": artefacts,
        "exit": code,
    }
    text = json.dumps(report, indent=2, sort_keys=False)
    with open(out_dir / "report.json", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    if a.json:
        sys.stdout.write(text + "\n")
    return code


def evaluate_assertion(page, kind, params, state, tone_brightness, silent_ref):
    def diff(a, b):
        return page.evaluate("([a,b]) => window.__gate.diff(a,b)", [a, b])

    if kind in ("motion(idle)", "no_motion"):
        d = diff("t0", "t20")
        if d is None:
            return {"pass": False, "detail": "no canvas snapshots to compare"}
        moved = d["changed"] > 0
        ok = moved if kind == "motion(idle)" else not moved
        return {"pass": ok,
                "detail": "idle 0->%d frames: mean abs diff %.4f/255, %d of %d pixels changed"
                          % (IDLE_FRAMES, d["mean"], d["changed"], d["total"])}

    if kind == "responds(click)":
        d = diff("t20", "click")
        if d is None:
            return {"pass": False, "detail": "no canvas snapshots to compare"}
        return {"pass": d["changed"] > 0,
                "detail": "click at canvas centre, %d frames after: mean abs diff %.4f/255, "
                          "%d of %d pixels changed" % (PROBE_FRAMES, d["mean"], d["changed"], d["total"])}

    if kind == "responds(drag)":
        d = diff("click", "drag")
        if d is None:
            return {"pass": False, "detail": "no canvas snapshots to compare"}
        return {"pass": d["changed"] > 0,
                "detail": "drag across 20%%->80%% of the canvas width: mean abs diff %.4f/255, "
                          "%d of %d pixels changed" % (d["mean"], d["changed"], d["total"])}

    if kind == "responds(audio)":
        if silent_ref is None or not silent_ref.get("b64"):
            return {"pass": False,
                    "detail": "the silent reference run produced no canvas to compare with"}
        d = page.evaluate("([n,w,h,b]) => window.__gate.diffExternal(n,w,h,b)",
                          ["audio", silent_ref["w"], silent_ref["h"], silent_ref["b64"]])
        if d is None:
            return {"pass": False, "detail": "the tone run produced no canvas to compare"}
        if d.get("mismatch"):
            return {"pass": False,
                    "detail": "canvas differs in size between the runs: %sx%s under the tone "
                              "vs %sx%s under silence, so the comparison is meaningless"
                              % (d.get("aw"), d.get("ah"), d.get("bw"), d.get("bh"))}
        frac = (d["changed"] / d["total"]) if d["total"] else 0.0
        ok = d["mean"] >= AUDIO_DIFF_MEAN or frac >= AUDIO_DIFF_FRACTION
        sb = silent_ref.get("brightness")
        return {"pass": ok,
                "detail": "vs the silent reference run: mean abs pixel diff %.4f/255 "
                          "(threshold %.1f), %d of %d pixels changed = %.2f%% (threshold "
                          "%.0f%%); mean canvas brightness %.3f under 440 Hz vs %.3f "
                          "under silence"
                          % (d["mean"], AUDIO_DIFF_MEAN, d["changed"], d["total"],
                             frac * 100.0, AUDIO_DIFF_FRACTION * 100.0,
                             tone_brightness if tone_brightness is not None else float("nan"),
                             sb if sb is not None else float("nan"))}

    if kind == "uses(webgl)":
        w = state.get("webgl")
        return {"pass": w is True,
                "detail": "renderer context is %s" % ("WebGL" if w else
                                                      ("2D" if w is False else "unreadable"))}

    if kind == "size":
        w, h = params
        gw, gh = state.get("width"), state.get("height")
        cw, ch = state.get("canvasW"), state.get("canvasH")
        return {"pass": gw == w and gh == h,
                "detail": "p5 width x height %sx%s, canvas backing store %sx%s, expected %dx%d"
                          % (gw, gh, cw, ch, w, h)}

    return {"pass": False, "detail": "unimplemented assertion %r" % kind}


if __name__ == "__main__":
    sys.exit(main())
