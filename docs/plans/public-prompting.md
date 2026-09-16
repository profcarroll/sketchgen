# Public prompting and critique

Two new surfaces on the published gallery, both behind the GitHub session the write path already
issues: a **prompt field** at the top of the grid page, and a **critique form** on an entry page.
A signed-in visitor can ask for a sketch, or ask for a revision of one that exists. Nothing they
submit reaches a model until a person releases it.

Mockup (the visual spec; the copy in it is final):
https://claude.ai/artifact/XXj5VjVL7jnqfH85LLSjWH

Three packets, described here as one document because they share one wire format and are
meaningless apart. §2 fixes that format; §3–§5 are the three sides of it and can be built in
parallel.

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at
  `~/sketchgen/app`. Python 3.12, stdlib only, `python3 -m unittest discover -s tests` (there is no
  pytest here and the suite does not use it). Templates are `string.Template` files in
  `sketchgen/templates/`.
- Write path: `writepath/` **inside that same repository** — `worker.js`, `schema.sql`,
  `wrangler.toml`, `test/worker.test.js`. Plain Web APIs, no dependencies, `node --test writepath/test/`.
  It is deployed to Cloudflare by hand; a PR does not deploy it.
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. Packet 9 changes the
  **generator** (`sketchgen/gallery.py`, `sketchgen/templates/`, `sketchgen/assets/`); the gallery
  repository receives the result on the next re-render — `render-index` for everything that is not
  an entry directory, `render-all` when `entry.html` changed (§6). Do not hand-edit it.

Conventions: small PRs, one packet per branch, commit messages in the repo's existing voice (see
`git log`). Every packet lands with tests. No new dependency, in either language.

## 1. Decisions already made

1. **The Cloudflare Worker, not GitHub Issues.** The write path is already a GitHub OAuth app that
   hands the gallery a signed session; a second submission channel would be a second identity
   system for one action. Issues stay the fallback if the Worker's surface ever has to shrink.
2. **A submission is not a job.** Public text lands in its own `submissions` table and becomes a
   `jobs` row only when the operator releases it. `jobs.state`, its CHECK constraint and
   `db.TRANSITIONS` are **not touched** — an unreleased submission is not a job, so nothing in the
   pipeline can pick it up by accident, and there is no table rebuild in this work.
3. **A prompt and a critique are the same thing at that stage** — a sentence a stranger wants run —
   so they share the table, separated by `kind` and by whether `entry_id` is set. The operator
   reviews one list.
4. **Released critiques go into `critiques` as they do today**, with `critique_by` = the GitHub
   login and `prompt_version` = `human:<login>`. This is the whole of the collision fix: the
   UNIQUE `(entry_id, prompt_version)` then admits one critique per person per entry, which is the
   right rule for people, while the critic model's rows keep the rule that is right for a model.
   `db.entries_to_critique` matches on the model's current prompt version and so never sees a human
   row; `db.get_critique` is unchanged; `gallery._critic_chip` reads `critique_by`, not
   `prompt_version`, and already draws a person differently from a model. **No new critiques table,
   no migration for this.**
5. **The depth limit needs no change.** `lineage.spawn` at `generation >= DEFAULT_MAX_DEPTH` still
   creates the job, forces `publication='hold'` and sets `needs='review'`. That is exactly the
   policy a public submission gets anyway, so the released critique calls `spawn()` with its
   existing defaults and the generation count stays truthful.
6. **Public jobs are `rules_file='random'`** for a fresh prompt, so submissions cannot skew the
   treatment/control split (spec §9). A released **critique** keeps `spawn`'s existing default,
   which is the parent's rules file — a line must stay a fair comparison with itself.
7. **Quotas live in D1**, per GitHub login, per UTC day: **3 prompts, 5 critiques**. A vote is
   idempotent per person so abuse of it is self-limiting; a prompt is roughly 67 s of the node. The
   cap has to be able to refuse, which the console cannot do after the fact.
8. **The filter bar is hidden.** It is the only real estate the composer needs and nobody uses it.
9. **Blinding is untouched.** A critique is not the paired comparison and the critiquer seeing the
   entry is the point. `judge.assert_blind()`, `/counts` and packet 5.2's rule are not in scope.

Still the instructor's to overrule before work starts: the quota numbers in 7 and the
`human:<login>` convention in 4.

## 2. The wire format

Fixed here. All three packets code against this and nothing else.

```
POST /prompt
Authorization: Bearer <session>        (or the Worker's own cookie)
Content-Type: application/json
{ "prompt": "a tide of small triangles that drifts toward the cursor" }

200 { "ok": true, "id": 31, "created_utc": "2026-09-16T15:04:22Z", "prompts_left": 2 }
400 { "error": "the prompt is empty" | "the prompt holds code ('{')" | "…" }
401 { "error": "sign in to submit a prompt" }
429 { "error": "3 prompts a day", "prompts_left": 0 }
```

```
POST /critique
Authorization: Bearer <session>
{ "entry_id": 412, "critique": "let the lines thin as they near the edge" }

200 { "ok": true, "id": 77, "created_utc": "…", "critiques_left": 4 }
400 { "error": "the critique is 2 sentences; one is the contract" | … }
401, 429 as above
```

`GET /me` gains the budget, so the page can render it without a second call:

```
200 { "username": "profcarroll", "prompts_left": 2, "critiques_left": 5 }
```

`GET /pull?since=…` keeps its shape and gains one array. The node reads submissions; it never reads
quota state:

```json
{ "since": "…", "next_since": "…",
  "votes": [], "likes": [], "views": [],
  "submissions": [
    { "id": 31, "kind": "prompt", "username": "profcarroll", "entry_id": null,
      "text": "a tide of small triangles…", "created_utc": "…", "updated_utc": "…" },
    { "id": 77, "kind": "critique", "username": "profcarroll", "entry_id": 412,
      "text": "let the lines thin…", "created_utc": "…", "updated_utc": "…" }
  ] }
```

`updated_utc` exists so a submission takes part in the same inclusive watermark the other three
arrays use. Nothing ever updates a submission row in D1 — it is written once — but the puller must
not have to special-case it.

## 3. Packet 7: the write path

Owner: one agent. Branch `packet-7-write-path`. Touches `writepath/schema.sql`,
`writepath/worker.js`, `writepath/test/worker.test.js`, `writepath/README.md`. **Nothing else in
the repository.** No new Cloudflare secret, no new binding: the four secrets and the one D1
database are the ones that already exist.

### 3.1 Schema

Appended to `writepath/schema.sql`, which is `IF NOT EXISTS` throughout and safe to rerun:

```sql
-- One row per thing a signed-in visitor asked for, prompt or critique. Written
-- once and never updated; `updated_utc` mirrors `created_utc` so a submission
-- rides the same inclusive /pull watermark as a vote.
CREATE TABLE IF NOT EXISTS submissions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT    NOT NULL CHECK (kind IN ('prompt', 'critique')),
    username    TEXT    NOT NULL,           -- GitHub login, nothing else
    entry_id    INTEGER,                    -- the parent, for a critique; NULL for a prompt
    text        TEXT    NOT NULL,
    created_utc TEXT    NOT NULL,
    updated_utc TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS submissions_updated_idx ON submissions (updated_utc);
CREATE INDEX IF NOT EXISTS submissions_budget_idx  ON submissions (username, kind, created_utc);
```

The budget is counted off this table — `COUNT(*) WHERE username = ? AND kind = ? AND created_utc >= ?`
with the start of the UTC day — so there is no second table to keep in step and no counter to drift.

### 3.2 Routes

Two new entries in the `SQL` map at the top of `worker.js` (`insertSubmission`, `countToday`,
`pullSubmissions`), two route functions beside `routeVote`, and three lines in the dispatcher.
Both routes take a username from `sessionToken(request)` → `readSession(...)` exactly as `/vote`
does; neither accepts `PULL_TOKEN`.

- `routePrompt(request, env, username)`: read JSON, validate (§3.3), count the day's prompts,
  refuse with 429 past 3, insert, answer with the remaining budget.
- `routeCritique(request, env, username)`: the same, plus `isEntryId(body.entry_id)` — the Worker
  has no entries table and must not pretend to: an id that is a positive integer is as far as it
  can check, and the node refuses an unknown parent when it applies the row.
- The `/me` branch in the dispatcher gains both remaining counts. Signed out it is still 401.

### 3.3 Validation, in the Worker

A prompt: non-empty after collapsing whitespace, at most 240 characters, and free of the code marks
`lineage._CODE_MARKS` lists (``` ``` ``, `{`, `}`, `;`, `()`, `=>`, `function `, `<script`, `//`,
`$`). A critique: the same marks, **one sentence**, under 40 words — `lineage.validate`'s rules,
restated in JavaScript with the same error sentences. Port the rules; do not invent new wording.

This is a duplicate of the Python validator and that is deliberate: the page should refuse before
the round trip, the Worker must refuse whatever the page does, and `sync.py` refuses again on the
way in. Put the marks in one exported constant so the test can assert the two lists match by eye.

### 3.4 `/pull`

One more statement, one more array, the same `PULL_LIMIT`, and the watermark loop gains
`...submissions`. Nothing else in `routePull` changes.

### 3.5 Acceptance — `node --test writepath/test/`

- A prompt with no session is 401; with a session, 200 and a row.
- The fourth prompt in one UTC day is 429 and writes nothing; the sixth critique likewise; a prompt
  does not spend the critique budget.
- Two sentences, 40 words, and each code mark are each 400, with the sentence the error names.
- A critique without `entry_id` is 400; `entry_id` 0, -1 and `"12"` are 400.
- `/me` reports both remaining counts with a session and is still 401 without one.
- `/pull` returns submissions, honours `since` inclusively, and refuses a session token.
- `/prompt` with `PULL_TOKEN` as the bearer is 401.

## 4. Packet 8: the node

Owner: one agent. Branch `packet-8-submissions`. Touches `migrations/010_submissions.sql`,
`sketchgen/db.py`, `sketchgen/sync.py`, `sketchgen/web.py`, `sketchgen/console.py`,
`sketchgen/templates/op_*.html`, `sketchgen/cli/`, and tests. **Does not touch `gallery.py`,
`assets/`, `worker.py` or `lineage.py`.**

### 4.1 Migration 010

```sql
-- 010_submissions.sql — what the public asked for, before a person released it.
--
-- A submission is not a job. It is a sentence a signed-in visitor typed on the
-- gallery, pulled down from the write path, and it becomes a job only when the
-- operator releases it. Keeping it out of `jobs` is what guarantees the worker
-- cannot claim unreviewed text: claim_next reads `jobs` and this is not it.
--
-- `state` is this table's own and has nothing to do with jobs.state:
--   pending   — waiting for a person
--   released  — became job_id
--   declined  — a person said no; decline_reason says why, and the row stays
--
-- `remote_id` is the write path's own id. It is UNIQUE because /pull is
-- at-least-once: the same row arrives again on the boundary second and the
-- upsert must recognise it rather than queue the prompt twice.

CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY,
    remote_id      INTEGER NOT NULL UNIQUE,
    kind           TEXT NOT NULL CHECK (kind IN ('prompt', 'critique')),
    username       TEXT NOT NULL,            -- GitHub login, nothing else
    entry_id       INTEGER REFERENCES entries(id),
    text           TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'pending'
                       CHECK (state IN ('pending', 'released', 'declined')),
    job_id         INTEGER REFERENCES jobs(id),
    decline_reason TEXT,
    created_utc    TEXT NOT NULL,            -- when the visitor submitted
    pulled_utc     TEXT NOT NULL,            -- when this node first saw it
    decided_utc    TEXT
);

CREATE INDEX IF NOT EXISTS submissions_state_idx ON submissions (state, created_utc, id);
```

House rules as the other migrations keep them: `IF NOT EXISTS` throughout, a header comment saying
what the table is for, applied by `db.migrate()` inside one transaction. `update.sh` runs it on the
node.

### 4.2 `db.py`

```python
def add_submission(conn, *, remote_id, kind, username, entry_id, text, created_utc) -> int | None
def pending_submissions(conn, limit: int = 50) -> list[sqlite3.Row]
def submission(conn, submission_id: int) -> sqlite3.Row | None
def release_submission(conn, submission_id: int, job_id: int) -> None
def decline_submission(conn, submission_id: int, reason: str) -> None
def submission_counts(conn) -> dict[str, int]      # for the console's tile
```

`add_submission` is an upsert on `remote_id` that **does nothing** if the row is already there, and
returns `None` in that case: at-least-once delivery means it will be called twice with the same
row, and the second call must not queue a second job or resurrect a declined one.

### 4.3 `sync.py`

One more key in the payload, one more apply function, everything else as it is: the watermark, the
inclusive window, the refusal to write a row whose login fails `LOGIN_RE`, the skipped counter.
Two extra refusals, both counted as skipped rather than raised:

- `kind` not in `{'prompt', 'critique'}`, or `text` empty after collapsing.
- `kind == 'critique'` and `entry_id` names no row in `entries` — the Worker cannot check this and
  the node can. A submission naming an entry this node has never published is dropped, not held.

`run_once`'s summary line gains `submissions=` beside the existing counts.

### 4.4 The operator's review

A **Submissions** page at `/submissions`, built from the decision card packet 6 already
established — the same `.card` markup, one form, `formaction` per verb, no JavaScript:

- The sentence, large. The username as a `person` chip. For a critique, the parent entry's strip
  and its prompt above the sentence, so the operator sees what is being critiqued.
- One text input, shared: the decline reason, or nothing.
- Two buttons. **Release** → for a `prompt`, `db.enqueue(conn, text, submitted_by=username,
  rules_file='random', publication='hold')`; for a `critique`, `lineage.spawn(conn,
  parent_entry_id=entry_id, critique=text, critique_by=username, submitted_by=username)` with every
  other default untouched, then `db.record_critique(...)` with `prompt_version=f"human:{username}"`
  and the returned job id. Either way `release_submission` records the job. **Decline** → the
  reason, and the row stays as the record.
- `spawn()` returning `None` (the parent was rejected between submission and review) declines the
  row with that as the reason rather than raising.
- The Console grows one tile, `N waiting`, linking here. It is the only new thing on that page.
- The nav gains one link.

`sketchgen submissions` in `cli/` lists pending rows and releases or declines one by id, so the
review is possible over SSH without the tunnel.

### 4.5 Acceptance

- `test_db.py`: `add_submission` twice with one `remote_id` writes one row and returns `None` the
  second time; releasing records the job id and the timestamp; declining keeps the text; a declined
  row is not returned by `pending_submissions`.
- `test_sync.py`: a payload with submissions writes them; the same payload applied twice is a no-op;
  a bad login, a bad kind, an empty text and an unknown `entry_id` are each skipped and counted; the
  watermark advances past a submission-only payload.
- `test_web.py`: the page lists pending rows oldest first; Release on a prompt creates a `queued`
  job with `rules_file='random'`, `publication='hold'` and `submitted_by` set to the login; Release
  on a critique creates the child and writes a `critiques` row whose `prompt_version` is
  `human:<login>`; two different people releasing critiques of one entry both succeed; Decline
  writes the reason and creates no job; a released row cannot be released twice.
- `test_console.py`: the waiting count is right and is zero on an empty table.
- `grep -n "TRANSITIONS" sketchgen/db.py` shows no change to the job state machine.

## 5. Packet 9: the gallery

Owner: one agent. Branch `packet-9-gallery-forms`. Touches `sketchgen/templates/grid.html`,
`sketchgen/templates/entry.html`, `sketchgen/assets/gallery.js`, `sketchgen/assets/gallery.css`,
`sketchgen/gallery.py`, `tests/test_gallery.py`, `tests/test_gallery_js.py`. **No database, no
Python outside `gallery.py`.**

The mockup is the spec for layout and for every string. Both forms follow the existing
`data-login` / `paintSession` pattern exactly: hidden until `/me` answers, no credential in the
page, `base()` empty means the whole block does not render.

### 5.1 The composer, `grid.html`

Above the `.sorts` row, in the space the filters give up, and **only on the gallery index** — the
rejections page and the line pages do not take submissions. `gallery.py` passes a `composer` slot
that is the empty string for every page but that one.

Signed out: one line, *"Sign in with GitHub to submit a prompt. Only your GitHub username is shared
and published."* Signed in: the textarea, `Queue it`, the budget from `/me`, and the note *"Held for
review before it runs"*. After a 200: the receipt, *"Queued for review."* and *"After review, check
back later to see if your sketch was successfully created."* After a 429: the budget line says
`0 of 3 left today` and the button is disabled.

### 5.2 The critique form, `entry.html`

It **replaces** the panel that currently reads *"A static page cannot start a job, and this one
holds no credential that could. Ask the operator."* — heading, paragraph and all.

The rules line under the textarea runs `lineage.validate`'s rules in JavaScript and updates as the
visitor types; the mockup's script is the reference implementation, including the sentence each
failure prints. Under it, the composed child prompt: the parent's prompt, then `Revise:` and the
sentence in italic — the same shape `_subtitle` already renders on a child entry. On a 200 the
panel becomes the receipt.

An entry whose own page is a rejection gets no form: `spawn()` refuses a rejected parent, so
offering the box would be a lie.

### 5.3 The filter bar

Delete the `<details class="filters">` block from `grid.html`. Nothing else:

- `gallery.py:_filters()` and the `filters=` argument **stay**, unused, so this reverts in one
  commit.
- `?rules=control` and `?executor=…` keep filtering the grid — that work is `applyVisibility()`,
  which reads the URL, not the chips.
- `gallery.js` needs no change: `querySelectorAll("a.filter")` over nothing is a no-op and the
  disclosure lookup is already guarded (`if (block && (rules || executor))`, gallery.js:343).
- The `.filters` CSS rules stay too. Dead for now, and the block that brings them back is four
  lines.

### 5.4 Acceptance

- `test_gallery.py`: the index carries the composer and the rejections and line pages do not; no
  page contains `details class="filters"`; a config with an empty `write_path` renders neither form;
  every string in both forms matches the mockup; a rejected entry's page has no critique form.
- `test_gallery.py`: `guard()` still refuses to publish — the forms add no absolute path, no token
  and no local URL.
- `test_gallery_js.py`: the validator's rules and messages match `lineage.validate` case for case.
- Existing filter tests are updated to assert the **URL** still filters, not that the chips exist.

## 6. Running them

Packets 7, 8 and 9 branch from `main` and touch disjoint files; §2 is the contract between them, so
they can be built in parallel and merged in any order.

Deployment is not a PR and is the instructor's, in this order. Step 1 is a **gate**, not the first
item on a list: do not run step 2 until its verification has printed `submissions`.

1. The new table:

       cd writepath && wrangler d1 execute sketchgen-writepath --remote --file=./schema.sql

   `--file` uploads through the D1 `/import` endpoint, which fails with
   `Authentication error [code: 10000]` on a `wrangler login` OAuth token even when that token
   has `d1 (write)`. The query endpoint is unaffected, so the fallback is the same DDL inline via
   `--command`. `schema.sql` is `IF NOT EXISTS` throughout and safe to rerun either way.

2. **Verify, before deploying anything:**

       wrangler d1 execute sketchgen-writepath --remote --command \
           "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"

   `submissions` must be in that list. If it is not, stop here.

3. `wrangler deploy` — the two routes. No new secret, no new binding.

4. `update.sh` on the node — migration 010, the restarted units, and a `render-index`. **Pull the
   gallery checkout first if any PR was merged from outside the node**, and remember that a deploy
   which pulls a new `update.sh` runs the old copy.

5. The entry pages, which step 4 does **not** touch — see below:

       ~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen render-all \
           --gallery-dir ~/sketchgen/gallery --db ~/sketchgen/sketchgen.db
       git -C ~/sketchgen/gallery add -A
       git -C ~/sketchgen/gallery commit -m "re-render: the critique form reaches every entry page"
       GIT_SSH_COMMAND='ssh -i ~/.ssh/sketchgen-gallery' git -C ~/sketchgen/gallery push

`update.sh` runs `render-index`, which is *everything that is not an entry directory*: the grid,
the rejections page, compare, the line pages, the assets and `config.json`. Packet 9 changes
`entry.html` as well as `grid.html`, and **no amount of `update.sh` will re-render an entry page**.
`render-all` is the command that does, and it writes files only — the commit and the push are by
hand. `sketchgen publish` is not this: it publishes one held entry by id.

### Why step 1 is a gate

An earlier draft of this section claimed that nothing between the first and last step is broken for
a visitor, on the reasoning that the forms do not exist until the end and the routes answer before
anything calls them. **That reasoning is wrong, and it cost an outage on 2026-09-16.**

Two of the routes this work touches were already being called before any form existed:

- `/pull` is called by the node every five minutes, and `routePull` reads `submissions`
  unconditionally. Deploying over a missing table stopped the sync dead — and took votes, likes and
  views down with it, none of which are part of this feature.
- `/me` is called by the gallery on every page load, and it now counts the day's budget. A failed
  `COUNT` answered 500, and `gallery.js` reads any non-200 from `/me` as signed out, so every
  signed-in visitor was logged out of a site whose sign-in was working. It presents as a broken
  OAuth flow and is nothing of the kind.

Deploying code before its schema does not stage a new feature behind a door nobody has opened yet.
It breaks the routes that were already working. The gate is the fix; `/me` degrading rather than
failing is the belt to its braces.

## 7. What this does not do

- It does not publish anything a person has not released. A submission is text in a table until the
  operator acts, and the job it becomes is `publication='hold'` like every other.
- It does not let the public publish, reject, archive or judge. The console keeps all four.
- It does not learn anything new about a person. A GitHub login, as before: no email, no display
  name, no avatar, no address, no header that identifies a client. A released submission publishes
  that login on the entry, which is what the sign-in line now says out loud.
- It does not moderate text. A declined submission stays in the table as the record of what was
  asked; there is no block list and no report button, and if one is ever needed it is its own packet.
- It does not touch blinding, `/counts`, the judge, the gate or the A/B rig beyond decision 6.
