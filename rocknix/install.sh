#!/bin/bash
# rocknix/install.sh — stand up sketchgen on a ROCKNIX handheld (the Retroid Flip 2), up to
# the enable lines. Run it on the device, as root, which is the only account ROCKNIX has:
#   scp rocknix/install.sh flip2-12g:/storage/ && ssh flip2-12g 'bash /storage/install.sh --ref main'
#
# Why not the node install.sh: ROCKNIX has no apt-get, no sudo, no git and no user manager;
# its root is a read-only squashfs and everything lives under /storage, which is $HOME, so
# ~/sketchgen is /storage/sketchgen and the layout matches every other node's
# (AGENTS.md → Where things run). Python is the system 3.14; pip must not byte-compile
# (--no-compile: a wheel install asserts on a missing .pyc otherwise, 2026-10-09); units go
# in /storage/.config/system.d as system units, enabled with plain systemctl.
#
# Flags:
#   --ref REF            branch, tag or commit of profcarroll/sketchgen to fetch as a tarball
#                        (default: main). No git here: the app is an archive, and app/BUILD
#                        records which (sketchgen build reads it when there is no checkout).
#   --archive FILE       a tarball of the app to use instead (git archive HEAD > app.tar on
#                        a laptop), for a branch that is not on GitHub yet.
#   --executor-host URL  the coder's Ollama, over the tailnet (default: sld-cloud's,
#                        http://100.76.50.85:11434).
#   --executor TAG       its model (default: qwen3-coder:30b-a3b-q4_K_M).
#   --llama-server PATH  llama-server (default: /storage/ollamadreno/bin-no8/llama-server).
#   --model FILE         the Gemma gguf (default: /storage/games-external/models/gemma-4-E4B_q4_0-it.gguf).
#   --mmproj FILE        its vision projector, beside it by default; fetched from Hugging Face
#                        (google/gemma-4-E4B-it-qat-q4_0-gguf, 991 MB) when missing.
#   --name TAG           what the records call that model (default: gemma4:e4b-qat-q4_0).
#   --operator LOGIN     SKETCHGEN_OPERATOR, a GitHub username.
#   -h, --help           Print this and exit.
#
# What it does, in order (every step is skipped when already done):
#   1. Checks: ROCKNIX, python3 ≥ 3.12, curl, the model files, the executor host answers.
#   2. The app: /storage/sketchgen/app from the tarball, plus app/BUILD.
#   3. The venv: pip --no-compile of requirements.txt, playwright's Chromium (the node's
#      build; every library it links is already in ROCKNIX), one launch.
#   4. db init (paused on a fresh database: bench first).
#   5. node.env from the flags, never overwritten; the units into /storage/.config/system.d.
#   6. Prints the enable lines and how to open the console and the gallery in Firefox.
# It enables nothing and starts nothing: that is a decision about what this device does.

set -euo pipefail

NODE_HOME="$HOME/sketchgen"                 # /storage/sketchgen: $HOME is /storage on ROCKNIX
APP="$NODE_HOME/app"
VENV_DIR="$NODE_HOME/.venv"
PY="$VENV_DIR/bin/python3"
DB="$NODE_HOME/sketchgen.db"
ENV_FILE="$NODE_HOME/node.env"
UNIT_DIR="/storage/.config/system.d"
REPO="profcarroll/sketchgen"
MODELS="/storage/games-external/models"
MMPROJ_URL="https://huggingface.co/google/gemma-4-E4B-it-qat-q4_0-gguf/resolve/main/gemma-4-E4B-it-mmproj.gguf"

REF=main
ARCHIVE=""
EXECUTOR_HOST="http://100.76.50.85:11434"
EXECUTOR="qwen3-coder:30b-a3b-q4_K_M"
LLAMA_SERVER="/storage/ollamadreno/bin-no8/llama-server"
MODEL="$MODELS/gemma-4-E4B_q4_0-it.gguf"
MMPROJ=""
NAME="gemma4:e4b-qat-q4_0"
OPERATOR=""

red()   { printf '\033[1;31m%s\033[0m\n' "$*"; }
green() { printf '\033[1;32m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
step()  { printf '\n\033[1;36m→ %s\033[0m\n' "$*"; }
die()   { red "FAILED: $*" >&2; exit 1; }
usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]:-$0}" 2>/dev/null | sed 's/^# \{0,1\}//'; }

need_value() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value (try --help)"; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref)           need_value "$@"; REF="$2"; shift 2 ;;
        --archive)       need_value "$@"; ARCHIVE="$2"; shift 2 ;;
        --executor-host) need_value "$@"; EXECUTOR_HOST="$2"; shift 2 ;;
        --executor)      need_value "$@"; EXECUTOR="$2"; shift 2 ;;
        --llama-server)  need_value "$@"; LLAMA_SERVER="$2"; shift 2 ;;
        --model)         need_value "$@"; MODEL="$2"; shift 2 ;;
        --mmproj)        need_value "$@"; MMPROJ="$2"; shift 2 ;;
        --name)          need_value "$@"; NAME="$2"; shift 2 ;;
        --operator)      need_value "$@"; OPERATOR="$2"; shift 2 ;;
        -h|--help)       usage; exit 0 ;;
        *)               die "unknown argument: $1 (try --help)" ;;
    esac
done
[[ "$NAME" == *:* ]] || die "--name is name:tag, like every model id in the database"
[[ -z "$OPERATOR" || "$OPERATOR" =~ ^[A-Za-z0-9-]+$ ]] || die "--operator is a GitHub username"
[[ -n "$MMPROJ" ]] || MMPROJ="$(dirname "$MODEL")/gemma-4-E4B-it-mmproj.gguf"

sg() { "$PY" "$APP/bin/sketchgen" "$@"; }

# --- 1. Checks ----------------------------------------------------------------
step "1. Checks"
grep -q '^OS_NAME="ROCKNIX"' /etc/os-release 2>/dev/null || die "this is for ROCKNIX; see install.sh for a node"
[[ "$HOME" == /storage ]] || die "HOME is $HOME, not /storage: not the account ROCKNIX runs as"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' \
    || die "python3 is $(python3 -V 2>&1); sketchgen needs 3.12 or newer"
command -v curl >/dev/null || die "no curl"
[[ -x "$LLAMA_SERVER" ]] || die "no llama-server at $LLAMA_SERVER (--llama-server)"
[[ -s "$MODEL" ]] || die "no model at $MODEL (--model)"
if [[ ! -s "$MMPROJ" ]]; then
    dim "no vision projector at $MMPROJ; fetching it (991 MB)"
    curl -L -sS --fail -o "$MMPROJ.part" "$MMPROJ_URL" && mv "$MMPROJ.part" "$MMPROJ" \
        || die "could not fetch the projector; pass --mmproj"
fi
if curl -fsS --max-time 8 "$EXECUTOR_HOST/api/tags" 2>/dev/null | grep -q "\"name\":\"$EXECUTOR\""; then
    green "✓ $EXECUTOR at $EXECUTOR_HOST"
else
    red "⚠ $EXECUTOR_HOST does not list $EXECUTOR (is tailscale up here, and the node's Ollama on the tailnet?)"
fi
green "✓ ROCKNIX, $(python3 -V), $LLAMA_SERVER, $(basename "$MODEL") + $(basename "$MMPROJ")"

# --- 2. The app ---------------------------------------------------------------
step "2. The app"
mkdir -p "$NODE_HOME" "$NODE_HOME/jobs"
if [[ -d "$APP" ]]; then
    dim "already at $APP ($(cat "$APP/BUILD" 2>/dev/null | head -1)); left where it is"
else
    tmp=$(mktemp -d /storage/sketchgen-install.XXXXXX)
    if [[ -n "$ARCHIVE" ]]; then
        [[ -s "$ARCHIVE" ]] || die "no archive at $ARCHIVE"
        mkdir -p "$tmp/app" && tar -xf "$ARCHIVE" -C "$tmp/app"
        built="archive $(basename "$ARCHIVE")"
    else
        curl -L -sS --fail -o "$tmp/app.tar.gz" "https://github.com/$REPO/archive/$REF.tar.gz" \
            || die "no such ref on github.com/$REPO: $REF"
        mkdir -p "$tmp/app" && tar -xzf "$tmp/app.tar.gz" -C "$tmp/app" --strip-components=1
        built="github $REPO $REF"
    fi
    [[ -f "$tmp/app/bin/sketchgen" ]] || die "that is not a sketchgen tree"
    printf '%s\nfetched %s by rocknix/install.sh\n' "$built" "$(date -u +%FT%TZ)" > "$tmp/app/BUILD"
    mv "$tmp/app" "$APP" && rm -rf "$tmp"
fi
green "✓ $APP: $(head -1 "$APP/BUILD")"

# --- 3. The venv and Chromium -------------------------------------------------
step "3. The venv, the pinned packages, Chromium"
[[ -x "$PY" ]] || python3 -m venv "$VENV_DIR"
"$PY" -m pip install -q --disable-pip-version-check --no-compile -r "$APP/requirements.txt"
"$PY" -m playwright install chromium
want=$(sed -n 's/^playwright==//p' "$APP/requirements.txt")
have=$("$PY" -m pip show playwright | sed -n 's/^Version: //p')
[[ "$have" == "$want" ]] || die "playwright is $have, requirements.txt pins $want"
"$PY" -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch().close()' || die "Chromium does not launch (ROCKNIX ships every library it needs as of 20261001)"
green "✓ playwright $have; headless Chromium launches"

# --- 4. The database ----------------------------------------------------------
step "4. The database"
fresh_db=no
[[ -e "$DB" ]] || fresh_db=yes
sg db init --db "$DB" >/dev/null
if [[ $fresh_db == yes ]]; then
    sg control pause --reason "bench first" --db "$DB" >/dev/null
    green "✓ $DB created, generator paused (a new node benches before it takes jobs)"
else
    green "✓ $DB migrated; its switch left as it was"
fi

# --- 5. node.env and the units ------------------------------------------------
step "5. node.env and the units"
conf=$(
    printf '# Written by rocknix/install.sh on %s. This device'"'"'s settings; never overwritten.\n' "$(date -u +%F)"
    printf '# See rocknix/node.env.example for what each line is.\n'
    printf 'LLAMA_SERVER=%s\nLLAMA_MODEL=%s\nLLAMA_MMPROJ=%s\n' "$LLAMA_SERVER" "$MODEL" "$MMPROJ"
    printf 'LLAMA_ARGS=-dev none -t 4 -tb 4 -fa on -c 8192 -np 1 --cache-ram 0\n'
    printf 'LLAMA_CPUS=4-7\nLLAMA_PORT=8092\nLLAMA_ENV=/dev/null\n'
    printf 'SKETCHGEN_PLANNER_MODEL=%s\nSKETCHGEN_JUDGE_MODEL=%s\nSKETCHGEN_CRITIC_MODEL=%s\n' "$NAME" "$NAME" "$NAME"
    printf 'SKETCHGEN_EXECUTOR_MODEL=%s\nSKETCHGEN_EXECUTOR_HOST=%s\n' "$EXECUTOR" "$EXECUTOR_HOST"
    printf 'SKETCHGEN_SHAPE=%s\n' "$(sed -n 's/^HW_CPU="\(.*\)"/\1/p' /etc/os-release | head -1) handheld, ROCKNIX $(sed -n 's/^OS_VERSION="\(.*\)"/\1/p' /etc/os-release)"
    [[ -z "$OPERATOR" ]] || printf 'SKETCHGEN_OPERATOR=%s\n' "$OPERATOR"
)
if [[ ! -e "$ENV_FILE" ]]; then
    printf '%s\n' "$conf" > "$ENV_FILE"
    dim "wrote $ENV_FILE"
elif ! diff -q <(printf '%s\n' "$conf" | grep -v '^#') <(grep -v '^#' "$ENV_FILE") >/dev/null; then
    red "kept the existing $ENV_FILE; these flags would have written it differently:"
    diff <(grep -v '^#' "$ENV_FILE") <(printf '%s\n' "$conf" | grep -v '^#') | sed 's/^/    /' || true
fi
mkdir -p "$UNIT_DIR"
for unit in "$APP"/rocknix/units/*.service; do
    cp "$unit" "$UNIT_DIR/$(basename "$unit")"
done
systemctl daemon-reload
green "✓ $(ls "$APP"/rocknix/units/*.service | xargs -n1 basename | tr '\n' ' ')in $UNIT_DIR"

# --- 6. What is left to a person -----------------------------------------------
step "6. Done. Nothing is enabled."
cat <<EOF
  systemctl enable --now sketchgen-llama sketchgen-shim sketchgen-web     # the model, its Ollama face, the console
  systemctl enable --now sketchgen-worker                                 # then the generator (resume it: sketchgen control resume)
  systemctl enable --now sketchgen-gallery                                # after the first rocknix/publish-local.sh

  /storage/apps/firefox-rocknix/launch.sh http://127.0.0.1:8081/          # the console, on the device
  SKETCHGEN_URL=http://127.0.0.1:8090 /storage/apps/firefox-rocknix/launch.sh --sketchgen kiosk

  SG="$PY $APP/bin/sketchgen"; \$SG control resume --db $DB
EOF
