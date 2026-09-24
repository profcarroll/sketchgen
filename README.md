# sketchgen

A self-hosted, self-generating, self-judging p5.js gallery running on one node.

A creative prompt goes in. A planner expands it into a brief with testable
assertions. An executor writes the sketch in a single model call. A headless
browser gates the result against those assertions, then plays a ghost pointer
over it so the sketch is seen touched as well as idle. On failure, the gate's
own evidence and the sketch it judged go back for bounded repair. On success,
a human publishes the entry to a static gallery on GitHub Pages — with full
provenance, paired human/agent judgments under blind conditions, and a
self-prompting lineage that lets the gallery grow on its own.

Every model runs on the node by default. Any of the four model steps — plan,
execute, judge, critique — can instead be answered by a model elsewhere, through
`sketchgen paid`, without a credential ever reaching the node; an entry any of
whose models ran off the node is badged **off-node** in the gallery, and its
provenance names the model that answered and what the work cost.

Built for **PSAM 5600 B: Small Linux Devices, Large Language Models** (Parsons
School of Design, Fall 2026). The design note is
`dossiers/sketchgen-gallery.md` in the class repo and the build order is
`dossiers/sketchgen-build-plan.md` beside it; every feature since has its own
plan under [docs/plans/](docs/plans/).

**[Gallery](https://profcarroll.github.io/sketchgen-gallery/)** ·
**[Rejections](https://profcarroll.github.io/sketchgen-gallery/rejections.html)** ·
**[Compare](https://profcarroll.github.io/sketchgen-gallery/compare.html)** ·
**[Kiosk](https://profcarroll.github.io/sketchgen-gallery/kiosk.html)** ·
**[Swipe](https://profcarroll.github.io/sketchgen-gallery/swipe.html)**

Python 3.12, standard library only. No dependencies, no framework. The one
pinned package on the node is Playwright, for the gate's browser.

## The pipeline

```
prompt
  │
  ├─ PLAN        brief + assertions from a closed vocabulary of eight words
  │                (gemma4:e4b, ~5 s)
  │
  ├─ EXECUTE     single-shot: one prompt in, one sketch.js out; a child job
  │              is handed its parent's sketch, a repair its own last attempt
  │                (qwen3-coder:30b, ~1 min, no agent loop)
  │
  ├─ GATE        headless browser: five checks that can fail a run, the
  │              plan's assertions, then a ghost pointer played over the
  │              sketch; deterministic (seeded, frozen clock)
  │                (Playwright + Chromium, a few seconds)
  │
  ├─ PREFLIGHT   if the gate fails, scan for p5 name shadowing and
  │              per-frame allocation before the evidence goes back
  │
  ├─ REPAIR      evidence-fed retries, up to max_attempts (default 3)
  │
  ├─ HOLD        a human publishes or rejects
  │
  └─ PUBLISH     entry dir + commit + push to GitHub Pages
```

When the queue is empty the worker does idle work — judging unpaired entries
and critiquing published ones to spawn children — before sleeping. One
inference slot, one worker, one process. A second worker is refused at the
fence (exit 3): two once wedged the node's model servers for half an hour.

## The gate

The referee between a model's sketch and the gallery, and the reason the
pipeline can be left alone overnight. `gate/sketch_gate.py` loads the sketch
in headless Chromium under a virtual clock, hand-steps 120 frames of idle,
clicks the canvas centre, drags across it, plays a tone into a fake microphone,
and reports:

- **five checks that fail a run**: the console threw, the draw loop stopped,
  the sketch asked for p5.sound and did not get it, audio started without a
  gesture, a frame cost more than the budget (100 ms, 90 s for the run);
- **the plan's assertions**, from a vocabulary of exactly eight words:
  `motion(idle)`, `no_motion`, `responds(click)`, `responds(drag)`,
  `responds(audio)`, `uses(webgl)`, `size(w,h)`, and `loads(image)` — the
  sketch fetched a raster image from a host outside itself and drew it;
- **what arrived and what did not**: every off-origin resource the page asked
  for, with the browser's reason for a failure, so a blank canvas from a
  missing picture reads as a missing picture and not as broken code;
- **the ghost window**: after the probes, the entry's own pointer script (or
  a built-in for its assertions) is played through real Chromium input, and
  four frames of it become `ghost.png` beside the four-frame `strip.png` the
  judges and the critic see.

An assertion miss is not a failure: a sketch that ran clean and missed its
plan goes to `held` with the miss recorded as *off-plan*, and a person decides.
Only a check failure ends a job as `failed-kept`. Every entry records the
`HARNESS_VERSION` it was gated under (now 5), because a change to what the
gate fails splits the corpus; the file itself is hash-pinned by a test, and
fourteen fixture sketches, one bug each, are what `accept.sh` runs on the node
after any change.

## The hypothesis

The research idea is not "can AI make art." It is: **when humans and agents
judge the same artefacts on the same questions under blind conditions, how much
do they disagree?**

Two entries are shown side by side, same seed, same frame strip. Two questions:

1. **Which is closer to its brief?** — fidelity.
2. **Which would you rather look at?** — taste; no correct answer.

Both populations get the identical page and the identical two questions. A
Bradley–Terry fit over the pairs gives every entry a score *per population*,
and the **divergence** between the two is the measurement
(`MEASURE[agent-human-correlation]`). Nothing in the system ever collapses them
into one number.

### Blinding

The agent judge never sees a human vote, a Bradley–Terry score, an engagement
tally, the producing model, the submitter, or the rules file.
`judge.py:assert_blind()` enforces a banned-field list against the rendered
prompt before anything is sent. The human sees the agent's verdict only after
their own choice.

### The A/B experiment

Every job records its **rules file**. `treatment` carries structured p5.js
conventions (the library trap, global mode, canvas sizing, verification norms).
`control` carries the class repo's generic context. The gallery's own paired
comparisons score the difference, making sketchgen an A/B rig measuring whether
structured context improves creative output from the same model on the same
prompts. Anything else that changes what a model is shown — a paid executor,
the parent's source on a child — is recorded on the attempt so it can be kept
apart from that measurement rather than folded into it.

## The self-generating loop

When the queue empties, the worker critiques published entries with the local
vision model. The critic looks at the gate's frame strip — it refuses to work
blind — and writes one sentence, under forty words, no code, describing a
visible change for the same sketch made again. That sentence becomes the
child's prompt under `Revise:`, and the child's executor is handed the
parent's kept sketch under the brief, so the revision starts from the code it
is revising rather than from a blank page:

```
entry 2: "a breathing grid of squares"
  └─ CRITIQUE → "the same grid, and this time the pulse colors
  │               are randomized upon impact"
  └─ entry 9  (generation 1)
       └─ CRITIQUE → …
            └─ entry N  (generation 2)
                 └─ generation 3 — then the line waits for a human
```

Lineage runs three generations unattended; at depth 3 the child is created but
held for human review, and a person's release lets it run on. Lines have
reached generation 25. Every entry stores its parent, its generation, the
critique, the critic's model id, and what its executor was given. The
provenance graph — which prompt begat which, through which model, past which
gate — is what "generates itself" means. Signed-in visitors can add to it: a
prompt field on the grid and a critique form on every entry page, held for a
person to release before anything reaches a model.

## Off the node

`sketchgen paid` lets a model that is not on the node answer any step. The
node writes what it would have asked as a JSON packet; the agent answers it;
the node reads the answer back through the same parser the local path uses,
gates the sketch itself, and records the model that answered as provenance.
No API key exists on the node, ever.

For an agent driving its own job the loop is three verbs over `ssh`, wrapped
by `bin/sg`: `paid start` queues one job and leases it; `paid next` returns
the next packet, or says *wait*, *done* or *stop*; `paid try` runs the node's
real gate over a candidate reply before an attempt is spent; `paid import`
lands the answer. The lease keeps the worker on that job and off idle work
while the agent is driving, and lapses twenty minutes after the agent's last
word. The attempt records the round trip the node timed, the reply's own
token counts read off the agent's transcript, and the *process cost* — the
minutes and tokens spent around the reply — as reported by the agent and
never entering any measurement. [AGENTS.md](AGENTS.md) is the whole contract;
`rig/` is the agent's local bench.

## The kiosk and the swipe page

`kiosk.html` is the projector: one published sketch at a time, full screen,
rotating on a timer through seven orders, with overlays for the prompt, the
code, the lineage and a QR code of the entry's URL, all on single keys. A
sketch built around input would sit blank on a wall, and a sandboxed frame
cannot be clicked from outside, so every published sketch page carries a
**ghost pointer**: a shim, inert unless the frame's URL asks for it, that
plays the entry's pointer script inside the frame and stops the moment a real
hand arrives. `swipe.html` is the same gallery for a phone: one sketch a
swipe, hold to touch it, a like and a share on each.

## Eight days in

From the node's database on 2026-09-22, the ninth day of running:

| | value |
|---|---|
| jobs | 1,335 |
| attempts | 2,167 (51% passed the gate outright) |
| first-attempt pass rate | 64.7% |
| avg attempts per kept entry | 1.42 |
| entries published · rejected (public) · archived | 1,035 · 137 · 119 |
| deepest generation | 25 |
| paired judgments, both populations | 5,177 |
| critiques written | 1,059 |
| likes · views | 180 · 877 |
| entries with a model off the node | 17 |
| published under control · treatment | 351 · 684 |
| tokens (in / out) | 4.34M / 2.17M |
| avg wall time per attempt | 84 s |

Agents (coding or paid-model): read [AGENTS.md](AGENTS.md) first.

## File map

```
bin/sketchgen               the one CLI entry point
bin/sg                      the same, run on the node over ssh from a laptop

sketchgen/                  the package
  db.py                     schema, helpers, state machine (TRANSITIONS)
  planner.py                prompt → brief + closed assertion list
  executor.py               single-shot code generation, the fallback page
  worker.py                 the loop: fence, claim, plan, execute, gate, repair
  paid.py                   packets: any step answered off the node
  judge.py                  blind paired comparison, local + paid judges
  pairs.py                  Bradley–Terry scoring (Hunter 2004 MM algorithm)
  lineage.py                critique → child prompt, generation depth
  preflight.py              p5.js name shadowing, and per-frame cost
  publish.py                held entry → git commit → push to Pages
  sync.py                   write-path pull (votes, likes, views, prompts)
  gallery.py                static site renderer: entries, grid, compare,
                            lines, kiosk, swipe, QR
  ghostshim.py              the pointer inside the frame, and its scripts
  soundshim.py              p5.sound inside a sandboxed frame on WebKit
  models.py                 the Ollama catalogue and who is paid
  qr.py                     the entry's QR codes, stdlib
  web.py                    operator UI: console, queue, job, held, new
  console.py                node vitals collector (/proc, Ollama, DB)
  cli/                      drop-in subcommands, one file per verb
  templates/                server-rendered HTML (14 files)
  assets/                   gallery CSS and JS: grid, compare, kiosk, swipe

migrations/                 001_init through 017_executor_source, in order
prompts/                    versioned prompt templates
  planner.md                expand a prompt into brief + assertions (v2)
  executor.md               single-shot sketch generation (v3)
  judge.md                  blind paired comparison (v1)
  critic.md                 one-sentence revision instruction (v3)
  rules/control.md          A/B control arm (generic context)
  rules/treatment.md        A/B treatment arm (p5.js conventions)

gate/                       the deterministic referee, run by the worker
  sketch_gate.py            headless Chromium: five checks, eight words,
                            the ghost window
  accept.sh                 the harness: every fixture against expected.json
  fixtures/                 fourteen sketches, one bug each, and what the gate
                            must say

rig/                        an agent's local bench: p5 pinned, a probe, a
                            benchmark, and cost.py over its own transcript

systemd/                    user-level units
  sketchgen-worker.service  the worker (daemon or drip mode)
  sketchgen-worker.timer    5-minute drip timer
  sketchgen-web.service     operator UI on 127.0.0.1:8081
  sketchgen-sync.service    write-path pull (oneshot)
  sketchgen-sync.timer      5-minute sync timer
  sketchgen-backup.service  one verified snapshot of the database (oneshot)
  sketchgen-backup.timer    daily at 04:10 UTC
  operator/                 units for the OPERATOR's machine, not the node

writepath/                  Cloudflare Worker for the gallery's write side
  worker.js                 votes, likes, views, prompts, GitHub OAuth
  schema.sql                D1 tables

bin/pull-backup.sh          pull the node's snapshots and jobs/ to this machine
bin/sketchgen-tunnel.sh     the operator UI's SSH tunnel: up, status, heal, down
update.sh                   deploy on the node: pull, pause, migrate, render,
                            restart, resume
requirements.txt            the venv's pins: playwright==1.62.0 and its three

tests/                      28 files, stdlib unittest, ~1,700 tests, ~95 s;
                            the kiosk, swipe and ghost shim run under node
docs/OPERATIONS.md          running the worker, the UI, the sync, the paid
                            steps, deploys and recovery
docs/plans/                 one build plan per feature, decisions dated
```

## The database

One SQLite file holds the whole system: `jobs` and their `attempts`, the `entries`
the gallery shows, paired `judgments`, `lineage`, `critiques`, `engagement` and
`likes`, `submissions` from the public prompt and critique forms, `billing_usage`
from the cloud meter, `activity` for the operator's status cards, `sync_state`
for the write-path watermark, `meta` for the worker's own stamps and the
settings that change without a deploy, and a singleton `control` row that is
the operator's pause switch. All timestamps are UTC, ISO 8601 with a trailing
`Z`, in columns named `*_utc`.

Create it (default path: `$SKETCHGEN_DB`, else `~/sketchgen/sketchgen.db`):

```
python3 bin/sketchgen db init --path /tmp/sketchgen.db
```

Rerunning `db init` applies only migrations that have not been applied yet, so it is
safe. Look at what is in there:

```
python3 bin/sketchgen db status --path /tmp/sketchgen.db
```

`db status` prints the schema version, every table with its row count, the control
row, and the job counts by state.

**You know this worked when** `db status` prints `schema_version: 17`, fifteen
tables, and `control: running`.

A job's legal moves are enforced in `sketchgen/db.py` (`TRANSITIONS`), not in SQL:
`queued → planning → executing → gating → held → published`, with `repairing` and
`needs-laptop` off to the side, three terminal states, and a `requeue` path back to
`queued` for the worker's stop-now control. An illegal move raises
`IllegalTransition` and changes nothing. Every write to the database has a verb;
there is no hand-editing path, on purpose.

An entry has a second, smaller machine beside it (`ENTRY_TRANSITIONS`), because a
job is over when the worker stops and an entry is not: a person still has to decide
about it. `held → published`, `held → rejected` (which publishes it to the
rejections page with the reason, rather than hiding it), and `held → archived` or
`failed-kept → archived` for anything the operator wants off their lists.
`archived` is terminal and never public. Nothing in either machine deletes a file.

## The worker

One job at a time, from the queue to `held` or to a kept failure:

```
python3 bin/sketchgen enqueue --prompt "sixty drifting circles" --by octocat
python3 bin/sketchgen worker --once
```

`worker --once` reads the control row, fences the inference slot (`pgrep -af
opencode`, and any other `bin/sketchgen worker` — a model left resident by
`KEEP_ALIVE` is not contention, but a second worker is, and is refused), claims
the oldest queued job, plans it if it arrived without a brief, then executes and
gates it up to `max_attempts` times, feeding the gate's evidence and the previous
attempt's sketch back into the next attempt. Every step is stamped in UTC on
stderr and in `<jobs>/<id>/job.log`, and every attempt is a row in `attempts`.
Without `--once` it stays resident, which is systemd's job — see
`docs/OPERATIONS.md`; on the node it always is, so `--once` is for a machine
without the service. Paths come from `$SKETCHGEN_DB`, `$SKETCHGEN_JOBS`,
`$SKETCHGEN_GATE` and `$OLLAMA_HOST_URL`, the same four the unit sets.

Pause, stop and resume are database rows, not signals:

```
python3 bin/sketchgen control pause --reason "someone else wants the slot"
python3 bin/sketchgen control stop            # abort the attempt, re-queue the job
python3 bin/sketchgen control resume
```

What the executor is shown is a setting, not a deploy:
`sketchgen executor-source --set none` restores the prompt with no source under
it, which is the control batch for measuring whether a child that can read its
parent follows the critique better than one that cannot.

## The console

```
python3 bin/sketchgen console                 # one reading, as a terminal page
python3 bin/sketchgen console --json          # the same document, for the web UI
python3 bin/sketchgen console --watch 2       # one document every two seconds
```

`console` collects the node's vitals the way `htop` shows them (per-core CPU,
memory split into what is used and what is page cache, swap, disk with the model
blobs and the browser named, load against the core count, the top three
processes), the resident model and who holds the inference slot, the instant
prefill and decode rates from the last attempt that had them, the token odometer,
the production funnel and the per-sketch averages with their cost at both node
shapes. It reads `/proc`, the filesystem, Ollama's `/api/ps` and the database; it
calls no model, writes nothing and needs no sudo, so it is safe to run while a job
holds the slot. A source that is missing or unreachable is `null` in the document,
never an exception. Process command lines are cut to the executable's basename
and redacted: an argument containing `key`, `token`, `secret` or `password` — and
the value after such a flag — never reaches the page.

The `--json` document's key names are the contract the web UI reads;
`tests/fixtures/console/sample.json` is a full example of it and `tests/test_console.py`
fails if the two ever disagree. "Session" means since the resident worker's last
start, which it stamps into `meta.worker_started_utc`; with no stamp, session and
total are the same numbers.

## The gallery

```
python3 bin/sketchgen render-all --gallery-dir ~/sketchgen-gallery
python3 bin/sketchgen publish-index             # re-render, commit and push
```

`index.html` and `rejections.html` are one grid of cards each, newest first in the
HTML itself; above the grid, `Sort` reorders it (newest, oldest, random, most liked)
and keeps the choice in the URL as `?sort=`, and `Filter` folds away until you open
it or arrive with `?rules=` or `?executor=` in the URL. The search box in the same
row narrows the grid as you type — over each entry's number, prompt, brief, rules
file, executor and submitter — composes with the filters, and keeps its query in the
URL as `?q=`. With JavaScript off the grid is still newest first, the filters are
still plain links, and the search box is a form that reloads the page it is on.

A card carries two standing bars in place of its scores in words: the track runs
from the weakest entry in the pool to the strongest, a filled dot is where the
humans place this entry and a ring is where the agents place it — each at its
percentile among the entries that population has scored on that question — and a
fainter mark has fewer pairs behind it. Hover a mark for the rank and the
Bradley–Terry score itself; the chip beside the tags says whether the two
populations agree about which you would rather look at.

An entry page puts the same two percentiles on one square above its score boxes — a
compass, across for *rather look at it* and up for *closer to its brief*, a dot for
the humans and a ring for the agents, the line between them the gap — so a
population that enjoys looking at an entry but thinks it ignored its brief lands in
a different corner from one that reads it the other way; a population that has
scored only one of the two questions is not placed at all. Below the frame: the
gate's strip and the ghost window's, the statement in the model's own words, and a
provenance table that says which models ran where, what the gate found on each
attempt, what the executor was given, which host a picture came from, and what
an off-node reply cost. `meta.json` beside every entry carries all of it as data.

Every lineage line has its own page under `lines/`, `lineage.json` places every
entry in its line, and each entry carries two QR codes of its own URL, one for the
kiosk. A kept rejection's `compare` link judges it against a random published
entry — the status quo — and says which side was rejected only after both
answers are in.

## Tests

```
python3 -m unittest discover -s tests -v
```

No network, no model, no browser: every model reply is a stub on disk, every
gate a stub, and the four skips are the ones that need the node. The gallery's
JavaScript is run for real under `node` against a small DOM.

Every script here exits 0 on success, 1 on failure, 3 when it refuses.
