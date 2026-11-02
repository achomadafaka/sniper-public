"""The chain-56 V3 bridge-token rung (Alpha, 2026-10-02, operator: "we keep on failing").

PI 0xf8e9...1f74 (attempt 9493dd86, 15:13Z): its real market is a PancakeSwap V3 1% pool against
ZEC, and ZEC has liquid V3 pools against WBNB. The v3 rung priced the thin PI/WBNB 1% pool, read a
-99.86% round trip and declined. dex_v3_quote tries the coin's own pair token only after a FULL
miss, so the ZEC path was never asked; mixed_v3_v2 needs the coin leg on V2. The bridge rung quotes
WBNB -> X -> coin for the coin's pair tokens, measures the round trip on the SAME pools reversed,
and a position it opened exits on its stored bridge, the larger of that path and the direct pool.
"""
import json

import pytest
from eth_abi import decode

from sniper.execution import evm_dex_quote as D

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
ZEC = "0x1ba42e5193dfa8b03d15dd1b86a3113bbbef8eeb"
PI = "0xf8e96697b82fffb055ab64b298305600f5771f74"
TAKER = "0x" + "dd" * 20
DEP = {"router": "0x1b81d678ffb9c0263b24a97847620c99d213eb14",
       "quoter": "0xb048bbc1ee6b733fffcfb9e9cef7375518e25997", "protocol": "pancake_v3"}
TIERS = (10000, 2500, 500, 100)
CLIP = 10 ** 17


def _word(n):
    return "0x" + format(int(n), "064x")


def _call_fn(table):
    """QuoterV2 stand-in: a data -> amountOut table, answered in ONE batch per scan."""
    log = []

    def call(_to, data):
        log.append(("single", data))
        return table.get(data)

    def batch(calls):
        log.append(("batch", len(calls)))
        return [table.get(data) for _to, data in calls]
    call.batch = batch
    return call, log


def _path(tokens, fees, amount=CLIP):
    return D._path_quote_data(D._encode_path(tokens, fees), amount)


def _quote(sell, buy, table, amount=CLIP, **kw):
    call, log = _call_fn(table)
    q = D.dex_v3_quote("56", sell, buy, amount, TAKER, cfg_addr=DEP, call_fn=call,
                       fee_tiers=TIERS, connectors=((USDT, 10000),), slippage_bps=300,
                       now_fn=lambda: 1_790_000_000, **kw)
    return q, log


# --- explicit paths in dex_v3_quote ----------------------------------------------------------------

def test_paths_only_quotes_just_the_given_paths_in_one_batch_and_builds_the_best():
    good, worse = ([WBNB, ZEC, PI], [500, 10000]), ([WBNB, ZEC, PI], [100, 10000])
    table = {_path(*good): _word(9_000), _path(*worse): _word(7_000),
             D._single_quote_data(WBNB, PI, CLIP, 10000): _word(1_000_000)}   # never asked
    q, log = _quote(WBNB, PI, table, extra_paths=[good, worse], paths_only=True)
    assert log == [("batch", 2)]
    assert int(q["buyAmount"]) == 9_000 and int(q["minBuyAmount"]) == 9_000 * 9700 // 10000
    assert q["route"] == {"protocol": "pancake_v3", "tokens": [WBNB, ZEC, PI], "fees": [500, 10000]}
    assert q["transaction"]["to"] == DEP["router"] and q["issues"]["allowance"]["spender"] == DEP["router"]
    data = q["transaction"]["data"]
    assert data.startswith("0x" + D.PANCAKE_EXACT_INPUT)
    path, recipient, _deadline, amount_in, min_out = decode(
        ["(bytes,address,uint256,uint256,uint256)"], bytes.fromhex(data[10:]))[0]
    assert path == D._encode_path(*good) and recipient.lower() == TAKER
    assert (amount_in, min_out) == (CLIP, 9_000 * 9700 // 10000)


def test_extra_paths_ride_the_standard_scan_and_a_thin_direct_pool_loses():
    bridge = ([PI, ZEC, WBNB], [10000, 500])
    table = {D._single_quote_data(PI, WBNB, CLIP, 10000): _word(13),        # the thin PI/WBNB 1%
             _path(*bridge): _word(98 * 10 ** 15)}
    q, log = _quote(PI, WBNB, table, extra_paths=[bridge])
    assert [e for e in log if e[0] == "batch"] == [("batch", len(TIERS) + len(TIERS) + 1)]
    assert q["route"]["tokens"] == [PI, ZEC, WBNB] and int(q["buyAmount"]) == 98 * 10 ** 15


def test_without_extra_paths_the_quote_is_unchanged():
    table = {D._single_quote_data(WBNB, PI, CLIP, 10000): _word(13)}
    q, log = _quote(WBNB, PI, table)
    assert q["route"] == {"protocol": "pancake_v3", "tokens": [WBNB, PI], "fees": [10000]}
    assert log == [("batch", len(TIERS) + len(TIERS))]


def test_paths_only_with_nothing_priced_is_a_plain_miss():
    q, _log = _quote(WBNB, PI, {}, extra_paths=[([WBNB, ZEC, PI], [500, 10000])], paths_only=True,
                     pair_tokens_fn=lambda *_a: pytest.fail("no discovery on a bridge-only scan"))
    assert q is None


# --- which bridges -----------------------------------------------------------------------------------

def test_bridge_paths_take_the_pair_tokens_minus_the_quote_and_connectors():
    pairs = lambda _c, coin: (ZEC, WBNB, USDT) if coin == PI else ()
    buy = D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=pairs, tiers=TIERS,
                            connectors=((USDT, 10000),), allowlist=None)
    assert len(buy) == 16 and {tuple(t) for t, _f in buy} == {(WBNB, ZEC, PI)}
    assert ([WBNB, ZEC, PI], [500, 10000]) in [(list(t), list(f)) for t, f in buy]
    sell = D.v3_bridge_paths("56", PI, WBNB, pair_tokens_fn=pairs, tiers=TIERS,
                             connectors=((USDT, 10000),), allowlist=None)
    assert {tuple(t) for t, _f in sell} == {(PI, ZEC, WBNB)}
    other = "0x" + "ab" * 20
    assert D.v3_bridge_paths("56", ZEC, PI, pair_tokens_fn=lambda *_a: (other,), tiers=TIERS,
                             connectors=(), allowlist=None) == []          # neither end is the quote token
    assert D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=lambda *_a: (WBNB, USDT),
                             tiers=TIERS, connectors=((USDT, 10000),), allowlist=None) == []


def test_a_failing_pair_lookup_is_no_bridge_not_an_error():
    def boom(*_a):
        raise RuntimeError("dexscreener down")
    assert D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=boom, tiers=TIERS, connectors=(),
                             allowlist=None) == []


# --- the rung inside the real ladder -----------------------------------------------------------------

def _ladder(monkeypatch, *, chains=("56",)):
    from sniper.settings import memecoin as cfg
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_CHAINS", frozenset(chains))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ENTRY_MEASURED_LADDER", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False)
    rungs = []

    def thin_v3(sell, buy, amount, slippage_bps=None):
        # PI/WBNB 1%: in, and back out at -99.86%
        out = amount * 14 // 10_000 if sell == PI else amount
        return {"buyAmount": str(out), "sellAmount": str(amount),
                "transaction": {"to": DEP["router"]},
                "route": {"protocol": "pancake_v3", "tokens": [sell, buy], "fees": [10000]}}
    fn = venue_quote_with_v2_fallback(thin_v3, chain="56", quote_token=WBNB, taker=TAKER,
                                      acct=object(), rpc_url="https://rpc", on_rung=rungs.append)
    return fn, rungs


def _fake_bridge_quotes(monkeypatch, *, back=0.97):
    seen = []

    def quote(chain, sell, buy, amount, taker, *, extra_paths=None, paths_only=False, **kw):
        seen.append({"sell": sell, "buy": buy, "amount": amount, "paths": extra_paths,
                     "paths_only": paths_only})
        if not paths_only:
            return None
        tokens, fees = extra_paths[0] if sell == PI else ([WBNB, ZEC, PI], [500, 10000])
        out = amount * 5 if sell == WBNB else int(amount // 5 * back)
        return {"buyAmount": str(out), "sellAmount": str(amount), "minBuyAmount": str(out),
                "transaction": {"to": DEP["router"]},
                "issues": {"allowance": {"spender": DEP["router"]}},
                "route": {"protocol": "pancake_v3", "tokens": list(tokens), "fees": list(fees)}}
    monkeypatch.setattr(D, "dex_v3_quote", quote)
    monkeypatch.setattr(D, "v3_bridge_paths", lambda chain, sell, buy, **_k: [
        ([WBNB, ZEC, PI], [500, 10000]), ([WBNB, ZEC, PI], [2500, 10000])] if sell == WBNB else [])
    return seen


def test_the_ladder_takes_the_bridge_over_the_thin_pool_and_measures_it_on_the_same_pools(monkeypatch):
    seen = _fake_bridge_quotes(monkeypatch)
    fn, rungs = _ladder(monkeypatch)
    q = fn(WBNB, PI, CLIP)
    assert q is not None and q["route"]["tokens"] == [WBNB, ZEC, PI]
    assert q["route"]["bridge"] is True and q["route"]["round_trip_measured_by"] == "v3_bridge"
    assert q["route"]["round_trip_pct"] == pytest.approx(-3.0)
    reverse = [s for s in seen if s["sell"] == PI]
    assert len(reverse) == 1 and reverse[0]["paths_only"] is True
    assert reverse[0]["paths"] == [([PI, ZEC, WBNB], [10000, 500])]     # the SAME pools, reversed
    assert reverse[0]["amount"] == CLIP * 5
    rec = {r["rung"]: r for r in rungs}
    assert rec["v3_bridge"]["hit"] is True
    assert str(rec["v3"].get("reason")).startswith("round_trip_over_budget")


def test_an_over_budget_bridge_is_refused_like_any_rung(monkeypatch):
    _fake_bridge_quotes(monkeypatch, back=0.5)
    fn, rungs = _ladder(monkeypatch)
    assert fn(WBNB, PI, CLIP) is None
    rec = {r["rung"]: r for r in rungs}["v3_bridge"]
    assert rec["hit"] is False and str(rec.get("reason")).startswith("round_trip_over_budget")


def test_no_pair_token_is_a_named_miss(monkeypatch):
    _fake_bridge_quotes(monkeypatch)
    monkeypatch.setattr(D, "v3_bridge_paths", lambda *_a, **_k: [])
    fn, rungs = _ladder(monkeypatch)
    fn(WBNB, PI, CLIP)
    rec = {r["rung"]: r for r in rungs}["v3_bridge"]
    assert rec["hit"] is False and rec.get("reason") == "no_bridge_token"


@pytest.mark.parametrize("chains,sell,buy,record", [((), WBNB, PI, None),
                                                     (("56",), PI, WBNB, "skipped")])
def test_off_by_default_and_never_on_a_sell(monkeypatch, chains, sell, buy, record):
    asked = []
    monkeypatch.setattr(D, "v3_bridge_paths", lambda *a, **_k: asked.append(a) or [])
    fn, rungs = _ladder(monkeypatch, chains=chains)
    fn(sell, buy, CLIP)
    rec = {r["rung"]: r for r in rungs}.get("v3_bridge")
    # Off: no record at all, so every other chain's ladder trace is byte-identical to today's.
    assert (rec is None) if record is None else (rec[record] is True
                                                 and rec["blind_reason"] is None)
    assert asked == []


def test_the_setting_defaults_to_no_chain():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_EVM_V3_BRIDGE_CHAINS == frozenset()


# --- the exit of a position the bridge opened ----------------------------------------------------------

def _executor(monkeypatch, quote):
    from eth_account import Account
    from sniper.execution import evm_real_seams as S, evm_wallet, evm_v2_exit_quote
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *_a, **_k: 10 ** 22)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 600.0)
    monkeypatch.setattr(D, "dex_v3_quote", quote)
    monkeypatch.setattr(evm_v2_exit_quote, "dex_v2_exit_quote", lambda *_a, **_k: None)
    return S.build_real_evm_executor(
        acct=Account.create(), chain_id="56", rpc_url="https://rpc",
        http=lambda *_a, **_k: (200, {}, None), usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)


BRIDGE_POSITION = {"mint": PI, "entry_notional": 60.0, "evm_route_json": json.dumps(
    {"protocol": "pancake_v3", "tokens": [WBNB, ZEC, PI], "fees": [500, 10000], "bridge": True})}


def test_a_bridge_position_exits_on_the_best_of_its_bridge_and_the_direct_pool(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_CHAINS", frozenset())   # switch off: still exits
    seen = []

    def quote(chain, sell, buy, amount, taker, *, extra_paths=None, paths_only=False, **kw):
        seen.append({"sell": sell, "paths": extra_paths, "paths_only": paths_only})
        return {"buyAmount": str(97 * 10 ** 15), "transaction": {"to": "0xbridge"},
                "route": {"protocol": "pancake_v3", "tokens": [PI, ZEC, WBNB]}}
    preview = _executor(monkeypatch, quote)["preview_exit_fn"](BRIDGE_POSITION)
    assert preview.route_id == "0xbridge"
    first = seen[0]
    assert first["sell"] == PI and first["paths_only"] is False      # direct + connectors too
    assert {tuple(t) for t, _f in first["paths"]} == {(PI, ZEC, WBNB)}
    assert ([PI, ZEC, WBNB], [10000, 500]) in [(list(t), list(f)) for t, f in first["paths"]]


def test_if_the_bridge_exit_fails_the_exit_falls_through_to_the_ladder(monkeypatch):
    calls = []

    def quote(chain, sell, buy, amount, taker, *, extra_paths=None, paths_only=False, **kw):
        calls.append(extra_paths)
        if extra_paths:
            raise D.V3ReadUnavailable("rpc_transport", "boom")
        return {"buyAmount": str(10 ** 15), "transaction": {"to": "0xv3dust"}}
    preview = _executor(monkeypatch, quote)["preview_exit_fn"](BRIDGE_POSITION)
    assert preview.route_id == "0xv3dust" and calls[0]


def test_a_cut_bridge_rung_counts_as_an_on_chain_rung_when_an_aggregator_wins():
    from sniper.execution.evm_swap_executor import _ONCHAIN_RUNGS
    assert "v3_bridge" in _ONCHAIN_RUNGS       # it reads pools at the latest block, like v3


# --- Gamma MED: only vetted bridge tokens ---------------------------------------------------------

def test_only_allowlisted_bridge_tokens_are_tried_and_the_rest_are_named():
    junk = "0x" + "ee" * 20
    pairs = lambda _c, coin: (junk, ZEC)
    rejected = []
    paths = D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=pairs, tiers=TIERS, connectors=(),
                              allowlist=frozenset({ZEC}), rejected=rejected)
    assert {tuple(t) for t, _f in paths} == {(WBNB, ZEC, PI)} and rejected == [junk]
    rejected = []
    assert D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=pairs, tiers=TIERS, connectors=(),
                             allowlist=frozenset(), rejected=rejected) == []
    assert rejected == [junk, ZEC]


def test_the_default_allowlist_is_the_settings_constant(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_TOKENS", {"56": frozenset({ZEC})})
    pairs = lambda _c, coin: (ZEC, "0x" + "ee" * 20)
    paths = D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=pairs, tiers=TIERS, connectors=())
    assert {tuple(t) for t, _f in paths} == {(WBNB, ZEC, PI)}
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_TOKENS", {})
    assert D.v3_bridge_paths("56", WBNB, PI, pair_tokens_fn=pairs, tiers=TIERS, connectors=()) == []


def test_a_bridge_token_off_the_list_is_a_counted_miss(monkeypatch):
    _fake_bridge_quotes(monkeypatch)
    junk = "0x" + "ee" * 20

    def paths(chain, sell, buy, *, rejected=None, **_k):
        rejected.append(junk)
        return []
    monkeypatch.setattr(D, "v3_bridge_paths", paths)
    fn, rungs = _ladder(monkeypatch)
    fn(WBNB, PI, CLIP)
    rec = {r["rung"]: r for r in rungs}["v3_bridge"]
    assert rec["hit"] is False and rec.get("reason") == "bridge_x_not_allowlisted:" + junk


def test_every_allowlisted_bridge_token_is_well_formed_and_never_the_quote_token():
    from sniper.settings import memecoin as cfg
    for chain, tokens in cfg.MEMECOIN_EVM_V3_BRIDGE_TOKENS.items():
        for x in tokens:
            assert x == x.lower() and x.startswith("0x") and len(x) == 42, x
            assert x != WBNB


def test_a_held_bridge_position_exits_on_its_stored_token_even_after_a_de_list(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V3_BRIDGE_TOKENS", {"56": frozenset()})
    seen = []

    def quote(chain, sell, buy, amount, taker, *, extra_paths=None, paths_only=False, **kw):
        seen.append(extra_paths)
        return {"buyAmount": str(97 * 10 ** 15), "transaction": {"to": "0xbridge"}}
    preview = _executor(monkeypatch, quote)["preview_exit_fn"](BRIDGE_POSITION)
    assert preview.route_id == "0xbridge" and seen[0]
    assert {tuple(t) for t, _f in seen[0]} == {(PI, ZEC, WBNB)}


def test_pi_s_bridge_token_is_on_the_list():
    from sniper.settings import memecoin as cfg
    assert ZEC in cfg.memecoin_evm_v3_bridge_tokens("56")
    stock_tokens = {"0x4902c5ebc598265ed2212b559b042de8a5eeec3f",      # BNCB
                    "0x205812cdbed920aff76c6580abd681a46d11efc7",      # QQQB
                    "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1"}      # FXIon
    assert not stock_tokens & cfg.memecoin_evm_v3_bridge_tokens("56")  # issuers can pause
    assert cfg.memecoin_evm_v3_bridge_tokens("8453") == frozenset()
