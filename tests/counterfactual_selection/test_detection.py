"""Tests for CSE detection battery on synthetic decision matrices."""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.detection import (
    within_group_discriminator, selection_edge, coverage,
)


def _row(gid, picked, price, resolved=None):
    return {"group_id": gid, "picked": picked, "features": {"price": price}, "resolved": resolved}


def test_discriminator_detects_real_separation():
    # group g1: picks at ~0.30, unpicked at ~0.60 → strong, real separation
    rows = [_row("g1", 1, 0.28 + 0.005 * i) for i in range(10)] + \
           [_row("g1", 0, 0.58 + 0.005 * i) for i in range(10)]
    res = within_group_discriminator(rows, "price", n_perm=300, seed=1)
    assert len(res) == 1
    r = res[0]
    assert r["direction"] == "picked_lower" and r["smd"] < -1.0
    assert r["p_value"] < 0.05


def test_discriminator_finds_nothing_in_noise():
    # picks and unpicked drawn from the same spread → no real separation
    vals = [0.40, 0.55, 0.45, 0.60, 0.50, 0.42, 0.58, 0.47, 0.53, 0.49]
    rows = [_row("g1", 1, v) for v in vals] + [_row("g1", 0, v) for v in vals]
    res = within_group_discriminator(rows, "price", n_perm=300, seed=1)
    assert res[0]["p_value"] > 0.05   # not significant


def test_selection_edge_supported_when_picks_outresolve():
    rows = [_row("g", 1, 0.4, resolved=1) for _ in range(14)] + \
           [_row("g", 1, 0.4, resolved=0) for _ in range(6)] + \
           [_row("g", 0, 0.4, resolved=1) for _ in range(8)] + \
           [_row("g", 0, 0.4, resolved=0) for _ in range(12)]
    res = selection_edge(rows, n_perm=500, seed=3)
    assert res["picked_win_rate"] == 0.7 and res["unpicked_win_rate"] == 0.4
    assert res["edge"] > 0 and res["supported"]


def test_selection_edge_not_supported_when_equal():
    rows = [_row("g", 1, 0.4, resolved=(i % 2)) for i in range(20)] + \
           [_row("g", 0, 0.4, resolved=(i % 2)) for i in range(20)]
    res = selection_edge(rows, n_perm=500, seed=3)
    assert abs(res["edge"]) < 1e-9 and not res["supported"]


def test_selection_edge_underpowered():
    rows = [_row("g", 1, 0.4, resolved=1) for _ in range(3)]
    assert selection_edge(rows)["supported"] is None


def test_coverage_exhaustive_vs_selective():
    # exhaustive: enters ~all longshots
    rows = [_row("g", 1, 0.08) for _ in range(9)] + [_row("g", 0, 0.08)]
    cov = coverage(rows, lambda f: f.get("price", 1) < 0.10)
    assert cov["coverage"] == 0.9
    # selective: enters few of them
    rows2 = [_row("g", 1, 0.08) for _ in range(2)] + [_row("g", 0, 0.08) for _ in range(8)]
    assert coverage(rows2, lambda f: f.get("price", 1) < 0.10)["coverage"] == 0.2
