"""Same-slot receipt truth is independent of feed-head timing and observer coverage."""
import copy
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_fill_timing as ft


def joined():
    source = dict(network_id='solana', source_sig='SourceSig', source_wallet='Trader',
                  mint='Mint', source_slot=100, stratum='native_local_proof', warm_hit=False)
    attempt = dict(network_id='solana', source_sig='SourceSig', source_wallet='Trader', mint='Mint',
                   custody_pubkey='Custody', attempt_id='A', pending_id=1,
                   tx_sig='OurSig', landing_slot=100, terminal_status='filled')
    receipt = dict(network_id='solana', tx_sig='OurSig', custody_pubkey='Custody', mint='Mint',
                   receipt_slot=100, chain_confirmed=True, chain_error=None,
                   payment_atomic='20', acquired_atomic='123')
    fill = dict(network_id='solana', tx_sig='OurSig', custody_pubkey='Custody', mint='Mint',
                receipt_slot=100, chain_confirmed=True, side='buy', attempt_id='A', pending_id=1,
                payment_atomic='20', acquired_atomic='123')
    return source, attempt, receipt, fill


def call(name, *args, **kwargs):
    fn = getattr(ft, name, None)
    assert callable(fn), name + ' missing'
    return fn(*args, **kwargs)


@pytest.mark.parametrize('landing,confirmed,error,quantity,outcome', [
    (100, True, None, '123', 'same_slot'), (101, True, None, '123', 'later_slot'),
    (103, True, None, '123', 'later_slot'), (100, True, 'ExceededSlippage', '0', 'failed'),
    (100, False, 'ExceededSlippage', '0', 'unknown'), (None, False, None, '123', 'unknown'),
    (99, True, None, '123', 'unknown'), (100, True, None, None, 'unknown'),
    (100, True, None, '0', 'unknown'), (100, True, None, '1.2', 'unknown'),
    (100, True, None, True, 'unknown'), (100, True, None, '١٢٣', 'unknown'),
])
def test_only_confirmed_positive_acquisition_grades_success(landing, confirmed, error, quantity, outcome):
    assert call('same_slot_outcome', source_slot=100, landing_slot=landing, confirmed=confirmed,
                chain_error=error, acquired_atomic=quantity) == outcome


def test_same_slot_does_not_need_processed_observation_or_matching_boot():
    source, attempt, receipt, fill = joined()
    source.update(boot_id='old', clock_domain='old', recv_monotonic=None)
    result = call('same_slot_receipt_evidence', source, attempt, receipt, fill)
    assert result['outcome'] == 'same_slot' and result['slot_delta'] == 0
    pending = dict(attempt, state='confirmed')
    assert ft.execution_bound(source, [], attempt, pending, fill,
                              custody_pubkey='Custody')['state'] == 'unknown'


def test_failed_source_slot_receipt_is_failed_even_without_fill():
    source, attempt, receipt, _fill = joined()
    attempt['terminal_status'] = 'failed'
    receipt['chain_error'] = {'InstructionError': [1, 'ExceededSlippage']}
    assert call('same_slot_receipt_evidence', source, attempt, receipt, None)['outcome'] == 'failed'
    assert ft.execution_bound(source, [], attempt, {}, {}, custody_pubkey='Custody')['reason'] == 'attempt_not_filled'


@pytest.mark.parametrize('row,key,value', [
    (1, 'source_sig', 'Other'), (1, 'source_wallet', 'Other'), (1, 'network_id', '56'),
    (2, 'tx_sig', 'Other'), (2, 'custody_pubkey', 'Other'), (2, 'mint', 'Other'),
    (2, 'receipt_slot', None), (2, 'chain_confirmed', False), (2, 'chain_error', 'unknown'),
    (3, 'tx_sig', 'Other'), (3, 'acquired_atomic', None), (3, 'payment_atomic', None),
    (3, 'receipt_slot', 101), (3, 'attempt_id', 'Other'), (3, 'side', 'sell'),
])
def test_exact_receipt_join_censors_mismatch_and_unknown(row, key, value):
    rows = list(joined())
    rows[row][key] = value
    # A confirmed error is failed, regardless of a separately booked successful row.
    expected = 'failed' if key == 'chain_error' else 'unknown'
    assert call('same_slot_receipt_evidence', *rows)['outcome'] == expected


def test_absent_receipt_never_confirms_attempt_error():
    source, attempt, _receipt, fill = joined()
    attempt.update(terminal_status='failed', chain_error='ExceededSlippage')
    assert call('same_slot_receipt_evidence', source, attempt, None, fill)['outcome'] == 'unknown'


def test_delayed_head_notification_is_skew_and_not_remaining_slot_time():
    from sniper.services.memecoin_slot_lane import receipt_head
    buy = SimpleNamespace(slot=100, head_at_recv=99, head_at_recv_mono=1.0,
                          recv_monotonic=1.1, connection_generation=7, slot_status='processed')
    observation = receipt_head(buy)
    assert observation['head_at_recv_minus_s'] == -1
    assert observation.get('head_skew_basis') == 'feed_local_head_minus_source_slot'
    assert observation.get('connection_generation') == 7
    assert observation.get('slot_status') == 'processed'
    assert 'remaining_ms' not in observation


def test_cohort_keeps_cold_misses_refusals_and_all_four_frozen_strata():
    sources, executions = [], []
    for i, (stratum, status) in enumerate([
        ('native_local_proof', 'filled'), ('native_local_proof', 'missing'),
        ('routed_confirmed_proof', 'refused'), ('graduation', 'no_submission'),
        ('ordinary_fallback', 'unknown'), ('ordinary_fallback', 'filled'),
        ('ordinary_fallback', 'filled'), ('ordinary_fallback', 'failed')]):
        source, attempt, receipt, fill = joined()
        source.update(source_sig=str(i), stratum=stratum)
        attempt.update(source_sig=str(i), terminal_status=status, attempt_id=str(i))
        fill['attempt_id'] = str(i)
        sources.append(source)
        if status == 'missing':
            continue
        if status in ('refused', 'no_submission'):
            attempt['tx_sig'] = None
        if status == 'unknown':
            receipt = None
        if i in (5, 6):
            attempt['landing_slot'] = receipt['receipt_slot'] = fill['receipt_slot'] = 100 + i - 4
        if status == 'failed':
            receipt['chain_error'] = 'ExceededSlippage'
            fill = None
        executions.append(dict(attempt=attempt, receipt=receipt, fill=fill))
    result = call('same_slot_cohort_summary', sources + [copy.copy(sources[0])], executions)
    assert result['n'] == 8 and result['counts'] == {
        'S+0': 1, 'S+1': 1, 'S+2+': 1, 'failed': 1, 'refused': 1,
        'no_submission': 1, 'unresolved': 1, 'missing': 1}
    assert result['strata']['native_local_proof']['n'] == 2
    assert result['strata']['native_local_proof']['same_slot_rate'] == .5
    assert result['secondary']['warm_n'] == 0
    assert result['secondary']['submitted_n'] == 5


def test_conflicting_frozen_stratum_cannot_be_reclassified_from_our_outcome():
    source = joined()[0]
    with pytest.raises(ValueError, match='stratum'):
        call('same_slot_cohort_summary', [source, dict(source, stratum='graduation')], [])


def test_identity_commit_span_keeps_durable_binding_ahead_of_dispatch(monkeypatch):
    from sniper.execution import submission_wiring as wiring
    from sniper.execution.submission_transport import AttemptState, SubmissionResult
    from solders.keypair import Keypair
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    import base64
    kp = Keypair()
    signed = base64.b64encode(bytes(VersionedTransaction(
        MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()), [kp]))).decode()
    events = []
    monkeypatch.setattr(wiring, '_resolve_owner_id', lambda *a: 1)
    monkeypatch.setattr(wiring.m, 'bind_entry_submission', lambda *a, **k: events.append('commit') or True)
    def submit(req):
        events.append('submit')
        return SubmissionResult(AttemptState.ACCEPTED, req.tx_identity, (), None, req.signed_payload_sha256)
    send = wiring.make_solana_send_fn(SimpleNamespace(submit=submit), conn=object(), action='entry',
        owner_kind='reservation', owner_id=1, persist=True, record=False,
        observe_fn=events.append)
    assert send(signed)
    assert events == ['identity_start', 'commit', 'identity_bound', 'dispatch', 'submit', 'ack']


def test_stage_summary_uses_explicit_spans_and_retains_unknown_network_write():
    attempt = dict(trace_json={'substage_events': [
        {'name': 'slot_receipt', 'receipt_to_enqueue_ms': 3, 'queue_ms': 4, 'receipt_to_proof_ms': 20},
        {'name': 'attempt_row', 'duration_ms': 1}, {'name': 'slot_classification', 'duration_ms': 2},
        {'name': 'slot_reservation', 'duration_ms': 5}, {'name': 'slot_build', 'duration_ms': 6},
        {'name': 'timing_local_sign', 'duration_ms': .2},
        {'name': 'slot_transport_identity_bound', 'duration_ms': 7},
        {'name': 'slot_transport_dispatch', 'duration_ms': 0, 'receipt_to_dispatch_ms': 40},
        {'name': 'slot_transport_ack', 'duration_ms': 8}]})
    spans = call('execution_stage_spans', attempt)
    assert spans['receipt_to_enqueue_ms'] == 3 and spans['queue_ms'] == 4
    assert spans['local_sign_ms'] == .2 and spans['reservation_ms'] == 5
    assert spans['signed_identity_commit_ms'] == 7 and spans['transport_ack_ms'] == 8
    assert spans['first_transport_write_ms'] is None
    summary = call('execution_stage_summary', [attempt, dict(trace_json={})])
    assert summary['local_sign_ms'] == dict(n=1, missing=1, p50_ms=.2, p90_ms=.2)


def test_deferred_completion_publishes_spans_through_existing_owned_note(tmp_path):
    from sniper.services import memecoin_slot_skips as skips
    skips.drain()
    skips.note('measurement-owned', 'submitted', attempt_id='A')
    try:
        assert skips.finish_note('measurement-owned', 'A', 'filled', evidence={
            'source_wallet': 'Trader', 'mint': 'Mint', 'stage_local_sign_ms': .2})
    except TypeError:
        pytest.fail('existing async completion cannot carry final stage spans')
    rows = skips.drain()
    assert rows[-1][4]['stage_local_sign_ms'] == .2
    conn = ft.connect(tmp_path / 'timing.db')
    skips.flush(conn, rows)
    import json
    evidence = json.loads(conn.execute('SELECT evidence_json FROM memecoin_slot_lane_skips '
                                      'WHERE signature=?', ('measurement-owned',)).fetchone()[0])
    assert evidence['stage_local_sign_ms'] == .2
    # A stale owner cannot erase a completed measurement.
    assert not skips.finish_note('measurement-owned', 'stale', 'failed', evidence={'stage_local_sign_ms': 99})
    conn.close()


@pytest.mark.parametrize('field', ['payment_atomic', 'acquired_atomic'])
def test_receipt_atoms_must_match_booked_evidence(field):
    source, attempt, receipt, fill = joined()
    receipt[field] = '124'
    assert call('same_slot_receipt_evidence', source, attempt, receipt, fill)['outcome'] == 'unknown'


def test_duplicate_receipt_conflict_cannot_choose_success_over_failure():
    source, attempt, receipt, fill = joined()
    failed = dict(receipt, chain_error='ExceededSlippage')
    summary = call('same_slot_cohort_summary', [source], [
        dict(attempt=attempt, receipt=receipt, fill=fill),
        dict(attempt=attempt, receipt=failed, fill=None)])
    assert summary['counts']['unresolved'] == 1 and summary['counts']['S+0'] == 0


def test_legacy_identity_commit_stays_before_transport_and_is_measured(tmp_path):
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.execution.solana_slot_executor import execute_prebuilt_entry
    from test_memecoin_solana_slot_executor import _reserve
    conn = sqlite3.connect(tmp_path / 'ledger.db')
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.commit()
    pending = _reserve(conn)
    events = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: events.append((name, kw)))
    def send(_payload):
        row = m.pending_row(conn, pending['id'])
        assert row['tx_sig'] == 'OurSig'
        commit = next((kw for name, kw in events if name == 'slot_transport_identity_bound'), None)
        assert commit is not None, 'legacy durable identity write has no explicit span'
        assert commit['duration_ms'] >= 0
        return None
    result = execute_prebuilt_entry(conn, pending=pending, signed_b64='AA==', sig='OurSig',
        validity={}, send_fn=send, confirm_fn=lambda sig: 'unknown',
        receipt_fill_fn=lambda *a: None, now_iso='2026-09-26T20:00:01+00:00', trace=trace)
    assert result['status'] == 'unknown' and m.pending_row(conn, pending['id'])['status'] == 'reserved'
    conn.close()


def test_legacy_dispatch_observation_is_explicit_and_does_not_invent_network_write():
    attempt = dict(trace_json={'substage_events': [
        {'name': 'slot_pre_send', 'receipt_to_dispatch_ms': 62, 'proof_to_dispatch_ms': 12}]})
    spans = call('execution_stage_spans', attempt)
    assert spans['first_transport_dispatch_ms'] == 62
    assert spans.get('proof_to_dispatch_ms') == 12
    assert spans['first_transport_write_ms'] is None


@pytest.mark.parametrize('head', [None, 99])
def test_received_lifecycle_survives_zero_status_and_missing_head(head):
    from sniper.services.memecoin_slot_lane import receipt_head
    buy = SimpleNamespace(slot=100, head_at_recv=head, recv_monotonic=1.1,
                          head_at_recv_mono=1.0, receipt_evidence={
                              'connection_generation': 7, 'slot_status': 0})
    result = receipt_head(buy)
    assert result.get('slot_status') == 0 and result.get('connection_generation') == 7


def test_paired_routed_source_is_not_labelled_native_before_conversion():
    from sniper.services.memecoin_slot_lane import _receipt_facts
    buy = SimpleNamespace(slot=100, owner='Trader', signature='SourceSig', mint='Mint',
                          paired_monotonic=1.2, receipt_evidence={'producer_run_id': 'source'})
    assert _receipt_facts(buy)['stratum'] == 'routed_confirmed_proof'


@pytest.mark.parametrize('field,value', [
    ('acquired_atomic', '124'), ('payment_atomic', '21'), ('receipt_slot', 101),
    ('tx_sig', 'OtherSig'), ('custody_pubkey', 'OtherOwner'), ('mint', 'OtherMint'),
    ('network_id', '56'),
])
@pytest.mark.parametrize('reverse', [False, True])
def test_conflicting_receipt_cannot_be_dropped_to_keep_same_slot(field, value, reverse):
    source, attempt, receipt, fill = joined()
    conflict = dict(receipt, **{field: value})
    rows = [dict(attempt=attempt, receipt=receipt, fill=fill),
            dict(attempt=attempt, receipt=conflict, fill=fill)]
    summary = ft.same_slot_cohort_summary([source], rows[::-1] if reverse else rows)
    assert summary['counts']['unresolved'] == 1
    assert summary['counts']['S+0'] == 0 and summary['same_slot_rate'] == 0


@pytest.mark.parametrize('reverse', [False, True])
def test_conflicting_frozen_source_slots_are_rejected_in_both_orders(reverse):
    source, attempt, receipt, fill = joined()
    sources = [source, dict(source, source_slot=99)]
    with pytest.raises(ValueError, match='source slot'):
        ft.same_slot_cohort_summary(sources[::-1] if reverse else sources,
                                   [dict(attempt=attempt, receipt=receipt, fill=fill)])


@pytest.mark.parametrize('reverse', [False, True])
def test_missing_source_slot_can_be_enriched_without_input_order_effect(reverse):
    source, attempt, receipt, fill = joined()
    sources = [dict(source, source_slot=None), source]
    summary = ft.same_slot_cohort_summary(sources[::-1] if reverse else sources,
                                         [dict(attempt=attempt, receipt=receipt, fill=fill)])
    assert summary['counts']['S+0'] == 1


@pytest.mark.parametrize('status', [None, 'pending', 'submitted', 'submitted_unknown', 'filled', 'failed'])
def test_missing_submission_metadata_never_proves_no_submission(status):
    source, attempt, _receipt, _fill = joined()
    attempt.pop('tx_sig')
    if status is None:
        attempt.pop('terminal_status')
    else:
        attempt['terminal_status'] = status
    summary = ft.same_slot_cohort_summary([source], [dict(attempt=attempt)])
    assert summary['counts']['unresolved'] == 1 and summary['counts']['no_submission'] == 0


def test_explicit_terminal_no_submission_is_required_for_that_bucket():
    source, attempt, _receipt, _fill = joined()
    attempt.update(tx_sig=None, terminal_status='no_submission')
    summary = ft.same_slot_cohort_summary([source], [dict(attempt=attempt)])
    assert summary['counts']['no_submission'] == 1


@pytest.mark.parametrize('reverse', [False, True])
def test_missing_receipt_can_be_enriched_by_exact_receipt_without_conflict(reverse):
    source, attempt, receipt, fill = joined()
    rows = [dict(attempt=attempt, receipt=None, fill=None),
            dict(attempt=attempt, receipt=receipt, fill=fill)]
    summary = ft.same_slot_cohort_summary([source], rows[::-1] if reverse else rows)
    assert summary['counts']['S+0'] == 1


@pytest.mark.parametrize('field', ['payment_atomic', 'acquired_atomic'])
@pytest.mark.parametrize('value', ['0', '-1', '1.2', True])
@pytest.mark.parametrize('row_name', ['receipt', 'fill'])
def test_explicit_invalid_receipt_atoms_cannot_be_discarded(field, value, row_name):
    source, attempt, receipt, fill = joined()
    conflict = dict(attempt=attempt, receipt=receipt, fill=fill)
    conflict[row_name] = dict(conflict[row_name], **{field: value})
    rows = [dict(attempt=attempt, receipt=receipt, fill=fill),
            conflict]
    summary = ft.same_slot_cohort_summary([source], rows)
    assert summary['counts']['unresolved'] == 1 and summary['counts']['S+0'] == 0


@pytest.mark.parametrize('field', ['payment_atomic', 'acquired_atomic'])
def test_absent_receipt_atoms_can_be_enriched_by_exact_receipt(field):
    source, attempt, receipt, fill = joined()
    partial = dict(receipt)
    partial.pop(field)
    summary = ft.same_slot_cohort_summary([source], [
        dict(attempt=attempt, receipt=partial, fill=fill),
        dict(attempt=attempt, receipt=receipt, fill=fill)])
    assert summary['counts']['S+0'] == 1


def test_bare_attempt_identity_remains_unresolved():
    source = joined()[0]
    shell = {key: source[key] for key in ('network_id', 'source_sig', 'source_wallet', 'mint')}
    summary = ft.same_slot_cohort_summary([source], [dict(attempt=shell)])
    assert summary['counts']['unresolved'] == 1 and summary['counts']['no_submission'] == 0


@pytest.mark.parametrize('slot', [0, 'invalid', 99])
def test_explicit_invalid_or_earlier_receipt_slot_cannot_keep_same_slot(slot):
    source, attempt, receipt, fill = joined()
    summary = ft.same_slot_cohort_summary([source], [
        dict(attempt=attempt, receipt=receipt, fill=fill),
        dict(attempt=dict(attempt, landing_slot=slot),
             receipt=dict(receipt, receipt_slot=slot), fill=dict(fill, receipt_slot=slot))])
    assert summary['counts']['unresolved'] == 1 and summary['counts']['S+0'] == 0


@pytest.mark.parametrize('side', ['sell', '', False])
@pytest.mark.parametrize('reverse', [False, True])
def test_explicit_nonbuy_fill_cannot_be_discarded_beside_same_slot(side, reverse):
    source, attempt, receipt, fill = joined()
    rows = [dict(attempt=attempt, receipt=receipt, fill=fill),
            dict(attempt=attempt, receipt=receipt, fill=dict(fill, side=side))]
    summary = ft.same_slot_cohort_summary([source], rows[::-1] if reverse else rows)
    assert summary['counts']['unresolved'] == 1 and summary['counts']['S+0'] == 0


@pytest.mark.parametrize('partial', [dict(side=None), dict(chain_confirmed=False),
                                   dict(chain_confirmed=None)])
@pytest.mark.parametrize('reverse', [False, True])
def test_missing_side_or_unconfirmed_fill_can_be_enriched(partial, reverse):
    source, attempt, receipt, fill = joined()
    rows = [dict(attempt=attempt, receipt=receipt, fill=dict(fill, **partial)),
            dict(attempt=attempt, receipt=receipt, fill=fill)]
    summary = ft.same_slot_cohort_summary([source], rows[::-1] if reverse else rows)
    assert summary['counts']['S+0'] == 1 and summary['counts']['unresolved'] == 0
