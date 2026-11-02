import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__); app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client(), c


def _matured(c, uid, eid, ref, gain):
    m.create_signal_outcome(c, fomo_event_id=eid, source_user_id=uid, handle=uid, mint="M"+eid,
                            ticker="T", buy_ts="2026-07-07T10:00:00Z", ref_price=ref,
                            their_size_usd=3000.0, now_iso="2026-07-07T10:00:00Z")
    m.save_outcome(c, eid, marks={60: {"price": ref*(1+gain/100), "age_s": 61}}, max_gain_pct=gain,
                   peak_horizon_s=60, last_price=ref, n_marks=1, status="matured", now_iso="2026-07-07T11:00:00Z")


def test_pump_endpoint_default_min_signals_honors_setting():
    # P2 (deep review): MEMECOIN_PUMP_MIN_SIGNALS (default 5) was dead config — the ranking
    # endpoint hardcoded n=1. It now defaults to the setting, so a thin-sample trader (3 matured
    # < 5) is excluded from the ranking, while an explicit ?n=1 can still show everything.
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    for i, g in enumerate([30.0, 50.0, 10.0]):        # 3 matured signals, < default 5
        _matured(c, "u1", f"e{i}", 0.005, g)
    assert cl.get("/api/memecoin/traders/pump").get_json()["rows"] == []     # default min=5 -> excluded
    assert cl.get("/api/memecoin/traders/pump?n=1").get_json()["rows"]       # explicit n=1 -> shown


def test_pump_endpoint_and_combined_merge():
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    for i, g in enumerate([30.0, 50.0, 10.0]):
        _matured(c, "u1", f"e{i}", 0.005, g)
    pump = cl.get("/api/memecoin/traders/pump?n=1").get_json()["rows"]
    assert pump and pump[0]["source_user_id"] == "u1" and pump[0]["n_signals"] == 3
    comb = cl.get("/api/memecoin/traders/combined").get_json()["rows"]
    row = [r for r in comb if r["source_user_id"] == "u1"][0]
    assert row["pump"] and abs(row["pump"]["median_max_gain_pct"] - 30.0) < 1e-6


def test_combined_bounds_pump_aggregation_to_returned_wallets(monkeypatch):
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    seen = []
    original = m.trader_pump_profile

    def bounded(conn, *args, **kwargs):
        seen.append(kwargs.get("source_user_ids"))
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(m, "trader_pump_profile", bounded)
    assert cl.get("/api/memecoin/traders/combined").status_code == 200
    assert seen == [["u1"]]
