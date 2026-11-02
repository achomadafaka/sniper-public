"""Each network owns its cursor/connection/wake; a slow hook cannot block another chain."""
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as db
from sniper.services import memecoin_onchain_lane as lane

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _fixture(tmp_path):
    path = tmp_path / 'lanes.db'
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(c)
    for chain, sig, age in [('solana', 'slow', 1), ('4663', 'fast', 1), ('4663', 'stale', 100)]:
        stamp = (NOW - timedelta(seconds=age)).isoformat()
        db.record_onchain_detection(c, signature=sig, source_user_id='u', handle='h', address='owner',
                                    mint='coin', network_id=chain, detected_at=stamp, created_at=stamp)
    return path, c


def test_startup_and_sweeps_scope_the_cursor_to_one_network(tmp_path):
    _, c = _fixture(tmp_path)
    cursor = lane.startup_cursor(c, now_fn=lambda: NOW.isoformat(), max_age_s=45, network_id='solana')
    assert cursor == 0  # another chain's newer stale id must not hide our fresh row
    calls = []
    cursor, _ = lane.sweep_onchain_detections(c, lambda conn, ev: calls.append(ev.fomo_event_id),
        cursor=cursor, now_fn=lambda: NOW.isoformat(), max_age_s=45, network_id='solana',
        approved_ids_fn=lambda c, n: {'u'})
    assert calls == ['onchain:slow'] and cursor == 1


# Upper bounds, never sleeps: each wait returns the moment its event or phase arrives. They are wide
# so host load cannot fail the test (it did: 'sweeping' seen where 'idle' was due, 2026-10-17), and
# the slow hook holds longer than all of them together, so "the slow chain is still in its hook"
# stays true for the whole check.
WAIT_S = 5.0
SLOW_HOLD_S = 30.0


def _until(predicate, timeout=WAIT_S):
    """True once `predicate()` holds, polled every 5 ms up to `timeout` seconds; False if it never did."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(.005)
    return True


def test_a_slow_chain_cannot_block_another_or_consume_its_wake(tmp_path, monkeypatch):
    path, c = _fixture(tmp_path)
    monkeypatch.setattr(db, 'real_approved_user_ids', lambda conn, network_id=None: {'u'})
    slow, release, fast, stop = (threading.Event() for _ in range(4))
    seen = []
    def hook(conn, ev):
        if ev.network_id == 'solana':
            slow.set()
            assert release.wait(SLOW_HOLD_S)
        else:
            fast.set()
        seen.append((ev.network_id, ev.fomo_event_id))
        return {'status': 'filled'}
    def factory():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    wakes, status, threads = lane.start_network_workers(('solana', '4663'), conn_factory=factory,
        real_hook=hook, now_fn=lambda: NOW.isoformat(), max_age_s=45, poll_s=1, stop=stop)
    try:
        assert slow.wait(WAIT_S) and fast.wait(WAIT_S)
        # The fast hook has run; its sweep commits and goes idle after the hook returns.
        assert _until(lambda: status['4663']['phase'] == 'idle'), status['4663']
        assert status['solana']['phase'] == 'sweeping' and not release.is_set()
        db.record_onchain_detection(c, signature='second-fast', source_user_id='u', handle='h',
            address='owner', mint='coin2', network_id='4663', detected_at=NOW.isoformat(), created_at=NOW.isoformat())
        fast.clear()
        lane.fanout_network_wake(wakes)
        assert fast.wait(WAIT_S)  # wakes the fast network while the slow network remains in its hook
        assert not release.is_set() and status['solana']['phase'] == 'sweeping'
        release.set()
        assert _until(lambda: len(seen) >= 3), seen
        assert sorted(seen) == [('4663', 'onchain:fast'), ('4663', 'onchain:second-fast'), ('solana', 'onchain:slow')]
    finally:
        release.set(); stop.set(); lane.fanout_network_wake(wakes)
        for t in threads: t.join(2)
        c.close()
    assert all(not t.is_alive() for t in threads)


def test_heartbeat_does_not_refresh_a_stuck_workers_timestamp(tmp_path):
    path = tmp_path / 'lane.heartbeat'
    lane.write_lane_heartbeat(cursor=4, now_iso=NOW.isoformat(), path=path,
        workers={'solana': {'ts': '2026-10-02T11:59:00+00:00', 'cursor': 1, 'phase': 'sweeping'},
                 '4663': {'ts': NOW.isoformat(), 'cursor': 4, 'phase': 'idle'}})
    heartbeat = json.loads(path.read_text())
    assert heartbeat['ts'] == '2026-10-02T11:59:00+00:00'
    assert heartbeat['workers']['solana']['phase'] == 'sweeping'
