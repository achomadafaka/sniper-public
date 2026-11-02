"""Confirmed signer proof resumes the cached native buy without resetting its receipt clock."""
import dataclasses
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_onchain_lane as O, memecoin_slot_lane as L
from sniper.services import memecoin_source_initiation as I
from sniper.execution import meteora_damm_v2_buy as D
from test_slot_lane_damm_v2 import CLASSIC, USER, _learned, _routed
from test_memecoin_onchain_lane import ARMED_UID, NOW, _slot_conn, _slot_attempt, approved


@pytest.mark.parametrize('fee_error', [0, -1, 1])
def test_rari_transfer_fee_receipt_resumes_only_for_exact_net_output(monkeypatch, fee_error):
    from sniper.services import memecoin_slot_signal as S
    f = json.loads((Path(__file__).parent / 'fixtures/memecoin/slot_rari_receipt_resume.json').read_text())
    frame = {'params': {'result': {'value': {'logs': f['logs']}}}}
    (swap,) = S._cpmm_swaps(frame, True)
    assert swap.output_amount == 5045720683197
    assert swap.output_transfer_fee == 151371620496
    swap = dataclasses.replace(swap, output_transfer_fee=swap.output_transfer_fee + fee_error)
    routed = S.RoutedBuy(source_user_id=f['source_user_id'], handle='OmakaseOnly', owner=f['owner'],
        signature=f['signature'], slot=f['slot'], mint=f['mint'],
        token_delta_atomic=f['token_delta_atomic'], token_decimals=6,
        usdc_spent_atomic=f['usdc_spent_atomic'], recv_monotonic=100., recv_utc='t',
        paired_monotonic=100.003, venue_events=(swap,))
    buy = L.cpmm_slot_buy(routed)
    ev = SimpleNamespace(network_id='solana', signal_source='onchain_watch',
        fomo_event_id='onchain:' + f['signature'], mint=f['mint'], source_user_id=f['source_user_id'],
        source_wallet=f['owner'], source_block_number=f['slot'], source_initiation=f['source_initiation'])
    queued = []
    monkeypatch.setattr(L, 'CONFIRMED_PENDING', L.PendingLegs())
    h = L.RoutedHandoff(conn_factory=lambda: pytest.fail('no DB read'), wake=None,
        put=queued.append, clock=SimpleNamespace(monotonic=lambda: 100.313))
    h.remember(buy)
    # A net leg ONE atom short of the receipt is part of it (a split route's leg is); a leg larger
    # than what the wallet received never is.
    assert h.offer(ev) is (fee_error >= 0)
    assert len(queued) == int(fee_error >= 0)
    if queued:
        assert queued[0].token_amount == 4894349062701 - fee_error
        assert queued[0].recv_monotonic == 100.
        h.finish(queued[0])


def setup(monkeypatch):
    clock = SimpleNamespace(now=100., monotonic=lambda: clock.now)
    routed = dataclasses.replace(_routed(CLASSIC), owner=USER, source_user_id=ARMED_UID,
                                 recv_monotonic=100.)
    buy, _ = D.damm_slot_buy(routed, _learned(CLASSIC))
    proof = dict(schema_version=1, proof_version=I.DIRECT_PROOF_VERSION, network_id='solana',
        tx_hash=buy.signature, block_number=buy.slot, mint=buy.mint, beneficiary=buy.owner,
        payer=buy.owner, source_user_id=buy.source_user_id,
        acquired_atomic=str(buy.token_amount), payment_atomic=str(buy.sol_amount))
    ev = SimpleNamespace(network_id='solana', signal_source='onchain_watch',
        fomo_event_id='onchain:'+buy.signature, mint=buy.mint, source_user_id=buy.source_user_id,
        source_wallet=buy.owner, source_block_number=buy.slot, source_initiation=proof)
    queued = []
    monkeypatch.setattr(L, 'CONFIRMED_PENDING', L.PendingLegs(), raising=False)
    h = L.RoutedHandoff(conn_factory=lambda: pytest.fail('resume opened a DB connection'),
                       wake=threading.Event(), put=queued.append, clock=clock)
    return buy, ev, queued, h, clock


def test_confirmed_receipt_requeues_once_and_holds_fallback_until_native_result(monkeypatch):
    buy, ev, queued, h, clock = setup(monkeypatch)
    h.remember(buy)
    clock.now += .3
    assert h.offer(ev)
    (resumed,) = queued
    assert resumed.recv_monotonic == buy.recv_monotonic and resumed.recv_utc == buy.recv_utc
    assert resumed.source_initiation == ev.source_initiation
    assert not h.offer(ev), 'one receipt cannot enqueue two native buys'
    conn = _slot_conn(buy.signature)
    _slot_attempt(conn, buy.signature, terminal='source_initiation_unknown', reason='missing_receipt_proof')
    assert O.slot_lane_outcome(conn, buy.signature, NOW.isoformat()) == 'in_flight'
    # Queued native work owns the source until its result/attempt is durable, even past freshness.
    clock.now += 2.
    assert O.slot_lane_outcome(conn, buy.signature, NOW.isoformat()) == 'in_flight'
    h.finish(resumed)
    assert h.wake.is_set()
    assert O.slot_lane_outcome(conn, buy.signature, NOW.isoformat()) == 'failed'
    conn.close()


@pytest.mark.parametrize('landed_failure', [False, True])
def test_onchain_sweep_fallback_requires_a_confirmed_native_chain_failure(monkeypatch, landed_failure):
    buy, ev, queued, h, clock = setup(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', True, raising=False)
    conn = _slot_conn(buy.signature)
    conn.execute('UPDATE onchain_wallet_detections SET mint=?,address=?,source_slot=?,source_valuation_json=?',
                 (buy.mint, buy.owner, buy.slot, json.dumps({'source_initiation': ev.source_initiation})))
    _slot_attempt(conn, buy.signature, terminal='source_initiation_unknown', reason='missing_receipt_proof')
    h.remember(buy)
    calls, deferred = [], {}
    def hook(c, event):
        if not getattr(event, 'native_failure_fallback', False):
            return {'status': 'rejected', 'reason': 'native_fast_lane_unavailable'}
        calls.append(event)
        c.execute('INSERT INTO memecoin_entry_attempts (attempt_id, origin_event_id, signal_source, '
                  'decision_started_at, terminal_status, terminal_reason, delivery_lane) VALUES (?,?,?,?,?,?,?)',
                  ('fallback', event.fomo_event_id, 'onchain_watch', NOW.isoformat(), 'rejected', 'test', event.delivery_lane))
        return {'status': 'rejected'}
    hook.receipt_resume = h.offer
    cursor, hits = O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert not calls and len(queued) == len(deferred) == 1
    assert hits[0]['result']['status'] == 'deferred'
    O.sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert not calls
    h.finish(queued[0])  # stale/busy refusal before a new native reservation
    if landed_failure:
        conn.execute("INSERT INTO memecoin_pending_entries(attempt_id,status,resolution_reason,tx_sig) VALUES (?,?,?,?)",
                     ('slot-'+buy.signature, 'failed', 'chain_error:custom', 'native-signed-tx'))
    O.sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert len(calls) == int(landed_failure) and not deferred
    O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert len(calls) == int(landed_failure) and len(queued) == 1
    conn.close()


@pytest.mark.parametrize('damage', ['missing', 'payer', 'uid', 'slot', 'mint', 'signature',
    'payment', 'acquired', 'age', 'wrong_buy_uid', 'wrong_buy_slot', 'wrong_buy_amount', 'program_proof'])
def test_resume_requires_exact_confirmed_identity_amounts_and_original_freshness(monkeypatch, damage):
    buy, ev, queued, h, clock = setup(monkeypatch)
    if damage.startswith('wrong_buy_'):
        field, value = {'wrong_buy_uid': ('source_user_id', 'other'),
                        'wrong_buy_slot': ('slot', buy.slot+1),
                        'wrong_buy_amount': ('token_amount', buy.token_amount+1)}[damage]
        buy = dataclasses.replace(buy, **{field: value})
    h.remember(buy)
    if damage == 'missing': ev.source_initiation = None
    elif damage == 'age': clock.now += L.MAX_SIGNAL_AGE_S+.001
    elif damage == 'program_proof': ev.source_initiation['proof_version'] = I.SOLANA_PROGRAM_PROOF_VERSION
    elif not damage.startswith('wrong_buy_'):
        field, value = {'payer': ('payer', '11111111111111111111111111111111'),
            'uid': ('source_user_id', 'other'), 'slot': ('block_number', buy.slot+1),
            'mint': ('mint', USER), 'signature': ('tx_hash', 'invalid'),
            'payment': ('payment_atomic', str(buy.sol_amount+1)),
            'acquired': ('acquired_atomic', str(buy.token_amount+1))}[damage]
        ev.source_initiation[field] = value
    assert not h.offer(ev) and not queued
    assert not L.CONFIRMED_PENDING.holds(buy.signature, clock.now)


def test_receipt_candidate_is_published_before_wake_and_native_consumer_uses_proof(monkeypatch, tmp_path):
    import sqlite3
    import time
    from sniper.persistence import db_memecoin as m
    from sniper.execution import pump_curve_buy as pcb
    from test_slot_lane_damm_v2 import _lane
    actual_guard = I.source_initiation_refusal
    buy, ev, queued, h, _ = setup(monkeypatch)
    buy = dataclasses.replace(buy, recv_monotonic=time.monotonic())
    h.clock = time
    c = sqlite3.connect(':memory:'); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    sent = []
    ln, _ = _lane(c, tmp_path, monkeypatch, mode='live', sent=sent)
    monkeypatch.setattr(I, 'source_initiation_refusal', actual_guard)
    monkeypatch.setattr(pcb, 'leg_clip_refusal', lambda *_: None)
    ln.armed_ids_fn = lambda: {buy.source_user_id}
    ln.routed_handoff = h
    class Wake:
        def set(self):
            assert h.awaiting.holds(buy.signature, time.monotonic())
            assert h.offer(ev), 'watcher wake must see the cached native candidate'
    ln.wake = Wake()
    assert ln.handle(buy)['reason'] == 'missing_receipt_proof' and not sent
    ln.wake = None
    result = ln.handle(queued[0])
    assert result['status'] == 'filled', result
    assert len(sent) == 1
    delivery = c.execute('SELECT delivery_lane FROM memecoin_entry_attempts ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    assert delivery == 'slot_lane_confirmed_receipt'
    h.finish(queued[0]); c.close()


@pytest.mark.parametrize('damage, why', [('age', 'age'), ('wrong_buy_slot', 'slot'),
                                         ('wrong_buy_amount', 'acquired'), ('expired', 'aged_out')])
def test_a_refused_resume_names_its_reason(monkeypatch, caplog, damage, why):
    """2026-10-06: two routed buys refused missing_receipt_proof were filled by the fallback at S+4
    and nothing said which resume check stopped them."""
    import logging
    buy, ev, queued, h, clock = setup(monkeypatch)
    if damage == 'wrong_buy_slot':
        buy = dataclasses.replace(buy, slot=buy.slot + 1)
    elif damage == 'wrong_buy_amount':
        buy = dataclasses.replace(buy, token_amount=buy.token_amount + 1)
    h.remember(buy)
    clock.now += {'age': L.MAX_SIGNAL_AGE_S - .001, 'expired': L.MAX_SIGNAL_AGE_S + .001}.get(damage, .1)
    if damage == 'age':
        h.awaiting._held[buy.signature] = (buy, clock.now + 1)      # held, but older than the gate
        clock.now += .002
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev) and not queued
    assert any('receipt resume refused %s ' % why in r.getMessage() and buy.signature in r.getMessage()
               for r in caplog.records)


def test_a_second_offer_of_a_taken_resume_is_not_named_a_miss(monkeypatch, caplog):
    """probablyinsider 9vCmcifh 2026-10-07 01:39:47Z: the resume took the buy, the lane's build
    aborted on leg_over_clip, and the mechanical fallback's own resume call (which filled position
    71271) logged 'not_awaiting', which read as a lost buy. A buy an earlier offer already took is
    named apart from one that aged out before its receipt came."""
    import logging
    buy, ev, queued, h, clock = setup(monkeypatch)
    h.remember(buy)
    clock.now += .1
    assert h.offer(ev) and len(queued) == 1
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev) and len(queued) == 1
    lines = [r.getMessage() for r in caplog.records if 'receipt resume refused' in r.getMessage()]
    assert len(lines) == 1 and 'refused already_offered ' in lines[0] and buy.signature in lines[0]
    h.finish(queued[0])


def test_a_split_route_resumes_on_its_venue_leg(monkeypatch):
    """KKK redemptionjizzy F4g7BYDm 22:30Z (operator: "next time we better fill"): the source paid
    780 USDC and got 18,291,076,272,685 atoms, of which the PumpSwap leg delivered
    17,717,395,355,313; another venue delivered the rest. The proof matched the routed receipt
    exactly, but the resume demanded the LEG equal the whole acquisition, refused, and fast-lane-only
    then refused the fallback. A leg that is part of the proven acquisition now resumes."""
    buy, ev, queued, h, clock = setup(monkeypatch)
    whole = buy.token_amount
    leg = whole * 17_717_395_355_313 // 18_291_076_272_685
    buy = dataclasses.replace(buy, token_amount=leg)
    h.remember(buy)
    clock.now += .29
    assert h.offer(ev) and len(queued) == 1
    assert queued[0].token_amount == leg and int(ev.source_initiation['acquired_atomic']) == whole
    h.finish(queued[0])


@pytest.mark.parametrize('leg', [0, -1, None])
def test_a_missing_or_empty_leg_never_resumes(monkeypatch, leg):
    buy, ev, queued, h, clock = setup(monkeypatch)
    buy = dataclasses.replace(buy, token_amount=leg)
    h.remember(buy)
    clock.now += .1
    assert not h.offer(ev) and not queued

@pytest.mark.parametrize('damage, why', [('no_proof', 'no_proof'), ('program_proof', 'proof_version'),
                                         ('payer', 'initiation_refused'), ('pending', 'pending_duplicate')])
def test_every_silent_refusal_of_a_remembered_buy_is_named(monkeypatch, caplog, damage, why):
    """Alpha 2026-10-07 01:5xZ: every False branch of offer() for a buy the lane remembered says
    why. A detection the lane never remembered (an ordinary fallback) still logs nothing."""
    import logging
    buy, ev, queued, h, clock = setup(monkeypatch)
    h.remember(buy)
    clock.now += .1
    if damage == 'no_proof':
        ev.source_initiation = None
    elif damage == 'program_proof':
        ev.source_initiation['proof_version'] = I.SOLANA_PROGRAM_PROOF_VERSION
    elif damage == 'payer':
        ev.source_initiation['payer'] = '11111111111111111111111111111111'
    else:
        L.CONFIRMED_PENDING.add(buy, deadline=1e9)
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev) and not queued
    assert any('receipt resume refused %s ' % why in r.getMessage() and buy.signature in r.getMessage()
               for r in caplog.records)


def test_an_ordinary_detection_the_lane_never_remembered_logs_nothing(monkeypatch, caplog):
    import logging
    buy, ev, queued, h, clock = setup(monkeypatch)
    ev.source_initiation = None
    with caplog.at_level(logging.WARNING, logger=L.logger.name):
        assert not h.offer(ev)
    assert not [r for r in caplog.records if 'receipt resume refused' in r.getMessage()]
