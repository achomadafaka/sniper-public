"""J7 relays carry a LOCAL monotonic receipt stamp, so an arrival comparison never reads a
provider's own clock as a clock (design 2026-09-16, section 4.2 and Codex's 'honest receipt clock').
"""
import sqlite3

import pytest

from sniper.feeds import j7_feed
from sniper.persistence.db_axiom import ensure_axiom_tables, record_j7_receipts


def _event(event_id="swap-1", received_at=1789560000000):
    return {"kind": "trade", "received_at": received_at,
            "data": {"id": event_id, "userId": "u1", "side": "buy", "usdAmount": 100.0,
                     "timestamp": "2026-09-16T12:00:00.000Z", "source": "j7",
                     "token": {"address": "MINT1", "networkId": "solana"}}}


def test_the_item_carries_a_local_monotonic_stamp():
    feed = j7_feed.J7WalletsFeed("tok")
    stamps = iter([111_000_000, 222_000_000])
    feed._mono_fn = lambda: next(stamps)
    feed.observe(_event())
    item = feed.drain()[0][0]
    assert item["received_mono_ns"] == 111_000_000


def test_two_relays_keep_their_own_arrival_order_whatever_the_server_says():
    """The server stamp of the SECOND relay is older; the local clock must not follow it."""
    feed = j7_feed.J7WalletsFeed("tok")
    stamps = iter([500_000_000, 550_000_000])
    feed._mono_fn = lambda: next(stamps)
    feed.observe(_event("swap-1", received_at=1789560000000))
    feed.observe(_event("swap-2", received_at=1789559000000))     # server clock goes backwards
    items, _epoch = feed.drain()
    assert [i["received_mono_ns"] for i in items] == [500_000_000, 550_000_000]
    assert items[1]["received_mono_ns"] - items[0]["received_mono_ns"] == 50_000_000


def test_receipts_are_buffered_for_a_writer_and_taken_once():
    feed = j7_feed.J7WalletsFeed("tok")
    feed._mono_fn = lambda: 777_000_000
    feed.observe(_event("swap-9"))
    rows = feed.take_receipts()
    assert [(r["fomo_event_id"], r["received_mono_ns"]) for r in rows] == [("swap-9", 777_000_000)]
    assert feed.take_receipts() == []


def test_a_non_trade_relay_buffers_nothing():
    feed = j7_feed.J7WalletsFeed("tok")
    feed._mono_fn = lambda: 1
    feed.observe({"kind": "presence", "data": {}})
    assert feed.take_receipts() == []


def test_the_buffer_is_bounded_and_drops_the_oldest():
    feed = j7_feed.J7WalletsFeed("tok", receipt_buffer=3)
    for i in range(5):
        feed._mono_fn = lambda i=i: i
        feed.observe(_event("swap-%d" % i))
    assert [r["fomo_event_id"] for r in feed.take_receipts()] == ["swap-2", "swap-3", "swap-4"]


def test_the_writer_stores_receipts_and_ignores_a_replay():
    conn = sqlite3.connect(":memory:")
    ensure_axiom_tables(conn)
    rows = [{"fomo_event_id": "swap-1", "received_mono_ns": 10, "received_at": "2026-09-16T12:00:00+00:00"},
            {"fomo_event_id": "swap-2", "received_mono_ns": 20, "received_at": "2026-09-16T12:00:01+00:00"}]
    assert record_j7_receipts(conn, rows) == 2
    assert record_j7_receipts(conn, rows) == 0          # the same relay again keeps the FIRST stamp
    stored = conn.execute("SELECT fomo_event_id, received_mono_ns FROM j7_receipts "
                          "ORDER BY fomo_event_id").fetchall()
    assert stored == [("swap-1", 10), ("swap-2", 20)]


def test_a_failing_writer_never_costs_a_relay():
    """The stamp is a measurement. A storage failure must not touch the signal path."""
    feed = j7_feed.J7WalletsFeed("tok")
    feed._mono_fn = lambda: 5
    feed._receipt_writer = lambda rows: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    feed.observe(_event("swap-x"))
    feed.flush_receipts()
    assert feed.drain()[0][0]["id"] == "swap-x"


def test_receipt_identity_and_loss_are_explicit():
    import json
    feed = j7_feed.J7WalletsFeed('tok', receipt_buffer=1)
    event = _event('first')
    event['data']['signature'] = 'source-signature'
    feed.observe(event)
    first = feed.take_receipts()[0]
    identity = json.loads(first['identity_json'])
    assert identity == {'source_user_id': 'u1', 'chain': 'solana', 'mint': 'MINT1',
                        'side': 'buy', 'amount_usd': 100.0, 'tx_sig': 'source-signature'}
    assert first['clock_domain'] and first['capture_run_id']
    feed.observe(_event('second'))
    feed.observe(_event('third'))
    assert feed.status()['receipt_dropped'] == 1
    assert 'tx_sig' not in json.loads(feed.take_receipts()[0]['identity_json'])


def test_blocked_writer_returns_promptly_and_retains_a_bounded_buffer():
    import threading
    import time
    started, release = threading.Event(), threading.Event()
    feed = j7_feed.J7WalletsFeed('tok', receipt_buffer=2)
    def writer(rows):
        started.set()
        release.wait(2)
        raise sqlite3.OperationalError('locked')
    feed._receipt_writer = writer
    feed.observe(_event('first'))
    before = time.monotonic()
    feed.flush_receipts()
    elapsed = time.monotonic() - before
    try:
        assert elapsed < 0.1
        assert started.wait(1)
        for i in range(4):
            feed.observe(_event(str(i)))
            feed.flush_receipts()
        assert feed.status()['receipt_dropped'] == 2
        assert feed.status()['receipt_writer_active'] is True
    finally:
        release.set()
    feed._receipt_thread.join(1)
    assert feed.status()['receipt_write_failures'] == 1
    assert feed.status()['receipt_failed_rows'] == 1
    assert len(feed.drain()[0]) == 5


def test_default_writer_initializes_schema_once_off_the_loop(tmp_path, monkeypatch):
    from sniper.persistence import db_axiom
    from sniper.util import state_paths
    path = tmp_path / 'receipts.db'
    monkeypatch.setenv('DB_PATH', str(path))
    original = db_axiom.ensure_axiom_tables
    calls = []
    def ensure(conn):
        calls.append(1)
        original(conn)
    monkeypatch.setattr(db_axiom, 'ensure_axiom_tables', ensure)
    feed = j7_feed.J7WalletsFeed('tok')
    for i in range(2):
        feed.observe(_event(str(i)))
        feed.flush_receipts()
        feed._receipt_thread.join(1)
    assert calls == [1]
    assert feed.status()['receipt_written'] == 2
    with sqlite3.connect(path) as conn:
        rows = conn.execute('SELECT clock_domain, capture_run_id, identity_json FROM j7_receipts').fetchall()
    assert len(rows) == 2 and all(all(row) for row in rows)


def test_receipt_wall_stamp_uses_the_shared_corrected_clock(monkeypatch):
    from sniper.util import clock
    corrected = '2026-09-16T10:00:00+00:00'
    monkeypatch.setattr(clock, 'real_utc_now_iso', lambda: corrected)
    feed = j7_feed.J7WalletsFeed('tok')
    feed.observe(_event())
    assert feed.take_receipts()[0]['received_at'] == corrected


def test_default_writer_respects_the_runner_database_override(tmp_path, monkeypatch):
    from sniper.util import state_paths
    configured = tmp_path / 'configured.db'
    wrong = tmp_path / 'state-default.db'
    monkeypatch.setenv('DB_PATH', str(configured))
    monkeypatch.setattr(state_paths, 'state_path', lambda name: wrong)
    feed = j7_feed.J7WalletsFeed('tok')
    feed.observe(_event())
    feed.flush_receipts()
    feed._receipt_thread.join(1)
    assert configured.exists() and not wrong.exists()
    with sqlite3.connect(configured) as conn:
        assert conn.execute('SELECT COUNT(*) FROM j7_receipts').fetchone()[0] == 1
