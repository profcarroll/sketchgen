# AGENTS.md — read this before touching sketchgen

For any coding agent working in this repository or on the node. It is short on
purpose: every rule here was learned from an incident, and the longer
explanations are in `docs/OPERATIONS.md`, which this file points into. If you
find a trap that is not here, add it here in the same PR that fixes it.

## What this is

A p5.js gallery that generates itself on one node (`sld-cloud`, an OCI ARM VM):
**plan → execute → gate → repair**, then a person publishes to a static gallery
(`profcarroll/sketchgen-gallery`); an idle loop **judges** pairs and **critiques**
entries, and a critique becomes the next prompt. Python 3.12, stdlib only.

## Where things run

| | laptop (this checkout) | node (`ssh sld-cloud`) |
|---|---|---|
| code | here | `~/sketchgen/app` (a checkout of `main`) |
| python | bare `python3`, no venv, no pytest | `~/sketchgen/.venv/bin/python3` — **not** `app/.venv` |
| database | none | `~/sketchgen/sketchgen.db` |
| worker | never | resident daemon, `sketchgen-worker.service` |

On the node, every command is:

```bash
SG="$HOME/sketchgen/.venv/bin/python3 $HOME/sketchgen/app/bin/sketchgen"
$SG <command> --db ~/sketchgen/sketchgen.db
```

From the laptop, `bin/sg <command>` runs exactly that over ssh, with every
argument quoted once for you and stdin/stdout passed through.

## Rules that are not negotiable

1. **Never run a second worker.** No `worker --once`, no `worker` of any kind,
   while `sketchgen-worker.service` is active — and it always is. Since #118 the
   fence refuses it (exit 3), but a stray one before that wedged two
   `llama-server`s at 800 % CPU for half an hour. Nothing you need requires a
   worker: the daemon claims queued jobs every ~30 s. **If you are waiting for
   the node, wait** — `paid next` is how; poll, don't act.
2. **No paid credential ever goes on the node** (DECIDE[credential-model] branch
   B). Paid models are reached only through `sketchgen paid` — see below.
3. **Never move a job into a running state by hand** (`planning`, `executing`,
   `gating`, `repairing`). `db.claim_next` only reads `queued`; anything else
   put there sits unattended until the stuck-sweep, 30 min later. Return legs
   go to `queued`.
4. **No hand edits to the database.** Every write has a verb. If one is
   missing, that is the bug to fix, not a reason to open `sqlite3`.
5. **Pushes go as `profcarroll`.** `gh` has two accounts; `mercurious` cannot
   write. `gh auth status` before a push that 403s. Branch, PR, never commit to
   `main`, never merge without being asked.

## Driving a job with a paid model (you, if you are one)

Any of the four steps — `plan`, `execute`, `judge`, `critique` — can be
answered by a model that is not on the node. The node writes what it would have
asked; you answer; the node reads it back through the same parser the local
path uses, and gates the sketch itself. Reference: `docs/OPERATIONS.md` →
*Paid steps*.

**If you are the paid model, you answer the items yourself.** Each item's
`prompt` is addressed to you. Put your reply, verbatim, in `answer`, and set the
packet's `model` to your own exact model id — it becomes the entry's public
provenance; never write a model you are not.

**A paid job is made only from this CLI, by an agent that will answer it.**
There is no paid choice on the New job page, and `paid assign` refuses a paid
planner or executor. Two jobs made without an agent attached — one from the
page, one inherited by a critique child — sat at `needs-laptop` for hours on
2026-09-21, and that is why.

### Before you start

**Budget.** Your first command is `date -u +%FT%TZ`; pass what it prints to
`paid start --since`. Every `next` and `import` then says how long you have been
at it, and `start` prints the budget you are holding yourself to — ten minutes
and 30,000 generated tokens by default, advisory, never enforced. A sketch is a
few minutes' work: read this file, plan, write under 150 lines, check it,
import. The executor prompt says "no second chance". That is written for local
models, which get one reply per attempt. You have three attempts and a verdict
takes seconds.

**Freshness.** `bin/sg paid preflight --as $ME` prints `node_commit`; if `git
merge-base --is-ancestor <it> HEAD` fails in your checkout, pull before reading
further — the node is ahead of the file you are reading, and its packets may
carry fields nothing here mentions (2026-09-21: a session read this file at #131
and answered packets cut under #132).

**Looking.** `rig/README.md` is a browser rig for looking at a sketch on the
laptop. It is a floor, not the gate. (Packet 7 replaces most of it with `paid
try`, which runs the real gate.)

**One session, both steps.** `--executor` with your own id is accepted and runs
as one session; you are not expected to hand off to yourself.

### The verbs

`bin/sg` runs one sketchgen command on the node over ssh. Every argument is
quoted once, by it; a prompt with spaces or quotes needs nothing more. Stdin
and stdout pass through, so a packet travels with no scp.

```bash
ME=claude-sonnet-5                                  # your own exact model id
SINCE=$(date -u +%FT%TZ)                            # when your work began

bin/sg paid start --as $ME --by profcarroll --since $SINCE \
    --note "skill=algorithmic-art; local prototype" \
    --prompt "a tide of slow lines"
#   registers $ME (a name, not a key), runs the preflight, queues one job with
#   you as planner and executor, and leases it to you. Prints `next: …`, the
#   budget line, and the node's commit.
#   NOT READY (exit 3): nothing was queued. Report the checks verbatim; stop.
#   --planner / --executor: `local`, an Ollama tag, or another *registered*
#   paid model (see "Two agents on one job" below). At least one step is yours.
#   --since  what your first `date -u +%FT%TZ` printed. A stamp in the future,
#            or more than a day old, is refused (exit 3) and nothing is queued.
#   --note   what the node cannot see about how you are making this one — a
#            skill, a local prototype. It goes on the entry page.

bin/sg paid next --job N --as $ME > packet.json     # returns within 4 minutes
#   one JSON object, with "do":
#     answer  the object IS the packet. Write your reply into items[0].answer,
#             then:  bin/sg paid import - < packet.json
#     wait    the worker has it; "worker" says what it is doing and "say" says
#             where your job is. Not an error. Run the same command again.
#             (On a split job, also: the other agent is still planning.)
#     done    held (report the entry id; a person publishes it), or failed
#             (report last_error). You are finished. "verdict" says what the
#             gate found in the attempt the entry kept — a clean pass, or a
#             sketch that ran and missed an assertion; do not infer it.
#     handoff the rest of a split job is the other paid model's. "then" is
#             that agent's command; report it and stop — you are finished.
#     stop    exit 3: a person is needed, the generator is paused, or another
#             agent holds the job. Report "say" verbatim; stop.

bin/sg paid try --job N --as $ME < answer.txt       # returns within 4 minutes
#   the node's own gate over a candidate, before you commit to it. Stdin is
#   the text you would put in items[0].answer — the fenced js block, an
#   optional html block, the statement — so a reply `import` would reject is
#   rejected here too, for free. One JSON object, with "do":
#     verdict the gate ran: `exit`, every check, each assertion with its
#             detail, `timings.ms_per_frame` (the number that decides the
#             frame budget; a laptop proxy reads about a third under it),
#             the console, and the node paths of strip.png, gate.png and
#             ghost.png (four frames of the sketch under the pointer script,
#             yours if you wrote a `ghost` block) — `scp sld-cloud:<path> .`
#             and look. `then` is the import on a clean pass, and another
#             try otherwise.
#     rejected no fenced js block. Nothing was written, no try spent.
#     wait    the worker has not reached it yet. Run the same command again.
#     stop    exit 3: you hold no lease, another agent does, or the cap is
#             spent (8 tries a job). Report "say" verbatim; stop.
#   It is advisory and capped: it writes no attempt, no entry and no row on
#   your job, and it uses none of your three attempts. It renews your lease,
#   and the gate it runs is the one that gates your import.

bin/sg paid import - < packet.json                  # JSON: recorded / rejected
#   then `next` again. The gate runs on the node; a failed gate comes back as
#   the next attempt, with the evidence in the prompt. Attempts count up to
#   max_attempts (3).
```

That is the whole loop: `start` once, then `next` → answer → `try` until the
verdict is clean → `import`, until `next` says `done`. Nothing else is needed
for your own job. A `try` is seconds of the node's time and is how you find
out what the gate thinks; an attempt is one of three. `next` and `import` each
print one `elapsed` line, and one more when the job is past the budget: that is
a nudge to finish and say so, never a refusal.

### While you wait

`next` returns `wait` when the worker is busy — on your job (planning takes
under a minute, an attempt with the gate a few), or on the one job it had
already claimed when you started (up to ten minutes when a local planner times
out). Your lease means the worker takes your job before anything else queued
and starts no idle work (no judge, no critique, no spawned child) while you
are driving; every `next` and `import` renews it, and it lapses twenty minutes
after your last one. **If `next` says `wait`, run `next` again.** Do not read
the node's logs, list its processes, or start anything to hurry it.

### Two agents on one job

The operator may want one paid model to plan and another to execute — the
split the New job page gives two local models. One agent cannot be both (the
model id on a packet is public provenance; never write one you are not), so it
is two sessions and a handoff, and `next` carries it:

```bash
# in a session that is claude-sonnet-5:
bin/sg paid start --as claude-sonnet-5 --by profcarroll --executor claude-opus-4-6 \
    --prompt "hot air balloons"
bin/sg paid next --job N --as claude-sonnet-5      # answer → import → next: handoff
# in a session that is claude-opus-4-6 (the operator opens it):
bin/sg paid next --job N --as claude-opus-4-6      # answer → import → … → done
```

The other model must already be registered — `bin/sg paid models list` shows
the exact ids; if the one you were asked for is not there, `start` refuses
(exit 3) and names the `paid models add` for the operator. Do not guess an
id: the second agent has to answer as exactly that name. The planner's `next`
says `handoff` as soon as its plan is in, and drops its lease; the executor's
`next` says `wait` while the other agent plans, then takes the attempts. If
the executor starts the job (`--planner` names the other), the same holds the
other way round. The other session has twenty minutes from the job parking
to run its `next`; after that the worker hands the job to this node's models,
as for any agent who left. `paid preflight` lists a job parked for another
agent with that agent's `next` command, and `paid release --job N` if no such
agent is coming; a job `leased to X` is being driven and has no command.

### Leaving

If you cannot finish — out of budget, told to stop — hand the job back so this
node's models finish it: `bin/sg paid release --job N --by $ME --reason "…"`.
If you vanish instead — killed, out of quota — the worker does the same
release itself once your lease has lapsed and the job has sat twenty minutes
parked (job 1263, 2026-09-21, whose agent's weekly quota ran out mid-plan).
The preflight shows a parked job as `leased to X until T` while its agent is
driving it — never release one of those — and as `no agent` once nobody is.

### Answers

- **Plan:** a line `Brief`, one paragraph describing the sketch, then a line
  `Assertions` and one word per line from the closed vocabulary in the prompt
  (e.g. `motion(idle)`, `responds(click)`, `size(800,600)`). Anything outside
  the vocabulary is dropped; the gate implements nothing else.
- **Execute:** a fenced ```` ```js ```` block with the whole sketch (p5.js,
  global mode), optionally ```` ```html ````, and the statement the prompt asks
  for. The gate is headless Chromium on the node and is not negotiable: on a
  failure, read the evidence at the end of the next prompt and fix what it
  names.
- **Optionally, a fenced ```` ```ghost ```` block:** the pointer script the
  kiosk and the gate play when nobody is at the keyboard, in canvas fractions
  (`[{"t": 800, "type": "move", "x": 0.5, "y": 0.5}, …]`, types
  `move`/`down`/`up`/`click`), at most 64 events over 8 s. Write the gestures
  you tested. Without it the entry gets the built-in script for its
  assertions. An invalid block is dropped and the reply still counts.
- **`usage` is optional.** If you know the token counts of your own reply,
  put them in the item's `usage` (`prompt_tokens`, `completion_tokens`); leave
  what you do not know `null`. Never estimate: a blank on the entry page is
  true, a guess is not. The node times the round trip itself.
- **`process` is optional too, and it is the other cost.** An attempt item
  carries a `process` slot for what the work *around* the reply cost you:
  `session_s`, `output_tokens`, `thinking_tokens`, `tool_calls`, `screenshots`,
  `effort`. Same rule as `usage` — leave out what your harness cannot tell you,
  and never estimate. The node adds the tries it ran for you and shows the line
  on the entry page as *as reported by the agent*; nothing in the A/B or any
  judgment reads it. `rig/cost.py` prints the object to paste.
- **Change nothing in a packet but `answer`, `usage`, `process` and `model`.** `guard`,
  `prompt_version` and `inputs` are how the node knows the answer is still
  about what it asked; a stale packet is refused — run `next` again.
- **Rejected is safe.** An answer that does not parse writes nothing, uses no
  attempt, and comes back verbatim in `import`'s output. Fix it, import again.

### The stop rule

Every verb here tells you what to do next. **When one exits 3 or says `stop`,
report what it printed — verbatim — and stop.** Do not investigate the node,
read unit files, restart services, resume the generator, or work around it.
If you find yourself guessing, that is a missing verb — say so.

### Other steps and settings

- **Judge / critique** items name images by their path on the node:
  `scp sld-cloud:<path> .` and look before answering — the local critic refuses
  to work blind, and so should you. `bin/sg paid export --step judge --as $ME
  --out -` (no `--job`); a critique packet claims its entries until imported
  (`bin/sg paid release --step critique --all` to give them back).
- `paid assign` sets a *local* model for `plan`/`execute` defaults and may set
  a paid one for `judge`/`critique`, which makes the idle loop leave that step
  for `paid export`. Never needed for your own job.
- `paid preflight --as $ME` is what `start` runs; run it alone to see the
  node's state (worker step, leases, parked jobs) without queuing anything.
- Everything you make is badged **off-node**, and a paid executor is a second
  variable in the rules-file A/B the gallery measures: keep a paid run as its
  own batch and say so.

## Deploying

`ssh sld-cloud 'bash ~/sketchgen/app/update.sh'` — pull, pause the generator,
reinstall units, **migrate**, re-render and push every entry page, restart
worker and web, resume. Traps:

- **update.sh only resumes what it paused.** If the node was already paused,
  `✓ all done` does not mean running. Read the `control` table.
- **A migration in the pull:** snapshot first
  (`sqlite3 sketchgen.db ".backup sketchgen.db.pre-NNN"`) and dry-run it on a
  copy. `update.sh` applies it; `db init` does too.
- **Unit reinstall overwrites the unit files.** Settings belong in drop-ins
  (`~/.config/systemd/user/<unit>.service.d/*.conf`), which survive.
- **`--no-render`** only when nothing touched `gallery.py`, a template, assets
  or the render path; a template change needs the full re-render (~25 min).
- **The write-path Worker (`writepath/`) is not deployed by update.sh.** Deploy
  it by hand with `npx --yes wrangler@latest deploy` from `writepath/`, and
  apply any D1 schema change **before** the deploy, verifying the table exists
  in between — a Worker that queries a missing table signs every visitor out.

## Working on the code

- Tests: `python3 -m unittest discover -s tests` from the repo root — stdlib,
  ~75 s, ~1330 tests, 3 skips that need the node. No network, no model, no
  browser: every model reply is a stub on disk. Keep it that way.
- Exit codes everywhere: 0 ok, 1 failed, 3 refused (nothing written).
- New CLI verbs go in `sketchgen/cli/<name>.py` with `register(top)`;
  `bin/sketchgen` picks them up. Give agent-facing verbs `--json`.
- A schema change is a new `migrations/NNN_*.sql`. SQLite cannot alter a
  CHECK: rebuild the table as `007` and `014` do, same column order.
- Provenance is who **answered**, never who was asked: model ids on attempts,
  entries, critiques and verdicts are the model that produced the text.
- Match the house style: comments say *why*, with the incident or date that
  made it necessary; commit messages are prose, not bullet lists.
