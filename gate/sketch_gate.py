#!/usr/bin/env python3
"""sketch_gate.py -- the deterministic gate for one p5.js sketch directory.

Runs a sketch in headless chromium (Playwright, sync API), applies the fixed
checks from the sketchgen spec section 3.1 to every sketch, then evaluates the
small assertion vocabulary of section 3.2 that a planner is allowed to choose
from.  Writes gate.png, strip.png, console.log and report.json into --out.

    python3 sketch_gate.py <sketch_dir> [--assert WORD ...] [--seed N]
                           [--out DIR] [--timeout S] [--json]
                           [--frame-budget-ms MS] [--budget-s S]
                           [--keep-browser-log] [--no-ghost]
                           [--revises FILE [--revision-min-lines N]]

After all of that, and after every assertion has been decided, it plays a short
pointer script through real chromium input and writes ghost.png -- four more
frames, of a sketch somebody is touching.  Nothing in that window can fail a
run; see THE GHOST WINDOW below.

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
    1  console_clean, frame_advancing, sound_lib_ok, audio_context_running,
       frame_budget or (with --revises) revised reported false, or a requested
       assertion failed.
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

--------------------------------------------------------------------------
THE GHOST WINDOW (2026-09-21, docs/plans/auto-mouse.md section 5)
--------------------------------------------------------------------------
Entry 1103 is a jigsaw puzzle: no_motion, responds(click), responds(drag), and
on the kiosk a photograph cut into pieces that never move, because nobody is at
the keyboard.  The gate had the same blindness.  One click at the canvas centre
and one drag across the middle were all the interaction a sketch ever got, so
the fourth frame of strip.png was the only interactive evidence a judge or a
critic ever saw.

So, last of all, the gate plays a pointer script -- the sketch's own
ghost.json if it wrote one, else the built-in its requested assertions name --
through page.mouse, stepping the virtual clock between events by the gap in
their timestamps, and writes four frames of it as ghost.png.

Four things it deliberately is not:

  - It is not strip.png.  strip.png and gate.png are byte-for-byte what they
    were before this existed (DECIDE[ghost-strip]); both judge populations and
    the critic compare against them and pairs.artefact_hash is over them, so
    changing one mid-corpus would split every measurement taken so far.
  - It is not an assertion.  Every assertion is evaluated before the window
    runs and against the snapshots taken before it, so responds(click) is
    exactly as strong -- or as weak -- as it was (DECIDE[click-power],
    MEASURE[click-assertion-power]).
  - It is not a check.  A BudgetExceeded inside the window is a note and no
    ghost.png; console_clean is read at the moment the window opens and not
    through it.  Nothing a synthetic pointer does to a sketch may fail a run
    that had already passed, which is what HARNESS_VERSION 3 promises.
  - It is not free.  The window does count against --budget-s, like every
    other probe, and a script of 8 s at 60 fps is 480 stepped frames.  It is
    outside the counted idle window, so timings.ms_per_frame is unchanged.

--------------------------------------------------------------------------
revised: A REVISION HAS TO CHANGE SOMETHING (2026-09-24)
--------------------------------------------------------------------------
Since 2026-09-21 the executor on a child job is handed its parent's sketch.js
and asked to revise it.  Of the first 156 children made that way, 27 returned
the parent unchanged and 37 changed fewer than five lines of code -- a loop
bound, a speed -- and every one of them passed every check and went to a person
as the revision the critic asked for.  Before the source was given, the fewest
lines any of 1,001 children changed was 14.

So --revises names the sketch.js this one revises, and the check `revised` is
false when fewer than --revision-min-lines lines of code differ between the
two.  Comments, blank lines and whitespace are not code: a sketch returned with
its comments reworded is the same sketch.  A line counts once whether it was
added, removed or rewritten.  The check reads source only, needs no browser,
and is absent from a report the flag was not given to, because a sketch that
revises nothing has no such question to answer.  It is failable, and the only
failable check that is not about whether the page works: a visitor who meets
a revision identical to the entry beside it has met a defect too.

It cannot see whether the change is visible, and does not pretend to.  Five
rewritten lines can be a new palette or a renamed variable.  It is a floor
under the failure that happened, not a judgment of the revision, which is
still a person's.

--------------------------------------------------------------------------
loads(image) AND THE PRELOAD WAIT (2026-09-22, docs/plans/media-assertion.md)
--------------------------------------------------------------------------
The network was always open -- p5 itself comes from cdnjs on every run -- and
ResourceLog recorded what a sketch asked for and did not get.  Nothing recorded
what DID arrive, so no assertion could ask for a picture.  Now every off-origin
response with an image/* content type and a 2xx status is kept, capped at
MAX_RESOURCES_LOADED, as report.json["resources_loaded"]; like the failures
beside it, it is evidence and never a check.

loads(image) passes on that list being non-empty AND the canvas at the end of
the idle window not being one flat colour.  The second half is the weakest
honest check there is: this runner cannot see what is drawn, only that
something is.  A data: URI does not pass -- it is not the web -- and the detail
says so when that is what it finds.

The wait for a canvas is the other half.  A preload() sketch has no canvas at
all until its image arrives, and the virtual clock above does not move the
network: entry 1103's 11 s runs are that wait, and the 10 s cap on it was a
sketch thrown away for a slow host.  So when loads(image) is among the
requested assertions the wait runs to the full --timeout, and
timings.preload_s records how much of it was spent.

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
import difflib
import hashlib
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
    "loads(image)",
]
SIMPLE_ASSERTIONS = {
    "motion(idle)",
    "responds(click)",
    "responds(drag)",
    "responds(audio)",
    "uses(webgl)",
    "no_motion",
    "loads(image)",
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
                   "audio_context_running", "frame_budget", "revised")

# The fewest lines of code a revision may change from the sketch it revises
# (see "revised" in the module docstring).  Five is where the 156 children
# gated between 2026-09-21 and 2026-09-24 divide: every one that changed fewer
# was a number or two moved -- 1574 changed one loop bound, 200 to 300, for a
# critique that asked for glowing coloured nodes -- and the smallest honest
# revision among them, 1552's emerald ripple, changed exactly five.
REVISION_MIN_LINES = 5

# ---------------------------------------------------------------------------
# The ghost pointer (2026-09-21, docs/plans/auto-mouse.md section 5)
# ---------------------------------------------------------------------------
# A ghost script is a list of events in canvas fractions -- t in milliseconds
# from the start of the window, type one of four words, x and y in [0, 1] of
# the canvas -- and two players read it: the shim inside the published page
# (sketchgen/ghostshim.py), which dispatches MouseEvents, and this file, which
# drives page.mouse.  The caps and the four words are DECIDE[ghost-script].
#
# Everything from here to GHOST_BUILTINS is a COPY of sketchgen/ghostshim.py's
# own values.  It is copied and not imported for the reason SOUND_RE is: this
# file is a standalone script with its own copy on the node and in the course
# repo, and it imports nothing from the package.  A test pins the two together
# (tests/test_gate_fixtures.py, the way tests/test_executor.py pins SOUND_RE),
# so a script the shim would truncate is not one this plays whole.
GHOST_MAX_EVENTS = 64
GHOST_MAX_MS = 8000
GHOST_TYPES = ("move", "down", "up", "click")
GHOST_KEYS = ("t", "type", "x", "y")

#: The gap the shim leaves between two built-ins played one after another.
GHOST_GAP_MS = 600

#: What the executor's own script is called on disk, written beside sketch.js
#: by executor.run and read from there by both players.
GHOST_JSON = "ghost.json"

#: The four snapshots ghost.png is made of, taken at the quartiles of the
#: script's own duration -- not at 0, which would be the frame strip.png
#: already ends on.
GHOST_SNAPS = ("g0", "g1", "g2", "g3")

#: One stepped frame, in virtual milliseconds: the FRAME_MS the in-page
#: harness above counts by, so that "step to t" and "the sketch's own clock
#: reads t" are the same sentence.
GHOST_FRAME_MS = 1000.0 / 60.0

#: The three default scripts, by name, for an entry that wrote none of its own.
#: responds(click) gets `click`, responds(drag) gets `drag`, both get both in
#: that order, and a sketch that asked for neither gets `wander` -- so every
#: run has a ghost window and every entry a ghost.png.
GHOST_BUILTINS = {
    # In from a corner to the centre, one press, then three clicks
    # apart, on the golden-section points -- three unrelated places say
    # more about a sketch that reads where it was clicked than three at
    # the same spacing.
    "click": [
        {"t": 50, "type": "move", "x": 0.1325, "y": 0.8675},
        {"t": 100, "type": "move", "x": 0.185, "y": 0.815},
        {"t": 150, "type": "move", "x": 0.2375, "y": 0.7625},
        {"t": 200, "type": "move", "x": 0.29, "y": 0.71},
        {"t": 250, "type": "move", "x": 0.3425, "y": 0.6575},
        {"t": 300, "type": "move", "x": 0.395, "y": 0.605},
        {"t": 350, "type": "move", "x": 0.4475, "y": 0.5525},
        {"t": 400, "type": "move", "x": 0.5, "y": 0.5},
        {"t": 500, "type": "down", "x": 0.5, "y": 0.5},
        {"t": 620, "type": "up", "x": 0.5, "y": 0.5},
        {"t": 1400, "type": "move", "x": 0.382, "y": 0.618},
        {"t": 1520, "type": "click", "x": 0.382, "y": 0.618},
        {"t": 2300, "type": "move", "x": 0.618, "y": 0.382},
        {"t": 2420, "type": "click", "x": 0.618, "y": 0.382},
        {"t": 3200, "type": "move", "x": 0.618, "y": 0.618},
        {"t": 3320, "type": "click", "x": 0.618, "y": 0.618},
    ],
    # Two legs: left to right across the middle, then top to bottom
    # down it, each one press, twelve moves over 800 ms, one release.
    "drag": [
        {"t": 200, "type": "move", "x": 0.2, "y": 0.5},
        {"t": 400, "type": "down", "x": 0.2, "y": 0.5},
        {"t": 467, "type": "move", "x": 0.25, "y": 0.5},
        {"t": 533, "type": "move", "x": 0.3, "y": 0.5},
        {"t": 600, "type": "move", "x": 0.35, "y": 0.5},
        {"t": 667, "type": "move", "x": 0.4, "y": 0.5},
        {"t": 733, "type": "move", "x": 0.45, "y": 0.5},
        {"t": 800, "type": "move", "x": 0.5, "y": 0.5},
        {"t": 867, "type": "move", "x": 0.55, "y": 0.5},
        {"t": 933, "type": "move", "x": 0.6, "y": 0.5},
        {"t": 1000, "type": "move", "x": 0.65, "y": 0.5},
        {"t": 1067, "type": "move", "x": 0.7, "y": 0.5},
        {"t": 1133, "type": "move", "x": 0.75, "y": 0.5},
        {"t": 1200, "type": "move", "x": 0.8, "y": 0.5},
        {"t": 1320, "type": "up", "x": 0.8, "y": 0.5},
        {"t": 1800, "type": "move", "x": 0.5, "y": 0.2},
        {"t": 2000, "type": "down", "x": 0.5, "y": 0.2},
        {"t": 2067, "type": "move", "x": 0.5, "y": 0.25},
        {"t": 2133, "type": "move", "x": 0.5, "y": 0.3},
        {"t": 2200, "type": "move", "x": 0.5, "y": 0.35},
        {"t": 2267, "type": "move", "x": 0.5, "y": 0.4},
        {"t": 2333, "type": "move", "x": 0.5, "y": 0.45},
        {"t": 2400, "type": "move", "x": 0.5, "y": 0.5},
        {"t": 2467, "type": "move", "x": 0.5, "y": 0.55},
        {"t": 2533, "type": "move", "x": 0.5, "y": 0.6},
        {"t": 2600, "type": "move", "x": 0.5, "y": 0.65},
        {"t": 2667, "type": "move", "x": 0.5, "y": 0.7},
        {"t": 2733, "type": "move", "x": 0.5, "y": 0.75},
        {"t": 2800, "type": "move", "x": 0.5, "y": 0.8},
        {"t": 2920, "type": "up", "x": 0.5, "y": 0.8},
    ],
    # A Lissajous path, three turns across against two down, no
    # button: a field that scatters under the cursor has to be crossed,
    # not visited.
    "wander": [
        {"t": 100, "type": "move", "x": 0.6816, "y": 0.8564},
        {"t": 200, "type": "move", "x": 0.8236, "y": 0.8951},
        {"t": 300, "type": "move", "x": 0.8951, "y": 0.8951},
        {"t": 400, "type": "move", "x": 0.8804, "y": 0.8564},
        {"t": 500, "type": "move", "x": 0.7828, "y": 0.7828},
        {"t": 600, "type": "move", "x": 0.6236, "y": 0.6816},
        {"t": 700, "type": "move", "x": 0.4374, "y": 0.5626},
        {"t": 800, "type": "move", "x": 0.2649, "y": 0.4374},
        {"t": 900, "type": "move", "x": 0.1436, "y": 0.3184},
        {"t": 1000, "type": "move", "x": 0.1, "y": 0.2172},
        {"t": 1100, "type": "move", "x": 0.1436, "y": 0.1436},
        {"t": 1200, "type": "move", "x": 0.2649, "y": 0.1049},
        {"t": 1300, "type": "move", "x": 0.4374, "y": 0.1049},
        {"t": 1400, "type": "move", "x": 0.6236, "y": 0.1436},
        {"t": 1500, "type": "move", "x": 0.7828, "y": 0.2172},
        {"t": 1600, "type": "move", "x": 0.8804, "y": 0.3184},
        {"t": 1700, "type": "move", "x": 0.8951, "y": 0.4374},
        {"t": 1800, "type": "move", "x": 0.8236, "y": 0.5626},
        {"t": 1900, "type": "move", "x": 0.6816, "y": 0.6816},
        {"t": 2000, "type": "move", "x": 0.5, "y": 0.7828},
        {"t": 2100, "type": "move", "x": 0.3184, "y": 0.8564},
        {"t": 2200, "type": "move", "x": 0.1764, "y": 0.8951},
        {"t": 2300, "type": "move", "x": 0.1049, "y": 0.8951},
        {"t": 2400, "type": "move", "x": 0.1196, "y": 0.8564},
        {"t": 2500, "type": "move", "x": 0.2172, "y": 0.7828},
        {"t": 2600, "type": "move", "x": 0.3764, "y": 0.6816},
        {"t": 2700, "type": "move", "x": 0.5626, "y": 0.5626},
        {"t": 2800, "type": "move", "x": 0.7351, "y": 0.4374},
        {"t": 2900, "type": "move", "x": 0.8564, "y": 0.3184},
        {"t": 3000, "type": "move", "x": 0.9, "y": 0.2172},
        {"t": 3100, "type": "move", "x": 0.8564, "y": 0.1436},
        {"t": 3200, "type": "move", "x": 0.7351, "y": 0.1049},
        {"t": 3300, "type": "move", "x": 0.5626, "y": 0.1049},
        {"t": 3400, "type": "move", "x": 0.3764, "y": 0.1436},
        {"t": 3500, "type": "move", "x": 0.2172, "y": 0.2172},
        {"t": 3600, "type": "move", "x": 0.1196, "y": 0.3184},
        {"t": 3700, "type": "move", "x": 0.1049, "y": 0.4374},
        {"t": 3800, "type": "move", "x": 0.1764, "y": 0.5626},
        {"t": 3900, "type": "move", "x": 0.3184, "y": 0.6816},
        {"t": 4000, "type": "move", "x": 0.5, "y": 0.7828},
    ],
}


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def tilde(path):
    home = os.path.expanduser("~")
    return path.replace(home, "~", 1) if path.startswith(home) else path


def refuse(msg):
    sys.stderr.write("sketch_gate: refused -- %s\n" % msg)
    sys.exit(3)


def human_bytes(size):
    """A size for a sentence a person reads, in decimal kB, or the plain truth.

    None is "size unknown" and not 0 kB: a host that served no content-length
    and a body the run could not read back is a thing nobody measured, and the
    entry page prints a blank for it for the same reason (media-assertion.md
    DECIDE[image-hosts], Packet 20).
    """
    if size is None:
        return "size unknown"
    if size < 1000:
        return "%d B" % size
    return "%d kB" % int(round(size / 1000.0))


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
  //
  // It does both for a p5 and nothing else.  The p5.Graphics constructor calls
  // this same method on the buffer it is building (p5 1.11.3:
  // p5.prototype._initializeInstanceVariables.apply(graphics)), and a Graphics
  // extends p5.Element, not p5.  Keeping every `this` made the last buffer the
  // sketch as far as state() could tell: no _loop, so is_looping false and
  // frame_advancing skipped as if the sketch had called noLoop(); a frameCount
  // copied from the prototype and never advanced; and the buffer's own size and
  // renderer for size(w,h) and uses(webgl).  Job 1542 (entry 1531, 2026-09-24)
  // made two gradient buffers and never called noLoop(), and read is_looping
  // false until they were plain canvases.
  function patchP5(P) {
    if (!P || !P.prototype || P.__gatePatched) return;
    const init = P.prototype._initializeInstanceVariables;
    if (typeof init !== 'function') return;      // try again on a later assignment
    P.__gatePatched = true;
    P.prototype._initializeInstanceVariables = function () {
      const r = init.apply(this, arguments);
      if (!(this instanceof P)) return r;
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
  // loads(image), the drawn half (2026-09-22): true when every pixel of the
  // snapshot is within 2/255 of one colour -- a canvas with nothing on it but
  // its background, which is what an image that arrived and was never drawn
  // looks like from out here. It reads the same imageData the diffs do, and
  // it is min/max per channel rather than a comparison against the first
  // pixel, so "within 2 of one colour" means a colour exists that they are
  // all within 2 of, whichever pixel happens to come first.
  g.flat = function (name) {
    const A = g.snaps[name];
    if (!A) return null;
    const d = A.data, n = d.length;
    if (!n) return null;
    let lo = [255, 255, 255], hi = [0, 0, 0];
    for (let i = 0; i < n; i += 4) {
      for (let c = 0; c < 3; c++) {
        const v = d[i + c];
        if (v < lo[c]) lo[c] = v;
        if (v > hi[c]) hi[c] = v;
      }
    }
    for (let c = 0; c < 3; c++) if (hi[c] - lo[c] > 2) return false;
    return true;
  };
  // What the page went out for, as the browser itself recorded it. The gate
  // cannot prove a data: URI was drawn -- a data: URI is not a resource and
  // has no entry here -- but a page with no off-origin image entry at all did
  // not use the web for its picture, and that is a sentence worth saying.
  g.fetched = function () {
    let out = [];
    try {
      const entries = performance.getEntriesByType('resource') || [];
      for (const e of entries) {
        out.push({ name: String(e.name || ''), kind: String(e.initiatorType || '') });
        if (out.length >= 64) break;
      }
    } catch (err) { return out; }
    return out;
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

# ---------------------------------------------------------------------------
# revised (2026-09-24): how much of a revision is new
# ---------------------------------------------------------------------------


def strip_js_comments(text):
    """*text* with its // and /* */ comments removed and its strings intact.

    A string is skipped whole, so the // in "https://..." is not a comment.
    A block comment keeps its newlines, so line numbers still line up for a
    person reading the two side by side.  A regular-expression literal is not
    recognised: one with an unescaped // or /* in it would be cut short, on
    both sides of the comparison alike, and no sketch in the corpus has one.
    """
    out = []
    i, n = 0, len(text)
    quote = None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            # A quote or apostrophe string cannot cross a line in JavaScript,
            # so a stray one -- inside a regex literal, say -- costs the rest
            # of its own line and not the rest of the file.
            if c == quote or (c == "\n" and quote != "`"):
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            stop = n if end < 0 else end + 2
            out.append("\n" * text.count("\n", i, stop))
            i = stop
            continue
        out.append(c)
        i += 1
    return "".join(out)


def code_lines(text):
    """The lines of code in *text*: no comments, no blanks, no whitespace.

    All whitespace goes, not just the ends: `x=1` and `x = 1` are one line of
    code written two ways, and a reformat is not a revision.
    """
    lines = []
    for line in strip_js_comments(text).splitlines():
        squeezed = "".join(line.split())
        if squeezed:
            lines.append(squeezed)
    return lines


def lines_changed(before, after):
    """How many lines of code differ between two lists from code_lines().

    Each run of difference counts as its longer side, so a rewritten line is
    one line, two lines added are two, and three lines replaced by one are
    three: the size of the edit, not the sum of its two halves.
    """
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    return sum(max(i2 - i1, j2 - j1)
               for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal")


def revision_verdict(sketch_text, revises_text, min_lines):
    """(passed, record, note) for a sketch against the sketch it revises."""
    before = code_lines(revises_text)
    after = code_lines(sketch_text)
    changed = lines_changed(before, after)
    passed = changed >= min_lines
    record = {
        "sha256": hashlib.sha256(revises_text.encode("utf-8")).hexdigest(),
        "lines_before": len(before),
        "lines_after": len(after),
        "changed": changed,
        "min": min_lines,
    }
    if changed == 0:
        what = ("sketch.js is the sketch it revises, unchanged: not one line of "
                "code differs from the %d it was given" % len(before))
    else:
        what = ("%d line%s of code differ%s from the sketch it revises (%d lines "
                "of code before, %d after)"
                % (changed, "" if changed == 1 else "s",
                   "s" if changed == 1 else "", len(before), len(after)))
    note = ("revised: %s; comments, blank lines and whitespace are not counted, "
            "and a revision changes at least %d" % (what, min_lines))
    return passed, record, note


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
    ap.add_argument("--no-ghost", dest="ghost", action="store_false", default=True,
                    help="skip the ghost window: play no pointer script and write "
                         "no ghost.png. Nothing it does can fail a run either way; "
                         "this is for a plain run that wants the old wall cost")
    ap.add_argument("--revises", default=None, metavar="FILE",
                    help="the sketch.js this sketch revises: adds the failable check "
                         "`revised`, false when fewer than --revision-min-lines lines "
                         "of code differ from it")
    ap.add_argument("--revision-min-lines", type=int, default=REVISION_MIN_LINES,
                    metavar="N",
                    help="lines of code a revision must change from --revises "
                         "(default %d; comments and whitespace are not code)"
                         % REVISION_MIN_LINES)
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


#: At most this many distinct failed resources are reported. A sketch looping a
#: broken fetch can produce thousands; the first few say the same thing.
MAX_RESOURCE_FAILURES = 8

#: And at most this many distinct images that did arrive (2026-09-22,
#: media-assertion.md section 3.1). The same number as the failures for the same
#: reason: a collage that fetches a hundred tiles from one host says what it has
#: to say in the first eight, and this list is read by a person deciding whether
#: to publish a sketch that calls a third host from their visitors' browsers.
MAX_RESOURCES_LOADED = 8

#: What makes a response an image: its content-type and nothing else. The size
#: never decides -- a 1x1 pixel is an image that arrived, and the detail says
#: how few bytes it was rather than pretending it was not one.
IMAGE_TYPE_PREFIX = "image/"


class ResourceLog:
    """Every resource the sketch asked for and did not get -- and the images
    that did arrive.

    The sketch's own files come off disk through the page.route() handler on
    SKETCH_ORIGIN and cannot fail. Anything else is the sketch reaching outside
    itself -- a CDN library, a font, an image -- and when one of those does not
    arrive the canvas is often simply blank with nothing in the console to say
    why: a failed image is not a page error, so console_clean stays true and the
    run looks healthy right up until every assertion reads zero pixels changed.

    Entry 429, a jigsaw puzzle, loaded https://picsum.photos/400/400 in
    preload() across eight attempts. Every run took eleven seconds and drew
    nothing, and the evidence the model was handed said only that no pixels had
    changed -- which reads as "your click handler is broken" when the truth was
    that the sketch never started. It spent those attempts rewriting handlers
    that already worked.

    This is not a check and never a verdict. Reaching outside the sketch is
    allowed, and a sketch that works out it can do so has worked something out.
    This is only the sentence that tells it what happened when the thing it
    reached for did not arrive.

    Since 2026-09-22 (media-assertion.md section 3.1) it also keeps what DID
    arrive, and only images: every off-origin 2xx response whose content-type
    starts image/, as {url, host, type, bytes, ms}. That list is what
    loads(image) is evaluated against, and what a person reads before
    publishing a sketch that will call a third host from a visitor's browser.
    It is still not a check: an image arriving fails nothing, and an image not
    arriving fails nothing either unless a planner asked for one.

    The sizes are deliberately not read inside the response handler. Playwright's
    sync API is the dispatcher's own greenlet there, and a round trip for a body
    from inside a handler is how a run hangs; :meth:`measure` reads the ones no
    content-length header declared, afterwards, from the ordinary flow.
    """

    def __init__(self):
        self.failures = []
        self.loaded = []
        self._seen = set()
        self._seen_loaded = set()
        self._started = {}
        self._pending = []

    def attach(self, page):
        page.on("request", self._on_request)
        page.on("requestfailed", self._on_failed)
        page.on("response", self._on_response)

    def _outside(self, url):
        return not str(url or "").startswith(SKETCH_ORIGIN)

    def _from_the_web(self, url):
        """Off-origin AND actually fetched over http(s).

        A data: or blob: URL is neither the sketch's own file nor the web, and
        Chromium reports a response for both. Counting one as an arrival would
        pass loads(image) on the sketch the gate's README remembers -- the one
        that built its own image as a data: URI when it could not fetch one --
        which is the case DECIDE[image-pass] rules out by name.
        """
        text = str(url or "")
        return self._outside(text) and (text.startswith("http://")
                                        or text.startswith("https://"))

    def _add(self, url, kind, why):
        if url in self._seen or len(self.failures) >= MAX_RESOURCE_FAILURES:
            return
        self._seen.add(url)
        self.failures.append({"url": url, "type": kind, "why": why})

    def _on_request(self, request):
        # Wall time from the request leaving to its response arriving is the
        # `ms` in the list. The page's own performance timings would be the
        # finer number, but the virtual clock above has already made
        # performance.now() a lie about anything that is not a stepped frame.
        try:
            if self._outside(request.url) and len(self._started) <= 4 * MAX_RESOURCES_LOADED:
                self._started[request] = time.time()
        except Exception:
            pass

    def _on_failed(self, request):
        try:
            url, kind, why = request.url, request.resource_type, request.failure
        except Exception:
            return
        if self._outside(url):
            self._add(url, kind, str(why or "the request failed"))

    def _on_response(self, response):
        try:
            url, status = response.url, response.status
            request = response.request
            kind = request.resource_type
        except Exception:
            return
        if status >= 400 and self._outside(url):
            self._add(url, kind, "HTTP %d" % status)
            return
        if 200 <= status < 300 and self._from_the_web(url):
            self._arrived(response, request, url)

    def _arrived(self, response, request, url):
        """Keep an off-origin 2xx image. Everything else is somebody's script."""
        # response.headers is the initializer's own dict, already lowercased:
        # no round trip, which header_value() and all_headers() both are, and
        # a round trip from inside a response handler is a hang.
        try:
            headers = response.headers or {}
        except Exception:
            headers = {}
        kind = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if not kind.startswith(IMAGE_TYPE_PREFIX):
            return
        if url in self._seen_loaded or len(self.loaded) >= MAX_RESOURCES_LOADED:
            return
        self._seen_loaded.add(url)
        declared = headers.get("content-length")
        try:
            size = int(declared) if declared is not None else None
        except (TypeError, ValueError):
            size = None
        started = self._started.pop(request, None)
        item = {"url": url,
                "host": urlsplit(url).netloc,
                "type": kind,
                "bytes": size,
                "ms": None if started is None else int(round((time.time() - started) * 1000))}
        self.loaded.append(item)
        if size is None:
            self._pending.append((item, response))

    def measure(self):
        """Fill in the sizes no content-length declared, from the bodies.

        Called from the run, not from a handler, and best effort: a host that
        served no content-length and a body this can no longer read leaves
        `bytes` null, which the detail and the entry page both print as
        unknown rather than as zero.
        """
        while self._pending:
            item, response = self._pending.pop()
            try:
                item["bytes"] = len(response.body())
            except Exception:
                pass

    def notes(self):
        return ["the sketch asked for %s (%s) and did not get it: %s"
                % (f["url"], f["type"], f["why"]) for f in self.failures]

    def arrival_notes(self):
        """One line per image that arrived, for a person reading the report."""
        return ["the sketch loaded an image from outside itself: %s (%s, %s, %s)"
                % (item["url"], item["type"], human_bytes(item["bytes"]),
                   "timing unknown" if item["ms"] is None else "%d ms" % item["ms"])
                for item in self.loaded]


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

    def clean_through(self, upto=None):
        """True when nothing up to entry *upto* was an error.

        The ghost window (2026-09-21) is the reason this takes a bound. It runs
        after every check and every assertion has been decided, and a synthetic
        pointer that clicks where the probe did not could otherwise turn a
        sketch that passed into a console_clean failure on the strength of input
        no person sent. HARNESS_VERSION 3 says nothing the gate fails changed,
        and this is where that is kept true. Everything the window logged is
        still in console.log and in the report's console list.
        """
        seen = self.entries if upto is None else self.entries[:upto]
        return not any(e["type"] in ("error", "pageerror") for e in seen)

    @property
    def clean(self):
        return self.clean_through()


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


#: How long a sketch has to put a canvas on the page before the run goes on
#: without one, when nothing was asserted about loading anything. Ten seconds
#: was the only number here until 2026-09-22.
CANVAS_WAIT_MS = 10000

#: And how often the wait below looks. It is a timer and not the default,
#: which is the whole point: see load_sketch.
CANVAS_POLL_MS = 100


def load_sketch(page, timeout_ms, wait_for_canvas_ms=CANVAS_WAIT_MS):
    """Load the sketch and wait for a canvas. Returns (seconds, canvas wait).

    The wait is a parameter because of preload(). A sketch that loads an image
    before setup() runs has no canvas at all until the image arrives, and the
    virtual clock does not move the network. When loads(image) is asserted,
    main passes the whole --timeout instead (DECIDE[image-wait]).

    `polling=CANVAS_POLL_MS` is load-bearing and was measured, not guessed
    (2026-09-22). Playwright's default for wait_for_function is `raf`: it
    evaluates the predicate once, and then on every animation frame. Section 2
    of the module docstring above replaces requestAnimationFrame with a queue
    that drains only when this runner steps it, and nothing steps it until
    after this function returns -- so for any sketch whose canvas did not
    already exist when the page's load event fired, the predicate was evaluated
    exactly once, false, and the wait then sat out its whole timeout.

    That is where the eleven seconds in the entry 429 and entry 1103 stories
    came from. Both load a photograph in preload(), both were read as slow
    hosts, and the node's own reports say otherwise: `load_s` is 10.19 s on nine
    of entry 429's ten attempts -- the cap, to the millisecond -- while the
    picture itself arrives in about a tenth of a second. The wait had never
    once ended early. On a timer it does, and raising the cap to --timeout for
    an asserted sketch costs nothing when the host is quick.
    """
    t0 = time.time()
    page.goto(SKETCH_ORIGIN + "/index.html", wait_until="load", timeout=timeout_ms)
    t_canvas = time.time()
    try:
        page.wait_for_function("() => !!(window.__gate && window.__gate.canvas())",
                               timeout=min(timeout_ms, wait_for_canvas_ms),
                               polling=CANVAS_POLL_MS)
    except Exception:
        pass
    now = time.time()
    return now - t0, now - t_canvas


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


# ---------------------------------------------------------------------------
# The ghost window
# ---------------------------------------------------------------------------

def ghost_number(value):
    """*value* as a float, or None if it is not a number.

    True is an int in Python and would otherwise be an x of 1.0 at the
    right-hand edge of the canvas. It is not a coordinate, it is a typo. The
    same sentence, and the same rule, as executor._number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def validate_ghost(text):
    """A ghost script as events, or (None, why).

    Character for character the decision executor.validate_ghost makes, and a
    test runs both over one list of samples: the executor is what lets a script
    onto disk, this is what plays it, and a gate that refused what the executor
    accepted would silently give an entry the built-in instead -- which looks
    exactly like a working entry.

    Events come back sorted by t, stably, rather than a list out of order being
    refused: the shim schedules one setTimeout per event and does the same.
    """
    try:
        events = json.loads(text)
    except ValueError:
        return None, "not a JSON list"
    if not isinstance(events, list):
        return None, "not a JSON list"
    if not events:
        return None, "an empty list, so there is nothing to play"
    if len(events) > GHOST_MAX_EVENTS:
        return None, "%d events, more than %d" % (len(events), GHOST_MAX_EVENTS)

    out = []
    for at, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return None, "event %d: not an object" % at
        if tuple(sorted(event)) != tuple(sorted(GHOST_KEYS)):
            return None, "event %d: its keys are %s, not %s" % (
                at, ", ".join(sorted(str(key) for key in event)) or "(none)",
                "/".join(GHOST_KEYS))
        when = ghost_number(event["t"])
        if when is None:
            return None, "event %d: t is %s, not a number" % (at, json.dumps(event["t"]))
        if when < 0 or when > GHOST_MAX_MS:
            return None, "event %d: t is %s, outside [0, %d] ms" % (
                at, json.dumps(event["t"]), GHOST_MAX_MS)
        if event["type"] not in GHOST_TYPES:
            return None, "event %d: type is %s, not one of %s" % (
                at, json.dumps(event["type"]), "/".join(GHOST_TYPES))
        for axis in ("x", "y"):
            where = ghost_number(event[axis])
            if where is None:
                return None, "event %d: %s is %s, not a number" % (
                    at, axis, json.dumps(event[axis]))
            if where < 0 or where > 1:
                return None, "event %d: %s is %s, outside [0, 1]" % (
                    at, axis, json.dumps(event[axis]))
        out.append({key: event[key] for key in GHOST_KEYS})

    out.sort(key=lambda event: ghost_number(event["t"]))
    return out, None


def ghost_default_name(wanted):
    """Which built-in a sketch that wrote no script of its own gets."""
    kinds = {kind for _, kind, _ in wanted}
    names = [name for name, kind in (("click", "responds(click)"),
                                     ("drag", "responds(drag)"))
             if kind in kinds]
    return ",".join(names) if names else "wander"


def ghost_builtin(name):
    """One or more built-ins, joined the way the shim's `chosen` joins them.

    A comma list is played in order with GHOST_GAP_MS between the end of one
    and the start of the next, and the result is held to the same caps as a
    script somebody wrote: the shim truncates at MAX_EVENTS and drops anything
    past MAX_MS, and two players that disagreed about where a script stops
    would disagree about what the sketch was shown.
    """
    out = []
    for part in str(name).split(","):
        script = GHOST_BUILTINS.get(part.strip())
        if not script:
            continue
        shift = (max(event["t"] for event in out) + GHOST_GAP_MS) if out else 0
        for event in script:
            out.append(dict(event, t=event["t"] + shift))
    return [event for event in out if event["t"] <= GHOST_MAX_MS][:GHOST_MAX_EVENTS]


def ghost_script(sketch_dir, wanted, notes):
    """The script this run plays: (events, source, name).

    The sketch's own ghost.json when it has one and it parses, else the
    built-in its requested assertions name. An unreadable or invalid file is a
    note and the default, never a refusal: the ghost window is evidence, and a
    run that stopped because a sketch's pointer script had a typo in it would
    be the gate failing a sketch for something the gate does not judge.
    """
    path = pathlib.Path(sketch_dir) / GHOST_JSON
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            events, why = None, "could not be read (%s)" % exc
        else:
            events, why = validate_ghost(text)
        if events is not None:
            return events, "executor", None
        notes.append("the sketch's own %s was not played: %s; the ghost window "
                     "used the default script instead" % (GHOST_JSON, why))
    name = ghost_default_name(wanted)
    return ghost_builtin(name), "default", name


def canvas_rect(page):
    """The canvas in viewport coordinates, as drag_across reads it."""
    return page.evaluate(
        "() => { const c = window.__gate.canvas(); if (!c) return null;"
        " const r = c.getBoundingClientRect();"
        " return {x: r.x, y: r.y, w: r.width, h: r.height}; }")


def ghost_send(page, box, event, held):
    """One event, as real chromium input. Returns whether a button is held.

    The move before a press is this player's own: the shim dispatches a bare
    mousedown at the point, and Playwright presses wherever the pointer already
    is, so moving there first is how the two arrive at the same clientX. Every
    built-in already has that move in it, at the same coordinates, which is
    why it changes nothing for them.
    """
    x = box["x"] + box["w"] * float(event["x"])
    y = box["y"] + box["h"] * float(event["y"])
    kind = event["type"]
    if kind == "move":
        page.mouse.move(x, y)
    elif kind == "down":
        page.mouse.move(x, y)
        page.mouse.down()
        return True
    elif kind == "up":
        page.mouse.move(x, y)
        page.mouse.up()
        return False
    else:
        page.mouse.click(x, y)
    return held


def play_ghost(page, out_dir, events, budget, notes):
    """Play *events* and write ghost.png. Never fails a run; returns a summary.

    The clock is stepped between events by the gap in their timestamps at 60
    frames a second, so the same script under the same seed draws the same
    frames however loaded the machine is -- the whole reason the rest of this
    runner hand-steps too.

    A BudgetExceeded here is caught here. The run has already decided
    everything it decides, and the sketch had already passed or failed on the
    window that matters; what it loses is its ghost.png, which is a picture.
    """
    summary = {"events": len(events), "played": 0, "ms": 0, "png": None}
    box = canvas_rect(page)
    if not box:
        notes.append("ghost window skipped: no canvas to play the pointer script on")
        return summary

    duration = max(float(event["t"]) for event in events)
    timeline = [(duration * (at + 1) / 4.0, 0, at, name)
                for at, name in enumerate(GHOST_SNAPS)]
    timeline += [(float(event["t"]), 1, at, event) for at, event in enumerate(events)]
    # Snapshots sort before events at the same millisecond: a frame is only
    # drawn when the clock is stepped, so an event dispatched at t has not
    # reached the canvas at t, and snapping after it would show the world
    # before it with the event's own timestamp on it.
    timeline.sort(key=lambda item: item[:3])

    stepped = 0
    held = False
    try:
        for when, kind, _at, payload in timeline:
            want = int(round(when / GHOST_FRAME_MS))
            if want > stepped:
                step(page, want - stepped, budget=budget)
                stepped = want
            if kind == 0:
                page.evaluate("name => window.__gate.snap(name)", payload)
            else:
                held = ghost_send(page, box, payload, held)
                summary["played"] += 1
            summary["ms"] = int(round(when))
    except BudgetExceeded as exc:
        # Deliberately not re-raised and deliberately not a check: see the
        # module docstring, THE GHOST WINDOW. The note is so a person reading
        # the report knows why the entry page shows no ghost frames.
        notes.append("the ghost window stopped early and wrote no %s: %s"
                     % ("ghost.png", exc))
        return summary
    finally:
        if held:
            # A script may end mid-drag. Nothing runs after this window, but a
            # pointer left pressed is a state somebody will trip over.
            try:
                page.mouse.up()
            except Exception as exc:  # noqa: BLE001 - tidying, not measuring
                notes.append("the ghost pointer could not be released: %s" % exc)

    path = out_dir / "ghost.png"
    if save_data_url(page.evaluate("names => window.__gate.strip(names)",
                                   list(GHOST_SNAPS)), path):
        summary["png"] = str(path)
    else:
        notes.append("ghost.png could not be produced from the canvas")
    return summary


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


def silent_reference_run(pw, init_js, wav, timeout_ms, sketch_dir,
                         wait_for_canvas_ms=CANVAS_WAIT_MS):
    """Second launch, silent fake microphone, same script.

    Returns the canvas at the same point the tone run snaps it: the raw RGBA as
    base64 plus its size and mean brightness.  The pixels are what decides
    responds(audio); the brightness is kept for the human reading the detail.

    It takes the same canvas wait as the tone run: the two runs have to differ
    only by the microphone, and a reference run that gave up on a preload()
    the first run waited out would compare a drawn canvas with a blank one.
    """
    browser = context = None
    try:
        browser, context, page, _ = open_page(pw, init_js, wav, None, timeout_ms, sketch_dir)
        load_sketch(page, timeout_ms, wait_for_canvas_ms)
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
    want_image = any(kind == "loads(image)" for _, kind, _ in wanted)

    out_dir = pathlib.Path(a.out).expanduser() if a.out else sketch_dir / ".gate"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        refuse("cannot create --out %s (%s)" % (out_dir, e))

    timeout_ms = int(a.timeout * 1000)
    # DECIDE[image-wait]: a sketch that was asked for a picture is given the
    # whole timeout to get one, because a preload() has no canvas until the
    # image arrives and a run that gave up at 10 s would report the sketch as
    # having drawn nothing -- which is what entry 1103's 11 s runs were doing
    # against the old cap. Every other run keeps the wait it always had.
    wait_for_canvas_ms = timeout_ms if want_image else CANVAS_WAIT_MS

    # source scan for the sound library, before any browser work
    source = ""
    for p in sorted(sketch_dir.glob("*.html")) + sorted(sketch_dir.glob("*.js")):
        try:
            source += p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    references_sound = bool(SOUND_RE.search(source))

    # revised: source against source, before any browser work, and refused
    # rather than guessed at when the sketch it revises cannot be read -- a
    # caller that names a file is saying there is a comparison to make.
    revision = None
    if a.revises is not None:
        if a.revision_min_lines < 1:
            refuse("--revision-min-lines must be at least 1; to ask nothing of a "
                   "revision, leave --revises out")
        try:
            revises_text = pathlib.Path(a.revises).expanduser().read_text(
                encoding="utf-8", errors="replace")
            sketch_text = sketch_js.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            refuse("cannot read --revises %s (%s)" % (a.revises, e))
        passed, record, note = revision_verdict(sketch_text, revises_text,
                                                a.revision_min_lines)
        record["of"] = tilde(str(pathlib.Path(a.revises).expanduser()))
        revision = {"passed": passed, "record": record, "note": note}

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
    # Only when asked: a sketch that revises nothing has no `revised` to be,
    # not even null, and a plain run's report is the shape it always was.
    if revision is not None:
        checks["revised"] = revision["passed"]
        notes.append(revision["note"])
    assertions = {}
    rec = Recorder()
    res = ResourceLog()
    # preload_s is the part of load_s spent waiting for a canvas to exist, the
    # stretch a preload() spends on the network (2026-09-22). It is recorded on
    # every run, not only the ones that asserted loads(image): the number is
    # what says whether a sketch's eleven seconds were the image or the code.
    timings = {"launch_s": None, "load_s": None, "preload_s": None,
               "total_s": None, "ms_per_frame": None, "idle_step_s": None}
    budget = Budget(a.frame_budget_ms, a.budget_s, t_start)
    chromium_path = None
    # The ghost window, and where the console stopped being read for
    # console_clean. Both stay as they are on a run that never reaches the
    # window -- one the budget stopped, or one run with --no-ghost.
    ghost = None
    ghost_console_mark = None

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
            res.attach(page)

            preload_s = None
            try:
                load_s, preload_s = load_sketch(page, timeout_ms, wait_for_canvas_ms)
            except Exception as e:
                notes.append("page load did not complete: %s" % e)
                load_s = time.time() - t_start
            timings["load_s"] = round(load_s, 3)
            if preload_s is not None:
                timings["preload_s"] = round(preload_s, 3)
            if want_image:
                # Worth saying out loud in the report, because this wait is
                # also wall time the --budget-s ceiling is counting: a host that
                # takes most of the timeout leaves the rest of the run less of
                # it, and a person reading a frame_budget failure should be able
                # to see that the seconds went to the network.
                notes.append("loads(image): the wait for a canvas ran to the whole "
                             "%g s timeout rather than the usual %g s, because a "
                             "preload() has no canvas until its image arrives; "
                             "%s of it was spent waiting"
                             % (a.timeout, CANVAS_WAIT_MS / 1000.0,
                                "an unmeasured amount" if timings["preload_s"] is None
                                else "%g s" % timings["preload_s"]))
            # The sizes the hosts did not declare, read now that the run is back
            # in its own flow: a body read from inside a response handler is how
            # the sync API hangs (ResourceLog.measure).
            res.measure()

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
                            pw, init_js, silent_wav, timeout_ms, sketch_dir,
                            wait_for_canvas_ms)
                    except Exception as e:
                        notes.append("silent reference run failed: %s" % e)
                    if silent_ref:
                        notes.append("responds(audio) compares the tone run with the silent "
                                     "reference run pixel by pixel; the two runs are identical "
                                     "except for the microphone because the seeds and the clock "
                                     "are fixed")

                # An image can arrive after the page's load event -- a
                # loadImage in setup() rather than preload() -- so the sizes are
                # read once more before anything is decided on them.
                res.measure()
                for literal, kind, params in wanted:
                    assertions[literal] = evaluate_assertion(
                        page, kind, params, state1, tone_brightness, silent_ref,
                        resources=res, timeout_s=a.timeout)

                # ---- the ghost window ------------------------------------
                # Last, and after every assertion, on purpose: what the ghost
                # does to the sketch is evidence for a person, the critic and
                # the judge, and nothing this window sees is allowed to change
                # a verdict (auto-mouse.md DECIDE[ghost-strip],
                # DECIDE[click-power]).
                if a.ghost:
                    ghost_console_mark = len(rec.entries)
                    events, source, script = ghost_script(sketch_dir, wanted, notes)
                    if events:
                        ghost = play_ghost(page, out_dir, events, budget, notes)
                        ghost.update(source=source, script=script)
                    else:
                        notes.append("ghost window skipped: no script to play")
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
                    "did not finish the probes or the assertions"
                    % (budget.frames, IDLE_FRAMES))
                # One readback before the browser closes, so the operator UI has
                # a still to draw its play button on and a person can see what
                # the sketch was doing without running it. Best effort: a page
                # that is already broken is not worth failing the report over.
                try:
                    if save_data_url(page.evaluate("() => window.__gate.png()"),
                                     out_dir / "gate.png"):
                        notes.append("gate.png is the frame the run stopped on")
                except Exception as snap:  # noqa: BLE001
                    notes.append("no frame could be read back after the budget "
                                 "stopped the run: %s" % snap)

            # console cleanliness is judged over the whole run -- up to the
            # ghost window, which opens after every check and assertion has
            # been decided and may not undo one (Recorder.clean_through).
            checks["console_clean"] = rec.clean_through(ghost_console_mark)
            if checks["console_clean"] and not rec.clean:
                notes.append("the page logged an error during the ghost window, "
                             "after every check and assertion was decided; it is "
                             "in console.log and console_clean did not read it")

        finally:
            for obj in (context, browser):
                try:
                    if obj:
                        obj.close()
                except Exception:
                    pass

    timings["total_s"] = round(time.time() - t_start, 3)

    # Before the verdict, because a resource that did not arrive is very often
    # the reason for the verdict, and it is never itself a reason to fail.
    notes.extend(res.notes())
    # And what did arrive, for the same reason in reverse: a person deciding
    # whether to publish this sketch is deciding whether to send every visitor
    # to whatever host is named here (media-assertion.md DECIDE[image-hosts]).
    notes.extend(res.arrival_notes())

    # is_looping is deliberately absent from FAILABLE_CHECKS: a sketch that
    # calls noLoop() declares itself static and the gate believes it.
    failed = any(checks.get(k) is False for k in FAILABLE_CHECKS) \
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
    # Only when there is a file: an entry gated before 2026-09-21, or one whose
    # window the budget stopped, has no ghost.png, and the gallery and the
    # operator's pages both read this to decide whether to show one.
    if ghost is not None and ghost.get("png"):
        artefacts["ghost"] = ghost["png"]
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
        "resources": res.failures,
        # Every off-origin image that arrived: {url, host, type, bytes, ms}.
        # Empty on a sketch that drew everything itself, and on every report
        # written before 2026-09-22. What loads(image) reads, and what the
        # entry page publishes as host and type only (Packet 20).
        "resources_loaded": res.loaded,
        "console": rec.entries,
        # Which pointer played, whose it was, and how much of it got played:
        # null on a run with --no-ghost and on every report written before
        # 2026-09-21. `ms` is the script's own clock, not wall time -- the
        # virtual millisecond the window reached, which is what falls short
        # when the budget stops it.
        "ghost": ({key: ghost[key] for key in
                   ("source", "script", "events", "played", "ms")}
                  if ghost is not None else None),
        # What `revised` was measured against and what it counted: null on a
        # run without --revises and on every report written before 2026-09-24.
        "revision": revision["record"] if revision is not None else None,
        "artefacts": artefacts,
        "exit": code,
    }
    text = json.dumps(report, indent=2, sort_keys=False)
    with open(out_dir / "report.json", "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    if a.json:
        sys.stdout.write(text + "\n")
    return code


def image_verdict(loaded, failure_notes, flat, sought_outside, timeout_s):
    """loads(image)'s pass/fail and its sentence, from five plain facts.

    Separated from :func:`evaluate_assertion` because the four ways this misses
    are four different sentences and each one is a thing the next attempt reads
    and acts on: entry 429 spent eight attempts rewriting handlers that already
    worked because nothing it was shown mentioned the image. A browser is not
    needed to check what they say, and tests/test_gate_fixtures.py does.

    *loaded* is ResourceLog.loaded, *failure_notes* its notes(); *flat* is
    g.flat('t20') -- True, False, or None for no canvas at all; *sought_outside*
    is whether the page made any off-origin image request, which is how the
    data: case is told from a sketch that simply never tried.
    """
    if flat is None:
        # No canvas at the end of the idle window. For a preload() sketch that
        # is the image: the sketch has not reached setup() yet, and the lines
        # below are the sentence entry 429 never got.
        detail = ("no canvas after %g s (preload never finished)" % timeout_s)
        if failure_notes:
            detail += ": " + "; ".join(failure_notes)
        return {"pass": False, "detail": detail}

    if loaded:
        arrived = ", ".join(
            "%s (%s, %s, %s)" % (item.get("host") or "an unnamed host",
                                 item.get("type") or "an unnamed type",
                                 human_bytes(item.get("bytes")),
                                 "timing unknown" if item.get("ms") is None
                                 else "%d ms" % item["ms"])
            for item in loaded)
        head = "%d image%s arrived: %s" % (len(loaded),
                                           "" if len(loaded) == 1 else "s",
                                           arrived)
        if flat:
            # The weakest honest half of the check (DECIDE[image-pass]): this
            # runner cannot see WHAT is drawn, only that something is. One flat
            # colour is the canvas entry 429 published -- the image arrived and
            # nothing was done with it.
            return {"pass": False,
                    "detail": head + "; but the canvas at the end of the idle "
                                     "window is one flat colour, so nothing was "
                                     "drawn with it"}
        return {"pass": True, "detail": head + "; canvas drawn"}

    if not flat and not sought_outside and not failure_notes:
        # Nothing arrived, nothing failed, and the page never asked anyone for
        # an image -- yet there is something on the canvas. p5 1.11.3 sets
        # crossOrigin, so this is not a tainted fetch; it is a picture the
        # sketch made or carried itself. The gate cannot prove a data: URI was
        # drawn, because a data: URI is not a resource and leaves no entry, but
        # it can say the web was not used.
        return {"pass": False,
                "detail": "the only image is a data: URI, which is not the web; "
                          "the page made no request for an image outside itself"}

    detail = "no image arrived from outside the sketch"
    if failure_notes:
        detail += ": " + "; ".join(failure_notes)
    return {"pass": False, "detail": detail}


def evaluate_assertion(page, kind, params, state, tone_brightness, silent_ref,
                       resources=None, timeout_s=None):
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

    if kind == "loads(image)":
        loaded = list(resources.loaded) if resources is not None else []
        failure_notes = resources.notes() if resources is not None else []
        flat = page.evaluate("() => window.__gate.flat('t20')")
        fetched = page.evaluate("() => window.__gate.fetched()") or []
        # initiatorType 'img' is what an <img> src is, which is what p5's
        # loadImage() builds; anything the sketch fetched by hand and decoded
        # itself counts too, because it is still the sketch going to the web.
        # data: and blob: are excluded for the reason ResourceLog._from_the_web
        # excludes them: p5 1.11.3 fetches a data: URI and then hands an <img>
        # a blob: URL, and Chromium records both, so a sketch that carried its
        # own picture would look like a sketch that went to the web for one.
        sought_outside = any(
            str(entry.get("kind")) in ("img", "image", "fetch", "xmlhttprequest")
            and str(entry.get("name") or "").startswith(("http://", "https://"))
            and not str(entry.get("name") or "").startswith(SKETCH_ORIGIN)
            for entry in fetched)
        return image_verdict(loaded, failure_notes, flat, sought_outside,
                             timeout_s if timeout_s is not None else 0.0)

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
