"""The source's own pool as an entry-ladder hint (2026-10-15, operator "fill fastest"; Alpha go).

4663 quote_initial ran p50 876 ms / p90 1508 ms over 12 submits since 10-13, nearly all route
discovery; the multihop class (coin paired with a token other than ETH) was p50 630 / p90 1508 and
won by v4_multihop 13 of 17 times through provider pool lists. The pool the source bought through is
already on the event (`ev.evm_chase_reference`, the receipt decode). Two rungs quote it beside
today's rungs with no discovery; anything they cannot do is a miss and today's rungs decide.

Pins (Alpha): a wrong, stale or unknown hint never refuses; the hint comes from the reference or a
remembered pool a hint already proved, never a provider; the cache is keyed by coin and pool and
dropped on a failed quote (not on a blind read); a source hit over budget is a miss; a source winner
that fails a price gate re-asks today's rungs instead of filling or refusing at once; the trace
carries each hit's output on a source ladder and a cut rung's log line names the winner's delta."""
import logging
import threading
import time

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_source_route as src
from sniper.execution import evm_v4_quote as v4q
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN = "4663"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE = "0x" + "0" * 40
COIN = "0x" + "22" * 20
BRIDGE = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
TAKER = "0x" + "11" * 20
IN = 10 ** 16
KEY = {"currency0": NATIVE, "currency1": COIN, "fee": 10000, "tick_spacing": 200, "hooks": NATIVE}
POOL = pool_id_for_key(KEY).lower()
X_KEY = {"currency0": COIN, "currency1": BRIDGE, "fee": 3000, "tick_spacing": 60, "hooks": NATIVE}
X_POOL = pool_id_for_key(X_KEY).lower()


class _Ref:
    def __init__(self, post):
        self.post = post

    def payload(self):
        return self.post


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_pools", {})
    monkeypatch.setattr(v4q, "_key_cache", {})
    yield


# --- the hint and the cache --------------------------------------------------------------------

@pytest.mark.parametrize("post, want", [
    ({"venue": "v4", "pool": POOL.upper().replace("0X", "0x")}, POOL),
    ({"venue": "v3", "pool": POOL}, None),
    ({"venue": "v4", "pool": "0xabc"}, None),
    ({"reason": "split_route"}, None),
    (None, None),
])
def test_the_hint_is_the_reference_pool_only(post, want):
    assert src.hint_from_reference(_Ref(post)) == want
    assert src.hint_from_reference(None) is None


def test_a_remembered_pool_answers_only_when_the_event_has_none():
    assert src.attempt_hint(CHAIN, _Ref({"reason": "split_route"}), COIN) is None
    src.remember(CHAIN, COIN, POOL)
    assert src.attempt_hint(CHAIN, _Ref({"reason": "split_route"}), COIN) == POOL
    assert src.attempt_hint(CHAIN, _Ref({"venue": "v4", "pool": X_POOL}), COIN) == X_POOL


def test_the_cache_is_keyed_by_coin_and_pool_expires_and_forgets(monkeypatch):
    src.remember(CHAIN, COIN, POOL, now=100.0)
    src.remember(CHAIN, COIN, X_POOL, now=200.0)
    assert src.remembered(CHAIN, COIN, now=300.0) == X_POOL
    src.forget(CHAIN, COIN, X_POOL)
    assert src.remembered(CHAIN, COIN, now=300.0) == POOL
    assert src.remembered(CHAIN, COIN, now=100.0 + src.CACHE_TTL_S + 1) is None
    assert src.remembered(CHAIN, "0x" + "33" * 20, now=300.0) is None
    monkeypatch.setattr(src, "CACHE_MAX_COINS", 2)
    for i in range(3):
        src.remember(CHAIN, "0x%040x" % (i + 1), POOL, now=1000.0 + i)
    assert len(src._pools) == 2


def test_the_knob_off_gives_no_hint(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", "")
    src.remember(CHAIN, COIN, POOL)
    assert src.attempt_hint(CHAIN, _Ref({"venue": "v4", "pool": POOL}), COIN) is None


def test_a_local_key_must_hash_to_the_pool_and_hold_the_coin(monkeypatch):
    monkeypatch.setattr(v4q, "_pool_key_cached", lambda c, p: dict(KEY))
    assert src.local_key(CHAIN, POOL, COIN) == KEY and v4q._key_cache[(CHAIN, POOL)] == KEY
    assert src.local_key(CHAIN, POOL, "0x" + "33" * 20) is None          # not this coin's pool
    monkeypatch.setattr(v4q, "_pool_key_cached", lambda c, p: dict(KEY, fee=3000))
    monkeypatch.setattr(src, "_tape_key", lambda c, p: None)
    assert src.local_key(CHAIN, POOL, COIN) is None                       # does not hash back
    monkeypatch.setattr(src, "_tape_key", lambda c, p: dict(KEY))
    assert src.local_key(CHAIN, POOL, COIN) == KEY                        # the tape answers


# --- the ladder ----------------------------------------------------------------------------------

def _quote(out, pool_id, protocol="uniswap_v4"):
    return {"sellAmount": str(IN), "buyAmount": str(out),
            "route": {"protocol": protocol, "pool_id": pool_id},
            "transaction": {"to": "0xrouter", "data": "0x"}, "issues": {"allowance": None}}


SLOW_S = 0.6


def _ladder(monkeypatch, *, key=KEY, pinned=None, reverse=0.97, slow_v4=None, multihop=None,
            rungs=None, hint=POOL, call_kw=None, grace="0", keys=None, reverse_fn=None):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", multihop is not None, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_FLAP_CURVE_ENABLED", False, raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_4663", grace)
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(S, "_v4_same_pool_reverse", reverse_fn or (lambda *a, **k: int(IN * reverse)))
    if keys is None:
        monkeypatch.setattr(src, "local_key", lambda c, p, m: dict(key) if key else None)
    else:
        monkeypatch.setattr(v4q, "_pool_key_cached", lambda c, p: keys.get(p))
        monkeypatch.setattr(src, "_tape_key", lambda c, p: None)
    pinned_calls = []

    def pinned_fn(chain, pool_id, k, sell, buy, amount, *, rpc_url, http=None):
        assert amount == IN
        pinned_calls.append(pool_id)
        if isinstance(pinned, Exception):
            raise pinned
        if pinned is False:
            return None
        return pinned if pinned is not None else {
            "amount_out": 1000, "pool_id": pool_id, "pool_key": k, "zero_for_one": True,
            "pool_fee": k["fee"], "quote_currency": NATIVE}
    monkeypatch.setattr(v4q, "v4_pinned_pool_quote", pinned_fn)

    def v4_fn(chain, sell, buy, amount, taker, **kw):
        if kw.get("quote_override") is not None:
            o = kw["quote_override"]
            return _quote(o["amount_out"], o["pool_id"])
        if slow_v4 is None:
            return None                          # the real dex_v4_quote answers a dict or None
        time.sleep(SLOW_S)
        return _quote(slow_v4, "0xprovider")
    if multihop is not None:
        monkeypatch.setattr("sniper.execution.evm_v4_multihop.dex_v4_multihop_quote", multihop)
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=v4_fn, relay_quote_fn=lambda *a, **k: None,
        acct=object(), rpc_url="https://rpc", on_rung=(rungs.append if rungs is not None else None))
    kw = dict(call_kw or {})
    if hint is not None:
        kw["route_hint"] = hint
    started = time.monotonic()
    out = fn(WETH, COIN, IN, slippage_bps=100, **kw)
    return out, time.monotonic() - started, pinned_calls


def _rec(rungs, name):
    return next(r for r in rungs if r["rung"] == name)


def test_the_source_pool_wins_before_slow_discovery_answers(monkeypatch):
    rungs = []
    out, took, pinned = _ladder(monkeypatch, slow_v4=900, rungs=rungs)
    assert out["route"]["pool_id"] == POOL and out["_sniper_winner"] == "v4_source"
    assert took < SLOW_S and pinned == [POOL]
    assert _rec(rungs, "v4_source")["hit"] and _rec(rungs, "v4_source")["buy_out"] == "1000"
    assert _rec(rungs, "v4")["blind_reason"] == "ladder_grace"
    assert src.remembered(CHAIN, COIN) == POOL                    # a source win is remembered


@pytest.mark.parametrize("why, kw", [
    ("source_key_unknown", dict(key=None)),
    ("source_pool_not_cash_paired", dict(key=X_KEY)),
])
def test_a_hint_that_does_not_resolve_falls_through_to_todays_winner(monkeypatch, why, kw):
    rungs = []
    out, _took, pinned = _ladder(monkeypatch, slow_v4=900, rungs=rungs, **kw)
    assert out["route"]["pool_id"] == "0xprovider" and out["_sniper_winner"] == "v4"
    assert _rec(rungs, "v4_source")["reason"] == why and pinned == []


def test_no_quote_forgets_the_pool_and_today_decides(monkeypatch):
    src.remember(CHAIN, COIN, POOL)
    rungs = []
    out, _took, _p = _ladder(monkeypatch, slow_v4=900, rungs=rungs, pinned=False)
    assert out["_sniper_winner"] == "v4"
    assert _rec(rungs, "v4_source")["reason"] == "source_pool_no_quote"
    assert src.remembered(CHAIN, COIN) is None


def test_a_blind_read_is_a_miss_that_keeps_the_pool(monkeypatch):
    src.remember(CHAIN, COIN, POOL)
    rungs = []
    out, _took, _p = _ladder(monkeypatch, slow_v4=900, rungs=rungs,
                             pinned=v4q.PinnedQuoteBlind("rpc_rate_limit"))
    assert out["_sniper_winner"] == "v4"
    rec = _rec(rungs, "v4_source")
    assert rec["blind_reason"] == "rpc_rate_limit" and rec["reason"] == "source_quote_blind"
    assert src.remembered(CHAIN, COIN) == POOL


def test_a_source_hit_over_budget_is_a_miss_not_a_worse_fill(monkeypatch):
    """Alpha pin (a): grace 0 means the first hit wins, so an expensive source pool must not."""
    rungs = []
    out, took, _p = _ladder(monkeypatch, slow_v4=900, rungs=rungs, reverse=0.60)
    assert _rec(rungs, "v4_source")["reason"].startswith("round_trip_over_budget:")
    # the slow provider rung shares the same fake reverse, so it is refused too: no fill at all
    assert out is None and took >= SLOW_S


def test_the_knob_off_builds_todays_rung_set(monkeypatch):
    rungs_on, rungs_off = [], []
    _ladder(monkeypatch, slow_v4=900, rungs=rungs_on)
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", "")
    out, _t, pinned = _ladder(monkeypatch, slow_v4=900, rungs=rungs_off)
    assert out["_sniper_winner"] == "v4" and pinned == []
    assert {r["rung"] for r in rungs_on} - {r["rung"] for r in rungs_off} == {"v4_source"}
    assert not any("buy_out" in r for r in rungs_off)
    no_hint = []
    _ladder(monkeypatch, slow_v4=900, rungs=no_hint, hint=None)
    assert [r["rung"] for r in no_hint] == [r["rung"] for r in rungs_off]


def test_the_winner_requote_asks_the_source_pool_again(monkeypatch):
    out, _t, pinned = _ladder(monkeypatch, slow_v4=900)
    again = out["_requote"]()
    assert again["route"]["pool_id"] == POOL and pinned == [POOL, POOL]


def test_a_multihop_source_pool_runs_the_hinted_search_alone(monkeypatch):
    seen = []

    def multihop(chain, sell, buy, amount, taker, **kw):
        seen.append(kw.get("coin_pool_hint"))
        if kw.get("coin_pool_hint"):
            q = _quote(1200, X_POOL, protocol="uniswap_v4_multihop")
            q["route"]["round_trip_pct"] = -3.0
            return q
        time.sleep(SLOW_S)
        return None
    rungs = []
    # Grace 0.2 s: the hinted hit ends the ladder, but v4_source still answers its miss before the
    # cut (with grace 0 it was recorded as a ladder_grace cut with no reason; Gamma, 1 of 25 runs).
    out, took, pinned = _ladder(monkeypatch, key=X_KEY, hint=X_POOL, multihop=multihop, rungs=rungs,
                                grace="0.2")
    assert out["_sniper_winner"] == "v4_multihop_source" and took < SLOW_S
    # Grace 0 ends the ladder on the hinted hit; today's multihop thread may not have been scheduled
    # yet when the ladder returns (deploy a0f649f48 refused on that race), so wait for its call.
    deadline = time.monotonic() + SLOW_S + 1.0
    while None not in seen and time.monotonic() < deadline:
        time.sleep(0.005)
    assert X_POOL in seen and None in seen and pinned == []          # today's multihop ran beside it
    assert _rec(rungs, "v4_source")["reason"] == "source_pool_not_cash_paired"
    assert _rec(rungs, "v4_multihop_source")["reason"] == "discovery:source"


def test_a_cut_rung_names_the_source_winners_delta(caplog):
    from concurrent.futures import Future
    f = Future()
    S._log_cut_rung_when_done(f, chain=CHAIN, run_id="r1", name="v4", cut="ladder_grace",
                              winner=("v4_source", 1000))
    with caplog.at_level(logging.WARNING):
        f.set_result((_quote(1100, "0xprovider"), None, 812.0, None, [], ()))
    line = next(r.getMessage() for r in caplog.records if "cut rung finished" in r.getMessage())
    assert "out=1100 winner=v4_source winner_out=1000 winner_vs_this_pct=-9.091" in line


# --- the executor: a refused source winner re-asks today's rungs ---------------------------------

def test_a_source_winner_refused_on_price_rechecks_without_the_source_rungs():
    """Alpha pin (a), executor half: the source pool quoted +100% over the source price while
    today's rungs were cut. The second ladder runs without the source rungs and fills."""
    from test_memecoin_evm_hookers_v4_key import KYBER_STALE, V4_FRESH, _entry
    source_quote = dict(KYBER_STALE, _sniper_winner="v4_source", _sniper_rungs=[])
    result, asked, signed, _c = _entry([source_quote, V4_FRESH])
    assert (result["status"], asked, signed) == ("filled", 2, 1)


def test_a_recheck_that_answers_nothing_keeps_the_source_refusal():
    from test_memecoin_evm_hookers_v4_key import KYBER_STALE, _entry
    source_quote = dict(KYBER_STALE, _sniper_winner="v4_source", _sniper_rungs=[])
    result, asked, signed, _c = _entry([source_quote, None])
    assert (result["status"], asked, signed) == ("skipped_chase", 2, 0)


def test_the_recheck_passes_the_exclusion_to_the_ladder(monkeypatch):
    import test_memecoin_evm_hookers_v4_key as H
    from sniper.execution import evm_swap_executor as X
    seen = []
    real = H.execute_entry_evm

    def spy(*a, **kw):
        inner = kw["quote_fn"]

        def quote_fn(mint, usd, **k):
            seen.append(k.get("exclude_rungs"))
            return inner(mint, usd, **k)
        return real(*a, **dict(kw, quote_fn=quote_fn))
    monkeypatch.setattr(H, "execute_entry_evm", spy)
    source_quote = dict(H.KYBER_STALE, _sniper_winner="v4_source", _sniper_rungs=[])
    H._entry([source_quote, H.V4_FRESH])
    # the third ask is the post-approval requote: the refused source pool must not come back
    excl = tuple(sorted(src.SOURCE_RUNGS))       # every source-route rung, the pinned one included
    assert seen == [None, excl, excl]
    seen.clear()
    H._entry([H.KYBER_STALE, H.V4_FRESH])                 # an aggregator recheck excludes nothing
    assert seen[:2] == [None, None]
    assert X.source_route_won({"_sniper_winner": "v4_source"})
    assert not X.source_route_won({"_sniper_winner": "v4"})
    assert {"v4_source", "v4_multihop_source"} <= X._ONCHAIN_RUNGS


def test_a_refused_source_route_pins_its_family_for_the_dip_watch():
    from sniper.execution.evm_swap_executor import _dip_route
    from sniper.services.memecoin_real_runner import EXPIRY_PROVEN_RUNGS
    base = {"sellAmount": "10", "buyAmount": "20", "route": {"pool_id": POOL}}
    assert _dip_route(dict(base, _sniper_winner="v4_source"))["rung"] == "v4"
    assert _dip_route(dict(base, _sniper_winner="v4_multihop_source"))["rung"] == "v4_multihop"
    assert _dip_route(dict(base, _sniper_winner="kyber"))["rung"] == "kyber"
    assert EXPIRY_PROVEN_RUNGS["v4_source"] == EXPIRY_PROVEN_RUNGS["v4_multihop_source"] == "v4"


# --- Gamma follow-ups on 147a99a2b (LOW, test-only) and L4 -------------------------------------

def test_a_hinted_multihop_over_budget_is_a_miss(monkeypatch):
    """L1 / M11: the source multihop rung applies the multihop budget itself."""
    def multihop(chain, sell, buy, amount, taker, **kw):
        if kw.get("coin_pool_hint"):
            q = _quote(1500, X_POOL, protocol="uniswap_v4_multihop")
            q["route"]["round_trip_pct"] = -40.0
            return q
        time.sleep(SLOW_S)
        q = _quote(1000, "0xslowpath", protocol="uniswap_v4_multihop")
        q["route"]["round_trip_pct"] = -3.0
        return q
    rungs = []
    out, took, _p = _ladder(monkeypatch, key=X_KEY, hint=X_POOL, multihop=multihop, rungs=rungs)
    assert _rec(rungs, "v4_multihop_source")["reason"] == "round_trip_over_budget:-40.0"
    assert out["_sniper_winner"] == "v4_multihop" and took >= SLOW_S


def test_a_recheck_seam_without_the_keyword_keeps_the_refusal(monkeypatch):
    """L2 / M3: a quote seam that cannot exclude the source rungs answers nothing -- never a
    fall-open to an unfiltered ladder that would bring the refused source pool back."""
    import test_memecoin_evm_hookers_v4_key as H
    seen = []
    real = H.execute_entry_evm

    def spy(*a, **kw):
        inner = kw["quote_fn"]

        def quote_fn(mint, usd, **k):
            if "exclude_rungs" in k:
                raise TypeError("quote_fn() got an unexpected keyword argument 'exclude_rungs'")
            seen.append(k)
            return inner(mint, usd, **k)
        return real(*a, **dict(kw, quote_fn=quote_fn))
    monkeypatch.setattr(H, "execute_entry_evm", spy)
    source_quote = dict(H.KYBER_STALE, _sniper_winner="v4_source", _sniper_rungs=[])
    result, asked, signed, _c = H._entry([source_quote, H.V4_FRESH])
    assert (result["status"], signed, len(seen)) == ("skipped_chase", 0, 1)


def test_a_hinted_multihop_search_never_asks_a_provider_or_the_store(monkeypatch):
    """L2 / M15."""
    from sniper.execution import evm_v4_multihop as MH
    from sniper.execution import evm_v4_quote as V
    called = []
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda *a, **k: called.append("provider") or ([], False))
    monkeypatch.setattr(V, "_stored_pools_for_mint", lambda *a, **k: called.append("store") or [])
    seen_pools = []

    def bridges(chain, cash, mint, rpc_url, configured, *, pools=None, **kw):
        seen_pools.append(pools)
        return []
    monkeypatch.setattr(MH, "_candidate_bridges", bridges)
    out = MH.multihop_quote(CHAIN, WETH, COIN, IN, rpc_url="https://rpc", clip_usd=100.0,
                            coin_pool_hint=X_POOL)
    assert out is None and called == [] and seen_pools == [[(X_POOL, None)]]


def test_a_sell_ladder_never_runs_the_source_rungs(monkeypatch):
    """L2: the hint is for buys only."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", False, raising=False)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(src, "local_key", lambda *a: pytest.fail("a sell resolved a source key"))
    rungs = []
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: None, acct=object(), rpc_url="https://rpc",
        on_rung=rungs.append)
    fn(COIN, WETH, IN, slippage_bps=100, route_hint=POOL)
    assert not any(r["rung"].endswith("_source") for r in rungs)


def test_the_capacity_probe_ladder_carries_the_hint():
    """L4: a probe quote the entry later reuses ran with the source pool."""
    from sniper.services import memecoin_real_runner as R
    seen = []

    def probe(mint, usd, **kw):
        seen.append(kw.get("route_hint"))
        return None
    venue = {"entry_quote_probe_fn": probe}
    assert R.reduce_clip_for_route_capacity("4663", COIN, 100.0, venue, route_hint=POOL) == (100.0, None)
    assert R.reduce_clip_for_route_capacity("4663", COIN, 100.0, venue) == (100.0, None)
    assert seen == [POOL, None]


# --- split receipt hints: discovery only, never one fabricated source price --------------------

@pytest.mark.parametrize("container", [list, tuple])
def test_split_candidates_are_normalized_for_entry_and_safety_discovery(monkeypatch, container):
    monkeypatch.setenv("MEMECOIN_EVM_SAFETY_EXIT_HINT_CHAINS", CHAIN)
    pools = container([X_POOL.upper().replace("0X", "0x"), POOL])
    ref = _Ref({"reason": "wallet_net_post_units_unproven", "hint_pools": pools})
    assert src.attempt_hint(CHAIN, ref, COIN) == (X_POOL, POOL)
    assert src.hint_from_reference(ref) == (X_POOL, POOL)
    assert src.safety_exit_hint(CHAIN, ref) == (X_POOL, POOL)


@pytest.mark.parametrize("pools", [
    None, POOL, {"pool": POOL}, [], [POOL], [POOL, "0x1234"], [POOL, None],
    [POOL, POOL], [POOL, POOL.upper().replace("0X", "0x")],
    ["0x%064x" % i for i in range(9)],
])
def test_malformed_split_candidates_never_supply_a_hint(pools):
    ref = _Ref({"hint_pools": pools})
    assert src.attempt_hint(CHAIN, ref, COIN) is None
    assert src.hint_from_reference(ref) is None


@pytest.mark.parametrize("field", ["pool", "hint_pool", "entry_hint_pool"])
def test_a_bound_scalar_hint_precedes_split_entry_candidates(field):
    post = {field: POOL, "hint_pools": [X_POOL, "0x" + "33" * 32]}
    if field == "pool":
        post["venue"] = "v4"
    assert src.attempt_hint(CHAIN, _Ref(post), COIN) == POOL


@pytest.mark.parametrize("cash", [NATIVE, WETH])
def test_split_unique_cash_pool_wins_and_refreshes_only_its_full_size_quote(monkeypatch, cash):
    caller, lookups, reverse = threading.get_ident(), [], []
    key_reader = src.local_key
    cash_key = dict(KEY, currency0=cash)
    cash_pool = pool_id_for_key(cash_key).lower()

    def local_key(chain, pool, mint):
        assert threading.get_ident() != caller, "source keys must resolve on the rung worker"
        lookups.append(pool)
        return key_reader(chain, pool, mint)

    def same_pool(chain, quote, mint, amount, rpc_url, http=None):
        reverse.append((quote["route"]["pool_id"], mint, amount))
        return IN * 97 // 100

    monkeypatch.setattr(src, "local_key", local_key)
    rungs = []
    out, took, pinned = _ladder(
        monkeypatch, hint=(X_POOL, cash_pool), keys={X_POOL: X_KEY, cash_pool: cash_key},
        slow_v4=900, rungs=rungs, reverse_fn=same_pool)
    assert out["_sniper_winner"] == "v4_source" and out["route"]["pool_id"] == cash_pool
    assert took < SLOW_S and pinned == [cash_pool]
    assert lookups == [X_POOL, cash_pool]
    assert reverse == [(cash_pool, COIN, 1000)]
    assert out["sellAmount"] == str(IN)
    assert src.remembered(CHAIN, COIN) == cash_pool
    assert out["_requote"]()["route"]["pool_id"] == cash_pool
    assert lookups == [X_POOL, cash_pool, cash_pool]
    assert pinned == [cash_pool, cash_pool]


@pytest.mark.parametrize("case", ["unknown", "wrong_hash", "wrong_coin", "no_cash", "two_cash"])
def test_split_unknown_or_ambiguous_cash_candidates_keep_broad_discovery(monkeypatch, case):
    other = dict(KEY, fee=3000)
    other_pool = pool_id_for_key(other).lower()
    keys = {X_POOL: X_KEY, POOL: KEY}
    if case == "unknown":
        keys.pop(X_POOL)
    elif case == "wrong_hash":
        keys[X_POOL] = KEY
    elif case == "wrong_coin":
        other = dict(KEY, currency1="0x" + "44" * 20)
        other_pool = pool_id_for_key(other).lower()
        keys = {other_pool: other, POOL: KEY}
    elif case == "no_cash":
        other = dict(X_KEY, fee=10000)
        other_pool = pool_id_for_key(other).lower()
        keys = {X_POOL: X_KEY, other_pool: other}
    else:
        keys = {other_pool: other, POOL: KEY}
    hints = tuple(keys) if case in {"wrong_coin", "no_cash", "two_cash"} else (X_POOL, POOL)
    out, _took, pinned = _ladder(monkeypatch, hint=hints, keys=keys, slow_v4=900)
    assert out["_sniper_winner"] == "v4" and pinned == []


@pytest.mark.parametrize("failure", ["blind", "no_quote", "over_budget"])
def test_split_candidate_failure_keeps_the_broad_rungs(monkeypatch, failure):
    def reverse(chain, quote, mint, amount, rpc_url, http=None):
        return IN * (60 if quote["route"]["pool_id"] == POOL else 97) // 100

    pinned = (v4q.PinnedQuoteBlind("rpc_rate_limit") if failure == "blind" else
              False if failure == "no_quote" else None)
    out, _took, _pinned = _ladder(
        monkeypatch, hint=(X_POOL, POOL), keys={X_POOL: X_KEY, POOL: KEY},
        pinned=pinned, reverse_fn=reverse, slow_v4=900)
    assert out["_sniper_winner"] == "v4"


def test_split_candidates_never_start_hinted_multihop_or_prefix_discovery(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", CHAIN)
    asked = []

    def multihop(*_a, **kw):
        asked.append(kw.get("coin_pool_hint"))
        return None

    rungs = []
    out, _took, _pinned = _ladder(
        monkeypatch, hint=(X_POOL, POOL), keys={X_POOL: X_KEY, POOL: KEY},
        slow_v4=900, multihop=multihop, rungs=rungs, grace="0.01")
    assert out["_sniper_winner"] == "v4_source"
    assert not {r["rung"] for r in rungs} & {"v4_multihop_source", "v4_multihop_pinned"}
    assert all(hint is None for hint in asked)


def test_source_winner_refresh_skips_unused_prefix_key_reads_on_the_caller(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", CHAIN)
    caller, read_key = threading.get_ident(), src.local_key

    def worker_key(*args):
        assert threading.get_ident() != caller, "unused prefix work reached the caller"
        return read_key(*args)

    monkeypatch.setattr(src, "local_key", worker_key)
    out, _took, pinned = _ladder(
        monkeypatch, hint=(X_POOL, POOL), keys={X_POOL: X_KEY, POOL: KEY},
        slow_v4=900, multihop=lambda *_a, **_kw: None)
    assert out["_sniper_winner"] == "v4_source"
    refreshed = out["_requote"]()
    assert refreshed["route"]["pool_id"] == POOL and pinned == [POOL, POOL]
