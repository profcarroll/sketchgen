# Operator cards

Two cards for the operator UI: a **process status card** that says what the worker is doing right
now, and a **decision card** on Held that asks for one decision without saying the same thing six
times.

Mockup (the visual spec; **option A** was chosen for both cards):
https://claude.ai/artifact/MAvnvQk9ecfQMR2nGUGKED

Both packets landed on 2026-09-16: packet 5 as #65, packet 6 as #66. This document is
kept as the record of what was decided and why, not as work outstanding.

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at
  `~/sketchgen/app`. Python 3.12, stdlib only, `pytest tests/`. Templates are `string.Template`
  files in `sketchgen/templates/`. The operator UI is one `http.server` process on 127.0.0.1:8081
  (`systemd/sketchgen-web.service`); the worker is a **different process**
  (`systemd/sketchgen-worker.service`). They share one WAL-mode SQLite file and nothing else.
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. **Neither packet here touches
  it.** No `gallery.py`, no `assets/`, no published entry changes.

Conventions: small PRs, one packet per branch, commit messages in the repo's existing voice (see
`git log`). Every packet lands with tests in `tests/`. Do not touch `prompts/` or
`tests/test_executor.py`; another session owns those uncommitted changes on the node.

## 1. Decisions already made

1. The status card is the **now line**: one sentence in the present tense, the actors under it,
   elapsed against the median for that step, and the three steps just before. Not a pipeline rail —
   a rail goes dead the moment the queue empties, and idle work is the half the operator cannot see
   today.
2. The status card **replaces** the Console's Worker tiles (control, job in flight, up since). One
   place on the screen answers "what is it doing". A one-line version sits above the Queue's jobs
   table.
3. The worker's step vocabulary is **plain language and fixed in this document** (§2.4). The job
   state machine keeps its own names — `jobs.state` is still `executing`, `gating`, `repairing`, and
   its CHECK constraint is not touched. The operator reads *writing*, *evaluating*, *correcting*;
   the database still reads `executing`. These are two vocabularies on purpose: one is a schema, the
   other is English.
4. **No thread, no second timer, no SSE.** The worker blocks inside one non-streaming HTTP call for
   a whole step (67 s for a sketch), so it cannot tick a heartbeat mid-step and must not be given a
   thread to do it with. Liveness is the recorded pid plus the step's start time. Progress is
   elapsed against the median.
5. The decision card is **one number, one image, one input, four verbs**. The same text box serves
   Reject (the reason) and Critique (the child's revision sentence); the button pressed says which.
6. `critique_by` leaves the Held page. A critique typed there is the operator's, and
   `spawn_child` already defaults to `operator_username(row)` when the field is absent.
7. The four-frame strip is drawn **once**. Today the poster the run button sits on *is* the strip
   file, and `_entry_image` prints it again underneath.

## 2. Packet 5: the process status card

Owner: one agent. Branch `packet-5-process-status`. Touches `migrations/`, `db.py`, `worker.py`,
`console.py`, `web.py`, `templates/op_console.html`, `templates/op_queue.html`,
`templates/op_layout.html`, `cli/console.py`, and tests.

### 2.1 Migration 008: one row per step

`migrations/008_activity.sql`:

```sql
CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY,
    step         TEXT NOT NULL,          -- the key from the table in 2.4
    headline     TEXT NOT NULL,          -- the sentence, already in English
    detail       TEXT,                   -- the line under it, or NULL
    job_id       INTEGER REFERENCES jobs(id),
    entry_id     INTEGER REFERENCES entries(id),
    model        TEXT,
    pid          INTEGER NOT NULL,       -- the worker process that wrote it
    started_utc  TEXT NOT NULL,
    ended_utc    TEXT                    -- NULL while the step is running
);

CREATE INDEX IF NOT EXISTS activity_recent_idx ON activity (id DESC);
```

Follow the house rules the other migrations keep: `IF NOT EXISTS` throughout, a header comment
saying what the table is for, and `db.migrate()` applies it inside one transaction. `update.sh`
already runs the migrations on the node.

### 2.2 `db.py`

```python
ACTIVITY_KEEP = 200          # rows kept; older ones are pruned on insert

def begin_step(conn, *, step, headline, detail=None, job_id=None,
               entry_id=None, model=None, pid=None) -> int
def update_step(conn, activity_id, *, detail=None, entry_id=None, model=None) -> None
def end_step(conn, activity_id) -> None
def current_activity(conn) -> sqlite3.Row | None
def recent_activity(conn, limit: int = 3) -> list[sqlite3.Row]
```

- `begin_step` closes any open row belonging to the same pid (`ended_utc = utc_now()`), inserts the
  new row, and prunes to the newest `ACTIVITY_KEEP`. `pid` defaults to `os.getpid()`.
- `current_activity` is the newest row with `ended_utc` null. `recent_activity` is the newest rows
  with `ended_utc` set, newest first.
- A database that predates this migration answers `None` / `[]` rather than raising, the way
  `worker.idle_summary` already treats a missing `judgments` table. An older file should still
  render.

### 2.3 `worker.py`

One helper beside the existing logger, and no new control flow:

```python
def _say(self, step, headline, detail=None, *, job_id=None, entry_id=None, model=None) -> None
```

It calls `db.begin_step`, keeps the id on `self._activity_id`, and also emits the existing
`self.log(...)` line, so the transcript is unchanged. **It never raises**: wrap the write in
`except sqlite3.Error` and log the failure. A card that cannot be written is not a reason to lose a
job — the same bargain `_idle_round` already makes.

Call sites, all of them already existing step boundaries:

| where | step |
| --- | --- |
| `run_once`, before the job is claimed | `claiming` |
| `_plan` | `planning` |
| `_attempt`, before `executor_fn` | `writing` |
| `_attempt`, after `db.transition(..., "gating")` | `evaluating` |
| `_preflight_lines` | `feedback` |
| `_attempt`, at the `repairing` transition | `correcting` |
| `_create_entry` | `submitting` |
| `sweep_stuck` | `sweeping` |
| `_idle_judge` | `judging` |
| `_critique_one` | `critiquing` |
| `_critique_one`, after `spawn_fn` returns a job id | `spawning` |
| `_nap` / the end of `_idle_round` with nothing to do | `idle` |

Two details that are easy to get wrong:

- **The nap is a step.** The `idle` row opens when the worker goes to sleep and closes when the next
  round begins, so an open row exists for as long as the worker is alive. Its detail carries the
  sleep length, which is where "next wake in 3 min 40 s" comes from.
- **The judge names its pair only after it has judged it.** `judge.run_local` picks the pair
  internally and reports it through the `log` callback as `judged 1 vs 2: brief=A look=B`, which
  `_idle_say` already intercepts. When it does, call `db.update_step(...)` with the pair in the
  detail. During one `judging` row the judge may do several pairs; the detail says what it last did,
  and that is correct.

`stub_*` executors and judges in tests must keep working unchanged.

### 2.4 The words

The card is only as good as its vocabulary, so the strings are fixed here rather than left to the
builder. Substitute the real ids, models and counts; keep the shape.

| step | headline | detail |
| --- | --- | --- |
| `claiming` | Picking up the next job | job 301 · queued 14 min ago · by profcarroll |
| `planning` | Turning the prompt into a brief | gemma4:e4b · job 301 |
| `writing` | Writing the sketch | qwen3-coder:30b · job 301, attempt 2 of 3 · correcting from the last evaluation |
| `evaluating` | Evaluating the sketch in a browser | chromium · job 301, attempt 2 · console, motion, frame budget, sound |
| `feedback` | Working out what went wrong | job 301, attempt 2 · reading the sketch beside the evaluation |
| `correcting` | Correcting the sketch from the feedback | job 301 · nothing moved after the first frame · attempt 3 of 3 next |
| `submitting` | Submitting the sketch for review | entry 271, waiting for a person · job 301 |
| `sweeping` | Re-queuing jobs nobody came back for | 2 jobs left running by a worker that stopped |
| `judging` | Comparing two sketches | gemma4:e4b · entry 231 against entry 88 · closer to the brief, rather look at |
| `critiquing` | Critiquing entry 214 | gemma4:e4b · one sentence that becomes a child prompt · generation 3 |
| `spawning` | Starting a child sketch | job 302 from entry 214 · generation 4 |
| `idle` | Nothing to do | queue empty · nothing to judge, nothing to critique · next wake in 3 min 40 s |

"the gate" keeps its name everywhere it already has one — the `gate/` directory, `gate_exit`,
`docs/OPERATIONS.md`, the Held card's "gate passed on attempt 2". Only the worker's own step
vocabulary is in plain language, and only on this card.

### 2.5 `console.py`

```python
def activity(conn) -> dict[str, Any]
```

The one reader, called both by `collect()` (as `doc["activity"]`) and by the operator UI's
two-second poll, so the Console document and the poll can never disagree. Shape:

```json
{
  "step": "writing",
  "headline": "Writing the sketch",
  "detail": "qwen3-coder:30b · job 301, attempt 2 of 3 · correcting from the last evaluation",
  "job_id": 301, "entry_id": null, "model": "qwen3-coder:30b", "pid": 4412,
  "started_utc": "2026-09-15T14:02:11Z",
  "elapsed_s": 41.2,
  "median_s": 67.0,
  "live": true,
  "state": "running",
  "recent": [
    {"headline": "the evaluation refused attempt 1 — nothing moved after the first frame",
     "seconds": 3.1},
    {"headline": "turned the prompt into a brief · gemma4:e4b", "seconds": 9.4},
    {"headline": "claimed job 301 from the queue", "seconds": 0.2}
  ]
}
```

- `median_s` is the median of `ended_utc - started_utc` over the newest 50 closed rows **of the same
  step**, through `statistics.median`. Fewer than five samples is `null`, and a null median draws no
  bar. No per-step constants anywhere.
- `live` is `Path(f"/proc/{pid}").exists()` — one stat call, because the collector is on a 200 ms
  budget and `_find_processes` already walks `/proc` once for the slot.
- `state` is the card's state, decided here rather than in the template, and it is exactly one of:
  `paused` (control says so — `control.reason` becomes the detail), `gone` (`live` false),
  `stalled` (`live` true and `elapsed_s` over 40 min, longer than the executor's own 1800 s
  timeout), `running` (a job step), `idle` (`judging`, `critiquing`, `sweeping`, `idle`), `unknown`
  (no rows at all).
- `recent` is `db.recent_activity(conn, 3)` with seconds computed, newest first.

`render_text` (the `sketchgen console --text` view) grows two lines: the headline and the detail.
`sketchgen db status` prints the current step too, since that is the one view available over SSH
without the tunnel.

### 2.6 `web.py` and the templates

- `/api/control.json` gains `"activity": console.activity(conn)`. It is three cheap queries and one
  stat; do **not** call `console.collect()` from this route.
- One renderer, `activity_card(doc, *, compact=False)`, used by `console_page` and `queue_page`.
  Every value carries a `data-act="<key>"` attribute, the way console fields carry `data-k`.
- The layout's existing two-second `tick()` (bottom of `op_layout.html`) paints
  `[data-act]` from `d.activity`. **Build the trail rows with `document.createElement` and
  `textContent`, never `innerHTML`** — a detail line can contain a sentence a model wrote.
  Server-side, everything goes through `esc()` for the same reason.
- Markup, following the mockup:

```html
<section class="panel status">
  <div class="head"><h2>Worker</h2><span class="pill ok" data-act="pill">running</span></div>
  <p class="now" data-act="headline">Writing the sketch</p>
  <p class="who" data-act="detail">…</p>
  <div class="prog"><div class="bar"><span></span></div><span class="t" data-act="elapsed">…</span></div>
  <div class="trail"><ol data-act="recent">…</ol></div>
  <p class="foot" data-act="foot">up 4 h 12 min · slot ours · 6 jobs this session</p>
</section>
```

- The bar is `min(100%, elapsed / median)` and turns amber (`var(--warn)`) past the median rather
  than growing past its track, so a stuck step looks stuck. No median, no bar.
- The pill reuses the existing `.pill` classes: green for a running job step, the dim/`idle`
  treatment for idle work, red for `paused` and `gone`.
- `gone` reads *"Worker not running — the last step was evaluating job 301, 14 min ago"* with
  `systemctl --user status sketchgen-worker` under it. `stalled` keeps the step but takes the amber
  pill and adds "longer than any step has taken; check the transcript".
- The Queue's copy is the compact one: pill, headline, detail and a `Console ↗` link on one line,
  in a `.panel` above the jobs table. No second panel on that page.
- The three Worker tiles come out of `op_console.html`. `control`, `up since` and `slot` survive as
  the card's foot line, so nothing is lost.

### 2.7 Acceptance

- `test_db.py`: `begin_step` closes the previous open row for that pid; `current_activity` returns
  the open one; pruning keeps `ACTIVITY_KEEP`; a database without the table answers `None`.
- `test_worker.py`: a stubbed job records `claiming, planning, writing, evaluating, submitting` in
  order; a failing gate records `feedback` and `correcting` and a second `writing`; an idle round
  records `judging`, `critiquing` and `idle`; a `db.begin_step` that raises `sqlite3.Error` does not
  fail the job.
- `test_console.py`: `activity()` reports `live` false for a pid that is gone; a median from five
  samples and `null` from four; `state` is `paused` when control is paused even with an open row.
- `test_web.py`: the Console and Queue pages carry the card; `/api/control.json` carries the
  `activity` block; no rows renders the `unknown` card rather than raising.
- `grep -c "Thread" sketchgen/worker.py` is unchanged by this packet.

## 3. Packet 6: the decision card

Owner: one agent. Branch `packet-6-decision-card`. Touches `web.py`, `templates/op_layout.html`
(CSS only), and `tests/test_web.py`. **No database change, no new route, no state-machine change.**

### 3.1 What comes off the card

Entry 271 today says its own number six times, the word "entry" eight, and draws the same strip
twice. Gone: "made by job 301" as its own line, the entry id inside every button label, the
`critique_by` input, the "Spawn a child of entry 271 from a critique" label, the two help strings
under the buttons, and the duplicate `_entry_image` under the poster.

### 3.2 The card

```html
<section class="panel card" id="entry-271">
  <div class="id">
    <span class="n">271</span>
    <span class="pill held">held</span>
    <span class="dim">generation 4</span>
  </div>
  …preview_frame: the poster with its run button…
  <p class="prompt">a cityscape with a sunrise to sunset animation</p>
  <p class="rev"><span class="k">revise:</span> try a colder palette after dusk…</p>
  <p class="meta">gate passed on attempt 2 · treatment · qwen3-coder:30b ·
    job <a href="/job/301">301</a> · from entry 214, critiqued by gemma4 ·
    <a href="/preview/301/2/" target="_blank" rel="noopener">open in a tab ↗</a></p>
  <form method="post" action="/held/271/publish" class="say">
    <input type="hidden" name="back" value="/held">
    <input type="text" name="text" id="say-271"
           placeholder="why you are rejecting, or one sentence for a child">
    <div class="acts">
      <button type="submit" class="pub" aria-label="Publish entry 271">+ Publish</button>
      <button type="submit" class="rej" formaction="/held/271/reject"
              aria-label="Reject entry 271">× Reject</button>
      <button type="submit" class="cri" formaction="/entry/271/spawn"
              aria-label="Spawn a child of entry 271">› Critique</button>
      <button type="submit" class="arc" formaction="/held/271/archive"
              aria-label="Archive entry 271">− Archive</button>
    </div>
    <p class="hint">Reject and Critique read the box. Archive takes it off this page;
      nothing is deleted.</p>
  </form>
</section>
```

- **One form, four `formaction`s.** Plain HTML5, no JavaScript, the routes are the ones that already
  exist. The visible labels lose the id; `aria-label` keeps it, so a screen reader still hears which
  entry the button acts on.
- The input is named `text`. `reject_entry` and `spawn_child` read `text` **falling back to their
  existing names** (`reason`, `critique`), so anything already posting to those routes — including
  the current tests — keeps working.
- The prompt is split with `lineage.split_prompt`: the root sentence is `.prompt`, the newest
  revision is `.rev` under a mono `revise:` label, in the same form the gallery entry page uses. No
  revisions, no second line.
- `.meta` is one line, wrapping: the gate summary, the rules file, the executor, the job link, the
  lineage note, the open-in-a-tab link. It keeps `_gate_summary` and `_lineage_note` as they are.
- The strip appears once, as the run button's poster. `_entry_image` stays in the module as the
  **fallback** for an entry with no runnable attempt (and for `entry_page`), not as a second image.
- Kept rejections: the same card without Publish's meaning changed and without Reject —
  `+ Publish` (onto the rejections page), `› Critique`, `− Archive`. The hint says where publishing
  puts it.
- `entry_page` (read-only, `/entry/<id>`) uses the same header, poster, prompt and meta, and no
  form. It keeps its "where it is" line.

### 3.3 CSS

New rules in `op_layout.html`'s stylesheet, beside the existing `.cards` block, using the tokens
already defined there: `.card .id`, `.card .prompt`, `.card .rev`, `.card .rev .k`, `.card .meta`,
`.card .say`, `.acts` and the four button colours (`--ok`, `--bad`, `--accent`, `--dim`). The Held
grid stays `minmax(300px, 1fr)`; the mockup is drawn at that width and fits.

### 3.4 Acceptance

- `test_web.py`: a held card contains exactly one `data-preview` and no second strip `img`; the four
  buttons carry the right `formaction`; the entry id appears once as visible text and otherwise only
  in `aria-label`s and hrefs.
- `test_web.py`: reject reads `text`; reject still reads a legacy `reason`; spawn reads `text` and
  attributes the critique to the operator with no `critique_by` field posted.
- `test_web.py`: a kept-failure card has no Reject button; an archived entry still renders at
  `/entry/<id>`.
- Existing `test_web.py` cases for publish, reject, archive and spawn pass unchanged.

## 4. Running them

Both packets branch from `main` and touch disjoint regions of `web.py` (packet 5:
`console_page`, `queue_page`, `/api/control.json`, the layout's script; packet 6: `_decision_card`,
`held_page`, `entry_page`, the layout's stylesheet). They can be built in parallel and merged in
either order; whichever lands second rebases on `main` and re-runs `pytest tests/`.

Neither is deployed by its PR. The node updates with `update.sh`, which applies migration 008 and
restarts the units.
