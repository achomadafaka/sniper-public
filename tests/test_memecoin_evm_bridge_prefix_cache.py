"""The full-prefix bridge cache and its own pinned rung (2026-10-15, operator "fill fastest"; Alpha GO,
MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS).

The 2-leg cache (cash -> X) had 0 pinned hits on the first live 4663 samples: coin 0xbb2d0f03 routes
ETH -> USDG -> 0x2e8c -> coin (MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS=3), and no ETH/0x2e8c pool exists.
The cache now holds the whole route from cash to X (1 or 2 legs), and a v4_multihop_pinned rung
measures it plus the coin leg as ONE path, beside today's discovery rung.

Pins (Alpha): a hit re-quotes the whole path at this call's amount under the same budget and screens,
and a budget fail is a miss (never a worse fill); the last budget-passing write wins; the pin is its
own rung, so a miss costs the discovery nothing; one lock, one TTL, no new module state; knob off is
today's 2-leg cache exactly, and a 3-leg entry left behind is ignored; every consumer of the
source-rung names derives from evm_source_route.SOURCE_RUNGS."""
import threading
import time

import pytest

from sniper.execution import evm_real_seams as S
from sniper.execution import evm_source_route as src
from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as v4q
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN = "4663"
NATIVE = "0x" + "0" * 40
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
X = "0x2e8c31162b855a2ffa90f6f8634643ad6f111e18"
COIN = "0xbb2d0f034d176dcc0b2cce8b52e2847dd8551e18"
TAKER = "0x" + "11" * 20
IN = 10 ** 16
DYN = MH.DYNAMIC_FEE_FLAG
H1, H2 = "0x08e52564bad99e05a694b4809f397edca417a080", "0x4e3468951d49f2eea976ed0d6e75ffcb44a9a544"
# The live route of position 73889 (2026-10-15 16:41:46Z).
M_KEY = {"currency0": NATIVE, "currency1": USDG, "fee": 100, "tick_spacing": 1, "hooks": NATIVE}
M_POOL = pool_id_for_key(M_KEY).lower()
MX_KEY = {"currency0": X, "currency1": USDG, "fee": DYN, "tick_spacing": 60, "hooks": H1}
COIN_KEY = {"currency0": X, "currency1": COIN, "fee": DYN, "tick_spacing": 8, "hooks": H2}
COIN_POOL = pool_id_for_key(COIN_KEY).lower()
PREFIX = ((USDG, M_KEY), (X, MX_KEY))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_bridges", {})
    monkeypatch.setattr(v4q, "_key_cache", {})


def _route(legs=3, pid=M_POOL):
    path = [{"currency": USDG, "key": M_KEY}, {"currency": X, "key": MX_KEY},
            {"currency": COIN, "key": COIN_KEY}]
    return {"legs_path": path if legs == 3 else [path[0], {"currency": COIN, "key": COIN_KEY}],
            "bridge_pool_id": pid, "bridge_depth_usd": None}


# --- the cache -----------------------------------------------------------------------------------

def test_a_three_leg_route_is_cached_under_the_coins_pair_currency(monkeypatch):
    src.remember_bridge(CHAIN, _route(), now=100.0)
    assert src.prefix_for(CHAIN, X, now=101.0) == (PREFIX, M_POOL, None)
    assert src.bridge_for(CHAIN, X, now=101.0) is None       # not a cash->X pool
    assert src.prefix_for(CHAIN, X, now=100.0 + src.CACHE_TTL_S) is None
    src.forget_bridge(CHAIN, X)
    assert src.prefix_for(CHAIN, X, now=101.0) is None


def test_knob_off_never_writes_three_legs_and_ignores_one_left_behind(monkeypatch):
    src.remember_bridge(CHAIN, _route(), now=100.0)           # written while the knob was on
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", "")
    assert src.prefix_for(CHAIN, X, now=101.0) is None and src.bridge_for(CHAIN, X, now=101.0) is None
    monkeypatch.setattr(src, "_bridges", {})
    src.remember_bridge(CHAIN, _route())
    assert src._bridges == {}


def test_the_last_budget_passing_write_wins(monkeypatch):
    two = {"legs_path": [{"currency": X, "key": dict(MX_KEY, currency1=NATIVE, fee=500, hooks=NATIVE)},
                         {"currency": COIN, "key": COIN_KEY}]}
    two["bridge_pool_id"] = pool_id_for_key(two["legs_path"][0]["key"]).lower()
    src.remember_bridge(CHAIN, two)
    assert len(src.prefix_for(CHAIN, X)[0]) == 1
    src.remember_bridge(CHAIN, _route())
    assert src.prefix_for(CHAIN, X)[0] == PREFIX


def test_the_cache_lock_is_the_existing_one():
    """No new module state: the prefix lives in _bridges under _lock."""
    assert isinstance(src._lock, type(threading.Lock()))
    src.remember_bridge(CHAIN, _route())
    assert list(src._bridges) == [(CHAIN, X)]


# --- the pinned search -----------------------------------------------------------------------------

@pytest.fixture
def search(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", True, raising=False)
    v4q._key_cache[(CHAIN, COIN_POOL)] = COIN_KEY
    monkeypatch.setattr(MH, "_pool_key_for", lambda c, pid, *a, **k: v4q._key_cache.get((c, pid)))
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "0x10")
    calls = {"enumerated": 0, "middle": 0, "forward_amounts": [], "paths": []}
    reverse = {"cash": int(IN * 0.97)}

    def bridge_options(*a, **k):
        calls["enumerated"] += 1
        return []
    monkeypatch.setattr(MH, "_bridge_options", bridge_options)

    def middle_options(*a, **k):
        calls["middle"] += 1
        return []
    monkeypatch.setattr(MH, "_middle_leg_options", middle_options)

    def quote_path(chain, currency_in, path, amount_in, rpc_url, block="latest", **kw):
        if currency_in == COIN:
            return reverse["cash"]
        calls["forward_amounts"].append(amount_in)
        calls["paths"].append(tuple(cur for cur, _k in path))
        return 5000
    monkeypatch.setattr(MH, "_quote_path", quote_path)

    def run(prefix=PREFIX, accept=None, amount=IN, only=True):
        return MH.multihop_quote(CHAIN, NATIVE, COIN, amount, rpc_url="https://rpc", clip_usd=100.0,
                                 coin_pool_hint=COIN_POOL, prefix_hint=(X, prefix, M_POOL, None),
                                 pinned_accept_fn=accept, pinned_only=only)
    return run, calls, reverse


def test_a_cached_prefix_is_one_measured_path_with_no_discovery(search):
    run, calls, _r = search
    route = run(amount=7 * IN)
    assert route["discovery"] == "pinned" and route["paths_considered"] == 1
    assert [leg["currency"] for leg in route["legs_path"]] == [USDG, X, COIN]
    assert calls["enumerated"] == calls["middle"] == 0
    assert calls["forward_amounts"] == [7 * IN]               # this call's amount, not the writer's


def test_a_three_leg_prefix_is_never_reused_without_the_pinned_budget(search):
    """Alpha's check: a pinned path over budget is a miss, whatever the cache says."""
    run, _calls, reverse = search
    reverse["cash"] = int(IN * 0.60)
    assert run(accept=lambda pct: pct > -20.0) is None
    assert MH.last_multihop_refusal() == "pinned_over_budget:-40.00"
    assert MH.last_pinned_refusal() == "pinned_over_budget:-40.00"


@pytest.mark.parametrize("prefix, why", [
    (((USDG, dict(M_KEY, fee=DYN)), (X, MX_KEY)), "bridge_pool_unusable"),     # leg 0 hook-priced
    (((USDG, M_KEY), (X, dict(MX_KEY, currency0=COIN))), "bridge_pool_unusable"),   # leg 1 wrong pair
    (((X, MX_KEY),), "bridge_pool_unusable"),                                  # leg 0 not from cash
    (((USDG, M_KEY), (COIN, MX_KEY)), "prefix_shape"),                          # does not end at X
])
def test_the_cached_legs_pass_the_search_screens_again(search, prefix, why):
    run, calls, _r = search
    assert run(prefix=prefix) is None and calls["paths"] == []
    assert MH.last_multihop_refusal().startswith(why)


def test_the_coin_leg_keeps_its_depth_floor(search, monkeypatch):
    from sniper.settings import memecoin as cfg
    run, calls, _r = search
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", False, raising=False)
    assert run() is None and calls["paths"] == []
    assert MH.last_multihop_refusal().startswith("coin_leg_missing_or_under_floor")


def test_a_lowered_leg_cap_refuses_a_longer_prefix(search, monkeypatch):
    from sniper.settings import memecoin as cfg
    run, calls, _r = search
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2, raising=False)
    assert run() is None and calls["paths"] == []
    assert MH.last_multihop_refusal() == "prefix_over_max_legs:2"


def test_pinned_only_never_falls_through_to_discovery(search):
    run, calls, reverse = search
    reverse["cash"] = None                                     # the reverse does not quote
    assert run() is None and calls["enumerated"] == 0 and calls["middle"] == 0


# --- the rung --------------------------------------------------------------------------------------

def _ladder(monkeypatch, multihop, *, rungs):
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_ROUTE_HINT_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_pools", {})
    for name, value in (("MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True),
                        ("MEMECOIN_EVM_V2_ENTRY_ENABLED", True), ("MEMECOIN_EVM_V4_ENABLED", True),
                        ("MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True),
                        ("MEMECOIN_EVM_RELAY_ENABLED", False),
                        ("MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False),
                        ("MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)):
        monkeypatch.setattr(cfg, name, value, raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_LADDER_GRACE_AFTER_HIT_S_4663", "0")
    monkeypatch.setattr(S, "_entry_tp_pct", lambda _c: 25.0)
    monkeypatch.setattr(S, "_rung_ladder_chains", lambda _c: {CHAIN})
    monkeypatch.setattr(src, "local_key", lambda c, p, m: dict(COIN_KEY))
    monkeypatch.setattr(MH, "dex_v4_multihop_quote", multihop)
    fn = S.venue_quote_with_v2_fallback(
        lambda *a, **k: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **k: None, v4_quote_fn=lambda *a, **k: None,
        relay_quote_fn=lambda *a, **k: None, acct=object(), rpc_url="https://rpc",
        on_rung=rungs.append)
    started = time.monotonic()
    out = fn(WETH, COIN, IN, slippage_bps=100, route_hint=COIN_POOL)
    return out, time.monotonic() - started


def _hit(pct=-3.0, legs=3):
    route = dict(_route(legs), protocol="uniswap_v4_multihop", pool_id=COIN_POOL,
                 round_trip_pct=pct, discovery="pinned")
    return {"sellAmount": str(IN), "buyAmount": "5000", "route": route,
            "transaction": {"to": "0xrouter", "data": "0x"}, "issues": {"allowance": None}}


SLOW_S = 0.5


def _rec(rungs, name):
    return next((r for r in rungs if r["rung"] == name), None)


def test_a_cached_prefix_runs_its_own_rung_beside_the_discovery_and_wins_first(monkeypatch):
    src.remember_bridge(CHAIN, _route())
    seen, rungs = [], []

    def multihop(*a, **kw):
        seen.append(kw)
        if kw.get("pinned_only"):
            return _hit()
        time.sleep(SLOW_S)                                      # today's discovery
        return None
    out, took = _ladder(monkeypatch, multihop, rungs=rungs)
    assert out["_sniper_winner"] == "v4_multihop_pinned" and took < SLOW_S
    pinned = next(kw for kw in seen if kw.get("pinned_only"))
    assert pinned["prefix_hint"] == (X, PREFIX, M_POOL, None) and pinned["coin_pool_hint"] == COIN_POOL
    assert pinned["pinned_accept_fn"](-3.0) and not pinned["pinned_accept_fn"](-40.0)
    # the discovery rung ran without an in-call pin
    deadline = time.monotonic() + SLOW_S + 1.0
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    discovery = next(kw for kw in seen if not kw.get("pinned_only"))
    assert not {"bridge_hint", "prefix_hint", "pinned_only"} & set(discovery)
    # the attempt row's trace carries the rung, hit and its time (Alpha add 2)
    rec = _rec(rungs, "v4_multihop_pinned")
    assert rec["hit"] and rec["reason"] == "discovery:pinned legs=3" and rec["duration_ms"] >= 0


def test_without_a_cached_prefix_there_is_no_pinned_rung(monkeypatch):
    rungs = []
    _ladder(monkeypatch, lambda *a, **kw: None, rungs=rungs)
    assert _rec(rungs, "v4_multihop_pinned") is None and _rec(rungs, "v4_multihop_source")


def test_knob_off_is_todays_rung_set_and_call(monkeypatch):
    src.remember_bridge(CHAIN, _route())                         # a 3-leg entry left behind
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", "")
    seen, rungs = [], []

    def multihop(*a, **kw):
        seen.append(kw)
        return None
    _ladder(monkeypatch, multihop, rungs=rungs)
    assert _rec(rungs, "v4_multihop_pinned") is None
    assert all("prefix_hint" not in kw and "bridge_hint" not in kw for kw in seen)


@pytest.mark.parametrize("note, kept", [
    ("pinned_over_budget:-40.00", False),
    ("reverse_path_no_quote", False),
    ("search_stopped:deadline", True),
])
def test_an_answered_pinned_miss_drops_the_prefix_and_a_blind_one_keeps_it(monkeypatch, note, kept):
    src.remember_bridge(CHAIN, _route())
    rungs = []

    def multihop(*a, **kw):
        if kw.get("pinned_only"):
            MH._note(note)
        return None
    _ladder(monkeypatch, multihop, rungs=rungs)
    assert _rec(rungs, "v4_multihop_pinned")["reason"] == "pinned:%s" % note
    assert (src.prefix_for(CHAIN, X) is not None) is kept


def test_a_pinned_hit_over_budget_is_a_miss_and_drops_the_prefix(monkeypatch):
    src.remember_bridge(CHAIN, _route())
    rungs = []

    def multihop(*a, **kw):
        return _hit(pct=-40.0) if kw.get("pinned_only") else None
    out, _took = _ladder(monkeypatch, multihop, rungs=rungs)
    assert out is None and _rec(rungs, "v4_multihop_pinned")["reason"] == "round_trip_over_budget:-40.0"
    assert src.prefix_for(CHAIN, X) is None


# --- every consumer derives from SOURCE_RUNGS (Alpha add 1) ---------------------------------------

def test_every_source_rung_consumer_derives_from_source_rungs():
    from sniper.execution import evm_swap_executor as E
    from sniper.services import memecoin_real_runner as R
    names = set(src.SOURCE_RUNGS)
    assert E._SOURCE_RUNGS == frozenset(names)
    assert set(src.SOURCE_FAMILY) == names
    assert names <= set(R.EXPIRY_PROVEN_RUNGS)
    assert names <= set(S.LADDER_ORDER) and len(S.LADDER_ORDER) == len(set(S.LADDER_ORDER))
    assert names <= E._ONCHAIN_RUNGS
    for name in names:
        assert E.source_route_won({"_sniper_winner": name})


def test_a_one_leg_prefix_under_the_knob_pins_in_its_rung_not_in_the_discovery(monkeypatch):
    """Both knobs on and a 2-leg route cached: the pin moves to v4_multihop_pinned, so the discovery
    rung gets no bridge_hint and nothing runs twice."""
    one = {"legs_path": [{"currency": X, "key": dict(MX_KEY, currency1=NATIVE, fee=500, hooks=NATIVE)},
                         {"currency": COIN, "key": COIN_KEY}]}
    one["bridge_pool_id"] = pool_id_for_key(one["legs_path"][0]["key"]).lower()
    src.remember_bridge(CHAIN, one)
    seen, rungs = [], []

    def multihop(*a, **kw):
        seen.append(kw)
        return None
    _ladder(monkeypatch, multihop, rungs=rungs)
    pinned = [kw for kw in seen if kw.get("pinned_only")]
    assert len(pinned) == 1 and len(pinned[0]["prefix_hint"][1]) == 1
    assert all("bridge_hint" not in kw for kw in seen)
