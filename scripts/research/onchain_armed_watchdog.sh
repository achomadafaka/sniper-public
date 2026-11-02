#!/bin/bash
# The Solana detector's roster-drift backstop. Since 2026-09-28 the detector itself is launched and
# kept alive, release-pinned, by memecoin_watchdog.sh (ensure_onchain_solana_watcher); this script
# only kills a watcher whose subscriptions lag its roster, and that supervisor relaunches it.
REPO="/home/bigpapi/Desktop/Sniper"
# THE LAUNCH MOVED TO THE PINNED RELEASE (2026-09-28, Alpha). memecoin_watchdog.sh's
# ensure_onchain_solana_watcher now starts this watcher from .runtime/memecoin/releases/<sha>, the
# way it has run the EVM watchers since 2026-09-06, and stops a source-tree copy beside it. Two
# supervisors produced two watchers there; this file therefore keeps only the drift kill below.
# THE EVM WATCHER IS NOT LAUNCHED HERE ANY MORE (2026-09-06), and this comment is the answer to
# "why does the Solana watcher have a research supervisor and the EVM one does not".
#
# `memecoin_watchdog.sh` owns the EVM watcher and owns it RELEASE-PINNED. This block owned it too,
# from the SOURCE TREE with a relative path, and the two supervisors produced a broken state that
# neither could see: the guard here matches `pgrep -f "...--duration 86400"`, which normally finds
# the pinned process and skips -- but during a window when nothing was running it launched an
# UNPINNED one, and `memecoin_watchdog.sh` then correctly refused to count a source-tree watcher as
# alive and launched the pinned one beside it. Two watchers, one heartbeat file, alternating writes:
# the push process wrote `coverage` and the poll process overwrote it with a beat that has no such
# key, so the health metric read None every few seconds and its watchdog kill could never fire.
#
# One supervisor per process. This file keeps the Solana watcher, which has no pinned owner.
#
# It is run by cron FROM THE SOURCE TREE, so this change takes effect at commit with no deploy.

# ROSTER DRIFT: A NEWLY CONFIRMED ADDRESS IS DARK UNTIL THE NEXT RESTART (2026-09-23).
# `load_watchlist()` runs ONCE inside `run()`, outside the reconnect loop, and the watcher starts
# with `--duration 86400`. So an address confirmed one minute after a start waits up to 24 hours
# to reach the socket. Measured: 18 addresses became confirmed at 2026-09-23T08:01:42Z and the
# watcher carried them only from the 09:42:53Z start, 1 h 41 min later, and only because a restart
# happened to follow. Before that day the watcher held 2 subscriptions for 33 days.
#
# The heartbeat already reports how many subscriptions the watcher holds. When the watcher's own
# desired set is LARGER than that for DRIFT_GRACE_S, kill the watcher BY PID and let the block
# above relaunch it on the next minute with the new list. The kill is all that is needed. Since
# 2026-09-27 the watcher refreshes in process, so this is the backstop, not the mechanism.
#
# THREE GUARDS, because a restart loop watches nobody at all:
#   1. FAIL CLOSED. No process, no heartbeat number, no count: do nothing.
#   2. NEVER ON A NEGATIVE DIFFERENCE. Fewer confirmed than subscribed means an address was
#      retired, and a watcher holding a dead subscription costs nothing.
#   3. TEN MINUTES OLD. A watcher that has not finished subscribing reports a low `subs` by
#      design, and a kill for that reason never ends.
BEAT="$REPO/data/onchain_watch.heartbeat"
PY="/home/bigpapi/.pyenv/versions/3.13.0/bin/python3"
DRIFT_MIN_AGE_S="${ONCHAIN_DRIFT_MIN_AGE_S:-600}"
watcher_pid_age() {
  # ANCHORED ON ARGUMENT POSITION. A bare substring match over `ps` output is satisfied by this
  # script, by the awk that reads it, and by any editor with the file open. Fields 4..6 make the
  # script the INTERPRETER'S argument, which no `awk` or `bash -c` line can collide with.
  # EXACTLY ONE (2026-10-07). Two are a release handover (memecoin_watchdog.sh keeps the old one until
  # the new one acks every owner); both write the one heartbeat, so a drift read would pair the new
  # one's partial `subs` with the old pid and kill the only subscribed socket. Two: no candidate.
  ps -eo pid=,etimes=,args= \
    | awk '$4 ~ /onchain_wallet_watch\.py$/ && $5 == "--duration" && $6 == "86400" {n++; row = $1 " " $2}
           END {if (n == 1) print row}'
}
heartbeat_subs() {
  sed -n 's/.*"subs": *\([0-9][0-9]*\).*/\1/p' "$BEAT" 2>/dev/null | head -1
}
desired_addresses() {
  # THE WATCHER'S OWN ANSWER (2026-09-27, registry step 3). The watcher subscribes the registry's
  # VERIFIED addresses of ARMED Solana profiles, capped, and refreshes them in process every 60 s.
  # Counting the legacy confirmed rows here (24 that night) against an armed-only 15 would restart
  # it every 12 minutes forever, so the count is the watcher's own function, not a second query.
  # THE LEDGER PATH IS ABSOLUTE (2026-09-26): cron runs this from $HOME, where an empty 0-byte
  # sniper_tracker.db exists. A failed read prints nothing, and nothing is a fail-closed no-op.
  # The PINNED script's own answer, the same code the running watcher loads.
  REL="$REPO/.runtime/memecoin/current"
  PYTHONPATH="$REL" SNIPER_STATE_ROOT="$REPO" "$PY" "$REL/scripts/research/onchain_wallet_watch.py" \
    --desired-count --db "$REPO/sniper_tracker.db" 2>/dev/null
}
now_s() { date +%s; }
# DRIFT MUST PERSIST (2026-09-27). The watcher now closes a drift itself within one 60 s refresh
# plus the acks, so a single tick that lands inside that window (a roster restore arming 30
# wallets at once) is not a failure. Kill only when the SAME watcher process has shown drift for
# DRIFT_GRACE_S: that is the backstop for a refresh that stopped or subscriptions that keep failing.
DRIFT_GRACE_S="${ONCHAIN_DRIFT_GRACE_S:-180}"
roster_drift_restart() {
  set -- $(watcher_pid_age)
  pid="${1:-}"
  age="${2:-}"
  [ -n "$pid" ] && [ -n "$age" ] || return 0
  [ "$age" -ge "$DRIFT_MIN_AGE_S" ] 2>/dev/null || return 0
  subs="$(heartbeat_subs)"
  desired="$(desired_addresses)"
  case "$subs" in ''|*[!0-9]*) return 0 ;; esac
  case "$desired" in ''|*[!0-9]*) return 0 ;; esac
  since_file="$REPO/data/onchain_watch.drift_since"
  if [ "$desired" -le "$subs" ]; then
    rm -f "$since_file"
    return 0
  fi
  now="$(now_s)"
  # "<pid> <first seen>": a drift recorded against another process starts over.
  set -- $(cat "$since_file" 2>/dev/null)
  if [ "${1:-}" != "$pid" ] || case "${2:-}" in ''|*[!0-9]*) true ;; *) false ;; esac; then
    echo "$pid $now" > "$since_file"
    return 0
  fi
  [ $((now - $2)) -ge "$DRIFT_GRACE_S" ] || return 0
  echo "$(date -u +%FT%TZ) roster drift: subs=$subs desired=$desired for $((now - $2))s, restarting" \
    >> "$REPO/data/onchain_armed.log"
  rm -f "$since_file"
  kill "$pid" 2>/dev/null
}
roster_drift_restart
