from sniper.algos.hl_microstructure.models import FeatureSnapshot
from sniper.algos.hl_microstructure.policies import PolicyConfig, evaluate_policies


def _snapshot(**overrides):
    features = {
        "mid": 100.0,
        "spread_bps": 2.0,
        "book_imbalance_5": 0.0,
        "flow_imbalance_10s": 0.0,
        "return_5s": 0.0,
        "return_15s": 0.0,
        "trade_count_10s": 0,
        "source_age_ms": 0,
    }
    features.update(overrides)
    return FeatureSnapshot("BTC", 10_000, "hlm_fv0", features, "ok", [])


def test_baseline_lane_always_skips_with_reason():
    decisions = evaluate_policies(_snapshot(), PolicyConfig())
    baseline = next(d for d in decisions if d.policy_id == "baseline_no_trade")

    assert baseline.action == "skip"
    assert baseline.reason == "control_lane"


def test_flow_momentum_enters_when_flow_and_return_confirm():
    decisions = evaluate_policies(
        _snapshot(flow_imbalance_10s=0.80, return_5s=0.001, trade_count_10s=8),
        PolicyConfig(),
    )
    flow = next(d for d in decisions if d.policy_id == "flow_momentum")

    assert flow.action == "enter"
    assert flow.side == "long"


def test_policy_skips_when_spread_is_too_wide():
    decisions = evaluate_policies(
        _snapshot(flow_imbalance_10s=0.90, return_5s=0.001, trade_count_10s=8, spread_bps=50.0),
        PolicyConfig(max_spread_bps=10.0),
    )
    assert all(d.action == "skip" for d in decisions)
    assert {d.reason for d in decisions if d.policy_id != "baseline_no_trade"} == {"spread_too_wide"}
