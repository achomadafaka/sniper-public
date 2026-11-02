"""56 pool identity beyond the PancakeSwap-V2 WBNB pair (Alpha 2026-10-08: DexScreener out of the money
path, so E7:56 needs our own counts on the coins it trades).

Measured 10-01..10-08 on 71 decoded 56 sources: 43 traded a PancakeSwap-V2 pair (15 against WBNB,
28 against another token: launchpad bases such as 0x9b8e98..., 0x205812..., CAKE), 6 a V3 pool, 22
no single leg. The source receipt's decode (memecoin_evm_post_trade) already names the pool that
moved the mint and which side the mint is on (`mint_is_c1`). That is enough to COUNT buys and sells
on the store's swap rows for the pool, whatever the quote; USD needs a known quote (WBNB, or a $1
BSC stable proved by its PancakeSwap-V2 CREATE2 address), else the window is `counts_only`: E7 reads
it, the volume gate does not.
"""
import pytest

from sniper.services import memecoin_bsc_v2_live as live
from sniper.services import memecoin_volume_writer as V
from sniper.services.memecoin_onchain_volume import own_sell_share_split

MINT = "0x" + "aa" * 20
OTHER_BASE = "0x9b8e987e6fec8cf1380c4dca7071e2c7853aeea1"
POOL = "0x" + "cd" * 20
SOURCE = "0x" + "11" * 32
HASH = "0x" + "33" * 32


def _store(tmp_path, *, coin_is_token1, rows):
    c = live.connect(tmp_path / "bsc.db")
    live.record_quote(c, 600.0, 1000.0)
    live.record_head(c, 1, HASH, 700.0, 700.1)
    live.record_head(c, 2, HASH, 1000.0, 1000.1)
    for i, (tx, q) in enumerate(rows, start=1):
        a0, a1 = (q, -q * 1000) if coin_is_token1 else (-q * 1000, q)   # quote on the other side
        live.record_swap(c, POOL, 2, HASH, tx, i, a0, a1)
    return c


ROWS = [(SOURCE, 10**18), ("0x" + "21" * 32, 10**18), ("0x" + "22" * 32, 2 * 10**18),
        ("0x" + "23" * 32, -10**18)]


@pytest.mark.parametrize("coin_is_token1", [True, False])
def test_an_unpriced_base_still_counts_buys_and_sells_from_the_receipts_side(tmp_path, coin_is_token1):
    c = _store(tmp_path, coin_is_token1=coin_is_token1, rows=ROWS)
    out = live.m5_bound(c, POOL, None, MINT, SOURCE, 1000.5, coin_is_token1=coin_is_token1)
    assert out["reason"] == "counts_only" and out["usd"] is None
    assert (out["buys"], out["sells"], out["swaps"]) == (2, 1, 3)
    split = own_sell_share_split(out)
    assert split["txns_m5_buys"] == 2 and split["txns_m5_sells"] == 1 and split["pool"] == POOL


def test_a_stable_quote_proved_by_its_pair_address_is_priced_at_one_dollar():
    usdt = "0x55d398326f99059ff775485246999027b3197955"
    assert live.quote_of_pair(MINT, live._pcs_v2_pair(MINT, usdt)) == usdt
    assert live.quote_of_pair(MINT, live.source_pair(MINT)) == live.WBNB
    assert live.quote_of_pair(MINT, POOL) is None


def test_the_volume_gate_never_reads_a_counts_only_window_as_a_bound():
    from sniper.services.memecoin_real_runner import onchain_h1_lower_bound, onchain_m5_txns_lower_bound
    ev = {"usd": None, "reason": "counts_only", "swaps": 3, "buys": 2, "sells": 1}
    assert onchain_h1_lower_bound(ev) is None and onchain_m5_txns_lower_bound(ev) is None


def test_gate_evidence_takes_the_receipts_pool_when_e7_56_is_off(monkeypatch):
    monkeypatch.setenv("MEMECOIN_DEXSCREENER_OFF", "E7:56")
    seen = []
    monkeypatch.setattr(live, "read_m5", lambda pool, quote, mint, tx, **kw: seen.append(
        (pool, quote, kw.get("coin_is_token1"))) or {})
    origin = "swap:raybot:%s:0x%s:%s" % (SOURCE, "ab" * 20, MINT)
    V.gate_evidence("56", MINT, origin, stats={"pair_address": "0x" + "99" * 20},
                    source_pool={"pool": POOL, "venue": "v2", "mint_is_c1": True})
    assert seen == [(POOL, None, True)]
    seen.clear()
    V.gate_evidence("56", MINT, origin, stats={}, source_pool={"pool": POOL, "venue": "v4",
                                                               "mint_is_c1": True})
    assert seen == [(live.source_pair(MINT), live.WBNB, None)]   # a venue the store does not hold
    # (V3 is in the store since the PancakeSwap-V3 topic joined: tests/test_memecoin_bsc_pcs_v3_swaps.py)


def test_the_runner_hands_the_receipt_decode_to_the_gate(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    got = {}
    monkeypatch.setattr(V, "gate_evidence", lambda *a, **kw: got.update(kw) or {})

    class Ref:
        def payload(self):
            return {"pool": POOL, "venue": "v2", "mint_is_c1": False, "ratio": 1.1}

    class Ev:
        mint, fomo_event_id, evm_chase_reference = MINT, "swap:raybot:x", Ref()
    R._own_m5_for_gate(Ev(), "56", {})
    assert got["source_pool"] == {"pool": POOL, "venue": "v2", "mint_is_c1": False}
