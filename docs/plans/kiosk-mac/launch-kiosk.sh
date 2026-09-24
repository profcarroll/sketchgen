#!/bin/bash
# launch-kiosk.sh — started by org.sketchgen.kiosk (a LaunchAgent with KeepAlive), which
# restarts it whenever Chrome exits. Installed at ~/Library/sketchgen-kiosk/launch-kiosk.sh.
# DRAFT: finalize in the on-device session (docs/plans/kiosk-mac.md §2.4).
URL="https://profcarroll.github.io/sketchgen-gallery/kiosk.html?unattended=1&site=d12"
# site= is where this machine learns where it lives (kiosk-mac.md §1.3): it switches views
# from the attendance rule to that site's building hours. Drop it and the kiosk counts as a default one.
PROFILE="$HOME/Library/Application Support/sketchgen-kiosk-chrome"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Wait for the gallery rather than for "the network": a Wi-Fi association with no route, or a
# captive portal, is not a network. The page retries by itself as well, but Chrome booted
# offline shows its own error page, which has no retry in it.
until curl -fsS -o /dev/null --max-time 5 "${URL%%\?*}"; do sleep 10; done

# A Chrome killed by launchd, a power cut or the watchdog marks its profile as crashed and
# offers "Restore pages?" over the wall on the next start. Mark it clean.
PREFS="$PROFILE/Default/Preferences"
[ -f "$PREFS" ] && sed -i '' -e 's/"exit_type":"[A-Za-z]*"/"exit_type":"Normal"/' \
    -e 's/"exited_cleanly":false/"exited_cleanly":true/' "$PREFS"

# Park the pointer bottom-right: Chrome does not always repaint cursor:none until the pointer
# moves (cliclick is `brew install cliclick`; absent, the page's own rule has to do).
command -v cliclick >/dev/null && cliclick m:9999,9999

exec "$CHROME" \
    --user-data-dir="$PROFILE" \
    --kiosk \
    --no-first-run --no-default-browser-check \
    --noerrdialogs \
    --autoplay-policy=no-user-gesture-required \
    --disable-features=Translate \
    --remote-debugging-port=9222 \
    "$URL"
