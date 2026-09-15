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
#   2. Pulls the gallery checkout (~/sketchgen/gallery)
#   3. Re-renders the gallery index from the database (new templates → new HTML)
#   4. Pushes the re-rendered gallery to GitHub
#   5. Restarts sketchgen-web so the operator UI picks up any code changes
#
# Safe to run more than once; every step is idempotent.

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

# --- 2. Pull the gallery checkout --------------------------------------------
step "Pulling gallery checkout (~/sketchgen/gallery)"
cd "$GALLERY" || die "cannot cd to $GALLERY"
git pull origin main || die "git pull failed in $GALLERY"
green "gallery checkout up to date"

# --- 3. Re-render the gallery index ------------------------------------------
step "Re-rendering gallery index from database"
cd "$APP"
"$VENV" bin/sketchgen render-index \
    --db "$DB" \
    --gallery-dir "$GALLERY" \
    || die "render-index failed"
green "gallery index re-rendered"

# --- 4. Commit and push the re-rendered gallery -------------------------------
step "Committing and pushing gallery"
cd "$GALLERY"
if [ -n "$(git status --porcelain)" ]; then
    git add -A .
    git commit -m "gallery: re-render index after code update"
    git push origin HEAD:refs/heads/main || die "gallery push failed"
    green "gallery pushed"
else
    dim "gallery unchanged, nothing to push"
fi

# --- 5. Restart the web service -----------------------------------------------
step "Restarting sketchgen-web"
systemctl --user restart sketchgen-web.service
sleep 1
systemctl --user is-active sketchgen-web.service >/dev/null 2>&1 \
    && green "sketchgen-web is running" \
    || die "sketchgen-web failed to start"

echo ""
green "✓ all done"
