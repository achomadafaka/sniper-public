import json
import sqlite3
import threading
from dataclasses import asdict, replace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from test_memecoin_entry_retry_lane import _ev


def connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


@pytest.fixture
def conn(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_WINDOW_S", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_INTERVAL_S", 6.0)
    monkeypatch.setattr(R, "_notify_entry_projection", lambda **kw: None)
    monkeypatch.setattr(R, "_safe_alert", lambda *a, **kw: None)
    c = connection()
    yield c
    c.close()


def queue(conn, fid, network, at="2026-08-14T20:00:00+00:00"):
    ev = replace(_ev(fid), network_id=network)
    m.enqueue_entry_retry(conn, fid, ev.mint, network, json.dumps(asdict(ev)), at,
                          retry_after_s=6)


def test_due_and_expired_rows_stay_with_their_network(conn):
    for chain in ("4663", "solana"):
        queue(conn, chain + "-due", chain)
        queue(conn, chain + "-expired", chain, "2026-08-14T19:59:00+00:00")
    calls = []
    result = R.run_entry_retry_lane(conn, lambda c, e: calls.append(e.fomo_event_id) or
                                   {"status": "filled"}, "2026-08-14T20:00:07+00:00",
                                   network_id="4663", include_dips=False)
    assert calls == ["4663-due"] and result["gave_up"] == 1
    rows = m.due_entry_retries(conn, "2026-08-14T20:00:07+00:00", window_s=120,
                              max_attempts=12)
    assert sorted(x["fomo_event_id"] for x in rows) == ["solana-due", "solana-expired"]


def test_background_retry_runs_without_a_main_loop_pass(conn):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    queue(conn, "robin", "4663")
    calls = []
    workers = EntryRetryWorkers(["4663"], conn_factory=connection,
        hook=lambda c, e: calls.append(e.fomo_event_id) or {"status": "filled"},
        now_fn=lambda: "2026-08-14T20:00:07+00:00", clock=lambda: 100.0)
    assert workers.pass_network("4663", conn, background=True)["filled"] == 1
    assert calls == ["robin"]
    assert workers.fallback(conn, "2026-08-14T20:00:07+00:00")["retried"] == 0


def test_busy_solana_retry_never_blocks_robin_or_replays_a_row(conn):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    queue(conn, "robin", "4663")
    calls = []
    workers = EntryRetryWorkers(["solana", "4663"], conn_factory=connection,
        hook=lambda c, e: calls.append(e.fomo_event_id) or {"status": "filled"},
        now_fn=lambda: "2026-08-14T20:00:07+00:00", clock=lambda: 100.0)
    workers.states["solana"].lock.acquire()
    try:
        assert workers.fallback(conn, "2026-08-14T20:00:07+00:00")["filled"] == 1
        assert workers.pass_network("solana", conn, background=True)["retried"] == 0
    finally:
        workers.states["solana"].lock.release()
    assert calls == ["robin"]


def test_worker_exception_releases_ownership_and_main_can_cover(conn, monkeypatch):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    clock = [100.0]
    workers = EntryRetryWorkers(["4663"], conn_factory=connection, hook=lambda *a: None,
        now_fn=lambda: "2026-08-14T20:00:07+00:00", clock=lambda: clock[0])
    def broken(*a, **kw):
        raise RuntimeError("read failed")
    monkeypatch.setattr(R, "run_entry_retry_lane", broken)
    with pytest.raises(RuntimeError):
        workers.pass_network("4663", conn, background=True)
    assert not workers.states["4663"].lock.locked()
    assert not workers.status()["4663"]["healthy"]
    called = []
    monkeypatch.setattr(R, "run_entry_retry_lane", lambda *a, **kw:
        called.append(kw["network_id"]) or {"retried": 1, "filled": 0, "gave_up": 0})
    assert workers.fallback(conn, "now")["retried"] == 1
    assert called == ["4663"]


def test_real_worker_owns_its_connection_and_stops_cleanly():
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    entered = threading.Event()
    owners = []
    def factory():
        owners.append(threading.get_ident())
        entered.set()
        return connection()
    workers = EntryRetryWorkers(["4663"], conn_factory=factory, hook=lambda *a: None,
        now_fn=lambda: "2026-08-14T20:00:07+00:00")
    workers.start()
    try:
        assert entered.wait(3)
        assert owners[0] != threading.get_ident()
    finally:
        workers.stop()
    assert not any(t.is_alive() for t in workers.threads)


def test_robin_fills_while_the_solana_retry_is_still_inside_its_hook(conn, tmp_path):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    path = tmp_path / "retry.db"
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    queue(c, "sol-retry", "solana"); queue(c, "rh-retry", "4663")
    c.close()
    sol_busy, release_sol, robin_filled = threading.Event(), threading.Event(), threading.Event()
    def hook(c, ev):
        if ev.network_id == "solana":
            sol_busy.set()
            assert release_sol.wait(3)
        else:
            assert sol_busy.wait(2)
            robin_filled.set()
        return {"status": "filled"}
    def factory():
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        return c
    workers = EntryRetryWorkers(["solana", "4663"], conn_factory=factory, hook=hook,
        now_fn=lambda: "2026-08-14T20:00:07+00:00")
    workers.start()
    try:
        assert robin_filled.wait(2)
        assert not release_sol.is_set()
    finally:
        release_sol.set(); workers.stop()
    assert workers.status()["4663"]["filled"] == 1


def test_retry_worker_health_is_in_the_runner_heartbeat(tmp_path):
    from sniper.services.memecoin_runner import write_heartbeat
    path = tmp_path / "heartbeat.json"
    status = {"4663": {"healthy": True, "last_pass_age_s": 0.4, "busy": False,
                       "error": None, "retried": 1, "filled": 1}}
    write_heartbeat({}, auth_ok=True, path=path, entry_retry_workers=status)
    assert json.loads(path.read_text())["entry_retry_workers"] == status


def test_worker_wakes_on_existing_retry_deadline_without_an_extra_poll(conn):
    from datetime import datetime, timedelta
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    queue(conn, 'bsc-deadline', '56')
    now = [datetime.fromisoformat('2026-08-14T20:00:05.750000+00:00')]
    calls, waits = [], []
    class Stop:
        stopped = False
        def is_set(self):
            return self.stopped
        def set(self):
            self.stopped = True
        def wait(self, seconds):
            waits.append(seconds)
            now[0] += timedelta(seconds=seconds)
            return self.stopped
    stop = Stop()
    def hook(c, ev):
        calls.append((ev.fomo_event_id, now[0].isoformat()))
        stop.set()
        return {'status': 'filled'}
    workers = EntryRetryWorkers(['56'], conn_factory=lambda: conn, hook=hook,
        now_fn=lambda: now[0].isoformat())
    workers.stopping = stop
    workers._worker('56')
    assert waits[0] == pytest.approx(.25)
    assert calls == [('bsc-deadline', '2026-08-14T20:00:06+00:00')]


def test_retry_deadline_wait_excludes_other_chains_dips_and_already_due_rows(conn):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    now = '2026-08-14T20:00:05.750000+00:00'
    for fid, chain in [('bsc', '56'), ('robin', '4663'), ('dip', '56'), ('busy', '56')]:
        queue(conn, fid, chain)
    conn.execute("UPDATE memecoin_entry_retry_queue SET next_attempt_at=? WHERE fomo_event_id='robin'",
                 ('2026-08-14T20:00:05.800000+00:00',))
    conn.execute("UPDATE memecoin_entry_retry_queue SET dip_json='{}', next_attempt_at=? WHERE fomo_event_id='dip'",
                 ('2026-08-14T20:00:05.800000+00:00',))
    conn.execute("UPDATE memecoin_entry_retry_queue SET next_attempt_at=? WHERE fomo_event_id='busy'",
                 ('2026-08-14T20:00:05.000000+00:00',))
    conn.commit()
    workers = EntryRetryWorkers(['56'], conn_factory=connection, hook=lambda *a: None, now_fn=lambda: now)
    assert workers._next_wait('56', conn) == pytest.approx(.25)
    conn.execute("DELETE FROM memecoin_entry_retry_queue WHERE fomo_event_id='bsc'")
    assert workers._next_wait('56', conn) == workers.POLL_S


@pytest.mark.parametrize('stamp', ['not-a-clock', '2026-08-14T20:00:06'])
def test_malformed_or_unzoned_retry_deadline_keeps_bounded_poll(conn, stamp):
    from sniper.services.memecoin_entry_retry_workers import EntryRetryWorkers
    queue(conn, 'bad-clock', '56')
    conn.execute('UPDATE memecoin_entry_retry_queue SET next_attempt_at=?', (stamp,))
    workers = EntryRetryWorkers(['56'], conn_factory=connection, hook=lambda *a: None,
        now_fn=lambda: '2026-08-14T20:00:05.750000+00:00')
    assert workers._next_wait('56', conn) == workers.POLL_S
