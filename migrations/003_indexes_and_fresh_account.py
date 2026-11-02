#!/usr/bin/env python3
"""
Migration 003 — Missing indexes + fresh account schema additions.

Adds:
  1. idx_low_buy_condition        — low_buy_trades(condition_id, timestamp DESC)
  2. idx_position_signals_market  — position_signals(condition_id, outcome, wallet)
  3. idx_order_intents_position   — order_intents(position_id, state)
  4. idx_strategy_positions_opened— strategy_positions(opened_ts DESC, state)
  5. idx_alerts_outbox_wallet_status — alerts_outbox(wallet, status, last_seen_ts DESC)
  6. idx_wap_first_activity       — wallet_profiles(first_activity_ts DESC)
  7. Adds `market_end_date_ts` to discovery_events if missing (for resolution<5d scoring)

Safe to run multiple times (CREATE INDEX IF NOT EXISTS).
"""

import sqlite3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "sniper_tracker.db")


INDEXES = [
    (
        "idx_low_buy_condition",
        "low_buy_trades",
        "CREATE INDEX IF NOT EXISTS idx_low_buy_condition "
        "ON low_buy_trades(condition_id, timestamp DESC);",
    ),
    (
        "idx_position_signals_market",
        "position_signals",
        "CREATE INDEX IF NOT EXISTS idx_position_signals_market "
        "ON position_signals(condition_id, outcome, wallet);",
    ),
    (
        "idx_order_intents_position",
        "order_intents",
        "CREATE INDEX IF NOT EXISTS idx_order_intents_position "
        "ON order_intents(position_id, state);",
    ),
    (
        "idx_strategy_positions_opened",
        "strategy_positions",
        "CREATE INDEX IF NOT EXISTS idx_strategy_positions_opened "
        "ON strategy_positions(opened_ts DESC, state);",
    ),
    (
        "idx_alerts_outbox_wallet_status",
        "alerts_outbox",
        "CREATE INDEX IF NOT EXISTS idx_alerts_outbox_wallet_status "
        "ON alerts_outbox(wallet, status, last_seen_ts DESC);",
    ),
    (
        "idx_wallet_profiles_first_activity",
        "wallet_profiles",
        "CREATE INDEX IF NOT EXISTS idx_wallet_profiles_first_activity "
        "ON wallet_profiles(first_activity_ts DESC);",
    ),
    (
        "idx_low_buy_wallet_ts",
        "low_buy_trades",
        # Composite for fresh account wallet-age lookup: proxy_wallet + min timestamp
        "CREATE INDEX IF NOT EXISTS idx_low_buy_wallet_ts "
        "ON low_buy_trades(proxy_wallet, timestamp ASC);",
    ),
    (
        "idx_candidate_users_first_seen",
        "candidate_users",
        "CREATE INDEX IF NOT EXISTS idx_candidate_users_first_seen "
        "ON candidate_users(first_seen_ts ASC);",
    ),
]


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def run(db_path: str = DB_PATH) -> None:
    log.info("Opening DB: %s", db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")

    # 1. Add indexes
    for name, table, sql in INDEXES:
        if not table_exists(conn, table):
            log.warning("Table %s does not exist — skipping index %s", table, name)
            continue
        log.info("Creating index %s on %s ...", name, table)
        conn.execute(sql)
        log.info("  ✓ %s", name)

    conn.commit()

    # 2. Add market_end_date_ts column to discovery_events if missing
    # Used by fresh account scoring to prefer markets resolving within 5 days
    if table_exists(conn, "discovery_events"):
        if not column_exists(conn, "discovery_events", "market_end_date_ts"):
            log.info("Adding market_end_date_ts to discovery_events ...")
            conn.execute(
                "ALTER TABLE discovery_events ADD COLUMN market_end_date_ts INTEGER NOT NULL DEFAULT 0;"
            )
            conn.commit()
            log.info("  ✓ market_end_date_ts added")
        else:
            log.info("  ✓ market_end_date_ts already exists")

    # 3. Add first_activity_ts to candidate_users if missing (needed for fast fresh-account checks)
    if table_exists(conn, "candidate_users"):
        if not column_exists(conn, "candidate_users", "wallet_age_days"):
            log.info("Adding wallet_age_days to candidate_users ...")
            conn.execute(
                "ALTER TABLE candidate_users ADD COLUMN wallet_age_days REAL NOT NULL DEFAULT -1;"
            )
            conn.commit()
            log.info("  ✓ wallet_age_days added to candidate_users")
        else:
            log.info("  ✓ wallet_age_days already exists in candidate_users")

    conn.close()
    log.info("Migration 003 complete.")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DB_PATH
    run(path)
