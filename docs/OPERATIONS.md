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

## The write-path sync as a timer

`install-unit` also copies `systemd/sketchgen-sync.{service,timer}`. The service is a
oneshot `sketchgen sync --once` reading the bearer from `~/sketchgen/writepath.token`;
the timer runs it three minutes after boot and every five minutes after that.

```bash
systemctl --user enable --now sketchgen-sync.timer
systemctl --user list-timers --all | grep sketchgen-sync
journalctl --user -u sketchgen-sync -n 20
```

