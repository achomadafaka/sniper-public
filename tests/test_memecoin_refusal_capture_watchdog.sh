#!/usr/bin/env bash
# The refusal-capture watchdog must launch a PINNED release and replace a loop that is not the
# current one (2026-09-18).
#
# The defect: it launched `$ROOT/scripts/...` from the mutable checkout and restarted only on a
# stale heartbeat, so a healthy loop kept its ORIGINAL import forever. The one that ran through
# 2026-09-17/18 captured 0 of 34 `drawdown_pause` and 0 of 1 `m5_volume_under_abs_floor` while
# reporting healthy, because a deploy cannot update a process that already imported its code.
#
# A SECOND DEFECT (Codex review, 2026-09-20): the watchdog matched and signaled by a bare relative
# script-path substring over the WHOLE host process table, so a process from an unrelated state
# root running the same relative script name would be killed too. This suite's OWN first fixture
# reproduced the hazard: it rewrote only ROOT and the interpreter, kept the real `ps`/`kill`, and
# so its "cold start" case matched and could have terminated the live production worker (verified
# separately: pid 484004 satisfied the old bare-substring match). No test run since is believed to
# have hit it, but the possibility alone made this file unsafe to run, which is the whole point.
#
# THE SAFETY BOUNDARY, now load-bearing for every case below: `ps` is replaced with a function
# that ignores its real arguments and returns a fixture-controlled table; `kill` is replaced with
# a function that only records what it would have sent. The watchdog is SOURCED, not executed as a
# subprocess, inside its own `bash -c` so its `exit` calls end only that subshell, and so the two
# overrides actually take effect -- a bash BUILTIN `kill` takes precedence over a PATH stub, but a
# shell FUNCTION named `kill` takes precedence over the builtin. No case below can therefore ever
# deliver a real signal, independent of whether the ownership check in the script under test is
# itself correct -- the isolation does not depend on the fix working.
#
# Two cases exercise the ownership check with a REAL background process this suite owns (killed by
# the test's own real `kill`, never by the code under test, which only ever sees the fake one):
# an old LOCAL release, which must be replaced, and an unrelated FOREIGN state root that happens to
# share a release directory shape, which must be left alone.
set -u
WD="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)/scripts/memecoin_refusal_capture_watchdog.sh"
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  ok   %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  FAIL %s\n     %s\n' "$1" "${2:-}"; }

# A tree that looks like the real one: a state root, two releases, a current link.
new_tree() {
  T="$(mktemp -d)"
  mkdir -p "$T/logs" "$T/data" "$T/scripts" \
           "$T/.runtime/memecoin/releases/aaaa1111/scripts" \
           "$T/.runtime/memecoin/releases/bbbb2222/scripts"
  printf 'MEMECOIN_CAPTURE_ENABLED=1\n' > "$T/.env"
  for p in "$T/scripts" "$T/.runtime/memecoin/releases/aaaa1111/scripts" \
           "$T/.runtime/memecoin/releases/bbbb2222/scripts"; do
    printf '#!/usr/bin/env python3\n' > "$p/memecoin_refusal_capture_loop.py"
  done
  ln -sfn "$T/.runtime/memecoin/releases/bbbb2222" "$T/.runtime/memecoin/current"
  echo "$T"
}

# A stub interpreter that records its argv and stamps the heartbeat, so the watchdog sees a start.
stub_py() {
  local tree="$1"
  cat > "$tree/stub_py" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$tree/launched.txt"
touch "$tree/data/memecoin_refusal_capture.heartbeat"
STUB
  chmod +x "$tree/stub_py"
  echo "$tree/stub_py"
}

# A harmless, disposable process this suite owns, so the watchdog's REAL ownership check
# (/proc/$pid/environ) has something genuine to read. `env` execs directly into `sleep`, so `$!`
# is also sleep's own pid -- no fork happens in between to lose track of. Redirected to /dev/null
# so an unreaped holder can never hold this script's own stdout/stderr open.
spawn_holder() {
  local state_root="$1"
  env SNIPER_STATE_ROOT="$state_root" sleep 300 >/dev/null 2>&1 &
  echo $!
}
reap_holder() {
  kill "$1" 2>/dev/null || true
  wait "$1" 2>/dev/null || true
}

# `ps_table`, when given, is a file of fixture-controlled `PID ARGS...` lines fed to the script in
# place of a real process scan. Omitted (or empty) means "nothing is running" -- the common case
# for the launch-behavior tests below, which never need the wedge/upgrade path to fire at all.
run_wd() {
  local tree="$1" stub="$2" ps_table="${3:-/dev/null}"
  sed -e "s#^ROOT=.*#ROOT=$tree#" -e "s#^PY=.*#PY=$stub#" "$WD" > "$tree/wd.sh"
  : > "$tree/kills.txt"
  bash -c '
    # Captured into named variables BEFORE ps/kill are defined: a call like `ps -eww -o pid=,args=`
    # binds ITS OWN $1/$2/... inside the function body, shadowing these outer positionals -- a
    # bare "$1" inside ps() would read the real ps flag, not the fixture path.
    _ps_table="$1"; _kills_file="$2"; _wd="$3"
    ps() { cat "$_ps_table"; }
    kill() { printf "%s\n" "$*" >> "$_kills_file"; }
    # shellcheck disable=SC1090
    . "$_wd"
  ' _ "$ps_table" "$tree/kills.txt" "$tree/wd.sh"
}

# --- 1. a cold start launches the CURRENT release, never the checkout ---------------------------
T="$(new_tree)"; S="$(stub_py "$T")"
run_wd "$T" "$S" >/dev/null 2>&1
if grep -q "releases/bbbb2222/scripts/memecoin_refusal_capture_loop.py" "$T/launched.txt" 2>/dev/null; then
  ok "a cold start launches the current release"
else
  bad "a cold start launches the current release" "launched: $(cat "$T/launched.txt" 2>/dev/null)"
fi
if grep -q "^-u $T/scripts/" "$T/launched.txt" 2>/dev/null; then
  bad "the checkout path must not be launched" "$(cat "$T/launched.txt")"
else
  ok "the mutable checkout is not launched"
fi
if [ -s "$T/kills.txt" ]; then
  bad "a cold start signals nothing" "$(cat "$T/kills.txt")"
else
  ok "a cold start signals nothing"
fi
rm -rf "$T"

# --- 2. no release yet: fall back to the checkout rather than stop capturing ---------------------
T="$(new_tree)"; S="$(stub_py "$T")"; rm -f "$T/.runtime/memecoin/current"
run_wd "$T" "$S" >/dev/null 2>&1
if grep -q "$T/scripts/memecoin_refusal_capture_loop.py" "$T/launched.txt" 2>/dev/null; then
  ok "with no current release it falls back to the checkout"
else
  bad "with no current release it falls back to the checkout" "$(cat "$T/launched.txt" 2>/dev/null)"
fi
if grep -q "falling back to the checkout" "$T/logs/memecoin_refusal_capture.log" 2>/dev/null; then
  ok "the fallback is logged, not silent"
else
  bad "the fallback is logged, not silent" "$(cat "$T/logs/memecoin_refusal_capture.log" 2>/dev/null)"
fi
rm -rf "$T"

# --- 3. the launch carries the release on PYTHONPATH and the state root separately ---------------
T="$(new_tree)"
sed -e "s#^ROOT=.*#ROOT=$T#" "$WD" > "$T/wd.sh"
if grep -q 'PYTHONPATH="${CURRENT:-$ROOT}" SNIPER_STATE_ROOT="$ROOT"' "$T/wd.sh"; then
  ok "the release carries the code and the state root carries .env and the ledger"
else
  bad "the release carries the code and the state root carries .env and the ledger" \
      "$(grep -n 'setsid' "$T/wd.sh")"
fi
rm -rf "$T"

# --- 4. matching is anchored, never a host-wide substring, and never pgrep -----------------------
T="$(new_tree)"
sed -e "s#^ROOT=.*#ROOT=$T#" "$WD" > "$T/wd.sh"
if grep -q 'ps -eww -o pid=,args=' "$T/wd.sh"; then
  ok "process enumeration uses ps -eww (the -ww is load-bearing against line truncation)"
else
  bad "process enumeration uses ps -eww" "$(grep -n 'ps -e' "$T/wd.sh")"
fi
if grep -q '/proc/\$pid/environ' "$T/wd.sh" && grep -q "SNIPER_STATE_ROOT=" "$T/wd.sh"; then
  ok "ownership reads the candidate's own environment, not just its path"
else
  bad "ownership reads the candidate's own environment" "$(grep -n 'environ' "$T/wd.sh")"
fi
# COMMENT-STRIPPED, and the first draft of this guard was not: the file's own comment says
# "never `pgrep -f`", so the check matched the prose warning against the thing it forbids and
# reported a violation that did not exist. Same defect as the blindness-attribution guards found
# on 2026-09-18 -- a guard must read code, never its own explanation.
if sed 's/#.*//' "$T/wd.sh" | grep -q 'pgrep -f'; then
  bad "pgrep -f must not be used" "it matches any command line that mentions the path"
else
  ok "pgrep -f is not used in code"
fi
rm -rf "$T"

# --- 5. an old LOCAL release is recognized as running and replaced -------------------------------
T="$(new_tree)"; S="$(stub_py "$T")"
HOLDER="$(spawn_holder "$T")"
printf '%s stub_py -u %s/.runtime/memecoin/releases/aaaa1111/scripts/memecoin_refusal_capture_loop.py\n' \
  "$HOLDER" "$T" > "$T/ps_table.txt"
run_wd "$T" "$S" "$T/ps_table.txt" >/dev/null 2>&1
if grep -qF "$HOLDER" "$T/kills.txt" 2>/dev/null; then
  ok "a loop on an old local release is signaled"
else
  bad "a loop on an old local release is signaled" "kills: $(cat "$T/kills.txt" 2>/dev/null)"
fi
if grep -q "loop running from a release that is not bbbb2222" "$T/logs/memecoin_refusal_capture.log" 2>/dev/null; then
  ok "replacing the old release is logged"
else
  bad "replacing the old release is logged" "$(cat "$T/logs/memecoin_refusal_capture.log" 2>/dev/null)"
fi
if grep -q "releases/bbbb2222/scripts/memecoin_refusal_capture_loop.py" "$T/launched.txt" 2>/dev/null; then
  ok "the current release is launched after replacing the old one"
else
  bad "the current release is launched after replacing the old one" "$(cat "$T/launched.txt" 2>/dev/null)"
fi
reap_holder "$HOLDER"
rm -rf "$T"

# --- 6. an unrelated FOREIGN state root is left alone, even sharing a release path shape ---------
# Same release-path shape as case 5 -- the point is that the path alone is not ownership. This
# holder's real environment names a different root entirely, and only that separate check may
# exclude it (Codex's finding: two processes can share a release directory while using different
# state roots).
T="$(new_tree)"; S="$(stub_py "$T")"
FOREIGN="$(mktemp -d)"
HOLDER="$(spawn_holder "$FOREIGN")"
printf '%s stub_py -u %s/.runtime/memecoin/releases/aaaa1111/scripts/memecoin_refusal_capture_loop.py\n' \
  "$HOLDER" "$T" > "$T/ps_table.txt"
run_wd "$T" "$S" "$T/ps_table.txt" >/dev/null 2>&1
if [ -s "$T/kills.txt" ]; then
  bad "an unrelated foreign root is never signaled" "kills: $(cat "$T/kills.txt")"
else
  ok "an unrelated foreign root is never signaled"
fi
if kill -0 "$HOLDER" 2>/dev/null; then
  ok "the foreign process is still alive"
else
  bad "the foreign process is still alive" "it exited unexpectedly"
fi
reap_holder "$HOLDER"
rm -rf "$T" "$FOREIGN"

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
