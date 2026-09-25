#!/bin/bash
# install.sh — the wall's own account (docs/plans/kiosk-mac.md §2.3–2.4): Chrome under
# launchd, the watchdog, kiosk-status, and the per-user settings. Needs no administrator.
# Run it at the Mac or over ssh, as the account that logs in automatically:
#   bash ~/Library/sketchgen-kiosk/kit/install.sh            install, or reinstall after an edit
#   bash ~/Library/sketchgen-kiosk/kit/install.sh --remove   take both agents out again
set -euo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
D="$HOME/Library/sketchgen-kiosk"
C="$HOME/Library/Caches/sketchgen-kiosk"
LA="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"
AGENTS="org.sketchgen.kiosk org.sketchgen.kiosk-watchdog"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

[ "$(id -u)" != 0 ] || { echo "install.sh: run it as the wall's account, not as root"; exit 3; }
# The agents belong to the logged-in desktop session; over ssh that session must already exist.
launchctl print "$DOMAIN" >/dev/null 2>&1 \
    || { echo "install.sh: $(whoami) is not logged in at the Mac; log in there first"; exit 3; }

gone() {  # wait for launchd to finish a bootout, which returns before the job is gone
    local i; for i in $(seq 20); do launchctl print "$DOMAIN/$1" >/dev/null 2>&1 || return 0; sleep 0.5; done
}

if [ "${1:-}" = --remove ]; then
    for a in $AGENTS; do launchctl bootout "$DOMAIN/$a" 2>/dev/null || true; gone $a; rm -f "$LA/$a.plist"; done
    echo "removed $AGENTS; settings, files and Chrome's profile are left as they are"
    exit 0
fi

[ -x "$CHROME" ] || { echo "install.sh: Google Chrome is not in /Applications; install it first"; exit 3; }

echo "files"
mkdir -p "$D" "$C" "$LA" "$HOME/bin"
install -m 755 "$KIT/launch-kiosk.sh" "$KIT/kiosk-watchdog.sh" "$D/"
install -m 755 "$KIT/kiosk-status" "$HOME/bin/"
# `ssh d12-kiosk kiosk-status` runs under zsh with its bare PATH, and ~/.zshenv is the one file
# a non-interactive zsh reads.
grep -qs '# sketchgen kiosk' ~/.zshenv || echo 'export PATH="$HOME/bin:$PATH"  # sketchgen kiosk' >> ~/.zshenv
echo "  $D/{launch-kiosk.sh,kiosk-watchdog.sh}, ~/bin/kiosk-status"

echo "settings for $(whoami)"
defaults -currentHost write com.apple.screensaver idleTime -int 0       # no screen saver
defaults write com.apple.loginwindow TALLogoutSavesState -bool false    # don't reopen windows at login
defaults write -g NSQuitAlwaysKeepsWindows -bool false
defaults write com.apple.CrashReporter DialogType -string none         # no crash dialog over the wall
for c in tl tr bl br; do                                                # no hot corners: 1 is "–"
    defaults write com.apple.dock "wvous-$c-corner" -int 1
    defaults write com.apple.dock "wvous-$c-modifier" -int 0
done
killall Dock 2>/dev/null || true
echo "  screen saver never, no window restore, no crash dialogs, no hot corners"

# A browser somebody started by hand — the first look on 2026-09-23 — would sit behind the
# wall's own Chrome, or in front of it. The wall's Chrome is the one with its own profile.
hand=$({ pgrep -u "$(id -u)" -f 'Google Chrome.app/Contents/MacOS/Google Chrome' 2>/dev/null || true; } \
    | while read -r p; do ps -o command= -p "$p" | grep -q 'sketchgen-kiosk-chrome' || echo "$p"; done)
if [ -n "$hand" ]; then
    kill $hand 2>/dev/null || true
    echo "quit the Chrome that was started by hand (pid $(echo $hand))"
fi

echo "agents"
for a in $AGENTS; do
    sed "s|/Users/USER|$HOME|g" "$KIT/$a.plist" > "$LA/$a.plist"
    plutil -lint -s "$LA/$a.plist"
    launchctl bootout "$DOMAIN/$a" 2>/dev/null || true; gone $a
    launchctl bootstrap "$DOMAIN" "$LA/$a.plist"
    echo "  $a loaded"
done

# The launcher waits for the gallery, Chrome takes a few seconds, and the page's title is the
# signal that all of it worked (kiosk-mac.md §1.4).
printf 'waiting for the page'
for i in $(seq 24); do
    t=$("$D/kiosk-watchdog.sh" --title 2>/dev/null || true)
    [ -n "$t" ] && break; printf '.'; sleep 5
done
echo
"$HOME/bin/kiosk-status"
