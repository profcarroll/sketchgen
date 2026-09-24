# Operating the worker

Everything here runs as the ordinary user on the node. No `sudo`, nothing listens,
nothing is enabled by any script — enabling is always your keystroke.

## Install the unit files

```
python3 bin/sketchgen install-unit --dry-run    # prints what it would copy, writes nothing
python3 bin/sketchgen install-unit              # copies + systemctl --user daemon-reload
```

It copies `systemd/sketchgen-worker.{service,timer}` into `~/.config/systemd/user/`,
reloads the user manager, then prints the enable commands without running them.
`loginctl enable-linger` is already set for this user, so a user unit survives
logout and a reboot. A worker started from a tool call does not — it dies with the
call (`dossiers/node-16x96-first-day.md` §7). `sketchgen worker` is packet 2.3's
subcommand; until it lands, an enabled unit fails to start.

## Pick one mode

```
# daemon — worker resident, restarts 30 s after a failure
systemctl --user enable --now sketchgen-worker.service

# drip — timer wakes the worker 2 min after boot, then every 5 min
systemctl --user enable --now sketchgen-worker.timer
```

Enable one, not both; in drip mode leave the service disabled, the timer starts it.
For a true drip the worker has to exit when the queue is empty — override
`ExecStart` to `… bin/sketchgen worker --once` with
`systemctl --user edit sketchgen-worker.service`, never by editing the shipped unit.

## Status and logs

```
systemctl --user status sketchgen-worker.service
systemctl --user list-timers --all
journalctl --user -u sketchgen-worker -n 50     # -f to follow
```

Node time is UTC and the laptop is not; two files about one event can disagree by
four hours.

## The Node card knows which machine it is on

The Console's Node card is drawn for the node it is running on, because a cloud
VM and a machine on a desk do not have the same facts about them. `node.kind` in
the console document is `cloud` or `local`, and it is `local` unless something
proves otherwise — the proof is `meta.node_shape`, which nothing writes but
`bin/sketchgen billing --identify`, reading it from the instance metadata
service on the node itself.

What moves with it:

| element | cloud | local |
|---|---|---|
| **gpu** meter — VRAM used of total, utilisation, card name | hidden (no A1 shape has one) | shown when `nvidia-smi` answers |
| **storage $** meter — block gigabytes against the free tier | shown | hidden; a disk that was bought once has no monthly rate, and this meter used to quote Oracle's to a desktop |
| **The bill** panel | shown | hidden; no tenancy, no meter, no invoice |
| **model volume** meter | shown when the blobs are on their own filesystem | same rule — and where the size is not knowable at all (Ollama running on the Windows side of a WSL node) no meter is drawn rather than one reading zero |
| core count, in the header and on **load** | `OCPU` | `threads`, which is what they are |

A WSL node also says so under the meters: its memory, swap and boot disk are the
distro's share, not the Windows host's, and a reader who takes 8 GiB for the
machine will misread every number above it. The GPU figures come from the host
driver, so those are the whole card.

| variable | default | what it does |
|---|---|---|
| `SKETCHGEN_NODE_KIND` | auto | `cloud` or `local`, overriding the detection above. Set it on a cloud node that has not been identified yet, or the bill will not be drawn. |
| `SKETCHGEN_SHAPE` | auto | the one-line machine description on entries and in the header. Set it where the bare architecture and core count miss what makes the timings — the GPU, or a host whose RAM is not the distro's. |
| `SKETCHGEN_GPU_TTL_S` | 5 | how long the GPU reading is cached. `nvidia-smi` costs about 65 ms and the console refreshes every two seconds. |

## Pause and resume

The pause switch is a row in the database, not systemctl, so it survives a worker
restart and does not fight the timer:

```
python3 bin/sketchgen db status                  # control: running | pausing | paused
python3 bin/sketchgen control pause --reason "someone else wants the slot"
python3 bin/sketchgen control stop --reason "swapping models"
python3 bin/sketchgen control resume
```

`running` takes jobs; `pausing` finishes the attempt in flight, releases the
inference slot and settles into `paused`; `paused` takes nothing. Use it to
de-contend the node when someone else wants the one inference slot, or for
maintenance. Stopping the unit instead kills the attempt in flight; pause does not.

`control stop` is stop-now: the worker abandons the attempt in flight, puts the job
back on the queue and settles into `paused`. Attempt rows already written are kept
and the job resumes at the next attempt number after `control resume` — nothing is
re-run, nothing is lost. On the wire it is `pausing` with a reason beginning `stop`,
so the control row still holds only the three values migration 001 allows;
`sketchgen/worker.py`'s docstring says why. A job paused mid-repair is re-queued
rather than stranded, so `db status` after a pause shows it back under `queued`.

**A pause asked for during idle work takes effect between its steps.** An idle
round judges a pair and then critiques an entry, and either call can sit on the
model host for as long as its timeout allows. Until 2026-09-21 the control row
was read once per pass and not again, so a stop asked for in the middle of a
round was not seen until the round had finished — and the round would start its
next step first. The row is now read after the judge and after the critic, so
the worker settles into `paused` there. Worst case is one step, not one round.

## Deploying a change

The whole deploy is one script — pull, pause, re-install units, migrate,
re-render the gallery, restart, resume:

```
ssh sld-cloud 'bash ~/sketchgen/app/update.sh'
```

Its slowest step is the gallery re-render (every entry page, minutes), and it is
there for a reason: entry pages are written once by the publisher, so a change to
`gallery.py`, a publisher template, or anything a generator writes onto a page
reaches `index.html` but none of the entry pages without it.

A change that touches **none of that** — the operator UI (`console.py`,
`web.py`), a unit file, worker logic — does not need the render. Skip it:

```
ssh sld-cloud 'bash ~/sketchgen/app/update.sh --no-render'   # or SKETCHGEN_SKIP_RENDER=1
```

That still pulls, re-installs units, migrates, restarts and resumes — it only
drops the gallery pull and render. When in doubt, leave it off: an unnecessary
render costs minutes, a skipped necessary one ships stale pages. Restarting only
the operator UI by hand, without pausing the worker, is smaller still:

```
ssh sld-cloud
cd ~/sketchgen/app && git pull origin main
.venv/bin/python3 bin/sketchgen install-unit          # picks up unit-file changes
systemctl --user restart sketchgen-web.service
```

## Watching one job

**Stop the unit first.** `worker --once` is a second worker, and two workers
against one database claim the same jobs and overwrite each other's
transitions — the symptom is `IllegalTransition: <state> -> <state>` in the
journal, and a job left in flight by whichever one lost. The fence refuses this
since 2026-09-21, so the second worker now exits 3 with `another sketchgen
worker is running` rather than racing; before that it ran, and on 2026-09-21 it
did.

```
systemctl --user stop sketchgen-worker.service   # or the timer
python3 bin/sketchgen worker --once              # one job, then exit
tail -f ~/sketchgen/jobs/<id>/job.log            # the same lines the unit logs
systemctl --user start sketchgen-worker.service  # put it back
```

Pausing is not a substitute: `control pause` stops the *worker* from claiming,
and `worker --once` reads the same row, so a paused node makes the hand-run
worker claim nothing either. Stopping the unit is the thing that frees the
queue for one process.

While a second worker is visible the sweep is held back too — a job in a
running state then belongs to a process that is alive and coming back for it,
and re-queueing it is how one worker takes a job out from under the other. That
is why the refusal is worth reading rather than working around: `db status`
showing jobs in flight while the fence refuses is the healthy shape of this,
not a stuck queue.

The status card tells the two apart. After a pass the fence refused, the nap
is the `fenced` step, headline *Waiting for the inference slot*, with the
fence's reason in its detail — the console shows it as an amber `fenced` pill,
`paid preflight` fails its worker check on it, and `paid next` and `paid
wait` return `stop` with the reason. Before 2026-09-23 that nap said *Nothing
to do*, indistinguishable from an empty queue: an `opencode` process held the
slot from about 21:30 to 00:30 UTC, every pass was refused, the preflight read
the process count alone and said READY, and the agent driving job 1524 polled
`next` for 13 minutes over a queue nothing was going to claim.

Each job has a directory under `$SKETCHGEN_JOBS` (default `~/sketchgen/jobs`):
`job.log`, then `attempt-1/`, `attempt-2/` … holding the prompt, the raw response,
the sketch, and the gate's own output under `attempt-N/.gate/`. That is the
canonical record of what happened; the database rows point at it.

## What the executor is shown

Since 2026-09-21 an attempt is given the sketch it is revising, under a heading
inside the brief, in the prompt the node writes to `attempt-N/prompt.txt`:

- **`## The sketch this revises`** — on attempt 1 of a job spawned from a
  critique, the parent entry's kept `sketch.js`, the one `entries.source_dir`
  points at.
- **`## Your previous attempt, which the gate sent back`** — on every attempt
  after the first, that job's own attempt *n*−1, above the gate's evidence
  about it. A child's attempt 2 gets its own attempt 1 and not the parent: the
  parent is two steps back by then.

Before this, the only thing carried from one sketch to its revision was prose —
the critic's one sentence, or the gate's findings — and entry 1103 is what that
cost: five attempts, the photograph and the `responds(drag)` that passed in
attempt 1 both gone by attempt 4, each attempt written from a blank page. The
executor template did not change and `executor-v3` is still the arm label the
rules-file A/B measures; what each attempt was given is recorded per attempt in
`attempts.given_source_json`, with the context window it ran under in
`attempts.num_ctx` (16384 with a sketch in the prompt, 8192 without).

Two `meta` rows govern it, and `sketchgen executor-source` is the verb:

```
# on the node: read both
$SG executor-source --db ~/sketchgen/sketchgen.db

# the control batch for MEASURE[source-follow]: byte for byte the prompt this
# node sent before 2026-09-21
$SG executor-source --db ~/sketchgen/sketchgen.db --set none

# back on; `parent` and `previous` give one kind and not the other
$SG executor-source --db ~/sketchgen/sketchgen.db --set both

# the length over which a sketch is named in one line instead of shown
$SG executor-source --db ~/sketchgen/sketchgen.db --max-chars 12000
```

A job can answer for itself, over the node's row: `jobs.executor_source`
(migration 017), NULL on every job that has no opinion, which is all of them
unless somebody said otherwise. The New job page writes it — with a parent
picked, the tick box *give the executor the parent's sketch* is on, and
unticking it queues that one job with `none`. That is how the two arms of
`MEASURE[source-follow]` go into the same queue: the alternative is flipping
the node-wide row between two `paid start`s and sweeping every job the worker
claimed in between into whichever arm was current. `lineage.spawn` sets
nothing, so the idle loop's children follow `meta`. The job page prints the
override under **executor** when there is one.

The worker reads the job's column first and both rows at the top of every
attempt, so a change lands on the next attempt claimed: no restart, no deploy,
and nothing in flight moves.
Over the cap the heading carries one line — *N lines, longer than the M
characters this prompt has room for; not shown* — and the evidence stands alone
as it did before; whole or not at all, because a model handed half a sketch
rewrites the half it cannot see.

Where the record shows, once an attempt has run:

- **The job page** (`/job/<id>`), one line under each attempt: *given: parent
  entry 1103 · 180 lines · ctx 16384*, or *given: attempt 1 · 143 lines · ctx
  16384*, or *given: nothing*. A sketch found and over the cap says *not shown
  (over the cap)*; an attempt written off the node says *ctx —*, because no
  local context window applied to it.
- **The entry page's provenance table**, under Lineage and only where it
  belongs: **Revised from** — *entry 1103's sketch, 180 lines, then 2 attempts
  on its own*. Absent, never dashed, on an entry whose kept attempt was shown
  no parent code, which is every entry published before 2026-09-21.
- **`meta.json`**: each `gate[]` entry gains `given` (the record, or null) and
  `num_ctx`, and `lineage.inherits_source` is true exactly where that
  **Revised from** row appears — the field the ledger reads to tell a
  generation that revised code from one that revised a sentence.

A paid attempt gets the same words: `paid export --step execute` renders the
prompt through the same helper, and the item's `inputs.source` names the kind,
the sha256, the line count and whether it was shown. The guard covers that hash
as well as the evidence, so a packet cut before the sketch changed under it is
refused at import with nothing written — export again.

## Where the picture a sketch loaded shows, and what is never published

A sketch may fetch a photograph from a host outside itself; with
`loads(image)` asserted the gate requires one and records what arrived
(`report.resources_loaded`: url, host, content type, size, milliseconds).
Entry 1103, the jigsaw of 2026-09-20, did it before the word existed — five
attempts, three of them eleven seconds of gate time waiting for picsum.photos
— and nothing anywhere said so. Now:

- **The Held page card**, in the meta line beside the rules file and the
  executor: *image from picsum.photos*, or both hosts when there are two. It is
  there because publishing such an entry publishes a page that sends every
  viewer to a third party under that party's terms
  (`DECIDE[image-licence]`: the entry's CC BY covers the code and the
  statement, not the photograph), and that was worth a click through to the
  attempt. The host shown is the **kept** attempt's, which is the sketch the
  publish will write.
- **The job page** (`/job/<id>`), one line under each attempt, with the type
  and the size: *loads: picsum.photos (image/jpeg, 61.4 kB)*. No line at all on
  an attempt that fetched nothing, unlike `given:`, which every attempt has.
- **The entry page**: a provenance row **Loads** — *a picture from
  picsum.photos (image/jpeg, 61.4 kB)*, two or more joined with *and* — and one
  line under the frame, *This sketch fetches an image from picsum.photos when
  it runs*, so a reader knows their own browser is about to call a third host.
  Both absent, never dashed, on an entry that loads nothing, which is every
  entry published before `loads(image)` existed.
- **`meta.json`**: each `gate[]` entry gains `resources_loaded` and
  `preload_s`, and the kept attempt's list is lifted to a top-level `loads`.

**The URL is never published.** `gallery.LOADS_KEYS` is `host`, `type`,
`bytes`, and that is the whole record the gallery writes: a URL can carry a
query string nobody chose to publish — a signed link, a key, a name — which is
the same reason `guard()` scans every file the generator writes for
email-shaped strings and for the node's own hostname. The full URL stays in the
attempt's `report.json` on the node, which the operator UI serves under
`/jobs/<id>/attempt-<n>/.gate/report.json`; if you need to see the picture
itself, open it from there.

There is **no allowlist of hosts** (`DECIDE[image-hosts]`): the gate reports
where the picture came from and a person decides before publishing. If a host
should not be on the gallery, reject the entry — the record of the rejection
keeps the reason.

## What the worker does when the queue is empty

Nothing stays idle for long. With an empty queue and `control: running`, the
worker does one bounded round of idle work before it sleeps:

1. **judges** up to `SKETCHGEN_IDLE_JUDGE` pairs with the local judge
   (`gemma4:e4b`, the model on this box that can see);
2. **critiques** up to `SKETCHGEN_IDLE_CRITIQUE` published entries that have no
   child yet, and queues the child each critique asks for;
3. then sleeps.

The critic is shown the entry's four-frame strip, the same picture the judge
looks at, and **refuses** a published entry that has no readable strip on record
rather than critiquing it blind. A refusal writes nothing and consumes nothing,
so that entry comes back round on the next idle cycle; if you see the same
`critique of entry N refused` line every cycle, the strip is missing or
unreadable and no amount of waiting will fix it.

This runs **inside the worker's own loop, not on a second timer**, and that is the
whole of the design: there is one inference slot, so one process is allowed to use
it. A second unit would need a second fence, and two fences racing each other is
the contention the fence exists to prevent. `worker --once` does a round too, so
you can watch one from the shell.

```
python3 bin/sketchgen db status | sed -n '/idle work/,$p'
```

prints how many agent verdicts and critiques exist, how many children were
spawned, how many critiques were rejected, and when the last of any of it
happened — read from the database, so it survives a restart.

Five environment variables tune it; none has to be set:

| variable | default | what it does |
|---|---|---|
| `SKETCHGEN_IDLE_JUDGE` | 1 | pairs judged per round. **0 turns judging off.** |
| `SKETCHGEN_IDLE_CRITIQUE` | 1 | entries critiqued per round. **0 turns critiquing off.** |
| `SKETCHGEN_JUDGE_MODEL` | `gemma4:e4b` | the local judge |
| `SKETCHGEN_CRITIC_MODEL` | `gemma4:e4b` | the critic whose sentence becomes the next prompt |
| `SKETCHGEN_LINEAGE_DEPTH` | 3 | generations a line runs before a person has to touch it |
| `SKETCHGEN_STUCK_MINUTES` | 30 | how long a job may sit in a running state with nobody attending it before the sweep re-queues it; `0` never sweeps |
| `SKETCHGEN_PAID_MODELS` | (none) | model ids answered off the node — see [Paid steps](#paid-steps-any-model-step-answered-off-the-node). Set it in the web unit too. |

Set both limits to `0` to leave the node quiet between jobs — the quickest way to
hand the inference slot to somebody else without pausing the worker at all:

```
systemctl --user edit sketchgen-worker.service   # add the two Environment= lines
systemctl --user restart sketchgen-worker.service
```

An entry is critiqued **once per version of `prompts/critic.md`**, and the row in
`critiques` is what remembers that — including when the critic's sentence was
rejected for breaking the one-sentence rule, which is kept with its reason rather
than retried. Editing that prompt bumps its `prompt_version`, and those entries
may be critiqued again under the new one.

### A new critic prompt version is a burst of work

Read that last sentence as an operator rather than as a reader. `critiques` is
UNIQUE on `(entry_id, prompt_version)`, so the bookkeeping that stops an entry
being critiqued twice only stops it *under the version it was critiqued under*.
The first idle round after a prompt-version bump therefore starts again from the
oldest published entry and works through **every** one of them — one per idle
cycle, each spawning one child job, each child a real sketch the executor has to
build. Nothing is wrong when you see that; it is what the bump is for. The
comparison MEASURE[critic-quality] wants is the same parents critiqued blind and
then sighted, and it only exists if the second pass actually runs.

It is still a burst, and there are two ways to hold it back:

- **`SKETCHGEN_IDLE_CRITIQUE=0`** in the unit's environment — the worker keeps
  draining the queue and keeps judging pairs, and critiques nothing:

  ```
  systemctl --user edit sketchgen-worker.service   # Environment=SKETCHGEN_IDLE_CRITIQUE=0
  systemctl --user restart sketchgen-worker.service
  ```

- **pause from the console header** — the control row, which stops the worker
  after the attempt in flight and so stops the idle round with it. Resume from
  the same place when you want the pass to run.

Raise the limit back (or resume) when you want it, and the pass picks up where
it left off: the rows already in `critiques` under the new version are what it
counts as done.

### Showing the critic the ghost window (cutting critic-v4)

Since 2026-09-21 the gate plays a ghost pointer after its probes and writes
`ghost.png` beside `strip.png`: the same sketch with something clicking and
dragging it. The critic can be shown both, and the code for it is in (Packet 16,
`docs/plans/auto-mouse.md` §6) — but it is off until you say so, because turning
it on means cutting a new critic prompt version, which is the burst of work
above. Entry 1103, the jigsaw, is what it is for: four identical frames of a
photograph cut into pieces that never move, and a critic that can only ask for a
different photograph.

The switch is `prompts/critic.md` and nothing else. Two lines at the top, in
this order:

```
prompt_version: critic-v4
images: strip ghost
```

and one sentence in the body, in the WHAT THE SKETCH ACTUALLY SHOWS section,
saying what the second picture is:

> The second image is the same sketch under a pointer that clicked and dragged
> it; nobody was at the keyboard.

`images:` is read from the header only, `strip` is always first and never
optional, and a name the reader does not know is ignored. With the line absent —
critic-v3 as it stands — the payload is the strip alone, byte for byte what it
has always been. **The presence of `ghost.png` never switches it on by itself**,
and that is deliberate: critiques are comparable only within one prompt version,
so a version that was sighted for some entries and half-sighted for others would
have nothing to measure. An entry with no ghost window is still critiqued, over
the strip alone, with the reason in the worker's log — nothing re-gates a
published entry, so most of the gallery will never have one.

Run it as a deliberate batch, not as a surprise on a Sunday:

1. `SKETCHGEN_IDLE_CRITIQUE=0` in the worker unit's environment, restart, and
   confirm the worker is quiet — this is the burst-of-work section's first
   switch, and every published entry becomes critiquable again the moment the
   version changes.
2. Edit `prompts/critic.md`, deploy (`update.sh --no-render`; the prompt file is
   read from disk on every critique, so nothing else has to change).
3. Raise `SKETCHGEN_IDLE_CRITIQUE` back and watch the first few rounds. The log
   line `entry N was critiqued over two images` names both sha256s; `entry N has
   no ghost.png` is the other half of the population and is not an error.
4. Keep the batch as its own batch. MEASURE[critic-quality] is the same parents
   critiqued blind and then over two images, and MEASURE[ghost-coverage] — how
   many entries even have a ghost window whose frames differ from their strip —
   is what says whether the second image was worth a version at all.

## When a job goes wrong

Two things the worker does for itself, both written after job 5 spent a night
stuck in `planning` on 2026-09-14:

**A step that fails is a job outcome, not a crash.** If the planner's reply
cannot be parsed, the raw text is saved to
`~/sketchgen/jobs/<id>/plan-response-<n>.txt` and the plan is tried once more
**with a different seed** — the log line says which seed each try used, because
the first version of this retry re-sampled with the same seed and the model
reproduced its mistake word for word. If both tries fail, the last reply is read
leniently: prose with no `Brief` heading still becomes the brief, and the entry
records `planner-v1+lenient` so you can see the plan was recovered rather than
parsed. Only a reply with no prose at all fails the job, with
`last_error: planner: …`, and no entry is kept because nothing was made. The
executor and the gate are the same: whatever goes wrong becomes an attempt row
with evidence, and the worker carries on.

**A failed gate is read for one bug before the evidence goes back.** Seven of
the eleven crashing attempts the gate has recorded were the same collision: the
sketch declares a variable whose name is a p5 global — `for (let line of lines)`,
`const scale = …` — which hides the library's own function, and then calls it.
All the browser says is `line is not a function`, and the executor reads that as
a bug in the drawing code: job 16 spent all three attempts on `line`, job 34 two
on `scale`. So whenever the gate exits non-zero the worker runs a deterministic
scan of `sketch.js` (`sketchgen/preflight.py`, no browser and no model) and puts
what it finds at the top of the evidence, above everything the gate said:

```
job 16: preflight: your variable `line` hides p5's `line()` function; rename it (sketch.js line 31)
```

The gate remains the authority on the verdict — the pre-flight only explains the
console error it reported, adds nothing when the gate passes, and is dropped with
a log line if the scan itself fails. A hidden *function* is reported only when
the sketch also calls it, so the harmless `let hue = …` beside the real bug stays
quiet. Run the same scan by hand over any attempt directory:

```
python3 bin/sketchgen preflight ~/sketchgen/jobs/16/attempt-1
```

Exit 0 is clean, 1 is something found, 2 is a directory with no `sketch.js`.

Since 2026-09-15 the same scan reads a second thing: what a frame costs. The
gate's `frame_budget` check is the authority on whether a sketch is too
expensive, and it says *1093 ms per frame*; this says which line does it —
`sphere()` inside a loop inside `draw()` in a WEBGL sketch, `line()`/`point()`
the same way (in WEBGL both are immediate mode and allocate a vertex buffer per
call), an all-pairs loop inside `draw()`, an allocation inside `draw()`, a
`filter()` inside a loop. Same mechanism, same place in the evidence, same
bargain: it adds nothing when the gate passes and would rather say nothing than
guess.

**A job nobody is attending goes back on the queue.** At startup, and again on
every idle cycle, the worker re-queues any job left in `planning`, `executing`,
`gating` or `repairing` that has not moved for `SKETCHGEN_STUCK_MINUTES`
(default 30) and that it is not working on itself. It says so in the log:

**Restarting the worker is safe mid-job.** On SIGTERM (what `systemctl --user restart` sends) a worker with a job in flight abandons the attempt, puts the job back on the queue with the reason "worker stopped", and exits; the control row is left as it was, so the next worker resumes rather than starting paused. On start, the new worker re-queues anything still in a running state without waiting `SKETCHGEN_STUCK_MINUTES`: there is one worker, so a running row at start is an orphan of the previous one. Before this, a restart mid-attempt left the old job showing as executing beside the new one for half an hour (job 58, 2026-09-14).

```
sweep: job 5 sat in planning for 47 minutes with no worker attending it; re-queued
```

The attempt rows that job already has are kept, and it resumes at the next
attempt number. Nothing has to be recovered by hand: `db status` shows the state
and the next pass picks it up.

## An unsafe sketch

Twice now — entry 165 (job 166, 2026-09-15) and entry 269 (job 270) — the
pipeline has produced a sketch that passed every check and then took the
operator's laptop down. Both drew about 1,500 `sphere()` meshes plus tens of
thousands of immediate-mode `line()` calls per frame in WEBGL. In the gate that
costs time; in a real browser tab, unseeded and at full speed, p5's WEBGL
`line()` builds and uploads a vertex buffer per call, so it costs memory until
something is killed. About 4 GB of GPU buffers, a `firefox CanvasRenderer` in
`ASAHI_GEM_CREATE`, and the oom-killer.

**Until this change is deployed on the node, do not open `/held` on a machine
you cannot afford to lose.** The page embeds a live iframe per held entry, so
opening it runs every held sketch at once. Look at one entry's job page instead,
or better, `gate-audit` first (below) and then decide what to open. After
deployment nothing on those screens autoplays: each card shows the strip as a
play button, one frame runs at a time, and stopping removes it.

### Spotting one

```
sketchgen gate-audit --over 30
```

Every attempt with a report, slowest gate run first, with its entry id, the
entry's state and — for runs from 2026-09-15 on — `ms_per_frame`. It reads the
database and the report files, runs no browser and never runs the gate, so it is
safe against a working node. Sorting by `timings.total_s` is how both incidents
were found after the fact, and how job 45 (504 s, twelve `filter(BLUR)` passes a
frame) and job 43 (120 s) turned up behind them. The gate's median run is about
2 s; a run in the hundreds is not a slow machine, it is a sketch.

A `frame_budget` of `FAILED` is the same finding made at the time, by the gate
itself. A blank `ms/frame` means the report predates the budget.

On the operator's own machine, after a crash, the kernel is the witness:

```
journalctl -k -b -1 | grep -i oom
```

`-b -1` is the boot before this one — the one that ended. A line naming the
browser process is the confirmation that the tab, and not something else, is
what went down.

### Neutralising one

The precedent is job 166, and job 270 was done the same way. The rule is that
nothing is deleted: the attempt directory is the canonical record (spec §10) and
the unsafe sketch is evidence of what the pipeline did.

In `~/sketchgen/jobs/<job>/attempt-<n>/`:

1. `git mv`-style rename, by hand: `sketch.js` → **`sketch.unsafe.js.txt`**. The
   `.txt` is the point — nothing serves it, nothing runs it, and it is still
   readable.
2. `.gate/` → **`.gate.unsafe/`**. The original run's report, strip and console
   are the measurement of the bug and must not be overwritten by the re-gate.
3. Write a **bounded rewrite** into `sketch.js`: the same picture, inside the
   frame budget. For both incidents that meant batching the particles into one
   `beginShape(POINTS)` cloud, finding neighbours through a spatial hash and
   drawing them as one capped `beginShape(LINES)`, `frameRate(30)`, no
   `sphere()` and no `push()`/`pop()` per particle. `sketchgen preflight` on the
   directory should come back clean.
4. **Re-gate with the same assertions and the same seed** as the original run —
   both are in the original `report.json`, under `assertions` and `seed`. Same
   question, same conditions, or the two runs are not comparable:

   ```
   . ~/sketchgen/.venv/bin/activate
   python3 ~/sketchgen/app/gate/sketch_gate.py ~/sketchgen/jobs/270/attempt-1 \
       --assert 'motion(idle)' --assert 'uses(webgl)' --seed 1
   ```

5. Write **`neutralised.md`** beside them: the date, what the original did
   measured rather than described (calls per frame, ms per frame, the gate's
   `total_s`), what the rewrite does instead, and the two numbers side by side.
   Job 166's is `~/sketchgen/jobs/166/forensics.md` and job 270's is
   `~/sketchgen/jobs/270/attempt-1/neutralised.md`.

The database is not edited. The entry still points at the same attempt
directory, and what changed inside it is recorded in the file that says so.

### Afterwards

Run `gate-audit --over 30` over the whole archive and read the list. A published
entry with a slow gate run is public, and the gate that passed it could not see
what it cost.

## The venv and the gate

The pipeline is stdlib-only; the venv exists for one thing, the gate, which
drives headless Chromium through Playwright. Both halves are pinned, because the
gate is the referee and a node whose Playwright drifted is a node whose verdicts
cannot be compared with the ones already in the database:

```
python3 -m venv ~/sketchgen/.venv
~/sketchgen/.venv/bin/pip install -r ~/sketchgen/app/requirements.txt   # playwright==1.62.0
~/sketchgen/.venv/bin/playwright install chromium                       # ~/.cache/ms-playwright
```

The browser is not pip-installable, so the second line is not optional and is
not implied by the first. With no Chromium the gate refuses with exit 3 rather
than passing anything.

The gate itself is now **tracked in the repo**, at `gate/`: `sketch_gate.py`,
`accept.sh`, `fixtures/` and a README saying what it checks. Until 2026-09-15 it
lived only at `~/sketchgen/gate/` on the node, which made the one referee the
one thing a reclaimed instance would have taken with it. Run its harness after
touching it:

```
. ~/sketchgen/.venv/bin/activate
~/sketchgen/app/gate/accept.sh          # six fixtures, PASS or MISMATCH each, non-zero on any
```

### Operator step: retire ~/sketchgen/gate (one release, then gone)

The units now set `SKETCHGEN_GATE=%h/sketchgen/app/gate/sketch_gate.py`, and
`update.sh` re-installs them, so the next update switches the worker and the UI
over by itself. The old directory stays for one release as a **symlink**, so
that a unit somebody has not reloaded yet still finds a gate. Do this by hand,
on the node, after an `update.sh` that has landed the repo copy:

```bash
ssh sld-cloud
cd ~/sketchgen
sha256sum gate/sketch_gate.py app/gate/sketch_gate.py   # must be the same hash, twice
mv gate gate.pre-repo                                    # keep it until the symlink is gone
ln -s app/gate gate
ls -l gate && ~/sketchgen/.venv/bin/python3 gate/sketch_gate.py --help >/dev/null && echo ok
systemctl --user restart sketchgen-worker.service sketchgen-web.service
journalctl --user -u sketchgen-worker -n 20              # watch one job pass the gate
```

One release later, when no unit refers to the old path and a job has passed the
gate through the new one, remove both:

```bash
ssh sld-cloud 'rm ~/sketchgen/gate && rm -rf ~/sketchgen/gate.pre-repo'
```

Nothing in the pipeline reads `~/sketchgen/gate` except through
`$SKETCHGEN_GATE`, so the symlink is a courtesy to stale units and to muscle
memory, not a dependency.

## The two deploy keys

```
python3 bin/sketchgen keygen app --dry-run       # read-only: pulls the app repo
python3 bin/sketchgen keygen gallery             # write: pushes the Pages repo
```

Each writes `~/.ssh/sketchgen-<which>` at mode 0600 plus its `.pub`, prints only
the public half, and refuses (exit 3) if that key already exists. Paste the
printed line into that repo's *Deploy keys* page with the permission the command
names. Never commit a private half, and never put one in a unit file or a DB row —
a scoped, write-only, one-click-revocable key is the only credential this system
keeps on the node (`dossiers/sketchgen-gallery.md` §6).

## Publish a held entry

Publication holds for a person by default (`dossiers/sketchgen-gallery.md` §9), so
this is a command someone runs, or a button someone clicks — never a step the worker
takes on its own.

```
python3 bin/sketchgen publish 12 --from /path/to/e12 --dry-run   # prints the plan
python3 bin/sketchgen publish 12 --from /path/to/e12 --by <github-username>
python3 bin/sketchgen reject  12 --reason "off brief"            # no git at all
```

### The Held page is one press

Since 2026-09-17 the four verbs on each card — `+ Publish`, `× Reject`,
`› Critique`, `− Archive` — **mark** rather than act. Pressing one fills it with
its own colour and makes no request; pressing another moves the fill; pressing
the filled one clears it. Publish, Reject and Archive are one choice per card.
Critique stacks with Publish or Archive, or stands alone — a child is queued and
the entry stays held — but it cannot join Reject, because both read the card's
one box and one sentence cannot be a reason and a revision at once. A kept
rejection still has no Reject. Nothing is typed twice: the box is the reason when
Reject reads it and the child's revision sentence when Critique does, and an
empty box is allowed for Reject (it becomes `rejected by operator`) and refused
for Critique — that card reads *needs a sentence* and **Process** stays dimmed
until it is typed or unmarked.

Curate the whole pile, then press **Process** once, in the second row of the
sticky header. The tally beside it says what the press will do, which is why
there is no confirm dialog. One batch runs it in the only order that is legal:
critiques while their parents are still held, then archives, then every
rejection and publication committed one at a time and carried out in **one push**
with **one index re-render**. Eight publishes used to be sixteen pushes; they are
two. While it runs, every toggle, every box and Process itself are disabled —
on every open tab, because "busy" is on the server — and the tray shows the now
line, a bar counted in real steps, the five phases, and each card's own state.

When it ends the result stays in the tray until you dismiss it:
`6 done · 1 refused in 48s`, refusals first with their reasons. Cards that went
through are gone on the reload. Refused and failed ones are still there, **still
marked**, with the reason on the card, so fixing one and pressing again is the
whole of the retry. `POST /held/<id>/publish`, `/reject` and `/archive` still
exist for scripts and for `/entry/<id>` habits, and they refuse with
`a batch is running — nothing changed; it will finish first` while one is in
flight. Deploy with the tray empty: `update.sh` restarts `sketchgen-web`, and a
batch in flight when it does is abandoned where it stands.

### One publisher at a time

Both the worker and the operator UI publish, and they share one working tree at
`~/sketchgen/gallery`. Since 2026-09-16 a publish takes an exclusive `flock` on
that checkout and holds it from the clean-tree check to the last push, so the
two serialise instead of racing. A publisher that finds the lock held prints

```
sketchgen: waiting for another publish to finish with /home/ubuntu/sketchgen/gallery
```

once, waits, and then does its work. That line is normal on a busy node and
needs nothing from you. After five minutes it gives up with `another publish has
held ... for more than 300s`, which is not a queue any more: look for a
publisher that died. The lock is advisory and the kernel drops it when the
holder exits, including when it is killed, so nothing has to be cleaned up by
hand.

Before the lock, two overlapping publishes of entry 488 raced and the loser's
push was refused with `cannot lock ref 'refs/heads/main': is at <x> but expected
<y>`. That one was harmless -- the loser rolled itself back after the winner had
finished -- but the same race the other way round has one publish's `git reset
--hard` landing while the other is still rendering into the tree, and the
survivor then publishes a half-reset gallery without reporting anything.

Rejecting from the **operator UI** does more than that CLI line: since the
lineage ledger's packet 2 a rejection stores the reason on the entry and then
publishes it to the rejections page, beside the gate's own rejections — in a
batch it travels with that batch's one push. The CLI `reject` above is still the
state flip on its own, for a rejection that should not go out at all. Neither one
deletes or moves a single file, and nor does Archive below: the entry row, its
attempt directories under `jobs/` and its strip all stay exactly where they are.

The fourth verb on a Held card is **Archive**. It takes a held entry, or a kept
failure nobody published, off the Held page and the kept list and does nothing
else — no publish, no push, no deletion. An archived entry is in no list at all;
it is still readable by id at `/entry/<id>` in the operator UI, and the console's
funnel counts it.

The entry must be `held`, `failed-kept` or `rejected`, and the gallery checkout (`--gallery-dir`,
default `$SKETCHGEN_GALLERY` or `~/sketchgen/gallery`) must be a clean git work tree
on its default branch. The files land in `e/<id>/`; the commit names the executor
model as `Co-Authored-By:` (ATTRIBUTION.md, carried into the gallery) and the person
as `Published-By:`; the push uses the gallery deploy key above. The database row
becomes `published` **only after the push succeeds** — a failed push undoes the local
commit and leaves the row `held`. Every file is scanned first for an email address
and for `instance-`, and one hit refuses the whole publish: the gallery repo is
public by construction.

Before the first real publish, in this order: create the `sketchgen-gallery` repo;
run `keygen gallery`; register the printed public half on that repo as a **write**
deploy key; clone the repo to `~/sketchgen/gallery` with that key in
`GIT_SSH_COMMAND`; set that checkout's own `user.name` and `user.email` — the commit
is made with the checkout's identity, not one this tool invents; then publish one
entry with `--dry-run` before publishing it for real.

### Operator step, once: re-render every entry after the lineage fix

Until the lineage fix, the publisher rendered an entry while its row was still
`held`, so the entry could not find itself in the forest and froze
`"parent_entry_id": null` into its own `meta.json` — 74 of 101 public non-root
entries claim to be roots, and their parents' files are missing the children to
match. The database was always right; only the published files are wrong, and
one `render-all` fixes every one of them.

Run it **once**, on the node, after the fix is deployed:

```
ssh sld-cloud 'bash ~/sketchgen/app/update.sh'     # pull, re-render, push
```

or by hand, to look before anything is pushed:

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen render-all \
    --gallery-dir ~/sketchgen/gallery --db ~/sketchgen/sketchgen.db
git -C ~/sketchgen/gallery diff --stat
git -C ~/sketchgen/gallery add -A
git -C ~/sketchgen/gallery commit -m "re-render: every entry's lineage restored"
GIT_SSH_COMMAND='ssh -i ~/.ssh/sketchgen-gallery' git -C ~/sketchgen/gallery push
```

Expect **one large commit touching every `e/*/meta.json`**, plus a new
`lineage.json` at the gallery root. That is intended; it is the point of the
step. `render-all` writes files and nothing else — it never updates a database
row, so `published_utc` and `publish_commit` come back out of the rows exactly
as they went in, and nothing is re-published or re-dated by re-rendering it.

Spot-check afterwards:

```
jq .lineage ~/sketchgen/gallery/e/230/meta.json    # parent_entry_id 176
jq .lineage ~/sketchgen/gallery/e/176/meta.json    # children [230]
```

### Operator step, once: publish the rejections that were only a state flip

Rejecting an entry used to be a state flip and nothing else, so every rejection
made before packet 2 is invisible with all of its files still on the node — 32
of them when the packet was written, and each one a hole in some published
entry's lineage. `publish-rejected` renders and pushes them, in id order, one
commit per entry, exactly as the Publish button does. The reason comes from the
entry's `reject_reason`, or from the originating job's `last_error` where that
reads as something a person typed; where it does not — the placeholder the old
UI wrote for an empty reason box, or an error message from the gate — the page
says the reason was not recorded rather than inventing one.

Deploy the code first, so the migration has run:

```
ssh sld-cloud 'bash ~/sketchgen/app/update.sh'     # pull, migrate, re-render, push
```

Then look before anything is pushed, and run it once:

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen publish-rejected \
    --all --dry-run                                 # one line per entry and its reason
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen publish-rejected \
    --all --by <github-username>
```

`--dry-run` reads the database and nothing else: no checkout, no push, no
write. The real run stops at the first entry it cannot publish rather than
carrying on; the entries already pushed stay pushed, and running it again picks
up where it stopped, because a published entry is no longer in the backlog.
Name ids instead of `--all` to do a few at a time.

Afterwards the rejections page lists those entries beside the gate's, each with
its chip — `rejected · operator` against `rejected · gate` — and every entry
whose parent was one of them has a real frame in its lineage instead of a blank.

### Operator step, once: put the critique form back on 36 published entries

Publishing one entry renders it into a fresh staging directory, scans those
bytes for personal data, and commits them. The generator reads the gallery's
config out of the directory it is writing into, and a staging directory has no
`config.json`, so every entry published this way was rendered with `write_path`
empty — and an entry page with no write path gets no critique form, because
there is nowhere to send a critique. Every page on the site went up that way.
The ones that have a form have it because a later `render-all` or
`publish-index` gave them one. Entries 518 through 563 were published after the
last of those and never got theirs — 36 pages. (A further 55 pages have no form
and should not: they are rejections, and `lineage.spawn` refuses a rejected
parent, so a form there would be an offer the pipeline will not honour.)

The same render was also the reason a freshly published page said "not
published" in its own lineage ledger and had no publish commit in Provenance:
it was made before the push, so `published_utc` and `publish_commit` were still
null when its bytes were written. Both are fixed at the source now — the
publisher passes the checkout's config to the generator, and re-renders the
entry after the push, with the stamps.

Neither fix reaches a page that is already on the site. Deploy, and the
re-render in step 4 of `update.sh` does that in one pass:

```
ssh sld-cloud 'bash ~/sketchgen/app/update.sh'     # pull, re-render every page, push
```

`update.sh` step 4 now runs `render-all` rather than `render-index`. An entry
page used to be written once, by the publisher, and never again: a template
change, a new panel or a fixed generator reached `index.html` and never reached
the hundreds of pages that are the gallery. That gap is the part worth keeping
fixed; the missing critique form was only the first thing it swallowed.

To look before anything is pushed, or to do it without a deploy:

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen publish-index
```

That re-renders every published entry and the index, commits once as
`gallery: re-render every page`, and pushes with the gallery deploy key. It
changes no entry's state and no database row.

Expect a commit touching `e/*/index.html` for every entry published since the
last full re-render. Spot-check afterwards:

```
curl -s https://profcarroll.github.io/sketchgen-gallery/e/531/ | grep -c critique-form
```

One, not zero.

### Operator step, once: point every kept failure at its best attempt

An entry has always taken its files from the attempt that **ended** the job.
Usually that is also its best one. Not always: on the first 45 kept entries the
dry-run moves **7**, and entry 429 is one of them — it publishes a blank canvas
from a tenth attempt whose image never arrived, while its second drew a working
puzzle from an image it built itself. The other 38 are already showing their
best attempt and this leaves them untouched.

The wider value of the run is the labelling. Counted off the run itself rather
than by eye, the 45 kept entries divide exactly:

| | |
|---|---|
| Ran clean — never failed a QA check | **30** |
| No clean attempt, a real gate failure | 15 |
| Pointed at the wrong attempt | 7 |

Those 30 ran. They threw nothing, did not freeze and stayed inside the frame
budget, and were recorded as gate failures for missing an assertion a model
wrote — `uses(webgl)` and `motion(idle)` most often, which is a sketch that chose
2D over 3D or stillness over motion. This writes what each actually diverged on
into `offplan_json`, which is what the pages read to stop calling them
rejections.

Running it a second time reports 0 repointed and changes nothing.

Deploy, then run it once on the node:

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen repoint-kept --dry-run
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen repoint-kept
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen publish-index
```

`--dry-run` reads the database and writes nothing; it prints one line per entry
saying which attempt it would move to and what that attempt missed. The real run
updates `source_dir`, `strip_path`, `png_path`, the statement and the executor,
and records the divergence in `offplan_json`. **It touches no file in any attempt
directory** and changes no state. Running it twice is a no-op — the second run
says "same attempt" for every entry.

`publish-index` is what puts the repaired pages on the site.

#### The 30, and the door back

A job now ends in `held` rather than `failed` when some attempt passed every QA
check and only missed assertions: the sketch runs, it just is not what the
planner predicted, and that is a judgement for a person. The kept failures
recorded under the old rule have no such path, and 30 of the 45 are in that
position.

`repoint-kept` had a `--reclassify` flag that moved them. **It is gone.** It
wrote the state with a raw UPDATE, and `failed-kept -> held` is not a transition
`ENTRY_TRANSITIONS` allows; 18 of those 30 are published, and `held` is not a
public state, so it would have taken 18 entries off the site. That is the
deletion this project does not do.

The 12 that nobody has published can be reopened, and that is worth doing for a
reason the flag never articulated: **publishing reads the state to decide the
page.** A `held` entry becomes `published` and joins the grid; a `failed-kept`
one keeps its state and joins the rejections page. So an off-plan sketch
recorded under the old rule can otherwise only ever be published as a failure,
however good it is.

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen reopen-offplan --all --dry-run
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen reopen-offplan --all
```

It goes through `db.entry_transition`, so the state machine decides, not the
command. Naming an ineligible id refuses rather than skipping it. The 18 already
on the site stay exactly where they are — the record stands, and their pages now
say what they diverged on instead of calling them rejections, which is the whole
repair those 18 need.

The job row is left `failed` on purpose: it is terminal, and a sixth terminal job
state meaning "its entry got a second look" would mean touching every count and
funnel in the console to say nothing new.

## The billing card

The console's last panel says what this tenancy has cost. It is the one number
the node cannot fetch for itself, on purpose: reading it needs an OCI API key
that can create and destroy infrastructure, and that key does not belong on a
machine that serves a public gallery and runs code a model wrote. There are no
credentials in `~/.oci` on the node and there should not be.

So it is two halves, joined by ssh. Ask on the operator's machine, record on the
node:

```
# on the operator's machine, where ~/.oci lives — does both, in one command
python3 bin/sketchgen billing --sync sld-cloud
```

`--sync` queries the Usage API here and pipes the reading into
`billing --record --from-json -` on the far side. `--record` touches no OCI
endpoint, which is what lets it run on the node at all. To look without
recording, run `billing` with no flags; to see the raw reading, `--json`.

`bin/sketchgen-tunnel.sh up` runs this `--sync` for you after the forward
answers, but only when the node is an OCI instance: it asks the node's metadata
service first, and a node where that does not answer (the DT lab PCs on
Tailscale) keeps its card empty rather than showing this tenancy's bill.

### What is stored, and why it is not one number

A reading is a *day at a time*, one row per service and SKU, in `billing_usage`
(migration 013). The old four `meta` keys are still written and the big figure
on the card still comes from them, but they are a snapshot and a snapshot
cannot say whether the bill is moving.

On an Always Free tenancy it never appears to move. The dollar figure is 0.00
every day and stays 0.00 right up until the day it doesn't. What moves first is
the metered **quantity** — 96 OCPU-hours in a day is four OCPUs held for
twenty-four hours — so the card draws thirty days of that, and turns the bars
amber on the first day anything is actually charged.

Two queries go into one reading, because neither of Oracle's answers is
complete: `queryType=USAGE` returns the quantity and its unit with no currency,
`queryType=COST` returns the amount and its currency with a null unit. `fetch()`
joins them on (day, service, SKU).

### Tell the node which tenancy it is in

Run this once on the node, after any rebuild:

```
~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen billing --identify \
    --db ~/sketchgen/sketchgen.db
```

It reads the instance metadata service at 169.254.169.254 — link-local,
readable by anything on the instance and nothing off it, and carrying no
authority to change anything — and records the tenancy, instance, shape, OCPUs
and memory into `meta`. No credentials involved.

That is what makes a reading checkable. `--record` refuses a reading whose
tenancy is not the node's, and the card says so in red rather than showing the
figure as if it were this account's. `--force` records it anyway, under its own
tenancy — the two accounts stay two accounts, because `tenancy` is in the
table's primary key.

**This is not hypothetical.** On 2026-09-18 the operator's `~/.oci/config` was
found to be pointing at a *different tenancy from the one the node runs in*: a
separate Oracle account holding a 4 OCPU / 24 GB instance created 2026-08-05.
Every figure this card had ever shown — including the "$0.00, every service,
from 1 August" recorded on 2026-09-17 — was that other account's. Nothing in the
old four keys could have caught it; an amount is just an amount. Until an API
key exists in the node's own tenancy, the card has nothing true to show, and it
now says that instead of showing a zero.

### The meter against the machine

The card also compares what Oracle is metering with what the node actually is,
and says so when they disagree. A tenancy metered at 4 OCPU for a machine
running 16 is not getting a discount, it is a discrepancy, and the right time to
find out is not when an invoice explains it. The line beneath the bars gives the
average OCPU and GB held over the full days in the window, against the Always
Free allowance of 4 OCPU / 24 GB.

The card never presents itself as live: Oracle's usage data lags a day or more,
so a card claiming to be current would be wrong twice over. It shows the figure,
the window it covers, when it was last checked, and marks itself once the
reading is over a week old.

## Backups: the nightly snapshot and the pull

The database and the attempt archive are the only parts of this system with no
copy anywhere else. The code is pushed, the gallery is pushed, votes and likes
are in D1 — but `sketchgen.db` holds a thousand-odd agent judgments, the
critiques and the lineage rows, and `jobs/` holds every attempt's code and
evidence. They are on a free-tier instance the provider may reclaim.

Two halves, and **both are needed**: a snapshot on the node, and a pull that
takes it off the node.

### On the node: one verified snapshot a day

```
systemctl --user enable --now sketchgen-backup.timer        # 04:10 UTC, daily
python3 bin/sketchgen backup snapshot --to ~/sketchgen/backups   # the first one, now
systemctl --user list-timers --all | grep sketchgen-backup
journalctl --user -u sketchgen-backup -n 30
```

Each run writes `~/sketchgen/backups/<utc>/` holding three files —
`sketchgen.db`, `gate.tar.gz`, `manifest.json` — then re-opens what it wrote and
verifies it, then removes all but the newest fourteen. The database is copied
through SQLite's **online backup API**, not `cp`: a file copy of a WAL database
taken while the worker is writing can be torn, and opens perfectly well
afterwards while being silently short. The worker is not paused and does not
need to be.

The manifest records sizes and SHA-256s, the schema version, a row count for
every table, the number of job directories, and the commit both checkouts were
on. `jobs/` is **not** tarred — 300 MB that only grows is the wrong shape for a
nightly tarball, so the puller rsyncs it and the manifest's job count is what a
pull is checked against.

Nothing in a snapshot is a credential. `~/.ssh` and `writepath.token` are never
read, and a snapshot refuses outright if a credential turns up inside the one
directory it walks.

```
python3 bin/sketchgen backup verify ~/sketchgen/backups        # the newest one
python3 bin/sketchgen backup verify ~/sketchgen/backups/<utc>  # a named one
```

`verify` runs `PRAGMA integrity_check`, re-digests every file the manifest names
and compares every row count. Exit 1 on any disagreement. An unopened backup is
a belief, not a backup.

### On the operator's machine: the pull, which is the actual backup

The snapshot above is a good copy on the same disk that would be lost with the
instance. This is the part that gets it off:

```
bin/pull-backup.sh sld-cloud                  # from the operator's clone
bin/pull-backup.sh sld-cloud --no-jobs        # snapshots only, for a quick check
```

It rsyncs `~/sketchgen/backups/` (mirrored, deletions followed) and
`~/sketchgen/jobs/` (**added to, never deleted from**) into
`~/sketchgen-backups/sld-cloud/`, verifies the newest snapshot it just pulled,
and prints one line: date, database size, row count, job directories, verified
or not. It **exits non-zero when the newest snapshot is more than 36 hours
old**, because a timer that stops on the node is silent by nature and this is
the only thing that notices.

A pull, not a push, on purpose: no new credential goes on a box whose whole
problem is that it may be taken away. The SSH connection is one that already
exists.

Daily, unattended, on a Linux operator machine:

```
cp systemd/operator/sketchgen-pull.{service,timer} ~/.config/systemd/user/
$EDITOR ~/.config/systemd/user/sketchgen-pull.service   # WorkingDirectory + the host name
systemctl --user daemon-reload
systemctl --user enable --now sketchgen-pull.timer
systemctl --user list-timers --all | grep sketchgen-pull
```

On macOS there is no `systemctl --user`; use launchd, cron, or run the script by
hand. What must not happen is nobody running it.

An optional third copy, to Oracle Object Storage, exists behind a flag and is
not wired into any timer — it is the only thing here that would put a cloud
credential back on the node:

```
python3 bin/sketchgen backup push --bucket sketchgen ~/sketchgen/backups --dry-run
```

It refuses with instructions if the `oci` CLI is not installed.

## Recovery

Last rehearsed: not yet

Every step is a command. The elapsed time goes on the line above once somebody
has actually run this against a throwaway instance from a real snapshot; until
then this section is a plan, not a runbook, and packet 0 is not finished.

**Before you start**, on the machine holding the mirror:

```bash
ls -1 ~/sketchgen-backups/sld-cloud/backups/ | tail -3
bin/pull-backup.sh sld-cloud --no-jobs      # if the old node is still reachable
python3 bin/sketchgen backup verify ~/sketchgen-backups/sld-cloud/backups
cat ~/sketchgen-backups/sld-cloud/backups/<utc>/manifest.json   # keep this open; it is the acceptance test
```

### Rehearsing on etk-cloud

The rehearsal venue is `etk-cloud`, the operator's second Oracle node: 4 CPU,
23 GB, aarch64, Ubuntu 24.04, checked 2026-09-15. It has neither Ollama nor
Playwright, so steps 2 through 5 get exercised for real rather than found
already done. It also runs Docker builds for other projects, so the rehearsal
lives in its own Unix user and leaves with one command.

Set it up so every command below works verbatim:

```bash
ssh etk-cloud 'sudo adduser --disabled-password --gecos "sketchgen rehearsal" sketchgen-rehearsal && sudo loginctl enable-linger sketchgen-rehearsal'
ssh etk-cloud 'sudo install -d -m 700 -o sketchgen-rehearsal ~sketchgen-rehearsal/.ssh && sudo cp ~/.ssh/authorized_keys ~sketchgen-rehearsal/.ssh/ && sudo chown sketchgen-rehearsal ~sketchgen-rehearsal/.ssh/authorized_keys'
cat >> ~/.ssh/config <<'CFG'
Host sld-cloud-new
    HostName <etk-cloud's address>
    User sketchgen-rehearsal
CFG
ssh sld-cloud-new 'id && loginctl show-user $USER | grep Linger'
```

What a rehearsal does differently from a recovery:

- **Before step 1**, the mirror on the operator's machine holds at least one
  verified snapshot (`bin/pull-backup.sh sld-cloud` has printed `VERIFIED`).
  Nothing in a rehearsal reads the live node.
- **Step 5**: pull `gemma4:e4b`; try `qwen3-coder:30b-a3b-q4_K_M` and let it
  fail if 23 GB is not enough. Record which.
- **Step 8**: generate the keys, add the *app* key as read-only if you want
  `update.sh` exercised, and **do not add the gallery write key**. Clone the
  gallery over HTTPS instead, so a rehearsal cannot push:
  `git clone https://github.com/profcarroll/sketchgen-gallery.git ~/sketchgen/gallery`.
- **Step 9**: write a made-up token. The sync will fail with a 401 in its log,
  which is the correct proof that the timer runs and the real token is not here.
- **Step 10**: enable the worker's `.timer`, not the service, so at most one
  drip job runs; a held entry is the end-to-end proof, and holding never pushes.
- **Do not rename the host** to `sld-cloud`. That step is for a real recovery.

Then clean up, revoke the throwaway app key on GitHub, and write the record:

```bash
ssh etk-cloud 'sudo loginctl disable-linger sketchgen-rehearsal && sudo userdel -r sketchgen-rehearsal'
# remove the sld-cloud-new block from ~/.ssh/config
```

The `Last rehearsed:` line reads `<date>, etk-cloud, <elapsed>, full` or
`..., without the executor` when the 30b model did not fit.

**1. A new instance.** Ubuntu 24.04, the same shape as before (ARM, 24 GB is
what the free tier gives). Add it to `~/.ssh/config` as `sld-cloud-new` so the
old entry still points at whatever is left of the old one.

```bash
ssh sld-cloud-new 'lsb_release -d && nproc && free -g && df -h /'
```

**2. Packages, and linger.** Linger is what lets a `--user` unit survive logout
and a reboot; without it nothing here stays up.

```bash
ssh sld-cloud-new 'sudo apt-get update && sudo apt-get install -y python3 python3-venv git rsync sqlite3'
ssh sld-cloud-new 'loginctl enable-linger $USER && loginctl show-user $USER | grep Linger'
```

**3. Clone the app.** Read-only deploy key first, so the clone is the same one
`update.sh` will pull with later.

```bash
ssh sld-cloud-new 'mkdir -p ~/sketchgen && cd ~/sketchgen && git clone https://github.com/profcarroll/sketchgen.git app'
ssh sld-cloud-new 'cd ~/sketchgen/app && git log --oneline -1'   # compare with manifest.git.app.commit
```

**4. The venv, Playwright, Chromium.** Pinned, because the gate is the referee:

```bash
ssh sld-cloud-new 'python3 -m venv ~/sketchgen/.venv'
ssh sld-cloud-new '~/sketchgen/.venv/bin/pip install -r ~/sketchgen/app/requirements.txt'
ssh sld-cloud-new '~/sketchgen/.venv/bin/playwright install chromium'
ssh sld-cloud-new '~/sketchgen/.venv/bin/pip show playwright | head -2'   # must say 1.62.0
```

**5. Ollama and the two models, by name.** These are large and they are the long
pole; start them before anything else that can wait.

```bash
ssh sld-cloud-new 'curl -fsSL https://ollama.com/install.sh | sh'
ssh sld-cloud-new 'systemctl is-active ollama'
ssh sld-cloud-new 'ollama pull qwen3-coder:30b-a3b-q4_K_M'
ssh sld-cloud-new 'ollama pull gemma4:e4b'
ssh sld-cloud-new 'ollama list'
```

**6. Restore the database.** From the newest verified snapshot in the mirror.
The snapshot is one self-contained file: there is no `-wal` to carry with it.

```bash
SNAP=~/sketchgen-backups/sld-cloud/backups/<utc>
python3 bin/sketchgen backup verify "$SNAP"                     # verify BEFORE shipping it
scp "$SNAP/sketchgen.db" sld-cloud-new:sketchgen/sketchgen.db
ssh sld-cloud-new 'sqlite3 ~/sketchgen/sketchgen.db "PRAGMA integrity_check;"'
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen db status'
```

Bring the schema up to the code, in case the restored database is older than the
clone (`migrate` is safe to rerun and does nothing when there is nothing to do):

```bash
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen db init'
```

**7. Restore the attempt archive.** 300 MB; run it in a screen or accept the
wait. This is a push from the mirror, the only push in this runbook.

```bash
rsync -a --info=progress2 ~/sketchgen-backups/sld-cloud/jobs/ sld-cloud-new:sketchgen/jobs/
ssh sld-cloud-new 'ls -1 ~/sketchgen/jobs | wc -l'   # compare with manifest.jobs_directories
```

**8. New deploy keys, on both repositories.** The old private halves are gone
with the old instance and that is the correct outcome; generate new ones and
revoke the old entries in each repository's *Deploy keys* page.

```bash
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen keygen app'
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen keygen gallery'
# paste each printed public half into that repo's Deploy keys page:
#   sketchgen          READ-ONLY
#   sketchgen-gallery  WRITE
# then delete the old node's keys from both pages.
```

Re-point the app clone at SSH and clone the gallery with the write key:

```bash
ssh sld-cloud-new 'cd ~/sketchgen/app && git remote set-url origin git@github.com:profcarroll/sketchgen.git'
ssh sld-cloud-new 'GIT_SSH_COMMAND="ssh -i ~/.ssh/sketchgen-gallery -o IdentitiesOnly=yes" git clone git@github.com:profcarroll/sketchgen-gallery.git ~/sketchgen/gallery'
ssh sld-cloud-new 'cd ~/sketchgen/gallery && git config user.name "sketchgen publisher" && git config user.email "<the publisher address>"'
```

**9. A new write-path token.** The old bearer is gone with the instance; rotate
it in the Cloudflare Worker rather than trying to recover it.

```bash
# in the writepath/ Worker's settings, set a new SKETCHGEN_TOKEN secret, then:
ssh sld-cloud-new 'install -m 600 /dev/stdin ~/sketchgen/writepath.token' <<< '<the new token>'
ssh sld-cloud-new 'ls -l ~/sketchgen/writepath.token'    # must be -rw-------
```

**10. Install the units and start the timers.** `install-unit` copies and
reloads; enabling stays a keystroke.

```bash
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen install-unit'
ssh sld-cloud-new 'systemctl --user enable --now sketchgen-web.service'
ssh sld-cloud-new 'systemctl --user enable --now sketchgen-sync.timer'
ssh sld-cloud-new 'systemctl --user enable --now sketchgen-backup.timer'
ssh sld-cloud-new 'systemctl --user enable --now sketchgen-worker.service'   # or the .timer, for drip
ssh sld-cloud-new 'systemctl --user list-timers --all | grep sketchgen'
```

**11. Prove it.** The gate first, because it is the referee and a recovered node
whose gate does not run cannot make another entry:

```bash
ssh sld-cloud-new '. ~/sketchgen/.venv/bin/activate && ~/sketchgen/app/gate/accept.sh'
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen db status'
SKETCHGEN_REMOTE_HOST=sld-cloud-new bin/sketchgen-tunnel.sh up && open http://localhost:8081/held
ssh sld-cloud-new '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen backup snapshot --to ~/sketchgen/backups'
bin/pull-backup.sh sld-cloud-new --no-jobs
```

**Recovered means all four of these, not three:**

1. `db status` row counts match `manifest.json`'s `row_counts`;
2. `ls ~/sketchgen/jobs | wc -l` matches `manifest.jobs_directories`;
3. the operator UI shows the held queue at http://localhost:8081/held;
4. `accept.sh` passes every fixture, and one new job runs end to end.

**Then rename the host** to `sld-cloud` in `~/.ssh/config` so every script and
unit in this document works unchanged, and write the date and elapsed time on
the `Last rehearsed:` line above.

## The shrink to 4/24, before 1 November

This was the 10/1 shrink until 2026-09-23, when the tenancy was confirmed
pay-as-you-go: the trial credit ends on 30 September, but the account keeps
running and bills the card after it, so nothing is reclaimed on 1 October. The
decision that day was to keep 16/96 through October (about $166 on the card)
while the D12 lab comes up, and to resize to 4 OCPU / 24 GB — the Always Free
allowance — **before 1 November**, so November bills nothing for compute.

The resize is a reboot into the new shape from the console (Instance → Edit →
Shape), with the generator paused first so no attempt is cut off. Afterwards
`billing --identify` on the node, so the Node card and new entries say 4/24.
The two models (19 GB + 9 GB) do not fit in 24 GB together, as they did not
before 2026-09-13: planner and executor take turns, and every job pays a load
between them. Read `attempts.load_s` after the resize to see what that costs.

Nothing else to do. The unit encodes no core count, no memory figure and no
concurrency: one job at a time, fenced against other clients. The node gets
smaller, jobs get slower, the drip drips further apart.

## A rented GPU node: OCI VM.GPU.A10.1

Written 2026-09-23 for the trial's last week: one NVIDIA A10 (24 GB), 15 Intel
OCPUs, 240 GB RAM, **$2.00 an hour** list, in the same tenancy as sld-cloud.
Why and what to run on it is `docs/plans/a10-benchmark.md`; this is how to
stand it up and take it down. Call it `sld-gpu` in `~/.ssh/config`.

**It is x86 with an NVIDIA card, like d12 — not like sld-cloud.** OCI's A1
(Ampere ARM) shapes take no GPU; this is a separate instance. So copy d12's
setup where the two differ, and match d12's versions exactly: arm C of the
hardware A/B is only a comparison if Ollama, the driver major and the model
digests are the same on both.

**0. Read d12's versions first**, and write them down; step 5 pins to them.
On 2026-09-23 they were Ollama 0.34.3, driver 595.91.07, Ubuntu 26.04.1
(Python 3.14.4), app a72f076 — read them again, they move. d12's Ollama
listens on its Tailscale address only (`OLLAMA_HOST=100.107.156.77:11434`),
so a bare `ollama` there says it cannot connect; that is not an outage.
The A10 keeps the default loopback address and needs no such line.

```bash
ssh dave@d12-node-profcarroll 'ollama -v; nvidia-smi --query-gpu=driver_version --format=csv,noheader; systemctl cat ollama | grep -i environment; systemctl --user show sketchgen-worker -p Environment --value; cd ~/sketchgen/app && git log --oneline -1'
```

**1. Launch.** Console → Compute → Create instance, in the availability domain
the limit was raised for. Shape `VM.GPU.A10.1`; image the newest Canonical
Ubuntu (26.04 if listed, to match d12); boot volume 200 GB (two models are
30 GB, the rest is attempts); same VCN and subnet as sld-cloud, public IPv4,
your usual SSH key. Ingress stays at the default: port 22 only. **Launch
as soon as it can be used and do not stop it until you are done** — stopping a
GPU VM gives its host back, and the restart can fail with *out of host
capacity*, which is days, not minutes.

```bash
ssh sld-gpu 'lsb_release -d && nproc && free -g && df -h / && lspci | grep -i nvidia'
```

**2. The driver.** The platform image has none. Pick the same major as d12's.

```bash
ssh sld-gpu 'sudo apt-get update && sudo ubuntu-drivers list --gpgpu'
ssh sld-gpu 'sudo ubuntu-drivers install --gpgpu nvidia:<d12 major>-server && sudo reboot'
ssh sld-gpu 'nvidia-smi'        # must say A10, 23028 MiB or so, and the driver you chose
```

**3–4. Packages, linger, the app, the venv, Chromium** — as steps 2–4 of
*Recovery* with `sld-gpu` for `sld-cloud-new`, plus the browser's system
libraries, which a fresh x86 image does not have:

```bash
ssh sld-gpu 'sudo ~/sketchgen/.venv/bin/playwright install-deps chromium'
ssh sld-gpu 'cd ~/sketchgen/app && git checkout <d12 commit>'   # the A/B's build, not main
```

**5. Ollama, pinned, and the models by digest.** The install script takes a
version. d12 runs Ollama on its defaults — its one drop-in (`tailnet.conf`)
only binds it to the Tailscale address and waits for `tailscale0` — so the
A10 gets **no** `OLLAMA_*` drop-in at all, and the A/B arm runs under the
same defaults; the variants in the plan change one at a time from there.
(sld-cloud is the odd one out: `KEEP_ALIVE=30m` and `MAX_LOADED_MODELS=2`.
The bench asks for 30 minutes per request, so its numbers are unaffected.)

```bash
ssh sld-gpu 'curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=<d12 version> sh'
ssh sld-gpu 'systemctl show ollama -p Environment --value'   # PATH only, no OLLAMA_*
ssh sld-gpu 'ollama pull qwen3-coder:30b && ollama pull gemma4:e4b'   # d12's tags
ssh sld-gpu 'ollama list'      # must show 06c1097efce0 and c6eb396dbd59, as d12 does
```

**6. A fresh database, not a restore.** This is a new arm, like d12: its entry
ids start at 1 and mean nothing outside it.

```bash
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen db init'
```

**7. A private gallery, as d12 has one.** A local bare repo as the checkout's
origin, so `publish` works and nothing leaves the box. No deploy keys, no
write-path token, and **do not enable `sketchgen-sync.timer`**: there is no
Worker for this node, and its votes table stays empty.

```bash
ssh sld-gpu 'cd ~/sketchgen && git init --bare gallery.git && git clone gallery.git gallery && cd gallery && git config user.name profcarroll && git config user.email profcarroll@users.noreply.github.com'
```

**8. Units and their settings.** `install-unit`, then one drop-in for the
worker and web units. `SKETCHGEN_RATE_PER_HOUR` is what the per-sketch cost is
figured at on a cloud node, and its default is the A1 rate; left alone, every
A10 sketch would be priced at a ninth of what it cost.

```bash
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen install-unit'
ssh sld-gpu 'for u in sketchgen-worker sketchgen-web; do mkdir -p ~/.config/systemd/user/$u.service.d; printf "[Service]\nEnvironment=SKETCHGEN_EXECUTOR_MODEL=qwen3-coder:30b\nEnvironment=SKETCHGEN_RATE_PER_HOUR=2.00\nEnvironment=SKETCHGEN_SHAPE=VM.GPU.A10.1 15/240 + A10 24G\n" > ~/.config/systemd/user/$u.service.d/node.conf; done; systemctl --user daemon-reload'
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen control pause --reason "bench first"'
ssh sld-gpu 'systemctl --user enable --now sketchgen-worker.service sketchgen-web.service sketchgen-backup.timer'
```

The worker comes up paused. `billing --identify` on the node records the OCI
shape; the tunnel's `billing --sync sld-gpu` reads the same tenancy bill
sld-cloud's card shows, because it is the same bill.

**9. Bench before anything else**, while the box is quiet — then the A/B arm:

```bash
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen bench --out ~/sketchgen/bench-sld-gpu.json'
scp sld-gpu:sketchgen/bench-sld-gpu.json . && python3 bin/sketchgen bench --compare bench-*.json
```

The executor should load **100% on the GPU** here (`gpu%` in the table), where
d12 shows about 75: that difference is most of what the rental is for.

**10. Take it down.** Snapshot, pull, verify, and only then terminate —
ticking *permanently delete the attached boot volume*, or it bills on.

```bash
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen control pause --reason teardown'
ssh sld-gpu '~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen backup snapshot'
bin/pull-backup.sh sld-gpu            # verifies the snapshot it pulled
scp 'sld-gpu:sketchgen/bench-*.json' 'sld-gpu:sketchgen/*.json' ~/sketchgen-backups/sld-gpu/
```

Remove the `sld-gpu` block from `~/.ssh/config` afterwards so nothing tries to
reach an address Oracle has given to somebody else.

## The operator UI as a service

`install-unit` also copies `systemd/sketchgen-web.service`. It runs `sketchgen web`
on `127.0.0.1:8081` with no authentication, so it must only ever be reached over an
SSH tunnel:

```bash
systemctl --user enable --now sketchgen-web.service      # on the node
bin/sketchgen-tunnel.sh up                                # on the laptop
```

`bin/sketchgen-tunnel.sh` is `up`, `status`, `heal`, `down` and `url`, and `up` is
idempotent: if the port already answers it does nothing. Reach for `heal` when the
console stops loading — it tears the forward down and builds it again.

Raise the tunnel by hand and you inherit a trap worth knowing about, because the
obvious command is the one that sets it:

```bash
ssh -f -N -L 8081:127.0.0.1:8081 sld-cloud                # don't
```

`-f` backgrounds ssh *before* the bind error is visible, and without
`ExitOnForwardFailure=yes` a forward that fails to bind does not end the session — it
leaves a connected ssh holding **no listener**, which never retries the bind, and
`ssh -f` still exits 0. Run it twice and the second one is already that stub; run it
again after the first dies and every survivor is one. What you see is a console that
does not load and a healthy-looking `ps` full of tunnels. Tell the two apart by socket
count — a real forward has three (the connection, the `127.0.0.1` listener and the
`::1` listener), a stub has one:

```bash
ls -l /proc/<pid>/fd | grep -c socket
```

The script closes that hole: it always passes `ExitOnForwardFailure=yes`, reaps
listener-less strays before starting, and decides whether the tunnel is up by asking
the port for HTTP rather than by finding a process. Defaults are `sld-cloud` and
8081; `SKETCHGEN_REMOTE_HOST`, `SKETCHGEN_LOCAL_PORT`, `SKETCHGEN_REMOTE_BIND`,
`SKETCHGEN_REMOTE_PORT` and `SKETCHGEN_HEALTH_PATH` override them.

Then open http://localhost:8081/ — the Console; Queue, New job, Job detail (live
transcript) and Held are in its top bar, with the worker's Pause / Stop now / Resume
control upper right. Logs: `journalctl --user -u sketchgen-web -n 50`.

Each item in that top bar carries a small live summary of its own page, so the node is
never out of sight while you are standing somewhere else. **Console** has a
five-segment meter: lit segments are the node's one-minute load per core as a
percentage, in fifths — the first two green, the third amber, the last two red — and
hovering it names the CPU figure and who holds the inference slot; beside the meter the
tokens gauge is a sparkline of tokens completed per five-minute bin over the last two
hours — flat on the floor while the worker idles, one spike per job — labelled with the
current decode rate in tok/s, which on this node barely moves and so is the label rather
than the line, and drawn again wider in the Console's own model panel. **Queue** has
three counts, `queued · in flight · failed`: amber is waiting, green is moving, and the red
one is failures **since the worker last started** (the worker stamps
`meta.worker_started_utc` when it comes up), not the all-time total, which would only
ever grow — so a red zero means this session has been clean, and restarting the worker
clears it. **Held** has an amber superscript, the number waiting for you to publish or
reject, with the kept rejections below them named on hover; that page's own second
header row is the batch tray, and its `Process` button is the only request on it.
**Gallery** has a dim
superscript: everything that is public, published entries and kept rejections
together. All of them are rendered by the server on every page load and repainted by the
same two-second poll that keeps the worker pill honest.

**New job** signs a job as you. The submitter is a pill carrying the operator's GitHub
login — `$SKETCHGEN_OPERATOR` if the web unit sets it, else whatever `gh auth status` on
the node reports, asked once per process — and a box for somebody else's username only
opens when you ask for one; with neither source the box is back and required. The same
login is what a critique on Held is signed by. The parent entry is a card, not a number:
type an id or pick one of the recent held, published or kept entries and the page draws
its strip, state, generation and prompt, presets its models and rules from it the way
`lineage.spawn` does, and refuses a rejected or archived one before the queue sees it;
`/new?parent=<id>` is what the job page's "descended from" link opens. **Planner** and
**Executor** are the models this node actually has (see below). Assertions and
the run options open the way you last saved them — **Save as defaults** keeps the
current ticks and options in the database's `meta` row, **Forget them** goes back to the
built-in ones, and either leaves the prompt you were typing where it is. **One job per
line** queues a batch of prompts under one setting, which is the cheap way to feed
`rules=random` for the A/B measurement; the button counts them, and every line shares
the one submitter, parent, assertions and options. Recent root prompts sit under the box
to run again under other rules.

### Which models run a job

The models live on their own volume — `OLLAMA_MODELS=/mnt/models`, 148 GB — so there
is room for more of them than the pipeline names, and the **Planner** and **Executor**
menus on New job list the ones that are there rather than offering `local` or `paid`.
Both read Ollama when the page is drawn (`GET /api/tags` for the list, `POST /api/show`
for each one's capabilities, cached 60 s) and show the models in two groups:

- **on this node** — runs here, costs electricity and nothing else. The worker's
  default is marked `(default)` and is what a job gets if nobody chooses.
- **off this node** — a `-cloud` tag, which Ollama proxies to ollama.com; (for the
  planner) `paid`, which stops the job at `needs-laptop` for the laptop to claim; and
  every model named in `SKETCHGEN_PAID_MODELS`, which does the same under the model's
  own name (see [Paid steps](#paid-steps-any-model-step-answered-off-the-node)). All of
  them send the prompt off the box, which is why they are not in the first group: the
  gallery's "0 API calls" is true of the first group only.

| | filtered on | default | `paid`? |
|---|---|---|---|
| **Planner** | `vision` | `worker.DEFAULT_PLANNER_MODEL` | yes — `needs='plan'`, and see below |
| **Executor** | `completion` | `executor.DEFAULT_MODEL` | yes — `needs='execute'`, once per attempt (migration 014) |

#### Bringing a paid plan back

`paid` parks the job and the node stops: branch B of DECIDE[credential-model]
keeps the credential off this box, so the planning happens wherever the key is.
`plan --job` is one way back in, one job at a time, and what follows is how it
works. `paid export --step plan` / `paid import` is the same return leg for every
parked job at once, and is what an agent should drive — see
[Paid steps](#paid-steps-any-model-step-answered-off-the-node).

```bash
# on the node: what is waiting, and what it was asked for
python3 bin/sketchgen db status | sed -n '/needs-laptop/p'
python3 -c "import sqlite3;print(sqlite3.connect('$HOME/sketchgen/sketchgen.db').execute('select id,prompt from jobs where state=\'needs-laptop\'').fetchall())"
```

Ask the model that holds the credential for a plan in the shape
`prompts/planner.md` asks for — a `Brief` heading and an `Assertions` list —
save its reply verbatim, and replay it:

```bash
python3 bin/sketchgen plan --job 1228 --model claude-sonnet-5 --stub reply.txt
```

That parses the reply through the same parser the local planner uses, writes
`plan.json` and `response.txt` into the job's own directory, and puts the brief,
the assertions and the model on the job as it goes back on the **queue**. The
worker claims it on its next pass and — because it now has a brief — takes it
straight to `executing` without re-planning it.

Queued rather than executing on purpose: `db.claim_next` selects on
`state = 'queued'` alone, so a job moved directly into a running state by
anything that is not the worker sits there with nobody attending it until the
stuck-sweep notices, `SKETCHGEN_STUCK_MINUTES` later.

Three things worth knowing:

- **`--model` is the model that answered, not the one you meant to ask.** It
  lands in `jobs.planner` in place of the word `paid`, and it is what provenance
  shows afterwards. The rule is the paid judge's: a verdict belongs to the model
  that gave it.
- **A reply that will not parse costs nothing.** The job stays at
  `needs-laptop`, `response.txt` is saved beside it, and the same `--job` takes
  a better reply. Only a parsed plan moves the job.
- **`--job` refuses a job in any other state**, so it cannot race the worker for
  `jobs.brief` — the one-writer rule the fence enforces for jobs, applied to the
  column. Planning the same job twice is refused for the same reason: the second
  run finds it `executing`.

The critic and the executor have their own routes now — see
[Paid steps](#paid-steps-any-model-step-answered-off-the-node).

The planner menu names `vision` on no row, because every row has it; the executor menu
does, because there it is news — a vision-capable executor is one that could be shown
the gate's screenshot rather than the text `build_evidence()` writes for a coder that
cannot see.

The choices land in `jobs.planner` and `jobs.executor`, columns migration 001 created
and that nothing in the pipeline used to write — no schema change — and the worker runs
each step with the model the job names (`Worker.planner_model_for`,
`Worker.executor_model_for`). A blank column, or the word `local` from a row written by
an older page, still means the worker's default. Both models are recorded on every
attempt and on the entry, so provenance says which ones made it, and `lineage.spawn`
carries both to a child: a revision written by a different model is a revision of
nothing.

**The executor is a second variable.** The A/B rig measures the rules file across one
population (spec §9), scored with Bradley–Terry. Varying the executor per job puts
another difference into the same gallery. It is recorded per attempt and per entry so
the analysis can control for it, but "recoverable" is not "controlled" — change it on
purpose, and preferably for a run you mean to compare on its own.

Two things worth knowing:

- **Capabilities come from `/api/show`, not `/api/tags`.** They disagree. On this node
  `/api/tags` reports `gemma4:e4b` as `completion, thinking, tools` while `/api/show`
  reports `audio, completion, thinking, tools, vision` — the tags list is written from
  the manifest as it was pulled, `/api/show` reads the model. Filtering on the tags list
  would drop the default planner out of its own menu.
- **A model host that does not answer is not an error.** Each menu falls back to the one
  entry the page offered before it could ask — `local — <the worker's default>` — and
  the page works. A tag chosen while the host was up is still accepted after it goes
  down; the worker finds out when it calls, and fails the job (planner) or the attempt
  (executor) with the model named.

To offer a model that is not there yet:

```bash
ssh sld-cloud 'ollama pull qwen3.5:9b'
```

and reload New job. Nothing needs restarting — the list is read per page, and the cache
is a minute long.

## Paid steps: any model step, answered off the node

Four steps ask a model something — the **planner**, the **executor**, the **judge**
and the **critic** — and any of them can be answered by a model that is not on this
node. The node never holds the credential (DECIDE[credential-model] branch B), so a
paid step is always two legs, driven from the CLI by an agent on a machine that
does: the node writes down what it would have asked, the agent answers each item
with its own model, and the node reads the answers back through the parser the
local path uses. `docs/plans/agentic-cli.md` is the design.

```bash
# on the node: what is waiting, per step
python3 bin/sketchgen paid status

# on the node: write what it would have asked
python3 bin/sketchgen paid export --step plan --as claude-opus-5 --out plan.json

# on a machine with the credential: an agent reads each item's "prompt" (and
# "images", by path over the tunnel), asks the model, and pastes the reply
# verbatim into that item's "answer". It sets "model" to what actually answered.

# on the node: land the answers
python3 bin/sketchgen paid import plan.json
```

Every verb takes `--json`. An item whose answer will not parse, or whose `guard`
no longer matches what the node holds, is **rejected with a reason and writes
nothing**; the rejected answers are kept verbatim in `plan.json.rejected.json`. An
item left with an empty `answer` is skipped in silence, so a packet handed back half
done is fine.

| step | offered | lands via | guard |
|---|---|---|---|
| `plan` | jobs at `needs-laptop`, `needs='plan'` | `planner.parse_response` → job back on the **queue** | none needed — `jobs.prompt` never changes |
| `execute` | jobs at `needs-laptop`, `needs='execute'` | reply saved to `attempt-N/paid-response.txt` → job back on the **queue**; the worker gates it | the attempt number and the previous attempt's evidence |
| `judge` | pairs that model has not judged | `judge.import_verdicts_detailed` | `artefact_hash` of both strips and briefs |
| `critique` | what the idle critic would take | `lineage.validate` → `record_critique` + `spawn` | sha256 of the strip |

**Which models count as paid.** `paid`, every id registered with `paid models
add`, and every name in `SKETCHGEN_PAID_MODELS`. Registration is the one to use:
it is stored in the database, so the worker, the web page and every CLI see it
at once, with no unit file and no restart — and an agent can check it, which it
cannot do for a unit's environment. Names only, never a key.

```bash
python3 bin/sketchgen paid models add claude-sonnet-5
python3 bin/sketchgen paid preflight --as claude-sonnet-5   # READY, or what to fix
```

`preflight` checks the schema, the registration, that exactly one worker is
running (or the drip timer is on), and that the generator is running; each
failure names its fix and whether the agent or the operator owns it. It also
reports what the worker is doing this second, every live lease, and every job
parked for a plan or an attempt — whose it is and the command that moves it.

A job whose planner is one of them parks at `needs-laptop` exactly as `paid` does,
and the entry records the model that answered.

**An agent's own job is a handful of verbs.** Since 2026-09-21 (the second
Sonnet 5 run, which sat ten minutes in a blocking `wait` behind an idle-spawned
job and was killed) the recipe in AGENTS.md is:

```bash
SINCE=$(date -u +%FT%TZ)
bin/sg paid start --as claude-sonnet-5 --by profcarroll --since $SINCE \
    --prompt "a tide of slow lines"
bin/sg paid next --job N --as claude-sonnet-5 > packet.json    # do: answer|wait|done|stop
bin/sg paid import - < packet.json                               # then next again
```

`bin/sg` runs one command on the node over ssh with every argument quoted
once (`printf %q`), so a prompt is quoted like any other argument; stdin and
stdout pass through. `start` registers the model if needed, runs the
preflight (NOT READY: exit 3, nothing queued), queues one job with the agent as
planner and executor (`--planner local` / `--executor local` to keep a step
here, an Ollama tag for a named local model), and **leases** the job. `next`
returns within `--timeout` (240 s, under a tool call's limit) with one JSON
object: the packet itself when the job is parked for the agent (`do: answer`),
`wait` with the worker's current step when the worker has it, `done`,
`handoff` (below), or `stop` (exit 3). `import` puts the job back on the queue
as before and prints the `next` command.

**A try is the node's gate, lent to the agent.** Between `next` and `import`
an agent can have the real gate look at a candidate:

```bash
bin/sg paid try --job N --as claude-sonnet-5 < answer.txt   # do: verdict|rejected|wait|stop
```

Stdin is the text the agent would put in `items[0].answer`, so a reply that
would be rejected at `import` is rejected here too, without reaching the
worker. What comes back is `sketch_gate.py`'s own verdict — the exit, every
check, each assertion with its detail, `timings.ms_per_frame`, the console,
the unreachable resources, and the node paths of `strip.png` and `gate.png`
to `scp` — in the same shape `paid next`'s `done` now carries for the attempt
an entry kept (`paid.verdict_summary`). It is **advisory**: it writes no
`attempts` row, no entry, no transition and touches no job column, it spends
none of the job's three attempts, and `HARNESS_VERSION` is untouched, because
the gate did not change — only who asked it.

It runs **inside the worker's loop**, not beside it (DECIDE[try-where],
dossier 01 §5.3). The CLI writes the reply to `<jobs_dir>/N/try-K/reply.txt`
and a request into the `meta` row `paid_tries`, then polls for
`try-K/result.json` the way `next` polls; the worker serves pending requests
at the top of every pass before it claims, once per idle round while it is
standing by, and every five seconds of its nap while any lease is live — so a
try costs the gate's own 5–15 s plus at most five, not a 30 s poll. Because
the worker is one thread and never serves a try from inside a job, a try can
never overlap a real gate run: nothing measures `ms_per_frame` beside the
referee, and no unrelated job fails on frame budget because an agent was
looking at something. The lease is the whole permission model — a job leased
to somebody else, or to nobody, refuses with exit 3 — and every try renews
it. A request whose lease has lapsed by the time the worker reaches it is
dropped, unrun: nothing gates for an agent who left.

Tries are capped per job at the `meta` key **`paid_try_cap`**, default **8**
(a gate run is 5–15 s of the node's CPU; an agent that needs more is designing
on the node's clock). The ninth refuses with exit 3 and the count. The verb
that changes the cap is `paid budget --tries N` (below), because rule 4 means
nothing edits the row by hand. The count is kept per job and lands on the
attempt as part of its process cost: a first-attempt pass after four tries is
not a first-attempt pass.

**Two paid models on one job.** `--planner` / `--executor` also take another
*registered* paid id, so an operator can have one model plan and another
execute from off the node — the split the New job page gives two local models.
Asked for exactly that on 2026-09-21 (Sonnet 5 planning, Opus executing), a
Sonnet 5 session found `start` refusing any paid id but its own and, rightly,
stopped: one agent cannot honestly be two models, since the id on a packet is
the entry's provenance. So it is two sessions and a handoff that `next`
carries. The planner's `next` answers `handoff` the moment its plan is in,
naming the executor's command (`sketchgen paid next --job N --as <executor>`)
and dropping its lease; the executor's `next` answers `wait` while the other
agent plans (leaving the planner's lease alone), then takes the attempts and
the lease. Whichever agent runs `start` holds the lease first; the other takes
it over when the step is its own. The second id must be registered before
`start` — the operator's `paid models add`, so the name the second session
must answer as is chosen, not guessed — and `start` refuses otherwise, listing
what is registered. `preflight` shows a job parked for another agent with that
agent's `next` command and the `release` beside it, and the sweep above still
applies: the second session has a lease's length from the park to arrive, or
the job goes to this node's models. The entry records each
step's model as before: planner and executor differ, both badged off-node.

**The lease** (`meta` row `paid_leases`, `SKETCHGEN_PAID_LEASE_MINUTES`,
default 20) is how the worker knows an agent is at the other end. While one
is live the worker claims the leased job ahead of anything else queued and
does **no idle work** — no judge, no critique, no spawned child — so a paid
round trip costs one worker pass, not a pass plus ten minutes of filler. Every
`start`, `next`, `export --job` and `import` renews it; it lapses on its own,
so an agent that vanishes holds the idle loop for minutes, not the night. The
worker's card says `standing by for job N (model)` while it waits.

When a lease lapses, the job goes too. Every pass, the worker hands any job
parked for a plan or an attempt that has no live lease, and has been parked
for at least a lease's length, to this node's models — the same
`paid release` an operator would run, recorded as `handed to the local path
by the worker: parked N minutes for MODEL with no agent holding a lease`.
Before 2026-09-21 only the idle loop recovered: job 1263's agent
(gemini-3.8-flash) ran out of its weekly quota mid-plan, and the job would
have sat parked until the quota reset a week later.

**What an off-node step costs is recorded, honestly.** The node cannot meter a
model it did not run, so it measures what it can and records what it is told:
`round_trip_s`, export to import, goes on the attempt as `wall_s` (and into
`plan.json` for the plan), and the token counts an agent puts in an item's
`usage` land as `prompt_tokens` / `completion_tokens`; unreported counts stay
null, never zero. A Claude Code session can report them: its transcript
carries the API's `usage` on every assistant message, and the message that
wrote the reply has the reply's own — what it read (`input_tokens` and both
cache counts) and what it generated, thinking included. `rig/cost.py --reply
FILE` finds that message by the reply's text and prints both counts beside
`process`. Until 2026-09-22 nothing read them: job 1319 (entry 1312) landed
with its process recorded under #145 and both counts still blank, and so did
every paid entry before it. The entry page says "as reported by the model" and
"round trip, export to import" beside those numbers, and an off-node entry's shape
reads `off-node (MODEL) · gated on SHAPE` — the node only gated it. A local
entry's shape is the IMDS-identified one (`meta.node_shape`, e.g.
`VM.Standard.A1.Flex 16/96`) when the node has identified itself.

**And what the work *around* the reply cost is recorded too, apart from it.**
Migration 015, after job 1286 (entry 1279, 2026-09-21): the node saw a 111 s
round trip, and the 37 minutes and 193,000 generated tokens the agent spent
before `paid start` — a browser rig, a local prototype, a skill — were nowhere,
so the entry reads as a sketch written in under two minutes. Two costs, kept
apart (dossier 01 §6.4):

- **reply cost** — `wall_s` (the round trip the node timed) and `usage`
  (`prompt_tokens`, `completion_tokens`, as reported). This is what compares
  with a local model's prompt and decode, and it is what every batch total,
  the judge and the A/B read. Unchanged.
- **process cost** — `attempts.process_json`: `session_s`, `output_tokens`,
  `thinking_tokens`, `tool_calls`, `screenshots`, `effort` from the item's
  optional `process` at import, plus `tries`, which the node counts itself.
  Every field nullable and never defaulted to zero. It is copied to the entry
  at `_create_entry` from the attempt the entry kept, shows on the entry page
  as one **Process** row after *Wall seconds* (`42 min · 207,537 tokens
  generated · 4 tries · as reported by the agent`, an em dash per blank), goes
  into `meta.json` as `process`, and appears beside the attempt on the
  operator's job page. **Nothing measures with it** — not `pairs.py`, not the
  judge, not a batch total. On a job with `since_utc`, an execute import whose
  `process` is all null is *rejected* (the ordinary rejection: nothing written,
  the answer kept, the `rig/cost.py --since` command in the reason) unless
  `paid import --no-process` says the harness cannot report one; that
  declaration is kept in the attempt's `paid.json` as `process_unreported`.
  Job 1308 (entry 1300, 2026-09-21) is why: a `--since` job whose agent never
  ran `cost.py`, landed in silence, and a page of dashes that could not say
  whether the cost was uncountable or uncounted. An all-null `usage` on the
  same kind of job is rejected the same way, with the same command
  (`rig/cost.py --since … --reply FILE` fills both), unless `paid import
  --no-usage` says cost.py could not find the reply in one message of the
  transcript — a reply edited in place across several tool calls — and that
  is kept as `usage_unreported`. The process check comes first, so a packet
  missing both is told once.

Two more columns carry what only the agent knows: `jobs.since_utc` from `paid
start --since ISO` (what its first `date -u +%FT%TZ` printed; a stamp in the
future or over a day old is refused, exit 3, nothing queued) and `jobs.note`
from `--note TEXT` (`skill=algorithmic-art; local prototype`), which lands on
the entry as a **Note** row. `next` and `import` echo `elapsed`, from
`since_utc` when there is one and from the lease's own start when there is not,
saying which.

**The budget is advisory, and it has a verb.**

```bash
python3 bin/sketchgen paid budget                       # print it
python3 bin/sketchgen paid budget --minutes 10 --tokens 30000 --tries 8
```

It writes two `meta` rows — **`paid_budget`** (minutes and generated tokens,
defaults 10 and 30,000) and **`paid_try_cap`** (tries, default 8) — and `paid
start` prints them as one line. Past either number, `next` and `import` print
`over budget by …; finish, and report it` and record the work anyway:
refusing an over-budget import would reward not reporting
(`DECIDE[agent-budget]`), and the numbers are a strawman from job 1286's job
phase (4 min 26 s, 14,798 tokens) with headroom, to be re-set when
`MEASURE[freenode-baseline]` is read.

**Which checkout the node is on.** `preflight` and `start` report
`node_commit` (git `rev-parse HEAD` in the checkout the running code lives in,
null where there is no git and never a failure) and `agents_md_sha256` in
`info`. An agent whose `git merge-base --is-ancestor <node_commit> HEAD` fails
is reading an AGENTS.md older than the node's, which is how a session on
2026-09-21 answered #132's packets having read #131's instructions
(`DECIDE[freshness]`).

**A paid job is made only by `paid start`.** The New job page lists what this
node runs and nothing else; `paid assign --plan/--execute` take local tags
only; and a critique child never inherits a paid planner or executor
(`lineage.spawn` blanks them, so the child is made here). Each of those was a
way to make a job parked for an agent that did not exist — jobs 1246 and 1252
on 2026-09-21 — and the console counted them as broken for hours. A parked job
nobody is coming for is handed to this node's models with

```bash
python3 bin/sketchgen paid release --job N [--by WHO --reason TEXT]
```

which blanks its paid columns, re-queues it, and records who handed it back;
`preflight` lists such jobs with that command, and the worker runs it itself
a lease's length after the last lease lapsed. A job leased to another agent
is listed as `leased to X until T` with no command: it is theirs. An agent
that must stop uses the same verb on its own job.

**A paid executor is one round trip per attempt, and the gate stays here.** The
worker parks the job at the top of each attempt it has no reply for. `paid export
--step execute` renders the prompt that attempt would have sent — the brief, and
from attempt 2 on the gate's evidence from the attempt before — and `paid import`
checks the reply has a fenced js block, leaves it in `attempt-N/` and re-queues the
job. The resident worker then does the rest of the attempt exactly as for a local
model: parse, the real gate, evidence, and either held or parked again for attempt
N+1. Repeat the two commands until `db status` shows the job held (or failed at
`max_attempts`). A reply with no js block is rejected and the job stays parked, so
a bad reply does not use up an attempt.

A paid executor is a second variable in a running experiment — much larger than
the difference between two local models warned about under
[Which models run a job](#which-models-run-a-job). It is on every attempt and every
entry, and the entry is badged **off-node** in the gallery, so the analysis can
control for it; it is not controlled for you.

**One setting for all four steps.** `paid assign` names the model each step runs
with by default — one flag per step, or `--all` for "a paid model runs all
tasks":

```bash
python3 bin/sketchgen paid assign --all claude-opus-5
python3 bin/sketchgen paid assign --judge local       # unset one step
python3 bin/sketchgen paid assign                     # print it
```

`plan` and `execute` are what New job preselects (under any saved defaults of
its own) and what a job that names no model gets — a job's own planner and
executor always win, and the word `local` on a job still means the worker's
model. `judge` and `critique` are what the idle loop runs: a local tag replaces
`SKETCHGEN_JUDGE_MODEL` / `SKETCHGEN_CRITIC_MODEL`, and a paid model makes the
idle loop leave that step alone for `paid export`. With a step assigned,
`paid export --step S` needs no `--as`. New job shows the assignment under the
Run options, and says plainly what a paid executor does to the A/B.

**An exported critique is assigned, not raced.** `critiques` holds one row per entry
per prompt version, so an entry in a critique packet is claimed for the model the
packet was cut for, and the idle loop's local critic skips it until the answer is
imported. Exporting again re-offers that model's own claims first. A packet that is
never coming back:

```bash
python3 bin/sketchgen paid release --step critique --all   # or: 41 57
```

Unlike the idle critic, a paid critique that fails the validator writes **no**
rejected row: the entry stays claimed and un-critiqued, for a better answer.

**Never run `worker --once` to make a paid job move.** An import puts the job back
on the queue and the resident worker claims it on its next pass. Nothing in this
path runs a worker, and #118 is what happens when something does.

`judge export|import` still work, and `paid import` reads a `judge export` packet
too. `paid wait` is kept for scripts that used it (its default timeout is now
240 s); `paid next` is what to use.

## The write-path sync as a timer

`install-unit` also copies `systemd/sketchgen-sync.{service,timer}`. The service is a
oneshot `sketchgen sync --once` reading the bearer from `~/sketchgen/writepath.token`;
the timer runs it three minutes after boot and every five minutes after that.

```bash
systemctl --user enable --now sketchgen-sync.timer
systemctl --user list-timers --all | grep sketchgen-sync
journalctl --user -u sketchgen-sync -n 20
```

