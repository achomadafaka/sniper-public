"""B: V4 multi-hop through a bridge currency (spec 2026-08-30 Amendment 5).

31% of armed-wallet 4663 coins have no sane ETH-side pool but DO have a sane pool quoted in
USDG or a tokenized equity. Measured four-leg round trips through ETH -> USDG -> coin and back:
-6.34% on a $9,083 pool and -5.52% on $4,816, against a single-hop 4663 baseline of -6.3%/-6.9%.
Four legs cost the same as the two we already accept, because HOP COUNT IS NOT WHAT SETS THE TAX
— clip-vs-depth is, and the bridge is cheap enough that the coin leg dominates.

The guards below are derived from that measurement, not assumed, and each is pinned here.
"""
import pytest
from eth_abi import decode

import sniper.execution.evm_v4_multihop as MH

CHAIN = "4663"
NATIVE = "0x" + "00" * 20
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
COIN = "0xffbb" + "22" * 18
DYNAMIC_FEE = 0x800000
CLIP_USD = 32.81


def _key(c0, c1, fee):
    return {"currency0": c0, "currency1": c1, "fee": fee, "tick_spacing": 60, "hooks": NATIVE}


@pytest.fixture(autouse=True)
def _offline_search(monkeypatch):
    """The route search must never read the production pool store or a live block number here."""
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda *a, **k: [])
    monkeypatch.setattr(MH, "_decision_block", lambda *a, **k: "latest")


@pytest.fixture
def wire(monkeypatch):
    st = {"pools": {}, "keys": {}, "depth": {}, "quotes": {}}

    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda chain, tok, **k: [(p, st["depth"].get(p)) for p in st["pools"].get(tok, [])])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)
    monkeypatch.setattr(MH, "_pool_key_for", lambda chain, pid, rpc, **k: st["keys"].get(pid))
    monkeypatch.setattr(MH, "_quote_leg",
                        lambda chain, key, zf1, amt, rpc, **k: st["quotes"].get((key["currency0"], key["currency1"], zf1)))

    def quote_path(_chain, currency_in, path, _amount, _rpc, **_kw):
        if currency_in == NATIVE:
            first = st["quotes"].get((NATIVE, path[0][1]["currency1"], True))
            second = st["quotes"].get((path[1][1]["currency0"], path[1][1]["currency1"], True))
            return second if first and second else None
        first = st["quotes"].get((path[0][1]["currency0"], path[0][1]["currency1"], False))
        second = st["quotes"].get((path[1][1]["currency0"], path[1][1]["currency1"], False))
        return second if first and second else None

    monkeypatch.setattr(MH, "_quote_path", quote_path)
    return st


def _bridge_ok(st, depth=2_503_797, fee=460):
    st["pools"].setdefault(USDG, []).append("p_bridge")
    st["keys"]["p_bridge"] = _key(NATIVE, USDG, fee)
    st["depth"]["p_bridge"] = depth
    st["quotes"][(NATIVE, USDG, True)] = 5000
    st["quotes"][(NATIVE, USDG, False)] = 900


def _coin_leg(st, depth, fee=0):
    st["pools"].setdefault(COIN, []).append("p_coin")
    st["keys"]["p_coin"] = _key(USDG, COIN, fee)
    st["depth"]["p_coin"] = depth
    st["quotes"][(USDG, COIN, True)] = 7000
    st["quotes"][(USDG, COIN, False)] = 4800


def _run(**kw):
    kw.setdefault("clip_usd", CLIP_USD)
    return MH.multihop_quote(CHAIN, NATIVE, COIN, 10**16, rpc_url="http://rpc",
                             bridge_currencies=(USDG,), **kw)


# --- guard 2: dynamic-fee bridge pools are a HARD exclusion ---------------------------------

def test_a_dynamic_fee_pool_is_never_chosen_as_the_bridge(wire):
    """A hook prices each swap on a 0x800000 pool, so `_fee_is_sane` CANNOT bound its cost, and
    the bridge leg carries every entry and every exit. Deeper is not a reason to take it."""
    wire["pools"][USDG] = ["p_dyn", "p_fixed"]
    wire["keys"]["p_dyn"] = _key(NATIVE, USDG, DYNAMIC_FEE)
    wire["depth"]["p_dyn"] = 99_000_000            # far deeper
    wire["keys"]["p_fixed"] = _key(NATIVE, USDG, 460)
    wire["depth"]["p_fixed"] = 2_503_797
    wire["quotes"][(NATIVE, USDG, True)] = 5000
    wire["quotes"][(NATIVE, USDG, False)] = 900
    _coin_leg(wire, depth=9083)
    r = _run()
    assert r is not None
    assert r["bridge_pool_id"] == "p_fixed", "chose a hook-priced bridge over a fixed-fee one"


def test_no_fixed_fee_bridge_means_refuse(wire):
    """Fail closed rather than fall back to a dynamic-fee bridge."""
    wire["pools"][USDG] = ["p_dyn"]
    wire["keys"]["p_dyn"] = _key(NATIVE, USDG, DYNAMIC_FEE)
    wire["depth"]["p_dyn"] = 99_000_000
    wire["quotes"][(NATIVE, USDG, True)] = 5000
    _coin_leg(wire, depth=9083)
    assert _run() is None


def test_bridge_scan_stops_at_the_deepest_eligible_pool(monkeypatch):
    pools = [("p_dynamic", 99_000_000), ("p_fixed", 2_503_797), ("p_lower", 1_000)]
    keys = {
        "p_dynamic": _key(NATIVE, USDG, DYNAMIC_FEE),
        "p_fixed": _key(NATIVE, USDG, 460),
    }
    seen = []
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: pools)
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)

    def pool_key(_chain, pool_id, _rpc, **_kw):
        seen.append(pool_id)
        if pool_id == "p_lower":
            raise AssertionError("the lower-depth pool must not be read")
        return keys[pool_id]

    monkeypatch.setattr(MH, "_pool_key_for", pool_key)

    result = MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")

    assert result[0] == "p_fixed"
    assert seen == ["p_dynamic", "p_fixed"]


def test_bridge_scan_never_reads_keys_of_unmeasured_pools(monkeypatch):
    """USDG's unmeasured list is every coin paired with it: 65,492 cached key reads took 122 s per
    decision on 2026-09-12, past the 8 s ladder deadline. An unmeasured pool is never a bridge, so
    its key must not be read at all."""
    pools = [("p_fixed", 2_503_797)] + [("u%d" % i, None) for i in range(1000)]
    seen = []
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: pools)
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)

    def pool_key(_chain, pool_id, _rpc, **_kw):
        seen.append(pool_id)
        return _key(NATIVE, "0x" + "99" * 20, 460)   # the measured pool is not the pair

    monkeypatch.setattr(MH, "_pool_key_for", pool_key)

    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc") is None
    assert seen == ["p_fixed"]


def test_an_empty_discovery_falls_back_to_the_last_good_bridge(monkeypatch):
    """ANIME 2026-09-12 08:10Z: GeckoTerminal spent, DexScreener never returns ETH/USDG (it lists
    USDG as the quote), so the bridge read 'missing' six times while the route existed."""
    pools = {"now": [("p_fixed", 2_503_797)]}
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: pools["now"])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)
    monkeypatch.setattr(MH, "_pool_key_for", lambda *_a, **_kw: _key(NATIVE, USDG, 460))
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")[0] == "p_fixed"
    pools["now"] = []                                 # provider blind on the next decision
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")[0] == "p_fixed"


def test_the_last_good_bridge_is_rescreened_before_use(monkeypatch):
    pools = {"now": [("p_fixed", 2_503_797)]}
    fee = {"now": 460}
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: pools["now"])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)
    monkeypatch.setattr(MH, "_pool_key_for", lambda *_a, **_kw: _key(NATIVE, USDG, fee["now"]))
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc") is not None
    pools["now"], fee["now"] = [], DYNAMIC_FEE           # the pool turned hook-priced
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc") is None


def test_bridge_scan_reads_keys_only_for_the_exact_pair(monkeypatch):
    pools = [("p_other", 99_000_000), ("p_fixed", 2_503_797)]
    seen = []
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: pools)
    monkeypatch.setattr(
        MH, "_direct_bridge_pools", lambda *_a, **_kw: [("p_fixed", 2_503_797)],
        raising=False,
    )

    def pool_key(_chain, pool_id, _rpc, **_kw):
        seen.append(pool_id)
        if pool_id == "p_other":
            raise AssertionError("an unrelated pool key must not be read")
        return _key(NATIVE, USDG, 460)

    monkeypatch.setattr(MH, "_pool_key_for", pool_key)

    result = MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")

    assert result[0] == "p_fixed"
    assert seen == ["p_fixed"]


def test_valid_direct_bridge_list_skips_provider_union(monkeypatch):
    monkeypatch.setattr(
        MH, "_direct_bridge_pools", lambda *_a, **_kw: [("p_fixed", 2_503_797)],
    )
    monkeypatch.setattr(
        MH, "_pools_with_depth",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("union must stay cold")),
    )
    monkeypatch.setattr(
        MH, "_pool_key_for", lambda *_a, **_kw: _key(NATIVE, USDG, 460),
    )

    result = MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")

    assert result[0] == "p_fixed"


def test_invalid_direct_bridge_list_falls_back_to_provider_union(wire, monkeypatch):
    _bridge_ok(wire)
    _coin_leg(wire, depth=9_083)
    wire["pools"][USDG].insert(0, "p_dynamic")
    wire["keys"]["p_dynamic"] = _key(NATIVE, USDG, DYNAMIC_FEE)
    wire["depth"]["p_dynamic"] = 99_000_000
    monkeypatch.setattr(
        MH, "_direct_bridge_pools", lambda *_a, **_kw: [("p_dynamic", 99_000_000)],
    )

    result = _run()

    assert result is not None
    assert result["bridge_pool_id"] == "p_bridge"


# --- guard 1: the depth floor, and the unmeasured band ---------------------------------------

def test_coin_leg_below_the_floor_is_refused(wire):
    """$368 = 11x clip round-tripped at -15.51%. $4,816 = 147x sat at -5.52%."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=368)
    assert _run(min_depth_clips=100.0) is None


def test_coin_leg_above_the_floor_is_admitted(wire):
    _bridge_ok(wire)
    _coin_leg(wire, depth=4816)
    assert _run(min_depth_clips=100.0) is not None


def test_the_default_floor_refuses_the_unmeasured_band(wire):
    """Evidence bounds 11x BAD and 147x GOOD. Everything between is UNMEASURED, and the default
    must refuse it rather than guess inside the gap. 50x would have admitted it."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=int(50 * CLIP_USD))       # 50x — inside the unmeasured band
    assert _run() is None, "default floor admitted a depth nobody has measured"


def test_floor_is_configurable_downward(wire):
    """It must be able to RELAX as evidence accumulates, not only tighten."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=int(20 * CLIP_USD))
    assert _run(min_depth_clips=100.0) is None
    assert _run(min_depth_clips=15.0) is not None


def test_unknown_depth_fails_CLOSED_when_the_flag_is_off(wire, monkeypatch):
    """The original guard, preserved. Absence of a disqualifying signal is not a qualifying one —
    the same rule as the premium gate's unpriceable_quote, and the reason `_fee_is_sane` admitting
    an unquotable pool bit us. `MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED=0` restores exactly this."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_ALLOW_UNMEASURED", False, raising=False)
    _bridge_ok(wire)
    _coin_leg(wire, depth=9083)
    wire["depth"]["p_coin"] = None
    assert _run() is None


def test_unknown_depth_is_carried_to_the_round_trip_quote_by_default(wire):
    """CONTRACT CHANGED 2026-09-04, deliberately. The providers lag fresh 4663 coins by MINUTES
    while a copy signal arrives in seconds, so "unknown depth" at decision time was almost never a
    statement about the pool — it was a statement about whether DexScreener had caught up. GPU was
    refused `no_route_all_venues` at 10:21 and the identical route quoted fine fourteen minutes
    later through a $14,102 pool.

    The pool is not admitted, it is DEFERRED to a stricter test: `multihop_quote` requires all four
    legs, in and back out, to quote at our clip before it returns anything."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=9083)
    wire["depth"]["p_coin"] = None
    assert _run() is not None


def test_unknown_depth_is_still_refused_when_the_round_trip_cannot_quote(wire):
    """The deferral only holds because the quote is the real gate. If a leg cannot quote, an
    unmeasured pool must still be refused — that is the trap `_fee_is_sane` could not catch."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=9083)
    wire["depth"]["p_coin"] = None
    wire["quotes"] = {}                     # nothing quotes
    assert _run() is None


# --- guard 3: all four legs, both directions -------------------------------------------------

def test_a_sell_leg_that_cannot_quote_is_refused(wire):
    """Buyable and not sellable is the one non-negotiable. A buy quote is not evidence of a sell."""
    _bridge_ok(wire)
    _coin_leg(wire, depth=9083)
    wire["quotes"][(USDG, COIN, False)] = None
    assert _run() is None


def test_a_bridge_sell_leg_that_cannot_quote_is_refused(wire):
    _bridge_ok(wire)
    _coin_leg(wire, depth=9083)
    wire["quotes"][(NATIVE, USDG, False)] = None
    assert _run() is None


def test_two_complete_path_quotes_cover_entry_and_exit(monkeypatch):
    """One call quotes both entry hops. One call quotes both exit hops."""
    bridge_key = _key(NATIVE, USDG, 460)
    coin_key = _key(USDG, COIN, 0)
    monkeypatch.setattr(
        MH, "_pools_with_depth",
        lambda _chain, token, **_kw: (
            [("p_coin", 9_083)] if token == COIN else [("p_bridge", 2_503_797)]
        ),
    )
    monkeypatch.setattr(
        MH, "_pool_key_for",
        lambda _chain, pool_id, _rpc, **_kw: (
            coin_key if pool_id == "p_coin" else bridge_key
        ),
    )
    seen = []

    def quote_path(_chain, currency_in, path, amount_in, _rpc, **_kw):
        seen.append((currency_in, tuple(item[0] for item in path), amount_in))
        return 7_000 if currency_in == NATIVE else 9 * 10**15

    monkeypatch.setattr(MH, "_quote_path", quote_path, raising=False)

    result = _run()

    assert result is not None
    assert seen == [
        (NATIVE, (USDG, COIN), 10**16),
        (COIN, (USDG, NATIVE), 7_000),
    ]


def test_coin_pools_beyond_the_per_step_limit_are_never_quoted(monkeypatch):
    """The search measures a bounded set of pools per step, not every pool of the coin.

    Bridge discovery still reads each coin pool's key once to learn its counter-currency; the
    limit bounds the QUOTES, which are the requests that grow with the number of combinations.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_POOLS_PER_STEP", 3, raising=False)
    bridge_key = _key(NATIVE, USDG, 460)
    coin_keys = {"p_coin": _key(USDG, COIN, 3000), "p_2": _key(USDG, COIN, 3001),
                 "p_3": _key(USDG, COIN, 3002), "p_unused": _key(USDG, COIN, 3003)}
    coin_pools = [("p_coin", 9_083), ("p_2", 8_900), ("p_3", 8_800), ("p_unused", 8_000)]
    monkeypatch.setattr(
        MH, "_pools_with_depth",
        lambda _chain, token, **_kw: coin_pools if token == COIN else [],
    )
    monkeypatch.setattr(
        MH, "_direct_bridge_pools", lambda *_a, **_kw: [("p_bridge", 2_503_797)],
    )
    monkeypatch.setattr(MH, "_pool_key_for",
                        lambda _chain, pool_id, _rpc, **_kw: coin_keys.get(pool_id, bridge_key))
    quoted_coin_fees = set()

    def quote_path(_chain, currency, path, _amount, _rpc, **_kw):
        quoted_coin_fees.update(key["fee"] for _c, key in path if key["fee"] >= 3000)
        return 7_000 if currency == NATIVE else 9 * 10**15

    monkeypatch.setattr(MH, "_quote_path", quote_path)

    result = MH.multihop_quote(
        CHAIN, NATIVE, COIN, 10**16, rpc_url="http://rpc",
        bridge_currencies=(), clip_usd=CLIP_USD,
    )

    assert result is not None
    assert quoted_coin_fees == {3000, 3001, 3002}


def test_complete_path_quote_uses_the_v4_quoter_abi(monkeypatch):
    seen = {}

    def rpc(url, method, params, http=None):
        seen.update(url=url, method=method, params=params, http=http)
        return {"result": "0x" + format(7_000, "064x") + format(123_456, "064x")}

    monkeypatch.setattr("sniper.execution.evm_read_rpc._rpc", rpc)
    bridge_key = _key(NATIVE, USDG, 460)
    coin_key = _key(USDG, COIN, 0)

    result = MH._quote_path(
        CHAIN, NATIVE, ((USDG, bridge_key), (COIN, coin_key)), 10**16,
        "http://rpc", http="funded-http",
    )

    assert result == 7_000
    assert seen["method"] == "eth_call"
    assert seen["http"] == "funded-http"
    data = seen["params"][0]["data"]
    assert data[:10] == "0xca253dc9"
    decoded = decode(
        ["(address,(address,uint24,int24,address,bytes)[],uint128)"],
        bytes.fromhex(data[10:]),
    )[0]
    assert decoded[0] == NATIVE
    assert tuple(item[0] for item in decoded[1]) == (USDG, COIN)
    assert decoded[2] == 10**16


def test_multihop_uses_the_funded_http_for_rpc_reads(wire, monkeypatch):
    funded_http = object()
    seen = []
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    monkeypatch.setattr(
        MH, "_pool_key_for",
        lambda chain, pid, rpc, **kw: seen.append(kw.get("http")) or wire["keys"].get(pid),
    )
    monkeypatch.setattr(
        MH, "_quote_path",
        lambda chain, currency, path, amt, rpc, **kw: (
            seen.append(kw.get("http"))
            or (7_000 if currency == NATIVE else 900)
        ),
    )

    assert _run(http=funded_http) is not None
    # The search wraps the funded transport to count and limit its requests; the bytes still go
    # out through the funded transport.
    assert seen and all(getattr(value, "_base", None) is funded_http for value in seen)


def test_result_reports_the_round_trip(wire):
    """The measured cost must come back with the quote so a caller can gate on it."""
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    r = _run()
    assert "round_trip_pct" in r and r["round_trip_pct"] is not None


def test_empty_bridge_config_discovers_the_coin_counter_currency(wire):
    """A foreign V4 pool must not stay dark because a new quote token lacks a config entry."""
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    result = MH.multihop_quote(
        CHAIN, NATIVE, COIN, 10**16, rpc_url="http://rpc",
        bridge_currencies=(), clip_usd=CLIP_USD,
    )
    assert result is not None
    assert result["bridge"].lower() == USDG.lower()


# --- no coin leg, no route -------------------------------------------------------------------

def test_no_sane_coin_leg_is_refused(wire):
    _bridge_ok(wire)
    wire["pools"][COIN] = ["p_trap"]
    wire["keys"]["p_trap"] = _key(USDG, COIN, 864114)      # 86.4% trap
    wire["depth"]["p_trap"] = 500_000
    wire["quotes"][(USDG, COIN, True)] = 10**9
    assert _run() is None


# --- the not-executable contract -------------------------------------------------------------

def test_a_route_is_marked_NOT_executable(wire):
    """The raw decision needs the transaction adapter before use."""
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    r = _run()
    assert r["executable"] is False


def test_multihop_ships_dark():
    """Default OFF, pinned separately from behaviour — a default flipped by accident is how dark
    code ships lit."""
    import os
    from sniper.settings import memecoin as cfg
    assert os.getenv("MEMECOIN_EVM_V4_MULTIHOP_ENABLED") or cfg.MEMECOIN_EVM_V4_MULTIHOP_ENABLED is False


def test_funded_startup_accepts_multihop_after_exit_recovery(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    cfg.validate_real_exit_policy()


def test_funded_startup_rejects_an_invalid_retry_slippage(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIN_OUT_RETRY_SLIPPAGE_PCT", 100.0)
    with pytest.raises(RuntimeError, match="MIN_OUT_RETRY_SLIPPAGE"):
        cfg.validate_real_exit_policy()


def test_the_default_depth_floor_is_the_conservative_one():
    """100x, not 50x. 50x sits inside the band between measured-bad (11x) and measured-good
    (147x) and would admit depths nobody has measured."""
    import os
    from sniper.settings import memecoin as cfg
    if not os.getenv("MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS"):
        assert cfg.MEMECOIN_EVM_V4_MULTIHOP_MIN_DEPTH_CLIPS >= 100.0


def test_no_clip_means_refuse(wire):
    """The floor is expressed in clip multiples, so without a clip it cannot be evaluated."""
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    assert MH.multihop_quote(CHAIN, NATIVE, COIN, 10**16, rpc_url="http://rpc",
                             bridge_currencies=(USDG,), clip_usd=None) is None


# --- WIRING, not logic. The gap that let a fail-closed-on-everything defect ship. -------------

def test_pools_with_depth_returns_ids_PAIRED_with_their_depth(monkeypatch):
    """The original bug: ids came from one call and depth from another keyed differently, so the
    depth lookup missed every time and the module refused 100% of routes while its tests passed.

    This exercises the REAL `_pools_with_depth` against stubbed providers, so a future change that
    breaks the pairing cannot pass by monkeypatching the seam away.
    """
    import sniper.execution.evm_v4_quote as Q
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda c, t, **k: ([(9083.0, "p_a")], False))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda c, t, **k: ([(4816.0, "p_b")], False))
    got = dict(MH._pools_with_depth(CHAIN, COIN))
    assert got == {"p_a": 9083.0, "p_b": 4816.0}, got


def test_pools_with_depth_prefers_the_deeper_report_for_the_same_pool(monkeypatch):
    import sniper.execution.evm_v4_quote as Q
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda c, t, **k: ([(100.0, "p_a")], False))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda c, t, **k: ([(9083.0, "p_a")], False))
    assert dict(MH._pools_with_depth(CHAIN, COIN)) == {"p_a": 9083.0}


def test_both_providers_dark_returns_empty_not_zero_depth(monkeypatch):
    """Absence of a provider answer must not read as a pool with zero depth."""
    import sniper.execution.evm_v4_quote as Q
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda c, t, **k: ([], True))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda c, t, **k: ([], True))
    assert MH._pools_with_depth(CHAIN, COIN) == []


# --- the venue rung adapter: decide AND build, or nothing ------------------------------------

@pytest.fixture
def rung(monkeypatch, wire):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES", {CHAIN: (USDG,)}, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", 1.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_COPY_SIZE_USD", 32.81, raising=False)
    import sniper.execution.evm_v4_multihop_tx as TX
    monkeypatch.setattr(TX, "_permit2_input", lambda *a, **k: "00" * 64)
    _bridge_ok(wire); _coin_leg(wire, depth=9083)
    return wire


WETH_TOKEN = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"


def _rung_call(**kw):
    kw.setdefault("rpc_url", "http://rpc")
    kw.setdefault("taker", "0x" + "11" * 20)
    kw.setdefault("now_fn", lambda: 0)
    return MH.dex_v4_multihop_quote(CHAIN, WETH_TOKEN, COIN, 10**16, kw.pop("taker"), **kw)


def test_rung_is_inert_when_the_flag_is_off(rung, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False, raising=False)
    assert _rung_call() is None


def test_rung_discovers_a_bridge_when_no_bridge_is_configured(rung, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES", {}, raising=False)
    assert _rung_call() is not None


def test_rung_builds_the_sell_side(rung):
    q = MH.dex_v4_multihop_quote(
        CHAIN, COIN, WETH_TOKEN, 10**18, "0x" + "11" * 20,
        rpc_url="http://rpc", now_fn=lambda: 0, acct=object())
    assert q is not None
    assert q["route"]["protocol"] == "uniswap_v4_multihop"


def test_sell_rung_uses_the_funded_http_for_rpc_reads(rung, monkeypatch):
    funded_http = object()
    route = _rung_call()["route"]
    seen = []
    monkeypatch.setattr(
        MH, "_quote_leg",
        lambda chain, key, zf1, amt, rpc, **kw: seen.append(kw.get("http")) or 100,
    )

    MH.dex_v4_multihop_exit_quote(
        CHAIN, COIN, 10**18, "0x" + "11" * 20,
        route=route, rpc_url="http://rpc", http=funded_http,
    )

    assert len(seen) == 2
    assert all(value is funded_http for value in seen)


def test_rung_returns_an_EXECUTABLE_quote(rung):
    """The decision layer alone is not enough: a route that cannot be BUILT must never come back
    as quotable. That separation is what stopped step 0 shipping a malformed transaction."""
    q = _rung_call()
    assert q is not None
    assert q["transaction"]["to"] and q["transaction"]["data"]
    assert q["route"]["protocol"] == "uniswap_v4_multihop"
    assert q["route"]["legs"] == 2


def test_rung_applies_slippage_to_the_END_TO_END_output(rung):
    q = _rung_call()
    assert int(q["minBuyAmount"]) < int(q["buyAmount"])
    assert int(q["minBuyAmount"]) == int(q["buyAmount"]) * 9900 // 10000


def test_rung_carries_the_evidence_forward(rung):
    """Depth and measured round trip travel with the quote so a caller can gate on them."""
    q = _rung_call()
    assert q["route"]["coin_depth_usd"] == 9083
    assert q["route"]["round_trip_pct"] is not None


def test_rung_refuses_when_the_route_fails_its_own_floor(rung):
    rung["depth"]["p_coin"] = 368          # 11x clip -> below the 100x floor
    assert _rung_call() is None


# --- bounded backward scan for the PoolKey (2026-09-02) -----------------------------------------
#
# The first read covers _V4_DISCOVERY_LOOKBACK_BLOCKS = 8192, which is 829 SECONDS on 4663. The
# coins we quote are a median 1.41h old (6x that) and p90 15.67h (68x). Of 475 funded 4663
# attempts that reserved capital in 48h, only 64 filled and 153 died on "no quote".


def _mk(monkeypatch, pages):
    """pages: list of results per eth_getLogs call, in order. An entry may be an Exception."""
    from sniper.execution import evm_v4_quote as V
    calls = []

    def fake_rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(1_000_000)}
        calls.append(params[0])
        nxt = pages.pop(0) if pages else []
        if isinstance(nxt, Exception):
            raise nxt
        return {"result": nxt}

    monkeypatch.setattr(V, "_rpc", fake_rpc)
    monkeypatch.setattr(V, "_stored_pool_key", lambda c, p: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda c, p, k: None)
    V._key_cache.clear()
    return V, calls


LOG = [{"topics": ["0x", "0x" + "1" * 64,
                   "0x" + "0" * 64, "0x" + "2" * 24 + "b" * 40],
        "data": "0x" + "0" * 64 + "0" * 64 + ("%064x" % 60) + "0" * 64}]


def test_it_walks_BACK_when_the_first_window_misses(monkeypatch):
    """8192 blocks is 13.8 minutes. A coin an hour old is invisible without this."""
    V, calls = _mk(monkeypatch, [[], [], LOG])
    V._pool_key("4663", "0x" + "1" * 64, "http://rpc")
    lows = [int(c["fromBlock"], 16) for c in calls if c.get("fromBlock") != "0x0"]
    assert len(lows) >= 3, calls
    assert lows[0] > lows[1] > lows[2], "the scan must walk BACKWARD"


def test_it_stops_on_the_first_hit(monkeypatch):
    V, calls = _mk(monkeypatch, [[], LOG, [], []])
    V._pool_key("4663", "0x" + "1" * 64, "http://rpc")
    assert len(_scan_calls(calls)) == 1, "kept scanning after finding the key"


def _scan_calls(calls):
    """Only the bounded backward-scan pages. Excludes the first window and the unbounded
    `0x0`->`latest` last resort, which still runs after the scan for pools older than its reach."""
    return [c for c in calls if c.get("fromBlock") != "0x0"][1:]


def test_the_scan_depth_is_bounded(monkeypatch):
    """An unbounded walk is the unbounded 0x0 query wearing a loop."""
    from sniper.execution import evm_v4_quote as V0
    V, calls = _mk(monkeypatch, [[]] * 50)
    V._pool_key("4663", "0x" + "1" * 64, "http://rpc")
    assert len(_scan_calls(calls)) <= V0._V4_KEY_SCAN_MAX_PAGES, len(calls)


def test_a_REFUSED_page_stops_the_walk_and_an_EMPTY_one_does_not(monkeypatch):
    """An empty page is absence for that range only; a refusal says nothing about the range
    below it, so continuing past one would read a provider fault as a clean no-pool."""
    V, calls = _mk(monkeypatch, [[], RuntimeError("refused"), LOG])
    V._pool_key("4663", "0x" + "1" * 64, "http://rpc")
    assert len(_scan_calls(calls)) == 1, (
        "the walk must STOP at a refusal — a refused page says nothing about the range below it")


def test_the_scan_reaches_past_the_p90_coin_age():
    from sniper.execution import evm_v4_quote as V
    assert V._V4_KEY_SCAN_PAGE_BLOCKS * V._V4_KEY_SCAN_MAX_PAGES >= 558_399, (
        "p90 of the coins we actually quote is 15.67h = ~558k blocks on 4663")



# --- on-chain pair pick: bridge and middle legs without a provider (2026-09-12) -----------------
HOOK = "0x" + "ab" * 20


def _stored(monkeypatch, pools, liq):
    monkeypatch.setattr(MH, "_stored_pair_pools", lambda chain, a, b: pools)
    calls = []

    def read(chain, pids, rpc, **kw):              # one batched extsload call per pair
        calls.extend(pids)
        return {pid: liq.get(pid) for pid in pids}

    monkeypatch.setattr(MH, "_onchain_liquidities", read)
    return calls


def test_the_pick_is_the_most_liquid_hookless_fixed_fee_pool(monkeypatch):
    k = lambda fee, hooks=None: dict(_key(NATIVE, USDG, fee), **({"hooks": hooks} if hooks else {}))
    pools = [("p_hooked", k(1, HOOK)), ("p_dyn", k(DYNAMIC_FEE)), ("p_460", k(460)), ("p_100", k(100))]
    calls = _stored(monkeypatch, pools, {"p_hooked": 10**20, "p_460": 10**17, "p_100": 4 * 10**17})
    pid, key, depth = MH._onchain_pair_pick(CHAIN, NATIVE, USDG, "http://rpc")
    assert (pid, key["fee"], depth) == ("p_100", 100, None)
    assert sorted(calls) == ["p_100", "p_460"], "hooked and dynamic-fee pools are never read"


def test_a_blind_provider_bridge_is_found_on_chain(monkeypatch):
    monkeypatch.setattr(MH, "_pools_with_depth", lambda *_a, **_kw: [])
    monkeypatch.setattr(MH, "_direct_bridge_pools", lambda *_a, **_kw: None)
    _stored(monkeypatch, [("p_100", _key(NATIVE, USDG, 100))], {"p_100": 5})
    assert MH._select_bridge(CHAIN, NATIVE, USDG, "http://rpc")[0] == "p_100"


def test_the_middle_leg_asks_the_chain_before_walking_the_provider_list(monkeypatch):
    fx = "0x72417f0f202aaf80199179e10e280ded5715807e"
    _stored(monkeypatch, [("p_mid", _key(USDG, fx, 500))], {"p_mid": 7})
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("provider walked")))
    assert MH._select_middle_leg(CHAIN, USDG, fx, "http://rpc")[0] == "p_mid"


def test_the_middle_leg_chain_read_admits_hook_priced_pools_the_cash_leg_does_not(monkeypatch):
    """0x250f720f (4663, 2026-09-28): the best route ran through a hook-priced dynamic-fee
    USDG/0xc72b96 middle pool (-3.47%). The chain-first middle-leg read was hookless-only, so once
    batched reads made it non-empty the providers were never asked and the route fell to -5.23%.
    The middle leg admits hook-priced pools (its quote is measured both ways); the cash leg does not.
    """
    fx = "0x72417f0f202aaf80199179e10e280ded5715807e"
    hooked = dict(_key(USDG, fx, DYNAMIC_FEE), hooks=HOOK)
    _stored(monkeypatch, [("p_hooked", hooked), ("p_2333", _key(USDG, fx, 2333))],
            {"p_hooked": 10**20, "p_2333": 10**15})
    monkeypatch.setattr(MH, "_pools_with_depth",
                        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("provider walked")))
    assert [p for p, _k, _d in MH._middle_leg_options(CHAIN, USDG, fx, "http://rpc", limit=3)] == [
        "p_hooked", "p_2333"]
    assert [p for p, _k, _d in MH._onchain_pair_options(CHAIN, USDG, fx, "http://rpc")] == ["p_2333"]


def test_an_unreadable_chain_is_not_remembered(monkeypatch):
    calls = _stored(monkeypatch, [("p_100", _key(NATIVE, USDG, 100))], {})
    assert MH._onchain_pair_pick(CHAIN, NATIVE, USDG, "http://rpc") is None
    assert MH._onchain_pair_pick(CHAIN, NATIVE, USDG, "http://rpc") is None
    assert calls == ["p_100", "p_100"], "a failed read retries on the next decision"



def test_both_providers_dark_still_returns_the_stores_pools(monkeypatch):
    """With GeckoTerminal and DexScreener forced blind, five live 4663 coins (ANIME and four
    stock-paired) refused no_bridge_candidates although every pool on their route is stored."""
    from sniper.execution import evm_v4_quote as Q
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda *a, **k: ([], True))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda *a, **k: ([], True))
    monkeypatch.setattr(Q, "_stored_pools_for_mint", lambda chain, token: ["0x" + "11" * 32])
    assert MH._pools_with_depth(CHAIN, USDG) == [("0x" + "11" * 32, None)]
