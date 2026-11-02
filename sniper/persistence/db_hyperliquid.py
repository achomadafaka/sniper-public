"""
db_hyperliquid.py — Table creation and CRUD for Hyperliquid-related tables.

Tables:
  - hl_trades: trade-level data from HL WebSocket
  - hl_liquidations: enriched liquidation events
  - hl_positions: strategy positions for leveraged perps
  - hl_fills: individual fill/funding events per position
  - signal_queue: unified cross-venue signal instrumentation
  - copy_viability: per-wallet copy scoring

All ensure_* functions are idempotent (CREATE IF NOT EXISTS).
"""

from __future__ import annotations

import sqlite3
from typing import Optional


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

def ensure_hl_trades_table(conn: sqlite3.Connection) -> None:
    """Trade-level data from Hyperliquid WebSocket."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_trades (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            asset          TEXT NOT NULL,
            side           TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
            size           REAL NOT NULL,
            price          REAL NOT NULL,
            time_ms        INTEGER NOT NULL,
            hash           TEXT NOT NULL DEFAULT '',
            user_addr      TEXT NOT NULL DEFAULT '',
            is_liquidation INTEGER NOT NULL DEFAULT 0,
            captured_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(asset, time_ms, hash)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_trades_asset_time
        ON hl_trades(asset, time_ms DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_trades_user
        ON hl_trades(user_addr, time_ms DESC)
    """)
    conn.commit()


def ensure_hl_liquidations_table(conn: sqlite3.Connection) -> None:
    """Enriched liquidation events (subset of trades).

    Note: ``side`` uses position direction ('long'/'short'), NOT trade direction
    ('buy'/'sell') like ``hl_trades``. A long liquidation means a long position
    was force-closed (i.e. a sell), and vice versa.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_liquidations (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            asset          TEXT NOT NULL,
            side           TEXT NOT NULL CHECK (side IN ('long', 'short')),
            size           REAL NOT NULL,
            price          REAL NOT NULL,
            liq_value_usd  REAL NOT NULL DEFAULT 0,
            time_ms        INTEGER NOT NULL,
            user_addr      TEXT NOT NULL DEFAULT '',
            captured_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_liq_asset_time
        ON hl_liquidations(asset, time_ms DESC)
    """)
    conn.commit()


def ensure_hl_positions_table(conn: sqlite3.Connection) -> None:
    """Strategy positions for Hyperliquid leveraged perps."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_positions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id         INTEGER NOT NULL,
            asset               TEXT NOT NULL,
            side                TEXT NOT NULL CHECK (side IN ('long', 'short')),
            entry_price         REAL NOT NULL,
            avg_entry_price     REAL NOT NULL DEFAULT 0,
            current_price       REAL NOT NULL DEFAULT 0,
            size                REAL NOT NULL,
            leverage            REAL NOT NULL DEFAULT 1.0,
            margin_usdc         REAL NOT NULL DEFAULT 0,
            liquidation_price   REAL NOT NULL DEFAULT 0,
            funding_accumulated REAL NOT NULL DEFAULT 0,
            unrealized_pnl      REAL NOT NULL DEFAULT 0,
            realized_pnl        REAL NOT NULL DEFAULT 0,
            state               TEXT NOT NULL DEFAULT 'open'
                                CHECK (state IN ('open', 'closing', 'closed')),
            opened_ts           INTEGER NOT NULL,
            closed_ts           INTEGER NOT NULL DEFAULT 0,
            updated_ts          INTEGER NOT NULL,
            instance_tag        TEXT,
            close_reason        TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_pos_strategy
        ON hl_positions(strategy_id, state)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_pos_state
        ON hl_positions(state, updated_ts DESC)
    """)
    conn.commit()


def ensure_hl_fills_table(conn: sqlite3.Connection) -> None:
    """Individual fill/funding events per HL position."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_fills (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER NOT NULL,
            fill_type   TEXT NOT NULL CHECK (fill_type IN ('open', 'close', 'partial', 'funding')),
            asset       TEXT NOT NULL,
            side        TEXT NOT NULL,
            price       REAL NOT NULL DEFAULT 0,
            size        REAL NOT NULL DEFAULT 0,
            fee_usdc    REAL NOT NULL DEFAULT 0,
            time_ms     INTEGER NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_fills_position
        ON hl_fills(position_id, time_ms DESC)
    """)
    conn.commit()


def ensure_signal_queue_table(conn: sqlite3.Connection) -> None:
    """Unified cross-venue signal instrumentation."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_queue (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source         TEXT NOT NULL,
            venue          TEXT NOT NULL DEFAULT 'polymarket',
            asset_id       TEXT NOT NULL,
            direction      TEXT NOT NULL,
            confidence     REAL NOT NULL DEFAULT 0,
            signal_ts      INTEGER NOT NULL,
            entry_ts       INTEGER,
            fill_ts        INTEGER,
            latency_ms     INTEGER,
            status         TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'entered', 'rejected', 'expired')),
            reject_reason  TEXT NOT NULL DEFAULT '',
            metadata_json  TEXT NOT NULL DEFAULT '{}',
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_queue_status
        ON signal_queue(status, created_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_signal_queue_source
        ON signal_queue(source, venue, created_at DESC)
    """)
    conn.commit()


def ensure_copy_viability_table(conn: sqlite3.Connection) -> None:
    """Per-wallet copy viability scoring."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS copy_viability (
            wallet           TEXT PRIMARY KEY,
            avg_latency_ms   REAL NOT NULL DEFAULT 0,
            avg_slippage_pct REAL NOT NULL DEFAULT 0,
            fill_rate        REAL NOT NULL DEFAULT 0,
            signal_count     INTEGER NOT NULL DEFAULT 0,
            score            REAL NOT NULL DEFAULT 0,
            updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def ensure_hl_tracked_wallets_table(conn: sqlite3.Connection) -> None:
    """Whale addresses to track on Hyperliquid."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_tracked_wallets (
            wallet    TEXT PRIMARY KEY,
            label     TEXT NOT NULL DEFAULT '',
            source    TEXT NOT NULL DEFAULT 'manual',
            added_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def ensure_hl_whale_positions_table(conn: sqlite3.Connection) -> None:
    """Latest position snapshots for tracked whales."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hl_whale_positions (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet             TEXT NOT NULL,
            asset              TEXT NOT NULL,
            side               TEXT NOT NULL CHECK (side IN ('long', 'short')),
            size               REAL NOT NULL,
            entry_price        REAL NOT NULL,
            mark_price         REAL NOT NULL,
            liquidation_price  REAL,
            margin_type        TEXT NOT NULL CHECK (margin_type IN ('isolated', 'cross')),
            leverage           REAL NOT NULL DEFAULT 1.0,
            margin_used        REAL NOT NULL DEFAULT 0,
            unrealized_pnl     REAL NOT NULL DEFAULT 0,
            position_value_usd REAL NOT NULL DEFAULT 0,
            distance_pct       REAL NOT NULL DEFAULT 0,
            prev_distance_pct  REAL,
            account_value      REAL,
            cross_mm_used      REAL,
            captured_at        TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(wallet, asset)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_whale_pos_distance
        ON hl_whale_positions(distance_pct ASC)
    """)
    conn.commit()


def ensure_all_hl_tables(conn: sqlite3.Connection) -> None:
    """Create all Hyperliquid-related tables. Idempotent."""
    ensure_hl_trades_table(conn)
    ensure_hl_liquidations_table(conn)
    ensure_hl_positions_table(conn)
    ensure_hl_fills_table(conn)
    ensure_signal_queue_table(conn)
    ensure_copy_viability_table(conn)
    ensure_hl_tracked_wallets_table(conn)
    ensure_hl_whale_positions_table(conn)


# ---------------------------------------------------------------------------
# Insert helpers
# ---------------------------------------------------------------------------

def insert_hl_trade(
    conn: sqlite3.Connection,
    *,
    asset: str,
    side: str,
    size: float,
    price: float,
    time_ms: int,
    hash: str = "",
    user_addr: str = "",
    is_liquidation: int = 0,
) -> Optional[int]:
    """Insert a single HL trade. Returns rowid or None on duplicate."""
    try:
        cur = conn.execute(
            """INSERT INTO hl_trades
               (asset, side, size, price, time_ms, hash, user_addr, is_liquidation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (asset, side, size, price, time_ms, hash, user_addr, is_liquidation),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def insert_hl_liquidation(
    conn: sqlite3.Connection,
    *,
    asset: str,
    side: str,
    size: float,
    price: float,
    liq_value_usd: float,
    time_ms: int,
    user_addr: str = "",
) -> int:
    """Insert a liquidation event. Returns rowid."""
    cur = conn.execute(
        """INSERT INTO hl_liquidations
           (asset, side, size, price, liq_value_usd, time_ms, user_addr)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (asset, side, size, price, liq_value_usd, time_ms, user_addr),
    )
    conn.commit()
    return cur.lastrowid


def insert_signal_queue_entry(
    conn: sqlite3.Connection,
    *,
    source: str,
    venue: str,
    asset_id: str,
    direction: str,
    confidence: float,
    signal_ts: int,
    status: str = "pending",
    metadata_json: str = "{}",
) -> int:
    """Insert a signal queue entry. Returns rowid."""
    cur = conn.execute(
        """INSERT INTO signal_queue
           (source, venue, asset_id, direction, confidence, signal_ts, status, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, venue, asset_id, direction, confidence, signal_ts, status, metadata_json),
    )
    conn.commit()
    return cur.lastrowid


def update_signal_queue_status(
    conn: sqlite3.Connection,
    signal_id: int,
    *,
    status: str,
    entry_ts: Optional[int] = None,
    fill_ts: Optional[int] = None,
    latency_ms: Optional[int] = None,
    reject_reason: str = "",
) -> None:
    """Update a signal queue entry status and optional timing fields."""
    parts = ["status = ?"]
    params: list = [status]
    if entry_ts is not None:
        parts.append("entry_ts = ?")
        params.append(entry_ts)
    if fill_ts is not None:
        parts.append("fill_ts = ?")
        params.append(fill_ts)
    if latency_ms is not None:
        parts.append("latency_ms = ?")
        params.append(latency_ms)
    if reject_reason:
        parts.append("reject_reason = ?")
        params.append(reject_reason)
    params.append(signal_id)
    conn.execute(
        f"UPDATE signal_queue SET {', '.join(parts)} WHERE id = ?",
        params,
    )
    conn.commit()
