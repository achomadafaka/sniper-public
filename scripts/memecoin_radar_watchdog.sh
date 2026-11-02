#!/usr/bin/env bash
# Coin Radar discovery watchdog. Cron calls this script every minute.
set -u

CODE_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
export SNIPER_STATE_ROOT="$STATE_ROOT"
export PYTHONPATH="$CODE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
WLOG="$STATE_ROOT/logs/memecoin_discovery.log"
PIDFILE="$STATE_ROOT/data/memecoin_radar.pid"
# The launch uses the active release. The match uses the path suffix only.
# A match that contains the release root cannot see a worker from an earlier release.
# The next cron tick then starts a second worker on the same database.
WORKER="$CODE_ROOT/scripts/memecoin_discovery_worker.py"
MATCH="scripts/memecoin_discovery_worker.py"
LOOP_S="${MEMECOIN_RADAR_DISCOVERY_LOOP_S:-300}"
mkdir -p "$STATE_ROOT/logs" "$STATE_ROOT/data"
# Use one watchdog process at a time. Close file descriptor 9 before the worker starts.
exec 9>"$STATE_ROOT/data/memecoin_radar.watchdog.lock" || exit 0
flock -n 9 || exit 0

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WLOG"; }

pid_matches() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
  esac
  [ -r "/proc/$1/cmdline" ] || return 1
  # Accept ONLY two explicit launch forms:
  #   $PY <worker>
  #   $PY -u <worker>
  # Treating the first non-flag argument as the script also accepted
  # `python3 -c 'print(1) # <worker>'`, because the code string ends with the worker
  # path. Any other interpreter flag is rejected rather than skipped.
  argv0=""
  argv1=""
  argv2=""
  index=0
  while IFS= read -r arg; do
    case "$index" in
      0) argv0="$arg" ;;
      1) argv1="$arg" ;;
      2) argv2="$arg" ;;
    esac
    index=$((index + 1))
  done <<EOF
$(tr '\0' '\n' <"/proc/$1/cmdline")
EOF
  [ "$argv0" = "$PY" ] || return 1
  case "$argv1" in
    -u) candidate="$argv2" ;;
    -*) return 1 ;;
    *) candidate="$argv1" ;;
  esac
  case "$candidate" in
    */"$MATCH") return 0 ;;
  esac
  return 1
}




pid=$(cat "$PIDFILE" 2>/dev/null || true)
if pid_matches "$pid" && kill -0 "$pid" 2>/dev/null; then
  exit 0
fi

for proc_dir in /proc/[0-9]*; do
  candidate="${proc_dir##*/}"
  if pid_matches "$candidate" && kill -0 "$candidate" 2>/dev/null; then
    echo "$candidate" > "$PIDFILE"
    exit 0
  fi
done

log "radar discovery daemon down; launching (--loop $LOOP_S)"
cd "$STATE_ROOT" || exit 1
setsid nohup "$PY" "$WORKER" --loop "$LOOP_S" \
  >> "$WLOG" 2>&1 < /dev/null 9>&- &
worker_pid=$!
echo "$worker_pid" > "$PIDFILE"
log "radar discovery daemon launched pid $worker_pid"
