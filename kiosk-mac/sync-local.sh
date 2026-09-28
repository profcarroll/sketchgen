#!/bin/bash
# sync-local.sh — put a local render on a wall (docs/plans/local-gallery.md §3). Run on the
# laptop, which reaches both machines; neither needs a key to the other.
#
#   kiosk-mac/sync-local.sh NODE:DIR MAC [--keep N]
#   kiosk-mac/sync-local.sh sld-cloud:sketchgen-local/sld-gpu d12-kiosk
#
# NODE:DIR is a directory `sketchgen render-local` wrote (it carries .sketchgen-local). MAC is a
# wall with the kit installed with --local. The tree is streamed node → laptop → Mac as one tar,
# never stored on the laptop, and unpacked into a new release beside the old one:
#
#   ~/Library/sketchgen-kiosk/releases/<utc stamp>/    one per sync
#   ~/Library/sketchgen-kiosk/gallery -> releases/<…>  what serve-gallery.py serves
#
# The symlink is swapped by rename once the release is whole, so the wall never reads a
# half-copied tree: it goes on playing the old one until the swap, and the page picks up the
# new one on its next manifest refresh (15 min), reloading if the kiosk's own code changed. The
# newest --keep releases stay (default 2: this one and the one before, to swap back to by hand);
# older ones are removed.
#
# Exit codes as everywhere: 0 synced, 1 failed (the wall still serves what it served), 3 refused
# (nothing written).
set -euo pipefail

usage() { sed -n '4,6p' "$0" | sed 's/^# \{0,1\}//'; exit 3; }
[ $# -ge 2 ] || usage
SRC=$1 MAC=$2; shift 2
KEEP=2
while [ $# -gt 0 ]; do
    case "$1" in
        --keep) [ $# -ge 2 ] && [ "$2" -ge 1 ] 2>/dev/null || usage; KEEP=$2; shift 2 ;;
        *) usage ;;
    esac
done
NODE=${SRC%%:*} DIR=${SRC#*:}
[ "$NODE" != "$SRC" ] && [ -n "$NODE" ] && [ -n "$DIR" ] || usage
SSH="ssh -o BatchMode=yes -o ConnectTimeout=20"
qdir=$(printf '%q' "$DIR")

# 1. The source is a whole local render, not a checkout or a half-written one.
$SSH "$NODE" "test -f $qdir/.sketchgen-local && test -f $qdir/kiosk.json" || {
    echo "sync-local: refused: $SRC is not a local render (no .sketchgen-local or kiosk.json there)"; exit 3; }
size=$($SSH "$NODE" "du -sm $qdir | cut -f1")

# 2. The wall can serve it: the tools python3 needs, and a gallery path that is ours to swap.
REL=$(date -u +%Y%m%dT%H%M%SZ)
$SSH "$MAC" bash -s -- "$REL" <<'EOF' || exit 3
set -e
D="$HOME/Library/sketchgen-kiosk"
xcode-select -p >/dev/null 2>&1 || {
    echo "sync-local: refused: no Command Line Tools on this Mac, so nothing can serve a gallery"; exit 3; }
[ -f "$D/serve-gallery.py" ] || {
    echo "sync-local: refused: the kit is not installed with --local here (no serve-gallery.py)"; exit 3; }
if [ -e "$D/gallery" ] && [ ! -L "$D/gallery" ]; then
    echo "sync-local: refused: $D/gallery is a real directory, not a release link; move it aside"; exit 3
fi
mkdir -p "$D/releases/$1.part"
EOF

# 3. The copy. pipefail makes a failure at either end a failure here.
echo "copying $SRC (${size} MB) to $MAC as release $REL"
if ! $SSH "$NODE" "tar -C $qdir -cf - ." | $SSH "$MAC" "tar -C Library/sketchgen-kiosk/releases/$REL.part -xf -"; then
    $SSH "$MAC" "rm -rf Library/sketchgen-kiosk/releases/$REL.part" || true
    echo "sync-local: failed: the copy did not finish; the wall still serves what it served"; exit 1
fi

# 4. Whole? Then swap, prune, and say what the wall now serves.
$SSH "$MAC" bash -s -- "$REL" "$KEEP" <<'EOF'
set -e
D="$HOME/Library/sketchgen-kiosk"; REL=$1; KEEP=$2
part="$D/releases/$REL.part"
if [ ! -f "$part/kiosk.json" ] || [ ! -f "$part/.sketchgen-local" ]; then
    rm -rf "$part"
    echo "sync-local: failed: the copy arrived without kiosk.json; the wall still serves what it served"
    exit 1
fi
mv "$part" "$D/releases/$REL"
# One rename replaces the link. BSD mv and GNU mv each follow a link to a directory and move
# the new one inside it; os.replace does not, on either. python3 is here: step 2 checked.
/usr/bin/python3 - "$D" "releases/$REL" <<'PY'
import os, sys
d, target = sys.argv[1], sys.argv[2]
tmp = os.path.join(d, "gallery.new")
if os.path.lexists(tmp):
    os.remove(tmp)
os.symlink(target, tmp)
os.replace(tmp, os.path.join(d, "gallery"))
PY
current=$(readlink "$D/gallery")
# The newest KEEP releases stay, and never the one being served; a .part a failed sync left
# behind goes too.
ls -1 "$D/releases" | sort -r | awk -v keep="$KEEP" '!/\.part$/ { n++; if (n > keep) print } /\.part$/ { print }' \
    | while read -r old; do
        [ "releases/$old" = "$current" ] || rm -rf "$D/releases/$old"
    done
build=$(grep -o '"build": "[^"]*"' "$D/gallery/kiosk.json" | head -1)
served=$(curl -fsS --max-time 5 http://127.0.0.1:8090/kiosk.json 2>/dev/null | grep -o '"build": "[^"]*"' | head -1 || true)
echo "gallery -> $current  ($build)"
echo "served on 127.0.0.1:8090: ${served:-nothing answering (is org.sketchgen.gallery loaded?)}"
echo "releases kept: $(ls -1 "$D/releases" | tr '\n' ' ')"
EOF
