#!/usr/bin/env bash
# post_deploy_checks.sh
# Run immediately after deploy (smoke check) and again after 12-24h observation.
# Usage: bash post_deploy_checks.sh [path/to/sniper_tracker.db]

set -euo pipefail

# Dependency check
if ! command -v sqlite3 &>/dev/null; then
    echo "❌ sqlite3 not found. Install with: sudo apt-get install -y sqlite3"
    exit 1
fi

DB="${1:-sniper_tracker.db}"

if [[ ! -f "$DB" ]]; then
    echo "❌ DB not found: $DB"
    exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "❌ sqlite3 not found in PATH. Install sqlite3 before running this check script."
    exit 1
fi

echo ""
echo "========================================"
echo "  Sniper Post-Deploy Health Check"
echo "  DB: $DB"
echo "  $(date)"
echo "========================================"

PASS=0
FAIL=0

check() {
    local label="$1"
    local query="$2"
    local expect="$3"   # 'nonzero', 'zero', or an exact integer
    local result
    result=$(sqlite3 "$DB" "$query" 2>/dev/null || echo "ERROR")

    if [[ "$result" == "ERROR" ]]; then
        echo "❌ SKIP  $label (query error — column may not exist yet)"
        return
    fi

    local ok=false
    case "$expect" in
        nonzero)  [[ "$result" -gt 0 ]] 2>/dev/null && ok=true ;;
        zero)     [[ "$result" -eq 0 ]] 2>/dev/null && ok=true ;;
        *)        [[ "$result" == "$expect" ]] && ok=true ;;
    esac

    if $ok; then
        echo "✅ PASS  $label  →  $result"
        ((PASS++)) || true
    else
        echo "❌ FAIL  $label  →  $result  (expected: $expect)"
        ((FAIL++)) || true
    fi
}

# ── 1. Schema migrations applied ─────────────────────────────────────────────
echo ""
echo "── 1. Schema migrations ─────────────────────────────────────────────────"

# Use PRAGMA table_info (compatible with all SQLite versions)
col_exists() {
    local tbl="$1" col="$2"
    sqlite3 "$DB" "PRAGMA table_info($tbl);" 2>/dev/null | awk -F'|' '{print $2}' | grep -qx "$col" && echo 1 || echo 0
}

for col in paper_trade_outcome paper_trade_roi paper_trade_closed_at; do
    result=$(col_exists position_signals "$col")
    if [[ "$result" == "1" ]]; then
        echo "✅ PASS  position_signals has $col  →  1"
        ((PASS++)) || true
    else
        echo "❌ FAIL  position_signals missing $col (init_db not run yet?)"
        ((FAIL++)) || true
    fi
done

result=$(col_exists paper_trades position_signal_id)
if [[ "$result" == "1" ]]; then
    echo "✅ PASS  paper_trades has position_signal_id  →  1"
    ((PASS++)) || true
else
    echo "❌ FAIL  paper_trades missing position_signal_id (init_db not run yet?)"
    ((FAIL++)) || true
fi

# ── 2. Signal attribution ─────────────────────────────────────────────────────
echo ""
echo "── 2. Signal attribution (new trades in last 24h) ───────────────────────"

TOTAL_NEW=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE opened_at > datetime('now','-24 hours');" 2>/dev/null || echo 0)

ATTRIBUTED=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE opened_at > datetime('now','-24 hours') AND position_signal_id IS NOT NULL;" 2>/dev/null || echo 0)

UNATTRIBUTED=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE opened_at > datetime('now','-24 hours') AND position_signal_id IS NULL;" 2>/dev/null || echo 0)

echo "   New paper trades (24h): $TOTAL_NEW  |  Attributed: $ATTRIBUTED  |  Unattributed: $UNATTRIBUTED"
if [[ "$TOTAL_NEW" -gt 0 && "$UNATTRIBUTED" -gt 0 ]]; then
    echo "❌ FAIL  Some new trades missing position_signal_id"
    ((FAIL++)) || true
elif [[ "$TOTAL_NEW" -eq 0 ]]; then
    echo "⚠️  SKIP  No new paper trades in last 24h yet (normal right after deploy)"
else
    echo "✅ PASS  All new paper trades have attribution"
    ((PASS++)) || true
fi

# ── 3. Feedback writeback ─────────────────────────────────────────────────────
echo ""
echo "── 3. Feedback writeback (position_signals ← closed trades) ─────────────"

if [[ $(col_exists position_signals paper_trade_outcome) == "1" ]]; then
    ATTR_CLOSED=$(sqlite3 "$DB" "
        SELECT COUNT(*) FROM paper_trades
        WHERE position_signal_id IS NOT NULL
          AND status IN ('closed','resolution_win','resolution_loss','early_exit','source_exited','stale');
    " 2>/dev/null || echo 0)

    if [[ "$ATTR_CLOSED" -eq 0 ]]; then
        echo "⚠️  SKIP  No attributed closed trades yet (cannot validate feedback writeback)"
    else
        MISSING_FEEDBACK=$(sqlite3 "$DB" "
            SELECT COUNT(*)
            FROM paper_trades pt
            LEFT JOIN position_signals ps ON ps.rowid = pt.position_signal_id
            WHERE pt.position_signal_id IS NOT NULL
              AND pt.status IN ('closed','resolution_win','resolution_loss','early_exit','source_exited','stale')
              AND (
                    ps.rowid IS NULL
                    OR ps.paper_trade_outcome IS NULL
                    OR ps.paper_trade_closed_at IS NULL
                  );
        " 2>/dev/null || echo 0)

        check "All attributed closed trades wrote feedback to position_signals" \
            "SELECT $MISSING_FEEDBACK;" \
            "zero"
    fi
else
    echo "⚠️  SKIP  paper_trade_outcome column missing — run services first to apply migrations"
fi

# ── 4. Stale auto-close ───────────────────────────────────────────────────────
echo ""
echo "── 4. Stale auto-close ──────────────────────────────────────────────────"

if [[ $(col_exists paper_trades status) == "1" ]]; then
    check "No stale-eligible open positions older than 14 days (future event markets exempt)" \
        "SELECT COUNT(*)
         FROM paper_trades pt
         LEFT JOIN market_metadata mm ON mm.condition_id = lower(pt.condition_id)
         LEFT JOIN position_signals ps ON ps.rowid = pt.position_signal_id
         WHERE pt.status='open'
           AND pt.opened_at < datetime('now','-14 days')
           AND NOT (
             COALESCE(mm.is_open, 1) = 1
             AND COALESCE(NULLIF(mm.end_date_iso,''), NULLIF(ps.end_date_iso,''), '') != ''
             AND datetime(COALESCE(NULLIF(mm.end_date_iso,''), NULLIF(ps.end_date_iso,''))) > datetime('now')
           );" \
        "zero"
else
    echo "⚠️  SKIP  paper_trades not accessible"
fi

STALE_CLOSED=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE status='stale';" 2>/dev/null || echo 0)
echo "   Positions auto-closed as stale (all time): $STALE_CLOSED"

# ── 5. PnL friction (fee deducted) ───────────────────────────────────────────
echo ""
echo "── 5. PnL friction sanity (fee-adjusted) ────────────────────────────────"

# Validate stored ROI against the fee-adjusted exit-price model.
if [[ $(col_exists paper_trades roi_percent) == "1" ]]; then
    BAD_ROI=$(sqlite3 "$DB" "
        SELECT COUNT(*) FROM paper_trades
        WHERE exit_classification='resolution_win'
          AND entry_price > 0
          AND exit_price IS NOT NULL
          AND ABS(
                roi_percent - ((((exit_price * 0.98) - entry_price) / entry_price) * 100.0)
              ) > 0.5;
    " 2>/dev/null || echo 0)
    check "Resolution-win ROI matches the 2% exit-fee model" \
        "SELECT $BAD_ROI;" \
        "zero"
else
    echo "⚠️  SKIP  paper_trades not accessible"
fi

# ── 6. Performance stats completeness ────────────────────────────────────────
echo ""
echo "── 6. Terminal status breakdown ─────────────────────────────────────────"

sqlite3 "$DB" "
    SELECT status, COUNT(*) as n, ROUND(SUM(pnl_usdc),2) as total_pnl
    FROM paper_trades
    WHERE status != 'open'
    GROUP BY status
    ORDER BY n DESC;
" 2>/dev/null | while IFS='|' read -r status n pnl; do
    echo "   $status: $n trades  PnL: \$$pnl"
done

CLOSED_STATUS_ONLY=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE status='closed';" 2>/dev/null || echo 0)
ALL_TERMINAL=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM paper_trades WHERE status IN ('closed','resolution_win','resolution_loss','early_exit','source_exited','stale');" 2>/dev/null || echo 0)

if [[ "$ALL_TERMINAL" -gt "$CLOSED_STATUS_ONLY" ]]; then
    echo "✅ PASS  Stats count all terminal statuses ($ALL_TERMINAL total, not just 'closed'=$CLOSED_STATUS_ONLY)"
    ((PASS++)) || true
elif [[ "$ALL_TERMINAL" -eq 0 ]]; then
    echo "⚠️  SKIP  No terminal trades yet"
else
    echo "⚠️  INFO  All terminal trades happen to have status='closed' ($CLOSED_STATUS_ONLY)"
    ((PASS++)) || true
fi

# ── 7. Strategy watermark sanity ──────────────────────────────────────────────
echo ""
echo "── 7. Strategy watermark sanity ─────────────────────────────────────────"

sqlite3 "$DB" "
    SELECT wallet, last_copied_at, updated_at
    FROM followed_wallets
    ORDER BY last_copied_at DESC
    LIMIT 5;
" 2>/dev/null | while IFS='|' read -r wallet ts upd; do
    echo "   ${wallet:0:10}...  last_copied_at=$ts  updated_at=$upd"
done

FUTURE_WATERMARKS=$(sqlite3 "$DB" \
    "SELECT COUNT(*) FROM followed_wallets WHERE last_copied_at > datetime('now','+1 hour');" 2>/dev/null || echo 0)
check "No watermarks set in the future (race condition symptom)" \
    "SELECT $FUTURE_WATERMARKS;" \
    "zero"

# ── 8. WS dedupe key ──────────────────────────────────────────────────────────
echo ""
echo "── 8. Low-buy trade idempotency ─────────────────────────────────────────"

check "low_buy_trades table exists" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='low_buy_trades';" \
    "1"

# ── 9. Phase B schema hardening ───────────────────────────────────────────────
echo ""
echo "── 9. Phase B schema hardening ──────────────────────────────────────────"

# market_metadata table
check "market_metadata table exists (Phase B)" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='market_metadata';" \
    "1"

# Phase B indexes
for idx in \
    idx_market_metadata_synced \
    idx_market_metadata_open_volume \
    idx_market_outcomes_resolved_at \
    idx_low_buy_wallet_ts \
    idx_low_buy_condition_ts \
    idx_candidate_last_seen \
    idx_candidate_hits \
    idx_ua_class_score \
    idx_ua_bot_match \
    idx_alerts_sent_wallet_type_ts; do
    result=$(sqlite3 "$DB" \
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='$idx';" 2>/dev/null || echo 0)
    if [[ "$result" == "1" ]]; then
        echo "✅ PASS  Index $idx exists  →  1"
        ((PASS++)) || true
    else
        echo "❌ FAIL  Index $idx missing (apply_phase_b_hardening not run?)"
        ((FAIL++)) || true
    fi
done

# Phase 2 scaffold table
check "market_outcomes table exists (Phase 2 scaffold)" \
    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='market_outcomes';" \
    "1"

# market_metadata row count (SKIP if table empty — stream may not have run yet)
MMETA_COUNT=$(sqlite3 "$DB" "SELECT COUNT(*) FROM market_metadata;" 2>/dev/null || echo 0)
if [[ "$MMETA_COUNT" -gt 0 ]]; then
    echo "✅ PASS  market_metadata populated  →  $MMETA_COUNT markets"
    ((PASS++)) || true
else
    echo "⚠️  SKIP  market_metadata is empty (stream not yet started or no refresh cycle completed)"
fi

# ── Runtime release drift, across EVERY runtime tree ──────────────────────────
# WARN, never fail. On 2026-08-26 the web was restarted and came back on a release 73 commits old:
# fresh ENV, old CODE, and a fix reported live was absent from the tree being served.
#
# ENUMERATED, NOT LISTED. The first version of this check named `web` and `memecoin` — a two-item
# list, which is the same maintained-list shape this check exists to catch. There are FIVE trees
# (hyperliquid, memecoin, opportunity, v6, web); a readdir replaces the list with the fact.
#
# SEVERITY SPLIT, the same one the funded reconciliation uses. Two LIVE trees on different releases
# is the active defect — one lane serving code another does not have. A DEAD tree that is behind is
# LATENT: nothing is wrong until someone launches it, and then it silently runs old code through a
# current pointer nobody audited. Both are reported; only the first is a warning.
DRIFT_LIVE_SHAS=""
if [[ -d .runtime ]]; then
    echo ""
    echo "── runtime trees ──"
    for TREE_DIR in .runtime/*/; do
        TREE="$(basename "$TREE_DIR")"
        CUR="$TREE_DIR/current"
        [[ -e "$CUR" ]] || { echo "   $TREE: no current pointer"; continue; }
        RESOLVED="$(readlink -f "$CUR")"; TSHA="$(basename "$RESOLVED")"
        BEHIND="$(git rev-list --count "$TSHA..origin/main" 2>/dev/null || echo '?')"
        # Match BOTH paths: a process launched through the symlink carries the SYMLINK in argv, so
        # matching only the resolved release path misses it — that is how the web read as dead.
        LIVE="$(pgrep -fc "$RESOLVED" 2>/dev/null || true)"
        LIVE_SYM="$(pgrep -fc "$(readlink -f "$TREE_DIR")/current" 2>/dev/null || true)"
        LIVE=$(( ${LIVE:-0} + ${LIVE_SYM:-0} ))
        if [[ "$LIVE" -gt 0 ]]; then
            printf "   %-12s %s  %5s behind origin/main  LIVE(%d)\n" "$TREE" "${TSHA:0:8}" "$BEHIND" "$LIVE"
            DRIFT_LIVE_SHAS="$DRIFT_LIVE_SHAS $TREE:$TSHA"
        else
            printf "   %-12s %s  %5s behind origin/main  (no live process)\n" "$TREE" "${TSHA:0:8}" "$BEHIND"
            if [[ "$BEHIND" != "?" && "$BEHIND" -gt 0 ]]; then
                echo "              latent: the next launch of this lane runs $BEHIND-commit-old code"
            fi
        fi
    done
fi

DRIFT_UNIQUE="$(echo "$DRIFT_LIVE_SHAS" | tr ' ' '\n' | grep -v '^$' | cut -d: -f2 | sort -u | wc -l)"
if [[ "${DRIFT_UNIQUE:-0}" -le 1 ]]; then
    echo "✅ PASS  every live runtime tree is on the same release"
    ((PASS++)) || true
else
    echo "⚠️  WARN  live runtime trees are on DIFFERENT releases:"
    echo "$DRIFT_LIVE_SHAS" | tr ' ' '\n' | grep -v '^$' | sed 's/^/           /'
    NEWEST="$(echo "$DRIFT_LIVE_SHAS" | tr ' ' '\n' | grep -v '^$' | cut -d: -f2 | sort -u \
        | while read -r X; do echo "$(git rev-list --count "$X..origin/main" 2>/dev/null || echo 99999) $X"; done \
        | sort -n | head -1 | awk '{print $2}')"
    for ENTRY in $DRIFT_LIVE_SHAS; do
        ESHA="${ENTRY#*:}"; ETREE="${ENTRY%%:*}"
        [[ "$ESHA" == "$NEWEST" ]] && continue
        echo "           $ETREE is missing:"
        git log --oneline "$ESHA..$NEWEST" 2>/dev/null | head -6 | sed 's/^/             /'
    done
    echo "           a restart is not a deploy — deploy the lagging tree, then verify from"
    echo "           production: readlink -f .runtime/<tree>/current, and that every serving"
    echo "           pid started AFTER the flip (argv holds the symlink, not the release)."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "  Results: $PASS passed  |  $FAIL failed"
echo "========================================"
echo ""

if [[ "$FAIL" -gt 0 ]]; then
    echo "⚠️  Issues detected — review output above before proceeding."
    exit 1
else
    echo "✅ All checks passed. Safe to continue observation window."
    exit 0
fi
