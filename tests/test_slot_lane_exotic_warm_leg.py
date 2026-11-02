"""Warm-after-use exotic legs (Alpha GO 2026-10-07, Gamma's landing breakdown bucket 2).

A live exotic-quote buy waits for its USDC -> quote leg (Jupiter /quote + /swap-instructions on the
funded seam): leg_ms p50 179 / p90 326 / max 562 over 105 legs since 10-02, the whole decision
cost (6BamsvP4: leg 199 of 204 ms; QI decided at 248 ms, landed S+3). 14 of the 43 live legs had a
leg for the same quote mint in the 60 s before, all by the same wallet (ladder buys). With
MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS a live cold leg for (venue, quote mint, clip) is kept warm for
60 s on the prefetch thread, after the SOL legs, at most 2 at once, with its own 429 backoff; a buy
that finds a fresh one skips the leg. Any miss is today's cold leg.
"""
import time

import pytest

from sniper.execution import raydium_launchlab_buy as LL
from sniper.services import memecoin_slot_lane as L
from test_slot_lane_cpmm import PEPE_EV, _exotic_builder
from test_slot_lane_exotic_live import _buy, _live_worker, exotic_cpmm  # noqa: F401 (fixture)

QUOTE = PEPE_EV.input_mint


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def monotonic(self):
        return self.t


def _leg(fetched_at, usdc=10_000_000, mint=QUOTE):
    return {"ixs": [], "tables": [], "usdc_atomic": usdc, "min_out_lamports": 90, "out_lamports": 100,
            "fetched_at": fetched_at, "quote": {"outAmount": "100"}, "output_mint": mint}


def _warm(clock, fetches, *, statuses=None):
    """A WarmExoticLegs whose fetch answers a fresh leg, or the next queued Jupiter status."""
    statuses = list(statuses or [])

    def fetch(entry):
        fetches.append(entry["key"])
        if statuses:
            status = statuses.pop(0)
            if status != 200:
                return None, [status]
        return _leg(clock.monotonic(), usdc=entry["key"][2], mint=entry["key"][1]), [200, 200]
    # The window mechanics at the old 60 s; the live default is the 1800 s knob (test below).
    return LL.WarmExoticLegs(clock=clock, fetch_fn=fetch, window_s=60.0)


def test_a_noted_leg_is_refreshed_on_the_prefetch_pass_and_served_while_fresh():
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note("raydium_cpmm", QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000)["fetched_at"] == clock.t      # the cold leg serves first
    clock.t += 1.0
    warm.refresh_once()
    assert fetches == []                                    # younger than the 1.5 s refresh: no call
    clock.t += 0.6
    warm.refresh_once()
    assert fetches == [("raydium_cpmm", QUOTE, 10_000_000)]
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000)["fetched_at"] == clock.t
    clock.t += 3.1                                          # past MAX_LEG_AGE_S with no refresh
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000) is None


def test_a_different_clip_venue_or_quote_is_a_miss():
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note("raydium_cpmm", QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    assert warm.get("raydium_cpmm", QUOTE, 9_000_000) is None
    assert warm.get("raydium_launchlab", QUOTE, 10_000_000) is None
    assert warm.get("raydium_cpmm", "OtherQuote111111111111111111111111111111111", 10_000_000) is None


def test_the_window_ends_sixty_seconds_after_the_last_use_and_the_set_holds_two():
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    for n, mint in enumerate(("Q1", "Q2", "Q3")):
        warm.note("raydium_cpmm", mint, 10_000_000, leg=_leg(clock.t, mint=mint), builder=object())
        clock.t += 1.0
    assert sorted(k[1] for k in warm.keys()) == ["Q2", "Q3"]              # the oldest use went
    clock.t += 61.0
    warm.refresh_once()
    assert fetches == [] and warm.keys() == []                            # expired: nothing refreshed


def test_a_rate_limited_refresh_backs_off_the_warm_legs_only():
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches, statuses=[429])
    warm.note("raydium_cpmm", QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    clock.t += 2.0
    warm.refresh_once()
    assert len(fetches) == 1 and warm.stats["rate_limited"] == 1
    clock.t += 2.0
    warm.refresh_once()
    assert len(fetches) == 1                                              # inside its own backoff
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000) is None            # stale meanwhile: a miss


def _worker(monkeypatch, *, on, warm=None):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS", on, raising=False)
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10)
    w.warm = warm
    return w, b, funded, requeued


def test_a_live_cold_leg_is_noted_and_the_next_buy_skips_the_leg(monkeypatch):
    clock, fetches = Clock(time.monotonic()), []
    warm = LL.WarmExoticLegs(clock=time, fetch_fn=lambda e: pytest.fail("no refresh in this test"))
    w, b, funded, requeued = _worker(monkeypatch, on=True, warm=warm)
    assert w.process(_buy())["status"] == "requeued" and len(funded) == 2
    assert warm.keys() == [("raydium_cpmm", QUOTE, 10_000_000)]
    r = w.process(_buy())
    assert r["status"] == "requeued" and len(funded) == 2                # no Jupiter call for the 2nd
    assert r["warm_leg"] is True and requeued[-1].ondemand["warm_leg"] is True
    assert requeued[-1].ondemand["leg"] is requeued[0].ondemand["leg"]
    assert set(r["leg_timings_ms"]) == {"quote_safety", "signal_age_at_worker", "ondemand_quote", "worker"}


def test_with_the_knob_off_every_live_leg_is_cold_and_nothing_is_noted(monkeypatch):
    clock, fetches = Clock(time.monotonic()), []
    warm = LL.WarmExoticLegs(clock=time, fetch_fn=lambda e: pytest.fail("no refresh in this test"))
    w, b, funded, requeued = _worker(monkeypatch, on=False, warm=warm)
    w.process(_buy())
    w.process(_buy())
    assert len(funded) == 4 and warm.keys() == []


def test_the_prefetch_pass_refreshes_the_warm_legs_after_every_sol_leg(monkeypatch):
    order = []

    class Builder:
        def refresh(self, clip, snapshot, **kw):
            order.append("sol:" + str(clip))

    class Warm:
        def wake(self):
            order.append("warm")
    L.refresh_builder_legs({"pump_curve": Builder(), "pump_amm": Builder()}, 10.0, 1.0, None, warm=Warm())
    assert order == ["sol:10.0", "sol:10.0", "warm"]


def test_a_warm_leg_serves_only_while_a_second_inside_the_builders_age_bound():
    """Gamma LOW-1 (a5d27af7d): the builders refuse a leg older than MAX_LEG_AGE_S ("leg_stale",
    checked on the lane after the requeue), so a warm hit near that bound became a refusal where a
    cold leg (~180 ms) would have built. Warm serves only while age <= MAX_LEG_AGE_S - 1.0."""
    from sniper.execution.pump_curve_buy import MAX_LEG_AGE_S
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note("raydium_cpmm", QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    clock.t += MAX_LEG_AGE_S - 1.0
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000) is not None
    clock.t += 0.05
    assert warm.get("raydium_cpmm", QUOTE, 10_000_000) is None            # a miss: the cold leg runs


def test_a_leg_whose_buy_admission_refuses_is_not_kept_warm(monkeypatch):
    """Gamma LOW-3 (a5d27af7d): about 20 of 37 live exotic outcomes are refused by admission after
    the leg (mcap ceiling, source floor). Noting those legs opened a 60 s window of ~40 refreshes x
    2 funded Jupiter calls for buys that cannot send. Only a buy admission passes is noted."""
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS", True, raising=False)
    warm = LL.WarmExoticLegs(clock=time, fetch_fn=lambda e: pytest.fail("no refresh in this test"))
    b = _exotic_builder(PEPE_EV)
    w, funded, requeued, refused = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10,
                                                admission=lambda buy, q: "live_refused_mcap")
    w.warm = warm
    r = w.process(_buy())
    assert r["status"] != "requeued" and len(funded) == 2 and not requeued
    assert warm.keys() == [] and warm.stats["noted"] == 0


def test_the_exotic_worker_line_and_the_lane_result_name_a_warm_leg(exotic_cpmm, monkeypatch, tmp_path):
    """Gamma INFO (b): the forward grade reads warm hits from the log line and result["exotic"]."""
    import dataclasses
    from test_slot_lane_exotic_live import _Wake, _exotic_live_lane
    assert " warm=1 " in L.exotic_worker_line(_buy(), {"status": "requeued", "reason": "requeued",
                                                       "warm_leg": True})
    assert " warm=0 " in L.exotic_worker_line(_buy(), {"status": "requeued", "reason": "requeued"})
    legs, wake = L.PendingLegs(), _Wake()

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        assert (entry_min_slot, entry_max_slot) == (None, None)    # no slot window since #486
        return {"status": "filled", "position_id": 1, "sig": "TXE", "quote_residue_atomic": 1}
    lane, buy, _seen, closing = _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake)
    buy = dataclasses.replace(buy, ondemand=dict(buy.ondemand, warm_leg=True))
    legs.add(buy, deadline=buy.recv_monotonic + L.MAX_SIGNAL_AGE_S)
    with closing:
        assert lane.handle(buy)["exotic"]["warm_leg"] is True


def test_worker_timings_survive_handback_and_reach_trace(exotic_cpmm, monkeypatch, tmp_path):
    import json
    from sniper.execution import pump_curve_buy as P
    from test_slot_lane_exotic_live import _Wake, _exotic_live_lane
    legs, wake = L.PendingLegs(), _Wake()
    def execute(*args, **kwargs):
        return {'status': 'filled', 'position_id': 1, 'sig': 'TXE', 'quote_residue_atomic': 1}
    lane, buy, _, closing = _exotic_live_lane(tmp_path, monkeypatch, execute, legs, wake)
    w, b, funded, requeued = _worker(monkeypatch, on=False)
    clock = Clock(time.monotonic())
    w.clock = clock
    real_live_quote = w._live_quote
    def live_quote(*args, **kwargs):
        clock.t += .006
        return real_live_quote(*args, **kwargs)
    monkeypatch.setattr(w, '_live_quote', live_quote)
    real_jup = P._funded_jup_call
    def jup(method, path, body=None, *, http=None):
        clock.t += .01 if method == 'GET' else .02
        status, payload = real_jup(method, path, body, http=http)
        if method == 'POST':
            payload['addressLookupTableAddresses'] = ['test-table']
        return status, payload
    def rpc(*args):
        clock.t += .03
        return {'value': {'data': ['encoded']}}
    def alt(*args):
        clock.t += .04
        return object()
    real_parse = P.instruction_from_json
    def parse(value):
        clock.t += .005
        return real_parse(value)
    monkeypatch.setattr(P, '_funded_jup_call', jup)
    monkeypatch.setattr(P, '_rpc_result', rpc)
    monkeypatch.setattr(P, 'lookup_table_from_account', alt)
    monkeypatch.setattr(P, 'instruction_from_json', parse)
    result = w.process(buy)
    assert result['status'] == 'requeued'
    prepared = requeued[0]
    assert prepared.recv_monotonic == buy.recv_monotonic
    timings = prepared.ondemand['leg_timings_ms']
    assert timings == result['leg_timings_ms']
    for name, ms in [('quote', 10), ('swap_instructions', 20), ('alt_rpc', 30), ('alt_parse', 40)]:
        assert timings[name] == pytest.approx(ms)
    assert timings['instruction_parse'] > 0
    assert timings['quote_safety'] == pytest.approx(6)
    # The synthetic ALT is unnecessary to the lane build, which uses its own fixture message.
    monkeypatch.setattr(lane, 'build_fn', lambda *args: ('MESSAGE', {}))
    legs.add(prepared, deadline=prepared.recv_monotonic + L.MAX_SIGNAL_AGE_S)
    with closing:
        lane.handle(prepared)
        payload = json.loads(lane.conn.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
    event = next(e for e in payload['substage_events'] if e['name'] == 'slot_route_preparation')
    assert event['leg_timings_ms'] == timings and event['clock'] == 'monotonic'


def test_the_worker_times_its_part_before_the_leg(monkeypatch):
    """Lambda report 2026-10-16: 25% of cold-leg receipt->decision time sat outside every timed leg call
    (198 ms of XsDoV's 313). The worker now records the signal's age when it starts, its venue quote
    read and its own total until the hand-back, so r2e = signal_age_at_worker + worker (+ hand-back)."""
    w, b, _, requeued = _worker(monkeypatch, on=False)
    clock = Clock(time.monotonic())
    w.clock = clock
    real_quote = b.ondemand_quote
    def ondemand_quote(buy):
        clock.t += .007
        return real_quote(buy)
    monkeypatch.setattr(b, "ondemand_quote", ondemand_quote)
    buy = _buy(recv=clock.t - .040)
    r = w.process(buy)
    assert r["status"] == "requeued"
    timings = requeued[0].ondemand["leg_timings_ms"]
    assert timings["signal_age_at_worker"] == pytest.approx(40)
    assert timings["ondemand_quote"] == pytest.approx(7)
    assert timings["worker"] >= timings["ondemand_quote"] + timings["quote"]
    assert r["leg_timings_ms"]["worker"] == timings["worker"]


def test_worker_refusal_retains_completed_timings(monkeypatch):
    from sniper.execution import pump_curve_buy as P
    w, _, _, requeued = _worker(monkeypatch, on=False)
    clock = Clock(time.monotonic())
    w.clock = clock
    def refused(method, path, body=None, *, http=None):
        clock.t += .1
        return 429, {}
    monkeypatch.setattr(P, '_funded_jup_call', refused)
    result = w.process(_buy())
    assert result['reason'] == 'slot_lane_cpmm_leg_rate_limited' and not requeued
    assert result['leg_timings_ms']['quote'] == pytest.approx(100)
    assert 'swap_instructions' not in result['leg_timings_ms']


@pytest.mark.parametrize('knob, held', [(2, 2), (4, 3)])   # the cap follows the inventory above knob 2
def test_seed_saturation_preserves_both_live_worker_slots(monkeypatch, knob, held):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', knob, raising=False)
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    live, b, funded, requeued = _worker(monkeypatch, on=True, warm=warm)
    facts = b.pools.get(PEPE_EV.pool)
    warm.set_clip_inventory((10_000_000, 11_000_000, 12_000_000))
    for atoms in (10_000_000, 11_000_000, 12_000_000):
        warm.seed('raydium_cpmm', QUOTE, atoms, builder=b, facts=facts)
    assert len(warm.keys()) == held and fetches == []
    assert live.offer(_buy()) is True
    assert live.offer(_buy()) is True
    assert live.offer(_buy()) is False
    assert live._in_flight == 2 and live.max_in_flight == 2


def test_late_refresh_cannot_overwrite_newer_live_cold_leg():
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    old, newer = _leg(clock.t), _leg(clock.t + 1)
    warm = None
    def fetch(entry):
        warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=newer, builder=b)
        return _leg(clock.t + 2), [200, 200]
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=fetch)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=old, builder=b)
    clock.t += 2
    warm.refresh_once()
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is newer


def test_lookup_and_reseed_do_not_renew_unused_candidate(monkeypatch):
    """At knob 2 (the old warmer); above it a re-seed renews: test_a_reseed_of_the_same_binding_renews_the_window."""
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', 2, raising=False)
    clock, fetches = Clock(), []
    b = _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, fetches)
    warm.set_clip_inventory((10_000_000,))
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=facts)
    warm.refresh_once()
    original = warm._entries[('raydium_cpmm', QUOTE, 10_000_000)]['candidate_at']
    clock.t += 59
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=facts)
    warm.refresh_once()
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is not None
    assert warm._entries[('raydium_cpmm', QUOTE, 10_000_000)]['candidate_at'] == original
    clock.t += 2
    warm.refresh_once()
    assert warm.keys() == []


def test_inventory_replacement_during_fetch_cannot_publish():
    import threading
    entered, release = threading.Event(), threading.Event()
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    def fetch(entry):
        entered.set()
        assert release.wait(2)
        return _leg(clock.t), [200, 200]
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=fetch)
    warm.set_clip_inventory((10_000_000,))
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=b.pools.get(PEPE_EV.pool))
    thread = threading.Thread(target=warm.refresh_once)
    thread.start()
    assert entered.wait(2)
    try:
        warm.set_clip_inventory((11_000_000,))
        assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is None
    finally:
        release.set()
        thread.join(2)
    assert warm.keys() == []


def test_warm_actual_http_budget_counts_starts_and_exceptions():
    class Http:
        def __init__(self): self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url))
            if url == 'timeout': raise TimeoutError()
            return 200, {}, None
    clock, http = Clock(), Http()
    warm = LL.WarmExoticLegs(clock=clock, http=http)
    with pytest.raises(TimeoutError): warm._warm_http('GET', 'timeout')
    for _ in range(3): warm._warm_http('GET', 'test')
    with pytest.raises(LL.WarmHttpBudget): warm._warm_http('GET', 'test')
    clock.t += 1.49
    with pytest.raises(LL.WarmHttpBudget): warm._warm_http('GET', 'test')
    clock.t += .01
    assert warm._warm_http('GET', 'test')[0] == 200
    assert len(http.calls) == 5


@pytest.mark.parametrize('field,value', [('user', 'wrong'), ('ONDEMAND_QUOTE_PARAMS', '&wrong=true')])
def test_warm_lookup_rejects_builder_binding_change(field, value):
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    warm = _warm(clock, [])
    facts = b.pools.get(PEPE_EV.pool)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=b)
    setattr(b, field, value)
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=facts) is None


def test_blocked_warmer_does_not_hold_cache_or_essential_refresh(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    def fetch(entry):
        entered.set()
        assert release.wait(3)
        return _leg(clock.t), [200, 200]
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=fetch)
    warm.set_clip_inventory((10_000_000,))
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=b.pools.get(PEPE_EV.pool))
    thread = threading.Thread(target=warm.refresh_once)
    thread.start()
    assert entered.wait(2)
    try:
        live, _, _, _ = _worker(monkeypatch, on=True, warm=warm)
        assert live.offer(_buy()) and live.offer(_buy())
        assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is None
        warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=b)
        from test_memecoin_slot_lane_prefetch_telemetry import Clock as PrefetchClock
        pc = PrefetchClock()
        prefetch = L.LanePrefetcher(usdc_fn=lambda: 100., sol_fn=lambda: 1.,
            blockhash_fn=lambda: ('fresh', 9), sol_usd_fn=lambda: 150., clock=pc)
        prefetch.loop_once(groups=('balances', 'blockhash'), include_extra=False)
        assert prefetch.snapshot().usdc_balance == 100.
    finally:
        release.set()
        thread.join(3)


def test_refused_warm_hit_does_not_extend_speculative_lifetime(monkeypatch):
    clock, b = Clock(time.monotonic()), _exotic_builder(PEPE_EV)
    warm = _warm(clock, [])
    warm.set_clip_inventory((10_000_000,))
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=b.pools.get(PEPE_EV.pool))
    warm.refresh_once()
    clock.t += 59
    warm.refresh_once()
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True)
    live, _, _, _ = _live_worker(b, monkeypatch, out=PEPE_EV.input_amount // 10, admission=lambda buy, quote: 'refused')
    live.clock, live.warm = clock, warm
    assert live.process(_buy())['warm_leg'] is True
    clock.t += 2
    warm.refresh_once()
    assert warm.keys() == []


def _real_warm(monkeypatch, *, account=None, quote_over=None, signer=None):
    from sniper.execution import pump_curve_buy as P
    from test_slot_lane_pump_nonsol import SPCX, _builder
    from test_slot_lane_cpmm import _parsed_mint
    clock, b, calls = Clock(50), _builder(SPCX), []
    b.clock = b.pools.clock = clock.monotonic
    facts = b.pools.get(SPCX['pool'])
    value = _parsed_mint(decimals=6)
    value['owner'] = facts['quote_token_program']
    value['data']['parsed']['type'] = 'mint'
    if account is not None: value = account
    def rpc(method, params):
        calls.append(('rpc', params[1]['encoding']))
        return {'value': value}
    b.read_rpc = rpc
    quote = dict(inputMint=P.USDC_MINT, outputMint=SPCX['quote_mint'], inAmount='10000000',
        outAmount='100', otherAmountThreshold='90', swapMode='ExactIn',
        slippageBps=P.DEFAULT_LEG_SLIPPAGE_BPS,
        routePlan=[{'swapInfo': {'label': 'fixture', 'ammKey': SPCX['pool']}}])
    quote.update(quote_over or {})
    def jup(method, path, body=None, *, http=None):
        calls.append((method, path))
        clock.t += .1
        if method == 'GET': return 200, quote
        return 200, {'swapInstruction': {'programId': P.SYSTEM_PROGRAM, 'data': '',
            'accounts': [{'pubkey': signer or b.user, 'isSigner': True, 'isWritable': True}]}}
    monkeypatch.setattr(P, '_funded_jup_call', jup)
    warm = LL.WarmExoticLegs(clock=clock)
    warm.set_clip_inventory((10_000_000,))
    warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b, facts=facts)
    return warm, b, clock, calls, facts


def test_seed_prepares_quote_safety_before_get_and_preserves_get_start(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, clock, calls, facts = _real_warm(monkeypatch)
    warm.refresh_once()
    assert [c[0] for c in calls] == ['rpc', 'GET', 'POST']
    current = b.pools.get(SPCX['pool'])
    assert current is not facts and current['quote_read_at'] == 50
    leg = warm.get('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b, facts=current)
    assert leg is not None and leg['fetched_at'] == 50
    assert b._alt_cache == {}


def test_speculative_instruction_cannot_request_foreign_signer(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    from sniper.execution import pump_curve_buy as P
    warm, b, clock, calls, facts = _real_warm(monkeypatch, signer=P.USDC_MINT)
    warm.refresh_once()
    assert warm.get('pump_amm', SPCX['quote_mint'], 10_000_000) is None


@pytest.mark.parametrize('stamp', [None, 'bad', float('nan'), float('inf'), True, -1])
def test_invalid_leg_stamp_is_a_cold_miss(stamp):
    clock = Clock()
    warm = _warm(clock, [])
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(stamp), builder=object())
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is None


def test_real_alt_is_decoded_once_on_private_cache_and_foreign_owner_is_refused(monkeypatch):
    from sniper.execution import pump_curve_buy as P
    from test_slot_lane_pump_alt import _table_account, USER
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, clock, calls, facts = _real_warm(monkeypatch)
    original_rpc, original_jup = b.read_rpc, P._funded_jup_call
    table = P.USDC_MINT
    account = _table_account([USER])
    reads, decodes = [], []
    def rpc(method, params):
        if params[1]['encoding'] == 'jsonParsed': return original_rpc(method, params)
        reads.append(params[0])
        return {'value': account}
    def jup(method, path, body=None, *, http=None):
        status, value = original_jup(method, path, body, http=http)
        if method == 'POST': value['addressLookupTableAddresses'] = [table]
        return status, value
    original_decode = P.lookup_table_from_account
    def decode(*args):
        decodes.append(args[0])
        return original_decode(*args)
    b.read_rpc = rpc
    monkeypatch.setattr(P, '_funded_jup_call', jup)
    monkeypatch.setattr(P, 'lookup_table_from_account', decode)
    warm.refresh_once()
    assert reads == decodes == [table]
    assert b._alt_cache == {}
    clock.t += 1.5
    warm.refresh_once()
    assert reads == decodes == [table]
    leg = warm.get('pump_amm', SPCX['quote_mint'], 10_000_000)
    assert leg is not None and len(leg['tables']) == 1
    # A changed payer/binding gets no table from the prior owner.
    b.user = P.WSOL_MINT
    account = _table_account([USER], owner='foreign')
    warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b,
              facts=b.pools.get(SPCX['pool']))
    clock.t += 1.5
    warm.refresh_once()
    assert reads == [table, table] and decodes == [table]
    assert warm.get('pump_amm', SPCX['quote_mint'], 10_000_000) is None


def test_republished_same_identity_does_not_reset_candidate_or_used_priority(monkeypatch):
    """At knob 2 (the old warmer): cap 2 and the first seed's clock."""
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', 2, raising=False)
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    warm = _warm(clock, [])
    facts = b.pools.get(PEPE_EV.pool)
    warm.set_clip_inventory((10_000_000, 11_000_000, 12_000_000))
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=facts)
    clock.t += 59
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=dict(facts))
    assert warm._entries[('raydium_cpmm', QUOTE, 10_000_000)]['candidate_at'] == clock.t - 59
    warm.note('raydium_cpmm', QUOTE, 11_000_000, leg=_leg(clock.t, usdc=11_000_000), builder=b)
    warm.seed('raydium_cpmm', QUOTE, 12_000_000, builder=b, facts=facts)
    assert ('raydium_cpmm', QUOTE, 11_000_000) in warm.keys()
    clock.t += 2
    warm.refresh_once()
    assert ('raydium_cpmm', QUOTE, 10_000_000) not in warm.keys()


# 'facts' (another coin's pool facts with the same quote token) now SERVES: test below (Alpha (i)).
@pytest.mark.parametrize('change', ['quote_program', 'decimals', 'quote', 'future'])
def test_prepared_lookup_rejects_current_identity_or_freshness_mismatch(change):
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, [])
    leg = _leg(clock.t)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=leg, builder=b, facts=facts)
    current = facts
    if change == 'quote_program': facts['quote_token_program'] = 'changed'
    if change == 'decimals': facts['quote_decimals'] = 9
    if change == 'quote': facts['quote_mint'] = 'changed'
    if change == 'future': leg['fetched_at'] = clock.t + .01
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=current) is None


def test_no_inventory_means_no_speculative_io_and_wake_never_fetches():
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    b = _exotic_builder(PEPE_EV)
    for n in range(4):
        warm.seed('raydium_cpmm', QUOTE, 10_000_000 + n, builder=b, facts=b.pools.get(PEPE_EV.pool))
        warm.wake()
    warm.refresh_once()
    assert len(warm.keys()) == 2 and fetches == []


@pytest.mark.parametrize('scale', [None, 0, float('nan'), float('inf'), 1.1])
def test_unknown_or_unsupported_scale_cannot_seed_inventory(scale):
    assert L.exotic_preparation_clips(10., scale, ('a',)) == ()


def test_trial_inventory_reads_only_on_background_roster_pass_and_rolls_back_before_publish(monkeypatch):
    from types import SimpleNamespace
    targets, events = {}, []
    class Thread:
        def __init__(self, *, target, name, daemon): targets[name] = target
        def start(self): pass
    class StopLoop(BaseException): pass
    class TestClock(Clock):
        def sleep(self, seconds):
            self.t += seconds
            if self.t >= 1003: raise StopLoop()
    class Reader:
        def rollback(self): events.append('rollback')
        def close(self): pass
    class Warm:
        stats = {}
        def start(self): events.append('start')
        def set_clip_inventory(self, inventory):
            assert events[-1] == 'rollback'
            events.append('inventory')
        def wake(self): pass
    clock = TestClock()
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(LL, 'WarmExoticLegs', Warm)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True)
    monkeypatch.setattr(L, 'load_cu_demotions', lambda *_a, **_k: ())
    monkeypatch.setattr(L, 'owners_from_roster', lambda *_a: ({}, frozenset({'a'})))
    monkeypatch.setattr(L, 'leg_plan', lambda *_a: (10., 1.))
    monkeypatch.setattr(L, 'refresh_builder_legs', lambda *_a, **_k: None)
    monkeypatch.setattr(L.m, 'open_trial', lambda *_a: events.append('trial') or None)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_ONCHAIN_LANE_ENABLED', False)
    rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
              slot_lock_key='fixture')
    L.start_slot_lane(rc, builder=SimpleNamespace(), signal_start_fn=lambda *_a, **_k: None,
        sol_usd_fn=lambda: None, log=lambda _: None, roster_path='unused',
        conn_factory=object, readonly_conn_factory=Reader, clock=clock)
    with pytest.raises(StopLoop): targets['slot-lane-prefetch']()
    assert events.count('trial') == 1


def test_changed_clip_can_seed_current_same_pool_safety_snapshot(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, clock, calls, original = _real_warm(monkeypatch)
    warm.refresh_once()
    current = b.pools.get(SPCX['pool'])
    assert current is not original
    warm.set_clip_inventory((11_000_000,))
    warm.seed('pump_amm', SPCX['quote_mint'], 11_000_000, builder=b, facts=original)
    assert warm._entries[('pump_amm', SPCX['quote_mint'], 11_000_000)]['facts'] is current


def test_unverified_pump_pool_cannot_seed():
    from test_slot_lane_pump_nonsol import SPCX, _builder
    clock, b = Clock(), _builder(SPCX)
    facts = dict(b.pools.get(SPCX['pool']), pump_migrated=False)
    warm = _warm(clock, [])
    warm.set_clip_inventory((10_000_000,))
    warm.seed('pump_amm', SPCX['quote_mint'], 10_000_000, builder=b, facts=facts)
    assert warm.keys() == []


def test_two_exact_clips_share_current_quote_safety_snapshot(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from sniper.execution import pump_curve_buy as P
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, clock, calls, facts = _real_warm(monkeypatch)
    original_jup = P._funded_jup_call
    def jup(method, path, body=None, *, http=None):
        status, value = original_jup(method, path, body, http=http)
        if method == 'GET': value['inAmount'] = parse_qs(urlsplit(path).query)['amount'][0]
        return status, value
    monkeypatch.setattr(P, '_funded_jup_call', jup)
    warm.set_clip_inventory((10_000_000, 11_000_000))
    warm.seed('pump_amm', SPCX['quote_mint'], 11_000_000, builder=b, facts=facts)
    warm.refresh_once()
    assert [c[0] for c in calls] == ['rpc', 'GET', 'POST', 'GET', 'POST']
    current = b.pools.get(SPCX['pool'])
    for atoms in (10_000_000, 11_000_000):
        assert warm.get('pump_amm', SPCX['quote_mint'], atoms, builder=b, facts=current) is not None


def test_real_dbc_carried_facts_reuse_and_background_safety_refresh():
    from test_slot_lane_dbc import EXOTIC, FakeRpc, _buy, _builder
    buy, builder, clock = _buy(EXOTIC), _builder(), Clock()
    builder.rpc = FakeRpc(EXOTIC)
    facts = LL._warm_facts(builder, buy)
    warm = LL.WarmExoticLegs(clock=clock)
    warm.note(buy.venue, buy.venue_event.quote, 10_000_000,
              leg=_leg(clock.t, mint=buy.venue_event.quote), builder=builder, facts=facts, buy=buy)
    assert warm.get(buy.venue, buy.venue_event.quote, 10_000_000,
                    builder=builder, facts=LL._warm_facts(builder, buy)) is not None
    entry = dict(next(iter(warm._entries.values())))
    assert warm._prepare_quote_facts(entry)
    assert builder.rpc.calls[-1][0] == 'getAccountInfo'


def test_real_cpmm_old_quote_snapshot_rechecks_without_replacing_pool():
    builder, clock = _exotic_builder(PEPE_EV), Clock()
    facts = builder.pools.get(PEPE_EV.pool)
    facts['quote_read_at'] = clock.t - LL.QUOTE_RECHECK_S - 1
    value = facts['mint_values'][QUOTE]
    state = facts['state']
    value['owner'] = state.program_0 if state.mint_0 == QUOTE else state.program_1
    calls = []
    builder.pools.rpc = lambda method, params: (calls.append(method) or {'value': value})
    warm = LL.WarmExoticLegs(clock=clock)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=builder, facts=facts)
    entry = dict(next(iter(warm._entries.values())))
    assert warm._prepare_quote_facts(entry)
    assert builder.pools.get(PEPE_EV.pool) is facts
    assert facts['quote_read_at'] < clock.t - LL.QUOTE_RECHECK_S
    clock.t += 2
    assert warm._prepare_quote_facts(dict(next(iter(warm._entries.values()))))
    assert calls == ['getAccountInfo']


@pytest.mark.parametrize('change', ['owner', 'decimals', 'fee', 'missing'])
def test_real_dbc_failed_safety_refresh_does_not_renew(change):
    import copy
    from test_slot_lane_dbc import EXOTIC, FakeRpc, _buy, _builder
    buy, builder, clock = _buy(EXOTIC), _builder(), Clock()
    value = copy.deepcopy(EXOTIC['quote_value'])
    if change == 'owner':
        value['owner'] = '11111111111111111111111111111111'
    elif change == 'decimals':
        value['data']['parsed']['info']['decimals'] += 1
    elif change == 'fee':
        value['data']['parsed']['info']['extensions'].append({'extension': 'transferFeeConfig', 'state': {'olderTransferFee': {'transferFeeBasisPoints': 1, 'maximumFee': 1}, 'newerTransferFee': {'transferFeeBasisPoints': 1, 'maximumFee': 1}}})
    else:
        value = None
    builder.rpc = FakeRpc(EXOTIC)
    builder.rpc.quote_value = value
    warm = LL.WarmExoticLegs(clock=clock)
    warm.note(buy.venue, buy.venue_event.quote, 10_000_000, leg=None,
              builder=builder, facts=LL._warm_facts(builder, buy), buy=buy)
    assert not warm._prepare_quote_facts(dict(next(iter(warm._entries.values()))))
    assert 'quote_safety' not in next(iter(warm._entries.values()))


@pytest.fixture
def preparation_lane(monkeypatch):
    from types import SimpleNamespace
    warm, builder, clock, calls, facts = _real_warm(monkeypatch)
    warm = LL.WarmExoticLegs(clock=clock)  # only installed callback/reseed may add a key
    targets = {}
    class Thread:
        def __init__(self, *, target, name, daemon): targets[name] = target
        def start(self): pass
    class StopLoop(BaseException): pass
    class Reader:
        def rollback(self): pass
        def close(self): pass
    def sleep(_seconds): raise StopLoop()
    clock.sleep = sleep
    monkeypatch.setattr('threading.Thread', Thread)
    monkeypatch.setattr(LL, 'WarmExoticLegs', lambda: warm)
    monkeypatch.setattr(warm, 'start', lambda: None)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', True)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_ONCHAIN_LANE_ENABLED', False)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_ROUTED', 'off')
    monkeypatch.setattr(L, 'load_cu_demotions', lambda *_a, **_k: ())
    monkeypatch.setattr(L, 'owners_from_roster', lambda *_a: ({}, frozenset({'a'})))
    monkeypatch.setattr(L, 'leg_plan', lambda *_a: (10., 1.))
    monkeypatch.setattr(L, 'refresh_builder_legs', lambda *_a, **_k: None)
    monkeypatch.setattr(L.m, 'open_trial', lambda *_a: None)
    def start():
        rc = dict(usdc_fn=lambda: None, sol_fn=lambda: None, latest_blockhash_fn=lambda: None,
                  slot_lock_key='fixture')
        return L.start_slot_lane(rc, builders={'pump_amm': builder},
            signal_start_fn=lambda *_a, **_k: None, sol_usd_fn=lambda: None,
            log=lambda _: None, roster_path='unused', conn_factory=object,
            readonly_conn_factory=Reader, clock=clock)
    def inventory_pass():
        with pytest.raises(StopLoop): targets['slot-lane-prefetch']()
    return SimpleNamespace(warm=warm, builder=builder, clock=clock, calls=calls,
                           facts=facts, start=start, inventory_pass=inventory_pass)


@pytest.mark.parametrize('mode,venues,eligible', [
    ('live', (), False), ('shadow', ('pump_amm',), False), ('live', ('pump_amm',), True)])
@pytest.mark.parametrize('before_inventory', [True, False])
def test_installed_pool_callback_prepares_only_live_routes(
        preparation_lane, monkeypatch, mode, venues, eligible, before_inventory):
    case = preparation_lane
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_SLOT_LANE_MODE', mode)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', venues)
    case.start()
    if not before_inventory: case.inventory_pass()
    case.builder.pools.on_pool_known(case.facts['pool'], case.facts)
    if before_inventory: case.inventory_pass()
    assert case.warm.stats['seeded'] == int(eligible)
    case.warm.refresh_once()
    assert [call[0] for call in case.calls] == (['rpc', 'GET', 'POST'] if eligible else [])
    assert len(case.warm.keys()) == int(eligible)


def test_inventory_reseed_rechecks_route_live_after_pool_retention(preparation_lane, monkeypatch):
    case = preparation_lane
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_SLOT_LANE_MODE', 'live')
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', ('pump_amm',))
    case.start()
    case.builder.pools.on_pool_known(case.facts['pool'], case.facts)
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', ())
    case.inventory_pass()
    assert case.warm.stats['seeded'] == 0
    case.warm.refresh_once()
    assert case.warm.keys() == [] and case.calls == []


def test_clip_change_keeps_distinct_quotes_when_two_pools_share_one_quote(preparation_lane, monkeypatch):
    case = preparation_lane
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SOLANA_SLOT_LANE_MODE', 'live')
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', ('pump_amm',))
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_SCALED_SOL', False)
    case.start()
    case.inventory_pass()
    case.builder.pools.on_pool_known(case.facts['pool'], case.facts)
    for pool in ('OtherPool1', 'OtherPool2'):
        facts = dict(case.facts, pool=pool, quote_mint=QUOTE)
        case.builder.pools._pools[pool] = facts
        case.builder.pools.on_pool_known(pool, facts)
    assert set(case.warm.keys()) == {
        ('pump_amm', case.facts['quote_mint'], 10_000_000), ('pump_amm', QUOTE, 10_000_000)}
    monkeypatch.setattr(L, 'leg_plan', lambda *_a: (11., 1.))
    case.inventory_pass()
    assert set(case.warm.keys()) == {
        ('pump_amm', case.facts['quote_mint'], 11_000_000), ('pump_amm', QUOTE, 11_000_000)}
    assert case.warm.stats['cap'] == 2 and case.calls == []


def test_warmer_status_available_without_ondemand_builder(preparation_lane, monkeypatch):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES', ())
    status = preparation_lane.start()
    assert 'ondemand_leg' not in status
    assert status['exotic_warm']['http_calls'] == 0



def test_warmoff_leaves_no_daemon_status_or_pool_callback(preparation_lane, monkeypatch):
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_LEGS', False)
    status = preparation_lane.start()
    assert 'exotic_warm' not in status
    assert preparation_lane.builder.pools.on_pool_known is None


# --- Alpha (i), 2026-10-15: the warm leg is the quote token's, not one coin's -----------------------

def _other_coin(facts):
    other = dict(facts, pool='OtherPool1111111111111111111111111111111111', base_mint='OtherCoin111111111111111111111111111111111')
    other.pop('_identity', None)
    return other


def test_a_warm_leg_serves_a_different_coin_with_the_same_quote_token():
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, [])
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=b, facts=facts)
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=_other_coin(facts)) is not None


@pytest.mark.parametrize('change', ['quote_token_program', 'quote_decimals', 'quote_mint', 'clip'])
def test_a_different_quote_program_decimals_mint_or_clip_is_never_served(change):
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, [])
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=b, facts=facts)
    other = _other_coin(facts)
    clip = 10_000_000
    if change == 'clip':
        clip = 11_000_000
    else:
        other[change] = 'changed' if change != 'quote_decimals' else 9
    assert warm.get('raydium_cpmm', QUOTE, clip, builder=b, facts=other) is None


def test_the_served_leg_is_still_inside_the_builders_age_bound():
    from sniper.execution.pump_curve_buy import MAX_LEG_AGE_S
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, [])
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=b, facts=facts)
    clock.t += MAX_LEG_AGE_S - LL.EXOTIC_WARM_SERVE_MARGIN_S - 0.01
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=_other_coin(facts)) is not None
    clock.t += 0.02
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=_other_coin(facts)) is None


def test_the_window_is_the_knob_and_defaults_to_thirty_minutes(monkeypatch):
    import importlib
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S == 1800.0 or 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S' in __import__('os').environ
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S', 1800.0)
    clock, fetches = Clock(), []
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=lambda e: (_leg(clock.t), [200, 200]))
    assert warm.window_s == 1800.0
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    clock.t += 16.1 * 60                                     # the longest TTWofwAg gap seen
    warm.refresh_once()
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000) is not None
    clock.t += 1800
    warm.refresh_once()
    assert warm.keys() == []


def test_a_coin_whose_pool_left_the_cache_keeps_its_quote_leg_on_a_quote_only_check(monkeypatch):
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, clock, calls, facts = _real_warm(monkeypatch)
    warm.refresh_once()
    key = ('pump_amm', SPCX['quote_mint'], 10_000_000)
    assert warm._entries[key]['leg'] is not None
    b.pools._pools.pop(SPCX['pool'], None)                   # evicted
    clock.t += 2.0
    calls.clear()
    warm.refresh_once()
    entry = warm._entries[key]
    assert 'pool' not in (entry['facts'] or {}) and entry['leg']['fetched_at'] >= clock.t - 1.0
    assert ('rpc', 'jsonParsed') in calls                    # the quote mint's own safety read


# --- Alpha (b), 2026-10-15: Gamma MEDIUM on the shared funded Jupiter key (p90 ~578/min sits in
# the 429 zone; 23 of 74 429-minutes on 10-14 hit the funded role). The warmer is speculative load on
# that key, so it stays off it while ANY funded-role call was refused in the last ~10 s.

def _http_429(method, url, headers=None, body=None):
    return 429, {}, None


def test_the_warmer_skips_its_refresh_while_a_funded_call_saw_a_429(monkeypatch):
    from sniper.execution import solana_real_seams as S
    monkeypatch.setattr(S, '_jup_host_cooldown', {})
    monkeypatch.setattr(S, '_funded_last_429_at', None)
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    with S.jupiter_role(S.ROLE_FUNDED):
        assert S._jup_call('GET', '/quote?x', http=_http_429)[0] == 429       # an entry/exit quote refused
    clock.t += 2.0
    warm.refresh_once()
    assert fetches == [] and warm.stats['funded_429_skips'] == 1
    monkeypatch.setattr(S, '_funded_last_429_at', S._funded_last_429_at - LL.WARM_FUNDED_429_QUIET_S - 0.1)
    clock.t += 2.0
    warm.refresh_once()
    assert fetches == [('raydium_cpmm', QUOTE, 10_000_000)] and warm.stats['funded_429_skips'] == 1


def test_a_paper_role_429_never_quiets_the_warmer(monkeypatch):
    from sniper.execution import solana_real_seams as S
    monkeypatch.setattr(S, '_jup_host_cooldown', {})
    monkeypatch.setattr(S, '_funded_last_429_at', None)
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    with S.jupiter_role(S.ROLE_PAPER):
        S._jup_call('GET', '/quote?x', http=_http_429)
    clock.t += 2.0
    warm.refresh_once()
    assert len(fetches) == 1 and warm.stats['funded_429_skips'] == 0


def test_a_seed_for_another_coin_never_wipes_a_warm_same_quote_leg():
    """Gamma X4: the binding is the quote token, so a seed from a second coin's pool on the same quote
    is the same leg; it must keep the warm leg, its last use and its window."""
    clock, b = Clock(), _exotic_builder(PEPE_EV)
    facts = b.pools.get(PEPE_EV.pool)
    warm = _warm(clock, [])
    warm.set_clip_inventory((10_000_000,))
    leg = _leg(clock.t)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=leg, builder=b, facts=facts)
    other = _other_coin(facts)
    b.pools._facts[other['pool']] = other                    # its pool is cached, so seed() reaches _put
    clock.t += 1.0
    warm.seed('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=other)
    entry = warm._entries[('raydium_cpmm', QUOTE, 10_000_000)]
    assert entry['leg'] is leg and entry['last_use'] == clock.t - 1.0 and entry['facts'] is facts
    assert warm.get('raydium_cpmm', QUOTE, 10_000_000, builder=b, facts=other) is leg


@pytest.mark.parametrize('raw', ['abc', '', 'nan', 'inf', '-5', '0'])
def test_a_malformed_window_falls_back_to_the_old_sixty_seconds(monkeypatch, raw):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S', raw)
    try:
        assert importlib.reload(cfg).MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S == 60.0
        assert LL.WarmExoticLegs(clock=Clock(), fetch_fn=lambda e: (None, [])).window_s == 60.0
    finally:
        monkeypatch.delenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S')
        importlib.reload(cfg)


def test_a_well_formed_window_is_kept(monkeypatch):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S', '900')
    try:
        assert importlib.reload(cfg).MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S == 900.0
    finally:
        monkeypatch.delenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_WINDOW_S')
        importlib.reload(cfg)


@pytest.mark.parametrize('age_s, refreshed', [(9.9, False), (10.1, True)])
def test_the_funded_429_quiet_window_is_ten_seconds(monkeypatch, age_s, refreshed):
    """Gamma Q3 on af6ef387c: the window itself, in literal seconds, so a changed constant fails."""
    from sniper.execution import solana_real_seams as S
    monkeypatch.setattr(S, '_funded_last_429_at', S._monotonic() - age_s)
    clock, fetches = Clock(), []
    warm = _warm(clock, fetches)
    warm.note('raydium_cpmm', QUOTE, 10_000_000, leg=_leg(clock.t), builder=object())
    clock.t += 2.0
    warm.refresh_once()
    assert bool(fetches) is refreshed and warm.stats['funded_429_skips'] == int(not refreshed)


# THE CAP FOLLOWS THE CLIP INVENTORY, AND A RE-SEED RENEWS (Alpha GO 2026-10-16, RIBBIT 01cc75c7).
# The lane seeds one leg per inventory clip, smallest first. At the old cap of 2, a three-clip
# inventory (the 50 USD trial clip, the clip, its scaled alternate) evicted the 50 USD leg in the same
# pass, and a re-seed of the same binding kept the first seed's clock, so a pool seeded only at its
# migration went cold 1800 s later. MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX (default 4): the cap is
# max(2, min(knob, inventory clips)), the HTTP budget is 2 starts per leg per 1.5 s, and a same-binding
# re-seed renews the window. A knob of 2 or less is the old warmer exactly.
INVENTORY = (50_000_000, 101_880_000, 133_700_000)


class _Http:
    def request(self, method, url, *, headers=None, body=None):
        return 200, b'{}'


def _seeded(monkeypatch, knob):
    from test_slot_lane_pump_nonsol import SPCX, _builder
    monkeypatch.setattr(L.cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', knob, raising=False)
    clock, fetches, b = Clock(), [], _builder(SPCX)

    def fetch(entry):                                  # two real budgeted starts, as /quote + /swap-instructions
        fetches.append(entry['key'][2])
        warm._warm_http('GET', 'quote')
        warm._warm_http('POST', 'swap-instructions')
        return _leg(clock.monotonic(), usdc=entry['key'][2], mint=entry['key'][1]), [200, 200]
    warm = LL.WarmExoticLegs(clock=clock, fetch_fn=fetch, window_s=1800.0, http=_Http())
    warm.set_clip_inventory(INVENTORY)
    facts = b.pools.get(SPCX['pool'])
    for atoms in INVENTORY:
        warm.seed('pump_amm', SPCX['quote_mint'], atoms, builder=b, facts=facts)
    return warm, b, facts, clock, fetches


@pytest.mark.parametrize('knob, held, evicted', [(4, INVENTORY, 0), (3, INVENTORY, 0),
                                                  (2, INVENTORY[1:], 1), (6, INVENTORY, 0)])
def test_one_pool_seeding_three_clips_keeps_the_50_usd_leg(monkeypatch, knob, held, evicted):
    warm = _seeded(monkeypatch, knob)[0]
    assert tuple(sorted(k[2] for k in warm.keys())) == held
    assert warm.stats['evicted'] == evicted and warm.stats['cap'] == len(held)


@pytest.mark.parametrize('knob, renewed', [(4, True), (2, False)])
def test_a_reseed_of_the_same_binding_renews_the_window(monkeypatch, knob, renewed):
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, facts, clock, fetches = _seeded(monkeypatch, knob)
    clock.t += 1000.0
    warm.seed('pump_amm', SPCX['quote_mint'], INVENTORY[-1], builder=b, facts=facts)
    clock.t += 801.0                                   # 1801 s after the first seed, 801 s after the re-seed
    warm.refresh_once()
    key = ('pump_amm', SPCX['quote_mint'], INVENTORY[-1])
    assert (key in warm.keys()) is renewed
    assert (warm.get(*key) is not None) is renewed
    assert warm.stats['renewed'] == int(renewed)


@pytest.mark.parametrize('knob, refreshed, starts', [(4, 3, 6), (2, 2, 4)])
def test_every_warm_leg_is_refreshed_inside_one_cycle(monkeypatch, knob, refreshed, starts):
    warm, b, facts, clock, fetches = _seeded(monkeypatch, knob)
    warm.refresh_once()
    assert len(fetches) == refreshed and len(warm._http_starts) == starts
    assert all(warm.get('pump_amm', k[1], k[2]) is not None for k in warm.keys())
    with pytest.raises(LL.WarmHttpBudget):             # the budget is 2 starts per leg per 1.5 s, no more
        warm._warm_http('GET', 'one-more')


@pytest.mark.parametrize('raw, cap', [('abc', 2), ('', 2), ('1', 2), ('-3', 2), ('2', 2), ('3', 3), ('9', 3)])
def test_the_knob_parses_and_bounds_the_cap(monkeypatch, raw, cap):
    import importlib
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', raw)
    try:
        importlib.reload(cfg)
        warm = LL.WarmExoticLegs(clock=Clock(), fetch_fn=lambda e: (None, []))
        assert warm.stats['cap'] == 2                  # no inventory yet: the old cap
        warm.set_clip_inventory(INVENTORY)
        assert warm.stats['cap'] == cap
    finally:
        monkeypatch.delenv('MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX')
        importlib.reload(cfg)


@pytest.mark.parametrize('knob, renewed', [(4, True), (2, False)])
def test_a_reseed_renews_a_used_leg_past_its_last_use(monkeypatch, knob, renewed):
    """The window runs from the later of the last live use and the latest seed: a leg used once, then
    re-seeded 1000 s later, is still warm 1801 s after the use."""
    from test_slot_lane_pump_nonsol import SPCX
    warm, b, facts, clock, fetches = _seeded(monkeypatch, knob)
    key = ('pump_amm', SPCX['quote_mint'], INVENTORY[-1])
    warm.note(*key, leg=_leg(clock.t, usdc=key[2], mint=key[1]), builder=b, facts=facts)
    clock.t += 1000.0
    warm.seed(*key, builder=b, facts=facts)
    clock.t += 801.0
    warm.refresh_once()
    assert (key in warm.keys()) is renewed


def test_a_funded_429_landing_mid_cycle_stops_the_remaining_warm_legs(monkeypatch):
    """Gamma LOW-1 on 64aa0a150: the funded-429 quiet check ran only at cycle start, so at cap 3 a
    funded 429 landing during the first leg's fetch still let the other two legs (4 calls) out."""
    from sniper.execution import solana_real_seams as S
    monkeypatch.setattr(S, '_funded_last_429_at', None)
    warm, b, facts, clock, fetches = _seeded(monkeypatch, 4)
    first = warm.fetch_fn

    def fetch(entry):
        result = first(entry)
        monkeypatch.setattr(S, '_funded_last_429_at', S._monotonic())   # the funded seam 429s meanwhile
        return result
    warm.fetch_fn = fetch
    warm.refresh_once()
    assert len(fetches) == 1 and len(warm._http_starts) == 2
    assert warm.stats['funded_429_skips'] == 1


# --- F8 (Codex review through 648ada5d7): no speculative dispatch inside the funded 429 quiet window --

def test_f8_a_funded_429_during_the_warm_get_stops_the_post_before_dispatch():
    """The Codex probe: an unrelated funded call 429s while the warm /quote GET is in flight; the
    /swap-instructions POST of the same leg is cancelled locally, never dispatched."""
    from sniper.execution import solana_real_seams as seams

    class Http:
        def __init__(self): self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append((method, url))
            if method == 'GET':
                seams._funded_last_429_at = seams._monotonic()   # a funded exit 429s meanwhile
            return 200, {}, None
    http = Http()
    warm = LL.WarmExoticLegs(clock=Clock(), http=http)
    warm._actual_statuses = []
    assert warm._warm_http('GET', 'https://fake.invalid/quote')[0] == 200
    with pytest.raises(LL.WarmHttpBudget):
        warm._warm_http('POST', 'https://fake.invalid/swap-instructions')
    assert http.calls == [('GET', 'https://fake.invalid/quote')]
    assert warm.stats['funded_429_skips'] == 1 and warm.stats['http_calls'] == 1


def test_f8_a_failover_host_after_the_legs_own_funded_429_is_not_dispatched():
    """Host failover inside the leg: the keyed host 429s (stamping the funded quiet window), so
    the next host's request in the same _jup_call never leaves either."""
    from sniper.execution import solana_real_seams as seams

    class Http:
        def __init__(self): self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append(url)
            seams._funded_last_429_at = seams._monotonic()        # what _note_host_429 records
            return 429, {}, None
    http = Http()
    warm = LL.WarmExoticLegs(clock=Clock(), http=http)
    warm._actual_statuses = []
    warm._warm_http('GET', 'https://keyed.invalid/quote')
    with pytest.raises(LL.WarmHttpBudget):
        warm._warm_http('GET', 'https://lite.invalid/quote')
    assert http.calls == ['https://keyed.invalid/quote']


def test_f8_after_the_quiet_window_the_warm_request_goes_out_again(monkeypatch):
    from sniper.execution import solana_real_seams as seams
    now = {'t': 1000.0}
    monkeypatch.setattr(seams, '_monotonic', lambda: now['t'])
    seams._funded_last_429_at = 1000.0

    class Http:
        def __init__(self): self.calls = []
        def request(self, method, url, **kwargs):
            self.calls.append(url)
            return 200, {}, None
    http = Http()
    warm = LL.WarmExoticLegs(clock=Clock(), http=http)
    warm._actual_statuses = []
    now['t'] += LL.WARM_FUNDED_429_QUIET_S - 0.01
    with pytest.raises(LL.WarmHttpBudget):
        warm._warm_http('GET', 'q')
    now['t'] += 0.02
    assert warm._warm_http('GET', 'q')[0] == 200 and http.calls == ['q']
