"""
Wallet leaderboard and ranking queries.
"""
import math
import logging
import sqlite3
import time
from typing import Any, Dict, List, Tuple

from sniper.persistence.db_utils import (
    compute_followability_score,
    _followable_clamp,
    _followable_short_wallet,
    _classify_title_domain,
    _LOG20, _LOG200, _LOG_DENOM,
)

logger = logging.getLogger(__name__)


def high_winrate_wallets(
    conn: sqlite3.Connection,
    limit: int = 500,
    min_pnl: float = 0.0,
    min_max_position_value: float = 0.0,
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            a.wallet,
            a.analyzed_ts,
            a.position_count,
            a.pnl,
            a.max_position_value,
            a.top_open_position_title,
            a.top_open_position_value,
            a.total_open_value,
            a.trade_count,
            CASE WHEN a.position_count > 0 THEN CAST(a.trade_count AS REAL) / a.position_count ELSE NULL END AS avg_trades_per_position,
            a.low_buy_hits_14d,
            a.suspicion_score,
            a.insider_score,
            a.speculator_score,
            a.classification,
            a.win_rate,
            a.wins,
            a.losses,
            a.closed_sample_size,
            a.total_closed_count,
            a.resolution_rate,
            a.effective_resolved_wins,
            a.effective_resolved_losses,
            a.roi_percent,
            a.high_winrate_qualified,
            a.account_age_days,
            a.is_fresh_account,
            a.position_signal_count,
            a.position_insider_candidate_count,
            a.position_speculator_count,
            a.max_position_insider_score,
            a.max_position_speculator_score,
            a.tier,
            a.strategy_group,
            a.strategy_confidence,
            a.strategy_note,
            a.reason,
            a.qualifying_roi,
            a.qualifying_pnl,
            a.qualifying_capital,
            a.wallet_edge_tier,
            a.followability_cached,
            c.low_buy_hits,
            c.last_title,
            (SELECT event_slug FROM position_signals WHERE wallet = a.wallet AND event_slug != '' LIMIT 1) AS event_slug
        FROM user_analysis a
        LEFT JOIN candidate_users c ON c.wallet = a.wallet
        WHERE (a.high_winrate_qualified = 1 OR a.classification = 'arb_fund')
          AND COALESCE(a.classification, '') NOT IN ('insider_candidate', 'blocked')
          AND a.total_closed_count > 0
          AND a.pnl >= ?
          AND a.max_position_value >= ?
        ORDER BY a.qualifying_roi DESC, a.insider_score DESC, a.analyzed_ts DESC
        LIMIT ?
        """,
        (float(min_pnl or 0.0), float(min_max_position_value or 0.0), int(limit)),
    ).fetchall()


def wallet_leaderboard_rows(
    conn: sqlite3.Connection,
    *,
    window: str = "30d",
    sort_by: str = "qualifying_roi",
    quality_only: bool = False,
    limit: int = 500,
) -> List[sqlite3.Row]:
    """Phase 1 leaderboard built from user_analysis snapshots.

    Window semantics (Phase 1): the selected window filters wallets by recent
    low_buy_trades activity. ROI/PnL values still come from user_analysis
    aggregate metrics and are not yet true per-window rollups.
    """
    window_key = str(window or "30d").strip().lower()
    window_seconds = {
        "30d": 30 * 24 * 60 * 60,
        "90d": 90 * 24 * 60 * 60,
        "all": 0,
    }.get(window_key, 30 * 24 * 60 * 60)
    cutoff_ts = int(time.time()) - int(window_seconds) if window_seconds > 0 else 0

    sort_key = str(sort_by or "qualifying_roi").strip().lower()
    order_by_map = {
        "qualifying_roi": """
            CASE WHEN wb.qualifying_roi IS NULL THEN 1 ELSE 0 END ASC,
            wb.qualifying_roi DESC,
            COALESCE(wb.qualifying_pnl, wb.pnl) DESC,
            CASE WHEN wb.dollars_per_trade IS NULL THEN 1 ELSE 0 END ASC,
            wb.dollars_per_trade DESC,
            wb.analyzed_ts DESC
        """,
        "roi": """
            CASE WHEN wb.qualifying_roi IS NULL THEN 1 ELSE 0 END ASC,
            wb.qualifying_roi DESC,
            COALESCE(wb.qualifying_pnl, wb.pnl) DESC,
            wb.analyzed_ts DESC
        """,
        "pnl": """
            COALESCE(wb.qualifying_pnl, wb.pnl) DESC,
            CASE WHEN wb.qualifying_roi IS NULL THEN 1 ELSE 0 END ASC,
            wb.qualifying_roi DESC,
            wb.analyzed_ts DESC
        """,
        "dollars_per_trade": """
            CASE WHEN wb.dollars_per_trade IS NULL THEN 1 ELSE 0 END ASC,
            wb.dollars_per_trade DESC,
            CASE WHEN wb.qualifying_roi IS NULL THEN 1 ELSE 0 END ASC,
            wb.qualifying_roi DESC,
            wb.analyzed_ts DESC
        """,
        "win_rate": """
            CASE WHEN wb.closed_sample_size >= 5 THEN 0 ELSE 1 END ASC,
            wb.win_rate DESC,
            wb.closed_sample_size DESC,
            CASE WHEN wb.qualifying_roi IS NULL THEN 1 ELSE 0 END ASC,
            wb.qualifying_roi DESC,
            wb.analyzed_ts DESC
        """,
    }
    order_by = order_by_map.get(sort_key, order_by_map["qualifying_roi"])

    # Phase 2 path: if settlement outcomes are populated, compute wallet metrics
    # directly from resolved qualifying trades.
    has_market_outcomes = conn.execute(
        "SELECT 1 FROM market_outcomes LIMIT 1"
    ).fetchone() is not None
    if has_market_outcomes:
        phase2_query = f"""
            WITH recent_wallet_activity AS (
                SELECT
                    t.proxy_wallet AS wallet,
                    COUNT(*) AS raw_trade_count_window,
                    MAX(t.timestamp) AS last_trade_ts_window
                FROM low_buy_trades t
                WHERE (? = 0 OR t.timestamp >= ?)
                GROUP BY t.proxy_wallet
            ),
            valid_resolved_conditions AS (
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
                    LOWER(COALESCE(t.outcome, '')) AS outcome,
                    t.timestamp,
                    t.price,
                    t.size,
                    (t.price * t.size) AS notional
                FROM low_buy_trades t
                WHERE LOWER(COALESCE(t.side, '')) = 'buy'
                  AND COALESCE(t.price, 0) > 0
                  AND COALESCE(t.size, 0) > 0
                  AND COALESCE(t.price, 0) < 0.80
                  AND (? = 0 OR t.timestamp >= ?)
            ),
            single_sided_conditions AS (
                SELECT
                    wallet,
                    condition_id
                FROM qualifying_trades
                GROUP BY wallet, condition_id
                HAVING COUNT(DISTINCT outcome) = 1
            ),
            qualifying_positions AS (
                SELECT
                    qt.wallet,
                    qt.condition_id,
                    qt.outcome,
                    MIN(qt.timestamp) AS entry_ts,
                    SUM(qt.size) AS entry_size,
                    SUM(qt.notional) AS entry_notional,
                    COUNT(*) AS fill_count,
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
                    qp.condition_id,
                    qp.outcome,
                    qp.entry_ts,
                    qp.entry_size,
                    qp.entry_notional,
                    qp.fill_count,
                    qp.avg_entry_price,
                    mo.resolved_price,
                    mo.resolved_at,
                    ((mo.resolved_price - qp.avg_entry_price) * qp.entry_size) AS pnl_usdc
                FROM qualifying_positions qp
                INNER JOIN valid_resolved_conditions vc
                    ON vc.condition_id = qp.condition_id
                INNER JOIN market_outcomes mo
                    ON LOWER(COALESCE(mo.condition_id, '')) = qp.condition_id
                   AND LOWER(mo.outcome) = qp.outcome
                   AND COALESCE(mo.resolved_at, 0) > 0
                   AND COALESCE(mo.resolved_at, 0) <= CAST(strftime('%s', 'now') AS INTEGER)
                   AND COALESCE(mo.resolved_price, -1) >= 0.0
                   AND COALESCE(mo.resolved_price, -1) <= 1.0
            ),
            wallet_resolved AS (
                SELECT
                    rp.wallet,
                    COUNT(*) AS qualifying_trade_count,
                    SUM(rp.entry_notional) AS qualifying_capital,
                    SUM(rp.pnl_usdc) AS qualifying_pnl,
                    CASE
                        WHEN SUM(rp.entry_notional) > 0
                        THEN (SUM(rp.pnl_usdc) / SUM(rp.entry_notional)) * 100.0
                        ELSE NULL
                    END AS qualifying_roi,
                    SUM(CASE WHEN rp.pnl_usdc > 0 THEN 1 ELSE 0 END) AS effective_resolved_wins,
                    SUM(CASE WHEN rp.pnl_usdc < 0 THEN 1 ELSE 0 END) AS effective_resolved_losses,
                    SUM(rp.fill_count) AS raw_trade_count,
                    AVG(rp.fill_count) AS avg_trades_per_position,
                    CASE
                        WHEN COUNT(*) > 0 THEN SUM(rp.entry_notional) / COUNT(*)
                        ELSE NULL
                    END AS dollars_per_trade,
                    MAX(rp.entry_ts) AS last_entry_ts
                FROM resolved_positions rp
                GROUP BY rp.wallet
            ),
            wallet_base AS (
                SELECT
                    wr.wallet,
                    COALESCE(ua.analyzed_ts, wr.last_entry_ts, 0) AS analyzed_ts,
                    COALESCE(ua.position_count, 0) AS position_count,
                    COALESCE(wr.raw_trade_count, ua.trade_count, 0) AS trade_count,
                    COALESCE(wr.raw_trade_count, ua.trade_count, 0) AS raw_trade_count,
                    COALESCE(ua.pnl, 0) AS pnl,
                    COALESCE(ua.max_position_value, 0) AS max_position_value,
                    COALESCE(ua.top_open_position_title, '') AS top_open_position_title,
                    COALESCE(ua.top_open_position_value, 0) AS top_open_position_value,
                    COALESCE(ua.total_open_value, 0) AS total_open_value,
                    COALESCE(ua.low_buy_hits_14d, 0) AS low_buy_hits_14d,
                    COALESCE(ua.suspicion_score, 0) AS suspicion_score,
                    COALESCE(ua.insider_score, 0) AS insider_score,
                    COALESCE(ua.speculator_score, 0) AS speculator_score,
                    COALESCE(ua.classification, 'unclassified') AS classification,
                    CASE
                        WHEN (COALESCE(wr.effective_resolved_wins, 0) + COALESCE(wr.effective_resolved_losses, 0)) > 0
                        THEN CAST(wr.effective_resolved_wins AS REAL) /
                             CAST((wr.effective_resolved_wins + wr.effective_resolved_losses) AS REAL)
                        ELSE COALESCE(ua.win_rate, 0)
                    END AS win_rate,
                    COALESCE(wr.effective_resolved_wins, ua.wins, 0) AS wins,
                    COALESCE(wr.effective_resolved_losses, ua.losses, 0) AS losses,
                    COALESCE(
                        (wr.effective_resolved_wins + wr.effective_resolved_losses),
                        ua.closed_sample_size,
                        0
                    ) AS closed_sample_size,
                    COALESCE(
                        (wr.effective_resolved_wins + wr.effective_resolved_losses),
                        ua.total_closed_count,
                        0
                    ) AS total_closed_count,
                    CASE
                        WHEN COALESCE(wr.qualifying_trade_count, 0) > 0 THEN 1.0
                        ELSE COALESCE(ua.resolution_rate, 0.0)
                    END AS resolution_rate,
                    COALESCE(wr.effective_resolved_wins, ua.effective_resolved_wins, 0) AS effective_resolved_wins,
                    COALESCE(wr.effective_resolved_losses, ua.effective_resolved_losses, 0) AS effective_resolved_losses,
                    COALESCE(ua.roi_percent, 0) AS roi_percent,
                    COALESCE(ua.high_winrate_qualified, 0) AS high_winrate_qualified,
                    COALESCE(ua.account_age_days, 99999) AS account_age_days,
                    COALESCE(ua.is_fresh_account, 0) AS is_fresh_account,
                    COALESCE(ua.position_signal_count, 0) AS position_signal_count,
                    COALESCE(ua.position_insider_candidate_count, 0) AS position_insider_candidate_count,
                    COALESCE(ua.position_speculator_count, 0) AS position_speculator_count,
                    COALESCE(ua.max_position_insider_score, 0) AS max_position_insider_score,
                    COALESCE(ua.max_position_speculator_score, 0) AS max_position_speculator_score,
                    COALESCE(ua.tier, '') AS tier,
                    COALESCE(ua.strategy_group, '') AS strategy_group,
                    COALESCE(ua.strategy_confidence, 0) AS strategy_confidence,
                    COALESCE(ua.strategy_note, '') AS strategy_note,
                    COALESCE(ua.reason, 'phase2_resolved_trades') AS reason,
                    wr.qualifying_roi,
                    wr.qualifying_pnl,
                    wr.qualifying_capital,
                    ua.wallet_quality_score,
                    ua.wallet_quality_eligible,
                    ua.wallet_quality_fail_reason,
                    COALESCE(ua.market_maker_detected, 0) AS market_maker_detected,
                    COALESCE(rwa.raw_trade_count_window, 0) AS raw_trade_count_window,
                    COALESCE(rwa.last_trade_ts_window, wr.last_entry_ts, 0) AS last_trade_ts_window,
                    wr.qualifying_trade_count,
                    wr.avg_trades_per_position,
                    wr.dollars_per_trade
                FROM wallet_resolved wr
                LEFT JOIN user_analysis ua ON ua.wallet = wr.wallet
                LEFT JOIN recent_wallet_activity rwa ON rwa.wallet = wr.wallet
                WHERE COALESCE(ua.classification, '') <> 'blocked'
                  AND (? = 0 OR COALESCE(ua.classification, 'unclassified') NOT IN ('speculator_bot', 'low_signal'))
                  AND (? = 'all' OR rwa.wallet IS NOT NULL)
            )
            SELECT
                wb.*,
                c.low_buy_hits,
                c.last_title,
                (
                    SELECT ps.event_slug
                    FROM position_signals ps
                    WHERE ps.wallet = wb.wallet AND ps.event_slug != ''
                    LIMIT 1
                ) AS event_slug,
                'market_outcomes_phase2' AS metrics_source
            FROM wallet_base wb
            LEFT JOIN candidate_users c ON c.wallet = wb.wallet
            ORDER BY {order_by}
            LIMIT ?
        """
        phase2_rows = conn.execute(
            phase2_query,
            (
                int(cutoff_ts),
                int(cutoff_ts),
                int(cutoff_ts),
                int(cutoff_ts),
                1 if quality_only else 0,
                str(window_key),
                int(max(1, min(int(limit or 500), 5000))),
            ),
        ).fetchall()
        if phase2_rows:
            return phase2_rows

    query = f"""
        WITH recent_wallet_activity AS (
            SELECT
                t.proxy_wallet AS wallet,
                COUNT(*) AS raw_trade_count_window,
                MAX(t.timestamp) AS last_trade_ts_window
            FROM low_buy_trades t
            WHERE (? = 0 OR t.timestamp >= ?)
            GROUP BY t.proxy_wallet
        ),
        wallet_base AS (
            SELECT
                a.wallet,
                a.analyzed_ts,
                a.position_count,
                a.trade_count,
                a.trade_count AS raw_trade_count,
                a.pnl,
                a.max_position_value,
                a.top_open_position_title,
                a.top_open_position_value,
                a.total_open_value,
                a.low_buy_hits_14d,
                a.suspicion_score,
                a.insider_score,
                a.speculator_score,
                a.classification,
                a.win_rate,
                a.wins,
                a.losses,
                a.closed_sample_size,
                a.total_closed_count,
                a.resolution_rate,
                a.effective_resolved_wins,
                a.effective_resolved_losses,
                a.roi_percent,
                a.high_winrate_qualified,
                a.account_age_days,
                a.is_fresh_account,
                a.position_signal_count,
                a.position_insider_candidate_count,
                a.position_speculator_count,
                a.max_position_insider_score,
                a.max_position_speculator_score,
                a.tier,
                a.strategy_group,
                a.strategy_confidence,
                a.strategy_note,
                a.reason,
                a.qualifying_roi,
                a.qualifying_pnl,
                a.qualifying_capital,
                a.wallet_edge_tier,
                a.followability_cached,
                a.wallet_quality_score,
                a.wallet_quality_eligible,
                a.wallet_quality_fail_reason,
                COALESCE(a.market_maker_detected, 0) AS market_maker_detected,
                COALESCE(rwa.raw_trade_count_window, 0) AS raw_trade_count_window,
                rwa.last_trade_ts_window,
                a.closed_sample_size AS qualifying_trade_count,
                CASE
                    WHEN a.position_count > 0
                    THEN CAST(a.trade_count AS REAL) / CAST(a.position_count AS REAL)
                    ELSE NULL
                END AS avg_trades_per_position,
                CASE
                    WHEN a.closed_sample_size > 0 AND a.qualifying_capital > 0
                    THEN a.qualifying_capital / CAST(a.closed_sample_size AS REAL)
                    ELSE NULL
                END AS dollars_per_trade
            FROM user_analysis a
            LEFT JOIN recent_wallet_activity rwa ON rwa.wallet = a.wallet
            WHERE COALESCE(a.classification, '') <> 'blocked'
              AND (? = 0 OR COALESCE(a.classification, '') NOT IN ('speculator_bot', 'low_signal'))
              AND (? = 'all' OR rwa.wallet IS NOT NULL)
              AND COALESCE(a.total_closed_count, 0) > 0
        )
        SELECT
            wb.*,
            c.low_buy_hits,
            c.last_title,
            (
                SELECT ps.event_slug
                FROM position_signals ps
                WHERE ps.wallet = wb.wallet AND ps.event_slug != ''
                LIMIT 1
            ) AS event_slug,
            'user_analysis_phase1' AS metrics_source
        FROM wallet_base wb
        LEFT JOIN candidate_users c ON c.wallet = wb.wallet
        ORDER BY {order_by}
        LIMIT ?
    """
    return conn.execute(
        query,
        (
            int(cutoff_ts),
            int(cutoff_ts),
            1 if quality_only else 0,
            str(window_key),
            int(max(1, min(int(limit or 500), 5000))),
        ),
    ).fetchall()


def get_wallet_leaderboard_rows(
    conn: sqlite3.Connection,
    *,
    window: str = "30d",
    sort: str = "qualifying_roi",
    quality_only: bool = False,
    limit: int = 500,
) -> List[sqlite3.Row]:
    """Backward-compatible alias for leaderboard callers expecting get_* naming."""
    return wallet_leaderboard_rows(
        conn,
        window=window,
        sort_by=sort,
        quality_only=quality_only,
        limit=limit,
    )


def emerging_insider_wallets(
    conn: sqlite3.Connection,
    limit: int = 500,
    min_insider_score: float = 65.0,
    min_max_position_value: float = 0.0,
    min_total_open_value: float = 0.0,
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            a.wallet,
            a.analyzed_ts,
            a.position_count,
            a.pnl,
            a.max_position_value,
            a.top_open_position_title,
            a.top_open_position_value,
            a.total_open_value,
            a.trade_count,
            a.low_buy_hits_14d,
            a.suspicion_score,
            a.insider_score,
            a.speculator_score,
            a.classification,
            a.win_rate,
            a.wins,
            a.losses,
            a.closed_sample_size,
            a.total_closed_count,
            a.resolution_rate,
            a.effective_resolved_wins,
            a.effective_resolved_losses,
            a.roi_percent,
            a.high_winrate_qualified,
            a.account_age_days,
            a.is_fresh_account,
            a.position_signal_count,
            a.position_insider_candidate_count,
            a.position_speculator_count,
            a.max_position_insider_score,
            a.max_position_speculator_score,
            a.tier,
            a.strategy_group,
            a.strategy_confidence,
            a.strategy_note,
            a.reason,
            c.low_buy_hits,
            c.last_title
        FROM user_analysis a
        LEFT JOIN candidate_users c ON c.wallet = a.wallet
        WHERE COALESCE(a.classification, '') = 'emerging_insider'
          AND a.insider_score >= ?
          AND a.max_position_value >= ?
          AND a.total_open_value >= ?
        ORDER BY a.insider_score DESC, a.max_position_value DESC, a.total_open_value DESC, a.analyzed_ts DESC
        LIMIT ?
        """,
        (
            float(min_insider_score or 0.0),
            float(min_max_position_value or 0.0),
            float(min_total_open_value or 0.0),
            int(limit),
        ),
    ).fetchall()


def speculator_flow_wallets(
    conn: sqlite3.Connection,
    limit: int = 500,
    min_speculator_score: float = 55.0,
    min_position_speculator_score: float = 65.0,
    min_max_position_value: float = 0.0,
    min_total_open_value: float = 0.0,
    min_abs_pnl: float = 0.0,
) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            a.wallet,
            a.analyzed_ts,
            a.position_count,
            a.pnl,
            a.max_position_value,
            a.top_open_position_title,
            a.top_open_position_value,
            a.total_open_value,
            a.trade_count,
            a.low_buy_hits_14d,
            a.suspicion_score,
            a.insider_score,
            a.speculator_score,
            a.classification,
            a.win_rate,
            a.closed_sample_size,
            a.high_winrate_qualified,
            a.account_age_days,
            a.is_fresh_account,
            a.position_signal_count,
            a.position_insider_candidate_count,
            a.position_speculator_count,
            a.max_position_insider_score,
            a.max_position_speculator_score,
            a.tier,
            a.strategy_group,
            a.strategy_confidence,
            a.strategy_note,
            a.reason,
            c.low_buy_hits,
            c.last_title
        FROM user_analysis a
        LEFT JOIN candidate_users c ON c.wallet = a.wallet
        WHERE (
                a.speculator_score >= ?
                OR a.position_speculator_count > 0
                OR a.max_position_speculator_score >= ?
                OR COALESCE(a.classification, '') IN ('speculator_bot', 'mixed_review')
              )
          AND COALESCE(a.classification, '') <> 'insider_candidate'
          AND a.max_position_value >= ?
          AND a.total_open_value >= ?
          AND ABS(a.pnl) >= ?
        ORDER BY
            a.speculator_score DESC,
            a.max_position_speculator_score DESC,
            a.total_open_value DESC,
            ABS(a.pnl) DESC,
            a.analyzed_ts DESC
        LIMIT ?
        """,
        (
            float(min_speculator_score or 0.0),
            float(min_position_speculator_score or 0.0),
            float(min_max_position_value or 0.0),
            float(min_total_open_value or 0.0),
            float(min_abs_pnl or 0.0),
            int(limit),
        ),
    ).fetchall()


def latest_external_leaderboard_rows(
    conn: sqlite3.Connection,
    *,
    category: str = "OVERALL",
    time_period: str = "ALL",
) -> List[Dict[str, Any]]:
    """Coalesce PNL+VOL rows into one wallet-level view."""
    cat = str(category).upper()
    tp = str(time_period).upper()
    rows = conn.execute(
        """
        SELECT
            wallet,
            MAX(CASE WHEN order_by = 'PNL' THEN rank END) AS official_rank_pnl,
            MAX(CASE WHEN order_by = 'VOL' THEN rank END) AS official_rank_vol,
            COALESCE(
                MAX(CASE WHEN order_by = 'PNL' THEN official_pnl END),
                MAX(official_pnl)
            ) AS official_pnl,
            COALESCE(
                MAX(CASE WHEN order_by = 'VOL' THEN official_volume END),
                MAX(official_volume)
            ) AS official_volume,
            COALESCE(
                MAX(CASE WHEN order_by = 'PNL' THEN NULLIF(username, '') END),
                MAX(NULLIF(username, ''))
            ) AS username,
            COALESCE(
                MAX(CASE WHEN order_by = 'PNL' THEN NULLIF(profile_image, '') END),
                MAX(NULLIF(profile_image, ''))
            ) AS profile_image,
            COALESCE(
                MAX(CASE WHEN order_by = 'PNL' THEN NULLIF(x_username, '') END),
                MAX(NULLIF(x_username, ''))
            ) AS x_username,
            MAX(verified_badge) AS verified_badge,
            MAX(fetched_at) AS official_fetched_at
        FROM external_leaderboard_entries
        WHERE category = ? AND time_period = ?
        GROUP BY wallet
        """,
        (cat, tp),
    ).fetchall()
    return [dict(r) for r in rows]


def wallet_leaderboard_v2_rows(
    conn: sqlite3.Connection,
    *,
    category: str = "OVERALL",
    time_period: str = "ALL",
    sort_by: str = "official_pnl",
    limit: int = 100,
    include_unqualified: bool = True,
    min_sample: int = 50,
    min_capital: float = 25000.0,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Build the V2 leaderboard from official discovery + internal proof.

    Returns (qualified_items, unqualified_items, meta).
    """
    # 1. Official rows for requested filter
    official_map: Dict[str, Dict[str, Any]] = {}
    for row in latest_external_leaderboard_rows(conn, category=category, time_period=time_period):
        official_map[row["wallet"]] = row

    # 2. Tracked wallets: watchlist + followed_positions
    tracked: set[str] = set()
    for r in conn.execute("SELECT wallet FROM watchlist_wallets").fetchall():
        w = str(r["wallet"]).lower()
        if w:
            tracked.add(w)
    for r in conn.execute("SELECT DISTINCT wallet FROM followed_positions").fetchall():
        w = str(r["wallet"]).lower()
        if w:
            tracked.add(w)

    # 3. Candidate set = union
    candidates = set(official_map.keys()) | tracked

    # 4. Exclusions
    ignored: set[str] = set()
    for r in conn.execute("SELECT wallet FROM ignored_wallets").fetchall():
        w = str(r["wallet"]).lower()
        if w:
            ignored.add(w)
    for r in conn.execute("SELECT wallet FROM blocked_wallets").fetchall():
        w = str(r["wallet"]).lower()
        if w:
            ignored.add(w)
    candidates -= ignored

    if not candidates:
        meta = _leaderboard_v2_meta(category, time_period, sort_by, 0, 0, 0, official_map)
        return [], [], meta

    # 5. Bulk-load user_analysis
    placeholders = ",".join("?" for _ in candidates)
    analysis_map: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        f"""SELECT wallet, classification, insider_score, speculator_score,
                   win_rate, closed_sample_size, total_closed_count,
                   qualifying_roi, qualifying_pnl, qualifying_capital,
                   pnl, roi_percent, position_count, trade_count,
                   strategy_group, strategy_confidence,
                   analyzed_ts
            FROM user_analysis
            WHERE LOWER(wallet) IN ({placeholders})""",
        [w for w in candidates],
    ).fetchall():
        analysis_map[str(r["wallet"]).lower()] = dict(r)

    # 6. Bulk-load wallet_profiles
    profile_map: Dict[str, Dict[str, Any]] = {}
    for r in conn.execute(
        f"""SELECT wallet, username, pseudonym,
                   profile_image, profile_image_optimized,
                   last_profile_sync_ts
            FROM wallet_profiles
            WHERE LOWER(wallet) IN ({placeholders})""",
        [w for w in candidates],
    ).fetchall():
        profile_map[str(r["wallet"]).lower()] = dict(r)

    # 7. Check for blocked classification
    blocked_class: set[str] = set()
    for w in candidates:
        ua = analysis_map.get(w, {})
        if str(ua.get("classification", "")).lower() == "blocked":
            blocked_class.add(w)
    candidates -= blocked_class

    # 8. Build items
    qualified: List[Dict[str, Any]] = []
    unqualified: List[Dict[str, Any]] = []

    for w in candidates:
        off = official_map.get(w, {})
        ua = analysis_map.get(w, {})
        prof = profile_map.get(w, {})

        # Identity precedence
        display_name = (
            str(prof.get("username") or "").strip()
            or str(off.get("username") or "").strip()
            or str(prof.get("pseudonym") or "").strip()
            or (w[:6] + "..." + w[-4:])
        )
        image = (
            str(prof.get("profile_image_optimized") or "").strip()
            or str(prof.get("profile_image") or "").strip()
            or str(off.get("profile_image") or "").strip()
        )

        cls = str(ua.get("classification") or "").strip() or None
        insider = ua.get("insider_score")
        spec = ua.get("speculator_score")
        sample = int(ua.get("closed_sample_size") or 0)
        q_capital = float(ua.get("qualifying_capital") or 0)
        q_roi = ua.get("qualifying_roi")
        q_pnl = ua.get("qualifying_pnl")
        wr = ua.get("win_rate")

        has_official = bool(off)
        has_analysis = bool(ua)

        # Qualification
        reasons = []
        if not has_official:
            reasons.append("missing_official_rank")
        if not has_analysis:
            reasons.append("missing_analysis")
        if has_analysis and sample < min_sample:
            reasons.append("sample_below_threshold")
        if has_analysis and q_capital < min_capital:
            reasons.append("capital_below_threshold")
        if has_analysis and q_roi is None:
            reasons.append("qualifying_roi_missing")

        is_qualified = has_official and has_analysis and not reasons

        # Copyability
        if cls in ("arb_fund", "speculator_bot"):
            copy_label = "do_not_copy"
        elif (
            cls not in ("arb_fund", "speculator_bot")
            and q_roi is not None
            and q_roi > 0
            and sample >= min_sample
            and (insider is not None and insider >= 50)
        ):
            copy_label = "copyable"
        else:
            copy_label = "review"

        item = {
            "wallet": w,
            "display_name": display_name,
            "username": str(off.get("username") or prof.get("username") or ""),
            "pseudonym": str(prof.get("pseudonym") or ""),
            "profile_image": image,
            "verified_badge": bool(off.get("verified_badge")),
            "official_rank_pnl": off.get("official_rank_pnl"),
            "official_rank_vol": off.get("official_rank_vol"),
            "official_pnl": off.get("official_pnl"),
            "official_volume": off.get("official_volume"),
            "official_category": category.upper() if has_official else None,
            "official_time_period": time_period.upper() if has_official else None,
            "qualifying_roi": q_roi,
            "qualifying_pnl": q_pnl,
            "qualifying_capital": q_capital if has_analysis else None,
            "closed_sample_size": sample if has_analysis else None,
            "win_rate": wr,
            "position_count": ua.get("position_count"),
            "trade_count": ua.get("trade_count"),
            "classification": cls,
            "insider_score": insider,
            "speculator_score": spec,
            "strategy_group": ua.get("strategy_group") or None,
            "strategy_confidence": ua.get("strategy_confidence"),
            "copyability_label": copy_label,
            "qualification_status": "qualified" if is_qualified else "unqualified",
            "qualification_reason": reasons[0] if reasons else None,
            "analysis_updated_at": ua.get("analyzed_ts"),
            "profile_updated_at": prof.get("last_profile_sync_ts"),
            "official_fetched_at": off.get("official_fetched_at"),
        }

        if is_qualified:
            qualified.append(item)
        else:
            unqualified.append(item)

    # 9. Sort
    sort_key = str(sort_by or "official_pnl").strip().lower()
    qualified = _sort_leaderboard_v2(qualified, sort_key)
    # Unqualified: official wallets by pnl, then tracked-only by qualifying_pnl
    unqualified.sort(
        key=lambda x: (
            0 if x.get("official_pnl") is not None else 1,
            -(x.get("official_pnl") or 0),
            -(x.get("qualifying_pnl") or 0),
        )
    )

    meta = _leaderboard_v2_meta(
        category, time_period, sort_key,
        len(qualified) + len(unqualified),
        len(qualified), len(unqualified),
        official_map,
    )

    return qualified[:limit], unqualified if include_unqualified else [], meta


def _sort_leaderboard_v2(
    items: List[Dict[str, Any]], sort_key: str
) -> List[Dict[str, Any]]:
    """Sort qualified leaderboard items by the requested key with tiebreakers."""
    def _val(item: Dict[str, Any], key: str, default: float = 0.0) -> float:
        v = item.get(key)
        return float(v) if v is not None else default

    if sort_key == "official_volume":
        items.sort(key=lambda x: (
            -_val(x, "official_volume"),
            -_val(x, "official_pnl"),
            -_val(x, "qualifying_pnl"),
            -_val(x, "closed_sample_size"),
        ))
    elif sort_key == "fit_roi":
        items.sort(key=lambda x: (
            -_val(x, "qualifying_roi"),
            -_val(x, "qualifying_pnl"),
            -_val(x, "closed_sample_size"),
            -_val(x, "official_pnl"),
        ))
    elif sort_key == "win_rate":
        items.sort(key=lambda x: (
            -_val(x, "win_rate"),
            -_val(x, "closed_sample_size"),
            -_val(x, "qualifying_pnl"),
            -_val(x, "official_pnl"),
        ))
    else:  # official_pnl (default)
        items.sort(key=lambda x: (
            -_val(x, "official_pnl"),
            -_val(x, "qualifying_pnl"),
            -_val(x, "qualifying_roi"),
            -_val(x, "closed_sample_size"),
            -_val(x, "official_volume"),
        ))
    return items


def _leaderboard_v2_meta(
    category: str,
    time_period: str,
    sort_key: str,
    candidate_count: int,
    qualified_count: int,
    unqualified_count: int,
    official_map: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    fetched_times = [
        r.get("official_fetched_at", 0) for r in official_map.values()
        if r.get("official_fetched_at")
    ]
    max_fetched = max(fetched_times) if fetched_times else 0
    stale_threshold = int(time.time()) - 7200  # 2 hours
    return {
        "candidate_count": candidate_count,
        "qualified_count": qualified_count,
        "unqualified_count": unqualified_count,
        "official_category": category.upper(),
        "official_time_period": time_period.upper(),
        "sort": sort_key,
        "official_data_fetched_at": max_fetched,
        "official_data_stale": max_fetched < stale_threshold,
        "official_source": "polymarket_public_leaderboard",
        "internal_scope": "all_time",
        "qualification_thresholds": {
            "min_closed_sample_size": 50,
            "min_qualifying_capital": 25000,
        },
    }


def wallet_followable_leaderboard_rows(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    include_filtered: bool = True,
    filtered_limit: int = 200,
    min_sample: int = 20,
    min_capital: float = 5000.0,
    max_capital: float = 250000.0,
    min_win_rate: float = 0.55,
    activity_window_days: int = 30,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """Return (qualified, filtered, meta) for the followable wallet board."""

    # 1. Load all candidate rows from user_analysis
    rows = conn.execute(
        """SELECT wallet, classification, insider_score, speculator_score,
                  win_rate, closed_sample_size, total_closed_count, qualifying_roi,
                  qualifying_capital, qualifying_pnl, pnl,
                  lb_pnl, closed_realized_pnl, open_cash_pnl,
                  position_count, trade_count, low_buy_hits_14d,
                  updated_at as analysis_updated_at,
                  wallet_quality_score,
                  wallet_quality_eligible,
                  wallet_quality_fail_reason,
                  COALESCE(market_maker_detected, 0) AS market_maker_detected
           FROM user_analysis
           WHERE closed_sample_size > 0"""
    ).fetchall()

    if not rows:
        return [], [], {
            "candidate_count": 0, "qualified_count": 0,
            "filtered_total_count": 0, "filtered_returned_count": 0,
            "min_sample": min_sample, "min_capital": min_capital,
            "max_capital": max_capital, "min_win_rate": min_win_rate,
            "activity_window_days": activity_window_days,
            "board_goal": "followable_wallets",
        }

    wallets_lower = [str(r["wallet"]).lower() for r in rows]

    # 2. Bulk load wallet_profiles for identity
    profile_map: Dict[str, Dict[str, Any]] = {}
    if wallets_lower:
        ph = ",".join("?" for _ in wallets_lower)
        for pr in conn.execute(
            f"""SELECT wallet, username, pseudonym, profile_image, profile_image_optimized
                FROM wallet_profiles WHERE LOWER(wallet) IN ({ph})""",
            wallets_lower,
        ).fetchall():
            profile_map[str(pr["wallet"]).lower()] = dict(pr)

    # 3. Recent watchlist trades (activity source 1)
    cutoff_ts = int(time.time()) - activity_window_days * 86400
    recent_trade_wallets: set = set()
    for r in conn.execute(
        "SELECT DISTINCT LOWER(wallet) as w FROM watchlist_trades WHERE timestamp >= ?",
        (cutoff_ts,),
    ).fetchall():
        recent_trade_wallets.add(str(r["w"]))

    # 4. Official month presence (activity source 2)
    month_wallets: set = set()
    for r in conn.execute(
        "SELECT DISTINCT LOWER(wallet) as w FROM external_leaderboard_entries WHERE time_period = 'MONTH'"
    ).fetchall():
        month_wallets.add(str(r["w"]))

    # 5. Exclusion sets
    blocked_set: set = set()
    for r in conn.execute("SELECT LOWER(wallet) as w FROM blocked_wallets").fetchall():
        blocked_set.add(str(r["w"]))
    ignored_set: set = set()
    for r in conn.execute("SELECT LOWER(wallet) as w FROM ignored_wallets").fetchall():
        ignored_set.add(str(r["w"]))

    # 6. Followed wallets (for is_followed flag)
    followed_set: set = set()
    for r in conn.execute("SELECT LOWER(wallet) as w FROM watchlist_wallets").fetchall():
        followed_set.add(str(r["w"]))

    # Process each candidate
    qualified: List[Dict[str, Any]] = []
    filtered_items: List[Dict[str, Any]] = []
    total_candidates = len(rows)

    for row in rows:
        wallet = str(row["wallet"])
        wl = wallet.lower()
        classification = str(row["classification"] or "low_signal")
        insider_score = float(row["insider_score"] or 0)
        speculator_score = float(row["speculator_score"] or 0)
        win_rate = float(row["win_rate"] or 0)
        closed_sample_size = int(row["closed_sample_size"] or 0)
        total_closed_count = int(row["total_closed_count"] or 0)
        qualifying_roi = row["qualifying_roi"]  # may be None
        qualifying_capital = float(row["qualifying_capital"] or 0)
        qualifying_pnl = float(row["qualifying_pnl"] or 0)
        total_pnl = float(row["pnl"] or 0)
        lb_pnl_val = float(row["lb_pnl"]) if row["lb_pnl"] is not None else None
        closed_realized_pnl_val = float(row["closed_realized_pnl"]) if row["closed_realized_pnl"] is not None else None
        open_cash_pnl_val = float(row["open_cash_pnl"]) if row["open_cash_pnl"] is not None else None
        position_count = int(row["position_count"] or 0)
        trade_count = int(row["trade_count"] or 0)
        low_buy_hits_14d = int(row["low_buy_hits_14d"] or 0)
        analysis_updated_at = str(row["analysis_updated_at"] or "")

        # Identity
        prof = profile_map.get(wl, {})
        username = str(prof.get("username") or "")
        pseudonym = str(prof.get("pseudonym") or "")
        display_name = username or pseudonym or _followable_short_wallet(wallet)
        profile_image = str(prof.get("profile_image_optimized") or prof.get("profile_image") or "")

        # Activity sources
        activity_sources = []
        if wl in recent_trade_wallets:
            activity_sources.append("recent_watchlist_trade")
        if wl in month_wallets:
            activity_sources.append("official_month_presence")
        if low_buy_hits_14d > 0:
            activity_sources.append("low_buy_hits_14d")
        if position_count > 0:
            activity_sources.append("has_open_positions")

        # Build identity dict for filtered items
        def _identity():
            return {
                "wallet": wallet,
                "display_name": display_name,
                "username": username,
                "pseudonym": pseudonym,
                "profile_image": profile_image,
            }

        # Qualification filter (ordered, first fail wins)
        filter_reason = None
        if wl in ignored_set:
            filter_reason = "ignored"
        elif wl in blocked_set:
            filter_reason = "blocked"
        elif classification == "arb_fund":
            filter_reason = "arb_fund"
        elif classification == "speculator_bot":
            filter_reason = "speculator_bot"
        elif classification == "blocked":
            filter_reason = "blocked"
        elif closed_sample_size < min_sample:
            filter_reason = "sample_below_min"
        elif qualifying_capital < min_capital:
            filter_reason = "capital_below_min"
        elif qualifying_capital > max_capital:
            filter_reason = "capital_above_max"
        elif qualifying_roi is None:
            filter_reason = "qualifying_roi_missing"
        elif float(qualifying_roi) <= 0:
            filter_reason = "qualifying_roi_nonpositive"
        elif win_rate < min_win_rate:
            filter_reason = "win_rate_below_min"
        elif not activity_sources:
            filter_reason = "inactive_30d"

        if filter_reason is not None:
            filtered_items.append({
                **_identity(),
                "classification": classification,
                "win_rate": win_rate,
                "closed_sample_size": closed_sample_size,
                "total_closed_count": total_closed_count,
                "qualifying_roi": float(qualifying_roi) if qualifying_roi is not None else None,
                "qualifying_capital": qualifying_capital,
                "qualifying_pnl": qualifying_pnl,
                "pnl": total_pnl,
                "total_pnl": total_pnl,
                "filter_reason": filter_reason,
                "wallet_quality_score": float(row["wallet_quality_score"]) if row["wallet_quality_score"] is not None else None,
                "wallet_quality_eligible": int(row["wallet_quality_eligible"]) if row["wallet_quality_eligible"] is not None else None,
                "wallet_quality_fail_reason": str(row["wallet_quality_fail_reason"] or "") if row["wallet_quality_fail_reason"] else None,
                "market_maker_detected": bool(int(row["market_maker_detected"] or 0)),
            })
            continue

        # Compute followability score (via shared helper — P&L-first formula)
        qr = float(qualifying_roi)
        followability_score = compute_followability_score(
            win_rate=win_rate,
            qualifying_roi=qr,
            closed_sample_size=closed_sample_size,
            qualifying_capital=qualifying_capital,
            insider_score=insider_score,
            speculator_score=speculator_score,
            classification=classification,
            lb_pnl=lb_pnl_val,
            closed_realized_pnl=closed_realized_pnl_val,
            open_cash_pnl=open_cash_pnl_val,
        )

        # Recompute components for score_breakdown output (mirrors db_utils formula)
        _best_pnl = lb_pnl_val if lb_pnl_val is not None else closed_realized_pnl_val
        pnl_score = _followable_clamp(50.0 + 50.0 * math.tanh(_best_pnl / 150_000.0)) if _best_pnl is not None else 50.0
        win_rate_score = _followable_clamp(((win_rate * 0.85 - 0.50) / 0.40) * 100)
        roi_score = _followable_clamp((qr / 40.0) * 100)
        sample_score = _followable_clamp(
            ((math.log(max(closed_sample_size, 20)) - _LOG20) / _LOG_DENOM) * 100
        )
        if 10000 <= qualifying_capital <= 100000:
            capital_fit_score = 100.0
        elif 5000 <= qualifying_capital < 10000:
            capital_fit_score = 60 + 40 * ((qualifying_capital - 5000) / 5000)
        elif 100000 < qualifying_capital <= 250000:
            capital_fit_score = 100 - 40 * ((qualifying_capital - 100000) / 150000)
        else:
            capital_fit_score = 0.0
        open_loss_penalty = 0.0
        if open_cash_pnl_val is not None and open_cash_pnl_val < -10_000:
            open_loss_penalty = min(20.0, (-open_cash_pnl_val - 10_000) / 5_000)
        pnl_penalty = 0.0  # kept for backward compat; open_loss_penalty replaces it

        # Confidence label
        if lb_pnl_val is not None and lb_pnl_val > 50_000:
            confidence_label = "pnl-verified"
        elif closed_realized_pnl_val is not None and closed_realized_pnl_val > 50_000:
            confidence_label = "pnl-verified"
        elif classification in ("high_winrate_review", "emerging_insider", "insider_candidate"):
            confidence_label = "reviewed"
        else:
            confidence_label = "metric-qualified"

        # Why-ranked chips (first 4)
        why_ranked = []
        if _best_pnl is not None and _best_pnl > 100_000:
            why_ranked.append("strong lifetime P&L")
        if win_rate >= 0.65:
            why_ranked.append("high hit rate")
        if float(qualifying_roi) >= 15:
            why_ranked.append("strong qualifying ROI")
        if closed_sample_size >= 50:
            why_ranked.append("large enough sample")
        elif closed_sample_size >= 20:
            why_ranked.append("meets sample bar")
        if 10000 <= qualifying_capital <= 100000:
            why_ranked.append("capital in followable band")
        if insider_score >= 60:
            why_ranked.append("insider signal")
        why_ranked = why_ranked[:4]

        qualified.append({
            **_identity(),
            "classification": classification,
            "confidence_label": confidence_label,
            "followability_score": round(followability_score, 1),
            "win_rate": win_rate,
            "closed_sample_size": closed_sample_size,
            "total_closed_count": total_closed_count,
            "qualifying_roi": qr,
            "qualifying_capital": qualifying_capital,
            "qualifying_pnl": qualifying_pnl,
            "pnl": total_pnl,
            "total_pnl": total_pnl,
            "insider_score": insider_score,
            "speculator_score": speculator_score,
            "position_count": position_count,
            "trade_count": trade_count,
            "activity_sources": activity_sources,
            "score_breakdown": {
                "pnl_score": round(pnl_score, 1),
                "win_rate_score": round(win_rate_score, 1),
                "roi_score": round(roi_score, 1),
                "sample_score": round(sample_score, 1),
                "capital_fit_score": round(capital_fit_score, 1),
                "open_loss_penalty": round(open_loss_penalty, 1),
                "pnl_source": "lb_pnl" if lb_pnl_val is not None else ("closed_realized" if closed_realized_pnl_val is not None else "none"),
            },
            "why_ranked": why_ranked,
            "analysis_updated_at": analysis_updated_at,
            "is_followed": wl in followed_set,
            "wallet_quality_score": float(row["wallet_quality_score"]) if row["wallet_quality_score"] is not None else None,
            "wallet_quality_eligible": int(row["wallet_quality_eligible"]) if row["wallet_quality_eligible"] is not None else None,
            "wallet_quality_fail_reason": str(row["wallet_quality_fail_reason"] or "") if row["wallet_quality_fail_reason"] else None,
            "market_maker_detected": bool(int(row["market_maker_detected"] or 0)),
        })

    # Sort qualified: stable two-pass so analysis_updated_at DESC is the final tiebreaker
    qualified.sort(key=lambda w: w.get("analysis_updated_at", ""), reverse=True)
    qualified.sort(key=lambda w: (
        -w["followability_score"],
        -w["win_rate"],
        -(w["qualifying_roi"] or 0),
        -w["closed_sample_size"],
        w["qualifying_capital"],
    ))

    # Sort filtered
    filtered_items.sort(key=lambda w: (
        w.get("filter_reason", ""),
        -w.get("win_rate", 0),
        -(w.get("qualifying_roi") or -999999),
        -w.get("closed_sample_size", 0),
    ))

    filtered_out = filtered_items[:filtered_limit] if include_filtered else []

    # Enrich with market domain context
    all_wallets = [w["wallet"] for w in qualified] + [w["wallet"] for w in filtered_out]
    if all_wallets:
        domains_map = wallet_top_domains(conn, all_wallets, max_domains=3)
        for w in qualified:
            w["top_domains"] = domains_map.get(w["wallet"].lower(), [])
        for w in filtered_out:
            w["top_domains"] = domains_map.get(w["wallet"].lower(), [])

    meta = {
        "candidate_count": total_candidates,
        "qualified_count": len(qualified),
        "filtered_total_count": len(filtered_items),
        "filtered_returned_count": len(filtered_out),
        "min_sample": min_sample,
        "min_capital": min_capital,
        "max_capital": max_capital,
        "min_win_rate": min_win_rate,
        "activity_window_days": activity_window_days,
        "board_goal": "followable_wallets",
    }

    return qualified[:limit], filtered_out, meta


def wallet_top_domains(
    conn: sqlite3.Connection,
    wallets: List[str],
    *,
    max_domains: int = 3,
) -> Dict[str, List[Dict[str, Any]]]:
    """Return top market domains per wallet from position_signals titles.

    Returns {wallet: [{"domain": "Sports", "count": 15, "share": 0.68}, ...]}
    """
    if not wallets:
        return {}

    wallets_lower = [w.lower() for w in wallets]
    ph = ",".join("?" for _ in wallets_lower)
    rows = conn.execute(
        f"""SELECT LOWER(wallet) as wallet, title
            FROM position_signals
            WHERE LOWER(wallet) IN ({ph}) AND title IS NOT NULL AND title != ''""",
        wallets_lower,
    ).fetchall()

    # Group titles by wallet
    from collections import defaultdict, Counter
    wallet_titles: Dict[str, List[str]] = defaultdict(list)
    for r in rows:
        wallet_titles[r["wallet"]].append(r["title"])

    result: Dict[str, List[Dict[str, Any]]] = {}
    for wl in wallets_lower:
        titles = wallet_titles.get(wl, [])
        if not titles:
            result[wl] = []
            continue

        domain_counts: Counter = Counter()
        for t in titles:
            d = _classify_title_domain(t)
            if d:
                domain_counts[d] += 1

        total_classified = sum(domain_counts.values())
        if total_classified == 0:
            result[wl] = []
            continue

        top = domain_counts.most_common(max_domains)
        result[wl] = [
            {"domain": d, "count": c, "share": round(c / total_classified, 2)}
            for d, c in top
        ]
    return result
