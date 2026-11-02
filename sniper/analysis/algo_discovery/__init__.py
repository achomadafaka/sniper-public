"""Algorithm Discovery workflow — end-to-end orchestration of the existing
wallet-analysis stages (dossier → rule extraction → OOS backtest → decision
gate → shadow-copy paper-mirror).

Spec: docs/superpowers/specs/2026-05-29-algorithm-discovery-workflow-design.md
Plan: docs/superpowers/plans/2026-05-29-algorithm-discovery-workflow.md

Strategy-intelligence, not a copy bot: the workflow never emits live triggers,
real-money sizing, or expected PnL. A `proceed_to_paper` verdict means a rule is
*eligible for paper validation* and nothing more.
"""
from __future__ import annotations

from sniper.analysis.algo_discovery.decision_gate import (
    DecisionVerdict,
    GATE_VERSION,
    evaluate,
)
from sniper.analysis.algo_discovery.classifier import (
    BehaviorClassification,
    classify_wallet_behavior,
)
from sniper.analysis.algo_discovery.paper_eval import evaluate_paper_results

__all__ = [
    "DecisionVerdict",
    "GATE_VERSION",
    "evaluate",
    "BehaviorClassification",
    "classify_wallet_behavior",
    "evaluate_paper_results",
]
