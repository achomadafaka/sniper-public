"""Tests for the algorithm-discovery decision gate.

Spec: docs/superpowers/specs/2026-05-29-algorithm-discovery-workflow-design.md §4.2
"""
from __future__ import annotations

from sniper.analysis.algo_discovery.decision_gate import evaluate, DecisionVerdict


def _bt(**kw):
    """A passing-by-default BacktestResult-shaped dict: 34/40 underdog buys at
    0.40 → WR 0.85 vs null 0.40 → strong z; profitable."""
    base = dict(
        harness_version="backtest_v1",
        oos_simulated_pnl_usdc=120.0,
        oos_simulated_wins=34,
        oos_simulated_losses=6,
        oos_match_count=40,
        oos_simulated_win_rate=0.85,
        oos_actual_pnl_usdc_on_same_positions=90.0,
        caveats=[],
        per_position_results=[{"sim_entry_price": 0.40, "final_price": 1.0}] * 40,
    )
    base.update(kw)
    return base


# ===== disqualifying caveats =====

def test_unsupported_exit_mode_rejects():
    v = evaluate(_bt(caveats=["unsupported_exit_mode:pre_resolution"]),
                 {"exit_rule": {"mode": "pre_resolution"}})
    assert v.verdict == "reject"
    assert "exit_mode_unsupported" in v.reason_codes


def test_no_market_filter_rejects():
    v = evaluate(_bt(caveats=["rule_has_no_market_filter"]), {})
    assert v.verdict == "reject"
    assert "no_market_filter" in v.reason_codes


# ===== sample adequacy =====

def test_too_few_oos_is_insufficient():
    v = evaluate(_bt(oos_simulated_wins=5, oos_simulated_losses=3, oos_match_count=8,
                     per_position_results=[{"sim_entry_price": 0.4, "final_price": 1.0}] * 8), {})
    assert v.verdict == "insufficient_data"
    assert "oos_sample_below_floor" in v.reason_codes


def test_low_confidence_flag_below_100():
    v = evaluate(_bt(), {})  # n_decided=40 → proceed but flagged
    assert v.verdict == "proceed_to_paper"
    assert "low_confidence_sample" in v.reason_codes


# ===== profitability + edge =====

def test_unprofitable_rejects():
    v = evaluate(_bt(oos_simulated_pnl_usdc=-5.0), {})
    assert v.verdict == "reject"
    assert "oos_not_profitable" in v.reason_codes


def test_profitable_but_below_price_null_rejects():
    # 40 buys at 0.40, 18 wins → WR 0.45; z=(0.45-0.40)/sqrt(.4*.6/40)=0.05/0.0775≈0.65 < 1.96
    v = evaluate(_bt(oos_simulated_wins=18, oos_simulated_losses=22, oos_match_count=40,
                     oos_simulated_pnl_usdc=5.0,
                     per_position_results=[{"sim_entry_price": 0.40, "final_price": 1.0}] * 40), {})
    assert v.verdict == "reject"
    assert "edge_not_significant_vs_price_null" in v.reason_codes


def test_strong_edge_proceeds():
    v = evaluate(_bt(), {})
    assert v.verdict == "proceed_to_paper"
    assert v.metrics["edge_z"] is not None and v.metrics["edge_z"] >= 1.96


def test_high_entry_price_needs_high_win_rate():
    # Favorites at 0.90: 36/40 wins → WR 0.90 == null 0.90 → z≈0 → no edge → reject.
    v = evaluate(_bt(oos_simulated_wins=36, oos_simulated_losses=4, oos_match_count=40,
                     per_position_results=[{"sim_entry_price": 0.90, "final_price": 1.0}] * 40), {})
    assert v.verdict == "reject"
    assert "edge_not_significant_vs_price_null" in v.reason_codes


def test_no_per_position_is_insufficient():
    v = evaluate(_bt(per_position_results=[]), {})
    assert v.verdict == "insufficient_data"
    assert "no_per_position_for_null" in v.reason_codes


# ===== slippage robustness =====

def test_slippage_fragile_rejects():
    v = evaluate(_bt(), {}, stressed_backtest=_bt(oos_simulated_pnl_usdc=-15.0))
    assert v.verdict == "reject"
    assert "edge_slippage_fragile" in v.reason_codes


def test_slippage_robust_proceeds():
    v = evaluate(_bt(), {}, stressed_backtest=_bt(oos_simulated_pnl_usdc=40.0))
    assert v.verdict == "proceed_to_paper"


# ===== fidelity soft warn =====

def test_sim_exceeds_wallet_fidelity_warns_but_proceeds():
    v = evaluate(_bt(oos_simulated_pnl_usdc=500.0, oos_actual_pnl_usdc_on_same_positions=10.0), {})
    assert v.verdict == "proceed_to_paper"
    assert "sim_exceeds_wallet_fidelity_warn" in v.reason_codes


# ===== shape =====

def test_verdict_is_dataclass_with_version():
    v = evaluate(_bt(), {})
    assert isinstance(v, DecisionVerdict)
    assert v.gate_version == "algo_discovery_decision_gate_v1"
    assert v.verdict in {"proceed_to_paper", "reject", "insufficient_data"}
