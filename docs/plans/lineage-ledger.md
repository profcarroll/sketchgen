# Lineage ledger

Entry page lineage panel, shorter prompt titles, and rejected sketches that stay in the record.

Mockup (the visual spec, option **B ledger** and **prompt 2** were chosen):
https://claude.ai/artifact/7qS4YhN7GTCmWxcjFZNSRC

Repositories:

- Pipeline: `profcarroll/sketchgen`, local clone `/home/dave/sketchgen`, on the node at `~/sketchgen/app`. Python 3.12, stdlib only, `pytest tests/`. Templates are `string.Template` files in `sketchgen/templates/`. Gallery CSS and JS ship from `sketchgen/assets/` (copied into the gallery by `render_index`).
- Gallery: `profcarroll/sketchgen-gallery`, generated output only. Never hand-edit it; every commit there is made by the publisher on the node.

Conventions: small PRs, one packet per branch, commit messages in the repo's existing voice (see `git log`). Every packet lands with tests in `tests/`. Do not touch `prompts/` or `tests/test_executor.py`; another session owns those uncommitted changes on the node.

## 1. Decisions already made

1. The lineage panel is the **ledger**: one row per generation, frame on the left, the critique that produced it on the right, this entry highlighted, forks as nested rows, descendants closing the panel.
2. The entry title becomes the **root prompt**, with the **latest revision as a subtitle**. The full accumulated prompt stays in `meta.json` and the Provenance table, unchanged.
3. Operator-rejected sketches are **never deleted and never invisible**. Rejecting publishes the entry to the rejections catalog with the operator's reason. It stays reachable through lineage.
4. A new terminal state **`archived`** takes held and unpublished kept-failure entries off the operator's primary lists without deleting anything.
5. **Retention and recovery come first.** The database and the attempt archive exist only on the Oracle instance, which the free tier can reclaim; nothing new ships until a verified nightly copy leaves it.
6. A lineage panel must render correctly when a parent is **not public** (archived, or historical). Show the generation, say it is not published, keep counting.

## 2. Findings that shape the work

- **Parent links are missing on 74 of 101 public non-root entries** (as of 2026-09-15). `gallery._forest()` builds only over `PUBLIC_STATES`, and `render_entry(publishing=True)` runs while the entry is still `held`, so the entry's own parent lookup misses and `null` is frozen into `meta.json`. Children lists are frozen at publish time for the same reason (entry 80's meta says no children; it has none, but 176's says none and it has 230). The `lineage` table is complete: entry 230's ten ancestors are all published. The `lines/` pages read the database, which is why they are right.
- **Rejected entries' files all survive.** All 32 `rejected` entries still have `source_dir` and `strip_path` on the node. `reject_entry` in `web.py` is a state flip only.
- **Every fork so far is human.** 185 links, 180 distinct parents, 5 parents with two children, and each fork was an operator critique. All 14 children of rejected parents were spawned by a human critique; 11 are published. A rejected sketch already leads to productive lines.
- **12 public entries have a non-public parent** today (11 rejected, 1 unpublished kept failure). After the backfill in packet 2 that number goes to 1, and the panel still has to handle it.
- The prompt is always splittable: `lineage.REVISE_HEADING` is a fixed heading, and `compose_prompt` is the only writer. Each `Revise:` line is exactly one ancestor.
- Prompt length grows about 150 characters per generation; generation 10 titles are 1,700 characters.

## 3. Packet 0: retention and recovery

Owner: one agent, first. Nothing else should ship on a node that can vanish with the only copy of the database.

### 3.1 What is off the instance today, and what is not

Verified 2026-09-15. Off the instance: the pipeline code (pushed, plus a clone on the operator's machine), the gallery (pushed, 209 entries), human votes, likes and views (Cloudflare D1 behind the write path, whose source and schema are in `writepath/`), the systemd units (`systemd/`), the models (Ollama). The gate script on the node is byte-identical to the tracked copy in the operator's course repo, `sld-fall-2026/examples/week11-self-hosted-ai/sketch-gate/`.

Only on the instance: `sketchgen.db` (WAL mode, 6 MB: 262 entries of which 53 are not public, 355 attempts, 191 lineage rows, 158 critiques, 1,114 agent judgments that exist nowhere else), `jobs/` (301 MB, every attempt's code, gate report and capture), `gate/accept.sh` and seven gate fixtures, `critic/` and `dev-*` experiments, the deploy keys and the write-path token. `pairs.json` in the gallery carries fitted scores and eight pairs, not the judgments behind them.

### 3.2 The gate moves into the repo

- `gate/` at the repo root: `sketch_gate.py`, `accept.sh`, `fixtures/`, and a `README.md` saying what the gate checks and how `accept.sh` runs the fixtures. Copy from the node, not from the course repo, and confirm the checksum matches both.
- `systemd/sketchgen-worker.service` sets `SKETCHGEN_GATE=%h/sketchgen/app/gate/sketch_gate.py`. `update.sh` re-installs the unit files it already touches, so the path changes on the next update. Leave `~/sketchgen/gate` on the node as a symlink to the repo copy for one release, then remove it.
- Pin Playwright in the venv requirements at the version installed on the node (1.62.0) and record the Chromium install step in `docs/OPERATIONS.md`.
- `accept.sh` runs in CI if the repo has any; otherwise `tests/test_gate_fixtures.py` runs the gate against the fixtures when Playwright is importable and skips cleanly when it is not.

### 3.3 `sketchgen backup`

A subcommand in `sketchgen/cli/backup.py`:

- `sketchgen backup snapshot --to DIR` writes `DIR/<utc>/sketchgen.db` through `sqlite3.Connection.backup()` (the online backup API; a file copy of a WAL-mode database can be torn), then `DIR/<utc>/gate.tar.gz` and `DIR/<utc>/manifest.json` with sizes, SHA-256 of the database, the schema version, row counts per table, and the git commits of `app` and `gallery`. It never copies `~/.ssh` or `writepath.token`. It keeps the newest 14 snapshots in `DIR` and removes older ones.
- `jobs/` is not tarred; it is rsynced by the puller (3.4), because it only grows and a nightly 300 MB tarball is the wrong shape. The manifest records the count of job directories so a pull can be checked.
- `sketchgen backup verify PATH` opens a snapshot read-only, runs `PRAGMA integrity_check`, and compares row counts with the manifest.
- `systemd/sketchgen-backup.service` and `.timer`, daily at 04:10 UTC, `--to %h/sketchgen/backups`. `update.sh` installs and enables them like the others.

### 3.4 The copy that leaves Oracle

Default: a pull from the operator's machine over the SSH connection already in use, so no new credential lives on the node.

- `bin/pull-backup.sh HOST` in the repo: `rsync -a --delete-excluded` of `HOST:~/sketchgen/backups/` and `rsync -a` (no delete) of `HOST:~/sketchgen/jobs/`, into `~/sketchgen-backups/HOST/`, then runs `sketchgen backup verify` on the newest snapshot and prints one line: date, database size, job count, verified or not. Exit non-zero if the newest snapshot is older than 36 hours, so a silent stall is noticed.
- A user systemd timer on the operator's machine (Linux) runs it daily; the unit file ships in `systemd/operator/` with a comment on installing it with `systemctl --user`.
- Optional second copy: `sketchgen backup push --bucket NAME` to Oracle Object Storage through the `oci` CLI, for a copy that survives the operator's machine too. Spec it, implement it behind a flag, do not require it.

### 3.5 Recovery runbook, tested

`docs/OPERATIONS.md` gets a `Recovery` section: new instance, packages, clone `app`, create the venv, install Playwright and Chromium, pull models by name (`qwen3-coder:30b-a3b-q4_K_M`, `gemma4:e4b`), restore the newest `sketchgen.db` and `jobs/`, generate new deploy keys and add them to both repositories, new write-path token, install units, start the timers, `sketchgen db status`. Every step is a command.

Acceptance is a rehearsal: run the runbook on a throwaway instance from a real snapshot until `sketchgen db status` matches the manifest counts and the operator UI shows the held queue. Record the date and the elapsed time at the top of the section. Packet 0 is not done until that date is there.

### 3.6 Acceptance

- `test_backup.py`: snapshot of a WAL-mode fixture database verifies; retention keeps 14; the manifest counts match; secrets are absent from the snapshot directory.
- `test_gate_fixtures.py` passes where Playwright is present and skips where it is not.
- Worker unit points at the repo gate; a job on the node passes the gate after the switch.
- The rehearsal date is in `OPERATIONS.md`.

## 4. Packet 1: lineage data is always right

Owner: one agent. No UI. Everything else depends on the JSON shape here, so it is fixed in this document and other packets can build against it in parallel.

### 5.1 Fix the forest at publish time

In `sketchgen/gallery.py`, `_forest(conn, *, admit: int | None = None)` includes `admit` in `ids` when given. `render_entry(..., publishing=True)` passes the entry being published. `_meta` then records the real parent. Add a test in `tests/test_gallery.py`: publish a held child of a published parent, assert `meta["lineage"]["parent_entry_id"]` is the parent and the parent appears in `_forest` children.

### 5.2 Generate `lineage.json`

`render_index` writes `<gallery>/lineage.json` next to `pairs.json`. Client code paints the parts of the panel that change after an entry is published (siblings, children, state chips). Shape:

```json
{
  "generated_utc": "2026-09-15T14:00:00Z",
  "entries": {
    "82": {
      "state": "published",
      "public": true,
      "parent": 78,
      "children": [124],
      "generation": 6,
      "root": 11,
      "critique": "try again with a different color palette and mood",
      "critique_by": "profcarroll",
      "submitted_by": "profcarroll",
      "strip": "e/82/strip.png",
      "root_prompt": "a cityscape with a sunrise to sunset animation ..."
    },
    "255": {
      "state": "held",
      "public": false,
      "parent": 212,
      "children": [],
      "generation": 4,
      "root": 11,
      "critique": "...",
      "critique_by": "gemma4:e4b"
    }
  }
}
```

Rules: every entry in the `entries` table appears, whatever its state, so generation counts add up. Non-public entries carry no `strip`, no `root_prompt`, no `submitted_by`. `public` is true for states in `PUBLIC_STATES` with `published_utc` set. `parent` comes from `_parent_of` regardless of the parent's state (this is the difference from `_forest`, which is for the site's tree pages and stays as it is). `root_prompt` is the prompt split at the first `REVISE_HEADING`. Keep it under 100 KB at 300 entries; if `critique` pushes it over, truncate `root_prompt` to 200 characters.

### 5.3 Split the prompt

Add `lineage.split_prompt(prompt) -> tuple[str, list[str]]` returning the root sentence and the revisions in order, splitting on `REVISE_HEADING`. Round-trips with `compose_prompt`. Tests in `tests/test_lineage.py` including a root with no revisions and a prompt containing the heading text mid-sentence (split only on the heading as `compose_prompt` writes it, at a line start).

### 5.4 Re-render every entry once

`sketchgen gallery render-all` (the existing render-every-public-entry command in `sketchgen/cli/gallery.py`) is run once on the node after 4.1 lands, then the gallery is pushed. Confirm the command regenerates `meta.json` and the entry page, and does not change `published_utc` or `publish_commit`. Document the operator step in `docs/OPERATIONS.md`. Expect one large gallery commit touching every `e/*/meta.json`; that is intended.

### 5.5 Acceptance

- `_forest` test above passes; existing `test_gallery.py` and `test_publish.py` unchanged.
- `lineage.json` validates against the shape above in a test that builds a four-entry line with one held child.
- After `render-all` on the node, `e/230/meta.json` says parent 176, and `e/176/meta.json` says children [230].

## 5. Packet 2: rejected stays, held can be archived

Owner: one agent. Touches `db.py`, `web.py`, `gallery.py`, operator templates, `cli/gallery.py`.

### 6.1 State machine

In `db.py` transitions:

- `held -> {published, rejected, archived}`
- `failed-kept -> {archived}` only while `published_utc` is null (kept failures nobody put on the site)
- `archived` is terminal. `rejected` stays terminal.

`archived` is not in `PUBLIC_STATES`. The job row moves to a matching terminal state; reuse `rejected` on the job with `last_error = "archived by operator"` rather than adding a job state.

### 6.2 Rejected is public

- `PUBLIC_STATES = ("published", "failed-kept", "rejected")`.
- `_state_chip`: `failed-kept` reads `rejected · gate`, `rejected` reads `rejected · operator`. Use the labels `web.py` already has in `STATE_LABELS`.
- `reject_entry(conn, entry_id, reason)` becomes reject-and-publish: flip the state, store the reason (add `entries.reject_reason TEXT` in a new migration; do not overload `last_error`), then run the same render-and-push path `publish_entry` uses. A person pressed the button, which is what spec §9 asks for. If the push fails the state stays `rejected` with `published_utc` null and the console shows it as pending, exactly as a kept failure does today.
- `rejections.html` lists both kinds, newest first, each card carrying its chip and, for operator rejections, the reason in the existing `$reason` slot. Keep one page; do not add a route.
- The entry page for a rejected entry carries the chip in `stage-meta` and a one-line note under the stage: "Rejected by the operator: <reason>." Everything else renders as it does for a kept failure.
- Compare: `pairs.offer` already excludes `rejected` and `failed-kept`; leave that alone.

### 6.3 Archive from the operator UI

- `/held/<id>/archive` (POST, form with a `back` field like reject) for held entries.
- The kept-failures list on the console gets the same button for entries with `published_utc` null.
- Archived entries disappear from `/held` and the kept list. The console summary gets an `archived` count. No page lists archived entries; they are findable by id through `/entry/<id>` in the operator UI, which should render them read-only with the state.
- Files are untouched. Add a comment in `reject_entry` and the archive handler saying so, because the question "does rejecting delete the sketch" has already been asked once.

### 6.4 Backfill the 32 existing rejections

`sketchgen gallery publish-rejected [--all | <id>...] [--dry-run]` renders and pushes existing `rejected` entries whose `published_utc` is null, in id order, one commit per entry like the publisher. `reject_reason` for these is the job's `last_error` if it looks like an operator reason, otherwise "rejected by operator (reason not recorded)". Dry run prints the list. This is operator-run once; document in `OPERATIONS.md`.

### 6.5 Acceptance

- `test_db.py`: the new transitions, and that `published -> archived` is refused.
- `test_web.py`: reject stores the reason and calls the publish path; archive hides the entry from `/held`; archive on a published entry is refused with a flash, not a 500.
- `test_gallery.py`: a rejected entry renders with the operator chip and the reason; `rejections.html` shows both kinds.
- On the node after backfill: `rejections.html` lists 32 more entries, and entry 82's panel (packet 3) can show its rejected ancestors as frames instead of blanks.

## 6. Packet 3: the ledger panel

Owner: one agent, after packet 1 merges (or against the JSON shape in 4.2, with a fixture). Touches `templates/entry.html`, `gallery.py` (`_lineage_panel`), `assets/gallery.css`, `assets/gallery.js`. Build to the mockup's option B exactly; the mockup's CSS class names are suggestions, its layout is the spec.

### 7.1 What the server renders

At publish time the server knows the whole ancestry and it never changes, so the ancestors, the fold, and this entry's row are static HTML in the page. Siblings and descendants change later, so the server renders their container empty with the ids it knows, and `gallery.js` paints them from `lineage.json`. With JavaScript off the page still shows the ancestry and a text line "children: 124" as today.

Panel heading: `Lineage · generation 6 of 10 in the line from entry 11`. The "of 10" is the deepest generation in the line at render time; JS updates it from `lineage.json`.

### 7.2 Rows

Each row is a tile and a text block:

- Tile: a `button.play` wrapping `img` of `../<id>/strip.png`, sized to `aspect-ratio: 64/45`, `object-fit: cover; object-position: left`, which shows the first of the four frames without a new file. Width 6.5rem in the ledger, 9.5rem in the descendants grid.
- Text: for the root, the root prompt in weight 600 and no `Revise:` label; for every other row, the mono `Revise:` label then the critique in italic, full length, never clamped. Under it: `entry N` linked, `generation N`, a critic chip.
- Critic chip: `critique_by` containing `:` is a model, chip shows the name before the colon (`gemma4`) in the agent colour; otherwise a human, chip shows the username in the ok colour. The root's chip is `submitted_by`. Non-public rows get the `not published` chip.
- This entry's row has the highlighted background and ring from the mockup and is not a link.

### 7.3 Folding

Always show the root, the grandparent, the parent, and this entry. Ancestors strictly between root and grandparent are folded into one row when there are two or more of them: `▸ 3 generations folded · 21, 27, 42 · all by gemma4` (or `by gemma4 and profcarroll`). The fold is a `details` element whose body holds the folded rows, so it opens without JS. One ancestor between root and grandparent renders inline, no fold.

### 7.4 Forks

A sibling (another child of this entry's parent) renders as a nested block under this entry's row: label `also from entry 78`, then sibling rows in the same tile-and-text form at 6.5rem width, sorted by id. Siblings with children add `· N children` after the generation. If this entry has more than four siblings, show four and a link to the line page for the rest.

Ancestor forks (an ancestor with another child) are not drawn in the ledger; the line page shows them. Add `· forked` after that ancestor's generation with a link to the line page.

### 7.5 Descendants

Below a rule: `After this entry: 1 child, 4 generations so far`, then a grid (`minmax(9.5rem, 1fr)`) of direct children first, then further descendants in generation order, up to eight tiles, then `…and N more in the line` linking to the line page. Each tile shows its critique clamped to three lines. No children: `No children yet.` as today.

### 7.6 Non-public generations

Where a parent or ancestor is not public, render the row with a blank tile (chip background, no image, no button), `entry N` unlinked, `not published` chip, and the critique that produced it if known, since the critique came from its public parent. Counting continues so generation numbers match `meta.json`.

### 7.7 Run in place

Lift `startSketch`, `stopSketch`, and `runNote` out of `wireCompare` into a shared `runInPlace(container, href)` in `gallery.js` with the same rules: one iframe at a time on the page, `sandbox="allow-scripts"`, the iframe is removed on stop so the sketch unloads, the button reads `stop` while running. The compare page keeps using it; the ledger binds it to every public tile. The frame replaces the image inside the tile at the tile's size, so nothing below it moves. The entry's own stage iframe is not part of this: running a ledger tile does not stop the stage, and a note under the panel says only one ledger tile runs at a time.

### 7.8 Mobile

The ledger is a two-column grid at every width; at 40rem and below the tile is 5.5rem and the text keeps the rest. The descendants grid falls to two columns. Nothing scrolls sideways.

### 7.9 Acceptance

- `test_gallery.py`: a fixture line of eleven generations with one fork renders the fold, the sibling block, the highlighted row, and the descendants grid; a line with a rejected ancestor renders the blank tile.
- The rendered panel for entry 82 on a local render matches the mockup's option B in structure.
- Run in place works on both compare and entry pages; only one frame runs; stopping removes the iframe (assert `document.querySelector("iframe.sketch")` count).

## 7. Packet 4: root title and revision subtitle

Owner: one agent, independent of the others once `split_prompt` (4.3) exists; may reimplement the split locally and switch when packet 1 merges. Touches `gallery.py` (`render_entry`, the card renderer around line 1361, `_line_node`), `templates/entry.html`, `templates/card.html`, `gallery.css`.

- `h1` is the root prompt. Under it, when there is at least one revision, `p.sub`: mono `Revise:`, the latest revision in italic, the critic chip, then dim `· generation 6 · 5 earlier revisions in the lineage below`. With no revisions, no subtitle.
- `<title>` is the root prompt truncated at 80 characters, as now.
- Cards on the index and rejections pages: the card prompt is the root prompt, with a second dim line `g6 · try again with a different color palette and mood` clamped to two lines. `data-search` keeps the full prompt so search still finds revisions.
- The Provenance table keeps the full prompt. The Brief panel is unchanged.
- Line pages: `_line_node` shows the critique already; change `node-prompt` to the root prompt only on the root card and drop it from the others, which repeat it.

Acceptance: `test_gallery.py` covers a root, a generation 1, and a generation 10 entry; the generation 10 title is one sentence.

## 8. Packet 5: attempts are entries too

Owner: one agent, after packet 2 (it extends the same operator handlers and migration style). Touches `db.py`, `worker.py`, `web.py`, `gallery.py`, `lineage.py`, `cli/lineage.py`, a migration `007_attempts.sql`.

### 9.1 What is there

- Every attempt survives on disk: `jobs/<job>/attempt-<n>/` holds `sketch.js`, `index.html`, `statement.md`, `result.json`, and `.gate/` with `report.json`, `gate.png`, `console.log`, and on all but the earliest jobs `strip.png`. Nothing purges them. Counted on 2026-09-15: 67 attempts under the 21 kept failures, 40 failed attempts under published entries, 5 under rejections.
- An entry is its job's last attempt. `gallery._source_dir` takes `attempts[-1]`; `worker._create_entry` copies the last attempt's statement and gate report into the entry row. `meta.json["gate"]` already logs every attempt's exit and assertions, so provenance for the other attempts exists; the code and frames do not reach the gallery.
- The operator job page already renders every attempt with a live preview, latest open and earlier ones folded (`preview_frame`, `_artefacts`).
- `lineage.spawn` is by entry and inherits the prompt only. A child never inherits code, so "spawn from attempt 2" changes only which sketch the critic looked at. That is worth recording, not worth a different job.
- `max_attempts` is per job, default 3. `failed` is terminal in `db.TRANSITIONS`; a job that exhausts its attempts cannot get more.

### 9.2 Publish an attempt over the gate

`POST /job/<job>/attempt/<n>/publish` with `reason` (required, one line) and `back`. Allowed when the job's entry is `held` or an unpublished `failed-kept`; refused with a flash otherwise. Effect, in one transaction then the ordinary publish path:

- migration adds `entries.published_attempt INTEGER`, `entries.gate_override TEXT`, `entries.gate_override_by TEXT`.
- the entry row's `source_dir`, `strip_path`, `png_path`, `statement`, `attempts`, token and wall columns are set from attempt `n`; `published_attempt = n`; `gate_override = reason`; `gate_override_by` = the operator's username.
- state moves to `published` through `publish_entry`. Add `failed-kept -> published` to the transitions for this path only, guarded by `gate_override` being set.
- `gallery._source_dir` and `_artefact` prefer `published_attempt` when set. `strip.png` missing on an old attempt falls back to `gate.png` cropped to one frame; the strip is four frames of the gate viewport and the tile is `object-fit: cover; object-position: left`, so the fallback reads the same.

Honesty on the page. Everywhere the state chip appears, an override reads `published · gate overridden`. The entry stage line becomes `attempt 2 of 3 · the gate refused it: console_clean, frame_advancing · published by profcarroll: <reason>`. `meta.json` gains `published_attempt`, `gate_override`, `gate_override_by`, all null when unset; the key list in `META_KEYS` and its test grow by three. Cards get a small `override` chip beside the state chip.

Measurement. Overrides enter the compare pool like any published entry; agents judge blind from the strip and never see the chip. They stay in the Bradley-Terry scores, flagged in `pairs.json` by `gate_override: true` so the divergence analysis can drop them. This is a default, not a finding; the operator can ask for them to be excluded from the scores later without touching the pages.

### 9.3 Spawn from any attempt

- `lineage` table gains `parent_attempt INTEGER` (same migration). `lineage.spawn` takes `parent_attempt: int | None = None` and stores it; `record_child` carries it through. `sketchgen lineage critique` and `spawn` take `--attempt N`; the critic reads that attempt's `statement.md` and strip instead of the entry's.
- The job page gets a spawn form under each attempt's preview, posting to `/entry/<id>/spawn` with a hidden `attempt` field. The held page keeps one form, for the counting attempt.
- `meta.json["lineage"]["parent_attempt"]` and the ledger row show `critiqued attempt 2` after the generation whenever `parent_attempt` differs from the parent's `published_attempt` or last attempt. `lineage.json` carries `parent_attempt`.

### 9.4 More attempts, and the gate again

Two operator actions for a job whose entry is an unpublished `failed-kept`:

- `POST /job/<job>/continue` with `more` (1 to 5): `max_attempts += more`, job `failed -> queued`, `resume_from` set to the last attempt's number so the worker starts the next attempt as a repair of it rather than from the plan. Add `failed -> queued` to the job transitions for this route only. When the job later reaches `held`, `worker._create_entry` must update the existing entry row (the `job_id` is unique) instead of inserting: add `failed-kept -> held` to entry transitions, and re-derive the row from the new last attempt.
- `POST /job/<job>/attempt/<n>/regate`: enqueue a gate-only run of that attempt. The worker performs it under the same slot fence (the gate is Chromium, not inference, but two gates at once skew timings). A new report replaces `.gate/report.json` with the old one kept as `report.<utc>.json`; the attempt row's `gate_exit` and `evidence` update. Exit 0 moves the entry to `held` pointing at that attempt, no override recorded, because the gate passed. Use this when the failure was a timeout or a flaky check; use 8.2 when the gate's verdict stands and the operator disagrees with it.

### 9.5 Every attempt reaches the gallery

For kept failures, overrides, and operator rejections, `_write_entry` copies each attempt's `sketch.js`, `index.html`, `statement.md`, and `.gate/strip.png` (or `gate.png`) into `e/<id>/attempts/attempt-<n>/`. Published passes copy only the attempt that passed, as now. The entry page gets an `Attempts` panel between Source and Lineage: one tile per attempt with its gate line from `meta["gate"]`, run in place through the packet 3 helper. `meta["source"]["attempts"]` becomes a list of objects `{name, gate_exit, sketch, strip}`. Sizes are small (a sketch is a few KB, a strip about 15 KB), so `render-all` after this lands is the right way to backfill the 21 kept failures.

### 9.6 Acceptance

- `test_db.py`: the three new transitions and their guards.
- `test_web.py`: override publish requires a reason and records the operator; refused on a published entry; continue increments `max_attempts` and requeues; regate enqueues without inference.
- `test_worker.py`: a resumed job starts at attempt `last + 1` as a repair; a gate-only task rewrites the report and moves the entry to `held` on exit 0; `_create_entry` updates rather than inserts when a row exists.
- `test_gallery.py`: an override renders the chip, the stage line and the three meta keys; a kept failure renders the Attempts panel with three tiles; `parent_attempt` shows in the ledger row.
- `test_lineage.py`: spawn with `--attempt` records it and the critic reads that attempt's statement.

## 9. Order and parallelism

1. Packet 0 first and alone: the gate move and the backup timer land before any other packet is deployed, and the recovery rehearsal is scheduled, not skipped.
2. Packet 1 next; it is small and everything reads its JSON.
3. Packets 2, 3, 4 in parallel on separate branches. Packet 3 uses a `lineage.json` fixture until packet 1 merges.
4. Packet 5 after packet 2 merges; it can start against packet 2's branch.
5. Deploy in order: packet 0 (units, timer, first snapshot pulled and verified); packet 1 code, `render-all` on the node, push; packet 2 code, migration, `publish-rejected --all`; packets 3 and 4; packet 5 and its migration; then a final `render-all` so every page carries the new panel, title, and attempts.

Deployment runs through `~/sketchgen/app/update.sh` on the node (restored from HEAD on 2026-09-15 after it had been deleted in the working tree).

## 10. Out of scope

- Redesigning the `lines/` pages (the mockup's filmstrip is a candidate for them later).
- Any change to judging, pairs, or scores.
- Deleting anything. Archived and rejected entries keep their files, and so does every attempt.
- Backing up the models, the venv, or the `dev-*` scratch directories; they are rebuilt or abandoned.
