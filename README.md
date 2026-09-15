# sketchgen

A self-hosted, self-generating, self-judging p5.js gallery running on one node.

A creative prompt goes in. A planner expands it into a brief with testable
assertions. An executor writes the sketch in a single model call. A headless
browser gates the result against those assertions. On failure, the gate's own
evidence goes back for bounded repair. On success, a human publishes the entry
to a static gallery on GitHub Pages — with full provenance, paired human/agent
judgments under blind conditions, and a self-prompting lineage that lets the
gallery grow on its own.

Built for **PSAM 5600 B: Small Linux Devices, Large Language Models** (Parsons
School of Design, Fall 2026). The design note is
`dossiers/sketchgen-gallery.md` in the class repo and the build order is
`dossiers/sketchgen-build-plan.md` beside it.

**[Gallery](https://profcarroll.github.io/sketchgen-gallery/)** ·
**[Rejections](https://profcarroll.github.io/sketchgen-gallery/rejections.html)** ·
**[Compare](https://profcarroll.github.io/sketchgen-gallery/compare.html)**

Python 3.12, standard library only. No dependencies, no framework.

## The pipeline

```
prompt
  │
  ├─ PLAN        brief + assertions from a closed vocabulary
  │                (gemma4:e4b, ~5 s)
  │
  ├─ EXECUTE     single-shot: one prompt in, one sketch.js out
  │                (qwen3-coder:30b, ~67 s, no agent loop)
  │
  ├─ GATE        headless browser: console errors, frame advancing,
  │              assertion checks, deterministic (seeded, frozen clock)
  │                (Playwright + Chromium, ~4 s)
  │
  ├─ PREFLIGHT   if the gate fails, scan for p5 name shadowing
  │              before the evidence goes back
  │
  ├─ REPAIR      evidence-fed retries, up to max_attempts
  │
  ├─ HOLD        a human publishes or rejects
  │
  └─ PUBLISH     entry dir + commit + push to GitHub Pages
```

When the queue is empty the worker does idle work — judging unpaired entries
and critiquing published ones to spawn children — before sleeping. One
inference slot, one worker, one process.

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
prompts.

## The self-generating loop

When the queue empties, the worker critiques published entries with the local
vision model. The critic writes one sentence — under forty words, no code —
describing a visible change for the same sketch made again. That sentence
becomes the child's prompt:

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
held for human review. Every entry stores its parent, its generation, the
critique, and the critic's model id. The provenance graph — which prompt begat
which, through which model, past which gate — is what "generates itself" means.

## Early numbers

From the console's production telemetry, after the first day of running:

| | value |
|---|---|
| generation attempts | 124 |
| passed gate | 87 (70%) |
| first-attempt pass rate | 66.3% |
| avg attempts to pass | 1.51 |
| lineage children spawned | 17 |
| avg wall time per sketch | 102 s |
| avg sketch length | 71 lines |
| cost per sketch (16/96) | $0.006 |
| total tokens (in / out) | 118k / 75k |
| prefill / decode | 94.8 / 25.4 tok/s |
| API calls | 0 — all inference local |

## File map

```
bin/sketchgen               the one CLI entry point

sketchgen/                  the package
  db.py                     schema, helpers, state machine (TRANSITIONS)
  planner.py                prompt → brief + closed assertion list
  executor.py               single-shot code generation
  worker.py                 the loop: fence, claim, plan, execute, gate, repair
  judge.py                  blind paired comparison, local + paid judges
  pairs.py                  Bradley–Terry scoring (Hunter 2004 MM algorithm)
  lineage.py                critique → child prompt, generation depth
  preflight.py              p5.js name shadowing, and per-frame cost
  publish.py                held entry → git commit → push to Pages
  sync.py                   write-path pull (votes, likes, views) into the DB
  gallery.py                static site renderer: entries, grid, compare, lines
  web.py                    operator UI: console, queue, job detail, held
  console.py                node vitals collector (/proc, Ollama, DB)
  cli/                      drop-in subcommands, one file per packet
  templates/                server-rendered HTML (11 files)
  assets/                   gallery CSS and JS

migrations/                 001_init through 006_critiques, applied in order
prompts/                    versioned prompt templates
  planner.md                expand a prompt into brief + assertions
  executor.md               single-shot sketch generation
  judge.md                  blind paired comparison (two images, two questions)
  critic.md                 one-sentence revision instruction
  rules/control.md          A/B control arm (generic context)
  rules/treatment.md        A/B treatment arm (p5.js conventions)

gate/                       the deterministic referee, run by the worker
  sketch_gate.py            headless Chromium: six fixed checks + assertions
  accept.sh                 the harness: every fixture against expected.json
  fixtures/                 six sketches, one bug each, and what the gate must say

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
  worker.js                 votes, likes, views, GitHub OAuth
  schema.sql                D1 tables

bin/pull-backup.sh          pull the node's snapshots and jobs/ to this machine
requirements.txt            the venv's one pin: playwright==1.62.0

tests/                      17 files, stdlib unittest
docs/OPERATIONS.md          running the worker, the UI, the sync, and recovery
```

## The database

One SQLite file holds the whole system: `jobs` and their `attempts`, the `entries`
the gallery shows, paired `judgments`, `lineage`, `critiques`, `engagement` and
`likes`, `sync_state` for the write-path watermark, `meta` for the worker's own
timestamps, and a singleton `control` row that is the operator's pause switch. All
timestamps are UTC, ISO 8601 with a trailing `Z`, in columns named `*_utc`.

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

**You know this worked when** `db status` prints `schema_version: 1`, nine tables,
and `control: running`.

A job's legal moves are enforced in `sketchgen/db.py` (`TRANSITIONS`), not in SQL:
`queued → planning → executing → gating → held → published`, with `repairing` and
`needs-laptop` off to the side, three terminal states, and a `requeue` path back to
`queued` for the worker's stop-now control. An illegal move raises
`IllegalTransition` and changes nothing.

## The worker

One job at a time, from the queue to `held` or to a kept failure:

```
python3 bin/sketchgen enqueue --prompt "sixty drifting circles" --by octocat
python3 bin/sketchgen worker --once
```

`worker --once` reads the control row, fences the inference slot (`pgrep -af
opencode`; a model left resident by `KEEP_ALIVE` is not contention), claims the
oldest queued job, plans it if it arrived without a brief, then executes and gates
it up to `max_attempts` times, feeding the gate's evidence back into the next
attempt. Every step is stamped in UTC on stderr and in `<jobs>/<id>/job.log`, and
every attempt is a row in `attempts`. Without `--once` it stays resident, which is
systemd's job — see `docs/OPERATIONS.md`. Paths come from `$SKETCHGEN_DB`,
`$SKETCHGEN_JOBS`, `$SKETCHGEN_GATE` and `$OLLAMA_HOST_URL`, the same four the unit
sets.

Pause, stop and resume are database rows, not signals:

```
python3 bin/sketchgen control pause --reason "someone else wants the slot"
python3 bin/sketchgen control stop            # abort the attempt, re-queue the job
python3 bin/sketchgen control resume
```

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
scored only one of the two questions is not placed at all.

A kept rejection's `compare` link judges it against a random published entry — the
status quo — and says which side was rejected only after both answers are in.

## Tests

```
python3 -m unittest discover -s tests -v
```

Every script here exits 0 on success, 1 on failure, 3 when it refuses.
