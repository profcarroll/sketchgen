#!/bin/bash
# prep.sh — at the kiosk Mac, in the account the wall runs as (kiosk-mac/README.md). It gets
# the Mac to where the laptop can finish the job over ssh (docs/plans/kiosk-mac.md §2.1–2.3).
#
# The probe is read-only. Three steps change things, and each one asks first:
#   1. the laptop's keys (github.com/profcarroll.keys) into this account's authorized_keys;
#   2. system.sh — machine-wide settings — run through the Mac's own administrator dialog, so
#      the account the wall runs as does not have to be an administrator;
#   3. starting Tailscale in this account.
# Then it prints what only a person at the Mac can click, and stops.
set -uo pipefail
KIT="$(cd "$(dirname "$0")" && pwd)"
ME=$(whoami)
TS_APP=/Applications/Tailscale.app
TS_CLI="$TS_APP/Contents/MacOS/Tailscale"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
row() { printf '  %-14s %s\n' "$1" "$2"; }
ask() { local a; read -r -p "  $1 [y/N] " a </dev/tty; [[ $a == [yY]* ]]; }
sshd_on() { nc -z -G 2 127.0.0.1 22 >/dev/null 2>&1; }

say "Probe (read-only; softwareupdate makes it take a minute or two) → ~/kiosk-probe.txt"
bash "$KIT/probe.sh" > ~/kiosk-probe.txt 2>&1
admin=no; dsmemberutil checkmembership -U "$ME" -G admin 2>/dev/null | grep -q 'is a member' && admin=yes
mdm=$(profiles status -type enrollment 2>&1 | tr '\n' ' ')
fv=$(fdesetup status 2>&1 | head -1)
row "Mac"          "$(sysctl -n hw.model), macOS $(sw_vers -productVersion), $(( $(sysctl -n hw.memsize) / 1073741824 )) GB"
row "account"      "$ME (uid $(id -u), admin: $admin)"
row "MDM"          "$mdm"
row "FileVault"    "$fv"
row "auto-login"   "$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null || echo none)"
row "Chrome"       "$([ -x "$CHROME" ] && "$CHROME" --version 2>/dev/null || echo 'NOT INSTALLED')"
row "Tailscale"    "$([ -d "$TS_APP" ] && echo "app installed" || echo 'app not in /Applications')"
row "Remote Login" "$(sshd_on && echo on || echo off)"
row "gallery"      "$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 https://profcarroll.github.io/sketchgen-gallery/kiosk.json 2>&1)"

if echo "$mdm" | grep -qi 'enrollment: Yes\|Enrolled via DEP: Yes'; then
    say "This Mac is enrolled in MDM: its IT department manages it."
    echo "  The plan (§2.1) stops here until IT has agreed to auto-login, Remote Login and Tailscale."
    ask "Carry on anyway?" || exit 3
fi

say "1/3  Let the laptop in over ssh"
echo "  Adds the keys at github.com/profcarroll.keys to ~/.ssh/authorized_keys for $ME."
echo "  system.sh limits ssh to those keys, from the tailnet only."
if ask "Add them?"; then
    mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
    if keys=$(curl -fsSL --max-time 20 https://github.com/profcarroll.keys) && [ -n "$keys" ]; then
        n=0
        while read -r k; do
            [ -n "$k" ] && ! grep -qxF "$k" ~/.ssh/authorized_keys && { echo "$k" >> ~/.ssh/authorized_keys; n=$((n + 1)); }
        done <<< "$keys"
        echo "  added $n key(s); $(grep -c . ~/.ssh/authorized_keys) in the file"
    else
        echo "  could not fetch the keys — nothing written"
    fi
fi

say "2/3  System settings for a wall (system.sh)"
echo "  Never sleep; boot when power returns; restart after a freeze; no Bluetooth setup"
echo "  assistant; macOS updates download but never install by themselves; New York time;"
echo "  Remote Login on, keys only, tailnet only, for $ME alone; and $ME may run exactly"
echo "  'sudo shutdown -r now', so the laptop can restart the Mac. Each line prints its result."
if ask "Apply them? (the Mac asks for an administrator's name and password)"; then
    # `with administrator privileges` puts up macOS's own dialog, which takes any
    # administrator's name — the wall's account can stay a standard user.
    osascript -e "do shell script \"/bin/bash '$KIT/system.sh' '$ME' 2>&1\" with administrator privileges without altering line endings" \
        || echo "  system.sh did not run (dialog cancelled?) — run prep.sh again, or: sudo bash $KIT/system.sh $ME"
fi

say "3/3  Tailscale, in this account"
if [ -d "$TS_APP" ]; then
    open -a "$TS_APP"; sleep 5
    st=$("$TS_CLI" status --self --peers=false 2>&1 | head -1)
    row "status" "${st:-no answer yet}"
else
    echo "  Tailscale is not in /Applications. Install it (tailscale.com/download/mac) as an"
    echo "  administrator, then run this again."
fi

say "Left for a person at the Mac (System Settings), then tell Claude \"ready\":"
cat <<EOF
  [ ] Tailscale (menu bar icon): signed in and connected as d12-kiosk.
      Its Settings → "Launch Tailscale at login" on, in THIS account.
$(sshd_on || echo '  [ ] General → Sharing → Remote Login: on (system.sh could not turn it on).')
$(echo "$fv" | grep -q 'is On' && echo '  [ ] Privacy & Security → FileVault: Turn Off. Auto-login cannot work while it is on.')
  [ ] Users & Groups → "Automatically log in as": $ME (asks for $ME's password).
  [ ] Lock Screen → "Start Screen Saver when inactive": Never;
      "Require password after screen saver begins or display is turned off": Never.
  [ ] Control Center → Focus → Do Not Disturb: on, in this account.
  [ ] Quit any browser that was started by hand, and take it out of
      General → Login Items if it is there. The installer starts the wall's own Chrome.
$([ -x "$CHROME" ] || echo '  [ ] Install Google Chrome into /Applications (an administrator).')
EOF
echo
echo "  The rest — the browser under launchd, the watchdog, kiosk-status — Claude installs"
echo "  over ssh. At the Mac instead:  bash $KIT/install.sh"
echo "  The whole probe is in ~/kiosk-probe.txt."
