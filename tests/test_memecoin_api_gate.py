import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def test_gated_endpoint_attaches_gate_and_windows():
    cl, c = _client()
    m.upsert_trader_analytics(c, {
        "source_user_id": "u1", "handle": "alice",
        "fomo_total_pnl": 100_000.0, "fomo_unrealized_pnl": 10_000.0,
        "realized_pnl_7d": 500.0, "realized_7d_covered": 1,
        "realized_pnl_30d": 2_000.0, "realized_30d_covered": 1,
        "median_hold_s": 3600.0, "last_swap_age_h": 5.0, "active_days": 10})
    r = cl.get("/api/memecoin/traders/gated")
    assert r.status_code == 200
    rows = r.get_json()["rows"]
    assert len(rows) == 1
    g = rows[0]["gate"]
    assert g["lifetime"]["state"] == "green" and g["w7d"]["state"] == "green"
    assert g["summary"]["green"] == 4
    assert "fomo_pnl_delta_7d" in rows[0]


def test_combined_and_candidates_carry_gate():
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    m.ingest_leaderboard(c, [{"id": "u2", "userHandle": "bob", "totalPnL": 50000.0,
                              "topHoldings": [{"pnl": 1000.0}]}], "2026-07-07T00:00:00Z")
    comb = cl.get("/api/memecoin/traders/combined").get_json()["rows"]
    assert comb and "gate" in comb[0]
    cand = cl.get("/api/memecoin/candidates?min_usd=0").get_json()["rows"]
    bob = [r for r in cand if r["source_user_id"] == "u2"]
    assert bob and bob[0]["source"] == "leaderboard"
    assert bob[0]["gate"]["lifetime"]["state"] == "green"    # +50k lifetime, 2% bags
