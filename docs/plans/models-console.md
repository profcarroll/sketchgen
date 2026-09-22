# Models: the console page, and keys that stay on the laptop

The console gets a **Models** page — what this node has, what each step is assigned, what a
model has done, what would fit, and which off-node models can be answered right now — and the
paid path gets the leg it has been missing: **the operator's own API keys, used from the laptop,
at the moment the tunnel opens**, by a script that drives the same verbs an agent does.

Mockup (the visual spec, two boards: the page and one model's detail):
https://claude.ai/artifact/MDL7KJh23as8agzphLkDYF

Why: two things are missing and they are one thing. There is no screen for models — assignment
is a CLI verb, the catalogue is a menu on New job, a model's record is a query nobody runs, and
pulling one is `ssh sld-cloud 'ollama pull …'` — and there is no way to answer a paid step
without a coding-agent session at the keyboard. The second is why 2026-09-21's two `needs-laptop`
jobs sat for hours: something asked for a paid model, and nothing that held a key was listening.
The operator does hold keys, on the laptop, in the same place and under the same rule as the OCI
key that reads the bill. That key already gets used on a schedule the operator does not have to
remember — `sgt` runs `billing --sync` when the tunnel comes up (#154) — and that is the shape.

Repositories and conventions are as in `held-batch.md`: `profcarroll/sketchgen`, Python 3.12,
stdlib only, `python3 -m unittest discover -s tests`, every model reply and every HTTP answer in
a test is a stub on disk. Small PRs, one packet per branch. **Nothing here touches the gallery
generator, the prompts, or the gate.**

## 0. The precedent: what `billing --sync` decided

`sketchgen/cli/billing.py` is two halves because only one machine can do each. *Reading* the
figure needs `~/.oci/oci_api_key.pem`, a key that can destroy infrastructure, so it runs on the
laptop and never on the node. *Recording* needs the node's database and no credential, so
`--record` takes the reading on stdin. `--sync` joins them over one `ssh`. The tunnel script
calls it after a successful `up`, non-fatal, with its own ssh, so a billing hiccup never costs
the tunnel.

Everything below is that pattern applied to paid inference. A vendor key is less dangerous than
the OCI key and the rule is the same, because the reason is the same: the node serves a public
gallery and runs code written by a model. DECIDE[credential-model] branch B stands: **the node
never holds a credential and never calls a vendor.** What crosses the ssh is a *reading* about
the keys (which models the laptop can answer for, checked when) and *answers* to packets the
node cut — the same envelope an agent hands to `paid import`, with the same guard.

`docs/plans/agentic-cli.md` §8 put "automating the laptop leg from the node" out of scope, on the
grounds that a queue the node offers is a queue somebody will one day drain with a key on the
node. This plan automates the laptop leg **from the laptop**, and §1.7 is the fence that keeps
it there. The concern was right; the answer is to make the laptop the only place the answerer
will run.

## 1. Decisions

### 1.1 What syncs is a reading and answers, never a key

`paid keys --sync HOST` ships one JSON object: for each model id the laptop's config names, the
vendor, the API model id, whether the key file is present, whether a one-token ping succeeded,
and how much of the day's budget is left. The node registers the ids (`db.set_paid_models`,
names only, as `paid models add` does) and stores the reading in `meta` as `paid_answerable`.
The Models page shows it with its age. `--record` on the node refuses a reading that carries
anything key-shaped, so a misconfigured laptop cannot leak one by accident: belt over the braces.

### 1.2 The tunnel is the trigger, not the transport

Opening the tunnel is the operator sitting down. That is the moment the keys are present and
wanted, so `sgt` (`bin/sketchgen-tunnel.sh up`) runs, after billing: the keys sync, then starts
the **answerer** in the background for as long as the tunnel lives. Nothing about keys goes
through the `-L` forward; the answerer talks to the node the way `bin/sg` does, one ssh per verb,
and calls the vendor from the laptop. `sgt down` stops it; a tunnel that dies stops it on its
own (two failed health checks in a row); a laptop lid closing stops it by stopping everything.
The browser at `localhost:8081` sees the node's page, which shows what the answerer has done.

### 1.3 The answerer is the agent recipe, run by a script

AGENTS.md's loop — `preflight` to see what is parked and whose it is, `next` for the packet,
answer, `import`, `next` again until `done`; `export --step judge|critique --as MODEL` for the
idle steps — is exactly what `paid answer --watch HOST` runs, with the vendor's API in the seat
the session sat in. No new node-side verb is needed for it, and no verb is bypassed. The lease
works as it does for an agent: `next` takes it, `import` renews it, and if the answerer
vanishes the worker's sweep hands the job to this node's models after a lease's length, as it
did for job 1263. #118 is the record of what happens when the laptop leg improvises; a script
that only knows five verbs cannot.

`paid try` is not used. A try is the gate lent to something that will look at the verdict and
change its mind, and an API call does not. Three attempts with the gate's evidence in the next
prompt is the loop the local executor already lives in.

### 1.4 Provenance is the model that answered, read from the response

The laptop config maps a registered id to the vendor's API model id. The reply's `model` is set
from the API response's own `model` field — never from the config — because the rule since
`plan --job` is that the column holds who answered. When the vendor reports a dated variant
(`claude-sonnet-5-20260415`), that is what is written, and the `--sync` reading registers both
the config's id and the last id the vendor reported, so `import` accepts it. `usage` is the
API's own counts: prompt tokens including cache reads, completion tokens including thinking.
`process` is filled with what a script honestly knows — `session_s` the round trip, `output_tokens`
the completion count, `tool_calls` 0, `screenshots` 0, `effort` the request's setting — and
nothing estimated. For the first time the token rows on a paid entry page come from the API
that produced the text rather than from a transcript read after the fact (#148).

### 1.5 `paid assign` keeps refusing a paid planner or executor default

The refusal (#131, after the 2026-09-21 parkings) exists because a paid job with nobody to
answer it sits. The answerer makes that untrue *while the tunnel is open*, and untrue is not
never: a child the idle critic spawns at 03:00 would park until the operator's morning. So the
rule stays, and a paid plan or execute still begins with `paid start`. DECIDE[paid-defaults],
after the answerer has run for a week: whether a parked job that the reading says is answerable
is a job that may be queued from the New job page, with the wait shown. Judge and critique
defaults may already be paid, and those are what the answerer will mostly drain.

### 1.6 Budget lives on the laptop, with the key

The config carries caps — calls per tunnel session, completion tokens per day — and a ledger
(`~/.config/sketchgen/paid-ledger.jsonl`, one line per call: when, host, model, step, job or
entry, both token counts, round trip, outcome). A cap reached stops the answerer with exit 3
and a line saying which; the reading carries `budget_left` so the page can show it. The node's
own record is per attempt, as today, and nothing on the node enforces a spend: it cannot see
the invoice, and pretending to would be the odometer decision agentic-cli §8 declined.

### 1.7 The answerer refuses to run on the node

`paid keys --sync` and `paid answer` require a HOST and refuse `localhost`. Before reading a key
file they ask the instance metadata service (`169.254.169.254`, the call `billing --identify`
uses to learn the tenancy) with a one-second timeout: an answer means this is an OCI instance,
and the verb exits 3 with *this is the node; the answerer runs on the laptop*. A key file that
is not mode 0600, or not a regular file, is refused as `sync.py` refuses its token file. The
config directory is `~/.config/sketchgen/`, never the checkout, so a `git add` cannot carry it.

### 1.8 Images travel by scp, once, to a temp directory

Judge and critique items name images by node path (agentic-cli §3.2). The answerer copies each
to a per-run temp directory, sends them as image content blocks, and removes the directory at
the end of the pass. A vendor entry without `vision = true` skips those items with a reason in
the ledger and the item's `answer` left empty, which `import` treats as not attempted.

### 1.9 One call per item, no tools, no loop

An item's `prompt` is sent verbatim as one user message; the reply is the answer. There is no
system prompt of our own, no tool use, no retry on a parse failure — the node's parsers and
guards decide, and a rejected answer comes back as the next attempt's evidence exactly as it
does for a local model. This is the injection stance: a packet holds model-written text (sketch
source, evidence, critiques), and the only thing the laptop ever does with it is send it to a
vendor and hand the text that comes back to `import`. Nothing in a packet is executed, opened,
or followed on the laptop.

### 1.10 The Models page shows readings and calls verbs

The console runs on the node (127.0.0.1:8081, `sketchgen-web.service`); only the browser is on
the laptop. So the page cannot touch a key, and every number on it is either read from Ollama,
the database, or `meta`, or written by a POST that calls the function a CLI verb calls
(rule 4: every write has a verb). The Keys panel of the mockup is therefore a status card —
*laptop holds keys for … · synced at … · answerer running, last pass … · budget left …* — and
the "answer from this tab" option in the first draft is dropped, superseded by §1.2.

## 2. The laptop config

`~/.config/sketchgen/paid.toml`, read with `tomllib`, mode 0600:

```toml
[budget]
calls_per_session = 60           # per `sgt up`
completion_tokens_per_day = 400000

[vendors.anthropic]
key_file = "~/.config/sketchgen/keys/anthropic"   # or key_env = "ANTHROPIC_API_KEY"
# base_url defaults to the vendor's

[vendors.openai]
key_env = "OPENAI_API_KEY"
base_url = "https://api.openai.com/v1"           # any OpenAI-compatible host

[models."claude-sonnet-5"]       # the id registered on the node, and on every packet
vendor = "anthropic"
api_model = "claude-sonnet-5"    # what the request names; the response's id is what is written
vision = true
max_tokens = 8192
effort = "medium"                # sent when the vendor takes it; recorded in process.effort

[models."claude-opus-4-8"]
vendor = "anthropic"
api_model = "claude-opus-4-8"
vision = true
```

Two vendor adapters and no more: `anthropic` (Messages API) and `openai` (chat completions,
which covers every compatible host by `base_url`). Both are stdlib `urllib`, both read `usage`
from the response, both are tested against fixture responses on disk. A third vendor is a
packet of its own when somebody has a key for one.

## 3. The packets

### Packet 1 — `paid keys`: the reading

`sketchgen paid keys` (no args): read the config, check modes, print one line per model —
vendor, API id, vision, *key present* or *key missing* — and never a key. `--ping`: one
minimal call per vendor, reported as ok or the vendor's error. `--sync HOST`: check, ping
(skip with `SKETCHGEN_PAID_NO_PING=1`), build the reading, and `ssh HOST sketchgen paid keys
--record --from-json -`. On the node, `--record` registers the ids, stores `paid_answerable`,
and refuses a reading with a key-shaped value. Guards from §1.7. `--json` on all of it.

Tests: config parsing and the mode check; the reading's shape; `--record` refusing a
`sk-`-shaped value and a `localhost` host; the metadata-service guard with the call stubbed.

### Packet 2 — `paid answer`: the API leg

`sketchgen paid answer PACKET` (file or `-`): for each item with an empty `answer` whose packet
`model` the config knows, call the vendor and fill `answer`, `model`, `usage`, `process`; print
the packet. Pure function from packet to packet, so `bin/sg paid next … | sketchgen paid answer
- | bin/sg paid import -` is the whole thing by hand. `--sync HOST`: one pass of the loop in
§1.3 — parked jobs that are a configured model's, then the judge and critique steps assigned to
one — and exit. `--watch HOST`: the pass every 30 s until stopped, a cap, or the tunnel gone;
a pidfile under `$XDG_RUNTIME_DIR`, a log beside the ledger. Ledger and caps from §1.6; images
from §1.8; guards from §1.7.

Tests: the two adapters against fixture responses, including `usage` mapping and a dated model
id in the response; a packet round trip with the node side stubbed by `SKETCHGEN_SSH` as
`test_sg.py` does; an item skipped for no vision; a cap stopping a pass; nothing on the
network. No test opens a real socket.

### Packet 3 — the tunnel hook

In `bin/sketchgen-tunnel.sh`: after `refresh_billing`, `sync_keys` then `start_answerer`, each
non-fatal in the billing hook's exact manner (the tunnel is what `up` guarantees). `down` stops
the answerer first. `status` shows its pid and the ledger's last line. `SKETCHGEN_NO_PAID=1`
skips both; `SKETCHGEN_PAID_WATCH=0` syncs the reading and starts nothing; `SKETCHGEN_PAID_TIMEOUT`
bounds the sync. The usage text says, as it does for billing, that the keys are on this laptop
and never on the node. A `test_ops.py` case runs the script with `ssh` and `sketchgen` replaced
by fakes and checks the order and the non-fatality.

### Packet 4 — `/models`, the page

Route, template `op_models.html`, and a nav entry between New job and Held. Read-mostly:

- **Assignments**: the four steps as selects, drawn from `models.catalogue()` filtered as the
  New job menus are, plus the registered paid ids for judge and critique. The fit-together bar:
  the assigned pair's `size_bytes` against the node's RAM, amber when the two do not fit
  beside each other. POST `/models/assign` runs the checks `cli/paid.py:_assign_changes` runs
  today — move that function into `paid.py` so the CLI and the page share one — and then
  `db.set_assignment`.
- **Catalogue**: on-node and off-node groups; `/api/show` capabilities as chips, with the
  tags-only claims dashed; resident state from `/api/ps` as the console's Model card reads it;
  the roles each qualifies for and holds; entries and first-pass rate by model id from
  `attempts` and `entries`; last use. Remove is `DELETE /api/delete` then `models.forget()`,
  refused while the model is assigned or resident on a running step. Warm is Ollama's load
  idiom, an empty `/api/generate` with `keep_alive`. Register and Forget are
  `db.set_paid_models`, refused for an id an assignment names or a lease holds.
- **Off-node and keys**: the `paid_answerable` reading with its age (older than a day reads
  *unknown · last tunnel N days ago*), `paid.parked_jobs` and `paid.waiting` as the parked table
  with each row's command, and *Give back to the node* as `paid.release`.
- **Housekeeping**: `/mnt/models` from `os.statvfs`, never-used models, the tags-versus-show
  disagreements, the Ollama version.

Nothing on this packet is new data. Tests in `test_web.py` with the catalogue and `/api/ps`
stubbed, as `test_console.py` does.

### Packet 5 — how they do, and one model's page

`sketchgen/modelstats.py`: per (model, step, prompt version, node shape) — n, first-pass rate,
mean attempts, wall, tokens per second where the attempt recorded tokens and duration, lines,
the most common gate failure, and the two Bradley–Terry columns as the model's entries' mean
strength inside the population `pairs.py` already scores, printed as `n<20` under twenty.
Migration `018_node_shape.sql` adds `attempts.node_shape`, backfilled by date — before
2026-09-13T04:02Z the node was A1.Flex 4/24, after it 16/96 (`billing.py`'s comment records
the resize) — and the worker stamps `node_shape()` on every attempt from then on. A shape
selector on the page lists the shapes that have rows and shows a GPU shape disabled until one
has, so expansion is a column and not a rewrite. `/models/<tag>` is the detail board: the three
capability sources, the per-step table, the last five entries as the gate's strips, the last
error, Remove. `sketchgen models stats --json` is the same query from the CLI.

### Packet 6 — pull

The registry has no search, so the page takes a name and lists its tags from
`registry.ollama.ai/v2/library/<name>/tags/list`, and reads each tag's manifest for its layer
sizes — that is the GB before anything is downloaded. Capabilities from the manifest's config
blob when it has them, dashed as claims either way; `/api/show` after the pull is the truth,
and the row moves into the catalogue with the confirmed set. Fit: weights plus the assigned pair
against RAM, and weights against free disk; a decode estimate from the node's measured tok/s on
models of the same active-parameter count, labelled a guess until the model has run an attempt.
POST `/models/pull` streams `/api/pull` in one thread of the web process as the held batch does,
one pull at a time, progress on the page. A pull runs beside the worker: it is a download on a
sixteen-core box and has never been seen to slow a step; if a measurement says otherwise, the
fix is to serve pulls from the worker's idle pass the way tries are served, not to pause.

### Packet 7 — audition

Migration `019_batch.sql`: `jobs.batch TEXT`, carried to attempts and entries, and read by
`pairs.py` so a batch is its own population. `sketchgen audition --model TAG --step execute
--n 10` queues ten jobs on prompts the default has answered, everything else at the defaults,
under `audition/<tag>/<step>/<date>`; the detail page's Audition panel is that verb. A paid
run's batch is the note AGENTS.md already asks an agent to keep, made a column.

## 4. Acceptance

1. `paid keys --sync sld-cloud` from the laptop: the node's Models page shows each configured
   id as answerable with the check time; `paid models list` on the node shows the ids; `grep`
   of the node's database and journal for the key's first eight characters finds nothing.
2. A job started by `paid start --as claude-sonnet-5 --planner local` with no session attached,
   then `sgt up`: the answerer takes the attempt, the entry records the model the vendor
   reported, `usage` on the entry page is the API's counts, and the ledger has the line.
3. Six entries assigned to a paid critic: one pass drains them; each critique's `critique_by`
   is the vendor's model; each spawned child is badged off-node.
4. `sgt down` while a pass is mid-call: the call completes or the packet is dropped unimported;
   nothing is half-written on the node; the lease lapses and the worker's sweep does what it
   does for any agent who left.
5. `paid answer` on the node exits 3 before reading any file.
6. The cap reached: the answerer exits 3 naming the cap; the page's reading shows zero budget.
7. `/models` renders with Ollama down, with no reading, and with the database's `meta` empty —
   each panel says what it does not know rather than 500ing, as the New job page does.
8. The full suite passes with no socket opened.

## 5. Effort and order

Packet 1 is half a day. Packet 2 is a day: two adapters, the loop, the ledger, and their tests.
Packet 3 is a morning. Packet 4 is a day, most of it the template. Packet 5 is a day, the
migration and the backfill first. Packets 6 and 7 are half a day each. Order: 1 → 2 → 3, which
is the whole of the key decision and is worth landing alone, then 4 → 5 → 6 → 7. Packet 4 can
begin beside 2; it needs only the reading's shape.

## 6. Out of scope, and open

- **A key anywhere but the laptop.** Not in the node's environment, not in a Worker secret, not
  in the browser. The billing plan's future — a console hosted outside the tunnel with real
  auth — reaches the same conclusion for OCI: a scoped read-only key on a server is a different
  decision with a different threat model, and paid inference has no read-only key.
- **The New job page offering a paid model.** DECIDE[paid-defaults], §1.5, after a week.
- **Other vendors.** A packet each, when there is a key.
- **Pricing paid inference on the odometer.** Still agentic-cli §8: the ledger is the laptop's
  and the entry page says *as reported*.
- **Reverse forwards.** A `-R` that let the node reach a laptop service would make the node,
  for the life of the tunnel, a thing that can call a vendor. The laptop polls; it stays that
  way.
