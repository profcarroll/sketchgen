# `loads(image)`: the gate asks for a picture from the web

Build plan for an eighth assertion in the closed vocabulary — the sketch fetches a raster
image from a host outside itself and draws it — so a planner can require external media and
the gate can verify it arrived. Grounded in the code as it stands at `main` 1c39d24 (#139).
Written for Opus builders; one packet per branch, one PR each, numbering continues from
`docs/plans/child-source.md` (packets 17–18).

*Status: draft for the operator's review. Nothing here is built.*

## 0. What is there today, in one paragraph

The vocabulary has seven words (`gate/sketch_gate.py:115`, copied to `planner.py:54` and
`executor.py:155`, spelled out to the model in `prompts/planner.md`), and none of them is
about media, so no plan can ask for a picture and no gate can miss one. The network is
already open: the gate serves only `http://sketch.localhost` from disk (`serve_from_disk`,
`:730`) and lets everything else go to the wire — p5 itself comes from cdnjs on every run
— and `ResourceLog` (`:623`) records what a sketch asked for and did not get, as evidence
that never fails a run (`gate/README.md`, *Resources the sketch did not get*). Nothing
records what did arrive. Models reach out rarely: entry 429 loaded `picsum.photos` in
`preload()` across eight attempts and drew nothing, which is why `ResourceLog` exists; entry
1103, the jigsaw puzzle prompted on 2026-09-20, loads `https://picsum.photos/800/600` in
`preload()`, and its published `meta.json` shows five attempts, the first three taking 11 s
of gate time and the last two 1.4 s — the photograph was fetched, then abandoned — and
`responds(click)` missed on every one. The gate's README remembers a sketch that *went to
Wikimedia for the real paintings* and one that *built its own image as a `data:` URI when it
could not fetch one*, both thrown away for an assertion. There is no rule anywhere against
reaching out (`prompts/executor.md`, both rules files: the only `loadImage` mention is
`treatment.md:70`, a frame-budget placement rule), and no `DECIDE` or `MEASURE` about media.

## 1. Decisions, with recommendations

| id | decision | recommendation |
| --- | --- | --- |
| `DECIDE[image-word]` | the word | **`loads(image)`**, beside `uses(webgl)`: *the sketch loads at least one raster image from a web host outside itself and draws it.* The gate implements the first clause and reads the second off the canvas (§3.1). |
| `DECIDE[image-pass]` | what passes | **An off-origin response with an `image/*` content type, status 2xx and a body, and a canvas at `t20` that is not one flat colour.** The second half is the weakest honest check: the gate cannot see what is drawn, only that something is. A `data:` URI does not pass — it is not the web — and the evidence says so when that is what it finds. |
| `DECIDE[image-hosts]` | an allowlist | **None in the gate.** The gate reports the host; a person sees it before publishing (Packet 20). The executor's meaning line (§3.2) names two hosts known to serve CORS headers and stable URLs, `picsum.photos/seed/<word>/<w>/<h>` and `upload.wikimedia.org`, because p5 1.11's `loadImage` sets `crossOrigin` and a host without the header fails to load at all rather than tainting the canvas; that failure is what `ResourceLog` already narrates, and `worker.py`'s evidence already says *use a host that serves CORS headers*. |
| `DECIDE[image-wait]` | preload and the clock | **When `loads(image)` is asserted, the canvas wait in `load_sketch` (`:773`, capped at 10 s today) runs to `--timeout`**, and `timings.preload_s` records it. A `preload()` sketch has no canvas until the image arrives, and the virtual clock does not move the network; entry 1103's 11 s runs are that wait. A canvas that never appears fails the assertion with the resource line in its detail, which is the sentence entry 429 never got. |
| `DECIDE[image-determinism]` | what a random picture does to the strip | **Accepted and named.** `sketch_gate.py:95` already says *anything the sketch fetches from the network is as stable as the network*. The meaning line asks for a seeded URL so two runs draw the same picture; an unseeded one is allowed and the report says `resources_loaded[].url`. |
| `DECIDE[image-versions]` | what bumps | **`HARNESS_VERSION` 3, `planner-v2`, `GATE_SHA256`; `executor-v3` stays.** The gate gains a word, so the version that says what it fails moves. `prompts/planner.md` says *these seven lines are the only assertions that exist*, so it is rewritten and is a new planner version. `MEANINGS` in `executor.py:166` gains a line, but a meaning is rendered only when its word is asserted; a prompt without the word is byte-identical, and the template file is untouched. If `docs/plans/auto-mouse.md` Packet 15 lands first, this is version 4 and the docstring lists both. |
| `DECIDE[image-liveness]` | how the planner pairs it | **With either.** A photograph that scatters (`motion(idle)`) or a puzzle of one (`no_motion`, `responds(drag)`); the rule stays *exactly one of the two*. |
| `DECIDE[image-licence]` | the picture's licence | **Recorded, not resolved.** An entry's `licence` (CC BY 4.0) covers the code and the statement; the picture keeps its host's terms, and the entry page says which host it came from. Wikimedia files carry their own licence per file; picsum serves Unsplash photographs under Unsplash's terms. A person publishes with that in view. |

## 2. What every packet inherits

- Repository, tests, rules: as `docs/plans/agent-rig.md` §2. No network in the unit suite:
  the fixtures that fetch a picture run only under `gate/accept.sh` on the node, which
  already needs the network for p5 from cdnjs (`gate/README.md`, *The fixtures and accept.sh*).
- The four copies of the vocabulary move together, and the tests say so:
  `tests/test_planner.py:195` iterates `planner.VOCAB` against `prompts/planner.md`;
  `tests/test_planner.py:168` validates every word; `tests/test_executor.py:127` is the
  pattern for pinning a gate string in a module. `rig/README.md`'s facts table is tied to
  the gate's constants by `tests/test_rig.py`.
- AGENTS.md rules 1–5 bind the builder; the gate's copy on the node is the repo's copy once
  `update.sh` runs, and `accept.sh` is run there by hand before the PR.

## 3. Packet 19: the word and the gate

**Branch** `feat/loads-image`. About two days; the fixtures are half of it.

### 3.1 The gate

- `VOCAB` and `SIMPLE_ASSERTIONS` gain `loads(image)` (`sketch_gate.py:115`).
- `ResourceLog` (`:623`) records arrivals as well as failures: `_on_response` keeps every
  off-origin response with a `content-type` starting `image/` and a 2xx status, as
  `{url, host, type, bytes, ms}`, capped at `MAX_RESOURCES_LOADED = 8`. `report.resources_loaded`
  beside `report.resources`. Never a check; the list is evidence for people and for the
  assertion below.
- `load_sketch` takes `wait_for_canvas_ms`; `main` passes the full timeout when
  `loads(image)` is among the assertions (`DECIDE[image-wait]`) and records
  `timings.preload_s`.
- `INIT_JS` gains `g.flat(name)`: true when a snapshot's pixels are all within 2/255 of one
  colour, using the `imageData` the diffs already read.
- `evaluate_assertion` (`:1278`) gains the branch:

  ```
  loads(image): pass iff resources_loaded is non-empty and not g.flat('t20')
  detail on pass:   "1 image arrived: picsum.photos (image/jpeg, 61 kB, 340 ms); canvas drawn"
  detail on miss:   "no image arrived from outside the sketch" + the resources lines
                    (the entry-429 sentence), or "an image arrived but the canvas at the
                    end of the idle window is one flat colour", or "no canvas after 60 s
                    (preload never finished): " + the resources lines,
                    or "the only image is a data: URI, which is not the web"
  ```

  The `data:` case is read from `resources_loaded` being empty while the page's
  `performance.getEntriesByType('resource')` shows none either and the canvas is not flat:
  the gate cannot prove a `data:` image but can say the web was not used.

- `HARNESS_VERSION = 3` in `worker.py:722` with the docstring line *3, from <date>:
  `loads(image)` exists; the gate reports `resources_loaded`; the canvas wait runs to the
  timeout when the word is asserted.* `GATE_SHA256` updated; `rig/README.md` facts table gains
  `MAX_RESOURCES_LOADED`.

### 3.2 The planner and the executor

- `planner.py:54` `VOCAB` gains the word. `INTERACTIVE` is unchanged: `loads(image)` says
  nothing about liveness (`DECIDE[image-liveness]`).
- `prompts/planner.md` becomes `planner-v2`: *eight lines*, and the row

  ```
  loads(image)      the sketch fetches a photograph or picture from the web and draws it
  ```

  with one rule under it: *`loads(image)` only when the prompt asks for a photograph, a
  picture, an image from the web, or a thing made of one (a jigsaw of a photo, a collage).
  Never for shapes the sketch can draw itself.* The example in the liveness rule, the
  jigsaw, gains it: *a puzzle of a photograph: `responds(click)`, `no_motion`,
  `loads(image)`.*
- `executor.py:155` `VOCAB`, `:146` `SIMPLE_ASSERTIONS`, and `MEANINGS` (`:166`) gain:

  > `loads(image)` — the gate watches the network: at least one image must arrive from a
  > host outside the sketch and be drawn before the idle window ends. Load it in
  > `preload()` from a host that serves CORS headers and a stable URL, such as
  > `https://picsum.photos/seed/<word>/800/600` or a file on `upload.wikimedia.org`; a
  > `data:` URI is not the web and does not pass; a host that does not serve CORS fails to
  > load at all.

  `prompts/executor.md` is untouched (`DECIDE[image-versions]`).
- `worker.py`'s evidence for a missed `loads(image)` is the assertion's own detail, which
  already carries the resource lines; `CHECK_REMEDIES` gains nothing, since the meaning line
  is the remedy and is in the next prompt.
- AGENTS.md, *Answers → Plan*: the example list gains `loads(image)`.

### 3.3 Fixtures

- `gate/fixtures/good-image`: `preload()` loads `https://picsum.photos/seed/sketchgen/400/300`,
  `setup()` draws it once, `noLoop()`. `expected.json`: exit 0, `assertions_expected`
  `{"loads(image)": true, "no_motion": true}`, note: *the word exists because of entries 429
  and 1103.*
- `gate/fixtures/bad-image-missing`: the same sketch against a URL that 404s on a real host.
  Expected: exit 1 under `--assert loads(image)`, `console_clean` true (a failed image is not
  a page error, which is the whole point), `resources` non-empty.
- `gate/fixtures/bad-image-data-uri`: draws a 2×2 `data:` PNG. Expected: exit 1 under the
  word, detail naming the `data:` case.
- `tests/test_gate_fixtures.py` `test_every_fixture_has_an_expectation` covers them as it
  covers the seven; `AcceptHarnessTests` runs them on the node, skips on the laptop.

### 3.4 Tests that run everywhere

- `tests/test_planner.py`: a fixture plan `image.txt` with the word validates; the
  vocabulary test passes against the new prompt; `planner-v2` is what `prompt_version`
  reads.
- `tests/test_executor.py`: the rendered prompt carries the meaning line only when the word
  is asserted, and a prompt without it is byte-identical to the current fixture; the
  vocabulary in `executor.py` equals the gate's, read from the file as the sound regex is.
- `tests/test_worker.py`: with `StubGate` reporting `resources_loaded` and a missed
  `loads(image)`, the evidence carries the detail and the job goes to `held` off-plan when
  QA is clean, as any other missed assertion does (`gate/README.md`, *What may fail a run*);
  `HARNESS_VERSION` is 3 on the entry.
- `tests/test_gate_fixtures.py`: `ResourceLog` is importable without Playwright (the
  `_gate_module` trick at `:154`); a synthetic response with `image/png` lands in
  `resources_loaded` and a `text/html` 200 does not; the cap holds.

### 3.5 Acceptance

1. `gate/accept.sh` on the node: every existing fixture unchanged, the three new ones as
   expected, pasted into the PR.
2. Entry 1103's kept attempt re-gated by hand on the node with
   `--assert no_motion --assert loads(image)`: `resources_loaded` names `picsum.photos`,
   `loads(image)` passes, `timings.preload_s` is about what its 11 s runs were.
3. A job on the node with the prompt *a jigsaw of a photograph* plans `loads(image)` and
   the executor's first attempt fetches one; if it misses, the evidence names the host.

## 4. Packet 20: where the picture came from

**Branch** `feat/loads-image-shown`. About a day.

- `gallery._gate_log` (`gallery.py:1250`) folds `resources_loaded` into `meta.json`'s
  `gate` array entries, and `_meta` lifts the kept attempt's list to a top-level
  `loads: [{host, type, bytes}]` — host and type only, never the full URL, which can carry
  a query string a person did not choose to publish (`guard()` scans for email-shaped
  strings for the same reason).
- The entry page: a provenance row **Loads** — *a picture from picsum.photos (image/jpeg,
  61 kB)* — and, under the frame, one line *this sketch fetches an image from picsum.photos
  when it runs*, so a viewer knows their browser will call a third host. Dashes and no line
  when the list is empty.
- The Held page card (`web.py`, the batch card of `docs/plans/held-batch.md` §4.1) shows
  the host in the card's meta line, so the person publishing sees it without opening the
  attempt (`DECIDE[image-hosts]`, `DECIDE[image-licence]`).
- `kiosk.json` and `swipe.json` gain nothing: an opaque-origin frame fetches cross-origin
  images without help, and there is no CSP on the published site to loosen
  (`gallery.py:1682`; `web.py:230` is a framing header only).
- Tests: `tests/test_gallery.py` for the row, the line, the `meta.json` field and that an
  entry without the list renders as before; `tests/test_web.py` for the card.

## 5. Out of scope, and why

- **Other media.** `loads(video)`, `loads(font)`, `loads(json)` are each a content type and
  a different *drawn* check; the `ResourceLog` change in §3.1 records them all, so a later
  word costs only the evaluate branch. Not now: one word, measured, first.
- **Proxying or caching images on the node**, so a run is reproducible when the host is
  down. It is the right fix for determinism and it is a service on the node; decide after
  `MEASURE[image-arrival]`.
- **An allowlist of hosts** (`DECIDE[image-hosts]`).
- **`responds(click)` cannot fail a moving sketch** (`MEASURE[click-assertion-power]`);
  entry 1103's five misses are a different bug — the click landed on no piece — and not
  this plan's.
- **Re-gating old entries** under version 3 to find the ones that already load pictures.
  A read of the node's `report.json`s for the `preload`/`loadImage` string in `sketch.js`
  gives that list without a run; §6.

## 6. Order, parallelism, deploys

19 then 20. If `docs/plans/auto-mouse.md` Packet 15 is in flight, coordinate the
`HARNESS_VERSION` and `GATE_SHA256` bumps: whichever merges second rebases and takes the next
number, and the docstring lists both changes under their own versions.

| packet | deploy |
| --- | --- |
| 19 | `update.sh --no-render`; the worker restarts with the new version and the node's gate copy matches the hash. Run `accept.sh` on the node once by hand before the PR; the new fixtures need the network, which the node has. |
| 20 | `update.sh` **with** the full render: the entry page and `meta.json` changed. |

Remember `update.sh` runs its old copy if the pull changes it, and resumes only what it
paused.

Effort: 19 two days, 20 a day.

## 7. Measurements to take alongside, not to build

- `MEASURE[image-baseline]`: how many published sketches already call `loadImage`, from a
  grep over `jobs/*/attempt-*/sketch.js` on the node, and how many of those have a
  `resources` line in their report (the image never arrived). A read; it says how rare the
  behaviour is before the word exists.
- `MEASURE[image-arrival]`: after Packet 19, of the plans that assert `loads(image)`, the
  share whose kept attempt passed it, and the hosts they used. Sets whether the proxy in
  §5 is needed.
- `MEASURE[image-planner-rate]`: how often `planner-v2` asserts the word on prompts that do
  not mention a picture. If it decorates, the rule in §3.2 is tightened.
- `MEASURE[image-judge]`: whether entries that pass `loads(image)` win more pairs than
  their batch, from the judgments `pairs.py` already totals. The plan's premise is that a
  photograph is another layer of possibility; this is where that is checked.
