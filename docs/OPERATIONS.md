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

Exit 0 is clean, 1 is something shadowed, 2 is a directory with no `sketch.js`.

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

The entry must be `held` or `failed-kept`, and the gallery checkout (`--gallery-dir`,
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
ssh -f -N -L 8081:127.0.0.1:8081 sld-cloud-new && open http://localhost:8081/held
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
ssh -f -N -L 8081:127.0.0.1:8081 sld-cloud                # on the laptop
```

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
reject, with the kept rejections below them named on hover. **Gallery** has a dim
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

