"""DuckDB schema for the stink-bid backtest snapshot.

apply_schema() is idempotent — safe to call on an existing DB. Creates
tables + indexes if missing, records schema_version. Future-proof for
forward migrations via SCHEMA_VERSION bump.

Trades table uses a surrogate `row_hash` primary key (sha256 of
transaction_hash|asset|price|size|side|outcome) because a single
on-chain tx can map to multiple fills (verified empirically: 100 trades
→ 47 unique transactionHashes on Polymarket /trades).
"""
from __future__ import annotations

import os
from typing import Optional

import duckdb

SCHEMA_VERSION = 1

DDL = [
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        git_commit VARCHAR
    )""",

    """CREATE TABLE IF NOT EXISTS gamma_events (
        condition_id VARCHAR PRIMARY KEY,
        event_id VARCHAR,
        title VARCHAR,
        event_slug VARCHAR,
        tour_prefix VARCHAR,
        token_id_yes VARCHAR NOT NULL,
        token_id_no VARCHAR,
        start_date TIMESTAMP,
        end_date TIMESTAMP,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_gamma_events_enddate ON gamma_events(end_date)",
    "CREATE INDEX IF NOT EXISTS idx_gamma_events_tour ON gamma_events(tour_prefix)",

    """CREATE TABLE IF NOT EXISTS prices_history (
        token_id VARCHAR NOT NULL,
        timestamp_sec BIGINT NOT NULL,
        price DOUBLE NOT NULL,
        fidelity INTEGER NOT NULL,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (token_id, timestamp_sec, fidelity)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prices_token_ts ON prices_history(token_id, timestamp_sec)",

    """CREATE TABLE IF NOT EXISTS trades (
        row_hash VARCHAR PRIMARY KEY,
        condition_id VARCHAR NOT NULL,
        transaction_hash VARCHAR,
        asset VARCHAR,
        timestamp_sec BIGINT NOT NULL,
        outcome VARCHAR,
        price DOUBLE,
        size DOUBLE,
        side VARCHAR,
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trades_cid_ts ON trades(condition_id, timestamp_sec)",
    "CREATE INDEX IF NOT EXISTS idx_trades_cid_asset_ts ON trades(condition_id, asset, timestamp_sec)",
    "CREATE INDEX IF NOT EXISTS idx_trades_txhash ON trades(transaction_hash)",

    """CREATE TABLE IF NOT EXISTS trade_coverage (
        condition_id VARCHAR PRIMARY KEY,
        start_ts BIGINT NOT NULL,
        end_ts BIGINT NOT NULL,
        page_size INTEGER NOT NULL,
        max_pages INTEGER NOT NULL,
        pages_fetched INTEGER NOT NULL,
        rows_fetched INTEGER NOT NULL,
        rows_inserted INTEGER NOT NULL,
        hit_page_cap BOOLEAN NOT NULL,
        first_trade_ts BIGINT,
        last_trade_ts BIGINT,
        fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        error VARCHAR
    )""",
    "CREATE INDEX IF NOT EXISTS idx_trade_coverage_hit_cap ON trade_coverage(hit_page_cap)",

    """CREATE TABLE IF NOT EXISTS sackmann_matches (
        tour VARCHAR NOT NULL,
        tourney_date VARCHAR NOT NULL,
        winner_name VARCHAR NOT NULL,
        loser_name VARCHAR NOT NULL,
        tourney_name VARCHAR,
        surface VARCHAR,
        tourney_level VARCHAR,
        minutes INTEGER,
        score VARCHAR,
        source VARCHAR DEFAULT 'sackmann',
        pulled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (tour, tourney_date, winner_name, loser_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sack_date ON sackmann_matches(tourney_date)",

    """CREATE TABLE IF NOT EXISTS event_alignment (
        condition_id VARCHAR PRIMARY KEY,
        sack_tour VARCHAR,
        sack_date VARCHAR,
        sack_winner VARCHAR,
        sack_loser VARCHAR,
        date_delta_days INTEGER,
        confidence DOUBLE,
        aligned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""",
]


def apply_schema(con: duckdb.DuckDBPyConnection, git_commit: Optional[str] = None) -> None:
    for stmt in DDL:
        con.execute(stmt)
    con.execute(
        "INSERT OR REPLACE INTO schema_version (version, git_commit) VALUES (?, ?)",
        [SCHEMA_VERSION, git_commit],
    )


def open_backtest_db(
    path: str = "data/backtest/stink_bid/backtest.db",
    git_commit: Optional[str] = None,
) -> duckdb.DuckDBPyConnection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = duckdb.connect(path)
    apply_schema(con, git_commit=git_commit)
    return con
