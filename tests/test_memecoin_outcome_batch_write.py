"""One pass must be one write transaction per chunk, not one per row.

`save_outcome` committed per row and `memecoin_pump` looped over every open capture row, so a pass
became ~124 transactions per second. Each commit copies its dirty pages into the WAL as separate
frames, and the next row copies the SAME pages again — `status` is a three-value column over 104k
rows, so every write lands on the same handful of `idx_mc_outcome_status` leaves. Live measurement:
4,072 WAL frames over 1,223 distinct pages in eight seconds, hottest pages written 84-192 times.

`memecoin_signal_outcomes_v2` has the same table shape and the same three-value status index and
does NOT appear in the hot pages at all — because `memecoin_capture_v2` already executes per row
and commits ONCE after the loop. That is the shape being adopted here.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_signal_outcomes(
        fomo_event_id TEXT PRIMARY KEY, marks_json TEXT, max_gain_pct REAL, peak_horizon_s INT,
        last_price REAL, n_marks INT, status TEXT, updated_at TEXT)""")
    c.executemany("INSERT INTO memecoin_signal_outcomes(fomo_event_id, status) VALUES(?, 'open')",
                  [(f"e{i}",) for i in range(20)])
    c.commit()
    return c


def _u(i, status="matured"):
    return {"fomo_event_id": f"e{i}", "marks": {60: {"price": 1.0, "age_s": 61}},
            "max_gain_pct": 5.0, "peak_horizon_s": 60, "last_price": 1.0, "n_marks": 1,
            "status": status}


def test_a_batch_writes_every_row(conn):
    assert m.save_outcomes(conn, [_u(i) for i in range(20)], now_iso="T") == 20
    got = conn.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes WHERE status='matured'")
    assert got.fetchone()[0] == 20


def test_an_empty_batch_commits_nothing(conn):
    assert m.save_outcomes(conn, [], now_iso="T") == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_signal_outcomes WHERE status='open'").fetchone()[0] == 20


class _CountingConn:
    """Counts commits without touching the sqlite3 C type, which cannot be monkeypatched."""

    def __init__(self, inner):
        self._inner = inner
        self.commits = 0

    def executemany(self, *a, **k):
        return self._inner.executemany(*a, **k)

    def execute(self, *a, **k):
        return self._inner.execute(*a, **k)

    def commit(self):
        self.commits += 1
        return self._inner.commit()


def test_the_batch_is_chunked_so_the_write_lock_stays_bounded(conn, monkeypatch):
    """The chunk bound is what keeps the lock hold near 3ms as the open set grows.

    Measured on a replica of the real shape: 0.6ms at 100 rows, 3.2ms at 1,000, 13.8ms for the
    whole 4,400-row open set. Even unbounded it never approached a sweep interval, so this bound
    is insurance against growth, not a fix for an observed stall.
    """
    monkeypatch.setattr(m, "SAVE_OUTCOME_CHUNK", 7)
    counting = _CountingConn(conn)
    assert m.save_outcomes(counting, [_u(i) for i in range(20)], now_iso="T") == 20
    assert counting.commits == 3, "20 rows at a chunk of 7 is three transactions, not twenty"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_signal_outcomes WHERE status='matured'").fetchone()[0] == 20


def test_save_outcome_still_works_for_a_single_row(conn):
    m.save_outcome(conn, "e3", marks={60: {"price": 2.0, "age_s": 61}}, max_gain_pct=1.0,
                   peak_horizon_s=60, last_price=2.0, n_marks=1, status="matured", now_iso="T")
    row = conn.execute(
        "SELECT status, n_marks FROM memecoin_signal_outcomes WHERE fomo_event_id='e3'").fetchone()
    assert row["status"] == "matured" and row["n_marks"] == 1


# ------------------------------------------------------------ a failed batch must release the lock

def _file_conn(tmp_path):
    """Built like `db_core.get_connection`: transactional (Python's default isolation_level="").

    That property is what makes the batching real — and it is also why a raising `executemany`
    leaves an open write transaction behind. `get_readonly_connection` twelve lines below it uses
    isolation_level=None deliberately, so the two connections on this file differ in exactly the
    property this code depends on.
    """
    c = sqlite3.connect(str(tmp_path / "t.db"))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE memecoin_signal_outcomes(
        fomo_event_id TEXT PRIMARY KEY, marks_json TEXT, max_gain_pct REAL, peak_horizon_s INT,
        last_price REAL, n_marks INT, status TEXT NOT NULL, updated_at TEXT)""")
    c.executemany("INSERT INTO memecoin_signal_outcomes(fomo_event_id, status) VALUES(?, 'open')",
                  [(f"e{i}",) for i in range(20)])
    c.commit()
    return c


def test_a_failed_batch_rolls_back_and_releases_the_write_lock(tmp_path):
    """Reproduced before the fix: IntegrityError, `in_transaction` True, 5 of 10 rows applied.

    An open write transaction on this connection blocks the funded exit sweep and the web's
    manual-intent writes until something else commits or the process dies — the exact contention
    the batching exists to avoid.
    """
    c = _file_conn(tmp_path)
    batch = ([_u(i) for i in range(5)]
             + [_u(5, status=None)]            # NOT NULL violation, mid-batch
             + [_u(i) for i in range(6, 10)])
    with pytest.raises(sqlite3.IntegrityError):
        m.save_outcomes(c, batch, now_iso="T")
    assert c.in_transaction is False, "a failed batch must not leave a write transaction open"
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_signal_outcomes WHERE status='matured'").fetchone()[0] == 0


def test_chunks_committed_before_a_failure_stay_committed(tmp_path, monkeypatch):
    """Each chunk is a complete unit. Partial progress across chunks costs resolution, not truth."""
    monkeypatch.setattr(m, "SAVE_OUTCOME_CHUNK", 5)
    c = _file_conn(tmp_path)
    batch = [_u(i) for i in range(5)] + [_u(5, status=None)] + [_u(i) for i in range(6, 10)]
    with pytest.raises(sqlite3.IntegrityError):
        m.save_outcomes(c, batch, now_iso="T")
    assert c.in_transaction is False
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_signal_outcomes WHERE status='matured'").fetchone()[0] == 5
