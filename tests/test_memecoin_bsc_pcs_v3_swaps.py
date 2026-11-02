"""PancakeSwap-V3 swaps into the BSC store, so E7:56 also counts V3 sources (Alpha 2026-10-08).

The store's V3 subscription carried only the Uniswap-V3 Swap topic. PancakeSwap V3 has its own topic
(two extra protocol-fee words), so every PancakeSwap-V3 source (6 of 71 decoded since 10-01) read
uncovered. Measured on BSC over 4 blocks: V2 7.0 swaps per block, PancakeSwap V3 5.8, Uniswap V3 0,
so the store's swap writes grow by about 80%.

A V3 pool's quote is priced only when the pool is the mint's PancakeSwap-V3 CREATE2 address against
WBNB or a $1 stable at one of the fee tiers (deployer and init-code hash proved on 7 of 7 stored
v3_pools); otherwise its window is counts_only, which E7 reads and the volume gate does not.
"""
import pytest

from sniper.services import memecoin_bsc_v2_live as live
from sniper.services import memecoin_volume_writer as V

MINT = "0x" + "aa" * 20
SOURCE = "0x" + "11" * 32


def test_the_v3_subscription_carries_both_swap_topics():
    from _source_guard import code_of
    src = code_of(live.run)
    assert "(5, ['logs', {'topics': [[V3_SWAP, PANCAKE_V3_SWAP]]}])" in src


@pytest.mark.parametrize("fee", [100, 500, 2500, 10000])
def test_a_pancake_v3_pool_against_wbnb_or_a_stable_is_priced(fee):
    assert live.quote_of_pair(MINT, live._pcs_v3_pool(MINT, live.WBNB, fee)) == live.WBNB
    usdt = live.STABLES[0]
    assert live.quote_of_pair(MINT, live._pcs_v3_pool(MINT, usdt, fee)) == usdt


def test_the_pcs_v3_constants_name_a_real_stored_pool():
    """The CREATE2 deployer and init hash reproduce a PancakeSwap-V3 pool the live store holds:
    0x99219a38... = mint 0x320474b5... against WBNB at fee 10000 (from its v3_pools row)."""
    mint = "0x320474b5f11b030407b7e7bd5f5a989c730a0000"
    assert live._pcs_v3_pool(mint, live.WBNB, 10000) == "0x99219a385ec01ed3b2a422dbc1c5a988ef3461c6"
    assert live.quote_of_pair(mint, "0x99219a385ec01ed3b2a422dbc1c5a988ef3461c6") == live.WBNB


def test_gate_evidence_reads_a_v3_receipt_pool_now(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:56")
    seen = []
    monkeypatch.setattr(live, "read_m5", lambda pool, quote, mint, tx, **kw: seen.append(
        (pool, quote, kw.get("coin_is_token1"))) or {})
    origin = "swap:raybot:%s:0x%s:%s" % (SOURCE, "ab" * 20, MINT)
    pool = "0x" + "cd" * 20
    V.gate_evidence("56", MINT, origin, stats={}, source_pool={"pool": pool, "venue": "v3",
                                                               "mint_is_c1": False})
    assert seen == [(pool, None, False)]
