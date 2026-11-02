"""Confirmation must not own admission; only exact receipts can settle the signed reservation."""
import sqlite3
import threading

import pytest

from sniper.execution.solana_slot_executor import execute_prebuilt_entry
from sniper.persistence import db_memecoin as m


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / 'ledger.db'
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    conn = connect()
    m.ensure_memecoin_tables(conn)
    conn.commit()
    yield conn, connect
    conn.close()


def dispatch(conn, confirm, **kwargs):
    pid = m.reserve_pending(conn, mint='MintA', source_user_id='wallet', strategy='alpha_b',
                            notional=20., reserved_at='2026-10-09T20:00:00+00:00',
                            network_id='solana', origin_event_id='source-A',
                            signal_source='slot_lane', source_price=.001)
    return execute_prebuilt_entry(
        conn, pending=m.pending_row(conn, pid), signed_b64='AA==', sig='sig-A', validity=None,
        send_fn=lambda _: True, confirm_fn=confirm,
        receipt_fill_fn=lambda *_: (20., 1500., 6),
        now_iso='2026-10-09T20:00:01+00:00', sleep_fn=lambda _: None,
        defer_confirmation=True, **kwargs)


def test_dispatch_returns_before_confirmation_and_settles_on_own_connection(ledger):
    conn, connect = ledger
    calls = []
    result = dispatch(conn, lambda sig: calls.append(sig) or 'confirmed')
    assert result['status'] == 'submitted'
    assert calls == []
    assert m.pending_row(conn, result['pending_id'])['status'] == 'reserved'
    result_holder = []
    def settle():
        with connect() as worker:
            result_holder.append(result['_settlement'].resolve(worker))
    thread = threading.Thread(target=settle)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result_holder[0]['status'] == 'filled'
    assert calls == ['sig-A']
    assert conn.execute('SELECT COUNT(*) FROM memecoin_positions').fetchone()[0] == 1


@pytest.mark.parametrize('status', ['unknown', 'failed'])
def test_deferred_unknown_stays_reserved_failed_releases(ledger, status):
    conn, _ = ledger
    result = dispatch(conn, lambda _: status)
    settled = result['_settlement'].resolve(conn)
    assert settled['status'] == status
    pending = m.pending_row(conn, result['pending_id'])
    assert pending['status'] == ('reserved' if status == 'unknown' else 'failed')
    assert conn.execute('SELECT COUNT(*) FROM memecoin_positions').fetchone()[0] == 0


def test_one_deferred_identity_can_only_resolve_once(ledger):
    conn, _ = ledger
    result = dispatch(conn, lambda _: 'confirmed')
    first = result['_settlement'].resolve(conn)
    assert result['_settlement'].resolve(conn) is first
    assert conn.execute('SELECT COUNT(*) FROM memecoin_fills').fetchone()[0] == 1


def test_two_background_slots_are_bounded_and_keep_debits_until_read_started_after_settlement():
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    jobs = []
    completed = []
    pool = SlotConfirmations(conn_factory=lambda: None, put=completed.append,
                             start_worker=lambda target: jobs.append(target))
    assert len(jobs) == 2
    result = {'status': 'submitted', 'pending_id': 1, '_settlement': object()}
    pool.offer('A', result)
    pool.offer('B', {**result, 'pending_id': 2})
    assert not pool.available()
    assert pool.snapshot_debits(9.) == (1, 2)
    pool.completed(1, 10.)
    assert pool.available()
    assert pool.snapshot_debits(9.999) == (1, 2)
    assert pool.snapshot_debits(10.) == (2,)
    assert pool.snapshot_debits(9.) == (1, 2)  # older ordinary prereads can finish after a new read


def test_bounded_cash_history_refuses_a_read_older_than_forgotten_settlement(monkeypatch):
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    monkeypatch.setattr(SlotConfirmations, 'MAX_RETAINED', 2)
    pool = SlotConfirmations(conn_factory=lambda: None, put=lambda _: None, start_worker=lambda _: None)
    def offer(pid):
        pool.offer(str(pid), {'pending_id': pid, '_settlement': object()})
    offer(1)
    pool.jobs.get_nowait()
    pool.completed(1, 10.)
    offer(2)
    pool.jobs.get_nowait()
    pool.completed(2, 20.)
    offer(3)
    with pytest.raises(ValueError, match='history'):
        pool.snapshot_debits(9.)
    assert pool.snapshot_debits(10.1) == (2, 3)


def test_connection_close_failure_does_not_lose_completion():
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    from sniper.execution.solana_slot_executor import DeferredSlotEntry
    callbacks = []
    class Connection:
        def close(self):
            raise RuntimeError('close failed')
    class Done(Exception):
        pass
    def post(result):
        callbacks.append(result)
        raise Done()
    pool = SlotConfirmations(conn_factory=Connection, put=post, start_worker=lambda _: None)
    pool.offer('A', {'pending_id': 1, '_settlement': DeferredSlotEntry(lambda _: {'status': 'unknown'})})
    with pytest.raises(Done):
        pool._work()
    assert callbacks[0].result['status'] == 'unknown'


def test_unknown_send_cash_stays_held_until_recovery_is_observed():
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    pool = SlotConfirmations(conn_factory=lambda: None, put=lambda _: None, start_worker=lambda _: None)
    pool.offer('A', {'pending_id': 1, '_settlement': object(), '_gas_debit_lamports': 120000})
    pool.completed(1, 10., status='unknown')
    assert pool.snapshot_funding(20.) == {'pending_ids': (1,), 'extra_sol_atomic': 120000}
    assert pool.unresolved == frozenset({1})
    pool.recovered(1, 30.)
    assert pool.snapshot_debits(29.999) == (1,)
    assert pool.snapshot_funding(30.) == {'pending_ids': (), 'extra_sol_atomic': 0}


def test_unknown_history_capacity_is_checked_before_another_send(monkeypatch):
    from sniper.services.memecoin_slot_lane import SlotConfirmations
    monkeypatch.setattr(SlotConfirmations, 'MAX_RETAINED', 2)
    pool = SlotConfirmations(conn_factory=lambda: None, put=lambda _: None, start_worker=lambda _: None)
    for pid in (1, 2):
        pool.offer(str(pid), {'pending_id': pid, '_settlement': object()})
        pool.jobs.get_nowait()
        pool.completed(pid, 10., status='unknown')
    assert not pool.available()


@pytest.mark.parametrize('wrapper_fails', [False, True])
def test_live_lane_releases_wallet_after_dispatch_and_finishes_trace_after_receipt(
        ledger, tmp_path, monkeypatch, wrapper_fails):
    import time
    from test_memecoin_slot_lane import _buy, _snap
    from sniper.services import memecoin_slot_lane as lane
    from sniper.execution.exec_lock import wallet_exec_lock
    from sniper.execution import exec_lock
    conn, connect = ledger
    monkeypatch.setattr(exec_lock, '_lock_path', lambda key, lock_dir=None: str(tmp_path/'wallet.lock'))
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_FUNDED_REGIME_RISK_SCALE', 1.)
    monkeypatch.setattr(lane.cfg, 'MEMECOIN_REAL_MAX_MCAP_USD', 250000.)
    calls = []
    pool = lane.SlotConfirmations(conn_factory=connect, put=lambda _: None, start_worker=lambda _: None)
    def execute(c, pending, message, **kwargs):
        assert kwargs.pop('defer_confirmation', False), 'native lane still blocks for confirmation'
        return execute_prebuilt_entry(
            c, pending=pending, signed_b64='AA==', sig='sig-A', validity=None,
            send_fn=lambda _: True, confirm_fn=lambda sig: calls.append(sig) or 'confirmed',
            receipt_fill_fn=lambda *_: (pending['reserved_notional_usd'], 1500., 6),
            now_iso=kwargs['now_iso'],
            trace=kwargs['trace'], defer_confirmation=True)
    live = lane.SlotLane(
        conn=conn, executor={'slot_execute_fn': execute}, build_fn=lambda *_: ('MESSAGE', {}),
        snapshot_fn=lambda: _snap(time.monotonic()), armed_ids_fn=lambda: {'w1'},
        kill_tripped_fn=lambda: False, now_iso_fn=lambda: '2026-10-09T20:00:00+00:00',
        lock_key='test-deferred', mode='live', confirmations=pool)
    if wrapper_fails:
        def fail_after_send(*_):
            raise RuntimeError('wrapper failed after durable dispatch')
        monkeypatch.setattr(live, '_routed_fallback', fail_after_send)
    result = live.handle(_buy())
    assert result['status'] == 'submitted', result
    assert not calls
    assert pool.snapshot_debits(time.monotonic()) == (result['pending_id'],)
    assert pool.active == {result['pending_id']}
    assert pool.jobs.empty()  # outer handle wrappers still own the result metadata
    result['exotic'] = {'route': 'test'}
    pool.offer('A', result)
    _, frozen = pool.jobs.get_nowait()
    result['later'] = True
    assert frozen['exotic'] == {'route': 'test'} and 'later' not in frozen
    assert m.pending_row(conn, result['pending_id'])['tx_sig'] == 'sig-A'
    assert conn.execute('SELECT terminal_status FROM memecoin_entry_attempts').fetchone()[0] is None
    with wallet_exec_lock('test-deferred', blocking=False) as held:
        assert held
    worker = connect()
    settled = result['_settlement'].resolve(worker)
    worker.close()
    final = result['_complete']({**settled, 'pending_id': result['pending_id']})
    assert final['status'] == 'filled'
    assert conn.execute('SELECT terminal_status FROM memecoin_entry_attempts').fetchone()[0] == 'filled'


@pytest.mark.parametrize('persisted', [True, False])
@pytest.mark.parametrize('status', ['filled', 'failed', 'unknown', 'submitted_unknown'])
def test_deferred_note_changes_only_after_successful_terminal_trace(persisted, status):
    from types import SimpleNamespace
    from sniper.services import memecoin_slot_lane as lane, memecoin_slot_skips as skips
    skips.note('finish-fixture', 'submitted', attempt_id='attempt')
    calls = []
    live = object.__new__(lane.SlotLane)
    live.clock = SimpleNamespace(perf_counter=lambda: 1.)
    live._record_classification = lambda _: None
    live.wake = SimpleNamespace(set=lambda: calls.append(skips.decision_for('finish-fixture')))
    trace = SimpleNamespace(finish=lambda *a, **k: persisted)
    lane.SlotLane._finish_traced(live, SimpleNamespace(signature='finish-fixture'),
        dict(status=status, reason='done'), trace, dict(mint='M'), 0., expected_note_attempt='attempt')
    expected = (status, 'done') if persisted and status != 'submitted_unknown' else ('submitted', None)
    assert calls == [expected]
