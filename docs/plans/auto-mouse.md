# The ghost at the kiosk: synthetic input for sketches that wait to be touched

Build plan for a ghost pointer — mouse movement and clicks the kiosk sends into a sketch that
does nothing until it is touched — and for the same script replayed by the gate, so the strip,
the judge and the critic see the sketch a viewer would have seen. Grounded in the code as it
stands at `main` 1c39d24 (#139). Written for Opus builders; one packet per branch, one PR each,
numbering continues from `docs/plans/held-batch.md` (packets 10–12).

*Status: Packet 13 is built — branch `feat/ghost-shim`, PR #143: `sketchgen/ghostshim.py`,
the kiosk's `?ghost=` and `responds` in `kiosk.json`. Packet 14 is built — branch
`feat/ghost-dataset`, PR #144: the `ghost` block, `executor.validate_ghost`, `ghostshim.with_script`,
`meta.json`'s `ghost`, and the bullet in AGENTS.md. Packet 15 is built — branch
`feat/ghost-gate`: the gate's ghost window and `ghost.png`, `HARNESS_VERSION` 3, the
`ghost-echo` fixture, and the file on the entry page, the attempt page and a `try`
verdict. Packet 16 is built — branch `feat/ghost-critic`: `lineage.critic_images`
and the `images: strip ghost` header line, the two-image payload, `ghost_path` on a
paid critique item, and the operator's recipe for cutting `critic-v4`; option 1, and
`prompts/critic.md` is not touched. Two things are corrected
where they stand: `DECIDE[ghost-off]` says the key is `G`, and `G` is the generation
overlay — the key built is `M`; and §5.1 does not say what `console_clean` is read over,
which turned out to matter (see the dated note there).*

## 0. What is wrong, in one paragraph

The kiosk puts a published sketch on a projector for sixty seconds and moves on
(`sketchgen/assets/kiosk.js`, `DEFAULT_EVERY`). A sketch built around input — a puzzle, a
drawing tool, a field that scatters under the cursor — sits still for the whole minute, and on a
wall with nobody at the keyboard it reads as blank or broken. The plan for the kiosk decided
this on purpose: *the kiosk does not try to forward a click* (`docs/plans/kiosk.md` §1.7), and
the reason still holds — the frame is `sandbox="allow-scripts"` with no `allow-same-origin`, so
it is an opaque origin, and nothing the kiosk page does can reach into it. `swipe.json` counts
how many entries this touches: *27 entries respond to a click, 25 to a drag, 7 to audio, of 910*
(`docs/plans/swipe.md` §133–137). Entry 1103, a jigsaw puzzle, is one of them: `no_motion`,
`responds(click)`, `responds(drag)`, and on the kiosk a photograph cut into pieces that never
move. The same blindness reaches the gate. Its one real click at the canvas centre
(`gate/sketch_gate.py:891`) and one drag (`:902`) are all the interaction a sketch ever gets, so
the fourth frame of `strip.png` is the only interactive evidence the judge and the critic see,
and `responds(click)` cannot fail a sketch that moves on its own
(`MEASURE[click-assertion-power]`, dossier 01 §7.3).

## 1. Decisions, with recommendations

Each is decided here unless the operator overrides; builders take the recommendation.

| id | decision | recommendation |
| --- | --- | --- |
| `DECIDE[ghost-where]` | the kiosk page synthesises events, or a script inside the frame does | **Inside the frame.** A parent cannot dispatch into an opaque-origin iframe at all, and `kiosk.md` §5 pins *one iframe with `sandbox="allow-scripts"` and no other attribute*, which the tests enforce. A shim injected into `e/<id>/sketch/index.html` at render time — exactly as `soundshim.py` does for p5.sound — runs inside the sketch's own document, where the canvas and its `getBoundingClientRect()` are in reach, so the coordinate problem that forced `_canvas_size` and the swipe shield does not arise. |
| `DECIDE[ghost-channel]` | how the kiosk tells the frame to run the ghost | **A query string on the frame's `src`: `sketch/?ghost=click,drag`.** One-way, read once at load, nothing after. `kiosk-views.md` §3.5's *no `postMessage`* stands; the shim is inert without the parameter, so the entry page, swipe and the operator's preview load the same bytes and never ghost a real viewer. |
| `DECIDE[ghost-script]` | what a ghost script is | **A JSON list of events in canvas fractions:** `[{"t": 800, "type": "move", "x": 0.5, "y": 0.5}, {"t": 1200, "type": "down", …}, {"t": 1300, "type": "up", …}]`, `t` in milliseconds from start, `type` one of `move`, `down`, `up`, `click`, `x`/`y` in `[0, 1]` of the canvas. At most 64 events, `t` at most 8,000. Two players read it: the shim (DOM `MouseEvent`s on the canvas) and the gate (`page.mouse`). Three built-in scripts, named `click`, `drag`, `wander`, live in the shim and are what an entry gets when it carries no script of its own. |
| `DECIDE[ghost-who]` | which entries the kiosk ghosts | **Entries whose gate-confirmed assertions include `responds(click)` or `responds(drag)`**, the rule `_swipe_entry` already applies (`sketchgen/gallery.py:2849`, off-plan misses subtracted). Never `responds(audio)`: a synthetic event is not a user gesture and cannot resume an `AudioContext`; §1.7's sentence stays true for sound and is rewritten to say so. |
| `DECIDE[ghost-yields]` | what happens when a person touches the sketch | **The ghost stops for the rest of that entry.** The shim sees every pointer event on the document; one with `isTrusted` true ends the run. A hand always wins, and nobody watches a cursor fight them. |
| `DECIDE[ghost-off]` | the off switches | **Three, as `kiosk_views` has:** `config.json` `kiosk_ghost: false` (a render-index, no deploy), `?ghost=0` on the kiosk URL, and a key on the kiosk. ~~`G` (free per `kiosk-fullscreen.md` §5)~~ — **`M`, corrected 21 September 2026:** `G` is not free, it is the *generation and lineage* overlay (`kiosk.js` `OVERLAYS`), and `kiosk-fullscreen.md` §5 is its test section and says nothing about keys. `M` for the mouse nobody is holding; `X` and `Y` both stay the test harness's openers. |
| `DECIDE[ghost-strip]` | whether the gate's `strip.png` changes | **No. A second artefact, `ghost.png`, four frames from the ghost window, and `strip.png` byte-for-byte what it is today.** Both judge populations and the critic see `strip.png` (`judge.py:362`, `lineage.py:202`), `pairs.artefact_hash` is over it, and every published `meta.json` names it. Changing it mid-corpus splits every measurement; adding beside it splits none. Who reads `ghost.png` is Packet 16's question. |
| `DECIDE[ghost-dataset]` | whether the executor may supply the script | **Yes, optionally, as a fenced block tagged `ghost` in its reply — asked of paid agents first, through AGENTS.md, and of local models only as `executor-v4`.** `parse_response` already counts and ignores blocks it does not know (`executor.py:305`), so accepting one changes no contract; asking a local model for one changes the prompt file and is an arm in the rules-file A/B. |
| `DECIDE[click-power]` | whether to fix `responds(click)` while the gate is open | **Not in this series.** Packet 15 bumps `HARNESS_VERSION` once for the ghost window; folding an assertion change into the same bump is tempting and wrong until `MEASURE[click-assertion-power]` has a number. Left in §7. |

## 2. What every packet inherits

- Repository: `profcarroll/sketchgen`, laptop clone `/home/dave/sketchgen`, node checkout
  `~/sketchgen/app`. Python 3.12, stdlib only. Tests: `python3 -m unittest discover -s tests`
  from the repo root, ~75 s, no network, no model, no browser; the JS that runs is driven by
  `node` from `tests/test_gallery_js.py` with the DOM stub in `tests/js/dom.js`, and the kiosk's
  own harness is `tests/js/kiosk.js`. Anything needing Playwright is a skip that names the node.
- AGENTS.md rules 1–5 bind the builder. Nothing here needs a worker, a database edit or a
  push to `main`.
- The gate is hash-pinned: any edit to `gate/sketch_gate.py` changes `GATE_SHA256` in
  `tests/test_gate_fixtures.py:56`, and `rig/README.md`'s facts table is tied to the gate's
  constants by `tests/test_rig.py`. A packet that touches the gate updates both in the same
  commit and runs `gate/accept.sh` on the node before its PR is opened.
- Comments say why, with the incident or date; commit messages are prose. The dates in this
  file are the ones to cite.
- Kiosk invariants that stay true (`kiosk.md` §4.5, §5; `kiosk-views.md` §3.5): one iframe at a
  time, `sandbox="allow-scripts"` and no other attribute, no `postMessage`, no `eval` of sketch
  source, no storage key but `sketchgen-kiosk`. A ghost script is data the shim reads, not
  source the kiosk evaluates.

## 3. Packet 13: the shim and the kiosk

**Branch** `feat/ghost-shim`. About a day and a half.

### 3.1 `sketchgen/ghostshim.py`

Modelled on `soundshim.py` line for line: a `MARKER` (`    <script>/* ghost pointer */`), a
`SHIM` string, and `with_shim(html)` that inserts the shim once, after the `sketch.js` tag —
after, because p5 attaches its handlers to `window` when `sketch.js` runs, and the shim only
dispatches. Idempotent on the marker; a no-op when the page has no `sketch.js` tag. Two
callers, as for the sound shim: `executor.index_html_for` (`executor.py:111`), so the gate runs
the bytes that are published, and `gallery._write_entry` (`gallery.py:2235`, beside the
`soundshim.with_shim` call at `:2263`), so entries published before the shim existed pick it
up on the next render-all. `with_shim` is applied to an executor-supplied `html` block too:
a page with p5.sound and its own tags still gets the ghost.

The shim, in plain script, no module:

1. Reads `location.search`. No `ghost` parameter, or `ghost=0`: return before touching the
   DOM. This is the whole guarantee that the entry page, swipe and the preview are unchanged.
2. Chooses a script: `window.__ghostScript` if Packet 14 inlined one, else the built-in named
   by the parameter (`click`, `drag`, `wander`, or a comma list played in order). Built-ins:
   - `click`: move to centre over 400 ms, `down`, `up` 120 ms later, then three more clicks at
     the golden-ratio points of the canvas, 900 ms apart;
   - `drag`: move to 20 %/50 %, `down`, twelve `move`s to 80 %/50 % over 800 ms, `up`; then
     the same from 50 %/20 % to 50 %/80 %;
   - `wander`: a Lissajous path of 40 `move`s over 4 s, no button.
3. Waits for a canvas (`document.querySelector('canvas')`, polled at 100 ms for up to 10 s;
   a `preload()` sketch has none until its assets arrive), then plays the script on
   `setTimeout`s from the first canvas sighting, computing `clientX`/`clientY` from
   `canvas.getBoundingClientRect()` and the event's fractions, and dispatching
   `new MouseEvent(kind, {bubbles: true, cancelable: true, clientX, clientY, button: 0,
   buttons})` on the canvas. `move` is `mousemove`; `down`/`up` are `mousedown`/`mouseup`;
   `click` is the three in order. p5 1.11 reads `clientX` against the canvas rect and sets
   `mouseIsPressed` from `mousedown`, so `mousePressed`, `mouseDragged`, `mouseReleased`,
   `mouseMoved` and `mouseIsPressed` all behave as for a hand; `isTrusted` is false, which is
   why audio cannot start (`DECIDE[ghost-who]`).
4. Loops: when the script ends, waits `loop_ms` (default 6,000; `ghost_loop=` in the query,
   the kiosk passes what `config.json` says) and plays it again, until the document sees a
   trusted `pointerdown` or `mousemove`, after which it never plays again in this load
   (`DECIDE[ghost-yields]`).
5. Never throws into the sketch's console: the whole player is in one `try` and a failure is
   `console.debug`, not `error`, so `console_clean` in the gate cannot be tripped by the shim.

### 3.2 `kiosk.json` and `kiosk.js`

- `gallery._kiosk_entry` (`gallery.py:2734`) gains `responds`, computed exactly as
  `_swipe_entry` does at `:2849` — the same `_responds` over the same off-plan subtraction, so
  the two manifests keep deriving from one `_manifest_base`. Present only when non-empty.
- `config.json` gains `kiosk_ghost` (default `true`) and `kiosk_ghost_loop_s` (default 6),
  read where `kiosk_views` is (`gallery.py` `Config`; `kiosk.js:338` for the read pattern).
- `kiosk.js` `showEntry` (`:655`): when the entry's `responds` has `click` or `drag`, ghosting
  is on in config, not turned off by `?ghost=0`, and not toggled off by `G`, the frame's `src`
  is `ROOT + entry.sketch + "?ghost=" + kinds.join(",") + "&ghost_loop=" + ms`. Nothing else
  about the frame changes; the sandbox attribute is untouched. The header of the file lists
  the parameter beside `kiosk_views`.
- ~~`G`~~ **`M`** toggles `state.ghost` (see `DECIDE[ghost-off]`: `G` is the generation
  overlay), persisted in the existing `sketchgen-kiosk` key beside `every`
  and `order`; the menu shows *ghost pointer on/off*. The caption gets a two-word tag, *ghost
  pointer*, while an entry is being ghosted, in the same place *responds to touch* sits on
  swipe, so a viewer who sees movement knows nobody is at the keyboard.

### 3.3 The plan files

`docs/plans/kiosk.md` §1.7 is rewritten in place, dated, the way `kiosk-views.md` §1.5 was:
the click is not forwarded because it cannot be, a shim inside the frame plays it instead,
and sound still needs a hand. §4.5's prohibitions gain the line *dispatch nothing into the
frame; the frame's own shim does, on a query parameter*.

### 3.4 Tests

- `tests/test_ghostshim.py`: injection after the `sketch.js` tag, idempotence, the no-op
  cases, and the ordering test `tests/test_executor.py:110–118` already has for the sound
  shim extended to *p5 → addon → sound marker → `sketch.js` → ghost marker*.
- `tests/js/ghostshim.js`, driven by `tests/test_gallery_js.py`: the shim run under
  `tests/js/dom.js` with a stub canvas at a known rect and a fake timer queue; asserts the
  dispatched events' kinds, order, `clientX`/`clientY` for each built-in, that nothing is
  dispatched without the parameter, that a trusted `mousemove` stops the loop, and that a
  thrown handler surfaces as `console.debug`.
- `tests/js/kiosk.js`: an entry with `responds: ["click"]` seats a frame whose `src` ends in
  `?ghost=click&ghost_loop=6000`; one without `responds` seats the bare `src`; `G` and
  `?ghost=0` and `kiosk_ghost: false` each remove it; the sandbox assertion in §5 of
  `kiosk.md` still passes unchanged.
- `tests/test_gallery.py`: `kiosk.json` carries `responds` for a fixture entry with a
  confirmed `responds(click)` and omits it when the same assertion is in `offplan_json`.

### 3.5 Acceptance

1. On the laptop preview, entry 1103's frame under `?ghost=click,drag` shows pieces moving
   within two seconds of load, and the entry page for 1103 shows none.
2. A trusted click on the kiosk stage stops the ghost for that entry and it resumes on the
   next entry.
3. `render-index` alone (no `render-all`) is enough for the kiosk to start ghosting entries
   whose `sketch/index.html` already carries the shim; entries without it wait for the
   render-all in §8.
4. `git grep postMessage sketchgen/assets/kiosk.js` is still empty.

## 4. Packet 14: the executor's own script

**Branch** `feat/ghost-dataset`. About a day.

### 4.1 The block

An executor reply may carry a fenced block tagged `ghost` holding one JSON list in the
`DECIDE[ghost-script]` shape. `executor.parse_response` (`executor.py:305`) keeps the first,
counts extras into `result.json` as it does for `js` and `html`, and a new
`executor.validate_ghost(text) -> (events, reason)` refuses anything that is not a list of
objects with the four keys in range, more than 64 events, or a `t` past 8,000. An invalid
block is **not** a rejected reply: the sketch is still the answer, the block is dropped, and
`result.json` says `ghost: {"rejected": "…"}` so the evidence for the next attempt can
mention it. `executor.run` writes the accepted list to `<attempt>/ghost.json`.

### 4.2 Where it travels

- `worker._create_entry` copies nothing: the entry's `source_dir` already names the attempt
  directory and `ghost.json` sits beside `sketch.js`, the way `_canvas_size` finds the
  source (`gallery.py:758`). No migration.
- `gallery._write_entry` inlines it: when `<source_dir>/ghost.json` exists,
  `ghostshim.with_script(html, events)` writes `<script>window.__ghostScript = […]</script>`
  immediately before the shim's marker, and the shim prefers it (§3.1 step 2). It is also
  copied to `e/<id>/ghost.json` so a reader can see it, and `meta.json` gains
  `ghost: {"events": N, "by": "executor"}` or `{"events": N, "by": "default", "script": "click"}`.
- `paid try` and `paid import` carry it for free: both run the reply through
  `executor.run(stub=…)`.

### 4.3 Who is asked

- **AGENTS.md**, under *Answers → Execute*: one paragraph. *Optionally, a fenced block tagged
  `ghost`: the pointer script the kiosk and the gate play when nobody is at the keyboard, in
  canvas fractions, at most 64 events over 8 s. Write the gestures you tested. Without it the
  entry gets the built-in script for its assertions.* The paid agent has `try`, so it can
  watch its own script in `ghost.png` from Packet 15 before importing.
- **`prompts/executor.md` is not touched.** A local model is asked in `executor-v4`, a
  separate decision with its own A/B arm; the block is accepted from it already if it happens
  to emit one.

### 4.4 Tests

`tests/test_executor.py`: a reply with a valid `ghost` block parses to the same `js`, `html`
and statement as without, `ghost.json` is written, an invalid block is dropped with its reason
in `result.json`, and a reply with a `ghost` block and no `js` block is rejected as today.
`tests/test_gallery.py`: the inline script lands before the marker and `meta.json` says who
supplied it. `tests/test_paid.py`: an import whose answer carries the block records the
attempt with `ghost.json` present.

## 5. Packet 15: the gate plays it

**Branch** `feat/ghost-gate`. About a day and a half. Touches the gate, so `HARNESS_VERSION`
and `GATE_SHA256` move.

### 5.1 The ghost window

After the audio dwell and the last existing snapshot (`sketch_gate.py:1212` region), and
before `report` is built, the gate plays a script through `page.mouse`, real Chromium input:

1. The script is `<sketch_dir>/ghost.json` when present, else the built-in for the
   assertions requested (`click` for `responds(click)`, `drag` for `responds(drag)`, `wander`
   when neither is asked, so every entry gets a ghost window and a `ghost.png`). The
   built-ins are one shared definition: `ghostshim.py` owns them as Python data, renders them
   into the JS shim, and the gate imports nothing — it is a standalone script with its own
   copy on the node — so it carries its own copy, with the test in §5.4 pinning the two.
2. Each event is `page.mouse.move/down/up/click` at the canvas rect scaled by the fractions,
   and between events the virtual clock is stepped by the difference in `t` at 60 frames a
   second (`step(page, n)`, `:866`), so a script is deterministic under the same seed.
3. Four snapshots at the quartiles of the script's duration, `g0`…`g3`, and
   `ghost.png = __gate.strip(['g0','g1','g2','g3'])` written beside `strip.png`;
   `artefacts.ghost` in the report; `strip.png` and `gate.png` are unchanged
   (`DECIDE[ghost-strip]`).
4. The window counts against `--budget-s` like every probe (`Budget`, `:794`) but not
   against `ms_per_frame`, which stays the idle window's number. A script of 8 s at 60 fps is
   480 stepped frames, about the cost of four idle windows; `EARLY_TRIP_FACTOR` applies. A
   `BudgetExceeded` inside the ghost window is a note and ~~an empty `ghost.png`~~ **no
   `ghost.png` at all, 21 September 2026** — a nought-byte PNG is a file every reader has
   to learn to ignore, and `artefacts.ghost` is simply absent instead — never a check
   failure: the sketch had already passed or failed on the window that matters.
5. `report.ghost = {"source": "executor"|"default", "script": name|null, "events": N,
   "played": M, "ms": …}`.

Assertions are not evaluated against the ghost window (`DECIDE[click-power]`). The `click`
and `drag` probes stay exactly where and what they are, so every existing fixture's
`assertions_expected` still holds.

**`console_clean` is read at the moment the window opens, not through it — added
21 September 2026, in the build.** §5.1.4 says a `BudgetExceeded` inside the window is
never a check failure and stops there, but `console_clean` is judged over the whole run
at `sketch_gate.py:1220`, so a ghost that clicked where the probe did not could have
turned a sketch that passed into a `console_clean` failure on the strength of input no
person sent. That would contradict the one sentence §5.2 puts in the `HARNESS_VERSION`
docstring — *nothing it fails changed* — so `Recorder.clean_through` bounds the check at
the window, and everything the window logged is still in `console.log`, in the report's
`console` list and in a note. Whether a sketch that throws under a synthetic pointer
ought to fail is a real question; it is `MEASURE[click-assertion-power]`'s, not this
packet's.

### 5.2 Version, hash, facts

`HARNESS_VERSION = 3` in `worker.py:722`, with a line in its docstring: *3, from <date>: the
gate plays a ghost script after the probes and writes `ghost.png`; nothing it fails changed.*
`GATE_SHA256` updated. `rig/README.md`'s facts table gains the ghost window's constants
(`GHOST_MAX_EVENTS`, `GHOST_MAX_MS`) and `tests/test_rig.py` pins them.

### 5.3 Where it shows

- No migration and no new entry column: `gallery._artefact` (`gallery.py:716`) resolves
  `ghost.png` from `<source_dir>/.gate/` exactly as it falls back for `strip.png` today, and
  `_write_entry` copies it to `e/<id>/ghost.png`. `meta.json`'s `gate` array entries gain
  `ghost` from each attempt's report (`_gate_log`, `gallery.py:1250`).
- The entry page shows `ghost.png` ~~under `strip.png`~~ **under the stage, 21 September
  2026:** the entry page has never shown `strip.png` on its own — it is the poster on the
  play button, and only for a sketch too heavy to start itself (`gallery._frame`) — so the
  ghost frames go under the stage, where a person is already looking, with the caption
  *with the ghost pointer*. The template's `$ghost` sits at the end of the line above it,
  so an entry without one renders byte for byte as it does today. The operator's job page
  shows it per attempt.
- `paid try`'s verdict names the `ghost.png` path beside the strip's, so a paid agent
  checks its own script before importing.

### 5.4 Tests

- A fixture `ghost-echo`: a `no_motion` sketch that paints a dot where it is clicked and a
  line where it is dragged, with its own `ghost.json`. `expected.json`: exit 0, `ghost.played`
  equals its event count. `accept.sh` runs on the node; the unit suite skips.
- `tests/test_gate_fixtures.py`: the built-in scripts in the gate module equal
  `ghostshim.BUILTINS` (the sound-regex parity test at `tests/test_executor.py:127` is the
  pattern).
- `tests/test_worker.py`: with `StubGate` reporting `artefacts.ghost`, the entry's `meta.json`
  carries it and `strip_path` is unchanged; `HARNESS_VERSION` is 3 on the new entry.
- One check by hand on the node, pasted into the PR: `ghost-echo` through the gate and the
  same directory in the rig under `?ghost=…` produce the same dot and line, so the two
  players agree on coordinates.

## 6. Packet 16: what the critic and the judge see

**Branch** `feat/ghost-critic`. Half a day of code; the decisions are the work.

The two readers of `strip.png` are the critic (`lineage.critique`, `lineage.py:672`, one
image on `/api/generate`) and the judge (`judge.py:379`, two images on `/api/chat`), and a
person on the compare page. Three ways to give them the ghost window, and one recommended:

1. **Critic sees both strips, judge unchanged.** `lineage.critique` sends `strip.png` and
   `ghost.png` when the latter exists; `prompts/critic.md` says *the second image is the same
   sketch under a pointer that clicked and dragged it; nobody was at the keyboard*. This is
   `critic-v4`, and `docs/OPERATIONS.md` *A new critic prompt version is a burst of work* says
   what that costs: every published entry becomes critiquable again and each critique spawns
   a child. Run it as a deliberate batch (`SKETCHGEN_IDLE_CRITIQUE=0` first, then on), and
   keep `MEASURE[critic-quality]` in mind — the same parents critiqued blind and compared.
2. **Judge sees both.** Changes `pairs.artefact_hash` and every verdict's comparability, and
   the human side of the compare page would have to show the same two images or the two
   populations are no longer looking at the same thing. Not in this series.
3. **Nothing changes; `ghost.png` is for people.** Costs nothing, measures nothing.

**Recommendation: 1, when the operator says so, as its own batch**, after
`MEASURE[ghost-coverage]` (§8) has said how many entries even have a ghost window that
differs from their strip. The packet builds the two-image path behind the prompt version, so
turning it on is editing `prompts/critic.md`; it does not edit that file itself. The judge is
left as it is and recorded in §7.

**How the prompt asks, and what it does when there is nothing to show — added 21
September 2026, in the build.** The recommendation says *behind the prompt version* and
does not say how a prompt file says so. It is a second header line, `images: strip
ghost`, under `prompt_version:`, read by `lineage.critic_images` from the header block
only and dropped out of the rendered prompt with the version line; absent means `strip`
alone, which is critic-v3 byte for byte. Two things fell out of building it. An entry
the prompt asks about that has no `ghost.png` is critiqued over the strip alone and
**never refused** — nothing re-gates a published entry, so refusing would stop the idle
loop on the first entry it reached — and the reason goes in the worker's log line.
And the second image gets no column: migration 009 gave the strip `strip_path` and
`strip_sha256`, and a matching pair for a picture most entries will never have is a
migration to record an absence, so what was shown is in the log (`entry N was critiqued
over two images: strip … and ghost …`). A `ghost_sha256` column is the honest follow-up
if a critique's second picture ever has to be audited rather than read.

Tests: `tests/test_lineage.py`: two images in the payload when `ghost.png` exists and the
prompt asks for it, one otherwise; the prompt-version bump is what switches it, not the
presence of the file.

## 7. Out of scope, and why

- **Fixing `responds(click)`** (`MEASURE[click-assertion-power]`). A gate bump is in Packet
  15 already; adding assertion power to it without a measurement would change what the
  gate fails under the same version number that changed what it draws. Measure, then bump
  again.
- **`responds(move)` / `responds(hover)`.** A new vocabulary word touches four copies
  (`sketch_gate.py:115`, `planner.py:54`, `executor.py:155` and `MEANINGS`,
  `prompts/planner.md`) and `planner-v2`. `wander` gives the kiosk hover without the word.
- **Audio in the kiosk.** No synthetic event is a user gesture. `kiosk.md` §1.7 keeps saying
  so for sound.
- **Touch events.** p5 maps touch to mouse for its own handlers; a sketch reading
  `touches[]` directly gets nothing from the ghost. Recorded, not built.
- **Asking a local executor for a script** is `executor-v4`, an A/B arm. Decide with the other
  prompt changes queued for that version.
- **The judge.** §6 option 2.

## 8. Order, parallelism, deploys

Packets 13 and 14 touch disjoint files (`ghostshim.py`, `kiosk.js`, `gallery.py` manifest and
`_write_entry`; versus `executor.py`, `paid`, AGENTS.md) except for `gallery._write_entry`,
where 14 adds one call under 13's; build 13 first or rebase 14 onto it. 15 needs 14's
`ghost.json` on disk and 13's built-ins. 16 needs 15's `ghost.png`.

| packet | deploy |
| --- | --- |
| 13 | `update.sh` **with** the full render (~25 min): `e/<id>/sketch/index.html` changes for every entry. `render-index` alone gets the kiosk manifest and page but no shim, and the kiosk would pass `?ghost=` to frames that ignore it. |
| 14 | `update.sh --no-render`, then new entries carry their scripts; nothing old changes. |
| 15 | `update.sh --no-render`; the worker restarts with `HARNESS_VERSION` 3 and the node's gate copy matches the hash. Run `gate/accept.sh` on the node once by hand and paste the `ghost-echo` line into the PR. Old entries have no `ghost.png` until re-gated, which nothing does; that is fine and the entry page shows nothing for them. |
| 16 | `update.sh --no-render`; the critic change waits behind the prompt version. |

Remember `update.sh` runs its old copy if the pull changes it, and resumes only what it
paused.

Effort: 13 a day and a half, 14 a day, 15 a day and a half, 16 half a day.

## 9. Measurements to take alongside, not to build

- `MEASURE[ghost-coverage]`: of the published entries with a confirmed `responds(click)` or
  `responds(drag)`, how many have a `ghost.png` whose frames differ from `strip.png`'s by more
  than the idle motion would explain. A read over `report.json`s on the node after Packet 15
  has gated a batch; it decides whether §6 is worth a critic version.
- `MEASURE[ghost-yield]`: on a kiosk session with views on, how often a trusted event ends a
  ghost run, from a counter the shim keeps in the console. If nobody ever touches, the loop
  interval can grow.
- `MEASURE[click-assertion-power]`: still open; §7.
- `MEASURE[ghost-script-quality]`: paid agents' own scripts against the built-ins, by a person
  looking at both `ghost.png`s for the same entry. Ten entries is enough to decide whether
  `executor-v4` should ask.
