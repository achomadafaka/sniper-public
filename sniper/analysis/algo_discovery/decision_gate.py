"""Decision gate — pre-registered kill-criteria over an OOS backtest result.

Pure function. Given a `BacktestResult` dict (from
`sniper.analysis.backtest_harness.backtest_rule_on_wallet`) plus the
`wallet_rule_v1` it was run on, return a `DecisionVerdict`:
`proceed_to_paper | reject | insufficient_data`, with reason codes and the
metrics it judged.

Methodology (grounded in OOS-validation literature + prediction-market domain):
- Sample adequacy: <20 decided OOS trades is noise (refuse); <100 is low-confidence.
- Profitability is judged on PnL, not raw win-rate.
- EDGE is measured against the prediction-market null: a position entered at
  price p wins with probability p by chance, so the no-edge baseline win-rate is
  the MEAN ENTRY PRICE of the OOS positions — NOT 0.5. We require the observed
  OOS win-rate to beat that price-implied null at a one-sided binomial z >= 1.96.
- Slippage robustness: a stressed-slippage re-run must stay profitable.
- The single 75/25 OOS split is a SCREEN, not proof; the shadow-copy
  paper-forward is the true never-touched holdout (see spec §9).

Thresholds via env (codebase inline-getenv convention, cf. strategies/base.py).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping

GATE_VERSION = "algo_discovery_decision_gate_v1"

MIN_OOS_DECIDED = int(os.getenv("ALGO_DISCOVERY_MIN_OOS_DECIDED", "20"))
LOW_CONFIDENCE_OOS = int(os.getenv("ALGO_DISCOVERY_LOW_CONFIDENCE_OOS", "100"))
EDGE_Z = float(os.getenv("ALGO_DISCOVERY_EDGE_Z", "1.96"))
SLIPPAGE_STRESS_PCT = float(os.getenv("ALGO_DISCOVERY_SLIPPAGE_STRESS_PCT", "0.015"))
SIM_FIDELITY_MULT = float(os.getenv("ALGO_DISCOVERY_SIM_FIDELITY_MULT", "3.0"))

# Backtest caveat prefix → reason code. Presence of any disqualifies the rule.
_DISQUALIFYING = {
    "unsupported_exit_mode": "exit_mode_unsupported",
    "rule_has_no_market_filter": "no_market_filter",
    "no_oos_matches_for_rule": "no_oos_matches",
    "no_closed_positions": "no_closed_positions",
}


@dataclass
class DecisionVerdict:
    gate_version: str
    verdict: str            # proceed_to_paper | reject | insufficient_data
    reason_codes: list[str]
    metrics: dict[str, Any]


def _caveat_reason(caveats: list[str]) -> str | None:
    for cav in caveats or []:
        for prefix, code in _DISQUALIFYING.items():
            if str(cav).startswith(prefix):
                return code
    return None


def _binomial_z(wins: int, n: int, p0: float) -> float | None:
    """One-sided z for observed win-rate vs null p0. None if undefined."""
    if n <= 0:
        return None
    p0 = min(max(p0, 1e-6), 1.0 - 1e-6)
    se = math.sqrt(p0 * (1.0 - p0) / n)
    if se == 0:
        return None
    return (wins / n - p0) / se


def _null_win_rate(per_position: list[Mapping]) -> float | None:
    """No-edge baseline = mean entry price of the OOS positions. A position
    bought at 0.40 wins ~40% of the time by chance; edge is beating that."""
    prices = [
        float(r["sim_entry_price"])
        for r in (per_position or [])
        if r.get("sim_entry_price") is not None
    ]
    return sum(prices) / len(prices) if prices else None


def evaluate(
    backtest: Mapping[str, Any],
    rule: Mapping[str, Any],
    *,
    stressed_backtest: Mapping[str, Any] | None = None,
) -> DecisionVerdict:
    """Apply the kill-criteria. `stressed_backtest` is an optional re-run at a
    higher slippage; when supplied, a sign flip to non-profit rejects the rule."""
    reasons: list[str] = []
    wins = int(backtest.get("oos_simulated_wins") or 0)
    losses = int(backtest.get("oos_simulated_losses") or 0)
    n_decided = wins + losses
    pnl = float(backtest.get("oos_simulated_pnl_usdc") or 0.0)
    null_wr = _null_win_rate(list(backtest.get("per_position_results") or []))
    metrics: dict[str, Any] = {
        "n_decided": n_decided,
        "oos_pnl_usdc": pnl,
        "oos_win_rate": (wins / n_decided if n_decided else None),
        "null_win_rate": null_wr,
    }

    # 1. disqualifying caveats
    cav = _caveat_reason(list(backtest.get("caveats") or []))
    if cav:
        return DecisionVerdict(GATE_VERSION, "reject", [cav], metrics)
    if "rule_size_missing_using_10usdc_default" in (backtest.get("caveats") or []):
        reasons.append("synthetic_size_used")

    # 2. sample adequacy
    if n_decided < MIN_OOS_DECIDED:
        return DecisionVerdict(GATE_VERSION, "insufficient_data",
                               reasons + ["oos_sample_below_floor"], metrics)
    if n_decided < LOW_CONFIDENCE_OOS:
        reasons.append("low_confidence_sample")

    # 3. profitability (PnL, not win-rate)
    if pnl <= 0:
        return DecisionVerdict(GATE_VERSION, "reject",
                               reasons + ["oos_not_profitable"], metrics)

    # 4. edge vs the price-implied null (NOT 0.5)
    if null_wr is None:
        return DecisionVerdict(GATE_VERSION, "insufficient_data",
                               reasons + ["no_per_position_for_null"], metrics)
    z = _binomial_z(wins, n_decided, null_wr)
    metrics["edge_z"] = z
    if z is None or z < EDGE_Z:
        return DecisionVerdict(GATE_VERSION, "reject",
                               reasons + ["edge_not_significant_vs_price_null"], metrics)

    # 5. slippage robustness
    if stressed_backtest is not None:
        spnl = float(stressed_backtest.get("oos_simulated_pnl_usdc") or 0.0)
        metrics["stressed_oos_pnl_usdc"] = spnl
        if spnl <= 0:
            return DecisionVerdict(GATE_VERSION, "reject",
                                   reasons + ["edge_slippage_fragile"], metrics)

    # 6. sim-vs-wallet fidelity (soft warn, not a hard kill in v1)
    actual = float(backtest.get("oos_actual_pnl_usdc_on_same_positions") or 0.0)
    if actual > 0 and pnl > SIM_FIDELITY_MULT * actual:
        reasons.append("sim_exceeds_wallet_fidelity_warn")

    return DecisionVerdict(GATE_VERSION, "proceed_to_paper", reasons or ["passed"], metrics)
