#!/usr/bin/env bash
# fetch-p5.sh — put the p5 the gate loads next to rig/index.html, or refuse.
#
#   bash rig/fetch-p5.sh
#
# The rig is only worth anything if it runs the same library as the node, so
# the version is the one sketchgen/executor.py writes into every index.html
# (1.11.3, from cdnjs) and the file is checked against a pinned sha256 before
# it is allowed to stay. A quietly different p5 would make every number the rig
# prints a measurement of something else, and the agent on job 1286 avoided
# that only by reading the executor first, which cost it part of its
# thirty-seven minutes (2026-09-21).
#
# The hash was taken on 2026-09-21 from the file cdnjs serves, after checking
# it against the sha512 subresource-integrity value cdnjs publishes for the
# same file:
#   sha512-I0Pwwz3PPNQkWes+rcSoQqikKFfRmTfGQrcNzZbm8ALaUyJuFdyRinl805shE8xT6iEWsWgvRxdXb3yhQNXKoA==
# Bump both together, never one, and only after reading executor._P5_TAG.
#
# Exit status follows the repository's: 0 ok, 1 failed, 3 refused — and a
# refusal leaves nothing behind, so a mismatch can never be mistaken for a
# download.
set -u

P5_VERSION="1.11.3"
P5_URL="https://cdnjs.cloudflare.com/ajax/libs/p5.js/${P5_VERSION}/p5.min.js"
P5_SHA256="af51e6211e061b5ae463fbc5c3c1c272e5ca67fa560ed3513fde17325d837506"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$here/p5.min.js"

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | cut -d' ' -f1
    else
        python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
    fi
}

if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$P5_SHA256" ]; then
    echo "p5 $P5_VERSION already here, hash ok: $dest"
    exit 0
fi

tmp="$(mktemp "${TMPDIR:-/tmp}/p5.min.js.XXXXXX")" || exit 1
trap 'rm -f "$tmp"' EXIT

if ! curl -fsSL --max-time 120 -o "$tmp" "$P5_URL"; then
    echo "could not fetch $P5_URL" >&2
    exit 1
fi

got="$(sha256_of "$tmp")"
if [ "$got" != "$P5_SHA256" ]; then
    # Refused, not failed: nothing is written, and the rig keeps whatever p5 it
    # already had rather than timing a sketch against an unknown library.
    echo "refusing $P5_URL: sha256 mismatch" >&2
    echo "  expected $P5_SHA256" >&2
    echo "  got      $got" >&2
    exit 3
fi

mv "$tmp" "$dest" || exit 1
trap - EXIT
echo "p5 $P5_VERSION -> $dest"
echo "sha256 $P5_SHA256"
