"""ALON's two-hop PumpSwap buy resumes only from its whole confirmed receipt."""
import dataclasses
import json
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace

import pytest

from sniper.execution import pumpswap_buy as ps
from sniper.services import memecoin_onchain_lane as O, memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S, memecoin_source_initiation as I
from sniper.services import memecoin_source_valuation as V
from test_memecoin_onchain_lane import ARMED_UID, NOW, _slot_attempt, _slot_conn, approved

TX = json.loads((Path(__file__).parent / 'fixtures/memecoin/alon_routed_buy_20261011.json').read_text())
OWNER = 'DAejzMs5cUeCCENNvapy9KWFwzwegh7LvcgNkZ6hnf1y'
MINT = 'ALoNmPYp9HxW5Gi4QHGRomXoBuwepwZWcygtvdEXzfBH'
TARGET_POOL = '782aJ74HWj1uxMAnf66jyiP46vNUwSHo4gFHoYoK8yoJ'
PUMP = 'pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn'


def alon_buy(monkeypatch):
    frame = {'params': {'result': {'context': {'slot': TX['slot']},
        'value': {'signature': TX['transaction']['signatures'][0], 'err': None,
                  'logs': TX['meta']['logMessages']}}}}
    events = S.routed_pump_buys(frame, True)
    routed = S.RoutedBuy(source_user_id=ARMED_UID, handle='fmpumpguy', owner=OWNER,
        signature=TX['transaction']['signatures'][0], slot=455671383, mint=MINT,
        token_delta_atomic=10908646100860, token_decimals=6, usdc_spent_atomic=1500000000,
        recv_monotonic=100., recv_utc=NOW.isoformat(), paired_monotonic=100.001,
        venue_events=events, requires_receipt_proof=True)
    pools = SimpleNamespace(get=lambda key: {
        '2uF4Xh61rDwxnG9woyxsVQP7zuA6kLFpb3NvnRQeoiSd':
            {'base_mint': PUMP, 'quote_mint': S.USDC_MINT, 'pump_migrated': True},
        TARGET_POOL: {'base_mint': MINT, 'quote_mint': PUMP, 'pump_migrated': True,
            'base_supply': 10**15, 'base_decimals': 6, 'quote_decimals': 6, 'virtual_quote': 0},
    }.get(key))
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({'pump_amm'}))
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_SLOT_LANE_MODE', 'live')
    buy, why = L.pump_slot_buy(routed, pools)
    assert why is None and buy.pool == TARGET_POOL
    assert buy.token_amount == 6951053888159
    return buy, pools


def alon_session(monkeypatch):
    buy, pools = alon_buy(monkeypatch)
    facts = V.value_source_buy(TX, OWNER, sol_usd=None, now=TX['blockTime'])
    proof = I.solana_receipt_initiation(TX, OWNER, facts)
    assert proof['payment_atomic'] == '1500000000'
    assert proof['acquired_atomic'] == '10908646100860'
    assert proof['route_payment_atomic'] == '1497150270'
    proof['source_user_id'] = ARMED_UID
    ev = SimpleNamespace(network_id='solana', signal_source='onchain_watch',
        fomo_event_id='onchain:' + buy.signature, mint=MINT, source_user_id=ARMED_UID,
        source_wallet=OWNER, source_block_number=455671383, source_initiation=proof)
    clock = SimpleNamespace(now=100., monotonic=lambda: clock.now)
    queued = []
    monkeypatch.setattr(L, 'CONFIRMED_PENDING', L.PendingLegs())
    handoff = L.RoutedHandoff(conn_factory=lambda: pytest.fail('receipt resume must use cached facts'),
                            wake=None, put=queued.append, clock=clock)
    handoff.remember(buy)
    return buy, pools, ev, handoff, queued, clock


def test_two_hop_alon_never_uses_the_target_leg_as_wallet_payment_proof(monkeypatch):
    buy, _ = alon_buy(monkeypatch)
    assert I.solana_program_initiation(buy, MINT) is None
    ev = SimpleNamespace(signal_source='slot_lane', fomo_event_id='slotlane:' + buy.signature,
                         source_initiation=None)
    assert I.source_initiation_refusal(ev, 'solana') == 'missing_receipt_proof'


def floor_setup(monkeypatch):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_MCAP_USD', 0.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET', {})
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO', 0.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND', {})
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD', 0.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_SOURCE_BUY_USD', 0.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT', 0.)
    monkeypatch.setattr(L.cfg, 'min_source_buy_usd_for', lambda *_: 1000.)


def test_alon_source_size_floor_judges_whole_payment_not_the_intermediate_pump_leg(monkeypatch):
    buy, pools = alon_buy(monkeypatch)
    floor_setup(monkeypatch)
    why, facts = L.admission_refusal(buy, armed_ids={ARMED_UID}, sol_usd=200.,
        kill_tripped=False, amm_pools=pools, venue_quote=(6, .0056))
    assert why is None
    assert facts['their_usd'] == 1500.


@pytest.mark.parametrize('basis, size', [('gross', 1500.), ('net', 1497.150270)])
def test_confirmed_alon_source_floor_preserves_size_basis_while_copy_average_uses_route(monkeypatch, basis, size):
    _, pools, ev, handoff, queued, clock = alon_session(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOURCE_SIZE_BASIS', basis)
    clock.now += .3
    assert handoff.offer(ev)
    resumed, = queued
    floor_setup(monkeypatch)
    why, facts = L.admission_refusal(resumed, armed_ids={ARMED_UID}, sol_usd=200.,
        kill_tripped=False, amm_pools=pools, venue_quote=(6, .0056))
    assert why is None and facts['their_usd'] == pytest.approx(size)
    assert resumed.routed.usdc_spent_atomic == 1497150270
    handoff.finish(resumed)


def test_confirmed_no_native_attempt_offers_cached_candidate_and_holds_detection(monkeypatch):
    buy, _, ev, handoff, queued, clock = alon_session(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', True)
    conn = _slot_conn(buy.signature)
    conn.execute('UPDATE onchain_wallet_detections SET mint=?,address=?,source_slot=?,source_valuation_json=?',
        (buy.mint, buy.owner, buy.slot, json.dumps({'source_initiation': ev.source_initiation})))
    clock.now += .3
    def hook(*_):
        pytest.fail('confirmed candidate should resume before the ordinary entry hook')
    hook.receipt_resume = handoff.offer
    deferred = {}
    cursor, hits = O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert hits[0]['result'] == {'status': 'deferred', 'reason': 'slot_lane_receipt_resume'}
    assert len(queued) == len(deferred) == 1
    assert queued[0].recv_monotonic == 100.
    _, hits = O.sweep_onchain_detections(conn, hook, cursor=cursor, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred=deferred)
    assert not hits and len(queued) == 1
    handoff.finish(queued[0])
    conn.close()


def test_exact_alon_receipt_resumes_selected_pool_with_whole_bound_payment(monkeypatch):
    buy, _, ev, handoff, queued, clock = alon_session(monkeypatch)
    clock.now += .3
    assert handoff.offer(ev)
    resumed, = queued
    assert resumed.pool == TARGET_POOL and resumed.token_amount == 6951053888159
    assert resumed.routed.token_delta_atomic == 10908646100860
    assert resumed.routed.usdc_spent_atomic == 1497150270
    assert resumed.source_initiation['payment_atomic'] == '1500000000'
    assert resumed.recv_monotonic == buy.recv_monotonic == 100.
    handoff.finish(resumed)


@pytest.mark.parametrize('route', [None, 0, -1, True, 1497150270.5, 'wrong', '1500000001'])
def test_alon_requires_known_bounded_route_payment_even_when_copy_cap_is_off(monkeypatch, route):
    _, _, ev, handoff, queued, clock = alon_session(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 0.)
    ev.source_initiation['route_payment_atomic'] = route
    clock.now += .3
    assert not handoff.offer(ev) and not queued


@pytest.mark.parametrize('damage', ['age', 'payer', 'uid', 'slot', 'mint', 'signature',
                                    'payment', 'acquired', 'program_proof'])
def test_exact_alon_resume_never_weakens_receipt_identity_or_original_clock(monkeypatch, damage):
    _, _, ev, handoff, queued, clock = alon_session(monkeypatch)
    clock.now += .501 if damage == 'age' else .3
    if damage != 'age':
        key, value = {
            'payer': ('payer', '11111111111111111111111111111111'),
            'uid': ('source_user_id', 'foreign'), 'slot': ('block_number', 455671384),
            'mint': ('mint', PUMP), 'signature': ('tx_hash', 'invalid'),
            'payment': ('payment_atomic', '1500000001'),
            'acquired': ('acquired_atomic', '10908646100861'),
            'program_proof': ('proof_version', I.SOLANA_PROGRAM_PROOF_VERSION),
        }[damage]
        ev.source_initiation[key] = value
    assert not handoff.offer(ev) and not queued


def test_alon_does_not_choose_between_two_target_pools(monkeypatch):
    buy, pools = alon_buy(monkeypatch)
    routed = dataclasses.replace(buy.routed, venue_events=buy.routed.venue_events +
                                (buy.routed.venue_events[-1],))
    assert L.pump_slot_buy(routed, pools) == (None, 'multi_pump_event')


def test_ordinary_success_path_does_not_offer_receipts_when_fast_only_is_off(monkeypatch):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', False)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', False, raising=False)
    conn = _slot_conn('ordinary')
    def hook(_conn, ev):
        return {'status': 'filled', 'mint': ev.mint}
    hook.receipt_resume = lambda _ev: pytest.fail('existing ordinary path must not acquire resume locks')
    _, hits = O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred={})
    assert hits[0]['result'] == {'status': 'filled', 'mint': 'Mintordinary'}
    conn.close()


def test_unreadable_pumpswap_post_reserves_refuse_without_throwing(monkeypatch):
    buy, pools = alon_buy(monkeypatch)
    def invalid(_event):
        raise ValueError('invalid retained fee')
    monkeypatch.setattr(ps, 'post_trade_reserves', invalid)
    assert L.amm_market_facts(buy, pools, 200., quote=(6, .0056)) == ('slot_lane_price_unreadable', {})


@pytest.mark.parametrize('state', ['signed', 'filled', 'policy'])
def test_confirmed_alon_cannot_resume_over_native_ownership_or_policy_refusal(monkeypatch, state):
    buy, _, ev, handoff, queued, clock = alon_session(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', True)
    conn = _slot_conn(buy.signature)
    conn.execute('UPDATE onchain_wallet_detections SET mint=?,address=?,source_slot=?,source_valuation_json=?',
        (MINT, OWNER, 455671383, json.dumps({'source_initiation': ev.source_initiation})))
    _slot_attempt(conn, buy.signature, terminal='rejected' if state == 'policy' else None,
                  pending={'signed': 'reserved', 'filled': 'confirmed'}.get(state),
                  reason='mcap over ceiling' if state == 'policy' else None)
    if state == 'signed':
        conn.execute("UPDATE memecoin_pending_entries SET tx_sig='signed-native-id'")
    clock.now += .3
    def hook(*_):
        pytest.fail('existing native owner or policy refusal must bypass the entry hook')
    hook.receipt_resume = handoff.offer
    _, hits = O.sweep_onchain_detections(conn, hook, cursor=0, now_fn=NOW.isoformat,
        max_age_s=45., approved_ids_fn=approved, deferred={})
    assert not queued
    assert hits[0]['result']['status'] == ('deferred' if state == 'signed' else 'skipped')
    conn.close()


def test_alon_refuses_two_distinct_cached_target_pools(monkeypatch):
    buy, pools = alon_buy(monkeypatch)
    cached_get = pools.get
    pools.get = lambda key: {**cached_get(key), 'base_mint': MINT} if cached_get(key) else None
    assert L.pump_slot_buy(buy.routed, pools) == (None, 'multi_pump_event')


def test_alon_receipt_does_not_resume_its_intermediate_pump_coin(monkeypatch):
    buy, pools, ev, handoff, queued, clock = alon_session(monkeypatch)
    handoff.awaiting.claim(buy.signature, clock.now)
    routed = dataclasses.replace(buy.routed, mint=PUMP)
    intermediate, why = L.pump_slot_buy(routed, pools)
    assert why is None and intermediate.pool != TARGET_POOL
    handoff.remember(intermediate)
    clock.now += .3
    assert not handoff.offer(ev) and not queued


def durable_owner(conn, buy, status):
    conn.execute('INSERT INTO memecoin_entry_attempts '
        '(attempt_id,origin_event_id,signal_source,decision_started_at,terminal_status,terminal_reason) '
        'VALUES (?,?,?,?,?,?)', ('durable', 'slotlane:' + buy.signature, 'slot_lane',
        (NOW - timedelta(seconds=300)).isoformat(), 'filled' if status == 'confirmed' else 'submitted_unknown', 'owned'))
    conn.execute('INSERT INTO memecoin_pending_entries(attempt_id,status,tx_sig) VALUES (?,?,?)',
        ('durable', status, 'native-signed'))
    _slot_attempt(conn, buy.signature, terminal='source_initiation_unknown', reason='missing_receipt_proof')


@pytest.mark.parametrize('state, outcome', [('reserved', 'in_flight'), ('confirmed', 'filled')])
def test_older_durable_native_owner_cannot_be_hidden_by_a_newer_proof_refusal(monkeypatch, state, outcome):
    buy, _, _, _, _, _ = alon_session(monkeypatch)
    conn = _slot_conn(buy.signature)
    durable_owner(conn, buy, state)
    assert O.slot_lane_verdict(conn, buy.signature, NOW.isoformat()) == (outcome, None)
    conn.close()


@pytest.mark.parametrize('proof_first', [False, True])
def test_background_resume_refuses_durable_owner_even_when_proof_arrives_before_candidate(monkeypatch, proof_first):
    buy, _, ev, _, _, clock = alon_session(monkeypatch)
    conn = _slot_conn(buy.signature)
    queued, proofs = [], []
    h = L.RoutedHandoff(conn_factory=lambda: pytest.fail('no mutable ledger handle'),
                       wake=None, put=queued.append, clock=clock)
    # The production callback is injected here; it runs only on the receipt/routed worker.
    h.owner_guard = lambda signature: O.slot_funded_owner(conn, signature)
    h.proof_put = proofs.append
    if proof_first:
        assert not h.offer(ev)
    durable_owner(conn, buy, 'reserved')
    h.remember(buy)
    if proof_first:
        assert not queued and len(proofs) == 1, 'the slot thread must enqueue proof without a ledger read'
        L.route_routed_buy(proofs[0], handoff=h, log=lambda *_: None, put=queued.append,
            amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True)
    else:
        assert not h.offer(ev)
    assert not queued
    conn.close()


def test_proof_first_owner_read_failure_stays_on_background_queue_and_blocks(monkeypatch):
    buy, _, ev, _, _, clock = alon_session(monkeypatch)
    conn = _slot_conn(buy.signature)
    queued, proofs = [], []
    h = L.RoutedHandoff(conn_factory=lambda: pytest.fail('no mutable ledger handle'), wake=None,
        put=queued.append, clock=clock, owner_guard=lambda sig: O.slot_funded_owner(conn, sig),
        proof_put=proofs.append)
    assert not h.offer(ev)
    conn.close()
    h.remember(buy)
    assert not queued and len(proofs) == 1
    assert L.route_routed_buy(proofs[0], handoff=h, log=lambda *_: None, put=queued.append,
        amm_pools=None, cpmm_on=False, pump_on=False, lane_live=True) == ('receipt_resume', False)
    assert not queued


def test_signed_attempt_without_pending_row_still_owns_exact_source(monkeypatch):
    buy, _, _, _, _, _ = alon_session(monkeypatch)
    conn = _slot_conn(buy.signature)
    durable_owner(conn, buy, 'reserved')
    conn.execute("UPDATE memecoin_entry_attempts SET tx_sig='native-signed' WHERE attempt_id='durable'")
    conn.execute("DELETE FROM memecoin_pending_entries WHERE attempt_id='durable'")
    assert O.slot_funded_owner(conn, buy.signature)
    assert O.slot_lane_verdict(conn, buy.signature, NOW.isoformat()) == ('in_flight', None)
    conn.close()
