#!/bin/bash
# kiosk-watchdog.sh — run every 5 minutes by org.sketchgen.kiosk-watchdog.
# Reads the kiosk tab's title from Chrome's loopback DevTools endpoint; the page keeps its state
# there (docs/plans/kiosk-mac.md §1.4) and changes it at every seat, at least every ten minutes.
#   kiosk-watchdog.sh           one round: restart Chrome if the page is gone or hung
#   kiosk-watchdog.sh --title   print the kiosk tab's title and nothing else (kiosk-status)
# STALE_S and RESTART_AT in the environment override the defaults, for the fault tests (§2.5).
AGENT="gui/$(id -u)/org.sketchgen.kiosk"
STATE="$HOME/Library/Caches/sketchgen-kiosk"
STALE_S=${STALE_S:-1200}          # 20 minutes without a new title is a hung page
RESTART_AT=${RESTART_AT:-0430}    # a fresh Chrome once a night: its updates, and a day's memory

# The kiosk page, not the first target: a sandboxed sketch frame can be a target of its own
# with its own title. JSON is parsed by JavaScript for Automation, which every Mac has.
title() {
    curl -fsS --max-time 5 http://127.0.0.1:9222/json/list 2>/dev/null | osascript -l JavaScript -e '
        ObjC.import("Foundation");
        var raw = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
        var list = [];
        try { list = JSON.parse($.NSString.alloc.initWithDataEncoding(raw, $.NSUTF8StringEncoding).js); } catch (e) {}
        var page = list.filter(function (t) { return t.type === "page" && t.url.indexOf("/kiosk.html") >= 0; })[0];
        page ? page.title : "";' 2>/dev/null
}
[ "${1:-}" = --title ] && { title; exit 0; }

mkdir -p "$STATE"
log() { echo "$(date '+%F %T') $*" >> "$STATE/watchdog.log"; }
restart() { log "restart: $1"; launchctl kickstart -k "$AGENT"; date +%s > "$STATE/since"; }

now=$(date +%s)
hhmm=$(date +%H%M)
# The nightly restart, once: StartInterval is 300 s, so exactly one round lands in the five
# minutes after RESTART_AT.
if [ "$hhmm" -ge "$RESTART_AT" ] && [ "$hhmm" -lt "$(printf '%04d' $((10#$RESTART_AT + 5)))" ]; then
    restart "nightly"; exit 0
fi

t=$(title)
if [ -z "$t" ]; then
    # Chrome may still be starting (the launcher waits for the gallery): give it one round.
    if [ -f "$STATE/nochrome" ]; then rm -f "$STATE/nochrome"; restart "no kiosk page twice"
    else touch "$STATE/nochrome"; log "no kiosk page (once)"; fi
    exit 0
fi
rm -f "$STATE/nochrome"

if [ "$t" != "$(cat "$STATE/title" 2>/dev/null)" ]; then
    echo "$t" > "$STATE/title"; echo "$now" > "$STATE/since"; exit 0
fi
since=$(cat "$STATE/since" 2>/dev/null || echo "$now")
[ $((now - since)) -ge "$STALE_S" ] && restart "title unchanged $((now - since)) s: $t"
exit 0
