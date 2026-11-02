"""TDD for the per-position intra-trade mid marks — the data the viability study (Q2) lacked, so a
theta-aware exit counterfactual becomes computable. Logging only; tested against a real in-memory sqlite."""
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import position_marks as PM


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    PM.ensure_schema(c)
    return c


def test_ensure_schema_idempotent(conn):
    PM.ensure_schema(conn)  # must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(crypto5m_position_marks)")}
    for c in ("position_key", "seconds_held", "side", "mid", "bid", "ask"):
        assert c in cols


def test_record_mark_inserts(conn):
    rid = PM.record_mark(conn, {"position_key": "w1-1", "window_start_ts": 1000, "tick_ts": 1010,
                                "seconds_held": 5, "side": "Up", "mid": 0.58, "bid": 0.57, "ask": 0.59})
    assert isinstance(rid, int) and rid > 0
    row = conn.execute("SELECT side, mid FROM crypto5m_position_marks WHERE id=?", (rid,)).fetchone()
    assert row["side"] == "Up" and row["mid"] == pytest.approx(0.58)


def test_marks_for_position_ordered_by_seconds_held(conn):
    for sh, mid in ((10, 0.62), (0, 0.55), (5, 0.59)):
        PM.record_mark(conn, {"position_key": "w1-1", "window_start_ts": 1000, "tick_ts": 1000 + sh,
                              "seconds_held": sh, "side": "Up", "mid": mid, "bid": mid - 0.01, "ask": mid + 0.01})
    PM.record_mark(conn, {"position_key": "OTHER", "window_start_ts": 1000, "tick_ts": 1003,
                          "seconds_held": 3, "side": "Down", "mid": 0.4, "bid": 0.39, "ask": 0.41})
    marks = PM.marks_for_position(conn, "w1-1")
    assert [m["seconds_held"] for m in marks] == [0, 5, 10]        # ordered, and the OTHER position excluded
    assert [round(m["mid"], 2) for m in marks] == [0.55, 0.59, 0.62]
