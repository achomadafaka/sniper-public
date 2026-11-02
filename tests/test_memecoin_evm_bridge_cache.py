"""The source-hinted multihop rung tries the last winning cash->bridge pool first (2026-10-15,
operator "fill fastest"; Alpha GO on (i), MEMECOIN_EVM_BRIDGE_CACHE_CHAINS).

With the source's own coin pool known, the hinted multihop still spent ~1.1 s enumerating
cash->bridge pools and measuring every path. The bridge currency follows from the coin pool's key,
and the cash->bridge pool that last passed the budget for that currency is cached for 600 s. One
pinned path is measured first; anything it cannot do falls through to today's search.

Pins (Alpha): the pinned path passes the same depth floor and round-trip budget; a non-blind miss
drops the entry and a blind one keeps it; a cached bridge whose key does not join cash and that
bridge is a miss; the knob off is today's rung."""
import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_source_route as src
from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as v4q
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN = "4663"
NATIVE = "0x" + "0" * 40
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
COIN = "0x" + "22" * 20
BRIDGE = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
OTHER = "0x" + "44" * 20
TAKER = "0x" + "11" * 20
IN = 10 ** 16
COIN_KEY = {"currency0": COIN, "currency1": BRIDGE, "fee": 3000, "tick_spacing": 60,
            "hooks": NATIVE}
COIN_POOL = pool_id_for_key(COIN_KEY).lower()
B_KEY = {"currency0": NATIVE, "currency1": BRIDGE, "fee": 500, "tick_spacing": 10, "hooks": NATIVE}
B_POOL = pool_id_for_key(B_KEY).lower()
B2_KEY = dict(B_KEY, fee=3000, tick_spacing=60)
B2_POOL = pool_id_for_key(B2_KEY).lower()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_bridges", {})
    monkeypatch.setattr(v4q, "_key_cache", {})
    yield


# --- the cache ---------------------------------------------------------------------------------

def _route(pid=B_POOL, key=B_KEY, legs=2, depth=None):
    path = [{"currency": BRIDGE, "key": key}, {"currency": COIN, "key": COIN_KEY}]
    if legs == 3:
        path.insert(0, {"currency": OTHER, "key": key})
    return {"legs_path": path, "bridge_pool_id": pid, "bridge_depth_usd": depth}


def test_a_winning_two_leg_route_is_cached_by_bridge_currency_and_expires():
    src.remember_bridge(CHAIN, _route(depth=1e6), now=100.0)
    assert src.bridge_for(CHAIN, BRIDGE.upper().replace("0X", "0x"), now=101.0) == (
        B_POOL, B_KEY, 1e6)
    assert src.bridge_for(CHAIN, BRIDGE, now=100.0 + src.CACHE_TTL_S) is None
    src.remember_bridge(CHAIN, _route(), now=200.0)                     # no USD depth: still cached
    assert src.bridge_for(CHAIN, BRIDGE, now=201.0) == (B_POOL, B_KEY, None)
    src.forget_bridge(CHAIN, BRIDGE)
    assert src.bridge_for(CHAIN, BRIDGE, now=201.0) is None


@pytest.mark.parametrize("route", [
    _route(legs=3),                       # a three-leg route has no single cash->bridge pool
    _route(pid=B2_POOL),                  # the key does not hash to the pool id
    {"legs_path": None},
    {},
])
def test_only_a_verified_two_leg_route_is_cached(route):
    src.remember_bridge(CHAIN, route)
    assert src._bridges == {}


def test_the_knob_is_per_chain_and_malformed_means_off(monkeypatch):
    assert src.bridge_cache_enabled(CHAIN)
    for raw in ("", "56", "4663,x"):
        monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", raw)
        assert not src.bridge_cache_enabled(CHAIN)


# --- the pinned search -------------------------------------------------------------------------

@pytest.fixture
def search(monkeypatch):
    """multihop_quote on the source pool with the chain reads faked. `quotes[(bridge pool)]` is the
    reverse cash a path through that cash->bridge pool returns (None = no quote)."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", True, raising=False)
    v4q._key_cache[(CHAIN, COIN_POOL)] = COIN_KEY
    monkeypatch.setattr(MH, "_pool_key_for", lambda c, pid, *a, **k: v4q._key_cache.get((c, pid)))
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "0x10")
    calls = {"enumerated": 0, "paths": []}
    quotes = {B_POOL: int(IN * 0.97), B2_POOL: int(IN * 0.98)}

    def bridge_options(chain, cash, bridge, rpc_url, *, limit, budget=None, providers=True, **kw):
        calls["enumerated"] += 1
        return [(B_POOL, B_KEY, None), (B2_POOL, B2_KEY, None)]
    monkeypatch.setattr(MH, "_bridge_options", bridge_options)

    def quote_path(chain, currency_in, path, amount_in, rpc_url, block="latest", **kw):
        keys = [key for _c, key in path]
        if currency_in == COIN:                           # the reverse quote
            pid = pool_id_for_key(keys[-1]).lower()
            calls["paths"].append(pid)
            return quotes.get(pid)
        return 5000
    monkeypatch.setattr(MH, "_quote_path", quote_path)

    def run(hint=(BRIDGE, B_POOL, B_KEY, None), accept=None):
        kw = {"bridge_hint": hint} if hint is not None else {}
        if accept is not None:
            kw["pinned_accept_fn"] = accept
        return MH.multihop_quote(CHAIN, NATIVE, COIN, IN, rpc_url="https://rpc", clip_usd=100.0,
                                 coin_pool_hint=COIN_POOL, **kw)
    return run, calls, quotes


def test_a_cached_bridge_measures_one_pinned_path_and_skips_enumeration(search):
    run, calls, _q = search
    route = run()
    assert route["discovery"] == "pinned" and route["bridge_pool_id"] == B_POOL
    assert route["paths_considered"] == 1 and calls["enumerated"] == 0
    assert calls["paths"] == [B_POOL] and MH.last_pinned_refusal() is None


def test_without_a_cached_bridge_the_search_is_todays(search):
    run, calls, _q = search
    route = run(hint=None)
    # the best measured round trip of the enumerated pools wins, exactly as before
    assert route["discovery"] == "source" and route["bridge_pool_id"] == B2_POOL
    assert calls["enumerated"] == 1 and route["pinned_refusal"] is None


@pytest.mark.parametrize("hint, why", [
    ((BRIDGE, B_POOL, dict(B_KEY, currency1=OTHER), None), "bridge_pool_unusable"),   # wrong X
    ((BRIDGE, B_POOL, dict(B_KEY, fee=MH.DYNAMIC_FEE_FLAG), None), "bridge_pool_unusable"),
])
def test_a_cached_key_that_fails_the_bridge_screen_is_a_miss(search, hint, why):
    run, calls, _q = search
    route = run(hint=hint)
    assert route["discovery"] == "source" and calls["enumerated"] == 1
    assert route["pinned_refusal"].startswith(why) and MH.last_pinned_refusal() == route[
        "pinned_refusal"]


def test_a_pinned_path_that_does_not_quote_falls_through(search):
    run, calls, quotes = search
    quotes[B_POOL] = None
    route = run()
    assert route["discovery"] == "source" and route["bridge_pool_id"] == B2_POOL
    assert route["pinned_refusal"] == "reverse_path_no_quote"


def test_a_pinned_path_over_budget_falls_through_to_the_search(search):
    run, calls, quotes = search
    quotes[B_POOL] = int(IN * 0.70)
    route = run(accept=lambda pct: pct > -20.0)
    assert route["discovery"] == "source" and route["bridge_pool_id"] == B2_POOL
    assert route["pinned_refusal"] == "pinned_over_budget:-30.00"


def test_the_pinned_coin_leg_keeps_its_depth_floor(search, monkeypatch):
    from sniper.settings import memecoin as cfg
    run, calls, _q = search
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", False, raising=False)
    assert run() is None and calls["paths"] == []
    assert MH.last_pinned_refusal().startswith("coin_leg_missing_or_under_floor")


def test_a_stale_pinned_note_never_survives_into_the_next_call(search, monkeypatch):
    run, _c, quotes = search
    quotes[B_POOL] = None
    run()
    assert MH.last_pinned_refusal() == "reverse_path_no_quote"
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    assert MH.dex_v4_multihop_quote(CHAIN, WETH, COIN, IN, TAKER) is None
    assert MH.last_pinned_refusal() is None


def test_the_rung_entry_carries_the_bridge_fields_and_the_hint(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    seen = []

    def multihop(*a, **kw):
        seen.append(kw)
        return {"amount_out": 5000, "bridge_pool_id": B_POOL, "bridge_depth_usd": None,
                "discovery": "pinned", "pinned_refusal": None}
    monkeypatch.setattr(MH, "multihop_quote", multihop)
    monkeypatch.setattr("sniper.execution.evm_v4_multihop_tx.as_zerox_quote",
                        lambda *a, **k: {"route": {}})
    accept = lambda pct: True                                          # noqa: E731
    out = MH.dex_v4_multihop_quote(CHAIN, WETH, COIN, IN, TAKER, slippage_bps=100,
                                   coin_pool_hint=COIN_POOL,
                                   bridge_hint=(BRIDGE, B_POOL, B_KEY, None),
                                   pinned_accept_fn=accept)
    assert out["route"]["bridge_pool_id"] == B_POOL and out["route"]["discovery"] == "pinned"
    assert seen[-1]["bridge_hint"] == (BRIDGE, B_POOL, B_KEY, None)
    assert seen[-1]["pinned_accept_fn"] is accept
    MH.dex_v4_multihop_quote(CHAIN, WETH, COIN, IN, TAKER, slippage_bps=100,
                             coin_pool_hint=COIN_POOL)
    assert "bridge_hint" not in seen[-1] and "pinned_accept_fn" not in seen[-1]


# --- the rung ----------------------------------------------------------------------------------

def _rung(monkeypatch, multihop, *, knob=CHAIN, rungs=None):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", knob)
    monkeypatch.setattr(src, "_pools", {})
    for name, value in (("MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True),
                        ("MEMECOIN_EVM_V2_ENTRY_ENABLED", True), ("MEMECOIN_EVM_V4_ENABLED", True),
                        ("MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True),
                        ("MEMECOIN_EVM_RELAY_ENABLED", False),
                        ("MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False),
                        ("MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)):
        monkeypatch.setattr(cfg, name, value, raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_4663", "1.0")
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(src, "local_key", lambda c, p, m: dict(COIN_KEY))
    monkeypatch.setattr(MH, "dex_v4_multihop_quote", multihop)
    rungs = [] if rungs is None else rungs
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: None, acct=object(), rpc_url="https://rpc",
        on_rung=rungs.append)
    out = fn(WETH, COIN, IN, slippage_bps=100, route_hint=COIN_POOL)
    return out, next(r for r in rungs if r["rung"] == "v4_multihop_source")


def _hit(discovery="pinned", pct=-3.0, pid=B_POOL):
    route = dict(_route(pid=pid), protocol="uniswap_v4_multihop", pool_id=COIN_POOL,
                 round_trip_pct=pct, discovery=discovery)
    return {"sellAmount": str(IN), "buyAmount": "5000", "route": route,
            "transaction": {"to": "0xrouter", "data": "0x"}, "issues": {"allowance": None}}


def test_a_cached_bridge_is_passed_to_the_hinted_rung_with_the_same_budget(monkeypatch):
    src.remember_bridge(CHAIN, _route())
    seen = []

    def multihop(*a, **kw):
        if kw.get("coin_pool_hint"):
            seen.append(kw)
            return _hit()
        return None
    out, rec = _rung(monkeypatch, multihop)
    assert out["_sniper_winner"] == "v4_multihop_source" and rec["reason"] == "discovery:pinned"
    assert seen[0]["bridge_hint"] == (BRIDGE, B_POOL, B_KEY, None)
    accept = seen[0]["pinned_accept_fn"]
    assert accept(-3.0) and not accept(-40.0)        # the multihop budget at the 25% TP


def test_the_knob_off_is_todays_rung_call_and_writes_nothing(monkeypatch):
    src.remember_bridge(CHAIN, _route())     # unexpired, so a read would hint; a write moves expiry
    cached = dict(src._bridges)
    seen = []

    def multihop(*a, **kw):
        seen.append(kw)
        return _hit(discovery="source") if kw.get("coin_pool_hint") else None
    out, rec = _rung(monkeypatch, multihop, knob="")
    hinted = [kw for kw in seen if kw.get("coin_pool_hint")]
    assert out["_sniper_winner"] == "v4_multihop_source" and rec["reason"] == "discovery:source"
    assert "bridge_hint" not in hinted[0] and "pinned_accept_fn" not in hinted[0]
    assert src._bridges == cached                     # neither read nor written


def test_a_hinted_win_within_budget_fills_the_cache(monkeypatch):
    def multihop(*a, **kw):
        return _hit(discovery="source") if kw.get("coin_pool_hint") else None
    _rung(monkeypatch, multihop)
    assert src.bridge_for(CHAIN, BRIDGE)[0] == B_POOL


def test_a_hinted_route_over_budget_is_not_cached(monkeypatch):
    def multihop(*a, **kw):
        return _hit(discovery="source", pct=-40.0) if kw.get("coin_pool_hint") else None
    _out, rec = _rung(monkeypatch, multihop)
    assert rec["reason"] == "round_trip_over_budget:-40.0" and src._bridges == {}


def test_todays_multihop_win_within_budget_fills_the_cache(monkeypatch):
    def multihop(*a, **kw):
        return None if kw.get("coin_pool_hint") else _hit(discovery="provider")
    out, _rec = _rung(monkeypatch, multihop)
    assert out["_sniper_winner"] == "v4_multihop" and src.bridge_for(CHAIN, BRIDGE)[0] == B_POOL


@pytest.mark.parametrize("note, kept", [
    ("reverse_path_no_quote", False),
    ("bridge_pool_unusable:0x5fc5360d", False),
    ("pinned_over_budget:-30.00", False),
    ("search_stopped:deadline", True),
    ("decision_block_unavailable", True),
])
def test_an_answered_pinned_miss_drops_the_bridge_and_a_blind_one_keeps_it(monkeypatch, note,
                                                                          kept):
    src.remember_bridge(CHAIN, _route())

    def multihop(*a, **kw):
        if kw.get("coin_pool_hint"):
            MH._refusal.pinned = note
            return None
        return None
    _rung(monkeypatch, multihop)
    assert (src.bridge_for(CHAIN, BRIDGE) is not None) is kept
