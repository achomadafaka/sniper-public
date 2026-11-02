"""Measurement cannot turn an ACK, foreign receipt or fork into a funded fill."""
import importlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_fill_timing as ft
from sniper.services.memecoin_research_pacer import SharedPacer
from sniper.util.receipt_clock import ReceiptClock

TX, OTHER, BLOCK = "0x" + "a" * 64, "0x" + "b" * 64, "0x" + "c" * 64


@pytest.fixture
def module():
    spec = importlib.util.find_spec("sniper.services.memecoin_bsc_execution_observation")
    assert spec is not None, "bounded BSC successful-receipt observer is missing"
    return importlib.import_module(spec.name)


def receipt(**changes):
    return dict(transactionHash=TX, blockNumber="0x64", blockHash=BLOCK,
                status="0x1", logs=[], **changes) if not changes else {
                    **receipt(), **changes}


def stamp():
    clock = ReceiptClock("fake")
    return clock.capture(monotonic_ns=1_000_000_000)


def dispatch(publisher, tx=TX, aid="a", t0=True):
    publisher.dispatch(aid, tx, stamp() if t0 else None)
    return publisher.ring.popleft()


@pytest.fixture
def observer(module, tmp_path):
    timing = ft.connect(tmp_path / "timing.db")
    now = [1_010_000_000]
    replies, calls = [], []
    def read(method, params):
        calls.append((method, params))
        return replies.pop(0)
    engine = module.ReceiptObserver(timing, read=read, budget=lambda: True,
                                    clock_ns=lambda: now[0], boot_id=stamp()["boot_id"])
    publisher = module.Publisher(generation="g", clock_ns=lambda: now[0])
    return engine, publisher, timing, replies, calls, now


def row(timing, aid="a"):
    timing.row_factory = sqlite3.Row
    return dict(timing.execute("SELECT * FROM memecoin_execution_observations WHERE attempt_id=?",
                               (aid,)).fetchone())


def head(engine, number=100, block=BLOCK):
    engine.observe_head({"number": hex(number), "hash": block})


def test_ack_is_not_success_null_then_delayed_inclusion(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    assert row(timing)["first_success_json"] is None
    replies.append(None)
    head(engine)
    assert row(timing)["first_success_json"] is None
    now[0] = 1_150_000_000
    replies.append(receipt())
    head(engine, number=101, block=OTHER)
    got = json.loads(row(timing)["first_success_json"])
    assert got["receipt_elapsed_ms"] == 150
    assert got["block_hash"] == BLOCK
    assert row(timing)["validation_state"] == "pending"
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [receipt(status="0x0"), receipt(transactionHash=OTHER),
                                  receipt(blockHash=None), receipt(logs=None),
                                  receipt(logs=[{"removed": True}])])
def test_invalid_receipt_never_success(observer, bad):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    replies.append(bad)
    head(engine)
    assert row(timing)["first_success_json"] is None


def test_fork_rejected_then_same_hash_canonical_success(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    replies.append(receipt(blockHash=OTHER))
    head(engine)
    assert row(timing)["first_success_json"] is None
    replies.append(receipt())
    head(engine, number=101, block=OTHER)
    assert json.loads(row(timing)["first_success_json"])["block_hash"] == BLOCK


def test_missing_t0_observes_but_has_no_latency(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher, t0=False))
    replies.append(receipt())
    head(engine)
    assert json.loads(row(timing)["first_success_json"])["receipt_elapsed_ms"] is None


def test_bounded_hashes_requests_expiry_and_budget_skip(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    engine.admit(dispatch(publisher, OTHER, "b"))
    engine.admit(dispatch(publisher, BLOCK, "c"))
    assert row(timing, "c")["observation_state"] == "refused_capacity"
    engine.budget = lambda: False
    head(engine)
    assert calls == []
    engine.budget = lambda: True
    replies.extend([None, None])
    head(engine, number=101)
    assert len(calls) == 2
    head(engine, number=101)
    assert len(calls) == 2
    now[0] += 10_000_000_001
    head(engine, number=102)
    assert len(calls) == 2
    assert row(timing)["observation_state"] == "expired"


def test_ring_overflow_never_calls_wait_or_sender_io(module):
    publisher = module.Publisher(generation="g", clock_ns=lambda: 1)
    for index in range(100):
        publisher.record_signal({"signal_id": str(index), "eligibility": None})
    assert len(publisher.ring) == 32
    assert publisher.ring[0]["sequence"] == 69
    assert publisher.overwritten == 68
    publisher.record_outcome({"signal_id": "99", "reason": "busy", "eligibility": None})
    assert publisher.ring[-1]["kind"] == "outcome"


def test_later_validation_needs_booked_exact_canonical_hash(observer, module):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    replies.append(receipt())
    head(engine)
    publisher.booked("a", TX, receipt(blockHash=OTHER), 7)
    engine.admit(publisher.ring.popleft())
    assert module.validate_witness(timing, "a", TX, 7) == "mismatch"
    publisher.booked("a", TX, receipt(), 7)
    engine.admit(publisher.ring.popleft())
    assert module.validate_witness(timing, "a", OTHER, 7) == "mismatch"
    assert module.validate_witness(timing, "a", TX, 8) == "mismatch"
    assert module.validate_witness(timing, "a", TX, 7) == "verified"


def test_try_acquire_does_not_reserve_a_future_or_sleep(tmp_path):
    now, sleeps = [100.0], []
    pacer = SharedPacer(tmp_path / "pacer.json", gap_s=1.1,
                        clock=lambda: now[0], sleep=sleeps.append)
    assert hasattr(pacer, "try_acquire"), "observer needs a nonblocking shared read-budget check"
    assert pacer.try_acquire()
    before = json.loads((tmp_path / "pacer.json").read_text())
    assert not pacer.try_acquire()
    assert json.loads((tmp_path / "pacer.json").read_text()) == before
    now[0] += 1.1
    assert pacer.try_acquire()
    pacer.pause(60)
    assert not pacer.try_acquire()
    assert sleeps == []


def test_authenticated_ipc_requires_cached_pid_uid_generation(module):
    event = {'generation': 'g', 'sequence': 1, 'kind': 'dispatch'}
    data = json.dumps(event).encode()
    assert module.authenticated_event(data, (44, 1000, 1000), (44, 1000, 'g')) == event
    assert module.authenticated_event(data, (45, 1000, 1000), (44, 1000, 'g')) is None
    assert module.authenticated_event(data, (44, 1001, 1000), (44, 1000, 'g')) is None
    assert module.authenticated_event(data, (44, 1000, 1000), (44, 1000, 'new')) is None
    assert module.authenticated_event(data, None, None) is None


def test_native_send_publishes_after_binding_before_sender_call(module, monkeypatch):
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_rh_fast_lane import LiveLane
    publisher = module.Publisher(generation='g')
    for i in range(32):
        publisher.record_signal({'signal_id': str(i)})
    bound = []
    monkeypatch.setattr(db, 'bind_entry_submission', lambda *a, **k: bound.append(k) or True)
    def post(raw, payload):
        assert bound
        assert publisher.ring[-1]['kind'] == 'dispatch'
        assert publisher.ring[-1]['tx_hash'] == TX
        assert publisher.ring[-1]['receipt_clock'] == payload['receipt_clock']
        assert publisher.ring[-1]['route_lane'] == 'native'
        return 'accepted', TX
    lane = SimpleNamespace(ctx={'lane_claim_nonce_fn': lambda n: 7,
                               'sign_tx_fn': lambda tx: ('0xraw', TX),
                               'bsc_execution_observer': publisher},
                           CHAIN_ID=56, BACKEND='bsc_fast_lane', send_times=[],
                           _gas_price=lambda snap: 1, _quote_asset_usd=lambda payload: 1,
                           _post=post, host_clock=lambda: 1,
                           _send_evidence=lambda p: {}, _fanout_allowed=lambda p: True)
    assert LiveLane._send(lane, {'receipt_clock': stamp(), 'snapshot': {}},
                          {'attempt_id': 'a'}, None, 1,
                          {'to': 'addr', 'data': '0x', 'gas': 1}, 1, None) == TX


def test_shared_send_publishes_exact_durably_bound_swap(module, monkeypatch):
    from test_memecoin_evm_entry_executor import _c, _reserve, _seams, _QUOTE
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as db
    conn = _c()
    pending_id = _reserve(conn)
    seams, _ = _seams(quote=_QUOTE, sign=('0xraw', TX, 7), confirm='unknown')
    published = []
    def dispatch(tx, quote):
        assert db.pending_row(conn, pending_id)['tx_sig'] == TX
        published.append(tx)
    def send(raw):
        assert published == [TX]
        return TX
    seams['send_fn'] = send
    result = execute_entry_evm(conn, pending_id=pending_id, mint='0xtok', network_id='8453',
                              usd_amount=10, strategy='alpha_b', source_user_id='trader1',
                              now_iso='2026-07-09T00:00:00Z', sender_dispatch_fn=dispatch, **seams)
    assert result['status'] == 'unknown'


def test_same_exact_hash_is_one_read_per_head(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    engine.admit(dispatch(publisher, aid='b'))
    replies.append(receipt())
    head(engine)
    assert len(calls) == 1
    assert row(timing, 'b')['first_success_json'] is not None


def test_expiry_without_head_never_polls_receipts(observer):
    engine, publisher, timing, replies, calls, now = observer
    engine.admit(dispatch(publisher))
    now[0] += 10_000_000_001
    engine.expire()
    assert row(timing)['observation_state'] == 'expired'
    assert calls == []


def test_later_resolver_verifies_even_already_complete_timing(observer, module):
    from sniper.persistence import db_memecoin as db
    from scripts.research import fill_timing_resolver as resolver
    from datetime import datetime, timezone
    engine, publisher, timing, replies, calls, now = observer
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    engine.admit(dispatch(publisher))
    replies.append(receipt())
    head(engine)
    publisher.booked('a', TX, receipt(), 7)
    engine.admit(publisher.ring.popleft())
    ledger.execute("INSERT INTO memecoin_entry_attempts (attempt_id,network_id,tx_sig,terminal_status,"
                   "decision_started_at,terminal_reason,completed_at) VALUES "
                   "('a','56',?,'filled','2026-10-11T00:00:00Z','filled','2026-10-11T00:00:01Z')", (TX,))
    ledger.execute("INSERT INTO memecoin_fills (position_id,kind,attempt_id,tx_sig) "
                   "VALUES (7,'entry','a',?)", (TX,))
    ledger.commit()
    resolver.resolve_pass(ledger, timing, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert row(timing)['validation_state'] == 'verified'


def test_nonblocking_budget_skips_held_file_lock(tmp_path):
    import fcntl
    import os
    pacer = SharedPacer(tmp_path / 'pacer.json', sleep=lambda _: pytest.fail('must not sleep'))
    fd = os.open(str(pacer.path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert not pacer.try_acquire()
        assert not (tmp_path / 'pacer.json').exists()
    finally:
        os.close(fd)


def test_production_context_forwards_observer(monkeypatch, tmp_path, module):
    from test_memecoin_evm_context_keys import _production_context
    marker = module.Publisher(generation='g')
    monkeypatch.setattr(module, 'create_publisher', lambda chain: marker if str(chain) == '56' else None)
    assert _production_context(monkeypatch, tmp_path, '56')['bsc_execution_observer'] is marker


def test_native_no_attempt_is_retained_with_unknown_eligibility(module):
    from sniper.services.memecoin_bsc_fast_lane import BscLive
    publisher = module.Publisher(generation='g')
    live = BscLive.__new__(BscLive)
    live.ctx = {'bsc_execution_observer': publisher}
    live._handle = lambda payload, recv_epoch: {'reason': 'source_initiation_unknown'}
    payload = {'tx_hash': TX, 'mint': '0x' + '1' * 40, 'receipt_clock': stamp()}
    assert live.handle(payload, recv_epoch=1)['reason'] == 'source_initiation_unknown'
    events = list(publisher.ring)
    assert [event['kind'] for event in events] == ['signal', 'outcome']
    assert all(event['eligibility'] is None for event in events)
    assert events[0]['receipt_clock'] == payload['receipt_clock']


def test_view_exposes_only_later_verified_success(observer):
    from sniper.persistence import db_memecoin as db
    engine, publisher, timing, replies, calls, now = observer
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    engine.admit(dispatch(publisher))
    replies.append(receipt())
    head(engine)
    detail = ft._details(ledger, {'attempt_id': 'a', 'network_id': '56'}, timing, {}, False)
    assert detail.get('successful_receipt_observation_ms') is None
    timing.execute("UPDATE memecoin_execution_observations SET validation_state='verified'")
    detail = ft._details(ledger, {'attempt_id': 'a', 'network_id': '56'}, timing, {}, False)
    assert detail['successful_receipt_observation_ms'] == 10


@pytest.mark.parametrize('answer', [
    {'jsonrpc': '2.0', 'id': 2, 'result': None},
    {'jsonrpc': '2.0', 'id': 1, 'result': None, 'error': {'code': -1}},
    {'id': 1, 'result': None},
    {},
])
def test_rpc_rejects_wrong_identity_partial_and_result_with_error(module, answer):
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: answer)
    client = SimpleNamespace(post=lambda *a, **kw: response)
    pacer = SimpleNamespace(pause=lambda seconds: None)
    with pytest.raises(ValueError):
        module.read_rpc(client, 'https://fake', 'eth_getTransactionReceipt', [TX], pacer)


def test_publisher_start_failure_is_status_not_a_funded_exception(module, monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_BSC_EXECUTION_OBSERVER_ENABLED', True)
    monkeypatch.setenv('SNIPER_STATE_ROOT', str(tmp_path))
    monkeypatch.setattr(module, '_identity', lambda root: None)
    monkeypatch.setattr(module.Publisher, 'start', lambda self, *a, **kw: self)
    publisher = module.create_publisher('56')
    assert publisher.status()['ready'] is False
    assert publisher.status()['initialization_error'] == 'pinned_runner_identity_unavailable'


def test_multiple_contexts_share_runner_auth_without_sequence_collisions(module, tmp_path):
    one, two = module.Publisher(generation='g'), module.Publisher(generation='g')
    first, second = dispatch(one, aid='a'), dispatch(two, aid='b')
    assert first['sequence'] == second['sequence'] == 1
    assert first['generation'] != second['generation']
    assert first['runner_generation'] == second['runner_generation'] == 'g'
    timing = ft.connect(tmp_path / 'timing.db')
    engine = module.ReceiptObserver(timing, read=lambda *a: None, budget=lambda: False)
    engine.admit(first)
    engine.admit(second)
    assert timing.execute('SELECT count(*) FROM memecoin_execution_observer_events').fetchone()[0] == 2
    assert module.authenticated_event(json.dumps(first).encode(), (44, 1000, 1000),
                                      (44, 1000, 'g')) == first


def test_shared_hook_keeps_no_fill_refusal_unknown(module, monkeypatch):
    from test_memecoin_real_runner import _c, _Ev
    from sniper.services import memecoin_real_runner as runner
    publisher = module.Publisher(generation='g')
    monkeypatch.setattr(runner, 'real_entry_hook', lambda *a, **kw: {'status': 'not_approved'})
    conn = _c()
    event = _Ev('0x' + '1' * 40, created_at='2026-07-18T11:59:59+00:00')
    event.network_id, event.receipt_clock = '56', stamp()
    event.their_market_cap = 500_000
    hook = runner.make_real_hook(None, {'56': {'bsc_execution_observer': publisher}},
                                 now_fn=lambda: '2026-07-18T12:00:00+00:00',
                                 trade_size=10, max_age_s=120, blind_mcap_fn=lambda *a: None,
                                 resolve_fn=lambda *a: '56')
    hook(conn, event)
    assert publisher.ring[-1]['signal_id'] == event.fomo_event_id
    assert publisher.ring[-1]['eligibility'] is None


@pytest.mark.parametrize('failure', ['database', 'client'])
def test_tape_initialization_failure_is_visible_and_closes_resources(module, monkeypatch, tmp_path, failure):
    import requests
    observer = module.TapeObserver.__new__(module.TapeObserver)
    observer.root, observer.rpc_url = tmp_path, 'https://fake'
    observer.worker_error, observer.ready = None, False
    observer.thread = SimpleNamespace(is_alive=lambda: False)
    closed = []
    observer.server = SimpleNamespace(close=lambda: closed.append('socket'))
    timing = ft.connect(tmp_path / 'timing.db')
    if failure == 'database':
        monkeypatch.setattr(ft, 'connect', lambda: (_ for _ in ()).throw(OSError('unavailable')))
    else:
        monkeypatch.setattr(ft, 'connect', lambda: timing)
        monkeypatch.setattr(requests, 'Session', lambda: (_ for _ in ()).throw(RuntimeError('unavailable')))
    observer._run()
    assert observer.status()['ready'] is False
    assert observer.status()['worker_error'] == ('OSError' if failure == 'database' else 'RuntimeError')
    assert closed == ['socket']
    if failure == 'client':
        with sqlite3.connect(tmp_path / 'timing.db') as check:
            health = json.loads(check.execute('SELECT state_json FROM memecoin_execution_observer_health '
                                              "WHERE owner='tape'").fetchone()[0])
        assert not health['alive'] and health['worker_error'] == 'RuntimeError'
    else:
        timing.close()
