#!/usr/bin/env bash
set -euo pipefail

# Canary report for discovery + analyzer rollout.
# Usage:
#   export CANARY_START_TS=$(date +%s)
#   ./canary_report.sh
#
# Optional overrides:
#   DB=sniper_tracker.db
#   MAX_RESOLVED_OVERRIDE_PCT=10
#   MAX_DEAD_COUNT=5
#   MAX_DEAD_PCT=2
#   MAX_STUCK_PROCESSING=0
#   MAX_QUEUE_TOTAL=5000
#   MIN_ANALYZED_WALLETS_FOR_YIELD=10
#   MIN_HIGH_SIGNAL_RATE_PCT=5

DB="${DB:-sniper_tracker.db}"
CANARY_START_TS="${CANARY_START_TS:-}"
MAX_RESOLVED_OVERRIDE_PCT="${MAX_RESOLVED_OVERRIDE_PCT:-10}"
MAX_DEAD_COUNT="${MAX_DEAD_COUNT:-5}"
MAX_DEAD_PCT="${MAX_DEAD_PCT:-2}"
MAX_STUCK_PROCESSING="${MAX_STUCK_PROCESSING:-0}"
MAX_QUEUE_TOTAL="${MAX_QUEUE_TOTAL:-5000}"
MIN_ANALYZED_WALLETS_FOR_YIELD="${MIN_ANALYZED_WALLETS_FOR_YIELD:-10}"
MIN_HIGH_SIGNAL_RATE_PCT="${MIN_HIGH_SIGNAL_RATE_PCT:-5}"

if [[ -z "$CANARY_START_TS" ]]; then
  cat <<'EOF'
ERROR: CANARY_START_TS is not set.

Set it once at canary start, for example:
  export CANARY_START_TS=$(date +%s)

Then run:
  ./canary_report.sh
EOF
  exit 1
fi

if [[ ! -f "$DB" ]]; then
  echo "ERROR: DB not found: $DB"
  exit 1
fi

sql_scalar() {
  local q="$1"
  sqlite3 -noheader -batch "$DB" "$q"
}

pct() {
  local num="${1:-0}"
  local den="${2:-0}"
  awk -v n="$num" -v d="$den" 'BEGIN { if (d <= 0) { printf "0.00" } else { printf "%.2f", (100.0*n)/d } }'
}

cmp_le() {
  local a="$1"
  local b="$2"
  awk -v x="$a" -v y="$b" 'BEGIN { exit !(x <= y) }'
}

cmp_ge() {
  local a="$1"
  local b="$2"
  awk -v x="$a" -v y="$b" 'BEGIN { exit !(x >= y) }'
}

status_line() {
  local label="$1"
  local status="$2"
  local detail="$3"
  printf "  - %-34s %-6s %s\n" "$label" "$status" "$detail"
}

read -r total_events large_bet_events resolved_override_hits <<<"$(sqlite3 -separator ' ' "$DB" "
WITH p AS (SELECT ${CANARY_START_TS} AS start_ts)
SELECT
  COALESCE(COUNT(*), 0),
  COALESCE(SUM(CASE WHEN stream_type='large_bet' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN stream_type='large_bet' AND (price >= 0.95 OR price <= 0.05) THEN 1 ELSE 0 END), 0)
FROM discovery_events de, p
WHERE de.detected_at >= p.start_ts;
")"

large_bet_share_pct="$(pct "$large_bet_events" "$total_events")"
resolved_override_pct="$(pct "$resolved_override_hits" "$large_bet_events")"

read -r queue_total queued_count processing_count retry_count dead_count <<<"$(sqlite3 -separator ' ' "$DB" "
SELECT
  COALESCE(COUNT(*), 0),
  COALESCE(SUM(CASE WHEN state='queued' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN state='processing' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN state='retry' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN state='dead' THEN 1 ELSE 0 END), 0)
FROM wallet_analysis_queue;
")"

stuck_processing_expired="$(sql_scalar "
SELECT COALESCE(COUNT(*), 0)
FROM wallet_analysis_queue
WHERE state='processing'
  AND lease_expires_ts > 0
  AND lease_expires_ts <= strftime('%s','now');
")"

dead_pct="$(pct "$dead_count" "$queue_total")"

read -r large_wallets analyzed_wallets high_signal_wallets <<<"$(sqlite3 -separator ' ' "$DB" "
WITH p AS (SELECT ${CANARY_START_TS} AS start_ts),
large_wallets AS (
  SELECT DISTINCT de.wallet
  FROM discovery_events de, p
  WHERE de.stream_type='large_bet'
    AND de.detected_at >= p.start_ts
)
SELECT
  COALESCE(COUNT(*), 0),
  COALESCE(SUM(CASE WHEN ua.wallet IS NOT NULL THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN ua.classification IN ('insider_candidate','emerging_insider') THEN 1 ELSE 0 END), 0)
FROM large_wallets lw
LEFT JOIN user_analysis ua ON ua.wallet = lw.wallet;
")"

high_signal_rate_pct="$(pct "$high_signal_wallets" "$analyzed_wallets")"

echo
echo "=== Canary Report ==="
echo "DB: $DB"
echo "Canary start ts: $CANARY_START_TS"
echo "Report ts: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo

echo "[1] New events by stream_type (since canary start)"
sqlite3 -header -column "$DB" "
WITH p AS (SELECT ${CANARY_START_TS} AS start_ts)
SELECT
  de.stream_type,
  COUNT(*) AS events,
  SUM(CASE WHEN de.quick_score > 0 THEN 1 ELSE 0 END) AS score_gt_0,
  ROUND(AVG(de.quick_score), 2) AS avg_quick_score
FROM discovery_events de, p
WHERE de.detected_at >= p.start_ts
GROUP BY de.stream_type
ORDER BY events DESC;
"
echo

echo "[2] Large_bet share of total new events"
printf "  large_bet_events=%s total_events=%s share_pct=%s%%\n" \
  "$large_bet_events" "$total_events" "$large_bet_share_pct"
echo

echo "[3] Resolved-override hits (large_bet with price >= 0.95 or <= 0.05)"
printf "  resolved_override_hits=%s total_large_bet=%s resolved_override_pct=%s%%\n" \
  "$resolved_override_hits" "$large_bet_events" "$resolved_override_pct"
echo

echo "[4] Queue growth / health"
printf "  total=%s queued=%s processing=%s retry=%s dead=%s dead_pct=%s%% stuck_processing_expired_lease=%s\n" \
  "$queue_total" "$queued_count" "$processing_count" "$retry_count" "$dead_count" "$dead_pct" "$stuck_processing_expired"
echo

echo "[5] Analyzer yield from large_bet wallets"
printf "  large_bet_wallets=%s analyzed_wallets=%s high_signal_wallets=%s high_signal_rate_pct=%s%%\n" \
  "$large_wallets" "$analyzed_wallets" "$high_signal_wallets" "$high_signal_rate_pct"
echo
sqlite3 -header -column "$DB" "
WITH p AS (SELECT ${CANARY_START_TS} AS start_ts),
large_wallets AS (
  SELECT DISTINCT de.wallet
  FROM discovery_events de, p
  WHERE de.stream_type='large_bet'
    AND de.detected_at >= p.start_ts
)
SELECT
  COALESCE(ua.classification, '(not_analyzed)') AS classification,
  COUNT(*) AS wallets
FROM large_wallets lw
LEFT JOIN user_analysis ua ON ua.wallet = lw.wallet
GROUP BY classification
ORDER BY wallets DESC;
"
echo

echo "=== Compact Pass/Fail Summary ==="

overall_fail=0
warn_count=0

if cmp_le "$queue_total" "$MAX_QUEUE_TOTAL"; then
  status_line "Queue size <= MAX_QUEUE_TOTAL" "PASS" "(queue_total=${queue_total}, max=${MAX_QUEUE_TOTAL})"
else
  status_line "Queue size <= MAX_QUEUE_TOTAL" "FAIL" "(queue_total=${queue_total}, max=${MAX_QUEUE_TOTAL})"
  overall_fail=1
fi

if cmp_le "$dead_count" "$MAX_DEAD_COUNT" && cmp_le "$dead_pct" "$MAX_DEAD_PCT"; then
  status_line "Dead state near zero" "PASS" "(dead=${dead_count}, dead_pct=${dead_pct}%)"
else
  status_line "Dead state near zero" "FAIL" "(dead=${dead_count}, dead_pct=${dead_pct}%)"
  overall_fail=1
fi

if cmp_le "$stuck_processing_expired" "$MAX_STUCK_PROCESSING"; then
  status_line "No expired processing leases" "PASS" "(stuck=${stuck_processing_expired})"
else
  status_line "No expired processing leases" "FAIL" "(stuck=${stuck_processing_expired})"
  overall_fail=1
fi

if [[ "$large_bet_events" -eq 0 ]]; then
  status_line "Resolved override minority" "WARN" "(no large_bet events yet)"
  warn_count=$((warn_count + 1))
elif cmp_le "$resolved_override_pct" "$MAX_RESOLVED_OVERRIDE_PCT"; then
  status_line "Resolved override minority" "PASS" "(resolved_override_pct=${resolved_override_pct}%, max=${MAX_RESOLVED_OVERRIDE_PCT}%)"
else
  status_line "Resolved override minority" "FAIL" "(resolved_override_pct=${resolved_override_pct}%, max=${MAX_RESOLVED_OVERRIDE_PCT}%)"
  overall_fail=1
fi

if [[ "$analyzed_wallets" -lt "$MIN_ANALYZED_WALLETS_FOR_YIELD" ]]; then
  status_line "Large_bet high-signal yield" "WARN" \
    "(insufficient sample: analyzed=${analyzed_wallets}, min=${MIN_ANALYZED_WALLETS_FOR_YIELD})"
  warn_count=$((warn_count + 1))
elif cmp_ge "$high_signal_rate_pct" "$MIN_HIGH_SIGNAL_RATE_PCT"; then
  status_line "Large_bet high-signal yield" "PASS" \
    "(rate=${high_signal_rate_pct}%, min=${MIN_HIGH_SIGNAL_RATE_PCT}%)"
else
  status_line "Large_bet high-signal yield" "FAIL" \
    "(rate=${high_signal_rate_pct}%, min=${MIN_HIGH_SIGNAL_RATE_PCT}%)"
  overall_fail=1
fi

echo
if [[ "$overall_fail" -eq 0 ]]; then
  if [[ "$warn_count" -gt 0 ]]; then
    echo "OVERALL: PASS (with ${warn_count} warning(s))"
  else
    echo "OVERALL: PASS"
  fi
else
  echo "OVERALL: FAIL"
fi
echo
