"""Pure-function mapping from ``ProtocolReport`` to operational action decision.

Spec: docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md § 4.2

This module has zero side effects. No DB reads, no env reads, no clock reads.
``MIN_CUSUM_N`` is the only constant the mapping reads from outside ``pr``;
changing it is a minor-version bump per spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from sniper.analysis.wallet_protocol.types import BranchReport, ProtocolReport

MIN_CUSUM_N: Final[int] = 30


@dataclass(frozen=True)
class Layer5Decision:
    """Output of the mapping function. Three locked fields per spec § 4.2."""

    action_decision: str
    reason_codes: list[str] = field(default_factory=list)
    confidence_band: str = "n/a"


# ----- Helpers (pure functions of pr) -----


def _dw_negative(pr: ProtocolReport) -> bool:
    return pr.dw_roi_point is not None and pr.dw_roi_point < 0


def _dw_vs_ew_diverges(pr: ProtocolReport) -> bool:
    """DISPLAY-ONLY descriptor; never authorizes (spec § 4.2).

    Returns True when both ROIs are present AND either:
    1. sign-flip (dw * ew < 0), OR
    2. abs(dw - ew) > 2 * max(abs(ew), 0.05).
    """
    if pr.dw_roi_point is None or pr.ew_roi_point is None:
        return False
    if pr.dw_roi_point * pr.ew_roi_point < 0:
        return True
    return abs(pr.dw_roi_point - pr.ew_roi_point) > 2 * max(abs(pr.ew_roi_point), 0.05)


def _active_branches(pr: ProtocolReport) -> list[BranchReport]:
    return [
        b
        for b in (pr.held_analyst_branch, pr.flipper_branch)
        if b is not None and b.is_active
    ]


def _h7_signaled(pr: ProtocolReport) -> bool:
    return any(
        b.stationarity is not None and b.stationarity.signaled
        for b in _active_branches(pr)
    )


def _independence_concentrated(pr: ProtocolReport) -> bool:
    return any(
        b.independence_check is not None and b.independence_check.flag == "concentrated"
        for b in _active_branches(pr)
    )


def _stationary_and_robust(pr: ProtocolReport) -> bool:
    """Per spec § 4.2: every active branch has signaled=False, n>=MIN_CUSUM_N,
    and primary hypothesis NOT flagged underpowered_reason='n_below_threshold'.

    Empty active-branches set returns False (universal-quantifier-over-empty
    guard required by spec).
    """
    branches = _active_branches(pr)
    if not branches:
        return False
    for b in branches:
        if b.stationarity is None or b.stationarity.signaled:
            return False
        if b.stationarity.pre_regime_n < MIN_CUSUM_N:
            return False
        primary_name = "FH1" if b.branch == "flipper" else "H1"
        primary = next((h for h in b.hypotheses if h.name == primary_name), None)
        if primary is None:
            return False
        if primary.underpowered_reason == "n_below_threshold":
            return False
    return True


def _powered_reason_codes(pr: ProtocolReport) -> list[str]:
    """One code per underpowered hypothesis, ``H<n>_underpowered:<reason>``."""
    out: list[str] = []
    for b in _active_branches(pr):
        for h in b.hypotheses:
            if h.verdict == "underpowered" and h.underpowered_reason:
                out.append(f"{h.name}_underpowered:{h.underpowered_reason}")
    return out


def _branch_conflict_codes(pr: ProtocolReport) -> list[str]:
    """Locked vocabulary; one code per active branch's primary hypothesis verdict.

    Used only by the ``operator_review`` arm. No other strings may be emitted.
    """
    out: list[str] = []
    for b in _active_branches(pr):
        primary_name = "FH1" if b.branch == "flipper" else "H1"
        primary = next((h for h in b.hypotheses if h.name == primary_name), None)
        if primary is None:
            continue
        prefix = "flipper_branch" if b.branch == "flipper" else "held_branch"
        if primary.verdict == "supported":
            out.append(f"{prefix}_supported")
        elif primary.verdict == "refuted":
            out.append(f"{prefix}_refuted")
        elif primary.verdict == "inconclusive":
            out.append(f"{prefix}_inconclusive")
        elif primary.verdict == "underpowered":
            out.append(f"{prefix}_underpowered")
    return out


# ----- Mapping function -----


def map_protocol_report_to_decision(pr: ProtocolReport) -> Layer5Decision:
    """Deterministic, total mapping per spec § 4.2. Pure function of ``pr``."""
    # Stage 1 hard rejects — gate_result dominates overall_verdict.
    if pr.gate_result == "reject_hedge_archetype":
        return Layer5Decision("anti_pattern", ["reject_hedge_archetype"], "n/a")
    if pr.gate_result == "reject_dormant":
        return Layer5Decision("exclude_for_copy", ["reject_dormant"], "n/a")
    if pr.gate_result == "reject_insufficient_sample":
        return Layer5Decision(
            "exclude_for_copy", ["reject_insufficient_sample"], "n/a"
        )

    flags = set(pr.data_quality_flags or [])

    # Caveat reason codes — display-only, always emitted when condition holds.
    caveats: list[str] = []
    if _dw_negative(pr):
        caveats.append("dw_negative")
    if _dw_vs_ew_diverges(pr):
        caveats.append("dw_vs_ew_divergent")
    if _h7_signaled(pr):
        caveats.append("H7_regime_change_signaled")
    if "F3_recent_regime" in flags:
        caveats.append("F3_recent_regime")
    if _independence_concentrated(pr):
        caveats.append("independence_concentrated")
        caveats.append("domain_specialization_caveat")

    # Hard anti-pattern combinations (cross-verdict).
    hard_ap: list[str] = []
    if "F1_sloppy_operator" in flags and "F2_concentration_top3" in flags:
        hard_ap.append("F1_F2_combined")
    if "F4_heavy_ctf" in flags and _dw_negative(pr):
        hard_ap.append("F4_with_dw_negative")

    if hard_ap and pr.overall_verdict != "edge_detected":
        return Layer5Decision(
            "anti_pattern",
            [pr.overall_verdict] + caveats + sorted(hard_ap),
            "low",
        )

    # Verdict-specific routing (no edge_detected).
    if pr.overall_verdict == "no_edge":
        return Layer5Decision("exclude_for_copy", ["no_edge"] + caveats, "low")

    if pr.overall_verdict == "underpowered":
        return Layer5Decision(
            "study",
            ["underpowered"] + _powered_reason_codes(pr) + caveats,
            "underpowered",
        )

    if pr.overall_verdict == "data_insufficient":
        return Layer5Decision(
            "monitor", ["data_insufficient"] + caveats, "underpowered"
        )

    if pr.overall_verdict == "operator_review":
        return Layer5Decision(
            "reverse_engineer_candidate",
            ["operator_review"] + _branch_conflict_codes(pr) + caveats,
            "medium",
        )

    # edge_detected — only path that can mint validated_copy_candidate.
    if pr.overall_verdict == "edge_detected":
        edge_codes = [
            "edge_detected",
            f"primary_edge_type:{pr.primary_edge_type or 'unspecified'}",
        ]
        downshift: list[str] = []
        if "F2_concentration_top3" in flags:
            downshift.append("F2_concentration_top3")
        if "F3_recent_regime" in flags:
            downshift.append("F3_recent_regime")
        if pr.trading_mode == "mixed_mode":
            downshift.append("mode_mixed")
        if _independence_concentrated(pr):
            downshift.append("independence_concentrated")

        if hard_ap:
            return Layer5Decision(
                "reverse_engineer_candidate",
                edge_codes + caveats + sorted(hard_ap),
                "medium",
            )
        if downshift:
            return Layer5Decision(
                "reverse_engineer_candidate",
                edge_codes + caveats + sorted(set(downshift)),
                "medium",
            )

        band = "high" if _stationary_and_robust(pr) else "medium"
        return Layer5Decision("validated_copy_candidate", edge_codes + caveats, band)

    raise NotImplementedError(pr.overall_verdict)


__all__ = [
    "MIN_CUSUM_N",
    "Layer5Decision",
    "map_protocol_report_to_decision",
]
