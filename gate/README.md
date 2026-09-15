# The gate

The deterministic referee between a model's sketch and the gallery. Nothing
reaches a person until this has run, which is the whole reason the pipeline can
be left alone overnight.

Until 2026-09-15 this directory existed **only** on the node, at
`~/sketchgen/gate/`. That made the gate the one piece of the pipeline that a
reclaimed free-tier instance would have taken with it. It is tracked here now;
the copy on the node is byte-identical (sha256 of `sketch_gate.py`:
`426e8e7429984b1f5377c068886bebeaab8795a2a102a687c06938bb6a66b658`, the same as
the copy in the course repo at
`sld-fall-2026/examples/week11-self-hosted-ai/sketch-gate/`).

## What it checks

`sketch_gate.py` loads one sketch directory (`index.html` plus `sketch.js`) in
headless Chromium through Playwright's sync API and reports five fixed checks on
every sketch:

| check | false means |
|---|---|
| `console_clean` | the page threw, at load or at any frame up to the end of the idle window |
| `is_looping` | the sketch called `noLoop()`; a **declaration**, not a verdict, and it never fails a run on its own |
| `frame_advancing` | `frameCount` stopped moving in the second half of the idle window |
| `sound_lib_ok` | the sketch asked for p5.sound and did not get it |
| `audio_context_running` | audio was started without a user gesture, so the context is suspended |

Then it evaluates the small closed vocabulary of assertions the planner is
allowed to choose from (`--assert motion(idle)`, `responds(click)`,
`no_motion`, and the rest), and writes `gate.png`, `strip.png`, `console.log`
and `report.json` into `--out` (default `<sketch_dir>/.gate`).

Exit codes are the project's: **0** everything that can fail passed, **1** a
check or an assertion failed, **3** refused — no browser, an unknown assertion
word, an unusable sketch directory.

```
python3 gate/sketch_gate.py <sketch_dir> [--assert WORD ...] [--seed N]
                            [--out DIR] [--timeout S] [--json]
```

Determinism is bought with a virtual clock, hand-stepped frames, seeded
`random()`/`noise()`/`Math.random`, and a `page.route()` interception that
serves the sketch from `http://sketch.localhost` without opening a socket. The
long comment at the top of `sketch_gate.py` explains each of those and says
where determinism stops; read it before changing any sampling window.

## The fixtures and `accept.sh`

`fixtures/` holds six sketch directories, each one a bug the gate was built to
catch (or a clean pass it must not fail), and `fixtures/expected.json` records
for each the expected exit code, the expected value of all five checks, the
assertions a planner would have chosen for it, and a note saying *why that
fixture exists*. Two of them are subtle enough to be worth naming here:
`bad-audio-no-gesture` passes everything a reader can check and is caught only
by the suspended `AudioContext`, and `bad-frozen` passes every fixed check while
drawing nothing, so only `motion(idle)` catches it — which is why its expected
exit is 0.

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
