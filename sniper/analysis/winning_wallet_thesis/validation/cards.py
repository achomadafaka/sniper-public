from __future__ import annotations

import math

from sniper.analysis.thesis_cards.contract import ThesisCard, producer_card
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding
from sniper.analysis.winning_wallet_thesis.types import EdgeCluster

# Detectors whose `effect` is in percentage points (vs a [0,1] fraction).
_PP_EFFECT_METHODS = {"specialist_domain_edge", "recurring_template_edge"}


def _proxy_p_value(finding: MethodFinding) -> float | None:
    """Normal-approx one-sided p-value that the cluster calibration edge > 0, over n
    independent events (sd of a proportion ~0.5). This is the in-sample FDR input the
    conveyor admission requires; it is a PROXY until detectors carry exact binomial tests.
    Only computed for supported, positive-effect statistical findings."""
    n_events = finding.n_events if finding.n_events > 0 else finding.n
    if finding.effect is None or n_events < 2 or not finding.state.startswith("supported"):
        return None
    edge_frac = finding.effect / 100.0 if finding.method in _PP_EFFECT_METHODS else finding.effect
    if edge_frac <= 0:
        return None
    # use the INDEPENDENT event count (positions within one event are correlated). Falls back to
    # n when a detector doesn't set n_events. Fewer independent obs -> larger (less confident) p.
    z = edge_frac * math.sqrt(max(n_events, 1)) / 0.5
    return 0.5 * math.erfc(z / math.sqrt(2.0))      # standard-normal survival function


def _state_for_card(finding: MethodFinding) -> str:
    if finding.state in {"supported_proxy", "supported_direct"}:
        return "supported"
    if finding.state == "contradicted":
        return "contradicted"
    if finding.state == "blocked_missing_feed":
        return "underpowered"
    return "descriptive_only"


def method_finding_to_card(wallet: str, cluster: EdgeCluster, finding: MethodFinding) -> ThesisCard:
    design = finding.forward_test_design.as_dict() if finding.forward_test_design else None
    n_events = finding.n_events if finding.n_events > 0 else finding.n
    return producer_card(
        card_id=f"winning_wallet:{cluster.cluster_id}:{finding.method}",
        lens=f"{cluster.label} / {finding.method}",
        n=n_events,
        score=finding.score,
        effect=finding.effect,
        p_value=_proxy_p_value(finding),
        placebo_passed=True if finding.state in {"supported_proxy", "supported_direct"} else None,
        evidence_state=_state_for_card(finding),
        evidence={
            "wallet": wallet,
            "cluster_id": cluster.cluster_id,
            "method": finding.method,
            "data_tier": "HAVE" if finding.state != "blocked_missing_feed" else "BLOCKED",
            "method_evidence": finding.evidence,
            "n_positions": finding.n,
            "n_events": n_events,
        },
        card_version="winning_wallet_v1",
        forward_test_design=design,
    )
