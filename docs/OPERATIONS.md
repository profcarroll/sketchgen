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
python3 bin/sketchgen db status      # prints: control: running | pausing | paused
```

`running` takes jobs; `pausing` finishes the attempt in flight, releases the
inference slot and settles into `paused`; `paused` takes nothing. Use it to
de-contend the node when someone else wants the one inference slot, or for
maintenance. Packet 2.3 adds the subcommand that sets it. Stopping the unit
instead kills the attempt in flight; pause does not.

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

## The 10/1 shrink

Nothing to do. The unit encodes no core count, no memory figure and no
concurrency: one job at a time, fenced against other clients. The node gets
smaller, jobs get slower, the drip drips further apart.
