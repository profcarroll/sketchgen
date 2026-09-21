# rig — looking at a sketch on the laptop, before the node sees it

**The rig is advisory. The gate's number is the number, and on the node it runs
about 1.5× the rig's.** Nothing here decides anything: a sketch passes when
`gate/sketch_gate.py` says so, on the node, in headless Chromium with no GPU.
What this directory gives you is a way to look at a sketch and get a floor under
its cost before you spend an attempt on it.

It exists because on 2026-09-21 the agent driving job 1286 (entry 1279) built
all of it from scratch — a static server, a pinned p5, a harness page, a CPU
benchmark, a robustness probe — and spent 37 minutes and 193,000 generated
tokens doing so, most of it before the node knew the job existed. The dossier is
`docs/plans/agentic_cli_test_feedback_dossier_01.md`; this is its §5.4, staged so
nobody builds it twice.

**Packet 7 is coming and replaces most of this.** `bin/sg paid try --job N --as
$ME` will send the text you would import and run the *real* gate on the node,
between claims, returning the checks, the assertions, `ms_per_frame` and the
strip. When it lands, the only reason to come back here is interactive looking:
watching the thing move, and poking it by hand.

## The recipe

```bash
bash rig/fetch-p5.sh              # p5 1.11.3 from cdnjs, sha256-pinned, refuses otherwise
```

Then, in the harness: start the preview from `.claude/launch.json` (the `rig`
configuration — python's `http.server` on this directory; it is tracked, so
there is nothing to create and nothing to delete afterwards), `resize_window` to
1280×900, and **select the tab**. Write your sketch to `rig/sketch.js`;
`index.html` loads it exactly as the node's does. Paste `rig/probe.js` into the
console and run `await __probe.all()`, then paste `rig/bench.js` and run
`await __bench.run()`.

`rig/p5.min.js` and `rig/sketch.js` are both gitignored: after the recipe,
`git status` is clean.

`rig/cost.py` is the other half, and has nothing to do with the browser:
`python3 rig/cost.py --since ISO` totals what the session has cost from the
transcript Claude Code already writes, and prints one JSON line to paste into a
packet once Packet 8 accepts it.

## The six traps

Each cost the job-1286 agent time, and every one of them is a property of the
laptop's browser pane, not of the sketch.

1. **`file://` pages are not scriptable in the pane.** Serve the directory over
   http — that is all `.claude/launch.json` is for.
2. **A hidden pane runs no `requestAnimationFrame` and reports `windowWidth` 0
   at load**, and an unguarded sketch threw NaN every frame and never came back.
   Select the tab before anything is measured, and keep the arithmetic finite at
   0×0 (`probe.js` checks both).
3. **The pane sits at device pixel ratio 1.75**, so p5 picks density 2 and every
   screenshot is a downscaled device frame; one read as a bug in the sketch. The
   gate is DPR 1.
4. **Timing the pane's GPU canvas is meaningless.** `getImageData` after each
   frame adds a ~20 ms flush floor, and one identical variant read 14.7 ms and
   then 38 ms. Time an offscreen `willReadFrequently` 2d context instead — that
   is what `bench.js` swaps in, and it is what showed a gradient backdrop
   costing 5.7 ms a frame, as much as the whole rope.
5. **The console tool keeps errors across reloads**, so a stale error reads as a
   new one. Trust the hook `probe.js` installs after the reload, not the list.
6. **A synthetic click dispatched to both the canvas and `window` fires p5's
   handler twice.** Dispatch to the canvas only; `probe.js` does.

## What the gate does, and where each fact lives

Read from the repository, not from memory. A test (`tests/test_rig.py`) keeps
the numbers in this table tied to the constants they came from, so the table
cannot drift from the gate.

| fact | value | where |
| --- | --- | --- |
| viewport | 1280×900, device pixel ratio 1 | `gate/sketch_gate.py` `VIEWPORT` |
| idle window | 120 stepped frames on a virtual 16.667 ms clock, then one click at the canvas centre, then 30 more frames | `gate/sketch_gate.py` `IDLE_FRAMES`, `PROBE_FRAMES`, `click_centre` |
| assertions | `motion(idle)` and `responds(click)` pass on any changed pixel | `gate/sketch_gate.py` `evaluate_assertion` |
| QA | no console error; the loop keeps advancing; mean wall time per frame ≤ 100 ms, tripping early at 2× it, under a 90 s ceiling on the whole run | `gate/sketch_gate.py` `DEFAULT_FRAME_BUDGET_MS`, `EARLY_TRIP_FACTOR`, `DEFAULT_BUDGET_S` |
| library | p5 1.11.3, in the `index.html` the executor writes; `rig/index.html` is that page with the one script line pointed at the local copy | `sketchgen/executor.py` `_P5_TAG`, `DEFAULT_INDEX_HTML` |
| rules | global mode, `createCanvas(windowWidth, windowHeight)` with a `windowResized()`, ~150 lines, ~2,000 shape calls a frame in 2D | `prompts/rules/treatment.md`, as quoted in your packet |

## How far off the rig is: `MEASURE[rig-proxy-vs-gate]`

One point so far. The sketch that became entry 1279 read **6.4 ms a frame** on
the laptop's CPU canvas and **9.8 ms** on the node's gate (`meta.json`): the rig
under-reads the ARM node by about a third, which is why `bench.js` multiplies by
1.5 before comparing with the budget. Treat its number as a floor and keep real
headroom under 100 ms.

If you use the rig, add your row.

| entry | rig, ms/frame | gate, ms/frame | ratio | note |
| --- | --- | --- | --- | --- |
| 1279 | 6.4 | 9.8 | 1.53 | job 1286, `claude-sonnet-5`, 2026-09-21; 2D, 2,560 curve pieces |
