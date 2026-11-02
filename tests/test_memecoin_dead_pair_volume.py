"""A pair that REPORTS zero volume is a measured zero, not missing data (RAYGUN 64145, 2026-09-23).

DexScreener listed RAYGUN's only pool with volume.h1 = 0 and txns.h1 = 0 on a 42-day-old pair.
`_stats_from_pairs` dropped the literal 0 (positive_only), so the volume gate saw None, said
"no_volume_data" and failed OPEN; $175 went into a coin with 3 trades an hour. A fresh pair that
the provider has not indexed yet also reports 0, so the zero counts only once the pair is older
than the window it describes.
"""
import time

from sniper.execution.solana_quote import _stats_from_pairs
from sniper.services.memecoin_real_runner import volume_alive_decision

MINT = "EsBXXj4inBpMrUjXeA8HEpuGrCbBM11QMjYpGTHEMnF3"


def _pair(*, age_s, h1=0.0, txns_h1=0, with_volume=True):
    p = {"chainId": "solana", "dexId": "raydium", "pairAddress": "4Uo7925DR3F29fTrqiGRx5WABCdLtVi3sYY4ewUAs71G",
         "baseToken": {"address": MINT, "symbol": "RAYGUN"},
         "quoteToken": {"address": "So11111111111111111111111111111111111111112", "symbol": "SOL"},
         "priceUsd": "0.00001813", "marketCap": 18134.0, "fdv": 18134.0,
         "liquidity": {"usd": 17440.26, "quote": 1517.33},
         "txns": {"m5": {"buys": 0, "sells": 0}, "h1": {"buys": txns_h1, "sells": 0}},
         "pairCreatedAt": int((time.time() - age_s) * 1000)}
    if with_volume:
        p["volume"] = {"m5": 0.0, "h1": h1, "h24": 0.0}
    return p


def test_a_mature_pair_reporting_zero_h1_volume_is_a_measured_zero():
    st = _stats_from_pairs([_pair(age_s=42 * 86400)], MINT, "solana")
    assert st["volume_h1"] == 0.0


def test_a_pair_younger_than_the_window_keeps_zero_as_unknown():
    st = _stats_from_pairs([_pair(age_s=60)], MINT, "solana")
    assert st["volume_h1"] is None


def test_a_missing_volume_field_stays_unknown():
    st = _stats_from_pairs([_pair(age_s=42 * 86400, with_volume=False)], MINT, "solana")
    assert st["volume_h1"] is None


def test_zero_volume_with_trades_reported_stays_unknown():
    # A provider that counts trades but prices none of them has not measured the volume.
    st = _stats_from_pairs([_pair(age_s=42 * 86400, txns_h1=3)], MINT, "solana")
    assert st["volume_h1"] is None


def test_the_flat_h1_floor_refuses_a_measured_zero():
    ok, need, why = volume_alive_decision(0.0, 17964.0, abs_floor=3000.0, turnover_floor=0.0,
                                          signal_usd=531.56)
    assert (ok, need, why) == (False, 3000.0, "under_abs_floor")
    ok, _, why = volume_alive_decision(None, 17964.0, abs_floor=3000.0, turnover_floor=0.0)
    assert (ok, why) == (True, "no_volume_data")
