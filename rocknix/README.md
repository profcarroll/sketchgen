# sketchgen on a ROCKNIX handheld

The generator on a Retroid Pocket Flip 2 (Snapdragon 865, 12 GB, ROCKNIX): plan, judge
and critique with a Gemma on the device, the sketch written by the 30B coder on another
node over the tailnet, the gate in the same headless Chromium build the nodes run, and
the console and the gallery in the device's own Firefox. Measured 2026-10-09 before any
of this was written (`flip2-12g`, ROCKNIX 20261001):

| | Flip 2 12 GB | sld-cloud |
|---|---|---|
| gate, job 1924's sketch | 2.1 ms/frame, 6.0 s warm | 2.4 ms/frame, 4.6 s |
| `gate/accept.sh` | 16/16 fixtures as expected | same |
| test suite | 2042 tests, 157 s; only `git`/sudo errors | |
| plan (Gemma 4 E4B QAT Q4_0, CPU) | 781-token prompt in 22 s, 7 tok/s out | |
| qwen3-coder on sld-cloud from here | 27.5 s cold load, then as on the node | |

## What is different here

- **No Ollama.** The model runs under llama.cpp's `llama-server` (the ollamadreno lab's
  build, with the OpenCL and MTP work), and `sketchgen llama-shim` serves Ollama's API in
  front of it on 127.0.0.1:11434, so the worker is configured as on any node. The model is
  named `gemma4:e4b-qat-q4_0` in every record, not `gemma4:e4b`: Ollama's tag is a Q4_K_M,
  and the model id on an attempt is the model that answered.
- **The coder is elsewhere.** `SKETCHGEN_EXECUTOR_HOST` points the executor at sld-cloud's
  Ollama over the tailnet (its unit binds 0.0.0.0 behind a firewall that admits only
  loopback, the tailnet and ssh). Every other step, the fence and `/api/ps` use this box.
- **No git.** The app is a tarball; `app/BUILD` says which. `update.sh`, `bin/fleet` and
  `publish` do not apply. Updating is `rm -rf /storage/sketchgen/app` and `install.sh` again
  (the database, venv and node.env stay).
- **No public gallery.** `publish-local.sh` renders held and published entries into
  `/storage/sketchgen/gallery-local`, which `sketchgen-gallery.service` serves on
  127.0.0.1:8090 for Firefox's kiosk mode. A person sifts there (the Pick toggle).
- **Root, `/storage`, system units.** ROCKNIX runs everything as root with `HOME=/storage`,
  so `~/sketchgen` is `/storage/sketchgen` and the node commands in AGENTS.md hold. Units
  are system units in `/storage/.config/system.d`, enabled with `systemctl enable --now`.
  Per-device settings are one file, `/storage/sketchgen/node.env`, read by every unit.
- **pip `--no-compile`.** The system Python 3.14 cannot write `.pyc` files where pip
  expects them; a wheel install without the flag fails on an assertion.

## Install

```sh
scp rocknix/install.sh flip2-12g:/storage/
ssh flip2-12g 'bash /storage/install.sh --ref main --operator profcarroll'
```

Then the lines it prints: enable the model, the shim and the console; look at
http://127.0.0.1:8081/ in Firefox on the device; enable the worker and resume the
generator; after the first `publish-local.sh`, enable the gallery and open the kiosk.

The GPU: the Adreno 650 generates slower than the four A77 cores but reads prompts 1.4×
faster and runs Gemma's vision encoder 6.6× faster (ollamadreno
`docs/Findings_20260930_opencl.md`, `Findings_20260928_s2.md`). `node.env.example` shows
the three lines that move the encoder onto it (the `bin-ocl` build, its `env.sh`, and
`-mmdev GPUOpenCL`). The judge and the critic are the steps that pay for the encoder.

## Traps

- `pgrep -f` and `pkill -f` match the shell that runs them (busybox too): write the pattern
  as `"[s]ketchgen llama-shim"`.
- A `nohup … &` inside an `ssh` command keeps the session open until it exits; `setsid` and
  `< /dev/null` let the ssh return.
- The Firefox kit's `launch.sh --sketchgen kiosk` reads `SKETCHGEN_URL`; set it to the local
  gallery or it opens the public one.
- The shim refuses any model name but its own with Ollama's own 404, so a worker left at
  the default `gemma4:e4b` fails loudly rather than being answered by the wrong weights.
