"""Read-only grading must separate bid cohorts and acquisition from landing."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest
from solders.signature import Signature

spec = importlib.util.spec_from_file_location(
    'fee_split_grade', Path(__file__).parents[1] / 'scripts/research/nozomi_ab_grade.py')
grade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(grade)
RULE = 'nozomi-fee-split-v1'
SOURCE = str(Signature.from_bytes(bytes([1]) * 64))
OTHER_SOURCE = str(Signature.from_bytes(bytes([2]) * 64))


def assignment(aid):
    digest = hashlib.sha256((RULE + ':' + aid).encode()).digest()
    return ('T' if int.from_bytes(digest[:4], 'big') % 2 else 'C', digest[:4].hex())


def aid_for(arm):
    return next(str(i) for i in range(100) if assignment(str(i))[0] == arm)


def trace(aid, *, ordinary=False, origin=SOURCE, terminal='failed', receipt=False):
    fee_arm, stamp = assignment(aid)
    tip, priority = (6_000_000, 520_000) if fee_arm == 'T' else (5_000_000, 1_520_000)
    events = [
        {'name': 'nozomi_ab', 'arm': 'B', 'rule': RULE, 'fee_arm': fee_arm,
         'fee_assignment_hash': stamp, 'tip_lamports': tip},
        {'name': 'entry_bid_signed', 'tip_lamports': tip, 'priority_fee_lamports': priority,
         'combined_fee_lamports': tip + priority},
        {'name': 'nozomi_ab_send', 'tip_lamports': tip, 'nozomi_state': 'accepted',
         'nozomi_returned_sig': 'own-' + aid, 'nozomi_payload_sha_match': True,
         'nozomi_sig_match': True, 'nozomi_post_start_ms': 10., 'nozomi_post_finish_ms': 19.,
         'nozomi_ms': 9.},
        {'name': 'slot_receipt', 'source_slot': 10},
        {'name': 'slot_resend', 'landing_slot': 10}]
    if not ordinary:
        events.append({'name': 'slot_send_call', 'tx_sig': 'own-' + aid, 'elapsed_ms': 9.})
    if receipt:
        events.append({'name': 'receipt_execution_evidence', 'evidence': {
            'state': 'verified', 'tx_sig': 'own-' + aid, 'receipt_slot': 10,
            'network_id': 'solana', 'mint': 'mint', 'acquired_atomic': '125',
            'payment_atomic': '50', 'acquired_decimals': 6}})
    return {'terminal_status': terminal, 'terminal_reason': 'chain_error' if terminal == 'failed' else 'ok',
            'source_evidence': {'source_sig': origin, 'source_slot': 10, 'mint': 'mint'},
            'substage_events': events}


def event(t, name):
    return next(e for e in t['substage_events'] if e['name'] == name)


@pytest.mark.parametrize('fee_arm', ['C', 'T'])
def test_fee_cohorts_keep_every_provider_arm_b(fee_arm):
    aid = aid_for(fee_arm)
    row = grade.grade_row(aid, 'wallet', trace(aid))
    assert row['problems'] == []
    assert row['arm'] == 'B' and row['fee_arm'] == fee_arm
    assert row['source_origin'] == ('wallet', SOURCE, 'mint')


@pytest.mark.parametrize('field,value,problem', [
    ('fee_arm', 'wrong', 'FEE_assignment_mismatch'),
    ('fee_assignment_hash', '00000000', 'FEE_assignment_mismatch'),
    ('tip_lamports', 5_999_999, 'TIP_fee_allocation_violation'),
    ('arm', 'A', 'arm_or_rule_mismatch')])
def test_frozen_assignment_and_treatment_tip_are_checked(field, value, problem):
    aid = aid_for('T'); t = trace(aid)
    event(t, 'nozomi_ab')[field] = value
    assert problem in grade.grade_row(aid, 'wallet', t)['problems']


@pytest.mark.parametrize('priority,combined', [(520_001, 6_520_001), (-1, 5_999_999),
                                               (520_000, 6_519_999), (True, 6_000_001)])
def test_signed_treatment_math_never_borrows_from_the_cap(priority, combined):
    aid = aid_for('T'); t = trace(aid)
    event(t, 'entry_bid_signed').update(priority_fee_lamports=priority, combined_fee_lamports=combined)
    assert grade.grade_row(aid, 'wallet', t)['problems']


def test_unknown_cu_reports_bounded_upper_bound_without_inventing_exact_fee():
    aid = aid_for('T'); t = trace(aid)
    event(t, 'entry_bid_signed').update(priority_fee_lamports=None, combined_fee_lamports=None,
        priority_fee_upper_bound_lamports=520_000, combined_fee_upper_bound_lamports=6_520_000,
        priority_source='unknown_cu_limit')
    row = grade.grade_row(aid, 'wallet', t)
    assert row['signed_bid_state'] == 'bounded_unknown'
    assert row['priority_fee_lamports'] is None
    assert row['problems'] == ['signed_bid_unknown']


@pytest.mark.parametrize('field,value', [('nozomi_payload_sha_match', False),
                                       ('nozomi_sig_match', False), ('nozomi_returned_sig', 'wrong-sig'),
                                       ('nozomi_post_finish_ms', 9.)])
def test_provider_identity_and_actual_post_are_checked(field, value):
    aid = aid_for('T'); t = trace(aid)
    event(t, 'nozomi_ab_send')[field] = value
    row = grade.grade_row(aid, 'wallet', t)
    assert row['problems'] and grade.early_stops([row])


def test_ordinary_send_is_included_when_nozomi_proves_the_signed_identity():
    aid = aid_for('T')
    row = grade.grade_row(aid, 'wallet', trace(aid, ordinary=True))
    assert row is not None and row['tx_sig'] == 'own-' + aid
    assert row['problems'] == []
    assert row['send_ms'] is None


def test_same_slot_chain_failure_never_becomes_a_fill():
    aid = aid_for('T')
    row = grade.grade_row(aid, 'wallet', trace(aid))
    assert row['execution_outcome'] == 'chain_failed'
    assert row['same_slot_acquired'] is False
    unknown = grade.grade_row(aid, 'wallet', trace(aid, terminal='filled'))
    assert unknown['execution_outcome'] == 'acquisition_unknown'
    assert unknown['same_slot_acquired'] is False
    acquired = grade.grade_row(aid, 'wallet', trace(aid, terminal='filled', receipt=True))
    assert acquired['execution_outcome'] == 'acquired' and acquired['same_slot_acquired'] is True


@pytest.mark.parametrize('field,value', [('tx_sig', 'other'), ('acquired_atomic', '0'),
                                       ('payment_atomic', None), ('mint', 'other')])
def test_receipt_acquisition_is_bound_to_the_actual_send(field, value):
    aid = aid_for('T'); t = trace(aid, terminal='filled', receipt=True)
    event(t, 'receipt_execution_evidence')['evidence'][field] = value
    row = grade.grade_row(aid, 'wallet', t)
    assert row['same_slot_acquired'] is False
    assert row['execution_outcome'] == 'acquisition_unknown'


def test_untipped_packet_fallback_is_measured_without_pretending_it_received_treatment():
    aid = aid_for('T'); t = trace(aid)
    t['substage_events'] = [e for e in t['substage_events'] if e['name'] != 'nozomi_ab_send']
    t['substage_events'].append({'name': 'nozomi_ab_fallback', 'sent': 'untipped'})
    event(t, 'entry_bid_signed').update(tip_lamports=0, priority_fee_lamports=1_520_000,
                                      combined_fee_lamports=1_520_000)
    row = grade.grade_row(aid, 'wallet', t)
    assert row['problems'] == [] and row['fallback'] is True
    assert row['signed_tip_lamports'] == 0 and row['fee_arm'] == 'T'


def test_primary_denominator_uses_exact_origins_and_exposes_cross_arm_retries():
    c, t = aid_for('C'), aid_for('T')
    first = grade.grade_row(c, 'wallet', trace(c))
    retry = copy.deepcopy(first); retry['attempt_id'] = 'same-arm-retry'
    other = grade.grade_row(t, 'wallet', trace(t, origin=OTHER_SOURCE))
    unknown = grade.grade_row(c, 'wallet', trace(c, origin=None))
    pop = grade.fee_split_population([first, retry, other, unknown])
    assert len(pop['primary']) == 2 and len(pop['retries']) == 1 and len(pop['unknown_origins']) == 1
    cross = grade.grade_row(t, 'wallet', trace(t))
    pop = grade.fee_split_population([first, cross, other])
    assert len(pop['primary']) == 1 and len(pop['cross_arm_origins']) == 1
    assert 'FEE_cross_arm_origin' in first['problems']
    assert not grade.early_stops([first, cross, other])


def test_cli_includes_ordinary_population_and_prints_fee_cohorts(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'ledger.db'; c, t = aid_for('C'), aid_for('T')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE memecoin_entry_attempts(attempt_id,source_user_id,trace_json,'
                   'delivery_lane,network_id,decision_started_at)')
        for aid, lane, content in [(c, 'slot_lane_processed', trace(c)),
                                   (t, 'ordinary', trace(t, ordinary=True, origin=OTHER_SOURCE)),
                                   ('unknown', 'ordinary', {'substage_events': [
                                       {'name': 'nozomi_ab', 'arm': 'B', 'rule': RULE}]})]:
            db.execute('INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?)',
                       (aid, 'wallet', json.dumps(content), lane, 'solana', '2026-10-13'))
    monkeypatch.setattr(grade, 'env_wallets', lambda db: {'wallet': 'wallet'})
    monkeypatch.setattr(grade, 'key_hits', lambda *_: None)
    monkeypatch.setattr(grade.sys, 'argv', ['grade', '--db', str(path), '--read'])
    assert grade.main() == 0
    out = capsys.readouterr().out
    assert '2 signed sends' in out
    assert 'ordinary ungraded/missing signed proof: 1' in out
    assert 'fee C: primary=1' in out and 'fee T: primary=1' in out
    assert 'successful same-slot acquisitions=0' in out


def test_default_cli_retains_population_only_before_explicit_outcome_read(tmp_path, monkeypatch, capsys):
    path = tmp_path / 'ledger.db'; aid = aid_for('T')
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE memecoin_entry_attempts(attempt_id,source_user_id,trace_json,'
                   'delivery_lane,network_id,decision_started_at)')
        db.execute('INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?)',
                   (aid, 'wallet', json.dumps(trace(aid, terminal='filled', receipt=True)),
                    'slot_lane_processed', 'solana', '2026-10-13'))
    monkeypatch.setattr(grade, 'env_wallets', lambda db: {'wallet': 'wallet'})
    monkeypatch.setattr(grade, 'key_hits', lambda *_: None)
    monkeypatch.setattr(grade.sys, 'argv', ['grade', '--db', str(path)])
    assert grade.main() == 0
    out = capsys.readouterr().out
    assert 'fee T: primary=1' in out
    assert 'successful same-slot acquisitions' not in out


def test_opaque_and_conflicting_source_ids_cannot_add_independent_origins():
    aid = aid_for('T'); t = trace(aid, origin='opaque-j7-id')
    assert grade.grade_row(aid, 'wallet', t)['source_origin'] is None
    t['source_evidence']['source_initiation'] = {'tx_hash': SOURCE, 'mint': 'mint'}
    assert grade.grade_row(aid, 'wallet', t)['source_origin'] == ('wallet', SOURCE, 'mint')
    t['source_evidence']['source_sig'] = OTHER_SOURCE
    assert grade.grade_row(aid, 'wallet', t)['source_origin'] is None
