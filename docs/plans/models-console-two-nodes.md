# The Models page, with two nodes

An amendment to `docs/plans/models-console.md` (Packets 4, 5 and 7) and to the console's control
strip, in the manner of `docs/plans/model-load-cost.md`, which it builds on and does not reverse.
Written 2026-09-23, from one night: `d12-node-profcarroll` (x86, RTX 5070 Ti 16 GB, 64 GB) came
up beside `sld-cloud` (OCI A1.Flex 16/96, no GPU), both nodes were deployed to the same build, and
a hardware A/B was queued on both — the same 100 prompts, planner `gemma4:e4b`, treatment rules,
three attempts, seed 1 (d12 jobs 102–201, sld-cloud jobs 1373–1472).

The plan was written for one CPU node. Three things it specifies stop being true, or stop being
enough, the moment there are two, and the operator put them first. Three smaller ones follow.
None of them needs a new privilege surface, and §1.10's rule — every number read from Ollama, the
database, `/proc` or `meta` — holds throughout.

## 1. A GPU node is sized against VRAM, and its loads are now invisible

### 1.1 The fit bar reads the wrong memory

Packet 4's bar compares the resident set against RAM (as `model-load-cost.md` §3 corrected it).
On d12 RAM is not the constraint. Read from `/api/ps` during the A/B:

| model | `size` | `size_vram` | on the GPU |
| --- | --- | --- | --- |
| `qwen3-coder:30b` (executor) | 19.03 GiB | 14.18 GiB | **75%** |

`nvidia-smi` at the same moment: 14,855 of 16,303 MiB used. So a quarter of the executor's
weights run on the CPU, and the planner cannot be resident beside it at all. (This said the
planner was 8.9 GiB; that is its size on disk. `sketchgen bench` read it back from `/api/ps` on
2026-09-23 as 3.37 GB resident at 8k context, all of it on the GPU — still more than the
card has left once the executor is loaded.) The worker's log shows what that costs: **the executor was reloaded for 22 of the first 22
A/B jobs**, a mean of 4.31 s each, because every plan evicts it. On sld-cloud the same pair sits
in 94 GB of RAM and neither is evicted by the other.

A RAM bar would call d12's assignment green with 50 GB to spare. The bar on a node with a GPU
should be:

- **VRAM first.** The resident set's `size_vram` against the card's total, from the `nvidia-smi`
  collector the console already runs (`console.py:1368`; d12's console header already prints
  *gpu 4.6 of 15.9 GB VRAM, 96% busy*).
- **The split, per model.** The console's Model card already derives it — `console.py:717`,
  `"25%/75% CPU/GPU"`. The catalogue and the assignments panel should show the same string, and
  amber any assigned model that is not 100% GPU on a node that has one: it is the difference
  between a model that fits and one that half-fits.
- **Co-residency as the verdict.** Amber when the assigned steps cannot all be resident in VRAM
  at once, which is `model-load-cost.md` §3's rule with VRAM as the denominator: on d12 today,
  planner + executor = 28 GiB against 16.

`model-load-cost.md` §2 found `/api/ps size` under-reporting one model by 14× on the CPU node.
Whether `size_vram` has the same failure on CUDA is unmeasured; `nvidia-smi`'s total is the
cross-check, and the per-process figure it can give is the honest one if they disagree.

### 1.2 The shape has to say GPU

Packet 5 keys stats on `meta.node_shape`, which only an OCI node can learn (IMDS; #161 already
skips the billing refresh on a node that is not on OCI). Off OCI, `worker.node_shape()` falls back
to architecture, cores and `MemTotal` — `x86_64 16/59` for d12, which says nothing about the one
component that made its decode five times faster (99–102 tok/s against 18–22). The fallback
should append the GPU and its VRAM when `nvidia-smi` answers: `x86_64 16/59 + RTX 5070 Ti 16G`.
Migration `018_node_shape` is unchanged; only the string it stores gets longer. The plan's
"GPU shape disabled until one has rows" now has rows.

### 1.3 Load time stopped being recorded on 2026-09-22

`model-load-cost.md` derives a load as `wall_s - prefill_s - decode_s`. Since #159 (`765f95b`)
the worker loads the model in a separate warm step *before* the attempt starts, and logs it —
`job 104: qwen3-coder:30b resident at ctx 16384 in 4.3s` — but writes it nowhere. The attempts
are correspondingly clean: of d12's first 26 A/B attempts, **0** show a load by that formula,
against 22 loads in the journal. The same is true on sld-cloud from the #159 deploy on.

So Packet 5's load column, as specified, reads zero for every attempt after 2026-09-22 on both
nodes. The warm step's `took` needs a home before the page is built: a nullable
`attempts.load_s` (the warm belongs to the attempt that asked for it), set by `_warm_for`,
null for an attempt whose model was already resident. The column then reads `load_s` where it is
set and the old formula before #159, and says which.

## 2. The switch says who holds it, and a deploy's pause is protected

### 2.1 What happened

At 03:50 `update.sh` paused sld-cloud for a deploy and began the full re-render. At 04:29:26, with
the render at a few hundred of 1,198 pages, **Resume was pressed on the console** — the web log
has `POST /control 303` and the flash *Running — the worker claims jobs again*. Nothing on the
page said a deploy was running. The worker claimed A/B job 1373 and planned it at 100% of sixteen
cores beside the render; decode read 18.2 tok/s against the node's usual ~22. The operator paused
again at 04:34 once it was pointed out.

Had it run on, the deploy's last step restarts the worker unconditionally (`update.sh:259`). The
start-up sweep puts an orphaned job straight back on the queue (`worker.py:2007`), so nothing
would have been stuck — but the attempt in flight would have been thrown away, and with it an
A/B job's first attempt. Job 1373's timings are excluded from the comparison as it is.

### 2.2 What the control strip needs

`control` already has the fields: `state`, `reason`, `updated_utc`. The console shows the state
and, on the Console card, the reason. What it lacks is the holder's *liveness* and the Resume
button's awareness of it.

- **A deploy registers itself.** `update.sh` pauses with `--reason "update.sh deploy"` today. It
  should also write `meta.deploy` — pid, start time, the step it is on (the six `step` lines it
  already prints), and the render's `done/total` from the progress callback `publish-index`
  already has — and clear it in the same EXIT trap that resumes. The console reads it and the
  strip says *paused by update.sh (pid 629861) · re-rendering 331/1198 · 12 min*, not *PAUSED*.
- **Resume asks while a deploy is live.** `post_control` (`web.py:7312`) sets `running` with no
  check. With `meta.deploy` present and its pid alive, Resume becomes a confirm that names the
  deploy and what resuming costs: the jobs it starts share the CPU with the render, and the
  deploy's closing restart discards the attempt in flight. A stale `meta.deploy` (pid gone) is
  shown as *a deploy that did not finish* — which is what the first run that night was, killed
  with the laptop's ssh session and leaving the node paused — and Resume is then plain.
- **Every hand on the switch is named.** The console's pause writes reason `pause`, a CLI pause
  whatever `--reason` says, a deploy its own. Resume writes `NULL`. And `set_control` upserts a
  single row (`db.py`, `INSERT … ON CONFLICT(id) DO UPDATE`), so the last change erases the one
  before it: nothing can say who resumed at 04:29 except the web server's journal. Each change
  should also write an `activity` row — the table migration 008 added for the console card — with
  the source (console, CLI, deploy), and the strip shows the last three, so *who is working on
  what, when and why* has an answer for the switch itself.

This is the console's control strip rather than the Models page, but it is the same operator
question and the same packet of template work; it goes with Packet 4.

## 3. Batches are needed now, not at Packet 7

Packet 7 adds `jobs.batch` (migration `019_batch`) so an audition is its own population. The A/B
queued that night needed exactly that and did not have it: the batch is two job-id ranges on two
nodes and a sidecar file on each (`~/sketchgen/ab-2026-09-23.json`, harvest entry → job id),
written by a throwaway enqueue script. Every question about it — how far along, which entries
are in it, which pairs to show a judge — is a range query someone has to remember.

- **Pull `019_batch` forward to land with Packet 4**, and give `sketchgen enqueue` a
  `--batch TAG`. The A/B becomes `ab/hardware/2026-09-23` on both nodes, and `paid start` can take
  the same flag for the paid batches AGENTS.md already asks agents to keep apart.
- **The Queue shows a batch's progress** — queued, running, held, failed, per tag — and the
  Models page's *How they do* can filter to one. On two nodes the same tag is how the two halves
  of a comparison find each other, until the hub (`gallery-hub.md` Step 3) holds both.
- **The batch travels to the entry**, as Packet 7 says, so `pairs.py` can treat it as a
  population. Unchanged; only the timing moves.

## 4. Smaller, from the same night

- **Say why the idle loop is idle.** d12's worker logged `nothing to judge; nothing to critique`
  every 30 s for a day. The reason was structural: both steps draw only from `published` entries
  (`db.entries_to_critique`, `pairs.py:399`), and d12 had no gallery to publish to (it now has a
  private one, a bare repository beside its checkout). The Console card should give the
  precondition, not the outcome: *critique: 0 candidates — no published entries*.
- **Show the node's code state.** sld-cloud ran for hours on a feature branch holding an
  unpushed commit, and the first sign was `update.sh` failing on divergent branches. The header
  should carry branch, short commit, dirty, and ahead/behind `origin/main`; `paid preflight`
  already reads `node_commit`.
- **One row per node.** The operator watched the two consoles in two browser tabs over two
  tunnels. The hub is where the fleet view belongs; until then a strip in each console naming the
  other nodes it has been told about (state, current step, queue depth) would answer the glance.

## 5. What does not change

The key decision and Packets 1–3; `model-load-cost.md` in full, except that §1.3 here says where
its load number must now come from; the page's shape; and the order within Packets 4–7, except
that `019_batch` moves up to land with Packet 4.

## 6. Open

- Whether `size_vram` under-reports on CUDA the way `size` did on the CPU node for `gemma4:26b`.
- Whether d12 should run the planner on its CPU (64 GB RAM, 16 threads idle at 4%) so the
  executor stays resident in VRAM — an Ollama per-model setting, and a node configuration rather
  than a page question, but the page is where the reload count that argues for it would show.
- How `meta.deploy` behaves when two deploys overlap, which `update.sh` does not prevent today.
