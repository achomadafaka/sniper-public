"""§7.7 grain key + §7.8 mechanism evidence rules + §8 honesty gates.

This module composes positions + episodes into per-grain aggregations
and computes the mechanism evidence + promotion eligibility decisions.

Doctrine constraints honored:
- Grain key is the 7-dimension tuple per §7.7 — every dimension mandatory.
- Category-only `data_unavailable` → grain_quality = `interpretable_evidence_only`
  (codex split between evidence eligibility and promotion eligibility).
- Any other broad-bucket leakage → `not_interpretable_at_this_grain` (no
  evidence, no promotion).
- `mechanism_evidence` is per-grain — no wallet-level mechanism.
- `candidate_thesis_evidence` non-null ONLY when all 7 gates pass.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

GrainQuality = Literal[
    "interpretable",
    "interpretable_evidence_only",
    "diagnostic_only",
    "not_interpretable_at_this_grain",
    "underpowered",
]
EvidenceStrength = Literal[
    "metric_supported",  # codex-renamed from "supported"
    "consistent",
    "inconclusive",
    "refuted",
    "n/a",
]
Mechanism = Literal[
    "prediction_driven",
    "trading_driven",
    "risk_management_driven",
    "market_selection_driven",
    "mixed",
    "underpowered",
]


# Values that count as broad-bucket leakage on non-category dimensions
# (§7.7 hard rule + §8 gate 7).
LEAKY_BROAD_VALUES = frozenset({"unknown", "mixed", "OTHER", "data_unavailable"})

# §8 gate thresholds (from spec §7.8 + §8)
MIN_EPISODES_PER_GRAIN = 30
MIN_ATTRIBUTION_COVERAGE = 0.7
MAX_PNL_TOP5_SHARE = 0.7

# §7.8 mechanism metric thresholds
THRESHOLD_OUTCOME_WIN_RATE = 0.55
THRESHOLD_TRADE_WIN_RATE = 0.55
THRESHOLD_PROFITABLE_EXIT_LATER_LOST_SHARE = 0.30  # for trading_driven
THRESHOLD_LOSS_CUT_CORRECTNESS = 0.55
THRESHOLD_SOLD_WINNER_AT_LOSS_MAX = 0.30


# ---------------------------------------------------------------------------
# Grain key
# ---------------------------------------------------------------------------


GrainKeyDimensions = (
    "category_source",
    "category",
    "market_type",
    "outcome_role",
    "entry_price_bucket",
    "exit_mode",
    "position_pattern",
)


def build_grain_key(
    *,
    category_source: str,
    category: str,
    market_type: str,
    outcome_role: str,
    entry_price_bucket: str,
    exit_mode: str,
    position_pattern: str,
) -> dict[str, str]:
    """Mandatory 7-dimension grain key per §7.7.

    Every dimension required. Callers must substitute `unknown` / `mixed` /
    `data_unavailable` sentinels when the underlying classification is
    unavailable — those values trigger gate failures downstream.
    """
    return {
        "category_source": category_source,
        "category": category,
        "market_type": market_type,
        "outcome_role": outcome_role,
        "entry_price_bucket": entry_price_bucket,
        "exit_mode": exit_mode,
        "position_pattern": position_pattern,
    }


def grain_id(grain_key: dict[str, str]) -> str:
    """Stable hash of the grain key — primary key for result_semantics."""
    canonical = json.dumps(grain_key, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def grain_key_json(grain_key: dict[str, str]) -> str:
    return json.dumps(grain_key, sort_keys=True)


# ---------------------------------------------------------------------------
# Grain aggregation
# ---------------------------------------------------------------------------


@dataclass
class GrainAggregate:
    """In-memory accumulator for one grain across positions + episodes."""

    grain_key: dict[str, str]
    grain_id: str = ""
    position_ids: list[str] = field(default_factory=list)
    n_episodes: int = 0
    episodes_by_class: dict[str, int] = field(default_factory=dict)
    pnl_per_position: list[float] = field(default_factory=list)
    total_size: float = 0.0
    n_attributable: int = 0  # episodes with non-`unknown`/`insufficient_data` class

    def add_position(self, *, position_id: str, position_pnl: float) -> None:
        self.position_ids.append(position_id)
        self.pnl_per_position.append(position_pnl)

    def add_episode(self, *, klass: str, size: float) -> None:
        self.n_episodes += 1
        self.episodes_by_class[klass] = self.episodes_by_class.get(klass, 0) + 1
        self.total_size += max(0.0, size)
        if klass not in ("unknown", "insufficient_data"):
            self.n_attributable += 1

    # --- derived metrics ---

    @property
    def n_positions(self) -> int:
        return len(self.position_ids)

    @property
    def pnl_total(self) -> float:
        return sum(self.pnl_per_position)

    @property
    def pnl_concentration_top5_share(self) -> float | None:
        if not self.pnl_per_position:
            return None
        total_abs = sum(abs(p) for p in self.pnl_per_position)
        if total_abs <= 0:
            return None
        top5 = sorted((abs(p) for p in self.pnl_per_position), reverse=True)[:5]
        return sum(top5) / total_abs

    @property
    def attribution_coverage(self) -> float:
        if self.n_episodes == 0:
            return 0.0
        return self.n_attributable / self.n_episodes

    def metric(self, *names: str) -> int:
        return sum(self.episodes_by_class.get(n, 0) for n in names)

    @property
    def outcome_win_rate_held(self) -> float | None:
        held_won = self.metric("held_won")
        held_lost = self.metric("held_lost")
        denom = held_won + held_lost
        return None if denom == 0 else held_won / denom

    @property
    def trade_win_rate_preres(self) -> float | None:
        profitable = self.metric("profitable_exit_later_won", "profitable_exit_later_lost")
        loss_exits = self.metric("loss_exit_later_won", "loss_exit_later_lost")
        denom = profitable + loss_exits
        return None if denom == 0 else profitable / denom

    @property
    def sold_winners_early_share(self) -> float | None:
        won = self.metric("profitable_exit_later_won")
        lost = self.metric("profitable_exit_later_lost")
        denom = won + lost
        return None if denom == 0 else won / denom

    @property
    def sold_winner_at_loss_share(self) -> float | None:
        bad = self.metric("loss_exit_later_won")
        good = self.metric("loss_exit_later_lost")
        denom = bad + good
        return None if denom == 0 else bad / denom

    @property
    def loss_cut_correctness_rate(self) -> float | None:
        good = self.metric("loss_exit_later_lost")
        bad = self.metric("loss_exit_later_won")
        denom = good + bad
        return None if denom == 0 else good / denom


# ---------------------------------------------------------------------------
# §8 honesty gates
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    quality: GrainQuality
    reasons: list[str]  # which gates failed (empty when interpretable)


def evaluate_honesty_gates(g: GrainAggregate) -> GateResult:
    """Evaluate the 7 gates from §8.

    Returns (grain_quality, list_of_failed_gates). Gate 3 (category) has
    the codex special case: category-only failure (with all other gates
    passing) → interpretable_evidence_only.
    """
    failed: list[str] = []
    # Gate 1: sample size
    if g.n_episodes < MIN_EPISODES_PER_GRAIN:
        failed.append(f"gate1_sample_size:n_episodes={g.n_episodes}<{MIN_EPISODES_PER_GRAIN}")
    # Gate 2: attribution coverage
    if g.attribution_coverage < MIN_ATTRIBUTION_COVERAGE:
        failed.append(
            f"gate2_attribution_coverage:{g.attribution_coverage:.3f}<{MIN_ATTRIBUTION_COVERAGE}"
        )
    # Gate 3: category homogeneity — within a grain, category is the
    # grain_key dimension, so all positions share it by construction.
    # The substantive check: is category a real string or `data_unavailable`?
    category_unavailable = g.grain_key["category"] == "data_unavailable"
    # Gates 4 & 5: market_type / outcome_role homogeneity — also true by
    # construction (grain_key dimensions). Substantive check: leaky values.
    # Gate 6: PnL concentration
    top5 = g.pnl_concentration_top5_share
    if top5 is not None and top5 > MAX_PNL_TOP5_SHARE:
        failed.append(f"gate6_pnl_concentration:top5_share={top5:.3f}>{MAX_PNL_TOP5_SHARE}")
    # Gate 7: no broad-bucket leakage on non-category dimensions
    non_category_leaks: list[str] = []
    for dim in GrainKeyDimensions:
        if dim == "category" or dim == "category_source":
            continue
        if g.grain_key[dim] in LEAKY_BROAD_VALUES:
            non_category_leaks.append(f"{dim}={g.grain_key[dim]}")
    if non_category_leaks:
        failed.append(
            f"gate7_broad_bucket_leakage:{','.join(non_category_leaks)}"
        )

    # Mapping per §8.2:
    # - sample-size or attribution failure → underpowered
    # - non-category broad-bucket leakage → not_interpretable_at_this_grain
    # - concentration failure → diagnostic_only
    # - only category unavailable → interpretable_evidence_only
    # - all pass → interpretable
    sample_failed = any(f.startswith("gate1_") or f.startswith("gate2_") for f in failed)
    leakage_failed = any(f.startswith("gate7_") for f in failed)
    concentration_failed = any(f.startswith("gate6_") for f in failed)

    if sample_failed:
        return GateResult(quality="underpowered", reasons=failed)
    if leakage_failed:
        return GateResult(quality="not_interpretable_at_this_grain", reasons=failed)
    if concentration_failed:
        return GateResult(quality="diagnostic_only", reasons=failed)
    if category_unavailable:
        return GateResult(quality="interpretable_evidence_only", reasons=failed)
    return GateResult(quality="interpretable", reasons=failed)


# ---------------------------------------------------------------------------
# §7.8 mechanism evidence rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MechanismEvidenceRow:
    grain_id: str
    mechanism: Mechanism
    evidence_strength: EvidenceStrength
    supporting_metrics: dict[str, Any]
    sample_size: int
    caveats: list[str]
    missing_data: list[str]
    candidate_thesis_evidence: str | None
    not_promoted_reason: str | None


def _eval_prediction_driven(g: GrainAggregate, quality: GrainQuality) -> MechanismEvidenceRow:
    """prediction_driven: held to resolution AND outcome_win_rate_held > threshold"""
    caveats: list[str] = []
    missing: list[str] = []
    metrics: dict[str, Any] = {
        "outcome_win_rate_held": g.outcome_win_rate_held,
        "pnl_total": g.pnl_total,
        "n_held_episodes": g.metric("held_won", "held_lost"),
    }
    held_n = g.metric("held_won", "held_lost")
    quality_ok = quality in ("interpretable", "interpretable_evidence_only")
    if not quality_ok:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="prediction_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=held_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"grain_quality={quality}",
        )
    if held_n < MIN_EPISODES_PER_GRAIN:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="prediction_driven",
            evidence_strength="inconclusive",
            supporting_metrics=metrics, sample_size=held_n,
            caveats=[f"n_held_episodes={held_n}<{MIN_EPISODES_PER_GRAIN}"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason="insufficient_held_episodes",
        )
    if g.grain_key["exit_mode"] != "held":
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="prediction_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=held_n,
            caveats=["prediction_driven only applies to exit_mode=held grains"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"exit_mode={g.grain_key['exit_mode']}",
        )
    wr = g.outcome_win_rate_held or 0.0
    if wr > THRESHOLD_OUTCOME_WIN_RATE and g.pnl_total > 0:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="prediction_driven",
            evidence_strength="metric_supported",
            supporting_metrics=metrics, sample_size=held_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=_maybe_thesis(
                quality,
                f"In this grain, held-to-resolution positions show "
                f"outcome_win_rate={wr:.2%} (n={held_n}) and net pnl={g.pnl_total:.0f} — "
                f"prediction-driven evidence."
            ),
            not_promoted_reason=_not_promoted_reason(quality),
        )
    return MechanismEvidenceRow(
        grain_id=g.grain_id, mechanism="prediction_driven",
        evidence_strength="refuted" if wr <= THRESHOLD_OUTCOME_WIN_RATE else "inconclusive",
        supporting_metrics=metrics, sample_size=held_n,
        caveats=caveats, missing_data=missing,
        candidate_thesis_evidence=None,
        not_promoted_reason="metric_thresholds_not_met",
    )


def _eval_trading_driven(g: GrainAggregate, quality: GrainQuality) -> MechanismEvidenceRow:
    """trading_driven: pre-resolution exit + trade WR > threshold + share of
    profitable-exit-later-LOST > 0.30 (proves trade skill independent of prediction)"""
    caveats: list[str] = []
    missing: list[str] = []
    preres_n = (
        g.metric("profitable_exit_later_won", "profitable_exit_later_lost")
        + g.metric("loss_exit_later_won", "loss_exit_later_lost")
    )
    metrics: dict[str, Any] = {
        "trade_win_rate_preres": g.trade_win_rate_preres,
        "profitable_exit_later_lost_share": (
            g.metric("profitable_exit_later_lost") / g.metric(
                "profitable_exit_later_won", "profitable_exit_later_lost"
            )
            if g.metric("profitable_exit_later_won", "profitable_exit_later_lost") > 0
            else None
        ),
        "n_preres_episodes": preres_n,
    }
    quality_ok = quality in ("interpretable", "interpretable_evidence_only")
    if not quality_ok:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="trading_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=preres_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"grain_quality={quality}",
        )
    if preres_n < MIN_EPISODES_PER_GRAIN:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="trading_driven",
            evidence_strength="inconclusive",
            supporting_metrics=metrics, sample_size=preres_n,
            caveats=[f"n_preres_episodes={preres_n}<{MIN_EPISODES_PER_GRAIN}"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason="insufficient_preres_episodes",
        )
    if g.grain_key["exit_mode"] != "pre_resolution":
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="trading_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=preres_n,
            caveats=["trading_driven only applies to exit_mode=pre_resolution grains"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"exit_mode={g.grain_key['exit_mode']}",
        )
    twr = g.trade_win_rate_preres or 0.0
    pell_share = metrics["profitable_exit_later_lost_share"] or 0.0
    if twr > THRESHOLD_TRADE_WIN_RATE and pell_share > THRESHOLD_PROFITABLE_EXIT_LATER_LOST_SHARE:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="trading_driven",
            evidence_strength="metric_supported",
            supporting_metrics=metrics, sample_size=preres_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=_maybe_thesis(
                quality,
                f"In this grain, pre-resolution trades win {twr:.2%} of the time "
                f"and {pell_share:.2%} of profitable exits resolved AGAINST the "
                f"wallet — evidence of trading skill independent of prediction."
            ),
            not_promoted_reason=_not_promoted_reason(quality),
        )
    return MechanismEvidenceRow(
        grain_id=g.grain_id, mechanism="trading_driven",
        evidence_strength="inconclusive",
        supporting_metrics=metrics, sample_size=preres_n,
        caveats=caveats, missing_data=missing,
        candidate_thesis_evidence=None,
        not_promoted_reason="metric_thresholds_not_met",
    )


def _eval_risk_management_driven(g: GrainAggregate, quality: GrainQuality) -> MechanismEvidenceRow:
    """risk_management_driven: pre-resolution loss exits resolved AGAINST the
    wallet > 0.55 (cut a loser that would have stayed a loser)."""
    caveats: list[str] = []
    missing: list[str] = []
    loss_exit_n = g.metric("loss_exit_later_won", "loss_exit_later_lost")
    metrics: dict[str, Any] = {
        "loss_cut_correctness_rate": g.loss_cut_correctness_rate,
        "sold_winner_at_loss_share": g.sold_winner_at_loss_share,
        "n_loss_exits": loss_exit_n,
    }
    quality_ok = quality in ("interpretable", "interpretable_evidence_only")
    if not quality_ok:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="risk_management_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=loss_exit_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"grain_quality={quality}",
        )
    if loss_exit_n < MIN_EPISODES_PER_GRAIN:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="risk_management_driven",
            evidence_strength="inconclusive",
            supporting_metrics=metrics, sample_size=loss_exit_n,
            caveats=[f"n_loss_exits={loss_exit_n}<{MIN_EPISODES_PER_GRAIN}"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason="insufficient_loss_exit_episodes",
        )
    if g.grain_key["exit_mode"] != "pre_resolution":
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="risk_management_driven",
            evidence_strength="n/a",
            supporting_metrics=metrics, sample_size=loss_exit_n,
            caveats=["risk_management_driven only applies to exit_mode=pre_resolution grains"],
            missing_data=missing,
            candidate_thesis_evidence=None,
            not_promoted_reason=f"exit_mode={g.grain_key['exit_mode']}",
        )
    lcc = g.loss_cut_correctness_rate or 0.0
    swal = g.sold_winner_at_loss_share or 1.0
    if lcc > THRESHOLD_LOSS_CUT_CORRECTNESS and swal < THRESHOLD_SOLD_WINNER_AT_LOSS_MAX:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="risk_management_driven",
            evidence_strength="metric_supported",
            supporting_metrics=metrics, sample_size=loss_exit_n,
            caveats=caveats, missing_data=missing,
            candidate_thesis_evidence=_maybe_thesis(
                quality,
                f"In this grain, {lcc:.2%} of loss exits avoided a position that "
                f"would have resolved against the wallet; bad-exit share "
                f"{swal:.2%} is below {THRESHOLD_SOLD_WINNER_AT_LOSS_MAX:.0%}. "
                f"Risk-management-driven evidence."
            ),
            not_promoted_reason=_not_promoted_reason(quality),
        )
    return MechanismEvidenceRow(
        grain_id=g.grain_id, mechanism="risk_management_driven",
        evidence_strength="inconclusive",
        supporting_metrics=metrics, sample_size=loss_exit_n,
        caveats=caveats, missing_data=missing,
        candidate_thesis_evidence=None,
        not_promoted_reason="metric_thresholds_not_met",
    )


def _eval_market_selection_driven(g: GrainAggregate, quality: GrainQuality) -> MechanismEvidenceRow:
    """DEFERRED in v0 — requires comparable-unselected-markets baseline
    which isn't computable from a single-wallet fixture."""
    return MechanismEvidenceRow(
        grain_id=g.grain_id,
        mechanism="market_selection_driven",
        evidence_strength="n/a",
        supporting_metrics={},
        sample_size=g.n_episodes,
        caveats=["selection_baseline_not_computable_offline"],
        missing_data=["comparable_unselected_markets_baseline"],
        candidate_thesis_evidence=None,
        not_promoted_reason="market_selection_driven_deferred",
    )


def _eval_underpowered(g: GrainAggregate, quality: GrainQuality) -> MechanismEvidenceRow:
    """Always emitted for completeness. Marked metric_supported when the
    grain fails sample-size or coverage gates."""
    if g.n_episodes < MIN_EPISODES_PER_GRAIN or g.attribution_coverage < MIN_ATTRIBUTION_COVERAGE:
        return MechanismEvidenceRow(
            grain_id=g.grain_id, mechanism="underpowered",
            evidence_strength="metric_supported",
            supporting_metrics={
                "n_episodes": g.n_episodes,
                "attribution_coverage": g.attribution_coverage,
            },
            sample_size=g.n_episodes,
            caveats=[],
            missing_data=[],
            candidate_thesis_evidence=None,
            not_promoted_reason="grain_is_underpowered",
        )
    return MechanismEvidenceRow(
        grain_id=g.grain_id, mechanism="underpowered",
        evidence_strength="refuted",
        supporting_metrics={
            "n_episodes": g.n_episodes,
            "attribution_coverage": g.attribution_coverage,
        },
        sample_size=g.n_episodes,
        caveats=[],
        missing_data=[],
        candidate_thesis_evidence=None,
        not_promoted_reason=None,
    )


def _eval_mixed(rows: list[MechanismEvidenceRow]) -> MechanismEvidenceRow:
    """`mixed` fires when 2+ other mechanisms reached metric_supported in the
    same grain — report 'do not promote'."""
    supported = [
        r for r in rows
        if r.evidence_strength == "metric_supported"
        and r.mechanism in {"prediction_driven", "trading_driven", "risk_management_driven"}
    ]
    grain_id = rows[0].grain_id if rows else ""
    if len(supported) >= 2:
        return MechanismEvidenceRow(
            grain_id=grain_id, mechanism="mixed",
            evidence_strength="metric_supported",
            supporting_metrics={
                "supported_mechanisms": [r.mechanism for r in supported],
            },
            sample_size=max((r.sample_size for r in supported), default=0),
            caveats=["multiple mechanisms supported in same grain — do not promote"],
            missing_data=[],
            candidate_thesis_evidence=None,
            not_promoted_reason="mixed_mechanisms",
        )
    return MechanismEvidenceRow(
        grain_id=grain_id, mechanism="mixed",
        evidence_strength="refuted",
        supporting_metrics={},
        sample_size=0,
        caveats=[],
        missing_data=[],
        candidate_thesis_evidence=None,
        not_promoted_reason=None,
    )


def _maybe_thesis(quality: GrainQuality, sentence: str) -> str | None:
    """Per §8.3: candidate_thesis_evidence is non-null ONLY when grain
    quality is `interpretable` (all 7 gates pass including real category)."""
    return sentence if quality == "interpretable" else None


def _not_promoted_reason(quality: GrainQuality) -> str | None:
    if quality == "interpretable":
        return None
    if quality == "interpretable_evidence_only":
        return "category_unavailable"
    return f"grain_quality={quality}"


def build_mechanism_evidence(
    *, g: GrainAggregate, quality: GrainQuality
) -> list[MechanismEvidenceRow]:
    """Emit one row per mechanism for this grain. Order is stable for
    deterministic SQLite content."""
    rows = [
        _eval_prediction_driven(g, quality),
        _eval_trading_driven(g, quality),
        _eval_risk_management_driven(g, quality),
        _eval_market_selection_driven(g, quality),
        _eval_underpowered(g, quality),
    ]
    rows.append(_eval_mixed(rows))
    return rows
