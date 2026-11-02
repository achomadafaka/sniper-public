"""The EVM post-trade chase reference (spec 2026-09-28-memecoin-evm-chase-post-trade-reference-design.md).

Every receipt below is a REAL frame: tests/fixtures/memecoin/evm_post_trade_receipts.json (fetched
2026-09-28 from the research hosts; V4 PoolKeys from memecoin_evm_v4_pool_keys) and
tests/fixtures/relay_purchases_2026-09-28.json.gz. The expected ratios are NOT recomputed with the
decoder's own formula: V4/V3 legs are checked against the concentrated-liquidity identity (the
average price of a swap inside one range is the geometric mean of the prices before and after, and
the price before is recovered from the event's liquidity), V2 legs against the base-side
constant-product ratio. A wrong word, a flipped coin side or a wrong sign breaks those identities."""
import copy
import gzip
import json
import threading
import time
from concurrent.futures import Future
from decimal import Decimal, getcontext
from pathlib import Path

import pytest
from eth_utils import keccak

from sniper.services import memecoin_evm_post_trade as pt

getcontext().prec = 80
HERE = Path(__file__).parent
FIX = json.load(open(HERE / "fixtures" / "memecoin" / "evm_post_trade_receipts.json"))
RELAY = json.load(gzip.open(HERE / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
CASES = FIX["cases"]
Q96 = Decimal(2) ** 96
V4 = "0x" + keccak(b"Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)").hex()
V3 = "0x" + keccak(b"Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
V2 = "0x" + keccak(b"Swap(address,uint256,uint256,uint256,uint256,address)").hex()
SYNC = "0x" + keccak(b"Sync(uint112,uint112)").hex()
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _keys(chain, pool):
    return FIX["v4_keys"].get("%s:%s" % (chain, str(pool).lower()))


def _signed(word):
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def _words(log):
    d = log["data"][2:]
    return [d[i:i + 64] for i in range(0, len(d), 64)]


def _owner(case):
    """The J7 trader, else (an on-chain-lane fill) whoever received the last mint Transfer."""
    if case["trader"]:
        return case["trader"]
    last = [log for log in case["receipt"]["logs"] if log["topics"] and log["topics"][0] == TRANSFER
            and log["address"].lower() == case["mint"]][-1]
    return "0x" + last["topics"][2][-40:]


def _swaps(receipt, topic, pool):
    return [(i, log) for i, log in enumerate(receipt["logs"])
            if log["topics"] and log["topics"][0] == topic
            and (log["topics"][1].lower() if topic == V4 else log["address"].lower()) == pool]


def _cl_ratio(a_in, sqrt_post, liquidity, fee_pips, coin_is_token1):
    """post/average for a buy inside one tick range, from the input amount, the post price and the
    range's liquidity only. The fee is taken from the input, so the average carries it."""
    s_post = Decimal(sqrt_post) / Q96
    keep = 1 - Decimal(fee_pips) / Decimal(10 ** 6)
    moved = Decimal(a_in) * keep / Decimal(liquidity)
    if coin_is_token1:                      # token0 in: 1/sqrtP rises by moved
        return float(keep * (1 / s_post) / (1 / s_post - moved))
    return float(keep * s_post / (s_post - moved))       # token1 in: sqrtP rises by moved


def _coin_price(sqrt_price, coin_is_token1):
    p = (Decimal(sqrt_price) / Q96) ** 2
    return 1 / p if coin_is_token1 else p


# --------------------------------------------------------------------------------- the decoder

@pytest.mark.parametrize("name", ["v4_hooked_usdg", "v4_hooked_eth", "v4_fill_66620", "v4_fill_66549"])
def test_a_v4_buy_matches_the_concentrated_liquidity_identity(name):
    case = CASES[name]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r.get("venue") == "v4", r
    swaps = _swaps(case["receipt"], V4, r["pool"])
    assert len(swaps) == 1
    w = _words(swaps[0][1])
    key = _keys("4663", r["pool"])
    coin_is_token1 = key["currency1"].lower() == case["mint"]
    a_in = abs(_signed(w[0] if coin_is_token1 else w[1]))
    expected = _cl_ratio(a_in, int(w[2], 16), int(w[3], 16), int(w[5], 16), coin_is_token1)
    assert r["ratio"] == pytest.approx(expected, rel=1e-6)
    assert 1.0 < r["ratio"] < 1.2


@pytest.mark.parametrize("name,route_pool", [
    ("v4_fill_66620", "0xd42db345a4bd4df194902102fddc4edbb46f6b63c805cd242b359884d3e86e88"),
    ("v4_fill_66549", "0x5394735ca0f081b9e17cdaf0b4c492daed942a3494f5cad2c5379fe341b81109")])
def test_the_source_pool_is_the_pool_our_filled_route_named(name, route_pool):
    """Real fills 66620 and 66549: our quote's route pool_id (memecoin_positions.evm_route_json) is
    the pool the source's own receipt traded, so the binding can match in production."""
    case = CASES[name]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r["pool"] == route_pool


def test_a_hook_that_sells_its_fee_after_the_buy_prices_the_final_state():
    """0xf7320fb9: the buy, then the launchpad hook sells its coin fee into the same pool twice. The
    reference is the price the NEXT buyer faces: the last swap's, lower than right after the buy."""
    case = CASES["v4_hook_sells_after_buy"]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    swaps = _swaps(case["receipt"], V4, r["pool"])
    assert len(swaps) == 3
    key = _keys("4663", r["pool"])
    coin_is_token1 = key["currency1"].lower() == case["mint"]
    assert not coin_is_token1
    buy = _words(swaps[0][1])
    immediate = _cl_ratio(abs(_signed(buy[1])), int(buy[2], 16), int(buy[3], 16), int(buy[5], 16), False)
    last = _words(swaps[-1][1])
    drift = float(_coin_price(int(last[2], 16), False) / _coin_price(int(buy[2], 16), False))
    assert r["ratio"] == pytest.approx(immediate * drift, rel=1e-6)
    assert r["ratio"] < immediate


def test_a_bridge_pool_without_the_coin_is_not_the_leg():
    """0x501a6002 routes V3 -> a V4 bridge pool that does not hold the coin -> the coin's V4 pool."""
    case = CASES["v4_multihop_bridge"]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r["venue"] == "v4"
    assert r["pool"].startswith("0xf40edb0e1448")


@pytest.mark.parametrize("name", ["v2_56_fill_66521", "v2_56_taxed_refusal", "v2_56_fill"])
def test_a_v2_buy_sits_inside_the_base_side_constant_product_bound(name):
    case = CASES[name]
    r = pt.source_post_trade("56", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r.get("venue") == "v2", r
    pair = r["pool"]
    swap = _swaps(case["receipt"], V2, pair)
    assert len(swap) == 1
    amounts = [int(x, 16) for x in _words(swap[0][1])[:4]]
    sync = [int(x, 16) for x in _words(_swaps(case["receipt"], SYNC, pair)[-1][1])[:2]]
    coin = 0 if amounts[2] > 0 else 1
    base_side = (sync[coin] + amounts[2 + coin]) / sync[coin]     # B_pre / B_post, no quote leg
    assert 0.9975 * base_side <= r["ratio"] <= base_side        # PancakeSwap V2 takes 0.25%
    assert r["ratio"] > 1.0


def test_a_v3_leg_on_8453_matches_one_fee_tier():
    b = RELAY["buys"][11]                                          # prevaile, V3 bridge -> V3 coin pool
    r = pt.source_post_trade("8453", b["receipt"], b["mint"], owner=b["wallet"], pool_key_fn=_keys)
    assert r.get("venue") == "v3", r
    (_, log), = _swaps(b["receipt"], V3, r["pool"])
    w = _words(log)
    a0, a1 = _signed(w[0]), _signed(w[1])
    coin_is_token1 = a1 < 0                                          # pool perspective: out is negative
    a_in = a0 if coin_is_token1 else a1
    hits = [fee for fee in (100, 500, 2500, 3000, 10000)
            if r["ratio"] == pytest.approx(_cl_ratio(a_in, int(w[2], 16), int(w[3], 16), fee,
                                                     coin_is_token1), rel=1e-6)]
    assert len(hits) == 1, r


def test_a_curve_buy_has_no_ratio():
    case = CASES["curve_no_swap"]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r['reason'] == 'pons_post_state_unavailable' and r['venue'] == 'pons_v2'
    assert 'ratio' not in r and 'sqrt_price_x96' not in r


@pytest.mark.parametrize("index", [2, 4])
def test_a_split_route_gives_no_ratio(index):
    """CryoPalmar (two V4 pools of the coin) and Morgoth11 (two V4 pools and a V3 pool): two legs
    paid the coin, so no single pool's price is the source's."""
    b = RELAY["buys"][index]
    r = pt.source_post_trade(b["chain"], b["receipt"], b["mint"], owner=b["wallet"], pool_key_fn=_keys)
    assert r['reason'] == 'split_route' and 'ratio' not in r and 'pool' not in r


def test_the_leg_must_deliver_what_the_owner_received():
    case = CASES["v2_56_taxed_refusal"]
    tax_wallet = next("0x" + log["topics"][2][-40:] for log in case["receipt"]["logs"]
                      if log["topics"] and log["topics"][0] == TRANSFER
                      and log["topics"][2][-40:].startswith("3a91c7"))
    r = pt.source_post_trade("56", case["receipt"], case["mint"], owner=tax_wallet, pool_key_fn=_keys)
    assert r == {"reason": "leg_does_not_bind"}          # the 1% tax wallet did not buy 100x its take
    r = pt.source_post_trade("56", case["receipt"], case["mint"], owner="0x" + "ab" * 20, pool_key_fn=_keys)
    assert r == {"reason": "owner_received_nothing"}


def test_an_unknown_v4_key_gives_no_ratio():
    case = CASES["v4_hooked_usdg"]
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case),
                             pool_key_fn=lambda chain, pool: None)
    assert r == {"reason": "v4_key_unknown"}


def test_a_reverted_receipt_gives_no_ratio():
    case = copy.deepcopy(CASES["v4_hooked_usdg"])
    case["receipt"]["status"] = "0x0"
    r = pt.source_post_trade("4663", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r == {"reason": "reverted"}


def test_a_broken_receipt_never_raises():
    assert pt.source_post_trade("4663", {"status": "0x1", "logs": [{"topics": [V4], "data": "0x12"}]},
                                "0x" + "11" * 20, owner=None, pool_key_fn=_keys) == {"reason": "no_venue_leg"}
    assert pt.source_post_trade("4663", None, "0x" + "11" * 20) == {"reason": "no_receipt"}


# --------------------------------------------------------------------------------- our route

@pytest.mark.parametrize("route,pool", [
    # real 56 fills: our route (evm_route_json) and the pair our OWN receipt traded (executed_pool)
    ({"protocol": "uniswap_v2", "router": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
      "tokens": ["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x4902c5ebc598265ed2212b559b042de8a5eeec3f",
                 "0x8e7307bee5200a0d6ae4dde971383db19b277777"]},
     "0xb8cf4b21b07bf725082edd3254550ef0ead9a025"),                                 # position 66596
    ({"protocol": "uniswap_v2", "router": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
      "tokens": ["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x3074bb17fc920369f16fe0fca16237612e48bbbb"]},
     "0x7e3a61a9f88254e9ac0b8d03bca122cd471a8dcd"),                                 # position 66594
    ({"protocol": "uniswap_v2", "router": "0x10ed43c718714eb63d5aa57b78b54704e256024e",
      "tokens": ["0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1",
                 "0x5074546cb787d5a698ec8e9a1734e33a3fae7777"]},
     "0xd5f4fd64733321a5118e6109764e2a74c04ad85d"),                                 # position 66521
    ({"protocol": "uniswap_v4", "pool_id": "0xD42DB345A4BD4DF194902102FDDC4EDBB46F6B63C805CD242B359884D3E86E88"},
     "0xd42db345a4bd4df194902102fddc4edbb46f6b63c805cd242b359884d3e86e88")])
def test_route_pool_names_the_pool_our_quote_trades(route, pool):
    assert pt.route_pool("56" if route["protocol"] == "uniswap_v2" else "4663", route) == pool


def test_a_multihop_route_is_bound_by_its_coin_leg_key():
    pool = "0xd42db345a4bd4df194902102fddc4edbb46f6b63c805cd242b359884d3e86e88"
    key = _keys("4663", pool)
    route = {"protocol": "uniswap_v4_multihop", "coin_key": key, "bridge_key": {"currency0": "0x" + "00" * 20}}
    assert pt.route_pool("4663", route) == pool


@pytest.mark.parametrize("route", [
    {"protocol": "relay_same_chain", "request_id": "x"}, {"protocol": "kyberswap", "hops": 2},
    {"protocol": "uniswap_v3", "tokens": ["0xa", "0xb"], "fees": [3000]}, {"protocol": "pons_v2", "curve": "0xc"},
    {"protocol": "uniswap_v2", "router": "0x" + "99" * 20, "tokens": ["0x" + "01" * 20, "0x" + "02" * 20]},
    None, {}])
def test_a_route_without_one_known_pool_names_none(route):
    assert pt.route_pool("56", route) is None


# --------------------------------------------------------------------------------- the reference

POST = {"ratio": 1.25, "pool": "0xpool", "venue": "v4"}


@pytest.fixture
def post_trade_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_CHASE_POST_TRADE_MAX_RATIO", 2.0, raising=False)
    monkeypatch.setenv("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", "4663,56")


def test_a_bound_pool_gives_the_post_trade_price(post_trade_on):
    price, basis, note = pt.resolve("4663", avg_price=2.0, post=POST, our_pool="0xPOOL")
    assert (price, basis) == (2.5, "post_trade")
    assert "v4" in note and "0xpool" in note


@pytest.mark.parametrize("our_pool,why", [("0xother", "pool_mismatch"), (None, "route_pool_unknown")])
def test_a_price_from_a_pool_we_will_not_trade_is_never_used(post_trade_on, our_pool, why):
    price, basis, note = pt.resolve("4663", avg_price=2.0, post=POST, our_pool=our_pool)
    assert (price, basis) == (2.0, "avg_fill") and note.startswith(why)


def test_the_reference_is_clamped_like_solana(post_trade_on):
    price, basis, note = pt.resolve("56", avg_price=2.0, post=dict(POST, ratio=3.0), our_pool="0xpool")
    assert (price, basis) == (4.0, "post_trade_clamped")
    assert "2.0x" in note


@pytest.mark.parametrize("post,why", [({"reason": "split_route"}, "no_ratio split_route"),
                                      (dict(POST, ratio=0.5), "no_ratio ratio_out_of_range"),
                                      (dict(POST, ratio=float("inf")), "no_ratio ratio_out_of_range"),
                                      (None, "no_ratio none")])
def test_no_ratio_keeps_the_average(post_trade_on, post, why):
    assert pt.resolve("4663", avg_price=2.0, post=post, our_pool="0xpool") == (2.0, "avg_fill", why)


def test_a_chain_not_listed_or_the_global_knob_off_keeps_the_average(post_trade_on, monkeypatch):
    assert pt.resolve("8453", avg_price=2.0, post=POST, our_pool="0xpool") == (2.0, "avg_fill", None)
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "avg_fill")
    assert pt.resolve("4663", avg_price=2.0, post=POST, our_pool="0xpool") == (2.0, "avg_fill", None)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade")
    monkeypatch.setenv("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", "")
    assert pt.resolve("4663", avg_price=2.0, post=POST, our_pool="0xpool") == (2.0, "avg_fill", None)


def test_a_missing_average_is_never_replaced(post_trade_on):
    assert pt.resolve("4663", avg_price=None, post=POST, our_pool="0xpool") == (None, "avg_fill", None)


def test_the_j7_read_is_peeked_never_joined(post_trade_on):
    """Alpha's condition 1: a receipt read that has not finished is receipt_pending, at once."""
    ref = pt.EvmChaseReference("4663", future=Future())             # never completes
    started = time.perf_counter()
    got = ref.resolve(2.0, "0xpool")
    assert time.perf_counter() - started < 0.05
    assert got == (2.0, "avg_fill", "receipt_pending")
    done = Future()
    done.set_result(POST)
    assert pt.EvmChaseReference("4663", future=done).resolve(2.0, "0xpool")[:2] == (2.5, "post_trade")
    failed = Future()
    failed.set_exception(RuntimeError("boom"))
    assert pt.EvmChaseReference("4663", future=failed).resolve(2.0, "0xpool") == (
        2.0, "avg_fill", "no_ratio receipt_read_failed")


def test_the_j7_receipt_read_decodes_on_the_pool():
    case = CASES["v4_hooked_usdg"]
    calls = []

    def read(method, params):
        calls.append((method, params))
        return case["receipt"]
    fut = pt.submit_receipt_reference("4663", case["tx"], case["mint"], case["trader"], read_fn=read,
                                      pool_key_fn=_keys)
    r = fut.result(timeout=5)
    assert r["venue"] == "v4" and calls == [("eth_getTransactionReceipt", [case["tx"]])]
    assert pt.submit_receipt_reference("4663", case["tx"], case["mint"], case["trader"],
                                       read_fn=lambda m, p: None, pool_key_fn=_keys).result(5)["reason"] == "receipt_null"

    def boom(method, params):
        raise TimeoutError("slow")
    assert pt.submit_receipt_reference("4663", case["tx"], case["mint"], case["trader"],
                                       read_fn=boom, pool_key_fn=_keys).result(5)["reason"] == "receipt_read_failed"


def test_the_j7_tx_comes_from_the_raybot_origin():
    tx = "0x" + "ab" * 32
    trader, mint = "0x" + "cd" * 20, "0x" + "ef" * 20
    assert pt.raybot_origin("swap:raybot:%s:%s:%s" % (tx, trader, mint)) == (tx, trader, mint)
    assert pt.raybot_origin("onchain:4663:%s" % tx) is None
    assert pt.raybot_origin("swap:raybot:notatx:%s:%s" % (trader, mint)) is None
    assert pt.raybot_origin(None) is None


def _observation_receipt(**changes):
    receipt = {"transactionHash": "0x" + "a9" * 32, "status": "0x1",
               "blockNumber": "0x7913546", "blockHash": "0x" + "bc" * 32, "logs": []}
    receipt.update(changes)
    return receipt


def _read_observation(receipt):
    return pt.submit_receipt_reference("56", "0x" + "a9" * 32, "0x" + "cd" * 20,
                                       "0x" + "ef" * 20, read_fn=lambda *_: receipt).result(5)


def test_receipt_observation_survives_a_venue_decode_refusal():
    result = _read_observation(_observation_receipt())
    assert result["reason"] == "no_venue_leg"
    assert result.get("source_receipt_observation") == {
        "network_id": "56", "source_tx": "0x" + "a9" * 32,
        "source_block_number": 126956870, "source_block_hash": "0x" + "bc" * 32}
    assert "source_evidence" not in result and "pool" not in result


def test_receipt_observation_keeps_a_block_without_an_optional_hash():
    receipt = _observation_receipt()
    receipt.pop("blockHash")
    result = _read_observation(receipt)
    assert result.get("source_receipt_observation") == {
        "network_id": "56", "source_tx": "0x" + "a9" * 32, "source_block_number": 126956870}


@pytest.mark.parametrize("changes", [
    {"transactionHash": "0x" + "ab" * 32}, {"transactionHash": None},
    {"status": "0x0"}, {"blockNumber": None}, {"blockNumber": True},
    {"blockNumber": 0}, {"blockNumber": -1}, {"blockNumber": 1.5},
    {"blockNumber": "0xno"}, {"blockHash": "bad"}, {"logs": None},
    {"logs": [{"removed": True}]},
    {"logs": [{"transactionHash": "0x" + "ab" * 32}]},
    {"logs": [{"blockNumber": "0x64"}]},
    {"logs": [{"blockHash": "0x" + "ab" * 32}]},
])
def test_receipt_observation_rejects_partial_wrong_or_removed_receipts(changes):
    assert _read_observation(_observation_receipt(**changes)).get("source_receipt_observation") is None


def test_receipt_observation_pending_is_unknown():
    pending = Future()
    assert pt.EvmChaseReference("56", future=pending).payload() is None
    assert _read_observation(None).get("source_receipt_observation") is None


def test_receipt_observation_oversized_rpc_integer_is_unknown_without_a_future_error():
    result = _read_observation(_observation_receipt(blockNumber="9" * 4500))
    assert result.get("source_receipt_observation") is None


# --------------------------------------------------------------------------------- the executor
# The harness is test_memecoin_evm_entry_executor's (pure seams, in-memory ledger). Prices: the
# quote sells 10 USD (6 decimals) for buyAmount tokens (6 decimals), so price = 10e6 / buyAmount.

POOL_X = "0x" + "a1" * 32
POOL_Y = "0x" + "b2" * 32


def _entry(monkeypatch, *, reference, route_pool, fresh_buy=None, initial_buy="7352941", chase=30.0,
           chains="4663"):
    from sniper.execution.evm_swap_executor import execute_entry_evm
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    from test_memecoin_evm_entry_executor import _QUOTE, _TraceRecorder, _c, _seams
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_POST_APPROVAL_WINNER_REQUOTE", fresh_buy is not None, raising=False)
    monkeypatch.setenv("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", chains)
    monkeypatch.setenv("MEMECOIN_REAL_QUOTE_OVER_SOURCE_MAX_PCT_4663", "30")
    c = _c()
    pid = m.reserve_pending(c, mint="0xtok", source_user_id="trader1", strategy="alpha_b",
                            notional=10.0, reserved_at="2026-07-09T00:00:00Z", network_id="4663",
                            source_price=1.0)
    route = {"protocol": "uniswap_v4", "pool_id": route_pool}
    initial = {**_QUOTE, "buyAmount": initial_buy, "route": route, "_sniper_winner": "v4"}
    if fresh_buy is not None:
        fresh = {**_QUOTE, "buyAmount": fresh_buy, "route": route, "_sniper_winner": "v4"}
        initial["_requote"] = lambda slippage_bps=None: fresh
    seams, calls = _seams(quote=initial, token=((0.0, 6), (10.0, 6)))
    trace = _TraceRecorder()
    kw = {} if reference is None else {"chase_reference": reference}
    result = execute_entry_evm(c, pending_id=pid, mint="0xtok", network_id="4663", usd_amount=10.0,
                               strategy="alpha_b", source_user_id="trader1",
                               now_iso="2026-07-09T00:00:00Z", chase_max_pct=chase, trace=trace,
                               **seams, **kw)
    marks = [md for name, md in trace.substages if name == "chase_quote_confirmation"]
    _entry.substages = [name for name, _md in trace.substages]
    return result, calls, marks


def _ref(ratio=1.2, pool=POOL_X):
    return pt.EvmChaseReference("4663", post={"ratio": ratio, "pool": pool, "venue": "v4"})


def test_the_quote_over_source_cap_judges_the_post_trade_price_on_the_bound_pool(monkeypatch):
    """Quote +36% over the source's average (cap 30): refused today; on the pool the source traded,
    +13% over its post-trade price, admitted."""
    result, calls, marks = _entry(monkeypatch, reference=None, route_pool=POOL_X)
    assert result["status"] == "skipped_chase" and "above source price" in result["reason"]
    result, calls, marks = _entry(monkeypatch, reference=_ref(), route_pool=POOL_X)
    assert result["status"] == "filled", result
    assert marks[0]["reference_basis"] == "post_trade"
    assert marks[0]["reference_price"] == pytest.approx(1.2)
    assert marks[0]["source_price"] == pytest.approx(1.0)
    assert "v4 pool " + POOL_X in marks[0]["reference_note"]


def test_a_quote_on_another_pool_keeps_the_average_and_says_why(monkeypatch):
    result, calls, marks = _entry(monkeypatch, reference=_ref(), route_pool=POOL_Y)
    assert result["status"] == "skipped_chase" and calls["sign"] == 0
    assert marks[0]["reference_basis"] == "avg_fill"
    assert marks[0]["reference_note"].startswith("pool_mismatch")
    assert "avg_fill" in result["reason"]


def test_a_pending_j7_read_never_blocks_the_gate(monkeypatch):
    """Alpha's condition 1, through the executor: the Future never completes, the entry decides at
    once on the average."""
    started = time.perf_counter()
    result, calls, marks = _entry(monkeypatch, reference=pt.EvmChaseReference("4663", future=Future()),
                                  route_pool=POOL_X)
    assert time.perf_counter() - started < 2.0
    assert result["status"] == "skipped_chase"
    assert (marks[0]["reference_basis"], marks[0]["reference_note"]) == ("avg_fill", "receipt_pending")


def test_the_post_approval_requote_is_bound_to_its_own_route(monkeypatch):
    """Initial quote at the source price; the refreshed one +36% (cap 30). On the bound pool the
    post-trade price admits it; without a reference it is refused exactly as today."""
    result, calls, _ = _entry(monkeypatch, reference=None, route_pool=POOL_X, initial_buy="10000000",
                              fresh_buy="7352941")
    assert result["status"] == "skipped_chase" and calls["sign"] == 0
    result, calls, _ = _entry(monkeypatch, reference=_ref(), route_pool=POOL_X, initial_buy="10000000",
                              fresh_buy="7352941")
    assert result["status"] == "filled", result


def test_no_reference_is_byte_identical_to_today(monkeypatch):
    result, calls, marks = _entry(monkeypatch, reference=None, route_pool=POOL_X, initial_buy="10000000")
    assert result["status"] == "filled"
    assert not any(k.startswith("reference_") for k in marks[0])


@pytest.mark.parametrize("fresh_buy,initial_buy", [(None, "7352941"), ("7352941", "10000000")])
def test_the_knob_off_is_byte_identical_even_with_a_reference(monkeypatch, fresh_buy, initial_buy):
    """Beta F1: an on-chain-lane EVM row carries evm_post whatever the knob says. With the chain not
    listed, refusal strings, the chase substage and the substage sequence are today's exactly."""
    base = _entry(monkeypatch, reference=None, route_pool=POOL_X, fresh_buy=fresh_buy,
                  initial_buy=initial_buy, chains="")
    base_seq = _entry.substages
    got = _entry(monkeypatch, reference=_ref(), route_pool=POOL_X, fresh_buy=fresh_buy,
                 initial_buy=initial_buy, chains="")
    assert got[0] == base[0] and got[2] == base[2] and _entry.substages == base_seq
    assert "reference" not in got[0]["reason"]


# --------------------------------------------------------------------------------- the runner and the lane

def test_the_pre_quote_chase_takes_an_explicit_reference():
    from types import SimpleNamespace
    from sniper.services.memecoin_real_runner import chase_gap_pct, chase_ok
    ev = SimpleNamespace(their_price=1.0, chase_reference_price=None)
    assert chase_gap_pct(ev, our_price=1.36) == pytest.approx(36.0)
    assert chase_gap_pct(ev, our_price=1.36, reference=1.2) == pytest.approx(13.333333)
    assert not chase_ok(ev, our_price=1.36, max_pct=30)
    assert chase_ok(ev, our_price=1.36, max_pct=30, reference=1.2)


def test_the_pre_quote_leg_binds_to_the_display_pair(post_trade_on):
    from types import SimpleNamespace
    ev = SimpleNamespace(their_price=2.0, network_id="4663",
                         evm_chase_reference=pt.EvmChaseReference("4663", post=POST))
    assert pt.pre_quote_reference(ev, {"pair_address": "0xPOOL"})[:2] == (2.5, "post_trade")
    assert pt.pre_quote_reference(ev, {"pair_address": "0xother"})[:2] == (2.0, "avg_fill")
    assert pt.pre_quote_reference(ev, {})[:2] == (2.0, "avg_fill")
    assert pt.pre_quote_reference(SimpleNamespace(their_price=2.0, network_id="4663"), {"pair_address": "0xpool"}) \
        == (2.0, "avg_fill", None)


def _row(network, valuation):
    return {"mint": "0x" + "11" * 20, "source_user_id": "u1", "handle": "h", "network_id": network,
            "source_size_usd": 100.0, "source_price": 2.0, "signature": "%s:0xtx:0xm" % network,
            "detected_at": "2026-09-28T20:00:00+00:00", "block_time": None,
            "source_valuation_json": json.dumps(valuation)}


def test_the_onchain_lane_carries_the_watchers_evm_facts_without_a_chain_agnostic_price(post_trade_on):
    from sniper.services.memecoin_onchain_lane import OnchainEv, event_from_retry_payload, retry_payload
    ev = OnchainEv(_row("4663", {"basis": "evm_onchain_watch", "evm_post": POST}))
    assert ev.chase_reference_price is None, "an EVM row never feeds the Solana reference"
    assert ev.evm_chase_reference.resolve(2.0, "0xpool")[:2] == (2.5, "post_trade")
    again = event_from_retry_payload(retry_payload(ev))
    assert again.evm_chase_reference.resolve(2.0, "0xpool")[:2] == (2.5, "post_trade")
    sol = OnchainEv(_row("solana", {"source_post_price": 3.0}))
    assert getattr(sol, "evm_chase_reference", None) is None


def test_the_watcher_records_the_post_trade_facts_of_a_real_relay_buy(monkeypatch):
    """A real 8453 Relay buy (relay_purchases fixture #11, prevaile, V3 coin pool) through the
    watcher's own decode: the detection's valuation carries the leg."""
    import scripts.research.onchain_evm_wallet_watch as w
    from eth_abi import decode
    from sniper.services import memecoin_evm_onchain_watch as M
    b = RELAY["buys"][11]

    def read(method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_getCode":
            return RELAY["code_8453"].get(params[0], "0xdeadbeef")
        if method == "eth_getTransactionByHash":
            return b["tx"]
        if method == "eth_getTransactionReceipt":
            return b["receipt"]
        raise AssertionError(method)
    w._configure("8453")
    try:
        log = next(lg for lg in b["receipt"]["logs"] if lg["topics"] and lg["topics"][0] == TRANSFER
                   and lg["address"].lower() == b["mint"] and lg["topics"][2][-40:] == b["wallet"][2:])
        payment = decode(M.PERMIT2_TYPES, bytes.fromhex(b["tx"]["input"][10:]))[1][0][0][0].lower()
        outcome = {}
        row = w.push_detection("http://unused", log, {b["wallet"]: ("uid", b["handle"])},
                               decimals_cache={b["mint"]: 18, payment: 18}, quote_usd=600.0,
                               now="2026-09-28T06:30:00+00:00", read_fn=read, outcome=outcome,
                               payment_price_fn=lambda token: 1.0 if token == payment else None)
    finally:
        w._configure("4663")
    assert row is not None
    assert outcome["evm_post"]["venue"] == "v3" and outcome["evm_post"]["ratio"] > 1.0


def test_the_watcher_reads_pool_keys_through_its_own_connection(tmp_path):
    """No new connection per detection: a cold read-only open cost 3.1 ms (p50, 14 pools) on the
    detection path; the watcher already holds a ledger connection."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS
    conn = sqlite3.connect(tmp_path / "ledger.db")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    # a pool id that exists in NO real ledger: only a read through `conn` can find it
    pool = "0x" + "5a" * 32
    key = FIX["v4_keys"][next(k for k in FIX["v4_keys"] if k.startswith("4663:"))]
    m.put_evm_v4_pool_key(conn, chain_id="4663", pool_id=pool, pool_manager=V4_POOL_MANAGERS["4663"], key=key)
    conn.commit()
    pt._KEY_CACHE.clear()
    fn = pt.conn_v4_key_fn(conn)
    assert fn("4663", pool) == key
    assert fn("4663", "0x" + "00" * 32) is None
    conn.close()
    assert fn("4663", pool) == key, "served from the cache once read"
    pt._KEY_CACHE.clear()


def test_no_dedicated_read_host_means_no_j7_read(monkeypatch, post_trade_on):
    """Beta F2: a chain without MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain> never reads the funded RPC;
    the J7 event carries no reference and every gate keeps the average."""
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as r
    started = []
    monkeypatch.setattr(pt, "submit_receipt_reference", lambda *a, **k: started.append(a) or Future())
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "http://funded.invalid")
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", raising=False)
    origin = "swap:raybot:0x%s:0x%s:0x%s" % ("ab" * 32, "cd" * 20, "ef" * 20)
    ev = SimpleNamespace(fomo_event_id=origin, mint="0x" + "ef" * 20)
    assert r._start_evm_post_trade_read(ev, "56") is None and not started
    assert getattr(ev, "evm_chase_reference", None) is None
    assert pt.read_host("56") is None
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_56", "https://read.invalid")
    assert r._start_evm_post_trade_read(ev, "56") is not None and len(started) == 1


def test_the_chain_knob_is_canonical(monkeypatch, caplog):
    """Beta F5: 'robin' is 4663; an unknown name is refused loudly, never silently kept."""
    from sniper.settings import memecoin as cfg
    got = cfg.memecoin_evm_post_trade_chains({"MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS": "robin, base,bsc,bogus"})
    assert got == frozenset({"4663", "8453", "56"})
    assert "bogus" in caplog.text
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setenv("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", "robin")
    assert pt.enabled("4663") and pt.enabled("robin") and not pt.enabled("8453")


@pytest.mark.parametrize("bad", [0.5, 0.0, -1.0, float("nan"), float("inf"), None])
def test_a_max_ratio_below_one_falls_back_to_two_on_both_chains(monkeypatch, bad):
    """Beta F6: a MEMECOIN_CHASE_POST_TRADE_MAX_RATIO below 1 (or not finite) disabled the clamp, so
    a ratio of 40 was admitted. It now falls back to 2.0, on EVM and on the Solana lane."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_onchain_lane import chase_reference
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_REFERENCE", "post_trade", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_CHASE_POST_TRADE_MAX_RATIO", bad, raising=False)
    monkeypatch.setenv("MEMECOIN_REAL_CHASE_REFERENCE_EVM_CHAINS", "4663")
    assert cfg.chase_post_trade_max_ratio() == 2.0
    assert pt.resolve("4663", avg_price=1.0, post=dict(POST, ratio=40.0, pool="0xp"), our_pool="0xp")[:2] == (
        2.0, "post_trade_clamped")
    row = {"source_price": 1.0, "source_valuation_json": json.dumps({"source_post_price": 40.0})}
    assert chase_reference(row) == (2.0, "post_trade_clamped")


# --- the buy tax on the source's V2 leg (2026-09-30, tax-aware entry minimum) ------------------------

@pytest.mark.parametrize("name, tax", [("v2_56_fill", 0.01), ("v2_56_taxed_refusal", 0.01),
                                       ("v2_56_fill_66521", 0.0)])
def test_the_decode_measures_the_coins_transfer_tax_on_the_v2_leg(name, tax):
    """Real 56 receipts: the pair splits its payout, the fee to the token contract and the rest to
    the next hop. A single payout is a decoded zero, never an unknown."""
    case = CASES[name]
    r = pt.source_post_trade("56", case["receipt"], case["mint"], owner=_owner(case), pool_key_fn=_keys)
    assert r["venue"] == "v2" and r["buy_tax"] == pytest.approx(tax, abs=1e-9)


def _taxed_leg(*, extra_swap=False, fee_to=None):
    """A synthetic V2 buy shaped like BINF's (2026-09-30): 626,252 to the token contract and
    20,248,822 to the router out of the pair, one Swap, one Sync."""
    mint, pair, router, owner = "0x" + "77" * 20, "0x" + "25" * 20, "0x" + "0d" * 20, "0x" + "6f" * 20
    word = lambda v: format(v, "064x")
    topic = lambda a: "0x" + "0" * 24 + a[2:]
    fee, main, into = 626_252 * 10 ** 18, 20_248_822 * 10 ** 18, 8 * 10 ** 18
    logs = [
        {"address": mint, "topics": [TRANSFER, topic(pair), topic(fee_to or mint)], "data": "0x" + word(fee)},
        {"address": mint, "topics": [TRANSFER, topic(pair), topic(router)], "data": "0x" + word(main)},
        {"address": pair, "topics": [SYNC], "data": "0x" + word(10 ** 20) + word(10 ** 26)},
        {"address": pair, "topics": [V2, topic(router), topic(router)],
         "data": "0x" + word(into) + word(0) + word(0) + word(fee + main)},
        {"address": mint, "topics": [TRANSFER, topic(router), topic(owner)], "data": "0x" + word(main)},
    ]
    if extra_swap:
        logs.append({"address": pair, "topics": [V2, topic(router), topic(router)],
                     "data": "0x" + word(1) + word(0) + word(0) + word(1)})
    return {"status": "0x1", "logs": logs}, mint, owner


def test_a_binf_shaped_buy_reads_three_percent():
    receipt, mint, owner = _taxed_leg()
    r = pt.source_post_trade("56", receipt, mint, owner=owner, pool_key_fn=_keys)
    assert r["venue"] == "v2" and r["buy_tax"] == pytest.approx(626_252 / (626_252 + 20_248_822))


@pytest.mark.parametrize("kw", [{"extra_swap": True}, {"fee_to": "0x" + "ab" * 20}],
                         ids=["two-swaps-on-the-pair", "fee-to-a-wallet"])
def test_a_shape_that_is_not_the_token_taking_its_fee_is_an_unknown_tax(kw):
    """Alpha: more than one Swap on the pair in the source's transaction reads unknown. A fee sent
    anywhere but the token contract is not the shape measured either: unknown, today's minimum."""
    receipt, mint, owner = _taxed_leg(**kw)
    r = pt.source_post_trade("56", receipt, mint, owner=owner, pool_key_fn=_keys)
    assert "buy_tax" not in r
