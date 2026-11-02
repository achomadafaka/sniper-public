"""Discovery provenance survives promotion so forward copy-PnL can be split by pool."""
import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__); app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return csrf_client(app), c


def test_promote_preserves_leaderboard_provenance():
    cl, c = _client()
    m.ingest_leaderboard(c, [{"id": "u2", "userHandle": "bob", "totalPnL": 9.0,
                              "topHoldings": [{"pnl": 1.0}]}], "2026-07-07T00:00:00Z")
    cl.post("/api/memecoin/candidates/u2/promote", json={})
    w = m.get_wallet_by_handle(c, "bob")
    assert w["discovery_source"] == "leaderboard"


def test_first_discovery_wins_not_overwritten():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice")
    m.set_wallet_discovery_source(c, "u1", "large_buy")
    m.set_wallet_discovery_source(c, "u1", "leaderboard")   # must NOT overwrite
    assert m.get_wallet_by_handle(c, "alice")["discovery_source"] == "large_buy"
