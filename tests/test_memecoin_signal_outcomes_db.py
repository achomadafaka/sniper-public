"""memecoin_signal_outcomes: per-sighting forward price-path capture (CRUD layer)."""
import json
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_create_outcome_dedups_by_event_id():
    c = _conn()
    ok = m.create_signal_outcome(c, fomo_event_id="e1", source_user_id="u1", handle="alice",
                                 mint="M", ticker="PEPE", buy_ts="2026-07-07T10:00:00Z",
                                 ref_price=0.005, their_size_usd=3000.0, now_iso="2026-07-07T10:00:01Z")
    assert ok is True
    dup = m.create_signal_outcome(c, fomo_event_id="e1", source_user_id="u1", handle="alice",
                                  mint="M", ticker="PEPE", buy_ts="2026-07-07T10:00:00Z",
                                  ref_price=0.005, their_size_usd=3000.0, now_iso="2026-07-07T10:00:02Z")
    assert dup is False
    assert c.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes").fetchone()[0] == 1


def test_list_open_outcomes_parses_marks_and_excludes_matured():
    c = _conn()
    m.create_signal_outcome(c, fomo_event_id="e1", source_user_id="u1", handle="a", mint="M",
                            ticker="T", buy_ts="2026-07-07T10:00:00Z", ref_price=0.005,
                            their_size_usd=3000.0, now_iso="2026-07-07T10:00:01Z")
    m.save_outcome(c, "e1", marks={60: {"price": 0.006, "age_s": 61}}, max_gain_pct=20.0,
                   peak_horizon_s=60, last_price=0.006, n_marks=1, status="open",
                   now_iso="2026-07-07T10:01:01Z")
    op = m.list_open_outcomes(c)
    assert len(op) == 1 and op[0]["marks"] == {"60": {"price": 0.006, "age_s": 61}}
    m.save_outcome(c, "e1", marks={60: {"price": 0.006, "age_s": 61}}, max_gain_pct=20.0,
                   peak_horizon_s=60, last_price=0.006, n_marks=1, status="matured",
                   now_iso="2026-07-07T11:00:00Z")
    assert m.list_open_outcomes(c) == []


def test_save_outcome_persists_scalars():
    c = _conn()
    m.create_signal_outcome(c, fomo_event_id="e1", source_user_id="u1", handle="a", mint="M",
                            ticker="T", buy_ts="2026-07-07T10:00:00Z", ref_price=0.005,
                            their_size_usd=3000.0, now_iso="2026-07-07T10:00:01Z")
    m.save_outcome(c, "e1", marks={300: {"price": 0.01, "age_s": 305}}, max_gain_pct=100.0,
                   peak_horizon_s=300, last_price=0.01, n_marks=1, status="matured",
                   now_iso="2026-07-07T10:05:05Z")
    r = dict(c.execute("SELECT * FROM memecoin_signal_outcomes WHERE fomo_event_id='e1'").fetchone())
    assert abs(r["max_gain_pct"] - 100.0) < 1e-6 and r["peak_horizon_s"] == 300
    assert r["status"] == "matured" and json.loads(r["marks_json"])["300"]["price"] == 0.01
