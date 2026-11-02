"""Exotic PumpSwap admission uses fresh, amount-bound prewarmed reverse evidence only."""
import sqlite3
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.execution import pump_curve_buy as P, pumpswap_buy as PS, raydium_launchlab_buy as LL
from sniper.persistence import db_memecoin as M
from sniper.services import memecoin_slot_lane as L
from test_slot_lane_pump_nonsol import SPCX, USDC, SNAP, _builder, _slot_buy, _leg
from test_slot_lane_exotic_warm_leg import Clock


def model():
    # Post-source reserves 1,000 base / 1,000 quote; spend100 buys90, and selling90
    # after our buy returns98. All amounts atomic; no floating rate/decimal assumptions.
    event = dict(pool='pool', pool_base_token_reserves=1000, base_amount_out=0,
                 pool_quote_token_reserves=1000, quote_amount_in_with_lp_fee=0)
    facts = dict(pool='pool', quote_mint='quote', virtual_quote=0)
    leg = dict(usdc_atomic=100, min_out_lamports=100, out_lamports=100,
               fetched_at=50., output_mint='quote', quote={},
               reverse=dict(input_mint='quote', output_mint=P.USDC_MINT,
                            in_atomic=98, out_usdc_atomic=98, min_usdc_atomic=97,
                            usdc_atomic=100, quote_spend_atomic=100, fetched_at=50.))
    return event, facts, leg


def test_reverse_minimum_is_kept_unchanged_when_current_return_has_surplus():
    event, facts, leg = model()
    leg['reverse']['in_atomic'] = 97
    got = PS.exotic_round_trip(event, facts, leg, clock_now=50., max_age_s=3.)
    assert got['state'] == 'measured'
    assert got['quote_back_atomic'] == 98
    assert got['usdc_back_atomic'] == 97
    assert got['cost_pct'] == pytest.approx(3.)


def test_reverse_for_more_input_than_current_pool_return_refuses_without_scaling():
    event, facts, leg = model()
    leg['reverse']['in_atomic'] = 100
    got = PS.exotic_round_trip(event, facts, leg, clock_now=50., max_age_s=3.)
    assert got == {'state': 'unmeasured', 'reason': 'reverse_input_exceeds_return'}


@pytest.mark.parametrize('field,value', [
    ('fetched_at', 'bad'), ('fetched_at', 51.), ('fetched_at', 46.99),
    ('fetched_at', float('nan')), ('fetched_at', 49.9), ('in_atomic', -1), ('in_atomic', True),
    ('min_usdc_atomic', 0), ('min_usdc_atomic', 99), ('input_mint', 'other'),
    ('output_mint', P.WSOL_MINT), ('usdc_atomic', 101), ('quote_spend_atomic', 101),
])
def test_bad_reverse_evidence_is_unknown_not_a_measurement(field, value):
    event, facts, leg = model()
    leg['reverse'][field] = value
    got = PS.exotic_round_trip(event, facts, leg, clock_now=50., max_age_s=3.)
    assert got['state'] == 'unmeasured'


def test_exact_age_boundary_is_fresh_and_no_rounding_hides_a_cap_excess():
    event, facts, leg = model()
    leg['usdc_atomic'] = leg['reverse']['usdc_atomic'] = 1_000_000
    leg['reverse'].update(out_usdc_atomic=900_000, min_usdc_atomic=899_999)
    got = PS.exotic_round_trip(event, facts, leg, clock_now=53., max_age_s=3.)
    assert got['state'] == 'measured' and got['cost_pct'] > 10.


def _warm(monkeypatch, *, bad=None, entries=1, reverse_status=200, reverse_delay_s=0.):
    clock = Clock(50.)
    builder = _builder(SPCX)
    builder.clock = clock.monotonic
    facts = builder.pools.cached(SPCX['pool'])
    facts = builder.pools.publish_quote_snapshot(SPCX['pool'], expected_facts=facts,
        value=facts['mint_values'][SPCX['quote_mint']], read_at=50.) if facts.get('mint_values') else facts
    calls = []
    class Http:
        def request(self, method, url, **kw):
            calls.append((method, url))
            q = parse_qs(urlsplit(url).query)
            if method == 'POST':
                return 200, {'swapInstruction': {'programId': P.SYSTEM_PROGRAM, 'data': '',
                    'accounts': [{'pubkey': builder.user, 'isSigner': True, 'isWritable': True}]}}, None
            reverse = q['inputMint'][0] != P.USDC_MINT
            if reverse:
                clock.t += reverse_delay_s
            mint = SPCX['quote_mint']
            quote = dict(inputMint=mint if reverse else P.USDC_MINT,
                         outputMint=P.USDC_MINT if reverse else mint, inAmount=q['amount'][0],
                         outAmount='10000000' if reverse else '40000',
                         otherAmountThreshold='9900000' if reverse else '39400',
                         swapMode='ExactIn', slippageBps=P.DEFAULT_LEG_SLIPPAGE_BPS,
                         routePlan=[{'swapInfo': {'ammKey': SPCX['pool'], 'label': 'fixture'}}])
            if reverse and bad:
                quote.update(bad)
            return reverse_status if reverse else 200, quote, None
    warm = LL.WarmExoticLegs(clock=clock, http=Http())
    monkeypatch.setattr(warm, '_prepare_quote_facts', lambda entry: True)
    warm.set_clip_inventory(tuple(10_000_000 + i for i in range(entries)))
    for i in range(entries):
        warm.note('pump_amm', SPCX['quote_mint'], 10_000_000 + i, leg=None,
                  builder=builder, facts=facts, buy=_slot_buy(SPCX))
    return warm, builder, clock, calls, facts


def test_reverse_is_quoted_off_path_for_the_modeled_return_and_binds_the_floor(monkeypatch):
    warm, builder, clock, calls, facts = _warm(monkeypatch)
    warm.refresh_once()
    assert [c[0] for c in calls] == ['GET', 'POST', 'GET']
    leg = warm.get('pump_amm', SPCX['quote_mint'], 10_000_000, builder=builder, facts=facts)
    reverse = leg['reverse']
    assert reverse['in_atomic'] < leg['min_out_lamports']
    assert parse_qs(urlsplit(calls[-1][1]).query)['amount'] == [str(reverse['in_atomic'])]
    assert reverse['min_usdc_atomic'] == 9_900_000
    assert PS.exotic_round_trip(PS.parse_buy_event(SPCX['buy_event_log']), facts, leg,
                               clock_now=clock.t, max_age_s=3.)['usdc_back_atomic'] == 9_900_000


@pytest.mark.parametrize('bad', [{'inputMint': 'other'}, {'inAmount': '1'}, {'swapMode': 'ExactOut'},
                               {'otherAmountThreshold': '10000001'}, {'routePlan': []}])
def test_bad_reverse_quote_cannot_be_published_as_usable_evidence(monkeypatch, bad):
    warm, _, _, _, _ = _warm(monkeypatch, bad=bad)
    warm.refresh_once()
    leg = next(iter(warm._entries.values()))['leg']
    assert leg is not None and leg.get('reverse') is None


def test_two_three_phase_legs_refresh_fairly_inside_the_existing_four_start_budget(monkeypatch):
    warm, _, clock, calls, _ = _warm(monkeypatch, entries=2)
    warm.refresh_once()
    assert len(calls) == 3
    assert sum(e['leg'] is not None for e in warm._entries.values()) == 1
    clock.t += 1.5
    warm.refresh_once()
    assert len(calls) == 6
    assert all(e['leg'] is not None and e['leg'].get('reverse') for e in warm._entries.values())


def _live(monkeypatch, tmp_path, detail, *, builder=None):
    from sniper.execution import exec_lock
    import sniper.services.memecoin_real_runner as R
    monkeypatch.setattr(exec_lock, '_lock_path', lambda key, lock_dir=None: str(tmp_path / 'wallet.lock'))
    for name, value in [('MEMECOIN_REAL_ENABLED', True), ('MEMECOIN_FUNDED_REGIME_RISK_SCALE', 1.),
                        ('MEMECOIN_REAL_MAX_MCAP_USD', 0.), ('MEMECOIN_REAL_CHASE_MAX_PCT', 30.),
                        ('MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP', True),
                        ('MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE', {}),
                        ('MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({'pump_amm'})),
                        ('MEMECOIN_SOLANA_SLOT_LANE_MODE', 'live')]:
        monkeypatch.setattr(L.cfg, name, value)
    monkeypatch.setattr(R, 'source_buy_family_reason', lambda **kw: (None, {}))
    c = sqlite3.connect(tmp_path / 'ledger.db')
    c.row_factory = sqlite3.Row
    M.ensure_memecoin_tables(c)
    now = time.monotonic()
    snap = L.PrefetchSnapshot(sol_usd=150., usdc_balance=400., sol_balance=50., blockhash=SNAP.blockhash,
        last_valid_height=1, observed_mono={k: now for k in ('sol_usd', 'balances', 'blockhash')})
    b = builder or _builder(USDC)
    sends = []
    def execute(*args, **kw):
        sends.append(kw)
        return {'status': 'filled', 'position_id': 1}
    def build(buy, clip, snap, **kw):
        return 'MESSAGE', {'target_mint': b.pools.cached(USDC['pool'])['base_mint'], **detail}
    lane = L.SlotLane(conn=c, executor={'slot_execute_fn': execute},
        build_fn=L.dispatch_build({'pump_amm': b}) if builder else build,
        snapshot_fn=lambda: snap, armed_ids_fn=lambda: {'u1'}, kill_tripped_fn=lambda: False,
        now_iso_fn=lambda: '2026-10-17T20:00:00+00:00', lock_key='fixture', mode='live',
        amm_pools=b.pools, shadow_clip_fn=lambda: 10., sol_buffer_cap_usd=1000.)
    return c, lane, sends, now


@pytest.mark.parametrize('evidence', [None, {'state': 'unmeasured', 'reason': 'no_reverse_leg'},
                                    {'state': 'measured', 'cost_pct': float('nan')},
                                    {'state': 'measured', 'cost_pct': 10.0001}])
def test_missing_invalid_or_over_cap_evidence_durably_aborts_before_execute(monkeypatch, tmp_path, evidence):
    detail = {} if evidence is None else {'exotic_round_trip': evidence}
    c, lane, sends, now = _live(monkeypatch, tmp_path, detail)
    monkeypatch.setattr(L.cfg, 'entry_roundtrip_cap_pct', lambda *a: 10.)
    try:
        result = lane.handle(_slot_buy(USDC, recv_monotonic=now))
        assert result['status'] == 'aborted', result
        assert not sends
        pending = M.pending_row(c, result['pending_id'])
        assert pending['status'] == 'failed' and pending.get('tx_sig') is None
        row = c.execute('SELECT terminal_status,terminal_reason,trace_json FROM memecoin_entry_attempts').fetchone()
        assert row['terminal_status'] == 'aborted' and 'exotic_round_trip' in row['terminal_reason']
        assert 'exotic_round_trip' in row['trace_json']
    finally:
        c.close()


@pytest.mark.parametrize('reverse_status,bad,delay_s', [
    (200, None, 0.), (200, {'inputMint': 'other'}, 0.), (503, None, 0.),
    (401, None, 0.), (429, None, 0.), (200, None, .501),
])
@pytest.mark.parametrize('forward_warm', [False, True])
def test_first_cold_buy_has_reverse_proof_or_stays_blocked_without_fire_io(
        monkeypatch, tmp_path, reverse_status, bad, delay_s, forward_warm):
    import dataclasses
    from test_slot_lane_cpmm import _parsed_mint
    warm, builder, clock, calls, facts = _warm(monkeypatch, bad=bad, reverse_status=reverse_status,
                                             reverse_delay_s=delay_s)
    # This fixture's exotic source initiation receipt is outside this admission test.
    monkeypatch.setattr('sniper.services.memecoin_source_initiation.source_initiation_refusal', lambda *a: None)
    warm._entries.clear()  # first use, no successful entry or warm evidence exists yet
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True)
    def funded(method, path, body=None, *, http=None):
        request = warm._http.request if http is None else http
        status, payload, _ = request(method, path, body=body)
        return status, payload
    monkeypatch.setattr(P, '_funded_jup_call', funded)
    builder.read_rpc = lambda *_: {'value': _parsed_mint(decimals=6)}
    requeued = []
    worker = LL.OnDemandLegWorker(builders={'pump_amm': builder},
        admission_fn=lambda buy, quote: None, snapshot_fn=lambda: SNAP,
        clip_fn=lambda: 10., offer_fn=lambda *a: None, start=False,
        live_fn=lambda venue: True, requeue_fn=requeued.append, warm=warm,
        clock=SimpleNamespace(monotonic=clock.monotonic))
    c, lane, sends, _ = _live(monkeypatch, tmp_path, {}, builder=builder)
    lane.clock = SimpleNamespace(monotonic=clock.monotonic, perf_counter=time.perf_counter)
    lane.snapshot_fn = lambda: L.PrefetchSnapshot(sol_usd=150., usdc_balance=400., sol_balance=50.,
        blockhash=SNAP.blockhash, last_valid_height=1,
        observed_mono={key: clock.t for key in ('sol_usd', 'balances', 'blockhash')})
    monkeypatch.setattr(L.cfg, 'entry_roundtrip_cap_pct', lambda *a: 10.)
    try:
        if forward_warm:
            warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=builder, facts=facts)
            warm.refresh_once()
            original_leg = warm.get('pump_amm', SPCX['quote_mint'], 10_000_000,
                                    builder=builder, facts=facts)
            assert original_leg is not None and original_leg.get('reverse') is None
            assert [method for method, _ in calls] == ['GET', 'POST']
            assert warm.stats['http_calls'] == 2
            calls.clear()
        first = _slot_buy(SPCX, signature='cold-first', recv_monotonic=clock.t)
        deadline = first.recv_monotonic + L.MAX_SIGNAL_AGE_S
        lane.pending_legs.add(first, deadline=deadline)
        processed = worker.process(first)
        assert [method for method, _ in calls] == (['GET'] if forward_warm else ['GET', 'POST', 'GET'])
        if reverse_status in (401, 429):
            assert processed['reason'] == builder.REASON_PREFIX + '_leg_rate_limited'
            assert not requeued and not sends
            if forward_warm:
                assert original_leg.get('reverse') is None and warm.stats['http_calls'] == 2
            else:
                assert not warm.keys()
            before = list(calls)
            assert worker.process(first)['reason'] == builder.REASON_PREFIX + '_leg_backoff'
            assert calls == before
            return
        assert processed['status'] == 'requeued'
        assert warm.keys() == [('pump_amm', SPCX['quote_mint'], 10_000_000)]
        assert next(iter(warm._entries.values()))['buy'] is first
        resumed = requeued[-1]
        assert resumed.signature == first.signature and resumed.recv_monotonic == first.recv_monotonic
        assert resumed.mint == first.mint and resumed.trade_event_log == first.trade_event_log
        leg = resumed.ondemand['leg']
        assert processed['warm_leg'] is forward_warm
        if forward_warm:
            assert leg is not original_leg and original_leg.get('reverse') is None
            assert warm.stats['http_calls'] == 2
        assert leg['usdc_atomic'] == 10_000_000 and resumed.ondemand['requested_clip_usd'] is None
        if reverse_status == 200 and bad is None:
            reverse = leg['reverse']
            assert reverse['fetched_at'] == leg['fetched_at'] == first.recv_monotonic
            assert reverse['min_usdc_atomic'] == 9_900_000
            assert parse_qs(urlsplit(calls[-1][1]).query)['amount'] == [str(reverse['in_atomic'])]
        else:
            assert leg.get('reverse') is None
        before = list(calls)
        def forbidden(*a, **kw):
            pytest.fail('entry fire performed a synchronous provider read')
        monkeypatch.setattr(P, '_funded_jup_call', forbidden)
        builder.jup_call = builder.rpc = builder.pools.rpc = builder.read_rpc = forbidden
        result = lane.handle(resumed)
        assert calls == before
        if delay_s:
            assert result == {'status': 'rejected', 'reason': L.EXOTIC_AGED_OUT_REASON}
            assert not sends and not lane.pending_legs.holds(first.signature, now=clock.t)
        elif reverse_status != 200 or bad:
            assert result['status'] == 'aborted' and 'no_reverse_leg' in result['reason'], result
            assert not sends and M.pending_row(c, result['pending_id'])['status'] == 'failed'
        else:
            assert result['status'] == 'filled', result
            assert len(sends) == 1
            second = dataclasses.replace(first, signature='warm-second', recv_monotonic=clock.t)
            assert worker.process(second)['warm_leg'] is True
            assert requeued[-1].ondemand['leg'] is leg and calls == before
    finally:
        c.close()


@pytest.mark.parametrize('clip', [9., 11.])
def test_fire_cannot_use_a_reverse_leg_for_a_different_approved_clip(monkeypatch, clip):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({'pump_amm'}))
    b = _builder(SPCX)
    leg = _leg(SPCX['quote_mint'], 40_000)
    def forbidden(*a, **kw):
        pytest.fail('wrong clip triggered an entry provider call')
    b.jup_call = b.rpc = b.pools.rpc = forbidden
    message, reason = b.build(_slot_buy(SPCX), clip, SNAP, leg=leg, quote=(6, 250., 0, 0))
    assert message is None and reason in ('leg_over_clip', 'leg_under_clip', 'exotic_round_trip_clip_mismatch')


def test_switch_off_restores_legacy_exotic_path_without_reverse_or_gate(monkeypatch, tmp_path):
    warm, _, _, calls, _ = _warm(monkeypatch)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP', False)
    warm.refresh_once()
    assert [method for method, _ in calls] == ['GET', 'POST']
    c, lane, sends, now = _live(monkeypatch, tmp_path, {})
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_ROUND_TRIP', False)
    try:
        assert lane.handle(_slot_buy(USDC, recv_monotonic=now))['status'] == 'filled'
        assert len(sends) == 1
    finally:
        c.close()


def test_sell_model_rounds_each_fee_up_without_overstating_proceeds():
    # gross=1000*100//1100=90; each independent nonzero fee rounds to one atom.
    assert PS.expected_quote_out(100, 1000, 1000, 0, (1, 1, 1)) == 87


def test_retained_event_from_another_pool_cannot_warm_a_reverse(monkeypatch):
    warm, _, _, calls, _ = _warm(monkeypatch)
    next(iter(warm._entries.values()))['buy'] = _slot_buy(USDC)
    warm.refresh_once()
    assert [method for method, _ in calls] == ['GET', 'POST']
    assert next(iter(warm._entries.values()))['leg']['reverse'] is None


@pytest.mark.parametrize('stamp', [None, 'bad', True, float('nan'), 51., 46.99])
def test_bad_forward_clock_refuses_build_without_provider_io(monkeypatch, stamp):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({'pump_amm'}))
    b = _builder(SPCX)
    leg = _leg(SPCX['quote_mint'], 40_000)
    leg['fetched_at'] = stamp
    def forbidden(*a, **kw):
        pytest.fail('invalid clock triggered an entry provider call')
    b.jup_call = b.rpc = b.pools.rpc = forbidden
    message, reason = b.build(_slot_buy(SPCX), 10., SNAP, leg=leg, quote=(6, 250., 0, 0))
    assert message is None and reason == 'leg_stale'


def test_quote_curve_graduation_cold_reverse_uses_verified_pool_without_entry_reads(monkeypatch, tmp_path):
    import copy
    import dataclasses
    from test_slot_lane_cpmm import _parsed_mint
    import test_slot_lane_graduation_quote_curves as G
    from sniper.services import memecoin_slot_graduation as graduation
    clock = Clock(50.)
    state = G._hi_state()
    source = P.parse_trade_event(G.HI['trade_event_log'])
    quote_usd = .05
    source_price, _ = P.quote_curve_prices(source, 8, quote_usd, 6)
    # The watcher observes a valid migrated pool ten percent above the source's curve price.
    state['quote_reserve'] = int(source_price * 1.10 / quote_usd * state['base_reserve'] / 1e6 * 1e8) - state['virtual_quote']
    builder = PS.SlotLanePumpSwapBuilder(user=G.USER, rpc=lambda *a: None, clock=clock.monotonic)
    builder._buyback = PS.buyback_fee_recipient_from_global_config(G.BL['global_config_b64'])
    builder._protocol_recipient = PS.protocol_fee_recipient_from_global_config(G.BL['global_config_b64'])
    def mint_rpc(method, params):
        assert method == 'getMultipleAccounts'
        values = []
        for mint in params[0]:
            value = _parsed_mint(decimals=8 if mint == G.QUOTE else 6)
            value['owner'] = P.TOKEN_PROGRAM if mint == G.QUOTE else state['base_token_program']
            values.append(value)
        return {'value': values}
    curve = P.SlotLaneCurveBuilder(user=G.USER, rpc=mint_rpc, clock=clock.monotonic)
    curve._buyback = builder._buyback
    # The actual transaction is the migrated AMM build checked below, not this discarded curve fit.
    monkeypatch.setattr(curve, 'ondemand_fits', lambda *a: True)
    calls = []
    class Http:
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            if method == 'POST':
                return 200, {'swapInstruction': {'programId': P.SYSTEM_PROGRAM, 'data': '',
                    'accounts': [{'pubkey': G.USER, 'isSigner': True, 'isWritable': True}]}}, None
            query = parse_qs(urlsplit(url).query)
            reverse = query['inputMint'][0] == G.QUOTE
            return 200, {'inputMint': G.QUOTE if reverse else P.USDC_MINT,
                'outputMint': P.USDC_MINT if reverse else G.QUOTE, 'inAmount': query['amount'][0],
                'outAmount': '10000000' if reverse else '20100000000',
                'otherAmountThreshold': '9900000' if reverse else '20000000000',
                'swapMode': 'ExactIn', 'slippageBps': P.DEFAULT_LEG_SLIPPAGE_BPS,
                'routePlan': [{'swapInfo': {'ammKey': G.POOL, 'label': 'fixture'}}]}, None
    warm = LL.WarmExoticLegs(clock=clock, http=Http())
    warm.set_clip_inventory((10_000_000,))
    original = P._funded_jup_call
    monkeypatch.setattr(P, '_funded_jup_call', lambda method, path, body=None, *, http=None:
                        original(method, path, body, http=http or warm._warm_http))
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True)
    monkeypatch.setattr('sniper.services.memecoin_source_initiation.source_initiation_refusal', lambda *a: None)
    requeued = []
    worker = LL.OnDemandLegWorker(builders={'pump_curve': curve, 'pump_amm': builder},
        admission_fn=lambda buy, quote: None, snapshot_fn=lambda: SNAP,
        clip_fn=lambda: 10., offer_fn=lambda *a: None, start=False,
        live_fn=lambda venue: True, requeue_fn=requeued.append, warm=warm,
        clock=SimpleNamespace(monotonic=clock.monotonic))
    c, lane, sends, _ = _live(monkeypatch, tmp_path, {}, builder=builder)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', frozenset({'pump_curve', 'pump_amm'}))
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_GRADUATION_QUOTE_CURVES', 'live')
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_CHASE_MAX_PCT', 30.)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_REAL_MAX_EXEC_PREMIUM_BY_WALLET', {})
    monkeypatch.setattr(L.cfg, 'entry_roundtrip_cap_pct', lambda *a: 10.)
    lane.clock = SimpleNamespace(monotonic=clock.monotonic, perf_counter=time.perf_counter)
    lane.snapshot_fn = lambda: L.PrefetchSnapshot(sol_usd=150., usdc_balance=400., sol_balance=50.,
        blockhash=SNAP.blockhash, last_valid_height=1,
        observed_mono={key: clock.t for key in ('sol_usd', 'balances', 'blockhash')})
    def observed_graduation(signature):
        # Each call stands for a new watcher bank observation; no production clock is renewed.
        payload = graduation.payload(copy.deepcopy(state), trigger='source_completed',
                                     offered_mono=clock.t, read_ms=1., now_mono=clock.t)
        return G._buy(G.HI['trade_event_log'], signature=signature, recv_monotonic=clock.t,
                      quote_mint=G.QUOTE, graduation=payload)
    try:
        first = observed_graduation('graduation-cold')
        assert worker.process(first)['status'] == 'requeued'
        assert [method for method, _ in calls] == ['GET', 'POST', 'GET']
        assert warm.keys() == [('pump_curve', G.QUOTE, 10_000_000)]
        lane.pending_legs.add(first, deadline=first.recv_monotonic + L.MAX_SIGNAL_AGE_S)
        assert requeued[-1].recv_monotonic == first.recv_monotonic
        assert requeued[-1].graduation is first.graduation
        before = list(calls)
        def forbidden(*a, **kwargs):
            pytest.fail('graduation entry fire performed provider I/O')
        with monkeypatch.context() as fire:
            fire.setattr(P, '_funded_jup_call', forbidden)
            fire.setattr(curve, 'rpc', forbidden)
            fire.setattr(builder, 'rpc', forbidden)
            fire.setattr(builder.pools, 'rpc', forbidden)
            filled = lane.handle(requeued[-1])
        assert filled['status'] == 'filled', filled
        assert len(sends) == 1 and calls == before
        clock.t += 1.5
        before_refresh = len(calls)
        warm.refresh_once()
        assert len(calls) - before_refresh == 3 <= 2 * warm.max_warm
        assert first.graduation['requeued_mono'] == 50., 'warming never renews source pool evidence'
        reverse = next(iter(warm._entries.values()))['leg']['reverse']
        assert reverse is not None, 'a validated cached graduation must have a legitimate reverse warm path'
        assert reverse['min_usdc_atomic'] == 9_900_000
        second = observed_graduation('graduation-warm')
        before = list(calls)
        monkeypatch.setattr(P, '_funded_jup_call', forbidden)
        curve.rpc = builder.rpc = builder.pools.rpc = forbidden
        assert worker.process(second)['warm_leg'] is True
        assert calls == before
        assert [r[0] for r in c.execute('SELECT terminal_status FROM memecoin_entry_attempts')] == ['filled']
    finally:
        c.close()


@pytest.mark.parametrize('broken', ['pool', 'source_mint', 'source_quote', 'virtual_quote', 'fee_shape'])
def test_graduation_reverse_requires_the_existing_canonical_pool_and_curve_identity(monkeypatch, broken):
    import copy
    import dataclasses
    import test_slot_lane_graduation_quote_curves as G
    from sniper.services import memecoin_slot_graduation as graduation
    clock = Clock(50.)
    grad = graduation.payload(G._hi_state(), trigger='source_completed', offered_mono=50.,
                              read_ms=1., now_mono=50.)
    grad = copy.deepcopy(grad)
    buy = G._buy(G.HI['trade_event_log'], quote_mint=G.QUOTE, graduation=grad)
    if broken == 'pool':
        grad['pool'] = G.USER
    elif broken == 'source_mint':
        buy = dataclasses.replace(buy, mint=G.USER)
    elif broken == 'source_quote':
        grad['pool_facts']['quote_mint'] = P.WSOL_MINT
    elif broken == 'virtual_quote':
        grad['state']['virtual_quote'] += 1
    else:
        grad['state']['fees'] = [1, 2]
    entry = {'key': ('pump_curve', G.QUOTE, 10_000_000), 'facts': {}, 'buy': buy}
    leg = {'output_mint': G.QUOTE, 'usdc_atomic': 10_000_000, 'min_out_lamports': 20_000_000_000}
    def forbidden(*args, **kwargs):
        pytest.fail('invalid graduation reached a reverse HTTP request')
    monkeypatch.setattr(P, '_funded_jup_call', forbidden)
    warm = LL.WarmExoticLegs(clock=clock)
    assert warm._reverse_leg(P, entry, leg, [], {}, 50.) is None
