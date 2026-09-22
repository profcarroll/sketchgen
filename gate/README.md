# The gate

The deterministic referee between a model's sketch and the gallery. Nothing
reaches a person until this has run, which is the whole reason the pipeline can
be left alone overnight.

Until 2026-09-15 this directory existed **only** on the node, at
`~/sketchgen/gate/`. That made the gate the one piece of the pipeline that a
reclaimed free-tier instance would have taken with it. It is tracked here now,
and the repo is the copy that leads: the three copies — here, the node, the
course repo at `sld-fall-2026/examples/week11-self-hosted-ai/sketch-gate/` —
share one sha256, recorded as `GATE_SHA256` in `tests/test_gate_fixtures.py`,
and the other two match it once this branch is deployed. The frame budget below
changed that hash, and so did the ghost window; until `update.sh` runs on the
node, the node's copy is the one before it.

## What it checks

`sketch_gate.py` loads one sketch directory (`index.html` plus `sketch.js`) in
headless Chromium through Playwright's sync API and reports six fixed checks on
every sketch:

| check | false means |
|---|---|
| `console_clean` | the page threw, at load or at any frame up to the end of the idle window |
| `is_looping` | the sketch called `noLoop()`; a **declaration**, not a verdict, and it never fails a run on its own |
| `frame_advancing` | `frameCount` stopped moving in the second half of the idle window |
| `sound_lib_ok` | the sketch asked for p5.sound and did not get it |
| `audio_context_running` | audio was started without a user gesture, so the context is suspended |
| `frame_budget` | one frame of the idle window cost more wall time than `--frame-budget-ms`, or the whole run passed the `--budget-s` ceiling |

## What may fail a run, and what may not

The gate reports two different kinds of thing and they are not the same kind of
thing at all.

`FAILABLE_CHECKS` is **quality assurance**: the page threw, the sketch froze, the
addon it asked for is missing, audio started without a gesture, a frame cost more
than the budget. A visitor meets the result of every one of these. They keep
their teeth and always will.

The **assertions** are a different question: does this sketch match a brief
another model wrote? A sketch that misses one is not broken. It is a different
sketch from the one that was predicted, which is sometimes a mistake and
sometimes the only interesting thing that happened all day.

`sketchgen/worker.py` now tells the two apart. A job that spends its attempts
without ever failing a QA check goes to `held` — the same queue a clean pass goes
to — with the assertions it missed recorded in `entries.offplan_json`, and a
person decides. Only a QA failure still ends a job as `failed-kept`.

This was measured before it was changed: of the first 65 failed jobs, **39 had an
attempt that passed every check above** and was thrown away for missing an
assertion. Among them a sketch that studied Chuck Close and went to Wikimedia for
the real paintings, and one that worked out it could build its own image as a
`data:` URI when it could not fetch one.

The gate itself is unchanged by this. It still reports exactly what it sees and
still exits 1 on either kind of miss; what changed is that the worker no longer
treats both as the end of the road.

## Resources the sketch did not get

`report.json` carries a `resources` list beside the checks: every URL outside
`SKETCH_ORIGIN` that the page asked for and did not receive, each with the
browser's own reason (`net::ERR_FAILED`, `HTTP 404`). The same lines appear in
`notes`.

**It is not a check and it never fails a run.** A sketch may reach outside
itself for an image, a font or a library, and one that works out that it can has
worked something out. This exists because failing to *arrive* was invisible: a
failed image is not a page error, so `console_clean` stays true, the canvas
stays blank, and the only evidence is every assertion reading zero pixels
changed — which reads as broken interaction code.

Entry 429, a jigsaw puzzle, loaded `https://picsum.photos/400/400` in
`preload()` across eight attempts. Every run took eleven seconds and drew
nothing, and the model spent those attempts rewriting handlers that already
worked, because nothing it was shown mentioned the image.

## The frame budget

Added 2026-09-15, after job 166 (entry 165) and job 270 (entry 269). Both drew
about 1,500 `sphere()` meshes and tens of thousands of immediate-mode `line()`
calls per frame in WEBGL; both passed every check above; both took minutes of
gate time (209 s and 284 s against a median of 2 s) and pinned about 4 GB of GPU
buffers when the operator opened the preview, which took the laptop down. The
gate had timed that window all along and never read the number. `gate.png` looks
fine, so nothing a person could see was wrong.

    --frame-budget-ms MS    default 100
    --budget-s S            default 90

`timings.ms_per_frame` in `report.json` is the wall time this runner spends
inside `__gate.step()` over the 120-frame idle window, divided by the frames it
stepped. The runner's own work — the snapshots between the three step calls, the
PNG readbacks, the assertion diffs — is outside the timed stretch, and so are
the priming frames and the probes after the window; only the deadline applies to
those.

**Where 100 ms came from.** Per-frame cost derived from the 371 gate reports on
the node on 2026-09-15 — `(total_s - launch_s - load_s - settle) / frames
stepped` — has a median of 12.0 ms, of which about 3.3 ms is the runner's own
fixed overhead spread over the window. The median sketch therefore costs roughly
9 ms a frame and clears 100 ms by more than ten times. Forty-six of the 371
derive above 100 ms, and the top of that list is job 45 (3,311 ms), job 270
attempt 1 (1,867 ms) and job 166 attempt 3 (1,370 ms) — the sketches this check
exists for. 100 ms is also three frames at 30 fps: past it a sketch has stopped
being animation.

**Where 90 s came from.** Eight of those 371 runs took longer than 90 s and all
eight are sketches the budget should refuse; the median run is 2.4 s. The
ceiling is far below the worker's own 600 s subprocess timeout on purpose — a
run killed out there writes no `report.json` at all, and the evidence the next
attempt got from that was *"gate did not finish"*, a sentence with no cause in
it.

The run stops early rather than finishing the window in two cases: when the wall
deadline passes, and when the running mean is already more than twice the budget
(`EARLY_TRIP_FACTOR`). A chunk of frames is the finest grain available — the
`page.evaluate` that steps it cannot be interrupted from here — so a very
expensive sketch can overshoot the ceiling by up to one chunk. Below twice the
budget the window is always finished, because the check is about the mean over
the window and one expensive first frame is not the bug being looked for.

The evidence sentence names the number, the window and the budget, and then what
to do about it:

    frame_budget: 1093 ms per frame over the first 30 frames of the 120-frame
    idle window, budget 100 ms — batch points and lines into one shape, no
    sphere() per particle, no all-pairs loop

Then it evaluates the small closed vocabulary of assertions the planner is
allowed to choose from (`--assert motion(idle)`, `responds(click)`,
`no_motion`, and the rest), and writes `gate.png`, `strip.png`, `ghost.png`,
`console.log` and `report.json` into `--out` (default `<sketch_dir>/.gate`).

## The ghost window

Added 2026-09-21, for entry 1103 and the 27 published entries like it. A
jigsaw puzzle asserts `no_motion`, `responds(click)` and `responds(drag)`, and
on the kiosk it is a photograph cut into pieces that never move, because nobody
is at the keyboard. The gate was no better off: one click at the canvas centre
and one drag across the middle were all the interaction a sketch ever got, so
the fourth frame of `strip.png` was the only interactive evidence a judge or a
critic ever saw.

So, after every check and every assertion has been decided, the gate plays a
pointer script through `page.mouse` — real Chromium input, stepping the virtual
clock between events by the gap in their timestamps — and writes four frames of
it, at the quartiles of the script's own duration, as `ghost.png`. The script
is the sketch's own `ghost.json` when it wrote one, and otherwise the built-in
its requested assertions name: `click` for `responds(click)`, `drag` for
`responds(drag)`, both where both are asked, and `wander` for a sketch that
asked for neither. `report.json` gains a `ghost` object saying which it was and
how much of it played.

**It decides nothing, and that is the point.** `strip.png` and `gate.png` are
byte-for-byte what they were before it existed: both judge populations and the
critic compare against them, `pairs.artefact_hash` is over them, and every
published `meta.json` names them, so changing one mid-corpus would split every
measurement taken so far. The assertions are evaluated before the window opens
and against the snapshots taken before it, so `responds(click)` is exactly as
strong as it was. `console_clean` is read at the moment the window opens and
not through it — a synthetic pointer clicking where the probe did not may not
turn a sketch that passed into a failure — and a budget that runs out inside
the window is a note and no `ghost.png`, never a failed check. The window does
count against `--budget-s`, like every other probe, and sits outside the
counted idle window, so `timings.ms_per_frame` is the same number it always
was. `--no-ghost` skips it altogether.

The script shape, the caps (64 events, 8,000 ms) and the three built-ins are
`sketchgen/ghostshim.py`'s, copied into this file the way `SOUND_RE` is —
this directory imports nothing from the package — with
`tests/test_gate_fixtures.py` pinning the two copies together. The other player
is the shim itself, inside the published page, which is how the kiosk moves the
same sketch on a projector.

Exit codes are the project's: **0** everything that can fail passed, **1** a
check or an assertion failed, **3** refused — no browser, an unknown assertion
word, an unusable sketch directory.

```
python3 gate/sketch_gate.py <sketch_dir> [--assert WORD ...] [--seed N]
                            [--out DIR] [--timeout S] [--json]
                            [--frame-budget-ms MS] [--budget-s S]
                            [--no-ghost]
```

Determinism is bought with a virtual clock, hand-stepped frames, seeded
`random()`/`noise()`/`Math.random`, and a `page.route()` interception that
serves the sketch from `http://sketch.localhost` without opening a socket. The
long comment at the top of `sketch_gate.py` explains each of those and says
where determinism stops; read it before changing any sampling window.

## The fixtures and `accept.sh`

`fixtures/` holds eight sketch directories, each one a bug the gate was built to
catch (or a clean pass it must not fail), and `fixtures/expected.json` records
for each the expected exit code, the expected value of the checks that fixture
is about, the assertions a planner would have chosen for it, and a note saying
*why that fixture exists*. Three of them are subtle enough to be worth naming
here: `bad-audio-no-gesture` passes everything a reader can check and is caught
only by the suspended `AudioContext`; `bad-frozen` passes every fixed check
while drawing nothing, so only `motion(idle)` catches it — which is why its
expected exit is 0; and `bad-frame-budget` passes every check the gate had
before 2026-09-15 and still takes about a third of a second to draw one frame,
which is job 166's bug with nothing else wrong with it.

A fixture's expected `checks` object need not name every check — only the keys
it lists are compared. `bad-frame-budget` uses that: the budget stops its run
before `is_looping` and `frame_advancing` are read at the end of the idle
window, so on a slow machine they come back null and on a fast one true, and
neither is what the fixture is for. An optional `ghost` object is compared the
same way, and `ghost-echo` is the fixture that has one: a `no_motion` sketch
that paints a dot where it is clicked and a line where it is dragged, with a
`ghost.json` of its own, so `source: executor` and `played` equal to `events`
is the harness saying the gate played the sketch's own script and all of it.

`accept.sh` is the harness: for every fixture it runs the gate twice, once plain
for the fixed checks and once with that fixture's assertions, compares both
against `expected.json`, and prints one `PASS` or `MISMATCH` line each with a
summary and a non-zero exit on any mismatch.

```
. ~/sketchgen/.venv/bin/activate     # it needs playwright on PATH
./gate/accept.sh                     # or: ./accept.sh [fixtures_dir] [artefact_dir]
```

The fixtures load p5.js from a CDN, so `accept.sh` needs the network as well as
a browser.

`tests/test_gate_fixtures.py` runs exactly this harness from the ordinary test
suite when Playwright is importable, and skips cleanly when it is not — which is
every machine that has not built the venv. A skip there is not a pass; run
`accept.sh` on the node after touching anything in this directory.

## Where the pipeline finds it

`sketchgen/worker.py` reads `$SKETCHGEN_GATE`, and the units set it to
`%h/sketchgen/app/gate/sketch_gate.py` — this copy. See the *The gate lives in
the repo* section of `docs/OPERATIONS.md` for the one-release symlink that keeps
the old path working.
