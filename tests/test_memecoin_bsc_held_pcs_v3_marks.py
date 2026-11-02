"""A funded 56 position held on a PancakeSwap-V3 pool gets observer marks (Gamma LOW on 53ed9c288).

The held-pool V3 subscription asked for the Uniswap-V3 Swap topic only; a PancakeSwap-V3 pool emits
its own topic, so `v3_marks` never filled and `latest_v3_mark` stayed None for those positions (the
held V3 pools are PancakeSwap's: `_resolve_v3` checks the pool's factory against the configured 56
quoter's). `decode_evm_log` already reads PancakeSwap-V3 swaps as v3_swap with the sqrtPrice.
"""
from sniper.services import memecoin_bsc_v2_live as live
from sniper.services.memecoin_tick_stream import PANCAKE_V3_SWAP, decode_evm_log


def test_the_held_pool_subscription_carries_both_v3_swap_topics():
    from _source_guard import code_of
    src = code_of(live.run)
    assert "{'address': pool, 'topics': [[V3_SWAP, PANCAKE_V3_SWAP]]}" in src


def test_a_pancake_v3_swap_decodes_with_its_sqrt_price():
    pool = "0x" + "ab" * 20
    words = [(-5 * 10**18) % (1 << 256), 3 * 10**18, 2**96, 10**20, 0, 0, 0]
    log = {"address": pool, "topics": [PANCAKE_V3_SWAP, "0x" + "00" * 32, "0x" + "00" * 32],
           "data": "0x" + "".join("%064x" % w for w in words), "blockNumber": "0x10",
           "transactionHash": "0x" + "11" * 32, "logIndex": "0x1", "blockHash": "0x" + "22" * 32}
    row = decode_evm_log("56", log, "2026-10-08T23:00:00+00:00")
    assert row and row["kind"] == "v3_swap" and row["sqrt_price_x96"] == str(2**96)
