#!/usr/bin/env bash
# sketchgen-tunnel.sh — manage the SSH local forward to the sketchgen console
# on sld-cloud.
#
# Why this exists: `ssh -f -N -L ...` backgrounds itself BEFORE reporting a bind
# failure, and without ExitOnForwardFailure a failed forward leaves a live SSH
# session that holds no listener and never retries the bind. Re-running the raw
# command then stacks more of these silent stubs. This script always starts the
# tunnel with ExitOnForwardFailure=yes, health-checks through the port rather
# than trusting that a process exists, and reaps listener-less strays.

set -uo pipefail

LOCAL_PORT="${SKETCHGEN_LOCAL_PORT:-8081}"
REMOTE_HOST="${SKETCHGEN_REMOTE_HOST:-sld-cloud}"
REMOTE_BIND="${SKETCHGEN_REMOTE_BIND:-127.0.0.1}"
REMOTE_PORT="${SKETCHGEN_REMOTE_PORT:-8081}"
HEALTH_PATH="${SKETCHGEN_HEALTH_PATH:-/}"

FWD_SPEC="${LOCAL_PORT}:${REMOTE_BIND}:${REMOTE_PORT}"
URL="http://127.0.0.1:${LOCAL_PORT}${HEALTH_PATH}"
STARTUP_WAIT=15   # seconds to wait for the forward to answer after starting

SSH_OPTS=(
  -f -N
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval=15
  -o ServerAliveCountMax=3
  -o ConnectTimeout=15
  -o BatchMode=yes
)

c_red=''; c_grn=''; c_yel=''; c_dim=''; c_off=''
if [[ -t 1 ]]; then
  c_red=$'\033[31m'; c_grn=$'\033[32m'; c_yel=$'\033[33m'
  c_dim=$'\033[2m';  c_off=$'\033[0m'
fi
ok()   { printf '%s✓%s %s\n' "$c_grn" "$c_off" "$*"; }
warn() { printf '%s!%s %s\n' "$c_yel" "$c_off" "$*"; }
err()  { printf '%s✗%s %s\n' "$c_red" "$c_off" "$*" >&2; }
dim()  { printf '%s%s%s\n' "$c_dim" "$*" "$c_off"; }

# --- state inspection -------------------------------------------------------

# PIDs of ssh processes carrying *our* forward spec to *our* host.
tunnel_pids() {
  local pid cmd
  for pid in $(pgrep -x ssh 2>/dev/null); do
    cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) || continue
    [[ $cmd == *"-L $FWD_SPEC"* && $cmd == *"$REMOTE_HOST"* ]] && printf '%s\n' "$pid"
  done
}

# PIDs currently holding a LISTEN socket on the local port (any address family).
listener_pids() {
  ss -tlnp 2>/dev/null \
    | awk -v p=":${LOCAL_PORT}\$" '$4 ~ p' \
    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
}

# Our ssh processes that hold NO listener — the silent-stub failure mode.
stale_pids() {
  local live pid
  live=" $(listener_pids | tr '\n' ' ') "
  for pid in $(tunnel_pids); do
    [[ $live == *" $pid "* ]] || printf '%s\n' "$pid"
  done
}

# Anything listening on our port that is NOT one of our tunnels.
foreign_listener_pids() {
  local mine pid
  mine=" $(tunnel_pids | tr '\n' ' ') "
  for pid in $(listener_pids); do
    [[ $mine == *" $pid "* ]] || printf '%s\n' "$pid"
  done
}

# True if the port actually answers HTTP. This is the real test: a live ssh
# process proves nothing.
health() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$URL" 2>/dev/null)
  [[ -n $code && $code != 000 && $code -lt 500 ]]
}

http_code() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$URL" 2>/dev/null
}

remote_listening() {
  timeout 25 ssh -o BatchMode=yes -o ConnectTimeout=15 "$REMOTE_HOST" \
    "ss -tln 2>/dev/null | grep -q '${REMOTE_BIND}:${REMOTE_PORT}'" 2>/dev/null
}

reap() {
  local pids n
  pids=$(stale_pids)
  [[ -z $pids ]] && return 0
  n=$(printf '%s\n' "$pids" | grep -c .)
  warn "reaping $n listener-less tunnel process(es): $(echo $pids)"
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  sleep 1
  pids=$(stale_pids)
  # shellcheck disable=SC2086
  [[ -n $pids ]] && kill -9 $pids 2>/dev/null
  return 0
}

# --- billing refresh --------------------------------------------------------
# Opening the tunnel is the operator sitting down to look at the console — the
# one moment the OCI key (on THIS laptop, never the node — see cli/billing.py)
# is both present and wanted. So we take a fresh usage reading and ship it to
# the node, where the console's billing card reads it. Before this the card
# only moved when someone remembered to run `billing --sync` by hand, so it sat
# stale for days.
#
# Deliberately non-fatal and independent of the -L forward: `billing --sync`
# opens its own ssh (cli/billing.py), so a billing hiccup — OCI slow, key
# missing, network down — must never cost you the tunnel you actually came for.
# Runs only after cmd_up succeeds; skip it entirely with SKETCHGEN_NO_BILLING=1.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BILLING_TIMEOUT="${SKETCHGEN_BILLING_TIMEOUT:-30}"

# The sketchgen CLI on THIS laptop. Not a sibling of this script: sgt runs a
# copy deployed to ~/.local/bin, away from bin/sketchgen, so guessing $SCRIPT_DIR
# looked for a CLI that was not there. Try, in order: an explicit override, the
# PATH, the usual laptop checkout, then a sibling for when it IS run from bin/.
find_sketchgen() {
  [[ -n "${SKETCHGEN_CLI:-}" ]] && { printf '%s\n' "$SKETCHGEN_CLI"; return 0; }
  local c
  for c in \
    "$(command -v sketchgen 2>/dev/null)" \
    "$HOME/sketchgen/bin/sketchgen" \
    "$SCRIPT_DIR/sketchgen"; do
    [[ -n $c && -f $c ]] && { printf '%s\n' "$c"; return 0; }
  done
  return 1
}

refresh_billing() {
  [[ "${SKETCHGEN_NO_BILLING:-}" == 1 ]] && return 0
  # Bare python3 on the laptop: there is no venv here (AGENTS.md), and ~/.oci is.
  command -v python3 >/dev/null 2>&1 || { warn "no python3 for billing refresh"; return 0; }
  local cli
  cli=$(find_sketchgen) || {
    warn "billing refresh skipped: no sketchgen CLI found"
    dim  "set SKETCHGEN_CLI, or check ~/sketchgen/bin/sketchgen"
    return 0
  }
  dim "refreshing billing from OCI (this laptop's ~/.oci) → ${REMOTE_HOST} ..."
  timeout "$BILLING_TIMEOUT" python3 "$cli" billing --sync "$REMOTE_HOST" \
    || { warn "billing refresh skipped/failed (tunnel is up regardless)"
         dim  "retry: python3 $cli billing --sync $REMOTE_HOST"; }
  return 0
}

# --- commands ---------------------------------------------------------------

cmd_status() {
  local t l s f code
  t=$(tunnel_pids | tr '\n' ' ')
  l=$(listener_pids | tr '\n' ' ')
  s=$(stale_pids | tr '\n' ' ')
  f=$(foreign_listener_pids | tr '\n' ' ')

  echo "tunnel : ${REMOTE_HOST}  ${FWD_SPEC}"
  echo "url    : $URL"
  dim   "ssh pids       : ${t:-none}"
  dim   "listener pids  : ${l:-none}"
  [[ -n ${s// } ]] && warn "stale (no listener): $s"
  [[ -n ${f// } ]] && warn "foreign listener on :${LOCAL_PORT}: $f"

  code=$(http_code)
  if [[ -n $code && $code != 000 && $code -lt 500 ]]; then
    ok "responding (HTTP $code)"
    return 0
  fi
  err "not responding (HTTP ${code:-000})"
  return 1
}

cmd_up() {
  if health; then
    ok "already up — $URL (HTTP $(http_code))"
    return 0
  fi

  reap

  local f
  f=$(foreign_listener_pids | tr '\n' ' ')
  if [[ -n ${f// } ]]; then
    local flist
    flist=$(printf '%s' "$f" | tr -s ' ' | sed 's/^ *//; s/ *$//; s/ /,/g')
    err "port ${LOCAL_PORT} is held by another process: $flist"
    ps -o pid,user,cmd -p "$flist" --no-headers 2>/dev/null | sed 's/^/    /'
    echo "    free it, or run with SKETCHGEN_LOCAL_PORT=<other port>" >&2
    return 1
  fi

  echo "starting tunnel → ${REMOTE_HOST} (${FWD_SPEC}) ..."
  if ! ssh "${SSH_OPTS[@]}" -L "$FWD_SPEC" "$REMOTE_HOST"; then
    err "ssh failed to establish the forward (ExitOnForwardFailure tripped)"
    return 1
  fi

  local i
  for ((i = 0; i < STARTUP_WAIT; i++)); do
    health && { ok "up — $URL (HTTP $(http_code))"; return 0; }
    sleep 1
  done

  # Forward exists but nothing answers: the far-side service is the suspect.
  err "tunnel established but $URL does not answer"
  if remote_listening; then
    err "remote IS listening on ${REMOTE_BIND}:${REMOTE_PORT} — app may be wedged"
  else
    err "remote is NOT listening on ${REMOTE_BIND}:${REMOTE_PORT} — start the app there"
  fi
  return 1
}

cmd_down() {
  local pids n
  pids=$(tunnel_pids)
  if [[ -z $pids ]]; then
    dim "no tunnel processes found"
    return 0
  fi
  n=$(printf '%s\n' "$pids" | grep -c .)
  # shellcheck disable=SC2086
  kill $pids 2>/dev/null
  sleep 1
  pids=$(tunnel_pids)
  # shellcheck disable=SC2086
  [[ -n $pids ]] && kill -9 $pids 2>/dev/null
  ok "stopped $n tunnel process(es)"
}

cmd_heal() {
  echo "healing: tearing down and re-establishing ..."
  cmd_down
  sleep 1
  cmd_up
}

usage() {
  cat <<USAGE
sketchgen-tunnel.sh — SSH forward to the sketchgen console on ${REMOTE_HOST}

  up       (default) start if not already responding; reaps dead stubs first,
                    then refreshes the console's billing card from OCI
  status            show processes, listeners and a real HTTP health check
  heal              force full teardown + restart
  down              stop the tunnel
  url               print the URL

On a successful 'up' a fresh OCI usage reading is taken here (this laptop holds
~/.oci; the node never does) and shipped to the node for the console's billing
card. It is non-fatal — the tunnel is what 'up' guarantees, not the reading.

Env overrides: SKETCHGEN_LOCAL_PORT, SKETCHGEN_REMOTE_HOST,
               SKETCHGEN_REMOTE_BIND, SKETCHGEN_REMOTE_PORT, SKETCHGEN_HEALTH_PATH,
               SKETCHGEN_NO_BILLING=1 (skip the refresh),
               SKETCHGEN_BILLING_TIMEOUT (seconds, default 30),
               SKETCHGEN_CLI (path to the sketchgen CLI, if not auto-found)
USAGE
}

case "${1:-up}" in
  up|start)        cmd_up && refresh_billing ;;
  status|st)       cmd_status ;;
  heal|restart|fix) cmd_heal ;;
  down|stop)       cmd_down ;;
  url)             echo "$URL" ;;
  -h|--help|help)  usage ;;
  *) err "unknown command: $1"; usage; exit 2 ;;
esac
