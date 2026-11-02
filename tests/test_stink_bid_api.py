"""Tests for stink bid API endpoint in api_strategies blueprint."""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from db import init_db
from paper_trader import PaperTrader
from sniper.routes.api_strategies import create_api_strategies_blueprint


def _get_conn(tmp_path):
    """Create a test DB with full schema and return the connection."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    pt = PaperTrader.__new__(PaperTrader)
    pt.conn = conn
    pt._init_tables()
    return conn


def _make_client(conn):
    """Create a Flask test client with the strategies blueprint."""
    app = Flask(__name__)
    bp = create_api_strategies_blueprint({
        "conn": conn,
        "normalize_wallet": lambda w: w,
        "wallet_valid": lambda w: True,
        "safe_int": lambda v, d=0: int(v) if v else d,
        "optional_float": lambda v: float(v) if v else None,
    })
    app.register_blueprint(bp)
    return app.test_client()


@pytest.fixture
def client(tmp_path):
    conn = _get_conn(tmp_path)
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (1, '0x0000000000000000000000000000000000000001',
                   'Stink Bid Tennis', 'stink_bid_v1', '{}', 'active')""",
    )
    conn.execute(
        """INSERT INTO paper_limit_orders
           (strategy_id, condition_id, market_title, event_slug,
            favorite_token_id, favorite_token_label, favorite_price_at_entry,
            bid_price, dip_pct, min_fill_size, state, tour, player1, player2,
            placed_at, updated_at)
           VALUES (1, 'cond_abc', 'Djokovic vs Nadal - Winner', 'atp-final',
                   'tok_abc', 'Djokovic', 0.85,
                   0.12, 0.30, 50, 'resting', 'atp', 'Djokovic', 'Nadal',
                   '2026-04-08T12:00:00Z', '2026-04-08T12:00:00Z')""",
    )
    conn.execute(
        """INSERT INTO paper_limit_orders
           (strategy_id, condition_id, market_title, event_slug,
            favorite_token_id, favorite_token_label, favorite_price_at_entry,
            bid_price, dip_pct, min_fill_size, state, tour, player1, player2,
            placed_at, updated_at, closed_at)
           VALUES (1, 'cond_def', 'Alcaraz vs Sinner - Winner', 'atp-open',
                   'tok_def', 'Alcaraz', 0.80,
                   0.15, 0.30, 50, 'filled', 'atp', 'Alcaraz', 'Sinner',
                   '2026-04-08T10:00:00Z', '2026-04-08T11:00:00Z',
                   '2026-04-08T11:00:00Z')""",
    )
    filled_order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # slippage = bid_price - fill_price = 0.15 - 0.14 = +0.01 (positive = better than expected)
    conn.execute(
        """INSERT INTO paper_limit_fills
           (order_id, fill_label, trade_price, trade_size, trade_ts, bid_price, slippage, created_at)
           VALUES (?, 'strict', 0.14, 60.0, '2026-04-08T11:00:00Z', 0.15, 0.01, '2026-04-08T11:00:00Z')""",
        (filled_order_id,),
    )
    conn.commit()
    yield _make_client(conn)
    conn.close()


def test_stink_bid_orders_returns_order_book(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "orders" in data
    assert len(data["orders"]) == 2
    resting = [o for o in data["orders"] if o["state"] == "resting"]
    filled = [o for o in data["orders"] if o["state"] == "filled"]
    assert len(resting) == 1
    assert len(filled) == 1
    assert resting[0]["bid_price"] == 0.12
    assert resting[0]["player1"] == "Djokovic"


def test_stink_bid_orders_includes_fills(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    data = resp.get_json()
    filled = [o for o in data["orders"] if o["state"] == "filled"]
    assert len(filled[0]["fills"]) == 1
    assert filled[0]["fills"][0]["fill_label"] == "strict"
    assert filled[0]["fills"][0]["trade_price"] == 0.14
    assert filled[0]["fills"][0]["slippage"] == 0.01


def test_stink_bid_orders_summary(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    data = resp.get_json()
    assert "summary" in data
    s = data["summary"]
    assert s["total"] == 2
    assert s["resting"] == 1
    assert s["filled"] == 1


def test_stink_bid_orders_wrong_strategy_type(tmp_path):
    conn = _get_conn(tmp_path)
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (1, '0x0000000000000000000000000000000000000001',
                   'Other', 'insider_copy_v1', '{}', 'active')""",
    )
    conn.commit()
    c = _make_client(conn)
    resp = c.get("/api/strategies/1/stink-bid-orders")
    assert resp.status_code == 400
    conn.close()


def test_stink_bid_orders_not_found(client):
    resp = client.get("/api/strategies/999/stink-bid-orders")
    assert resp.status_code == 404
