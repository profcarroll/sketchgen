# The A10 week: spending the trial's last credit on one measurement

Written 2026-09-23. OCI approved one `VM.GPU.A10.1` the same week the first
D12 lab box (`d12-node-profcarroll`, RTX 5070 Ti 16 GB) started decoding at
about 100 tok/s. The trial credit runs out on 30 September. This plan spends
what is left on the one thing the lab cannot tell us, and ends with the
instance terminated and its data on the laptop. Setup and teardown are in
`docs/OPERATIONS.md` → *A rented GPU node*.

## 1. The question

The lab will be built from desktop cards. d12's card is fast (896 GB/s) but
16 GB, and the executor is 19 GB: about a quarter of it runs on the CPU, and
planner plus executor (28 GB) cannot be resident together, so every job
reloads (docs/plans/models-console-two-nodes.md §1). The A10 is the opposite:
slower memory (600 GB/s), but 24 GB, so the executor is resident whole.

**What does whole residency buy, against raw bandwidth?** That decides whether
the next boxes want a bigger card or just more of the same one, and it is the
only thing here the lab cannot measure for itself. Everything else in the week
is either the control that makes that answer trustworthy, or production that
turns spare hours into entries.

## 2. The budget

| | |
|---|---|
| trial credit, 2026-09-23 | $300 − $38.72 spent ≈ **$261** |
| sld-cloud 16/96 to 30 Sep, $5.47/day | − $38 |
| reserve: the 14–17 Sep charges Oracle may backfill (~$22) and a day of reporting lag | − $30 |
| **for the A10** at $2.00/h | **≈ $190, about 95 hours** |

The account is pay-as-you-go (confirmed 2026-09-23), so going over costs card
money rather than getting the node reclaimed — the reserve is there to keep
it a small amount, not to prevent an outage. Two OCI Budget alerts, at $240
and $255 of the month, are the tripwire; they are console settings, set by
hand.

**The window: launch Wed 24 Sep about noon, terminate Sun 28 Sep about noon.**
Continuous, because a stopped GPU VM can fail to restart for capacity. Leaving
Monday and Tuesday unrented is the room for Oracle's lag to post before the
month closes.

## 3. The runs, in order

Each layer is cheaper and more comparable than the one after it, so if the
week is cut short, what is already done still stands.

**L0 — `sketchgen bench` on all three nodes (about 1 h of A10).** Fixed
prompts at about 1k, 4k and 12k tokens, 512 out, three runs each, at the
pipeline's context sizes, after a cold load; Ollama's own counters, and where
the model landed (`size_vram`, `nvidia-smi`). Matched on digest, so d12's
`qwen3-coder:30b` and sld-cloud's `qwen3-coder:30b-a3b-q4_K_M` line up.
sld-cloud benches after the B arm (jobs 1373–1472) is done — the bench
refuses while the generator runs — and d12 between its own runs.
`bench --compare` on the laptop makes the table.

**L1 — arm C of the hardware A/B (about 6 h).** The same 100 harvest prompts
as d12's jobs 102–201 and sld-cloud's 1373–1472, at the same build (a72f076)
and the same settings: gemma4:e4b planner, treatment rules, three attempts,
seed 1, held. Then:

- **C′**, the same 100 again, for determinism on this machine (d12's A vs A′
  is the same check there);
- **the rules-control 100**, matching d12's jobs 202–301.

The frame budget is wall-clock on each node's CPU, and this one is a
different CPU again (Intel, 15 cores). Re-gate every arm's first attempts on
one node before reading gate outcomes, as the A/B already plans, so the
comparison is of the artists and not the referees.

**L2 — what only 24 GB can do (about 12 h), one change at a time from L1's
settings, each benched and then given the harvest's first 30 prompts:**

1. **Planner on the CPU** (240 GB RAM idle): Ollama `num_gpu 0` for
   gemma4:e4b, so the executor never leaves the card. The same setting is
   open for d12 (models-console-two-nodes.md, open questions); this measures
   whether it is worth doing there.
2. **A bigger executor quant** that fits in 24 and not in 16 (q5_K_M or q6_K
   of the same model, about 21–25 GB). Does quality move, and what does it
   cost in speed?
3. **`OLLAMA_NUM_PARALLEL=2` and `4`**, `bench --parallel`: sketches an hour
   rather than tokens a second. The number that sizes a cluster.

**L3 — production for the rest (about 70 h).** The worker runs L1's
settings on the private gallery until Sunday. Every hour becomes held
entries, and critique children once some are published there, rather than an
idle card.

## 4. What comes out

- `bench-*.json` from three nodes and the compare table: load, prefill,
  decode, GPU residency per model.
- Arm C and C′ beside A, A′ and B: time per job, attempts, gate outcomes
  after re-gating, and blind matched pairs.
- **Cost per sketch** on each: the A10 at $2/h (the Node card prices it
  correctly once `SKETCHGEN_RATE_PER_HOUR=2.00` is set), sld-cloud at its A1
  rate, the lab at its electricity. That is the column that makes the case
  for the lab, or does not.

## 5. After 30 September

sld-cloud stays at 16/96 through October on the card (about $166) and is
resized to 4/24 — the Always Free allowance — before 1 November
(OPERATIONS.md → *The shrink to 4/24*). By then the D12 lab should carry the
generation, and this week's numbers are what it is built from.
