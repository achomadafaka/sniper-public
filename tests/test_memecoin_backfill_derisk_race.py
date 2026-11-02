"""Two concurrent backfills of one confirmed partial book it once (Codex CORE-01, 2026-10-07).

backfill_unbooked_derisk read "already booked?" and then inserted, as two statements in Python's
implicit transaction, which only begins at the INSERT: two operator --confirm runs could both see
no fill for the same tx and both insert (the audit's probe: one 25-unit sale became two rows, 50).
The check and the insert are now one write transaction (BEGIN IMMEDIATE when the connection has
none) and the insert itself is guarded (INSERT ... WHERE NOT EXISTS) for a caller already in one.
Two real WAL connections here; the barrier sits inside the dedup read, where the race was.
"""
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from sniper.persistence import db_memecoin as m


def _db(tmp_path):
    path = str(Path(tmp_path) / "ledger.db")
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,is_open) "
              "VALUES(1,'M','8453','real',1)")
    c.execute("INSERT INTO memecoin_positions(id,mint,network_id,execution_mode,is_open) "
              "VALUES(2,'P','8453','paper',1)")
    c.commit()
    c.close()
    return path


def _racing_connection(path, gate):
    class Conn(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            cur = super().execute(sql, parameters)
            if sql.startswith("SELECT 1 FROM memecoin_fills"):
                row = cur.fetchone()
                cur.close()
                try:
                    gate.wait(timeout=0.5)       # both callers inside the read at once, if they can
                except threading.BrokenBarrierError:
                    pass
                return SimpleNamespace(fetchone=lambda: row)
            return cur
    return sqlite3.connect(path, factory=Conn, timeout=5)


def _race(path, sigs):
    gate, results, errors = threading.Barrier(len(sigs)), [], []

    def book(sig):
        conn = _racing_connection(path, gate)
        try:
            results.append(m.backfill_unbooked_derisk(conn, position_id=1, sig=sig, qty=25,
                                                      fraction=0.25, at="2026-10-07T00:00:00Z"))
        except Exception as exc:
            errors.append(repr(exc))
        finally:
            conn.close()
    threads = [threading.Thread(target=book, args=(s,)) for s in sigs]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    c = sqlite3.connect(path)
    rows = c.execute("SELECT tx_sig, qty FROM memecoin_fills ORDER BY id").fetchall()
    c.close()
    return results, errors, rows


def test_concurrent_backfills_of_one_tx_book_it_once(tmp_path):
    """THE regression: two rows, 50 units, for one 25-unit sale."""
    results, errors, rows = _race(_db(tmp_path), ["same-tx", "same-tx"])
    assert not errors, errors
    assert rows == [("same-tx", 25.0)], rows
    assert sorted(r is None for r in results) == [False, True]


def test_concurrent_backfills_of_different_txs_both_book(tmp_path):
    results, errors, rows = _race(_db(tmp_path), ["tx-a", "tx-b"])
    assert not errors and sorted(r[0] for r in rows) == ["tx-a", "tx-b"]


def test_a_sequential_rerun_is_harmless(tmp_path):
    path = _db(tmp_path)
    c = sqlite3.connect(path)
    first = m.backfill_unbooked_derisk(c, position_id=1, sig="t", qty=25, fraction=0.25, at="x")
    again = m.backfill_unbooked_derisk(c, position_id=1, sig="t", qty=25, fraction=0.25, at="x")
    assert first is not None and again is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 1


def test_bad_inputs_create_no_row(tmp_path):
    path = _db(tmp_path)
    c = sqlite3.connect(path)
    for kw in (dict(position_id=2, qty=25), dict(position_id=99, qty=25), dict(position_id=1, qty=0),
               dict(position_id=1, qty=float("nan")), dict(position_id=1, qty=float("inf"))):
        assert m.backfill_unbooked_derisk(c, sig="t", fraction=0.25, at="x", **kw) is None, kw
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 0
    assert not c.in_transaction, "no write transaction left open"


def test_a_caller_already_in_a_transaction_still_cannot_double_book(tmp_path):
    path = _db(tmp_path)
    c = sqlite3.connect(path)
    c.execute("INSERT INTO memecoin_fills(position_id,kind,qty,tx_sig,created_at) VALUES(1,'derisk',25,'t','x')")
    assert c.in_transaction
    assert m.backfill_unbooked_derisk(c, position_id=1, sig="t", qty=25, fraction=0.25, at="x") is None


def test_the_insert_guard_alone_refuses_a_row_the_read_missed(tmp_path):
    """Gamma: each guard on its own. The dedup read is made to miss a committed row; the guarded
    INSERT still adds nothing."""
    path = _db(tmp_path)
    first = sqlite3.connect(path)
    m.backfill_unbooked_derisk(first, position_id=1, sig="t", qty=25, fraction=0.25, at="x")

    class Blind(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT 1 FROM memecoin_fills"):
                return SimpleNamespace(fetchone=lambda: None)
            return super().execute(sql, parameters)
    blind = sqlite3.connect(path, factory=Blind)
    assert m.backfill_unbooked_derisk(blind, position_id=1, sig="t", qty=25, fraction=0.25, at="x") is None
    assert first.execute("SELECT COUNT(*) FROM memecoin_fills").fetchone()[0] == 1


def test_the_write_lock_is_taken_before_the_dedup_read(tmp_path):
    path = _db(tmp_path)
    c = sqlite3.connect(path)
    sqls = []
    c.set_trace_callback(sqls.append)
    m.backfill_unbooked_derisk(c, position_id=1, sig="t", qty=25, fraction=0.25, at="x")
    first_read = next(i for i, q in enumerate(sqls) if q.startswith("SELECT 1 FROM memecoin_fills"))
    assert "BEGIN IMMEDIATE" in sqls[:first_read], sqls
