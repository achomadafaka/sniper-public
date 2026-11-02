from sniper.algos.hl_microstructure.fusion import FusionContext
from sniper.algos.hl_microstructure.models import FeatureSnapshot
from sniper.algos.hl_microstructure.policies_gated import evaluate_fusion_gated_policy


def _snap(**overrides):
    features = {
        "mid": 100.0,
        "spread_bps": 4.0,
        "bid_depth_5": 30_000.0,
        "ask_depth_5": 28_000.0,
        "trade_count_10s": 10,
        "flow_imbalance_10s": 0.5,
        "book_imbalance_5": 0.4,
        "return_5s": 0.0,
        "realized_vol_60s": 0.004,
        "funding": None,
        "premium": None,
    }
    features.update(overrides)
    return FeatureSnapshot("BTC", 1_000, "hlm_fv1", features, "ok", [])


def test_b1_enters_when_two_fast_families_agree_in_tight_deep_active_book():
    d = evaluate_fusion_gated_policy(_snap(), FusionContext(), arm="b1")
    assert d.policy_id == "fusion_gated_b1"
    assert d.action == "enter"
    assert d.side == "long"


def test_b1_rejects_when_only_one_fast_family_even_if_carry_confirms():
    # flow (fast) long + premium (carry) long == 2 families agree, but only 1 is fast.
    snap = _snap(book_imbalance_5=0.0, premium=-0.004)
    d = evaluate_fusion_gated_policy(snap, FusionContext(), arm="b1")
    assert d.action == "skip"
    assert d.reason == "needs_two_fast_families"


def test_b1_rejects_spread_wider_than_gate_even_when_below_control():
    d = evaluate_fusion_gated_policy(_snap(spread_bps=10.0), FusionContext(), arm="b1")
    assert d.action == "skip"
    assert d.reason == "spread_too_wide"


def test_b1_rejects_shallow_book():
    d = evaluate_fusion_gated_policy(_snap(bid_depth_5=5_000.0), FusionContext(), arm="b1")
    assert d.action == "skip"
    assert d.reason == "shallow_book"


def test_b1_rejects_low_trade_count():
    d = evaluate_fusion_gated_policy(_snap(trade_count_10s=5), FusionContext(), arm="b1")
    assert d.action == "skip"
    assert d.reason == "not_enough_flow"


def test_b2_rejects_when_estimated_edge_below_cost_plus_buffer():
    # Passes B1 (flow+book long, tight/deep/active) but moderate confidence + low vol
    # gives est edge < round-trip cost + buffer.
    d = evaluate_fusion_gated_policy(_snap(), FusionContext(), arm="b2")
    assert d.action == "skip"
    assert d.reason == "cost_gate_not_cleared"


def test_b2_enters_when_estimated_edge_clears_cost_plus_buffer():
    # Strong agreement + high vol -> est edge clears 9 + 4 + 3 bps.
    snap = _snap(flow_imbalance_10s=0.9, book_imbalance_5=0.8, realized_vol_60s=0.01)
    d = evaluate_fusion_gated_policy(snap, FusionContext(), arm="b2")
    assert d.policy_id == "fusion_gated_b2"
    assert d.action == "enter"
    assert d.side == "long"
