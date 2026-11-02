"""Position-stake helpers for analyzer consumers."""
from __future__ import annotations

from sniper.analysis.wallet_stakes import position_stakes, q75_index


def _act(typ="TRADE", side="BUY", usd=50000, px=0.40, cid="0xc", oidx=0):
    return {
        "type": typ,
        "side": side,
        "usdcSize": usd,
        "price": px,
        "conditionId": cid,
        "outcomeIndex": oidx,
    }


def test_position_stakes_aggregates_fills_per_position():
    rows = [
        _act(usd=20000, cid="0xc", oidx=0),
        _act(usd=30000, cid="0xc", oidx=0),
        _act(usd=8000, cid="0xd", oidx=1),
        _act(usd=100000, cid="0xe", side="SELL"),
        _act(usd=100000, cid="0xf", px=1.0),
    ]

    assert position_stakes(rows) == [8000.0, 50000.0]


def test_q75_index_nearest_rank_not_max():
    # Sorted [10k,20k,30k,40k] -> index 2 -> 30k, not 40k.
    stakes = [10000.0, 20000.0, 30000.0, 40000.0]
    assert q75_index(len(stakes)) == 2
    assert stakes[q75_index(len(stakes))] == 30000.0
    assert q75_index(0) == 0
    assert q75_index(1) == 0
    assert q75_index(1000) in (749, 750)
