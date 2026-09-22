# What a model costs to load

An amendment to `docs/plans/models-console.md`, Packets 4, 5 and 6. The plan is sound and its key
decision — keys stay on the laptop — is untouched by anything here. What this document changes is
narrower and entirely about the page: **the Models page as specified measures a model by the two
numbers that do not separate models on this node, and omits the one that does.**

Grounded in measurements taken 2026-09-22 while diagnosing a throughput complaint. The same
session produced `docs/plans/child-source-num-ctx.md`, which reverses `DECIDE[num-ctx]`; this
document is the other half of what that investigation turned up, and it is about the page rather
than the pipeline.

No packet of its own. These are edits to three packets that have not been built yet, so they are
cheaper to make now than after the template exists.

## 0. What the plan says today

| where | what it says |
| --- | --- |
| `models-console.md:231` | the fit-together bar is *"the assigned pair's `size_bytes` against the node's RAM, amber when the two do not fit beside each other"* |
| `models-console.md:254` | Packet 5's per-model stats are *"n, first-pass rate, mean attempts, wall, tokens per second …"* |
| `models-console.md:271–272` | pull fit is *"weights plus the assigned pair against RAM"*, with *"a decode estimate from the node's measured tok/s on models of the same active-parameter count"* |
| `models-console.md:276` | a pull *"is a download on a sixteen-core box and has never been seen to slow a step"* |
| `models-console.md:141` (§1.10) | every number on the page is read from Ollama, the database, or `meta` |

§1.10 is the constraint that makes all of this cheap: **the missing number is already in the
database.** Load time is `wall_s - prefill_s - decode_s` on an `attempts` row. Nothing below asks
for a new collector.

## 1. Tokens per second cannot rank models on this node

Ollama derives its rates as `eval_count / eval_duration` and **excludes `load_duration` from both
denominators**. So tok/s is the speed of a model that is already resident, and says nothing about
what it cost to get there. On a CPU-only node that is most of the bill.

Every local model that has run attempts with timings:

| model | n | cold loads | mean cold load | worst | mean tok/s | share of wall spent loading |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen3-coder:30b-a3b-q4_K_M` | 2040 | 975 | 29.7 s | 229.4 s | 20.6 | **17.7%** |
| `laguna-xs-2.1` | 107 | 34 | 74.4 s | 275.7 s | 19.5 | **23.0%** |
| `gemma4:26b` | 5 | 4 | 30.0 s | 31.6 s | 15.4 | 13.3% |
| `qwen3.5:9b` † | 30 | 0 | — | 0.2 s | 12.5 | 0.1% |

Read the first two rows: `qwen3-coder` and `laguna-xs-2.1` are **5% apart on tok/s and 2.5× apart
on cold load**. A page that ranks by tok/s calls them equivalent. They are not. That contrast is
the whole of the argument and it does not need the other rows.

† The `qwen3.5:9b` row **is not evidence that the model is cheap to load**, and it was nearly
quoted as if it were. All 30 attempts ran on 2026-09-19/20, the node's cheapest stretch (7% cold
in the day table below), with the model holding a slot across consecutive jobs. Measured directly
on 2026-09-22, a cold `qwen3.5:9b` cost **89.5 s** — about what 6.6 GB comes to at the repack rate
in §1. The zeros were a warm runner, not a cheap model, which is the mundane answer and the one
that should have been assumed. Nothing in this document is argued from that row.

And the rate is not a fixed property of the node. It is a property of its *configuration*, and it
has swung by an order of magnitude in eight days:

| day | attempts | cold loads | % cold | worst load |
| --- | --- | --- | --- | --- |
| 2026-09-14 | 221 | 166 | 75% | 40.4 s |
| 2026-09-15 | 276 | 188 | 68% | 33.6 s |
| 2026-09-16 | 362 | 204 | 56% | 34.7 s |
| 2026-09-17 | 320 | 209 | 65% | 36.9 s |
| **2026-09-18** | 312 | 47 | **15%** | 39.7 s |
| 2026-09-19 | 188 | 13 | **7%** | 229.4 s |
| 2026-09-20 | 314 | 103 | 33% | 229.8 s |
| 2026-09-21 | 130 | 52 | 40% | 104.9 s |
| 2026-09-22 | 59 | 31 | 53% | 275.7 s |

The step down on 09-18 is `OLLAMA_MAX_LOADED_MODELS` going from 1 to 2. The climb back from 7% to
53% over the four days after is the rotation growing to four models against those same two slots,
and the worst-case load tripling is `laguna-xs-2.1`'s 19 GB arriving in it. Both were invisible
to everyone until someone went looking, because **no page shows this number** — and tok/s over
the same nine days barely moves.

This is the argument for the column. A model's cost on this node is not a constant the catalogue
can state; it is a function of what else is assigned and how many slots there are, and it is the
first thing to look at when the queue slows down.

The reason it costs what it does is that a load here is not a file read. Ollama logs

```
msg="disabling mmap for llama-server load by default" ... reason=cpu
```

and llama.cpp then repacks the weights for ARM:

```
load_tensors:   CPU_REPACK model buffer size = 19142.57 MiB
14:17:09 → 14:19:28   = 139 s
```

139 seconds of all sixteen cores, disk idle. It is CPU work proportional to the weights, paid in
full every time a runner is replaced — and *invisible in every rate the page plans to show*.

**Packet 5 should carry load as a first-class column**, per (model, node shape): number of loads,
mean, worst, and share of wall time. Migration `018_node_shape` is already the right key —
repack cost is a property of model × shape, which is exactly what that column exists to express,
and a GPU shape would drive it to near zero and show that as a column rather than a rewrite.

## 2. The footprint numbers the page would read are wrong

Packet 4's fit bar reads `size_bytes` from the catalogue; the resident state comes from
`/api/ps`. Both were measured against real process RSS with three models resident:

| model | `/api/tags` `size_bytes` | `/api/ps` `size` | actual RSS |
| --- | --- | --- | --- |
| `gemma4:e4b` | 8.95 GiB | 8.92 GiB | **6.81 GiB** |
| `qwen3-coder:30b-a3b` | 17.28 GiB | 18.98 GiB | 19.42 GiB |
| `gemma4:26b` | 17.33 GiB | **1.34 GiB** | **19.46 GiB** |
| **total** | 43.56 GiB | 29.24 GiB | **45.69 GiB** |

Two distinct problems, and the second is the dangerous one:

1. **`size_bytes` is the blob, not the footprint.** mmap is disabled on CPU, so weights are
   anonymous RSS rather than reclaimable page cache. The totals are close here (43.6 vs 45.7) but
   the per-model error runs **both ways** — `gemma4:e4b` over-reports by 2.1 GiB while
   `qwen3-coder` under-reports by 2.1 GiB — so a flat safety margin does not fix it.
2. **`/api/ps` reported `gemma4:26b` at 1.34 GiB against 19.46 GiB resident — 7% of its true
   footprint.** That model is the one carrying a `--mmproj` and a `--spec-draft-model`; the other
   two rows are close enough. The cause is not confirmed and should not be guessed at in the
   template. What matters for the design is that the total from `/api/ps` understates reality by
   **16.5 GiB** — comfortably enough to green-light a model that will not fit.

Reproduced across a one-hour window and an ollama restart, so it is not a transient.

**Packet 4 should not compute fit from `size_bytes` or `/api/ps size` alone.** The honest
number is resident RSS, which the console's node collector can already read from `/proc`, with
the catalogue's `size_bytes` used only as the estimate for a model that has never been loaded —
and labelled an estimate, the way Packet 6 already labels its decode guess.

## 3. The resident set is not a pair

Packets 4 and 6 both size the fit against *"the assigned pair"*. That was true when the rotation
was two models. It is four now — `gemma4:e4b` (judge, critic, lineage), `gemma4:26b` (planner),
and two executor A/B arms — and the ceiling is `OLLAMA_MAX_LOADED_MODELS`, raised from 2 to 3 on
2026-09-22 because one job's working set is planner + executor + judge.

So the quantity the bar must not exceed is **the resident set bounded by
`OLLAMA_MAX_LOADED_MODELS`, not the assigned pair.** With three resident the node currently sits
at 45.69 GiB of 94 GB; four of the larger models would be roughly 70 GB, which is why the slot
count is 3 and not 4. The bar should read that ceiling from the environment rather than assume a
pair, and amber should mean *this assignment cannot be co-resident with the others*, which is a
statement about the slot count and not about arithmetic on two numbers.

## 4. `num_ctx` belongs on the assignments panel

Ollama keys a loaded runner on its context size, so a model asked for a different `num_ctx` is
torn down and reloaded — 7 times out of 7 in the measurements behind
`docs/plans/child-source-num-ctx.md`, at a mean of 64.9 s. The executor's two windows have since
been collapsed, but one case survives: **`gemma4:26b` serves the planner at 8,192 and
occasionally the executor at 16,384**, one model at two windows.

Packet 4's assignments panel has a select per step for the *model* and no field for the
*window*, so the page as specified can neither show that collision nor prevent it. It should show
the window beside each step, and amber the pair when one model is assigned to two steps at two
windows — that is a reload per flip, and it is the single remaining instance of a cost this
project has already decided it does not want to pay.

## 5. Packet 6, in passing

Two smaller notes, neither blocking:

- **The decode estimate should be paired with a load estimate.** Packet 6 estimates tok/s from
  active-parameter count, which is the right method for the wrong term: on this node load
  dominates, and unlike decode it is *predictable from weight size*, because repack is
  proportional to the bytes. A model's first-load cost can be estimated before it is pulled with
  considerably more confidence than its decode rate.
- **"Has never been seen to slow a step" is worth re-testing rather than asserted.** A pull is
  I/O and a step is CPU, so the claim is probably still true. But `llama-server` takes 15.87 of
  16 cores during generation, and the node's inference baseline already records the Chromium
  gate, `sketchgen-sync` and the nightly backup stealing cores directly from inference. The
  "sixteen-core box" framing is the part the measurements complicate; the conclusion may well
  survive it.

## 6. Packet 7: an audition is where this cost distorts most

`audition` queues N jobs on a candidate model, and it is the one place on the page where load
cost does not merely go unreported but actively **biases the result against the candidate**.

Measured on job 1366, an audition of `qwen3.5:9b` against the resident `qwen3-coder:30b-a3b`:

- The challenger paid **89.5 s** to load on its first attempt. The incumbent had been paying
  **0.1 s** on the jobs either side of it, because it was resident.
- Compared on wall time the challenger therefore looks about 90 s a job worse than it is. Some of
  the gap is real — 12.6 against 20.6 tok/s — but the load is not part of what an audition is
  trying to measure, and on a first attempt it is the larger term.
- The audition also **evicted both executor arms**, so the next job needing either paid 30–276 s
  to bring it back. An audition bills the queue, not only itself.

So Packet 7 should report a candidate's attempts with load **separated**, not folded into wall,
and the detail page should say plainly how many of the audition's attempts paid a cold load. A
fair trial of a model is a trial of the sketches it writes, not of whether it happened to be
resident. The same column §1 asks for answers this; what Packet 7 adds is that here it is not a
diagnostic nicety but a correctness question about the comparison.

## 7. What does not change

1. **The key decision, and Packets 1–3.** Nothing here touches what syncs, where the answerer
   runs, or §1.7's fence. This amendment does not reach the paid path at all.
2. **§1.10's rule.** Every number proposed above is read from `/proc`, Ollama, or the `attempts`
   table, and every write still calls the function a CLI verb calls. No new privilege surface.
3. **Packet 7, and the migrations.** `018_node_shape` becomes *more* load-bearing under §1, not
   less; `019_batch` is untouched.
4. **The page's shape.** Assignments, catalogue, off-node, housekeeping. These are columns and a
   corrected denominator, not a redesign.

## 8. Open

- **Why `/api/ps` under-reports `gemma4:26b` by 14×.** Worth ten minutes before Packet 4 reads
  that field for anything load-bearing. The workaround (read RSS) does not depend on the answer.
- ~~Why `qwen3.5:9b`'s 30 attempts all report a near-zero load.~~ **Answered 2026-09-22**: a cold
  `qwen3.5:9b` costs **89.5 s** (job 1366, attempt 1), so the model is not cheap to load and the
  zeros were a warm runner holding a slot across consecutive jobs on the node's 7%-cold day. One
  detail is still unaccounted for and is not worth chasing: a 0.08 s load at 06:02 on 09-20 after a
  six-hour gap in that model's own attempts, against a 30 m `KEEP_ALIVE` — most likely other jobs
  kept the runner in its slot. It changes nothing above.
- **Whether a per-model mean is meaningful at all across a configuration change.** The day table
  says a model's load cost moved by 10× without the model changing. Packet 5 plans to key stats on
  (model, step, prompt version, node shape) — none of which captures slot count or keep-alive. A
  mean over 09-14 to 09-22 would blend a 75%-cold regime with a 7%-cold one. This may want a
  fifth key, or a date cut, and it is the one thing here that could change the migration.
