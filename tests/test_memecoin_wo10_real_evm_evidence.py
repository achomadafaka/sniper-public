"""WO-10 — real funded-chain-4663 Uniswap-V3 evidence closure.

Loads the committed RAW fixtures captured from live 4663 (tests/fixtures/memecoin/evm/4663/) — the
tests NEVER reconstruct ABI words; they decode the raw log through the production decoder and verify
against the real event. Proves: canonical topics (independent keccak), ABI field positions + signed
tick, tx/block/log cross-identity, Swap price/liquidity state, Mint adds depth only in-range, Burn
severity respects range + active liquidity, Collect never changes depth, hot==pull decode parity, and
V2 behavior unchanged. Only with all four families proven does 4663 join MEMECOIN_V3_DEPTH_VERIFIED_CHAINS.
"""
import hashlib
import json
import os

from Crypto.Hash import keccak

from sniper.analysis.exit_features import v3_largest_lp_removal_fraction
from sniper.services.memecoin_evm_hot import evm_hot_row
from sniper.services.memecoin_tick_stream import (
    V3_BURN, V3_COLLECT, V3_MINT, V3_SWAP, decode_evm_log)

FIXDIR = os.path.join(os.path.dirname(__file__), "fixtures", "memecoin", "evm", "4663")
KINDS = ("swap", "swap_inrange", "mint", "burn", "collect")


def _load(name):
    with open(os.path.join(FIXDIR, f"v3_{name}.json")) as fh:
        return json.load(fh)


def _decode(name):
    fx = _load(name)
    return decode_evm_log("4663", fx["log"], "obs")


def _topic0(sig):
    h = keccak.new(digest_bits=256)
    h.update(sig.encode())
    return "0x" + h.hexdigest()


# --- canonical topics, independently derived ------------------------------------------------------

def test_canonical_event_topics_match_uniswap_v3_keccak():
    sigs = {
        V3_SWAP: "Swap(address,address,int256,int256,uint160,uint128,int24)",
        V3_MINT: "Mint(address,address,int24,int24,uint128,uint256,uint256)",
        V3_BURN: "Burn(address,int24,int24,uint128,uint256,uint256)",
        V3_COLLECT: "Collect(address,address,int24,int24,uint128,uint128)",
    }
    for const, sig in sigs.items():
        assert _topic0(sig) == const, f"topic0 mismatch for {sig}"
    # and every real fixture's topics[0] IS that canonical topic
    for name, topic in (("swap", V3_SWAP), ("mint", V3_MINT), ("burn", V3_BURN), ("collect", V3_COLLECT)):
        assert _load(name)["log"]["topics"][0].lower() == topic.lower()


# --- fixture identity: log belongs to its tx AND block, payload hash reproduces --------------------

def test_fixture_identity_ties_log_to_tx_and_block():
    for name in KINDS:
        fx = _load(name)
        log, rec, hdr = fx["log"], fx["receipt_identity"], fx["block_header"]
        assert log["blockHash"].lower() == rec["blockHash"].lower() == hdr["hash"].lower()
        assert log["transactionHash"].lower() == rec["transactionHash"].lower()
        assert int(log["blockNumber"], 16) == int(rec["blockNumber"], 16) == int(hdr["number"], 16)
        assert fx["pool"].lower() == log["address"].lower()
        recomputed = hashlib.sha256(json.dumps({
            "address": log["address"], "topics": log["topics"], "data": log["data"],
            "blockNumber": log["blockNumber"], "blockHash": log["blockHash"],
            "transactionHash": log["transactionHash"], "logIndex": log["logIndex"]},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert recomputed == fx["payload_sha256"]


# --- ABI field positions + signed tick + swap price/liquidity state --------------------------------

def test_v3_swap_decodes_price_liquidity_and_signed_tick():
    r = _decode("swap")
    assert r["kind"] == "v3_swap"
    assert int(r["amount0"]) > 0 and int(r["amount1"]) < 0        # signed in/out of pool
    assert int(r["sqrt_price_x96"]) > 0 and int(r["liquidity"]) > 0
    assert r["tick"] == 138848 and isinstance(r["tick"], int)    # signed int24 decode (positive here)


def test_v3_mint_burn_collect_abi_field_positions():
    m = _decode("mint")
    assert m["kind"] == "v3_mint" and int(m["amount0"]) > 0 and int(m["amount1"]) > 0
    assert int(m["liquidity"]) > 0 and m["tick_lower"] == 139400 and m["tick_upper"] == 140400
    b = _decode("burn")
    assert b["kind"] == "v3_burn" and int(b["amount1"]) < 0       # burn reads negative (out of pool)
    assert int(b["liquidity"]) > 0 and b["tick_lower"] == 139400 and b["tick_upper"] == 140200
    c = _decode("collect")
    assert c["kind"] == "v3_collect" and c.get("liquidity") is None and c.get("tick_lower") is None


# --- depth semantics on REAL decoded rows (order controlled via block_ts; values are real) --------

def _seq(*named_ts):
    rows = []
    for name, ts in named_ts:
        r = _decode(name)
        r["block_ts"] = float(ts)
        rows.append(r)
    return rows


def test_burn_severity_respects_range_and_active_liquidity():
    burn = _decode("burn")
    swap_in = _decode("swap_inrange")            # tick 139548 IN [139400,140200)
    frac_in = v3_largest_lp_removal_fraction(_seq(("swap_inrange", 0), ("burn", 1)))
    assert frac_in == int(burn["liquidity"]) / int(swap_in["liquidity"])   # removed / active in-range
    # out-of-range: swap tick 138848 not in the burn range -> the burn changes NO executable depth
    assert v3_largest_lp_removal_fraction(_seq(("swap", 0), ("burn", 1))) == 0.0


def test_mint_adds_depth_only_when_in_range():
    frac_no_mint = v3_largest_lp_removal_fraction(_seq(("swap_inrange", 0), ("burn", 2)))
    # the in-range mint (range contains tick 139548) increases active liquidity BEFORE the burn,
    # so the same removal is a SMALLER fraction of a deeper book
    frac_with_mint = v3_largest_lp_removal_fraction(
        _seq(("swap_inrange", 0), ("mint", 1), ("burn", 2)))
    assert frac_with_mint < frac_no_mint
    swap_in, mint, burn = _decode("swap_inrange"), _decode("mint"), _decode("burn")
    assert frac_with_mint == int(burn["liquidity"]) / (int(swap_in["liquidity"]) + int(mint["liquidity"]))


def test_collect_never_changes_depth():
    with_collect = v3_largest_lp_removal_fraction(
        _seq(("swap_inrange", 0), ("collect", 1), ("burn", 2)))
    without = v3_largest_lp_removal_fraction(_seq(("swap_inrange", 0), ("burn", 2)))
    assert with_collect == without                                # a fee claim is not a removal
    assert v3_largest_lp_removal_fraction(_seq(("swap_inrange", 0), ("collect", 1))) is None  # no burn


# --- hot lane and pull/materializer decode agree (same decode_evm_log underlies both) -------------

def test_hot_and_pull_decoding_agree():
    for name in KINDS:
        fx = _load(name)
        pull = decode_evm_log("4663", fx["log"], "obs")           # materializer/pull ingest path
        hot = evm_hot_row("4663", fx["log"], "obs",
                          block_ts=int(fx["block_header"]["timestamp"], 16), block_time_true=True)
        for k in pull:                                            # hot row carries the identical decode
            assert hot.get(k) == pull[k], f"{name}: hot/pull disagree on {k}"
        assert hot["transaction_decoded"] is True                # EVM row is natively tx-grounded


# --- V2 behavior unchanged (regression: the V3 additions did not disturb the V2 path) -------------

def test_v2_decode_behavior_unchanged():
    from sniper.services.memecoin_tick_stream import V2_SWAP, V2_SYNC
    def _w(v):
        return format(v & (2 ** 256 - 1), "064x")
    sync = {"topics": [V2_SYNC], "data": "0x" + _w(1000) + _w(2000), "address": "0xp",
            "blockNumber": hex(5), "transactionHash": "0xt", "logIndex": hex(0), "blockHash": "0xb"}
    r = decode_evm_log("4663", sync, "obs")
    assert r["kind"] == "v2_sync" and r["amount0"] == "1000" and r["amount1"] == "2000"
    swap = {"topics": [V2_SWAP, "0x" + _w(0)], "data": "0x" + _w(100) + _w(0) + _w(0) + _w(60),
            "address": "0xp", "blockNumber": hex(6), "transactionHash": "0xt2", "logIndex": hex(1),
            "blockHash": "0xb"}
    s = decode_evm_log("4663", swap, "obs")
    assert s["kind"] == "v2_swap" and s["amount0"] == "100" and s["amount1"] == "-60"


# --- gated capability: 4663 joins the verified set ONLY now that all four families are proven ------

def test_4663_v3_depth_now_verified_8453_not():
    from sniper.settings import memecoin as cfg
    assert "4663" in cfg.MEMECOIN_V3_DEPTH_VERIFIED_CHAINS   # WO-10 closed: real Swap/Mint/Burn/Collect
    assert "8453" not in cfg.MEMECOIN_V3_DEPTH_VERIFIED_CHAINS   # no captured 8453 evidence


def test_all_four_v3_event_families_present_as_fixtures():
    for name in ("swap", "mint", "burn", "collect"):
        assert os.path.exists(os.path.join(FIXDIR, f"v3_{name}.json"))
