#!/usr/bin/env bash
# The main watchdog owns this observer. Do not add a separate cron entry.
set -u
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
CODE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
case "$CODE_ROOT" in "$STATE_ROOT"/.runtime/memecoin/releases/*) ;; *) exit 1 ;; esac
export SNIPER_STATE_ROOT="$STATE_ROOT"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
# PER CHAIN SINCE 2026-09-22. Pass the chain id as $1; without one this supervises 4663, so the
# existing main-watchdog call keeps working. Base (8453) runs as a SECOND invocation with its own
# lock, pidfile and process match, so neither chain can adopt or kill the other's observer.
CHAIN="${1:-4663}"
CHAINS=$(grep '^MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS=' "$STATE_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"\047 ')
case ",${CHAINS:-}," in *,"$CHAIN",*) ;; *) exit 0 ;; esac
mkdir -p "$STATE_ROOT/data" "$STATE_ROOT/logs"
if [ "$CHAIN" = "4663" ]; then
  # 4663 keeps every legacy path: the live observer holds these, and renaming them mid-deploy
  # would supervise a process nothing is holding and start a duplicate beside it.
  LOCKFILE="$STATE_ROOT/data/memecoin_onchain_volume.watchdog.lock"
  PIDFILE="$STATE_ROOT/data/memecoin_onchain_volume.pid"
  LOGFILE="$STATE_ROOT/logs/memecoin_onchain_volume.log"
else
  LOCKFILE="$STATE_ROOT/data/memecoin_onchain_volume.watchdog.$CHAIN.lock"
  PIDFILE="$STATE_ROOT/data/memecoin_onchain_volume.$CHAIN.pid"
  LOGFILE="$STATE_ROOT/logs/memecoin_onchain_volume.$CHAIN.log"
fi
exec 10>"$LOCKFILE"
flock -n 10 || exit 0
WORKER="$CODE_ROOT/scripts/memecoin_onchain_volume_worker.py"
MATCH="/scripts/memecoin_onchain_volume_worker.py"
valid_pid() {
  local cmd first tail
  [[ "${1:-}" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$1" 2>/dev/null || return 1
  cmd=$(tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true)
  first=${cmd%% *}
  case "$(basename "$first")" in python|python3) ;; *) return 1 ;; esac
  [[ "$cmd" == *"$MATCH"* ]] || return 1
  # THE CHAIN MUST MATCH, or 4663's supervisor would adopt Base's observer and then kill it as a
  # stale release. The pre-2026-09-22 worker took no argument and is always 4663.
  tail=${cmd#*"$MATCH"}
  tail=${tail// /}
  [[ "$tail" == "$CHAIN" ]] || { [ "$CHAIN" = "4663" ] && [ -z "$tail" ]; }
}
pid=$(cat "$PIDFILE" 2>/dev/null || true)
if ! valid_pid "$pid"; then
  pid=""
  while read -r candidate; do
    if valid_pid "$candidate"; then pid="$candidate"; break; fi
  done < <(pgrep -f "$MATCH" || true)
fi
if valid_pid "$pid"; then
  cmd=$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)
  if [[ "$cmd" == *"$WORKER"* ]]; then echo "$pid" > "$PIDFILE"; exit 0; fi
  # A release switch requires a new observer process.
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
  kill -0 "$pid" 2>/dev/null && exit 1
fi
setsid nohup "$PY" -u "$WORKER" "$CHAIN" >>"$LOGFILE" 2>&1 </dev/null 8>&- 9>&- 10>&- &
echo "$!" > "$PIDFILE"
