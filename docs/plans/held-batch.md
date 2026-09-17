# Held, as a batch

The Held page stops being one request per decision. The four verbs on each card become
**toggles that mark**, and one **Process** button in the page header runs every mark as a single
batch with a progress bar. The operator curates the whole pile, presses once, and the page is
locked until the transaction has finished.

Mockup (the visual spec; interactive, the run is simulated):
https://claude.ai/artifact/AYg8YM7DAt2hNAqp51DAh5

Why: a publish is a render, two commits and two pushes, and the page waits on all of it before it
redirects. With thirty entries waiting that is thirty waits, and a second press during any of them
is a second publisher queuing on the checkout lock behind the first. A batch removes the waiting
*and* the second press: nothing on the page makes a request except Process, and Process is disabled
while a batch runs.

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at
  `~/sketchgen/app`. Python 3.12, stdlib only, `python3 -m unittest discover -s tests` (no pytest).
  Templates are `string.Template` files in `sketchgen/templates/`. The operator UI is one
  `ThreadingHTTPServer` on 127.0.0.1:8081 (`sketchgen/web.py`).
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. **No packet here touches the
  generator** (`gallery.py`, `assets/`, `templates/entry.html` …). What a published page looks
  like does not change; only how many commits and pushes it takes to get there.

Conventions: small PRs, one packet per branch, commit messages in the repo's existing voice (see
`git log`). Every packet lands with tests in `tests/`. No migration is needed by any packet here;
if a builder finds they want one, stop and say so instead. Do not touch `prompts/` or
`tests/test_executor.py`.

Deploy note for whoever lands these: `update.sh` restarts `sketchgen-web`. A batch in flight when
the web process restarts is abandoned where it stands (§1.9) — deploy with the tray empty.

## 1. Decisions already made

1. **Verbs mark, Process acts.** `+ Publish`, `× Reject`, `› Critique`, `− Archive` keep their
   shape, colour, order and labels. Pressed, a verb fills with its own colour (`--ok`, `--bad`,
   `--accent`, `--dim`; label in `--panel`) and nothing else happens. No redesign of the card.
2. **One outcome, plus an optional critique.** Publish, Reject and Archive are mutually exclusive
   on a card: pressing another moves the fill, pressing the filled one clears it. Critique is
   independent and stacks with Publish or Archive, or stands alone (a child is queued and the entry
   stays held). **Critique cannot join Reject**: both read the card's one text box, and one
   sentence cannot be a reason and a revision at once. Marking Reject clears Critique and the
   other way round. A kept rejection still has no Reject.
3. **The box is read at Process time**, by whichever marked verb reads it: the reason for Reject
   (empty is allowed and becomes `rejected by operator`, as today), the revision sentence for
   Critique (empty is **not** allowed). A marked Critique with an empty box shows on the card and
   in the tally as *needs a sentence*, and Process stays disabled until it is typed or unmarked.
4. **The tray is the second row of the sticky header**, on `/held` only: the page title and
   counts, a tally of marks by verb, `Clear marks`, and `Process N`. The layout's first row
   (nav, worker pill, Pause / Stop now / Resume) is untouched.
5. **One batch at a time, per web process.** While one runs, Process reads `Processing…` and is
   disabled, every toggle and box on the page is disabled, and the single-entry POST routes refuse
   (§3.5). This is the fix for the double-press bug: the state that says "busy" lives on the
   server, not in the page that was open when the press happened.
6. **Order inside a batch** is fixed and is about what is legal, not about the order of marking:
   1. **Critiques** — `spawn_child` while the parent is still `held` (a line may not grow from a
      `rejected` parent, and an archived one is off the lists).
   2. **Archives** — a state flip, no git.
   3. **Rejections, then publications** — each rendered, scanned and committed to the gallery
      checkout, one commit per entry, message unchanged.
   4. **One push** for all of those commits.
   5. **One index** re-render, commit and push.
7. **One push, one index.** Eight publishes are 16 pushes today and 2 in a batch. The invariant
   from `publish.py`'s first paragraph holds exactly: a row becomes `published` only after the
   push that carried its commit has succeeded. If the one push fails, the checkout goes back to
   where the batch found it and every publication in the batch is reported failed and is still
   held. Per-entry refusals (the personal-data scan, the generator, "already in the gallery with
   these exact bytes") drop **that entry** before its commit and the batch goes on.
8. **Progress is counted in real steps, not time.** Steps = one per critique, one per archive, one
   per entry committed, one for the push, one for the index. The tray shows the now line (one
   present-tense sentence), the bar, `i of n steps · elapsed`, and the five phases with counts.
   There is no median to measure against and the bar does not pretend to one.
9. **The batch lives in memory**, on `App`, behind a lock, run by one `threading.Thread` of the web
   process. (The operator-cards rule "no thread" is about the *worker*; the web process is already
   threaded.) No table, no migration. If the web process dies mid-batch, every step is atomic on
   its own — a finished step is finished, an unfinished publish left no row changed — and
   `publish.py`'s existing clean-tree check and `_undo` are what make the next run safe. What is
   lost is only the report.
10. **When it ends**, the result stays in the tray until dismissed or until the next batch starts:
    `6 done · 1 refused in 48s`, refused rows first with their reasons. Done cards are gone on the
    reload. Refused and failed cards are still there, **still marked**, with the reason on the
    card, so fix-and-retry is one press.
11. **No script, no loss.** The toggles are real form controls in one `<form>`, and Process is its
    submit button. Without JavaScript the POST starts the batch and redirects to `/held`, which
    renders the tray's progress server-side and refreshes itself while the batch runs. Script
    makes it live (polling), enforces exclusivity on press, and remembers marks across reloads.
12. **No confirm dialog.** The tally beside the button says exactly what the press will do, and
    the press is the person's decision spec §9 asks for.

## 2. Packet 10: `publish_many`

Owner: one agent. Branch `packet-10-publish-many`. Touches `sketchgen/publish.py`,
`tests/test_publish.py`. Nothing in `web.py`.

### 2.1 The function

```python
@dataclass
class ManyResult:
    published: list[Published]            # in the order they were committed
    refused: dict[int, str]               # entry_id -> why; dropped before its commit
    failed: dict[int, str]                # entry_id -> why; the push failed under them
    index_commit: str | None = None
    index_note: str | None = None

def publish_many(
    conn, entry_ids, *,
    gallery_dir=DEFAULT_GALLERY_DIR, key=DEFAULT_KEY_PATH, remote=None,
    by=None, model=None, url_base=DEFAULT_URL_BASE,
    on_step=None,       # Callable[[str, int | None, str], None]
) -> ManyResult:
```

Behaviour, in order, all inside **one** `_checkout_lock(gallery_dir)`:

1. `gallery_checkout()` once. A refusal here (not a repo, dirty tree, wrong branch, no remote)
   raises `PublishRefused` as it does today — nothing has happened yet, the caller reports it
   against every entry.
2. `before = HEAD`. For each entry id, in the order given: state check against `PUBLISHABLE`,
   render with the generator into a temp dir, `scan_for_personal_data`, copy to `e/<id>/`,
   `git add -- e/<id>`, commit with `_message(...)` — the same commit a single publish makes.
   Record the sha per entry. A `PublishRefused` for one entry goes into `refused`, and the
   checkout must be left exactly at the previous commit (remove the copied `e/<id>/` and unstage
   it) so the next entry starts clean. Call `on_step("commit", entry_id, sentence)` before each.
3. If no entry was committed, return — no push, no index.
4. `on_step("push", None, …)`; one `git push target HEAD:refs/heads/<branch>`. On failure:
   `_undo(checkout, before)`, every committed entry moves to `failed` with git's stderr, return.
   **No row has been touched.**
5. Stamp every committed entry in one transaction: `state` (`published` only from `held`, as
   today), `published_utc`, `publish_commit` = that entry's own sha; its job follows when it is
   still `held`. Commit the transaction.
6. `on_step("index", None, …)`; re-render **every entry of this batch** into the checkout (the
   "same generator, later truth" pass `_publish_index` does for one), then `render_index`, one
   commit `gallery index after entries 431, 432, 435`, the existing leftover second pass, one
   push. As today, a failure here is a note, never an exception: the entries are already public.

### 2.2 How to get there

Refactor, do not fork. Pull the body of `publish()` between the lock and the push into a helper
(`_stage_and_commit(conn, entry, checkout, …) -> (sha, files)`), the row update into `_stamp(…)`,
and let `_publish_index` take a list of entry ids. `publish()` is then a batch of one and **its
behaviour, its exceptions, its return type and every existing test in `tests/test_publish.py`
stay exactly as they are**, `--from` and `dry_run` included (`publish_many` takes neither).

`LOCK_TIMEOUT` stays 300 s. A batch of forty holds the lock for longer than a single publish
ever did, and a worker or CLI publisher that waits past the timeout already reports a clear
refusal; say so in the comment above the constant rather than raising it.

### 2.3 Tests

In `tests/test_publish.py`, against the bare-remote fixture the file already builds:

- three held entries → three entry commits, **one** index commit, the remote has all four,
  exactly two pushes happened (count them by wrapping `_git`), all three rows `published` with
  three different `publish_commit`s.
- a middle entry that trips the personal-data scan → in `refused`, still `held`, not in the
  remote; the other two published; the tree is clean afterwards.
- a push that fails (remote pointed at nowhere) → all in `failed`, no row changed, `HEAD` is
  `before`, tree clean.
- a `rejected` and a `failed-kept` entry in the batch keep their states and gain the stamps.
- `on_step` sees `commit` × n, `push`, `index`, in that order.
- an empty list returns an empty `ManyResult` and takes no lock.

## 3. Packet 11: the batch runner and its routes

Owner: one agent. Branch `packet-11-held-batch-runner`. Touches `sketchgen/web.py` (a new section
after "The held page", the `ROUTES` table, new handlers, guards in three existing handlers) and
`tests/test_web.py`. **Does not touch** `_decision_card`, `held_page`, or any template — those are
packet 12's. May be built in parallel with packet 10 (§3.3).

### 3.1 The request

`POST /held/batch`, `application/x-www-form-urlencoded`, the fields packet 12's form will send:

| field | value | meaning |
| --- | --- | --- |
| `do-<entry_id>` | `publish` \| `reject` \| `archive` | the one outcome; absent = none |
| `cri-<entry_id>` | `on` | queue a child from the box |
| `text-<entry_id>` | string, ≤ 400 | the card's one box |

Validation, before anything runs — any failure is a 303 to `/held` with a refusal flash (or 400
JSON, §3.4) and **nothing starts**:

- nothing marked → `nothing is marked — nothing changed`
- `cri` with an empty box → `entry 441: a critique needs a sentence — nothing changed`
- `cri` together with `do=reject` → refused, naming the entry
- `do=reject` on an entry that is not `held` → refused (a kept rejection has no Reject)
- unknown entry id, or a `do` value outside the three → refused
- a batch already running → `a batch is already running — nothing changed` (409 for JSON)

An entry whose state changed between the page load and the press is not a validation failure: it
is that item's own refusal at run time, reported on that item.

### 3.2 The runner

```python
@dataclass
class BatchItem:
    entry_id: int
    verb: str                 # "critique" | "archive" | "reject" | "publish"
    text: str = ""
    state: str = "queued"     # queued | working | done | refused | failed
    message: str = ""

@dataclass
class Batch:
    id: str                   # utc_now() at start
    items: list[BatchItem]    # already in run order (§1.6)
    state: str = "running"    # running | done
    phase: str = ""           # critique | archive | commit | push | index
    now: str = ""             # the sentence
    step: int = 0
    steps: int = 0
    started_utc: str = ""
    ended_utc: str | None = None
    index_note: str | None = None
```

`App` gains `batch: Batch | None = None` and `batch_lock: threading.Lock`. Starting a batch takes
the lock, refuses when `app.batch` is running, replaces a finished one, releases, then starts one
daemon `threading.Thread`. The thread opens **its own** connection (`app.connect()`), never the
request's. A card marked Publish **and** Critique is two items. Every mutation of the batch
happens under the lock; readers take a snapshot under it.

The thread, in order: critiques via `spawn_child` (build the `form` dict it expects:
`{"text": [sentence]}`), archives via `archive_entry`, then rejections' state flip — the first
half of `reject_entry`, split out as `_reject_state(conn, entry_id, reason) -> str | None` so the
single route and the batch share it — then one `publish.publish_many(...)` for the rejected and
the to-be-published ids together, rejections first. `on_step` moves `phase`, `now`, `step` and the
item's `state` to `working`; an item that has been committed but not pushed goes back to `queued`
with `committed, waiting for the push`, and everything in `ManyResult.published` turns `done`
after the push. Map the existing message conventions: a function that returns a string starting
`refused`, `there is no`, or containing `nothing changed` is a `refused` item (use `is_refusal()`,
which already decides this for the flash).

The thread never raises out: any exception becomes `failed` on the items still `queued` or
`working`, with `str(exc)`, and the batch still ends `done` with `ended_utc` set.

The now-line sentences are fixed here so the three packets agree:

| phase | sentence |
| --- | --- |
| critique | `Entry 435 — queuing a child from the critique` |
| archive | `Entry 438 — archiving` |
| commit | `Entry 431 — rendering, scanning, committing e/431/` |
| push | `Pushing 4 commits to the gallery` |
| index | `Re-rendering the index and pushing it` |
| (done) | `6 done · 1 refused in 48s` (`failed` counted with its own word when present) |

### 3.3 Without packet 10

If `publish.publish_many` does not exist, the runner loops `publish_entry(app, conn, id)` per item
and reports each as its own `commit` step, with no separate `push`/`index` steps. That is today's
cost with the batch's interface, and it lets this packet land first. Test both paths (patch
`publish_many` in and out).

### 3.4 The document

`GET /api/batch.json` → `{"batch": null}` or:

```json
{ "batch": {
    "id": "2026-09-17T14:12:03Z", "state": "running",
    "phase": "commit", "now": "Entry 431 — rendering, scanning, committing e/431/",
    "step": 3, "steps": 9, "bar_pct": 33.3, "elapsed_s": 14,
    "phases": [ {"key": "critique", "label": "Critiques", "done": 1, "of": 1},
                {"key": "archive",  "label": "Archives", "done": 2, "of": 2},
                {"key": "commit",   "label": "Render & commit", "done": 0, "of": 4},
                {"key": "push",     "label": "Push",  "done": 0, "of": 1},
                {"key": "index",    "label": "Index", "done": 0, "of": 1} ],
    "items": [ {"entry_id": 435, "verb": "critique", "state": "done",
                "message": "Queued as #612 — generation 4 of entry 435"} ],
    "summary": null } }
```

Phases with `of: 0` are omitted. `summary` is the done sentence once `state` is `done`.
`POST /held/batch` answers 303 → `/held` for a form post, and `202` with this same document when
the request carries `Accept: application/json`. `POST /held/batch/dismiss` clears a **finished**
batch (refuses a running one) and redirects to `/held`.

### 3.5 The guards

While a batch is running, `post_publish`, `post_reject` and `post_archive` do nothing and redirect
with `a batch is running — nothing changed; it will finish first`. `post_spawn` is **not**
guarded: it is also the job page's form, it touches no git, and SQLite serialises the write.

### 3.6 Tests

`tests/test_web.py`, with `publish_many` patched to a fake that calls `on_step` and returns a
`ManyResult` (no git in this file's fixtures): every validation refusal in §3.1 leaves the
database untouched and `app.batch` unset; run order is §1.6 whatever order the fields arrive in;
a Publish + Critique card spawns while the parent is still `held`; a second POST during a run is
refused and the first finishes; the three guarded routes refuse during a run and work after it;
an exception inside the thread ends the batch `done` with the items `failed`; the JSON matches
§3.4 field for field; dismiss refuses a running batch. Use a `threading.Event` inside the fake to
hold the batch open — no sleeps.

## 4. Packet 12: the page

Owner: one agent. Branch `packet-12-held-batch-page`. **After packet 11 has merged.** Touches
`_decision_card`, `held_page`, `page_held` and `layout()` in `web.py`, `templates/op_held.html`,
`templates/op_layout.html` (CSS and one new slot), and `tests/test_web.py`. The mockup is the
visual spec; its CSS uses the layout's own tokens and can be lifted nearly verbatim (`.tray`,
`.tally`, `.tog`, `.phases`, `.results`, `button.process`, the card state classes).

### 4.1 The card

`_card_face` is unchanged. Under it, the per-card `<form>` goes away and the controls join one
page-wide form by attribute (`form="held-batch"`), so the cards stay siblings in the grid:

```html
<input type="text" form="held-batch" name="text-431" id="say-431" maxlength="400" …>
<div class="acts" data-entry="431">
  <label class="tog pub"><input type="radio" form="held-batch" name="do-431" value="publish"
         aria-label="Mark entry 431 to publish"><span>+ Publish</span></label>
  <label class="tog rej"><input type="radio" … value="reject" …><span>× Reject</span></label>
  <label class="tog cri"><input type="checkbox" form="held-batch" name="cri-431"
         aria-label="Mark entry 431 for a critique child"><span>› Critique</span></label>
  <label class="tog arc"><input type="radio" … value="archive" …><span>− Archive</span></label>
</div>
<p class="hint" data-hint="431">…</p>
```

Radios give one-outcome-per-card with no script; the script adds press-again-to-clear and the
Reject/Critique exclusion (§1.2). The input is visually hidden but focusable, and
`input:focus-visible + span` draws the focus ring. The fill is `input:checked + span`. Keep the
`aria-label`s naming the entry. The hint says what Process will do to this card in one sentence
(the mockup's `hintFor` is the wording); server-rendered it is the static line
`Mark one outcome, and Critique if it should have a child. Nothing happens until Process.`

The single-entry routes stay in `ROUTES` — scripts, the CLI-minded and `/entry/<id>` habits may
still post to them — but no button on `/held` points at them any more. Update
`test_the_held_page_offers_kept_rejections_for_publishing` and the `aria-label` assertions to the
new markup rather than deleting what they check: a kept card has no Reject toggle; a published
kept entry has no card.

### 4.2 The tray

`layout()` gains `subheader: str = ""`, rendered inside a wrapper that makes both header rows
sticky together (move `position: sticky` from `header.top` to the wrapper). `/held` passes the
tray; every other page passes nothing and looks as it does today.

```html
<section class="tray" aria-label="Batch"><form id="held-batch" method="post" action="/held/batch">
  <h1>Held <span class="dim">— 8 waiting · 2 kept</span></h1>
  <div class="tally" id="tally" aria-live="polite"></div>
  <button type="reset" id="clear">Clear marks</button>
  <button type="submit" class="process" id="process">Process</button>
  … progress row, phases, results: rendered by the server from app.batch when there is one …
</form></section>
```

`op_held.html` loses its first `<h1>` (the tray is the title now) and keeps the kept-rejections
heading and paragraph.

Server-rendered states, all from `app.batch`:

- **no batch** — tally empty, Process enabled (the server refuses an empty press; the script
  disables it until something is marked).
- **running** — progress row, phases, Process disabled reading `Processing…`, every card control
  `disabled`, each batch item's card wearing its state pill (`queued` quiet, `working` warn,
  `done` ok, `refused`/`failed` bad) beside the state pill in `.id`, and the tray carries
  `<noscript><meta http-equiv="refresh" content="2"></noscript>` so a scriptless page follows
  along while a scripted one polls (below) and is never reloaded under the operator.
- **done** — summary line, refused/failed rows first then done rows, a `Dismiss` button
  (`formaction="/held/batch/dismiss"`). Cards of refused/failed items are rendered **pre-marked**
  (their `do`/`cri` checked, the box refilled from the item's `text`) with the reason in the hint
  in `--bad`.

### 4.3 The script

A `HELD_SCRIPT` constant beside `PREVIEW_SCRIPT`, passed with it as `page_script`. Same house
rules as the layout's script: ES5-plain, `textContent` only (a refusal message can quote a
prompt), no library.

- Press handling: clear-on-second-press for radios (track the checked value on `pointerdown` /
  `keydown`, uncheck on `click` when unchanged); Reject clears Critique and the reverse.
- Tally and the Process label (`Process 6`) recomputed on every change and on `input` in a box;
  `needs a sentence` chip and the disabled Process with its `title` per §1.3; `.card.marked`.
- Marks and box text saved to `sessionStorage` under `held-marks` on every change and restored on
  load for entries still on the page; cleared for entries a finished batch reports `done`.
  Wrapped in try/catch — the page works without it.
- Submit: `fetch` the POST with `Accept: application/json`; on 202 switch to the running state
  in place, then poll `GET /api/batch.json` every 1000 ms; paint the now line, bar (`bar_pct`),
  `step of steps · elapsed`, phases and per-card pills from the document. On `state: "done"`,
  reload `/held` once — the server renders the result and the surviving cards. On a 4xx, show the
  `error` in the tray as a refusal and unlock.
- On load, if the server-rendered tray says a batch is running, start polling immediately. This
  is what makes a reload or a second tab safe.
- `prefers-reduced-motion`: the bar's width transition is off.

### 4.4 Tests

`tests/test_web.py`: `/held` renders one `form#held-batch` and no per-card `<form>`; each held
card has three radios and a checkbox bound to it, each kept card two radios and a checkbox; no
`formaction` remains on the page; with a running batch (held open by an Event) the page has
`Processing…`, `disabled` on card inputs, the item pills, and the `<noscript>` refresh; with a finished batch holding one refusal the refused card is pre-marked with its text
restored and the summary is in the tray; every other page renders with no `.tray`; the header is
still sticky (the wrapper class is present on all pages). If `tests/js` already runs page script
under node (see `tests/test_gallery_js.py`), cover the exclusivity and tally logic there the same
way; if that harness is gallery-only, leave the script to the mockup and say so in the PR.

## 5. Order and hand-off

```
packet 10 (publish.py) ─┐
                        ├─→ packet 12 (the page)
packet 11 (web.py)  ────┘        needs 11 merged; works without 10
```

10 and 11 touch different files and go out together. 12 starts when 11 is merged and does not
wait for 10: with 11's fallback (§3.3) the page is complete and merely slower.

After all three, by hand on the node: mark two held entries Publish, one Archive, one
Critique-only; Process; confirm two entry commits, one index commit and two pushes in the
gallery's `git log`, the child in the Queue, and a second browser tab showing `Processing…`
for the duration. Then update `docs/OPERATIONS.md`'s Held section and the module docstring at
the top of `web.py` (lines 16, 53–77 describe the one-press card) — that edit rides with
packet 12.

## 6. As built: what packet 12 should know

Packets 10 and 11 were built on 2026-09-17 (`packet-10-publish-many`,
`packet-11-held-batch-runner`). Where they differ from the text above, the code is right:

- `Batch.index_note` is **not** in `/api/batch.json`. The done-state tray is rendered
  server-side, so read it from `app.batch` there.
- `Batch` also carries `phase_of` and `phase_done` (the §3.4 phase counts), and `step` is the
  count of *finished* steps. Use `batch_document(app)` rather than reading the fields raw.
- The runner writes the now-line sentences itself from `(phase, entry_id)`; the sentence
  `publish_many` passes to `on_step` is ignored.
- Exceptions out of `publish_many` are classified explicitly (`PublishRefused` → `refused`,
  anything else → `failed`); `is_refusal()` is only used on the functions that answer in sentences.
- Box text over 400 characters is truncated, not refused.
- A guarded single-entry route redirects to `/held` whatever `back` said.
- In `publish_many`, a `git add`/`commit` failure on one entry puts that entry in `failed` and the
  batch goes on; job rows follow their entries just after the stamping transaction, not inside it
  (`db.transition` opens its own).
- Helpers packet 12 may want: `batch_running(app)`, `batch_document(app)`, `batch_summary`,
  `BATCH_PHASES`, `BATCH_GUARD`.

## 7. Not in this plan

- Select-all, shift-click ranges, keyboard verbs (`p` / `r` / `a`). Worth having once the batch
  exists; the form-field contract above already carries them.
- Batching on `/submissions` (Release / Decline). Same shape, separate plan.
- Persisting batch history. The activity table is the worker's; the gallery's `git log` is the
  record of what was published and when.
