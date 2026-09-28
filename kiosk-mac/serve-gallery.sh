#!/bin/bash
# serve-gallery.sh — started by org.sketchgen.gallery (a LaunchAgent with KeepAlive), which starts
# it again thirty seconds after it exits (docs/plans/local-gallery.md §3). Installed by
# `install.sh --local` at ~/Library/sketchgen-kiosk/serve-gallery.sh.
#
# The one check that has to come before python3 does: on a Mac without the Command Line Tools,
# /usr/bin/python3 is a stub, and running it puts an "install the command line developer
# tools?" dialog over the wall. install.sh refuses the agent without them; this refuses again
# at every start, because the tools can be removed after the agent was installed.
D="$HOME/Library/sketchgen-kiosk"
log() { echo "$(date '+%F %T') serve-gallery: $*"; }   # stdout is ~/Library/Caches/sketchgen-kiosk/gallery.log

if ! xcode-select -p >/dev/null 2>&1; then
    log "no Command Line Tools, so no python3; not serving (xcode-select --install at the Mac)"
    exit 1
fi
# The log is errors and one line per start; keep it from growing without bound anyway.
LOG="$HOME/Library/Caches/sketchgen-kiosk/gallery.log"
[ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ] && : > "$LOG"
[ -e "$D/gallery" ] || log "no gallery yet at $D/gallery: sync one with kiosk-mac/sync-local.sh"
exec /usr/bin/python3 "$D/serve-gallery.py" --bind 127.0.0.1 --port 8090 --directory "$D/gallery"
