"""Counterfactual Selection Engine (CSE).

Tells HUMAN market-selection from ALGORITHMIC market-selection — and, if
algorithmic, reverse-engineers the selection rule — by comparing a wallet's
picks against the matched *unselected candidate markets* it could have chosen.

Specs: docs/superpowers/specs/2026-05-30-selection-reconstruction-engine-design.md
       + sre-f0..f5 (F0 methodology guards are mandatory & cross-cutting).

Strategy-intelligence, decision-support only: the engine never emits a live
trigger, sizing, or expected PnL, and never claims to have *proved* intent
(human vs bot). Forward paper is the arbiter.

Build status (foundation-up):
  F1 entity_layers        — selection vs execution grain
  F0 guards               — leakage tags, FDR, negative controls, candidate quality
  F2 pit_market_context   — point-in-time universe + price (the counterfactual)
  F3/F4 matrix            — stratified decision matrix (picks vs unselected)
  F5-F10 detection        — within-group discriminators, selection edge, coverage
  F11 rule_learner        — interpretable threshold rules, leakage-guarded, OOS
  F12 verdict             — 4 evidence dims + routed classification (no certainty)
  runner                  — chains F1→F2→F3/F4→detection(FDR)→learner→verdict
Data feed: scripts/build_cse_pit_index.py builds the MarketContext from the
Becker archive (block→epoch calibrated; trades carry no timestamp).
"""
from __future__ import annotations

from sniper.analysis.counterfactual_selection.entity_layers import (
    build_engagements, build_clips, build_sessions, build_baskets,
    selection_cadence, selection_schedule, execution_intensity,
)
from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext
from sniper.analysis.counterfactual_selection.matrix import build_decision_matrix
from sniper.analysis.counterfactual_selection.detection import (
    within_group_discriminator, selection_edge, coverage,
)
from sniper.analysis.counterfactual_selection.rule_learner import learn_selection_rules
from sniper.analysis.counterfactual_selection.verdict import synthesize_verdict
from sniper.analysis.counterfactual_selection.runner import run_counterfactual_selection
from sniper.analysis.counterfactual_selection import guards

__all__ = [
    "build_engagements", "build_clips", "build_sessions", "build_baskets",
    "selection_cadence", "selection_schedule", "execution_intensity",
    "MarketContext", "build_decision_matrix",
    "within_group_discriminator", "selection_edge", "coverage",
    "learn_selection_rules", "synthesize_verdict", "run_counterfactual_selection",
    "guards",
]
