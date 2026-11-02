"""Display-only synthesis of a stored thesis profile into a plain verdict + top patterns."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis import narrative as N

BASE = {
    "headline": "x",
    "capital_risk": {"roi_on_peak_deployed_pct": 12.0},
    "shuffle_baseline": {"obs_over_expected": 2.4, "p_value": 0.01},
    "data_gaps": ["first_attention_discovery", "stale_liquidity_sniping"],
    "edge_clusters": [
        {"label": "big NBA favorites", "pnl": 50000, "n_positions": 120, "edge_pp": 4.0, "is_loss_center": 0},
        {"label": "Soccer unders", "pnl": 20000, "n_positions": 60, "edge_pp": 3.0, "is_loss_center": 0},
        {"label": "tiny longshots", "pnl": 500, "n_positions": 10, "edge_pp": 0.2, "is_loss_center": 0},
        {"label": "noise", "pnl": 100, "n_positions": 5, "edge_pp": 0.1, "is_loss_center": 0}],
    "loss_centers": [{"label": "live tennis", "pnl": -8000, "n_positions": 30, "edge_pp": -2.0, "is_loss_center": 1}],
}


def test_synthesize_profitable_provable_top_patterns():
    out = N.synthesize(BASE)
    assert out["state"] == "profitable_provable"        # roi>0 and obs_over_expected>=2
    assert len(out["top_patterns"]) == 3                # capped at 3
    assert out["top_patterns"][0]["label"] == "big NBA favorites"   # ranked by abs(pnl)
    assert out["more_patterns"] == 1                    # 4 edge clusters -> 1 folded away
    assert "NBA" in out["verdict_line"]
    low = out["verdict_line"].lower()
    assert "copy" not in low and "should" not in low and "expected profit" not in low


def test_synthesize_net_loser():
    rep = {**BASE, "capital_risk": {"roi_on_peak_deployed_pct": -5.0},
           "shuffle_baseline": {"obs_over_expected": 1.0}, "edge_clusters": [], "loss_centers": []}
    assert N.synthesize(rep)["state"] == "net_loser"


def test_synthesize_profitable_unproven():
    rep = {**BASE, "shuffle_baseline": {"obs_over_expected": 1.2}}
    assert N.synthesize(rep)["state"] == "profitable_unproven"


def test_synthesize_dedupes_same_pnl_axis_duplicates():
    # 'sports' and 'Sports' are the same positions on different axes -> identical pnl -> one row
    rep = {**BASE, "shuffle_baseline": {"obs_over_expected": 3.0},
           "edge_clusters": [
               {"label": "sports", "pnl": 77084.32, "n_positions": 100, "edge_pp": 3, "is_loss_center": 0},
               {"label": "Sports", "pnl": 77084.32, "n_positions": 100, "edge_pp": 3, "is_loss_center": 0},
               {"label": "held", "pnl": 50000.0, "n_positions": 80, "edge_pp": 2, "is_loss_center": 0}]}
    out = N.synthesize(rep)
    labels = [p["label"] for p in out["top_patterns"]]
    assert labels == ["sports", "held"]            # the duplicate-pnl 'Sports' collapsed
    assert out["more_patterns"] == 0


def test_synthesize_prefers_specific_market_over_generic_axis():
    # a generic axis bucket ('sports') with the BIGGEST pnl must not bury a specific market ('NBA');
    # the verdict should name the market, not the whole-book bucket.
    rep = {**BASE, "shuffle_baseline": {"obs_over_expected": 3.0},
           "edge_clusters": [
               {"label": "sports", "pnl": 90000, "n_positions": 300, "edge_pp": 2, "is_loss_center": 0},
               {"label": "NBA", "pnl": 40000, "n_positions": 110, "edge_pp": 4, "is_loss_center": 0},
               {"label": "held", "pnl": 85000, "n_positions": 280, "edge_pp": 1, "is_loss_center": 0}]}
    out = N.synthesize(rep)
    assert out["top_patterns"][0]["label"] == "NBA"        # specific leads despite lower pnl
    assert "NBA" in out["verdict_line"]
    assert "sports" not in out["verdict_line"] and "held" not in out["verdict_line"]


def test_synthesize_dedupes_by_position_overlap():
    # two clusters 95% the same positions but different pnl -> the lower one is dominated/dropped
    rep = {**BASE, "shuffle_baseline": {"obs_over_expected": 3.0},
           "edge_clusters": [
               {"label": "NBA", "pnl": 50000, "n_positions": 100, "edge_pp": 4, "is_loss_center": 0,
                "member_position_keys": [[f"c{i}", 0, 0] for i in range(100)]},
               {"label": "NBA favorites", "pnl": 48000, "n_positions": 96, "edge_pp": 4, "is_loss_center": 0,
                "member_position_keys": [[f"c{i}", 0, 0] for i in range(96)]},   # 96/96 ⊂ NBA -> dominated
               {"label": "Soccer", "pnl": 20000, "n_positions": 40, "edge_pp": 3, "is_loss_center": 0,
                "member_position_keys": [[f"s{i}", 0, 0] for i in range(40)]}]}
    out = N.synthesize(rep)
    labels = [p["label"] for p in out["top_patterns"]]
    assert labels == ["NBA", "Soccer"]                     # 'NBA favorites' collapsed into 'NBA'


def test_synthesize_surfaces_top_loss_center():
    # the verdict block should name where it LOSES money (worst loss center by |pnl|), not just edges
    out = N.synthesize(BASE)            # BASE has one loss center: "live tennis" -$8000
    assert out["top_loss"] is not None
    assert out["top_loss"]["label"] == "live tennis"
    assert out["top_loss"]["pnl"] == -8000


def test_synthesize_top_loss_none_when_no_losses():
    rep = {**BASE, "loss_centers": []}
    assert N.synthesize(rep)["top_loss"] is None


def test_synthesize_top_loss_picks_worst():
    rep = {**BASE, "loss_centers": [
        {"label": "small leak", "pnl": -1000, "n_positions": 5, "is_loss_center": 1},
        {"label": "big leak", "pnl": -25000, "n_positions": 40, "is_loss_center": 1}]}
    assert N.synthesize(rep)["top_loss"]["label"] == "big leak"


def test_synthesize_loss_only_profile():
    # a net-loser with no edge clusters: no patterns, but the loss center still surfaces
    rep = {"capital_risk": {"roi_on_peak_deployed_pct": -7.0}, "shuffle_baseline": {"obs_over_expected": 0.8},
           "edge_clusters": [], "data_gaps": [],
           "loss_centers": [{"label": "live tennis", "pnl": -12000, "n_positions": 50, "is_loss_center": 1}]}
    out = N.synthesize(rep)
    assert out["state"] == "net_loser" and out["top_patterns"] == []
    assert out["top_loss"]["label"] == "live tennis"
    assert "loses" not in out["verdict_line"].lower()      # verdict is the headline; loss is a separate field


def test_synthesize_handles_empty():
    out = N.synthesize({})
    assert out["state"] in ("mixed", "net_loser", "profitable_unproven")
    assert out["top_patterns"] == [] and out["verdict_line"] and out["top_loss"] is None
