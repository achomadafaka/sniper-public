from eth_abi import encode
from eth_utils import keccak

from sniper.services.memecoin_evm_v4_events import (
    V4_MODIFY_LIQUIDITY, V4_SWAP, decode_v4_log, v4_pool_id,
)


KEY = {
    "currency0": "0x0000000000000000000000000000000000000000",
    "currency1": "0xe934e36a439c94017b64a3fece66af12099abf50",
    "fee": 10_000,
    "tick_spacing": 200,
    "hooks": "0x0000000000000000000000000000000000000000",
}


def _word(value):
    if value < 0:
        value += 1 << 256
    return f"{value:064x}"


def _log(topic, words, *, pool_id, sender="0x" + "ab" * 20):
    return {
        "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "topics": [topic, pool_id, "0x" + sender[2:].rjust(64, "0")],
        "data": "0x" + "".join(_word(value) for value in words),
        "blockNumber": hex(10), "blockHash": "0xblock",
        "transactionHash": "0xtx", "logIndex": hex(3),
    }


def test_pool_id_matches_canonical_abi_hash():
    expected = "0x" + keccak(encode(
        ["address", "address", "uint24", "int24", "address"],
        [KEY["currency0"], KEY["currency1"], KEY["fee"],
         KEY["tick_spacing"], KEY["hooks"]],
    )).hex()
    assert v4_pool_id(KEY) == expected


def test_decode_v4_swap_uses_pool_id_not_singleton_address():
    pool_id = v4_pool_id(KEY)
    row = decode_v4_log(
        "4663", _log(V4_SWAP, [-100, 55, 123456, 999_000, -12, 10_000], pool_id=pool_id),
        "2026-07-26T12:00:00+00:00",
    )
    assert row["pool"] == pool_id
    assert row["kind"] == "v3_swap"
    # PoolManager emits the SWAPPER's deltas (-100 paid, +55 received); the tape stores INTO-POOL
    # amounts, the db_ticks contract V3 and V2 rows already follow (2026-09-29).
    assert (row["amount0"], row["amount1"]) == ("100", "-55")
    assert (row["sqrt_price_x96"], row["liquidity"], row["tick"]) == (
        "123456", "999000", -12)


def test_decode_v4_liquidity_delta_maps_to_depth_events():
    pool_id = v4_pool_id(KEY)
    removed = decode_v4_log(
        "4663", _log(V4_MODIFY_LIQUIDITY, [-400, 600, -250_000, 0], pool_id=pool_id),
        "2026-07-26T12:00:00+00:00",
    )
    added = decode_v4_log(
        "4663", _log(V4_MODIFY_LIQUIDITY, [-400, 600, 90_000, 0], pool_id=pool_id),
        "2026-07-26T12:00:00+00:00",
    )
    assert (removed["kind"], removed["liquidity"]) == ("v3_burn", "250000")
    assert (added["kind"], added["liquidity"]) == ("v3_mint", "90000")
    assert (removed["tick_lower"], removed["tick_upper"]) == (-400, 600)


# --- one sign convention for every pool family (2026-09-29, Gamma's latent finding) ------------------
# V4 rows kept PoolManager's swapper sign while every flow reader applies the into-pool rule, so on V4
# pools net_flow, buy_n and sell_n were inverted: structure_break and net_flow_negative would fire on
# net BUYS. The fix is at the decoder, the single point every V4 row passes through.

def _v3_log(amount0, amount1):
    pool = "0x" + "12" * 20
    return {"address": pool,
            "topics": ["0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",
                       "0x" + "ab" * 32, "0x" + "cd" * 32],
            "data": "0x" + "".join(_word(v) for v in (amount0, amount1, 123456, 999_000, -12)),
            "blockNumber": hex(10), "blockHash": "0xblock", "transactionHash": "0xtx3",
            "logIndex": hex(1)}


def test_a_v4_buy_and_a_v3_buy_store_the_same_signs_and_read_as_buys():
    """The same trade on both families: pay 100 of the quote (currency0 here), receive 55 of the
    coin. V3's Swap is the POOL's side (+100 in, -55 out); V4's is the swapper's (-100, +55)."""
    from sniper.analysis.exit_features import pool_flow
    from sniper.analysis.tick_features import compute_window_features
    from sniper.services.memecoin_tick_stream import decode_evm_log
    v4 = decode_evm_log("4663", _log(V4_SWAP, [-100, 55, 123456, 999_000, -12, 10_000],
                                     pool_id=v4_pool_id(KEY)), "2026-09-29T02:00:00+00:00")
    v3 = decode_evm_log("4663", _v3_log(100, -55), "2026-09-29T02:00:00+00:00")
    assert (v4["amount0"], v4["amount1"]) == (v3["amount0"], v3["amount1"]) == ("100", "-55")
    for row in (v4, v3):
        flow = pool_flow([row], quote_side=0)
        assert (flow.buy_n, flow.sell_n, flow.net_flow_quote) == (1, 0, "100")
        feats = compute_window_features([row], quote_side=0)
        assert (feats["buy_n"], feats["sell_n"]) == (1, 0)


def test_a_v4_sell_reads_as_a_sell():
    """Swapper pays 55 of the coin (currency1) and receives 100 of the quote."""
    from sniper.analysis.exit_features import pool_flow
    row = decode_v4_log("4663", _log(V4_SWAP, [100, -55, 123456, 999_000, -12, 10_000],
                                     pool_id=v4_pool_id(KEY)), "2026-09-29T02:00:00+00:00")
    assert (row["amount0"], row["amount1"]) == ("-100", "55")
    flow = pool_flow([row], quote_side=0)
    assert (flow.buy_n, flow.sell_n) == (0, 1)
