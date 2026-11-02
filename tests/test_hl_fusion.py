from sniper.algos.hl_microstructure.catalysts import CatalystSignal
from sniper.algos.hl_microstructure.experts import ExpertSignal
from sniper.algos.hl_microstructure.fusion import FusionContext, dynamic_exit_plan, evaluate_fusion_policy
from sniper.algos.hl_microstructure.liquidation import LiquidationStress
from sniper.algos.hl_microstructure.models import FeatureSnapshot
from sniper.algos.hl_microstructure.radar import RadarCandidate


def _snapshot(asset="ZEC", **overrides):
    features = {
        "mid": 100.0,
        "spread_bps": 4.0,
        "book_imbalance_5": 0.0,
        "flow_imbalance_10s": 0.0,
        "return_5s": 0.0,
        "return_15s": 0.0,
        "realized_vol_60s": 0.001,
        "trade_count_10s": 8,
        "source_age_ms": 20,
    }
    features.update(overrides)
    return FeatureSnapshot(asset, 20_000, "hlm_fv0", features, "ok", [])


def _ctx(*, catalyst=None, liquidation=None):
    return FusionContext(
        catalysts={"ZEC": catalyst} if catalyst else {},
        liquidation_stress={"ZEC": liquidation} if liquidation else {},
        radar_candidates={
            "ZEC": RadarCandidate("ZEC", 12.0, 1, ["volume", "funding_extreme"], 80_000_000, 10_000_000, -0.0001, -0.002)
        },
    )


def test_fusion_rejects_single_source_catalyst():
    catalyst = CatalystSignal("ZEC", "long", 0.9, "ZEC listing confirmed", "coindesk", "listing", "explicit_symbol", 19_000)

    decision = evaluate_fusion_policy(_snapshot(), _ctx(catalyst=catalyst))

    assert decision.action == "skip"
    assert decision.reason == "needs_two_source_families"


def test_fusion_rejects_single_source_liquidation():
    liq = LiquidationStress("ZEC", "short", 0.9, 900_000, 0.4, "isolated_whale_near_liquidation")

    decision = evaluate_fusion_policy(_snapshot(), _ctx(liquidation=liq))

    assert decision.action == "skip"
    assert decision.reason == "needs_two_source_families"


def test_fusion_enters_when_flow_book_and_catalyst_align():
    catalyst = CatalystSignal("ZEC", "long", 0.8, "ZEC upgrade confirmed", "theblock", "upgrade", "explicit_symbol", 19_000)
    snap = _snapshot(flow_imbalance_10s=0.72, book_imbalance_5=0.42, return_5s=0.001)

    decision = evaluate_fusion_policy(snap, _ctx(catalyst=catalyst))

    assert decision.policy_id == "fusion_radar"
    assert decision.action == "enter"
    assert decision.side == "long"
    assert decision.confidence >= 0.6
    assert "fusion" in decision.reason


def test_fusion_can_use_reliable_expert_vote_as_source_family():
    expert = ExpertSignal("flow_30s", "flow", "long", 0.82, 30_000, "flow", 0.75)
    snap = _snapshot(book_imbalance_5=0.0, flow_imbalance_10s=0.0, return_5s=0.0012)

    decision = evaluate_fusion_policy(snap, FusionContext(expert_signals={"ZEC": [expert]}))

    assert decision.action == "enter"
    assert decision.side == "long"
    assert "expert" in decision.metadata["source_votes"]


def test_dynamic_exit_plan_uses_volatility_not_fixed_only():
    plan = dynamic_exit_plan(_snapshot(realized_vol_60s=0.004), side="long")

    assert plan["stop_loss_pct"] > 0.0015
    assert plan["take_profit_pct"] > plan["stop_loss_pct"]
    assert plan["time_stop_ms"] >= 120_000
