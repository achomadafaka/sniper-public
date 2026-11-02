"""All RH timing endpoints use the same immutable original receipt after feed deduplication."""
import copy
import json
import sqlite3

import pytest

from scripts.research import bsc_entry_speed_report as common
from scripts.research import rh_entry_speed_report as rh
from sniper.util.receipt_clock import ReceiptClock


def anchor(ns, *, boot=None):
    value = dict(ReceiptClock('report-test', raw_time_fn=lambda: 1791753535).capture(monotonic_ns=ns))
    if boot is not None:
        value['boot_id'] = boot
    return value


def measured_row():
    from test_bsc_entry_speed_report import row
    clock = anchor(1_100_000_000)
    return dict(row(1), chain_id='4663', receipt_clock=clock, stratum='shared_unknown',
        attempt_id='attempt', dispatch_boundary='sender_call_after_durable_binding',
        dispatch_monotonic_ns=1_200_000_000, dispatch_boot_id=clock['boot_id'], dispatch_ms=100,
        ack_monotonic_ns=1_300_000_000, ack_boot_id=clock['boot_id'], ack_ms=200,
        observed_ns=1_400_000_000, observed_boot_id=clock['boot_id'],
        observed_chain='4663', confirmed_chain='4663', booked_chain='4663')


def test_all_rh_metrics_rebase_to_earliest_original_same_boot_clock():
    datum = measured_row()
    datum['receipt_clock'] = anchor(1_000_000_000)
    rebased = common.rebase_receipt_latencies(datum)
    assert rebased['dispatch_ms'] == 200
    assert rebased['ack_ms'] == 300
    assert common._witness_ms(rebased) == 400
    result = common.evaluate([datum])['strata']['shared_unknown']
    assert result['sender_dispatch_ms']['p50'] == 200
    assert result['ack_ms']['p50'] == 300
    assert result['fill_ms']['p50'] == 400


@pytest.mark.parametrize('bad', ['missing_clock', 'cross_boot', 'history_unknown',
                                'missing_endpoints', 'reversed_endpoints'])
def test_unavailable_rh_anchors_or_endpoints_cannot_retain_old_metrics(bad):
    datum = measured_row()
    if bad == 'missing_clock':
        datum['receipt_clock'] = None
    elif bad == 'cross_boot':
        datum['receipt_clock'] = anchor(1_000_000_000, boot='other-boot')
    elif bad == 'history_unknown':
        datum['receipt_clock']['history_unknown'] = True
    elif bad == 'missing_endpoints':
        datum.pop('dispatch_monotonic_ns')
        datum.pop('ack_monotonic_ns')
    else:
        datum['dispatch_monotonic_ns'] = datum['ack_monotonic_ns'] = 999_000_000
    rebased = common.rebase_receipt_latencies(datum)
    assert rebased['dispatch_ms'] is None
    assert rebased['ack_ms'] is None
    result = common.evaluate([datum])['strata']['shared_unknown']
    assert result['sender_dispatch_ms']['n'] == 0
    assert result['ack_ms']['n'] == 0


def test_bsc_sparse_legacy_metrics_remain_unchanged():
    from test_bsc_entry_speed_report import row
    datum = dict(row(1), chain_id='56')
    assert common.rebase_receipt_latencies(datum) == datum


@pytest.mark.parametrize('earlier_boot,expected_dispatch,expected_ack,expected_fill',
                         [(None, 200, 300, 400), ('other-boot', None, None, None)])
def test_rh_fan_in_rebases_after_final_exact_source_dedup(
        monkeypatch, earlier_boot, expected_dispatch, expected_ack, expected_fill):
    measured = measured_row()
    early = {key: value for key, value in measured.items() if key in
             ('chain_id', 'source_tx', 'wallet', 'mint')}
    early['receipt_clock'] = anchor(1_000_000_000, boot=earlier_boot)
    document = dict(rows=[early, measured], metadata={'publication_losses': 0})
    monkeypatch.setattr(common, 'read_cohort', lambda *args, **kwargs: copy.deepcopy(document))
    ledger, timing = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    try:
        report = rh.read_cohort(ledger, timing, 'a' * 40,
            since='2026-10-11T21:00:00+00:00', until='2026-10-11T22:00:00+00:00')
    finally:
        ledger.close()
        timing.close()
    assert len(report['rows']) == 1
    selected = report['rows'][0]
    assert selected['dispatch_ms'] == expected_dispatch
    assert selected['ack_ms'] == expected_ack
    assert common._witness_ms(selected) == expected_fill


def test_rh_cohort_boundary_uses_original_receipt_after_exact_source_dedup(monkeypatch):
    measured = measured_row()
    measured['receipt_clock'] = dict(measured['receipt_clock'],
                                    corrected_utc='2026-10-11T21:01:00+00:00')
    early = {key: value for key, value in measured.items() if key in
             ('chain_id', 'source_tx', 'wallet', 'mint')}
    early['receipt_clock'] = dict(anchor(1_000_000_000),
                                 corrected_utc='2026-10-11T20:59:00+00:00')
    document = dict(rows=[early, measured], metadata={'publication_losses': 0})
    monkeypatch.setattr(common, 'read_cohort', lambda *args, **kwargs: copy.deepcopy(document))
    ledger, timing = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    try:
        report = rh.read_cohort(ledger, timing, 'a' * 40,
            since='2026-10-11T21:00:00+00:00', until='2026-10-11T22:00:00+00:00')
    finally:
        ledger.close()
        timing.close()
    assert report['rows'] == []


@pytest.mark.parametrize('case,expected_ns,expected_ack', [
    ('single', 1_100_000_000, 200), ('earlier_event', 1_000_000_000, 300),
    ('earlier_trace', 1_000_000_000, 300), ('event_boot_conflict', None, None),
    ('trace_boot_conflict', None, None), ('malformed_duplicate', 1_100_000_000, 200),
    ('malformed_only', None, None), ('missing_only', None, None),
    ('history_unknown_only', None, None), ('missing_boot_only', None, None)])
def test_rh_common_reader_merges_validated_original_clocks_before_rebasing(
        case, expected_ns, expected_ack):
    from sniper.persistence import db_memecoin as db
    from sniper.services.memecoin_fill_timing import SCHEMA
    ledger, timing = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
    ledger.row_factory = timing.row_factory = sqlite3.Row
    try:
        db.ensure_memecoin_tables(ledger)
        timing.executescript(SCHEMA)
        datum = measured_row()
        source_id = 'onchain:4663:' + datum['source_tx'] + ':' + datum['mint']
        generation = 'a' * 40 + ':1:2:producer'
        event = dict(kind='signal', chain_id='4663', generation=generation,
            signal_id=source_id, source_user_id=datum['wallet'], mint=datum['mint'],
            receipt_clock=datum['receipt_clock'])
        clocks, trace_clock = [datum['receipt_clock']], datum['receipt_clock']
        if case == 'earlier_event':
            clocks.append(anchor(1_000_000_000))
        elif case == 'earlier_trace':
            trace_clock = anchor(1_000_000_000)
        elif case == 'event_boot_conflict':
            clocks.append(anchor(1_000_000_000, boot='other-boot'))
        elif case == 'trace_boot_conflict':
            trace_clock = anchor(1_000_000_000, boot='other-boot')
        elif case == 'malformed_duplicate':
            clocks.append({'boot_id': datum['ack_boot_id']})
        elif case.endswith('_only'):
            trace_clock = None
            clocks = [{'boot_id': datum['ack_boot_id']} if case == 'malformed_only'
                      else None if case == 'missing_only' else
                      dict(datum['receipt_clock'], history_unknown=True) if case == 'history_unknown_only'
                      else dict(datum['receipt_clock'], boot_id=None)]
        for sequence, receipt in enumerate(clocks, 1):
            timing.execute('INSERT INTO memecoin_execution_observer_events VALUES (?,?,?,?)',
                (generation, sequence, 'signal', json.dumps(dict(event, receipt_clock=receipt))))
        trace = {'receipt_clock': trace_clock, 'submission_events': [{
            'stage': 'transaction_submitted', 'receipt_elapsed_ms': 200,
            'monotonic_ns': 1_300_000_000, 'boot_id': datum['ack_boot_id']}]}
        ledger.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,network_id,'
            'source_user_id,mint,decision_started_at,tx_sig,trace_json) VALUES (?,?,?,?,?,?,?,?)',
            ('attempt', source_id, '4663', datum['wallet'], datum['mint'], 'now',
             datum['copy_tx'], json.dumps(trace)))
        dispatch = dict(attempt_id='attempt', chain_id='4663', tx_hash=datum['copy_tx'],
            emitted_ns=1_200_000_000, boot_id=datum['dispatch_boot_id'], route_lane='shared')
        timing.execute('INSERT INTO memecoin_execution_observations VALUES (?,?,?,?,?,?,?,?)',
            (generation, 'attempt', datum['copy_tx'], json.dumps(dispatch), None, None,
             'pending', 'pending'))
        selected = common.read_cohort(ledger, timing, 'a' * 40, chain='4663')['rows'][0]
        assert selected['dispatch_monotonic_ns'] == 1_200_000_000
        assert selected['ack_monotonic_ns'] == 1_300_000_000
        assert selected['dispatch_boot_id'] == selected['ack_boot_id'] == datum['ack_boot_id']
        assert (selected['receipt_clock'] or {}).get('monotonic_ns') == expected_ns
        assert selected['ack_ms'] == expected_ack
        if case in {'event_boot_conflict', 'trace_boot_conflict'}:
            assert selected['receipt_clock_conflict'] is True
    finally:
        ledger.close()
        timing.close()


@pytest.mark.parametrize('conflict,expected', [(False, 1000), (True, None)])
def test_rh_utc_diagnostics_rebase_and_cannot_repair_a_clock_conflict(conflict, expected):
    datum = measured_row()
    datum['receipt_clock'] = dict(datum['receipt_clock'],
                                 corrected_utc='2026-10-11T21:00:00+00:00')
    datum.update(receipt_clock_conflict=conflict, diagnostic_signal_to_ack_ms=100,
        diagnostic_signal_to_confirmation_ms=100,
        diagnostic_ack_completed_at='2026-10-11T21:00:01+00:00',
        diagnostic_confirmation_completed_at='2026-10-11T21:00:01+00:00')
    rebased = common.rebase_receipt_latencies(datum)
    assert rebased['diagnostic_signal_to_ack_ms'] == expected
    assert rebased['diagnostic_signal_to_confirmation_ms'] == expected


@pytest.mark.parametrize('case,expected_losses,expected_gap', [
    ('reset', 2, 'sequencer_coverage_counter_reset'),
    ('after_cutoff', 2, None), ('missing_counter', None, 'sequencer_coverage_heartbeat_malformed'),
    ('truncated', 0, 'sequencer_coverage_heartbeat_window_truncated')])
def test_rh_source_heartbeat_window_preserves_historical_gaps_and_unknown_coverage(
        monkeypatch, case, expected_losses, expected_gap):
    document = dict(rows=[], metadata={'publication_losses': 0})
    monkeypatch.setattr(common, 'read_cohort', lambda *args, **kwargs: copy.deepcopy(document))
    ledger, timing, source = (sqlite3.connect(':memory:') for _ in range(3))
    source.executescript('CREATE TABLE rh_feed_signals(id INTEGER PRIMARY KEY,tx_hash,source_user_id,wallet,mint,signal,'
        'reason,arrival_utc);CREATE INDEX idx_rh_feed_signals_arrival ON rh_feed_signals(arrival_utc);'
        'CREATE TABLE rh_feed_heartbeat(at,queue_drops,seq_gaps,bad_frames,frame_errors);')
    stamps = [('2026-10-11T20:59:00+00:00', 0), ('2026-10-11T21:01:00+00:00', 2)]
    stamps.append(('2026-10-11T22:01:00+00:00' if case == 'after_cutoff'
                   else '2026-10-11T21:02:00+00:00', None if case == 'missing_counter' else 0))
    source.executemany('INSERT INTO rh_feed_heartbeat VALUES (?,0,?,0,0)', stamps)
    try:
        report = rh.read_cohort(ledger, timing, 'a' * 40, source=source,
            since='2026-10-11T21:00:00+00:00', until='2026-10-11T22:00:00+00:00',
            limit=1 if case == 'truncated' else 100)
    finally:
        ledger.close()
        timing.close()
        source.close()
    metadata = report['metadata']
    assert metadata['source_publication_losses'] == expected_losses
    if expected_gap:
        assert expected_gap in metadata['source_coverage_gaps']
    assert metadata['source_heartbeat_window']['losses_exact'] is False
