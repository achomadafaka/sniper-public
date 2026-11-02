"""Acceptance test — copy-latency capture grid helper.

Spec: docs/superpowers/specs/2026-05-22-copy-latency-surface-design.md §4, §10(15)

`copy_latency_grid_pairs` derives the post-entry price-grid (asset, ts)
pairs the capture must resolve: per closed position, the first BUY's
(asset, entry_ts) plus (asset, entry_ts + h*60) for each horizon.

RED until `copy_latency_grid_pairs` exists.
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.copy_latency import (
    HORIZONS_MIN,
    copy_latency_grid_pairs,
)


def _buy(cid: str, asset: str, ts: int) -> dict:
    return {"type": "TRADE", "side": "BUY", "conditionId": cid,
            "asset": asset, "timestamp": ts}


def _pos(cid: str, asset: str) -> dict:
    return {"conditionId": cid, "asset": asset, "avgPrice": 0.4,
            "curPrice": 1.0}


def test_grid_pairs_from_earliest_buy_with_no_buy_skipped():
    """#15 — emits (asset, entry_ts) + one pair per horizon, anchored on the
    EARLIEST BUY; a position with no BUY emits nothing; deterministic."""
    entry = 1_700_000_000
    closed = [
        _pos("c1", "tok-1"),          # has BUYs
        _pos("c2", "tok-2"),          # has NO BUY in activity → skipped
    ]
    activity = [
        _buy("c1", "tok-1", entry + 500),   # later buy
        _buy("c1", "tok-1", entry),         # earliest — the entry anchor
    ]
    pairs = copy_latency_grid_pairs(closed, activity)

    expected = {("tok-1", entry)}
    for h in HORIZONS_MIN:
        expected.add(("tok-1", entry + h * 60))
    assert pairs == expected                       # c2 contributes nothing
    assert ("tok-2", entry) not in pairs

    # deterministic — same input, same set
    assert copy_latency_grid_pairs(closed, activity) == pairs
