"""Historical first-buy anchors require the same local source-initiation proof as live admission."""
import json

import base58
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_source_initiation as initiation
from test_memecoin_entry_opportunity import ledger, claim, proof, UID, OWNER, MINT, SIG, SECOND, ARM, NOW

SPOOF = base58.b58encode(bytes([15]) * 64).decode()


def attempt(c, signature=SECOND, *, source_slot=99, evidence=None, seen=ARM, suffix=''):
    source = dict(source_wallet=OWNER, source_slot=source_slot)
    if evidence is not None:
        source['source_initiation'] = evidence
    c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,network_id,mint,'
        'source_user_id,decision_started_at,trace_json,signal_source) VALUES(?,?,?,?,?,?,?,?)',
        ('history-' + signature + suffix, 'slotlane:' + signature, 'solana', MINT, UID, seen,
         json.dumps({'source_evidence': source}), 'slot_lane'))
    c.commit()


def detection(c, signature=SECOND, *, source_slot=99, evidence=None, seen=ARM):
    c.execute('INSERT INTO onchain_wallet_detections(signature,source_user_id,address,mint,network_id,'
        'detected_at,created_at,source_slot,source_valuation_json) VALUES(?,?,?,?,?,?,?,?,?)',
        (signature, UID, OWNER, MINT, 'solana', seen, seen, source_slot,
         json.dumps({'source_initiation': evidence})))
    c.commit()


@pytest.mark.parametrize('add', [attempt, detection])
def test_unproven_history_cannot_poison_the_next_verified_first(add):
    c = ledger()
    add(c)
    assert claim(c)['source_signature'] == SIG


@pytest.mark.parametrize('damage', ['signature', 'owner', 'mint', 'uid', 'slot', 'program', 'version'])
def test_bad_historical_proof_is_skipped_before_the_next_valid_native(damage):
    c = ledger()
    bad = proof(SPOOF, native=True)
    key, value = dict(signature=('tx_hash', SECOND), owner=('payer', MINT), mint=('mint', OWNER),
        uid=('source_user_id', 'wrong'), slot=('block_number', 98),
        program=('program_id', OWNER), version=('proof_version', 'unknown'))[damage]
    bad[key] = value
    attempt(c, SPOOF, evidence=bad)
    attempt(c, SECOND, evidence=proof(native=True), seen='2026-10-18T17:00:00.5+00:00')
    assert claim(c)['source_signature'] == SECOND


@pytest.mark.parametrize('add,native', [(attempt, True), (detection, False)])
def test_valid_known_program_and_confirmed_history_still_consume_first(add, native):
    c = ledger()
    add(c, evidence=proof(native=native))
    assert claim(c)['source_signature'] == SECOND


def test_current_initial_trace_without_proof_does_not_make_its_first_slot_unknown():
    c = ledger()
    attempt(c, SIG, source_slot=None, seen=NOW)
    first = claim(c)
    assert first['source_signature'] == SIG and first['source_slot'] == 100


def test_valid_historical_proof_can_validate_identity_without_replacing_a_missing_slot():
    c = ledger()
    detection(c, SIG, source_slot=None, evidence=proof(SIG))
    first = claim(c)
    assert first['source_signature'] == SIG and first['source_slot'] is None


def test_historical_budget_holds_before_skipping_unseen_candidates(monkeypatch):
    c = ledger()
    calls = []
    check = initiation.source_initiation_refusal
    def counted(ev, chain):
        calls.append(ev.fomo_event_id)
        return check(ev, chain)
    monkeypatch.setattr(initiation, 'source_initiation_refusal', counted)
    for i in range(17):
        signature = base58.b58encode(bytes([30 + i]) * 64).decode()
        attempt(c, signature, evidence=proof(SECOND), seen=f'2026-10-18T17:00:00.{i:03}+00:00')
    with pytest.raises(RuntimeError, match='historical.*budget'):
        claim(c)
    assert len(calls) == 16
    assert c.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0
