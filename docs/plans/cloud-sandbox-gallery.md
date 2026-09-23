# The cloud sandbox gallery

A whole sketchgen node, stood up inside one Claude Code cloud session, making one hundred
sketches with paid models answering every model step. A person curates the results from a phone,
and the kept ones go out to a public gallery on GitHub Pages. Nothing touches `sld-cloud`, the
D12 node or `profcarroll/sketchgen-gallery`.

It is not a new pipeline. The node, the worker, the gate, the repair loop, the held page, the
publisher and the renderer all run unchanged. What this plan adds is three bridges across the one
wall a cloud sandbox has: **a person cannot reach a port inside it.** The review page crosses it
outward (§3), the decision block crosses it back (§4), and the gallery repo carries the result
out for good (§5).

The rule the whole plan is built around, from the operator (2026-09-23): *it can't be "burn
through all my tokens and never show me what you made." It has to be Show Your Work, even when
the work is trapped in a sandbox.* So every batch ends in something a person can open on an
iPhone, and **the next batch does not start until that person has decided on this one** (§6).

Four packets, one branch each, one PR each (§8). The code lives in `sandbox/` (new) and
`tests/test_sandbox.py` (new). **No change to `sketchgen/`, `gate/`, `prompts/`, `migrations/`
or `writepath/`.** The gate stays hash-pinned, and the verdicts it gives in the sandbox are the
verdicts it gives on the node.

Read `AGENTS.md` → *Driving a job with a paid model* first. This plan assumes that loop, and in
the sandbox it runs as `bin/sketchgen paid …` directly, where the node would use `bin/sg` over ssh.
Read `docs/plans/held-batch.md` for the batch form that §4 replays.

## 0. What the repository says today

Every piece the sandbox needs already exists, and the first thing the session does is to find
out whether the sandbox lets each piece run.

| need | what is there | where |
| --- | --- | --- |
| a node from nothing | stdlib Python and one pin, Playwright 1.62.0, plus its Chromium | `requirements.txt`; `db init` builds schema 17 from `migrations/` |
| model steps with no Ollama | the paid loop: `start` → `next` → `try` → `import` | `sketchgen/paid.py`, `sketchgen/cli/paid.py`, `AGENTS.md` |
| a worker the preflight accepts | the preflight reads `/proc`, not systemd, so a backgrounded `worker` passes | `paid.py:1571` `preflight`, `worker.py:504` `worker_processes` |
| the gate | headless Chromium, which loads p5 1.11.3 **from cdnjs on every run** | `executor.py:102`, `gate/sketch_gate.py` §"loads(image)" |
| human curation | `/held`: publish, reject, archive and critique, one entry at a time or in a batch | `web.py:6592–6598`; `batch_plan` at `web.py:4839` |
| archive | **no CLI verb**; only the web route and `db.archive_entry` | `web.py:4706`, `db.py:741` |
| critique → child | `lineage spawn --parent --critique --critique-by`, and `POST /entry/<id>/spawn` | `cli/lineage.py`, `web.py:7260` |
| publishing | an **https remote pushes with no deploy key**, using ambient git credentials | `publish.py:464` `_is_ssh_remote`, `:470` `_push_env` |
| a gallery at any URL | `SKETCHGEN_GALLERY`, `SKETCHGEN_GALLERY_URL` | `publish.py:86–92` |
| foreign files in the gallery | the renderer deletes only `e/<id>/` and files it wrote itself | `publish.py:569`, `gallery.py` `_Written` |
| the preset prompts | 50 roots and 50 critique children at or above the median judgment percentile, harvested from the public gallery on 2026-09-22 | `~/sketchgen/harvest-2026-09-22.json` on D12; **not in the repo** |
| what an answer cost | `rig/cost.py --since --reply` finds the API's own counts for the reply | `rig/cost.py` |

Two rows are gaps. Archive has no verb, and AGENTS.md rule 4 forbids working around that with
`sqlite3`. §4 goes through the web route, which is the verb a person's click runs. The harvest
file lives outside the repo, so packet 0 commits it (§8).

## 1. Decisions

**DECIDE[sandbox-models] — which paid models, over which prompts.** Open; the operator decides at
the start of the session. The options:

- (a) one model, all 100 prompts. The simplest run, and a clean pass rate for that model.
- (b) three models, 33 or 34 different prompts each. Covers the most prompts, but makes no
  comparison between models.
- (c) **three models on the same 33 prompts, 99 sketches. Recommended.** Every prompt has three
  entries side by side, the gallery's `?executor=` filter separates them, and the operator's
  publish/reject rate per model becomes the human verdict on what each one is worth.

Default roster for (c): `claude-opus-5-5`, `claude-sonnet-5`, `claude-fable-5-1`. The 33 prompts
are the first 17 roots and the first 16 children in harvest order. Every item in that file is
already at or above the median, so taking them in order is not cherry-picking.

**DECIDE[review-visibility] — who can see a held sketch before the operator has decided on it.**
Open. On the node a held entry is private by design (`gallery.py`, the states that get a
directory). A review page on public Pages publishes it before any decision.

- (a) public, unlinked, `noindex`, under `review/` in the gallery repo. Free, and it works today.
  Anyone who guesses the URL sees the drafts, and the drafts include rejects.
- (b) a private review repo with Pages. Needs a paid GitHub plan, and a second repo the session
  must be able to push to (§2, check 3).
- (c) no page: the review cards are shown as images in the session chat. Private, but the live
  sketches can't be touched, and touching them is half the point of the ghost pointer.

Recommended: (a), with the review page deleted from `main` once its batch is decided. The history
keeps it. Say that plainly on the page itself.

**DECIDE[rules] — which rules file.** `treatment` for every job, recorded as usual. The A/B is not
what this run measures, and a paid executor is already a second variable in it (`AGENTS.md`,
*Other steps*). Keep this run as its own batch, and say so on the gallery's front page.

**DECIDE[judging] — no agent judgments.** A model judging its own entries is blind to the model id
(`judge.py:assert_blind`) but it is not an independent population, and the gallery's measurement
is between two populations. The operator is the only judge in this run. `paid assign --step judge`
and `--step critique` go to a registered paid id so the idle loop parks those steps rather than
calling an Ollama that is not there, and nothing ever answers the parked steps.

## 2. The sandbox and the smoke test

The session starts on a clone of `profcarroll/sketchgen` with the gallery repo `profcarroll/
sketchgen-sandbox` attached. The operator creates that repo beforehand: public, empty, with Pages
set to serve `main` from `/`. The environment needs **full network access**, or a custom allowlist
covering at least `pypi.org`, `files.pythonhosted.org`, Playwright's browser CDN,
`cdnjs.cloudflare.com` and `github.com`. `loads(image)` prompts fetch from arbitrary hosts, and on
a narrow allowlist those prompts fail as honest missing-picture verdicts, not as broken code.

`sandbox/bootstrap.sh` (packet 1) does this, in this order, and stops at the first failure:

```
python3 -m venv ~/sketchgen/.venv
~/sketchgen/.venv/bin/pip install -r requirements.txt
~/sketchgen/.venv/bin/playwright install --with-deps chromium
sketchgen db init  --db ~/sketchgen/sketchgen.db
bash gate/accept.sh                    # the eleven fixtures, the gate's own proof
git clone <gallery https url> ~/sketchgen/gallery
sketchgen web --port 8081 &            # the real operator UI, inside the sandbox
sketchgen worker &                     # exactly one, and never a second (AGENTS.md rule 1)
sketchgen paid models add <id> …       # every id in DECIDE[sandbox-models]
sketchgen paid assign --step judge    --model <first id>
sketchgen paid assign --step critique --model <first id>
```

with `SKETCHGEN_DB`, `SKETCHGEN_JOBS`, `SKETCHGEN_GALLERY` and
`SKETCHGEN_GALLERY_URL=https://profcarroll.github.io/sketchgen-sandbox` exported.

Then a **smoke test of one sketch**. It answers the three questions nobody can answer from
outside a session, and the run does not start until all three come back yes:

1. **Network.** `gate/accept.sh` passes, and the one sketch's gate report shows p5 arrived.
2. **Publishing.** The operator publishes that one sketch from the review page (§3, §4), and it
   shows up at the Pages URL. Cloud sessions may limit pushes to the session's own working branch.
   If they do, publish.py pushes to `origin/HEAD`, which is `main`, and is refused. Fallback: set
   the gallery repo's default branch and Pages source to the session branch, or turn each batch's
   push into a PR that the operator merges. The merge then is the publish click.
3. **A second repo**, only if DECIDE[review-visibility] chose (b).

The smoke test also times one sketch end to end and measures its tokens with `rig/cost.py`. That
number, times the batch size, is the first real forecast of spend, and it is printed before
batch 1 starts (§6).

## 3. The review page (outward)

`sandbox/review.py` (packet 2) renders the entries that are currently held (and failed-kept) into
`review/batch-NN/index.html` in the gallery checkout, commits and pushes it. It reads the database
read-only and copies the files it needs out of `jobs/`. The sandbox's `/held` page is the source of
truth, but the review page is **not** a scrape of it: `/held` is a desktop operator page that
posts forms to 127.0.0.1, and a copy of it would be neither usable on a phone nor able to post.

**Phone first, one card per screen**, scrolled vertically, the way `swipe.html` already works. It
reuses swipe's CSS where it can. Each card:

- the **live sketch** in a sandboxed iframe, with the ghost shim on, so a sketch built for touch
  plays itself, and a thumb stops it (`ghostshim.py`);
- `strip.png` and `ghost.png`, the four frames the gate saw idle and under the pointer;
- the prompt, the brief, the assertions and what the gate found on each — pass, off-plan, or
  failed-kept, with the check that failed;
- **provenance**: the model id that answered, attempts used, `try`s used, and the reported cost
  (`usage` and `process`, *as reported by the agent*, exactly as the entry page words it);
- the code, folded;
- four buttons, **Publish · Reject · Archive · Critique**, and one text box. Reject takes an
  optional reason; Critique needs a sentence (§4 has the rules).

Along the top: the batch number, the tally per model, the batch's total tokens and elapsed time,
and a **Copy decisions** button. Marks are kept in `localStorage`, inside try/catch, so a phone
that reloads keeps them and a private tab that refuses storage still works. The page links to the
public gallery and to every earlier review batch.

## 4. The decision block (inward)

**Copy decisions** puts plain text on the clipboard. The operator pastes it into the session chat
from the Claude app. The format is short enough to type by hand, so a person can decide without
the page:

```
sandbox-decisions batch=03
112 publish
113 reject static, and it ignores the brief
114 archive
115 publish
115 critique the same loom, but the threads fray where you touch them
```

`sandbox/replay.py` (packet 3) reads it and **replays it as one press of the real held page's
Process button**: a single `POST /held/batch` to the sandbox's own web UI, with `do-<id>`,
`cri-<id>` and `text-<id>` exactly as the page's form would send them (`web.py:4776`
`BATCH_FIELD_RE`). So `batch_plan` validates it all-or-nothing, and archive runs its own code
path with no verb added and no `sqlite3` involved. The replayer:

- refuses the whole block before it posts anything if it names an entry that isn't in this batch,
  names a verb that isn't one of the four, pairs `reject` with `critique` (a child can't be spawned
  from a rejected parent, `lineage spawn`), or gives two texts for one entry. The page's single box
  per card is the same limit;
- prints what the web UI answered, verbatim, and then the state of each entry read back from the
  database. A publish is done only when the row says `published` and the Pages URL answers 200.
  Nothing is claimed from the POST alone;
- records the block, as pasted, in `review/batch-NN/decisions.txt`, so the gallery repo shows who
  decided what.

**Critique is how the operator steers.** A critique spawns a child job, and the next batch
**starts with those children**. Claude drives each one with the model that made its parent, so a
line stays one model's. That makes a human-directed lineage: the operator writes the next prompt,
and the paid models have to answer it.

`Published-By:` in each gallery commit is whatever username the web UI stamps. Set it to the
operator's GitHub username in the session environment before batch 1.

## 5. The gallery (out for good)

`profcarroll/sketchgen-sandbox` at `https://profcarroll.github.io/sketchgen-sandbox/`: the same
renderer, grid, entry pages, compare, kiosk, swipe, lines and rejections page as the class
gallery. It is empty on day one and holds only what the operator chose. Every entry is badged
**off-node** and names the model that answered, its attempts and its reported cost. Rejects appear
on the rejections page with their reasons, because that is what reject means in this codebase
(`held → rejected` publishes the rejection; `archive` is the private way out).

After the last batch, one `publish-index` renders the front page, and the session writes
`REPORT.md` at the repo root (§7) and links it from the index's header via `config.json`, or from
the README if the renderer has no slot for it. **Its URL is what the operator gets at the end.**

State is saved so that a sandbox that dies loses nothing: after every batch, `sketchgen backup`
writes a verified snapshot of the database, which is committed together with a tarball of `jobs/`
to an orphan branch `state` in the gallery repo. A new session can rebuild the node from `state`
and carry on at the next batch.

## 6. Cadence and the spend ceiling

Ten batches of ten sketches (nine of eleven under DECIDE (c), grouped by prompt so one batch holds
all three models' takes). One batch is:

1. **Children first**, from the last batch's critiques, then fresh prompts until the batch is full.
2. **Drive each job.** Subagents run one sketch each, **three at a time**, each with
   `model` set to its arm and the AGENTS.md loop as its whole brief. Each subagent answers as its
   own exact model id, runs `rig/cost.py` before each import, and returns just the entry id,
   the verdict and the cost line. The main session keeps the conclusions, not the transcripts, so
   its own context holds up over ten batches. Leases are per job and the worker serves them in
   turn. The smoke test also tries two concurrent leases before three are trusted; if that fails,
   run one at a time.
3. **Render and push** the review page and the `state` snapshot.
4. **Report and stop.** One message: the review page URL, the tally per model (held · off-plan ·
   failed-kept), tokens and minutes for the batch and for the run so far, and the forecast for the
   rest. Then the session **waits for a decision block**. It doesn't start batch N+1 on its own,
   whatever the budget says.

The ceilings are the operator's to set at the start. Defaults: AGENTS.md's advisory 10 minutes and
30,000 generated tokens per sketch, and a **hard stop for the batch** once its token total passes
1.5× the smoke test's forecast. A stopped batch is still rendered and reported, and every job it
didn't reach is handed back with `paid release` and then cancelled. With no Ollama, a released job
would otherwise wait for local models forever.

## 7. The report

`sandbox/report.py` (packet 3) reads the database only and writes `REPORT.md`, with one table per
model and one row per batch:

- sketches attempted, first-attempt gate pass, attempts per kept entry, `try`s per job;
- held clean · held off-plan · failed-kept;
- **the operator's verdicts**: published · rejected · archived · critiqued, and the rate each
  model was published at, which is the number this run exists to produce;
- tokens (reply and process), minutes, and `ms_per_frame` median and worst;
- the deepest line a critique grew, and whose it was.

Every number carries its source query in a folded block under the table, the house habit of saying
where a figure came from. No blended score: models are compared on each measure separately, the
same way the gallery never collapses humans and agents into one number.

## 8. Packets

**Packet 0 — the prompts in the repo.** `sandbox/harvest-2026-09-22.json`, copied verbatim from
D12, with a README line saying where it came from and when. These are already public text from the
gallery. Test: the file parses, it has 100 items, 50 of each `kind`, and no email-shaped string.

**Packet 1 — `sandbox/bootstrap.sh`.** §2, as one script that is safe to rerun: each step checks
before it acts, and a second run with the worker up doesn't start another worker. It exits 3 on a
refusal. `--check` runs only the three smoke questions that need no model, and prints them as
`ok`/`fail` lines with the fix for each.

**Packet 2 — `sandbox/review.py`.** §3. Tests: rendering from a fixture database and fixture
`jobs/` (the suite's existing stubs), the page carries every held entry and no published one, no
email-shaped string reaches it (the publisher's own `scan_for_personal_data`), and the page's
`Copy decisions` output parses under packet 3's parser, under `node` the way the gallery's JS
tests run.

**Packet 3 — `sandbox/replay.py` and `sandbox/report.py`.** §4 and §7. Tests: every refusal in §4
posts nothing; a good block posts the exact form fields `batch_plan` expects (asserted against
`batch_plan` itself, not a copy of it); the report's numbers from a fixture database match a hand
count.

**Then the session** (`docs/plans/cloud-sandbox-gallery.md` §2 onward, run by the dedicated
session and not by CI). Its first message to the operator asks DECIDE[sandbox-models],
DECIDE[review-visibility] and the ceilings, with the defaults above, and does nothing else until
they are answered.

## 9. You know this worked when

- On the phone, the operator opened each batch's review page, touched the live sketches, and sent
  a decision block from the Claude app. The session came back with each entry's new state read from
  the database.
- `https://profcarroll.github.io/sketchgen-sandbox/` shows only the entries the operator published,
  each with its model and cost, plus the rejections with their reasons.
- `REPORT.md` there gives the publish rate per model, and every figure in it has its query.
- The `state` branch rebuilds the node in a new session.
- The session never started a batch without a decision block, and never went past a ceiling
  without stopping to say so.

## 10. Not in this plan

- Any change to the node, D12, `sketchgen-gallery` or the write-path Worker. The sandbox gallery
  has no votes, likes or views: no `writepath`, no sync.
- Local models in the sandbox. There's no GPU, and `qwen3-coder:30b` on CPU would take hours per
  sketch. The comparison with the node's local models is made afterwards, by reading this
  gallery's report beside the D12 batch over the same harvest. It's a model comparison, not a
  replication, and the report says so.
- Agent judging, blind pairs and Bradley–Terry scores (DECIDE[judging]).
- Folding any of `sandbox/` into `sketchgen/`. If the review page and the replayer earn their
  keep, a later plan can make "curate from a phone" a feature of the real node.
