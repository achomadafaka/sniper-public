"""CSE-F12 — bot-vs-human verdict synthesizer.

Combines the detection battery + rule learner into FOUR independent evidence
dimensions (G7) and a routed classification (G8), never a certainty claim (G9).
The human-vs-bot sentence is downstream of the evidence, not the product.

Pure function. Inputs are the structured results of the other modules.
Spec: sre-f5-rule-learner-and-verdict.md + sre-f0-methodology-guards.md.
"""
from __future__ import annotations

import os
from typing import Any, Mapping

VERDICT_VERSION = "cse_verdict_v1"
MIN_DECISIONS = int(os.getenv("CSE_VERDICT_MIN_DECISIONS", "30"))
EXHAUSTIVE_COVERAGE = float(os.getenv("CSE_EXHAUSTIVE_COVERAGE", "0.70"))
SELECTIVE_COVERAGE = float(os.getenv("CSE_SELECTIVE_COVERAGE", "0.30"))
DEAD_HOURS_24_7 = int(os.getenv("CSE_DEAD_HOURS_24_7", "3"))
DEAD_HOURS_HUMAN = int(os.getenv("CSE_DEAD_HOURS_HUMAN", "6"))


def _edge_dim(edge: Mapping[str, Any]) -> str:
    if not edge or edge.get("supported") is None:
        return "underpowered"
    if edge.get("supported"):
        return "supported"
    return "contradicted" if (edge.get("edge") or 0) <= 0 else "weak"


def _rule_dim(rule: Mapping[str, Any], *, underpowered: bool) -> str:
    if underpowered:
        return "underpowered"
    best = (rule or {}).get("best")
    if best and best.get("determinism", 0) >= 0.8 and best.get("oos_confirmed"):
        return "high"
    if best and best.get("determinism", 0) >= 0.5:
        return "medium"
    return "low"  # rule_found-but-weak or no separating predicate


def _automation_dim(automation: Mapping[str, Any], rule: Mapping[str, Any], *, underpowered: bool) -> str:
    if underpowered:
        return "underpowered"
    cov = automation.get("coverage")
    dead = automation.get("dead_hours")
    best = (rule or {}).get("best") or {}
    sharp = best.get("determinism", 0) >= 0.8
    votes = 0
    if cov is not None and cov >= EXHAUSTIVE_COVERAGE:
        votes += 1
    if dead is not None and dead <= DEAD_HOURS_24_7:
        votes += 1
    if sharp:
        votes += 1
    return "high" if votes >= 2 else ("medium" if votes == 1 else "low")


def _human_dim(automation: Mapping[str, Any], rule_dim: str, *, underpowered: bool) -> str:
    if underpowered:
        return "underpowered"
    cov = automation.get("coverage")
    dead = automation.get("dead_hours")
    votes = 0
    if cov is not None and cov <= SELECTIVE_COVERAGE:
        votes += 1
    if dead is not None and dead >= DEAD_HOURS_HUMAN:
        votes += 1
    if rule_dim == "low":
        votes += 1
    return "high" if votes >= 2 else ("medium" if votes == 1 else "low")


def synthesize_verdict(*, edge: Mapping[str, Any], rule: Mapping[str, Any],
                       automation: Mapping[str, Any], n_decisions: int,
                       edge_matched: Mapping[str, Any] | None = None,
                       matching_balance: Mapping[str, Any] | None = None,
                       confound_flag: bool = False,
                       calibration: Mapping[str, Any] | None = None,
                       promotion: Mapping[str, Any] | None = None,
                       placebo: Mapping[str, Any] | None = None) -> dict:
    underpowered = n_decisions < MIN_DECISIONS
    cal_supported = bool(calibration and calibration.get("supported"))
    # A finding is promotable to a deployable thesis only if the formal gate passes
    # (n + effect + FDR q + OOS). promotion=None means "not evaluated" → don't block.
    promotable = (promotion is None) or bool(promotion.get("promotable"))
    dims = {
        "selection_edge": _edge_dim(edge),
        "rule_extractability": _rule_dim(rule, underpowered=underpowered),
        "automation_evidence": _automation_dim(automation, rule, underpowered=underpowered),
    }
    if confound_flag and dims["rule_extractability"] != "high":
        dims["selection_edge"] = "confounded"
    dims["calibration_edge"] = (
        "supported" if cal_supported
        else ("contradicted" if (calibration and calibration.get("supported") is False) else "underpowered"))
    dims["placebo_control"] = (
        "passed" if (placebo and placebo.get("passed")) else
        ("failed" if (placebo and placebo.get("passed") is False) else "underpowered"))
    dims["human_discretion_evidence"] = _human_dim(automation, dims["rule_extractability"], underpowered=underpowered)

    # G8 — classification + route (observed-vs-unobserved aware)
    edge_dim, rule_ex, auto, human = (dims["selection_edge"], dims["rule_extractability"],
                                      dims["automation_evidence"], dims["human_discretion_evidence"])
    if edge_dim == "confounded" or underpowered or edge_dim == "underpowered":
        # A supported calibration edge can RESCUE a confounded/edge-underpowered
        # counterfactual (she beats the price she pays — real PnL-relevant skill) —
        # but it must NOT bypass GLOBAL underpower (< MIN_DECISIONS): too few
        # decisions overall to route on anything. Forward paper rather than dismiss.
        if cal_supported and not underpowered:
            classification, route, note = ("mixed", "copy_mirror",
                "Counterfactual edge confounded/underpowered, but she beats the entry price she pays "
                "(calibration edge) — a real PnL-relevant selection edge; forward-paper to separate it from survivorship.")
        elif underpowered:
            classification, route, note = ("undetermined", "collect_more",
                "Insufficient decisions (< minimum) to route on any signal, including calibration.")
        elif edge_dim == "confounded":
            classification, route, note = ("undetermined", "collect_more",
                "Raw selection edge is not defensible after matched-control balance; treat as confounded.")
        else:
            classification, route, note = ("undetermined", "collect_more",
                "Insufficient power: collect more decisions / forward observation before routing.")
    elif rule_ex == "high" and auto in ("high", "medium") and promotable:
        classification, route, note = ("algorithmic_self_contained", "rebuild_rule",
            "Evidence supports a stable, OOS-confirmed selection rule on self-contained features.")
    elif rule_ex == "high" and auto in ("high", "medium") and not promotable:
        classification, route, note = ("undetermined", "collect_more",
            "Extractable rule but it fails the promotion gate (%s) — collect more before deploying."
            % ", ".join((promotion or {}).get("blockers", []) or ["unspecified"]))
    elif edge_dim == "supported" and rule_ex == "low":
        classification, route, note = ("algorithmic_unobserved_signal", "flag_human_ai",
            "A real selection edge exists but no observed-feature rule explains it — likely a private/external signal we cannot see; NOT necessarily human.")
    elif human == "high" and rule_ex == "low" and edge_dim in ("weak", "contradicted", "underpowered"):
        classification, route, note = ("human_or_noise", "copy_mirror",
            "Selective, no separating predicate, no clear within-class edge — evidence leans human-directed selection; copy live and let forward paper separate skill from survivorship.")
    elif auto == "high" and rule_ex in ("low", "medium"):
        classification, route, note = ("mixed", "copy_mirror",
            "Automation evidence high but selection rule not cleanly extractable — likely human selection + automated execution; route on selection.")
    else:
        classification, route, note = ("undetermined", "collect_more",
            "Evidence mixed/insufficient to route confidently.")

    # G9 — language guard: confidence capped, counter-evidence always reported
    confidence = min(0.8, 0.4 + 0.1 * sum(1 for d in dims.values() if d in ("high", "supported")))
    return {
        "verdict_version": VERDICT_VERSION,
        "dimensions": dims,
        "classification": classification,
        "route": route,
        "confidence": round(confidence, 2),
        "qualitative_note": note,
        "honest_caveat": "Data cannot prove intent; a bot can mimic human irregularity and a human can run scripts. Forward paper is the arbiter.",
        "learned_rule": (rule or {}).get("best"),
        "n_decisions": n_decisions,
        "selection_edge_matched": edge_matched,
        "matching_balance": matching_balance,
        "confound_flag": confound_flag,
        "calibration": calibration,
        "promotion": promotion,
        "placebo": placebo,
    }
