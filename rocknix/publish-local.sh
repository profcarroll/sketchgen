#!/bin/bash
# rocknix/publish-local.sh — the handheld's gallery, rendered from its own database into
# /storage/sketchgen/gallery-local, which sketchgen-gallery.service serves on 127.0.0.1:8090.
#
# There is no `publish` here: that verb pushes a git checkout, and ROCKNIX has no git and
# this device no public gallery. What a person does instead is look — held entries beside
# published ones, with the Pick toggle a local render carries (docs/plans/local-gallery.md)
# — so this renders both by default. --published renders only what has been published.
set -euo pipefail
D="$HOME/sketchgen"
SG="$D/.venv/bin/python3 $D/app/bin/sketchgen"
include=(--include published --include held)
[[ "${1:-}" == --published ]] && include=(--include published)
origin=$(hostname 2>/dev/null || echo handheld)
$SG render-local --db "$D/sketchgen.db" --jobs "$D/jobs" --out "$D/gallery-local" \
    --origin "$origin" "${include[@]}"
echo "rendered into $D/gallery-local; sketchgen-gallery.service serves it on http://127.0.0.1:8090/"
