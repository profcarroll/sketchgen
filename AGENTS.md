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

## Rules that are not negotiable

1. **Never run a second worker.** No `worker --once`, no `worker` of any kind,
   while `sketchgen-worker.service` is active — and it always is. Since #118 the
   fence refuses it (exit 3), but a stray one before that wedged two
   `llama-server`s at 800 % CPU for half an hour. Nothing you need requires a
   worker: the daemon claims queued jobs every ~30 s. **If you are waiting for
   the node, wait** — poll, don't act.
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

## Driving a step with a paid model (you, if you are one)

Any of the four steps — `plan`, `execute`, `judge`, `critique` — can be
answered by a model that is not on the node. The node writes what it would have
asked; you answer; the node reads it back through the same parser the local
path uses, and gates the sketch itself. Reference: `docs/OPERATIONS.md` →
*Paid steps*.

**If you are the paid model, you answer the items yourself.** Each item's
`prompt` is addressed to you. Put your reply, verbatim, in `answer`, and set the
packet's `model` to your own exact model id — it becomes the entry's public
provenance; never write a model you are not.

### The stop rule

**Run `preflight` first. If it says NOT READY, fix only the checks marked
`fix (you)`, run it again, and if anything is still failing, report the failing
checks to the operator — verbatim — and stop.** Do not investigate the node,
read unit files, restart services, resume the generator, or work around a
failed check. The same applies at every step below: when a verb exits 3 or
says `stop`, report what it printed and stop. Every verb here tells you what to
do next; if you find yourself guessing, that is a missing verb — say so.

### The recipe (from the laptop)

```bash
# every sketchgen command runs on the node; the packet travels over ssh stdio
sg() { ssh sld-cloud "~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen $* --db ~/sketchgen/sketchgen.db"; }
ME=claude-sonnet-5                                  # your own exact model id

sg paid preflight --as $ME                          # READY, or the stop rule
# (the only fix that is yours: `sg paid models add $ME` — a name, not a key)

sg enqueue --prompt "'a tide of slow lines'" --by profcarroll \
           --planner $ME --executor $ME --json      # → {"job": N, …}
# the prompt crosses ssh, so it is quoted twice

sg paid wait --job N                                # blocks; prints `next:`
sg paid export --step plan --job N --as $ME --out - > plan.json
#   write your plan into items[0].answer (see "Answers" below)
sg paid import - < plan.json                        # JSON: recorded / rejected

sg paid wait --job N                                # → next: export --step execute
sg paid export --step execute --job N --as $ME --out - > sketch.json
#   write your sketch into items[0].answer
sg paid import - < sketch.json

sg paid wait --job N
# held            → done: a person publishes it. Report the entry id.
# next: execute   → the gate failed; the new export carries its evidence in the
#                   prompt. Answer again. Attempts count up to max_attempts (3).
# failed          → done: report last_error.
```

`wait` only reads; it is how you wait. It returns when the job needs you, is
finished, or cannot move (the generator paused, or a person is needed) — and
exits 3 in the last case: report and stop. It gives up after 30 minutes (exit
1); run it again once, then report.

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
- **Change nothing in a packet but `answer` and `model`.** `guard`,
  `prompt_version` and `inputs` are how the node knows the answer is still
  about what it asked; a stale packet is refused — export again.
- **Rejected is safe.** An answer that does not parse writes nothing, uses no
  attempt, and comes back verbatim in `import`'s output. Fix it, import again.

### Other steps and settings

- **Judge / critique** items name images by their path on the node:
  `scp sld-cloud:<path> .` and look before answering — the local critic refuses
  to work blind, and so should you. Export them without `--job`; a critique
  packet claims its entries until imported (`sg paid release --step critique
  --all` to give them back).
- `paid assign` sets a model for every job that names none. Prefer
  `enqueue --planner/--executor`, which touches only your job. If you did
  assign, unset it: `sg paid assign --plan local --execute local`.
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
