#!/usr/bin/env bash
# Watchdog for the refused-mint forward capture loop. Cron calls this once a minute; the flock
# permits exactly one loop process. DEFAULT OFF — it exits unless MEMECOIN_CAPTURE_ENABLED=1.
#
# This process measures. It never signs, submits, or touches a wallet key. The loop it starts
# reads the funded ledger READ-ONLY and writes only its own database.
#
# IT RESTARTS ON A STALE HEARTBEAT, NOT ONLY ON AN ABSENT PROCESS. A restart-if-gone watchdog
# cannot see a wedge, and the capture loop is silent by design — at roughly four capturable
# refusals an hour, "no log output" is the normal healthy case, so a hung poll looks exactly like
# a quiet one. The loop stamps data/memecoin_refusal_capture.heartbeat at the top of every pass;
# this kills and relaunches when that stops advancing. The clock is the WATCHDOG'S, never the
# loop's own counter: a liveness signal written by the thing it monitors is blind to that thing
# dying (finding: liveness-counters-need-external-clocks, hit three times).
set -u

ROOT=/home/bigpapi/Desktop/Sniper
LOG="$ROOT/logs/memecoin_refusal_capture.log"
LOCK="$ROOT/data/memecoin_refusal_capture.worker.lock"
BEAT="$ROOT/data/memecoin_refusal_capture.heartbeat"
PY=/home/bigpapi/.pyenv/versions/3.13.0/bin/python3

# RELEASE-PINNED, AND THAT IS THE WHOLE POINT OF THIS BLOCK (2026-09-18).
#
# This launched `$ROOT/scripts/...` — the MUTABLE checkout — and restarted only on a stale
# heartbeat. A live loop with a fresh heartbeat therefore ran its ORIGINAL import of
# `memecoin_refusal_capture` forever: it started 2026-09-17T08:43Z, two refusal families shipped
# after it, and it captured 0 of 34 `drawdown_pause` and 0 of 1 `m5_volume_under_abs_floor` while
# reporting healthy. A deploy cannot update a process that already imported its code, and nothing
# here ever noticed the code had moved.
#
# The funded watchdog solved this in 2026-09-06 and its reasoning transfers exactly:
#   * LIVENESS is release-INDEPENDENT, matched on the releases DIRECTORY plus the script. A loop
#     started by the previous release must still be recognised, or every deploy launches another
#     one. The 2026-09-02 leak was seven concurrent watchers, one per deploy.
#   * UPGRADE is a separate question: a loop whose command line does not name the CURRENT release
#     is stopped, and the launch below starts the current one in the same pass.
#   * LAUNCH is release-PINNED, so only the deployed code ever runs.
#   * `ps -eww -o args=` and never `pgrep -f`. The `-ww` is load-bearing: plain `ps` truncates to
#     the terminal width and this needle sits well past column 100, which made the same check
#     answer differently on consecutive invocations while the funded fix was being verified.
CURRENT="$(readlink -f "$ROOT/.runtime/memecoin/current" 2>/dev/null || true)"
SCRIPT_REL="scripts/memecoin_refusal_capture_loop.py"
MATCH="$CURRENT/$SCRIPT_REL"

# No deployed release yet, or a release without this script: stay on the checkout rather than stop
# capturing. Degrading to the old behaviour is better than a silent outage, and it is logged.
UNPINNED_REASON=""
if [ -z "$CURRENT" ]; then
    UNPINNED_REASON="no current release link"
elif [ ! -f "$MATCH" ]; then
    UNPINNED_REASON="release $(basename "$CURRENT") has no $SCRIPT_REL"
fi
if [ -n "$UNPINNED_REASON" ]; then
    MATCH="$ROOT/$SCRIPT_REL"
    CURRENT=""
fi

# Generous against MEMECOIN_CAPTURE_POLL_S. A margin that is tight against the poll turns one slow
# provider call into a kill, and killing a healthy loop costs more than a late restart.
STALE_S="${MEMECOIN_CAPTURE_HEARTBEAT_MAX_AGE_S:-300}"

cd "$ROOT" || exit 1
mkdir -p "$ROOT/logs" "$ROOT/data"

log() { printf '%s watchdog %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }

# Said once a minute is noise; said never is a silent downgrade. The heartbeat gate below means
# this only reaches the log on a pass that actually starts something.
[ -z "$UNPINNED_REASON" ] || log "falling back to the checkout: $UNPINNED_REASON"

# Read the flag from .env without exporting the whole file into this shell.
ENABLED=$(grep -E '^MEMECOIN_CAPTURE_ENABLED=' "$ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ')
[ "${ENABLED:-0}" = "1" ] || exit 0

# --- wedge check -------------------------------------------------------------------------------
# ANCHORED ON ARGUMENT POSITION, never a bare substring of the whole command line (Codex review,
# 2026-09-20). `grep -F "$SCRIPT_REL"` over raw `ps` output matches ANY host process whose command
# line contains that relative path anywhere -- including a copy of this same script run from a
# completely different state root. The script argument (the last field of the invocation this
# watchdog itself uses: `"$PY" -u "$MATCH"`) must instead be exactly the checkout fallback path or
# a release under THIS root's own .runtime tree.
#
# A MATCHING PATH IS NOT OWNERSHIP. Two processes can share a release directory while pointed at
# different state roots -- SNIPER_STATE_ROOT is an independent env var set by whoever launched
# them, not implied by which files they run -- so ownership is verified separately by reading each
# candidate's OWN environment before it is ever eligible for a signal.
#
# `ps -eww -o pid=,args=`, parsed field-by-field, and never `pgrep -f`: pgrep matches every command
# line, so a cron wrapper or an editor holding the path reads as the process itself (finding:
# runtime-repoint-hazards-2026-08-03). Any release satisfies liveness; the source tree does too,
# because an operator may be running one by hand and this watchdog must not launch a second
# alongside it.
owned_loop_pids() {
    ps -eww -o pid=,args= | while read -r pid rest; do
        script="${rest##* }"
        case "$script" in
            "$ROOT/$SCRIPT_REL") ;;
            "$ROOT"/.runtime/memecoin/releases/*/"$SCRIPT_REL") ;;
            *) continue ;;
        esac
        env_root=$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^SNIPER_STATE_ROOT=//p')
        [ "$env_root" = "$ROOT" ] || continue
        printf '%s %s\n' "$pid" "$script"
    done
}
OWNED="$(owned_loop_pids)"
# `grep -c .` and never `wc -l`: $OWNED is a captured (trailing-newline-stripped) variable, so
# `printf '%s\n' ""` on an empty result still emits one blank line -- `wc -l` would count that as
# one running loop, `grep -c .` (a blank line matches no character) correctly counts zero.
RUNNING=$(printf '%s\n' "$OWNED" | grep -c . || true)

# --- upgrade: a loop that is not the current release must go ------------------------------------
# Only when a current release exists. `grep -cF -- "$CURRENT/"` counts the ones that ARE current;
# anything else running is stale or unpinned, and a deploy is exactly when that must be corrected.
if [ -n "$CURRENT" ] && [ "$RUNNING" -gt 0 ]; then
    CURRENT_N=$(printf '%s\n' "$OWNED" | grep -cF -- "$CURRENT/" || true)
    if [ "$CURRENT_N" -lt "$RUNNING" ]; then
        log "loop running from a release that is not $(basename "$CURRENT") — replacing it"
        OWNED_PIDS="$(printf '%s\n' "$OWNED" | awk 'NF {print $1}')"
        kill -TERM $OWNED_PIDS 2>/dev/null
        sleep 5
        kill -KILL $OWNED_PIDS 2>/dev/null
        RUNNING=0
    fi
fi
if [ "$RUNNING" -gt 0 ] && [ -f "$BEAT" ]; then
    AGE=$(( $(date -u +%s) - $(stat -c %Y "$BEAT" 2>/dev/null || echo 0) ))
    if [ "$AGE" -gt "$STALE_S" ]; then
        log "heartbeat ${AGE}s stale (max ${STALE_S}s) — killing the wedged loop"
        OWNED_PIDS="$(printf '%s\n' "$OWNED" | awk 'NF {print $1}')"
        kill -TERM $OWNED_PIDS 2>/dev/null
        sleep 5
        kill -KILL $OWNED_PIDS 2>/dev/null
        RUNNING=0
    fi
fi

# --- start -------------------------------------------------------------------------------------
# Single-instance is enforced by the lock ALONE. A pgrep guard was removed from the sibling
# watchdog because its false positive made the watchdog decline to start the worker forever,
# silently. flock holds for exactly the lifetime of the process that owns it.
#
# The watchdog must NOT flock this file itself: the loop acquires the same path internally, so a
# launcher-held flock is inherited by the child's environment and the loop reads its own parent
# as "another instance" and exits — every launch self-blocks (live, 2026-08-28 17:16, twice).
# The loop's internal acquire_run_lock is the single point of enforcement, as the comment above
# always claimed; the launcher just starts it.
# LOGGED ONLY WHEN A START ACTUALLY SURVIVES. Cron runs this every minute, so on a healthy loop
# every one of those launches exits immediately against the loop's internal lock — an unconditional
# line here is ~1,440 entries a day reporting a start that never happened, on the one channel whose
# job is to make a real event visible.
#
# `... &` cannot tell us: the shell's status is "did the fork succeed", which is always true. So
# note the heartbeat's mtime, launch, and let the new loop write one. Only a launch that moves a
# heartbeat that was ABSENT OR STALE is a start; a launch that loses the lock leaves it untouched,
# because the holder advances it on its own clock either way.
BEFORE=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
NOW_S=$(date -u +%s)
WAS_DEAD=$(( BEFORE == 0 || NOW_S - BEFORE > STALE_S ))
# PYTHONPATH and the state root are explicit: the release checkout carries the code and the
# state root carries `.env`, the ledger and this loop's own database.
setsid env PYTHONPATH="${CURRENT:-$ROOT}" SNIPER_STATE_ROOT="$ROOT" \
    "$PY" -u "$MATCH" >> "$LOG" 2>&1 &
[ "$WAS_DEAD" -eq 1 ] || exit 0
sleep 5
AFTER=$(stat -c %Y "$BEAT" 2>/dev/null || echo 0)
if [ "$AFTER" -gt "$BEFORE" ]; then
    log "started — heartbeat began advancing"
else
    log "start FAILED — no heartbeat within 5s of launch"
fi
exit 0
