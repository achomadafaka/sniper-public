"""API: thesis endpoints (stance-decorated) + alert-latency summary + trader-row decoration."""
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


def _th(fid, uid="u1", mint="MINT", pnl=-30.0, comment="gm",
        created="2026-07-08T09:00:00Z"):
    return {"fomo_thesis_id": fid, "comment_id": "c-" + fid, "source_user_id": uid,
            "handle": "alice", "mint": mint, "ticker": "PEPE", "comment": comment,
            "has_link": False, "their_pnl_pct": pnl, "their_realized_pnl_pct": 0.0,
            "their_notional_usd": 1000.0, "their_unrealized_pnl_usd": -300.0,
            "market_cap": 1e6, "fdv": 2e6, "price": 0.01, "tag": None, "network_id": "1",
            "swap_id": None, "trade_id": None, "created_at": created,
            "received_at": created, "lag_s": 2.0, "signal_source": "global_feed"}


def test_theses_endpoint_decorates_stance():
    cl, c = _client()
    m.record_thesis(c, _th("t1", pnl=-49.0))
    m.record_thesis(c, _th("t2", pnl=331.0, created="2026-07-08T09:05:00Z"))
    rows = cl.get("/api/memecoin/theses").get_json()["rows"]
    assert [r["fomo_thesis_id"] for r in rows] == ["t2", "t1"]        # newest first
    by_id = {r["fomo_thesis_id"]: r for r in rows}
    assert by_id["t1"]["stance"] == "underwater" and by_id["t2"]["stance"] == "winning"


def test_theses_endpoint_filters():
    cl, c = _client()
    m.record_thesis(c, _th("t1", uid="u1", mint="A"))
    m.record_thesis(c, _th("t2", uid="u2", mint="B", created="2026-07-08T09:05:00Z"))
    assert [r["fomo_thesis_id"] for r in
            cl.get("/api/memecoin/theses?uid=u1").get_json()["rows"]] == ["t1"]
    assert [r["fomo_thesis_id"] for r in
            cl.get("/api/memecoin/theses?mint=B").get_json()["rows"]] == ["t2"]


def test_trader_theses_alias():
    cl, c = _client()
    m.record_thesis(c, _th("t1", uid="u7"))
    rows = cl.get("/api/memecoin/traders/u7/theses").get_json()["rows"]
    assert len(rows) == 1 and rows[0]["source_user_id"] == "u7"


def test_alert_latency_summary_endpoint():
    cl, c = _client()
    m.record_alert(c, {"fomo_event_id": "e1", "source_user_id": "u1", "handle": "a",
                       "mint": "M", "ticker": "T", "their_size_usd": 3000.0,
                       "market_cap": 1e6, "fomo_created_at": "2026-07-08T10:00:00Z",
                       "detected_at": "2026-07-08T10:00:01Z", "lag_s": 0.6,
                       "signal_source": "global_feed"})
    m.update_alert_sent(c, "e1", "2026-07-08T10:00:01Z", True, 12.5)
    r = cl.get("/api/memecoin/alert-latency").get_json()
    assert r["n"] == 1 and r["n_sent"] == 1 and r["median_send_ms"] == 12.5
    assert r["send_ok_rate"] == 1.0


def test_alert_latency_send_median_is_success_only():
    # a timed-out send (send_ok=0, ~3000ms) must NOT inflate median_send_ms.
    cl, c = _client()
    for eid, ok, ms in (("e1", True, 10.0), ("e2", False, 3000.0)):
        m.record_alert(c, {"fomo_event_id": eid, "source_user_id": "u1", "handle": "a",
                           "mint": "M", "ticker": "T", "their_size_usd": 3000.0,
                           "market_cap": 1e6, "fomo_created_at": "2026-07-08T10:00:00Z",
                           "detected_at": "2026-07-08T10:00:01Z", "lag_s": 0.6,
                           "signal_source": "global_feed"})
        m.update_alert_sent(c, eid, "2026-07-08T10:00:01Z", ok, ms, local_ms=5.0)
    r = cl.get("/api/memecoin/alert-latency").get_json()
    assert r["n_sent"] == 2 and r["n_sent_ok"] == 1
    assert r["median_send_ms"] == 10.0        # success-only, the 3000ms timeout excluded
    assert r["median_total_ms"] == 15.0       # local(5) + send(10), successful only
    assert r["send_ok_rate"] == 0.5


def test_combined_rows_carry_latest_thesis():
    cl, c = _client()
    m.add_wallet(c, "u1", "alice")
    m.record_thesis(c, _th("t1", uid="u1", comment="old", created="2026-07-08T09:00:00Z"))
    m.record_thesis(c, _th("t2", uid="u1", comment="latest", created="2026-07-08T09:30:00Z"))
    rows = cl.get("/api/memecoin/traders/combined").get_json()["rows"]
    row = next(r for r in rows if r["source_user_id"] == "u1")
    assert row["latest_thesis"]["comment"] == "latest"
    assert row["latest_thesis"]["stance"] == "underwater"
