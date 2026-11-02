"""
Retrospective analysis, replay configs/runs, and history capture.
"""
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sniper.persistence.db_utils import _current_instance_tag

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Analyzer helpers — paper trades, signal outcomes, FWA trades
# ---------------------------------------------------------------------------


def analyzer_paper_trades(
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """Return closed paper trades with entry-time scores for retrospective
    analysis.  These are the cleanest data: ``signal_insider_score`` and
    ``signal_speculator_score`` are frozen at trade entry time.

    Each row dict contains the columns needed by the analyzer to apply
    configurable gate filters and compute P&L.
    """
    rows = conn.execute(
        """
        SELECT
            pt.trade_id,
            pt.wallet,
            pt.condition_id,
            pt.outcome,
            pt.market_title,
            pt.entry_price,
            pt.exit_price,
            pt.size_usdc,
            pt.pnl_usdc,
            pt.roi_percent,
            pt.status,
            pt.signal_insider_score,
            pt.signal_speculator_score,
            pt.wallet_win_rate,
            pt.signal_type,
            pt.opened_at,
            pt.closed_at
        FROM paper_trades pt
        WHERE pt.status IN ('resolution_win', 'resolution_loss', 'early_exit', 'manual')
          AND pt.signal_insider_score IS NOT NULL
        ORDER BY pt.opened_at
        """
    ).fetchall()
    return [dict(r) for r in rows]


def analyzer_signal_outcomes(
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """Return position signals joined with their resolved market outcomes.

    **CAVEAT**: ``position_insider_score`` and ``position_speculator_score``
    reflect the LATEST analysis run, NOT the values at signal creation time.
    Results from this source must be labeled with a look-ahead bias warning.
    """
    rows = conn.execute(
        """
        SELECT
            ps.wallet,
            ps.condition_id,
            ps.outcome,
            ps.title,
            ps.entry_price,
            ps.entry_notional,
            ps.position_insider_score,
            ps.position_speculator_score,
            ps.position_classification,
            ps.analyzed_ts,
            ps.entry_anchor_ts,
            mo.resolved_price,
            mo.resolved_at
        FROM position_signals ps
        INNER JOIN market_outcomes mo
            ON ps.condition_id = mo.condition_id
            AND LOWER(ps.outcome) = LOWER(mo.outcome)
        WHERE ps.entry_price IS NOT NULL
          AND ps.entry_price > 0
        ORDER BY ps.entry_anchor_ts
        """
    ).fetchall()
    return [dict(r) for r in rows]


def analyzer_fwa_trades(
    conn: sqlite3.Connection,
) -> List[Dict[str, Any]]:
    """Return followed-wallet BUY trades joined with resolved outcomes and
    the wallet's CURRENT user_analysis scores.

    **CAVEATS**:
    - ``insider_score`` / ``speculator_score`` are the wallet's LATEST scores,
      not the scores at the time of the trade.
    - Only a small fraction of FWA markets have resolved (check row count).
    """
    rows = conn.execute(
        """
        SELECT
            fwa.wallet,
            fwa.condition_id,
            fwa.outcome,
            fwa.title,
            fwa.price   AS entry_price,
            fwa.size,
            fwa.usdc_size,
            fwa.activity_ts,
            mo.resolved_price,
            mo.resolved_at,
            ua.insider_score,
            ua.speculator_score,
            ua.classification,
            ua.win_rate,
            ua.qualifying_roi
        FROM followed_wallet_activity fwa
        INNER JOIN market_outcomes mo
            ON fwa.condition_id = mo.condition_id
            AND LOWER(fwa.outcome) = LOWER(mo.outcome)
        LEFT JOIN user_analysis ua
            ON fwa.wallet = ua.wallet
        WHERE fwa.side = 'BUY'
          AND fwa.price IS NOT NULL
          AND fwa.price > 0
        ORDER BY fwa.activity_ts
        """
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Phase 1.5: Immutable History Capture
# ---------------------------------------------------------------------------


def closed_signals_for_wallet(
    conn: sqlite3.Connection,
    wallet: str,
    analyzed_ts: int = 0,
) -> list:
    """Return closed signals that lack a terminal snapshot in signal_history.

    Finds ``position_signals`` rows with ``is_open = 0`` for this wallet
    where no ``signal_history`` row with ``is_open = 0`` exists yet.
    This is idempotent and does not depend on ``analyzed_ts`` matching,
    so it catches closures from any code path (mark_open_state, auto-close,
    price threshold, etc.).
    """
    rows = conn.execute(
        """SELECT ps.wallet, ps.condition_id, ps.outcome, ps.title, ps.is_open,
                  ps.entry_price, ps.entry_notional, ps.current_price,
                  ps.position_insider_score, ps.position_speculator_score,
                  ps.position_classification, ps.activity_state, ps.hold_hours,
                  ps.entry_anchor_ts, ps.analyzed_ts, ps.analysis_version
           FROM position_signals ps
           WHERE ps.wallet = ? AND ps.is_open = 0
             AND NOT EXISTS (
                 SELECT 1 FROM signal_history sh
                 WHERE sh.wallet = ps.wallet
                   AND sh.condition_id = ps.condition_id
                   AND sh.outcome = ps.outcome
                   AND sh.is_open = 0
             )""",
        (wallet,),
    ).fetchall()
    return [dict(r) for r in rows]


def final_score_for_wallet(
    conn: sqlite3.Connection,
    wallet: str,
) -> dict | None:
    """Read the final user_analysis row for a wallet after all reclassification.

    Returns a dict with the gate-relevant fields, or None if not found.
    """
    row = conn.execute(
        """SELECT wallet, insider_score, speculator_score, classification, tier,
                  strategy_group, win_rate, closed_sample_size, qualifying_roi,
                  qualifying_capital, pnl, position_count, wallet_edge_tier,
                  followability_cached, is_bot, analyzed_ts, analysis_version
           FROM user_analysis WHERE wallet = ?""",
        (wallet,),
    ).fetchone()
    return dict(row) if row else None


def insert_signal_history(
    conn: sqlite3.Connection,
    *,
    signals: list,
    analyzed_ts: int,
    analysis_version: int = 0,
) -> int:
    """Append position-signal snapshots to signal_history (append-only).

    Parameters
    ----------
    signals : list[dict]
        Each dict should contain the kwargs that were passed to
        ``upsert_position_signal()``.  We extract the gate-relevant subset.
    analyzed_ts : int
        Unix timestamp of this analysis run.
    analysis_version : int
        Monotonic version counter.

    Returns
    -------
    int
        Number of rows inserted.
    """
    if not signals:
        return 0
    rows = []
    for s in signals:
        rows.append((
            s.get("wallet", ""),
            s.get("condition_id", ""),
            s.get("outcome", ""),
            s.get("title", ""),
            1 if s.get("is_open", True) else 0,
            float(s.get("entry_price", 0) or 0),
            float(s.get("entry_notional", 0) or 0),
            float(s.get("current_price", 0) or 0),
            float(s.get("position_insider_score", 0) or 0),
            float(s.get("position_speculator_score", 0) or 0),
            s.get("position_classification", ""),
            s.get("activity_state", "holding"),
            float(s.get("hold_hours", 0) or 0),
            int(s.get("entry_anchor_ts", 0) or 0),
            analyzed_ts,
            analysis_version,
        ))
    conn.executemany(
        """INSERT INTO signal_history
           (wallet, condition_id, outcome, title, is_open, entry_price,
            entry_notional, current_price, position_insider_score,
            position_speculator_score, position_classification,
            activity_state, hold_hours, entry_anchor_ts, analyzed_ts,
            analysis_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    return len(rows)


def insert_score_history(
    conn: sqlite3.Connection,
    *,
    wallet: str,
    insider_score: float = 0,
    speculator_score: float = 0,
    classification: str = "",
    tier: str = "",
    strategy_group: str = "",
    win_rate: float = 0,
    closed_sample_size: int = 0,
    qualifying_roi: float | None = None,
    qualifying_capital: float = 0,
    pnl: float = 0,
    position_count: int = 0,
    wallet_edge_tier: str = "",
    followability_cached: float | None = None,
    is_bot: bool = False,
    analyzed_ts: int = 0,
    analysis_version: int = 0,
) -> None:
    """Append a wallet-level score snapshot to score_history (append-only).

    Captures exactly the fields that entry gates depend on, so gate logic
    can be replayed against any historical point.
    """
    conn.execute(
        """INSERT INTO score_history
           (wallet, insider_score, speculator_score, classification, tier,
            strategy_group, win_rate, closed_sample_size, qualifying_roi,
            qualifying_capital, pnl, position_count, wallet_edge_tier,
            followability_cached, is_bot, analyzed_ts, analysis_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            wallet,
            float(insider_score or 0),
            float(speculator_score or 0),
            classification,
            tier,
            strategy_group,
            float(win_rate or 0),
            int(closed_sample_size or 0),
            qualifying_roi,
            float(qualifying_capital or 0),
            float(pnl or 0),
            int(position_count or 0),
            wallet_edge_tier,
            followability_cached,
            1 if is_bot else 0,
            int(analyzed_ts or 0),
            int(analysis_version or 0),
        ),
    )


# ── Replay Engine: curated signal feed ──────────────────────────────────


def replay_curated_signals(
    conn: sqlite3.Connection,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[dict]:
    """Return deduplicated, chronologically ordered signal snapshots for replay.

    Each unique (wallet, condition_id, outcome, entry_anchor_ts) tuple produces
    exactly one row — the *first* snapshot (earliest ``analyzed_ts``) wins,
    preserving the scores the analyzer assigned at initial detection.

    Filters
    -------
    - ``entry_anchor_ts > 0`` — excludes legacy rows without an anchor.
    - ``entry_price > 0`` — excludes zero-price fills.
    - Optional ``start_ts`` / ``end_ts`` bounds on ``entry_anchor_ts``.

    Resolution
    ----------
    LEFT JOINs ``market_outcomes`` so that resolved markets carry
    ``resolved_price`` (1.0 or 0.0); unresolved markets return ``None``.
    The join uses ``LOWER(fs.outcome)`` to match the lowercase convention in
    ``market_outcomes``.

    Returns
    -------
    list[dict]
        Rows ordered by ``entry_anchor_ts ASC``.
    """
    where_parts = ["sh.entry_anchor_ts > 0", "sh.entry_price > 0"]
    params: list = []

    if start_ts is not None:
        where_parts.append("sh.entry_anchor_ts >= ?")
        params.append(int(start_ts))
    if end_ts is not None:
        where_parts.append("sh.entry_anchor_ts <= ?")
        params.append(int(end_ts))

    where_clause = " AND ".join(where_parts)

    sql = f"""
        WITH first_seen AS (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY wallet, condition_id, LOWER(outcome), entry_anchor_ts
                       ORDER BY analyzed_ts ASC
                   ) AS rn
            FROM signal_history sh
            WHERE {where_clause}
        )
        SELECT fs.wallet,
               fs.condition_id,
               fs.outcome,
               fs.title,
               fs.is_open,
               fs.entry_price,
               fs.entry_notional,
               fs.position_insider_score,
               fs.position_speculator_score,
               fs.position_classification,
               fs.entry_anchor_ts,
               mo.resolved_price,
               mo.resolved_at
        FROM first_seen fs
        LEFT JOIN market_outcomes mo
            ON mo.condition_id = fs.condition_id
           AND mo.outcome = LOWER(fs.outcome)
        WHERE fs.rn = 1
        ORDER BY fs.entry_anchor_ts ASC
    """

    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Replay config & run persistence ───────────────────────────────


def save_replay_config(conn, *, name: str, config_json: dict, notes: str = "") -> int:
    """Save a named replay configuration. Returns config ID."""
    ensure_replay_tables(conn)
    instance_tag = _current_instance_tag()
    cur = conn.execute(
        "INSERT INTO replay_configs (name, config_json, notes, instance_tag) VALUES (?, ?, ?, ?)",
        (name, json.dumps(config_json), notes, instance_tag),
    )
    conn.commit()
    return cur.lastrowid


def get_replay_config(conn, config_id: int) -> dict | None:
    """Get a replay config by ID."""
    ensure_replay_tables(conn)
    row = conn.execute(
        "SELECT * FROM replay_configs WHERE id = ?", (config_id,)
    ).fetchone()
    return dict(row) if row else None


def ensure_replay_tables(conn: sqlite3.Connection) -> None:
    """Ensure replay tables exist on legacy or partially bootstrapped DBs."""
    # Lazy-import to avoid circular dependency (with_db_init_lock and
    # ensure_instance_tags live in db.py which re-exports from this module).
    from db import with_db_init_lock, ensure_instance_tags

    with with_db_init_lock(conn):
        conn.executescript(
            """
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
            """
        )
        # Inline _ensure_column calls to avoid circular import
        try:
            conn.execute("ALTER TABLE replay_configs ADD COLUMN instance_tag TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE replay_runs ADD COLUMN instance_tag TEXT")
        except Exception:
            pass
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replay_configs_instance_tag "
            "ON replay_configs(instance_tag, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_replay_runs_instance_tag "
            "ON replay_runs(instance_tag, created_at DESC)"
        )
        ensure_instance_tags(conn)
        conn.commit()


def list_replay_configs(conn) -> list[dict]:
    """List all saved replay configs, newest first."""
    ensure_replay_tables(conn)
    rows = conn.execute(
        "SELECT id, name, config_json, notes, created_at, updated_at, instance_tag FROM replay_configs ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def save_replay_run(
    conn,
    *,
    config_id: int | None,
    config_json: dict,
    summary_json: dict,
    result_json: dict,
    signal_count: int,
    trade_count: int,
    duration_ms: int,
) -> int:
    """Save a replay run result. Returns run ID."""
    ensure_replay_tables(conn)
    instance_tag = _current_instance_tag()
    cur = conn.execute(
        """INSERT INTO replay_runs
           (config_id, config_json, summary_json, result_json,
            signal_count, trade_count, duration_ms, instance_tag)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (config_id, json.dumps(config_json), json.dumps(summary_json),
         json.dumps(result_json), signal_count, trade_count, duration_ms, instance_tag),
    )
    conn.commit()
    return cur.lastrowid


def get_replay_run(conn, run_id: int) -> dict | None:
    """Get a replay run by ID."""
    ensure_replay_tables(conn)
    row = conn.execute(
        "SELECT * FROM replay_runs WHERE id = ?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def list_replay_runs(conn, *, config_id: int | None = None, limit: int = 50) -> list[dict]:
    """List replay runs, newest first. Optionally filter by config_id."""
    ensure_replay_tables(conn)
    if config_id is not None:
        rows = conn.execute(
            """SELECT id, config_id, summary_json, signal_count, trade_count,
                      duration_ms, created_at, instance_tag
               FROM replay_runs WHERE config_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (config_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, config_id, summary_json, signal_count, trade_count,
                      duration_ms, created_at, instance_tag
               FROM replay_runs
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
