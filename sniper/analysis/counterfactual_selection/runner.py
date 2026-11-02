"""CSE runner — chains the engine end-to-end on one wallet.

activity_rows (+ a MarketContext) → engagements (F1) → decision matrix (F3/F4) →
detection (F5-F10) → rule learner (F11) → verdict (F12). FDR-corrects the
discriminator p-values across all (feature × group). Pure given the context.

Spec: docs/superpowers/specs/2026-05-30-selection-reconstruction-engine-design.md
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from sniper.analysis.counterfactual_selection.entity_layers import (
    build_engagements, selection_cadence, selection_schedule, execution_intensity,
)
from sniper.analysis.counterfactual_selection.pit_market_context import MarketContext
from sniper.analysis.counterfactual_selection.matrix import (
    build_decision_matrix, nearest_neighbor_match, MATCH_FEATURES,
)
from sniper.analysis.counterfactual_selection.detection import (
    within_group_discriminator, selection_edge, coverage, calibration_edge, calibration_by_band,
    stratified_edge_point,
)
from sniper.analysis.counterfactual_selection.rule_learner import learn_selection_rules
from sniper.analysis.counterfactual_selection.verdict import synthesize_verdict
from sniper.analysis.counterfactual_selection.guards import (
    benjamini_hochberg, candidate_universe_quality, leakage_violations, matching_balance,
    placebo_pass, promotion_decision,
)
import random as _random


def _placebo_edges(rows, *, k: int = 200, seed: int = 11) -> list[float]:
    """Negative control: shuffle picked labels WITHIN each group and recompute the
    SAME stratified edge statistic as selection_edge (apples-to-apples under group
    imbalance). The real edge must beat this placebo distribution. Does not call
    selection_edge (stays cheap + test-mock-friendly)."""
    decided = [dict(r) for r in rows if r.get("resolved") in (0, 1)]
    if len(decided) < 10:
        return []
    by_group: dict[str, list] = {}
    for r in decided:
        by_group.setdefault(str(r.get("group_id")), []).append(r)
    rng = _random.Random(seed)
    out: list[float] = []
    for _ in range(k):
        for grp in by_group.values():
            labels = [int(r.get("picked", 0)) for r in grp]
            rng.shuffle(labels)
            for r, lab in zip(grp, labels):
                r["picked"] = lab
        stat = stratified_edge_point(decided)
        if stat is not None:
            out.append(stat)
    return out


def _balance(matched, features=MATCH_FEATURES) -> dict:
    """SMD per covariate between matched picks and matched controls."""
    picks = [r for r in matched if int(r.get("picked", 0)) == 1]
    ctrls = [r for r in matched if int(r.get("picked", 0)) == 0]
    def col(rows, f):
        return [float((r.get("features") or {}).get(f)) for r in rows
                if (r.get("features") or {}).get(f) is not None]
    pf = {f: col(picks, f) for f in features}
    cf = {f: col(ctrls, f) for f in features}
    return matching_balance(pf, cf)

# Numeric, leakage-clean candidate features the discriminator + learner may use.
_RULE_FEATURES = ("price", "market_age", "time_to_resolution", "favorite_rank")


def _attach_position_fields(engagements, closed_positions):
    """Enrich engagements with her exact side (outcome_index) + event_family
    (eventSlug) from the positions snapshot, joined on the held token."""
    by_asset = {str(p.get("asset")): p for p in (closed_positions or [])}
    for e in engagements:
        p = by_asset.get(str(e.get("asset")))
        if not p:
            continue
        if p.get("outcomeIndex") is not None:
            try:
                e["outcome_index"] = int(p["outcomeIndex"])
            except (TypeError, ValueError):
                pass
        ev = str(p.get("eventSlug") or "").strip()
        if ev:
            e["event_family"] = ev
    return engagements


def run_counterfactual_selection(
    activity_rows: Sequence[Mapping[str, Any]],
    context: MarketContext,
    *,
    n_perm: int = 500,
    real_scope_axes: Sequence[str] = ("category", "market_type"),
    closed_positions: Sequence[Mapping[str, Any]] | None = None,
) -> dict:
    engagements = build_engagements(activity_rows)
    if closed_positions:
        engagements = _attach_position_fields(engagements, closed_positions)
    rows = build_decision_matrix(engagements, context, real_scope_axes=real_scope_axes)

    # F1 decision-grain signals (selection vs execution)
    signals = {
        "selection_cadence": selection_cadence(engagements),
        "selection_schedule": selection_schedule(engagements),
        "execution_intensity": execution_intensity(engagements),
    }

    # candidate-universe quality (G1)
    n_picked = sum(1 for r in rows if r["picked"] == 1)
    n_unsel = sum(1 for r in rows if r["picked"] == 0)
    price_share = (sum(1 for r in rows if (r["features"] or {}).get("price") is not None) / len(rows)) if rows else 0.0
    res_share = (sum(1 for r in rows if r.get("resolved") in (0, 1)) / len(rows)) if rows else 0.0
    quality = candidate_universe_quality(
        n_picked=n_picked, n_unselected=n_unsel, point_in_time_verified=True,
        price_available_share=price_share, resolution_available_share=res_share,
    )

    # T1 within-group discriminators across features, FDR-corrected jointly
    clean_features = [f for f in _RULE_FEATURES if f not in leakage_violations(_RULE_FEATURES)]
    disc: list[dict] = []
    for f in clean_features:
        disc.extend(within_group_discriminator(rows, f, n_perm=n_perm))

    # T2 edge — raw (within-scope) AND C7 matched (covariate-balanced controls).
    # The matched edge is the defensible one: it cures confounds like a market_age
    # imbalance between picks and the first-N candidate slice.
    # Raw within-scope edge is the verdict's headline signal. The C7 matched edge
    # is a DEFENSIBILITY DIAGNOSTIC, never a primary signal — matching on a small
    # candidate set can manufacture a spurious edge, so it may only DOWNGRADE
    # (confound_flag), never upgrade, the read.
    edge = edge_raw = selection_edge(rows, n_perm=n_perm)
    matched = nearest_neighbor_match(rows)
    edge_matched = selection_edge(matched, n_perm=n_perm)
    # #2 calibration edge (vs the PRICE she paid) — a verdict-MOVING test, so it
    # MUST be in the same FDR family as the discriminators + edges.
    calibration = calibration_edge(rows)
    calibration_bands = calibration_by_band(rows)
    fdr_family = [d for d in disc if d.get("p_value") is not None]
    fdr_family.extend(e for e in (edge_raw, edge_matched, calibration) if e.get("p_value") is not None)
    if fdr_family:
        qs = benjamini_hochberg([d["p_value"] for d in fdr_family])
        for item, q in zip(fdr_family, qs):
            item["q_value"] = q
        for d in disc:
            if d.get("q_value") is not None:
                d["significant"] = d["q_value"] <= 0.05
        for e in (edge_raw, edge_matched):
            if e.get("q_value") is not None:
                e["supported"] = bool((e.get("edge") or 0) > 0 and e["q_value"] <= 0.05)
        if calibration.get("q_value") is not None:
            calibration["supported"] = bool(
                (calibration.get("edge") or 0) > 0 and (calibration.get("z") or 0) > 0
                and calibration["q_value"] <= 0.05)
    balance = _balance(matched)
    worst = balance.get("worst_abs_smd")
    balanced = worst is not None and worst <= 0.25
    # The raw edge looked real, but matching cannot confirm it. If a high-OOS
    # rule exists, verdict can still route on the rule; otherwise this downgrades.
    confound_flag = bool(
        edge_raw.get("supported") and not (balanced and edge_matched.get("supported") is True)
    )

    # negative control (placebo): the real stratified edge must beat the
    # within-group label-shuffled placebo distribution (same statistic + null).
    placebo = placebo_pass(float(edge_raw.get("edge") or 0.0), _placebo_edges(rows))

    # F11 rule
    rule = learn_selection_rules(rows, clean_features)

    # automation inputs: coverage of the learned rule + schedule dead-hours + sharpness
    cov = None
    best = rule.get("best")
    if best:
        def _pred(feats, _b=best):
            v = (feats or {}).get(_b["feature"])
            if v is None:
                return False
            return float(v) <= _b["threshold"] if _b["op"] == "<=" else float(v) >= _b["threshold"]
        cov = coverage(rows, _pred).get("coverage")
    automation = {
        "coverage": cov,
        "dead_hours": signals["selection_schedule"].get("dead_hours"),
        "hour_coverage_fraction": signals["selection_schedule"].get("hour_coverage_fraction"),
    }

    # formal discovery→thesis gate (was dead code): a finding is promotable only
    # with enough decisions, effect size, FDR q, an OOS-confirmed rule, AND the
    # placebo negative control must pass (#3 — placebo is now a hard blocker).
    promotion_ok, promotion_reasons = promotion_decision(
        n=n_picked,
        effect_size=float(edge_raw.get("edge") or 0.0),
        q_value=float(edge_raw.get("q_value") if edge_raw.get("q_value") is not None else 1.0),
        oos_confirmed=bool(best and best.get("oos_confirmed")),
    )
    if placebo.get("passed") is False:
        promotion_ok = False
        promotion_reasons = list(promotion_reasons) + ["placebo_not_passed"]
    promotion = {"promotable": promotion_ok, "blockers": promotion_reasons,
                 "placebo_passed": placebo.get("passed")}

    verdict = synthesize_verdict(
        edge=edge,
        rule=rule,
        automation=automation,
        n_decisions=n_picked,
        edge_matched=edge_matched,
        matching_balance=balance,
        confound_flag=confound_flag,
        calibration=calibration,
        promotion=promotion,
        placebo=placebo,
    )

    return {
        "n_engagements": len(engagements),
        "decision_signals": signals,
        "candidate_quality": quality,
        "discriminators": disc,
        "selection_edge": edge,
        "selection_edge_raw": edge_raw,
        "selection_edge_matched": edge_matched,
        "matching_balance": balance,
        "confound_flag": confound_flag,
        "calibration": calibration,
        "calibration_bands": calibration_bands,
        "placebo": placebo,
        "promotion": promotion,
        "rule": rule,
        "automation": automation,
        "verdict": verdict,
    }
