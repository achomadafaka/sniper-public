"""thesis_profile_assembler — ranks clusters into a wallet thesis profile.

Phase 1 stops at clusters. Per-cluster `method_detector_runner` findings and the
`thesis_validation_spine` are Phase 2/3 — the `theses` field is intentionally left for
them, so this output is an UNVALIDATED thesis profile, not a validated edge claim."""
from __future__ import annotations

import hashlib

from sniper.analysis.winning_wallet_thesis.capital_risk import build_capital_risk_profile
from sniper.analysis.winning_wallet_thesis.methods.runner import run_detectors
from sniper.analysis.winning_wallet_thesis.validation.spine import build_validation_plans
from sniper.analysis.winning_wallet_thesis.types import (
    EdgeCluster, PositionBook, ShuffleBaseline, ThesisProfile,
)


def _run_id(wallet: str, clusters: list[EdgeCluster]) -> str:
    fp = wallet + "|" + ";".join(sorted(f"{c.cluster_id}:{c.pnl}" for c in clusters))
    return hashlib.sha256(fp.encode()).hexdigest()[:16]


def assemble_profile(wallet: str, book: PositionBook,
                     slice_tables: dict, baseline: ShuffleBaseline | None,
                     clusters: list[EdgeCluster], *, reward_share: float = 0.0,
                     reward_non_usdc_flagged: bool = False,
                     fill_identity=None) -> ThesisProfile:
    edge = sorted([c for c in clusters if not c.is_loss_center],
                  key=lambda c: c.pnl, reverse=True)
    loss = sorted([c for c in clusters if c.is_loss_center], key=lambda c: c.pnl)
    top = edge[0].label if edge else "none"
    worst = loss[0].label if loss else "none"
    headline = (f"UNVALIDATED profile: {len(edge)} edge cluster(s); top={top}; "
                f"loss-center={worst}")
    gaps: list[str] = []
    if reward_share >= 25:
        gaps.append("reward_share high — verify prediction vs liquidity-farming (#12)")
    if reward_non_usdc_flagged:
        gaps.append("reward rows denominated in a non-USDC token — excluded from reward_share (#12)")
    if baseline and baseline.obs_over_expected < 2.0:
        gaps.append("weak shuffle baseline: her winning groups are only modestly above what random "
                    "reshuffling of her own results produces, so no single pattern stands out as the "
                    "driver from her history alone. The realized profit is a fact; what is unproven "
                    "from this data is a specific repeatable rule — confirm one with paper validation")
    method_findings = run_detectors(book, clusters, fill_identity=fill_identity)
    validation_plans = build_validation_plans(wallet, book, clusters, method_findings)
    capital_risk = build_capital_risk_profile(wallet, book, clusters)
    gaps.extend(capital_risk.warnings)
    return ThesisProfile(
        wallet=wallet, run_id=_run_id(wallet, clusters), headline=headline,
        edge_clusters=edge, loss_centers=loss, reward_share_pct=round(reward_share, 1),
        shuffle_baseline=baseline, data_gaps=gaps, method_findings=method_findings,
        validation_plans=validation_plans, capital_risk=capital_risk)
