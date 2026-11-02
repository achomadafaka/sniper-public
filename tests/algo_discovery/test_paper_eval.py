"""Tests for the forward-paper evaluation rubric (reuses the decision gate)."""
from __future__ import annotations

from sniper.analysis.algo_discovery.paper_eval import evaluate_paper_results


def _pos(status, entry_price, pnl):
    return {"status": status, "entry_price": entry_price, "pnl_usdc": pnl}


def test_favorite_buyer_paper_winstreak_still_rejected():
    # 30 paper wins at entry ~0.97 → high WR, profitable, but null WR ~0.97 → no edge.
    positions = [_pos("resolution_win", 0.97, 0.3) for _ in range(30)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "reject"
    assert "edge_not_significant_vs_price_null" in v.reason_codes


def test_genuine_underdog_edge_confirms():
    # 26/30 wins at entry 0.40 → WR 0.87 vs null 0.40 → strong edge, profitable.
    positions = [_pos("resolution_win", 0.40, 1.5) for _ in range(26)] + \
                [_pos("resolution_loss", 0.40, -0.4) for _ in range(4)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "proceed_to_paper"   # forward: paper edge confirmed


def test_too_few_resolved_is_insufficient():
    positions = [_pos("resolution_win", 0.40, 1.0) for _ in range(5)] + \
                [_pos("open", 0.40, 0.0) for _ in range(50)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "insufficient_data"
    assert "oos_sample_below_floor" in v.reason_codes


def test_unprofitable_paper_rejected():
    positions = [_pos("resolution_loss", 0.40, -0.4) for _ in range(25)] + \
                [_pos("resolution_win", 0.40, 0.6) for _ in range(5)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "reject"


def test_open_positions_ignored():
    positions = [_pos("open", 0.5, 0.0) for _ in range(40)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "insufficient_data"  # nothing resolved yet


def test_accepts_avg_price_key():
    positions = [{"status": "resolution_win", "avgPrice": 0.40, "pnl_usdc": 1.5} for _ in range(26)] + \
                [{"status": "resolution_loss", "avgPrice": 0.40, "pnl_usdc": -0.4} for _ in range(4)]
    v = evaluate_paper_results(positions)
    assert v.verdict == "proceed_to_paper"
