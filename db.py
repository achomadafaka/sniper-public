import hashlib
import json
import math
import os
import re
import sqlite3
import time
import datetime as dt
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from uuid import uuid4

from sniper.utils import _normalize_tx_hash as _utils_normalize_tx_hash
from sniper.utils import _normalize_wallet as _utils_normalize_wallet

from sniper.persistence.db_core import (  # noqa: F401 — re-export for backward compatibility
    CORE_SCHEMA_VERSION, DEFAULT_DB_PATH,
    _connection_db_path, _core_schema_is_current, _ensure_column,
    _pragma_user_version, _set_user_version, _table_exists,
    connection_scope, get_connection, get_db_path, get_readonly_connection,
    with_db_init_lock,
)
from sniper.persistence.db_utils import (  # noqa: F401 — re-export for backward compatibility
    _coerce_int, _coerce_float,
    _normalize_wallet, _normalize_tx_hash, _normalize_condition_id, _normalize_outcome,
    _coerce_history_ts, _followed_wallet_activity_key,
    _extract_signal_identity, _build_execution_key,
    _classify_title_domain, _classify_news_domain,
    _followable_clamp, _followable_short_wallet, compute_followability_score,
    _LOG20, _LOG200, _LOG_DENOM,
    _current_instance_tag,
    _normalize_alert_status, _normalize_alert_severity,
    _normalize_agent_mute_scope, _normalize_agent_mute_scope_value,
    _ALERT_STATUSES, _ALERT_SEVERITIES, _AGENT_MUTE_SCOPE_TYPES,
)
from sniper.persistence.db_snapshots import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_snapshots import (  # noqa: F401 — private names needed by tests/patches
    _fetch_clob_outcomes, _H2H_SKIP_PATTERNS,
)
from sniper.persistence.db_leaderboards import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_signals import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_followed import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_markets import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_replay import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_hyperliquid import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_wallet_signals import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.persistence.db_memecoin import *  # noqa: F401,F403 — re-export for backward compatibility
from sniper.analysis.tag_wallet_pnl import ensure_wallet_tag_pnl_table
from sniper.persistence.db_layer5 import (
    ensure_analyzer_runs,
    ensure_position_market_state,
    ensure_position_signal_decision,
    ensure_tag_market_stats,
    ensure_wallet_analysis_batch,
    ensure_wallet_analyzer_intake_queue,
    ensure_wallet_layer5_decision,
)
from sniper.persistence.db_operator_decisions import ensure_operator_decisions


def polymarket_market_outcomes(conn, condition_id: str) -> list[dict]:
    """Return canonical outcome labels for a Polymarket market.

    Tries the ``market_outcomes`` table first, then ``position_signals``,
    then the live CLOB API as a last resort.  Returns a list of dicts,
    each with at least an ``"outcome"`` key.
    """
    # Try market_outcomes first
    rows = conn.execute(
        "SELECT DISTINCT outcome FROM market_outcomes WHERE condition_id = ?",
        (condition_id,),
    ).fetchall()
    if rows:
        return [{"outcome": r["outcome"]} for r in rows]

    # Fallback to position_signals
    rows = conn.execute(
        "SELECT DISTINCT outcome FROM position_signals WHERE condition_id = ?",
        (condition_id,),
    ).fetchall()
    if rows:
        return [{"outcome": r["outcome"]} for r in rows]

    # Last resort: fetch from CLOB API (covers sports markets with no
    # tracked wallets in position_signals or market_outcomes)
    return _fetch_clob_outcomes(condition_id)


MIGRATIONS = {
    "low_buy_trades": [
        ("notional", "REAL"),
        ("market_volume_24h", "REAL"),
        ("source", "TEXT NOT NULL DEFAULT 'realtime'"),
        ("trigger_type", "TEXT NOT NULL DEFAULT 'low_price_entry'"),
        ("trigger_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "user_analysis": [
        ("low_buy_hits_14d", "INTEGER NOT NULL DEFAULT 0"),
        ("avg_low_buy_notional_14d", "REAL NOT NULL DEFAULT 0"),
        ("market_diversity_14d", "INTEGER NOT NULL DEFAULT 0"),
        ("concentration_ratio", "REAL NOT NULL DEFAULT 0"),
        ("suspicion_score", "REAL NOT NULL DEFAULT 0"),
        ("insider_score", "REAL NOT NULL DEFAULT 0"),
        ("speculator_score", "REAL NOT NULL DEFAULT 0"),
        ("classification", "TEXT NOT NULL DEFAULT 'low_signal'"),
        ("tier", "TEXT NOT NULL DEFAULT 'watch'"),
        ("strategy_group", "TEXT NOT NULL DEFAULT 'balanced_mixed'"),
        ("strategy_confidence", "REAL NOT NULL DEFAULT 0"),
        ("strategy_note", "TEXT NOT NULL DEFAULT ''"),
        ("top_open_position_title", "TEXT NOT NULL DEFAULT ''"),
        ("top_open_position_value", "REAL NOT NULL DEFAULT 0"),
        ("wins", "INTEGER NOT NULL DEFAULT 0"),
        ("losses", "INTEGER NOT NULL DEFAULT 0"),
        ("closed_sample_size", "INTEGER NOT NULL DEFAULT 0"),
        ("win_rate", "REAL NOT NULL DEFAULT 0"),
        ("high_winrate_qualified", "INTEGER NOT NULL DEFAULT 0"),
        ("first_activity_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("account_age_days", "REAL NOT NULL DEFAULT 99999"),
        ("is_fresh_account", "INTEGER NOT NULL DEFAULT 0"),
        ("position_signal_count", "INTEGER NOT NULL DEFAULT 0"),
        ("position_insider_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
        ("position_speculator_count", "INTEGER NOT NULL DEFAULT 0"),
        ("max_position_insider_score", "REAL NOT NULL DEFAULT 0"),
        ("max_position_speculator_score", "REAL NOT NULL DEFAULT 0"),
        ("total_capital_deployed", "REAL NOT NULL DEFAULT 0"),
        ("roi_percent", "REAL NOT NULL DEFAULT 0"),
        ("qualifying_pnl", "REAL NOT NULL DEFAULT 0"),
        ("qualifying_capital", "REAL NOT NULL DEFAULT 0"),
        ("qualifying_roi", "REAL"),
        ("analysis_version", "INTEGER NOT NULL DEFAULT 0"),
        ("is_bot", "INTEGER NOT NULL DEFAULT 0"),
        ("bot_detection_reason", "TEXT NOT NULL DEFAULT ''"),
        ("bot_checked_at", "INTEGER NOT NULL DEFAULT 0"),
        ("total_closed_count", "INTEGER NOT NULL DEFAULT 0"),
        ("resolution_rate", "REAL NOT NULL DEFAULT 0"),
        ("effective_resolved_wins", "INTEGER NOT NULL DEFAULT 0"),
        ("effective_resolved_losses", "INTEGER NOT NULL DEFAULT 0"),
        ("lb_pnl", "REAL"),
        ("lb_vol", "REAL"),
        ("lb_rank", "TEXT"),
        ("lb_username", "TEXT"),
        ("lb_updated_at", "TEXT"),
        ("closed_realized_pnl", "REAL NOT NULL DEFAULT 0"),
        ("open_cash_pnl", "REAL NOT NULL DEFAULT 0"),
        ("wallet_edge_tier", "TEXT NOT NULL DEFAULT ''"),
        ("followability_cached", "REAL"),
        # Tier-based probabilistic classification fields (added in v2)
        ("wallet_confidence_score", "REAL NOT NULL DEFAULT 0"),
        ("wallet_tier", "INTEGER NOT NULL DEFAULT 0"),
        ("wallet_domain_stats", "TEXT NOT NULL DEFAULT '{}'"),
        ("insider_score_trend", "REAL NOT NULL DEFAULT 0"),
        ("effective_copy_fraction", "REAL NOT NULL DEFAULT 1.0"),
    ],
    "flagged_users": [
        ("low_buy_hits_14d", "INTEGER NOT NULL DEFAULT 0"),
        ("suspicion_score", "REAL NOT NULL DEFAULT 0"),
        ("insider_score", "REAL NOT NULL DEFAULT 0"),
        ("speculator_score", "REAL NOT NULL DEFAULT 0"),
        ("classification", "TEXT NOT NULL DEFAULT 'low_signal'"),
        ("win_rate", "REAL NOT NULL DEFAULT 0"),
        ("closed_sample_size", "INTEGER NOT NULL DEFAULT 0"),
        ("high_winrate_qualified", "INTEGER NOT NULL DEFAULT 0"),
        ("account_age_days", "REAL NOT NULL DEFAULT 99999"),
        ("is_fresh_account", "INTEGER NOT NULL DEFAULT 0"),
        ("position_signal_count", "INTEGER NOT NULL DEFAULT 0"),
        ("position_insider_candidate_count", "INTEGER NOT NULL DEFAULT 0"),
        ("position_speculator_count", "INTEGER NOT NULL DEFAULT 0"),
        ("max_position_insider_score", "REAL NOT NULL DEFAULT 0"),
        ("max_position_speculator_score", "REAL NOT NULL DEFAULT 0"),
        ("tier", "TEXT NOT NULL DEFAULT 'watch'"),
        ("strategy_group", "TEXT NOT NULL DEFAULT 'balanced_mixed'"),
        ("strategy_confidence", "REAL NOT NULL DEFAULT 0"),
        ("strategy_note", "TEXT NOT NULL DEFAULT ''"),
        ("top_open_position_title", "TEXT NOT NULL DEFAULT ''"),
        ("top_open_position_value", "REAL NOT NULL DEFAULT 0"),
    ],
    "watchlist_wallets": [
        ("label", "TEXT NOT NULL DEFAULT ''"),
        ("last_synced_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "followed_trades": [
        ("status_reason", "TEXT NOT NULL DEFAULT ''"),
        ("is_closed", "INTEGER NOT NULL DEFAULT 0"),
        ("is_win", "INTEGER"),
        ("exited_early", "INTEGER"),
        ("live_price", "REAL"),
        ("exit_price", "REAL"),
        ("current_value", "REAL NOT NULL DEFAULT 0"),
        ("realized_pnl", "REAL NOT NULL DEFAULT 0"),
        ("close_ts", "INTEGER"),
        ("event_end_ts", "INTEGER"),
        ("added_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_synced_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "followed_positions": [
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("event_slug", "TEXT NOT NULL DEFAULT ''"),
        ("label", "TEXT NOT NULL DEFAULT ''"),
        ("added_source", "TEXT NOT NULL DEFAULT 'manual'"),
        ("entry_price", "REAL NOT NULL DEFAULT 0"),
        ("current_price", "REAL NOT NULL DEFAULT 0"),
        ("entry_notional", "REAL NOT NULL DEFAULT 0"),
        ("current_amount", "REAL NOT NULL DEFAULT 0"),
        ("cash_pnl", "REAL"),
        ("realized_pnl", "REAL"),
        ("current_size", "REAL NOT NULL DEFAULT 0"),
        ("added_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_synced_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "ignored_wallets": [
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("ignored_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "wallet_profiles": [
        ("username", "TEXT NOT NULL DEFAULT ''"),
        ("pseudonym", "TEXT NOT NULL DEFAULT ''"),
        ("profile_image", "TEXT NOT NULL DEFAULT ''"),
        ("profile_image_optimized", "TEXT NOT NULL DEFAULT ''"),
        ("bio", "TEXT NOT NULL DEFAULT ''"),
        ("first_activity_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_profile_sync_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("profile_joined_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("profile_prediction_count", "INTEGER NOT NULL DEFAULT 0"),
        ("profile_enriched_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("profile_enrichment_source", "TEXT NOT NULL DEFAULT ''"),
    ],
    "position_signals": [
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("event_slug", "TEXT NOT NULL DEFAULT ''"),
        ("is_open", "INTEGER NOT NULL DEFAULT 1"),
        ("position_value", "REAL NOT NULL DEFAULT 0"),
        ("position_share", "REAL NOT NULL DEFAULT 0"),
        ("position_count_wallet", "INTEGER NOT NULL DEFAULT 0"),
        ("entry_anchor_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("entry_price", "REAL NOT NULL DEFAULT 0"),
        ("entry_size", "REAL NOT NULL DEFAULT 0"),
        ("entry_notional", "REAL NOT NULL DEFAULT 0"),
        ("current_price", "REAL NOT NULL DEFAULT 0"),
        ("current_size", "REAL NOT NULL DEFAULT 0"),
        ("latest_trade_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("buys_5m", "INTEGER NOT NULL DEFAULT 0"),
        ("buys_1h", "INTEGER NOT NULL DEFAULT 0"),
        ("buys_24h", "INTEGER NOT NULL DEFAULT 0"),
        ("trades_5m", "INTEGER NOT NULL DEFAULT 0"),
        ("trades_1h", "INTEGER NOT NULL DEFAULT 0"),
        ("trades_24h", "INTEGER NOT NULL DEFAULT 0"),
        ("sells_5m", "INTEGER NOT NULL DEFAULT 0"),
        ("sells_1h", "INTEGER NOT NULL DEFAULT 0"),
        ("sells_24h", "INTEGER NOT NULL DEFAULT 0"),
        ("notional_5m", "REAL NOT NULL DEFAULT 0"),
        ("notional_1h", "REAL NOT NULL DEFAULT 0"),
        ("notional_24h", "REAL NOT NULL DEFAULT 0"),
        ("sell_notional_5m", "REAL NOT NULL DEFAULT 0"),
        ("sell_notional_1h", "REAL NOT NULL DEFAULT 0"),
        ("sell_notional_24h", "REAL NOT NULL DEFAULT 0"),
        ("buys_since_entry", "INTEGER NOT NULL DEFAULT 0"),
        ("sells_since_entry", "INTEGER NOT NULL DEFAULT 0"),
        ("buy_notional_since_entry", "REAL NOT NULL DEFAULT 0"),
        ("sell_notional_since_entry", "REAL NOT NULL DEFAULT 0"),
        ("net_notional_24h", "REAL NOT NULL DEFAULT 0"),
        ("net_notional_since_entry", "REAL NOT NULL DEFAULT 0"),
        ("last_trade_side", "TEXT NOT NULL DEFAULT ''"),
        ("activity_state", "TEXT NOT NULL DEFAULT 'holding'"),
        ("hold_hours", "REAL NOT NULL DEFAULT -1"),
        ("unique_sizes_5m", "INTEGER NOT NULL DEFAULT 0"),
        ("max_trades_60s", "INTEGER NOT NULL DEFAULT 0"),
        ("position_insider_score", "REAL NOT NULL DEFAULT 0"),
        ("position_speculator_score", "REAL NOT NULL DEFAULT 0"),
        ("position_classification", "TEXT NOT NULL DEFAULT 'position_low_signal'"),
        ("signal_reason", "TEXT NOT NULL DEFAULT ''"),
        ("analyzed_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("analysis_version", "INTEGER NOT NULL DEFAULT 0"),
        ("end_date_iso", "TEXT"),
        # Feedback loop: written back when a paper trade closes
        ("paper_trade_outcome", "TEXT"),
        ("paper_trade_roi", "REAL"),
        ("paper_trade_closed_at", "TIMESTAMP"),
    ],
    "deleted_trades": [
        ("deleted_source", "TEXT NOT NULL DEFAULT ''"),
        ("deleted_reason", "TEXT NOT NULL DEFAULT ''"),
    ],
    "alerts_outbox": [
        ("dedupe_key", "TEXT NOT NULL DEFAULT ''"),
        ("alert_type", "TEXT NOT NULL DEFAULT ''"),
        ("wallet", "TEXT NOT NULL DEFAULT ''"),
        ("condition_id", "TEXT NOT NULL DEFAULT ''"),
        ("outcome", "TEXT NOT NULL DEFAULT ''"),
        ("severity", "TEXT NOT NULL DEFAULT 'info'"),
        ("title", "TEXT NOT NULL DEFAULT ''"),
        ("message", "TEXT NOT NULL DEFAULT ''"),
        ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("status", "TEXT NOT NULL DEFAULT 'new'"),
        ("first_seen_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("last_seen_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
        ("dedupe_until_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("sent_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("acknowledged_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "agent_mutes": [
        ("scope_type", "TEXT NOT NULL DEFAULT 'wallet'"),
        ("scope_value", "TEXT NOT NULL DEFAULT ''"),
        ("reason", "TEXT NOT NULL DEFAULT ''"),
        ("expires_ts", "INTEGER NOT NULL DEFAULT 0"),
        ("created_ts", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def ensure_paper_limit_order_tables(conn: sqlite3.Connection) -> None:
    """Create paper limit order tables for the stink-bid tennis strategy.

    Idempotent — safe to call on every startup.  All DDL uses IF NOT EXISTS.
    These tables are completely separate from the real-order tables
    (order_intents / order_fills).
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS paper_limit_orders (
            order_id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id           INTEGER NOT NULL,
            condition_id          TEXT NOT NULL,
            market_title          TEXT NOT NULL,
            event_slug            TEXT NOT NULL DEFAULT '',
            favorite_token_id     TEXT NOT NULL,
            favorite_token_label  TEXT NOT NULL,
            favorite_price_at_entry REAL NOT NULL,
            bid_price             REAL NOT NULL,
            dip_pct               REAL NOT NULL,
            min_fill_size         REAL NOT NULL DEFAULT 50,
            state                 TEXT NOT NULL DEFAULT 'resting'
                CHECK (state IN ('resting', 'filled', 'cancelled', 'expired')),
            cancel_reason         TEXT NOT NULL DEFAULT '',
            placed_at             TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            closed_at             TEXT NOT NULL DEFAULT '',
            espn_event_id         TEXT NOT NULL DEFAULT '',
            tour                  TEXT NOT NULL DEFAULT '',
            player1               TEXT NOT NULL DEFAULT '',
            player2               TEXT NOT NULL DEFAULT ''
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_paper_limit_orders_strategy_match
            ON paper_limit_orders(strategy_id, condition_id);

        CREATE TABLE IF NOT EXISTS paper_limit_fills (
            fill_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id          INTEGER NOT NULL REFERENCES paper_limit_orders(order_id),
            fill_label        TEXT NOT NULL CHECK (fill_label IN ('strict', 'loose')),
            trade_price       REAL NOT NULL,
            trade_size        REAL NOT NULL,
            trade_side        TEXT NOT NULL DEFAULT '',
            trade_token_id    TEXT NOT NULL DEFAULT '',
            trade_tx_hash     TEXT NOT NULL DEFAULT '',
            trade_ts          TEXT NOT NULL,
            bid_price         REAL NOT NULL,
            slippage          REAL NOT NULL DEFAULT 0,
            created_at        TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS paper_limit_events (
            event_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id          INTEGER,
            event_type        TEXT NOT NULL,
            state_before      TEXT NOT NULL DEFAULT '',
            state_after       TEXT NOT NULL DEFAULT '',
            details_json      TEXT NOT NULL DEFAULT '{}',
            created_at        TEXT NOT NULL
        );
        """
    )


def init_db(conn: sqlite3.Connection) -> None:
    if _core_schema_is_current(conn):
        _ensure_post_gate_idempotent(conn)
        return
    with with_db_init_lock(conn):
        if _core_schema_is_current(conn):
            _ensure_post_gate_idempotent(conn)
            return
        conn.executescript(
            """
        CREATE TABLE IF NOT EXISTS low_buy_trades (
            transaction_hash TEXT PRIMARY KEY,
            proxy_wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            asset TEXT,
            side TEXT NOT NULL,
            price REAL NOT NULL,
            size REAL,
            notional REAL,
            timestamp INTEGER NOT NULL,
            title TEXT,
            slug TEXT,
            event_slug TEXT,
            outcome TEXT,
            ws_price REAL,
            ws_timestamp_ms INTEGER,
            market_volume_24h REAL,
            source TEXT NOT NULL DEFAULT 'realtime',
            trigger_type TEXT NOT NULL DEFAULT 'low_price_entry',
            trigger_reason TEXT NOT NULL DEFAULT '',
            inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_low_buy_wallet ON low_buy_trades(proxy_wallet);
        CREATE INDEX IF NOT EXISTS idx_low_buy_time ON low_buy_trades(timestamp DESC);

        CREATE TABLE IF NOT EXISTS candidate_users (
            wallet TEXT PRIMARY KEY,
            first_seen_ts INTEGER NOT NULL,
            last_seen_ts INTEGER NOT NULL,
            low_buy_hits INTEGER NOT NULL DEFAULT 1,
            last_condition_id TEXT,
            last_title TEXT,
            stage1_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS user_analysis (
            wallet TEXT PRIMARY KEY,
            analyzed_ts INTEGER NOT NULL,
            position_count INTEGER NOT NULL,
            pnl REAL NOT NULL,
            max_position_value REAL NOT NULL,
            top_open_position_title TEXT NOT NULL DEFAULT '',
            top_open_position_value REAL NOT NULL DEFAULT 0,
            total_open_value REAL NOT NULL,
            trade_count INTEGER NOT NULL,
            low_buy_hits_14d INTEGER NOT NULL DEFAULT 0,
            avg_low_buy_notional_14d REAL NOT NULL DEFAULT 0,
            market_diversity_14d INTEGER NOT NULL DEFAULT 0,
            concentration_ratio REAL NOT NULL DEFAULT 0,
            suspicion_score REAL NOT NULL DEFAULT 0,
            insider_score REAL NOT NULL DEFAULT 0,
            speculator_score REAL NOT NULL DEFAULT 0,
            classification TEXT NOT NULL DEFAULT 'low_signal',
            tier TEXT NOT NULL DEFAULT 'watch',
            strategy_group TEXT NOT NULL DEFAULT 'balanced_mixed',
            strategy_confidence REAL NOT NULL DEFAULT 0,
            strategy_note TEXT NOT NULL DEFAULT '',
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            closed_sample_size INTEGER NOT NULL DEFAULT 0,
            win_rate REAL NOT NULL DEFAULT 0,
            high_winrate_qualified INTEGER NOT NULL DEFAULT 0,
            first_activity_ts INTEGER NOT NULL DEFAULT 0,
            account_age_days REAL NOT NULL DEFAULT 99999,
            is_fresh_account INTEGER NOT NULL DEFAULT 0,
            position_signal_count INTEGER NOT NULL DEFAULT 0,
            position_insider_candidate_count INTEGER NOT NULL DEFAULT 0,
            position_speculator_count INTEGER NOT NULL DEFAULT 0,
            max_position_insider_score REAL NOT NULL DEFAULT 0,
            max_position_speculator_score REAL NOT NULL DEFAULT 0,
            total_capital_deployed REAL NOT NULL DEFAULT 0,
            roi_percent REAL NOT NULL DEFAULT 0,
            qualifying_pnl REAL NOT NULL DEFAULT 0,
            qualifying_capital REAL NOT NULL DEFAULT 0,
            qualifying_roi REAL,
            lb_pnl REAL,
            lb_vol REAL,
            lb_rank TEXT,
            lb_username TEXT,
            lb_updated_at TEXT,
            closed_realized_pnl REAL NOT NULL DEFAULT 0,
            open_cash_pnl REAL NOT NULL DEFAULT 0,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            is_bot INTEGER NOT NULL DEFAULT 0,
            bot_detection_reason TEXT NOT NULL DEFAULT '',
            bot_checked_at INTEGER NOT NULL DEFAULT 0,
            total_closed_count INTEGER NOT NULL DEFAULT 0,
            resolution_rate REAL NOT NULL DEFAULT 0,
            effective_resolved_wins INTEGER NOT NULL DEFAULT 0,
            effective_resolved_losses INTEGER NOT NULL DEFAULT 0,
            matches_criteria INTEGER NOT NULL,
            reason TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_match ON user_analysis(matches_criteria, analyzed_ts DESC);

        CREATE TABLE IF NOT EXISTS flagged_users (
            wallet TEXT PRIMARY KEY,
            flagged_ts INTEGER NOT NULL,
            source_note TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- flagged_users is intentionally thin: it only tracks WHICH wallets are
        -- flagged and WHEN. All scoring columns live in user_analysis and are
        -- joined at query time. This eliminates the 24-column duplication that
        -- previously caused stale data whenever user_analysis was updated.

        CREATE TABLE IF NOT EXISTS position_signals (
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_slug TEXT NOT NULL DEFAULT '',
            is_open INTEGER NOT NULL DEFAULT 1,
            position_value REAL NOT NULL DEFAULT 0,
            position_share REAL NOT NULL DEFAULT 0,
            position_count_wallet INTEGER NOT NULL DEFAULT 0,
            entry_anchor_ts INTEGER NOT NULL DEFAULT 0,
            entry_price REAL NOT NULL DEFAULT 0,
            entry_size REAL NOT NULL DEFAULT 0,
            entry_notional REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_size REAL NOT NULL DEFAULT 0,
            latest_trade_ts INTEGER NOT NULL DEFAULT 0,
            buys_5m INTEGER NOT NULL DEFAULT 0,
            buys_1h INTEGER NOT NULL DEFAULT 0,
            buys_24h INTEGER NOT NULL DEFAULT 0,
            trades_5m INTEGER NOT NULL DEFAULT 0,
            trades_1h INTEGER NOT NULL DEFAULT 0,
            trades_24h INTEGER NOT NULL DEFAULT 0,
            sells_5m INTEGER NOT NULL DEFAULT 0,
            sells_1h INTEGER NOT NULL DEFAULT 0,
            sells_24h INTEGER NOT NULL DEFAULT 0,
            notional_5m REAL NOT NULL DEFAULT 0,
            notional_1h REAL NOT NULL DEFAULT 0,
            notional_24h REAL NOT NULL DEFAULT 0,
            sell_notional_5m REAL NOT NULL DEFAULT 0,
            sell_notional_1h REAL NOT NULL DEFAULT 0,
            sell_notional_24h REAL NOT NULL DEFAULT 0,
            buys_since_entry INTEGER NOT NULL DEFAULT 0,
            sells_since_entry INTEGER NOT NULL DEFAULT 0,
            buy_notional_since_entry REAL NOT NULL DEFAULT 0,
            sell_notional_since_entry REAL NOT NULL DEFAULT 0,
            net_notional_24h REAL NOT NULL DEFAULT 0,
            net_notional_since_entry REAL NOT NULL DEFAULT 0,
            last_trade_side TEXT NOT NULL DEFAULT '',
            activity_state TEXT NOT NULL DEFAULT 'holding',
            hold_hours REAL NOT NULL DEFAULT -1,
            unique_sizes_5m INTEGER NOT NULL DEFAULT 0,
            max_trades_60s INTEGER NOT NULL DEFAULT 0,
            position_insider_score REAL NOT NULL DEFAULT 0,
            position_speculator_score REAL NOT NULL DEFAULT 0,
            position_classification TEXT NOT NULL DEFAULT 'position_low_signal',
            signal_reason TEXT NOT NULL DEFAULT '',
            analyzed_ts INTEGER NOT NULL DEFAULT 0,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            paper_trade_outcome TEXT,
            paper_trade_roi REAL,
            paper_trade_closed_at TIMESTAMP,
            PRIMARY KEY (wallet, condition_id, outcome)
        );

        CREATE INDEX IF NOT EXISTS idx_position_signals_wallet
            ON position_signals(wallet, is_open, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_position_signals_class_score
            ON position_signals(position_classification, position_insider_score DESC, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_position_signals_analyzed_ts
            ON position_signals(analyzed_ts DESC);
        CREATE TABLE IF NOT EXISTS analysis_runs (
            analysis_version INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            started_ts INTEGER NOT NULL,
            committed_ts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'started'
                CHECK (status IN ('started', 'committed', 'failed')),
            error TEXT NOT NULL DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_runs_wallet_status
            ON analysis_runs(wallet, status, analysis_version DESC);

        CREATE TABLE IF NOT EXISTS execution_decisions (
            decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_key TEXT NOT NULL UNIQUE,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            action TEXT NOT NULL
                CHECK (action IN ('ENTRY', 'EXIT')),
            analysis_version INTEGER NOT NULL DEFAULT 0,
            signal_ts INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'new'
                CHECK (state IN ('new', 'claimed', 'executed', 'skipped', 'failed')),
            claim_token TEXT NOT NULL DEFAULT '',
            claimed_by TEXT NOT NULL DEFAULT '',
            claimed_ts INTEGER NOT NULL DEFAULT 0,
            completed_ts INTEGER NOT NULL DEFAULT 0,
            position_id INTEGER,
            reason TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_ts INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_execution_decisions_state
            ON execution_decisions(state, created_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_execution_decisions_lookup
            ON execution_decisions(wallet, condition_id, outcome, action, analysis_version);

        CREATE TABLE IF NOT EXISTS strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            state TEXT NOT NULL
                CHECK (state IN ('pending_entry', 'open', 'pending_exit', 'closed', 'error')),
            entry_decision_id INTEGER,
            exit_decision_id INTEGER,
            token_id TEXT NOT NULL DEFAULT '',
            entry_price REAL NOT NULL DEFAULT 0,
            avg_entry_price REAL NOT NULL DEFAULT 0,
            entry_shares REAL NOT NULL DEFAULT 0,
            filled_entry_shares REAL NOT NULL DEFAULT 0,
            avg_exit_price REAL NOT NULL DEFAULT 0,
            filled_exit_shares REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            opened_ts INTEGER NOT NULL DEFAULT 0,
            closed_ts INTEGER NOT NULL DEFAULT 0,
            last_reconciled_ts INTEGER NOT NULL DEFAULT 0,
            version INTEGER NOT NULL DEFAULT 0,
            updated_ts INTEGER NOT NULL,
            instance_tag TEXT,
            end_date_iso TEXT NOT NULL DEFAULT '',
            entry_insider_score REAL NOT NULL DEFAULT 0,
            size_usdc REAL NOT NULL DEFAULT 0,
            pnl_usdc REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_positions_state
            ON strategy_positions(state, updated_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_positions_strategy
            ON strategy_positions(strategy_id, state, updated_ts DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_strategy_positions_active_owner
            ON strategy_positions(wallet, condition_id, outcome)
            WHERE state IN ('pending_entry', 'open', 'pending_exit');

        CREATE TABLE IF NOT EXISTS strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            strategy_type TEXT NOT NULL DEFAULT 'signal_only',
            parameters TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'active',
            ai_generated INTEGER NOT NULL DEFAULT 0,
            ai_analysis TEXT NOT NULL DEFAULT '',
            trades_triggered INTEGER NOT NULL DEFAULT 0,
            pnl_usdc REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            instance_tag TEXT,
            paused_at TEXT,
            pause_reason TEXT,
            lifecycle_stage TEXT NOT NULL DEFAULT 'paper'
        );

        CREATE INDEX IF NOT EXISTS idx_strategies_wallet_status
            ON strategies(wallet, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            execution_type TEXT NOT NULL DEFAULT 'paper',
            size_usdc REAL NOT NULL DEFAULT 0,
            entry_price REAL NOT NULL DEFAULT 0,
            token_id TEXT NOT NULL DEFAULT '',
            order_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            pnl_usdc REAL NOT NULL DEFAULT 0,
            error_msg TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_executions_strategy_created
            ON strategy_executions(strategy_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at TEXT NOT NULL DEFAULT '',
            discovered_count INTEGER NOT NULL DEFAULT 0,
            entered_count INTEGER NOT NULL DEFAULT 0,
            exited_count INTEGER NOT NULL DEFAULT 0,
            open_positions INTEGER NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            unrealized_pnl REAL NOT NULL DEFAULT 0,
            error_msg TEXT NOT NULL DEFAULT '',
            details_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_runs_strategy_started
            ON strategy_runs(strategy_id, started_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_research (
            slug TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            hypothesis TEXT NOT NULL DEFAULT '',
            edge_summary TEXT NOT NULL DEFAULT '',
            entry_logic TEXT NOT NULL DEFAULT '',
            exit_logic TEXT NOT NULL DEFAULT '',
            risk_notes TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            source_wallet TEXT NOT NULL DEFAULT '',
            source_market TEXT NOT NULL DEFAULT '',
            sample_size INTEGER NOT NULL DEFAULT 0,
            tags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_strategy_research_status
            ON strategy_research(status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS order_intents (
            intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            decision_id INTEGER NOT NULL,
            side TEXT NOT NULL
                CHECK (side IN ('BUY', 'SELL')),
            token_id TEXT NOT NULL,
            limit_price REAL NOT NULL CHECK (limit_price > 0),
            requested_shares REAL NOT NULL CHECK (requested_shares > 0),
            requested_notional REAL NOT NULL CHECK (requested_notional >= 0),
            exchange_order_id TEXT NOT NULL DEFAULT '',
            client_order_id TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending_submit'
                CHECK (state IN ('pending_submit', 'submitted', 'partially_filled', 'filled', 'cancelled', 'rejected', 'error')),
            filled_shares REAL NOT NULL DEFAULT 0,
            filled_notional REAL NOT NULL DEFAULT 0,
            avg_fill_price REAL NOT NULL DEFAULT 0,
            submit_ts INTEGER NOT NULL DEFAULT 0,
            last_update_ts INTEGER NOT NULL DEFAULT 0,
            close_ts INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '',
            raw_exchange_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_order_intents_decision_side
            ON order_intents(decision_id, side);
        CREATE UNIQUE INDEX IF NOT EXISTS uq_order_intents_exchange_order
            ON order_intents(exchange_order_id)
            WHERE exchange_order_id <> '';
        CREATE INDEX IF NOT EXISTS idx_order_intents_state
            ON order_intents(state, last_update_ts DESC);

        CREATE TABLE IF NOT EXISTS order_fills (
            fill_pk TEXT PRIMARY KEY,
            intent_id INTEGER NOT NULL,
            exchange_order_id TEXT NOT NULL,
            fill_ts INTEGER NOT NULL,
            side TEXT NOT NULL
                CHECK (side IN ('BUY', 'SELL')),
            price REAL NOT NULL CHECK (price > 0),
            shares REAL NOT NULL CHECK (shares > 0),
            notional REAL NOT NULL CHECK (notional >= 0),
            fee REAL NOT NULL DEFAULT 0,
            tx_hash TEXT NOT NULL DEFAULT '',
            raw_exchange_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE INDEX IF NOT EXISTS idx_order_fills_intent_ts
            ON order_fills(intent_id, fill_ts DESC);

        CREATE TABLE IF NOT EXISTS wallet_profiles (
            wallet TEXT PRIMARY KEY,
            username TEXT NOT NULL DEFAULT '',
            pseudonym TEXT NOT NULL DEFAULT '',
            profile_image TEXT NOT NULL DEFAULT '',
            profile_image_optimized TEXT NOT NULL DEFAULT '',
            bio TEXT NOT NULL DEFAULT '',
            first_activity_ts INTEGER NOT NULL DEFAULT 0,
            last_profile_sync_ts INTEGER NOT NULL DEFAULT 0,
            profile_joined_ts INTEGER NOT NULL DEFAULT 0,
            profile_prediction_count INTEGER NOT NULL DEFAULT 0,
            profile_enriched_ts INTEGER NOT NULL DEFAULT 0,
            profile_enrichment_source TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watchlist_wallets (
            wallet TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            added_ts INTEGER NOT NULL,
            last_synced_ts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS followed_wallets (
            wallet TEXT PRIMARY KEY,
            label TEXT NOT NULL DEFAULT '',
            added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            added_reason TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            strategy_json TEXT,
            notes TEXT,
            total_positions INTEGER NOT NULL DEFAULT 0,
            total_pnl_usdc REAL NOT NULL DEFAULT 0,
            last_copied_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_followed_status
            ON followed_wallets(status);

        CREATE TABLE IF NOT EXISTS watchlist_trades (
            wallet TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            condition_id TEXT,
            side TEXT,
            price REAL NOT NULL,
            size REAL NOT NULL,
            notional REAL NOT NULL,
            timestamp INTEGER NOT NULL,
            title TEXT,
            outcome TEXT,
            event_slug TEXT,
            source TEXT NOT NULL DEFAULT 'watchlist_sync',
            inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (wallet, transaction_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_watchlist_trades_time ON watchlist_trades(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_watchlist_trades_wallet_time ON watchlist_trades(wallet, timestamp DESC);

        CREATE TABLE IF NOT EXISTS followed_trades (
            wallet TEXT NOT NULL,
            transaction_hash TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            side TEXT,
            outcome TEXT,
            title TEXT,
            event_slug TEXT,
            entry_price REAL NOT NULL,
            entry_size REAL NOT NULL,
            entry_notional REAL NOT NULL,
            entry_ts INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            status_label TEXT NOT NULL DEFAULT 'Active',
            status_reason TEXT NOT NULL DEFAULT '',
            is_closed INTEGER NOT NULL DEFAULT 0,
            is_win INTEGER,
            exited_early INTEGER,
            live_price REAL,
            exit_price REAL,
            current_value REAL NOT NULL DEFAULT 0,
            realized_pnl REAL NOT NULL DEFAULT 0,
            close_ts INTEGER,
            event_end_ts INTEGER,
            added_ts INTEGER NOT NULL,
            last_synced_ts INTEGER NOT NULL DEFAULT 0,
            inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (wallet, transaction_hash)
        );

        CREATE INDEX IF NOT EXISTS idx_followed_trades_wallet ON followed_trades(wallet);
        CREATE INDEX IF NOT EXISTS idx_followed_trades_status ON followed_trades(status, added_ts DESC);

        CREATE TABLE IF NOT EXISTS followed_wallet_activity (
            activity_key TEXT PRIMARY KEY,
            wallet TEXT NOT NULL,
            transaction_hash TEXT NOT NULL DEFAULT '',
            condition_id TEXT NOT NULL DEFAULT '',
            activity_type TEXT NOT NULL DEFAULT '',
            side TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            slug TEXT NOT NULL DEFAULT '',
            event_slug TEXT NOT NULL DEFAULT '',
            price REAL NOT NULL DEFAULT 0,
            size REAL NOT NULL DEFAULT 0,
            usdc_size REAL NOT NULL DEFAULT 0,
            activity_ts INTEGER NOT NULL DEFAULT 0,
            source_endpoint TEXT NOT NULL DEFAULT 'activity',
            raw_json TEXT NOT NULL DEFAULT '{}',
            inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_followed_wallet_activity_wallet_ts
            ON followed_wallet_activity(wallet, activity_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_followed_wallet_activity_condition_ts
            ON followed_wallet_activity(condition_id, activity_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_fwa_reconcile
            ON followed_wallet_activity(wallet, condition_id, outcome, activity_type, side, activity_ts);

        CREATE TABLE IF NOT EXISTS followed_positions (
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            event_slug TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            added_source TEXT NOT NULL DEFAULT 'manual',
            entry_price REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            entry_notional REAL NOT NULL DEFAULT 0,
            current_amount REAL NOT NULL DEFAULT 0,
            current_size REAL NOT NULL DEFAULT 0,
            added_ts INTEGER NOT NULL,
            last_synced_ts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (wallet, condition_id, outcome)
        );

        CREATE INDEX IF NOT EXISTS idx_followed_positions_wallet
            ON followed_positions(wallet, last_synced_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_followed_positions_sync
            ON followed_positions(last_synced_ts ASC, added_ts DESC);

        CREATE TABLE IF NOT EXISTS dismissed_suspicious_trades (
            transaction_hash TEXT PRIMARY KEY,
            wallet TEXT,
            condition_id TEXT,
            title TEXT,
            note TEXT NOT NULL DEFAULT '',
            dismissed_ts INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_dismissed_suspicious_trades_ts
            ON dismissed_suspicious_trades(dismissed_ts DESC);

        CREATE TABLE IF NOT EXISTS ignored_wallets (
            wallet TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            ignored_ts INTEGER NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_ignored_wallets_ts
            ON ignored_wallets(ignored_ts DESC);

        CREATE TABLE IF NOT EXISTS blocked_wallets (
            wallet TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '',
            blocked_ts INTEGER NOT NULL,
            blocked_by TEXT NOT NULL DEFAULT 'user',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_blocked_wallets_ts
            ON blocked_wallets(blocked_ts DESC);

        CREATE TABLE IF NOT EXISTS external_leaderboard_entries (
            wallet TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'OVERALL',
            time_period TEXT NOT NULL DEFAULT 'ALL',
            order_by TEXT NOT NULL DEFAULT 'PNL',
            rank INTEGER NOT NULL,
            official_pnl REAL NOT NULL DEFAULT 0,
            official_volume REAL NOT NULL DEFAULT 0,
            username TEXT NOT NULL DEFAULT '',
            profile_image TEXT NOT NULL DEFAULT '',
            x_username TEXT NOT NULL DEFAULT '',
            verified_badge INTEGER NOT NULL DEFAULT 0,
            fetched_at INTEGER NOT NULL,
            PRIMARY KEY (wallet, category, time_period, order_by)
        );

        CREATE INDEX IF NOT EXISTS idx_ext_lb_cat_time_order_rank
            ON external_leaderboard_entries (category, time_period, order_by, rank);
        CREATE INDEX IF NOT EXISTS idx_ext_lb_fetched
            ON external_leaderboard_entries (fetched_at);

        CREATE TABLE IF NOT EXISTS deleted_trades (
            transaction_hash TEXT PRIMARY KEY,
            deleted_ts INTEGER NOT NULL,
            deleted_source TEXT NOT NULL DEFAULT '',
            deleted_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_deleted_trades_ts
            ON deleted_trades(deleted_ts DESC);

        CREATE TABLE IF NOT EXISTS alerts_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT NOT NULL UNIQUE,
            alert_type TEXT NOT NULL,
            wallet TEXT NOT NULL DEFAULT '',
            condition_id TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL DEFAULT '',
            severity TEXT NOT NULL DEFAULT 'info',
            title TEXT NOT NULL DEFAULT '',
            message TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'new',
            first_seen_ts INTEGER NOT NULL DEFAULT 0,
            last_seen_ts INTEGER NOT NULL DEFAULT 0,
            seen_count INTEGER NOT NULL DEFAULT 1,
            dedupe_until_ts INTEGER NOT NULL DEFAULT 0,
            sent_ts INTEGER NOT NULL DEFAULT 0,
            acknowledged_ts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_alerts_outbox_status_seen
            ON alerts_outbox(status, last_seen_ts DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_outbox_wallet_seen
            ON alerts_outbox(wallet, last_seen_ts DESC, id DESC);

        CREATE TABLE IF NOT EXISTS agent_mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope_type TEXT NOT NULL DEFAULT 'wallet',
            scope_value TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            expires_ts INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_agent_mutes_scope
            ON agent_mutes(scope_type, scope_value);
        CREATE INDEX IF NOT EXISTS idx_agent_mutes_expires
            ON agent_mutes(expires_ts, created_ts DESC);

        CREATE TABLE IF NOT EXISTS mcp_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tool_name TEXT NOT NULL,
            caller TEXT NOT NULL DEFAULT '',
            args_json TEXT NOT NULL DEFAULT '{}',
            result_summary TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_mcp_audit_log_tool_created
            ON mcp_audit_log(tool_name, created_at DESC);
            """
        )
        # Hard cleanup after dropping Kalshi support.
        conn.execute("DROP TABLE IF EXISTS kalshi_low_buy_alerts")

        # Backward-compatible migrations for existing DBs.
        for table, columns in MIGRATIONS.items():
            for column, definition in columns:
                _ensure_column(conn, table, column, definition)

        # Create indexes that depend on migrated columns.
        conn.execute(
            """
        CREATE INDEX IF NOT EXISTS idx_position_signals_wallet_version_open
            ON position_signals(wallet, analysis_version DESC, is_open)
            """
        )
        conn.execute(
            """
        CREATE INDEX IF NOT EXISTS idx_user_analysis_wallet_version
            ON user_analysis(wallet, analysis_version DESC)
            """
        )

        # Fill derived values for rows created before schema upgrades.
        conn.execute(
            "UPDATE low_buy_trades SET notional = COALESCE(price, 0) * COALESCE(size, 0) WHERE notional IS NULL"
        )
        conn.execute("UPDATE low_buy_trades SET market_volume_24h = 0 WHERE market_volume_24h IS NULL")
        conn.execute("UPDATE low_buy_trades SET source = 'realtime' WHERE source IS NULL OR source = ''")
        conn.execute(
            "UPDATE low_buy_trades SET trigger_type = 'low_price_entry' WHERE trigger_type IS NULL OR trigger_type = ''"
        )
        conn.execute("UPDATE low_buy_trades SET trigger_reason = '' WHERE trigger_reason IS NULL")
        conn.execute("UPDATE followed_trades SET added_ts = entry_ts WHERE added_ts <= 0")
        conn.execute("UPDATE followed_trades SET last_synced_ts = 0 WHERE last_synced_ts IS NULL")
        conn.execute("UPDATE followed_trades SET status_reason = '' WHERE status_reason IS NULL")
        conn.execute("UPDATE followed_trades SET current_value = 0 WHERE current_value IS NULL")
        conn.execute("UPDATE followed_trades SET realized_pnl = 0 WHERE realized_pnl IS NULL")
        conn.execute("UPDATE followed_wallets SET label = '' WHERE label IS NULL")
        conn.execute("UPDATE followed_wallets SET status = 'active' WHERE status IS NULL OR status = ''")
        conn.execute("UPDATE followed_wallets SET strategy_json = NULL WHERE strategy_json = ''")
        conn.execute("UPDATE followed_wallets SET notes = '' WHERE notes IS NULL")
        conn.execute("UPDATE followed_wallets SET total_positions = 0 WHERE total_positions IS NULL")
        conn.execute("UPDATE followed_wallets SET total_pnl_usdc = 0 WHERE total_pnl_usdc IS NULL")
        conn.execute("UPDATE followed_positions SET title = '' WHERE title IS NULL")
        conn.execute("UPDATE followed_positions SET event_slug = '' WHERE event_slug IS NULL")
        conn.execute("UPDATE followed_positions SET label = '' WHERE label IS NULL")
        conn.execute("UPDATE followed_positions SET added_source = 'manual' WHERE added_source IS NULL OR added_source = ''")
        conn.execute("UPDATE followed_positions SET entry_price = 0 WHERE entry_price IS NULL")
        conn.execute("UPDATE followed_positions SET current_price = 0 WHERE current_price IS NULL")
        conn.execute("UPDATE followed_positions SET entry_notional = 0 WHERE entry_notional IS NULL")
        conn.execute("UPDATE followed_positions SET current_amount = 0 WHERE current_amount IS NULL")
        conn.execute("UPDATE followed_positions SET current_size = 0 WHERE current_size IS NULL")
        conn.execute("UPDATE followed_positions SET added_ts = 0 WHERE added_ts IS NULL")
        conn.execute("UPDATE followed_positions SET last_synced_ts = 0 WHERE last_synced_ts IS NULL")
        conn.execute("UPDATE ignored_wallets SET reason = '' WHERE reason IS NULL")
        conn.execute("UPDATE ignored_wallets SET ignored_ts = 0 WHERE ignored_ts IS NULL")
        conn.execute("UPDATE wallet_profiles SET username = '' WHERE username IS NULL")
        conn.execute("UPDATE wallet_profiles SET pseudonym = '' WHERE pseudonym IS NULL")
        conn.execute("UPDATE wallet_profiles SET profile_image = '' WHERE profile_image IS NULL")
        conn.execute("UPDATE wallet_profiles SET profile_image_optimized = '' WHERE profile_image_optimized IS NULL")
        conn.execute("UPDATE wallet_profiles SET bio = '' WHERE bio IS NULL")
        conn.execute("UPDATE wallet_profiles SET first_activity_ts = 0 WHERE first_activity_ts IS NULL")
        conn.execute("UPDATE wallet_profiles SET last_profile_sync_ts = 0 WHERE last_profile_sync_ts IS NULL")
        conn.execute("UPDATE wallet_profiles SET profile_joined_ts = 0 WHERE profile_joined_ts IS NULL")
        conn.execute("UPDATE wallet_profiles SET profile_prediction_count = 0 WHERE profile_prediction_count IS NULL")
        conn.execute("UPDATE wallet_profiles SET profile_enriched_ts = 0 WHERE profile_enriched_ts IS NULL")
        conn.execute(
            "UPDATE wallet_profiles SET profile_enrichment_source = '' "
            "WHERE profile_enrichment_source IS NULL"
        )
        conn.execute("UPDATE position_signals SET title = '' WHERE title IS NULL")
        conn.execute("UPDATE position_signals SET event_slug = '' WHERE event_slug IS NULL")
        conn.execute("UPDATE position_signals SET is_open = 1 WHERE is_open IS NULL")
        conn.execute("UPDATE position_signals SET position_value = 0 WHERE position_value IS NULL")
        conn.execute("UPDATE position_signals SET position_share = 0 WHERE position_share IS NULL")
        conn.execute("UPDATE position_signals SET position_count_wallet = 0 WHERE position_count_wallet IS NULL")
        conn.execute("UPDATE position_signals SET entry_anchor_ts = 0 WHERE entry_anchor_ts IS NULL")
        conn.execute("UPDATE position_signals SET entry_price = 0 WHERE entry_price IS NULL")
        conn.execute("UPDATE position_signals SET entry_size = 0 WHERE entry_size IS NULL")
        conn.execute("UPDATE position_signals SET entry_notional = 0 WHERE entry_notional IS NULL")
        conn.execute("UPDATE position_signals SET current_price = 0 WHERE current_price IS NULL")
        conn.execute("UPDATE position_signals SET current_size = 0 WHERE current_size IS NULL")
        conn.execute("UPDATE position_signals SET latest_trade_ts = 0 WHERE latest_trade_ts IS NULL")
        conn.execute("UPDATE position_signals SET buys_5m = 0 WHERE buys_5m IS NULL")
        conn.execute("UPDATE position_signals SET buys_1h = 0 WHERE buys_1h IS NULL")
        conn.execute("UPDATE position_signals SET buys_24h = 0 WHERE buys_24h IS NULL")
        conn.execute("UPDATE position_signals SET trades_5m = 0 WHERE trades_5m IS NULL")
        conn.execute("UPDATE position_signals SET trades_1h = 0 WHERE trades_1h IS NULL")
        conn.execute("UPDATE position_signals SET trades_24h = 0 WHERE trades_24h IS NULL")
        conn.execute("UPDATE position_signals SET sells_5m = 0 WHERE sells_5m IS NULL")
        conn.execute("UPDATE position_signals SET sells_1h = 0 WHERE sells_1h IS NULL")
        conn.execute("UPDATE position_signals SET sells_24h = 0 WHERE sells_24h IS NULL")
        conn.execute("UPDATE position_signals SET notional_5m = 0 WHERE notional_5m IS NULL")
        conn.execute("UPDATE position_signals SET notional_1h = 0 WHERE notional_1h IS NULL")
        conn.execute("UPDATE position_signals SET notional_24h = 0 WHERE notional_24h IS NULL")
        conn.execute("UPDATE position_signals SET sell_notional_5m = 0 WHERE sell_notional_5m IS NULL")
        conn.execute("UPDATE position_signals SET sell_notional_1h = 0 WHERE sell_notional_1h IS NULL")
        conn.execute("UPDATE position_signals SET sell_notional_24h = 0 WHERE sell_notional_24h IS NULL")
        conn.execute("UPDATE position_signals SET buys_since_entry = 0 WHERE buys_since_entry IS NULL")
        conn.execute("UPDATE position_signals SET sells_since_entry = 0 WHERE sells_since_entry IS NULL")
        conn.execute("UPDATE position_signals SET buy_notional_since_entry = 0 WHERE buy_notional_since_entry IS NULL")
        conn.execute("UPDATE position_signals SET sell_notional_since_entry = 0 WHERE sell_notional_since_entry IS NULL")
        conn.execute("UPDATE position_signals SET net_notional_24h = 0 WHERE net_notional_24h IS NULL")
        conn.execute("UPDATE position_signals SET net_notional_since_entry = 0 WHERE net_notional_since_entry IS NULL")
        conn.execute("UPDATE position_signals SET last_trade_side = '' WHERE last_trade_side IS NULL")
        conn.execute("UPDATE position_signals SET activity_state = 'holding' WHERE activity_state IS NULL OR activity_state = ''")
        conn.execute("UPDATE position_signals SET hold_hours = -1 WHERE hold_hours IS NULL OR hold_hours >= 99998")
        conn.execute("UPDATE position_signals SET unique_sizes_5m = 0 WHERE unique_sizes_5m IS NULL")
        conn.execute("UPDATE position_signals SET max_trades_60s = 0 WHERE max_trades_60s IS NULL")
        conn.execute("UPDATE position_signals SET position_insider_score = 0 WHERE position_insider_score IS NULL")
        conn.execute("UPDATE position_signals SET position_speculator_score = 0 WHERE position_speculator_score IS NULL")
        conn.execute(
            "UPDATE position_signals SET position_classification = 'position_low_signal' "
            "WHERE position_classification IS NULL OR position_classification = ''"
        )
        conn.execute("UPDATE position_signals SET signal_reason = '' WHERE signal_reason IS NULL")
        conn.execute("UPDATE position_signals SET analyzed_ts = 0 WHERE analyzed_ts IS NULL")
        conn.execute("UPDATE position_signals SET analysis_version = 0 WHERE analysis_version IS NULL")
        conn.execute("UPDATE deleted_trades SET deleted_source = '' WHERE deleted_source IS NULL")
        conn.execute("UPDATE deleted_trades SET deleted_reason = '' WHERE deleted_reason IS NULL")
        conn.execute("UPDATE alerts_outbox SET dedupe_key = '' WHERE dedupe_key IS NULL")
        conn.execute("UPDATE alerts_outbox SET alert_type = '' WHERE alert_type IS NULL")
        conn.execute("UPDATE alerts_outbox SET wallet = '' WHERE wallet IS NULL")
        conn.execute("UPDATE alerts_outbox SET condition_id = '' WHERE condition_id IS NULL")
        conn.execute("UPDATE alerts_outbox SET outcome = '' WHERE outcome IS NULL")
        conn.execute("UPDATE alerts_outbox SET severity = 'info' WHERE severity IS NULL OR severity = ''")
        conn.execute("UPDATE alerts_outbox SET title = '' WHERE title IS NULL")
        conn.execute("UPDATE alerts_outbox SET message = '' WHERE message IS NULL")
        conn.execute("UPDATE alerts_outbox SET payload_json = '{}' WHERE payload_json IS NULL OR payload_json = ''")
        conn.execute("UPDATE alerts_outbox SET status = 'new' WHERE status IS NULL OR status = ''")
        conn.execute("UPDATE alerts_outbox SET first_seen_ts = 0 WHERE first_seen_ts IS NULL")
        conn.execute("UPDATE alerts_outbox SET last_seen_ts = 0 WHERE last_seen_ts IS NULL")
        conn.execute("UPDATE alerts_outbox SET seen_count = 1 WHERE seen_count IS NULL OR seen_count <= 0")
        conn.execute("UPDATE alerts_outbox SET dedupe_until_ts = 0 WHERE dedupe_until_ts IS NULL")
        conn.execute("UPDATE alerts_outbox SET sent_ts = 0 WHERE sent_ts IS NULL")
        conn.execute("UPDATE alerts_outbox SET acknowledged_ts = 0 WHERE acknowledged_ts IS NULL")
        conn.execute(
            "UPDATE agent_mutes SET scope_type = 'wallet' "
            "WHERE scope_type IS NULL OR scope_type = ''"
        )
        conn.execute("UPDATE agent_mutes SET scope_value = '' WHERE scope_value IS NULL")
        conn.execute("UPDATE agent_mutes SET reason = '' WHERE reason IS NULL")
        conn.execute("UPDATE agent_mutes SET expires_ts = 0 WHERE expires_ts IS NULL")
        conn.execute("UPDATE agent_mutes SET created_ts = 0 WHERE created_ts IS NULL")
        conn.execute("UPDATE user_analysis SET position_signal_count = 0 WHERE position_signal_count IS NULL")
        conn.execute(
            "UPDATE user_analysis SET position_insider_candidate_count = 0 WHERE position_insider_candidate_count IS NULL"
        )
        conn.execute("UPDATE user_analysis SET position_speculator_count = 0 WHERE position_speculator_count IS NULL")
        conn.execute("UPDATE user_analysis SET max_position_insider_score = 0 WHERE max_position_insider_score IS NULL")
        conn.execute(
            "UPDATE user_analysis SET max_position_speculator_score = 0 WHERE max_position_speculator_score IS NULL"
        )
        conn.execute("UPDATE user_analysis SET total_capital_deployed = 0 WHERE total_capital_deployed IS NULL")
        conn.execute("UPDATE user_analysis SET roi_percent = 0 WHERE roi_percent IS NULL")
        conn.execute("UPDATE user_analysis SET analysis_version = 0 WHERE analysis_version IS NULL")
        conn.execute("UPDATE user_analysis SET is_bot = 0 WHERE is_bot IS NULL")
        conn.execute("UPDATE user_analysis SET bot_detection_reason = '' WHERE bot_detection_reason IS NULL")
        conn.execute("UPDATE user_analysis SET bot_checked_at = 0 WHERE bot_checked_at IS NULL")
        conn.execute("UPDATE user_analysis SET total_closed_count = 0 WHERE total_closed_count IS NULL")
        conn.execute("UPDATE user_analysis SET resolution_rate = 0 WHERE resolution_rate IS NULL")
        conn.execute("UPDATE user_analysis SET effective_resolved_wins = 0 WHERE effective_resolved_wins IS NULL")
        conn.execute("UPDATE user_analysis SET effective_resolved_losses = 0 WHERE effective_resolved_losses IS NULL")
        conn.execute("UPDATE flagged_users SET position_signal_count = 0 WHERE position_signal_count IS NULL")
        conn.execute(
            "UPDATE flagged_users SET position_insider_candidate_count = 0 WHERE position_insider_candidate_count IS NULL"
        )
        conn.execute("UPDATE flagged_users SET position_speculator_count = 0 WHERE position_speculator_count IS NULL")
        conn.execute("UPDATE flagged_users SET max_position_insider_score = 0 WHERE max_position_insider_score IS NULL")
        conn.execute(
            "UPDATE flagged_users SET max_position_speculator_score = 0 WHERE max_position_speculator_score IS NULL"
        )
        conn.commit()
        _set_user_version(conn, CORE_SCHEMA_VERSION)
        conn.commit()
        _ensure_post_gate_idempotent(conn)


def _ensure_post_gate_idempotent(conn: sqlite3.Connection) -> None:
    """Idempotent schema ensure-calls that run on EVERY init_db invocation.

    These catch tables added after CORE_SCHEMA_VERSION was last bumped; without
    this, existing production DBs with schema already "current" never gain
    new tables (e.g. paper_limit_orders added post-v2). All DDL inside uses
    CREATE TABLE/INDEX IF NOT EXISTS and is safe to call repeatedly.
    """
    ensure_wallet_directory_schema(conn)
    ensure_all_hl_tables(conn)
    ensure_wallet_signals_tables(conn)
    ensure_paper_limit_order_tables(conn)
    ensure_analyzer_runs(conn)
    ensure_wallet_layer5_decision(conn)
    ensure_position_signal_decision(conn)
    ensure_tag_market_stats(conn)
    ensure_position_market_state(conn)
    ensure_wallet_analysis_batch(conn)
    ensure_wallet_analyzer_intake_queue(conn)
    ensure_wallet_tag_pnl_table(conn)
    ensure_operator_decisions(conn)
    apply_phase_b_hardening(conn)
    conn.commit()


def apply_phase_b_hardening(conn: sqlite3.Connection) -> None:
    """Phase B schema hardening migration (2026-03-01).

    Idempotent — safe to call on every startup.  Creates the market_metadata
    canonical table and adds missing performance indexes that were absent from
    the Phase A schema.

    Design rules:
    - All CREATE TABLE / CREATE INDEX statements use IF NOT EXISTS → zero-op on
      subsequent runs.
    - No destructive DDL (no DROP, no column removal).
    - Backfill UPDATE statements are skipped if the column already has data, so
      re-runs are cheap.

    Phase B goals:
    1. market_metadata  — single source of truth for condition→market info so
       stream, analyzer, and alerter all read from DB instead of in-memory dicts.
    2. market_outcomes — per-market settlement outcome scaffolding for Phase 2
       ROI-by-trade leaderboard and signal validation.
    3. positions — live position aggregator keyed by (wallet, condition_id, outcome).
    4. Missing composite indexes on hot query paths (flagged by EXPLAIN QUERY PLAN).
    5. low_buy_trades composite index for wallet+timestamp range scans.
    6. candidate_users index on last_seen_ts for TTL/pruning queries.
    7. user_analysis classification+score index for alerter ORDER BY queries.
    """
    # condition_id composite index: only safe when the column exists (legacy DBs
    # created before 2026-02-20 may not have it if init_db never added it via
    # _ensure_column).  Create it here, outside executescript, so we can guard.
    _cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(low_buy_trades)").fetchall()
    }
    if "condition_id" in _cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_low_buy_condition_ts "
            "ON low_buy_trades(condition_id, timestamp DESC)"
        )
        conn.commit()

    conn.executescript(
        """
        -- ── 1. market_metadata ─────────────────────────────────────────────
        -- Canonical store for market-level metadata, populated by stream_low_buys
        -- during startup and on each metadata refresh cycle.  Consumers (alerter,
        -- paper_trader, web_server) read from here instead of from in-memory dicts.
        CREATE TABLE IF NOT EXISTS market_metadata (
            condition_id  TEXT PRIMARY KEY,
            title         TEXT NOT NULL DEFAULT '',
            slug          TEXT NOT NULL DEFAULT '',
            event_slug    TEXT NOT NULL DEFAULT '',
            end_date_iso  TEXT NOT NULL DEFAULT '',
            category      TEXT NOT NULL DEFAULT '',
            category_source TEXT NOT NULL DEFAULT '',
            volume_24h    REAL NOT NULL DEFAULT 0.0,
            is_open       INTEGER NOT NULL DEFAULT 1,
            last_synced_ts INTEGER NOT NULL DEFAULT 0,
            inserted_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Index: fast lookup of recently synced markets (used by refresh diff)
        CREATE INDEX IF NOT EXISTS idx_market_metadata_synced
            ON market_metadata(last_synced_ts DESC);

        -- Index: open markets by volume (used by alerter to filter low-volume)
        CREATE INDEX IF NOT EXISTS idx_market_metadata_open_volume
            ON market_metadata(is_open, volume_24h DESC);

        -- ── 2. market_outcomes (Phase 2 scaffold) ──────────────────────────
        -- Stores final resolved payout per side for each market.
        -- resolved_price: winner=1.0, loser=0.0.
        -- This table is intentionally independent from paper_trades so it can
        -- evaluate ALL observed wallets in low_buy_trades, not only followed ones.
        CREATE TABLE IF NOT EXISTS market_outcomes (
            condition_id   TEXT NOT NULL,
            outcome        TEXT NOT NULL,
            resolved_price REAL NOT NULL DEFAULT 0.0,
            resolved_at    INTEGER NOT NULL DEFAULT 0,
            source         TEXT NOT NULL DEFAULT 'polymarket_markets_api',
            inserted_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, outcome)
        );

        CREATE INDEX IF NOT EXISTS idx_market_outcomes_resolved_at
            ON market_outcomes(resolved_at DESC);

        -- ── 2b. market_tags (wallet-taxonomy migration Stage 1) ────────────
        -- Raw gamma tag slugs per condition_id. One row per (condition, tag).
        -- Populated by polymarket_api's event fetcher alongside market_metadata.
        -- Consumers MUST treat `tag` values as opaque strings — no code branch
        -- depending on specific values. Replaces the coarse 2-value
        -- market_metadata.category for wallet-analysis purposes.
        CREATE TABLE IF NOT EXISTS market_tags (
            condition_id TEXT NOT NULL,
            tag          TEXT NOT NULL,
            tag_label    TEXT,
            ingested_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (condition_id, tag)
        );

        CREATE INDEX IF NOT EXISTS idx_market_tags_tag
            ON market_tags(tag);
        CREATE INDEX IF NOT EXISTS idx_market_tags_condition_id
            ON market_tags(condition_id);

        -- ── 2c. wallet_tag_pnl (B1 tag_wallet_pnl, 2026-04-24) ──────────────
        -- Per-(wallet, tag) realized-PnL matrix, computed offline by
        -- scripts/build_wallet_tag_pnl.py (or on demand) from
        -- followed_wallet_activity × market_outcomes × market_tags.
        -- `tag` is the primary (alphabetically-first) opaque slug, or
        -- `unclassified` when no market_tags row exists.
        CREATE TABLE IF NOT EXISTS wallet_tag_pnl (
            wallet                    TEXT    NOT NULL,
            tag                       TEXT    NOT NULL,
            n_positions               INTEGER NOT NULL,
            realized_pnl              REAL    NOT NULL,
            win_rate                  REAL    NOT NULL,
            conviction_concentration  REAL    NOT NULL,
            computed_at               INTEGER NOT NULL,
            win_rate_held_only        REAL,
            n_held_to_resolution      INTEGER NOT NULL DEFAULT 0,
            open_unrealized_pnl       REAL    NOT NULL DEFAULT 0,
            n_open_positions          INTEGER NOT NULL DEFAULT 0,
            n_open_dead               INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (wallet, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_wallet
            ON wallet_tag_pnl (wallet);
        CREATE INDEX IF NOT EXISTS idx_wallet_tag_pnl_tag
            ON wallet_tag_pnl (tag);

        -- ── 2d. wallet_tag_fit (B2 tag_archetype_fit, 2026-04-24) ──────────
        -- Per-(wallet, tag) within-tag percentile fit score in [0,1],
        -- computed offline by scripts/build_wallet_tag_fit.py from
        -- wallet_tag_pnl + followed_wallet_activity (recency).
        -- NOT cross-tag portable; consumers filter on confidence_flag='ok'.
        CREATE TABLE IF NOT EXISTS wallet_tag_fit (
            wallet           TEXT    NOT NULL,
            tag              TEXT    NOT NULL,
            fit_score        REAL    NOT NULL,
            fit_rank         INTEGER NOT NULL,
            peer_count       INTEGER NOT NULL,
            confidence_flag  TEXT    NOT NULL,
            computed_at      INTEGER NOT NULL,
            inputs_json      TEXT    NOT NULL,
            PRIMARY KEY (wallet, tag)
        );
        CREATE INDEX IF NOT EXISTS idx_wtf_tag_rank
            ON wallet_tag_fit (tag, fit_rank);
        CREATE INDEX IF NOT EXISTS idx_wtf_wallet
            ON wallet_tag_fit (wallet);

        -- ── 2e. tag_nav_category (protocol v2 §8, 2026-04-25) ───────────────
        -- Maps Polymarket leaf tags to one of 13 nav-bar parent categories
        -- (politics/sports/crypto/esports/iran/finance/geopolitics/tech/
        -- culture/economy/weather/mentions/elections) plus 'other'.
        -- Source: data/tag_nav_category_seed.yaml + scripts/seed_tag_nav_category.py.
        -- Used by H5/FH4 multi-grain inference (test edge at leaf AND nav grain).
        CREATE TABLE IF NOT EXISTS tag_nav_category (
            tag           TEXT    PRIMARY KEY,
            nav_category  TEXT    NOT NULL,
            confidence    TEXT    NOT NULL DEFAULT 'manual',
            updated_at    INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tag_nav_category_nav
            ON tag_nav_category (nav_category);

        -- ── 3. positions (Phase 2 position aggregator) ─────────────────────
        CREATE TABLE IF NOT EXISTS positions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet              TEXT NOT NULL,
            condition_id        TEXT NOT NULL,
            outcome             TEXT NOT NULL,
            total_shares        REAL NOT NULL DEFAULT 0.0,
            avg_entry_price     REAL NOT NULL DEFAULT 0.0,
            total_notional      REAL NOT NULL DEFAULT 0.0,
            trade_count         INTEGER NOT NULL DEFAULT 0,
            first_trade_ts      INTEGER NOT NULL DEFAULT 0,
            last_trade_ts       INTEGER NOT NULL DEFAULT 0,
            current_price       REAL,
            market_end_date_iso TEXT,
            market_category     TEXT,
            is_open             INTEGER NOT NULL DEFAULT 1,
            trigger_type        TEXT,
            inserted_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(wallet, condition_id, outcome)
        );

        CREATE INDEX IF NOT EXISTS idx_positions_wallet
            ON positions(wallet);
        CREATE INDEX IF NOT EXISTS idx_positions_open_notional
            ON positions(is_open, total_notional DESC);
        CREATE INDEX IF NOT EXISTS idx_positions_condition_outcome
            ON positions(condition_id, outcome);
        CREATE INDEX IF NOT EXISTS idx_positions_last_trade
            ON positions(last_trade_ts DESC);

        -- ── 4. low_buy_trades composite indexes ────────────────────────────
        -- wallet + timestamp: covers wallets_for_analysis() 14-day window scan
        CREATE INDEX IF NOT EXISTS idx_low_buy_wallet_ts
            ON low_buy_trades(proxy_wallet, timestamp DESC);

        -- ── 5. candidate_users indexes ─────────────────────────────────────
        -- last_seen_ts: used by TTL pruning queries and stage advancement checks
        CREATE INDEX IF NOT EXISTS idx_candidate_last_seen
            ON candidate_users(last_seen_ts DESC);

        -- low_buy_hits: used by wallets_for_analysis() threshold filter
        CREATE INDEX IF NOT EXISTS idx_candidate_hits
            ON candidate_users(low_buy_hits DESC);

        -- ── 6. user_analysis hot-path indexes ─────────────────────────────
        -- classification + insider_score: covers alerter ORDER BY queries
        CREATE INDEX IF NOT EXISTS idx_ua_class_score
            ON user_analysis(classification, insider_score DESC);

        -- is_bot + matches_criteria: covers bot-filter fast-path in alerter
        CREATE INDEX IF NOT EXISTS idx_ua_bot_match
            ON user_analysis(is_bot, matches_criteria, analyzed_ts DESC);

        -- ── 7. alerts_sent deduplication index ────────────────────────────
        -- Keep schema compatible with alerter.log_alert() inserts.
        -- Include payload for backward compatibility with older helper code.
        CREATE TABLE IF NOT EXISTS alerts_sent (
            alert_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_type   TEXT NOT NULL,
            wallet       TEXT,
            message      TEXT NOT NULL DEFAULT '',
            sent_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            channel      TEXT DEFAULT 'telegram',
            acknowledged INTEGER DEFAULT 0,
            response     TEXT,
            payload      TEXT
        );
        -- wallet + alert_type + sent_at: covers the 7-day dedup EXISTS subquery
        CREATE INDEX IF NOT EXISTS idx_alerts_sent_wallet_type_ts
            ON alerts_sent(wallet, alert_type, sent_at DESC);

        -- ── 8. redemption_log (real redemption audit + idempotency) ───────
        CREATE TABLE IF NOT EXISTS redemption_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet          TEXT NOT NULL,
            condition_id    TEXT NOT NULL,
            outcome         TEXT NOT NULL,
            attempt_mode    TEXT NOT NULL DEFAULT 'dry_run',
            status          TEXT NOT NULL DEFAULT 'dry_run',
            resolved_price  REAL,
            threshold_price REAL,
            tx_hash         TEXT,
            amount_claimed  REAL,
            error           TEXT,
            metadata_json   TEXT NOT NULL DEFAULT '{}',
            attempted_ts    INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_redemption_log_wallet_ts
            ON redemption_log(wallet, attempted_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_redemption_log_status_ts
            ON redemption_log(status, attempted_ts DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_redemption_log_success_unique
            ON redemption_log(wallet, condition_id, outcome)
            WHERE status = 'success';
        """
    )
    _ensure_column(conn, "market_metadata", "category", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "market_metadata", "category_source", "TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_market_metadata_category_end "
        "ON market_metadata(category, is_open, end_date_iso)"
    )
    # Backfill missing columns for DBs created before this schema unified with
    # alerter.py (e.g. id/payload-only alerts_sent variants).
    _ensure_column(conn, "alerts_sent", "message", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "alerts_sent", "channel", "TEXT DEFAULT 'telegram'")
    _ensure_column(conn, "alerts_sent", "acknowledged", "INTEGER DEFAULT 0")
    _ensure_column(conn, "alerts_sent", "response", "TEXT")
    _ensure_column(conn, "alerts_sent", "payload", "TEXT")
    _ensure_column(conn, "redemption_log", "attempt_mode", "TEXT NOT NULL DEFAULT 'dry_run'")
    _ensure_column(conn, "redemption_log", "status", "TEXT NOT NULL DEFAULT 'dry_run'")
    _ensure_column(conn, "redemption_log", "resolved_price", "REAL")
    _ensure_column(conn, "redemption_log", "threshold_price", "REAL")
    _ensure_column(conn, "redemption_log", "tx_hash", "TEXT")
    _ensure_column(conn, "redemption_log", "amount_claimed", "REAL")
    _ensure_column(conn, "redemption_log", "error", "TEXT")
    _ensure_column(conn, "redemption_log", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "redemption_log", "attempted_ts", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "redemption_log", "updated_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
    ensure_paper_trades_slippage_columns(conn)
    ensure_paper_trades_duplicate_of_column(conn)
    ensure_paper_trades_disqualification_columns(conn)
    ensure_user_analysis_probabilistic_columns(conn)
    ensure_strategy_positions_live_price_columns(conn)
    ensure_wallet_directory_schema(conn)
    ensure_wallet_quality_columns(conn)

    # ── Research capture tables (unfiltered, append-only) ──
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS position_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            avg_price REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            size REAL NOT NULL DEFAULT 0,
            current_value REAL NOT NULL DEFAULT 0,
            initial_value REAL NOT NULL DEFAULT 0,
            cash_pnl REAL NOT NULL DEFAULT 0,
            pnl_percent REAL NOT NULL DEFAULT 0,
            resolved INTEGER NOT NULL DEFAULT 0,
            snapshot_ts INTEGER NOT NULL,
            inserted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_pos_snap_wallet_ts
            ON position_snapshots(wallet, snapshot_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_pos_snap_wallet_cond
            ON position_snapshots(wallet, condition_id, outcome, snapshot_ts DESC);

        -- ── signal_history: append-only log of position signal snapshots ──────
        -- Captures point-in-time scores every time the analyzer runs, so we
        -- can later replay gates against historical data (true backtesting).
        CREATE TABLE IF NOT EXISTS signal_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            is_open INTEGER NOT NULL DEFAULT 1,
            entry_price REAL NOT NULL DEFAULT 0,
            entry_notional REAL NOT NULL DEFAULT 0,
            current_price REAL NOT NULL DEFAULT 0,
            position_insider_score REAL NOT NULL DEFAULT 0,
            position_speculator_score REAL NOT NULL DEFAULT 0,
            position_classification TEXT NOT NULL DEFAULT '',
            activity_state TEXT NOT NULL DEFAULT 'holding',
            hold_hours REAL NOT NULL DEFAULT 0,
            entry_anchor_ts INTEGER NOT NULL DEFAULT 0,
            analyzed_ts INTEGER NOT NULL,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_signal_history_wallet_cond
            ON signal_history(wallet, condition_id, outcome, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_history_ts
            ON signal_history(analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_signal_history_anchor
            ON signal_history(condition_id, entry_anchor_ts);

        -- ── score_history: append-only log of wallet-level score snapshots ────
        -- One row per wallet per analysis run.  Captures the fields that entry
        -- gates depend on, so gate logic can be replayed at any historical point.
        CREATE TABLE IF NOT EXISTS score_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT NOT NULL,
            insider_score REAL NOT NULL DEFAULT 0,
            speculator_score REAL NOT NULL DEFAULT 0,
            classification TEXT NOT NULL DEFAULT '',
            tier TEXT NOT NULL DEFAULT '',
            strategy_group TEXT NOT NULL DEFAULT '',
            win_rate REAL NOT NULL DEFAULT 0,
            closed_sample_size INTEGER NOT NULL DEFAULT 0,
            qualifying_roi REAL,
            qualifying_capital REAL NOT NULL DEFAULT 0,
            pnl REAL NOT NULL DEFAULT 0,
            position_count INTEGER NOT NULL DEFAULT 0,
            wallet_edge_tier TEXT NOT NULL DEFAULT '',
            followability_cached REAL,
            is_bot INTEGER NOT NULL DEFAULT 0,
            analyzed_ts INTEGER NOT NULL,
            analysis_version INTEGER NOT NULL DEFAULT 0,
            captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_score_history_wallet_ts
            ON score_history(wallet, analyzed_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_score_history_ts
            ON score_history(analyzed_ts DESC);

        CREATE TABLE IF NOT EXISTS replay_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            config_json TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS replay_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            config_id INTEGER,
            config_json TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            signal_count INTEGER NOT NULL DEFAULT 0,
            trade_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (config_id) REFERENCES replay_configs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_replay_runs_config
            ON replay_runs(config_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_replay_runs_created
            ON replay_runs(created_at DESC);
    """)

    # ── Instance tagging (multi-VPS) ──
    # Guard: these tables are created by init_db's executescript, but
    # apply_phase_b_hardening can be called standalone on legacy DBs that
    # lack the strategy/replay/mcp tables.
    _existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    _instance_tag_tables = [
        "strategies",
        "strategy_positions",
        "strategy_executions",
        "strategy_runs",
        "replay_configs",
        "replay_runs",
        "mcp_audit_log",
    ]
    for _tbl in _instance_tag_tables:
        if _tbl in _existing_tables:
            _ensure_column(conn, _tbl, "instance_tag", "TEXT")

    if "strategy_positions" in _existing_tables:
        # Task 1: strategy_positions needs end_date_iso for resolution-aware plugin decisions
        _ensure_column(conn, "strategy_positions", "end_date_iso", "TEXT NOT NULL DEFAULT ''")

        # entry_insider_score for score-decay exit detection
        _ensure_column(conn, "strategy_positions", "entry_insider_score", "REAL NOT NULL DEFAULT 0")

        # P&L write-back: per-position realized/unrealized P&L visible to dashboards and APIs
        _ensure_column(conn, "strategy_positions", "pnl_usdc", "REAL")
        _ensure_column(conn, "strategy_positions", "current_price", "REAL")
        _ensure_column(conn, "strategy_positions", "current_value", "REAL")

    if "strategies" in _existing_tables:
        # Lifecycle stage for strategy state machine (Phase 4A)
        # research → paper → promoted → retired
        # runner only processes paper + promoted; research + retired are excluded
        _ensure_column(conn, "strategies", "lifecycle_stage", "TEXT NOT NULL DEFAULT 'paper'")

    # Indexes: (instance_tag, timestamp) for per-instance queries
    _instance_tag_indexes = [
        ("idx_strategies_instance_tag",
         "strategies", "(instance_tag, created_at DESC)"),
        ("idx_strategy_positions_instance_tag",
         "strategy_positions", "(instance_tag, updated_ts DESC)"),
        ("idx_strategy_executions_instance_tag",
         "strategy_executions", "(instance_tag, created_at DESC)"),
        ("idx_strategy_runs_instance_tag",
         "strategy_runs", "(instance_tag, started_at DESC)"),
        ("idx_replay_configs_instance_tag",
         "replay_configs", "(instance_tag, created_at DESC)"),
        ("idx_replay_runs_instance_tag",
         "replay_runs", "(instance_tag, created_at DESC)"),
        ("idx_mcp_audit_log_instance_tag",
         "mcp_audit_log", "(instance_tag, created_at DESC)"),
        # Compound indexes: (instance_tag, strategy_id, timestamp)
        ("idx_strategy_executions_instance_tag_strategy",
         "strategy_executions", "(instance_tag, strategy_id, created_at DESC)"),
        ("idx_strategy_runs_instance_tag_strategy",
         "strategy_runs", "(instance_tag, strategy_id, started_at DESC)"),
    ]
    for _idx_name, _idx_table, _idx_cols in _instance_tag_indexes:
        if _idx_table in _existing_tables:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_idx_name} "
                f"ON {_idx_table}{_idx_cols}"
            )

    ensure_instance_tags(conn)
    conn.commit()


def ensure_instance_tags(conn: sqlite3.Connection) -> None:
    """Backfill NULL/empty instance_tag values from INSTANCE_TAG env var.

    Safe to call repeatedly — only updates rows where instance_tag is NULL or
    empty string.  Already-tagged rows are never overwritten.

    Does nothing if INSTANCE_TAG is not set or is empty.
    Silently skips tables that do not exist (legacy DBs).
    """
    tag = os.environ.get("INSTANCE_TAG", "").strip()
    if not tag:
        return
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    tables = [
        "strategies",
        "strategy_positions",
        "strategy_executions",
        "strategy_runs",
        "replay_configs",
        "replay_runs",
        "mcp_audit_log",
    ]
    for table in tables:
        if table not in existing_tables:
            continue
        existing_cols = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if "instance_tag" not in existing_cols:
            continue
        conn.execute(
            f"UPDATE {table} SET instance_tag = ? "
            "WHERE instance_tag IS NULL OR instance_tag = ''",
            (tag,),
        )


def ensure_paper_trades_slippage_columns(conn: sqlite3.Connection) -> None:
    """Ensure slippage tracking columns exist on paper_trades."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "paper_trades", "signal_entry_price", "REAL")
        _ensure_column(conn, "paper_trades", "slippage_pct", "REAL")
        _ensure_column(conn, "paper_trades", "client_request_id", "TEXT")
        # Unique index for race-safe idempotency (partial: only rows with non-null client_request_id)
        conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_idempotency
        ON paper_trades (wallet, condition_id, outcome, client_request_id)
        WHERE client_request_id IS NOT NULL
        """)
        conn.commit()


def ensure_paper_trades_duplicate_of_column(conn: sqlite3.Connection) -> None:
    """Ensure legacy paper_trades tables can represent soft-deduped rows."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "paper_trades", "duplicate_of", "INTEGER")
        conn.commit()


def ensure_user_analysis_probabilistic_columns(conn: sqlite3.Connection) -> None:
    """Ensure legacy user_analysis tables expose the newer wallet-scoring fields."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_analysis' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "user_analysis", "wallet_confidence_score", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_analysis", "wallet_tier", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_analysis", "wallet_domain_stats", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(conn, "user_analysis", "insider_score_trend", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "user_analysis", "effective_copy_fraction", "REAL NOT NULL DEFAULT 1.0")
        _ensure_column(conn, "user_analysis", "lb_pnl", "REAL")
        _ensure_column(conn, "user_analysis", "lb_vol", "REAL")
        _ensure_column(conn, "user_analysis", "lb_rank", "TEXT")
        _ensure_column(conn, "user_analysis", "lb_username", "TEXT")
        _ensure_column(conn, "user_analysis", "lb_updated_at", "TEXT")
        conn.commit()


def ensure_strategy_positions_live_price_columns(conn: sqlite3.Connection) -> None:
    """Ensure legacy strategy_positions tables include live-pricing/dashboard fields."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_positions' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "strategy_positions", "end_date_iso", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "strategy_positions", "entry_insider_score", "REAL NOT NULL DEFAULT 0")
        _ensure_column(conn, "strategy_positions", "pnl_usdc", "REAL")
        _ensure_column(conn, "strategy_positions", "current_price", "REAL")
        _ensure_column(conn, "strategy_positions", "current_value", "REAL")
        conn.commit()


def ensure_paper_trades_disqualification_columns(conn: sqlite3.Connection) -> None:
    """Ensure disqualification tracking columns exist on paper_trades."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "paper_trades", "signal_disqualified_at", "TEXT DEFAULT NULL")
        _ensure_column(conn, "paper_trades", "signal_disqualification_reason", "TEXT DEFAULT NULL")
        conn.commit()


def ensure_wallet_quality_columns(conn: sqlite3.Connection) -> None:
    """Ensure wallet quality scoring columns exist in user_analysis.

    Columns added:
      - wallet_quality_score REAL DEFAULT NULL   -- NULL = not scored yet (rollout grace)
      - wallet_quality_eligible INTEGER DEFAULT NULL -- NULL = not evaluated; 0 = hard-filter fail; 1 = eligible
      - wallet_quality_fail_reason TEXT           -- human-readable reason when eligible=0
      - market_maker_detected INTEGER DEFAULT 0   -- 1 if wallet holds both sides of >50% markets
    """
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_analysis' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "user_analysis", "wallet_quality_score", "REAL DEFAULT NULL")
        _ensure_column(conn, "user_analysis", "wallet_quality_eligible", "INTEGER DEFAULT NULL")
        _ensure_column(conn, "user_analysis", "wallet_quality_fail_reason", "TEXT")
        _ensure_column(conn, "user_analysis", "market_maker_detected", "INTEGER DEFAULT 0")
        conn.commit()




def upsert_user_analysis(
    conn: sqlite3.Connection,
    wallet: str,
    analyzed_ts: int,
    position_count: int,
    pnl: float,
    max_position_value: float,
    top_open_position_title: str,
    top_open_position_value: float,
    total_open_value: float,
    trade_count: int,
    low_buy_hits_14d: int,
    avg_low_buy_notional_14d: float,
    market_diversity_14d: int,
    concentration_ratio: float,
    suspicion_score: float,
    insider_score: float,
    speculator_score: float,
    classification: str,
    tier: str,
    strategy_group: str,
    strategy_confidence: float,
    strategy_note: str,
    wins: int,
    losses: int,
    closed_sample_size: int,
    win_rate: float,
    high_winrate_qualified: bool,
    first_activity_ts: int,
    account_age_days: float,
    is_fresh_account: bool,
    total_closed_count: int = 0,
    resolution_rate: float = 0.0,
    effective_resolved_wins: int = 0,
    effective_resolved_losses: int = 0,
    position_signal_count: int = 0,
    position_insider_candidate_count: int = 0,
    position_speculator_count: int = 0,
    max_position_insider_score: float = 0.0,
    max_position_speculator_score: float = 0.0,
    total_capital_deployed: float = 0.0,
    roi_percent: float = 0.0,
    qualifying_pnl: float = 0.0,
    qualifying_capital: float = 0.0,
    qualifying_roi: float | None = None,
    closed_realized_pnl: float = 0.0,
    open_cash_pnl: float = 0.0,
    analysis_version: int = 0,
    is_bot: bool = False,
    bot_detection_reason: str = "",
    bot_checked_at: int = 0,
    matches_criteria: bool = False,
    reason: str = "",
    wallet_edge_tier: str = "",
    followability_cached: float | None = None,
    wallet_confidence_score: float = 0.0,
    wallet_tier: int = 0,
    wallet_domain_stats: str = "{}",
    insider_score_trend: float = 0.0,
    effective_copy_fraction: float = 1.0,
    wallet_quality_score: float | None = None,
    wallet_quality_eligible: int | None = None,
    wallet_quality_fail_reason: str | None = None,
    market_maker_detected: int = 0,
    gate0_status: str | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO user_analysis (
            wallet,
            analyzed_ts,
            position_count,
            pnl,
            max_position_value,
            top_open_position_title,
            top_open_position_value,
            total_open_value,
            trade_count,
            low_buy_hits_14d,
            avg_low_buy_notional_14d,
            market_diversity_14d,
            concentration_ratio,
            suspicion_score,
            insider_score,
            speculator_score,
            classification,
            tier,
            strategy_group,
            strategy_confidence,
            strategy_note,
            wins,
            losses,
            closed_sample_size,
            win_rate,
            high_winrate_qualified,
            total_closed_count,
            resolution_rate,
            effective_resolved_wins,
            effective_resolved_losses,
            first_activity_ts,
            account_age_days,
            is_fresh_account,
            position_signal_count,
            position_insider_candidate_count,
            position_speculator_count,
            max_position_insider_score,
            max_position_speculator_score,
            total_capital_deployed,
            roi_percent,
            qualifying_pnl,
            qualifying_capital,
            qualifying_roi,
            closed_realized_pnl,
            open_cash_pnl,
            analysis_version,
            is_bot,
            bot_detection_reason,
            bot_checked_at,
            matches_criteria,
            reason,
            wallet_edge_tier,
            followability_cached,
            wallet_confidence_score,
            wallet_tier,
            wallet_domain_stats,
            insider_score_trend,
            effective_copy_fraction,
            wallet_quality_score,
            wallet_quality_eligible,
            wallet_quality_fail_reason,
            market_maker_detected
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            analyzed_ts = excluded.analyzed_ts,
            position_count = excluded.position_count,
            pnl = excluded.pnl,
            max_position_value = excluded.max_position_value,
            top_open_position_title = excluded.top_open_position_title,
            top_open_position_value = excluded.top_open_position_value,
            total_open_value = excluded.total_open_value,
            trade_count = excluded.trade_count,
            low_buy_hits_14d = excluded.low_buy_hits_14d,
            avg_low_buy_notional_14d = excluded.avg_low_buy_notional_14d,
            market_diversity_14d = excluded.market_diversity_14d,
            concentration_ratio = excluded.concentration_ratio,
            suspicion_score = excluded.suspicion_score,
            insider_score = excluded.insider_score,
            speculator_score = excluded.speculator_score,
            classification = excluded.classification,
            tier = excluded.tier,
            strategy_group = excluded.strategy_group,
            strategy_confidence = excluded.strategy_confidence,
            strategy_note = excluded.strategy_note,
            wins = excluded.wins,
            losses = excluded.losses,
            closed_sample_size = excluded.closed_sample_size,
            win_rate = excluded.win_rate,
            high_winrate_qualified = excluded.high_winrate_qualified,
            total_closed_count = excluded.total_closed_count,
            resolution_rate = excluded.resolution_rate,
            effective_resolved_wins = excluded.effective_resolved_wins,
            effective_resolved_losses = excluded.effective_resolved_losses,
            first_activity_ts = excluded.first_activity_ts,
            account_age_days = excluded.account_age_days,
            is_fresh_account = excluded.is_fresh_account,
            position_signal_count = excluded.position_signal_count,
            position_insider_candidate_count = excluded.position_insider_candidate_count,
            position_speculator_count = excluded.position_speculator_count,
            max_position_insider_score = excluded.max_position_insider_score,
            max_position_speculator_score = excluded.max_position_speculator_score,
            total_capital_deployed = excluded.total_capital_deployed,
            roi_percent = excluded.roi_percent,
            qualifying_pnl = excluded.qualifying_pnl,
            qualifying_capital = excluded.qualifying_capital,
            qualifying_roi = excluded.qualifying_roi,
            closed_realized_pnl = excluded.closed_realized_pnl,
            open_cash_pnl = excluded.open_cash_pnl,
            analysis_version = excluded.analysis_version,
            is_bot = excluded.is_bot,
            bot_detection_reason = excluded.bot_detection_reason,
            bot_checked_at = excluded.bot_checked_at,
            matches_criteria = excluded.matches_criteria,
            reason = excluded.reason,
            wallet_edge_tier = excluded.wallet_edge_tier,
            followability_cached = excluded.followability_cached,
            wallet_confidence_score = excluded.wallet_confidence_score,
            wallet_tier = excluded.wallet_tier,
            wallet_domain_stats = excluded.wallet_domain_stats,
            insider_score_trend = excluded.insider_score_trend,
            effective_copy_fraction = excluded.effective_copy_fraction,
            wallet_quality_score = excluded.wallet_quality_score,
            wallet_quality_eligible = excluded.wallet_quality_eligible,
            wallet_quality_fail_reason = excluded.wallet_quality_fail_reason,
            market_maker_detected = excluded.market_maker_detected,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            analyzed_ts,
            position_count,
            pnl,
            max_position_value,
            top_open_position_title,
            top_open_position_value,
            total_open_value,
            trade_count,
            low_buy_hits_14d,
            avg_low_buy_notional_14d,
            market_diversity_14d,
            concentration_ratio,
            suspicion_score,
            insider_score,
            speculator_score,
            classification,
            tier,
            strategy_group,
            strategy_confidence,
            strategy_note,
            int(wins or 0),
            int(losses or 0),
            int(closed_sample_size or 0),
            float(win_rate or 0.0),
            1 if high_winrate_qualified else 0,
            int(total_closed_count or 0),
            float(resolution_rate or 0.0),
            int(effective_resolved_wins or 0),
            int(effective_resolved_losses or 0),
            int(first_activity_ts or 0),
            float(account_age_days or 0.0),
            1 if is_fresh_account else 0,
            int(position_signal_count or 0),
            int(position_insider_candidate_count or 0),
            int(position_speculator_count or 0),
            float(max_position_insider_score or 0.0),
            float(max_position_speculator_score or 0.0),
            float(total_capital_deployed or 0.0),
            float(roi_percent or 0.0),
            float(qualifying_pnl or 0.0),
            float(qualifying_capital or 0.0),
            qualifying_roi,  # None is stored as NULL — means no reliable data
            float(closed_realized_pnl or 0.0),
            float(open_cash_pnl or 0.0),
            int(analysis_version or 0),
            1 if is_bot else 0,
            str(bot_detection_reason or ""),
            int(bot_checked_at or 0),
            1 if matches_criteria else 0,
            reason,
            str(wallet_edge_tier or ""),
            followability_cached,
            float(wallet_confidence_score or 0.0),
            int(wallet_tier or 0),
            str(wallet_domain_stats or "{}"),
            float(insider_score_trend or 0.0),
            float(effective_copy_fraction if effective_copy_fraction is not None else 1.0),
            wallet_quality_score,   # None stored as NULL = not scored yet
            wallet_quality_eligible,  # None = not evaluated; 0 = fail; 1 = eligible
            wallet_quality_fail_reason,  # None when eligible
            int(market_maker_detected or 0),
        ),
    )

    upsert_wallet_registry(
        conn,
        wallet=wallet,
        status="blocked" if str(classification or "").strip().lower() == "blocked" else None,
        gate0_status=gate0_status,
        analysis_status="analyzed" if int(analyzed_ts or 0) > 0 else "pending",
        classification=str(classification or ""),
        notes=str(reason or ""),
        first_seen_ts=int(first_activity_ts or 0),
        last_seen_ts=int(analyzed_ts or 0),
    )
    replace_wallet_tags(
        conn,
        wallet=wallet,
        tag_type="classification",
        source="user_analysis",
        tags=[str(classification or "").strip()] if str(classification or "").strip() else [],
    )
    replace_wallet_tags(
        conn,
        wallet=wallet,
        tag_type="wallet_edge_tier",
        source="user_analysis",
        tags=[str(wallet_edge_tier or "").strip()] if str(wallet_edge_tier or "").strip() else [],
    )

    if matches_criteria:
        # Support both the current slim flagged_users schema and older live DBs
        # that still have the legacy wide table with NOT NULL scoring columns.
        flagged_columns = {
            str(row[1] or "").strip()
            for row in conn.execute("PRAGMA table_info(flagged_users)")
            if len(row) > 1
        }
        flagged_payload = {
            "wallet": wallet,
            "flagged_ts": int(analyzed_ts or 0),
            "position_count": int(position_count or 0),
            "pnl": float(pnl or 0.0),
            "max_position_value": float(max_position_value or 0.0),
            "total_open_value": float(total_open_value or 0.0),
            "trade_count": int(trade_count or 0),
            "source_note": reason or None,
            "low_buy_hits_14d": int(low_buy_hits_14d or 0),
            "suspicion_score": float(suspicion_score or 0.0),
            "tier": str(tier or ""),
            "top_open_position_title": str(top_open_position_title or ""),
            "top_open_position_value": float(top_open_position_value or 0.0),
            "strategy_group": str(strategy_group or ""),
            "strategy_confidence": float(strategy_confidence or 0.0),
            "strategy_note": str(strategy_note or ""),
            "insider_score": float(insider_score or 0.0),
            "speculator_score": float(speculator_score or 0.0),
            "classification": str(classification or ""),
            "win_rate": float(win_rate or 0.0),
            "closed_sample_size": int(closed_sample_size or 0),
            "high_winrate_qualified": 1 if high_winrate_qualified else 0,
            "account_age_days": float(account_age_days or 0.0),
            "is_fresh_account": 1 if is_fresh_account else 0,
            "position_signal_count": int(position_signal_count or 0),
            "position_insider_candidate_count": int(position_insider_candidate_count or 0),
            "position_speculator_count": int(position_speculator_count or 0),
            "max_position_insider_score": float(max_position_insider_score or 0.0),
            "max_position_speculator_score": float(max_position_speculator_score or 0.0),
        }
        insert_columns = [col for col in flagged_payload if col in flagged_columns]
        if insert_columns:
            insert_sql = ", ".join(insert_columns)
            placeholder_sql = ", ".join("?" for _ in insert_columns)
            update_sql = ", ".join(
                f"{col} = excluded.{col}" for col in insert_columns if col != "wallet"
            )
            if "updated_at" in flagged_columns:
                insert_sql = f"{insert_sql}, updated_at"
                placeholder_sql = f"{placeholder_sql}, CURRENT_TIMESTAMP"
                if update_sql:
                    update_sql = f"{update_sql}, updated_at = CURRENT_TIMESTAMP"
                else:
                    update_sql = "updated_at = CURRENT_TIMESTAMP"
            conn.execute(
                f"""
                INSERT INTO flagged_users ({insert_sql})
                VALUES ({placeholder_sql})
                ON CONFLICT(wallet) DO UPDATE SET
                    {update_sql}
                """,
                tuple(flagged_payload[col] for col in insert_columns),
            )
    else:
        conn.execute("DELETE FROM flagged_users WHERE wallet = ?", (wallet,))

    if commit:
        conn.commit()


def update_user_analysis_bot_status(
    conn: sqlite3.Connection,
    wallet: str,
    *,
    is_bot: bool,
    bot_detection_reason: str,
    bot_checked_at: int,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        UPDATE user_analysis
        SET
            is_bot = ?,
            bot_detection_reason = ?,
            bot_checked_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
        """,
        (
            1 if is_bot else 0,
            str(bot_detection_reason or ""),
            int(bot_checked_at or 0),
            str(wallet or "").strip().lower(),
        ),
    )
    if commit:
        conn.commit()


def flagged_users(conn: sqlite3.Connection, limit: int = 500) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            f.wallet,
            f.flagged_ts,
            f.source_note,
            f.updated_at,
            -- all scoring columns come from user_analysis (always fresh)
            ua.position_count,
            ua.pnl,
            ua.max_position_value,
            ua.top_open_position_title,
            ua.top_open_position_value,
            ua.total_open_value,
            ua.trade_count,
            ua.low_buy_hits_14d,
            ua.suspicion_score,
            ua.insider_score,
            ua.speculator_score,
            ua.classification,
            ua.win_rate,
            ua.closed_sample_size,
            ua.high_winrate_qualified,
            ua.account_age_days,
            ua.is_fresh_account,
            ua.position_signal_count,
            ua.position_insider_candidate_count,
            ua.position_speculator_count,
            ua.max_position_insider_score,
            ua.max_position_speculator_score,
            ua.tier,
            ua.strategy_group,
            ua.strategy_confidence,
            ua.strategy_note,
            ua.roi_percent,
            ua.resolution_rate,
            ua.is_bot,
            ua.bot_detection_reason,
            c.low_buy_hits,
            c.last_title
        FROM flagged_users f
        LEFT JOIN user_analysis ua ON ua.wallet = f.wallet
        LEFT JOIN candidate_users c ON c.wallet = f.wallet
        ORDER BY f.flagged_ts DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def recent_low_buy_trades(conn: sqlite3.Connection, limit: int = 200) -> Iterable[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM low_buy_trades
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def mark_trade_deleted(
    conn: sqlite3.Connection,
    transaction_hash: str,
    deleted_source: str = "",
    deleted_reason: str = "",
) -> bool:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return False
    conn.execute(
        """
        INSERT INTO deleted_trades (
            transaction_hash,
            deleted_ts,
            deleted_source,
            deleted_reason
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(transaction_hash) DO UPDATE SET
            deleted_ts = excluded.deleted_ts,
            deleted_source = excluded.deleted_source,
            deleted_reason = excluded.deleted_reason,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            tx_hash,
            int(time.time()),
            str(deleted_source or "").strip(),
            str(deleted_reason or "").strip(),
        ),
    )
    conn.commit()
    return True


def is_trade_deleted(conn: sqlite3.Connection, transaction_hash: str) -> bool:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM deleted_trades
        WHERE transaction_hash = ?
        LIMIT 1
        """,
        (tx_hash,),
    ).fetchone()
    return row is not None


def deleted_trade_hashes(
    conn: sqlite3.Connection,
    transaction_hashes: Iterable[str] | None = None,
) -> set[str]:
    if transaction_hashes is None:
        rows = conn.execute("SELECT transaction_hash FROM deleted_trades").fetchall()
        return {str(row["transaction_hash"] or "") for row in rows if row["transaction_hash"]}

    normalized: List[str] = []
    seen: set[str] = set()
    for tx_hash in transaction_hashes:
        tx = _normalize_tx_hash(tx_hash)
        if not tx or tx in seen:
            continue
        seen.add(tx)
        normalized.append(tx)
    if not normalized:
        return set()

    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT transaction_hash
        FROM deleted_trades
        WHERE transaction_hash IN ({placeholders})
        """,
        normalized,
    ).fetchall()
    return {str(row["transaction_hash"] or "") for row in rows if row["transaction_hash"]}


def delete_trade_globally(conn: sqlite3.Connection, transaction_hash: str) -> Dict[str, int]:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return {
            "low_buy_trades": 0,
            "watchlist_trades": 0,
            "followed_trades": 0,
            "dismissed_suspicious_trades": 0,
        }

    low_buy_cur = conn.execute("DELETE FROM low_buy_trades WHERE transaction_hash = ?", (tx_hash,))
    watchlist_cur = conn.execute("DELETE FROM watchlist_trades WHERE transaction_hash = ?", (tx_hash,))
    followed_cur = conn.execute("DELETE FROM followed_trades WHERE transaction_hash = ?", (tx_hash,))
    dismissed_cur = conn.execute("DELETE FROM dismissed_suspicious_trades WHERE transaction_hash = ?", (tx_hash,))
    conn.commit()
    return {
        "low_buy_trades": int(low_buy_cur.rowcount or 0),
        "watchlist_trades": int(watchlist_cur.rowcount or 0),
        "followed_trades": int(followed_cur.rowcount or 0),
        "dismissed_suspicious_trades": int(dismissed_cur.rowcount or 0),
    }


def add_agent_mute(
    conn: sqlite3.Connection,
    *,
    scope_type: str,
    scope_value: str | None = None,
    wallet: str | None = None,
    dedupe_key: str | None = None,
    condition_id: str | None = None,
    outcome: str | None = None,
    ttl_seconds: int | None = None,
    reason: str = "",
) -> Dict[str, Any]:
    normalized_scope = _normalize_agent_mute_scope(scope_type)
    normalized_value = _normalize_agent_mute_scope_value(
        normalized_scope,
        scope_value,
        wallet=wallet,
        dedupe_key=dedupe_key,
        condition_id=condition_id,
        outcome=outcome,
    )
    if not normalized_value:
        return {"ok": False, "error": "scope_value is required"}

    now_ts = int(time.time())
    ttl = int(ttl_seconds or 0)
    expires_ts = now_ts + ttl if ttl > 0 else 0
    cur = conn.execute(
        """
        INSERT INTO agent_mutes (
            scope_type,
            scope_value,
            reason,
            expires_ts,
            created_ts
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            normalized_scope,
            normalized_value,
            str(reason or "").strip(),
            expires_ts,
            now_ts,
        ),
    )
    conn.commit()
    mute_id = int(cur.lastrowid or 0)
    row = conn.execute(
        """
        SELECT id, scope_type, scope_value, reason, expires_ts, created_ts, updated_at
        FROM agent_mutes
        WHERE id = ?
        LIMIT 1
        """,
        (mute_id,),
    ).fetchone()
    return {"ok": True, "id": mute_id, "row": row}


def agent_mutes(
    conn: sqlite3.Connection,
    *,
    include_expired: bool = False,
    limit: int = 1000,
) -> List[sqlite3.Row]:
    where: List[str] = ["1=1"]
    params: List[Any] = []
    if not include_expired:
        now_ts = int(time.time())
        where.append("(expires_ts <= 0 OR expires_ts > ?)")
        params.append(now_ts)
    params.append(max(1, int(limit)))
    return conn.execute(
        f"""
        SELECT id, scope_type, scope_value, reason, expires_ts, created_ts, updated_at
        FROM agent_mutes
        WHERE {" AND ".join(where)}
        ORDER BY created_ts DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def delete_agent_mute(conn: sqlite3.Connection, mute_id: int) -> bool:
    mute_id = int(mute_id or 0)
    if mute_id <= 0:
        return False
    cur = conn.execute("DELETE FROM agent_mutes WHERE id = ?", (mute_id,))
    conn.commit()
    return int(cur.rowcount or 0) > 0


def prune_expired_agent_mutes(conn: sqlite3.Connection) -> int:
    now_ts = int(time.time())
    cur = conn.execute("DELETE FROM agent_mutes WHERE expires_ts > 0 AND expires_ts <= ?", (now_ts,))
    conn.commit()
    return int(cur.rowcount or 0)


def enqueue_alert_outbox(
    conn: sqlite3.Connection,
    *,
    dedupe_key: str,
    alert_type: str,
    wallet: str = "",
    condition_id: str = "",
    outcome: str = "",
    severity: str = "medium",
    title: str = "",
    message: str = "",
    payload: Dict[str, Any] | None = None,
    event_ts: int | None = None,
    dedupe_seconds: int = 900,
) -> Dict[str, Any]:
    normalized_key = str(dedupe_key or "").strip().lower()
    if not normalized_key:
        return {"ok": False, "error": "dedupe_key is required"}

    now_ts = int(time.time())
    event_value = int(event_ts or now_ts)
    if event_value <= 0:
        event_value = now_ts
    dedupe_until = event_value + max(0, int(dedupe_seconds))

    row = conn.execute(
        """
        SELECT
            id,
            status,
            first_seen_ts,
            last_seen_ts,
            seen_count,
            dedupe_until_ts
        FROM alerts_outbox
        WHERE dedupe_key = ?
        LIMIT 1
        """,
        (normalized_key,),
    ).fetchone()

    payload_json = "{}"
    if payload is not None:
        try:
            payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError):
            payload_json = "{}"

    normalized_wallet = _normalize_wallet(wallet)
    normalized_condition_id = _normalize_condition_id(condition_id)
    normalized_outcome = _normalize_outcome(outcome)
    normalized_severity = _normalize_alert_severity(severity, default="medium")
    normalized_type = str(alert_type or "").strip().lower() or "generic"
    normalized_title = str(title or "").strip()
    normalized_message = str(message or "").strip()

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO alerts_outbox (
                dedupe_key,
                alert_type,
                wallet,
                condition_id,
                outcome,
                severity,
                title,
                message,
                payload_json,
                status,
                first_seen_ts,
                last_seen_ts,
                seen_count,
                dedupe_until_ts,
                sent_ts,
                acknowledged_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, 1, ?, 0, 0)
            """,
            (
                normalized_key,
                normalized_type,
                normalized_wallet,
                normalized_condition_id,
                normalized_outcome,
                normalized_severity,
                normalized_title,
                normalized_message,
                payload_json,
                event_value,
                event_value,
                dedupe_until,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "created": True,
            "deduped": False,
            "reactivated": False,
            "id": int(cur.lastrowid or 0),
            "dedupe_key": normalized_key,
            "status": "new",
        }

    alert_id = int(row["id"] or 0)
    existing_dedupe_until = int(row["dedupe_until_ts"] or 0)
    existing_first_seen_ts = int(row["first_seen_ts"] or event_value)
    existing_seen_count = int(row["seen_count"] or 0)
    within_dedupe_window = event_value <= existing_dedupe_until

    if within_dedupe_window:
        conn.execute(
            """
            UPDATE alerts_outbox
            SET
                wallet = CASE WHEN ? <> '' THEN ? ELSE wallet END,
                condition_id = CASE WHEN ? <> '' THEN ? ELSE condition_id END,
                outcome = CASE WHEN ? <> '' THEN ? ELSE outcome END,
                severity = ?,
                title = CASE WHEN ? <> '' THEN ? ELSE title END,
                message = CASE WHEN ? <> '' THEN ? ELSE message END,
                payload_json = ?,
                last_seen_ts = ?,
                seen_count = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                normalized_wallet,
                normalized_wallet,
                normalized_condition_id,
                normalized_condition_id,
                normalized_outcome,
                normalized_outcome,
                normalized_severity,
                normalized_title,
                normalized_title,
                normalized_message,
                normalized_message,
                payload_json,
                max(event_value, int(row["last_seen_ts"] or 0)),
                existing_seen_count + 1,
                alert_id,
            ),
        )
        conn.commit()
        return {
            "ok": True,
            "created": False,
            "deduped": True,
            "reactivated": False,
            "id": alert_id,
            "dedupe_key": normalized_key,
            "status": _normalize_alert_status(row["status"], default="new"),
        }

    conn.execute(
        """
        UPDATE alerts_outbox
        SET
            alert_type = ?,
            wallet = ?,
            condition_id = ?,
            outcome = ?,
            severity = ?,
            title = ?,
            message = ?,
            payload_json = ?,
            status = 'new',
            first_seen_ts = CASE
                WHEN first_seen_ts <= 0 THEN ?
                ELSE first_seen_ts
            END,
            last_seen_ts = ?,
            seen_count = ?,
            dedupe_until_ts = ?,
            sent_ts = 0,
            acknowledged_ts = 0,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            normalized_type,
            normalized_wallet,
            normalized_condition_id,
            normalized_outcome,
            normalized_severity,
            normalized_title,
            normalized_message,
            payload_json,
            existing_first_seen_ts if existing_first_seen_ts > 0 else event_value,
            event_value,
            existing_seen_count + 1,
            dedupe_until,
            alert_id,
        ),
    )
    conn.commit()
    return {
        "ok": True,
        "created": False,
        "deduped": False,
        "reactivated": True,
        "id": alert_id,
        "dedupe_key": normalized_key,
        "status": "new",
    }


def alert_outbox_rows(
    conn: sqlite3.Connection,
    *,
    statuses: Iterable[str] | None = None,
    since_ts: int | None = None,
    wallet: str | None = None,
    limit: int = 200,
) -> List[sqlite3.Row]:
    where: List[str] = ["1=1"]
    params: List[Any] = []
    if statuses is not None:
        normalized = sorted({_normalize_alert_status(status, default="") for status in statuses if status})
        normalized = [status for status in normalized if status in _ALERT_STATUSES]
        if normalized:
            placeholders = ",".join("?" for _ in normalized)
            where.append(f"status IN ({placeholders})")
            params.extend(normalized)
    if since_ts is not None and int(since_ts) > 0:
        where.append("last_seen_ts >= ?")
        params.append(int(since_ts))
    if wallet:
        where.append("wallet = ?")
        params.append(_normalize_wallet(wallet))
    params.append(max(1, int(limit)))

    return conn.execute(
        f"""
        SELECT
            id,
            dedupe_key,
            alert_type,
            wallet,
            condition_id,
            outcome,
            severity,
            title,
            message,
            payload_json,
            status,
            first_seen_ts,
            last_seen_ts,
            seen_count,
            dedupe_until_ts,
            sent_ts,
            acknowledged_ts,
            updated_at
        FROM alerts_outbox
        WHERE {" AND ".join(where)}
        ORDER BY
            CASE status
                WHEN 'new' THEN 0
                WHEN 'sent' THEN 1
                ELSE 2
            END ASC,
            last_seen_ts DESC,
            id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def mark_alert_outbox_sent(
    conn: sqlite3.Connection,
    *,
    alert_id: int,
    sent_ts: int | None = None,
) -> bool:
    alert_id = int(alert_id or 0)
    if alert_id <= 0:
        return False
    ts = int(sent_ts or time.time())
    cur = conn.execute(
        """
        UPDATE alerts_outbox
        SET
            status = CASE
                WHEN status = 'acknowledged' THEN status
                ELSE 'sent'
            END,
            sent_ts = CASE
                WHEN ? > 0 THEN ?
                ELSE sent_ts
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ts, ts, alert_id),
    )
    conn.commit()
    return int(cur.rowcount or 0) > 0


def acknowledge_alert_outbox(
    conn: sqlite3.Connection,
    *,
    alert_id: int,
    acknowledged_ts: int | None = None,
) -> bool:
    alert_id = int(alert_id or 0)
    if alert_id <= 0:
        return False
    ts = int(acknowledged_ts or time.time())
    cur = conn.execute(
        """
        UPDATE alerts_outbox
        SET
            status = 'acknowledged',
            acknowledged_ts = CASE
                WHEN ? > 0 THEN ?
                ELSE acknowledged_ts
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ts, ts, alert_id),
    )
    conn.commit()
    return int(cur.rowcount or 0) > 0


def acknowledge_alert_outbox_by_dedupe_key(
    conn: sqlite3.Connection,
    *,
    dedupe_key: str,
    acknowledged_ts: int | None = None,
) -> bool:
    normalized_key = str(dedupe_key or "").strip().lower()
    if not normalized_key:
        return False
    ts = int(acknowledged_ts or time.time())
    cur = conn.execute(
        """
        UPDATE alerts_outbox
        SET
            status = 'acknowledged',
            acknowledged_ts = CASE
                WHEN ? > 0 THEN ?
                ELSE acknowledged_ts
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE dedupe_key = ?
        """,
        (ts, ts, normalized_key),
    )
    conn.commit()
    return int(cur.rowcount or 0) > 0


def delete_low_buy_trade(conn: sqlite3.Connection, transaction_hash: str) -> bool:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return False
    cur = conn.execute("DELETE FROM low_buy_trades WHERE transaction_hash = ?", (tx_hash,))
    # Keep dismiss table in sync if legacy hidden rows exist.
    conn.execute("DELETE FROM dismissed_suspicious_trades WHERE transaction_hash = ?", (tx_hash,))
    conn.commit()
    return cur.rowcount > 0


def dismiss_suspicious_trade(
    conn: sqlite3.Connection,
    transaction_hash: str,
    wallet: str = "",
    condition_id: str = "",
    title: str = "",
    note: str = "",
) -> bool:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return False
    conn.execute(
        """
        INSERT INTO dismissed_suspicious_trades (
            transaction_hash,
            wallet,
            condition_id,
            title,
            note,
            dismissed_ts
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(transaction_hash) DO UPDATE SET
            wallet = excluded.wallet,
            condition_id = excluded.condition_id,
            title = excluded.title,
            note = excluded.note,
            dismissed_ts = excluded.dismissed_ts,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            tx_hash,
            _normalize_wallet(wallet),
            str(condition_id or "").strip(),
            str(title or "").strip(),
            str(note or "").strip(),
            int(time.time()),
        ),
    )
    conn.commit()
    return True


def undismiss_suspicious_trade(conn: sqlite3.Connection, transaction_hash: str) -> bool:
    tx_hash = _normalize_tx_hash(transaction_hash)
    if not tx_hash:
        return False
    cur = conn.execute("DELETE FROM dismissed_suspicious_trades WHERE transaction_hash = ?", (tx_hash,))
    conn.commit()
    return cur.rowcount > 0


def clear_dismissed_suspicious_trades(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM dismissed_suspicious_trades")
    conn.commit()
    return int(cur.rowcount or 0)


def ignore_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    reason: str = "",
) -> bool:
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return False
    conn.execute(
        """
        INSERT INTO ignored_wallets (
            wallet,
            reason,
            ignored_ts
        ) VALUES (?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            reason = excluded.reason,
            ignored_ts = excluded.ignored_ts,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            normalized,
            str(reason or "").strip(),
            int(time.time()),
        ),
    )
    conn.commit()
    return True


def unignore_wallet(conn: sqlite3.Connection, wallet: str) -> bool:
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return False
    cur = conn.execute("DELETE FROM ignored_wallets WHERE wallet = ?", (normalized,))
    conn.commit()
    return bool(cur.rowcount)


def clear_ignored_wallets(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM ignored_wallets")
    conn.commit()
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Blocked wallets — hard permanent ban: wipes signals and prevents re-entry
# ---------------------------------------------------------------------------

def block_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    reason: str = "blocked by user",
    blocked_by: str = "user",
) -> dict:
    """
    Permanently block a wallet.

    1. Adds the wallet to blocked_wallets (permanent blocklist)
    2. Closes all open position_signals for this wallet
    3. Removes from candidate_users (streamer won't re-add it)
    4. Removes from wallet_analysis_queue
    5. Marks user_analysis classification = 'blocked'
    6. Removes from ignored_wallets (block supersedes ignore)

    Audit trail preserved: alerts_sent and wallet_profiles are NOT deleted.
    """
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return {"ok": False, "error": "invalid wallet address"}

    now_ts = int(time.time())

    # 1. Add to blocked_wallets
    conn.execute(
        """
        INSERT INTO blocked_wallets (wallet, reason, blocked_ts, blocked_by)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            reason = excluded.reason,
            blocked_ts = excluded.blocked_ts,
            blocked_by = excluded.blocked_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (normalized, reason, now_ts, blocked_by),
    )

    # 2. Close all open position_signals
    sig_cur = conn.execute(
        "UPDATE position_signals SET is_open=0, updated_at=CURRENT_TIMESTAMP WHERE wallet=? AND is_open=1",
        (normalized,),
    )
    signals_closed = sig_cur.rowcount

    # 3. Remove from candidate_users
    conn.execute("DELETE FROM candidate_users WHERE wallet=?", (normalized,))

    # 4. Remove from wallet_analysis_queue
    conn.execute("DELETE FROM wallet_analysis_queue WHERE wallet=?", (normalized,))

    # 5. Mark user_analysis as blocked
    conn.execute(
        """
        UPDATE user_analysis
        SET classification = 'blocked',
            insider_score = 0,
            high_winrate_qualified = 0,
            matches_criteria = 0,
            reason = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE wallet = ?
        """,
        (f"blocked: {reason}", normalized),
    )

    # 6. Remove from ignored_wallets (block supersedes ignore)
    conn.execute("DELETE FROM ignored_wallets WHERE wallet=?", (normalized,))

    conn.commit()
    return {
        "ok": True,
        "wallet": normalized,
        "reason": reason,
        "signals_closed": signals_closed,
        "blocked_ts": now_ts,
    }


def is_wallet_blocked(conn: sqlite3.Connection, wallet: str) -> bool:
    """Return True if the wallet is on the permanent blocklist."""
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return False
    return conn.execute(
        "SELECT 1 FROM blocked_wallets WHERE wallet=?", (normalized,)
    ).fetchone() is not None


def blocked_wallets_list(conn: sqlite3.Connection, limit: int = 2000) -> list:
    """Return all blocked wallets ordered by most recently blocked."""
    return conn.execute(
        """SELECT wallet, reason, blocked_ts, blocked_by, updated_at
           FROM blocked_wallets ORDER BY blocked_ts DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def ignored_wallets(conn: sqlite3.Connection, limit: int = 2000) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT wallet, reason, ignored_ts, updated_at
        FROM ignored_wallets
        ORDER BY ignored_ts DESC, wallet ASC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()


def ignored_wallet_set(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT wallet FROM ignored_wallets").fetchall()
    return {str(row["wallet"] or "") for row in rows if row["wallet"]}


def dismissed_suspicious_trade_hashes(
    conn: sqlite3.Connection,
    transaction_hashes: Iterable[str],
) -> set[str]:
    normalized = []
    seen: set[str] = set()
    for tx_hash in transaction_hashes:
        tx = _normalize_tx_hash(tx_hash)
        if not tx or tx in seen:
            continue
        seen.add(tx)
        normalized.append(tx)
    if not normalized:
        return set()

    placeholders = ",".join("?" for _ in normalized)
    rows = conn.execute(
        f"""
        SELECT transaction_hash
        FROM dismissed_suspicious_trades
        WHERE transaction_hash IN ({placeholders})
        """,
        normalized,
    ).fetchall()
    return {str(row["transaction_hash"] or "") for row in rows}


def dismissed_suspicious_trade_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM dismissed_suspicious_trades").fetchone()
    if row is None:
        return 0
    return int(row["count"] or 0)


def wallet_low_buy_features(
    conn: sqlite3.Connection,
    wallet: str,
    lookback_days: int = 14,
    min_notional: float = 0.0,
    min_impact_ratio: float = 0.0,
) -> Dict[str, float]:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS hits,
            COALESCE(AVG(notional), 0.0) AS avg_notional,
            COALESCE(SUM(notional), 0.0) AS total_notional,
            COUNT(DISTINCT condition_id) AS market_diversity
        FROM low_buy_trades
        WHERE proxy_wallet = ?
          AND timestamp >= (strftime('%s', 'now') - (? * 86400))
          AND COALESCE(notional, 0) >= ?
          AND (
            CASE
              WHEN COALESCE(market_volume_24h, 0) > 0
              THEN COALESCE(notional, 0) / market_volume_24h
              ELSE 0
            END
          ) >= ?
        """,
        (wallet, lookback_days, min_notional, min_impact_ratio),
    ).fetchone()
    if row is None:
        return {
            "hits": 0.0,
            "avg_notional": 0.0,
            "total_notional": 0.0,
            "market_diversity": 0.0,
        }
    return {
        "hits": float(row["hits"] or 0.0),
        "avg_notional": float(row["avg_notional"] or 0.0),
        "total_notional": float(row["total_notional"] or 0.0),
        "market_diversity": float(row["market_diversity"] or 0.0),
    }


def candidate_user_first_seen(conn: sqlite3.Connection, wallet: str) -> int:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return 0
    row = conn.execute(
        """
        SELECT first_seen_ts
        FROM candidate_users
        WHERE wallet = ?
        """,
        (wallet,),
    ).fetchone()
    if row is None:
        return 0
    return int(row["first_seen_ts"] or 0)


def get_wallet_profile(conn: sqlite3.Connection, wallet: str) -> sqlite3.Row | None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return None
    return conn.execute(
        """
        SELECT
            wallet,
            username,
            pseudonym,
            profile_image,
            profile_image_optimized,
            bio,
            first_activity_ts,
            last_profile_sync_ts,
            profile_joined_ts,
            profile_prediction_count,
            profile_enriched_ts,
            profile_enrichment_source,
            updated_at
        FROM wallet_profiles
        WHERE wallet = ?
        """,
        (wallet,),
    ).fetchone()


def wallet_profiles_map(conn: sqlite3.Connection, wallets: Iterable[str]) -> Dict[str, sqlite3.Row]:
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
    rows = conn.execute(
        f"""
        SELECT
            wallet,
            username,
            pseudonym,
            profile_image,
            profile_image_optimized,
            bio,
            first_activity_ts,
            last_profile_sync_ts,
            profile_joined_ts,
            profile_prediction_count,
            profile_enriched_ts,
            profile_enrichment_source
        FROM wallet_profiles
        WHERE wallet IN ({placeholders})
        """,
        normalized,
    ).fetchall()
    return {str(row["wallet"] or ""): row for row in rows}


def upsert_wallet_profile(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    username: str = "",
    pseudonym: str = "",
    profile_image: str = "",
    profile_image_optimized: str = "",
    bio: str = "",
    first_activity_ts: int = 0,
    last_profile_sync_ts: int = 0,
    profile_joined_ts: int = 0,
    profile_prediction_count: int = 0,
    profile_enriched_ts: int = 0,
    profile_enrichment_source: str = "",
) -> None:
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return

    conn.execute(
        """
        INSERT INTO wallet_profiles (
            wallet,
            username,
            pseudonym,
            profile_image,
            profile_image_optimized,
            bio,
            first_activity_ts,
            last_profile_sync_ts,
            profile_joined_ts,
            profile_prediction_count,
            profile_enriched_ts,
            profile_enrichment_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(wallet) DO UPDATE SET
            username = CASE
                WHEN excluded.username <> '' THEN excluded.username
                ELSE wallet_profiles.username
            END,
            pseudonym = CASE
                WHEN excluded.pseudonym <> '' THEN excluded.pseudonym
                ELSE wallet_profiles.pseudonym
            END,
            profile_image = CASE
                WHEN excluded.profile_image <> '' THEN excluded.profile_image
                ELSE wallet_profiles.profile_image
            END,
            profile_image_optimized = CASE
                WHEN excluded.profile_image_optimized <> '' THEN excluded.profile_image_optimized
                ELSE wallet_profiles.profile_image_optimized
            END,
            bio = CASE
                WHEN excluded.bio <> '' THEN excluded.bio
                ELSE wallet_profiles.bio
            END,
            first_activity_ts = CASE
                WHEN wallet_profiles.first_activity_ts <= 0 THEN excluded.first_activity_ts
                WHEN excluded.first_activity_ts <= 0 THEN wallet_profiles.first_activity_ts
                WHEN excluded.first_activity_ts < wallet_profiles.first_activity_ts THEN excluded.first_activity_ts
                ELSE wallet_profiles.first_activity_ts
            END,
            last_profile_sync_ts = CASE
                WHEN excluded.last_profile_sync_ts > wallet_profiles.last_profile_sync_ts
                THEN excluded.last_profile_sync_ts
                ELSE wallet_profiles.last_profile_sync_ts
            END,
            profile_joined_ts = CASE
                WHEN wallet_profiles.profile_joined_ts <= 0 THEN excluded.profile_joined_ts
                WHEN excluded.profile_joined_ts <= 0 THEN wallet_profiles.profile_joined_ts
                WHEN excluded.profile_joined_ts < wallet_profiles.profile_joined_ts THEN excluded.profile_joined_ts
                ELSE wallet_profiles.profile_joined_ts
            END,
            profile_prediction_count = CASE
                WHEN excluded.profile_prediction_count > wallet_profiles.profile_prediction_count
                THEN excluded.profile_prediction_count
                ELSE wallet_profiles.profile_prediction_count
            END,
            profile_enriched_ts = CASE
                WHEN excluded.profile_enriched_ts > wallet_profiles.profile_enriched_ts
                THEN excluded.profile_enriched_ts
                ELSE wallet_profiles.profile_enriched_ts
            END,
            profile_enrichment_source = CASE
                WHEN excluded.profile_enrichment_source <> '' THEN excluded.profile_enrichment_source
                ELSE wallet_profiles.profile_enrichment_source
            END,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            wallet,
            str(username or "").strip(),
            str(pseudonym or "").strip(),
            str(profile_image or "").strip(),
            str(profile_image_optimized or "").strip(),
            str(bio or "").strip(),
            int(first_activity_ts or 0),
            int(last_profile_sync_ts or time.time()),
            int(profile_joined_ts or 0),
            max(0, int(profile_prediction_count or 0)),
            int(profile_enriched_ts or 0),
            str(profile_enrichment_source or "").strip(),
        ),
    )
    conn.commit()


def start_analysis_run(
    conn: sqlite3.Connection,
    wallet: str,
    now_ts: int,
    commit: bool = True,
) -> int:
    """Create an analysis run marker and return the new analysis version."""
    wallet = _normalize_wallet(wallet)
    if not wallet:
        raise ValueError("wallet is required")
    ts = _coerce_int(now_ts, default=int(time.time()))
    cur = conn.execute(
        """
        INSERT INTO analysis_runs (wallet, started_ts, committed_ts, status, error)
        VALUES (?, ?, 0, 'started', '')
        """,
        (wallet, ts),
    )
    if commit:
        conn.commit()
    return int(cur.lastrowid or 0)


def commit_analysis_run(
    conn: sqlite3.Connection,
    analysis_version: int,
    now_ts: int,
    commit: bool = True,
) -> None:
    """Mark an analysis run as committed."""
    ts = _coerce_int(now_ts, default=int(time.time()))
    cur = conn.execute(
        """
        UPDATE analysis_runs
        SET status = 'committed',
            committed_ts = ?,
            error = ''
        WHERE analysis_version = ?
        """,
        (ts, int(analysis_version)),
    )
    if cur.rowcount <= 0:
        raise ValueError(f"analysis_version not found: {analysis_version}")
    if commit:
        conn.commit()


def fail_analysis_run(
    conn: sqlite3.Connection,
    analysis_version: int,
    now_ts: int,
    error: str,
    commit: bool = True,
) -> None:
    """Mark a specific analysis run as failed."""
    ts = _coerce_int(now_ts, default=int(time.time()))
    cur = conn.execute(
        """
        UPDATE analysis_runs
        SET status = 'failed',
            committed_ts = ?,
            error = ?
        WHERE analysis_version = ?
        """,
        (ts, str(error or "")[:1000], int(analysis_version)),
    )
    if cur.rowcount <= 0:
        raise ValueError(f"analysis_version not found: {analysis_version}")
    if commit:
        conn.commit()


def fail_latest_started_analysis_run(
    conn: sqlite3.Connection,
    wallet: str,
    now_ts: int,
    error: str,
    commit: bool = True,
) -> int:
    """Mark the most recent started run for a wallet as failed. Returns version or 0."""
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return 0
    ts = _coerce_int(now_ts, default=int(time.time()))
    row = conn.execute(
        """
        SELECT analysis_version
        FROM analysis_runs
        WHERE wallet = ?
          AND status = 'started'
        ORDER BY analysis_version DESC
        LIMIT 1
        """,
        (normalized_wallet,),
    ).fetchone()
    if row is None:
        return 0
    analysis_version = _coerce_int(row["analysis_version"], default=0)
    if analysis_version <= 0:
        return 0
    conn.execute(
        """
        UPDATE analysis_runs
        SET status = 'failed',
            committed_ts = ?,
            error = ?
        WHERE analysis_version = ?
        """,
        (ts, str(error or "")[:1000], analysis_version),
    )
    if commit:
        conn.commit()
    return analysis_version


def get_latest_committed_analysis(conn: sqlite3.Connection, wallet: str) -> int:
    """Return the latest committed analysis version for a wallet (or 0 if none)."""
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return 0
    row = conn.execute(
        """
        SELECT analysis_version
        FROM analysis_runs
        WHERE wallet = ? AND status = 'committed'
        ORDER BY analysis_version DESC
        LIMIT 1
        """,
        (wallet,),
    ).fetchone()
    return _coerce_int(row["analysis_version"] if row else 0, default=0)


def acquire_decision(
    conn: sqlite3.Connection,
    signal: Dict[str, Any],
    strategy_id: str,
    action: str,
    worker_id: str,
) -> Dict[str, Any]:
    """
    Acquire an idempotent execution decision claim for a signal.

    Returns a dictionary that includes:
    - acquired: whether this worker owns the claim
    - decision_id: decision row id
    - execution_key: unique decision key
    - claim_token: per-claim token when acquired, otherwise ''
    """
    action_value = str(action or "").strip().upper()
    if action_value not in {"ENTRY", "EXIT"}:
        raise ValueError("action must be ENTRY or EXIT")
    strategy = str(strategy_id or "").strip()
    worker = str(worker_id or "").strip()
    if not strategy or not worker:
        raise ValueError("strategy_id and worker_id are required")

    wallet, condition_id, outcome, signal_ts, analysis_version = _extract_signal_identity(signal)
    now_ts = int(time.time())
    execution_key = _build_execution_key(
        wallet=wallet,
        condition_id=condition_id,
        outcome=outcome,
        strategy_id=strategy,
        action=action_value,
        analysis_version=analysis_version,
    )
    claim_token = uuid4().hex
    stale_claim_after = now_ts - 120
    started_tx = False

    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        conn.execute(
            """
            INSERT OR IGNORE INTO execution_decisions (
                execution_key,
                wallet,
                condition_id,
                outcome,
                strategy_id,
                action,
                analysis_version,
                signal_ts,
                state,
                created_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
            """,
            (
                execution_key,
                wallet,
                condition_id,
                outcome,
                strategy,
                action_value,
                analysis_version,
                signal_ts,
                now_ts,
            ),
        )

        row = conn.execute(
            "SELECT * FROM execution_decisions WHERE execution_key = ?",
            (execution_key,),
        ).fetchone()
        if row is None:
            raise RuntimeError("failed to load execution decision row")

        state = str(row["state"] or "")
        if state in {"executed", "skipped"}:
            if started_tx:
                conn.commit()
            return {
                "acquired": False,
                "decision_id": int(row["decision_id"]),
                "execution_key": execution_key,
                "claim_token": "",
                "state": state,
            }

        updated = conn.execute(
            """
            UPDATE execution_decisions
            SET state = 'claimed',
                claim_token = ?,
                claimed_by = ?,
                claimed_ts = ?,
                error = ''
            WHERE decision_id = ?
              AND (
                    state = 'new'
                 OR (state = 'claimed' AND claimed_by = ?)
                 OR (state = 'claimed' AND claimed_ts <= ?)
                  )
            """,
            (
                claim_token,
                worker,
                now_ts,
                int(row["decision_id"]),
                worker,
                stale_claim_after,
            ),
        )
        if updated.rowcount <= 0:
            locked = conn.execute(
                "SELECT * FROM execution_decisions WHERE decision_id = ?",
                (int(row["decision_id"]),),
            ).fetchone()
            if started_tx:
                conn.commit()
            locked_state = str((locked["state"] if locked else row["state"]) or "")
            return {
                "acquired": False,
                "decision_id": int(row["decision_id"]),
                "execution_key": execution_key,
                "claim_token": "",
                "state": locked_state,
            }

        claimed = conn.execute(
            "SELECT * FROM execution_decisions WHERE decision_id = ?",
            (int(row["decision_id"]),),
        ).fetchone()
        if started_tx:
            conn.commit()
        return {
            "acquired": True,
            "decision_id": int(claimed["decision_id"]),
            "execution_key": execution_key,
            "claim_token": str(claimed["claim_token"] or ""),
            "state": str(claimed["state"] or "claimed"),
        }
    except sqlite3.Error:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise


def claim_or_create_position(
    conn: sqlite3.Connection,
    signal: Dict[str, Any],
    strategy_id: str,
    decision_id: int,
) -> Dict[str, Any]:
    """
    Claim existing active strategy position ownership or create a new one.

    Enforces one active owner per (wallet, condition_id, outcome) via DB constraints.
    """
    strategy = str(strategy_id or "").strip()
    if not strategy:
        raise ValueError("strategy_id is required")
    wallet, condition_id, outcome, signal_ts, _analysis_version = _extract_signal_identity(signal)
    now_ts = int(time.time())
    started_tx = False

    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        existing = conn.execute(
            """
            SELECT *
            FROM strategy_positions
            WHERE wallet = ?
              AND condition_id = ?
              AND outcome = ?
              AND state IN ('pending_entry', 'open', 'pending_exit')
            ORDER BY position_id DESC
            LIMIT 1
            """,
            (wallet, condition_id, outcome),
        ).fetchone()

        if existing is not None:
            position_id = int(existing["position_id"])
            owner_strategy = str(existing["strategy_id"] or "")
            if owner_strategy != strategy:
                conn.execute(
                    """
                    UPDATE execution_decisions
                    SET state = 'skipped',
                        completed_ts = ?,
                        reason = ?
                    WHERE decision_id = ?
                    """,
                    (now_ts, f"position owned by strategy {owner_strategy}", int(decision_id)),
                )
                if started_tx:
                    conn.commit()
                return {
                    "claimed": False,
                    "created": False,
                    "position_id": position_id,
                    "state": str(existing["state"] or ""),
                    "owner_strategy": owner_strategy,
                }

            conn.execute(
                """
                UPDATE execution_decisions
                SET position_id = ?, reason = ''
                WHERE decision_id = ?
                """,
                (position_id, int(decision_id)),
            )
            if started_tx:
                conn.commit()
            return {
                "claimed": True,
                "created": False,
                "position_id": position_id,
                "state": str(existing["state"] or ""),
                "owner_strategy": owner_strategy,
            }

        cur = conn.execute(
            """
            INSERT INTO strategy_positions (
                wallet,
                condition_id,
                outcome,
                strategy_id,
                state,
                entry_decision_id,
                opened_ts,
                updated_ts
            ) VALUES (?, ?, ?, ?, 'pending_entry', ?, ?, ?)
            """,
            (wallet, condition_id, outcome, strategy, int(decision_id), int(signal_ts or now_ts), now_ts),
        )
        position_id = int(cur.lastrowid or 0)
        conn.execute(
            """
            UPDATE execution_decisions
            SET position_id = ?, reason = ''
            WHERE decision_id = ?
            """,
            (position_id, int(decision_id)),
        )
        if started_tx:
            conn.commit()
        return {
            "claimed": True,
            "created": True,
            "position_id": position_id,
            "state": "pending_entry",
            "owner_strategy": strategy,
        }
    except sqlite3.Error:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise


def _flatten_exchange_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "fills", "trades"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _map_order_state(exchange_state: str, filled_shares: float, requested_shares: float, fallback: str) -> str:
    state = str(exchange_state or "").strip().lower()
    if state in {"filled", "matched", "executed", "complete", "completed"}:
        return "filled"
    if state in {"cancelled", "canceled"}:
        return "cancelled"
    if state in {"rejected", "failed"}:
        return "rejected"
    if filled_shares >= requested_shares > 0:
        return "filled"
    if filled_shares > 0:
        return "partially_filled"
    if state in {"submitted", "open", "live", "booked", "pending", "new"}:
        return "submitted"
    return fallback or "submitted"


def reconcile_order_intent(conn: sqlite3.Connection, clob: Any, intent_id: int) -> Dict[str, Any]:
    """
    Reconcile local order intent state with exchange order status and fills.

    This updates `order_intents`, records discovered fills in `order_fills`, and nudges
    `strategy_positions` / `execution_decisions` to terminal states when appropriate.
    """
    now_ts = int(time.time())
    started_tx = False

    try:
        row = conn.execute(
            "SELECT * FROM order_intents WHERE intent_id = ?",
            (int(intent_id),),
        ).fetchone()
        if row is None:
            return {"intent_id": int(intent_id), "status": "missing"}

        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_tx = True

        exchange_order_id = str(row["exchange_order_id"] or "").strip()
        order_payload: Dict[str, Any] = {}
        exchange_fills: List[Dict[str, Any]] = []
        fetch_error = ""

        if exchange_order_id:
            if hasattr(clob, "get_order"):
                try:
                    payload = clob.get_order(exchange_order_id)
                    if isinstance(payload, dict):
                        order_payload = payload
                except Exception as exc:  # pragma: no cover - network/client specific
                    fetch_error = str(exc)

            if hasattr(clob, "get_fills"):
                try:
                    payload = clob.get_fills(order_id=exchange_order_id)
                    exchange_fills = _flatten_exchange_items(payload)
                except TypeError:
                    try:
                        payload = clob.get_fills(exchange_order_id)
                        exchange_fills = _flatten_exchange_items(payload)
                    except Exception as exc:  # pragma: no cover - network/client specific
                        fetch_error = str(exc)
                except Exception as exc:  # pragma: no cover - network/client specific
                    fetch_error = str(exc)

            if not exchange_fills and hasattr(clob, "get_trades"):
                try:
                    payload = clob.get_trades(order_id=exchange_order_id)
                    exchange_fills = _flatten_exchange_items(payload)
                except TypeError:
                    try:
                        payload = clob.get_trades()
                        all_rows = _flatten_exchange_items(payload)
                        exchange_fills = [
                            item
                            for item in all_rows
                            if str(item.get("orderID") or item.get("order_id") or "") == exchange_order_id
                        ]
                    except Exception as exc:  # pragma: no cover - network/client specific
                        fetch_error = str(exc)
                except Exception as exc:  # pragma: no cover - network/client specific
                    fetch_error = str(exc)

        for item in exchange_fills:
            fill_ts = _coerce_int(item.get("timestamp") or item.get("created_ts") or item.get("fill_ts"), default=now_ts)
            price = _coerce_float(item.get("price"), default=0.0)
            shares = _coerce_float(item.get("size") or item.get("shares"), default=0.0)
            if price <= 0 or shares <= 0:
                continue
            notional = _coerce_float(item.get("notional"), default=price * shares)
            fill_pk = str(
                item.get("id")
                or item.get("fill_id")
                or item.get("trade_id")
                or item.get("tx_hash")
                or f"{exchange_order_id}:{fill_ts}:{price:.10f}:{shares:.10f}"
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO order_fills (
                    fill_pk,
                    intent_id,
                    exchange_order_id,
                    fill_ts,
                    side,
                    price,
                    shares,
                    notional,
                    fee,
                    tx_hash,
                    raw_exchange_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill_pk,
                    int(intent_id),
                    exchange_order_id,
                    fill_ts,
                    str(item.get("side") or row["side"] or "BUY").upper(),
                    price,
                    shares,
                    notional,
                    _coerce_float(item.get("fee"), default=0.0),
                    _normalize_tx_hash(item.get("tx_hash") or item.get("transactionHash") or ""),
                    json.dumps(item, separators=(",", ":"), sort_keys=True, default=str),
                ),
            )

        agg = conn.execute(
            """
            SELECT
                COALESCE(SUM(shares), 0) AS total_shares,
                COALESCE(SUM(notional), 0) AS total_notional,
                COALESCE(SUM(price * shares), 0) AS weighted_sum
            FROM order_fills
            WHERE intent_id = ?
            """,
            (int(intent_id),),
        ).fetchone()
        filled_shares = _coerce_float(agg["total_shares"] if agg else 0.0, default=0.0)
        filled_notional = _coerce_float(agg["total_notional"] if agg else 0.0, default=0.0)
        avg_fill_price = (
            _coerce_float(agg["weighted_sum"] if agg else 0.0, default=0.0) / filled_shares
            if filled_shares > 0
            else 0.0
        )

        requested_shares = _coerce_float(row["requested_shares"], default=0.0)
        exchange_state = str(order_payload.get("status") or order_payload.get("state") or "").strip().lower()
        resolved_state = _map_order_state(exchange_state, filled_shares, requested_shares, str(row["state"] or "submitted"))
        close_ts = now_ts if resolved_state in {"filled", "cancelled", "rejected", "error"} else _coerce_int(row["close_ts"], 0)
        raw_exchange_json = json.dumps(
            {"order": order_payload, "fills": exchange_fills},
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )

        conn.execute(
            """
            UPDATE order_intents
            SET state = ?,
                filled_shares = ?,
                filled_notional = ?,
                avg_fill_price = ?,
                last_update_ts = ?,
                close_ts = ?,
                error = ?,
                raw_exchange_json = ?
            WHERE intent_id = ?
            """,
            (
                resolved_state,
                filled_shares,
                filled_notional,
                avg_fill_price,
                now_ts,
                close_ts,
                fetch_error,
                raw_exchange_json,
                int(intent_id),
            ),
        )

        position_id = _coerce_int(row["position_id"], default=0)
        if position_id > 0:
            if str(row["side"] or "").upper() == "BUY":
                conn.execute(
                    """
                    UPDATE strategy_positions
                    SET filled_entry_shares = ?,
                        avg_entry_price = CASE
                            WHEN ? > 0 THEN ?
                            ELSE avg_entry_price
                        END,
                        state = CASE
                            WHEN state = 'pending_entry' AND ? > 0 THEN 'open'
                            ELSE state
                        END,
                        last_reconciled_ts = ?,
                        updated_ts = ?,
                        version = version + 1
                    WHERE position_id = ?
                    """,
                    (filled_shares, avg_fill_price, avg_fill_price, filled_shares, now_ts, now_ts, position_id),
                )
            else:
                close_flag = 1 if resolved_state == "filled" and filled_shares > 0 else 0
                conn.execute(
                    """
                    UPDATE strategy_positions
                    SET filled_exit_shares = ?,
                        avg_exit_price = CASE
                            WHEN ? > 0 THEN ?
                            ELSE avg_exit_price
                        END,
                        state = CASE
                            WHEN ? = 1 THEN 'closed'
                            ELSE state
                        END,
                        closed_ts = CASE
                            WHEN ? = 1 THEN ?
                            ELSE closed_ts
                        END,
                        last_reconciled_ts = ?,
                        updated_ts = ?,
                        version = version + 1
                    WHERE position_id = ?
                    """,
                    (
                        filled_shares,
                        avg_fill_price,
                        avg_fill_price,
                        close_flag,
                        close_flag,
                        now_ts,
                        now_ts,
                        now_ts,
                        position_id,
                    ),
                )

        decision_id = _coerce_int(row["decision_id"], default=0)
        if decision_id > 0:
            decision_state = "claimed"
            if resolved_state == "filled":
                decision_state = "executed"
            elif resolved_state in {"cancelled", "rejected", "error"}:
                decision_state = "failed"
            if decision_state in {"executed", "failed"}:
                conn.execute(
                    """
                    UPDATE execution_decisions
                    SET state = ?,
                        completed_ts = ?,
                        error = CASE
                            WHEN ? <> '' THEN ?
                            ELSE error
                        END
                    WHERE decision_id = ?
                    """,
                    (decision_state, now_ts, fetch_error, fetch_error, decision_id),
                )

        if started_tx:
            conn.commit()

        return {
            "intent_id": int(intent_id),
            "state": resolved_state,
            "filled_shares": filled_shares,
            "filled_notional": filled_notional,
            "avg_fill_price": avg_fill_price,
            "exchange_order_id": exchange_order_id,
            "fill_count": len(exchange_fills),
            "error": fetch_error,
        }
    except sqlite3.Error:
        if started_tx and conn.in_transaction:
            conn.rollback()
        raise




# ---------------------------------------------------------------------------
# Paper-trade deployment gate — shared between API and real_trader
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = (
    "closed", "manual", "resolution_win", "resolution_loss",
    "early_exit", "source_exited", "stale",
)


def paper_trade_gate_status(conn):
    """Return a dict with paper-trade gate metrics and a boolean ``gate_ready``.

    Gate conditions (all must be true):
      * closed_count >= gate_threshold  (from PROOF_MIN_TRADES, default 150)
      * total_pnl > 0
      * win_rate >= PROOF_MIN_WIN_RATE / 100  (default 0.60)
      * avg_roi >= 10.0
    """
    from sniper.settings.web import PROOF_MIN_TRADES, PROOF_MIN_WIN_RATE
    placeholders = ",".join("?" for _ in _TERMINAL_STATUSES)
    row = conn.execute(
        f"""
        SELECT
            COUNT(*)                                          AS closed_count,
            COALESCE(SUM(pnl_usdc), 0)                       AS total_pnl,
            COALESCE(SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END), 0) AS wins,
            AVG(CASE WHEN roi_percent IS NOT NULL THEN roi_percent END) AS avg_roi
        FROM paper_trades
        WHERE status IN ({placeholders})
          AND duplicate_of IS NULL
""",
        _TERMINAL_STATUSES,
    ).fetchone()

    closed_count = int(row["closed_count"] or 0)
    total_pnl = float(row["total_pnl"] or 0)
    wins = int(row["wins"] or 0)
    avg_roi = float(row["avg_roi"] or 0) if row["avg_roi"] is not None else 0.0
    win_rate = wins / max(closed_count, 1)

    gate_threshold = PROOF_MIN_TRADES
    win_rate_threshold = PROOF_MIN_WIN_RATE / 100.0
    gate_ready = (
        closed_count >= gate_threshold
        and total_pnl > 0
        and win_rate >= win_rate_threshold
        and avg_roi >= 10.0
    )

    reasons = []
    if closed_count < gate_threshold:
        reasons.append(f"trades {closed_count}/{gate_threshold}")
    if total_pnl <= 0:
        reasons.append(f"pnl ${total_pnl:.2f} <= 0")
    if win_rate < win_rate_threshold:
        reasons.append(f"WR {win_rate:.1%} < {PROOF_MIN_WIN_RATE:.0f}%")
    if avg_roi < 10.0:
        reasons.append(f"avg ROI {avg_roi:.1f}% < 10%")

    return {
        "gate_ready": gate_ready,
        "closed_count": closed_count,
        "gate_threshold": gate_threshold,
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 4),
        "avg_roi": round(avg_roi, 2),
        "fail_reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Proof Bar — go-live readiness dashboard
# ---------------------------------------------------------------------------

def proof_bar_status(conn: sqlite3.Connection) -> dict:
    """Compute all 5 proof bar criteria and supporting data.

    Returns a dict with: criteria, overall, pace, monthly, health_details,
    charts, data_gaps.  See spec for full schema.
    """
    from sniper.settings.web import (
        PROOF_MIN_TRADES, PROOF_MIN_INSIDER, PROOF_MIN_WIN_RATE,
        PROOF_SLIPPAGE_PCT, PROOF_MIN_MONTHS,
    )
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    # --- Qualifying statuses (clean resolution paths only) ---
    PROOF_STATUSES = ("resolution_win", "resolution_loss", "early_exit", "manual")
    placeholders = ",".join("?" for _ in PROOF_STATUSES)

    # --- Fetch all qualifying closed trades ---
    rows = conn.execute(
        f"""
        SELECT entry_price, exit_price, size_usdc, pnl_usdc, roi_percent,
               signal_insider_score, status, closed_at
        FROM paper_trades
        WHERE signal_insider_score >= ?
          AND status IN ({placeholders})
          AND duplicate_of IS NULL
""",
        (PROOF_MIN_INSIDER, *PROOF_STATUSES),
    ).fetchall()

    # --- Filter and compute ---
    trades = []
    data_gaps = {"null_roi": 0, "zero_entry_price": 0}

    for r in rows:
        entry_price = r["entry_price"] if r["entry_price"] is not None else 0
        if entry_price <= 0:
            data_gaps["zero_entry_price"] += 1
            continue
        trades.append(dict(r))

    trade_count = len(trades)

    # Win rate (ADR-002: entry_price < 0.80 only)
    wr_trades = [t for t in trades if t["entry_price"] < 0.80]
    wr_wins = sum(1 for t in wr_trades if (t["pnl_usdc"] or 0) > 0)
    wr_denom = len(wr_trades)
    win_rate = (wr_wins / wr_denom * 100) if wr_denom > 0 else 0.0

    # Slippage-adjusted P&L (one-sided, entry only)
    slip_pnl = 0.0
    for t in trades:
        roi = t["roi_percent"]
        size = t["size_usdc"] or 0
        if roi is None:
            data_gaps["null_roi"] += 1
            continue
        raw_pnl = (roi / 100) * size
        # I4 audit: slippage is one-sided (entry only). For resolution trades this
        # is correct (exit is redemption, no market impact). For early_exit trades
        # this underestimates friction by ~1.25x. Accepted as conservative for the
        # go-live gate — real slippage measurement should replace this before scaling.
        slippage = size * PROOF_SLIPPAGE_PCT
        slip_pnl += raw_pnl - slippage

    slip_pnl = round(slip_pnl, 3)

    # Monthly breakdown
    month_buckets: dict = {}
    for t in trades:
        closed_at = t.get("closed_at")
        if not closed_at:
            continue
        month_key = str(closed_at)[:7]  # "YYYY-MM"
        month_buckets.setdefault(month_key, []).append(t)

    monthly = []
    complete_months = 0
    losing_months = 0
    for month_key in sorted(month_buckets.keys()):
        bucket = month_buckets[month_key]
        m_trades = len(bucket)
        m_wr_trades = [t for t in bucket if t["entry_price"] < 0.80]
        m_wr_wins = sum(1 for t in m_wr_trades if (t["pnl_usdc"] or 0) > 0)
        m_wr = (m_wr_wins / len(m_wr_trades) * 100) if m_wr_trades else 0
        m_pnl_raw = sum(t["pnl_usdc"] or 0 for t in bucket)
        m_pnl_slip = sum(
            ((t["roi_percent"] or 0) / 100) * (t["size_usdc"] or 0)
            - (t["size_usdc"] or 0) * PROOF_SLIPPAGE_PCT
            for t in bucket if t["roi_percent"] is not None
        )
        # Wins/losses for display (all trades, not just WR-qualifying)
        m_wins = sum(1 for t in bucket if (t["pnl_usdc"] or 0) > 0)
        m_losses = m_trades - m_wins

        # Month is complete if we are past its last day
        try:
            year, mon = int(month_key[:4]), int(month_key[5:7])
            if mon == 12:
                first_of_next = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                first_of_next = datetime(year, mon + 1, 1, tzinfo=timezone.utc)
            complete = now >= first_of_next
        except (ValueError, IndexError):
            complete = False

        profitable = m_pnl_slip > 0
        if complete:
            complete_months += 1
            if not profitable:
                losing_months += 1

        monthly.append({
            "month": month_key,
            "trades": m_trades,
            "wins": m_wins,
            "losses": m_losses,
            "win_rate": round(m_wr, 1),
            "pnl_raw": round(m_pnl_raw, 2),
            "pnl_slip": round(m_pnl_slip, 2),
            "complete": complete,
            "profitable": profitable,
        })

    # Criterion 4: need PROOF_MIN_MONTHS complete months with ZERO losing
    no_losing_month_value = complete_months if losing_months == 0 else 0

    # --- Status per criterion ---
    def _status(value, target, warn_fn):
        if value >= target:
            return "pass"
        if warn_fn(value, target):
            return "warn"
        return "fail"

    criteria = {
        "closed_trades": {
            "value": trade_count,
            "target": PROOF_MIN_TRADES,
            "status": _status(trade_count, PROOF_MIN_TRADES,
                              lambda v, t: v >= t * 0.9),
        },
        "win_rate": {
            "value": round(win_rate, 1),
            "target": PROOF_MIN_WIN_RATE,
            "status": _status(win_rate, PROOF_MIN_WIN_RATE,
                              lambda v, t: v >= t - 2.0),
        },
        "slip_pnl": {
            "value": slip_pnl,
            "target": 0.0,
            "status": _status(slip_pnl, 0.01,  # must be strictly > 0
                              lambda v, t: 0 < v < 50),
        },
        "no_losing_month": {
            "value": no_losing_month_value,
            "target": PROOF_MIN_MONTHS,
            "status": _status(no_losing_month_value, PROOF_MIN_MONTHS,
                              lambda v, t: v == t - 1),
        },
        "health": {
            "value": "pass",
            "target": "pass",
            "status": "pass",  # computed separately via health_check
        },
    }

    overall = "ready" if all(
        c["status"] == "pass" for c in criteria.values()
    ) else "not_ready"

    # --- Pace ---
    seven_days_ago = (now - timedelta(days=7)).isoformat()
    recent_trades = [t for t in trades if (t.get("closed_at") or "") >= seven_days_ago]
    seven_day_rate = round(len(recent_trades) / 7, 1) if recent_trades else 0
    trades_to_go = max(0, PROOF_MIN_TRADES - trade_count)

    eta_date = None
    if seven_day_rate > 0 and trades_to_go > 0:
        days_remaining = trades_to_go / seven_day_rate
        eta_date = (now + timedelta(days=days_remaining)).strftime("%Y-%m-%d")

    # Open exposure
    open_row = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(size_usdc), 0) AS total "
        "FROM paper_trades WHERE status = 'open'"
    ).fetchone()
    open_positions = int(open_row["cnt"] or 0)
    open_exposure = round(float(open_row["total"] or 0), 2)

    pace = {
        "seven_day_rate": seven_day_rate,
        "trades_to_go": trades_to_go,
        "eta_date": eta_date,
        "cumulative_equity": slip_pnl,
        "open_exposure": open_exposure,
        "open_positions": open_positions,
    }

    # --- Chart data (daily granularity) ---
    # Cumulative trades by closed_at date
    sorted_trades = sorted(trades, key=lambda t: t.get("closed_at") or "")
    cum_trades_chart = []
    cum_count = 0
    last_date = None
    for t in sorted_trades:
        d = str(t.get("closed_at") or "")[:10]
        if not d:
            continue
        cum_count += 1
        if d != last_date:
            cum_trades_chart.append({"date": d, "count": cum_count})
            last_date = d
        else:
            cum_trades_chart[-1]["count"] = cum_count

    # 30-day equity curve (daily cumulative slip-adj P&L)
    equity_chart = []
    cum_eq = 0.0
    last_eq_date = None
    for t in sorted_trades:
        roi = t["roi_percent"]
        if roi is None:
            continue
        size = t["size_usdc"] or 0
        pnl = (roi / 100) * size - size * PROOF_SLIPPAGE_PCT
        cum_eq += pnl
        d = str(t.get("closed_at") or "")[:10]
        if not d:
            continue
        if d != last_eq_date:
            equity_chart.append({"date": d, "equity": round(cum_eq, 2)})
            last_eq_date = d
        else:
            equity_chart[-1]["equity"] = round(cum_eq, 2)

    # --- Health (point-in-time, populated by caller or API layer) ---
    health_details = {}  # populated by API endpoint using health_check module

    return {
        "criteria": criteria,
        "overall": overall,
        "pace": pace,
        "monthly": monthly,
        "health_details": health_details,
        "charts": {
            "cumulative_trades": cum_trades_chart,
            "equity_curve_30d": equity_chart,
        },
        "data_gaps": data_gaps,
    }


# ---------------------------------------------------------------------------
# Strategy CRUD helpers (shared by Flask routes and MCP tools)
# ---------------------------------------------------------------------------

VALID_STRATEGY_STATUSES = {"active", "paused", "stopped"}


def insert_strategy(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    strategy_type: str,
    parameters: dict,
    status: str = "paused",
    label: str = "",
    commit: bool = True,
) -> int:
    """Insert a new strategy row. Returns the new strategy ID."""
    if status not in VALID_STRATEGY_STATUSES:
        raise ValueError(
            f"Invalid status '{status}'. Must be one of {VALID_STRATEGY_STATUSES}"
        )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    params_json = json.dumps(parameters) if isinstance(parameters, dict) else parameters
    instance_tag = _current_instance_tag()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO strategies
           (wallet, label, strategy_type, parameters, status,
            ai_generated, trades_triggered, pnl_usdc, created_at, updated_at, instance_tag)
           VALUES (?, ?, ?, ?, ?, 0, 0, 0.0, ?, ?, ?)""",
        (wallet, label, strategy_type, params_json, status, now, now, instance_tag),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid


def update_strategy_status(
    conn: sqlite3.Connection,
    strategy_id: int,
    new_status: str,
    *,
    commit: bool = True,
) -> bool:
    """Update a strategy's status. Returns True if a row was updated."""
    if new_status not in VALID_STRATEGY_STATUSES:
        raise ValueError(
            f"Invalid status '{new_status}'. Must be one of {VALID_STRATEGY_STATUSES}"
        )
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE strategies SET status = ?, updated_at = ? WHERE id = ?",
        (new_status, now, strategy_id),
    )
    if commit:
        conn.commit()
    return cursor.rowcount > 0


def get_strategy_by_id(
    conn: sqlite3.Connection,
    strategy_id: int,
) -> sqlite3.Row | None:
    """Return the full strategies row for a single ID, or None."""
    return conn.execute(
        "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
    ).fetchone()


def insert_mcp_audit_log(
    conn: sqlite3.Connection,
    *,
    tool_name: str,
    caller: str = "",
    args_json: str = "{}",
    result_summary: str = "",
    error: str = "",
    commit: bool = True,
) -> int:
    """Insert a row into mcp_audit_log and return its id."""
    cursor = conn.execute(
        """INSERT INTO mcp_audit_log (tool_name, caller, args_json, result_summary, error)
           VALUES (?, ?, ?, ?, ?)""",
        (tool_name, caller, args_json, result_summary, error),
    )
    if commit:
        conn.commit()
    return cursor.lastrowid


def get_wallet_analysis(
    conn: sqlite3.Connection,
    wallet: str,
) -> sqlite3.Row | None:
    """Return the full user_analysis row for a single wallet, or None."""
    wallet = _normalize_wallet(wallet)
    if not wallet:
        return None
    return conn.execute(
        "SELECT * FROM user_analysis WHERE wallet = ?", (wallet,)
    ).fetchone()


_CLASSIFICATION_REASON_CHECK_KEYS: tuple[str, ...] = (
    "insider_score_gte_threshold",
    "emerging_score_gte_threshold",
    "repeat_hits_ok",
    "positions_ok",
    "pnl_ok",
    "top_open_value_ok",
    "total_open_value_ok",
    "max_position_ok",
    "serious_trades_ok",
    "serious_notional_ok",
    "low_frequency_ok",
    "position_signal_strength_ok",
    "repeat_or_position_signal_ok",
    "position_speculator_guard_ok",
    "emerging_top_open_value_ok",
    "emerging_total_open_value_ok",
    "serious_or_position_signal_ok",
)
_CLASSIFICATION_REASON_CHECK_SET = set(_CLASSIFICATION_REASON_CHECK_KEYS)
_CLASSIFICATION_REASON_RE = re.compile(r"(?:^|,\s*)([a-z_]+)=([^,]+)")


def _parse_classification_reason_flags(reason: str | None) -> Dict[str, bool]:
    """Extract boolean gate flags from the structured analyze_users reason string."""
    parsed: Dict[str, bool] = {}
    text = str(reason or "").strip()
    if not text or "=" not in text:
        return parsed
    for key, raw_value in _CLASSIFICATION_REASON_RE.findall(text):
        if key not in _CLASSIFICATION_REASON_CHECK_SET:
            continue
        value = raw_value.strip().lower()
        if value == "yes":
            parsed[key] = True
        elif value == "no":
            parsed[key] = False
    return parsed


def classification_diagnostics(
    conn: sqlite3.Connection,
    *,
    min_insider_score: float = 55.0,
    limit: int = 20,
) -> Dict[str, Any]:
    """Summarize why high-insider-score wallets are or are not qualifying.

    This is meant to explain live signal droughts: high insider score alone is
    not enough for `emerging_insider` / `insider_candidate`.
    """
    rows = conn.execute(
        """
        SELECT wallet,
               analyzed_ts,
               insider_score,
               speculator_score,
               classification,
               reason,
               closed_sample_size,
               qualifying_roi,
               position_count,
               pnl,
               max_position_value,
               total_open_value,
               position_signal_count,
               position_insider_candidate_count,
               position_speculator_count,
               max_position_insider_score,
               max_position_speculator_score,
               is_bot
        FROM user_analysis
        WHERE insider_score >= ?
        ORDER BY insider_score DESC, closed_sample_size DESC, analyzed_ts DESC
        """,
        (float(min_insider_score),),
    ).fetchall()

    classification_counts: Dict[str, int] = {}
    failed_check_counts: Dict[str, int] = {}
    samples: List[Dict[str, Any]] = []
    low_signal_wallets = 0
    parsed_wallets = 0

    for row in rows:
        classification = str(row["classification"] or "unclassified")
        classification_counts[classification] = classification_counts.get(classification, 0) + 1

        flags = _parse_classification_reason_flags(row["reason"])
        failed_checks = sorted(key for key, ok in flags.items() if not ok)

        if classification == "low_signal":
            low_signal_wallets += 1
            if flags:
                parsed_wallets += 1
            for key in failed_checks:
                failed_check_counts[key] = failed_check_counts.get(key, 0) + 1

            if len(samples) < max(1, int(limit)):
                samples.append(
                    {
                        "wallet": row["wallet"],
                        "insider_score": float(row["insider_score"] or 0.0),
                        "speculator_score": float(row["speculator_score"] or 0.0),
                        "classification": classification,
                        "closed_sample_size": int(row["closed_sample_size"] or 0),
                        "qualifying_roi": (
                            float(row["qualifying_roi"]) if row["qualifying_roi"] is not None else None
                        ),
                        "position_count": int(row["position_count"] or 0),
                        "position_signal_count": int(row["position_signal_count"] or 0),
                        "position_insider_candidate_count": int(row["position_insider_candidate_count"] or 0),
                        "max_position_insider_score": float(row["max_position_insider_score"] or 0.0),
                        "max_position_speculator_score": float(row["max_position_speculator_score"] or 0.0),
                        "max_position_value": float(row["max_position_value"] or 0.0),
                        "total_open_value": float(row["total_open_value"] or 0.0),
                        "is_bot": bool(int(row["is_bot"] or 0)),
                        "failed_checks": failed_checks,
                        "reason": str(row["reason"] or ""),
                    }
                )

    signal_row = conn.execute(
        """
        SELECT COUNT(*) AS open_signals,
               COALESCE(SUM(CASE WHEN position_insider_score >= ? THEN 1 ELSE 0 END), 0) AS open_signals_meeting_insider_threshold,
               MAX(position_insider_score) AS max_open_signal_insider_score
        FROM position_signals
        WHERE is_open = 1
        """,
        (float(min_insider_score),),
    ).fetchone()

    top_failed_checks = [
        {
            "check": key,
            "count": count,
            "pct_of_low_signal": round((count / max(low_signal_wallets, 1)) * 100.0, 1),
        }
        for key, count in sorted(failed_check_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    return {
        "summary": {
            "min_insider_score": float(min_insider_score),
            "wallets_above_threshold": len(rows),
            "classification_counts": classification_counts,
            "insider_like_wallets": sum(
                classification_counts.get(name, 0)
                for name in ("insider_candidate", "emerging_insider")
            ),
            "high_winrate_review_wallets": classification_counts.get("high_winrate_review", 0),
            "low_signal_wallets": low_signal_wallets,
            "parsed_low_signal_wallets": parsed_wallets,
            "open_signals": int(signal_row["open_signals"] or 0) if signal_row else 0,
            "open_signals_meeting_insider_threshold": (
                int(signal_row["open_signals_meeting_insider_threshold"] or 0) if signal_row else 0
            ),
            "max_open_signal_insider_score": (
                float(signal_row["max_open_signal_insider_score"] or 0.0) if signal_row else 0.0
            ),
        },
        "top_failed_checks": top_failed_checks,
        "sample_low_signal_wallets": samples,
    }


def paper_trades_with_scores(
    conn: sqlite3.Connection,
    *,
    min_insider_score: float | None = None,
    status_filter: str | None = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return paper trades with signal scores and aggregates for MCP tools."""
    where = ["1=1"]
    params: List[Any] = []

    if status_filter:
        where.append("status = ?")
        params.append(status_filter)
    else:
        where.append("status != 'open'")

    if min_insider_score is not None:
        where.append("signal_insider_score >= ?")
        params.append(float(min_insider_score))

    params.append(int(limit))
    rows = conn.execute(
        f"""SELECT trade_id, wallet, condition_id, outcome, direction,
                   entry_price, exit_price, size_usdc, pnl_usdc, roi_percent,
                   signal_insider_score, signal_speculator_score,
                   status, opened_at, closed_at
            FROM paper_trades
            WHERE {" AND ".join(where)}
            ORDER BY closed_at DESC NULLS LAST, opened_at DESC
            LIMIT ?""",
        params,
    ).fetchall()

    trades = []
    for r in rows:
        trades.append({
            "trade_id": r["trade_id"],
            "wallet": r["wallet"],
            "condition_id": r["condition_id"],
            "outcome": r["outcome"],
            "entry_price": float(r["entry_price"] or 0),
            "exit_price": float(r["exit_price"] or 0) if r["exit_price"] else None,
            "size_usdc": float(r["size_usdc"] or 0),
            "pnl_usdc": float(r["pnl_usdc"] or 0),
            "roi_percent": float(r["roi_percent"] or 0) if r["roi_percent"] else None,
            "signal_insider_score": float(r["signal_insider_score"] or 0),
            "signal_speculator_score": float(r["signal_speculator_score"] or 0),
            "status": r["status"],
            "opened_at": r["opened_at"],
            "closed_at": r["closed_at"],
        })
    return trades


def wallet_tag_pnl_rows(
    conn: sqlite3.Connection,
    wallet: str | None = None,
) -> list[dict]:
    """Read rows from `wallet_tag_pnl`, optionally filtered by wallet.

    Returns a list of plain dicts. Empty list if the table doesn't exist yet
    (e.g. first-run before `ensure_wallet_tag_pnl_table` / init_db).
    avg_hold_days is NEVER included in output (dropped in B1 source-switch spec §4.2).
    """
    # Defensive: if the table isn't present yet, return [].
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='wallet_tag_pnl'"
    ).fetchone()[0]
    if not exists:
        return []

    # PRAGMA-guard: detect which optional columns are present so we can SELECT
    # safely regardless of migration state.
    pragma_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(wallet_tag_pnl)").fetchall()
    }
    has_held_only = "win_rate_held_only" in pragma_cols
    has_n_held = "n_held_to_resolution" in pragma_cols
    has_open_pnl = "open_unrealized_pnl" in pragma_cols
    has_n_open = "n_open_positions" in pragma_cols
    has_n_dead = "n_open_dead" in pragma_cols

    held_select = ""
    if has_held_only:
        held_select += ", win_rate_held_only"
    if has_n_held:
        held_select += ", n_held_to_resolution"
    if has_open_pnl:
        held_select += ", open_unrealized_pnl"
    if has_n_open:
        held_select += ", n_open_positions"
    if has_n_dead:
        held_select += ", n_open_dead"

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        base_select = (
            "SELECT wallet, tag, n_positions, realized_pnl, win_rate, "
            "       conviction_concentration, computed_at"
            + held_select
        )
        if wallet is None:
            rows = conn.execute(
                base_select + " FROM wallet_tag_pnl ORDER BY wallet ASC, tag ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                base_select + " FROM wallet_tag_pnl WHERE wallet = ? ORDER BY tag ASC",
                (wallet,),
            ).fetchall()
    finally:
        conn.row_factory = prev_factory

    out: list[dict] = []
    for r in rows:
        d: dict = {
            "wallet": r["wallet"],
            "tag": r["tag"],
            "n_positions": int(r["n_positions"] or 0),
            "realized_pnl": float(r["realized_pnl"] or 0.0),
            "win_rate": float(r["win_rate"] or 0.0),
            "conviction_concentration": float(r["conviction_concentration"] or 0.0),
            "computed_at": int(r["computed_at"] or 0),
        }
        if has_held_only:
            raw = r["win_rate_held_only"]
            d["win_rate_held_only"] = float(raw) if raw is not None else None
        if has_n_held:
            d["n_held_to_resolution"] = int(r["n_held_to_resolution"] or 0)
        if has_open_pnl:
            d["open_unrealized_pnl"] = float(r["open_unrealized_pnl"] or 0.0)
        if has_n_open:
            d["n_open_positions"] = int(r["n_open_positions"] or 0)
        if has_n_dead:
            d["n_open_dead"] = int(r["n_open_dead"] or 0)
        out.append(d)
    return out

def peer_baseline_for_grain(
    conn: sqlite3.Connection,
    grain: str,
    tag: str,
    *,
    min_n_held: int = 10,
    exclude_wallet: str | None = None,
) -> tuple[float, int] | None:
    """Equal-weighted peer baseline for the held-branch H5 tag-edge analyst.

    Closes wallet-protocol-bulletproofing gap C3. Adapts `wallet_tag_pnl` into
    the `peer_baseline_fn(grain, tag, wallet) -> (mean_wr, peer_count) | None`
    contract consumed by held / flipper tag-grain analysts.

    Args:
        grain: "leaf" — `tag` matches `wallet_tag_pnl.tag` directly.
                "nav"  — `tag` is a nav category joined through
                         `tag_nav_category`.
        tag: tag value to look up.
        min_n_held: minimum `n_held_to_resolution` for a wallet to count as a
            peer. Mirrors the protocol's `H5_MIN_N_PER_GRAIN` so peer WRs are
            themselves powered.
        exclude_wallet: when set, the analyzed wallet is removed from the
            peer set so it does not contaminate its own baseline. Bulletproofing
            follow-up to gap C3 (self-contamination defect).

    Returns:
        None when the table is missing, the grain is unsupported, or no peers
        meet the floor / have a non-NULL `win_rate_held_only` (after self-
        exclusion). Otherwise `(mean_wr, peer_count)` with mean_wr the
        equal-weighted average of `win_rate_held_only` across qualifying peers.
    """
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='wallet_tag_pnl'"
    ).fetchone()[0]
    if not exists:
        return None
    if grain == "leaf":
        sql = (
            "SELECT win_rate_held_only "
            "  FROM wallet_tag_pnl "
            " WHERE tag = ? "
            "   AND n_held_to_resolution >= ? "
            "   AND win_rate_held_only IS NOT NULL"
        )
        params: list = [tag, int(min_n_held)]
        if exclude_wallet is not None:
            sql += " AND wallet != ?"
            params.append(exclude_wallet)
        rows = conn.execute(sql, tuple(params)).fetchall()
        if not rows:
            return None
        wrs = [float(r[0]) for r in rows]
        return (sum(wrs) / len(wrs), len(wrs))

    if grain != "nav":
        return None

    nav_exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='tag_nav_category'"
    ).fetchone()[0]
    if not nav_exists:
        return None

    sql = (
        "SELECT wallet, "
        "       SUM(win_rate_held_only * n_held_to_resolution) AS weighted_wins, "
        "       SUM(n_held_to_resolution) AS n_held "
        "  FROM wallet_tag_pnl "
        "  JOIN tag_nav_category ON tag_nav_category.tag = wallet_tag_pnl.tag "
        " WHERE tag_nav_category.nav_category = ? "
        "   AND COALESCE(tag_nav_category.confidence, '') NOT IN ('skip', 'ambiguous') "
        "   AND wallet_tag_pnl.n_held_to_resolution > 0 "
        "   AND wallet_tag_pnl.win_rate_held_only IS NOT NULL"
    )
    params = [tag]
    if exclude_wallet is not None:
        sql += " AND wallet_tag_pnl.wallet != ?"
        params.append(exclude_wallet)
    sql += " GROUP BY wallet HAVING SUM(n_held_to_resolution) >= ?"
    params.append(int(min_n_held))
    rows = conn.execute(sql, tuple(params)).fetchall()
    if not rows:
        return None
    peer_wrs = [float(r[1]) / float(r[2]) for r in rows if float(r[2] or 0) > 0]
    if not peer_wrs:
        return None
    return (sum(peer_wrs) / len(peer_wrs), len(peer_wrs))


def wallet_tag_fit_rows(
    conn: sqlite3.Connection,
    wallet: str | None = None,
    tag: str | None = None,
    confidence_only: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Read rows from `wallet_tag_fit`, with optional filters.

    Returns a list of plain dicts. Empty list if the table doesn't exist yet.
    """
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='wallet_tag_fit'"
    ).fetchone()[0]
    if not exists:
        return []

    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        conditions: list[str] = []
        params: list = []
        if wallet is not None:
            conditions.append("wallet = ?")
            params.append(wallet)
        if tag is not None:
            conditions.append("tag = ?")
            params.append(tag)
        if confidence_only:
            conditions.append("confidence_flag = 'ok'")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""
        sql = (
            f"SELECT wallet, tag, fit_score, fit_rank, peer_count, "
            f"       confidence_flag, computed_at, inputs_json "
            f"FROM wallet_tag_fit {where} "
            f"ORDER BY tag ASC, fit_rank ASC "
            f"{limit_clause}"
        )
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.row_factory = prev_factory

    out: list[dict] = []
    for r in rows:
        out.append({
            "wallet": r["wallet"],
            "tag": r["tag"],
            "fit_score": float(r["fit_score"] or 0.0),
            "fit_rank": int(r["fit_rank"] or 0),
            "peer_count": int(r["peer_count"] or 0),
            "confidence_flag": r["confidence_flag"] or "",
            "computed_at": int(r["computed_at"] or 0),
            "inputs_json": r["inputs_json"] or "{}",
        })
    return out
