#!/usr/bin/env bash
# Swap the fomo account safely, and restore the pacing that survived the 34 days between the two
# bans. Operator-run, one shot, fail-closed at every step.
#
# WHY THIS IS A SCRIPT AND NOT A CHECKLIST
# `FomoAuth._load_store()` says "a persisted rotated token wins over env". So new credentials in
# .fomo.env are IGNORED while .fomo_tokens.json still holds the old ones. Done by hand, the swap
# looks successful — the runner restarts, auth_ok reads true — and the BANNED account is still in
# use. The store must move aside BEFORE the runner dies, or a watchdog relaunch inside the 60 s
# cron window re-reads it.
#
# ORDER MATTERS: verify -> move store -> set pacing -> stop runner -> relaunch -> verify identity.
#
# Usage:  bash scripts/memecoin_fomo_account_swap.sh            # dry run, changes nothing
#         bash scripts/memecoin_fomo_account_swap.sh --confirm  # perform the swap
set -euo pipefail

REPO="/home/bigpapi/Desktop/Sniper"
cd "$REPO"
STORE="$REPO/.fomo_tokens.json"
FOMO_ENV="$REPO/.fomo.env"
ENV_FILE="$REPO/.env"
# EVERY account fomo has refused. .fomo.env was found still holding the FIRST one, so a guard that
# knew only the most recent ban would have swapped us onto a different dead account and called it
# success. Add a sub here the moment an account is refused.
# THIRD BAN 2026-09-09T14:38Z: cmr6rem8o was refused on GET /v2/users/current with 403 while
# its JWT was still 40 minutes from expiry, and the activity WebSocket had reconnected 9 times
# with zero payloads. MEMECOIN_FOMO_BASE_GAP_S was already 0, so the 2026-09-06 rate-floor
# cause was NOT in play this time.
BANNED_SUBS="did:privy:cmlflprko00d1lc0bckz0k4tw did:privy:cmsd52yly00dx0cl9iezjvhxs did:privy:cmr6rem8o00op0cl4hcn2l5jp"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CONFIRM="${1:-}"

say() { printf '%s\n' "$*"; }
die() { printf 'REFUSED: %s\n' "$*" >&2; exit 1; }

say "=== fomo account swap ($([ "$CONFIRM" = "--confirm" ] && echo LIVE || echo DRY-RUN)) ==="

# --- 1. the new credentials must exist, and must not be the banned account -----------------------
[ -f "$FOMO_ENV" ] || die ".fomo.env not found — write the new FOMO_ACCESS_TOKEN / FOMO_REFRESH_TOKEN / FOMO_CA_ID first"
for k in FOMO_ACCESS_TOKEN FOMO_REFRESH_TOKEN FOMO_CA_ID; do
    grep -qE "^${k}=.+" "$FOMO_ENV" || die "$k is missing or empty in .fomo.env"
done

NEW_SUB="$(python3 - <<'PY'
import base64, json, os, re
env = {}
for line in open("/home/bigpapi/Desktop/Sniper/.fomo.env"):
    m = re.match(r'^([A-Z_0-9]+)=(.*)$', line.strip())
    if m:
        env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
tok = env.get("FOMO_ACCESS_TOKEN", "")
try:
    b = tok.split(".")[1]; b += "=" * (-len(b) % 4)
    print(json.loads(base64.urlsafe_b64decode(b)).get("sub", ""))
except Exception:
    print("")
PY
)"
[ -n "$NEW_SUB" ] || die "could not decode a 'sub' from the new FOMO_ACCESS_TOKEN — is it a JWT?"
for b in $BANNED_SUBS; do
    [ "$NEW_SUB" != "$b" ] || die "the token in .fomo.env is a BANNED account ($b). .fomo.env is stale — write the NEW credentials into it first."
done
say "  new account sub : $NEW_SUB"
say "  known-banned    : $BANNED_SUBS"

# --- 2. nothing may be in flight ----------------------------------------------------------------
PENDING="$(python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:/home/bigpapi/Desktop/Sniper/sniper_tracker.db?mode=ro", uri=True)
try:
    print(c.execute("SELECT COUNT(*) FROM memecoin_pending_entries "
                    "WHERE status NOT IN ('confirmed','failed')").fetchone()[0])
except Exception:
    print("ERR")
PY
)"
[ "$PENDING" = "0" ] || die "pending reservations = $PENDING (want 0). A swap mid-reservation can strand a funded entry."
say "  pending reservations: 0"

if [ "$CONFIRM" != "--confirm" ]; then
    say ""
    say "DRY RUN — would now:"
    say "  1. move $STORE -> .fomo_tokens.banned-$STAMP.json"
    say "  2. set MEMECOIN_FOMO_BASE_GAP_S=0 in .env (the value that ran the 34 safe days)"
    say "  3. stop the runner; the cron watchdog relaunches it within 60s on the pinned release"
    say "  4. verify the live JWT sub == $NEW_SUB and that auth_ok is true"
    say ""
    say "Re-run with --confirm to perform it."
    exit 0
fi

# --- 3. store aside FIRST, so a fast watchdog relaunch cannot re-read the banned tokens ----------
if [ -f "$STORE" ]; then
    mv "$STORE" "$REPO/.fomo_tokens.banned-$STAMP.json"
    say "  moved store aside -> .fomo_tokens.banned-$STAMP.json"
else
    say "  no existing store to move"
fi

# --- 4. restore the pacing that survived between the bans ---------------------------------------
cp "$ENV_FILE" "$ENV_FILE.bak.$STAMP"
python3 - "$ENV_FILE" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p).read()
# The floor did not exist during the 34 safe days: the guard started at 0 and rose only on THEIR
# 429s. A 0.25 s floor replaced that feedback with constant pressure and the account was refused
# the same day. 0 restores the proven behaviour exactly.
s = re.sub(r'^MEMECOIN_FOMO_BASE_GAP_S=.*$',
           'MEMECOIN_FOMO_BASE_GAP_S=0', s, flags=re.M)
open(p, "w").write(s)
PY
say "  MEMECOIN_FOMO_BASE_GAP_S=0 (backup: .env.bak.$STAMP)"

# --- 5. stop the runner; the cron watchdog relaunches it on the pinned release -------------------
PID="$(ps -eo pid,args | grep '[m]emecoin_runner_loop' | grep -v 'bash -c' | awk '{print $1}' | head -1 || true)"
if [ -n "$PID" ]; then
    kill "$PID" && say "  stopped runner pid $PID; watchdog relaunches within 60s"
else
    say "  no runner process was up"
fi

# --- 6. verify the swap actually took ------------------------------------------------------------
say "  waiting for relaunch + first auth..."
for _ in $(seq 1 24); do
    sleep 5
    [ -f "$STORE" ] && break
done

python3 - "$NEW_SUB" <<'PY'
import base64, json, os, sys, time
new_sub = sys.argv[1]
store = "/home/bigpapi/Desktop/Sniper/.fomo_tokens.json"
hb = "/home/bigpapi/Desktop/Sniper/data/memecoin_runner.heartbeat"
ok = True
if not os.path.exists(store):
    print("  VERIFY FAIL: no token store written — the runner has not authenticated yet")
    ok = False
else:
    tok = json.load(open(store)).get("access", "")
    b = tok.split(".")[1]; b += "=" * (-len(b) % 4)
    live = json.loads(base64.urlsafe_b64decode(b)).get("sub", "")
    print("  live account sub:", live)
    if live != new_sub:
        print("  VERIFY FAIL: the live account is NOT the new one")
        ok = False
try:
    d = json.load(open(hb))
    age = time.time() - d["ts"]
    print("  heartbeat age %.1fs  auth_ok %s" % (age, d.get("auth_ok")))
    if age > 90 or not d.get("auth_ok"):
        print("  VERIFY FAIL: runner unhealthy after the swap")
        ok = False
except Exception as e:
    print("  VERIFY FAIL: heartbeat unreadable:", e); ok = False
print("SWAP OK" if ok else "SWAP INCOMPLETE — investigate before trading")
sys.exit(0 if ok else 1)
PY
