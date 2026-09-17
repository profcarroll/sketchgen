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
#   2. Re-installs the unit files into ~/.config/systemd/user and reloads
#   2b. Applies any pending database migration (db init)
#   3. Pulls the gallery checkout (~/sketchgen/gallery)
#   4. Re-renders the gallery from the database — the index AND every entry page
#   5. Pushes the re-rendered gallery to GitHub
#   6. Restarts sketchgen-web so the operator UI picks up any code changes
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

# --- 1. Pull the generator ---------------------------------------------------
step "Pulling generator (~/sketchgen/app)"
cd "$APP" || die "cannot cd to $APP"
git pull origin main || die "git pull failed in $APP"
green "generator up to date"

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

# --- 4. Re-render the whole gallery ------------------------------------------
# Every entry page, not only the index. An entry page is written once, by the
# publisher, at the moment that entry goes up, and until now nothing re-rendered
# it afterwards: a template change, a new panel or a fixed generator reached
# index.html on this step and never reached the hundreds of pages that ARE the
# gallery. That is how 36 published entries, 518 through 563, are on the site
# with no critique form.
# render-all is render-index plus one render per public entry, from the same
# database and the same config.json, so a page that is already current comes out
# byte-identical and step 5 sees nothing to commit for it. It takes a few
# minutes over five hundred entries — the price of the pages never drifting from
# the code again.
step "Re-rendering gallery from database (index and every entry)"
cd "$APP"
written=$("$VENV" bin/sketchgen render-all \
    --db "$DB" \
    --gallery-dir "$GALLERY" | wc -l) || die "render-all failed"
green "gallery re-rendered ($written files)"

# --- 5. Commit and push the re-rendered gallery -------------------------------
step "Committing and pushing gallery"
cd "$GALLERY"
if [ -n "$(git status --porcelain)" ]; then
    git add -A .
    git commit -m "gallery: re-render every page after code update"
    git push origin HEAD:refs/heads/main || die "gallery push failed"
    green "gallery pushed"
else
    dim "gallery unchanged, nothing to push"
fi

# --- 6. Restart the web service -----------------------------------------------
step "Restarting sketchgen-web"
systemctl --user restart sketchgen-web.service
sleep 1
systemctl --user is-active sketchgen-web.service >/dev/null 2>&1 \
    && green "sketchgen-web is running" \
    || die "sketchgen-web failed to start"

echo ""
green "✓ all done"
