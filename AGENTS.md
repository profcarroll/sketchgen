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
path uses. Full reference: `docs/OPERATIONS.md` → *Paid steps*.

**If you are the paid model, you answer the items yourself** — the `prompt` in
each item is addressed to you. Put your reply, verbatim, in that item's
`answer`, and set `model` to your own exact model id. That id is the entry's
provenance and the gallery prints it; do not write a model you are not.

One-time setup (the operator's, not yours, unless asked):

- Your model id must be in `SKETCHGEN_PAID_MODELS` in **both**
  `sketchgen-web` and `sketchgen-worker` (a systemd drop-in). Otherwise the
  worker sends your id to Ollama and the job fails with a 404. Check:
  `systemctl --user show sketchgen-worker -p Environment`.
- `sketchgen paid assign --plan <you> --execute <you>` makes every job that names no
  model yours. **Unset it afterwards** (`--plan local --execute local`): while
  set, it also catches released public submissions and preselects you on the
  New job page.

A whole job, from the laptop:

```bash
# runs sketchgen on the node; the packet files live on the node too
sg() { ssh sld-cloud "~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen $* --db ~/sketchgen/sketchgen.db"; }

# 1. queue it — with NO --planner flag, so it takes the assignment
#    (`--planner local` forces the node's model; enqueue has no --executor).
#    Two layers of quoting: the prompt crosses ssh.
sg enqueue --prompt "'a tide of slow lines'" --by profcarroll
# 2. wait until the worker parks it (≤ ~30 s; poll, don't act)
sg paid status --json
# 3. export, copy, answer, copy back, import
sg paid export --step plan --out /tmp/plan.json
scp sld-cloud:/tmp/plan.json .        # fill in each item's "answer" and "model"
scp plan.json sld-cloud:/tmp/plan.json
sg paid import /tmp/plan.json --json
# 4. wait for it to park again at needs='execute'; repeat 3 with --step execute
# 5. wait for the gate. Held → done (a person publishes). Parked again at
#    needs='execute' → the gate failed, and the next export carries its
#    evidence in the prompt. Repeat 3 until held, or failed at max_attempts
#    (default 3).
```

What to know while doing it:

- **Rejected is safe.** An answer that will not parse, or whose `guard` no
  longer matches, writes nothing and the job stays parked; the raw answer is
  kept in `FILE.rejected.json`. Fix it and import again. A rejected execute
  answer does not use up an attempt.
- **Change nothing in a packet but `answer` and `model`.** `guard`,
  `prompt_version` and `inputs` are how the node knows the answer is still
  about what it asked. A packet cut before a prompt file changed is refused:
  export again.
- **Plan answers** follow `prompts/planner.md`: a `Brief` heading and
  paragraph, then `Assertions`, one vocabulary word per line. Only words in
  the closed vocabulary survive (`planner.VOCAB`); the gate implements nothing
  else.
- **Execute answers** must contain a fenced ```` ```js ```` block; the prompt
  says the rest. The gate — headless Chromium on the node — is the referee and
  you cannot talk it round: read the evidence in the next prompt and fix what
  it names.
- **Images are paths on the node** (`images` in judge and critique items).
  `scp sld-cloud:<path> .` to look at them. A critique or verdict written
  without looking is worse than none — the local critic refuses to work blind.
- **Critique packets claim their entries**; the idle critic skips them until
  imported. Abandoning a packet: `sg paid release --step critique --all`.
- Everything you make is badged **off-node** in the gallery, and a paid
  executor is a second variable in the rules-file A/B the gallery is measuring:
  keep a paid run as its own batch, and say which in the PR or notes.

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
