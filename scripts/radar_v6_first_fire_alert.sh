#!/usr/bin/env bash
# Pages Telegram ONCE when pump-confirmation-truth-v6 records its first durable alert —
# the forward clock's first tick. One-shot by sentinel, like memecoin_exit_read_alert.sh.
# The latch is written only after CONFIRMED delivery, so a failed page retries next cron.
# Cron: every 15 minutes. State (DB, sentinel, .env) from SNIPER_STATE_ROOT; code pinned.
set -u

CODE_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
ROOT="${SNIPER_STATE_ROOT:-$CODE_ROOT}"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
SENTINEL="$ROOT/data/radar_v6_first_fire.paged"
DB="$ROOT/data/memecoin_opportunity_insights.db"

[ -f "$SENTINEL" ] && exit 0
[ -f "$DB" ] || exit 0

FIRST="$("$PY" - "$DB" <<'PYEOF'
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = conn.execute(
    "SELECT substr(observed_at,1,19) FROM op_insight_alerts"
    " WHERE rule_version='pump-confirmation-truth-v6' ORDER BY observed_at LIMIT 1"
).fetchone()
print(row[0] if row else "")
PYEOF
)" || exit 0
[ -n "$FIRST" ] || exit 0

if PYTHONPATH="$CODE_ROOT" "$PY" -c "
import sys
from sniper.util.envfiles import load_env_files
load_env_files('$ROOT')
from sniper.services.memecoin_telegram import send_telegram
ok, _ms = send_telegram('🟢 radar v6 FIRST FIRE @ ' + sys.argv[1] + 'Z — the forward clock'
                        ' is ticking. 25 matched pairs at the gate band ≈ 3 days out.')
sys.exit(0 if ok else 1)" "$FIRST" >/dev/null 2>&1; then
    date -u +%FT%TZ > "$SENTINEL"
fi
