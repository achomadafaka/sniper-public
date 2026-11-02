"""The safety sell probe reuses a cached full prefix without discovering another path."""
import copy
import time

import pytest

from sniper.execution import evm_real_seams as seams
from sniper.execution import evm_source_route as src
from sniper.execution import evm_v4_multihop as multihop
from sniper.execution import evm_v4_multihop_tx as tx
from sniper.execution import evm_v4_quote as v4q
from sniper.execution.evm_v4_launchpad import pool_id_for_key
from test_memecoin_evm_safety_exit_hint import (
    BRIDGE as USDG, CHAIN, COIN, NATIVE, TAKER, WETH, _rec, _sell_ladder,
)

X = "0x" + "44" * 20
FIRST = {"currency0": NATIVE, "currency1": USDG, "fee": 100,
         "tick_spacing": 1, "hooks": NATIVE}
MIDDLE = {"currency0": X, "currency1": USDG, "fee": 3000,
          "tick_spacing": 60, "hooks": NATIVE}
COIN_KEY = {"currency0": COIN, "currency1": X, "fee": 3000,
            "tick_spacing": 60, "hooks": NATIVE}
POOL = pool_id_for_key(COIN_KEY).lower()
AMOUNT = 10 ** 30 + 17
LEGS = [{"currency": USDG, "key": FIRST}, {"currency": X, "key": MIDDLE},
        {"currency": COIN, "key": COIN_KEY}]


@pytest.fixture
def probe(monkeypatch):
    from sniper.settings import memecoin as cfg

    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_CACHE_CHAINS", CHAIN)
    monkeypatch.setattr(src, "_bridges", {})
    monkeypatch.setattr(src, "_pools", {})
    monkeypatch.setattr(v4q, "_key_cache", {})
    monkeypatch.setattr(v4q, "_pool_key_cached", lambda c, p: COIN_KEY if p == POOL else None)
    monkeypatch.setattr(src, "_tape_key", lambda *a: None)
    for name, value in (("MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True),
                        ("MEMECOIN_EVM_V2_ENTRY_ENABLED", True),
                        ("MEMECOIN_EVM_V4_ENABLED", True),
                        ("MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True),
                        ("MEMECOIN_EVM_RELAY_ENABLED", False),
                        ("MEMECOIN_EVM_LAUNCHPAD_CURVE_ENABLED", False),
                        ("MEMECOIN_EVM_FLAP_CURVE_ENABLED", False)):
        monkeypatch.setattr(cfg, name, value, raising=False)
    monkeypatch.setattr(seams, "_rung_ladder_chains", lambda c: {CHAIN})
    calls, rungs = [], []

    def quote_path(chain, mint, path, amount, rpc_url, **kw):
        calls.append((chain, mint, path, amount))
        return 123456

    def forbidden(*a, **kw):
        raise AssertionError("cached safety prefix must not discover or quote separate legs")

    monkeypatch.setattr(multihop, "_quote_path", quote_path)
    monkeypatch.setattr(multihop, "_quote_leg", forbidden)
    monkeypatch.setattr(multihop, "_pools_with_depth", forbidden)
    monkeypatch.setattr(multihop, "_candidate_bridges", forbidden)
    monkeypatch.setattr(tx, "_permit2_input", lambda *a, **kw: "00")
    fn = seams.venue_quote_with_v2_fallback(
        lambda *a, **kw: None, chain=CHAIN, quote_token=WETH, taker=TAKER,
        v2_quote_fn=lambda *a, **kw: None, v4_quote_fn=lambda *a, **kw: None,
        acct=object(), rpc_url="https://unused", on_rung=rungs.append)

    def run():
        return fn(COIN, WETH, AMOUNT, exit_hint=POOL, only_rung="v4_source_exit",
                  slippage_bps=100, first_available=True)

    return run, calls, rungs


def remember(legs=None, *, now=None):
    legs = copy.deepcopy(LEGS if legs is None else legs)
    src.remember_bridge(CHAIN, {"legs_path": legs,
                                "bridge_pool_id": pool_id_for_key(legs[0]["key"])}, now=now)


def test_three_leg_safety_exit_quotes_the_full_reversed_path_at_exact_atomic_amount(probe):
    run, calls, rungs = probe
    remember()
    before = copy.deepcopy(src._bridges)
    assert src.bridge_for(CHAIN, X) is None
    out = run()
    assert out is not None, "a cached cash→USDG→X prefix must reach the source safety exit"
    assert out["_sniper_winner"] == "v4_source_exit"
    assert out["sellAmount"] == str(AMOUNT)
    assert out["route"]["coin_key"] == COIN_KEY
    assert out["route"]["legs_path"] == LEGS
    assert calls == [(CHAIN, COIN, ((X, COIN_KEY), (USDG, MIDDLE), (NATIVE, FIRST)), AMOUNT)]
    assert _rec(rungs, "v4_source_exit")["hit"]
    assert src._bridges == before, "the safety probe must not change cached evidence"


@pytest.mark.parametrize("case", ["unknown", "expired", "disabled", "wrong_pair", "bad_key",
                                  "wrong_target", "wrong_first_hash", "dynamic_first"])
def test_unusable_prefix_is_a_miss_before_any_quote(probe, monkeypatch, case):
    run, calls, rungs = probe
    legs = copy.deepcopy(LEGS)
    if case == "wrong_pair":
        legs[1]["key"]["currency0"] = COIN
    elif case == "bad_key":
        del legs[1]["key"]["tick_spacing"]
    elif case == "wrong_target":
        legs[1]["currency"] = COIN
    elif case == "dynamic_first":
        legs[0]["key"]["fee"] = multihop.DYNAMIC_FEE_FLAG
    if case != "unknown":
        remember(legs, now=time.monotonic() - src.CACHE_TTL_S if case == "expired" else None)
    if case == "disabled":
        monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", "")
    if case == "wrong_first_hash":
        expiry, _pid, key, depth, prefix = src._bridges[(CHAIN, X)]
        src._bridges[(CHAIN, X)] = (expiry, "0x" + "ff" * 32, key, depth, prefix)
    assert run() is None
    assert not calls
    assert _rec(rungs, "v4_source_exit")["reason"] == "source_bridge_uncached"


def test_multihop_disabled_never_reads_the_cached_prefix(probe, monkeypatch):
    from sniper.settings import memecoin as cfg

    run, calls, rungs = probe
    remember()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", False)
    monkeypatch.setattr(src, "prefix_for", lambda *a, **kw: pytest.fail("disabled prefix read"))
    assert run() is None and not calls
    assert _rec(rungs, "v4_source_exit")["reason"] == "source_multihop_disabled"


@pytest.mark.parametrize("prefix_enabled", [True, False])
@pytest.mark.parametrize("cached", ["one_leg", "three_legs", "missing"])
def test_source_exit_reads_one_cache_snapshot(probe, monkeypatch, prefix_enabled, cached):
    run, _calls, _rungs = probe
    if cached == "one_leg":
        direct = dict(FIRST, currency1=X)
        remember([{"currency": X, "key": direct}, {"currency": COIN, "key": COIN_KEY}])
    elif cached == "three_legs":
        remember()
    monkeypatch.setenv("MEMECOIN_EVM_BRIDGE_PREFIX_CHAINS", CHAIN if prefix_enabled else "")
    live, readers = [], []
    old_live, old_bridge, old_prefix = src._live, src.bridge_for, src.prefix_for

    def read_live(*a, **kw):
        live.append(a)
        return old_live(*a, **kw)

    def read_bridge(*a, **kw):
        readers.append("bridge")
        return old_bridge(*a, **kw)

    def read_prefix(*a, **kw):
        readers.append("prefix")
        return old_prefix(*a, **kw)

    monkeypatch.setattr(src, "_live", read_live)
    monkeypatch.setattr(src, "bridge_for", read_bridge)
    monkeypatch.setattr(src, "prefix_for", read_prefix)
    monkeypatch.setattr(multihop, "dex_v4_multihop_exit_quote", lambda *a, **kw: None)
    run()
    assert len(live) == 1, "one existing lock acquisition per source-exit cache lookup"
    assert readers == (["prefix"] if prefix_enabled else ["bridge"])


@pytest.mark.parametrize("case, reason", [("partial", "source_exit_partial"),
                                         ("wrong_pool", "source_exit_pool_mismatch"),
                                         ("no_quote", "source:exit_path_no_quote_at_any_size:3_legs")])
def test_cached_prefix_keeps_full_amount_and_source_pool_refusals(probe, monkeypatch, case, reason):
    run, calls, rungs = probe
    remember()
    if case in ("partial", "no_quote"):
        quote = multihop._quote_path
        monkeypatch.setattr(multihop, "_quote_path", lambda *a, **kw:
                            None if case == "no_quote" or a[3] == AMOUNT else quote(*a, **kw))
    else:
        build = tx.as_zerox_exit_quote

        def wrong_pool(*a, **kw):
            out = build(*a, **kw)
            out["route"]["coin_key"] = dict(COIN_KEY, fee=500)
            return out

        monkeypatch.setattr(tx, "as_zerox_exit_quote", wrong_pool)
    assert run() is None
    assert _rec(rungs, "v4_source_exit")["reason"] == reason


def test_missing_prefix_preserves_ordinary_ladder_fallback(monkeypatch, probe):
    out, _took, _pinned, _builds = _sell_ladder(
        monkeypatch, exit_hint=POOL, keys={POOL: COIN_KEY})
    assert out["_sniper_winner"] == "v4"
