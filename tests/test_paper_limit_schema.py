"""Tests for paper_limit_orders / paper_limit_fills / paper_limit_events schema.

These tables underpin the stink-bid paper limit order system for tennis betting.
They are completely separate from the real-order tables (order_intents / order_fills).
"""

import sqlite3
import tempfile
import os

from db import init_db


def _init_test_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn, path


def test_paper_limit_orders_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_orders")
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_orders_unique_constraint():
    conn, path = _init_test_db()
    try:
        conn.execute("""
            INSERT INTO paper_limit_orders
            (strategy_id, condition_id, market_title, favorite_token_id,
             favorite_token_label, favorite_price_at_entry, bid_price,
             dip_pct, placed_at, updated_at)
            VALUES (1, 'cond_1', 'Test Match', 'tok_1', 'Player A',
                    0.70, 0.49, 0.30, '2026-04-06T12:00:00Z', '2026-04-06T12:00:00Z')
        """)
        conn.commit()
        try:
            conn.execute("""
                INSERT INTO paper_limit_orders
                (strategy_id, condition_id, market_title, favorite_token_id,
                 favorite_token_label, favorite_price_at_entry, bid_price,
                 dip_pct, placed_at, updated_at)
                VALUES (1, 'cond_1', 'Test Match 2', 'tok_2', 'Player B',
                        0.60, 0.42, 0.30, '2026-04-06T13:00:00Z', '2026-04-06T13:00:00Z')
            """)
            conn.commit()
            assert False, "Should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_fills_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_fills")
    finally:
        conn.close()
        os.unlink(path)


def test_paper_limit_events_table_exists():
    conn, path = _init_test_db()
    try:
        conn.execute("SELECT COUNT(*) FROM paper_limit_events")
    finally:
        conn.close()
        os.unlink(path)
