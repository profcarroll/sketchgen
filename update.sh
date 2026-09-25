#!/usr/bin/env bash
# update.sh — move the node to its build, re-render the gallery, restart services.
#
# Run from the laptop, for one node or all of them (docs/plans/fleet.md):
#   bin/fleet update sld-cloud            # as the sketchgen-update unit
# or by hand:
#   ssh sld-cloud 'bash ~/sketchgen/app/update.sh'
#
# "Its build" is main, unless `sketchgen pin` has pinned the node to one commit
# (an A/B arm): then the checkout goes to the pin and never past it, and this
# script only re-installs units and migrates. Before 2026-09-24 step 1 was
# `git pull origin main`, which fast-forwards a detached HEAD without a word,
# and three nodes were held on a72f076 for the hardware A/B by nothing else.
#
# Flags:
#   --no-render   Skip steps 3 and 4 (the gallery pull and the full re-render).
#                 Equivalent: SKETCHGEN_SKIP_RENDER=1. Use this ONLY when the
#                 change does not touch gallery output — operator UI (console.py,
#                 web.py), unit files, or worker logic. A change to gallery.py,
#                 a publisher template, or anything a generator writes onto an
#                 entry page MUST run the full render, because entry pages are
#                 written once and nothing else re-renders them (see step 4). When
#                 in doubt, leave it off: an unnecessary render costs minutes, a
#                 skipped necessary one ships stale pages.
#   --render      Force the render even if SKETCHGEN_SKIP_RENDER is set (default).
#   --render=auto Render only if the deploy changed something a page is made
#                 from: `sketchgen build --needs-render` reads the diff against
#                 the render path's own imports, templates and assets, and
#                 renders for anything it does not know. Equivalent:
#                 SKETCHGEN_RENDER=auto (which wins over SKETCHGEN_SKIP_RENDER).
#                 What bin/fleet passes, because it does not read the diff.
#   -h, --help    Print this and exit.
#
# What it does, in order:
#   0. Takes ~/sketchgen/update.lock: one deploy at a time on a node
#   1. Moves the generator repo (~/sketchgen/app) to its target — refusing,
#      before anything is paused, a dirty tree or a branch other than main
#   1a. If that move changed THIS script, hands over to the new copy
#   1b. Pauses the generator, letting the attempt in flight finish
#   2. Re-installs the unit files into ~/.config/systemd/user and reloads
#   2b. Applies any pending database migration (db init)
#   3. Pulls the gallery checkout (~/sketchgen/gallery)          [skipped by --no-render]
#   4. Re-renders the index AND every entry page, commits and pushes — through
#      publish-index, which holds the same checkout lock the publisher takes
#                                                                 [skipped by --no-render]
#   5. Restarts sketchgen-web and the resident generator onto the new code
#   6. Resumes the generator (from a trap, so a failure resumes it too)
#
# Safe to run more than once; every step is idempotent. --no-render changes what
# is deployed (it does not render), not whether a rerun is safe.
#
# It does NOT enable anything. Copying a unit is a code update; enabling one is a
# decision about what this node does, and that stays a keystroke (see
# docs/OPERATIONS.md). Step 2 prints the enable lines for anything not yet on.

set -euo pipefail

NODE_HOME="$HOME/sketchgen"
APP="$NODE_HOME/app"
GALLERY="$NODE_HOME/gallery"
VENV="$NODE_HOME/.venv/bin/python3"
DB="$NODE_HOME/sketchgen.db"

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }

step() { printf '\n\033[1;36m→ %s\033[0m\n' "$*"; }

die() { red "FAILED: $*" >&2; exit 1; }

usage() { sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'; }

# --- Flags -------------------------------------------------------------------
# The environment is read first so the setting survives the step-1a re-exec
# (env is exported; the flag is also carried on "$@"). SKETCHGEN_RENDER, when
# set, wins over the older SKETCHGEN_SKIP_RENDER: a copy of this script from
# before --render=auto exports SKIP_RENDER=no on its way to handing over, and
# the auto that bin/fleet asked for must survive that. The flags then have the
# last word, in the order given.
case "${SKETCHGEN_SKIP_RENDER:-}" in 1|yes|true|YES|Yes) RENDER=no ;; *) RENDER=yes ;; esac
case "${SKETCHGEN_RENDER:-}" in
    auto) RENDER=auto ;; yes) RENDER=yes ;; no) RENDER=no ;;
    "") ;; *) die "SKETCHGEN_RENDER must be auto, yes or no, not '$SKETCHGEN_RENDER'" ;;
esac
for arg in "$@"; do
    case "$arg" in
        --no-render|--render=no)  RENDER=no ;;
        --render|--render=yes)    RENDER=yes ;;
        --render=auto)            RENDER=auto ;;
        -h|--help)   usage; exit 0 ;;
        *)           die "unknown argument: $arg (try --help)" ;;
    esac
done
export SKETCHGEN_RENDER="$RENDER"

# --- The generator, which is hopefully running -------------------------------
# The worker is resident on this node (sketchgen-worker.service, not the timer),
# so it is producing sketches while this script runs. Two things follow.
#
# It publishes into the same gallery checkout this script re-renders. publish()
# and publish_index() hold an flock on that checkout for exactly that reason
# (the entry 488 race, 2026-09-16); step 4 below now goes through publish-index
# so it holds the same lock, instead of doing its own git add/commit/push beside
# a publisher that is mid-render.
#
# And it is a long-lived process, so it runs whatever code it started with until
# something restarts it. Step 6 does.
#
# The pause is the database switch, not systemctl: it lets the attempt in flight
# finish and then takes no more jobs, so nothing is killed and no job is
# re-queued. We only resume what WE paused — an operator who paused this node on
# purpose finds it still paused afterwards.
RESUME_AFTER=no

control_state() {
    "$VENV" -c "
import sys
sys.path.insert(0, '$APP')
from sketchgen import db
control = db.get_control(db.connect('$DB'))
print(control.state if control else 'missing')
" 2>/dev/null || echo unknown
}

# Non-zero when it tried to resume and could not, so the caller can refuse to
# call the deploy a success: a node left paused makes nothing, and it does that
# silently.
resume_generator() {
    [ "$RESUME_AFTER" = yes ] || return 0
    RESUME_AFTER=no
    if "$VENV" "$APP/bin/sketchgen" control resume --db "$DB"; then
        green "generator resumed"
        return 0
    fi
    red "COULD NOT RESUME THE GENERATOR — this node is paused and making nothing."
    red "Run this yourself: $VENV $APP/bin/sketchgen control resume --db $DB"
    return 1
}

# On the way out however we leave — success, die, or Ctrl-C — because a deploy
# that fails must not leave the node quietly making nothing. The trap is the
# last word on an already-failing path, so it never fails harder; the explicit
# call at the end is the one that decides whether this run succeeded.
trap 'resume_generator || true' EXIT

# --- 0. One deploy at a time ------------------------------------------------
# bin/fleet and the Console both start this as the sketchgen-update unit, whose
# name systemd will not give to two runs; a hand `ssh NODE update.sh` has no
# unit. The lock covers all three. A copy handed over in 1a inherits the fd and
# the lock with it, so there is no moment between the two copies when a second
# deploy could slip in.
LOCK="$NODE_HOME/update.lock"
if [ "${SKETCHGEN_UPDATE_REEXEC:-}" != yes ] || ! { true >&9; } 2>/dev/null; then
    exec 9>>"$LOCK"
fi
flock -n 9 || die "another update.sh is running on this node (it holds $LOCK)"

# --- 1. Move the generator to its target -------------------------------------
SELF="$APP/update.sh"
script_hash() { sha256sum "$SELF" 2>/dev/null | cut -d' ' -f1; }
self_before=$(script_hash)

# One meta row, read-only and without the package, so it reads the same
# whatever the checkout it is about to move holds.
meta_value() {
    "$VENV" - "$DB" "$1" 2>/dev/null <<'PY' || true
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (sys.argv[2],)).fetchone()
    print(row[0] if row and row[0] else "")
except sqlite3.Error:
    print("")
PY
}

step "Moving the generator to its build (~/sketchgen/app)"
cd "$APP" || die "cannot cd to $APP"
# Both refusals come before anything moves or pauses. The second is the
# 2026-09-22 case: a fix committed on sld-cloud on a branch nobody pushed, and
# a pull that failed on divergent branches — which was the right outcome, now
# said in words instead of git's.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    git status --short --untracked-files=no | sed 's/^/    /' >&2
    die "tracked files are changed in $APP — put them on a branch and a PR, or 'git checkout -- .'; nothing was moved or paused"
fi
branch=$(git symbolic-ref --quiet --short HEAD || true)
if [ -n "$branch" ] && [ "$branch" != main ]; then
    die "$APP is on branch '$branch', not main — 'git switch main' first; nothing was moved or paused"
fi
# Where this deploy started, for --render=auto. A copy handed over in 1a is
# told by the copy before it; one handed over by a copy that predates the
# variable is not, and then nobody knows, and auto renders.
if [ "${SKETCHGEN_UPDATE_REEXEC:-}" = yes ]; then
    from="${SKETCHGEN_UPDATE_FROM:-}"
else
    from=$(git rev-parse HEAD)
fi

pin=$(meta_value pin.sha)
# A pinned node needs nothing from GitHub if it already has its commit, and a
# private box that cannot reach it should still get its units and migration.
if ! git fetch --quiet origin main; then
    [ -n "$pin" ] || die "git fetch failed in $APP"
    dim "git fetch failed; going on, because this node is pinned"
fi
if [ -n "$pin" ]; then
    if ! git cat-file -e "${pin}^{commit}" 2>/dev/null; then
        git fetch --quiet origin || true
        git cat-file -e "${pin}^{commit}" 2>/dev/null \
            || die "this node is pinned to $pin, which this checkout does not have"
    fi
    if [ "$(git rev-parse HEAD)" != "$pin" ]; then
        git switch --quiet --detach "$pin" || die "could not move to the pin $pin"
    fi
    green "pinned at ${pin:0:7} — $(meta_value pin.reason)"
    dim "the code stays here; 'sketchgen pin --clear --by LOGIN' to follow main again"
else
    # A local main with commits of its own cannot fast-forward, and finding
    # that out after switching to it would leave the checkout moved under a
    # worker that has not been paused.
    if git show-ref --verify --quiet refs/heads/main \
            && ! git merge-base --is-ancestor main origin/main; then
        die "local main has commits origin/main does not — PR them first; nothing was moved or paused"
    fi
    [ -n "$branch" ] || git switch --quiet main || die "could not switch to main"
    git merge --quiet --ff-only origin/main || die "could not fast-forward main to origin/main"
    green "on main at $(git rev-parse --short HEAD)"
fi

# --- 1a. Hand over to the pulled copy of this script -------------------------
# bash reads a script as it runs it, from the file it was given when it
# started, so a pull that changes update.sh does not change the run that
# pulled it: the rest of this run is still the old script, and whatever the
# new one added — a migration step on 2026-09-16, the render's --progress flag
# on 2026-09-18 — silently does not happen until the deploy after. Twice that
# has looked like a broken feature. So when the pull changed the file on disk,
# exec the new copy: same arguments, same environment, nothing of this run to
# undo yet (the generator is not paused until 1b). The guard stops a loop if a
# file somehow changes on every pull; the second copy runs on regardless.
if [ "${SKETCHGEN_UPDATE_REEXEC:-}" != yes ] && [ "$(script_hash)" != "$self_before" ]; then
    dim "update.sh itself changed in that move — handing over to the new copy"
    export SKETCHGEN_UPDATE_REEXEC=yes
    export SKETCHGEN_UPDATE_FROM="$from"
    exec bash "$SELF" "$@"
fi

# --- Whether this deploy has to re-render (steps 3 and 4) -------------------
# --render=auto asks the new code, which knows its own render path, about the
# whole deploy: from where the first copy started to where the checkout is now.
to=$(git rev-parse HEAD)
if [ "$RENDER" = auto ]; then
    if [ -z "$from" ]; then
        RENDER=yes
        dim "render: the copy that moved the checkout did not say where it started"
    else
        verdict=$("$VENV" bin/sketchgen build --needs-render "$from" "$to" 2>/dev/null) \
            || verdict="render the diff could not be read"
        case "$verdict" in
            skip*) RENDER=no ;;
            *)     RENDER=yes ;;
        esac
        dim "render=auto: ${verdict}"
    fi
fi
if [ "$RENDER" = no ]; then SKIP_RENDER=yes; else SKIP_RENDER=no; fi

# --- 1b. Pause the generator -------------------------------------------------
step "Pausing the generator for the deploy"
cd "$APP"
state=$(control_state)
if [ "$state" = running ]; then
    "$VENV" "$APP/bin/sketchgen" control pause --db "$DB" \
        --reason "update.sh deploy" || die "control pause failed"
    RESUME_AFTER=yes
    # pausing -> paused when the attempt in flight ends. An attempt is minutes:
    # a ten-minute ceiling is generous and still bounded.
    settled=no
    for _ in $(seq 1 120); do
        if [ "$(control_state)" = paused ]; then settled=yes; break; fi
        sleep 5
    done
    [ "$settled" = yes ] || die "the generator did not settle into paused in 10 minutes"
    green "generator paused (it will be resumed on the way out)"
else
    dim "generator is '$state', not 'running' — leaving the switch exactly as it is"
fi

# --- 2. Re-install the unit files --------------------------------------------
# The units are tracked, so a pull can change them — a new Environment= line, a
# new timer, a corrected path. Copying them here is what makes that true on the
# node without a second command nobody remembers to run. install-unit copies and
# reloads; it never enables, and it prints what is still waiting to be enabled.
step "Re-installing unit files"
cd "$APP"
"$VENV" bin/sketchgen install-unit || die "install-unit failed"
green "unit files up to date"

# --- 2b. Migrate the database ------------------------------------------------
# A pull can bring a migration with it, and the code that follows assumes the
# schema it was written against: the 2026-09-15 deploy of packet 2 ran the
# backfill before anyone ran `db init`, and it fell over on a column that was
# not there yet. `db init` applies what is pending and is a no-op otherwise.
#
# A pending migration is snapshotted first, as AGENTS.md has always asked of a
# person: `sketchgen.db.pre-NNN`, NNN the newest migration this deploy brings.
# With bin/fleet one command migrates every node, and a rule kept by hand on
# each of them is a rule skipped on one. An existing snapshot of that name is
# kept, not overwritten: a rerun after a failed migration must not replace the
# good copy with the half-done one.
step "Migrating the database"
cd "$APP"
pending=$("$VENV" - "$DB" "$APP/migrations" 2>/dev/null <<'PY' || true
import re, sqlite3, sys
from pathlib import Path
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    have = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
except sqlite3.Error:
    have = 0
names = (re.match(r"(\d+)_", p.name) for p in Path(sys.argv[2]).glob("*.sql"))
want = max((int(m.group(1)) for m in names if m), default=0)
print(f"{want:03d}" if want > have else "")
PY
)
if [ -n "$pending" ] && [ -f "$DB" ]; then
    snapshot="$DB.pre-$pending"
    if [ -e "$snapshot" ]; then
        dim "a snapshot for migration $pending is already here, and kept: $snapshot"
    else
        "$VENV" - "$DB" "$snapshot" <<'PY' || die "could not snapshot the database before migrating; nothing was migrated"
import sqlite3, sys
source, copy = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
source.backup(copy)
copy.close()
source.close()
PY
        green "snapshot before migrating: $snapshot"
    fi
fi
"$VENV" bin/sketchgen db init --db "$DB" || die "db init (migrate) failed"
green "database schema up to date"

# --- 3 & 4. The gallery, unless --no-render ----------------------------------
if [ "$SKIP_RENDER" = yes ]; then
    step "Skipping the gallery pull and re-render"
    dim "deploying on the assumption this change touches no entry page or the index"
else

# --- 3. Pull the gallery checkout --------------------------------------------
step "Pulling gallery checkout (~/sketchgen/gallery)"
cd "$GALLERY" || die "cannot cd to $GALLERY"
git pull origin main || die "git pull failed in $GALLERY"
green "gallery checkout up to date"

# --- 4. Re-render and push the whole gallery ---------------------------------
# Through publish-index, not render-all plus a hand-rolled commit, because
# publish_index() holds the checkout flock for the whole thing — clean-tree
# check, render, commit, push — and the worker's publisher takes that same lock.
# Doing it by hand here meant this script rendered five hundred pages into a
# working tree that a publish could reset underneath it (publish.py::_undo runs
# git reset --hard on the shared tree), and nothing would have reported it: the
# pages would just have been wrong.
#
# It renders every public entry and the index. An entry page is written once, by
# the publisher, at the moment it goes up; nothing else re-renders it, so without
# this a template change, a new panel or a fixed generator reaches index.html and
# never reaches the hundreds of pages that ARE the gallery. That is how 36
# entries went up with no critique form. Same database, same config, so a page
# already current comes out byte-identical and there is nothing to commit for it.
# First the web copies of the gate's frames, which the render publishes in
# place of the PNGs (gallery-hub.md, Step 0). A no-op once every entry has
# them; the first run after 2026-09-22 made ~1,200. Not fatal: an entry with
# no copy publishes its PNG, as all of them used to.
step "Making web copies of the gate's frames"
cd "$APP"
"$VENV" bin/sketchgen web-frames --all --db "$DB" \
    || dim "web-frames did not finish; entries without a copy publish their PNG"

step "Re-rendering and pushing the gallery (index and every entry)"
cd "$APP"
# --progress: one line redrawn per page, with the count and an estimate. Five
# hundred pages is minutes and only ever more; a silent minutes-long step is
# indistinguishable from a hang, and got killed as one.
"$VENV" bin/sketchgen publish-index \
    --db "$DB" \
    --gallery-dir "$GALLERY" \
    --progress \
    || die "publish-index failed"
green "gallery re-rendered and pushed"
fi

# --- 5. Restart the services so they run the code we just pulled --------------
# The worker is the point of this step. On this node it is resident
# (sketchgen-worker.service), so it runs whatever it started with until
# something restarts it: every deploy before this one left the generator on the
# old code while the web UI moved on, and nothing said so. It is still paused
# here, so the restart interrupts nothing and it comes back up paused; the trap
# resumes it at the very end.
#
# Only if it is actually active. This script does not enable anything (see the
# header): a node deliberately running the timer instead, or nothing at all,
# keeps whatever the operator chose.
step "Restarting sketchgen-web"
systemctl --user restart sketchgen-web.service
sleep 1
systemctl --user is-active sketchgen-web.service >/dev/null 2>&1 \
    && green "sketchgen-web is running" \
    || die "sketchgen-web failed to start"

step "Restarting the generator onto the new code"
if systemctl --user is-active sketchgen-worker.service >/dev/null 2>&1; then
    systemctl --user restart sketchgen-worker.service
    sleep 1
    systemctl --user is-active sketchgen-worker.service >/dev/null 2>&1 \
        && green "sketchgen-worker restarted (paused; resumed below)" \
        || die "sketchgen-worker failed to start"
else
    dim "sketchgen-worker.service is not active — nothing to restart"
fi

# --- 6. Resume ---------------------------------------------------------------
# Explicitly, so that "all done" is the last thing printed and means it. The
# trap stays armed for every path that does NOT reach here; resume_generator
# disarms itself, so this and the trap cannot both fire.
step "Resuming the generator"
resume_generator || die "the deploy landed but the generator is still paused"

echo ""
green "✓ all done"
