#!/bin/bash
# probe.sh — read-only survey of the kiosk Mac (docs/plans/kiosk-mac.md §2.1).
# Changes nothing and needs no sudo. Run it first:  bash probe.sh | tee ~/kiosk-probe.txt
URL="https://profcarroll.github.io/sketchgen-gallery/kiosk.json"
WP="https://sketchgen-writepath.sketchgen.workers.dev/counts?entries=1"

s() { printf '\n=== %s\n' "$1"; shift; "$@" 2>&1; }

s "hardware"          system_profiler SPHardwareDataType
s "macOS"             sw_vers
s "MDM enrollment"    profiles status -type enrollment
s "FileVault"         fdesetup status
s "me"                id
s "users"             bash -c "dscl . list /Users | grep -v '^_'"
s "admins"            dscl . read /Groups/admin GroupMembership
s "auto-login user"   defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser
s "power"             pmset -g custom
s "power schedule"    pmset -g sched
s "assertions"        bash -c "pmset -g assertions | head -30"
s "network ports"     networksetup -listallhardwareports
s "default route"     route -n get default
s "network state"     scutil --nwi
s "proxy"             scutil --proxy
s "reach gallery"     curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' "$URL"
s "reach write path"  curl -sS -o /dev/null -w '%{http_code} %{time_total}s\n' "$WP"
s "time zone"         bash -c "readlink /etc/localtime; date"
s "ntp"               bash -c "sntp time.apple.com 2>&1 | tail -1"
s "displays"          system_profiler SPDisplaysDataType
s "applications"      bash -c "ls /Applications | grep -iE 'chrome|firefox|safari|tailscale|edge'"
s "chrome version"    bash -c "'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --version"
s "running browsers"  bash -c "ps -axo pid,rss,etime,command | grep -iE 'chrome|safari' | grep -v grep | cut -c1-200 | head -8"
s "launch agents"     bash -c "launchctl list | grep -v com.apple"
s "agent files"       bash -c "ls ~/Library/LaunchAgents /Library/LaunchAgents /Library/LaunchDaemons 2>&1"
s "login items"       osascript -e 'tell application "System Events" to get the name of every login item'
s "homebrew / CLT"    bash -c "which brew; xcode-select -p"
s "tailscale"         bash -c "which tailscale; ls -d /Applications/Tailscale.app; tailscale status 2>&1 | head -5"
s "remote login"      bash -c "launchctl print system/com.openssh.sshd >/dev/null 2>&1 && echo sshd loaded || echo sshd not loaded"
s "screen sharing"    bash -c "launchctl print system/com.apple.screensharing >/dev/null 2>&1 && echo loaded || echo not loaded"
s "disk"              df -h /
s "memory pressure"   bash -c "memory_pressure | tail -3"
s "input devices"     bash -c "system_profiler SPBluetoothDataType SPUSBDataType | grep -iE 'keyboard|mouse|trackpad|magic|connected|name:' | head -30"
s "bluetooth assist"  bash -c "defaults read /Library/Preferences/com.apple.Bluetooth BluetoothAutoSeekKeyboard; defaults read /Library/Preferences/com.apple.Bluetooth BluetoothAutoSeekPointingDevice"
s "screen saver"      defaults -currentHost read com.apple.screensaver idleTime
s "hot corners"       bash -c "for c in tl tr bl br; do printf '%s ' \$c; defaults read com.apple.dock wvous-\$c-corner 2>/dev/null || echo -; done"
s "update policy"     defaults read /Library/Preferences/com.apple.SoftwareUpdate
s "pending updates"   softwareupdate -l
s "firewall"          /usr/libexec/ApplicationFirewall/socketfilterfw --getglobalstate
s "uptime"            uptime
