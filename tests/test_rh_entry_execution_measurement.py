"""RH measurement reuses existing successful reads, never approvals, ACKs or head polling."""
import json
import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_bsc_execution_observation as O
from sniper.services import memecoin_fill_timing as ft
from sniper.util.receipt_clock import ReceiptClock

TX, OTHER, BLOCK = '0x' + 'a' * 64, '0x' + 'b' * 64, '0x' + 'c' * 64
SHA = 'd' * 40


def receipt(**changes):
    return dict(transactionHash=TX, blockNumber='0x64', blockHash=BLOCK,
                status='0x1', logs=[]) | changes


def clock():
    return ReceiptClock('rh-test').capture(monotonic_ns=1_000_000_000)


def observation(timing):
    timing.row_factory = sqlite3.Row
    return dict(timing.execute('SELECT * FROM memecoin_execution_observations').fetchone())


@pytest.fixture
def engine(tmp_path):
    timing = ft.connect(tmp_path / 'timing.db')
    calls = []
    worker = O.ReceiptObserver(timing, chain='4663', read=lambda *a: calls.append(a),
                               budget=lambda: False, clock_ns=lambda: 1_400_000_000,
                               boot_id=clock()['boot_id'])
    publisher = O.Publisher(generation=SHA + ':1:2', chain='4663',
                            clock_ns=lambda: 1_100_000_000)
    yield timing, worker, publisher, calls
    timing.close()


def test_existing_confirmation_stamp_preserved_without_any_observer_read(engine):
    timing, worker, publisher, calls = engine
    publisher.dispatch('a', TX, clock(), route_lane='shared')
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, receipt(), observed_ns=1_150_000_000,
                       boot_id=clock()['boot_id'])
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, receipt(), observed_ns=1_300_000_000,
                       boot_id=clock()['boot_id'])
    worker.admit(publisher.ring.popleft())
    got = json.loads(observation(timing)['first_success_json'])
    assert got['chain_id'] == '4663'
    assert got['observation_kind'] == 'confirmation_observation'
    assert got['observed_ns'] == 1_150_000_000
    assert got['receipt_elapsed_ms'] == 150
    worker.observe_head({'number': '0x64', 'hash': BLOCK})
    assert calls == []


@pytest.mark.parametrize('bad', [receipt(status='0x0'), receipt(transactionHash=OTHER),
                                receipt(blockHash=None), receipt(logs=None), receipt(chainId='0x38'),
                                receipt(removed=True)])
def test_bad_confirmation_is_never_a_successful_witness(engine, bad):
    timing, worker, publisher, _ = engine
    publisher.dispatch('a', TX, clock())
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, bad, observed_ns=1_150_000_000, boot_id=clock()['boot_id'])
    worker.admit(publisher.ring.popleft())
    assert observation(timing)['first_success_json'] is None


def test_chain_mismatch_is_not_admitted(engine):
    timing, worker, publisher, _ = engine
    publisher.dispatch('a', TX, clock())
    event = publisher.ring.popleft()
    event['chain_id'] = '56'
    worker.admit(event)
    assert timing.execute('SELECT count(*) FROM memecoin_execution_observations').fetchone()[0] == 0


@pytest.mark.parametrize('anchor,boot', [(None, None), ('valid', 'other-boot')])
def test_missing_or_cross_boot_clocks_never_gain_latency(engine, anchor, boot):
    timing, worker, publisher, _ = engine
    publisher.dispatch('a', TX, clock() if anchor else None)
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, receipt(), observed_ns=1_150_000_000,
                       boot_id=boot or clock()['boot_id'])
    worker.admit(publisher.ring.popleft())
    assert json.loads(observation(timing)['first_success_json'])['receipt_elapsed_ms'] is None


@pytest.mark.parametrize('boot', [None, '', False, 0, []])
def test_absent_observation_boot_preserves_success_with_unknown_latency(engine, boot):
    timing, worker, publisher, calls = engine
    publisher.dispatch('a', TX, clock())
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, receipt(), observed_ns=1_150_000_000, boot_id=boot)
    worker.admit(publisher.ring.popleft())
    got = json.loads(observation(timing)['first_success_json'])
    assert got['tx_hash'] == TX and got['block_hash'] == BLOCK
    assert got['receipt_elapsed_ms'] is None
    assert calls == []


def test_canonical_booking_requires_same_chain_status_hash_and_real_position(engine):
    from sniper.persistence import db_memecoin as db
    timing, worker, publisher, _ = engine
    publisher.dispatch('a', TX, clock())
    worker.admit(publisher.ring.popleft())
    publisher.observed('a', TX, receipt(), observed_ns=1_150_000_000, boot_id=clock()['boot_id'])
    worker.admit(publisher.ring.popleft())
    publisher.booked('a', TX, receipt(), 7)
    worker.admit(publisher.ring.popleft())
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO memecoin_entry_attempts "
                   "(attempt_id,network_id,tx_sig,terminal_status,terminal_reason,completed_at,decision_started_at) "
                   "VALUES ('a','4663',?,'filled','filled','2026-10-11T20:00:01+00:00',"
                   "'2026-10-11T20:00:00+00:00')", (TX,))
    ledger.execute("INSERT INTO memecoin_fills (position_id,kind,attempt_id,tx_sig,network_id) "
                   "VALUES (7,'entry','a',?,'4663')", (TX,))
    ledger.execute('UPDATE memecoin_fills SET qty=0,notional=1')
    ledger.execute("INSERT INTO memecoin_positions (id,mint,network_id,execution_mode) "
                   "VALUES (7,'token','4663','paper')")
    O.validate_pending_witnesses(ledger, timing)
    assert observation(timing)['validation_state'] != 'verified'
    ledger.execute("UPDATE memecoin_positions SET execution_mode='real'")
    O.validate_pending_witnesses(ledger, timing)
    assert observation(timing)['validation_state'] != 'verified'
    ledger.execute('UPDATE memecoin_fills SET qty=1')
    O.validate_pending_witnesses(ledger, timing)
    assert observation(timing)['validation_state'] == 'verified'


def test_eth_confirm_first_success_has_boot_stamp_and_unchanged_read_count(monkeypatch):
    from sniper.execution import evm_real_seams as S
    calls = []
    answers = iter([None, receipt()])
    monkeypatch.setattr(S, '_rpc', lambda *a, **k: calls.append(a[1]) or {'result': next(answers)})
    result = S.eth_confirm(TX, 'unused', max_attempts=2, sleep=lambda _: None)
    assert result == 'confirmed'
    assert isinstance(result.observed_monotonic_ns, int)
    assert result.observed_boot_id == clock()['boot_id']
    assert calls == ['eth_getTransactionReceipt', 'eth_getTransactionReceipt']


@pytest.mark.parametrize('changed_block,expected_ns', [(False, 100), (True, 200)])
def test_eth_confirm_preserves_first_success_only_for_same_exact_receipt(
        monkeypatch, changed_block, expected_ns):
    from sniper.execution import evm_real_seams as S
    import time
    second = receipt(blockHash=OTHER, blockNumber='0x65') if changed_block else receipt()
    answers, stamps, canonical = iter([receipt(), second]), iter([100, 200]), iter([False, True])
    calls = []
    monkeypatch.setattr(S, '_rpc', lambda *a, **k: calls.append(a[1]) or {'result': next(answers)})
    monkeypatch.setattr(time, 'monotonic_ns', lambda: next(stamps))
    monkeypatch.setattr(S, '_canonical_receipt_on_rpc', lambda *a, **k: next(canonical))
    result = S.eth_confirm(TX, 'unused', require_canonical=True,
                           max_attempts=2, sleep=lambda _: None)
    assert result == 'confirmed'
    assert result.observed_monotonic_ns == expected_ns
    assert result.receipt['blockHash'] == second['blockHash']
    assert calls == ['eth_getTransactionReceipt', 'eth_getTransactionReceipt']


def test_shared_entry_publishes_success_using_existing_receipt_without_extra_rpc(monkeypatch):
    from test_memecoin_evm_receipt_booking import _enter, _buy
    publisher = O.Publisher(generation='g', chain='4663')
    monkeypatch.setattr(O, 'create_publisher', lambda chain: publisher if str(chain) == '4663' else None)
    conn, pending_id, result, net, _ = _enter(monkeypatch, _buy(coin=4, chain='4663'), chain='4663')
    assert result['status'] == 'filled'
    events = list(publisher.ring)
    assert [event['kind'] for event in events] == ['dispatch', 'observed', 'booked']
    assert events[1]['observed_ns'] <= events[2]['emitted_ns']
    assert events[1]['chain_id'] == '4663'
    assert events[1]['tx_hash'] == result['tx_hash']
    assert net.calls.count('eth_getTransactionReceipt') == 1
    assert net.calls.count('eth_getBlockByNumber') == 1


@pytest.mark.parametrize('status', ['0x1', '0x0'])
def test_root_native_observes_only_success_after_existing_wallet_lock_release(monkeypatch, tmp_path, status):
    from test_memecoin_rh_fast_live import _ledger, Chain, _live, _accepting_sender, _run
    from sniper.execution import exec_lock
    held = [False]
    original_lock = exec_lock.wallet_exec_lock
    @contextmanager
    def tracked(*args, **kwargs):
        with original_lock(*args, **kwargs) as acquired:
            held[0] = bool(acquired)
            try:
                yield acquired
            finally:
                held[0] = False
    monkeypatch.setattr(exec_lock, 'wallet_exec_lock', tracked)
    conn, net = _ledger(), Chain(status=status)
    publisher = O.Publisher(generation='g', chain='4663')
    original_observed = publisher.observed
    def observed(*args, **kwargs):
        assert not held[0], 'native measurement published inside the wallet lock'
        return original_observed(*args, **kwargs)
    publisher.observed = observed
    live = _live(conn, _accepting_sender(), net, lock_dir=str(tmp_path), alert_fn=lambda _: None)
    live.ctx['bsc_execution_observer'] = publisher
    result, rec = _run(live, conn)
    successes = [e for e in publisher.ring if e['kind'] == 'observed']
    assert len(successes) == (1 if status == '0x1' else 0)
    if successes:
        assert successes[0]['tx_hash'] == result['tx_hash']
        assert type(successes[0]['observed_ns']) is int
        assert successes[0]['observed_boot_id'] == clock()['boot_id']
    assert net.asks == 1


def test_root_native_failed_success_publication_records_gap_without_blocking_fill(monkeypatch, tmp_path):
    from test_memecoin_rh_fast_live import _ledger, Chain, _live, _accepting_sender, _run
    conn, net = _ledger(), Chain(status='0x1')
    publisher = O.Publisher(generation='g', chain='4663')
    def fail(*args, **kwargs):
        raise ValueError('measurement metadata unavailable')
    publisher.observed = fail
    live = _live(conn, _accepting_sender(), net, lock_dir=str(tmp_path), alert_fn=lambda _: None)
    live.ctx['bsc_execution_observer'] = publisher
    original_execute = live.execute_reserved
    def execute(payload, rec, *args, **kwargs):
        rec['attempt_id'] = 'native-test-attempt'
        return original_execute(payload, rec, *args, **kwargs)
    live.execute_reserved = execute
    result, rec = _run(live, conn)
    assert result['status'] == 'filled'
    gaps = [event for event in publisher.ring if event['kind'] == 'gap']
    assert len(gaps) == 1
    assert gaps[0]['reason'] == 'confirmation_publication_failed'
    assert gaps[0]['attempt_id'] == rec['attempt_id']
    assert net.asks == 1


def test_report_reads_rh_only_and_keeps_received_refusal_unknown(engine):
    from scripts.research.bsc_entry_speed_report import read_cohort, evaluate
    from sniper.persistence import db_memecoin as db
    timing, worker, publisher, _ = engine
    publisher.record_signal(dict(signal_id='rhfast:4663:' + TX + ':mint', mint='mint',
                                 source_user_id='wallet', receipt_clock=clock(), eligibility=None))
    publisher.record_outcome(dict(signal_id='rhfast:4663:' + TX + ':mint', mint='mint',
                                  source_user_id='wallet', reason='busy', eligibility=None))
    for event in list(publisher.ring):
        worker.admit(event)
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    document = read_cohort(ledger, timing, SHA, chain='4663')
    assert len(document['rows']) == 1
    assert document['rows'][0]['chain_id'] == '4663'
    assert document['metadata']['observation_kind'] == 'confirmation_observation'
    report = evaluate(document['rows'])
    assert report['received_unique'] == 1 and report['unknown_eligibility'] == 1
    assert report['received_reasons']['busy'] == 1
    assert report['verified_witnesses'] == 0 and not report['goal_proved']


def test_thin_rh_report_keeps_source_without_handoff_or_original_clock(engine):
    from scripts.research import rh_entry_speed_report as report
    from sniper.persistence import db_memecoin as db
    timing, _, _, _ = engine
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    ledger.execute("INSERT INTO onchain_wallet_detections "
                   "(signature,source_user_id,address,network_id,mint,detected_at,created_at) "
                   "VALUES (?,'wallet','address','4663','mint','2026-10-11T20:00:00+00:00','now')",
                   ('4663:' + TX + ':mint',))
    doc = report.read_cohort(ledger, timing, SHA, since='2026-10-11T19:00:00+00:00',
                            until='2026-10-11T21:00:00+00:00')
    assert len(doc['rows']) == 1
    assert doc['rows'][0]['receipt_clock'] is None
    assert doc['rows'][0]['eligible'] is None
    assert doc['metadata']['source_clock_unknown'] == 1
    assert not doc['metadata']['universe_complete']
    oid = 'onchain:4663:' + TX + ':mint'
    ledger.execute("INSERT INTO memecoin_entry_attempts "
        "(attempt_id,origin_event_id,source_user_id,mint,network_id,tx_sig,decision_started_at,"
        "terminal_status,terminal_reason,completed_at) VALUES "
        "('filled',?,'wallet','mint','4663',?,'2026-10-11T20:00:00+00:00','filled','filled',"
        "'2026-10-11T20:00:01+00:00')", (oid, OTHER))
    ledger.execute("INSERT INTO memecoin_positions (id,mint,network_id,execution_mode) "
                   "VALUES (10,'mint','4663','real')")
    ledger.execute("INSERT INTO memecoin_fills (position_id,kind,attempt_id,tx_sig,network_id,qty,notional) "
                   "VALUES (10,'entry','filled',?,'4663',2,10)", (OTHER,))
    doc = report.read_cohort(ledger, timing, SHA, since='2026-10-11T19:00:00+00:00',
                            until='2026-10-11T21:00:00+00:00')
    measured = report.shared.evaluate(doc['rows'])
    assert measured['confirmed_fills'] == 1
    assert measured['verified_witnesses'] == 0
    assert doc['metadata']['source_clock_unknown'] == 1


def test_cross_chain_rows_cannot_supply_a_valid_latency():
    from test_bsc_entry_speed_report import row
    from scripts.research.bsc_entry_speed_report import _witness_ms
    datum = dict(row(1), chain_id='4663', observed_chain='56', confirmed_chain='4663',
                 booked_chain='4663')
    assert _witness_ms(datum) is None


@pytest.mark.parametrize('clock_kind', ['valid', 'incomplete', 'history_unknown'])
def test_direct_rh_evaluator_requires_complete_original_clock(clock_kind):
    from test_bsc_entry_speed_report import row
    from scripts.research.bsc_entry_speed_report import _witness_ms, evaluate
    anchor = dict(clock())
    if clock_kind == 'incomplete':
        anchor = {key: anchor[key] for key in ('boot_id', 'monotonic_ns')}
    elif clock_kind == 'history_unknown':
        anchor['history_unknown'] = True
    datum = dict(row(1), chain_id='4663', observed_chain='4663', confirmed_chain='4663',
                 booked_chain='4663', receipt_clock=anchor,
                 observed_boot_id=anchor['boot_id'], observed_ns=1_100_000_000)
    assert _witness_ms(datum) == (100 if clock_kind == 'valid' else None)
    assert evaluate([datum])['verified_witnesses'] == (1 if clock_kind == 'valid' else 0)


def test_report_retains_last_publisher_loss_even_without_another_event(engine):
    from scripts.research.bsc_entry_speed_report import read_cohort
    from sniper.persistence import db_memecoin as db
    timing, _, publisher, _ = engine
    ledger = sqlite3.connect(':memory:')
    ledger.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(ledger)
    state = dict(publisher.status(), chain_id='4663', overwritten=5, ipc_failures=2)
    timing.execute('INSERT INTO memecoin_execution_observer_health VALUES (?,?)',
                   ('publisher:4663:1:producer', json.dumps(state)))
    document = read_cohort(ledger, timing, SHA, chain='4663')
    assert document['metadata']['publication_losses'] == 7


@pytest.mark.parametrize('state,completion,expected', [('warm', 900_000_000, True),
    ('warm', 1_100_000_000, False), ('warm', None, None), ('cold', None, False), ('unknown', None, None)])
def test_shared_preparation_label_is_causal_original_ingress_metadata(state, completion, expected):
    from sniper.execution import evm_real_seams as S
    trace = SimpleNamespace(_source_evidence={'entry_preparation': {
        'state': state, 'completed_monotonic_ns': completion}}, receipt_context=lambda: {'receipt_clock': clock()})
    proof, ready = S.entry_preparation_at_ingress(trace)
    assert proof['state'] == state
    assert ready is expected


def test_shared_warm_and_cold_are_reported_separately():
    from test_bsc_entry_speed_report import row
    from scripts.research.bsc_entry_speed_report import evaluate
    report = evaluate([row(i, stratum='shared_warm' if i < 50 else 'shared_cold') for i in range(100)],
                      universe_complete=True)
    assert 'unknown_route_readiness' not in report['blockers']
    assert report['strata']['shared_warm']['eligible'] == 50
    assert report['strata']['shared_cold']['eligible'] == 50


def test_utc_diagnostics_are_separate_and_never_become_monotonic_witnesses():
    from scripts.research.bsc_entry_speed_report import evaluate
    datum = dict(source_tx=TX, wallet='wallet', mint='mint', eligible=True, outcome='filled',
                 stratum='shared_cold', diagnostic_signal_to_confirmation_ms=100)
    report = evaluate([datum])
    assert report['utc_diagnostics']['signal_to_confirmation_ms']['p50'] == 100
    assert report['verified_witnesses'] == 0
    assert report['strata']['shared_cold']['fill_ms']['n'] == 0
