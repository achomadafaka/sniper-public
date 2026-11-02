from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding


def classify_runnability(finding: MethodFinding) -> str:
    """Label whether WE could run an edge, from its blockers/state — honest about what we
    cannot observe (no book/news/fill-identity feeds)."""
    blockers = set(finding.blockers)
    if finding.state == "blocked_missing_feed" or any(b.startswith("missing_") for b in blockers):
        return "data_blocked"
    if "speed" in blockers or "low_latency_required" in blockers:
        return "speed_required"
    if "capital_constraint" in blockers:
        return "capital_required"
    if "private_information" in blockers:
        return "non_replicable"
    if finding.forward_test_design is not None and finding.state.startswith("supported"):
        return "forward_testable"
    return "descriptive_only"
