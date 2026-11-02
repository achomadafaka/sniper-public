import json
import sqlite3

from flask import Flask

from sniper.persistence import db_memecoin as m
import sniper.routes.api_memecoin as api


def test_traders_analytics_route():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.upsert_trader_analytics(c, {"source_user_id": "u1", "handle": "alice",
                                  "realized_pnl_usd": 50.0, "coverage_ratio": 0.2,
                                  "chain_mix": "{}", "provider_mix": "{}",
                                  "metrics_json": "{}"})

    bp = api.create_api_memecoin_blueprint({"conn": c})
    app = Flask(__name__)
    app.register_blueprint(bp)
    client = app.test_client()

    resp = client.get("/api/memecoin/traders/analytics")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["rows"][0]["source_user_id"] == "u1"
    assert data["rows"][0]["coverage_ratio"] == 0.2

    # the shared connection must remain usable after the request (not closed)
    assert m.get_trader_analytics(c, "u1")["handle"] == "alice"
