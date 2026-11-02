"""BTC 5m API surface — read-only status + backtest endpoints."""
from __future__ import annotations

import sqlite3

from flask import Flask

from sniper.algos.btc5m import data as D
from sniper.routes.api_btc5m import create_api_btc5m_blueprint


def _app(conn):
    app = Flask(__name__)
    app.register_blueprint(create_api_btc5m_blueprint({"conn": conn}))
    return app.test_client()


def _conn():
    conn = sqlite3.connect(":memory:")
    D.ensure_tables(conn)
    return conn


def test_status_empty_book_is_gathering_evidence():
    conn = _conn()
    D.save_bars(conn, D.parse_klines([[1000, "100", "101", "99", "100.5", "5", 1299999]], "BTCUSDT"))
    D.save_state(conn, "BTCUSDT", {"position": 0, "entry_px": None, "entry_ms": None}, 1299999)
    body = _app(conn).get("/api/btc5m/status").get_json()
    assert body["available"] is True
    assert body["price"] == 100.5
    assert body["position_label"] == "FLAT"
    assert body["verdict"]["n"] == 0
    assert "gathering" in body["verdict"]["status"].lower()
    assert "NOT advice" in body["disclaimer"]


def test_status_reflects_position_and_trades():
    conn = _conn()
    D.save_bars(conn, D.parse_klines([[1000, "100", "101", "99", "100", "5", 1299999]], "BTCUSDT"))
    D.save_state(conn, "BTCUSDT", {"position": 1, "entry_px": 100.0, "entry_ms": 1000}, 1299999)
    D.save_paper_trade(conn, "BTCUSDT", {"entry_ms": 1, "exit_ms": 2, "side": 1, "entry_px": 100.0,
                                         "exit_px": 102.0, "fee_pct": 0.0004, "pnl_pct": 0.0196})
    body = _app(conn).get("/api/btc5m/status").get_json()
    assert body["position_label"] == "LONG"
    assert body["verdict"]["n"] == 1
    assert len(body["recent_trades"]) == 1
    assert body["config"]["signal"] == "meanrev_zscore"


def test_no_loss_book_serializes_valid_json():
    # REGRESSION: a paper book with wins and NO losses gives profit_factor=+inf; jsonify writes the
    # bare token `Infinity`, which is invalid JSON -> the browser's JSON.parse throws and the tab
    # shows "5m algo unavailable". The response must be strictly valid JSON (inf -> null).
    import json as _json
    conn = _conn()
    D.save_bars(conn, D.parse_klines([[1000, "100", "101", "99", "100", "5", 1299999]], "BTCUSDT"))
    D.save_state(conn, "BTCUSDT", {"position": 0, "entry_px": None, "entry_ms": None}, 1299999)
    for i in range(3):  # all winners -> gross_loss 0 -> pf inf
        D.save_paper_trade(conn, "BTCUSDT", {"entry_ms": i, "exit_ms": i + 1, "side": 1,
                                             "entry_px": 100.0, "exit_px": 102.0, "fee_pct": 0.0004,
                                             "pnl_pct": 0.0196})
    raw = _app(conn).get("/api/btc5m/status").get_data(as_text=True)
    assert "Infinity" not in raw and "NaN" not in raw
    body = _json.loads(raw)                              # must parse without error
    assert body["verdict"]["profit_factor"] is None     # +inf coerced to null


def test_backtest_endpoint_handles_missing_file(monkeypatch, tmp_path):
    # REGRESSION (Codex #5): actually exercise the MISSING path (the gitignored local
    # runs/btc5m/best_config.json was masking it). Point BEST_CONFIG at a nonexistent file.
    import sniper.routes.api_btc5m as M
    monkeypatch.setattr(M, "BEST_CONFIG", str(tmp_path / "nope.json"))
    body = _app(_conn()).get("/api/btc5m/backtest").get_json()
    assert body["available"] is False
    assert "disclaimer" in body and "NOT advice" in body["disclaimer"]
    assert "No backtest yet" in body["message"]
