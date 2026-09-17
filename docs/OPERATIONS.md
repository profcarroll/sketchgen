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

## Watching one job

```
python3 bin/sketchgen worker --once            # one job, then exit
tail -f ~/sketchgen/jobs/<id>/job.log          # the same lines the unit logs
```

Each job has a directory under `$SKETCHGEN_JOBS` (default `~/sketchgen/jobs`):
`job.log`, then `attempt-1/`, `attempt-2/` … holding the prompt, the raw response,
the sketch, and the gate's own output under `attempt-N/.gate/`. That is the
canonical record of what happened; the database rows point at it.

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

So it is two commands. Ask on the operator's machine, record on the node:

```
# on the operator's machine, where ~/.oci lives
python3 bin/sketchgen billing                    # per service, and a total

# then put that figure where the console can see it
amount=$(python3 bin/sketchgen billing --amount-only)
ssh sld-cloud "~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen \
    billing --record --amount $amount --through $(date -u +%F) \
    --db ~/sketchgen/sketchgen.db"
```

`--record` takes the number as an argument and touches no OCI endpoint, which is
what lets it run on the node at all.

The card shows the figure, the window it covers and when it was last checked,
and marks itself once the reading is over a week old. It never presents itself
as live: Oracle's usage data lags a day or more, so a card claiming to be
current would be wrong twice over.

**As of 2026-09-17 the answer is $0.00** — every service, Compute and Block
Storage and VCN and Telemetry, from 1 August. The node is a
`VM.Standard.A1.Flex 16/94`, which is four times the documented Always Free ARM
allowance of 4 OCPU / 24 GB, and it is still being billed at nothing. Worth
re-checking before that figure is quoted anywhere public, which is what the card
is for.

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

## The 10/1 shrink

Nothing to do. The unit encodes no core count, no memory figure and no
concurrency: one job at a time, fenced against other clients. The node gets
smaller, jobs get slower, the drip drips further apart.

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

## The write-path sync as a timer

`install-unit` also copies `systemd/sketchgen-sync.{service,timer}`. The service is a
oneshot `sketchgen sync --once` reading the bearer from `~/sketchgen/writepath.token`;
the timer runs it three minutes after boot and every five minutes after that.

```bash
systemctl --user enable --now sketchgen-sync.timer
systemctl --user list-timers --all | grep sketchgen-sync
journalctl --user -u sketchgen-sync -n 20
```

