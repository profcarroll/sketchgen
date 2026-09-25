#!/usr/bin/env bash
# install.sh — stand up sketchgen on a fresh private node, up to the enable line.
#
# From the laptop, on a node you can ssh to (the app is a public repo):
#   scp install.sh NODE: && ssh -t NODE 'bash install.sh --ref a72f076'
# or on the node itself:
#   curl -fsSL https://raw.githubusercontent.com/profcarroll/sketchgen/main/install.sh | bash -s -- --ref main
#
# Flags:
#   --ref REF           commit, tag or branch to check out on a fresh clone
#                       (default: main). An A/B arm pins the other arms' build,
#                       and anything but main is written down as this node's
#                       pin (step 5b), so update.sh keeps it there.
#                       An existing ~/sketchgen/app is never moved: that is
#                       update.sh's job, and a checkout under a running worker
#                       is a trap.
#   --executor TAG      SKETCHGEN_EXECUTOR_MODEL (default: qwen3-coder:30b).
#   --ollama-url URL    OLLAMA_HOST_URL (default: http://127.0.0.1:11434). A box
#                       whose Ollama listens on its tailnet address needs it.
#   --rate DOLLARS      SKETCHGEN_RATE_PER_HOUR, for a node that bills by the
#                       hour. Leave it off a node that does not.
#   --shape TEXT        SKETCHGEN_SHAPE, what the Node card calls this machine.
#   --operator LOGIN    SKETCHGEN_OPERATOR, a GitHub username and nothing else.
#   -h, --help          Print this and exit.
#
# What it does, in order (every step is skipped when already done):
#   1. System packages: python3-venv, sqlite3, git, rsync, curl — through sudo, which
#      prompts when there is a terminal and otherwise prints the command and
#      stops (exit 2). Nothing else here uses sudo, except step 4b's.
#   2. Linger, so the --user units survive logout and reboot.
#   3. Clones the app into ~/sketchgen/app at --ref.
#   4. The venv in ~/sketchgen/.venv, requirements.txt (pinned), Chromium.
#   4b. Launches Chromium once; if the system libraries it needs are missing,
#      runs `playwright install-deps chromium` under sudo, as step 1 does.
#   5. `db init`: creates or migrates ~/sketchgen/sketchgen.db.
#   5b. On a clone this run made at a --ref other than main, the pin.
#   6. A private gallery: a local bare repo as the checkout's origin, so
#      publish works and nothing leaves the box. A gallery already there is
#      left alone — a public node's checkout is set up by hand (OPERATIONS.md).
#   7. `install-unit`, then a node.conf drop-in for the worker and web units
#      from the flags above. An existing node.conf is never overwritten; if the
#      flags differ from it, the difference is printed.
#   8. On a database this run created, pauses the generator ("bench first").
#   9. Checks Ollama answers at --ollama-url and has the models; warns if not.
#
# It does NOT install Ollama or a GPU driver (both are pinned to the other
# nodes' versions by hand: OPERATIONS.md → A rented GPU node, steps 2 and 5),
# and it does NOT enable anything. Enabling a unit is a decision about what
# this node does; the last thing it prints is the lines that do it.

set -euo pipefail

NODE_HOME="$HOME/sketchgen"
APP="$NODE_HOME/app"
GALLERY="$NODE_HOME/gallery"
VENV_DIR="$NODE_HOME/.venv"
PY="$VENV_DIR/bin/python3"
DB="$NODE_HOME/sketchgen.db"
REPO_URL="https://github.com/profcarroll/sketchgen.git"
PACKAGES=(python3-venv sqlite3 git rsync curl)
PLANNER="gemma4:e4b"

REF=main
EXECUTOR="qwen3-coder:30b"
OLLAMA_URL="http://127.0.0.1:11434"
RATE=""
SHAPE=""
OPERATOR=""

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;36m→ %s\033[0m\n' "$*"; }
die()   { red "FAILED: $*" >&2; exit 1; }

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

ARGS=("$@")
need_value() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value (try --help)"; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)        need_value "$@"; REF="$2"; shift 2 ;;
        --executor)   need_value "$@"; EXECUTOR="$2"; shift 2 ;;
        --ollama-url) need_value "$@"; OLLAMA_URL="$2"; shift 2 ;;
        --rate)       need_value "$@"; RATE="$2"; shift 2 ;;
        --shape)      need_value "$@"; SHAPE="$2"; shift 2 ;;
        --operator)   need_value "$@"; OPERATOR="$2"; shift 2 ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "unknown argument: $1 (try --help)" ;;
    esac
done
[[ -z "$RATE" || "$RATE" =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "--rate is dollars an hour, like 2.00"
[[ -z "$OPERATOR" || "$OPERATOR" =~ ^[A-Za-z0-9-]+$ ]] || die "--operator is a GitHub username"

# as_root CMD... — run CMD under sudo. With a terminal sudo can prompt; without
# one, only a sudo that needs no password will do. Otherwise this stops, before
# anything that depends on it, and says to run it again from a terminal: one
# password then covers every step, where printing the first root command alone
# would stop again at the next.
as_root() {
    if [[ $EUID -eq 0 ]]; then "$@"; return; fi
    if sudo -n true 2>/dev/null || [[ -t 0 ]]; then sudo "$@"; return; fi
    red "This step needs root (sudo $*), and there is no terminal for sudo to ask on." >&2
    red "Run install.sh again from one; it picks up where it stopped:" >&2
    printf "  ssh -t <this node> 'bash install.sh%s'\n" "$(printf ' %q' "${ARGS[@]}")" >&2
    exit 2
}

sg() { "$PY" "$APP/bin/sketchgen" "$@"; }

[[ "$(uname -s)" == Linux ]] || die "this is for a Linux node, not $(uname -s)"
command -v apt-get >/dev/null || die "no apt-get: this script knows Ubuntu/Debian nodes only"

# --- 1. System packages ------------------------------------------------------
step "1. System packages"
missing=()
for p in "${PACKAGES[@]}"; do dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p"); done
if ((${#missing[@]})); then
    dim "missing: ${missing[*]}"
    as_root apt-get update -q
    as_root apt-get install -y -q "${missing[@]}"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || die "python3 is $(python3 -V 2>&1); sketchgen needs 3.12 or newer"
green "✓ ${PACKAGES[*]}; $(python3 -V)"

# --- 2. Linger ---------------------------------------------------------------
step "2. Linger"
if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != yes ]]; then
    loginctl enable-linger "$USER" 2>/dev/null || as_root loginctl enable-linger "$USER"
fi
[[ "$(loginctl show-user "$USER" -p Linger --value)" == yes ]] || die "linger is still off for $USER"
green "✓ Linger=yes for $USER"

# --- 3. The app --------------------------------------------------------------
step "3. The app"
mkdir -p "$NODE_HOME"
cloned=no
if [[ -d "$APP/.git" ]]; then
    dim "already cloned; left where it is (update.sh moves it, not this)"
else
    cloned=yes
    git clone -q "$REPO_URL" "$APP"
    git -C "$APP" checkout -q "$REF" || die "no such ref in $REPO_URL: $REF"
fi
green "✓ $(git -C "$APP" log --oneline -1)"

# --- 4. The venv and Chromium ------------------------------------------------
step "4. The venv, the pinned packages, Chromium"
[[ -x "$PY" ]] || python3 -m venv "$VENV_DIR"
"$PY" -m pip install -q --disable-pip-version-check -r "$APP/requirements.txt"
"$VENV_DIR/bin/playwright" install chromium
want=$(sed -n 's/^playwright==//p' "$APP/requirements.txt")
have=$("$PY" -m pip show playwright | sed -n 's/^Version: //p')
[[ "$have" == "$want" ]] || die "playwright is $have, requirements.txt pins $want"
green "✓ playwright $have"

step "4b. Chromium actually starts"
launch='from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()'
if ! "$PY" -c "$launch" 2>/dev/null; then
    dim "it does not; installing the system libraries it needs"
    as_root "$VENV_DIR/bin/playwright" install-deps chromium
    "$PY" -c "$launch" || die "Chromium still does not launch"
fi
green "✓ headless Chromium launches"

# --- 5. The database ---------------------------------------------------------
step "5. The database"
fresh_db=no
[[ -e "$DB" ]] || fresh_db=yes
sg db init --db "$DB" >/dev/null
green "✓ $DB ($([[ $fresh_db == yes ]] && echo created || echo migrated))"

# --- 5b. The pin -------------------------------------------------------------
# Until 2026-09-24 an --ref arm was held on its build by a detached HEAD alone,
# and update.sh's `git pull origin main` fast-forwards one without a word
# (docs/plans/fleet.md §0). The rows are the ones `sketchgen pin` writes
# (sketchgen/build.py, PIN_KEYS), written here directly because the build this
# clone is at may predate the verb — a72f076 does.
if [[ $cloned == yes && "$REF" != main ]]; then
    pin_sha=$(git -C "$APP" rev-parse "$REF^{commit}")
    "$PY" - "$DB" "$pin_sha" "install.sh --ref $REF" "${OPERATOR:-$USER}" <<'PY'
import sqlite3, sys, time
db, sha, reason, by = sys.argv[1:5]
conn = sqlite3.connect(db, isolation_level=None)
if not (conn.execute("SELECT value FROM meta WHERE key = 'pin.sha'").fetchone() or [None])[0]:
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for key, value in (("pin.sha", sha), ("pin.reason", reason),
                       ("pin.by", by), ("pin.utc", now)):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
PY
    green "✓ pinned to ${pin_sha:0:7}; update.sh keeps it there (sketchgen pin --clear to follow main)"
fi

# --- 6. The gallery ----------------------------------------------------------
step "6. A private gallery"
if [[ -e "$GALLERY" ]]; then
    dim "a gallery checkout is already here; left alone"
else
    git init -q --bare "$NODE_HOME/gallery.git"
    git clone -q "$NODE_HOME/gallery.git" "$GALLERY" 2>/dev/null
    git -C "$GALLERY" config user.name profcarroll
    git -C "$GALLERY" config user.email profcarroll@users.noreply.github.com
fi
green "✓ $GALLERY → $(git -C "$GALLERY" remote get-url origin 2>/dev/null || echo 'no origin')"

# --- 7. Units and their settings ---------------------------------------------
step "7. Units and their settings"
sg install-unit >/dev/null
# The web unit's default OLLAMA_MODELS is sld-cloud's block volume; everywhere
# else the store is wherever Ollama's own unit says, or the installer's default.
models=$(systemctl show ollama -p Environment --value 2>/dev/null | tr ' ' '\n' | sed -n 's/^OLLAMA_MODELS=//p')
models=${models:-/usr/share/ollama/.ollama/models}
conf=$(
    printf '# Written by install.sh on %s. Settings for this node live here, not in\n' "$(date -u +%F)"
    printf '# the unit files, which install-unit and update.sh overwrite.\n[Service]\n'
    printf 'Environment=OLLAMA_HOST_URL=%s\n' "$OLLAMA_URL"
    printf 'Environment=SKETCHGEN_EXECUTOR_MODEL=%s\n' "$EXECUTOR"
    printf 'Environment=OLLAMA_MODELS=%s\n' "$models"
    [[ -z "$RATE" ]]     || printf 'Environment=SKETCHGEN_RATE_PER_HOUR=%s\n' "$RATE"
    [[ -z "$SHAPE" ]]    || printf 'Environment="SKETCHGEN_SHAPE=%s"\n' "$SHAPE"
    [[ -z "$OPERATOR" ]] || printf 'Environment=SKETCHGEN_OPERATOR=%s\n' "$OPERATOR"
)
for unit in sketchgen-worker sketchgen-web; do
    dir="$HOME/.config/systemd/user/$unit.service.d"
    mkdir -p "$dir"
    if [[ ! -e "$dir/node.conf" ]]; then
        printf '%s\n' "$conf" > "$dir/node.conf"
        dim "wrote $dir/node.conf"
    elif ! diff -q <(printf '%s\n' "$conf" | grep '^Environment') <(grep '^Environment' "$dir/node.conf") >/dev/null; then
        red "kept the existing $dir/node.conf; these flags would have written it differently:"
        diff <(grep '^Environment' "$dir/node.conf") <(printf '%s\n' "$conf" | grep '^Environment') | sed 's/^/    /' || true
    fi
done
systemctl --user daemon-reload
green "✓ units installed; settings in node.conf"

# --- 8. Bench first ----------------------------------------------------------
step "8. The generator's switch"
if [[ $fresh_db == yes ]]; then
    sg control pause --reason "bench first" --db "$DB" >/dev/null
    green "✓ paused (a new node benches before it takes jobs)"
else
    dim "existing database; its switch is left as it was"
fi

# --- 9. Ollama ---------------------------------------------------------------
step "9. Ollama"
if tags=$(curl -fsS --max-time 5 "$OLLAMA_URL/api/tags" 2>/dev/null); then
    for m in "$EXECUTOR" "$PLANNER"; do
        if grep -q "\"name\":\"$m\"" <<<"$tags"; then green "✓ $m"
        else red "⚠ $m is not pulled here: ollama pull $m"; fi
    done
else
    red "⚠ nothing answers at $OLLAMA_URL. Install Ollama at the other nodes' version:"
    echo "    curl -fsSL https://ollama.com/install.sh | OLLAMA_VERSION=<version> sh"
fi

# --- Next --------------------------------------------------------------------
# bench arrived after some builds an A/B arm pins; offer it only where it exists.
if sg bench --help >/dev/null 2>&1; then
    bench="$PY $APP/bin/sketchgen bench --out $NODE_HOME/bench-$(hostname).json"
else
    bench="# no \`bench\` at this build; bench from a newer checkout (a10-benchmark.md, L0)"
fi
cat <<EOF

$(green "✓ installed. Nothing is enabled yet; that is yours to do:")

  systemctl --user enable --now sketchgen-worker.service sketchgen-web.service sketchgen-backup.timer
  $bench
  $PY $APP/bin/sketchgen control resume --db $DB

sketchgen-sync.timer stays off on a private node: there is no write-path Worker for it.
EOF
