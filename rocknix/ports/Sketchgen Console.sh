#!/bin/sh
# Sketchgen Console — the Ports entry that is this handheld's only sketchgen switch.
#
# It starts sketchgen.target (the console and the power keeper, no model), opens the
# console in Firefox, and marks it open while Firefox runs. Resume there turns the
# generator on (the keeper loads the model, about a minute); Pause turns it off. Close
# Firefox (Guide) with the generator off and everything sketchgen stops; leave it on and
# it keeps generating until the entry is opened again and it is paused.
# rocknix/install.sh copies this into /storage/roms/ports.
FLAG=/run/sketchgen/console
mkdir -p /run/sketchgen
echo $$ > "$FLAG"
trap 'rm -f "$FLAG"' EXIT
trap 'exit 130' INT TERM HUP
systemctl start sketchgen.target
# The console answers a second or two after its unit starts; Firefox would open on an
# error page before that.
i=0
until curl -fs -o /dev/null http://127.0.0.1:8081/ || [ "$i" -ge 30 ]; do
    sleep 1
    i=$((i + 1))
done
/storage/apps/firefox-rocknix/launch.sh http://127.0.0.1:8081/
