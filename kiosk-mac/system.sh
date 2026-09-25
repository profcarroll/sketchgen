#!/bin/bash
# system.sh — the machine-wide half of docs/plans/kiosk-mac.md §2.3, run as root. prep.sh
# runs it through macOS's administrator dialog; by hand it is  sudo bash system.sh <user>.
# $1 is the account the wall runs as. Every line is reversible, idempotent, and prints what
# happened; one that fails says so and the rest still run.
set -u
U=${1:?usage: system.sh <the account the wall runs as>}
[ "$(id -u)" = 0 ] || { echo "system.sh: needs root — prep.sh asks for an administrator"; exit 3; }
id "$U" >/dev/null 2>&1 || { echo "system.sh: no such user: $U"; exit 3; }

row() { printf '  %-30s %s\n' "$1" "$2"; }
run() {
    local what=$1 out; shift
    if out=$("$@" 2>&1); then row "$what" "ok${out:+ ($(echo "$out" | tr '\n' ' ' | cut -c1-60))}"
    else row "$what" "FAILED: $(echo "$out" | tr '\n' ' ' | cut -c1-90)"; fi
}
# systemsetup exits 0 after its own errors — "### Error:-99" on the D12 Mac, 2026-09-24, which
# system.sh first reported as ok — so print the value it reads back instead of its exit code.
ss() {
    local what=$1 get=${*: -1}; shift
    systemsetup "${@:1:$#-1}" >/dev/null 2>&1
    row "$what" "$(systemsetup "$get" 2>&1 | grep -v '### Error' | tail -1)"
}

run "never sleep"                  pmset -a sleep 0 displaysleep 0 disksleep 0 powernap 0
run "boot when power returns"      pmset -a autorestart 1
ss "restart after a freeze"        -setrestartfreeze on       -getrestartfreeze
# Without these, a Mac that boots with no keyboard or mouse puts the Bluetooth setup assistant
# over the wall and waits for somebody to dismiss it.
run "no keyboard setup assistant"  defaults write /Library/Preferences/com.apple.Bluetooth BluetoothAutoSeekKeyboard -bool false
run "no mouse setup assistant"     defaults write /Library/Preferences/com.apple.Bluetooth BluetoothAutoSeekPointingDevice -bool false
# Download, never install: an update that installs itself restarts into a setup screen.
run "macOS updates: no auto-install" defaults write /Library/Preferences/com.apple.SoftwareUpdate AutomaticallyInstallMacOSUpdates -bool false
ss "time zone"                     -settimezone America/New_York -gettimezone
ss "network time"                  -setusingnetworktime on    -getusingnetworktime

# Remote Login, for the laptop. sshd on a Mac listens on every interface, the lab's network
# included, so it takes keys only (prep.sh put them there), from the tailnet only, for the
# wall's account only. The drop-in sorts before macOS's own 100-macos.conf because sshd keeps
# the first value it reads for each keyword.
D=/etc/ssh/sshd_config.d; F=$D/010-sketchgen-kiosk.conf; M=/etc/ssh/sshd_config
# The D12 Mac (macOS 15.8, 2026-09-24) shipped with no drop-in directory and no Include line.
# Add the line Ventura has, at the top so the drop-in's values come first, keeping the original
# beside it. A macOS upgrade may put the stock file back; the probe shows whether the line is there.
added=
if ! grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d' $M; then
    cp -p $M $M.pre-sketchgen
    # The comment gets a line of its own: older sshd reads a trailing one as more paths.
    { echo '# sketchgen kiosk (kiosk-mac/system.sh); the original is sshd_config.pre-sketchgen'
      echo 'Include /etc/ssh/sshd_config.d/*'; cat $M.pre-sketchgen; } > $M
    added=1
fi
if grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d' $M; then
    mkdir -p $D
    # sshd here is started per connection by launchd, so a file in place is live at once;
    # `sshd -t` over the whole configuration decides whether it stays.
    cat > $F <<EOF
# sketchgen kiosk (kiosk-mac/system.sh): keys only, only from the tailnet, only the wall's account.
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
AllowUsers $U@100.64.0.0/10 $U@fd7a:115c:a1e0::/48
EOF
    if out=$(/usr/sbin/sshd -t 2>&1); then
        row "ssh: keys, tailnet, $U only" "ok ($F${added:+; Include added to $M})"
    else
        rm -f $F; [ -n "$added" ] && mv $M.pre-sketchgen $M
        row "ssh: keys, tailnet, $U only" "FAILED: sshd -t: $(echo "$out" | head -1); put back as it was"; F=
    fi
else
    row "ssh: keys, tailnet, $U only" "FAILED: could not add an Include line to $M; not turning Remote Login on"
    F=
fi
if [ -n "$F" ] && [ -f "$F" ]; then
    # Turning Remote Login on with systemsetup needs Full Disk Access for the terminal on
    # recent macOS; launchctl does not.
    if ! nc -z -G 2 127.0.0.1 22 >/dev/null 2>&1; then
        systemsetup -f -setremotelogin on >/dev/null 2>&1
        nc -z -G 2 127.0.0.1 22 >/dev/null 2>&1 || {
            launchctl enable system/com.openssh.sshd
            launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist; } >/dev/null 2>&1
    fi
    # With "Allow access for: only these users" set in System Settings, sshd also requires
    # this group; without it the group does not exist and AllowUsers above is the whole rule.
    dscl . read /Groups/com.apple.access_ssh >/dev/null 2>&1 \
        && dseditgroup -o edit -a "$U" -t user com.apple.access_ssh >/dev/null 2>&1
    nc -z -G 2 127.0.0.1 22 >/dev/null 2>&1 && row "Remote Login" "on" \
        || row "Remote Login" "FAILED: turn it on in System Settings → General → Sharing"
fi

# The laptop restarts the Mac as the wall's account (kiosk-mac.md §3), which is not an
# administrator: exactly this command, and nothing else, without a password.
S=/etc/sudoers.d/sketchgen-kiosk
echo "$U ALL=(root) NOPASSWD: /sbin/shutdown -r now" > $S.new
if visudo -cf $S.new >/dev/null 2>&1; then
    chmod 440 $S.new && mv $S.new $S && row "remote restart for $U" "ok ($S)"
else
    rm -f $S.new; row "remote restart for $U" "FAILED: visudo rejected it; nothing written"
fi

echo
pmset -g | grep -E '^ *(sleep|displaysleep|disksleep|autorestart|powernap) ' | sed 's/^ */  pmset  /'
