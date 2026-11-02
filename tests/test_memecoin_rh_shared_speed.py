"""4663 lane shared routes, speed fix 1 (operator yes 2026-10-14 via Alpha). The early worker is
sized from the datagram's wallet snapshot (MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT), and a v4 or
split coin's fresh passing lane verdict stands in for the funded token safety
(MEMECOIN_RH_FAST_SHARED_CACHED_SAFETY). Both default off. A pons or curve verdict is never reused,
and every other verdict state runs the funded check."""
import inspect
from collections import deque
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_rh_fast_lane as lane
from sniper.settings import memecoin as cfg

MINT = "0x" + "ab" * 20
TTL, USD = 60.0, 175.0


def _cache(ok=True, reason="ok", at=1000.0):
    cache = lane.SafetyCache(lambda m, usd=None: (ok, reason), ttl_s=TTL, usd=USD)
    if at is not None:
        cache._verdicts[MINT] = (ok, reason, at, 0.0)
    return cache


class Funded:
    """The funded token safety: records each call, refuses so a fallback is visible."""

    def __init__(self):
        self.calls = []

    def __call__(self, mint, *, deadline=None, usd=None, timings=None):
        self.calls.append(dict(mint=mint, deadline=deadline, usd=usd, timings=timings))
        return False, "funded_refused"


# --- the reuse rule --------------------------------------------------------------------------

@pytest.mark.parametrize("verdict, usd, now, why", [
    (None, 50.0, 1010.0, "absent"),
    ((False, "sell_tax_high", 1000.0, 0.0), 50.0, 1010.0, "refused"),
    ((True, "ok", 1000.0, 0.0), 50.0, 1000.0 + TTL + 1, "stale"),
    ((True, "ok", 1000.0, 0.0), 50.0, 999.0, "stale"),               # a verdict from the future
    ((True, "ok", 1000.0, 0.0), None, 1010.0, "no_usd"),
    ((True, "ok", 1000.0, 0.0), USD + 0.01, 1010.0, "clip_above_verdict"),
    ((True, "launchpad_curve_exit_quotable_at_clip", 1000.0, 0.0), 50.0, 1010.0, "curve_verdict"),
])
def test_only_a_fresh_non_curve_pass_at_or_above_the_clip_is_reused(verdict, usd, now, why):
    assert lane.shared_cache_refusal(verdict, usd=usd, verdict_usd=USD, ttl_s=TTL, now=now) == why


@pytest.mark.parametrize("usd", [USD / 4, 50.0, USD])
def test_a_fresh_pass_covers_any_clip_from_a_quarter_up_to_its_own(usd):
    assert lane.shared_cache_refusal((True, "ok", 1000.0, 0.0), usd=usd, verdict_usd=USD,
                                     ttl_s=TTL, now=1000.0 + TTL) is None


def test_the_verdict_read_is_one_tuple_and_counts_nothing():
    cache = _cache()
    assert cache.verdict(MINT.upper().replace("0X", "0x")) == (True, "ok", 1000.0, 0.0)
    assert cache.verdict("0x" + "cd" * 20) is None
    assert not cache.lookups and not cache.hit_age_s        # the native coverage evidence untouched


# --- the funded check stays the fallback -------------------------------------------------------

def test_a_fresh_pass_answers_without_the_funded_check():
    funded = Funded()
    safety = lane.cache_first_safety(funded, _cache(), now_fn=lambda: 1010.0)
    ok, why = safety(MINT, usd=50.0, deadline=5.0)
    assert ok is True and why == "%s: ok" % lane.SHARED_CACHE_LABEL and funded.calls == []


@pytest.mark.parametrize("cache, usd, now", [
    (_cache(at=None), 50.0, 1010.0),                          # absent
    (_cache(at=1000.0), 50.0, 1000.0 + TTL + 1),              # stale
    (_cache(ok=False, reason="honeypot"), 50.0, 1010.0),      # refused
    (_cache(), USD + 1, 1010.0),                              # clip above the verdict's
    (_cache(), None, 1010.0),                                 # no clip given
    (_cache(reason="launchpad_curve_exit_quotable_at_clip"), 50.0, 1010.0),
])
def test_every_other_verdict_state_runs_the_funded_check_with_its_own_arguments(cache, usd, now):
    funded = Funded()
    safety = lane.cache_first_safety(funded, cache, now_fn=lambda: now)
    kwargs = dict(deadline=5.0, timings={})
    if usd is not None:
        kwargs["usd"] = usd
    assert safety(MINT, **kwargs) == (False, "funded_refused")
    assert funded.calls == [dict(mint=MINT, deadline=5.0, usd=usd, timings={})]


def test_the_wrapper_keeps_the_funded_signature_so_the_leg_passes_the_same_kwargs():
    funded = Funded()
    safety = lane.cache_first_safety(funded, _cache(), now_fn=lambda: 1010.0)
    assert inspect.signature(safety) == inspect.signature(funded)


def test_cached_answer_never_passes_a_verdict_that_turned_into_a_refusal():
    # Alpha's condition (2026-10-14): reason() saw a fresh pass, a warmer replaced it with a
    # refusal, and cached_answer() used to answer True with the refusal as its basis.
    cache = _cache()
    assert cache.reason(MINT, now=1010.0) is None
    cache._verdicts[MINT] = (False, "honeypot", 1011.0, 0.0)
    ok, why = cache.cached_answer(MINT, "rh_fast_cache")
    assert ok is False and "honeypot" in why
    del cache._verdicts[MINT]
    assert cache.cached_answer(MINT, "rh_fast_cache")[0] is False


# --- the datagram snapshot sizes the early worker ----------------------------------------------

SNAP = dict(read_at=1000.0, weth_wei=2 * 10**17, native_wei=3 * 10**15, gas_price_wei=10**7)


def _balances(snap=SNAP, quote_usd=4000.0, generation=990.0, now=1005.0):
    return lane.snapshot_quote_balances(dict(snapshot=snap), quote_usd, generation=generation,
                                        now_host=now, max_age_s=30.0, margin_s=0.5)


def test_a_fresh_snapshot_sizes_in_quote_usd_and_native_units():
    assert _balances() == pytest.approx((800.0, 0.003))


@pytest.mark.parametrize("change", [
    dict(snap=None), dict(generation=None), dict(generation=lane.GENERATION_INFLIGHT),
    dict(generation=999.6),                                  # read at or before last spend + margin
    dict(now=1031.0), dict(now=999.0),                       # too old / from the future
    dict(snap=dict(SNAP, weth_wei=0)), dict(snap=dict(SNAP, weth_wei="1")),
    dict(snap=dict(SNAP, native_wei=-1)), dict(snap=dict(SNAP, read_at=None)),
    dict(quote_usd=0.0), dict(quote_usd=float("nan")), dict(quote_usd=True),
])
def test_an_unusable_snapshot_sizes_nothing(change):
    assert _balances(**change) is None


def test_snapshot_sizing_never_touches_the_gate_balance_cache(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner, "_gate_balance_cache", {"4663": "sentinel"})
    _balances()
    assert runner._gate_balance_cache == {"4663": "sentinel"}


def test_the_detector_sends_its_inputs_on_a_shared_datagram():
    rec = dict(tx_hash="0x" + "cd" * 32, seq=7, wallet="0x" + "ef" * 20, mint=MINT, handle="h",
               source_user_id="uid", permit_nonce=1, arrival_utc="2026-10-14T20:00:00+00:00")
    out = dict(route_kind="v4_shared", pool="0x" + "11" * 32, src_out=10, post_ratio=1.0,
               source_fill=None, paid_usd=100.0)
    inputs = dict(snapshot=SNAP, eth_usd=4000.0, eth_usd_at=999.0)
    p = lane.lane_payload(rec, out, sent_epoch=1001.0, inputs=inputs)
    assert (p["snapshot"], p["eth_usd"], p["eth_usd_at"]) == (SNAP, 4000.0, 999.0)


# --- the shared hook's context (both knobs) ------------------------------------------------------

def _shared(monkeypatch, kind, *, snapshot_on, cache_on, cache=None):
    """_handle_shared up to the funded hook: the chain context the hook is built on."""
    from sniper.services import memecoin_bsc_fast_lane as bsc
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_SHARED_EARLY_SNAPSHOT", snapshot_on, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_SHARED_CACHED_SAFETY", cache_on, raising=False)
    monkeypatch.setattr(lane, "validate_source_fill",
                        lambda proof, sig: dict(token_decimals=18, source_post={"pools": ["p"]}))
    bsc.clear_policy_decisions()
    funded = Funded()
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.stopped = False
    live.cache = cache or _cache()
    live.stats_fn = lambda mint: None
    live.host_clock = lambda: 1010.0
    live.generation_fn = lambda: 990.0
    live.max_snapshot_age_s, live.generation_margin_s = 30.0, 0.5
    live.ctx, live.contexts, live.conn = dict(address="0x" + "ee" * 20, token_safety_fn=funded), {}, None
    live.clip_usd, live.fills, live.send_times = USD, 0, deque()
    seen = {}

    def factory(contexts, stats_fn):
        seen.update(contexts["4663"])
        return lambda conn, ev: dict(status="rejected", reason="fixture_only")
    live.shared_hook_factory = factory
    sig = dict(tx_hash="0x" + "cd" * 32, seq=7, wallet="0x" + "ef" * 20, mint=MINT,
               source_user_id="uid", permit_nonce=1, pool="0x" + "11" * 32, src_out=10**24,
               post_ratio=1.0, paid_usd=100.0, arrival_utc="2026-10-14T20:00:00+00:00",
               sent_epoch=1009.0, route_kind=kind, source_fill={}, snapshot=SNAP, eth_usd=4000.0,
               eth_usd_at=1000.0)
    try:
        assert live._handle_shared(sig, {})["reason"] == "fixture_only"
    finally:
        bsc.clear_policy_decisions()
    return seen, funded


@pytest.mark.parametrize("kind", ["v4_shared", "split_shared"])
def test_a_v4_or_split_shared_signal_reuses_a_fresh_pass_and_falls_back_otherwise(monkeypatch, kind):
    ctx, funded = _shared(monkeypatch, kind, snapshot_on=False, cache_on=True)
    assert ctx["token_safety_fn"](MINT, usd=50.0)[0] is True and funded.calls == []
    stale, funded = _shared(monkeypatch, kind, snapshot_on=False, cache_on=True,
                            cache=_cache(at=1010.0 - TTL - 1))
    assert stale["token_safety_fn"](MINT, usd=50.0) == (False, "funded_refused")
    assert len(funded.calls) == 1


@pytest.mark.parametrize("kind", ["pons_shared", "v3_shared"])
def test_pons_and_v3_shared_signals_keep_the_funded_check(monkeypatch, kind):
    ctx, funded = _shared(monkeypatch, kind, snapshot_on=True, cache_on=True)
    assert ctx["token_safety_fn"] is funded


def test_with_both_knobs_off_the_shared_context_is_todays(monkeypatch):
    ctx, funded = _shared(monkeypatch, "v4_shared", snapshot_on=False, cache_on=False)
    assert ctx["token_safety_fn"] is funded and "early_quote_balances_fn" not in ctx


def test_the_snapshot_knob_gives_the_early_worker_the_datagrams_balances(monkeypatch):
    ctx, _funded = _shared(monkeypatch, "pons_shared", snapshot_on=True, cache_on=False)
    assert ctx["early_quote_balances_fn"](4000.0) == pytest.approx((800.0, 0.003))


# --- the early worker's sizing, end to end through _early_evm_quote_clip -------------------------

from test_evm_early_cached_sizing import sizing  # noqa: E402,F401  (the HUSH sizing fixture)


@pytest.mark.parametrize("sizing", ["4663"], indirect=True)
def test_the_early_sizing_takes_the_snapshot_without_waiting_for_the_balance_read(monkeypatch, sizing):
    from functools import partial
    call, pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True, raising=False)
    pre.context["quote_price_peek_fn"] = lambda: {"px": 4000.0, "age_ms": 10.0}
    pre.context["early_quote_balances_fn"] = partial(
        lane.snapshot_quote_balances, dict(snapshot=SNAP), generation=990.0, now_host=1005.0,
        max_age_s=30.0, margin_s=0.5)
    (clip, refusal, facts), seen, fresh = call()
    assert (clip, refusal, facts["balance_source"]) == (25.0, None, "lane_snapshot")
    assert seen["usdc"] == pytest.approx(800.0) and fresh == [], "the balance read was never awaited"


@pytest.mark.parametrize("sizing", ["4663"], indirect=True)
def test_an_unusable_snapshot_leaves_the_early_sizing_on_todays_balance_read(monkeypatch, sizing):
    from functools import partial
    call, pre = sizing
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CACHED_SIZING", True, raising=False)
    pre.context["quote_price_peek_fn"] = lambda: {"px": 4000.0, "age_ms": 10.0}
    pre.context["early_quote_balances_fn"] = partial(
        lane.snapshot_quote_balances, dict(snapshot=SNAP), generation=1000.0, now_host=1005.0,
        max_age_s=30.0, margin_s=0.5)                 # read before our last spend
    (_clip, _refusal, facts), seen, fresh = call()
    assert facts["balance_source"] == "fresh" and fresh == [1] and seen["usdc"] == 300.0


@pytest.mark.parametrize("extra", [dict(bought_amount_atomic=10**21), dict(block_tag="0x10"),
                                   dict(some_future_exact_input=1)])
def test_an_exact_question_never_gets_the_nominal_cached_answer(extra):
    # Gamma MEDIUM on 13eaf8cea: the leg's quote-bound path asks with the bought amount (and Ink's
    # pinned block); a $175 nominal verdict must not answer it. Only usd/deadline/timings may.
    class ExactFunded:
        def __init__(self):
            self.calls = []

        def __call__(self, mint, *, deadline=None, usd=None, timings=None, bought_amount_atomic=None,
                     block_tag=None, **kw):
            self.calls.append(dict(bought_amount_atomic=bought_amount_atomic, block_tag=block_tag, **kw))
            return False, "funded_refused"
    funded = ExactFunded()
    safety = lane.cache_first_safety(funded, _cache(), now_fn=lambda: 1010.0)
    assert safety(MINT, usd=50.0, **extra) == (False, "funded_refused") and len(funded.calls) == 1


@pytest.mark.parametrize("usd", [USD / 4 - 0.01, 11.0])
def test_a_clip_under_a_quarter_of_the_verdicts_runs_the_funded_check(usd):
    # Alpha 2026-10-14 (Gamma's bound): a fixed per-swap cost reads ~16x larger at $11 than at $175.
    assert lane.shared_cache_refusal((True, "ok", 1000.0, 0.0), usd=usd, verdict_usd=USD,
                                     ttl_s=TTL, now=1010.0) == "clip_below_quarter"
    funded = Funded()
    safety = lane.cache_first_safety(funded, _cache(), now_fn=lambda: 1010.0)
    assert safety(MINT, usd=usd) == (False, "funded_refused") and len(funded.calls) == 1
