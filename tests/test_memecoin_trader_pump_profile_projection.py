"""`trader_pump_profile` must fetch only what it reads.

The UI refetches this every 4 seconds while the operator sits on the discovery, traders or
performance subtab, and it reads 96% of a 104k-row table. `SELECT *` pulled all 20 columns —
14 of them unused — at 410.8 ms / 40.90 MB against 150.2 ms / 17.51 MB for the projection.
End to end the function went 1266.2 ms -> 767.8 ms on the live ledger with byte-identical output.

The guard below counts the columns the query actually returns rather than matching the SQL text.
A literal source assertion is the shape that broke twice this week the moment someone edited the
line around it.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

NEEDED = {"source_user_id", "handle", "max_gain_pct", "peak_horizon_s", "ref_price", "marks_json"}


class _WatchingConn:
    """Records the column set of every query, without touching the sqlite3 C type."""

    def __init__(self, inner):
        self._inner = inner
        self.column_sets = []

    def execute(self, *a, **k):
        cur = self._inner.execute(*a, **k)
        if cur.description:
            self.column_sets.append({d[0] for d in cur.description})
        return cur


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_signal_outcomes(
        fomo_event_id TEXT PRIMARY KEY, source_user_id TEXT, handle TEXT, mint TEXT, ticker TEXT,
        buy_ts TEXT, ref_price REAL, their_size_usd REAL, marks_json TEXT, max_gain_pct REAL,
        peak_horizon_s INT, last_price REAL, n_marks INT, status TEXT, created_at TEXT,
        updated_at TEXT, network_id TEXT, buy_ts_normalized TEXT, capture_version INT,
        clock_domain TEXT)""")
    marks = '{"60": {"price": 2.0, "age_s": 61}, "300": {"price": 3.0, "age_s": 301}}'
    c.executemany(
        "INSERT INTO memecoin_signal_outcomes(fomo_event_id, source_user_id, handle, ref_price,"
        " marks_json, max_gain_pct, peak_horizon_s, status) VALUES(?,?,?,?,?,?,?, 'matured')",
        [(f"e{i}", "u1", "alice", 1.0, marks, 100.0 + i, 60) for i in range(4)])
    c.commit()
    return c


def test_it_fetches_only_the_columns_it_reads(conn):
    watch = _WatchingConn(conn)
    m.trader_pump_profile(watch, min_signals=1)
    assert watch.column_sets, "the function must have run a query"
    fetched = watch.column_sets[0]
    assert fetched == NEEDED, (
        "over-fetching costs ~500ms per call on a 4s poll; under-fetching breaks _mark_gain")


def test_the_aggregation_is_unchanged(conn):
    rows = m.trader_pump_profile(conn, min_signals=1)
    assert len(rows) == 1
    r = rows[0]
    assert r["source_user_id"] == "u1" and r["handle"] == "alice"
    assert r["n_signals"] == 4 and r["n_matured"] == 4
    assert r["median_max_gain_pct"] == pytest.approx(101.5)
    # marks_json must still be fetched, or these two go None
    assert r["median_1m_gain_pct"] == pytest.approx(100.0)
    assert r["median_5m_gain_pct"] == pytest.approx(200.0)


def test_the_user_filter_still_narrows(conn):
    assert m.trader_pump_profile(conn, min_signals=1, source_user_ids=["nobody"]) == []
    assert len(m.trader_pump_profile(conn, min_signals=1, source_user_ids=["u1"])) == 1
