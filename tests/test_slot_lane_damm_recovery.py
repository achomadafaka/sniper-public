import pytest

from sniper.execution import meteora_damm_v2_buy as D
from sniper.execution import raydium_launchlab_buy as LL
from test_slot_lane_damm_nonsol import SPCX, _builder, _quoted, _leg
from test_slot_lane_damm_v2 import CLASSIC, _routed, _learned, BLOCKHASH


def test_real_damm_builder_runs_shared_leg_worker(monkeypatch):
    b = _builder(_quoted(SPCX))
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), b.pools)
    queued, caches = [], []
    def prefetch(*args, **kwargs):
        caches.append(kwargs['alt_cache'])
        return _leg(SPCX)
    from sniper.execution import pump_curve_buy as P
    monkeypatch.setattr(P, 'prefetch_usdc_sol_leg', prefetch)
    w = LL.OnDemandLegWorker(builders={'meteora_damm_v2': b}, admission_fn=lambda *a: None,
        snapshot_fn=lambda: None, clip_fn=lambda: 100, offer_fn=lambda *a: None,
        live_fn=lambda *a: True, requeue_fn=queued.append, start=False)
    monkeypatch.setattr(w, '_live_quote', lambda *a, **kw: ((6, 0, 0), None))
    result = w.process(buy)
    assert result['status'] == 'requeued' and len(queued) == 1
    assert caches == [b._alt_cache]
    assert b._alt_cache is not _builder(_quoted(SPCX))._alt_cache


def test_damm_sol_build_spends_requested_leg_without_reusing_wrong_clip():
    from types import SimpleNamespace
    cache = _learned(CLASSIC)
    b = _builder(cache)  # leg_fn fails if the builder ignores the supplied event-specific leg
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    leg = _leg(D.WSOL_MINT, out=666_666_666, usdc=120_700_000)
    message, validity = b.build(buy, 120.70,
        SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.),
        leg=leg, quote=(9, 150., 0, 0))
    assert message is not None, validity
    assert validity['pump_price_limit']['cost_usdc_atomic'] == 120_700_000
    assert validity['pump_price_limit']['spend_lamports'] == leg['min_out_lamports']
    assert b.build(buy, 100., SimpleNamespace(blockhash=BLOCKHASH, sol_usd=150.),
                   leg=leg, quote=(9, 150., 0, 0)) == (None, 'leg_over_clip')


def test_damm_scaled_sol_build_reuses_the_warm_pumpswap_leg():
    from types import SimpleNamespace
    from sniper.services import memecoin_slot_lane as lane
    from sniper.execution import pumpswap_buy as A
    cache = _learned(CLASSIC)
    b = _builder(cache)
    buy, _ = D.damm_slot_buy(_routed(CLASSIC), cache)
    a = A.SlotLanePumpSwapBuilder(user=b.user, clock=lambda: 1000.)
    full = _leg(D.WSOL_MINT, out=888_888_888, usdc=160_000_000)
    scaled = _leg(D.WSOL_MINT, out=666_666_666, usdc=120_000_000)
    a._leg, a._cash_legs = full, (full, scaled)
    builders = {'pump_amm': a, 'meteora_damm_v2': b}
    assert lane.cached_cash_leg(builders, buy, 120.) is scaled
    message, validity = lane.dispatch_build(builders)(buy, 120.,
        SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.))
    assert message is not None, validity
    assert validity['pump_price_limit']['cost_usdc_atomic'] == 120_000_000
    # Cached inputs cannot substitute a wrong clip or pay a non-SOL pool.
    assert lane.cached_cash_leg(builders, buy, 100.) is None
    from dataclasses import replace
    assert lane.cached_cash_leg(builders, replace(buy, quote_mint=SPCX), 120.) is None
    a.clock = lambda: 1010.
    assert lane.cached_cash_leg(builders, buy, 120.) is None


def test_unexpected_worker_error_releases_hold_without_waiting_for_expiry(monkeypatch):
    refused = []
    w = LL.OnDemandLegWorker(builders={'meteora_damm_v2': object()}, admission_fn=lambda *a: None,
        snapshot_fn=lambda: None, clip_fn=lambda: 100, offer_fn=lambda *a: None,
        live_fn=lambda *a: True, refuse_fn=lambda b, why: refused.append((b.signature, why)), start=False)
    b, _ = D.damm_slot_buy(_routed(CLASSIC), _quoted(SPCX))
    assert w.offer(b)
    real_get = w._q.get
    def get():
        if refused:
            raise SystemExit
        if w._in_flight == 0:
            raise SystemExit
        return real_get()
    monkeypatch.setattr(w, '_q', type('Queue', (), {'get': staticmethod(get)})())
    monkeypatch.setattr(w, 'process', lambda *a, **k: (_ for _ in ()).throw(AttributeError('builder')))
    with pytest.raises(SystemExit):
        w._run()
    assert refused == [(b.signature, 'slot_lane_exotic_worker_error')]
    assert w._in_flight == 0
