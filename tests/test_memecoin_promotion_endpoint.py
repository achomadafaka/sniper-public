"""Promotion readout must preserve chain and outcome identity."""
import sqlite3
from datetime import datetime, timezone

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.ensure_promotion_lane_table(conn)
    now = datetime.now(timezone.utc).isoformat()
    for attempt_id, network_id in (("a1", "4663"), ("a2", "solana")):
        m.record_promotion_lane_row(conn, {
            "attempt_id": attempt_id, "network_id": network_id, "mint": "M",
            "source_user_id": "u1", "source_handle": "alice", "verdict": "admitted",
            "decision_started_at": now, "evaluated_at": now, "created_at": now,
            "exit_reason": "tp", "pnl_usd": 4.0,
        })
    conn.executemany("INSERT INTO memecoin_positions "
                     "(source_user_id, network_id, execution_mode, is_open, realized_pnl) "
                     "VALUES (?,?,?,?,?)", [
        ("u1", "4663", "real", 0, -3.0),
        ("u1", "robin", "real", 0, -2.0),
        ("u1", "solana", "real", 0, 100.0),
        ("u1", "4663", "real", 1, 50.0),
        ("u1", "4663", "paper", 0, 40.0),
        ("u1", "4663", "real", 0, None),
    ])
    conn.commit()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    return app.test_client(), conn


def test_promotion_endpoint_keeps_paper_and_funded_chain_scoped():
    client, _ = _client()
    response = client.get("/api/memecoin/promotion?chain=robin")
    assert response.status_code == 200
    body = response.get_json()
    assert len(body["wallets"]) == 1
    assert body["wallets"][0]["network_id"] == "4663"
    assert body["funded"]["u1"] == {"closed": 2, "pnl": -5.0}


def test_promotion_endpoint_rejects_missing_chain_and_invalid_window():
    client, _ = _client()
    assert client.get("/api/memecoin/promotion").status_code == 400
    assert client.get("/api/memecoin/promotion?chain=robin&days=nan").status_code == 400
    assert client.get("/api/memecoin/promotion?chain=robin&days=-1").status_code == 400


def test_promotion_endpoint_reports_read_failure():
    client, conn = _client()
    conn.execute("DROP TABLE memecoin_promotion_lane")
    response = client.get("/api/memecoin/promotion?chain=robin")
    assert response.status_code == 500
    assert response.get_json()["error"] == "promotion_unavailable"


def test_promotion_signal_detail_is_chain_scoped_and_bounded():
    client, _ = _client()
    response = client.get("/api/memecoin/promotion/signals/u1?chain=robin")
    assert response.status_code == 200
    rows = response.get_json()["signals"]
    assert len(rows) == 1
    assert rows[0]["network_id"] == "4663"
    assert rows[0]["verdict"] == "admitted"
    assert client.get("/api/memecoin/promotion/signals/u1").status_code == 400
    assert client.get("/api/memecoin/promotion/signals/u1?chain=robin&limit=1001").status_code == 400
