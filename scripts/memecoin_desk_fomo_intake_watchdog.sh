#!/usr/bin/env bash
# Operator-installed cron owner for the pinned Fomo collector. No .env, login or halt clearing.
# The xvfb-run PID is advisory; process authority comes from script, state root AND profile.
set -euo pipefail
umask 077

REPO_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
STATE_ROOT="$(realpath -m -- "${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}")"
PROFILE="$(readlink -m -- "${HOME:?}/.local/share/sniper/fomo_intake_profile")"
WORKER="$REPO_ROOT/scripts/memecoin_desk_fomo_intake.py"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
PROC_ROOT="${DESK_FOMO_INTAKE_WATCHDOG_PROC_ROOT:-/proc}"
DATA="$STATE_ROOT/data/desk/intake/fomo"

[[ "$STATE_ROOT" != "$REPO_ROOT" && "$STATE_ROOT" != "$REPO_ROOT/"* ]] || { echo 'Fomo state root must be outside the release' >&2; exit 2; }
[[ "$PY" == /* && -x "$PY" && -f "$WORKER" ]] || { echo 'Fomo interpreter or worker unavailable' >&2; exit 2; }
[[ -d "$PROFILE" && "$(stat -c %a -- "$PROFILE")" == 700 ]] && \
  cmp -s -- <(printf 'fomo-intake-v1\n') "$PROFILE/.sniper-fomo-intake-profile" || \
  { echo 'Fomo dedicated profile is not initialized/private' >&2; exit 2; }
mkdir -p "$DATA" "$STATE_ROOT/logs"
exec 9>"$DATA/watchdog.lock"
flock -n 9 || exit 0
[[ ! -e "$PROFILE/halt.json" && ! -L "$PROFILE/halt.json" ]] || { echo "$(date -u +%FT%TZ) halt.json present in $PROFILE; no start" >&2; exit 0; }
for dependency in Xvfb xvfb-run xauth; do
  command -v "$dependency" >/dev/null || { echo "Fomo display dependency unavailable: $dependency" >&2; exit 2; }
done
XVFB_RUN="$(command -v xvfb-run)"
export SNIPER_STATE_ROOT="$STATE_ROOT"
source "$REPO_ROOT/scripts/lib/desk_proc_match.sh"

profile_matches() {
  "$PY" - "$1" "$PROFILE" <<'PY'
from pathlib import Path
import os,sys
try:
    proc=Path(sys.argv[1])
    raw=(proc/'cmdline').read_bytes()
    env=(proc/'environ').read_bytes()
    if not raw.endswith(b'\0') or not env.endswith(b'\0'):
        raise ValueError('partial process record')
    args=[os.fsdecode(arg) for arg in raw[:-1].split(b'\0')]
    home=None
    for item in env[:-1].split(b'\0'):
        if item.startswith(b'HOME='):
            home=os.fsdecode(item[5:])
    value=None
    i=2
    while i<len(args):
        arg=args[i]
        if arg=='--profile-dir':
            i+=1
            if i>=len(args) or args[i].startswith('--'):
                raise ValueError('missing profile')
            value=args[i]
        elif arg.startswith('--profile-dir='):
            value=arg.partition('=')[2]
        elif arg=='--' or (arg.startswith('--') and '--profile-dir'.startswith(arg.partition('=')[0])):
            raise ValueError('unproved abbreviated profile')
        i+=1
    if value is None:
        if not home:
            raise ValueError('unknown default home')
        value=home+'/.local/share/sniper/fomo_intake_profile'
    if value=='~' or value.startswith('~/'):
        if not home:
            raise ValueError('unknown home')
        value=home+value[1:]
    elif value.startswith('~'):
        raise ValueError('unproved named-user home')
    path=Path(value)
    if not path.is_absolute():
        path=(proc/'cwd').resolve(strict=True)/path
    match=path.resolve()==Path(sys.argv[2])
except (OSError,ValueError,RuntimeError):
    match=False
raise SystemExit(0 if match else 1)
PY
}

candidate() {
  local proc="$PROC_ROOT/$1" arg root
  [[ -r "$proc/cmdline" ]] || return 1
  local argv=()
  while IFS= read -r -d '' arg; do argv+=("$arg"); done < "$proc/cmdline" 2>/dev/null || true
  local arg0="${argv[0]:-}" arg1="${argv[1]:-}"
  [[ "${arg0##*/}" == python* && "${arg1##*/}" == "${WORKER##*/}" ]] || return 1
  root="$(set -o pipefail; desk_proc_state_root "$proc")" || return 1
  [[ "$(realpath -m -- "$root")" == "$STATE_ROOT" ]] || return 1
  profile_matches "$proc" || return 1
  CANDIDATE_SCRIPT="$arg1"
}

launcher_running() {
  local proc arg root index arg0
  local expected=("$XVFB_RUN" -a -s '-screen 0 1280x1024x24 -nolisten tcp' "$PY" "$WORKER"
                  --profile-dir "$PROFILE" --headed --loop)
  for proc in "$PROC_ROOT"/[0-9]*; do
    [[ -r "$proc/cmdline" ]] || continue
    local argv=()
    while IFS= read -r -d '' arg; do argv+=("$arg"); done < "$proc/cmdline" 2>/dev/null || true
    arg0="${argv[0]:-}"
    [[ "${arg0##*/}" == sh || "${arg0##*/}" == dash || "${arg0##*/}" == bash || "${arg0##*/}" == python* ]] || continue
    [[ "${#argv[@]}" -eq "$((${#expected[@]}+1))" ]] || continue
    for ((index=0; index<${#expected[@]}; index++)); do
      [[ "${argv[index+1]}" == "${expected[index]}" ]] || break
    done
    [[ "$index" -eq "${#expected[@]}" ]] || continue
    root="$(set -o pipefail; desk_proc_state_root "$proc")" || continue
    [[ "$(realpath -m -- "$root")" == "$STATE_ROOT" ]] || continue
    profile_matches "$proc" && return 0
  done
  return 1
}

scan() {
  local proc pid
  ALIVE=0
  SUPERSEDED=()
  for proc in "$PROC_ROOT"/[0-9]*; do
    pid="${proc##*/}"
    candidate "$pid" || continue
    if [[ "$CANDIDATE_SCRIPT" == "$WORKER" ]]; then
      ALIVE=1
    else
      SUPERSEDED+=("$pid")
    fi
  done
}

scan
[[ "$ALIVE" -eq 0 ]] || exit 0
# xvfb-run can be alive before its Python child exists; bind that launch instead of racing it.
launcher_running && exit 0
for pid in "${SUPERSEDED[@]}"; do
  candidate "$pid" || continue
  [[ "$CANDIDATE_SCRIPT" != "$WORKER" ]] || continue
  echo "$(date -u +%FT%TZ) stopping superseded Fomo collector pid=$pid" >&2
  desk_proc_term "$PROC_ROOT" "$pid" || true
done
for ((attempt=0; attempt<20; attempt++)); do
  scan
  [[ "$ALIVE" -eq 0 ]] || exit 0
  [[ "${#SUPERSEDED[@]}" -gt 0 ]] || break
  sleep 0.5
done
scan
[[ "$ALIVE" -eq 0 ]] || exit 0
[[ "${#SUPERSEDED[@]}" -eq 0 ]] || { echo 'Fomo superseded collector still exiting; no replacement' >&2; exit 0; }
launcher_running && exit 0
# Native Chrome guards are refusal signals, including dangling symlinks; never stale-lock cleanup.
for guard in halt.json SingletonLock SingletonSocket; do
  [[ ! -e "$PROFILE/$guard" && ! -L "$PROFILE/$guard" ]] || { echo "$(date -u +%FT%TZ) $guard present in $PROFILE; no start" >&2; exit 0; }
done
exec 8>>"$PROFILE/collector.lock"
flock -n 8 || exit 0
flock -u 8
exec 8>&-
cd "$STATE_ROOT"
nohup "$XVFB_RUN" -a -s '-screen 0 1280x1024x24 -nolisten tcp' \
  "$PY" "$WORKER" --profile-dir "$PROFILE" --headed --loop \
  </dev/null >>"$STATE_ROOT/logs/memecoin_desk_fomo_intake.log" 2>&1 9>&- &
printf '%s\n' "$!" >"$DATA/launcher.pid"
