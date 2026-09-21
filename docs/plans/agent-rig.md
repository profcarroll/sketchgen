# The agent's rig: try before you commit, and say what it cost

Build plan for the three packets dossier 01 asked for
(`docs/plans/agentic_cli_test_feedback_dossier_01.md`), grounded in the code as it stands at
`main` f2789bd (#135). Written for Opus builders; one packet per branch, one PR each, numbering
continues from `docs/plans/agentic-cli.md` (packets 0–5) and the dossier (6–8).

*Status: draft for the operator's review. Nothing here is built.*

## 0. What the run showed, in one paragraph

Job 1286 became entry 1279 in 4 min 26 s of node time and passed the gate first time. Before
`paid start` the agent spent 37 minutes and 193,000 generated tokens, most of it building a way
to look at and time its sketch, because the CLI offered neither. The node recorded a 111 s round
trip and no token counts, which is true and misleading. The three packets below give the agent
the node's own gate to try against, stage the local looking so nobody rebuilds it, and record
what the work around the reply cost, kept apart from the reply.

One new fact since the dossier, from the published `meta.json`: the gate measured entry 1279 at
**9.8 ms per frame**. The agent's best local proxy, a CPU canvas on the laptop, read 6.4 ms.
`MEASURE[rig-proxy-vs-gate]` is answered: the proxy under-reads the ARM node by about a third.
A local rig is a floor, never the number. That is the argument for Packet 7 being the real fix
and Packet 6 being a stopgap, and both packets say so in their prose.

## 1. Decisions, with recommendations

The dossier left these open. Each is decided here unless the operator overrides; builders take
the recommendation.

| id | decision | recommendation |
| --- | --- | --- |
| `DECIDE[try-where]` | in the worker, or a CLI subprocess | **In the worker.** The worker is one thread, so a try can never overlap a real gate; it already owns `gate_fn`, `jobs_dir` and the stub-replay path (`Worker._paid_execution`); and while a lease is live it is standing by with nothing to do, which is exactly when tries arrive. A CLI subprocess would need a cross-process lock and would still contend with a local executor's decode. |
| `DECIDE[try-budget]` | tries per job | **8**, in a `meta` row so it can change without a deploy. The 9th refuses with exit 3 and the count. A gate run is ~5–15 s of the node's CPU; eight is a couple of minutes, and an agent that needs more is designing on the node's clock. |
| `DECIDE[agent-budget]` | the numbers `start` prints | **Advisory, from a `meta` row, defaults 10 min from `--since` to held and 30,000 generated tokens.** Set from this run's job phase (4 min 26 s, 14,798 tokens) with headroom, and to be re-set once `MEASURE[freenode-baseline]` is read. Never enforced: refusing an over-budget import would reward not reporting. |
| `DECIDE[freshness]` | how an agent learns the node is ahead of its checkout | **`preflight` and `start` report `node_commit`** (git `rev-parse HEAD` in the app checkout, null if unavailable) and AGENTS.md tells the agent the one-line check. `bin/sg` stays a dumb pipe. |
| `DECIDE[process-on-entry]` | whether the public entry shows process cost | **Yes, in the provenance table and `meta.json`, marked *as reported by the agent*, and on the operator's job page.** Never in the entry's headline, never in any A/B or judgment measure. The provenance table is already where "as reported by the model" lives. |
| skill on a job | whether a job may name a skill | **Record, do not model.** `start --note "skill=algorithmic-art; local prototype"` is free text on the job and entry. A lite skill is Anthropic's repository, not this one. |
| "paid and local" | who gets the rig | **Any agent holding a lease**, whatever model it is; `try` checks the lease, not the model's price. A local *Ollama executor* cannot call a verb; its rig is the prompt, and a prompt change is `executor-v4`, an A/B variable. Out of scope here, flagged in §6. |

## 2. What every packet inherits

- Repository: `profcarroll/sketchgen`, laptop clone `/home/dave/sketchgen`, node checkout
  `~/sketchgen/app`. Python 3.12, stdlib only. Tests: `python3 -m unittest discover -s tests`
  from the repo root, ~75 s, no network, no model, no browser; every gate in a test is a stub
  (`tests/test_worker.py` `StubGate`, `make_worker(gate_fn=…)`). Anything needing Playwright is a
  skip that names the node, like the three that exist.
- AGENTS.md rules 1–5 bind the builder too: never a second worker, no credential on the node,
  never move a job into a running state by hand, no hand edits to the database, push as
  `profcarroll` on a branch. Exit codes 0/1/3 everywhere; agent-facing verbs take `--json`.
- New CLI verbs go in `sketchgen/cli/paid.py` (it already holds the paid subparsers) with the
  `_run` wrapper `cmd_next` uses. One JSON object on stdout; progress lines on stderr.
- Comments say *why*, naming the incident (job 1286, entry 1279, 2026-09-21). Commit messages are
  prose. `docs/OPERATIONS.md` → *Paid steps* and AGENTS.md → *Driving a job with a paid model*
  are updated in the packet that changes the verbs they describe, not later.
- A packet's PR description ends with the deploy note from §5 for that packet.

## 3. Packet 6: the rig, staged

**Branch** `feat/agent-rig`. Files and prose; the only Python is `rig/cost.py`. Half a day.

### 3.1 What lands

```
rig/
  README.md        the six traps, the gate facts table, one rule
  index.html       the gate's own page: same script order as executor.index_html_for
  fetch-p5.sh      curl p5 1.11.3 from cdnjs into rig/p5.min.js, verify sha256, refuse otherwise
  probe.js         paste-in: 0×0 and 1×1 windows, portrait, wide, ten rapid clicks,
                   a finite-values sweep, an error hook that survives reload
  bench.js         paste-in: swaps drawingContext for an offscreen 2d canvas with
                   willReadFrequently, times N frames, prints ms/frame beside the gate's
                   100 ms budget and the 1.5× node factor from entry 1279
  cost.py          the dossier's appendix, unchanged in behaviour
.claude/launch.json   tracked: one configuration, python http.server on rig/, so the file the
                      agent had to create and delete is part of the repository
.gitignore            + rig/p5.min.js
```

`rig/README.md` opens with the rule, in bold: *the rig is advisory; the gate's number is the
number, and on the node it runs about 1.5× the rig's.* Then the six traps from dossier §5.1,
each as one line of symptom and one of fix. Then the facts table from dossier §5.2 with the
file and constant each fact comes from. Then the recipe: `bash rig/fetch-p5.sh`, start the
preview from `.claude/launch.json`, `resize_window` 1280×900, select the tab, paste the sketch
into `rig/sketch.js` (gitignored too), paste `probe.js` then `bench.js` into the console.

### 3.2 AGENTS.md

A new subsection under *Driving a job with a paid model*, before *The three verbs*, headed
**Before you start**:

> **Budget.** Your first command is `date -u +%FT%TZ`; keep it, Packet 8 takes it as
> `--since`. A sketch is a few minutes' work: read this file, plan, write under 150 lines,
> check it, import. The executor prompt says "no second chance". That is written for local
> models, which get one reply per attempt. You have three attempts and a verdict takes seconds.
> **Looking.** `rig/README.md` is a browser rig for looking at a sketch on the laptop. It is a
> floor, not the gate. (Packet 7 replaces most of it with `paid try`, which runs the real gate.)
> **One session, both steps.** `--executor` with your own id is accepted and runs as one session;
> you are not expected to hand off to yourself.

Dossier §7.6 and row 7 of §4 are closed by that paragraph. `prompts/executor.md` is not touched:
`executor-v3` is a variable in the running A/B.

### 3.3 Acceptance

1. A test in `tests/test_rig.py` imports `gate.sketch_gate` and asserts the README's facts table
   states `VIEWPORT`, `IDLE_FRAMES`, `PROBE_FRAMES`, `DEFAULT_FRAME_BUDGET_MS` and
   `DEFAULT_BUDGET_S` as the module defines them, so the table cannot drift from the gate.
2. The same test asserts `rig/index.html` contains `executor._P5_TAG`'s script line, adjusted
   only for the local path, in the same position relative to `sketch.js`.
3. `fetch-p5.sh` refuses (exit 3) on a sha256 mismatch; tested with a fake `curl` on `PATH`,
   the way `tests/test_sg.py` fakes `ssh`.
4. `cost.py --since X --until Y` on a fixture transcript in `tests/fixtures/` reproduces a known
   line; an empty window exits 1.
5. `git status` is clean after the recipe has been followed once (p5 and the pasted sketch are
   ignored).

## 4. Packet 7: `paid try`

**Branch** `feat/paid-try`. About a day and a half. The real fix.

### 4.1 The verb

```
bin/sg paid try --job N --as $ME < answer.txt
```

Stdin is the text the agent would put in `items[0].answer`: the fenced `js` block, optional
`html` block, and statement. Not a bare `sketch.js`, on purpose: a try then exercises
`executor.parse_response` too, so a reply that would be rejected at `import` is rejected here,
for free, without reaching the worker.

The CLI:

1. Checks the lease: the job must be leased to `$ME` (`db.paid_leases`). Otherwise exit 3 with
   `do: stop` and the holder, as `next` does. A live lease is the whole permission model; no
   new column says who may try.
2. Parses the answer. A parse failure returns at once, exit 0, `do: rejected` with the reason,
   the same words `import` would use. Nothing is written on the node.
3. Counts prior tries for the job against the cap (`meta` key `paid_try_cap`, default 8). At
   the cap: exit 3, `do: stop`, `say: "8 of 8 tries used on job N; import an attempt"`.
4. Writes the reply to `<jobs_dir>/<N>/try-K/reply.txt` and a request into the `meta` row
   `paid_tries` (JSON keyed by job id, like `paid_leases`), renews the lease, and polls for
   `result.json` in the try directory for up to 240 s, printing a stderr line every 30 s.
5. Returns one JSON object: `do: verdict` with `exit`, `checks`, `assertions` (each with
   `detail`), `timings.ms_per_frame`, `console` (first 20 lines), `resources`, `notes`, the
   node paths of `strip.png` and `gate.png` (fetched with the `scp` the judge steps already
   use), `tries_used`, `tries_cap`, and `then`, which is either `sketchgen paid import -` on a
   clean pass or `sketchgen paid try --job N --as $ME` with the failing check named. Or
   `do: wait` after 240 s with `worker` from `paid.worker_now`, and the same command as `then`.

### 4.2 The worker's half

`Worker._serve_tries()`: read `paid_tries`; for each pending request whose job's lease is
live, replay the reply through `executor.run(stub=…)` into `try-K/` exactly as
`_paid_execution` does, run `self.gate_fn` with the job's assertions (from `assertions_json`;
none if the job is not yet planned, so a try before the plan is QA-only and says so in
`notes`), write `result.json` from the report plus the verdict summary (§4.4), mark the
request served. Requests whose lease has lapsed are dropped with a note; nothing serves a try
for an agent who left. A status-card step `trying` with the job and try number, so the
console shows what the worker is doing (`_say`).

Where it is called:

- at the top of `_nap`, every 5 s of the slices, while any lease is live. That is the latency
  that matters: a try costs the gate's 5–15 s plus at most 5 s, instead of the 30 s poll;
- in `_idle_round` before `_standing_by()` returns;
- in `run_once` after the fence and the sweeps, before the claim.

Never from inside `_run_job`: a job in flight finishes first. That is what makes acceptance 3
true by construction rather than by a lock.

Constraints, stated in the docstring with their reason:

- writes no `attempts` row, no entry, no transition, and touches no job column: a try is
  advisory, and the gate stays the referee (agentic-cli §8);
- `HARNESS_VERSION` is untouched: the gate did not change;
- the try count is kept for Packet 8, which records it on the attempt.

### 4.3 The per-job record

`try-K/` under the job directory holds `reply.txt`, `sketch.js`, `index.html`, `.gate/` and
`result.json`. Nothing about tries is in `jobs` or `attempts` yet; the `meta` row and the
directories are the record. Packet 8 copies the count onto the attempt that is finally
imported, in `process_json`, so an entry can say *attempt 1 of 3, after 4 tries*. That number
matters to the A/B: a first-attempt pass after four tries is not a first-attempt pass.

### 4.4 `done` says what happened

Dossier §7.2. `paid.next_step` for `held`/`published` gains `verdict`: from the kept attempt's
`report.json`, `exit`, each assertion's pass/fail, `ms_per_frame`, `offplan` (the missed
assertions, if held that way), and `artefacts` (node paths). One helper,
`paid.verdict_summary(report)`, builds it, and `try` uses the same helper for `result.json`,
so an agent reads one shape in both places.

### 4.5 Acceptance

1. **Plumbing, offline.** With `make_worker(gate_fn=StubGate([0]))`: a request written for a
   leased job is served on the next `_nap` slice, `result.json` has the verdict shape, the
   `attempts` table and the job row are byte-identical before and after, and the lease's
   `until_utc` moved forward.
2. **Never overlaps.** A request written while `_run_job` is in progress (a stub executor that
   writes a request mid-attempt) is served only after the attempt's gate row exists; assert
   ordering on the activity steps.
3. **Lease is the permission.** No lease, or another model's lease: exit 3, nothing written to
   the node. A lapsed lease at serve time: the request is dropped with a note, no gate run.
4. **The cap.** The 9th try exits 3; changing `paid_try_cap` to 2 makes the 3rd refuse.
5. **A bad reply costs nothing.** An answer with no `js` block returns `do: rejected` and the
   `paid_tries` row is unchanged.
6. **Same referee, node only.** `gate/fixtures/*` each submitted through `try` return the
   `exit` and checks in `expected.json`. A skip on the laptop that names the node, run once by
   the operator with the real worker and recorded in the PR.
7. `done` on a held job carries `verdict` with the assertions and `ms_per_frame` from
   `report.json`; `test_paid.py`'s existing done test is extended, not replaced.
8. The whole existing suite passes with no edits outside the tests the packet adds.

## 5. Packet 8: the process cost, recorded

**Branch** `feat/paid-process-cost`. About a day. Lands after Packet 7 (it records the try
count) and touches the entry page, so it needs the full render.

### 5.1 Migration `015_process_cost.sql`

Additive only, `ALTER TABLE … ADD COLUMN`, as `012` did, so no CHECK rebuild:

- `jobs.since_utc TEXT` — when the agent says the task began; *declared*, never inferred;
- `jobs.note TEXT` — free text from `start --note`: skill used, local prototype, anything the
  node cannot see;
- `attempts.process_json TEXT` — `{session_s, output_tokens, thinking_tokens, tool_calls,
  screenshots, effort, tries}`, every field nullable, from the item's `process` at import,
  with `tries` filled by the node from Packet 7's count;
- `entries.note TEXT`, `entries.process_json TEXT` — copied at `_create_entry` from the job
  and the kept attempt, "while the worker still knows which attempt was the one that counted",
  the way every other entry column is filled.

The header comment carries the argument from `012`: what the node cannot see about how an
entry was made (a 37-minute prototype, a skill) is a variable the comparison cannot control
for unless it is recorded; entry 1279 is the case.

### 5.2 Verbs

- `paid start --since ISO --note TEXT`, both optional. `since` is validated as UTC ISO and
  stored; a `since` in the future or more than a day old is refused (exit 3) as a typo.
- `start` prints a **budget** line from `meta` `paid_budget` (defaults §1): `budget: held
  within 10 min of --since, under 30,000 tokens generated, 8 tries · advisory`. `next` and
  `import` echo `elapsed` (from `since_utc`, else from the lease's `since_utc`, said which)
  and, past either number, one line `over budget by …; finish, and report it`. Never a refusal.
- `import` accepts `process` beside `usage` on an item, validates types, stores it on the
  attempt. AGENTS.md's honesty rule stands: leave a field out rather than guess; `rig/cost.py`
  prints the object to paste.
- `paid budget [--minutes N] [--tokens N] [--tries N]` for the operator; with no flags, prints.
  It writes `paid_budget` and `paid_try_cap`. This is the verb rule 4 asks for.
- `preflight` and `start` report `node_commit` and `agents_md_sha256` in `info`
  (`DECIDE[freshness]`); AGENTS.md's *Before you start* gains: *`bin/sg paid preflight` prints
  `node_commit`; if `git merge-base --is-ancestor <it> HEAD` fails in your checkout, pull
  before reading further.*

### 5.3 Where it shows

- Entry page provenance table (`gallery._provenance_rows`), after *Wall seconds*: **Process**,
  one line, e.g. `42 min · 207,537 tokens generated · 4 tries · as reported by the agent`, dashes
  for missing fields; and **Note** when set. `meta.json` gains `process` and `note`.
- The operator's job page (`web.py`) shows the same beside the attempt, plus `since_utc`.
- Nothing in `pairs.py`, the judge, or any batch total reads these columns. A test asserts
  the entry's `wall_s`, `prompt_tokens` and `completion_tokens` are unchanged by a `process`
  object, so reply cost and process cost stay two numbers.

### 5.4 Acceptance

1. Migration on a copy of a pre-015 database leaves every existing row readable and every
   test fixture database at schema 15; `db init` applies it (update.sh's path).
2. A `start --since` round trip: the job row has it; `next` echoes `elapsed`; an over-budget
   `import` prints the line and still records.
3. A `process` object on an import lands on the attempt, then on the entry with `tries` from
   Packet 7; a missing `process` leaves nulls, never zeros.
4. The entry page renders the Process row with dashes for nulls and the *as reported by the
   agent* suffix; `meta.json` round-trips it.
5. `paid budget --tokens 20000` changes what `start` prints; no flags prints the current
   values.
6. `preflight` reports `node_commit` as null when git is absent and never fails on it.

## 6. Out of scope, and why

- **A rig for the local executor.** An Ollama model cannot call `try`; giving it the gate's
  facts in the prompt is `executor-v4`, a new arm in the rules-file A/B. Decide separately.
- **`responds(click)` cannot fail a moving sketch** (dossier §7.3). A gate change bumps
  `HARNESS_VERSION` and splits the corpus; `MEASURE[click-assertion-power]` first.
- **A lite `algorithmic-art` skill.** Not this repository. `--note` records that one was used.
- **Enforcing a budget.** See `DECIDE[agent-budget]`.
- **The two instructions that disagree** (dossier §7.4): the rules file is an experimental
  variable; recorded, not fixed.

## 7. Order, parallelism, deploys

Packets 6 and 7 touch disjoint files (`rig/`, AGENTS.md, one test; versus `worker.py`,
`paid.py`, `cli/paid.py`, their tests) and can be built by two builders at once, with 7
adding its own AGENTS.md lines under the subsection 6 creates; whichever merges second
rebases. Packet 8 starts when 7 is on `main`.

| packet | deploy |
| --- | --- |
| 6 | `update.sh --no-render`: nothing on the node changes but files; AGENTS.md is read from the laptop. |
| 7 | `update.sh --no-render`; the worker restarts and serves tries from its next pass. Run acceptance 6 on the node once, by hand, and paste the fixture lines into the PR. |
| 8 | snapshot first (`sqlite3 sketchgen.db ".backup sketchgen.db.pre-015"`), dry-run the migration on a copy, then `update.sh` **with** the full render (~25 min): the entry page changed. Remember update.sh runs its old copy if the pull changes it, and resumes only what it paused. |

Effort: 6 half a day, 7 a day and a half, 8 a day.

## 8. Measurements to take alongside, not to build

- `MEASURE[freenode-baseline]`: median and p90 time and tokens per entry with no paid step, from
  the batch totals `web.py` already has. A read on the node; it sets the numbers in
  `paid budget`.
- `MEASURE[agent-cost-vs-effort]`: three prompts at each effort level, with Packet 7's `try`
  and Packet 8's `process`, recording tries used. The dossier's suspect is effort `max`.
- `MEASURE[rig-proxy-vs-gate]`: one point so far, 6.4 ms local against 9.8 ms on the node for
  entry 1279. Each Packet 6 user adds a row to `rig/README.md`.
- The second paid batch: with the rig in place, run the plain paid batch AGENTS.md asks for, no
  skill, `--note` empty, so entry 1279 is not the only off-node point.
