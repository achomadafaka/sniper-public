#!/usr/bin/env bash
# Pinned memecoin runner watchdog. Never falls back to the mutable source checkout.
set -u

SCRIPT="$(readlink -f "$0")"

# ---- WATCHDOG SELF-SERIALIZATION (2026-08-26) -------------------------------------------------
# BEGIN watchdog-lock
# Cron runs this every minute and a deploy invokes it directly. On 2026-08-25 those two fired 2s
# apart, BOTH read "not running", both launched, both failed the runner's internal lock, and the
# deploy rolled back. Loud and self-correcting, but it cost a deploy.
#
# -n  : never wait. A losing invocation must not queue -- cron would stack a minute's worth.
# -E 75: a conflict exits 75 (EX_TEMPFAIL) so THIS preamble can tell a lost race from the run's
#       own result; the loser then exits 0 to cron. The loser did nothing wrong and must not look
#       like a failure -- but it must not be invisible either (2026-09-24: a 4-minute release
#       verification held the lock through a network outage and every cron minute in between left
#       no trace, which read as "the watchdog logged nothing for 36 minutes"). It appends one line,
#       at most once per 5 minutes, and never touches the lock. The run itself never exits 75.
# -o  : CLOSE THE DESCRIPTOR BEFORE RUNNING THE COMMAND. This is the whole trap
#       (finding-watchdog-lock-inherited-by-worker-2026-08-12): a bare `exec 9>lock` leaks fd 9
#       into the launched runner, the runner then holds the WATCHDOG's lock for its entire life,
#       and every later watchdog invocation loses the race forever -- the watchdog goes inert
#       while looking healthy. -o is preferred over `9>&-` on the launch line because a later edit
#       cannot forget it.
# The flock process itself stays as our parent and holds the lock for this script's lifetime; only
# the descendants are denied the descriptor.
#
# The lock is the WATCHDOG's own, never the runner's `sol-<pubkey>` execution lock and never
# MEMECOIN_RUN_LOCK. Serializing watchdog invocations must not touch either.
WATCHDOG_LOCK="${MEMECOIN_WATCHDOG_LOCK:-${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}/data/memecoin_watchdog.lock}"
if [ -z "${MEMECOIN_WATCHDOG_LOCK_HELD:-}" ] && command -v flock >/dev/null 2>&1; then
  mkdir -p "$(dirname "$WATCHDOG_LOCK")"
  export MEMECOIN_WATCHDOG_LOCK_HELD=1
  flock -n -E 75 -o "$WATCHDOG_LOCK" "$SCRIPT" "$@"
  _flock_rc=$?
  if [ "$_flock_rc" -eq 75 ]; then
    _skip_stamp="$WATCHDOG_LOCK.skip-logged"
    if [ -z "$(find "$_skip_stamp" -mmin -5 2>/dev/null)" ]; then
      _skip_log="${MEMECOIN_WATCHDOG_LOG:-${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}/logs/memecoin_watchdog.log}"
      mkdir -p "$(dirname "$_skip_log")"
      printf '%s %s\n' "$(date -u +%FT%TZ)" \
        "watchdog run skipped: a previous run still holds the lock" >> "$_skip_log"
      touch "$_skip_stamp"
    fi
    exit 0
  fi
  exit "$_flock_rc"
fi
# END watchdog-lock

# THE DEPLOY'S VERIFY, NAMED EXPLICITLY (2026-09-27). deploy_memecoin_runtime.sh sets this, to the
# SHA it has just verified, on its one post-switch call of this script, and nothing else ever sets
# it. Read once and removed from the environment at once, so no child of this script (the runner,
# the watchers, the verifier) inherits it. See the release verification below.
_DEPLOY_VERIFIED_SHA="${MEMECOIN_WATCHDOG_RELEASE_VERIFIED:-}"
unset MEMECOIN_WATCHDOG_RELEASE_VERIFIED

ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd -P)"
STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
MANIFEST="${MEMECOIN_DEPLOY_MANIFEST:-$STATE_ROOT/data/memecoin_deploy.json}"
PIDFILE="${MEMECOIN_PIDFILE:-$STATE_ROOT/data/memecoin_runner.pid}"
LOGFILE="${MEMECOIN_RUNNER_LOG:-$STATE_ROOT/logs/memecoin_runner.log}"
WATCHDOG_LOG="${MEMECOIN_WATCHDOG_LOG:-$STATE_ROOT/logs/memecoin_watchdog.log}"
MATCH="$ROOT/scripts/memecoin_runner_loop.py"
STARTUP_TIMEOUT_S="${MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S:-90}"
case "$STARTUP_TIMEOUT_S" in
  ''|*[!0-9]*) echo "invalid MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S" >&2; exit 1 ;;
esac
[ "$STARTUP_TIMEOUT_S" -ge 1 ] \
  || { echo "MEMECOIN_WATCHDOG_STARTUP_TIMEOUT_S must be >= 1" >&2; exit 1; }

export SNIPER_STATE_ROOT="$STATE_ROOT"
export DB_PATH="${DB_PATH:-$STATE_ROOT/sniper_tracker.db}"
export MEMECOIN_RUN_LOCK="${MEMECOIN_RUN_LOCK:-$STATE_ROOT/data/memecoin_runner.lock}"
export MEMECOIN_HEARTBEAT_PATH="${MEMECOIN_HEARTBEAT_PATH:-$STATE_ROOT/data/memecoin_runner.heartbeat}"
export MEMECOIN_FEED_SNAPSHOT_PATH="${MEMECOIN_FEED_SNAPSHOT_PATH:-$STATE_ROOT/data/memecoin_feed.json}"
export FOMO_TOKEN_STORE="${FOMO_TOKEN_STORE:-$STATE_ROOT/.fomo_tokens.json}"
export MEMECOIN_KILL_SWITCH_FILE="${MEMECOIN_KILL_SWITCH_FILE:-$STATE_ROOT/data/memecoin_kill_switch}"
export SNIPER_CLOCK_OFFSET_FILE="${SNIPER_CLOCK_OFFSET_FILE:-$STATE_ROOT/data/clock_offset.json}"

mkdir -p "$STATE_ROOT/logs" "$STATE_ROOT/data"
log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$WATCHDOG_LOG"; }

# Returns 0 only when the page was DELIVERED (2026-09-29). The primary sender imports the release,
# and the release may be the thing that failed, so a stdlib-only sender that reads the same state
# env files is the fallback. Callers that ignore the status keep the old fire-and-forget behaviour.
alert() {
  PYTHONPATH="$ROOT" "$PY" -c \
    "import sys; from sniper.util.envfiles import load_env_files; load_env_files('$STATE_ROOT'); from sniper.services.memecoin_telegram import send_telegram; sys.exit(0 if send_telegram(sys.argv[1])[0] else 3)" \
    "$1" >/dev/null 2>&1 && return 0
  alert_stdlib "$1"
}

alert_stdlib() {
  "$PY" - "$STATE_ROOT" "$1" >/dev/null 2>&1 <<'PY'
import json, os, sys, urllib.parse, urllib.request
root, text = sys.argv[1], sys.argv[2]
env = {}
for name in (".env", ".fomo.env"):
    try:
        with open(os.path.join(root, name)) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except OSError:
        pass
get = lambda key: os.environ.get(key) or env.get(key, "")
token = get("MEMECOIN_TG_BOT_TOKEN") or get("TELEGRAM_BOT_TOKEN")
chat = (get("MEMECOIN_TG_CHAT_ID") or get("TELEGRAM_CHAT_ID")).split(",")[0].strip()
if not token or not chat:
    sys.exit(4)
body = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
with urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % token, body, timeout=10) as r:
    sys.exit(0 if json.load(r).get("ok") else 5)
PY
}

# MATCH RELEASE-INDEPENDENT BUT RELEASE-REQUIRED, LAUNCH RELEASE-PINNED.
#
# `$ROOT` is the RELEASE directory, so a match built from it carries the release SHA. After a deploy
# the pattern no longer matched the watcher started by the PREVIOUS release, the check found
# nothing, and a SECOND watcher launched while the first kept running. On 2026-09-02 that left SEVEN
# concurrent watchers, one per deploy that day, the oldest running since 05:21.
#
# The naive fix -- match the bare script path -- trades that leak for a worse property, which is why
# the guard test asserted the release-pinned form in the first place. A watcher started by hand from
# the SOURCE tree would satisfy a relative match, and the watchdog would then decline to launch the
# pinned one, leaving the funded host supervised by an unpinned process. That is the same class as
# the "foreign runner" the runner match exists to catch.
#
# So match on the RELEASES DIRECTORY plus the script and its argument: any release satisfies it, the
# source tree does not. Release-independent across deploys, release-REQUIRED against a stray. The
# launch stays `$ROOT`, so only the deployed release is ever started.
#
# `ps -eww -o args=` rather than `pgrep -f`, and the `-ww` IS LOAD-BEARING: bare `ps -eo args`
# truncates each line to the terminal width, this needle sits past column 100 of a 208-character
# command line, and the same check returned "no match" then "1 match" on two consecutive invocations
# while this fix was being verified. A width-dependent matcher does not leak reliably; it leaks
# SOMETIMES, which is harder to diagnose than always.
#
# ANCHORED ON ARGUMENT POSITION, not on a bare substring of the whole command line. A substring
# match over `ps` output is satisfied by ANY process whose command line merely mentions the path --
# an editor, a monitoring one-liner, a `bash -c` wrapper. While verifying this fix, three of my own
# shell invocations matched, and they were excluded only because they happened to contain the word
# "grep". A liveness check that a stray command line can satisfy would DECLINE to launch the real
# watcher, which is the failure this whole change exists to prevent, arriving through a new door.
# Taking fields 2..4 makes the script the INTERPRETER'S ARGUMENT: a `bash -c` line has "-c" in
# field 2 and cannot collide.
#
# ONE WATCHER PER CHAIN (2026-09-06). A chain is launched only when it has a WebSocket endpoint --
# the socket is the whole reason this lane beats the feed, and a chain without one would add
# range-`eth_getLogs` load to a public endpoint for coverage the feed already provides. Base
# qualifies the moment MEMECOIN_EVM_WS_URL_8453 is set.
#
# 4663 WAS RETIRED 2026-09-23 AND REVIVED 2026-09-28 (operator order: the Robinhood fast lane).
# See the note at the funded loop below.
#
# THE 4663 BRANCHES IN THE FOUR FUNCTIONS BELOW ARE LOAD-BEARING. 4663 carries no `--chain`
# argument, because the liveness match is a string match on the command line and the script's
# default chain is 4663. A 4663 command line is therefore a PREFIX of every other chain's command
# line, and every one of the four functions must drop `--chain` lines when it matches for 4663.
ONCHAIN_EVM_SCRIPT="scripts/research/onchain_evm_wallet_watch.py"
onchain_evm_chains() {
  PYTHONPATH="$ROOT" "$PY" -c "
from sniper.util.envfiles import load_env_files
load_env_files('$STATE_ROOT')
from sniper.settings import memecoin as cfg
for c in sorted(cfg.MEMECOIN_EVM_ALLOWLIST):
    if cfg.memecoin_evm_ws_url(c):
        print(c)
" 2>/dev/null
}
# OBSERVE-ONLY CHAINS (2026-09-16, Arc build). A SEPARATE set from MEMECOIN_EVM_ALLOWLIST on
# purpose: pool-key discovery must be able to start for a chain before the (later, separate)
# decision to fund and arm it. The eligible/missing split lets the caller log a chain that was
# configured but has no WS URL, instead of it just quietly never launching.
onchain_evm_observe_chains() {
  PYTHONPATH="$ROOT" "$PY" -c "
from sniper.util.envfiles import load_env_files
load_env_files('$STATE_ROOT')
from sniper.settings import memecoin as cfg
for c in sorted(cfg.MEMECOIN_EVM_OBSERVE_CHAINS):
    if cfg.memecoin_evm_ws_url(c):
        print(c)
" 2>/dev/null
}
onchain_evm_observe_chains_missing_ws() {
  PYTHONPATH="$ROOT" "$PY" -c "
from sniper.util.envfiles import load_env_files
load_env_files('$STATE_ROOT')
from sniper.settings import memecoin as cfg
for c in sorted(cfg.MEMECOIN_EVM_OBSERVE_CHAINS):
    if not cfg.memecoin_evm_ws_url(c):
        print(c)
" 2>/dev/null
}
ensure_onchain_evm_watcher() {
  chain="$1"
  extra="${2:-}"
  if [ "$chain" = "4663" ]; then
    args="$ONCHAIN_EVM_SCRIPT --duration 86400"
    log_file="$STATE_ROOT/logs/onchain_evm_armed.log"
    set -- --duration 86400
  else
    args="$ONCHAIN_EVM_SCRIPT --duration 86400 --chain $chain"
    log_file="$STATE_ROOT/logs/onchain_evm_armed.$chain.log"
    set -- --duration 86400 --chain "$chain"
  fi
  # OBSERVE-ONLY CHAINS carry one more argument (2026-09-16, Arc build). `extra` is empty for
  # every existing call site, so this is a no-op for 4663 and every funded EVM chain.
  if [ -n "$extra" ]; then
    args="$args $extra"
    log_file="${log_file%.log}.${extra#--}.log"
    set -- "$@" "$extra"
  fi
  # The 4663 needle is a PREFIX of every other chain's command line, so a bare substring match
  # would let a running 8453 watcher satisfy the 4663 check and leave 4663 unsupervised. 4663 is
  # therefore matched only against lines that carry no `--chain` at all.
  #
  # THE FIELD WINDOW COVERS THE LONGEST ARGS STRING THIS FUNCTION BUILDS. `--observe-only` is a
  # 7th field (script, --duration, 86400, --chain, <chain>, --observe-only); narrower than that
  # and an observe-only watcher's own liveness check never matches its own running process, so
  # it relaunches every tick instead of recognising itself.
  matches=$(ps -eww -o args= | awk '{print $2" "$3" "$4" "$5" "$6" "$7}' \
     | grep -F -- "/.runtime/memecoin/releases/" | grep -F -- "$args" || true)
  if [ "$chain" = "4663" ]; then
    matches=$(printf '%s\n' "$matches" | grep -v -- "--chain" || true)
  fi
  if [ -n "$matches" ]; then
    return 0
  fi
  setsid nohup env PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB_PATH" \
    FOMO_TOKEN_STORE="$FOMO_TOKEN_STORE" "$PY" "$ROOT/$ONCHAIN_EVM_SCRIPT" "$@" \
    >> "$log_file" 2>&1 &
  log "launched EVM on-chain watcher chain=$chain pid $!"
}
# KILL A WATCHER THAT IS DELIVERING A FRACTION OF THE CHAIN (2026-09-06). Measured on 4663 that
# day: a fresh socket delivered 93% of blocks, decayed to 46% within about 35 minutes, and after a
# restart to 30% within ten -- with RECONNECTS AT ZERO throughout. The connection reported itself
# perfectly healthy while under a third of the chain was arriving, so nothing alarmed and the lane
# was hand-restarted twice. Hand-restarting is the thing this removes.
#
# THREE GUARDS, because a restart loop that watches nobody is worse than a degraded socket:
#   1. SUSTAINED. The watcher stamps `coverage_low_since` the first beat coverage falls below the
#      floor and clears it the moment coverage recovers OR becomes unknown, so a small sample can
#      never accumulate toward a kill. We require it to have been low continuously.
#   2. COOLDOWN. A kill marker bounds how often this may fire, whatever the heartbeat says.
#   3. UNKNOWN IS NOT UNHEALTHY. No coverage number, no kill. The absent-watcher check below is what
#      covers a process that is not running at all.
# The kill is all that is needed: `ensure_onchain_evm_watcher` relaunches on the next pass.
ONCHAIN_LOW_S="${MEMECOIN_PUSH_COVERAGE_LOW_S:-120}"
ONCHAIN_KILL_COOLDOWN_S="${MEMECOIN_PUSH_KILL_COOLDOWN_S:-900}"
# HOW LONG A WATCHER MAY WRITE NOTHING BEFORE IT COUNTS AS DEAD (2026-09-06).
#
# COVERAGE CANNOT ANSWER THIS AND MUST NOT BE ASKED TO. `coverage` is None when no heads have
# arrived, and None is UNKNOWN, never unhealthy -- so a lane delivering nothing at all is silent by
# design, indistinguishable from one that just started. Measured that day: the 4663 socket refuses
# `eth_subscribe` episodically with -32603, the refusal backoff then parks the lane on a poll
# fallback whose `eth_getLogs` cannot work on that chain (14,948 failures), and the heartbeat sat
# more than three minutes stale while the process ran and nothing said so. The lane recovers on
# retry, so this is intermittence rather than death -- but during those windows the lane IS dead.
#
# So the rule is "the heartbeat stopped advancing", which needs no sample to be meaningful and is
# the same signal the RUNNER's own `stale` reason uses. 180s is longer than the push lane's 120s
# fallback-to-poll transition, so a healthy handover cannot trip it.
ONCHAIN_BEAT_MAX_AGE_S="${MEMECOIN_WATCHER_HEARTBEAT_MAX_AGE_S:-180}"
kill_degraded_onchain_watcher() {
  chain="$1"
  extra="${2:-}"
  if [ "$chain" = "4663" ]; then
    beat="$STATE_ROOT/data/onchain_evm_watch.heartbeat"
    args="$ONCHAIN_EVM_SCRIPT --duration 86400"
  else
    beat="$STATE_ROOT/data/onchain_evm_watch.$chain.heartbeat"
    args="$ONCHAIN_EVM_SCRIPT --duration 86400 --chain $chain"
  fi
  [ -n "$extra" ] && args="$args $extra"
  [ -f "$beat" ] || return 0
  marker="$STATE_ROOT/data/onchain_evm_watch.$chain.lastkill"
  now="$(date -u +%s)"
  if [ -f "$marker" ]; then
    last="$(cat "$marker" 2>/dev/null || echo 0)"
    [ $((now - last)) -lt "$ONCHAIN_KILL_COOLDOWN_S" ] && return 0
  fi
  # TWO REASONS, ONE KILL PATH, so the cooldown and the pid matching cannot diverge between them.
  #   coverage_low  -- delivering a fraction of the chain while looking connected
  #   beat_stale    -- writing nothing at all, which coverage reports as UNKNOWN and never as sick
  reason="$(PYTHONPATH="$ROOT" "$PY" -c "
import json, sys, time
from datetime import datetime, timezone
try:
    beat = json.load(open('$beat'))
except Exception:
    print(''); sys.exit(0)          # unreadable: the absent-watcher check owns that case
now = time.time()
try:
    ts = datetime.fromisoformat(str(beat.get('ts')).replace('Z', '+00:00'))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = now - ts.timestamp()
except Exception:
    age = None
if age is not None and age >= $ONCHAIN_BEAT_MAX_AGE_S:
    print('beat_stale:%d' % age); sys.exit(0)
since = beat.get('coverage_low_since')
if since:
    low = now - float(since)
    if low >= $ONCHAIN_LOW_S:
        print('coverage_low:%d' % low)
" 2>/dev/null || echo "")"
  [ -n "$reason" ] || return 0
  low_for="${reason#*:}"
  # 4663's needle is a prefix of every other chain's line, the same collision the three functions
  # around this one filter. Unfiltered here, a stale 4663 beat killed every OTHER chain's watcher
  # (found on the 2026-09-28 revival: the 4663 beat file was 12 days old, 4663 not running).
  # Only a watcher of THIS state root's releases (Gamma H2, 2026-09-29): the beat read above is
  # $STATE_ROOT's, so a whole-script test whose tmp root holds a stale beat must not kill the live
  # watchers of the real root. See own_release_tree.
  pids="$(ps -eww -o pid=,args= | awk -v a="$args" -v c="$chain" -v t="$(own_release_tree)" \
    'index($0, a) && index($0, t) &&
     !(c == "4663" && index($0, "--chain")) {print $1}')"
  [ -n "$pids" ] || return 0
  log "on-chain watcher chain=$chain unhealthy ($reason); killing for restart"
  printf '%s\n' "$now" > "$marker"
  kill $pids 2>/dev/null || true
  alert "memecoin on-chain watcher chain=$chain restarted by watchdog ($reason)"
}

# A WATCHER FROM A PREVIOUS RELEASE SURVIVES A DEPLOY, AND THAT MADE A FIX INERT (2026-09-06).
# The liveness match above is release-INDEPENDENT on purpose -- it must recognise a watcher started
# by the previous release, or every deploy launches another one, which is the seven-watcher leak of
# 2026-09-02. The same property means an OLD watcher satisfies the check forever: after the coverage
# fix was deployed the running process was still on 015a2705, its heartbeat carried no `coverage`
# key at all, and it had to be killed by hand for the release to take effect.
#
# So liveness stays release-independent and UPGRADE is a separate question: a watcher whose command
# line names a release directory that is not THIS one is stopped, and the relaunch below starts it
# from the current release in the same pass. Matching on `$ROOT` rather than on a SHA means this
# needs no knowledge of which release is current beyond where the watchdog itself is running from.
# STOP AN UNPINNED WATCHER ONLY WHEN A PINNED ONE IS ALREADY RUNNING (2026-09-06).
#
# NARROW ON PURPOSE. That pair IS the broken state: two processes writing one heartbeat file, the
# push one carrying `coverage` and the poll one overwriting it with a beat that has no such key, so
# the metric reads None every few seconds and the kill above can never fire. Outside that state a
# source-tree watcher is somebody running one deliberately with no pinned owner, and killing it
# would be rude rather than corrective -- so this does nothing.
#
# The duplicate LAUNCHER was removed from scripts/research/onchain_armed_watchdog.sh in the same
# change; this is the backstop for anything else that starts one, not the primary fix.
# THE SOLANA ON-CHAIN WATCHER, RELEASE-PINNED (2026-09-28, Alpha). It feeds funded admission (the
# on-chain lane reads its detections) and imports sniper/ modules, yet it ran from the mutable
# checkout under scripts/research/onchain_armed_watchdog.sh. It now runs from this release, like the
# EVM watchers; that script keeps only the roster-drift kill. One supervisor per process.
#
# MATCHED ON ARGUMENT POSITION (pid, interpreter, script, --duration, 86400), so a shell, an editor
# or a grep naming the script never matches, and the EVM watcher (onchain_evm_wallet_watch.py)
# never does either. A watcher from an older release or an unpinned one is stopped only once the
# pinned one has acked every owner, so the handover overlaps rather than going dark.
#
# NO KILL BEFORE LAUNCH (operator order 2026-10-07). On every deploy this killed the older-release
# watcher, slept 1 s and launched the new one; the Solana watcher has no backfill, so a buy landing
# before the new socket subscribed was seen by neither lane (20:52:04.7Z SIGTERM, 20:52:05Z launch).
# Now the new one launches beside the old one, which is stopped on a later tick once the new pid is
# named in data/onchain_watch.ready (written when it has acked every owner). Two watchers are safe
# for the ledger: detections and sells are INSERT OR IGNORE on unique signature keys and the datagram
# wake follows only a stored row. A new watcher that dies before it acks leaves the old one running
# and is relaunched next tick.
# AT THE CAP A NEW ONE THAT ACKED NOTHING GOES (Gamma LOW-2, Alpha GO 2026-10-07): it is backing off
# (quota 429, no socket), so it is stopped and the old, acked one keeps watching; the next tick
# relaunches the new one with a fresh backoff. Never zero watchers: when the old ones are gone too,
# the new one stays. A PARTIAL new one (ready line "<pid> partial ...") holds a socket, so at the cap
# the old one goes instead: an owner refused on every try would otherwise relaunch it forever.
# THE DAILY END (Gamma LOW-1a): the watcher exits itself after --duration 86400; its same-release
# replacement launches beside it ONCHAIN_SOL_ROTATE_LEAD_S earlier and the old one exits on its own.
ONCHAIN_SOL_SCRIPT="scripts/research/onchain_wallet_watch.py"
ONCHAIN_SOL_HANDOVER_MAX_S="${ONCHAIN_SOL_HANDOVER_MAX_S:-300}"
ONCHAIN_SOL_ROTATE_LEAD_S="${ONCHAIN_SOL_ROTATE_LEAD_S:-180}"
# Shared-account migration stages runner auxiliary sockets until confirmed ownership hands over.
SOLANA_INFRA_PROFILE="$(PYTHONPATH="$ROOT" "$PY" -c \
  "import os; from sniper.util.envfiles import load_env_files; load_env_files('$STATE_ROOT'); print(os.getenv('MEMECOIN_SOLANA_INFRA_PROVIDER','legacy'))" \
  2>/dev/null || true)"
SHYFT_WATCHER_FIRST=0
[ "$SOLANA_INFRA_PROFILE" != "shyft" ] || SHYFT_WATCHER_FIRST=1
# ONLY THIS STATE ROOT'S RELEASES ARE OURS TO REPLACE (2026-09-29, Beta). "Stale" meant any path
# containing /.runtime/memecoin/releases/ that was not $ROOT. A test that runs this whole script with
# a pytest tmp dir as ROOT and STATE_ROOT therefore found the LIVE pinned watcher, called it an older
# release and killed it, logging only to the test's tmp log. The silent Solana watcher deaths of
# 2026-09-28/29 (21:15Z to 00:10Z) line up with peer runs of test_memecoin_watchdog_runner_first,
# runtime_deploy and runtime_verify. A stale process must now live under
# $STATE_ROOT/.runtime/memecoin/releases/. An unresolvable state root matches nothing.
own_release_tree() {
  _tree="$(cd "$STATE_ROOT" 2>/dev/null && pwd -P)" || _tree="/nonexistent-state-root"
  printf '%s/.runtime/memecoin/releases/\n' "$_tree"
}
solana_watchers() {
  if [ "$SHYFT_WATCHER_FIRST" -eq 1 ]; then
    PYTHONSAFEPATH=1 PYTHONPATH="$ROOT" "$PY" -c \
      "from scripts.memecoin_runtime_verify import _watcher_rows; [print(pid,path) for pid,path in _watcher_rows('$STATE_ROOT')]"
    return
  fi
  ps -eww -o pid=,args= | awk '$3 ~ /(^|\/)scripts\/research\/onchain_wallet_watch\.py$/ \
    && $4 == "--duration" && $5 == "86400" {print $1, $3}'
}
launch_solana_watcher() {
  rm -f "$STATE_ROOT/data/onchain_watch.ready"
  setsid nohup env PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB_PATH" \
    "$PY" "$ROOT/$ONCHAIN_SOL_SCRIPT" --duration 86400 >> "$STATE_ROOT/data/onchain_armed.log" 2>&1 &
  launched_pid=$!
  # The same line the old supervisor wrote: monitors wait on it, never on the argv.
  echo "$(date -u +%FT%TZ) relaunched armed watcher pid $launched_pid (pinned $ROOT)" \
    >> "$STATE_ROOT/data/onchain_armed.log"
  log "launched Solana on-chain watcher pid $launched_pid"
  if [ "$SHYFT_WATCHER_FIRST" -eq 1 ]; then
    # Capture exact process births after exec; never select the first same-release PID.
    handover="$STATE_ROOT/data/onchain_watch.handover.json"
    for attempt in $(seq 1 20); do
      if (umask 077; "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
          --state-root "$STATE_ROOT" --watcher-record-pid "$launched_pid" > "$handover.tmp"); then
        mv "$handover.tmp" "$handover"
        return 0
      fi
      sleep 0.025
    done
    rm -f "$handover.tmp"
    log "Solana Shyft watcher: replacement identity unavailable; keeping existing owners"
  fi
}
ensure_shyft_solana_watcher() {
  rows="$(solana_watchers)" || {
    log "Solana Shyft watcher: exact process discovery failed; replacement held"
    return 1
  }
  current="$(printf '%s\n' "$rows" | awk -v s="$ROOT/$ONCHAIN_SOL_SCRIPT" '$2 == s {print $1}')"
  if [ -z "$current" ]; then
    launch_solana_watcher
    return 0
  fi
  handover="$STATE_ROOT/data/onchain_watch.handover.json"
  if [ -f "$handover" ]; then
    candidate="$("$PY" -c "import json; r=json.load(open('$handover')); print(r['candidate'][0],r['candidate'][2])" 2>/dev/null || true)"
    read -r pinned birth <<< "$candidate"
    if [ -z "${pinned:-}" ] || [ -z "${birth:-}" ]; then
      log "Solana Shyft watcher: invalid handover identity; keeping existing owners"
      return 0
    fi
    if ! "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
        --state-root "$STATE_ROOT" --watcher-only --watcher-primary-only --allow-watcher-overlap \
        --watcher-candidate-pid "$pinned" --watcher-candidate-birth "$birth" >/dev/null 2>&1; then
      age="$(ps -o etimes= -p "$pinned" 2>/dev/null | tr -d ' ')"
      if { [ -z "$age" ] || [ "$age" -ge "$ONCHAIN_SOL_HANDOVER_MAX_S" ] 2>/dev/null; } && \
          "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
            --state-root "$STATE_ROOT" --abort-watcher-record "$handover" >/dev/null 2>&1; then
        rm -f "$handover" || return 1
        if [ -z "$age" ]; then
          # The survivor already published READY once; the dead candidate overwrote it. A normal
          # primary/secondary replacement writes real readiness and retires the survivor on ACK.
          log "Solana Shyft watcher: absent replacement $pinned cleared; rotating existing owners"
          launch_solana_watcher
        else
          log "Solana Shyft watcher: unready replacement $pinned stopped at deadline; keeping existing owners"
        fi
      else
        log "Solana Shyft watcher: primary $pinned ACK/head pending; keeping existing owners (${age:-?}s)"
      fi
      return 0
    fi
    # This helper checks the candidate's current primary ACK/head and captures a pidfd before
    # SIGTERM. A failed stop leaves the old identity alive, so the new watcher holds secondary.
    if "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
        --state-root "$STATE_ROOT" --retire-watcher-record "$handover" >/dev/null 2>&1; then
      log "Solana Shyft watcher: primary $pinned ready; old owners draining before secondary"
    else
      age="$(ps -o etimes= -p "$pinned" 2>/dev/null | tr -d ' ')"
      log "Solana Shyft watcher: primary $pinned not ready or old stop refused; keeping existing owners (${age:-?}s)"
      return 0
    fi
    if "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
        --state-root "$STATE_ROOT" --watcher-only --watcher-candidate-pid "$pinned" \
        --watcher-candidate-birth "$birth" >/dev/null 2>&1; then
      rm -f "$handover"
    fi
    return 0
  fi
  count="$(printf '%s\n' "$current" | wc -l)"
  total="$(printf '%s\n' "$rows" | awk 'NF {n++} END {print n+0}')"
  pinned="$(printf '%s\n' "$current" | head -1)"
  if [ "$count" -ne 1 ]; then
    log "Solana Shyft watcher: ambiguous replacement identity; keeping existing owners"
    return 0
  fi
  if [ "$total" -gt 1 ]; then
    if (umask 077; "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
        --state-root "$STATE_ROOT" --watcher-record-pid "$pinned" > "$handover.tmp"); then
      mv "$handover.tmp" "$handover"
    else
      rm -f "$handover.tmp"
    fi
    return 0
  fi
  age="$(ps -o etimes= -p "$pinned" 2>/dev/null | tr -d ' ')"
  if [ "${age:-0}" -ge $((86400 - ONCHAIN_SOL_ROTATE_LEAD_S)) ] 2>/dev/null; then
    launch_solana_watcher
  fi
}
ensure_onchain_solana_watcher() {
  if [ "$SHYFT_WATCHER_FIRST" -eq 1 ]; then
    ensure_shyft_solana_watcher
    return
  fi
  rows="$(solana_watchers)"
  current="$(printf '%s\n' "$rows" | awk -v s="$ROOT/$ONCHAIN_SOL_SCRIPT" '$2 == s {print $1}')"
  stale="$(printf '%s\n' "$rows" | awk -v s="$ROOT/$ONCHAIN_SOL_SCRIPT" -v t="$(own_release_tree)" \
    'NF && $2 != s && index($2, t) == 1 {print $1}')"
  unpinned="$(printf '%s\n' "$rows" | awk 'NF && !index($2, "/.runtime/memecoin/releases/") {print $1}')"
  if [ -z "$current" ]; then
    [ -z "$stale" ] || log "Solana on-chain watcher: launching beside older-release pid(s) $stale"
    launch_solana_watcher
    return 0
  fi
  others="$(printf '%s %s' "$stale" "$unpinned" | xargs)"
  pinned="$(printf '%s\n' "$current" | head -1)"
  age="$(ps -o etimes= -p "$pinned" 2>/dev/null | tr -d ' ')"
  if [ -z "$others" ]; then
    # The daily end: one pinned watcher within ROTATE_LEAD_S of its own --duration 86400 exit.
    if [ "$(printf '%s\n' "$current" | wc -l)" -eq 1 ] \
        && [ "${age:-0}" -ge $((86400 - ONCHAIN_SOL_ROTATE_LEAD_S)) ] 2>/dev/null; then
      log "Solana on-chain watcher: pinned $pinned ends its 86400 s run within ${ONCHAIN_SOL_ROTATE_LEAD_S}s (${age}s); launching its replacement beside it"
      launch_solana_watcher
    fi
    return 0
  fi
  line="$(head -1 "$STATE_ROOT/data/onchain_watch.ready" 2>/dev/null)"
  ready="${line%% *}"
  state="${line#* }"
  state="${state%% *}"
  full_ready=0
  if [ "$ready" = "$pinned" ] && [ "$state" != partial ]; then
    if [ "$SHYFT_WATCHER_FIRST" -eq 0 ] || \
        "$PY" "$ROOT/scripts/memecoin_runtime_verify.py" --code-root "$ROOT" \
          --state-root "$STATE_ROOT" --watcher-only --allow-watcher-overlap >/dev/null 2>&1; then
      full_ready=1
    fi
  fi
  if [ "$full_ready" -eq 1 ]; then
    log "Solana on-chain watcher: pinned $pinned acked every owner; stopping pid(s) $others"
    kill $others 2>/dev/null || true
  elif [ "${age:-0}" -lt "$ONCHAIN_SOL_HANDOVER_MAX_S" ] 2>/dev/null || [ -z "$age" ]; then
    log "Solana on-chain watcher: waiting for pinned $pinned to ack every owner (${age:-?}s); keeping pid(s) $others"
  elif [ "$ready" = "$pinned" ] && [ "$SHYFT_WATCHER_FIRST" -eq 0 ]; then
    # Partial at the cap: it holds a socket; an owner refused on every try would never end otherwise.
    log "Solana on-chain watcher: pinned $pinned PARTIAL ($line) after ${age}s (cap ${ONCHAIN_SOL_HANDOVER_MAX_S}s); stopping pid(s) $others"
    kill $others 2>/dev/null || true
  else
    alive=""
    for pid in $others; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
    if [ -n "$alive" ]; then
      log "Solana on-chain watcher: pinned $pinned NOT READY after ${age}s (cap ${ONCHAIN_SOL_HANDOVER_MAX_S}s); stopping it, keeping pid(s)$alive, relaunch next tick"
      kill $current 2>/dev/null || true
    else
      log "Solana on-chain watcher: pinned $pinned not ready after ${age}s (cap ${ONCHAIN_SOL_HANDOVER_MAX_S}s) and pid(s) $others gone; keeping it"
    fi
  fi
}


stop_unpinned_onchain_watcher() {
  chain="$1"
  extra="${2:-}"
  if [ "$chain" = "4663" ]; then
    args="$ONCHAIN_EVM_SCRIPT --duration 86400"
  else
    args="$ONCHAIN_EVM_SCRIPT --duration 86400 --chain $chain"
  fi
  [ -n "$extra" ] && args="$args $extra"
  rows="$(ps -eww -o pid=,args= | grep -F -- "$args" | grep -v -F -- "grep" || true)"
  if [ "$chain" = "4663" ]; then
    rows="$(printf '%s\n' "$rows" | grep -v -- "--chain" || true)"
  fi
  # "Pinned" is THIS root's release (Gamma M, 2026-09-29): a tmp-root test run beside the live
  # watcher must not treat it as its own owner and stop a developer's unpinned watcher.
  pinned="$(printf '%s\n' "$rows" | grep -F -- "$(own_release_tree)" | awk 'NF {print $1}')"
  unpinned="$(printf '%s\n' "$rows" | grep -v -F -- "/.runtime/memecoin/releases/" | awk 'NF {print $1}')"
  [ -n "$pinned" ] || return 0            # no pinned owner: leave a deliberate watcher alone
  [ -n "$unpinned" ] || return 0
  log "on-chain watcher chain=$chain: stopping unpinned pid(s) $unpinned beside pinned $pinned"
  alert "memecoin on-chain watcher chain=$chain: stopped an unpinned duplicate beside the pinned one"
  kill $unpinned 2>/dev/null || true
  sleep 1
}

upgrade_onchain_evm_watcher() {
  chain="$1"
  extra="${2:-}"
  if [ "$chain" = "4663" ]; then
    args="$ONCHAIN_EVM_SCRIPT --duration 86400"
  else
    args="$ONCHAIN_EVM_SCRIPT --duration 86400 --chain $chain"
  fi
  [ -n "$extra" ] && args="$args $extra"
  stale_pids="$(ps -eww -o pid=,args= \
    | grep -F -- "$(own_release_tree)" \
    | grep -F -- "$args" \
    | grep -v -F -- "$ROOT/" || true)"
  if [ "$chain" = "4663" ]; then
    stale_pids="$(printf '%s\n' "$stale_pids" | grep -v -- "--chain" || true)"
  fi
  stale_pids="$(printf '%s\n' "$stale_pids" | awk 'NF {print $1}')"
  [ -n "$stale_pids" ] || return 0
  log "on-chain watcher chain=$chain is from a previous release; stopping for the pinned relaunch"
  kill $stale_pids 2>/dev/null || true
  sleep 1
}

onchain_addresses_eligible() {
  local chain="$1" status code state temp
  state="$STATE_ROOT/data/onchain_evm_watch.$chain.watchdog.json"
  temp="$state.tmp.$$"
  status="$(PYTHONPATH="$ROOT" "$PY" "$ROOT/$ONCHAIN_EVM_SCRIPT" \
    --chain "$chain" --check-addresses 2>/dev/null)"
  code=$?
  if [ -z "$status" ] || { [ "$code" -ne 0 ] && [ "$code" -ne 3 ]; }; then
    code=2
    status='{"status":"blocked","reason":"address eligibility unavailable","eligible_addresses":null}'
  fi
  printf '%s\n' "$status" > "$temp" || return 1
  if ! cmp -s "$temp" "$state"; then
    case "$code" in
      0) log "on-chain watcher chain=$chain eligible: verified addresses available" ;;
      3) log "on-chain watcher chain=$chain blocked: no verified addresses" ;;
      *) log "on-chain watcher chain=$chain blocked: address eligibility unavailable" ;;
    esac
  fi
  mv -f "$temp" "$state" || return 1
  [ "$code" -eq 0 ]
}

# CHAIN 4663 IS BACK IN THE FUNDED LOOP (2026-09-28, operator order). It was retired on 2026-09-23
# by a `!= 4663` guard here: no 4663 address was verified, the watcher refused to launch after
# 2026-09-16T11:23:29Z and it had made no detection ever. On 2026-09-28 the J7 proof run
# (verify_axiom_addresses.py --confirm --j7 --chain 4663, receipts read from publicnode) wrote 47
# axiom_confirmed 4663 addresses, and the operator ordered the fast lane back, so the guard is gone.
# 4663 now goes through the same eligibility probe as every other chain.
#
# The revival found one collision the retirement hid: kill_degraded_onchain_watcher did not filter
# `--chain` lines for 4663, so a stale 4663 heartbeat killed every other chain's watcher.
# tests/test_memecoin_watchdog_4663_revival.py pins both the launch and the kill.
# ---- PERIPHERAL UPKEEP, AFTER THE RUNNER LAUNCH (2026-09-27) --------------------------------------
# BEGIN peripheral-upkeep
# The EVM watchers, observe-only chains, volume observers and the BSC worker used to be supervised
# HERE, at the top of every tick, before the runner check. On a deploy or a crash relaunch that put
# ~2-3 s of watcher kills (`sleep 1` each), eligibility probes and launches in front of the funded
# runner, with no runner up at all: exits down as well as entries (deploy 95e5e189: watchdog called
# 01:30:53, runner launched 01:30:58). None of it is needed by the runner to start; the watchers
# only feed onchain_wallet_detections and their own files.
#
# So it is a function, run ONCE per tick by `run_upkeep_once`: right after a launch (it then
# overlaps the runner's own ~10 s start), and otherwise on the way out through the EXIT trap
# (healthy runner, auth_dead, any refusal), so no path that used to supervise the watchers skips
# them. Its body is unchanged.
peripheral_upkeep() {
ensure_onchain_solana_watcher
# Ink's raw Subblocks reader is independent of the ordinary EVM head watcher and stays opt-in.
_ink_config=$(PYTHONPATH="$ROOT" "$PY" - <<'PY' 2>/dev/null
from sniper.util.envfiles import load_env_files
import os
load_env_files(os.environ['SNIPER_STATE_ROOT'])
from sniper.settings import memecoin as cfg
print('1' if cfg.MEMECOIN_INK_SUBBLOCKS_ENABLED else '0')
print(os.getenv('MEMECOIN_INK_BACKFILL_START_BLOCK', '').strip())
PY
 ) || log "Ink reader config unreadable; leaving current process untouched"
if [ -n "$_ink_config" ]; then
_ink_enabled=$(printf '%s\n' "$_ink_config" | head -1)
_ink_start=$(printf '%s\n' "$_ink_config" | sed -n '2p')
_ink_worker="$ROOT/scripts/research/onchain_ink_watch.py"
_ink_running=0
_ink_live_pids=""
_ink_stopping=""
while read -r _ink_pid _ink_path; do
  [ -n "$_ink_pid" ] || continue
  if [ "$_ink_enabled" != 1 ] || [ "$_ink_path" != "$_ink_worker" ]; then
    kill "$_ink_pid" 2>/dev/null || true
    _ink_stopping="$_ink_stopping $_ink_pid"
  else
    _ink_running=1
    _ink_live_pids="$_ink_live_pids $_ink_pid"
  fi
done < <(ps -eww -o pid=,args= | awk -v t="$(own_release_tree)" '{
  for (i=2; i<=NF; i++)
    if (index($i,t)==1 && $i ~ /\/scripts\/research\/onchain_ink_watch\.py$/) {
      print $1, $i; next
    }
}')
for _ink_try in 1 2 3 4 5 6 7 8 9 10; do
  _ink_alive=0
  for _ink_pid in $_ink_stopping; do
    kill -0 "$_ink_pid" 2>/dev/null && _ink_alive=1
  done
  [ "$_ink_alive" = 0 ] && break
  sleep 0.1
done
if [ "$_ink_enabled" = 1 ] && [ "$_ink_running" = 0 ]; then
  _ink_args=()
  if [ -n "$_ink_start" ]; then
    if [[ "$_ink_start" =~ ^[0-9]+$ ]]; then
      _ink_args=(--start-block "$_ink_start")
    else
      log "Ink reader refused: invalid MEMECOIN_INK_BACKFILL_START_BLOCK"
      _ink_enabled=0
    fi
  fi
  if [ "$_ink_enabled" = 1 ] && [ "$_ink_alive" = 0 ]; then
    setsid nohup env PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB_PATH" \
      "$PY" -u "$_ink_worker" "${_ink_args[@]}" \
      >> "$STATE_ROOT/logs/onchain_ink.log" 2>&1 </dev/null &
    log "launched pinned Ink Subblocks reader pid $!"
  elif [ "$_ink_alive" = 1 ]; then
    log "Ink reader old process did not stop; refusing duplicate"
  fi
fi
# A RUNNING READER IS NOT A HEALTHY ONE (2026-09-29, Task 11). Its own --check-beat judges the
# heartbeat: stale (hung process) or a dead worker thread is a kill; the next pass relaunches it.
# Only pids matched above in THIS release tree, and one kill per ONCHAIN_KILL_COOLDOWN_S.
if [ "$_ink_enabled" = 1 ] && [ "$_ink_running" = 1 ]; then
  _ink_reason=$(PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" "$_ink_worker" \
    --check-beat "$STATE_ROOT/data/onchain_ink_watch.heartbeat" 2>/dev/null || true)
  if [ -n "$_ink_reason" ]; then
    _ink_marker="$STATE_ROOT/data/onchain_ink_watch.lastkill"
    _ink_now="$(date -u +%s)"
    _ink_last="$(cat "$_ink_marker" 2>/dev/null)"
    case "$_ink_last" in ''|*[!0-9]*) _ink_last=0 ;; esac   # empty or partial marker (Gamma LOW)
    if [ $((_ink_now - _ink_last)) -ge "$ONCHAIN_KILL_COOLDOWN_S" ]; then
      log "Ink reader unhealthy ($_ink_reason); killing for restart"
      printf '%s\n' "$_ink_now" > "$_ink_marker"
      kill $_ink_live_pids 2>/dev/null || true
      alert "memecoin Ink reader restarted by watchdog ($_ink_reason)"
    fi
  fi
fi
fi
# Check eligibility before stale-heartbeat restarts as well as absent-process launches.
_ELIGIBLE_EVM_WATCHERS=""
_FUNDED_EVM_CHAINS="$(onchain_evm_chains)"
for _chain in $_FUNDED_EVM_CHAINS; do
  if onchain_addresses_eligible "$_chain"; then
    _ELIGIBLE_EVM_WATCHERS="$_ELIGIBLE_EVM_WATCHERS $_chain"
    stop_unpinned_onchain_watcher "$_chain"
    upgrade_onchain_evm_watcher "$_chain"
    kill_degraded_onchain_watcher "$_chain"
    ensure_onchain_evm_watcher "$_chain"
  fi
done

# OBSERVE-ONLY CHAINS (2026-09-16, Arc build): pool-key discovery, no wallet-eligibility gate --
# there is no wallet lane to be eligible for. Refuse loudly (once per chain per tick, via the
# ordinary log line rather than a silent absence) when a chain is configured here but has no WS
# URL, rather than never launching and saying nothing. A chain already handled by the funded loop
# above is skipped here only when it has eligible addresses. Pool capture must continue otherwise.
for _chain in $(onchain_evm_observe_chains_missing_ws); do
  log "on-chain watcher chain=$_chain observe-only: refusing to start, MEMECOIN_EVM_WS_URL_$_chain is unset"
done
for _chain in $(onchain_evm_observe_chains); do
  case " ${_ELIGIBLE_EVM_WATCHERS:-} " in
    *" $_chain "*) continue ;;
  esac
  stop_unpinned_onchain_watcher "$_chain" --observe-only
  upgrade_onchain_evm_watcher "$_chain" --observe-only
  kill_degraded_onchain_watcher "$_chain" --observe-only
  ensure_onchain_evm_watcher "$_chain" --observe-only
done

# Supervise the default-off volume source from this same pinned release, one call per chain. Each
# chain exits immediately unless MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS names it, so adding 8453 here
# starts nothing until the operator enables the chain and sets its WS URL.
for _vchain in 4663 8453; do
  bash "$ROOT/scripts/memecoin_onchain_volume_watchdog.sh" "$_vchain" \
    || log "volume observer supervisor failed chain=$_vchain"
done

# BSC V2/V3 marks and V2 source-pool swaps are read-only observation. Keep it release-pinned;
# the web reads its separate SQLite file and the funded entry only reads a local lower bound.
_bsc_chains=$(grep '^MEMECOIN_EVM_ALLOWLIST=' "$STATE_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '\"\047 ')
case ",${_bsc_chains:-}," in
  *,56,*)
    _bsc_worker="$ROOT/sniper/services/memecoin_bsc_v2_live.py"
    _bsc_pidfile="$STATE_ROOT/data/memecoin_bsc_v2_live.pid"
    _bsc_pid=$(cat "$_bsc_pidfile" 2>/dev/null || true)
    _bsc_cmd=""
    [[ "$_bsc_pid" =~ ^[0-9]+$ ]] && _bsc_cmd=$(tr '\0' ' ' <"/proc/$_bsc_pid/cmdline" 2>/dev/null || true)
    if [[ "$_bsc_cmd" != *"$_bsc_worker"* ]]; then
      # A pinned release switch must retire its old observer before a new one starts. Only an
      # observer of THIS state root's releases (2026-09-29): a whole-script test with a tmp
      # STATE_ROOT whose .env names 56 must not stop the live one (see own_release_tree).
      _bsc_old=$(ps -eww -o pid=,args= | awk -v t="$(own_release_tree)" '{
        for (i = 2; i <= NF; i++)
          if (index($i, t) == 1 && $i ~ /\/sniper\/services\/memecoin_bsc_v2_live\.py$/) { print $1; next }
      }')
      for _old in $_bsc_old; do
        [ "$_old" = "$$" ] || kill "$_old" 2>/dev/null || true
      done
      for _i in $(seq 1 20); do
        _still=0
        for _old in $_bsc_old; do kill -0 "$_old" 2>/dev/null && _still=1; done
        [ "$_still" -eq 0 ] && break
        sleep 0.1
      done
      if [ "${_still:-0}" -ne 0 ]; then
        log "BSC observer old release did not stop; refusing duplicate"
      else
        setsid nohup env PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" DB_PATH="$DB_PATH" \
          "$PY" -u "$_bsc_worker" >>"$STATE_ROOT/logs/memecoin_bsc_v2_live.log" 2>&1 </dev/null &
        echo "$!" >"$_bsc_pidfile"
      fi
    fi
    ;;
esac
}
# END peripheral-upkeep
_UPKEEP_DONE=0
run_upkeep_once() {
  [ "$_UPKEEP_DONE" = "1" ] && return 0
  _UPKEEP_DONE=1
  peripheral_upkeep
}
trap run_upkeep_once EXIT

# Outage escalation (2026-07-21): a persistent failure used to page an identical, reason-less
# line every watchdog minute (26 pages during the funded outage) and the recovery page carried
# no outage context. escalate_failure pages on the FIRST failure with the actual reason, then
# once per 15min while still down; escalate_recovery pages once with attempt count + downtime.
ESCALATE_STATE="$STATE_ROOT/data/memecoin_watchdog_fail.json"
escalate_failure() {
  MSG="$("$PY" "$ROOT/scripts/memecoin_watchdog_escalate.py" failure \
    --state "$ESCALATE_STATE" --reason "$1" 2>/dev/null)"
  _esc_rc=$?
  if [ "$_esc_rc" -ne 0 ] && [ -z "$MSG" ]; then
    # The helper lives in the release that may have failed: page anyway, once per 15 minutes.
    if [ -z "$(find "$ESCALATE_STATE.fallback" -mmin -15 2>/dev/null)" ]; then
      MSG="memecoin runner DOWN — escalation helper failed (rc=$_esc_rc): $1"
      touch "$ESCALATE_STATE.fallback"
    fi
  fi
  [ -n "$MSG" ] || return 0
  if ! alert "$MSG"; then
    "$PY" "$ROOT/scripts/memecoin_watchdog_escalate.py" unsent --state "$ESCALATE_STATE" >/dev/null 2>&1
    rm -f "$ESCALATE_STATE.fallback"
    log "page not delivered; the next tick retries: $(printf '%s' "$MSG" | cut -c1-160)"
  fi
}

# HOST NETWORK PROBE (Alpha GO (b), 2026-09-29). On 2026-09-24 every funded RPC failed on transport
# for 41 minutes: the host network, not the release. When a refusal carries >= 3 funded RPC
# transport errors, probe two independent HTTPS hosts. A failed probe is marked (no page can leave
# while it is down); the recovery page later says how long the host network was dark.
NETDOWN_MARK="$STATE_ROOT/data/memecoin_watchdog_netdown"
net_probe() {
  if [ -n "${MEMECOIN_WATCHDOG_NET_PROBE:-}" ]; then
    sh -c "$MEMECOIN_WATCHDOG_NET_PROBE" >/dev/null 2>&1
    return $?
  fi
  curl -sS -o /dev/null -m 5 https://www.cloudflare.com/cdn-cgi/trace >/dev/null 2>&1 \
    || curl -sS -o /dev/null -m 5 https://www.google.com/generate_204 >/dev/null 2>&1
}
net_check() {
  _nt="$(printf '%s' "$1" | grep -o 'transport_error' | wc -l | tr -d ' ')"
  [ "${_nt:-0}" -ge 3 ] || return 0
  if net_probe; then
    if [ -f "$NETDOWN_MARK" ] && [ "$(wc -w < "$NETDOWN_MARK" | tr -d ' ')" = "1" ]; then
      printf ' %s' "$(date +%s)" >> "$NETDOWN_MARK"
      log "host network back; verification still failing"
    fi
  else
    # A new outage (no mark, or the last one already has a back time) starts the clock again.
    if [ ! -f "$NETDOWN_MARK" ] || [ "$(wc -w < "$NETDOWN_MARK" | tr -d ' ')" != "1" ]; then
      printf '%s' "$(date +%s)" > "$NETDOWN_MARK"
    fi
    log "host network down: both probes failed; no page can leave until it returns"
  fi
}
# For a recovery page: how long the host network was dark, then the mark is cleared.
netdown_note() {
  [ -f "$NETDOWN_MARK" ] || return 0
  read -r _nd_first _nd_back < "$NETDOWN_MARK"
  rm -f "$NETDOWN_MARK"
  case "${_nd_first:-}" in ''|*[!0-9]*) printf '; host network was down'; return 0 ;; esac
  case "${_nd_back:-}" in ''|*[!0-9]*) _nd_back="$(date +%s)" ;; esac
  printf '; host network was down %dm' $(( (_nd_back - _nd_first) / 60 ))
}

# The page for a refused release: the first error line (not the JSON blob), the automatic retry and
# the manual rollback, with the previous release named from the deploy manifest.
verify_failure_page() {
  _first="$(printf '%s' "$1" | sed -n 's/.*"errors": *\["\([^"\\]*\).*/\1/p' | head -n 1 | cut -c1-240)"
  [ -n "$_first" ] || _first="$(printf '%s' "$1" | head -n 1 | cut -c1-240)"
  _prev="$(sed -n 's/.*"previous_sha": *"\([0-9A-Za-z]*\)".*/\1/p' "$MANIFEST" 2>/dev/null | head -n 1)"
  # 2026-09-24 led with "TPU sidecar degraded" while every funded RPC failed on transport: the host
  # network was the cause. Count those so the page does not point at the wrong component.
  _transport="$(printf '%s' "$1" | grep -o 'transport_error' | wc -l | tr -d ' ')"
  [ "${_transport:-0}" -gt 0 ] && _first="$_first (+ $_transport funded RPC transport errors: check the host network)"
  printf 'watchdog refused release %s: %s. Auto: it re-verifies every minute and launches when this clears. Manual: fix the error, then run bash %s/scripts/memecoin_watchdog.sh; roll back: scripts/deploy_memecoin_runtime.sh %s --allow-rollback' \
    "$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" "$_first" "$ROOT" "${_prev:-<previous sha from data/memecoin_deploy.json>}"
}
escalate_recovery() {
  MSG="$("$PY" "$ROOT/scripts/memecoin_watchdog_escalate.py" recovery \
    --state "$ESCALATE_STATE" --detail "$1" 2>/dev/null || true)"
  [ -n "$MSG" ] && alert "$MSG"
}
# last actionable runner-log line: the reason the operator needs is usually the sys.exit line
# (e.g. "funded contract reconciliation failed: ..."), not a traceback frame.
runner_fail_reason() {
  tail -n 40 "$LOGFILE" 2>/dev/null \
    | grep -vE '^\s|^Traceback|^During handling|^$' | tail -n 1 | cut -c1-300
}

runner_reason() {
  PYTHONPATH="$ROOT" "$PY" -c \
    "import time; from sniper.services.memecoin_runner import read_heartbeat, runner_health; print(runner_health(read_heartbeat(), time.time())['reason'])" \
    2>/dev/null || echo check_failed
}

release_unsigned_after_runner_stop() {
  RELEASED_UNSIGNED="$(PYTHONPATH="$ROOT" DB_PATH="$DB_PATH" "$PY" - <<'PY'
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection

with get_connection() as conn:
    print(len(m.release_unsigned_reservations_after_runner_stop(conn)))
PY
)" || {
    log "could not release unsigned reservations after runner stop"
    escalate_failure "could not release unsigned reservations after runner stop"
    exit 1
  }
  [ "$RELEASED_UNSIGNED" = "0" ] || log "released $RELEASED_UNSIGNED unsigned reservation(s) after runner stop"
  RELEASED_CLOSES="$(PYTHONPATH="$ROOT" DB_PATH="$DB_PATH" "$PY" - <<'PY'
from sniper.persistence import db_memecoin as m
from sniper.persistence.db_core import get_connection

with get_connection() as conn:
    print(len(m.release_unsigned_close_claims_after_runner_stop(conn)))
PY
)" || {
    log "could not release unsigned close claims after runner stop"
    escalate_failure "could not release unsigned close claims after runner stop"
    exit 1
  }
  [ "$RELEASED_CLOSES" = "0" ] || log "released $RELEASED_CLOSES unsigned close claim(s) after runner stop"
}

# Cluster admission is buy-only. A worker failure blocks new buys through the client.
# It never stops this watchdog from starting the funded exit runner.
CLUSTER_ENABLED="$(PYTHONPATH="$ROOT" SNIPER_STATE_ROOT="$STATE_ROOT" "$PY" -c \
  "from sniper.util.envfiles import load_env_files; load_env_files('$STATE_ROOT'); from sniper.settings.memecoin import MEMECOIN_REAL_CLUSTER_GATE_ENABLED; print(1 if MEMECOIN_REAL_CLUSTER_GATE_ENABLED else 0)" \
  2>/dev/null || echo 0)"
if [ "$CLUSTER_ENABLED" = "1" ]; then
  SNIPER_STATE_ROOT="$STATE_ROOT" bash "$ROOT/scripts/memecoin_cluster_gate_watchdog.sh" \
    || log "cluster worker launch failed; new buys will fail closed"
fi

# Seconds since the runner last wrote a heartbeat, or -1 when no heartbeat exists. The launch
# overwrites the file, so callers MUST read this before they launch.
blackout_gap_s() {
  PYTHONPATH="$ROOT" "$PY" -c \
    "import time; from sniper.services.memecoin_runner import read_heartbeat; h = read_heartbeat() or {}; ts = h.get('ts'); print(int(time.time() - float(ts)) if ts else -1)" \
    2>/dev/null || echo -1
}

# A host poweroff pages NOTHING on its own. escalate_failure only records a failure that a
# watchdog tick observed, and no tick runs while the host is off, so the next boot reads as a
# clean first launch. On 2026-08-12 that hid a 56-minute funded blackout (12:40:18-13:36:31Z,
# `journalctl --list-boots`) which cost one armed-wallet signal outright: cryptojuggler3 bought
# BENDER at 13:24:11Z with no process alive to see it, and the backfilled catch arrived 778s
# stale against a 120s bound. The only trace was a gap in memecoin_firehose.received_at.
page_blackout_before_launch() {
  GAP_S="$(blackout_gap_s)"
  # Any non-integer (missing heartbeat, unreadable ts, python failure) becomes -1 and pages
  # nothing: an absent heartbeat is unknown coverage, not measured downtime.
  case "$GAP_S" in ''|*[!0-9]*) GAP_S=-1 ;; esac
  MIN_S="${MEMECOIN_BLACKOUT_ALERT_MIN_S:-300}"
  case "$MIN_S" in ''|*[!0-9]*) MIN_S=300 ;; esac
  [ "$GAP_S" -gt "$MIN_S" ] || return 0
  log "funded blackout ${GAP_S}s since the last heartbeat"
  # IN THE BACKGROUND (Gamma LOW, 2026-09-29): this runs before the relaunch, and an unreachable
  # Telegram costs up to ~18 s (primary 8 s + stdlib fallback 10 s). The relaunch never waits on it.
  ( trap - EXIT
    alert "🕳 memecoin BLACKOUT | no funded coverage for $((GAP_S / 60))m$((GAP_S % 60))s (host down or runner gone); relaunching now"
  ) >/dev/null 2>&1 &
}

RUNNER_ROWS="$("$PY" "$ROOT/scripts/memecoin_runtime_processes.py" --expected "$MATCH" 2>/dev/null)"
if [ $? -ne 0 ]; then
  log "runner process discovery failed; refusing launch"
  escalate_failure "watchdog could not verify existing runner processes; launch refused"
  exit 1
fi
FOREIGN_ROWS="$(printf '%s\n' "$RUNNER_ROWS" | awk -F '\t' '$2 == "foreign"')"
if [ -n "$FOREIGN_ROWS" ]; then
  log "foreign runner detected; refusing pinned launch: $FOREIGN_ROWS"
  escalate_failure "watchdog found an unpinned/foreign runner; launch refused"
  exit 1
fi
PINNED_PIDS="$(printf '%s\n' "$RUNNER_ROWS" | awk -F '\t' '$2 == "pinned" {print $1}')"
if [ -n "$PINNED_PIDS" ]; then
  PINNED_COUNT="$(printf '%s\n' "$PINNED_PIDS" | awk 'NF {count++} END {print count+0}')"
  if [ "$PINNED_COUNT" -ne 1 ]; then
    log "multiple pinned runners detected ($PINNED_COUNT); refusing to choose one"
    escalate_failure "watchdog found multiple pinned runners; operator action required"
    exit 1
  fi
  if [ "$(cat "$PIDFILE" 2>/dev/null || true)" != "$PINNED_PIDS" ]; then
    log "repairing pinned pidfile to live pid $PINNED_PIDS"
    printf '%s\n' "$PINNED_PIDS" > "$PIDFILE"
  fi
  REASON="$(runner_reason)"
  if [ "$REASON" = "stale" ] || [ "$REASON" = "sweep_stale" ]; then
    log "runner unhealthy ($REASON); killing pinned process for restart"
    # Preserve the blocked call through the runner's registered thread-dump signal.
    kill -USR2 $PINNED_PIDS 2>/dev/null || true
    sleep 0.2
    kill $PINNED_PIDS 2>/dev/null || true
    # WAIT FOR THE EXIT, UP TO A BOUND, BEFORE CALLING IT A HANG (2026-09-24). The runner's SIGTERM
    # handler only runs when its main thread returns to bytecode, so a thread blocked in network
    # I/O delays it: 21:19Z took 17.5 s during a Wi-Fi outage, against 0.2-1.0 s otherwise. A 2 s
    # check called that a hang, the only time the line was ever logged. No SIGKILL: a funded
    # runner killed mid-signature can leave its write-ahead state ambiguous.
    _stop_wait_max="${MEMECOIN_WATCHDOG_STOP_WAIT_S:-30}"
    case "$_stop_wait_max" in ''|*[!0-9]*) _stop_wait_max=30 ;; esac
    _stop_waited=0
    while kill -0 "$PINNED_PIDS" 2>/dev/null && [ "$_stop_waited" -lt "$_stop_wait_max" ]; do
      sleep 1
      _stop_waited=$((_stop_waited + 1))
    done
    kill -0 "$PINNED_PIDS" 2>/dev/null && {
      log "runner did not stop within ${_stop_waited}s after stale restart request"
      escalate_failure "runner did not stop within ${_stop_waited}s after stale restart request"
      exit 1
    }
    [ "$_stop_waited" -le 1 ] || log "runner stopped ${_stop_waited}s after stale restart request"
    ( trap - EXIT; alert "memecoin runner restarted by watchdog ($REASON)" ) >/dev/null 2>&1 &   # never delays the relaunch
  elif [ "$REASON" = "auth_dead" ]; then
    log "runner auth_dead; not restarting because credentials need operator repair"
    exit 0
  else
    # healthy pinned runner: if a failure streak was active (operator fixed it by hand, or a
    # previous launch verified after this script's timeout window), page recovery once.
    escalate_recovery "pid $PINNED_PIDS healthy$(netdown_note)"
    exit 0
  fi
fi

# Process discovery proved no runner remains. A row with no signing identity cannot now reach a
# provider. Signed or ambiguous rows remain for chain recovery.
release_unsigned_after_runner_stop

# ONE VERIFY PER DEPLOY, NOT THREE (2026-09-27). A deploy verifies the release, switches to it and
# then calls this script, which verified it again: ~2 s with no runner, a temporary sidecar and every
# context rebuilt, for a release checked seconds earlier against the same manifest. The repeat is
# skipped only when the deploy named THIS release's exact SHA; cron, manual runs, a rollback
# relaunch, a mismatch or an unreadable HEAD all verify in full.
if [ -n "$_DEPLOY_VERIFIED_SHA" ] \
    && [ "$_DEPLOY_VERIFIED_SHA" = "$(git -C "$ROOT" rev-parse HEAD 2>/dev/null)" ]; then
  log "release $_DEPLOY_VERIFIED_SHA verified by the deploy calling this run; not verifying again"
else
  VERIFY_OUTPUT="$($PY "$ROOT/scripts/memecoin_runtime_verify.py" \
    --code-root "$ROOT" --state-root "$STATE_ROOT" --manifest "$MANIFEST" \
    --allow-shyft-unavailable-custody 2>&1)"
  if [ $? -ne 0 ]; then
    log "PINNED RELEASE VERIFICATION FAILED: $VERIFY_OUTPUT"
    net_check "$VERIFY_OUTPUT"
    escalate_failure "$(verify_failure_page "$VERIFY_OUTPUT")"
    exit 1
  fi
fi

if [ ! -f "$STATE_ROOT/.fomo.env" ]; then
  log "state .fomo.env missing; refusing launch"
  escalate_failure "state .fomo.env missing; runner cannot launch"
  exit 1
fi

page_blackout_before_launch

if [ "$SHYFT_WATCHER_FIRST" -eq 1 ]; then
  log "Shyft Solana entries and auxiliary streams staged by runner until quota-safe watcher handover; funded exit/recovery owner launching"
fi

log "pinned runner not running; launching sha $(git -C "$ROOT" rev-parse HEAD)"
cd "$ROOT" || exit 1
LAUNCH_STARTED="$(date +%s)"
# 5000ms, not 1500. fomo's own client polls the same REST /feed at 15s; at 1500ms we ran ~10x
# their app (57,600 req/day vs 5,760) and the account was blocked at the APPLICATION layer on
# 2026-07-31 — a valid Privy JWT returning 403 on every endpoint while a garbage token from the
# same IP still got a clean 401. That cost 4 days of paper collection and the account itself.
#
# This default is load-bearing because it OVERRIDES `.env`: load_env_files is setdefault, and
# this line exports the var before the runner reads any env file, so a .env entry can never win.
# The cron lines also pass MEMECOIN_POLL_MS=5000 explicitly; this keeps a deploy-triggered launch
# (deploy_memecoin_runtime.sh --restart calls this script WITHOUT the cron env) from silently
# reverting to the banned rate. 5000ms keeps detection lag ~5s, far inside the 60s latency cliff.
setsid nohup env PYTHONPATH="$ROOT" MEMECOIN_POLL_MS="${MEMECOIN_POLL_MS:-5000}" \
  "$PY" -u "$MATCH" >> "$LOGFILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PIDFILE"
run_upkeep_once   # while the runner starts; see peripheral-upkeep
# A WALL-CLOCK BOUND AND A 0.25 s POLL (2026-09-27). The old loop slept 1 s between checks and ran
# the process scan first, so a launch that had passed waited up to ~1.6 s to be noticed, and the
# deploy's entry gate with it. The heartbeat check runs first (it fails until the runner's first
# exit sweep, so most polls cost one python start); both checks must still pass.
_launch_deadline=$((LAUNCH_STARTED + STARTUP_TIMEOUT_S))
while [ "$(date +%s)" -lt "$_launch_deadline" ]; do
  sleep 0.25
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    break
  fi
  if PYTHONPATH="$ROOT" "$PY" -c \
        "import sys,time; from sniper.services.memecoin_runner import read_heartbeat,runner_launch_health; h=read_heartbeat(); sys.exit(0 if runner_launch_health(h,float(sys.argv[1]),time.time())['reason']=='ok' else 1)" \
        "$LAUNCH_STARTED" \
      && "$PY" "$ROOT/scripts/memecoin_runtime_processes.py" --expected "$MATCH" \
      | awk -F '\t' -v pid="$NEW_PID" '$1 == pid && $2 == "pinned" {found=1} END {exit !found}'; then
    log "runner launch verified pid $NEW_PID path $MATCH"
    # THE PAGES LEAVE THE CUTOVER PATH (2026-09-27). A deploy waits on this script before it
    # clears the entry gate, and these two sends (a Telegram round trip each) ran in front of the
    # exit: deploy e9781d89 logged the line above at 02:16:07Z and cleared entries at 02:16:15.8.
    # They now run in the background, without this script's EXIT trap; the launch is verified.
    ( trap - EXIT; escalate_recovery "pid $NEW_PID$(netdown_note)"
      alert "memecoin pinned runner launched (pid $NEW_PID)" ) >/dev/null 2>&1 &
    exit 0
  fi
done
if [ "$(cat "$PIDFILE" 2>/dev/null || true)" = "$NEW_PID" ]; then
  rm -f "$PIDFILE"
fi
log "pinned runner launch failed verification pid $NEW_PID"
escalate_failure "launch failed post-launch verification — $(runner_fail_reason)"
exit 1
