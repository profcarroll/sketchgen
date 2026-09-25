#!/bin/bash
# launch-kiosk.sh — started by org.sketchgen.kiosk (a LaunchAgent with KeepAlive), which
# starts it again ten seconds after Chrome exits for any reason. Installed by install.sh at
# ~/Library/sketchgen-kiosk/launch-kiosk.sh; edit it there over ssh and
#   launchctl kickstart -k gui/$(id -u)/org.sketchgen.kiosk
# to change the URL or a flag (docs/plans/kiosk-mac.md §3).
URL="https://profcarroll.github.io/sketchgen-gallery/kiosk.html?unattended=1&site=d12"
# site= is where this machine learns where it lives (kiosk-mac.md §1.3): it switches views
# from the attendance rule to that site's building hours. Drop it and the kiosk counts as a
# default one.
PROFILE="$HOME/Library/Application Support/sketchgen-kiosk-chrome"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
log() { echo "$(date '+%F %T') launch-kiosk: $*"; }   # stdout is ~/Library/Caches/sketchgen-kiosk/chrome.log

# Wait for the gallery rather than for "the network": a Wi-Fi association with no route, or a
# captive portal, is not a network. The page retries by itself as well, but Chrome booted
# offline shows its own error page, which has no retry in it.
n=0
until curl -fsS -o /dev/null --max-time 5 "${URL%%\?*}" 2>/dev/null; do
    [ $n -eq 0 ] && log "waiting for the gallery"
    n=$((n + 1)); sleep 10
done
[ $n -gt 0 ] && log "gallery answered after $((n * 10)) s"

# A Chrome killed by launchd, a power cut or the watchdog marks its profile as crashed and
# offers "Restore pages?" over the wall on the next start. Mark it clean; the flag below hides
# the bubble as well, on the Chromes that have it.
PREFS="$PROFILE/Default/Preferences"
[ -f "$PREFS" ] && sed -i '' -e 's/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/' \
    -e 's/"exited_cleanly":false/"exited_cleanly":true/' "$PREFS"

# Park the pointer in the bottom-right corner: Chrome does not always repaint cursor:none until
# the pointer moves. CoreGraphics through JavaScript for Automation, because the Mac has
# nothing else that can move it without an install; hot corners are off (install.sh), so the
# corner does nothing.
osascript -l JavaScript -e 'ObjC.import("CoreGraphics");
    var b = $.CGDisplayBounds($.CGMainDisplayID());
    $.CGWarpMouseCursorPosition({ x: b.origin.x + b.size.width - 1, y: b.origin.y + b.size.height - 1 });' \
    >/dev/null 2>&1 || log "could not park the pointer"

log "starting Chrome at $URL"
# --remote-debugging-port serves loopback only; it is how the watchdog and kiosk-status read
# the page's title, and Chrome refuses it on the default profile — one more reason for a
# profile of the wall's own. --use-mock-keychain keeps Chrome out of the login keychain, whose
# prompts would otherwise be able to appear over the wall.
exec "$CHROME" \
    --user-data-dir="$PROFILE" \
    --kiosk \
    --no-first-run --no-default-browser-check \
    --noerrdialogs \
    --hide-crash-restore-bubble \
    --use-mock-keychain \
    --autoplay-policy=no-user-gesture-required \
    --disable-features=Translate \
    --remote-debugging-port=9222 \
    "$URL"
