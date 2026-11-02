"""Storage stress: 100k book events — volume, idempotency, restart recovery, deterministic replay."""
from __future__ import annotations

import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.book_state import BookState

N = 100_000


def _insert_event(conn, i, *, commit):
    return P.append_book_event(
        conn, condition_id="0xc", token_id="tok", side="Up", event_type="price_change",
        exchange_ts=1_000_000 + i, recv_ts=1_000_000 + i, reconnect_generation=0,
        quality_flags=[], raw_payload={"price_changes": [{"price": "0.50", "size": str(i % 7)}]},
        now_ts=1_000_000 + i, commit=commit)


def test_100k_events_volume_idempotency_and_restart(tmp_path):
    path = os.path.join(str(tmp_path), "stress.sqlite")
    conn = P.connect(path)
    P.ensure_schema(conn)

    for i in range(N):
        _insert_event(conn, i, commit=False)   # batch: one commit at the end
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_book_events").fetchone()[0] == N

    # idempotency: replaying the first 100 events inserts nothing new
    for i in range(100):
        assert _insert_event(conn, i, commit=True) is False
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_book_events").fetchone()[0] == N

    # storage-health probe is sane
    assert P.db_size_bytes(conn) > 0

    # restart recovery: reopen the DB (WAL) and the rows are still there
    conn.close()
    conn2 = P.connect(path)
    P.ensure_schema(conn2)
    assert conn2.execute("SELECT COUNT(*) FROM crypto5m_book_events").fetchone()[0] == N


def test_book_reconstruction_is_deterministic():
    # the same ordered event stream must rebuild an identical book (bounded, replayable)
    events = [{"price": f"0.{p:02d}", "size": str(p * 3), "side": "BUY"} for p in range(10, 40)]
    events += [{"price": f"0.{p:02d}", "size": str(p), "side": "SELL"} for p in range(60, 90)]

    def _rebuild():
        b = BookState(token_id="t", condition_id="c", side="Up")
        for e in events:
            b.apply_price_change([e])
        return b

    a, b = _rebuild(), _rebuild()
    assert a.snapshot_levels() == b.snapshot_levels()
    assert a.best_bid() == b.best_bid() and a.best_ask() == b.best_ask()
