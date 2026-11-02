#!/usr/bin/env bash
# Default-OFF, single-flight watchdog for the isolated opportunity capture worker.
set -u
RUNTIME_ROOT=$(cd -P "$(dirname "$0")/.." && pwd)
STATE_ROOT=/home/bigpapi/Desktop/Sniper
export PYTHONPATH="$RUNTIME_ROOT${PYTHONPATH:+:$PYTHONPATH}"
export SNIPER_STATE_ROOT="$STATE_ROOT"
cd "$STATE_ROOT" || exit 1
mkdir -p .logs data
exec 9>data/memecoin_opportunity.watchdog.lock || exit 0
flock -n 9 || exit 0

PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3
PIDFILE=data/memecoin_opportunity.pid
LOGFILE=.logs/memecoin_opportunity.log
# DISCOVERY is release-INDEPENDENT and LAUNCH is release-PINNED. A discovery match carrying
# $RUNTIME_ROOT stops recognizing the running worker as soon as a deploy repoints `current`, and the
# next cron tick then starts a SECOND worker on the same database — the hazard already recorded in
# the 2026-08-07 deploy note. Cutting a worker over to a new release is the deploy's job.
MATCH="scripts/memecoin_opportunity_worker.py"
WORKER="$RUNTIME_ROOT/scripts/memecoin_opportunity_worker.py"

"$PY" -u "$WORKER" --enabled-check >/dev/null 2>&1 || exit 0

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
if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && pid_matches "$pid"; then
  exit 0
fi

# 9>&- is load-bearing: without it the worker inherits the single-flight lock and holds it for life,
# so every later cron tick fails `flock -n 9` and exits 0 before any check runs (measured 2026-08-12).
setsid nohup "$PY" -u "$WORKER" >>"$LOGFILE" 2>&1 </dev/null 9>&- &
worker_pid=$!
echo "$worker_pid" >"$PIDFILE"
sleep 2
kill -0 "$worker_pid" 2>/dev/null || exit 1
