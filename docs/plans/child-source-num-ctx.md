# One context, not two

An amendment to `docs/plans/child-source.md` `DECIDE[num-ctx]`. That decision — *16,384 when a
source is included, 8,192 otherwise* — is reversed: **every executor call goes out at 16,384.**
The two context sizes were a reasonable trade when the plan was written, but the thing they were
trading against was not measured, and when it was, it turned out to cost between 65 and 275
seconds of a pinned node per flip.

**This document records a change that is already live**, not one to be scheduled. It was applied
on the node on 2026-09-22 at the instructor's go-ahead while diagnosing a throughput complaint,
and the plan is being brought into line after the fact rather than before. The ordering is worth
stating plainly: the measurement came out of an incident, and the decision was taken in the same
session.

No packet, no branch of its own — the code change is two constants and their comments. What this
document owes the repository is the evidence, because `DECIDE[num-ctx]` is well argued and a
reader who finds the code disagreeing with it deserves to know why rather than assume drift.

## 0. What the repository says today

| where | what it says |
| --- | --- |
| `docs/plans/child-source.md:44` | `DECIDE[num-ctx]`: *16,384 when a source is included, 8,192 otherwise* |
| `docs/plans/child-source.md:238` | `MEASURE[source-ctx-cost]`: the executor's decode time and the node's memory at the larger window |
| `sketchgen/executor.py:87` | `DEFAULT_NUM_CTX` — was `8192`, now `16384` |
| `sketchgen/worker.py:351` | `SOURCE_NUM_CTX = 16384`, unchanged |
| `sketchgen/worker.py:3101` | `num_ctx = SOURCE_NUM_CTX if (given and given.shown) else executor.DEFAULT_NUM_CTX` — unchanged, and now yields the same value on both branches |

The ternary is deliberately left standing. Both names still describe real budgets, and keeping
the branch makes this reversible by editing one integer. A future reader who wants the split back
should read §2 first.

## 1. Why it is reversed: a context change reloads the model

The plan costed the larger window as *"a KV cache twice the size for the executor's decode"*. It
did not cost what happens at the moment the window **changes**, which is the more expensive
event: Ollama keys a loaded runner on its context size, so the same model asked for a different
`num_ctx` is torn down and loaded again.

On this node a load is not a file read. Ollama logs

```
msg="disabling mmap for llama-server load by default" ... reason=cpu
```

so the weights are read into anonymous memory and then **repacked for ARM**. For
`laguna-xs-2.1` that step is

```
load_tensors:   CPU_REPACK model buffer size = 19142.57 MiB
14:17:09 → 14:19:28   = 139 s
```

139 seconds of all sixteen cores at zero tokens per second, with the disk idle. It is CPU, not
I/O, and it is paid in full every time a runner is replaced.

Measured off the `attempts` table, where `wall_s - prefill_s - decode_s` is the load and ~0.2 s
means the runner was reused:

| transition | n | cold loads | mean load |
| --- | --- | --- | --- |
| model changed | 10 | 9 | 91.1 s |
| **same model, `num_ctx` changed** | 7 | **7** | **64.9 s** |
| same model, same `num_ctx` | 23 | 6 | 8.0 s |

A context flip forced a reload **seven times out of seven**. Job 1344 paid it twice in a row:
attempt 1 at 8,192 (275.7 s) and attempt 2 at 16,384 (152.4 s), same model both times. Across the
300 attempts before the change, loading was 84.5 of 463 wall-minutes — **18%** — and for
`laguna-xs-2.1` alone, 19 cold loads in 38 attempts at a mean of 79.3 s and a worst case of
275.7 s, which is 35% of that model's wall time.

The flip is not rare by accident. `worker.py:3101` selects the window from whether the attempt
holds a source, which varies per job and per attempt within a job, so the executor's runner was
being rebuilt on a coin toss.

## 2. What it costs: `MEASURE[source-ctx-cost]`

Honest accounting, because this is the part of `DECIDE[num-ctx]` that the reversal takes away.

**The measurement cannot continue as written.** It wanted decode time and node memory compared
across the two windows. Historical rows survive — 7 attempts at 8,192 and 37 at 16,384 — but the
8,192 arm stops growing, so the comparison is now closed rather than open.

**It was already confounded, and what it did show does not support the split.** The two arms
differ in prompt length as much as in window:

| `num_ctx` | n | mean prompt tokens | mean decode | predicted by the node's baseline |
| --- | --- | --- | --- | --- |
| 8,192 | 7 | 2,177 | 20.5 tok/s | 19.9 tok/s |
| 16,384 | 37 | 3,534 | 16.9 tok/s | 14.5 tok/s |

The node's measured baseline is `decode = 28.60 − 3.99 tok/s per 1000 prompt tokens` (1,249
attempts, 2026-09-18). Both arms land **at or above** what prompt length alone predicts. The
decode penalty the split was hedging against does not appear in the data: a decode step attends
over the tokens actually present, not over the allocated window, so doubling the allocation
changes occupancy not at all.

**The memory cost is real but small.** At 16,384 the executor's KV buffers are 640 MiB + 180 MiB,
against roughly half that at 8,192 — about 400 MiB more per resident executor, on a box with
94 GB. Set beside a 19 GB repack, it is not the term that matters.

So the trade `DECIDE[num-ctx]` made was a reload against a KV cache. The reload costs 65–275 s of
the whole node; the KV cache costs 400 MB and no measurable decode rate. That is the reversal.

## 3. What does not change

1. **The reason the bigger window exists.** `SOURCE_NUM_CTX`'s rationale is untouched and still
   correct: 2k in and 3k out against 8,192 leaves a 3k-token parent sketch no room for attempt
   3's accumulated evidence, and a prompt that overflows `num_ctx` loses its head — the rules
   file and the brief. This amendment gives *every* attempt that headroom; it does not decide the
   headroom was unnecessary.
2. **The A/B on the rules file.** `rules_file` is drawn independently of the context window.
   Collapsing the window removes a confound from that experiment rather than adding one — the
   control and treatment arms are no longer split across two runner configurations.
3. **The planner, judge, critic and lineage** stay at 8,192 (`planner.py:429`, `judge.py:429`
   and `:561`, `lineage.py:841`). They are normally distinct models from the executor, so raising
   them would buy KV cache and no fewer reloads. See §5 for the one case where that is not true.
4. **Everything `child-source.md` decided other than the window.** Threading `num_ctx` from
   `worker.default_executor` to `executor.run` was the packet's real work and it stands; this
   amendment only collapses the two values it threads.

## 4. The work

Done, on the node, uncommitted at the time of writing:

- `sketchgen/executor.py:87` — `DEFAULT_NUM_CTX` `8192` → `16384`, with a comment recording the
  repack cost so the next reader does not re-split it.
- `sketchgen/worker.py:342–351` — `SOURCE_NUM_CTX`'s comment notes the two are now equal and why.

Verified: 1,777 of 1,782 tests pass. The 5 failures are environment, not logic, and are identical
on a tree with only the constant reverted — four are one inherited `test_paid` method asserting a
CLI exit of 3, and the fifth is `test_gate_fixtures`, where all 11 fixtures fail with
`playwright is not importable` because `gate/accept.sh` shells out to a python without it.

Alongside it, in `/etc/systemd/system/ollama.service.d/override.conf`:
`OLLAMA_MAX_LOADED_MODELS` 2 → 3 and `OLLAMA_KEEP_ALIVE` 30m → 4h, with the stale comment block
rewritten. Those are node configuration, not repository state, and are recorded here only because
they were applied in the same change and the acceptance below cannot be read without them.

## 5. Risks, and the one flip that survives

- **`gemma4:26b` serves two steps at two windows.** It is the planner (8,192) and occasionally
  the executor (16,384) — 4 of the last 300 attempts — so that one model can still flip its own
  context and reload. Rare, and left alone deliberately rather than by oversight. Collapsing the
  planner to 16,384 would close it; that is a decision about the planner's budget, not this one.
- **Four models through three slots.** `MAX_LOADED_MODELS=3` matches one job's working set
  (planner + executor + judge), but the executor A/B means four models rotate. Model-change
  reloads (91.1 s mean) are not addressed by this amendment at all.
- **The measurement is closed, not answered.** If the larger window ever does look expensive, the
  8,192 arm has to be recreated deliberately — and §1's table is the reason to do that on a
  scratch model rather than in the live queue.

## 6. Acceptance

1. Two attempts on the same job and model report a load of ~0.2 s on the second, not 65 s or
   more. This is the whole point; if it fails, nothing else here matters.
2. `attempts.num_ctx` is 16,384 for every executor row written after the change, including first
   attempts with no source.
3. Decode stays at or above `28.60 − 3.99 × (prompt_tokens / 1000)` tok/s. A drop below it is the
   signal that the larger window costs something the 44 historical rows could not see.
4. The full suite passes with the same 5 pre-existing environment failures and no new ones.
