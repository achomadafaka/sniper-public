#!/usr/bin/env bash
# HL REAL copy-leg watchdog. This process signs REAL orders (API wallet) — the runner
# itself is fail-closed (HL_REAL_ENABLED=0 + HL_REAL_DRY_RUN=1 unless .env says otherwise).
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
CODE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
export SNIPER_STATE_ROOT="$STATE_ROOT"
cd "$CODE_ROOT" || exit 1

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
DB=$STATE_ROOT/data/hl_intel.sqlite
PIDFILE=$STATE_ROOT/data/hl_real.pid
HEARTBEAT=$STATE_ROOT/data/hl_real.heartbeat
HEARTBEAT_MAX_AGE_SECONDS=120
LOGFILE=$STATE_ROOT/.logs/hl_real.log
WATCHDOG_LOG=$STATE_ROOT/.logs/hl_real_watchdog.log
# MATCH is deliberately RELEASE-INDEPENDENT (no $CODE_ROOT prefix); LAUNCH stays pinned.
# CODE_ROOT resolves through .runtime/hyperliquid/current, so a release-scoped match cannot
# see a runner started from a PREVIOUS release. For the other HL workers that only meant a
# duplicate launch; here it is worse, because the retirement sweep below uses this same match:
# repointing the symlink would strand a real-order-signing runner that "disabled" could no
# longer find or kill, while HL_REAL_ENABLED=1 sits in .env. Reproduced 2026-08-03.
# `--db-path $DB` is absolute, so this stays scoped to THIS state root's runner.
MATCH="scripts/run_hl_real.py --db-path $DB"
LAUNCH="$PY -u $CODE_ROOT/scripts/run_hl_real.py --db-path $DB"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

pid_matches_runner() {
  local pid="${1:-}" cmd first
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  first=${cmd%% *}
  case "$(basename "$first")" in python|python3) ;; *) return 1 ;; esac
  printf '%s' "$cmd" | grep -Fq "$MATCH"
}

find_existing() {
  local pid
  pgrep -f "$MATCH" | while read -r pid; do
    [ "$pid" != "$$" ] || continue
    pid_matches_runner "$pid" && { echo "$pid"; break; }
  done
}

heartbeat_fresh() {
  local modified now
  [ -f "$HEARTBEAT" ] || return 1
  modified=$(stat -c %Y "$HEARTBEAT" 2>/dev/null) || return 1
  now=$(date +%s)
  [ $((now - modified)) -le "$HEARTBEAT_MAX_AGE_SECONDS" ]
}

stop_stale_runner() {
  local pid="$1" i
  log "STALE_HEARTBEAT pid=$pid"
  kill "$pid" 2>/dev/null || true
  for i in 1 2 3 4 5; do
    kill -0 "$pid" 2>/dev/null || return 0
    sleep 1
  done
  kill -KILL "$pid" 2>/dev/null || true
}

mkdir -p "$STATE_ROOT/.logs"

# This lineage has no funded fills and its dry-run reconcile is provider-faulted. Keep it
# formally retired unless the operator explicitly opts the watchdog back in.
if [ "${HL_REAL_WATCHDOG_ENABLED:-0}" != "1" ]; then
  existing=$(find_existing || true)
  if [ -n "${existing:-}" ]; then
    kill "$existing" 2>/dev/null || true
    log "DISABLED stopped_pid=$existing"
  fi
  : > "$PIDFILE"
  exit 0
fi

pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches_runner "$pid"; then
  heartbeat_fresh && exit 0
  stop_stale_runner "$pid"
fi

existing=$(find_existing || true)
if [ -n "${existing:-}" ]; then
  if heartbeat_fresh; then
    echo "$existing" > "$PIDFILE"
    log "reattached pid=$existing"
    exit 0
  fi
  stop_stale_runner "$existing"
fi

setsid nohup $LAUNCH >>"$LOGFILE" 2>&1 </dev/null &
sleep 5
real=$(find_existing || true)
echo "${real:-}" > "$PIDFILE"
if [ -n "${real:-}" ]; then log "RESTARTED pid=$real"; exit 0; fi
log "FAILED_TO_START"
exit 1
