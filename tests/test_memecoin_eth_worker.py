import threading
import time
from types import SimpleNamespace

from sniper.services.memecoin_eth_worker import EthereumWorker


def test_eth_receipt_wait_never_holds_other_chains_and_worker_owns_connection():
    waiting, release = threading.Event(), threading.Event()
    seen = []
    class Conn:
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
    owned = Conn()
    def hook(conn, ev):
        if ev.network_id == "1":
            seen.append((conn, ev))
            waiting.set()
            assert release.wait(2)
        return {"status": "checked"}
    worker = EthereumWorker(hook, connect=lambda: owned, tick=lambda conn: None, maxsize=1)
    worker.start()
    source = SimpleNamespace(network_id="1", mint="coin", metadata={"source": 1})
    try:
        assert worker(None, source)["status"] == "queued"
        assert waiting.wait(1)
        source.metadata["source"] = 2
        timings = []
        for _ in range(20):
            started = time.monotonic()
            assert worker(None, SimpleNamespace(network_id="56"))["status"] == "checked"
            timings.append(time.monotonic() - started)
        assert max(timings) < 0.05
        assert seen[0][0] is owned and seen[0][1].metadata == {"source": 1}
        assert worker(None, source)["status"] == "queued"
        assert worker(None, source) == {"status": "rejected", "reason": "eth_worker_queue_full"}
    finally:
        release.set()
        worker.stop()


def test_reservation_recovery_never_mutates_another_workers_chain():
    import sqlite3
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_real_runner import recover_stale_reservations
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    ids = {chain: db.reserve_pending(conn, mint="0x" + chain.zfill(40), source_user_id="source",
        strategy="alpha_b", notional=20, reserved_at="2026-10-06T10:00:00Z", network_id=chain)
        for chain in ("1", "56")}
    actions = recover_stale_reservations(conn, confirm_for=lambda *_: "unavailable",
        now_iso="2026-10-06T11:00:00Z", network_ids={"56"})
    assert actions == [(ids["56"], "released_never_broadcast")]
    assert db.pending_row(conn, ids["1"])["status"] == "reserved"


def test_eth_retry_attempt_outside_owner_preserves_durable_row_without_execution():
    calls = []
    def hook(conn, ev):
        calls.append(threading.current_thread().name)
        return {'status': 'filled'}
    hook.attempt = hook
    worker = EthereumWorker(hook, connect=lambda: None, tick=lambda _: None)
    assert worker.attempt(None, SimpleNamespace(network_id='1'))['status'] == 'duplicate_delivery'
    assert calls == []
    assert worker.attempt(None, SimpleNamespace(network_id='56'))['status'] == 'filled'


def test_exit_owner_scope_excludes_other_chain_before_price_work(monkeypatch):
    import sqlite3
    from sniper.persistence import db_memecoin as db
    from sniper.services import memecoin_real_runner as runner
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    for chain in ('1', '56'):
        pid = db.open_position(conn, source_user_id='source', source_handle='source',
            mint='0x' + chain.zfill(40), ticker='T', entry_price=1, entry_notional=20,
            entry_fill_source='chain', entry_lag_s=None, their_size_usd=None,
            opened_at='2026-10-06T10:00:00Z', strategy=runner.cfg.MEMECOIN_REAL_POLICY,
            execution_mode='real', network_id=chain)
        db.insert_fill(conn, position_id=pid, kind='entry', price=1, qty=20, notional=20,
            fill_source='chain', fraction=1, created_at='2026-10-06T10:00:00Z')
    priced = []
    runner.real_exit_sweep(conn, {}, price_fn=lambda mints: priced.extend(mints) or {},
        now_fn=lambda: '2026-10-06T10:00:01Z', network_ids={'1'})
    assert priced == ['0x' + '1'.zfill(40)]


def test_eth_sweep_preserves_other_owners_unsigned_close_claim():
    import sqlite3
    from sniper.persistence import db_memecoin as db
    from sniper.services import memecoin_real_runner as runner
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pid = db.open_position(conn, source_user_id='source', source_handle='source',
        mint='So11111111111111111111111111111111111111112', ticker='T',
        entry_price=1, entry_notional=20, entry_fill_source='chain', entry_lag_s=None,
        their_size_usd=None, opened_at='2026-10-06T09:00:00Z',
        strategy=runner.cfg.MEMECOIN_REAL_POLICY, execution_mode='real', network_id='solana')
    claim = db.claim_position_close(conn, pid, '2026-10-06T09:00:00Z', 'manual')
    runner.real_exit_sweep(conn, {'1': {}}, price_fn=lambda _: {},
        now_fn=lambda: '2026-10-06T10:00:00Z', network_ids={'1'})
    assert db.get_position(conn, pid)['close_claim_id'] == claim
    runner.recover_unverified_closes(conn, {}, '2026-10-06T10:00:00Z', network_ids={'solana'})
    assert db.get_position(conn, pid)['close_claim_id'] is None
