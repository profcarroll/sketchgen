# The fleet: four nodes, one build, and how we would know

As of 2026-09-24 sketchgen runs on four nodes: `sld-cloud` (the public gallery),
`d12-node-profcarroll` and `d12-node-flux` in the D12 lab, and the rented A10 `sld-gpu`
until 09-28. Each was deployed by hand, by `ssh NODE 'bash ~/sketchgen/app/update.sh'` or
`install.sh --ref`. The operator's question is simple: are they all running the same build?
Today nothing can answer it without an ssh session per node. And the answer, once you get it,
is not quite trustworthy (§0).

Two directions were on the table. **Push**: a script on the laptop that checks and deploys every
node. **Pull**: the Console gets a *Check for updates* badge and an *Update* button, so each node
brings itself up to date. This plan does both, in that order. Both need the same thing first,
which neither has today: a node that knows which build it is meant to be on.

**Status, 2026-09-24.** Packets 1 and 2 are built (`feat/fleet`), prompted by the
improved gate on sld-cloud (HARNESS 5 and 6, #176 and #178) that the GPU nodes need
between their batches. Built as written, with these differences:

- `--render=auto` does not use a list of render modules. It computes them from the
  imports of what step 4 runs (`build.render_closure`), and skips only for paths known
  not to reach a page. The render path imports `worker.py`, so a worker change renders.
- `bin/fleet update` also refuses a node with work in hand (`--mid-batch` overrides),
  because "between batches" is the reason for this build. There is also a
  `bin/fleet pin`, which pins several nodes in one command.
- `/build` on the Console has **Check now** (a fetch, nothing else). The *Update*
  button is still Packet 3.
- Not built yet: `sgt <name>` reading the fleet file (§2.1; the four `sgt-*`
  functions stay as they are), and the write-path Worker's `/version` (§2.4).

## 0. What is true today

A read-only survey on 2026-09-24, around 03:00Z:

| node | checkout | how it got there | what `git status` on the node says |
| --- | --- | --- | --- |
| sld-cloud | `89d633b` (#170), on `main` | `pull origin main`, 09-24 00:57Z | *up to date with origin/main*, but main is `e47b44e` (#174), four PRs later. The node has not fetched since. |
| d12-node-profcarroll | `a72f076` (#165), on `main` | `pull origin main`, 09-23 03:52Z | the same "up to date", nine PRs behind |
| sld-gpu | `a72f076`, detached | `install.sh --ref a72f076` | detached HEAD |
| d12-node-flux | `a72f076`, detached | `install.sh --ref a72f076` | detached HEAD |

All four are on schema 17 and all four generators are running. Three things in that table are
traps:

1. **A pin is only ever implicit.** The three nodes on `a72f076` are there on purpose: the
   hardware A/B needs one build on every arm. But nothing records that. On d12 the only thing
   holding the pin is that nobody has run `update.sh` there, and on the other two it is a
   detached HEAD. `update.sh` step 1 is `git pull origin main`, and a pull on a detached HEAD
   fast-forwards it to main without a word. That was checked on a scratch clone, and it is the
   same on a branch. So running a routine deploy on any of the three ends the A/B, and so would
   an *Update* button built on today's `update.sh`: one click, and a silent one.
2. **Nothing records which build ran a job.** The hardware A/B memo says *both nodes on build
   a72f076*. sld-cloud's reflog says its checkout moved to `995a9d8` (#166, #167) at
   2026-09-23 05:23:40Z. Of arm B's attempts (jobs 1373–1473), 7 started before that and 141
   after. The arm is clean anyway: neither PR touches `planner.py`, `executor.py`, `worker.py`,
   `preflight.py`, `prompts/` or `gate/`. But it took the reflog, `attempts.started_utc` and a
   `git diff` to show that. It also only proves where the *checkout* was. Whether the worker was
   restarted onto that checkout, nothing kept.
3. **The running code is not the checkout.** The worker is a long-lived process and runs
   whatever it imported when it started. OPERATIONS.md's own shortcut for a UI-only change is
   *Restarting only the operator UI by hand*: `git pull`, then restart `sketchgen-web`. That
   leaves the worker on the old code. Nothing on the Console shows a build at all, so nothing
   shows the gap.

"Confidently the same build" therefore means three facts per node: where the code is meant to
be (its **target**, which is main or a pin), where the checkout **is**, and what the processes
are **running**. It also needs a record of that last fact on every attempt.

## 1. Packet 1 — `feat/build-identity`: each node knows its build

Everything else depends on this packet. It is also the half of the pull direction that is safe
to have: a node that is honest about itself.

### 1.1 `sketchgen build`

```
$ sketchgen build --fetch
checkout  e47b44e  Merge pull request #174 …          clean, on main
target    main                                          → e47b44e, 0 behind (fetched just now)
worker    e47b44e  since 2026-09-24T03:10Z
web       e47b44e  since 2026-09-24T03:10Z
schema    17
on target
```

On a pinned node the target line reads `pinned a72f076 — "hardware A/B" (profcarroll,
2026-09-23) · main is 9 ahead`. "Behind" and "ahead" count PRs, the merges on main's
first-parent line, because the operator thinks in PRs. `--json` gives the same thing as a
document. Exit 0 means the checkout is the target's commit, the tree is clean, and both
processes run the checkout. Exit 1 means one of those is false, and the last line names which.
The verb never exits 3: it is a reading, not a check. `--fetch` runs `git fetch origin main`
first, with a 10 s timeout. When it is offline it says so and reports the last fetch.

`paid.node_commit()` moves into a small shared module (`sketchgen/build.py`), and `paid
preflight` reads it from there.

### 1.2 The running build

The worker and the web process each resolve `HEAD` once at start and write
`meta` `run.worker` / `run.web` = `<sha> <utc>`. A pull without a restart then shows up on the
next reading as `worker 89d633b ≠ checkout e47b44e: restart pending`. That is the §0.3 gap, with
a name.

### 1.3 Every attempt carries its build

Migration 018 adds two nullable columns, additive, with no CHECK to rebuild: `attempts.build`
and `jobs.plan_build`. The worker writes its *running* build (from 1.2, not the checkout's) when
it starts the step. A paid attempt gets the node's build too, because the node's gate judged it.
After this, the A/B question from §0.2 is one query:

```sql
SELECT build, count(*) FROM attempts WHERE job_id BETWEEN 1373 AND 1473 GROUP BY build;
```

One row means one build. Rows from before 018 stay NULL, which is true: nobody recorded it.

### 1.4 Pins are declared

```
sketchgen pin a72f076 --reason "hardware A/B, through 09-28" --by profcarroll
sketchgen pin --clear --by profcarroll
```

A pin resolves its ref to a full sha when it is set: a branch or tag moves, and a pin must not.
It refuses a ref the checkout cannot find after a fetch. It is stored as `meta` rows (`pin.sha`,
`pin.reason`, `pin.by`, `pin.utc`), which puts it in the nightly snapshot and within the
Console's reach, and gives it a verb (AGENTS.md rule 4). `install.sh --ref X`, for any X other
than `main`, now writes the pin in step 5, so an A/B arm is pinned by the command that made it,
not by a detached HEAD.

### 1.5 `update.sh` goes to the target, and never past it

Step 1 changes from `git pull origin main` to:

- **Refuse, before anything is paused**, if the tree is dirty or `HEAD` is on a branch other
  than `main`. Today's pull already fails on the second case (2026-09-22, a fix committed on the
  node); this makes both explicit and puts them in the message.
- `git fetch origin main`.
- **Target main:** `git switch main && git merge --ff-only origin/main`.
  **Pinned:** `git switch --detach <pin.sha>`, a no-op when it is already there. So `update.sh`
  on a pinned node is safe: it re-installs units and migrates, and it does not move the code.
- **One at a time:** `flock -n ~/sketchgen/update.lock` around the whole run. A second deploy,
  from another terminal, the fleet script or the button, refuses instead of racing the first.
- **`--render=auto`**, opt-in for now. It renders only if `git diff --name-only OLD NEW` touches
  anything outside a short list known not to reach a page: `console.py`, `web.py`,
  `templates/op_*`, `systemd/`, `docs/`, `tests/`, `*.md`. Anything unlisted renders, and the
  list grows only by a PR that shows a file cannot reach a page. This is the judgment the header asks a person to make, written down, and the fleet
  script and the button need it because neither of them reads the diff.

**The trap in deploying this.** The deploy that brings the new step 1 is pulled by the old one
(the `update-sh-self-change` pattern). On sld-cloud that is fine, because it follows main. On
the three pinned nodes, deploying Packet 1 *is* the unpin. They stay on `a72f076` until their
A/B is over, and the fleet script reads them without the verb (§2.2).

### 1.6 The badge

The Console header gets a chip beside the generator pill:

| chip | when | look |
| --- | --- | --- |
| `e47b44e` | on target, running it | quiet |
| `4 behind` | following main, main has moved | amber: *update available* |
| `pinned a72f076` | a pin; the reason on hover, `· main is 9 ahead` beside it | neutral: behind on purpose |
| `restart pending` | a process runs a build other than the checkout | red |
| `dirty` / `off main` | what `update.sh` would refuse | red |

The web process fetches in a background thread every 15 minutes, with a 10 s timeout, and the
chip says when it last looked. A failed fetch never fails the page. Clicking the chip opens a
small section on the Node card, `git log --oneline HEAD..origin/main`, so that *4 behind* reads
as the four PR titles. That is the *Check for updates* badge from the request. It does not need
the button.

### 1.7 Tests

`sketchgen build` and `pin`, against a scratch repo built in `setUp`: clean, dirty, detached,
pinned, behind, a run stamp that differs from the checkout. Step 1 of `update.sh`, extracted into
a function the test runs with stub tools on `PATH`, as `test_install_sh.py` does: a pinned node
stays put, main fast-forwards, a dirty tree and a foreign branch refuse before any pause, and a
second run refuses on the lock. Migration 018 goes through the usual up-from-017 test. The
`--render=auto` classifier gets a table test of paths.

## 2. Packet 2 — `feat/fleet`: one answer, from the laptop

The question in the request is about the fleet, not about one node, so it can only be answered
somewhere that sees all of them. That place is the laptop, which already reaches every node
(`bin/sg`, the tunnels).

### 2.1 One file names the fleet

`~/.config/sketchgen/fleet`, one node per line:

```
sld-cloud  sld-cloud             8081  billing
d12        d12-node-profcarroll  8082
sld-gpu    sld-gpu               8083  billing
d12-flux   d12-node-flux         8084
```

The four hand-written `sgt-*` functions in `~/.bashrc.d/sketchgen-tunnel.sh` become
`sgt <name>`, reading the same file. A fifth node is then one line, not a new function and a
port to remember. The file stays a laptop dotfile, and the repository ships `fleet.example`.

### 2.2 `bin/fleet status`

The laptop fetches `origin/main` once. That fetch is the single definition of "main" for the
whole report, and nodes need not reach GitHub to be read. Then one ssh per node, in parallel,
for about 3 s in total: `sketchgen build --json` where the verb exists, and otherwise the same
facts from raw `git` and `meta`, marked *(predates `build`)*. Today it would print:

```
node      checkout  target                 vs main    running   generator
sld-cloud 89d633b   main                   4 behind   ?         running
d12       a72f076   — (no pin; on main)    9 behind   ?         running
sld-gpu   a72f076   — (no pin; detached)   9 behind   ?         running
d12-flux  a72f076   — (no pin; detached)   9 behind   ?         running

2 builds on 4 nodes: 89d633b (sld-cloud) · a72f076 (d12, sld-gpu, d12-flux)
not on target: sld-cloud (4 behind main)
unpinned and behind: d12, sld-gpu, d12-flux: a deploy would move them to main
```

Exit 0 only if every node is on its target, clean, and running its checkout. `--same` also
requires a single build across the fleet: that is the "confidently all the same" check, and an
A/B can assert it before it enqueues. A node sha that is not on main at all (a commit made on
the node, as on 2026-09-22) is printed in red as *not on main*.

### 2.3 `bin/fleet update NODE… | --all [--render=auto|yes|no]`

For each node it starts `update.sh` as a transient user unit:
`systemd-run --user --unit=sketchgen-update --collect bash ~/sketchgen/app/update.sh …`. It then
returns at once with one *started* line per node, and `bin/fleet watch` follows the units'
journals until they finish, ending with a `status`. Two reasons for the unit:

- **It survives the laptop.** Today a dropped ssh mid-deploy sends `update.sh` a SIGHUP. Its trap
  resumes the generator, but the render or the migration is left half done.
- **Its name is a lock** that the button (§3) shares. systemd refuses a second
  `sketchgen-update` while one is active, whoever started it.

Returning at once is deliberate. sld-cloud's render is about 25 minutes, and a laptop command
that blocks for that long gets killed as a hang (the `--progress` lesson, 2026-09-18).

**`--all` refuses any node that predates `build`** unless it is named with `--adopt`. On the
first day that means all three A/B nodes, and so this rule is what stops `bin/fleet update --all`
from ending the A/B on day one.

### 2.4 The fifth deployable

The write-path Worker drifts behind main and nothing notices (the `deploy-order` memo, item 4,
2026-09-19). `worker.js` gains `GET /version`, set at deploy with `wrangler deploy --var
BUILD:<sha>`, and `bin/fleet status` prints it as a row. The Worker is still deployed by hand,
but the drift becomes visible.

### 2.5 Tests

`bin/fleet` goes through `SKETCHGEN_SSH` and a fake, as `tests/test_sg.py` already does for
`bin/sg`. The cases: a mixed fleet, a node that predates the verb, an unreachable node (a row
saying so, never a hang, with a 10 s timeout per node), `--same`, and `--all` refusing without
`--adopt`.

## 3. Packet 3 — `feat/console-update`: the button, later

- The amber chip opens a confirm with the commits, whether `--render=auto` would render, and the
  last render's duration. Pressing it has the web process start the same `sketchgen-update`
  unit as §2.3. **It never runs `update.sh` as its own child:** step 5 restarts `sketchgen-web`,
  which would kill the deploy halfway, with the generator paused and a migration or render
  unfinished.
- The Node card follows the unit's journal. The page's two-second poll has to ride out the web
  restart and come back on the new build; that is the one thing to verify in a browser.
- There is no button on a pinned node, where the chip says why. On sld-cloud a second confirm
  names the public gallery push.

**Why later.** For the operator's own four nodes, `bin/fleet update --all` is one command, where
the button is four tunnels, four tabs and four clicks. The button also cannot answer "are they all
the same". It is the right tool for a node whose operator is not at this laptop: a lab box
somebody else runs, or the gallery hub's multi-node future (`gallery-hub.md`, Step 3). Build it
when that is true.

**Not proposed: nodes that update themselves.** A timer pulling main is the far end of the pull
direction. It would have moved sld-cloud in the middle of arm B, as the hand pull did, and it
would have run each of September's deploys with nobody watching. Every one of them had a trap
that a person caught: migration 008 behind the web restart (09-16), `--progress` (09-18), the
divergent branch (09-22), the D1 order (09-16). The badge is how a node asks, and a
person answers.

## 4. Order of work

1. **Packet 1**, deployed to sld-cloud only. The three A/B nodes are not touched.
2. **Packet 2.** `bin/fleet status` reads the three old nodes through raw `git`, and from then on
   the fleet is visible even where it cannot yet be moved.
3. When an A/B arm ends, run `bin/fleet update d12 --adopt`: the node comes onto main with
   Packet 1. From then on an A/B is `SKETCHGEN_NODE=… bin/sg pin <sha> --reason …` on every arm,
   then `bin/fleet status --same` before the first job is enqueued. sld-gpu is terminated on
   09-28 and never adopts.
4. **Packet 3**, when a node has an operator of its own.

## 5. Decisions for the operator

1. **Push first, then pull** (recommended), meaning Packets 1–2 now and the button when it has a
   user. The alternative is the button first. It is quicker to feel, but it cannot say whether
   the fleet agrees, and it needs Packet 1's pin and lock anyway.
2. **Where a pin lives: `meta`, through `sketchgen pin`** (recommended), or a file beside
   `node.conf`. The file can be written by hand before the code exists, but that buys nothing:
   the old `update.sh` pulls before anything could read it.
3. **`--render=auto`**: opt-in for one release, then the default (recommended), or opt-in for
   good.
4. **Build on public entry pages?** Recommended no. The A/B reads it from the database, and a
   gallery visitor has no use for a sha.
5. **d12 and d12-flux**: when their arms end, adopt them onto main or pin them for the next A/B.
