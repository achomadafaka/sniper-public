"""
Position signal management, convergence detection, and market intelligence.
"""
import logging
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Sequence

from sniper.persistence.db_utils import _classify_title_domain, _normalize_wallet

logger = logging.getLogger(__name__)


def _retry_on_locked(
    fn,
    *,
    max_attempts: int = 5,
    delay_seconds: float = 10.0,
    op_name: str = "",
    conn: "sqlite3.Connection | None" = None,
):
    """Retry fn() up to max_attempts times on SQLite 'database is locked'.

    Uses time.sleep — safe for synchronous callers only.
    Each attempt already waits up to busy_timeout (30 s) inside SQLite itself.
    Total worst-case tolerance: max_attempts × (30 s + delay_seconds).

    ``op_name`` is an optional identifier (e.g. ``"commit_wallet_analysis:0x..."``)
    that is attached to retry log lines so WAL-bloat investigations can tell
    which operation is wedging the write lock. When a retry occurs, the final
    attempt count is also logged so callers can sum per-wallet retry burden.

    ``conn`` is an optional SQLite connection. When provided, on every locked
    failure we check ``conn.in_transaction`` and roll back any partial write
    transaction BEFORE sleeping/retrying. Without this, a closure that started
    or joined a transaction and then hit a lock mid-write can leave the
    connection in ``in_transaction=True`` across the retry window — which
    anchors WAL growth and cascades into further ``database is locked`` on
    unrelated writes. This was the pathology behind the 2026-04-20 analyzer
    incident (80s slow_wallet with in_transaction=True). Pass ``conn=conn``
    whenever the closure writes through a known connection.
    """
    tag = f" op={op_name}" if op_name else ""
    for attempt in range(1, max_attempts + 1):
        try:
            result = fn()
            if attempt > 1:
                logger.info(
                    "SQLite locked cleared after %d attempts%s",
                    attempt, tag,
                )
            return result
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= max_attempts:
                if attempt >= max_attempts:
                    logger.error(
                        "SQLite locked EXHAUSTED after %d/%d attempts%s: %s",
                        attempt, max_attempts, tag, exc,
                    )
                raise
            # Before sleeping, clear any stray open transaction on the caller's
            # connection so the retry starts from a clean state. Without this,
            # a leaked BEGIN can cascade into subsequent "locked" errors.
            if conn is not None:
                try:
                    if getattr(conn, "in_transaction", False):
                        try:
                            conn.rollback()
                            logger.warning(
                                "retry_on_locked: rolled back stray transaction before retry "
                                "(attempt %d/%d)%s",
                                attempt, max_attempts, tag,
                            )
                        except Exception as rb_exc:
                            logger.exception(
                                "retry_on_locked: rollback failed before retry%s: %s",
                                tag, rb_exc,
                            )
                except Exception:
                    logger.exception(
                        "retry_on_locked: unexpected error checking in_transaction%s",
                        tag,
                    )
            logger.warning(
                "SQLite locked (attempt %d/%d), retrying in %.0fs%s: %s",
                attempt, max_attempts, delay_seconds, tag, exc,
            )
            time.sleep(delay_seconds)


# Public alias so db.py's `from db_signals import *` re-exports it.
retry_on_locked = _retry_on_locked


def upsert_position_signal(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    title: str = "",
    event_slug: str = "",
    is_open: bool = True,
    position_value: float = 0.0,
    position_share: float = 0.0,
    position_count_wallet: int = 0,
    entry_anchor_ts: int = 0,
    entry_price: float = 0.0,
    entry_size: float = 0.0,
    entry_notional: float = 0.0,
    current_price: float = 0.0,
    current_size: float = 0.0,
    latest_trade_ts: int = 0,
    buys_5m: int = 0,
    buys_1h: int = 0,
    buys_24h: int = 0,
    trades_5m: int = 0,
    trades_1h: int = 0,
    trades_24h: int = 0,
    sells_5m: int = 0,
    sells_1h: int = 0,
    sells_24h: int = 0,
    notional_5m: float = 0.0,
    notional_1h: float = 0.0,
    notional_24h: float = 0.0,
    sell_notional_5m: float = 0.0,
    sell_notional_1h: float = 0.0,
    sell_notional_24h: float = 0.0,
    buys_since_entry: int = 0,
    sells_since_entry: int = 0,
    buy_notional_since_entry: float = 0.0,
    sell_notional_since_entry: float = 0.0,
    net_notional_24h: float = 0.0,
    net_notional_since_entry: float = 0.0,
    last_trade_side: str = "",
    activity_state: str = "holding",
    hold_hours: float = 0.0,
    unique_sizes_5m: int = 0,
    max_trades_60s: int = 0,
    position_insider_score: float = 0.0,
    position_speculator_score: float = 0.0,
    position_classification: str = "position_low_signal",
    signal_reason: str = "",
    analyzed_ts: int = 0,
    analysis_version: int = 0,
    end_date_iso: str | None = None,
) -> None:
    wallet = _normalize_wallet(wallet)
    condition_id = str(condition_id or "").strip().lower()
    outcome = str(outcome or "").strip().lower()
    if not wallet or not condition_id:
        return

    # Guard: reject signals from blocked wallets entirely.
    # Lazy import to avoid circular dependency (is_wallet_blocked lives in db.py).
    from db import is_wallet_blocked
    if is_wallet_blocked(conn, wallet):
        return

    # Guard: only allow signals from wallets that fit the "signals table" thesis:
    # Signals = wallets trying to predict event outcomes (potential insiders / market pros).
    # ROI traders (speculator_bot) belong on the leaderboard only — not here.
    #
    # Rules:
    #   - insider_candidate, emerging_insider, high_winrate_review → always allowed
    #   - low_signal → only if position_insider_score >= 60 AND qualifying_roi >= 10%
    #   - speculator_bot → NEVER (leaderboard only)
    #   - is_bot = 1 → NEVER (hedging bots, arb bots, etc.)
    #   - blocked → NEVER (already handled above)
    #   - anything else / unknown → allowed (new wallet, not yet classified)
    ua_row = conn.execute(
        "SELECT classification, is_bot, qualifying_roi, insider_score FROM user_analysis WHERE wallet=?",
        (wallet,),
    ).fetchone()
    if ua_row is not None:
        _classification = ua_row[0] or ""
        _is_bot = ua_row[1] or 0
        _wallet_qroi = ua_row[2]

        # Signal storage is permissive — store signals from all wallet types so they're
        # visible on the dashboard for analysis. Trading decisions (which signals to act on)
        # are made downstream by the paper trader with its own gates.
        # Previously this blocked is_bot, speculator_bot, low_signal without ROI, etc.
        # That created invisible signals that the system generated but never stored.

    # Guard: reject corrupt signals (no valid entry timestamp).
    # Price filtering is handled downstream by the paper trader — the DB should
    # store ALL scored signals for visibility and analysis regardless of price.
    # Previous gate at 0.80 was blocking high-conviction insider positions from
    # ever being stored, making them invisible to both trading and analytics.
    _entry_anchor_ts = int(entry_anchor_ts or 0)
    if _entry_anchor_ts == 0:
        return

    def _do_upsert():
        conn.execute(
            """
        INSERT INTO position_signals (
            wallet,
            condition_id,
            outcome,
            title,
            event_slug,
            is_open,
            position_value,
            position_share,
            position_count_wallet,
            entry_anchor_ts,
            entry_price,
            entry_size,
            entry_notional,
            current_price,
            current_size,
            latest_trade_ts,
            buys_5m,
            buys_1h,
            buys_24h,
            trades_5m,
            trades_1h,
            trades_24h,
            sells_5m,
            sells_1h,
            sells_24h,
            notional_5m,
            notional_1h,
            notional_24h,
            sell_notional_5m,
            sell_notional_1h,
            sell_notional_24h,
            buys_since_entry,
            sells_since_entry,
            buy_notional_since_entry,
            sell_notional_since_entry,
            net_notional_24h,
            net_notional_since_entry,
            last_trade_side,
            activity_state,
            hold_hours,
            unique_sizes_5m,
            max_trades_60s,
            position_insider_score,
            position_speculator_score,
            position_classification,
            signal_reason,
            analyzed_ts,
            analysis_version,
            end_date_iso
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet, condition_id, outcome) DO UPDATE SET
            title = excluded.title,
            event_slug = excluded.event_slug,
            is_open = excluded.is_open,
            position_value = excluded.position_value,
            position_share = excluded.position_share,
            position_count_wallet = excluded.position_count_wallet,
            entry_anchor_ts = excluded.entry_anchor_ts,
            entry_price = excluded.entry_price,
            entry_size = excluded.entry_size,
            entry_notional = excluded.entry_notional,
            current_price = excluded.current_price,
            current_size = excluded.current_size,
            latest_trade_ts = excluded.latest_trade_ts,
            buys_5m = excluded.buys_5m,
            buys_1h = excluded.buys_1h,
            buys_24h = excluded.buys_24h,
            trades_5m = excluded.trades_5m,
            trades_1h = excluded.trades_1h,
            trades_24h = excluded.trades_24h,
            sells_5m = excluded.sells_5m,
            sells_1h = excluded.sells_1h,
            sells_24h = excluded.sells_24h,
            notional_5m = excluded.notional_5m,
            notional_1h = excluded.notional_1h,
            notional_24h = excluded.notional_24h,
            sell_notional_5m = excluded.sell_notional_5m,
            sell_notional_1h = excluded.sell_notional_1h,
            sell_notional_24h = excluded.sell_notional_24h,
            buys_since_entry = excluded.buys_since_entry,
            sells_since_entry = excluded.sells_since_entry,
            buy_notional_since_entry = excluded.buy_notional_since_entry,
            sell_notional_since_entry = excluded.sell_notional_since_entry,
            net_notional_24h = excluded.net_notional_24h,
            net_notional_since_entry = excluded.net_notional_since_entry,
            last_trade_side = excluded.last_trade_side,
            activity_state = excluded.activity_state,
            hold_hours = excluded.hold_hours,
            unique_sizes_5m = excluded.unique_sizes_5m,
            max_trades_60s = excluded.max_trades_60s,
            position_insider_score = excluded.position_insider_score,
            position_speculator_score = excluded.position_speculator_score,
            position_classification = excluded.position_classification,
            signal_reason = excluded.signal_reason,
            analyzed_ts = excluded.analyzed_ts,
            analysis_version = excluded.analysis_version,
            end_date_iso = COALESCE(excluded.end_date_iso, end_date_iso),
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            condition_id,
            outcome,
            str(title or ""),
            str(event_slug or ""),
            1 if is_open else 0,
            float(position_value or 0.0),
            float(position_share or 0.0),
            int(position_count_wallet or 0),
            int(entry_anchor_ts or 0),
            float(entry_price or 0.0),
            float(entry_size or 0.0),
            float(entry_notional or 0.0),
            float(current_price or 0.0),
            float(current_size or 0.0),
            int(latest_trade_ts or 0),
            int(buys_5m or 0),
            int(buys_1h or 0),
            int(buys_24h or 0),
            int(trades_5m or 0),
            int(trades_1h or 0),
            int(trades_24h or 0),
            int(sells_5m or 0),
            int(sells_1h or 0),
            int(sells_24h or 0),
            float(notional_5m or 0.0),
            float(notional_1h or 0.0),
            float(notional_24h or 0.0),
            float(sell_notional_5m or 0.0),
            float(sell_notional_1h or 0.0),
            float(sell_notional_24h or 0.0),
            int(buys_since_entry or 0),
            int(sells_since_entry or 0),
            float(buy_notional_since_entry or 0.0),
            float(sell_notional_since_entry or 0.0),
            float(net_notional_24h or 0.0),
            float(net_notional_since_entry or 0.0),
            str(last_trade_side or ""),
            str(activity_state or "holding"),
            -1.0 if (hold_hours is None or float(hold_hours) >= 99998.0) else float(hold_hours),
            int(unique_sizes_5m or 0),
            int(max_trades_60s or 0),
            float(position_insider_score or 0.0),
            float(position_speculator_score or 0.0),
            str(position_classification or "position_low_signal"),
            str(signal_reason or ""),
            int(analyzed_ts or time.time()),
            int(analysis_version or 0),
            str(end_date_iso) if end_date_iso else None,
        ),
        )
    _retry_on_locked(_do_upsert)


def mark_wallet_position_signals_open_state(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    open_keys: Iterable[tuple[str, str]],
    analyzed_ts: int,
) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return

    key_set = {
        f"{str(condition_id or '').strip().lower()}::{str(outcome or '').strip().lower()}"
        for condition_id, outcome in open_keys
        if str(condition_id or "").strip()
    }

    if not key_set:
        _retry_on_locked(lambda: conn.execute(
            """
            UPDATE position_signals
            SET is_open = 0, analyzed_ts = ?, updated_at = CURRENT_TIMESTAMP
            WHERE wallet = ?
            """,
            (int(analyzed_ts or time.time()), wallet),
        ))
        return

    placeholders = ",".join("?" for _ in key_set)
    params = [int(analyzed_ts or time.time()), wallet, *sorted(key_set)]
    _retry_on_locked(lambda: conn.execute(
        f"""
        UPDATE position_signals
        SET is_open = 0, analyzed_ts = ?, updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
          AND (condition_id || '::' || outcome) NOT IN ({placeholders})
        """,
        params,
    ))

    # Auto-close any open signals that have drifted to near-certainty prices
    # (current_price > 0.80) or have no valid entry timestamp — these carry no
    # actionable alpha regardless of which wallet opened them.
    _retry_on_locked(lambda: conn.execute(
        """
        UPDATE position_signals
           SET is_open = 0, updated_at = CURRENT_TIMESTAMP
         WHERE wallet = ?
           AND is_open = 1
           AND (current_price >= 0.80 OR entry_anchor_ts = 0)
        """,
        (wallet,),
    ))


def position_signals(
    conn: sqlite3.Connection,
    *,
    wallet: str | None = None,
    classification: str | None = None,
    min_insider_score: float | None = None,
    max_entry_price: float | None = None,
    limit: int = 1000,
    open_only: bool = True,
) -> List[sqlite3.Row]:
    where = ["1=1"]
    params: List[Any] = []
    if wallet:
        where.append("ps.wallet = ?")
        params.append(_normalize_wallet(wallet))
    if classification:
        where.append("ps.position_classification = ?")
        params.append(str(classification or "").strip())
    if min_insider_score is not None:
        where.append("ps.position_insider_score >= ?")
        params.append(float(min_insider_score))
    if max_entry_price is not None:
        where.append("COALESCE(ps.entry_price, 0) <= ?")
        params.append(float(max_entry_price))
    if open_only:
        where.append("ps.is_open = 1")
        # Hard filter: keep open-signal feeds actionable by excluding resolved
        # or explicitly closed markets when metadata is available.
        where.append("COALESCE(mm.is_open, 1) = 1")
        where.append(
            "("
            "COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso, '') = '' "
            "OR datetime(COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso)) > datetime('now')"
            ")"
        )

    params.append(int(limit))
    return conn.execute(
        f"""
        SELECT
            ps.*,
            COALESCE(NULLIF(mm.title, ''), ps.title) AS market_title,
            COALESCE(NULLIF(mm.event_slug, ''), ps.event_slug) AS market_event_slug,
            COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso) AS market_end_date_iso,
            COALESCE(mm.volume_24h, 0.0) AS market_volume_24h,
            COALESCE(mm.is_open, 1) AS market_is_open
        FROM position_signals ps
        LEFT JOIN market_metadata mm
          ON mm.condition_id = ps.condition_id
        WHERE {" AND ".join(where)}
        ORDER BY
            ps.position_insider_score DESC,
            ps.position_value DESC,
            ps.buys_1h DESC,
            ps.analyzed_ts DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def wallet_resolved_outcome_stats(
    conn: sqlite3.Connection,
    wallets: Sequence[str],
    *,
    max_entry_price: float = 0.80,
) -> Dict[str, Dict[str, Any]]:
    """Compute per-wallet resolved W/L stats from low_buy_trades + market_outcomes.

    Stats are scoped to qualifying trades:
    - BUY fills only
    - entry price < max_entry_price
    - single-sided per condition (no YES/NO hedge)
    - aggregate position notional >= $1000
    """
    normalized_wallets = sorted({_normalize_wallet(w) for w in wallets if _normalize_wallet(w)})
    if not normalized_wallets:
        return {}

    has_outcomes = conn.execute("SELECT 1 FROM market_outcomes LIMIT 1").fetchone()
    if not has_outcomes:
        return {}

    placeholders = ",".join("?" for _ in normalized_wallets)
    query = f"""
        WITH valid_resolved_conditions AS (
            SELECT
                LOWER(COALESCE(mo.condition_id, '')) AS condition_id
            FROM market_outcomes mo
            WHERE COALESCE(mo.resolved_at, 0) > 0
              AND COALESCE(mo.resolved_at, 0) <= CAST(strftime('%s', 'now') AS INTEGER)
              AND COALESCE(mo.resolved_price, -1) >= 0.0
              AND COALESCE(mo.resolved_price, -1) <= 1.0
            GROUP BY LOWER(COALESCE(mo.condition_id, ''))
            HAVING SUM(CASE WHEN COALESCE(mo.resolved_price, 0) >= 0.999 THEN 1 ELSE 0 END) = 1
        ),
        qualifying_trades AS (
            SELECT
                LOWER(t.proxy_wallet) AS wallet,
                LOWER(COALESCE(t.condition_id, '')) AS condition_id,
                LOWER(TRIM(COALESCE(t.outcome, ''))) AS outcome,
                t.price,
                t.size,
                t.notional
            FROM low_buy_trades t
            WHERE LOWER(t.proxy_wallet) IN ({placeholders})
              AND UPPER(COALESCE(t.side, 'BUY')) = 'BUY'
              AND COALESCE(t.price, 0) > 0
              AND COALESCE(t.price, 0) < ?
              AND COALESCE(t.size, 0) > 0
        ),
        single_sided_conditions AS (
            SELECT wallet, condition_id
            FROM qualifying_trades
            GROUP BY wallet, condition_id
            HAVING COUNT(DISTINCT outcome) = 1
        ),
        qualifying_positions AS (
            SELECT
                qt.wallet,
                qt.condition_id,
                qt.outcome,
                SUM(qt.size) AS entry_size,
                SUM(qt.notional) AS entry_notional,
                CASE
                    WHEN SUM(qt.size) > 0 THEN SUM(qt.notional) / SUM(qt.size)
                    ELSE NULL
                END AS avg_entry_price
            FROM qualifying_trades qt
            INNER JOIN single_sided_conditions ss
              ON ss.wallet = qt.wallet
             AND ss.condition_id = qt.condition_id
            GROUP BY qt.wallet, qt.condition_id, qt.outcome
            HAVING SUM(qt.notional) >= 1000
        ),
        resolved_positions AS (
            SELECT
                qp.wallet,
                ((mo.resolved_price - qp.avg_entry_price) * qp.entry_size) AS pnl_usdc
            FROM qualifying_positions qp
            INNER JOIN valid_resolved_conditions vc
              ON vc.condition_id = qp.condition_id
            INNER JOIN market_outcomes mo
              ON LOWER(COALESCE(mo.condition_id, '')) = qp.condition_id
             AND LOWER(TRIM(COALESCE(mo.outcome, ''))) = qp.outcome
             AND COALESCE(mo.resolved_at, 0) > 0
             AND COALESCE(mo.resolved_at, 0) <= CAST(strftime('%s', 'now') AS INTEGER)
             AND COALESCE(mo.resolved_price, -1) >= 0.0
             AND COALESCE(mo.resolved_price, -1) <= 1.0
        )
        SELECT
            wallet,
            SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN pnl_usdc < 0 THEN 1 ELSE 0 END) AS losses,
            COUNT(*) AS closed_count,
            CASE
                WHEN COUNT(*) > 0
                THEN CAST(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) AS REAL) / CAST(COUNT(*) AS REAL)
                ELSE NULL
            END AS win_rate
        FROM resolved_positions
        GROUP BY wallet
    """
    rows = conn.execute(
        query,
        [*normalized_wallets, float(max_entry_price)],
    ).fetchall()

    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        wallet = str(row["wallet"] or "")
        out[wallet] = {
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "closed_count": int(row["closed_count"] or 0),
            "win_rate": float(row["win_rate"]) if row["win_rate"] is not None else None,
            "source": "market_outcomes_phase2",
        }
    return out


def thesis_signal_rows(
    conn: sqlite3.Connection,
    *,
    window_hours: int = 48,
    max_entry_price: float = 0.50,
    min_position_value: float = 100000.0,
    max_account_age_days: float = 14.0,
    max_lifetime_positions: int = 5,
    allowed_categories: Sequence[str] | None = ("politics", "sports"),
    exclude_speculator_bots: bool = True,
    single_market_only: bool = True,
    limit: int = 200,
) -> List[sqlite3.Row]:
    """Return active position signals that satisfy the strict thesis gates."""
    safe_window_hours = max(1, int(window_hours))
    safe_limit = max(1, int(limit))
    categories = [
        str(cat or "").strip().lower()
        for cat in (allowed_categories or [])
        if str(cat or "").strip()
    ]

    where = [
        "base.market_is_open = 1",
        "base.market_end_date_iso != ''",
        "datetime(base.market_end_date_iso) > datetime('now')",
        "datetime(base.market_end_date_iso) <= datetime('now', ?)",
        "base.entry_price < ?",
        "base.position_value >= ?",
        "base.account_age_days < ?",
        "base.lifetime_position_count <= ?",
        "base.entry_anchor_ts > 0",
        "(strftime('%s', base.market_end_date_iso) - base.entry_anchor_ts) BETWEEN 0 AND ?",
    ]
    params: List[Any] = [
        f"+{safe_window_hours} hours",
        float(max_entry_price),
        float(min_position_value),
        float(max_account_age_days),
        int(max_lifetime_positions),
        int(safe_window_hours * 3600),
    ]

    if single_market_only:
        where.append("base.open_positions_for_wallet = 1")
    if exclude_speculator_bots:
        where.append("COALESCE(base.wallet_classification, '') != 'speculator_bot'")
        where.append("COALESCE(base.wallet_is_bot, 0) = 0")
    if categories:
        placeholders = ",".join("?" for _ in categories)
        where.append(f"LOWER(COALESCE(base.market_category, '')) IN ({placeholders})")
        params.extend(categories)

    params.append(safe_limit)
    return conn.execute(
        f"""
        WITH base AS (
            SELECT
                ps.*,
                COALESCE(NULLIF(mm.title, ''), ps.title) AS market_title,
                COALESCE(NULLIF(mm.event_slug, ''), ps.event_slug) AS market_event_slug,
                COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso) AS market_end_date_iso,
                COALESCE(mm.volume_24h, 0.0) AS market_volume_24h,
                COALESCE(mm.is_open, 1) AS market_is_open,
                COALESCE(mm.category, '') AS market_category,
                COALESCE(mm.category_source, '') AS market_category_source,
                COALESCE(ua.account_age_days, 999999.0) AS account_age_days,
                COALESCE(ua.position_count, 999999) AS lifetime_position_count,
                COALESCE(ua.trade_count, 0) AS lifetime_trade_count,
                COALESCE(ua.classification, '') AS wallet_classification,
                COALESCE(ua.is_bot, 0) AS wallet_is_bot,
                COALESCE(ua.win_rate, 0.0) AS wallet_win_rate,
                COALESCE(ua.wins, 0) AS wallet_wins,
                COALESCE(ua.losses, 0) AS wallet_losses,
                COALESCE(ua.closed_sample_size, 0) AS wallet_closed_sample_size,
                COALESCE(ua.qualifying_roi, NULL) AS wallet_qualifying_roi,
                CASE
                    WHEN ps.entry_anchor_ts > 0
                     AND COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso) != ''
                    THEN (strftime('%s', COALESCE(NULLIF(mm.end_date_iso, ''), ps.end_date_iso)) - ps.entry_anchor_ts) / 3600.0
                    ELSE NULL
                END AS hours_to_end_at_entry,
                COUNT(*) OVER (PARTITION BY ps.wallet) AS open_positions_for_wallet
            FROM position_signals ps
            LEFT JOIN market_metadata mm
              ON mm.condition_id = ps.condition_id
            LEFT JOIN user_analysis ua
              ON ua.wallet = ps.wallet
            WHERE ps.is_open = 1
        )
        SELECT *
        FROM base
        WHERE {" AND ".join(where)}
        ORDER BY
            base.position_value DESC,
            base.position_insider_score DESC,
            base.entry_anchor_ts DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def thesis_candidate_rows(
    conn: sqlite3.Connection,
    *,
    window_hours: int = 48,
    max_entry_price: float = 0.50,
    candidate_min_position_value: float = 3000.0,
    strict_min_position_value: float = 10000.0,
    max_account_age_days: float = 14.0,
    max_lifetime_positions: int = 5,
    allowed_categories: Sequence[str] | None = ("politics", "sports"),
    exclude_speculator_bots: bool = True,
    single_market_only: bool = True,
    limit: int = 50,
) -> List[sqlite3.Row]:
    """Return near-miss thesis candidates (pass all gates except strict size gate).

    Candidate definition:
    - pass the same hard gates as thesis_signal_rows(), but with a relaxed minimum
      position value (candidate_min_position_value), then
    - fail the strict position value gate (strict_min_position_value).
    """
    safe_limit = max(1, min(int(limit or 50), 50))
    relaxed_min = float(candidate_min_position_value or 0.0)
    strict_min = float(strict_min_position_value or 0.0)
    if strict_min <= relaxed_min:
        return []

    # Overfetch because strict matches can occupy the top of the ordered set.
    # We filter to near-misses after query and then enforce the final limit.
    base_rows = thesis_signal_rows(
        conn,
        window_hours=window_hours,
        max_entry_price=max_entry_price,
        min_position_value=relaxed_min,
        max_account_age_days=max_account_age_days,
        max_lifetime_positions=max_lifetime_positions,
        allowed_categories=allowed_categories,
        exclude_speculator_bots=exclude_speculator_bots,
        single_market_only=single_market_only,
        limit=max(200, safe_limit * 10),
    )

    candidates = [
        row
        for row in base_rows
        if float(row["position_value"] or 0.0) < strict_min
    ]
    return candidates[:safe_limit]


def position_signal_summary_for_wallets(
    conn: sqlite3.Connection,
    wallets: Iterable[str],
    *,
    open_only: bool = True,
) -> Dict[str, Dict[str, float]]:
    normalized: List[str] = []
    seen: set[str] = set()
    for wallet in wallets:
        key = _normalize_wallet(wallet)
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)
    if not normalized:
        return {}

    placeholders = ",".join("?" for _ in normalized)
    where_open = "AND is_open = 1" if open_only else ""
    rows = conn.execute(
        f"""
        SELECT
            wallet,
            COUNT(*) AS position_signal_count,
            SUM(CASE WHEN position_classification = 'position_insider_candidate' THEN 1 ELSE 0 END)
                AS position_insider_candidate_count,
            SUM(CASE WHEN position_classification = 'position_speculator_ladder' THEN 1 ELSE 0 END)
                AS position_speculator_count,
            COALESCE(MAX(position_insider_score), 0) AS max_position_insider_score,
            COALESCE(MAX(position_speculator_score), 0) AS max_position_speculator_score
        FROM position_signals
        WHERE wallet IN ({placeholders})
        {where_open}
        GROUP BY wallet
        """,
        normalized,
    ).fetchall()
    out: Dict[str, Dict[str, float]] = {}
    for row in rows:
        key = str(row["wallet"] or "")
        out[key] = {
            "position_signal_count": float(row["position_signal_count"] or 0.0),
            "position_insider_candidate_count": float(row["position_insider_candidate_count"] or 0.0),
            "position_speculator_count": float(row["position_speculator_count"] or 0.0),
            "max_position_insider_score": float(row["max_position_insider_score"] or 0.0),
            "max_position_speculator_score": float(row["max_position_speculator_score"] or 0.0),
        }
    return out


def market_convergence_signals(
    conn,
    min_insider_score: float = 55.0,
    min_wallets: int = 2,
    open_only: bool = True,
    limit: int = 50,
) -> list:
    """Return markets where multiple insider wallets are positioned on the same side.

    Each row is a market-level aggregate: insider_count, avg/max insider score,
    total capital, wallet list, and market metadata.
    """
    open_clause = "AND ps.is_open = 1" if open_only else ""
    rows = conn.execute(
        f"""
        SELECT
            ps.condition_id,
            ps.outcome,
            COALESCE(mm.title, MAX(ps.title)) AS title,
            COUNT(DISTINCT ps.wallet) AS insider_count,
            ROUND(AVG(ps.position_insider_score), 1) AS avg_insider_score,
            ROUND(MAX(ps.position_insider_score), 1) AS max_insider_score,
            ROUND(SUM(ps.entry_notional), 2) AS total_capital,
            ROUND(AVG(ps.entry_price), 4) AS avg_entry_price,
            ROUND(AVG(ps.current_price), 4) AS avg_current_price,
            GROUP_CONCAT(DISTINCT ps.wallet) AS wallet_list,
            mm.end_date_iso,
            mm.category,
            mm.event_slug,
            MAX(ps.analyzed_ts) AS latest_analyzed_ts
        FROM position_signals ps
        LEFT JOIN market_metadata mm ON mm.condition_id = ps.condition_id
        WHERE ps.position_insider_score >= ?
            {open_clause}
        GROUP BY ps.condition_id, ps.outcome
        HAVING COUNT(DISTINCT ps.wallet) >= ?
        ORDER BY insider_count DESC, avg_insider_score DESC
        LIMIT ?
        """,
        (min_insider_score, min_wallets, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def market_convergence_detail(
    conn,
    condition_id: str,
    outcome: str | None = None,
    min_insider_score: float = 0.0,
) -> list:
    """Return per-wallet detail for a specific convergence market."""
    outcome_clause = "AND UPPER(ps.outcome) = UPPER(?)" if outcome else ""
    params: list = [condition_id]
    if outcome:
        params.append(outcome)
    params.append(min_insider_score)
    rows = conn.execute(
        f"""
        SELECT
            ps.wallet,
            ps.condition_id,
            ps.outcome,
            ps.title,
            ps.is_open,
            ps.entry_price,
            ps.entry_notional,
            ps.current_price,
            ps.position_value,
            ps.position_insider_score,
            ps.position_speculator_score,
            ps.position_classification,
            ps.entry_anchor_ts,
            ps.analyzed_ts,
            ua.win_rate,
            ua.qualifying_roi,
            ua.classification AS wallet_classification,
            ua.tier,
            wp.username,
            wp.pseudonym,
            wp.profile_image
        FROM position_signals ps
        LEFT JOIN user_analysis ua ON ua.wallet = ps.wallet
        LEFT JOIN wallet_profiles wp ON wp.wallet = ps.wallet
        WHERE ps.condition_id = ?
            {outcome_clause}
            AND ps.position_insider_score >= ?
            AND ps.is_open = 1
        ORDER BY ps.position_insider_score DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def convergence_from_history(
    conn,
    min_insider_score: float = 55.0,
    min_wallets: int = 2,
    max_window_hours: float = 168.0,
    open_only: bool = True,
    limit: int = 50,
) -> dict:
    """Return convergence signals using immutable signal_history timestamps.

    Unlike ``market_convergence_signals()`` which uses current-state
    ``position_signals``, this query uses ``signal_history`` rows with
    ``entry_anchor_ts > 0`` so that convergence timing reflects true
    first-detection timestamps.

    Parameters
    ----------
    min_insider_score : float
        Minimum ``position_insider_score`` for a wallet to count.
    min_wallets : int
        Minimum distinct wallets on same outcome to qualify.
    max_window_hours : float
        Maximum span (hours) between earliest and latest wallet anchor
        for the convergence to be considered "tight". Markets outside this
        window are still returned but flagged.
    open_only : bool
        If True, only consider signal_history rows where ``is_open = 1``.
    limit : int
        Maximum number of markets to return.

    Returns
    -------
    dict
        ``items``: list of market dicts with convergence timing metadata.
        ``coverage``: dict with anchored/total row counts and coverage pct.
    """
    open_clause = "AND sh.is_open = 1" if open_only else ""

    # For each (condition_id, outcome), take the FIRST snapshot per wallet
    # (earliest analyzed_ts) to get the score at first detection.
    rows = conn.execute(
        f"""
        WITH first_seen AS (
            SELECT
                sh.wallet,
                sh.condition_id,
                sh.outcome,
                sh.entry_anchor_ts,
                sh.position_insider_score,
                sh.entry_price,
                sh.entry_notional,
                sh.current_price,
                sh.title,
                ROW_NUMBER() OVER (
                    PARTITION BY sh.wallet, sh.condition_id, sh.outcome
                    ORDER BY sh.analyzed_ts ASC
                ) AS rn
            FROM signal_history sh
            WHERE sh.entry_anchor_ts > 0
              AND sh.position_insider_score >= ?
              {open_clause}
        )
        SELECT
            fs.condition_id,
            fs.outcome,
            MAX(fs.title) AS title,
            COUNT(DISTINCT fs.wallet) AS insider_count,
            ROUND(AVG(fs.position_insider_score), 1) AS avg_insider_score,
            ROUND(MAX(fs.position_insider_score), 1) AS max_insider_score,
            ROUND(SUM(fs.entry_notional), 2) AS total_capital,
            ROUND(AVG(fs.entry_price), 4) AS avg_entry_price,
            ROUND(AVG(fs.current_price), 4) AS avg_current_price,
            GROUP_CONCAT(DISTINCT fs.wallet) AS wallet_list,
            MIN(fs.entry_anchor_ts) AS earliest_anchor_ts,
            MAX(fs.entry_anchor_ts) AS latest_anchor_ts,
            ROUND(
                (MAX(fs.entry_anchor_ts) - MIN(fs.entry_anchor_ts)) / 3600.0,
                1
            ) AS window_hours,
            mm.end_date_iso,
            mm.category,
            mm.event_slug
        FROM first_seen fs
        LEFT JOIN market_metadata mm ON mm.condition_id = fs.condition_id
        WHERE fs.rn = 1
        GROUP BY fs.condition_id, fs.outcome
        HAVING COUNT(DISTINCT fs.wallet) >= ?
        ORDER BY insider_count DESC, avg_insider_score DESC
        LIMIT ?
        """,
        (min_insider_score, min_wallets, limit),
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        wh = d.get("window_hours") or 0
        d["tight_convergence"] = wh <= max_window_hours
        items.append(d)

    # Coverage metadata
    total_rows = conn.execute(
        "SELECT COUNT(*) FROM signal_history"
    ).fetchone()[0]
    anchored_rows = conn.execute(
        "SELECT COUNT(*) FROM signal_history WHERE entry_anchor_ts > 0"
    ).fetchone()[0]
    coverage_pct = round(anchored_rows / total_rows * 100, 1) if total_rows > 0 else 0

    return {
        "items": items,
        "coverage": {
            "anchored_rows": anchored_rows,
            "total_rows": total_rows,
            "coverage_pct": coverage_pct,
            "caveat": (
                "Convergence timing uses only signal_history rows with "
                "entry_anchor_ts > 0. Legacy rows without anchor timestamps "
                "are excluded. Coverage improves as the analyzer runs."
            ),
        },
    }


def market_intelligence_top_markets(
    conn,
    min_insider_score: float = 0.0,
    open_only: bool = False,
    limit: int = 30,
) -> list:
    """Top markets ranked by total insider capital deployed.

    Returns market-level aggregates: wallet count, total capital, avg insider
    score, domain classification, and price/resolution info.
    """
    open_clause = "AND ps.is_open = 1" if open_only else ""
    rows = conn.execute(
        f"""
        SELECT
            ps.condition_id,
            COALESCE(mm.title, MAX(ps.title)) AS title,
            COUNT(DISTINCT ps.wallet) AS wallet_count,
            COUNT(*) AS position_count,
            ROUND(SUM(ps.entry_notional), 2) AS total_capital,
            ROUND(AVG(ps.position_insider_score), 1) AS avg_insider_score,
            ROUND(MAX(ps.position_insider_score), 1) AS max_insider_score,
            ROUND(AVG(ps.entry_price), 4) AS avg_entry_price,
            ROUND(AVG(ps.current_price), 4) AS avg_current_price,
            mm.end_date_iso,
            mm.category,
            mm.event_slug,
            COALESCE(mm.volume_24h, 0) AS volume_24h,
            MAX(ps.analyzed_ts) AS latest_analyzed_ts,
            SUM(CASE WHEN ps.is_open = 1 THEN 1 ELSE 0 END) AS open_count,
            SUM(CASE WHEN ps.is_open = 0 THEN 1 ELSE 0 END) AS closed_count
        FROM position_signals ps
        LEFT JOIN market_metadata mm ON mm.condition_id = ps.condition_id
        WHERE ps.position_insider_score >= ?
            {open_clause}
        GROUP BY ps.condition_id
        ORDER BY total_capital DESC
        LIMIT ?
        """,
        (min_insider_score, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def market_intelligence_domain_summary(
    conn,
    min_insider_score: float = 0.0,
    open_only: bool = False,
) -> list:
    """Aggregate insider capital and activity by market domain.

    Uses _classify_title_domain() logic via CASE expression for domain
    classification of position_signals titles.
    """
    open_clause = "AND ps.is_open = 1" if open_only else ""
    # Fetch raw rows and classify in Python (more maintainable than SQL CASE)
    rows = conn.execute(
        f"""
        SELECT
            ps.title,
            ps.entry_notional,
            ps.position_insider_score,
            ps.wallet,
            ps.is_open
        FROM position_signals ps
        WHERE ps.position_insider_score >= ?
            {open_clause}
        """,
        (min_insider_score,),
    ).fetchall()

    domain_stats: dict = {}
    for r in rows:
        domain = _classify_title_domain(r["title"] or "")
        if domain not in domain_stats:
            domain_stats[domain] = {
                "domain": domain,
                "total_capital": 0.0,
                "position_count": 0,
                "wallets": set(),
                "avg_insider_scores": [],
                "open_count": 0,
                "closed_count": 0,
            }
        s = domain_stats[domain]
        s["total_capital"] += float(r["entry_notional"] or 0)
        s["position_count"] += 1
        s["wallets"].add(r["wallet"])
        s["avg_insider_scores"].append(float(r["position_insider_score"] or 0))
        if r["is_open"]:
            s["open_count"] += 1
        else:
            s["closed_count"] += 1

    result = []
    for d in sorted(domain_stats.values(), key=lambda x: x["total_capital"], reverse=True):
        scores = d["avg_insider_scores"]
        result.append({
            "domain": d["domain"],
            "total_capital": round(d["total_capital"], 2),
            "position_count": d["position_count"],
            "wallet_count": len(d["wallets"]),
            "avg_insider_score": round(sum(scores) / len(scores), 1) if scores else 0.0,
            "open_count": d["open_count"],
            "closed_count": d["closed_count"],
        })
    return result
