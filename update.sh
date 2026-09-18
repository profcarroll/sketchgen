#!/usr/bin/env bash
# update.sh — pull code on the node, re-render the gallery, and restart services.
#
# Run from anywhere:
#   ssh sld-cloud 'bash ~/sketchgen/app/update.sh'
# or if this file is on the local machine:
#   ssh sld-cloud 'bash -s' < update.sh
#
# What it does, in order:
#   1. Pulls the generator repo (~/sketchgen/app)
#   1b. Pauses the generator, letting the attempt in flight finish
#   2. Re-installs the unit files into ~/.config/systemd/user and reloads
#   2b. Applies any pending database migration (db init)
#   3. Pulls the gallery checkout (~/sketchgen/gallery)
#   4. Re-renders the index AND every entry page, commits and pushes — through
#      publish-index, which holds the same checkout lock the publisher takes
#   5. Restarts sketchgen-web and the resident generator onto the new code
#   6. Resumes the generator (from a trap, so a failure resumes it too)
#
# Safe to run more than once; every step is idempotent.
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

# --- 1. Pull the generator ---------------------------------------------------
step "Pulling generator (~/sketchgen/app)"
cd "$APP" || die "cannot cd to $APP"
git pull origin main || die "git pull failed in $APP"
green "generator up to date"

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
step "Migrating the database"
cd "$APP"
"$VENV" bin/sketchgen db init --db "$DB" || die "db init (migrate) failed"
green "database schema up to date"

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
