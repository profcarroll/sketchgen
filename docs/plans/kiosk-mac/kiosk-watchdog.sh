#!/bin/bash
# kiosk-watchdog.sh — run every 5 minutes by org.sketchgen.kiosk-watchdog.
# Reads the kiosk tab's title from Chrome's loopback DevTools endpoint; the page keeps its
# state there (docs/plans/kiosk-mac.md §1.4) and changes it at least every ten minutes.
# DRAFT: finalize in the on-device session.
AGENT="gui/$(id -u)/org.sketchgen.kiosk"
STATE="$HOME/Library/Caches/sketchgen-kiosk"
STALE_S=${STALE_S:-1200}          # 20 minutes without a new title is a hung page
RESTART_AT=${RESTART_AT:-0430}    # a fresh Chrome once a night: updates, and a day's memory
mkdir -p "$STATE"
log() { echo "$(date '+%F %T') $*" >> "$STATE/watchdog.log"; }
restart() { log "restart: $1"; launchctl kickstart -k "$AGENT"; date +%s > "$STATE/since"; }

now=$(date +%s)
hhmm=$(date +%H%M)
# The nightly restart, once: within the first five-minute window after RESTART_AT.
if [ "$hhmm" -ge "$RESTART_AT" ] && [ "$hhmm" -lt "$(printf '%04d' $((10#$RESTART_AT + 5)))" ]; then
    restart "nightly"; exit 0
fi

title=$(curl -fsS --max-time 5 http://127.0.0.1:9222/json/list 2>/dev/null \
    | grep -m1 '"title"' | sed -e 's/.*"title": *"//' -e 's/",*$//')
if [ -z "$title" ]; then
    # Chrome may still be starting (the launcher waits for the network): give it one round.
    if [ -f "$STATE/nochrome" ]; then rm -f "$STATE/nochrome"; restart "no DevTools answer twice"
    else touch "$STATE/nochrome"; fi
    exit 0
fi
rm -f "$STATE/nochrome"

if [ "$title" != "$(cat "$STATE/title" 2>/dev/null)" ]; then
    echo "$title" > "$STATE/title"; echo "$now" > "$STATE/since"; exit 0
fi
since=$(cat "$STATE/since" 2>/dev/null || echo "$now")
[ $((now - since)) -ge "$STALE_S" ] && restart "title unchanged $((now - since))s: $title"
exit 0
