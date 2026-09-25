# Pooled mode: one executor on two cards

Written 2026-09-25. On 2026-09-25 Dave chose **pooled** as the D12 pair's dual-node mode. The
bench in `docs/plans/d12-pair.md` §1 is why:
- `d12-node-flux` (RTX 5080) and `d12-node-profcarroll` (RTX 5070 Ti), pooled over llama.cpp
  RPC, generate at about twice the speed of either card alone. On a real median executor prompt
  that is 5.9 s against 11.0 s.
- They hold the Q5_K_S executor at 2.5× flux alone.
- The planner fits beside the pooled executor, so nothing is ever evicted.

The alternative was one queue with two GPUs: about 2× jobs per hour, but each card would still
hold the executor 75% on GPU and reload it every job. It would also have needed a per-host fence
and two gates sharing one CPU's frame budget.

The operator also asked for the Console to show the pairing working. That is **Packet 1**, and
it is built first, beside this plan.

## 0. The shape

| | flux, **the head** | d12, **the partner** |
| --- | --- | --- |
| sketchgen | worker, web, database, gallery, gate: everything it runs today | paused as *lent*; database and gallery kept as the record of its A/B arms |
| Ollama | the planner, judge and critic (`gemma4:e4b`, 3.4 GB, 100% on GPU) | idle; nothing may load |
| llama.cpp | `llama-server`, **the pool**: the executor, with the model split across CUDA0 (flux) and RPC0 (d12) | `ggml-rpc-server` on 127.0.0.1:50052: its card, lent |
| VRAM, measured | 9.9 GB pool + 3.4 GB planner, of 15.9 | 10.4 GB rpc, of 15.9 |

There is **one worker**, flux's. AGENTS.md rule 1 stands as written: the partner's worker is
paused, and Packet 4 makes it refuse to load anything while the card is lent.

### 0.1 How the two talk: SSH, one key that can do two things

`ggml-rpc-server` has no authentication, so it never listens on a network. On 2026-09-25 Claude
Code's auto-mode classifier refused to bind it to the Linksys LAN, and that refusal was correct.
It listens on d12's loopback. flux reaches it through SSH with one key of its own, which is
restricted in d12's `authorized_keys`:

```
restrict,port-forwarding,permitopen="127.0.0.1:50052",command="~/sketchgen/pair-status" ssh-ed25519 … sketchgen-pair d12-node-flux->d12-node-profcarroll
```

`~/sketchgen/pair-status` is a three-line script, so which tree answers the status is changed
in one file and never in `authorized_keys`. It sets `OLLAMA_HOST_URL` and runs
`sketchgen console --json` against d12's database.

The key can do exactly two things:

- **The link.** `ssh -N -L 127.0.0.1:50052:127.0.0.1:50052 pair-partner` opens the tunnel.
  `permitopen` allows nothing else, and `-N` opens no session, so the forced command never runs.
- **The status.** Any session runs the forced command and nothing else: d12's own Console
  document, 3.5 KB in 0.24 s (measured). That is how flux's Console sees d12's card, its control
  row and its Ollama (§1). No new code runs on d12 to answer it.
  - `OLLAMA_HOST_URL` is set because d12's Ollama listens only on its tailnet address.
    Without it, the document reports nothing resident.

**Installed on 2026-09-25 for Packet 1's demonstration, and left in place:**
- flux has `~/.ssh/id_ed25519_pair` and a `pair-partner` alias in `~/.ssh/config`.
- d12's `authorized_keys` has the line above.
- `pair-status` runs the Packet 1 branch from `~/sketchgen/pair-src` until that branch is
  merged and deployed; then it points at `~/sketchgen/app`.
- Checked by hand:
  - asked to run `id; hostname`, the key returned the console document
  - a forward to port 22 was refused: *administratively prohibited*

`pair-partner` is an alias in flux's `~/.ssh/config`: `HostName 192.168.1.133`, the key, and
`HostKeyAlias d12-node-profcarroll`. It uses d12's plain sshd on the LAN. Tailscale SSH on d12
asks for a browser check, which a unit cannot answer. On the USB4 cable (`d12-pair.md` §3),
`HostName` becomes `10.12.0.2` and nothing else changes.

### 0.2 Why llama.cpp, and which one

- **Why llama.cpp.** Ollama 0.34.3's bundled `llama-server` has no RPC backend. Upstream
  llama.cpp's release builds are made with `-DGGML_RPC=ON`.
- **Which build.** The pool is pinned to one build: `b11173`, `ubuntu-cuda-13.4-x64` plus its
  `cudart` bundle, with the sha256s recorded (`d12-pair.md` §1). It runs on the 595.91.07
  driver (CUDA 13.2).
- **The model file.** The pool serves the same GGUF that Ollama serves under the executor's
  tag, resolved from Ollama's manifest. The weights are one file and are never copied.
  - Not every Ollama blob loads upstream: `qwen3.5:4b` fails with `rope.dimension_sections has
    wrong array length`. So resolving a tag must check that the pool can load the file, and
    refuse clearly if it cannot.
- **Settings the bench proved** (`d12-pair.md` §1, the two traps):
  - `-dev RPC0,CUDA0`, so the output layer is on flux
  - `--cache-ram 0`
  - `-c 16384 -np 1`, the executor's own context and one slot, because the worker is serial
  - `--metrics`, for the Console
  - bound to 127.0.0.1

## 1. Packet 1 — the Console sees the pair (built with this plan)

The Console reads one local card, one Ollama and one worker (`console.py:1434`, `:1538`, `:783`).
Pooled, the executor lives in neither Ollama nor one card, and the partner's card is invisible
from the head. The document gets a `pair` block. It always has the same keys, and they are null
where the node is not in a pair, as the GPU block already does for a node with no GPU:

- **`pair.role`** is `head`, `partner` or null.
  - `head`: `SKETCHGEN_POOL_URL` is set in the unit's environment.
  - `partner`: a `ggml-rpc-server` process is running here. No configuration is needed, so d12
    says it is lent even if nobody told it.
- **`pair.pool`** (head) is the pool, read from `/health`, `/props`, `/slots` and `/metrics`:
  - state: `down`, `loading`, `idle` or `decoding`
  - the build and context
  - the request in flight: tokens decoded, and tok/s from the difference between two reads
  - lifetime prefill and decode rates, and requests and tokens served
  - its pid and its VRAM on this card
- **`pair.partner`** (head) is the partner as it reports itself over the status key: its card,
  what its `ggml-rpc-server` holds and burns, its control row, anything its Ollama has resident
  (which should be nothing), and how old the reading is.
  - A daemon thread in the web process refreshes it every 5 s, so the page's two-second poll
    never waits on SSH. `sketchgen console` fetches it once, synchronously.
- **`pair.link`** is the interface the pair uses, its negotiated speed, and bytes a second each
  way from `/proc/net/dev`. On the head it also says whether the tunnel's local port is open.
  On the partner it says whether a pool is connected.
- **`pair.lent`** (partner) is the lent card: the `ggml-rpc-server` pid, its CPU and VRAM, and
  whether it is serving a connection.

The GPU block gains **`node.gpu.apps`**: `nvidia-smi --query-compute-apps`, the VRAM each process
holds. This is the only honest answer to "where does the model sit": pool 9.9 GB here, rpc
10.4 GB there. It is also how the page tells Ollama's runner apart from the pool, because both
are called `llama-server`.

**The slot rule** (`console.py:783`) learns three things:

1. **Ollama's runner is not a stranger.** Since Ollama moved to bundling `llama-server`, its
   runner's basename no longer starts with `ollama`. So an idle judge on today's nodes reads as
   *busy — llama-server*. The runner is recognised by its executable's path under Ollama's
   library directory.
2. **On the head, the pool decoding without a job in flight is `busy`.** The holder is *the
   pool, not a job*. That is a bench, or a client that is not the worker, and the operator
   should see it.
3. **On the partner, a running `ggml-rpc-server` makes the slot `lent`,** with *serving the
   pool* or *waiting for the pool*.

**The page.** A *Pair* panel, rendered only when `pair.role` is set, like the bill panel:

- **Head:** the two cards side by side, each with its own VRAM bar and utilisation, and the
  processes that hold them. Under them, the pool's tiles (state, live decode, tokens this
  request, lifetime rates) and the link's (in and out per second, speed, tunnel), plus the
  partner's control row. There is a warning line when the partner's Ollama has anything
  resident, or its generator is running.
- **Partner:** one line that says the card is lent, to whom, and what it holds, with a warning
  if this node's own generator is running.

Every value carries `data-k`, so the existing two-second refresh moves it. `render_text` gets
the same lines for `sketchgen console` over SSH.

**No worker change, no schema change.** In Packet 1 the pool is whatever is serving on
`SKETCHGEN_POOL_URL`. Until Packet 3 that is a bench or a replay, and the Console shows those
too, as not a job.

## 2. Packet 2 — the pair's services

**The build.** A verb, `sketchgen pair fetch`, downloads the pinned build into
`~/sketchgen/llama/b11173/` and checks both sha256s. The pin lives in the repo, in
`pair/llama-build.json`. Nothing is installed system-wide.

**User units in `systemd/`:**

| unit | where | what it runs |
| --- | --- | --- |
| `sketchgen-rpc.service` | partner | `ggml-rpc-server -H 127.0.0.1 -p 50052 -c`. The cache makes every load after the first 14.6 s instead of 91. |
| `sketchgen-pair-link.service` | head | `ssh -N -L 127.0.0.1:50052:127.0.0.1:50052 pair-partner`, with `ExitOnForwardFailure`, `ServerAliveInterval` and `Restart=always` |
| `sketchgen-pool.service` | head | `sketchgen pair serve`, which resolves the executor tag to its blob and `exec`s llama-server with the settings in §0.2 on 127.0.0.1:8090. `After=` and `BindsTo=` the link, `Restart=on-failure`, `RestartSec=10`. |

**Configuration** lives in drop-ins, as every node setting does:

- **Head:** `SKETCHGEN_POOL_URL=http://127.0.0.1:8090`, `SKETCHGEN_PAIR_PARTNER=pair-partner`
  and `SKETCHGEN_PAIR_LINK=enp7s0` (later `thunderbolt0`) for the web and worker units.
- **Both:** `SKETCHGEN_SHAPE` says pair: `D12 pair: RTX 5080 + RTX 5070 Ti over RPC`.

**The runbook.** `docs/OPERATIONS.md` gets a *The D12 pair* section:
- the key and the restricted `authorized_keys` line
- the ssh alias
- the start order: partner's rpc first, then the head's link, then the pool
- how to take the pair apart again

`update.sh` restarts the pool only when the pin changes. A model load is 14.6 s with the cache
warm and 91 s cold, and a deploy should not pay that for a template change.

## 3. Packet 3 — the worker writes on the pool

- **Where.** `SKETCHGEN_EXECUTOR_URL` points the executor at the pool. The planner, judge and
  critic stay on Ollama.
- **The request.** `executor._call_pool` posts to `/v1/chat/completions` with the prompt as the
  one user turn: Ollama wraps this model's prompt in its chat format too. The reply's `timings`
  map onto the counters every attempt row records:
  - `prompt_n` / `prompt_ms`
  - `predicted_n` / `predicted_ms`
  - the wall time
- **Sampling parity, or the A/B has two variables.** Ollama applies the tag's own parameters,
  and llama-server does not know them. `qwen3-coder:30b` carries temperature 0.7, top_k 20,
  top_p 0.8 and repeat_penalty 1.05; Ollama's default min_p is 0, llama-server's is 0.05. Every
  pooled request sends the tag's `/api/show` parameters, plus `min_p: 0` and the seed. A test
  holds the mapping.
- **Template parity** is an acceptance check, not an assumption. On job 470 Ollama counted
  2,391 prompt tokens and the pool counted 2,391. The harvest-30 run (§6) compares every
  attempt's count.
- **Residency.** The pool is always loaded, so the executor's warm and resident checks read
  `/health` and `/props`: `n_ctx` must be 16384. The *Loading a new model* card appears only
  while the pool itself is starting.
- **When the pool is down**, the attempt runs on flux's Ollama alone (the same weights at 74% on
  GPU, about half the speed), and the card says *pool down: writing on flux alone*. The
  generator does not stop because the partner rebooted.
- **Provenance.** Migration 019 adds `attempts.backend`: `ollama`, or
  `pool b11173 RTX 5080 + RTX 5070 Ti`. The model column stays the tag, because the weights are
  the same file. This is what keeps pooled attempts apart from flux-alone ones in every
  statistic and in the A/B.
- **The card** says it: *Writing the sketch — qwen3-coder:30b on the pair (RTX 5080 + RTX
  5070 Ti)*.

## 4. Packet 4 — the partner is lent, and cannot forget it

- **The fence.** `worker.fence` refuses while a `ggml-rpc-server` process is running here: *this
  card is lent to the pool*. So a Resume clicked by mistake on d12 claims nothing and loads
  nothing. It reads the same `/proc` walk the fence already does.
- **Resume.** Resume on the partner, from the web or `control resume`, refuses while the card is
  lent and says why. The first step of `sketchgen pair return` stops `sketchgen-rpc` and then
  resumes, so giving the card back is one verb.
- **The control row.** The partner's reason reads `lent: <head>`.

## 5. Packet 5 — the cable

`d12-pair.md` §3: the USB4 cable, `10.12.0.0/30`, and `pair-partner`'s `HostName` changed to
`10.12.0.2`. Then the bench's pooled row again, which measures the latency this plan could not.

## 6. Acceptance

1. **Packet 1**, before any other: with the pool running and a replay driving it, flux's Console
   shows both cards holding their halves, the pool decoding at about 200 tok/s, bytes moving on
   the link, and d12 lent. d12's own Console shows *lent*.

   **Done on 2026-09-25, 02:05–02:11Z,** from the branch in `pair-src`, with a second web on
   flux's 127.0.0.1:8091 and flux's own generator running as usual:
   - The panel showed flux's pool at 9.6 GB, d12's `ggml-rpc-server` at 10.1 GB and 38–49%
     CPU, and live decode at 186–196 tok/s on flux's own executor prompts sent with the tag's
     sampling.
   - The link showed about 1.7 MB/s in and 3.5 MB/s out, and d12 showed *paused (lent:
     d12-node-flux)*.
   - d12's text Console said `slot lent — ggml-rpc-server … serving the pool`.
   - With the pool and the tunnel stopped, the panel said both, and still read d12 over its
     own ssh session.
2. **Packets 2–4:** reboot d12 and the pool comes back on its own. Kill the link and the worker
   falls back to flux alone and says so. Clicking Resume on d12 while lent is refused.
3. **The harvest's first 30 prompts** on flux, pooled, against flux alone at the same build,
   rules and seed:
   - per-attempt wall time
   - the same prompt-token counts (template parity)
   - gate outcomes, read with the same referee: flux's CPU, both arms
   - a blind look

   That makes it a hardware arm, kept apart in the A/B by `attempts.backend`.

## 7. Open

- **A third box.** Each board's ASM4242 has two USB4 ports (`usb4_port1`, `usb4_port3`). The
  RPC client is the head, so a third partner would be a second cable from flux: a star, with no
  switch. But every pooled card adds a hop, and decode is bound by round trips, not bytes
  (`d12-pair.md` §1). A third card only pays for a model that two cannot hold, and the 30B
  executor already fits in two with room to spare. For throughput, a third box is better as a
  generator of its own, or as half of a second pair. Past three, it is a switch, and these
  boards' Ethernet stops at 5 Gb/s.
- **Bigger models.** 31 GB of pooled VRAM holds more than a 30B at Q5. Which executor is worth
  it is a question for the harvest, not this plan.
- **d12's own Console** when it is lent shows the lent card and not much else. Its A/B history
  stays readable there, and nothing new is written to it.
