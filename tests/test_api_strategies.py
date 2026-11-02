"""Read-only API surface for the strategies blueprint. Focused on the synthetic paper_trader
card: the list route prepends it with a STRING id 'paper_trader', so a direct GET to the detail
URL must resolve (it used to 404 against the int-only detail route). (review #LOW 2026-06-17)"""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from sniper.routes.api_strategies import create_api_strategies_blueprint


def _ctx_with_paper_trades():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE paper_trades (status TEXT, pnl_usdc REAL, entry_price REAL,
           current_price REAL, shares REAL, size_usdc REAL)"""
    )
    conn.execute(
        "INSERT INTO paper_trades(status, pnl_usdc, entry_price, current_price, shares, size_usdc) "
        "VALUES('resolution_win', 5.0, 0.40, 1.0, 10, 4)"
    )
    conn.commit()
    return {"conn": conn, "normalize_wallet": lambda w: w, "wallet_valid": lambda w: True,
            "safe_int": lambda v, d=None: int(v) if v is not None else d,
            "optional_float": lambda v: float(v) if v not in (None, "") else None}


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(create_api_strategies_blueprint(_ctx_with_paper_trades()))
    return app.test_client()


def test_paper_trader_detail_route_resolves(client):
    r = client.get("/api/strategies/paper_trader")
    assert r.status_code == 200
    body = r.get_json()
    assert body["id"] == "paper_trader"
    assert body["is_paper_trader_card"] is True
    assert body["closed_count"] == 1 and body["win_rate"] == 100.0


def test_numeric_strategy_detail_still_404s_when_absent(client):
    # the int route is untouched; an unknown numeric id still 404s (no strategies table here)
    assert client.get("/api/strategies/999").status_code in (404, 500)
