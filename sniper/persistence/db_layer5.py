"""Layer-5 wallet decision persistence helpers.

Tables (all per Phase 2 output-contract spec § 3.1–§ 3.3 and Phase 3 plan § 0.2,
§ A7):
- wallet_layer5_decision: per-wallet operational verdict + cached analytical context
- position_signal_decision: per (wallet, condition_id, outcome) cached wallet decision
- analyzer_runs: append-only audit row per protocol invocation
- tag_market_stats: market-level tag aggregates (Workstream D fills it)
- position_market_state: per-position raw + derived market state (the KEEP-44
  half of the position_signals split; locked by Workstream B trace
  docs/research/scopes/2026-04-29-position-signals-column-validation.md)
- wallet_analysis_batch / wallet_analysis_batch_item: progressive per-wallet
  batch status + summary surface for Phase F backfill runs
- wallet_analyzer_intake_queue: analyzer-owned source-clean intake queue;
  never fed by the legacy low-buy/discovery stack

Schemas are locked here. Only ``ensure_*`` helpers live in this module —
the atomic write helpers (Task A5) land in ``sniper/persistence/persistence.py``.
"""
from __future__ import annotations

import sqlite3


def _ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_wallet_layer5_decision(conn: sqlite3.Connection) -> None:
    """Create wallet_layer5_decision table + indexes if absent.

    Per Phase 2 spec § 3.1. PK = (wallet, analysis_version) so analyzer
    upgrades preserve prior rows. Consumers MUST filter is_current=1 for
    canonical-latest reads; the persistence layer flips older versions to 0.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_layer5_decision (
            wallet                          TEXT    NOT NULL,
            analysis_version                TEXT    NOT NULL,
            analyzer_run_id                 TEXT    NOT NULL,

            as_of_ts                        INTEGER NOT NULL,
            data_window_start_ts            INTEGER,
            data_window_end_ts              INTEGER,

            gate_result                     TEXT    NOT NULL,
            trading_mode                    TEXT    NOT NULL,
            overall_verdict                 TEXT    NOT NULL,
            primary_edge_type               TEXT,
            flip_rate                       REAL,
            n_closed_positions              INTEGER NOT NULL DEFAULT 0,
            n_held_to_resolution            INTEGER NOT NULL DEFAULT 0,
            n_open_positions                INTEGER NOT NULL DEFAULT 0,
            n_open_dead                     INTEGER NOT NULL DEFAULT 0,
            net_pnl_usdc                    REAL    NOT NULL DEFAULT 0,
            dw_roi_point                    REAL,
            dw_roi_ci_lower                 REAL,
            dw_roi_ci_upper                 REAL,
            ew_roi_point                    REAL,
            activity_cap_hit                INTEGER NOT NULL DEFAULT 0,
            archetype_layer1                TEXT,

            action_decision                 TEXT    NOT NULL,
            action_reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
            confidence_band                 TEXT    NOT NULL,
            is_current                      INTEGER NOT NULL DEFAULT 1,

            held_branch_summary_json        TEXT    NOT NULL DEFAULT '{}',
            flipper_branch_summary_json     TEXT    NOT NULL DEFAULT '{}',
            tag_grain_summary_json          TEXT    NOT NULL DEFAULT '[]',
            capital_ledger_summary_json     TEXT    NOT NULL DEFAULT '{}',
            data_quality_flags_json         TEXT    NOT NULL DEFAULT '[]',
            applicable_limitations_json     TEXT    NOT NULL DEFAULT '[]',
            open_questions_json             TEXT    NOT NULL DEFAULT '[]',
            copyability_note                TEXT,

            report_markdown                 TEXT    NOT NULL DEFAULT '',

            created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

            PRIMARY KEY (wallet, analysis_version),
            FOREIGN KEY (analyzer_run_id) REFERENCES analyzer_runs(run_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wld_action_current
            ON wallet_layer5_decision(action_decision, is_current, as_of_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wld_wallet_current
            ON wallet_layer5_decision(wallet, is_current, as_of_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wld_run
            ON wallet_layer5_decision(analyzer_run_id)
        """
    )
    conn.commit()


def ensure_position_signal_decision(conn: sqlite3.Connection) -> None:
    """Create position_signal_decision table + indexes if absent.

    Per Phase 2 spec § 3.2. tag_fit_score / tag_fit_rank are context-only
    (cohort rank); no consumer may compare them to a threshold to authorize.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_signal_decision (
            wallet                          TEXT    NOT NULL,
            condition_id                    TEXT    NOT NULL,
            outcome                         TEXT    NOT NULL,
            analysis_version                TEXT    NOT NULL,

            wallet_action_decision          TEXT    NOT NULL,

            primary_tag                     TEXT,
            tag_fit_score                   REAL,
            tag_fit_rank                    INTEGER,
            tag_fit_peer_count              INTEGER,
            tag_fit_confidence_flag         TEXT,

            signal_reason_codes_json        TEXT    NOT NULL DEFAULT '[]',
            analyzed_ts                     INTEGER NOT NULL,
            created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

            PRIMARY KEY (wallet, condition_id, outcome, analysis_version)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_psd_action
            ON position_signal_decision(wallet_action_decision, analyzed_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_psd_wallet
            ON position_signal_decision(wallet, analyzed_ts DESC)
        """
    )
    conn.commit()


def ensure_analyzer_runs(conn: sqlite3.Connection) -> None:
    """Create analyzer_runs append-only audit table + indexes if absent.

    Per Phase 2 spec § 3.3. One row per ``run_wallet_protocol`` invocation.
    Pruning policy lives in a follow-up plan (Phase 3 plan § 0.5).
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analyzer_runs (
            run_id                          TEXT    PRIMARY KEY,
            analysis_version                TEXT    NOT NULL,
            wallet                          TEXT    NOT NULL,
            started_ts                      INTEGER NOT NULL,
            finished_ts                     INTEGER,
            duration_ms                     INTEGER,
            success                         INTEGER NOT NULL,
            error_class                     TEXT,
            error_message                   TEXT,
            invocation_source               TEXT    NOT NULL DEFAULT '',
            args_json                       TEXT    NOT NULL DEFAULT '{}',
            fetch_quality_flags_json        TEXT    NOT NULL DEFAULT '[]',
            activity_rows_fetched           INTEGER,
            activity_cap_hit                INTEGER,
            subgraph_fallback_used          INTEGER NOT NULL DEFAULT 0,
            created_at                      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ar_wallet_started
            ON analyzer_runs(wallet, started_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_ar_success_started
            ON analyzer_runs(success, started_ts DESC)
        """
    )
    conn.commit()


def ensure_tag_market_stats(conn: sqlite3.Connection) -> None:
    """Create tag_market_stats table + index if absent.

    Per Phase 3 plan § 0.2. Schema landed by Workstream A; Workstream D
    populates it from ``sniper.analysis.tag_analytics.compute_tag_market_stats``.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tag_market_stats (
            tag                       TEXT    NOT NULL PRIMARY KEY,
            n_markets                 INTEGER NOT NULL DEFAULT 0,
            n_resolved                INTEGER NOT NULL DEFAULT 0,
            resolved_yes_count        INTEGER NOT NULL DEFAULT 0,
            yes_share                 REAL,
            avg_resolved_price_yes    REAL,
            median_market_volume_24h  REAL,
            top_co_tags_json          TEXT    NOT NULL DEFAULT '[]',
            computed_at               INTEGER NOT NULL,
            inputs_version            TEXT    NOT NULL DEFAULT 'v1'
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tms_computed_at
            ON tag_market_stats(computed_at DESC)
        """
    )
    conn.commit()


def ensure_position_market_state(conn: sqlite3.Connection) -> None:
    """Create position_market_state table + indexes if absent.

    Per Phase 3 plan § A7 and Workstream B trace
    (docs/research/scopes/2026-04-29-position-signals-column-validation.md).

    Splits the legacy ``position_signals`` table: keeps the 44 raw + derived
    market columns; drops score-coupled fields (position_insider_score,
    position_speculator_score, position_classification, signal_reason) which
    were analyzer output, paper-feedback fields (paper_trade_*) which belong
    with paper trading, and legacy ``updated_at`` (new table owns its own
    timestamp). Replaces the legacy ``analysis_version`` with
    ``analyzer_run_id`` per Codex's Workstream B summary, joining to
    ``analyzer_runs.run_id``.

    PK = (wallet, condition_id, outcome) — overwrite-on-upsert preserves
    legacy single-live-row semantics. Phase 3 stays minimal; history-preserving
    semantics (per-run rows) are deferred.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS position_market_state (
            wallet                    TEXT    NOT NULL,
            condition_id              TEXT    NOT NULL,
            outcome                   TEXT    NOT NULL,

            analyzer_run_id           TEXT    NOT NULL,

            title                     TEXT,
            event_slug                TEXT,
            end_date_iso              TEXT,

            is_open                   INTEGER NOT NULL DEFAULT 1,
            position_value            REAL,
            position_share            REAL,
            position_count_wallet     INTEGER,
            current_price             REAL,
            current_size              REAL,

            entry_anchor_ts           INTEGER,
            entry_price               REAL,
            entry_size                REAL,
            entry_notional            REAL,

            buys_5m                   INTEGER NOT NULL DEFAULT 0,
            buys_1h                   INTEGER NOT NULL DEFAULT 0,
            buys_24h                  INTEGER NOT NULL DEFAULT 0,
            trades_5m                 INTEGER NOT NULL DEFAULT 0,
            trades_1h                 INTEGER NOT NULL DEFAULT 0,
            trades_24h                INTEGER NOT NULL DEFAULT 0,
            sells_5m                  INTEGER NOT NULL DEFAULT 0,
            sells_1h                  INTEGER NOT NULL DEFAULT 0,
            sells_24h                 INTEGER NOT NULL DEFAULT 0,

            notional_5m               REAL    NOT NULL DEFAULT 0,
            notional_1h               REAL    NOT NULL DEFAULT 0,
            notional_24h              REAL    NOT NULL DEFAULT 0,
            sell_notional_5m          REAL    NOT NULL DEFAULT 0,
            sell_notional_1h          REAL    NOT NULL DEFAULT 0,
            sell_notional_24h         REAL    NOT NULL DEFAULT 0,

            buys_since_entry          INTEGER NOT NULL DEFAULT 0,
            sells_since_entry         INTEGER NOT NULL DEFAULT 0,
            buy_notional_since_entry  REAL    NOT NULL DEFAULT 0,
            sell_notional_since_entry REAL    NOT NULL DEFAULT 0,

            net_notional_24h          REAL    NOT NULL DEFAULT 0,
            net_notional_since_entry  REAL    NOT NULL DEFAULT 0,

            last_trade_side           TEXT,
            activity_state            TEXT,
            hold_hours                REAL,
            unique_sizes_5m           INTEGER,
            max_trades_60s            INTEGER,
            latest_trade_ts           INTEGER,

            analyzed_ts               INTEGER NOT NULL,

            created_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

            PRIMARY KEY (wallet, condition_id, outcome),
            FOREIGN KEY (analyzer_run_id) REFERENCES analyzer_runs(run_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pms_run
            ON position_market_state(analyzer_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pms_wallet_open
            ON position_market_state(wallet, is_open, analyzed_ts DESC)
        """
    )
    conn.commit()


def ensure_wallet_analysis_batch(conn: sqlite3.Connection) -> None:
    """Create progressive wallet-analysis batch tables + indexes if absent."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_analysis_batch (
            batch_id        TEXT    PRIMARY KEY,
            source          TEXT    NOT NULL DEFAULT '',
            status          TEXT    NOT NULL DEFAULT 'running'
                            CHECK(status IN (
                                'queued', 'running', 'completed',
                                'failed', 'skipped'
                            )),
            total_wallets   INTEGER NOT NULL DEFAULT 0,
            created_ts      INTEGER NOT NULL,
            started_ts      INTEGER,
            finished_ts     INTEGER,
            created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_analysis_batch_item (
            batch_id           TEXT    NOT NULL,
            wallet             TEXT    NOT NULL,
            status             TEXT    NOT NULL DEFAULT 'queued'
                               CHECK(status IN (
                                   'queued', 'running', 'completed',
                                   'failed', 'skipped'
                               )),

            started_ts         INTEGER,
            heartbeat_ts       INTEGER,
            finished_ts        INTEGER,
            duration_ms        INTEGER,

            analyzer_run_id    TEXT,

            action_bucket      TEXT
                               CHECK(action_bucket IS NULL OR action_bucket IN (
                                   'actionable_now', 'review',
                                   'low_confidence', 'no_action', 'failed'
                               )),
            action_decision    TEXT,
            confidence_band    TEXT,
            overall_verdict    TEXT,
            primary_edge_type  TEXT,

            summary_markdown   TEXT,
            summary_json       TEXT,

            error_class        TEXT,
            error_message      TEXT,
            attempt_count      INTEGER NOT NULL DEFAULT 0,

            source             TEXT    NOT NULL DEFAULT 'manual',
            source_ref         TEXT    NOT NULL DEFAULT '',
            metadata_json      TEXT    NOT NULL DEFAULT '{}',
            intake_queue_id    INTEGER,

            created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,

            PRIMARY KEY (batch_id, wallet),
            FOREIGN KEY (batch_id) REFERENCES wallet_analysis_batch(batch_id),
            FOREIGN KEY (analyzer_run_id) REFERENCES analyzer_runs(run_id)
        )
        """
    )
    _ensure_column(
        conn,
        "wallet_analysis_batch_item",
        "source",
        "TEXT NOT NULL DEFAULT 'manual'",
    )
    _ensure_column(
        conn,
        "wallet_analysis_batch_item",
        "source_ref",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        conn,
        "wallet_analysis_batch_item",
        "metadata_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        conn,
        "wallet_analysis_batch_item",
        "intake_queue_id",
        "INTEGER",
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wabi_batch_status
            ON wallet_analysis_batch_item(batch_id, status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wabi_bucket
            ON wallet_analysis_batch_item(action_bucket, finished_ts DESC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_wabi_wallet
            ON wallet_analysis_batch_item(wallet, finished_ts DESC)
        """
    )
    conn.commit()


def ensure_wallet_analyzer_intake_queue(conn: sqlite3.Connection) -> None:
    """Create analyzer-owned intake queue, isolated from legacy discovery.

    This is intentionally separate from ``wallet_analysis_queue``, which
    belongs to the old low-buy / discovery-events / analyze_users pipeline.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wallet_analyzer_intake_queue (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet           TEXT    NOT NULL,
            source           TEXT    NOT NULL DEFAULT 'manual'
                             CHECK(source IN (
                                 'manual', 'discovery_agent',
                                 'import_file', 'api', 'other'
                             )),
            source_ref       TEXT    NOT NULL DEFAULT '',
            status           TEXT    NOT NULL DEFAULT 'pending'
                             CHECK(status IN (
                                 'pending', 'claimed', 'running',
                                 'completed', 'failed', 'skipped',
                                 'duplicate'
                             )),
            priority         INTEGER NOT NULL DEFAULT 0,
            created_ts       INTEGER NOT NULL,
            claimed_ts       INTEGER,
            started_ts       INTEGER,
            finished_ts      INTEGER,
            last_attempt_ts  INTEGER,
            attempt_count    INTEGER NOT NULL DEFAULT 0,
            metadata_json    TEXT    NOT NULL DEFAULT '{}',
            notes            TEXT    NOT NULL DEFAULT '',
            error_class      TEXT,
            error_message    TEXT,
            failure_category TEXT,
            created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    _ensure_column(
        conn,
        "wallet_analyzer_intake_queue",
        "failure_category",
        "TEXT",
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_waiq_active_wallet
            ON wallet_analyzer_intake_queue(wallet)
            WHERE status IN ('pending', 'claimed', 'running')
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_waiq_status_priority
            ON wallet_analyzer_intake_queue(status, priority DESC, created_ts ASC)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_waiq_wallet_status
            ON wallet_analyzer_intake_queue(wallet, status)
        """
    )
    conn.commit()


__all__ = [
    "ensure_wallet_layer5_decision",
    "ensure_position_signal_decision",
    "ensure_analyzer_runs",
    "ensure_tag_market_stats",
    "ensure_position_market_state",
    "ensure_wallet_analysis_batch",
    "ensure_wallet_analyzer_intake_queue",
]
