#!/usr/bin/env bash
# pull-backup.sh — bring the node's snapshots and its attempt archive here.
#
#   bin/pull-backup.sh sld-cloud
#   bin/pull-backup.sh sld-cloud --dest ~/sketchgen-backups --no-jobs
#
# Run this on the OPERATOR'S machine, not on the node. That is the whole design
# decision: the copy that leaves the instance is a *pull* over the SSH
# connection that already exists, so no new credential — no object-storage key,
# no third-party token — ever has to live on a box whose whole problem is that
# it might be taken away. `sketchgen backup snapshot` on the node makes a good
# copy on the same disk that would be lost with it; this is the part that makes
# it a backup.
#
# Two rsyncs, deliberately asymmetric:
#
#   backups/   -a --delete --delete-excluded.  The node prunes to fourteen
#              snapshots, and the mirror has to follow it down or this
#              directory grows forever.
#   jobs/      -a and NOTHING ELSE.  The attempt archive only ever grows, and a
#              --delete here would turn one bad night on the node — a wiped
#              directory, a half-restored instance — into the same loss over
#              here, at machine speed, before anybody looked. Attempts are never
#              deleted (spec §10); this side does not delete them either.
#
# Then it verifies the newest snapshot it just pulled — an unopened backup is a
# belief, not a backup — and prints one line: the snapshot's date, the database
# size, the job count from the manifest beside the job count actually on disk,
# and whether it verified.
#
# EXIT CODES  0 pulled and verified
#             1 something failed, OR the newest snapshot is more than 36 hours
#               old — a stalled timer on the node is silent by nature, and this
#               is the only thing that notices it. 36 hours, not 24, so that one
#               missed night does not cry wolf but two cannot pass unremarked.
#             3 refused: no host given, no rsync, no python3.
#
# Timestamps printed here are UTC, like everything else in this project.

set -uo pipefail

MAX_AGE_HOURS=36

here="$(cd "$(dirname "$0")" && pwd)"
CLI="$here/sketchgen"

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;36m→ %s\033[0m\n' "$*"; }

usage() {
    cat >&2 <<'USAGE'
usage: pull-backup.sh HOST [--dest DIR] [--no-jobs]

  HOST        an ssh destination, as in `ssh HOST` — normally sld-cloud
  --dest DIR  where the mirror lives (default ~/sketchgen-backups)
  --no-jobs   skip the 300 MB attempt archive; snapshots only
USAGE
}

HOST=""
DEST_ROOT="$HOME/sketchgen-backups"
PULL_JOBS=1

while [ $# -gt 0 ]; do
    case "$1" in
        --dest)    DEST_ROOT="${2:-}"; shift 2 || true ;;
        --no-jobs) PULL_JOBS=0; shift ;;
        -h|--help) usage; exit 0 ;;
        -*)        red "pull-backup: unknown option $1" >&2; usage; exit 3 ;;
        *)         if [ -n "$HOST" ]; then red "pull-backup: one HOST, not two" >&2; exit 3; fi
                   HOST="$1"; shift ;;
    esac
done

if [ -z "$HOST" ]; then
    red "pull-backup: refused — no HOST given" >&2
    usage
    exit 3
fi
for tool in rsync python3; do
    command -v "$tool" >/dev/null 2>&1 || { red "pull-backup: refused — $tool is not on PATH" >&2; exit 3; }
done

DEST="$DEST_ROOT/$HOST"
mkdir -p "$DEST/backups" || { red "pull-backup: cannot create $DEST" >&2; exit 1; }

echo "pull-backup: host      $HOST"
echo "pull-backup: mirror    $DEST"
echo "pull-backup: started   $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"

# --- 1. snapshots -------------------------------------------------------------
# --delete-excluded implies --delete; both are spelled out because the asymmetry
# with the jobs/ pull below is the point and should not have to be inferred.
step "Pulling snapshots"
if ! rsync -a --delete --delete-excluded \
        --exclude '.*' \
        "$HOST:sketchgen/backups/" "$DEST/backups/"; then
    red "pull-backup: rsync of backups/ failed" >&2
    exit 1
fi
green "snapshots mirrored"

# --- 2. the attempt archive ---------------------------------------------------
# No --delete. Ever. See the header.
if [ "$PULL_JOBS" -eq 1 ]; then
    step "Pulling the attempt archive (adds only, never deletes)"
    if ! rsync -a "$HOST:sketchgen/jobs/" "$DEST/jobs/"; then
        red "pull-backup: rsync of jobs/ failed" >&2
        exit 1
    fi
    green "attempt archive mirrored"
else
    dim "jobs/ skipped (--no-jobs)"
fi

# --- 3. verify the newest snapshot --------------------------------------------
step "Verifying the newest snapshot"
newest="$(ls -1d "$DEST"/backups/*/ 2>/dev/null | sed 's:/*$::' | sort | tail -1)"
if [ -z "$newest" ]; then
    red "pull-backup: no snapshot in $DEST/backups — has the node's timer ever run?" >&2
    exit 1
fi

verified=no
if python3 "$CLI" backup verify "$newest"; then
    verified=yes
fi

# --- 4. one line, and an opinion about the date -------------------------------
NEWEST="$newest" DEST="$DEST" HOST="$HOST" VERIFIED="$verified" \
MAX_AGE_HOURS="$MAX_AGE_HOURS" PULL_JOBS="$PULL_JOBS" python3 - <<'PY'
import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

newest = Path(os.environ["NEWEST"])
dest = Path(os.environ["DEST"])
max_age = float(os.environ["MAX_AGE_HOURS"])
verified = os.environ["VERIFIED"] == "yes"

manifest = json.loads((newest / "manifest.json").read_text(encoding="utf-8"))
stamp = manifest["snapshot_utc"]
taken = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
age = (datetime.now(timezone.utc) - taken).total_seconds() / 3600.0

size = manifest["files"]["sketchgen.db"]["bytes"]
rows = sum(manifest["row_counts"].values())
said = manifest.get("jobs_directories")
jobs_dir = dest / "jobs"
here = sum(1 for p in jobs_dir.iterdir() if p.is_dir()) if jobs_dir.is_dir() else 0

jobs = f"{here} job dirs"
if said is not None and os.environ["PULL_JOBS"] == "1" and here != said:
    jobs += f" (the node said {said})"

print()
print("%s  %s  %.1f MB  %s rows  %s  %s"
      % (os.environ["HOST"], stamp, size / 1_000_000, rows, jobs,
         "VERIFIED" if verified else "NOT VERIFIED"))

problems = []
if not verified:
    problems.append("the newest snapshot did not verify")
if age > max_age:
    problems.append("the newest snapshot is %.1f hours old (limit %.0f) — check "
                    "sketchgen-backup.timer on the node" % (age, max_age))
for problem in problems:
    print("pull-backup: " + problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
rc=$?

echo "pull-backup: finished  $(date -u +%Y-%m-%dT%H:%M:%SZ) (UTC)"
exit $rc
