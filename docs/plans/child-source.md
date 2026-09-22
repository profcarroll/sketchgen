# The executor reads what it revises: parent source on a child, previous source on a repair

Build plan for giving the executor the sketch it is being asked to change — the parent
entry's `sketch.js` when the job is a child spawned from a critique, and its own previous
attempt's `sketch.js` when the gate sent it back — instead of a prose description of either.
Grounded in the code as it stands at `main` 1c39d24 (#139). Written for Opus builders; one
packet per branch, one PR each, numbering continues from `docs/plans/auto-mouse.md`
(packets 13–16).

*Status: Packet 17 built on `feat/executor-source` (2026-09-21); Packet 18 built
on `feat/executor-source-shown` (2026-09-22), with migration 017 for
`jobs.executor_source` and one correction to §4.1, struck through below.*

## 0. What happens today, in one paragraph

A critique becomes a child job through `lineage.spawn` (`sketchgen/lineage.py:310`), whose
prompt is the parent's prompt with the critic's sentence under `Revise:`
(`compose_prompt`, `:270`) and nothing else: no brief, no code. The planner sees only that
prompt (`planner.build_prompt`, `planner.py:187`, two slots, `{prompt}` and `{by}`). The
executor sees rules, brief, assertions and seed (`executor.render_prompt`, `executor.py:252`,
four slots) and is told *there is no conversation after this message and no second chance to
revise* (`prompts/executor.md:3`). A repair attempt gets the same brief with the gate's
evidence under a heading (`worker.brief_with_evidence`, `worker.py:1056`, the whole mechanism
in five lines) and never the sketch that produced the evidence: `executor.py:5–6` says so on
purpose, *a repair turn sends the evidence again rather than re-sending a transcript*. The
critic, for its part, is forbidden from writing code (`lineage.validate`, `_CODE_MARKS`) and
writes one sentence under forty words. So the only bridge between a sketch and its revision
is that sentence, and the only bridge between attempt 1 and attempt 2 is prose about what
went wrong. Entry 1103 shows the cost: five attempts at a jigsaw puzzle; attempt 1 passed
`responds(drag)` and took 11 s to run because it fetched a photograph; attempts 4 and 5 ran
in 1.4 s and passed nothing, having dropped the photograph and the drag that worked. Each was
written from a blank page. `docs/plans/lineage-ledger.md:254` records the rule this plan
changes: *`lineage.spawn` is by entry and inherits the prompt only. A child never inherits
code.*

## 1. Decisions, with recommendations

| id | decision | recommendation |
| --- | --- | --- |
| `DECIDE[source-in-brief]` | the source goes under a heading inside `${brief}`, or in a new template slot | **Inside the brief, under a heading, `prompt_version` unchanged.** This is the precedent `worker.py:298–301` set for the evidence, for the stated reason: the template needs no slot and its version does not move. `executor-v3` stays the arm label in the rules-file A/B; what was given is recorded per attempt (§3.3) so the two populations can be told apart. A `${source}` slot would be `executor-v4`, would refuse every paid packet in flight (`paid.py:597`, the *cut under* refusal in `ExecuteAdapter.land`), and would say nothing the record does not. |
| `DECIDE[which-source]` | parent, previous attempt, or both | **Both, each under its own heading, and never both at once by accident:** attempt 1 of a child gets the parent's; attempt 2+ of any job gets its own previous attempt's; a child's attempt 2 gets its previous attempt's, not the parent's — the parent is already two steps back and the budget (§1, `num-ctx`) has room for one sketch. |
| `DECIDE[which-parent-attempt]` | which of the parent's attempts | **The one the entry kept: `entries.source_dir`**, which `worker._create_entry` sets from `best_attempt` (`worker.py:761`), with the fallback chain `console._sketch_lines` uses (`console.py:1028`): the row's `source_dir`, then `<jobs_dir>/<job>/attempt-<n>/`. lineage-ledger §8.3's `parent_attempt` column is not built here; the record in §3.3 names the path and hash of what was actually given, which is the fact that matters. |
| `DECIDE[source-cap]` | what to do with a long sketch | **Include whole or not at all.** A cut sketch is worse than none: the model rewrites the half it cannot see. Cap at `source_max_chars`, a `meta` row, default 16,000 (about 4,000 tokens, ~250 lines); over it, the heading says *the previous sketch was N lines and is not shown*, and the evidence stands alone as today. |
| `DECIDE[num-ctx]` | the local model's context | **16,384 when a source is included, 8,192 otherwise.** Today's budget is 2k in, 3k out against `DEFAULT_NUM_CTX = 8192` (`executor.py:75`); a 3k-token sketch under the brief leaves no room for attempt 3's accumulated evidence. `num_ctx` is not threaded from `worker.default_executor` (`worker.py:1088`) to `executor.run`; this packet threads it. The memory cost on the node is a KV cache twice the size for the executor's decode; `MEASURE[source-ctx-cost]` reads it. |
| `DECIDE[source-switch]` | how to turn it off | **A `meta` row, `executor_source`, values `both` (default), `parent`, `previous`, `none`.** No deploy to change; `none` is byte-for-byte today's prompt. A batch run with `none` is the control for `MEASURE[source-follow]`. |
| `DECIDE[planner-sees-parent]` | whether the planner gets the parent's brief or code | **Not in this series.** The planner's job on a child is to turn *the same X, and this time Y* into a brief, and the composed prompt already carries every `Revise:` line. Giving it the parent's brief is `planner-v2`, a second prompt version in the same change; giving it code invites a brief that narrates code. Measure `MEASURE[source-follow]` first; if the executor with source still misses the critique, the planner is the next suspect. |
| `DECIDE[template-wording]` | whether `prompts/executor.md` says anything about revising | **No.** The heading's own lead sentence tells the model what to do with the source (§3.1); the template's *one shot* sentence remains true of the reply. Touching the file is `executor-v4`. |

## 2. What every packet inherits

- Repository, tests, rules: as `docs/plans/agent-rig.md` §2. `python3 -m unittest discover
  -s tests`, ~75 s, no network, no model, no browser; every model reply is a stub on disk
  (`tests/fixtures/`), every gate a `StubGate`.
- AGENTS.md rules 1–5 bind the builder. A migration is a new `migrations/016_*.sql` — 015 is
  #139's `process_cost` — applied by `db init` and `update.sh`, snapshot first.
- Provenance is who answered. The source given to a model is an input, recorded as an
  input; the model on the attempt stays the model that wrote the reply.
- The two places a brief is assembled for execution are `worker._attempt` (`worker.py:2756`)
  and `paid.ExecuteAdapter.offer` (`paid.py:525`), which calls the same
  `brief_with_evidence`. Anything this plan adds goes through one helper both call, so a
  paid agent and a local model are shown the same words.

## 3. Packet 17: the source under the brief

**Branch** `feat/executor-source`. About a day and a half.

### 3.1 The helper

`worker.py`, beside `EVIDENCE_HEADING`:

```python
PARENT_HEADING = "## The sketch this revises"
PREVIOUS_HEADING = "## Your previous attempt, which the gate sent back"
```

`worker.source_for(conn, job, n, jobs_dir) -> GivenSource | None`: for `n == 1` and a job
with `parent_entry_id`, the parent's kept `sketch.js`; for `n > 1`, `attempt-<n-1>/sketch.js`;
subject to `executor_source` and `source_max_chars` from `meta`. `GivenSource` is a
dataclass: `kind` (`parent` | `previous`), `path`, `sha256`, `lines`, `text`, `shown`
(false when over the cap).

`worker.brief_with_source(brief, given) -> str`, the twin of `brief_with_evidence`, appended
**before** the evidence heading so the prompt reads brief → source → what the gate found.
Under `PARENT_HEADING`, one lead sentence, then the code fenced as `js`:

> This is the published sketch the brief revises, as its author wrote it. Keep what the
> brief keeps and change what the `Revise:` line asks for; emit the complete revised sketch,
> not a diff.

Under `PREVIOUS_HEADING`:

> This is your previous attempt, complete. The gate's findings on it follow. Fix what they
> name and keep the rest; emit the complete sketch.

When `shown` is false the same heading carries one line: *N lines, longer than the M
characters this prompt has room for; not shown.*

Call sites: `worker._attempt` at `:2756` becomes
`brief = brief_with_evidence(brief_with_source(job.brief or job.prompt, given), evidence)`,
and `paid.ExecuteAdapter.offer` does the same, replacing `inputs.previous_sketch` — a
node-side absolute path no off-node agent can read (`paid.py`, `previous` in `offer`) — with
`inputs.source = {"kind", "sha256", "lines", "shown"}`. The guard hash (`paid.py`, *attempt n
+ evidence*) gains the source's sha256, so a packet cut before the source changed is refused
as stale, as it is for a changed evidence.

### 3.2 `num_ctx`

`worker.default_executor` gains `num_ctx`, passed to `executor.run`; `_attempt` passes
`16384` when `given.shown` else `executor.DEFAULT_NUM_CTX`. `attempts` records it (§3.3), so a
decode time can be read against the context it ran under.

### 3.3 The record

`migrations/016_given_source.sql`:

```sql
ALTER TABLE attempts ADD COLUMN given_source_json TEXT;   -- {"kind","path","sha256","lines","shown"} or NULL
ALTER TABLE attempts ADD COLUMN num_ctx INTEGER;         -- what the executor was run with
```

NULL on every attempt before this migration, deliberately: it means *not given*, which is
true of all of them. `db.add_attempt` and `db.Attempt` (`db.py:575`, `:228`) carry both.
`worker._attempt` writes them on every attempt, including a paid one (`_paid_execution`
writes what the packet's `inputs.source` said, which is what the agent saw).

### 3.4 Tests

- `tests/test_worker.py`: `brief_with_source` output for each kind, the cap, and `none`;
  `source_for` on a child job resolves the parent's `source_dir`, falls back to the
  reconstructed path, and returns `None` when neither exists; attempt 2 of a job gets
  attempt 1's file and not the parent's; the attempt row carries `given_source_json` with
  the right sha256 and `num_ctx` 16384; with `executor_source = none` the prompt written to
  `prompt.txt` is byte-identical to today's fixture.
- `tests/test_paid.py`: the packet prompt carries the fenced parent; `inputs.source` is
  present; an import cut before a new attempt's source is refused as stale.
- `tests/test_executor.py`: `run` passes `num_ctx` through to the request's `options`.
- `tests/test_db.py`: migration 016 on a pre-016 fixture leaves every row readable and the
  new columns NULL.

### 3.5 Acceptance

1. A child job on the node, spawned from entry 1103 with the critic's sentence, produces an
   attempt whose `prompt.txt` contains 1103's `sketch.js` under `PARENT_HEADING`, and whose
   `attempts.given_source_json` names 1103's `source_dir` and the file's sha256.
2. The same job's attempt 2, if any, carries attempt 1 under `PREVIOUS_HEADING` and not the
   parent.
3. `paid next` on a child job returns a packet whose `items[0].prompt` contains the parent
   source and whose `inputs.source.sha256` matches it.
4. Setting `executor_source` to `none` in `meta` and re-running a job gives today's prompt.

## 4. Packet 18: where it shows, and the operator's hand

**Branch** `feat/executor-source-shown`. About a day.

### 4.1 The pages

- The operator's job page (`web.py`, the attempt list): a line per attempt, *given: parent
  entry 1103 · 180 lines · ctx 16384* or *given: attempt 1 · 143 lines*, or *given: nothing*.
  **Built 2026-09-22** with `ctx` on every line and `ctx —` where the attempt was written off
  the node, and one more segment — *not shown (over the cap)* — for a sketch that was found
  and was too long; ` · ` throughout, matching the dim line it joins.
- The entry page provenance table (`gallery._provenance_rows`, `gallery.py:2084`): one row,
  **Revised from**, on entries whose kept attempt was given a source: *entry 1103's sketch,
  180 lines, then 2 attempts on its own* — ~~dashes when NULL~~ **absent when NULL, corrected
  2026-09-22: a row of dashes on all 910 published entries invites the reader to wonder what
  it lost, and the promise the packet makes instead is that every page already published
  renders byte for byte what it did. The row appears exactly where
  `lineage.inherits_source` is true — the kept attempt held the parent's code *and* it was
  shown — so a parent sketch found and over the cap gets no row either: it revised nothing,
  and the fact that it was offered is on the job page and in `meta.json`.** `meta.json`'s
  `gate` array
  entries gain `given` from the attempt row, and the entry-level `lineage` object gains
  `inherits_source: true|false`, so the ledger can say which generations were revisions of
  code and which of prompts.
- The New job page's parent card (`web._parent_card`, `web.py:2692`) says what the child will
  be given, in one sentence, and a checkbox *give the executor the parent's sketch* that is
  on by default and, unchecked, sets the job's `executor_source` override — a job column,
  `jobs.executor_source TEXT`, NULL meaning *whatever `meta` says*, ~~added in the same
  migration as §3.3 if the packets are built together, else~~ `017`. `lineage.spawn` sets
  nothing; the idle loop's children follow `meta`. **Built 2026-09-22 just outside
  `#parent-card` rather than inside it: the page's own script replaces that element's
  innerHTML when a parent is picked without a reload (`pickParent`), and a form field inside
  it would be thrown away mid-form. The block is always in the DOM and `hidden` until there
  is a parent, and it carries a hidden `source_form` marker — a clear checkbox posts nothing,
  so without the marker "unticked" and "this form never had a box" arrive identical, and
  those are the two different answers *this job is the control arm* and *the node decides*.**

### 4.2 The ledger

`docs/plans/lineage-ledger.md:254` is rewritten in place, dated: a child inherits the
prompt, and since this packet its executor is shown the parent's kept sketch, recorded per
attempt; `lineage.spawn` itself still copies no code. §8.3's `parent_attempt` remains
unbuilt and now has a smaller job when it comes: choosing which attempt's sketch to show.

### 4.3 Tests

`tests/test_web.py` for the two lines and the checkbox round trip; `tests/test_gallery.py`
for the provenance row, the `meta.json` fields, and that an entry with NULL everywhere
renders exactly as before; `tests/test_lineage.py` for the child of a job with an override.

## 5. Out of scope, and why

- **The planner** (`DECIDE[planner-sees-parent]`).
- **Changing `prompts/executor.md`** (`DECIDE[template-wording]`). When `executor-v4` is
  cut for other reasons, its first line should stop saying *no second chance to revise* to a
  model that is holding its previous attempt; note it in that version's PR.
- **Showing the critic code.** `critic-v3` is built on the image winning over the words; a
  critic that reads code writes about code, and `lineage.validate` would reject it.
- **The paid `try` verb** already runs the reply through the same executor parser; a paid
  agent gets the source in its packet and needs nothing else.
- **Retroactive records.** Attempts before 016 stay NULL; nothing infers what they were given.

## 6. Order, parallelism, deploys

17 first; 18 depends on its columns. One builder for both is simplest; two can start 18's
page work against 17's branch.

| packet | deploy |
| --- | --- |
| 17 | snapshot first (`sqlite3 sketchgen.db ".backup sketchgen.db.pre-016"`), dry-run the migration on a copy, then `update.sh --no-render`: the worker restarts and the next attempt is given its source. Watch the first child job's `prompt.txt` on the node and the executor's decode time in the status card. |
| 18 | `update.sh` **with** the full render: the entry page and `meta.json` changed. If 18 carries the `jobs.executor_source` migration, snapshot again. |

Remember `update.sh` runs its old copy if the pull changes it, and resumes only what it
paused.

Effort: 17 a day and a half, 18 a day.

## 7. Measurements to take alongside, not to build

- `MEASURE[source-follow]`: does a child built with the parent's source follow the critique
  more often than one built without? Two batches of the same ten parents, `executor_source`
  `both` and `none`, the same critic sentences, judged by a person on one question: *is the
  change the critique asked for visible, and is the rest the same sketch?* This is the
  number the whole plan is for.
- `MEASURE[repair-keeps]`: among jobs that reach attempt 2, how often attempt 2 passes an
  assertion attempt 1 passed. Entry 1103 lost `responds(drag)` after attempt 1; before the
  change, a read over `report.json`s gives the baseline; after, the same read.
- `MEASURE[source-ctx-cost]`: the executor's decode time and the node's memory at
  `num_ctx` 16384 against 8192, from the attempt rows' `decode_s` and `num_ctx`. If the cost
  is large, `DECIDE[source-cap]`'s default comes down before the context does.
- `MEASURE[source-shown-rate]`: how many sources fall over the cap. If it is common, the
  cap is wrong or the sketches are.
