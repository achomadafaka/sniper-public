"""fwlm-v0 paper sim — §7 symbolic exits frozen at signal_ts."""
import pytest

from sniper.hyperlive import far_whale as FW
from sniper.hyperlive import far_whale_sim as SIM


def _cand(direction="down"):
    liq = 2950.0 if direction == "down" else 3050.0
    side = "long" if direction == "down" else "short"
    return {"asset": "ETH", "whale_address": "0xabc", "liquidation_price": liq,
            "mid_price": 3000.0, "distance_bps": abs(liq - 3000.0) / 3000.0 * 1e4,
            "direction_to_liquidation": direction, "position_side": side,
            "liquidation_type": f"{side}_liquidation", "position_notional": 2e6,
            "position_size": 100.0, "spread_bps": 2.0}


def test_plan_entry_symbolic_levels_down():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    D = 3000.0 - 2950.0
    assert e["entry_price"] == 3000.0
    assert e["target_price"] == 3000.0 - FW.TARGET_FRAC * D      # 2962.5
    assert e["adverse_stop_price"] == 3000.0 + FW.ADVERSE_FRAC * D  # 3025.0


def test_plan_entry_symbolic_levels_up():
    e = SIM.plan_entry(_cand("up"), 1_000_000)
    D = 3050.0 - 3000.0
    assert e["target_price"] == 3000.0 + FW.TARGET_FRAC * D
    assert e["adverse_stop_price"] == 3000.0 - FW.ADVERSE_FRAC * D


def test_step_target_hit():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    assert SIM.step_entry(e, 2990.0, 1_180_000) is None
    ex = SIM.step_entry(e, 2960.0, 1_360_000)
    assert ex["exit_reason"] == "target_hit" and ex["exit_price"] == 2960.0
    assert e["mfe_bps"] > 0


def test_step_adverse_stop():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 3026.0, 1_180_000)
    assert ex["exit_reason"] == "adverse_stop"
    assert e["mae_bps"] < 0


def test_step_time_stop_at_horizon():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    assert SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS - 1) is None
    ex = SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS)
    assert ex["exit_reason"] == "time_stop"


def test_step_target_moved_exits_immediately():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2995.0, 1_180_000, whale_event="liq_price_moved")
    assert ex["exit_reason"] == "target_moved"


def test_step_target_moved_beats_target_hit_when_both_fire_same_bar():
    """Collision-precedence regression: mid=2960.0 alone resolves target_hit (proven
    by test_step_target_hit above, same value). When a whale_event ALSO fires on that
    same bar, target_moved must win — the highest-precedence reason, not target_hit."""
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2960.0, 1_360_000, whale_event="liq_price_moved")
    assert ex["exit_reason"] == "target_moved"
    assert ex["exit_price"] == 2960.0


def test_settle_pnl_net_of_fee_and_spread():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2960.0, 1_360_000)
    out = SIM.settle(e, ex)
    gross_bps = (3000.0 - 2960.0) / 3000.0 * 1e4          # ~133.3 (short wins on drop)
    net_bps = gross_bps - FW.FEE_BPS - 2.0                # fee + signal-time spread
    assert abs(out["paper_pnl"] - net_bps / 1e4 * FW.PAPER_NOTIONAL) < 0.01
    assert out["migrated_toward_liq"] == 1                # mfe >= 25% of D
    assert out["hit_liq_level"] == 0


def test_settle_migrated_label_uses_25pct_of_distance():
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    # move 10% of D toward level, then time-stop: not migrated
    SIM.step_entry(e, 2995.0, 1_180_000)
    ex = SIM.step_entry(e, 2995.0, 1_000_000 + FW.HORIZON_MS)
    out = SIM.settle(e, ex)
    assert out["migrated_toward_liq"] == 0


def test_settle_includes_fee_assumption_for_store_persistence():
    """far_whale_store.settle_entry reads outcome["fee_assumption"] and falls back to
    the entry's stored value only when the outcome omits it. settle() must stamp the
    frozen fee_bps actually used (spec §10/§16 — auditable data fact, not implied)."""
    e = SIM.plan_entry(_cand("down"), 1_000_000)
    ex = SIM.step_entry(e, 2960.0, 1_360_000)
    out = SIM.settle(e, ex)
    assert out["fee_assumption"] == FW.FEE_BPS
    out2 = SIM.settle(e, ex, fee_bps=12.5)
    assert out2["fee_assumption"] == 12.5


def test_simulate_path_pure_replay_matches_stepping():
    path = [(1_000_000, 3000.0), (1_180_000, 2990.0), (1_360_000, 2960.0)]
    out = SIM.simulate_path("down", 166.67, path, spread_bps=2.0)
    assert out["exit_reason"] == "target_hit"
    assert out["migrated_toward_liq"] == 1


def test_simulate_path_empty_returns_none():
    assert SIM.simulate_path("down", 166.67, [], spread_bps=2.0) is None


def test_simulate_path_unresolved_before_horizon_is_loggable():
    """Non-empty path that never hits target/adverse/time_stop must NOT collapse to
    None (that's reserved for no-data-at-all) — it returns a settle()-shaped dict
    with exit_reason="unresolved", pinned to the last observed point, so Task 6/7
    dropped-vs-logged accounting can tell "no data" apart from "ran out of coverage"."""
    path = [(1_000_000, 3000.0), (1_180_000, 2990.0), (1_360_000, 2980.0)]
    out = SIM.simulate_path("down", 166.67, path, spread_bps=2.0)
    assert out is not None
    assert out["exit_reason"] == "unresolved"
    assert out["exit_ts_ms"] == 1_360_000
    assert out["exit_price"] == 2980.0
    assert out["mfe_bps"] > 0


def test_simulate_path_rejects_non_positive_d_bps():
    """Task 6 will call simulate_path with synthesized d_bps outside the upstream
    50bps candidate gate. d_bps<=0 collapses target/adverse to entry_price and would
    resolve target_hit trivially on the first tick — must fail loudly instead."""
    path = [(1_000_000, 3000.0), (1_180_000, 3000.0)]
    with pytest.raises(ValueError):
        SIM.simulate_path("down", 0.0, path, spread_bps=2.0)


def test_plan_entry_rejects_zero_distance():
    cand = _cand("down")
    cand["liquidation_price"] = cand["mid_price"]  # D == 0
    with pytest.raises(ValueError):
        SIM.plan_entry(cand, 1_000_000)
