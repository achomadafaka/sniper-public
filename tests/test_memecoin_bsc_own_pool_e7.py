"""56 without DexScreener (spec 2026-10-08-memecoin-evm-entry-gates-onchain-design.md build step 4).

- The BSC tape's pool comes from the source's own swap, never the provider's pair pick: the
  PancakeSwap-V2 mint/WBNB pair (CREATE2 from the proven factory and init-code hash), accepted only
  when the source transaction swapped in it (`m5_bound`'s existing source_tx check). A source that
  traded elsewhere (V3, Infinity, another factory) is unknown.
- The window's buys and sells are counted on that pool, source excluded. The store's amounts follow
  the house rule (positive flows INTO the pool), so quote in > 0 is a buy of the coin.
- With E7:56 off, the sell-share gate reads that split, as on 4663 and 8453.
"""
import pytest

from sniper.services import memecoin_bsc_v2_live as live
from sniper.services import memecoin_volume_writer as V

MINT = "0x" + "aa" * 20
WBNB = live.WBNB
SOURCE = "0x" + "11" * 32
HASH = "0x" + "33" * 32


@pytest.mark.parametrize("case", ["e7_off", "no_provider_pair", "provider_pair"])
def test_without_dexscreener_the_pool_is_the_pancake_v2_wbnb_pair_of_the_mint(monkeypatch, case):
    """While DexScreener is read for 56 its pair stays (today); with E7:56 off, or no provider pair,
    the pool is our own: the PancakeSwap-V2 mint/WBNB pair."""
    from sniper.services.memecoin_evm_post_trade import V2_FACTORIES, _v2_pair
    factory, init = V2_FACTORIES[("56", "0x10ed43c718714eb63d5aa57b78b54704e256024e")]
    if case == "e7_off":
        monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:56")
    seen = []
    monkeypatch.setattr(live, "read_m5", lambda pool, quote, mint, tx: seen.append((pool, quote)) or {})
    origin = "swap:raybot:%s:0x%s:%s" % (SOURCE, "ab" * 20, MINT)
    stats = ({} if case == "no_provider_pair" else
             {"pair_address": "0x" + "99" * 20, "quote_address": "0x" + "88" * 20})
    V.gate_evidence("56", MINT, origin, stats=stats)
    own = (_v2_pair(factory, init, MINT, WBNB).lower(), WBNB)
    assert seen == [("0x" + "99" * 20, "0x" + "88" * 20) if case == "provider_pair" else own]


def _store(tmp_path, pool, rows):
    c = live.connect(tmp_path / "bsc.db")
    live.record_quote(c, 600.0, 1000.0)
    live.record_head(c, 1, HASH, 700.0, 700.1)
    live.record_head(c, 2, HASH, 1000.0, 1000.1)
    for i, (tx, q) in enumerate(rows, start=1):
        quote0 = WBNB < MINT                     # the quote's leg is token0 when WBNB sorts first
        a0, a1 = (q, -q * 1000) if quote0 else (-q * 1000, q)
        live.record_swap(c, pool, 2, HASH, tx, i, a0, a1)
    return c


def test_the_window_counts_buys_and_sells_on_the_pool_source_excluded(tmp_path):
    pool = "0x" + "cc" * 20
    c = _store(tmp_path, pool, [(SOURCE, 10**18), ("0x" + "21" * 32, 10**18),
                                ("0x" + "22" * 32, 2 * 10**18), ("0x" + "23" * 32, -10**18)])
    out = live.m5_bound(c, pool, WBNB, MINT, SOURCE, 1000.5)
    assert out["reason"] == "ok" and out["buys"] == 2 and out["sells"] == 1 and out["swaps"] == 3
    assert out["pools"] == [{"pool": pool, "buys": 2, "sells": 1, "swaps": 3,
                             "usd": out["usd"], "txns": 3}]


def test_e7_off_on_56_reads_our_split(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:56")
    m5 = {"usd": 900.0, "reason": "ok", "covered_s": 300,
          "pools": [{"pool": "0x" + "cc" * 20, "buys": 6, "sells": 3, "swaps": 9, "usd": 900.0}]}
    stats, basis, own = R._sell_share_input({"txns_m5_buys": 1, "txns_m5_sells": 50}, m5, "56")
    assert basis == "onchain_pool_counts"
    assert stats["txns_m5_buys"] == 6 and stats["txns_m5_sells"] == 3
