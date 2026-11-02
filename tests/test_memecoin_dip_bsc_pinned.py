"""Dip retry on every funded EVM chain (operator ~15:00Z 2026-10-08: "These need to be for top traders
on all chains, not only RH and Solana"; design 92f34c9b7).

BSC 56 was left out because its v2, v3, v3_bridge and mixed rungs read DexScreener pair lists. On a
display-feed chain the watch now re-quotes ONLY the refused route, pinned from tick 0, with no pair
discovery, and its ladder tick excludes those rungs. Chain 1 ticks every 3 s (12 s blocks).

No network. Every quote function is a fake or a patched low-level read.
"""
import json
from dataclasses import asdict

import pytest

from sniper.execution import evm_swap_executor as X
from sniper.services import memecoin_dip_retry as D
from test_memecoin_dip_watch_winner import (BUY0, DEGEN, MINT, SELL, SIGNAL, T0, _conn, _Ctx,
                                            _refusal)
from sniper.feeds.fomo_feed import CopyEvent

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
CONN = "0x55d398326f99059ff775485246999027b3197955"
BASE = "0x" + "be" * 20
ROUTER = "0x10ed43c718714eb63d5aa57b78b54704e256024e"


@pytest.fixture(autouse=True)
def _bsc(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_S", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_WALLETS", ("degencapitalllc",))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_CHAINS", ("solana", "4663", "56", "1"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", frozenset({"56"}))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_PROBE_CHAINS", frozenset({"56"}))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_MAX_HITS", 3)
    from sniper.execution import evm_v2_exit_quote as V2
    monkeypatch.setattr(V2, "pair_quote_tokens",
                        lambda *a, **k: pytest.fail("the dip watch read a pair list"))
    return monkeypatch


def _ev56(fid="b1"):
    return CopyEvent(
        source_user_id=DEGEN, source_handle="DegenCapitalLLC", mint=MINT, ticker="", side="buy",
        their_price=1e-5, their_market_cap=50000.0, their_size_usd=500.0, is_first_buy=True,
        fomo_event_id=fid, created_at=SIGNAL, received_at=SIGNAL, lag_s=0.5,
        signal_source="follow_activity", network_id="56")


def _seed56(tmp_path, rung, pin):
    c = _conn(tmp_path)
    ev = _ev56()
    res = _refusal(c, ev, rung=rung)
    res["dip_route"]["pin"] = pin
    c.execute("UPDATE memecoin_entry_attempts SET network_id='56'")
    assert D.maybe_enqueue(c, ev, res, "2026-09-30T12:00:01+00:00", event_json=json.dumps(asdict(ev)))
    c.commit()
    return c


def _watch56(tmp_path, ctx, chain="56"):
    if ctx is not None:
        ctx["quote_token"] = WBNB                       # 56 quotes in WBNB
    return D.EvmDipWatch(str(tmp_path / "ledger.db"), chain, contexts_fn=lambda: {chain: ctx},
                         clock=lambda: T0 + 5)


def test_56_is_a_dip_chain_again_and_stays_a_display_feed_chain():
    assert "56" in D.chains() and "56" in D.display_feed_chains() and "56" in D.evm_chains()


def test_the_dip_route_carries_the_pin_each_discovery_rung_needs():
    v2 = X._dip_route({"sellAmount": str(SELL), "buyAmount": str(BUY0), "_sniper_winner": "v2",
                       "route": {"protocol": "uniswap_v2", "router": ROUTER,
                                 "tokens": [WBNB, CONN, MINT], "path": [WBNB, CONN, MINT]},
                       "_fot_exit": {"chain": "56", "router": ROUTER, "path": [WBNB, CONN, MINT],
                                     "cut_bps": 1500}})
    assert v2["pin"] == {"router": ROUTER, "path": [WBNB, CONN, MINT], "tokens": [WBNB, CONN, MINT],
                         "cut_bps": 1500}
    mixed = X._dip_route({"sellAmount": str(SELL), "buyAmount": str(BUY0),
                          "_sniper_winner": "mixed_v3_v2",
                          "route": {"protocol": "pcs_mixed_v3_v2", "router": "0xsmart", "base_token": BASE,
                                    "v3_tokens": [WBNB, BASE], "v3_fees": [2500], "round_trip_pct": -3.0}})
    assert mixed["pin"] == {"router": "0xsmart", "base_token": BASE, "v3_tokens": [WBNB, BASE],
                            "v3_fees": [2500]}
    assert json.loads(json.dumps(mixed)) == mixed


def test_a_56_v2_winner_is_re_quoted_on_its_own_router_and_path_at_its_cut(tmp_path, _bsc):
    from sniper.execution import evm_v2_exit_quote as V2
    seen = []
    _bsc.setattr(V2, "_quote_path", lambda call_fn, router, amount_in, path: seen.append(
        (router, amount_in, tuple(path))) or 1100)
    _seed56(tmp_path, "v2", {"router": ROUTER, "path": [WBNB, CONN, MINT], "cut_bps": 1500})
    ctx = _Ctx([])                                       # the ladder is never asked
    w = _watch56(tmp_path, ctx)
    w.tick()
    assert seen == [(ROUTER, SELL * 8500 // 10000, (WBNB, CONN, MINT))] and ctx.calls == []
    assert w.premium["b1"] == pytest.approx((1.6 * BUY0 / 1100 - 1) * 100)


def test_a_56_v3_winner_is_quoted_on_its_own_path_with_paths_only(tmp_path, _bsc):
    from sniper.execution import evm_dex_quote as V3
    seen = []

    def v3(chain, sell, buy, amount, taker, **kw):
        seen.append((chain, sell, buy, amount, kw.get("extra_paths"), kw.get("paths_only")))
        return {"buyAmount": "1200", "sellAmount": str(amount)}
    _bsc.setattr(V3, "dex_v3_quote", v3)
    _seed56(tmp_path, "v3", {"tokens": [WBNB, MINT], "fees": [10000]})
    ctx = _Ctx([])
    w = _watch56(tmp_path, ctx)
    w.tick()
    assert seen == [("56", WBNB, MINT, SELL, [([WBNB, MINT], [10000])], True)] and ctx.calls == []


def test_a_56_mixed_winner_is_its_v3_leg_then_its_one_v2_pair(tmp_path, _bsc):
    from sniper.execution import evm_dex_quote as V3
    from sniper.execution import evm_v2_exit_quote as V2
    from sniper.settings import memecoin as cfg
    _bsc.setattr(cfg, "MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", 4.0)
    legs = []
    _bsc.setattr(V3, "dex_v3_quote", lambda chain, sell, buy, amount, taker, **kw: legs.append(
        ("v3", sell, buy, amount, kw.get("extra_paths"), kw.get("paths_only"))) or {
        "buyAmount": "5000", "sellAmount": str(amount)})

    def v2(chain, sell, buy, amount, taker, **kw):
        legs.append(("v2", sell, buy, amount, kw.get("connector_tokens"), kw.get("tax_buffer_bps"),
                     kw["pair_tokens_fn"](chain, buy)))
        return {"buyAmount": "1300", "route": {"tokens": [sell, buy]}}
    _bsc.setattr(V2, "dex_v2_exit_quote", v2)
    _seed56(tmp_path, "mixed_v3_v2", {"base_token": BASE, "v3_tokens": [WBNB, BASE], "v3_fees": [2500]})
    ctx = _Ctx([])
    w = _watch56(tmp_path, ctx)
    w.tick()
    assert legs == [("v3", WBNB, BASE, SELL, [([WBNB, BASE], [2500])], True),
                    ("v2", BASE, MINT, 5000, (), 400, ())]
    assert w.premium["b1"] == pytest.approx((1.6 * BUY0 / 1300 - 1) * 100) and ctx.calls == []


@pytest.mark.parametrize("rung", ["flap_curve", "kyber_free_rung_placeholder"])
def test_a_56_winner_that_reads_no_feed_is_asked_alone_as_today(tmp_path, rung):
    rung = "flap_curve" if rung == "flap_curve" else "relay"
    _seed56(tmp_path, rung, {})
    ctx = _Ctx([1100])
    _watch56(tmp_path, ctx).tick()
    assert ctx.calls == [(WBNB, MINT, SELL, {"only_rung": rung})]


def test_the_56_ladder_tick_never_runs_a_discovery_rung(tmp_path):
    _seed56(tmp_path, "v2", {})                         # a v2 winner with no pin: straight to the ladder
    ctx = _Ctx([1100])
    _watch56(tmp_path, ctx).tick()
    [(_s, _b, _a, kw)] = ctx.calls
    assert set(kw["exclude_rungs"]) >= {"v4", "v4_multihop", "v2", "v3", "v3_bridge", "mixed_v3_v2"}


def test_a_4663_v2_winner_is_still_asked_through_its_rung(tmp_path):
    from test_memecoin_dip_watch_winner import _seed, _watch
    _seed(tmp_path, rung="v2")
    ctx = _Ctx([1100])
    _watch(tmp_path, ctx).tick()
    assert ctx.calls[0][3] == {"only_rung": "v2"}


def test_the_real_v3_pinned_quote_reads_no_pair_list():
    """paths_only skips the pair lookup in dex_v3_quote itself, not only in our fake."""
    from sniper.execution import evm_dex_quote as V3
    out = V3.dex_v3_quote("56", WBNB, MINT, SELL, "0x" + "11" * 20, extra_paths=[([WBNB, MINT], [10000])],
                          paths_only=True, slippage_bps=300,
                          cfg_addr={"router": "0x" + "22" * 20, "quoter": "0x" + "33" * 20},
                          call_fn=lambda to, data: None, fee_tiers=(10000,), connectors=())
    assert out is None                                  # nothing quoted, and no pair list was read


def test_chain_1_ticks_every_3_s_and_every_other_chain_keeps_the_global(monkeypatch, tmp_path):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S", 1.0)
    monkeypatch.delenv("MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S_1", raising=False)
    assert _watch56(tmp_path, None, chain="1")._poll_s() == 3.0
    assert _watch56(tmp_path, None, chain="56")._poll_s() == 1.0
    monkeypatch.setenv("MEMECOIN_REAL_DIP_RETRY_EVM_POLL_S_1", "6")
    assert _watch56(tmp_path, None, chain="1")._poll_s() == 6.0
