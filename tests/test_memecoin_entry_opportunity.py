import json
import sqlite3
import time
from types import SimpleNamespace

import base58
import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


ARM = '2026-10-18T17:00:00+00:00'
NOW = '2026-10-18T17:00:01+00:00'
OWNER = base58.b58encode(bytes([11]) * 32).decode()
UID = 'pump:' + OWNER
MINT = base58.b58encode(bytes([12]) * 32).decode()
SIG = base58.b58encode(bytes([13]) * 64).decode()
SECOND = base58.b58encode(bytes([14]) * 64).decode()


def proof(signature=SECOND, slot=99, *, native=False):
    from sniper.services import memecoin_source_initiation as initiation
    return dict(schema_version=1,
        proof_version=initiation.SOLANA_PROGRAM_PROOF_VERSION if native else initiation.DIRECT_PROOF_VERSION,
        network_id='solana', tx_hash=signature, block_number=slot, mint=MINT,
        beneficiary=OWNER, payer=OWNER, source_user_id=UID,
        acquired_atomic='1', payment_atomic='1',
        **({'program_id': '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'} if native else {}))


def ledger(path=':memory:'):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, UID, 'source')
    c.execute("UPDATE memecoin_wallets SET real_approved=1,real_chains='solana' WHERE source_user_id=?", (UID,))
    c.execute("INSERT INTO memecoin_wallet_approvals(source_user_id,action,actor,created_at,chain) VALUES(?, 'chain_arm','test',?,'solana')", (UID, ARM))
    c.commit()
    return c


def claim(c, signature=SIG, cohort=ARM):
    return m.claim_first_source_buy(c, source_user_id=UID, mint=MINT,
        source_signature=signature, source_slot=100, cohort_start=cohort, observed_at=NOW)


def test_first_refused_opportunity_is_consumed_without_a_position():
    c = ledger()
    first = claim(c)
    later = claim(c, SECOND)
    assert first['source_signature'] == later['source_signature'] == SIG
    assert c.execute('SELECT COUNT(*) FROM memecoin_positions').fetchone()[0] == 0


def test_same_transaction_from_another_feed_keeps_the_claim():
    c = ledger()
    assert claim(c) == claim(c)


def test_first_claim_survives_connection_restart(tmp_path):
    p = str(tmp_path / 'ledger.db')
    c = ledger(p)
    claim(c)
    c.close()
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    assert claim(c, SECOND)['source_signature'] == SIG


def test_new_armed_cohort_gets_its_own_first_opportunity():
    c = ledger()
    claim(c)
    assert claim(c, SECOND, '2026-10-19T00:00:00+00:00')['source_signature'] == SECOND


def test_recorded_prior_onchain_buy_seeds_a_missed_first():
    c = ledger()
    c.execute('INSERT INTO onchain_wallet_detections(signature,source_user_id,address,mint,network_id,detected_at,created_at,source_slot,source_valuation_json) VALUES(?,?,?,?,?,?,?,?,?)',
              (SECOND, UID, OWNER, MINT, 'solana', ARM, ARM, 99,
               json.dumps({'source_initiation': proof()})))
    c.commit()
    assert claim(c)['source_signature'] == SECOND


def test_prior_native_attempt_without_detection_still_consumes_first(monkeypatch):
    c = ledger()
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,network_id,mint,source_user_id,decision_started_at,trace_json) VALUES('old',?,'solana',?,?,?,?)",
              ('slotlane:'+SECOND, MINT, UID, ARM, json.dumps({'source_evidence': {
                  'source_slot': 99, 'source_wallet': OWNER, 'source_initiation': proof(native=True)}})))
    c.commit()
    assert evaluate(c, monkeypatch)[1] == 'first_source_buy_missed'


def test_historical_same_signature_without_slot_cannot_gain_a_new_deadline(monkeypatch):
    c = ledger()
    c.execute('INSERT INTO onchain_wallet_detections(signature,source_user_id,address,mint,network_id,detected_at,created_at,source_valuation_json) VALUES(?,?,?,?,?,?,?,?)',
              (SIG, UID, OWNER, MINT, 'solana', ARM, ARM,
               json.dumps({'source_initiation': proof(SIG)})))
    c.commit()
    assert evaluate(c, monkeypatch)[1] == 'source_buy_slot_unknown'


def evaluate(c, monkeypatch, **changes):
    from sniper.services import memecoin_entry_opportunity as policy
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    args = dict(source_user_id=UID, mint=MINT, source_signature=SIG, source_slot=100,
                source_pre_atomic=0, observed_at=NOW, head_fn=lambda: (101, 10.0), clock=lambda: 10.1)
    args.update(changes)
    return policy.evaluate(c, **args)


@pytest.mark.parametrize('observed_head', [100, 101, 102, 200])
def test_first_buy_is_admitted_at_any_slot_with_a_fresh_head(monkeypatch, observed_head):
    window, reason = evaluate(ledger(), monkeypatch, head_fn=lambda: (observed_head, 10.0))
    assert reason is None and window['source_slot'] == 100
    assert window['entry_max_slot'] is None and window['max_slot_delta'] is None


@pytest.mark.parametrize('head,reason', [((None, None), 'entry_slot_head_unknown'),
    ((200, 9.), 'entry_slot_head_stale')])
def test_unbounded_admission_still_requires_a_fresh_head(monkeypatch, head, reason):
    assert evaluate(ledger(), monkeypatch, head_fn=lambda: head)[1] == reason


def test_late_first_buy_never_admits_a_later_source_buy(monkeypatch):
    c = ledger()
    _, why = evaluate(c, monkeypatch, head_fn=lambda: (103, 10.0))
    assert why is None
    _, why = evaluate(c, monkeypatch, source_signature=SECOND, source_slot=103, head_fn=lambda: (103, 10.0))
    assert why == 'first_source_buy_missed'


@pytest.mark.parametrize('unknown_slot', [None, True, '100', -1])
def test_unknown_first_slot_still_consumes_first_opportunity(monkeypatch, unknown_slot):
    c = ledger()
    assert evaluate(c, monkeypatch, source_slot=unknown_slot)[1] == 'source_buy_slot_unknown'
    assert evaluate(c, monkeypatch, source_signature=SECOND)[1] == 'first_source_buy_missed'
    assert evaluate(c, monkeypatch)[1] == 'source_buy_slot_unknown'


@pytest.mark.parametrize('pre', [None, -1, True, '0', 1, 10**25])
def test_unknown_or_positive_source_holdings_never_admit_default_first(monkeypatch, pre):
    _, reason = evaluate(ledger(), monkeypatch, source_pre_atomic=pre)
    assert reason in {'source_first_buy_unknown', 'source_already_holding'}


def test_known_proof_of_same_first_transaction_can_resume_at_any_fresh_slot(monkeypatch):
    c = ledger()
    assert evaluate(c, monkeypatch, source_pre_atomic=None)[1] == 'source_first_buy_unknown'
    assert evaluate(c, monkeypatch)[0]['entry_max_slot'] is None
    window, reason = evaluate(c, monkeypatch, head_fn=lambda: (200, 10.0))
    assert reason is None and window['entry_max_slot'] is None


@pytest.mark.parametrize('later_exception', [False, True])
def test_same_source_signature_cannot_acquire_a_new_source_slot(monkeypatch, later_exception):
    c = ledger()
    if later_exception:
        monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', json.dumps({UID: {'max_slot_delta': 1, 'allow_later_buys': True}}))
        claim(c)
    sig = SECOND if later_exception else SIG
    assert evaluate(c, monkeypatch, source_signature=sig)[1] is None
    assert evaluate(c, monkeypatch, source_signature=sig, source_slot=105,
                    head_fn=lambda: (107, 10.0))[1] == 'source_buy_slot_mismatch'


def test_only_exact_qualified_wallet_can_allow_a_later_buy(monkeypatch):
    c = ledger()
    claim(c)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', json.dumps({UID: {'max_slot_delta': 1, 'allow_later_buys': True}}))
    window, reason = evaluate(c, monkeypatch, source_signature=SECOND, source_slot=104,
                              source_pre_atomic=123, head_fn=lambda: (105, 10.0))
    assert reason is None and window['entry_max_slot'] is None and window['exception'] is True
    assert window['max_slot_delta'] is None
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', '{}')
    assert evaluate(c, monkeypatch, source_signature=SECOND)[1] == 'first_source_buy_missed'


@pytest.mark.parametrize('raw', ['{', '{"*":{"max_slot_delta":10,"allow_later_buys":true}}',
    '{"'+UID+'":{"max_slot_delta":2,"allow_later_buys":false,"allow_later_buys":true}}',
    '{"'+UID+'":{"max_slot_delta":2,"allow_later_buys":false},"'+UID+'":{"max_slot_delta":4,"allow_later_buys":true}}',
    json.dumps({UID: {'max_slot_delta': True, 'allow_later_buys': True}}),
    json.dumps({UID: {'max_slot_delta': -1, 'allow_later_buys': True}})])
def test_malformed_or_wildcard_exceptions_do_not_widen_policy(monkeypatch, raw):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', raw)
    window, reason = evaluate(ledger(), monkeypatch)
    assert reason is None and window['entry_max_slot'] is None and not window['exception']


@pytest.mark.parametrize('delta', [2, 4, 8, 2**63])
def test_invalid_legacy_slot_exception_cannot_bypass_first_buy(monkeypatch, delta):
    from sniper.services import memecoin_entry_opportunity as policy
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON',
                        json.dumps({UID: {'max_slot_delta': delta, 'allow_later_buys': True}}))
    assert policy.exceptions() == {}
    c = ledger()
    window, reason = evaluate(c, monkeypatch, head_fn=lambda: (200, 10.0))
    assert reason is None and window['entry_max_slot'] is None
    assert evaluate(c, monkeypatch, source_signature=SECOND, source_slot=104,
                    head_fn=lambda: (105, 10.0))[1] == 'first_source_buy_missed'


def test_legacy_source_slot_only_exception_does_not_restore_slot_expiry(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON',
                        json.dumps({UID: {'max_slot_delta': 0, 'allow_later_buys': False}}))
    c = ledger()
    window, reason = evaluate(c, monkeypatch, head_fn=lambda: (100, 10.0))
    assert reason is None and window['source_slot'] == 100 and window['entry_max_slot'] is None
    assert evaluate(c, monkeypatch, head_fn=lambda: (200, 10.0))[1] is None


def test_late_native_first_claim_precedes_selection_and_blocks_its_successor(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    c = ledger()
    monkeypatch.setattr(lane, 'stale_input', lambda *_a: 'balances')
    clock = SimpleNamespace(monotonic=lambda: 10.1, perf_counter=lambda: 10.1)
    native = lane.SlotLane(conn=c, executor={}, build_fn=None,
        snapshot_fn=lambda: SimpleNamespace(sol_usd=100.),
        armed_ids_fn=lambda: {UID}, kill_tripped_fn=lambda: False,
        now_iso_fn=lambda: NOW, lock_key='test-first-source', mode='live', clock=clock,
        head_slot_fn=lambda: (200, 10.0))
    buy = SimpleNamespace(venue='pump_curve', source_user_id=UID, owner=OWNER, mint=MINT,
        signature=SIG, slot=100, source_pre_atomic=0, recv_monotonic=10.0,
        refusal=None, graduation=None, ondemand=None, rabbit=None)
    buy.source_initiation = dict(schema_version=1, proof_version='direct-wallet-paid-v1',
        network_id='solana', tx_hash=SIG, block_number=100, source_user_id=UID,
        mint=MINT, beneficiary=OWNER, payer=OWNER, acquired_atomic='100', payment_atomic='10')
    assert native._handle(buy)['reason'] == 'slot_lane_stale_balances'
    buy.signature, buy.slot = SECOND, 103
    buy.source_initiation = dict(buy.source_initiation, tx_hash=SECOND, block_number=103)
    assert native._handle(buy)['reason'] == 'first_source_buy_missed'


def test_shared_hook_keeps_startup_gate_after_late_first_buy_claim(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    from sniper.services.memecoin_slot_signal import HEAD
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    monkeypatch.setitem(HEAD, 'slot', 103)
    monkeypatch.setitem(HEAD, 'mono', time.monotonic())
    monkeypatch.setattr(runner, '_start_trace_best_effort', lambda *a, **k: None)
    monkeypatch.setattr(runner, '_entry_startup_refusal', lambda *a: {'status': 'rejected', 'reason': 'test_startup_hold'})
    ev = SimpleNamespace(fomo_event_id='onchain:'+SIG, source_user_id=UID, source_handle='source',
        source_wallet=OWNER, source_block_number=100, source_pre_atomic=0, mint=MINT,
        network_id='solana', signal_source='onchain_watch', side='buy', created_at=NOW)
    ev.source_initiation = dict(schema_version=1, proof_version='direct-wallet-paid-v1',
        network_id='solana', tx_hash=SIG, block_number=100, source_user_id=UID,
        mint=MINT, beneficiary=OWNER, payer=OWNER, acquired_atomic='100', payment_atomic='10')
    c = ledger()
    res = runner.real_entry_hook(c, ev, {}, now_fn=lambda: NOW, trade_size=50,
        max_age_s=10, resolved_identity=SimpleNamespace(status='resolved', network_id='solana'))
    assert res['reason'] == 'test_startup_hold'
    assert c.execute('SELECT source_signature FROM memecoin_first_source_buys').fetchone()[0] == SIG


@pytest.mark.parametrize('pre', [None, 0, 123])
def test_confirmed_event_keeps_atomic_pre_evidence_through_serialization(pre):
    from sniper.services import memecoin_onchain_lane as lane
    ev = lane.OnchainEv(dict(mint=MINT, source_user_id=UID, handle='source',
        network_id='solana', address=OWNER, source_slot=100, source_price=1,
        source_size_usd=100, signature=SIG, detected_at=NOW,
        source_valuation_json=json.dumps({'source_pre_atomic':pre})))
    assert ev.source_pre_atomic == pre
    back = lane.event_from_retry_payload(json.loads(json.dumps(lane.retry_payload(ev))))
    assert back.source_pre_atomic == back.source_evidence['source_pre_atomic'] == pre


@pytest.mark.parametrize('status', ['skipped_chase', 'aborted'])
def test_sol_entry_miss_never_schedules_dip_or_transient_retry(monkeypatch, status):
    from sniper.services import memecoin_real_runner as runner
    from sniper.services import memecoin_dip_retry
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_ENTRY_RETRY_WINDOW_S', 90)
    monkeypatch.setattr(runner, '_is_transient_entry_miss', lambda _: True)
    monkeypatch.setattr(runner, '_retry_payload', lambda _: {'source_user_id':UID})
    monkeypatch.setattr(memecoin_dip_retry, 'maybe_enqueue', lambda *a, **k: pytest.fail('delayed retry queued'))
    monkeypatch.setattr(m, 'enqueue_entry_retry', lambda *a, **k: pytest.fail('entry retry queued'))
    ev = SimpleNamespace(side='buy', network_id='solana', source_user_id=UID, mint=MINT,
                         fomo_event_id='onchain:'+SIG, signal_source='onchain_watch')
    assert runner._maybe_enqueue_retry(ledger(), ev, {'status':status}, NOW) is False
