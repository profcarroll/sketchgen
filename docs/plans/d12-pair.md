# The D12 pair: one wire between two boxes

Written 2026-09-25. `d12-node-profcarroll` and `d12-node-flux` were moved onto the same wired
network on 2026-09-24: a residential Linksys WiFi 5 router that also serves `d12-kiosk`. Dave
asked for two things. The first is the fastest link that can be put between the pair. The
second is whether the pair can work as one node. This plan answers the first. It gets there
through a bench of the second, because the right link depends on what the pair would send over
it. Flux is the primary in whatever dual-node mode follows (Dave, 2026-09-24).

The short version:

- Pooled over llama.cpp RPC, the two 16 GB cards generate at about twice the speed of either
  card alone.
- 1 GbE is not the bottleneck for that. Generation sends about 28 KB per token over the link.
- The link still matters for three things: prefill, cold loads, and isolating an RPC server
  that has no authentication.
- Recommended link: **one USB4 cable between the two boards**, with Ethernet left on the router.
  **Fallback**: a switch that really does 5GBASE-T.

## 0. What is there today

Read on 2026-09-24 and 25, with nothing changed on either box:

| | d12-node-profcarroll | d12-node-flux |
| --- | --- | --- |
| board | MSI MAG X870 TOMAHAWK WIFI (MS-7E51) | the same |
| CPU / RAM | Ryzen 7 9800X3D / 64 GB | the same |
| GPU | RTX 5070 Ti 16 GB | RTX 5080 16 GB |
| wired NIC | Realtek RTL8126 5GbE, **linked at 1000 Mb/s** | the same |
| USB4 | ASMedia ASM4242 (`1b21:2425`), two rear ports; the kernel has `domain0` with `security=user`, and `thunderbolt_net` is present | the same |
| LAN address | 192.168.1.133 | 192.168.1.146 |
| kernel | 7.0.0-34-generic | the same |

- **Link speed.** Both 5GbE NICs negotiate 1000 Mb/s because the Linksys ports are gigabit.
  Measured with a stdlib TCP test from d12 to flux, 1 GiB each way:
  - **0.94 Gb/s** over the LAN
  - **0.89 Gb/s** over Tailscale, which takes the direct LAN path between the two
- **Round trip.** 0.1–1.0 ms on the LAN (0.2–0.7 ms on average) and 0.5–1.5 ms over Tailscale.
- **Missing tools.** Neither box has a compiler or `iperf3`, and sudo needs a password on both.

## 1. What the pair did when pooled

**Setup:**
- **Build.** llama.cpp build b11173, `ubuntu-cuda-13.4-x64` plus its `cudart` bundle, from the
  project's own GitHub releases, with the sha256 checked. The release is built with
  `-DGGML_RPC=ON`. It runs on the boxes' driver (595.91.07, CUDA 13.2) and sees both cards. It is
  unpacked under `~/llama.cpp/` on both boxes, owned by the user; nothing is installed
  system-wide. Ollama's own `llama-server` has no RPC backend.
- **Servers.** `ggml-rpc-server -c` on d12, and `llama-server --rpc` on flux.
- **Model.** The same GGUF that Ollama serves as `qwen3-coder:30b`, read straight from Ollama's
  blob store.
- **Prompts.** `sketchgen bench`'s own three prompts:
  - 1.2k, 5k and 15k tokens in, 512 out
  - seed 1, a 16k context
  - numbers read from llama-server's timings
- **Nodes.** Both generators were paused from 01:01Z to 01:37Z.

**How flux reached d12.** The RPC server was bound to d12's `127.0.0.1` and reached through an
SSH tunnel from flux over the LAN. This was not the first choice. Binding it to 192.168.1.133
was refused by Claude Code's auto-mode classifier. The refusal was correct: `ggml-rpc-server`
has no authentication, and anything on the Linksys WiFi could have driven d12's GPU through it.
Every pooled number below therefore includes the tunnel's own cost, which was not measured
separately.

**Decode tok/s, with prefill tok/s in brackets:**

| configuration | short (1.2k) | rules (5k) | source (15k) | load |
| --- | --- | --- | --- | --- |
| Ollama, flux alone, 74% on GPU (`sketchgen bench`) | 99 (1406) | 92 (1786) | 86 (1810) | |
| llama.cpp, flux alone, `--fit` | 107 (1398) | 99 (1780) | 92 (1805) | |
| **pooled, d12's card first** | **215 (2678)** | **181 (2611)** | **155 (2030)** | 91 s cold, **14.6 s** warm |
| pooled, flux's card first | 101 (4332) | 93 (5985) | 86 (4382) | 87 s |
| pooled, d12 first, `-b 2048 -ub 2048` | 214 (3078) | 181 (3022) | 154 (2249) | 17.8 s |
| llama.cpp, flux alone, `-b 2048 -ub 2048` | 103 (3144) | 96 (3995) | 89 (4010) | |
| Q5_K_S (21 GB), flux alone | 81 (996) | 77 (1252) | 73 (1272) | |
| **Q5_K_S pooled, d12 first** | **202 (2684)** | **172 (2621)** | **148 (2032)** | 116 s cold |

**Real prompts.** Two executor prompts were replayed from flux's own jobs. Output was forced to
the original attempt's length (`ignore_eos`), so every run generates the same number of tokens.

| attempt | prompt tokens | output tokens | Ollama, originally | llama.cpp, flux alone | pooled |
| --- | --- | --- | --- | --- | --- |
| job 470, attempt 1 (median size) | 2391 | 1001 | 11.8 s | 11.0 s | **5.9 s** |
| job 72, attempt 3 | 2204 | 1108 | 13.0 s | 12.3 s | **6.8 s** |

**Where the models sit.** With the executor pooled, each card holds about 10 GB: flux 9.9 GB,
d12 10.4 GB. `gemma4:e4b`, loaded through flux's Ollama beside it, is **100% on GPU**:
- flux then uses 13.0 GB in all
- the planner decodes at 171 tok/s

Nothing would ever evict anything. Today d12 reloads its executor on every job, 4.3 s each time
(`models-console-two-nodes.md` §1.1).

**What that is worth per job.** Flux's 816 `qwen3-coder:30b` attempts have:
- a median of 2.4k tokens in and 950 out
- 90% of their model wall time spent decoding

So a pooled attempt takes about half the time. Job 470 as it ran on flux alone:

| step | flux alone | pooled (estimated) |
| --- | --- | --- |
| plan | 8 s | 8 s |
| executor load | 4 s | — |
| writing, three attempts | 12 + 10 + 11 s | about 17 s |
| three gates | 13 + 2 + 2 s | unchanged |
| **whole job** | **62 s** | **about 42 s** |

### What crosses the wire

Counted on flux's `enp7s0` around single requests, against an idle baseline of 4 KB in 3 s, and
checked against `GGML_RPC_DEBUG` on d12:

- **Generating (decode):** about **9 KB per token from d12 and 19 KB to it**. The RPC trace
  shows why. Each token carries:
  - in: an 8 KB embedding, positions and a mask
  - out: an 8 KB hidden state
  - the graph itself is reused, not re-sent

  At 215 tok/s that is about 50 Mb/s, **5% of the link**. Decode is bound by round trips, not
  by bandwidth.
- **Prefill:** about **15 KB to d12 and 9 KB back per prompt token**, about 0.5 Gb/s at 2.6k
  tok/s. That is 12 MB per 512-token batch: about 105 ms at 0.94 Gb/s, against a measured 197 ms
  per batch. So if transfer does not overlap compute, about half of pooled prefill is the wire.
  The flux-first row points the same way: it sends one crossing per batch instead of two, and
  prefills 1.6–2.3× faster.
- **Cold load:** d12's half of the model, about 9 GB, crosses once: **91 s**. `-c` keeps it in
  d12's cache, and every later load of the same split takes **14.6 s**.

### Two traps, both measured

1. **The layer order decides decode speed.** Use `-dev RPC0,CUDA0`: d12's layers first, so the
   output layer is on flux.
   - The other order puts the output layer on d12, and every token's logits cross back to flux:
     151,936 × f32 = 608 KB.
   - At 0.94 Gb/s that is 5.2 ms a token, on top of about 4.7 ms of compute. That predicts
     101 tok/s, which is exactly what the flux-first row shows.
2. **llama-server's host prompt cache must be off.** `--cache-ram` defaults to 8 GiB. Between
   requests it saves the slot's KV cache to flux's RAM, and for d12's layers that means reading
   it across the link: 48 KB per context token.
   - Measured: 282 MB and 400 MB crossed between two requests.
   - With `--cache-ram 0` the same pair of requests moved 15 MB.
   - The rates in the tables are llama-server's own timings, which exclude this. The wall times
     in the replay were taken with it off.

## 2. What the link is for

Pooling works on today's gigabit link, and at the pipeline's prompt sizes a faster link would
not change the result much:

| | on 1 GbE today | on a 10–25 Gb/s link |
| --- | --- | --- |
| decode | bound by round trips, not bytes | a faster link helps only through latency; expect little |
| prefill | about half of it may be the wire (§1) | should approach the flux-first order's 4–6k tok/s |
| median attempt (2.4k in) | about 0.9 s of prefill in about 6 s | saves about 0.45 s: **about 7%** |
| cold load | 91 s | about 10 s |

The cold load happens only when the pair restarts or switches model: the warm cache already
makes a load 14.6 s. The 10–25 Gb/s range is from others' reports, not measured here.

The reason to build the link anyway is **isolation**. The RPC server has no authentication:
- **On the Linksys LAN,** it is reachable from the kiosk and from whoever joins the WiFi.
- **Behind an SSH tunnel,** it is safe, but pays the tunnel's cost on every round trip, and that
  cost is unmeasured.
- **On a point-to-point cable,** the only other endpoint is the other box.

The link also makes moving a 19 GB model between the boxes seconds instead of minutes.

## 3. The recommendation: one USB4 cable

Connect **one certified 40 Gb/s USB4 or Thunderbolt 4 cable** between a rear USB4 port on each
board, and run Linux's `thunderbolt-net` over it. Leave each box's Ethernet in the Linksys for
the internet, Tailscale and the kiosk. This needs no new card and no free PCIe slot. A passive
cable reaches about 0.8 m; an active TB4 cable reaches 2 m.

Others report 9–11 Gb/s between two Strix Halo desktops and about 26 Gb/s between NUC 13s,
against 0.94 today. It is point to point, so nothing else is on the wire. Each board has two
ports, so a third box could join as a ring.

**The known risk.** An upstream change in 2026 enabled end-to-end flow control on transmit
("net: thunderbolt: Enable end-to-end flow control also in transmit").
- **What broke.** On ASM4242-to-ASM4242 links it left the Tx ring stalled, so `thunderbolt0`
  cycled with 100% packet loss.
- **The fix.** A revert, 1881f2efbf7f.
- **What didn't help.** `thunderbolt_net e2e=0` was reported not to help.
- **Our kernel.** Whether Ubuntu's 7.0.0-34 carries the bad change is not recorded anywhere on
  the box: the signed package's changelog lists no kernel commits. Ten minutes with the cable
  will tell.

**Setup, by hand, with sudo on both boxes:**

1. Plug the cable in. `ip -br link | grep thunderbolt` should show `thunderbolt0` on both boxes,
   and `dmesg | grep -i thunderbolt` should show the host-to-host (XDomain) connection. A host
   link should need no `boltctl` authorisation; that is untested here.
2. Give it a /30 of its own, outside 192.168.1.0/24 and the tailnet's 100.64.0.0/10: flux
   `10.12.0.1/30`, d12 `10.12.0.2/30`. Use a netplan file per box (`/etc/netplan/60-pair.yaml`,
   matching `thunderbolt0`, no gateway, no DNS).
3. Test the link. If `ping 10.12.0.2` from flux shows sustained loss while the link keeps
   cycling, that is the regression: stop and use §4 until a kernel with the revert arrives.
4. Measure throughput and round trip. The stdlib TCP test from §0 needs nothing installed;
   `sudo apt install iperf3` is the better tool. Then try a larger MTU (thunderbolt-net supports
   jumbo frames) and measure again.
5. Re-run the bench's pooled row with `ggml-rpc-server -H 10.12.0.2`, with no tunnel. That gives
   the latency number this plan could not measure, and the prefill number §2 predicts.
6. Fence the port anyway: an nftables rule on d12 that accepts 50052 only on `thunderbolt0` from
   10.12.0.1.

## 4. The fallback, and the way to three boxes: a 5GBASE-T switch

A small unmanaged multi-gig switch between the Linksys and the boxes:
- **Speed.** Both RTL8126s link at 5 Gb/s to each other, about 4.7 Gb/s of TCP, and the
  uplink to the router stays at 1 G.
- **Room.** The kiosk and a third box plug in too.
- **Buying.** The QNAP QSW-3205-5T lists 10G/5G/2.5G/1G on all five ports. Check the spec
  sheet for **5G** specifically: many "multi-gig" switches are 2.5G-only, and some 10G ones
  skip 5G.

This is 5× today, but it is still a shared segment. The RPC server would still want the SSH
tunnel, so USB4 remains the answer for pooling, and the switch is the answer for everything
else.

## 5. Not recommended

- **A Cat6 cable between the two 5GbE ports.** It takes each box's only wired NIC, which puts
  the internet, Tailscale and model pulls on WiFi 5.
- **10GbE PCIe cards.** They are slower than USB4, cost more, and need a free slot beside a
  wide GPU; whether either board has one was not checked.
- **Tailscale for the RPC path.** It measured 0.89 against 0.94 Gb/s, added 0.3–0.4 ms per
  round trip, and costs WireGuard crypto on both CPUs. Keep the tailnet for control.

## 6. What this does not decide

- **Which dual-node mode production uses.**

  | | pooled executor | one queue, two GPUs |
  | --- | --- | --- |
  | speed | about 1.85× per attempt, about 1.5× per job | about 2× jobs per hour (an estimate: two jobs in flight) |
  | models | Q5_K_S at 2.5× flux alone; room for more | each card still at 75% on GPU, reloading every job |
  | fence | no change to the one-worker fence | a per-host fence |
  | gates | one at a time, as now | two gates at once on flux's CPU, which skews `ms_per_frame` unless serialised |
  | new work | sketchgen must speak to llama-server | the fence and gate changes |

  sketchgen speaks only Ollama's `/api/generate`, `/api/ps` and `/api/show`. Either mode is a
  plan of its own, and flux is the primary in both.
- **llama.cpp as a dependency.** The build is unpacked under `~/llama.cpp/llama-b11173` on both
  boxes, user-owned, and nothing runs it. Production would need a pinned build, user units for
  both servers, and `--cache-ram 0` and `-dev RPC0,CUDA0` in them.
- **Whether USB4's latency moves decode.** It needs the cable (§3, step 5).

The results, and the four scratch scripts that produced them, are in
`~/sketchgen-backups/bench/dual-node-2026-09-25/` on the laptop. The server logs are in
`~/llama.cpp/bench-2026-09-25/` on flux.
