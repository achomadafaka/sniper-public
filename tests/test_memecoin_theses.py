"""memecoin_theses table + CRUD: dedup, filters, batch latest, token confluence counts."""
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _th(fid, uid="u1", mint="MINT", handle="alice", comment="gm", pnl=-30.0,
        created="2026-07-08T09:00:00Z", received="2026-07-08T09:00:03Z"):
    return {"fomo_thesis_id": fid, "comment_id": "c-" + fid, "source_user_id": uid,
            "handle": handle, "mint": mint, "ticker": "PEPE", "comment": comment,
            "has_link": False, "their_pnl_pct": pnl, "their_realized_pnl_pct": 0.0,
            "their_notional_usd": 1000.0, "their_unrealized_pnl_usd": -300.0,
            "market_cap": 1e6, "fdv": 2e6, "price": 0.01, "tag": None,
            "network_id": "1", "swap_id": None, "trade_id": None,
            "created_at": created, "received_at": received, "lag_s": 2.0,
            "signal_source": "global_feed"}


def test_tables_exist_and_idempotent():
    c = _conn()
    m.ensure_memecoin_tables(c)  # twice -> no error
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_theses)")}
    assert {"fomo_thesis_id", "source_user_id", "mint", "comment", "has_link",
            "their_pnl_pct", "their_notional_usd", "created_at", "received_at"} <= cols
    lat = {r[1] for r in c.execute("PRAGMA table_info(memecoin_alert_latency)")}
    assert {"fomo_event_id", "fomo_created_at", "detected_at", "lag_s",
            "alert_sent_at", "send_ok", "send_ms"} <= lat


def test_record_thesis_dedups():
    c = _conn()
    assert m.record_thesis(c, _th("t1")) is True
    assert m.record_thesis(c, _th("t1")) is False   # same fomo_thesis_id -> keep-first
    assert c.execute("SELECT COUNT(*) FROM memecoin_theses").fetchone()[0] == 1


def test_list_theses_newest_first_and_filters():
    c = _conn()
    m.record_thesis(c, _th("t1", uid="u1", mint="A", created="2026-07-08T09:00:00Z"))
    m.record_thesis(c, _th("t2", uid="u1", mint="B", created="2026-07-08T09:05:00Z"))
    m.record_thesis(c, _th("t3", uid="u2", mint="A", created="2026-07-08T09:10:00Z"))
    ids = [r["fomo_thesis_id"] for r in m.list_theses(c)]
    assert ids == ["t3", "t2", "t1"]                       # newest first
    assert [r["fomo_thesis_id"] for r in m.list_theses(c, uid="u1")] == ["t2", "t1"]
    assert [r["fomo_thesis_id"] for r in m.list_theses(c, mint="A")] == ["t3", "t1"]
    assert len(m.list_theses(c, limit=1)) == 1


def test_latest_thesis_by_trader_batch():
    c = _conn()
    m.record_thesis(c, _th("t1", uid="u1", created="2026-07-08T09:00:00Z", comment="old"))
    m.record_thesis(c, _th("t2", uid="u1", created="2026-07-08T09:05:00Z", comment="new"))
    m.record_thesis(c, _th("t3", uid="u2", created="2026-07-08T09:01:00Z", comment="solo"))
    out = m.latest_thesis_by_trader(c, ["u1", "u2", "u3"])
    assert out["u1"]["comment"] == "new"
    assert out["u2"]["comment"] == "solo"
    assert "u3" not in out


def test_token_thesis_counts_window_on_received_at():
    c = _conn()
    # two curated theses on MINT within window, one old (out of window)
    m.record_thesis(c, _th("t1", uid="u1", mint="MINT", received="2026-07-08T09:00:00Z"))
    m.record_thesis(c, _th("t2", uid="u2", mint="MINT", received="2026-07-08T09:30:00Z"))
    m.record_thesis(c, _th("t3", uid="u3", mint="MINT", received="2026-07-07T00:00:00Z"))
    m.record_thesis(c, _th("t4", uid="u1", mint="OTHER", received="2026-07-08T09:00:00Z"))
    counts = m.token_thesis_counts(c, ["MINT", "OTHER"], within_hours=24,
                                   now_iso="2026-07-08T10:00:00Z")
    assert counts["MINT"] == 2      # t1,t2 in window; t3 too old
    assert counts["OTHER"] == 1
