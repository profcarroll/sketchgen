#!/bin/bash
# get.sh — the one line typed at the kiosk Mac, in Terminal, in the account the wall runs as:
#
#   curl -fsSL https://raw.githubusercontent.com/profcarroll/sketchgen/main/kiosk-mac/get.sh | bash
#
# Fetches this directory into ~/Library/sketchgen-kiosk/kit and runs prep.sh from there. A ref
# other than main (a branch under review, or a commit) goes after `bash -s`:
#   curl -fsSL …/<ref>/kiosk-mac/get.sh | bash -s <ref>
# The Mac has no git until someone installs the command-line tools, so this is plain curl.
set -euo pipefail
REF=${1:-main}
RAW="https://raw.githubusercontent.com/profcarroll/sketchgen/$REF/kiosk-mac"
KIT="$HOME/Library/sketchgen-kiosk/kit"
FILES="prep.sh system.sh install.sh probe.sh launch-kiosk.sh kiosk-watchdog.sh kiosk-status
       org.sketchgen.kiosk.plist org.sketchgen.kiosk-watchdog.plist README.md"

mkdir -p "$KIT"
for f in $FILES; do curl -fsSL "$RAW/$f" -o "$KIT/$f"; done
chmod +x "$KIT"/*.sh "$KIT/kiosk-status"
echo "kit: $KIT (from $REF)"
# stdin is this script when it arrives through a pipe; prep.sh asks questions, so give it the
# terminal instead.
exec bash "$KIT/prep.sh" </dev/tty
