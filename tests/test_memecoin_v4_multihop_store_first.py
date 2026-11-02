"""Store-first v4_multihop discovery on 4663 (2026-09-27, Alpha GO with conditions).

7 of the 8 funded 4663 fills in the 48 h to 2026-09-27 executed on Uniswap V4 through the coin's
pool on a launchpad hook, WETH -> bridge -> coin. The v4_multihop rung that covers it hit once in
1.94 s and was cut blind at ladder_grace five times while Kyber (1.6-2.2 s) and Relay (1.0-3.4 s)
won. The fixture replays the real search offline, store rows and chain reads recorded from a
sandboxed run (tests/fixtures/memecoin/v4_multihop_store_first_4663.json):

1. The bridge-store scan in _bridge_options is skipped: every pool it adds has unknown depth and
   eligible() discards unknown depth, so it cost ~600 ms per bridge and could not add an option.
   The options are proved identical with and without it.
2. When the store already holds the coin's pools, the search runs on the store alone, with no
   GeckoTerminal or DexScreener read. No route (or every quote reverting) falls back to the provider
   path inside the same budget. The route names which path decided, and the ladder trace carries it.
   Flag MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST (default 1; 0 = the provider path as before).
"""
import collections
import json
from pathlib import Path

import pytest

from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as VQ
from sniper.settings import memecoin as cfg

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/v4_multihop_store_first_4663.json")
                .read_text())
LIVE = [(m, e) for m, e in FX["fills"] if not m.startswith("0x941cf3f6")]
DEAD = next(m for m, _e in FX["fills"] if m.startswith("0x941cf3f6"))
AMOUNT = 10 ** 16


def _qkey(currency_in, path, amount_in):
    return json.dumps([str(currency_in).lower(), [[str(c).lower(), k] for c, k in path],
                       str(int(amount_in))], sort_keys=True)


@pytest.fixture
def offline(monkeypatch):
    calls = collections.Counter()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS", 20.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 3, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", True, raising=False)

    def pair(chain, a, b):
        key = "%s|%s" % tuple(sorted((str(a).lower(), str(b).lower())))
        return [(p, k) for p, k in FX["stored_pair_pools"].get(key, [])]

    def quote(chain, currency_in, path, amount_in, rpc_url, block="latest", **kw):
        calls["quote"] += 1
        return FX["quote"].get(_qkey(currency_in, path, amount_in))

    def gt(*a, **k):
        calls["gt"] += 1
        return [], True

    def ds(*a, **k):
        calls["ds"] += 1
        return [], True
    monkeypatch.setattr(VQ, "_stored_pools_for_mint",
                        lambda chain, mint: list(FX["stored_pools_for_mint"].get(str(mint).lower(), [])))
    monkeypatch.setattr(MH, "_stored_pair_pools", pair)
    monkeypatch.setattr(MH, "_pool_key_for",
                        lambda chain, pid, rpc_url, **kw: FX["pool_key"].get(str(pid).lower()))
    monkeypatch.setattr(MH, "_onchain_liquidities", lambda chain, pids, rpc_url, **kw: {
        pid: FX["liquidity"].get(str(pid).lower()) for pid in pids})
    monkeypatch.setattr(MH, "_quote_path", quote)
    monkeypatch.setattr(MH, "_decision_block", lambda chain, rpc_url, **kw: "0x1")
    monkeypatch.setattr(VQ, "_gt_pools_ranked", gt)
    monkeypatch.setattr(VQ, "_ds_pools_ranked", ds)
    # WARM, AS THE RUNNER IS: one liquidity reading per pool is remembered across decisions
    # (_PAIR_PICK_TTL_S), and the recording was one process walking the 7 coins in order.
    import time
    monkeypatch.setattr(MH, "_POOL_LIQUIDITY", {
        ("4663", pid): (time.monotonic(), liq) for pid, liq in FX["liquidity"].items() if liq})
    monkeypatch.setattr(MH, "_BRIDGE_LAST_GOOD", {})
    return calls


def _search(mint):
    return MH.multihop_quote("4663", FX["cash"], mint, AMOUNT, rpc_url="https://rpc",
                             bridge_currencies=(), clip_usd=25.0)


@pytest.mark.parametrize("mint, executed", LIVE, ids=[m[:10] for m, _e in LIVE])
def test_the_store_finds_the_executed_pool_without_a_provider_read(offline, mint, executed):
    route = _search(mint)
    assert route is not None and route["coin_pool_id"].lower() == executed.lower()
    assert route["discovery"] == "store"
    assert offline["gt"] == 0 and offline["ds"] == 0


def test_the_dead_coin_reaches_the_provider_fallback(offline):
    route = _search(DEAD)
    assert route is None
    assert offline["gt"] + offline["ds"] > 0, "every store quote reverted and no provider was asked"


def test_a_coin_the_store_does_not_hold_goes_straight_to_the_providers(offline):
    assert _search("0x" + "ab" * 20) is None
    assert offline["gt"] + offline["ds"] > 0 and offline["quote"] == 0


def test_the_flag_off_is_the_provider_path_as_before(offline, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", False, raising=False)
    mint, executed = LIVE[0]
    route = _search(mint)
    assert offline["gt"] + offline["ds"] > 0
    # Providers dark in the fixture: the provider path falls through to the same store pools.
    assert route is not None and route["coin_pool_id"].lower() == executed.lower()
    assert route["discovery"] == "provider"


@pytest.mark.parametrize("providers_live", [False, True])
def test_the_bridge_scan_changes_no_option(offline, monkeypatch, providers_live):
    """Condition 1: the options with the bridge-store scan equal the options without it."""
    all_pools = sorted(FX["pool_key"])
    bridges = sorted({pair.split("|")[0] if pair.split("|")[1] == FX["cash"].lower()
                      else pair.split("|")[1]
                      for pair in FX["stored_pair_pools"] if FX["cash"].lower() in pair.split("|")})
    assert bridges
    real = MH._pools_with_depth

    def measured(chain, token):
        # A provider's depth list: real pair pools of this bridge with depths well over the floor.
        key = "%s|%s" % tuple(sorted((FX["cash"].lower(), str(token).lower())))
        pairs = FX["stored_pair_pools"].get(key, [])[:4]
        return [(p, 1_000_000.0 - i) for i, (p, _k) in enumerate(pairs)] if providers_live else []

    def with_scan(chain, token, include_store=True, **kw):
        # The OLD behaviour whatever the caller asks: provider pools plus every stored pool of the
        # token at unknown depth (a real popular bridge has ~100k).
        return measured(chain, token) + [(p, None) for p in all_pools]

    def without_scan(chain, token, include_store=True, **kw):
        return measured(chain, token) + ([(p, None) for p in all_pools] if include_store else [])
    for bridge in bridges:
        monkeypatch.setattr(MH, "_pools_with_depth", with_scan)
        monkeypatch.setattr(MH, "_POOL_LIQUIDITY", {})
        a = MH._bridge_options("4663", FX["cash"], bridge, "https://rpc", limit=3)
        monkeypatch.setattr(MH, "_pools_with_depth", without_scan)
        monkeypatch.setattr(MH, "_POOL_LIQUIDITY", {})
        b = MH._bridge_options("4663", FX["cash"], bridge, "https://rpc", limit=3)
        assert a == b, bridge
    monkeypatch.setattr(MH, "_pools_with_depth", real)


def test_the_bridge_options_never_ask_for_the_store_scan(offline, monkeypatch):
    seen = []
    real = MH._pools_with_depth
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda chain, token, include_store=True, **kw: seen.append(include_store)
                        or real(chain, token, include_store=include_store, **kw))
    MH._bridge_options("4663", FX["cash"], "0x" + "cd" * 20, "https://rpc", limit=3)
    assert seen and not any(seen)


def test_the_ladder_trace_names_the_discovery_path(monkeypatch):
    from test_memecoin_evm_measured_ladder import _ladder, _quote
    rungs = []
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"].update(round_trip_pct=-3.8, discovery="store")
    _ladder(monkeypatch, multihop=multihop, rungs=rungs)
    rec = next(r for r in rungs if r["rung"] == "v4_multihop")
    assert rec["hit"] and rec.get("reason") == "discovery:store"


def test_the_flag_defaults_off(monkeypatch):
    """Off by default since 2026-09-28: a cold store search starved its second bridge (-49.3% vs
    -2.6% on the provider path, coin 0x8920d999). It comes back only on a measured cold result."""
    import importlib
    monkeypatch.delenv("MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", raising=False)
    assert importlib.reload(cfg).MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST is False
    monkeypatch.setenv("MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", "1")
    assert importlib.reload(cfg).MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST is True
    monkeypatch.setenv("MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", "0")
    assert importlib.reload(cfg).MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST is False
    monkeypatch.delenv("MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST")
    importlib.reload(cfg)


@pytest.mark.parametrize("live", [False, True])
def test_pools_with_depth_leaves_the_store_out_on_request(offline, monkeypatch, live):
    mint = LIVE[0][0]
    stored = FX["stored_pools_for_mint"][mint.lower()]
    measured = [(900_000.0, stored[0])] if live else []
    monkeypatch.setattr(VQ, "_gt_pools_ranked", lambda *a, **k: (measured, not live))
    monkeypatch.setattr(VQ, "_ds_pools_ranked", lambda *a, **k: ([], not live))
    assert MH._pools_with_depth("4663", mint, include_store=False) == (
        [(stored[0], 900_000.0)] if live else [])
    full = MH._pools_with_depth("4663", mint)
    assert {pid for pid, _d in full} == set(stored)           # the default still unions the store


# --- store ONLY, for the exact capture lane (2026-09-30, Alpha) ---
# The 4663 exact capture lane priced no bridge-paired coin: 15 of 41 refusals with an exact row were
# no_route. `store_only=True` runs the search on our PoolKey store alone and NEVER falls back to the
# providers, whatever MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST says (production keeps it off, and the
# funded rung's behaviour is unchanged: the parameter defaults to False).

def _store_only(mint, **kw):
    return MH.multihop_quote("4663", FX["cash"], mint, AMOUNT, rpc_url="https://rpc",
                             bridge_currencies=(), clip_usd=25.0, store_only=True, **kw)


@pytest.mark.parametrize("flag", [False, True])
@pytest.mark.parametrize("mint, executed", LIVE, ids=[m[:10] for m, _e in LIVE])
def test_store_only_finds_the_executed_pool_whatever_the_funded_flag_says(
        offline, monkeypatch, flag, mint, executed):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", flag, raising=False)
    route = _store_only(mint)
    assert route is not None and route["coin_pool_id"].lower() == executed.lower()
    assert route["discovery"] == "store" and offline["gt"] == 0 and offline["ds"] == 0


def test_store_only_never_falls_back_to_the_providers(offline):
    assert _store_only(DEAD) is None                       # every store quote reverts
    assert _store_only("0x" + "ab" * 20) is None           # the store does not hold the coin
    assert offline["gt"] == 0 and offline["ds"] == 0


def test_store_only_without_a_stored_pool_names_it(offline):
    assert _store_only("0x" + "ab" * 20) is None
    assert MH.last_multihop_refusal() == "no_stored_pool" and offline["quote"] == 0


def test_the_caller_may_pass_its_own_search_limits(offline, monkeypatch):
    """The capture lane runs behind a 4 requests/s bucket, so the funded 5 s deadline would cut it."""
    monkeypatch.setattr(MH, "_search_limits", lambda: pytest.fail("the funded limits were read"))
    seen = {}
    real = MH._SearchBudget

    def budget(deadline_s, max_requests, **kw):
        seen.update(deadline_s=deadline_s, max_requests=max_requests, **kw)
        return real(deadline_s, max_requests, **kw)
    monkeypatch.setattr(MH, "_SearchBudget", budget)
    mint, executed = LIVE[0]
    route = _store_only(mint, limits=(3, 12, 45.0, 40, 12.0))
    assert route["coin_pool_id"].lower() == executed.lower()
    assert (seen["deadline_s"], seen["max_requests"], seen["quote_time_s"]) == (45.0, 40, 12.0)


def test_a_provider_decided_route_names_why_the_store_pass_did_not(offline, monkeypatch):
    """2026-10-07 (Alpha): 0 of 10 multihop hits on 4663 since 10-01 came from the store, and the
    trace said only `discovery:provider`. The store pass's refusal now rides on the route."""
    mint = LIVE[0][0]
    real = MH._candidate_bridges
    passes = []

    def bridges(*a, **k):
        passes.append(1)
        return [] if len(passes) == 1 else real(*a, **k)       # the store pass finds no bridge
    monkeypatch.setattr(MH, "_candidate_bridges", bridges)
    route = _search(mint)
    assert route is not None and route["discovery"] == "provider"
    assert route["store_refusal"] == "no_bridge_candidates"
    assert _search(mint)["store_refusal"] is None                 # the store decided: nothing to name


def test_the_ladder_trace_names_the_store_refusal(monkeypatch):
    from test_memecoin_evm_measured_ladder import _ladder, _quote
    rungs = []
    multihop = _quote(1000, protocol="uniswap_v4_multihop")
    multihop["route"].update(round_trip_pct=-3.8, discovery="provider", store_refusal="no_bridge_candidates")
    _ladder(monkeypatch, multihop=multihop, rungs=rungs)
    rec = next(r for r in rungs if r["rung"] == "v4_multihop")
    assert rec["hit"] and rec.get("reason") == "discovery:provider store_refusal=no_bridge_candidates"


def test_the_built_quote_carries_the_store_refusal(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(MH, "multihop_quote", lambda *a, **k: {
        "amount_out": 1_000_000, "round_trip_pct": -8.0, "coin_depth_usd": 5000, "bridge": "0x" + "ab" * 20,
        "discovery": "provider", "store_refusal": "coin_leg_missing_or_under_floor:min_depth_usd=2500"})
    from sniper.execution import evm_v4_multihop_tx as tx
    monkeypatch.setattr(tx, "as_zerox_quote", lambda *a, **k: {"route": {}})
    weth = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
    got = MH.dex_v4_multihop_quote("4663", weth, "0x" + "cd" * 20, 10**16, "0xtaker", rpc_url="https://rpc",
                                   clip_usd=25.0)
    assert got["route"]["discovery"] == "provider"
    assert got["route"]["store_refusal"] == "coin_leg_missing_or_under_floor:min_depth_usd=2500"
