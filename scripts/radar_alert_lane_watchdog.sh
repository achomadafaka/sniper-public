#!/usr/bin/env bash
# Page when a direct Radar quote path fails or keeps an old request.
# Cron calls this script every 15 minutes from the active opportunity release.
set -u

CODE_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"
ROOT="${SNIPER_STATE_ROOT:-$CODE_ROOT}"
PY="${SNIPER_PYTHON:-/home/bigpapi/.pyenv/versions/3.13.0/bin/python3}"
LATCH="$ROOT/data/radar_alert_lane_outage.latched"
DB="$ROOT/data/memecoin_opportunity.db"

[ -f "$DB" ] || exit 0

STATE="$("$PY" - "$DB" <<'PYEOF'
import sqlite3
import sys

conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
rows = conn.execute(
    "SELECT source_key,blocker_code FROM op_runtime_status "
    "WHERE source_key LIKE 'opportunity:quote:%' "
    "AND status='degraded' "
    "AND blocker_code IN ('QUOTE_PATH_FAILURE','QUOTE_PATH_STALE') "
    "AND heartbeat_at>=strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes') "
    "ORDER BY source_key"
).fetchall()
if rows:
    print("OUTAGE", ",".join(f"{row[0]}:{row[1]}" for row in rows))
else:
    print("OK")
PYEOF
)" || exit 0
VERDICT="${STATE%% *}"

if [ "$VERDICT" = "OK" ]; then
    rm -f "$LATCH"
    exit 0
fi

[ -f "$LATCH" ] && exit 0
if PYTHONPATH="$CODE_ROOT" "$PY" -c "
import sys
from sniper.util.envfiles import load_env_files
load_env_files('$ROOT')
from sniper.services.memecoin_telegram import send_telegram
ok, _ms = send_telegram(
    'Radar direct quote path failed: ' + sys.argv[1]
)
sys.exit(0 if ok else 1)" "$STATE" >/dev/null 2>&1; then
    date -u +%FT%TZ > "$LATCH"
fi
