#!/usr/bin/env bash
# Operator-installed owner only: this file never installs cron or changes configuration.
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="$(realpath -m -- "${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}")"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
WORKER="$REPO_ROOT/scripts/memecoin_desk_intake_assembler.py"
PROC_ROOT="${DESK_INTAKE_ASSEMBLER_WATCHDOG_PROC_ROOT:-/proc}"
DATA="$STATE_ROOT/data/desk/assembler_intake"
[[ "$(dirname "$REPO_ROOT")" == "$STATE_ROOT/.runtime/memecoin/releases" && "${REPO_ROOT##*/}" =~ ^[0-9a-f]{40,64}$ ]] || {
  echo 'intake assembler requires a pinned release outside mutable state' >&2; exit 2;
}
[[ "$PY" == /* && -x "$PY" && -f "$WORKER" ]] || { echo 'intake assembler worker or interpreter unavailable' >&2; exit 2; }
export SNIPER_STATE_ROOT="$STATE_ROOT"
mkdir -p "$DATA" "$STATE_ROOT/logs"
exec 9>>"$DATA/watchdog.lock"
flock -n 9 || exit 0
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
for pid in "${DESK_SUPERSEDED[@]}"; do
  # PID files are advisory. A pidfd binds the signal to the exact process we recheck, never a reused PID.
  if [[ "$PROC_ROOT" != /proc ]]; then
    desk_proc_term "$PROC_ROOT" "$pid" || { echo 'superseded process unproved; no replacement' >&2; exit 2; }
  else
    "$PY" - "$pid" "$STATE_ROOT" "$WORKER" <<'PY'
import os,signal,sys
from pathlib import Path
pid,root,worker=int(sys.argv[1]),Path(sys.argv[2]),Path(sys.argv[3])
fd=None
try:
    fd=os.pidfd_open(pid)
    proc=Path('/proc')/str(pid)
    args=(proc/'cmdline').read_bytes().split(b'\0')
    env=dict(item.split(b'=',1) for item in (proc/'environ').read_bytes().split(b'\0') if b'=' in item)
    old=Path(os.fsdecode(args[1]))
    release=old.parent.parent
    if (not Path(os.fsdecode(args[0])).name.startswith('python') or old.name!=worker.name or old==worker
            or release.parent!=root/'.runtime/memecoin/releases'
            or len(release.name) not in (40,64) or any(c not in '0123456789abcdef' for c in release.name)
            or Path(os.fsdecode(env.get(b'SNIPER_STATE_ROOT',b'/home/bigpapi/Desktop/Sniper'))).resolve()!=root):
        raise ValueError('process identity unproved')
    signal.pidfd_send_signal(fd,signal.SIGTERM)
except ProcessLookupError:
    pass
except (OSError,ValueError,IndexError,AttributeError):
    raise SystemExit(2)
finally:
    if fd is not None:os.close(fd)
PY
  fi
done
for ((attempt=0; attempt<20; attempt++)); do
  desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
  [[ "$DESK_ALIVE" -eq 0 ]] || exit 0
  [[ "${#DESK_SUPERSEDED[@]}" -gt 0 ]] || break
  sleep 0.5
done
desk_proc_scan "$PROC_ROOT" "$WORKER" "$STATE_ROOT"
[[ "$DESK_ALIVE" -eq 0 ]] || exit 0
[[ "${#DESK_SUPERSEDED[@]}" -eq 0 ]] || { echo 'superseded worker still exiting; no replacement' >&2; exit 2; }
cd "$STATE_ROOT"
nohup "$PY" "$WORKER" --desk-db "$STATE_ROOT/data/memecoin_desk.db" --loop \
  </dev/null >>"$STATE_ROOT/logs/memecoin_desk_intake_assembler.log" 2>&1 9>&- &
printf '%s\n' "$!" >"$DATA/assembler.pid"
