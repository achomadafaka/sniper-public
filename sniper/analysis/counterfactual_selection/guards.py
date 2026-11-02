"""CSE-F0 — methodology guards (mandatory, cross-cutting).

Pure functions that keep the engine from becoming a false-discovery machine:
- leakage / time-scope tagging (only pre/at-engagement features may inform a rule)
- Benjamini-Hochberg FDR + a promotion gate (min-sample + effect + FDR + OOS)
- candidate-universe quality + nearest-neighbor matching balance
- negative-control / placebo evaluation

Spec: docs/superpowers/specs/2026-05-30-sre-f0-methodology-guards.md
"""
from __future__ import annotations

import math
import os
from typing import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# G3 — leakage / time-scope
# --------------------------------------------------------------------------

# Known feature → time scope. Only PRE/AT may inform a selection rule.
FEATURE_TIME_SCOPE: dict[str, str] = {
    # pre/at-engagement (usable for rules)
    "entry_price": "at_engagement", "entry_vwap": "at_engagement",
    "latency_from_creation": "at_engagement", "entry_hour": "at_engagement",
    "entry_dow": "at_engagement",
    "price": "pre_engagement", "extremity": "pre_engagement",
    "drift_1h": "pre_engagement", "drift_6h": "pre_engagement", "drift_24h": "pre_engagement",
    "pre_vol": "pre_engagement", "market_age": "pre_engagement",
    "time_to_resolution": "pre_engagement", "frac_life_elapsed": "pre_engagement",
    "category": "pre_engagement", "market_type": "pre_engagement",
    "outcome_role": "pre_engagement", "favorite_rank": "pre_engagement",
    "event_family": "pre_engagement", "template": "pre_engagement",
    "vol_window": "pre_engagement", "n_trades_window": "pre_engagement",
    "n_wallets_in": "pre_engagement", "price_rank_in_group": "at_engagement",
    "vol_rank_in_group": "at_engagement", "scarcity": "at_engagement",
    "loyalty": "pre_engagement",
    # post-engagement label-only (edge test ONLY, never a rule input)
    "resolved_outcome": "post_engagement_label_only", "roi": "post_engagement_label_only",
    "exit_mode": "post_engagement_label_only", "post_entry_drift": "post_engagement_label_only",
}

_RULE_USABLE = {"pre_engagement", "at_engagement"}
_FORBIDDEN_HINTS = ("future", "final_", "post_", "_post", "resolved", "outcome_roi", "lookahead")


def classify_feature(name: str) -> str:
    """Return the time scope of a feature. Unknown → fail-closed to
    forbidden_lookahead so an unvetted feature can never enter a rule."""
    if name in FEATURE_TIME_SCOPE:
        return FEATURE_TIME_SCOPE[name]
    low = name.lower()
    if any(h in low for h in _FORBIDDEN_HINTS):
        return "forbidden_lookahead"
    return "forbidden_lookahead"  # fail-closed: classify before use


def leakage_violations(feature_names: Iterable[str]) -> list[str]:
    """Features that must NOT inform a selection rule (post/forbidden/unknown)."""
    return [n for n in feature_names if classify_feature(n) not in _RULE_USABLE]


def assert_rule_features_clean(feature_names: Iterable[str]) -> None:
    bad = leakage_violations(feature_names)
    if bad:
        raise ValueError(f"leakage: features not usable for a selection rule: {bad}")


# --------------------------------------------------------------------------
# G4 — multiple testing (Benjamini-Hochberg) + promotion gate
# --------------------------------------------------------------------------

def benjamini_hochberg(pvalues: Sequence[float]) -> list[float]:
    """BH-adjusted q-values (FDR). Monotone, clipped to [0,1]. Order preserved."""
    m = len(pvalues)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    prev = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        k = m - rank + 1  # original BH rank (largest p first)
        val = min(prev, pvalues[i] * m / k)
        q[i] = val
        prev = val
    return [max(0.0, min(1.0, v)) for v in q]


MIN_SAMPLE = int(os.getenv("CSE_MIN_SAMPLE", "30"))
MIN_EFFECT = float(os.getenv("CSE_MIN_EFFECT", "0.2"))
MAX_Q = float(os.getenv("CSE_MAX_Q", "0.05"))


def promotion_decision(*, n: int, effect_size: float, q_value: float,
                       oos_confirmed: bool) -> tuple[bool, list[str]]:
    """A discovery is promoted to thesis ONLY if it clears every gate."""
    reasons: list[str] = []
    if n < MIN_SAMPLE:
        reasons.append(f"sample<{MIN_SAMPLE}")
    if abs(effect_size) < MIN_EFFECT:
        reasons.append(f"effect<{MIN_EFFECT}")
    if q_value > MAX_Q:
        reasons.append(f"fdr_q>{MAX_Q}")
    if not oos_confirmed:
        reasons.append("no_oos_confirmation")
    return (not reasons), reasons


# --------------------------------------------------------------------------
# G1 — candidate-universe quality
# --------------------------------------------------------------------------

def candidate_universe_quality(*, n_picked: int, n_unselected: int,
                               point_in_time_verified: bool,
                               price_available_share: float,
                               resolution_available_share: float,
                               min_picked: int = 10, min_unselected: int = 10) -> dict:
    quality = "ok"
    if not point_in_time_verified or n_picked < min_picked or n_unselected < min_unselected:
        quality = "invalid"
    elif price_available_share < 0.6 or resolution_available_share < 0.6:
        quality = "sparse"
    total = n_picked + n_unselected
    return {
        "n_picked": n_picked, "n_unselected_candidates": n_unselected,
        "coverage_ratio": (n_picked / total) if total else None,
        "point_in_time_verified": point_in_time_verified,
        "price_available_share": price_available_share,
        "resolution_available_share": resolution_available_share,
        "scope_quality": quality,
        "underpowered": quality == "invalid",
    }


# --------------------------------------------------------------------------
# G2 — matching balance (nearest-neighbor / C7)
# --------------------------------------------------------------------------

def standardized_mean_diff(a: Sequence[float], b: Sequence[float]) -> float | None:
    """SMD between two groups on one feature. ~0 = balanced; |SMD|>0.1 = imbalance."""
    a = [float(x) for x in a if x is not None]
    b = [float(x) for x in b if x is not None]
    if len(a) < 2 or len(b) < 2:
        return None
    ma, mb = _mean(a), _mean(b)
    va, vb = _var(a, ma), _var(b, mb)
    pooled = math.sqrt((va + vb) / 2)
    if pooled == 0:
        # both groups constant: equal means → no separation; different → perfect
        return 0.0 if ma == mb else math.copysign(10.0, ma - mb)
    return (ma - mb) / pooled


def matching_balance(picked: Mapping[str, Sequence[float]],
                     controls: Mapping[str, Sequence[float]],
                     *, caliper: float = 0.1) -> dict:
    """Per-feature SMD between picked and matched controls; caliper pass if all
    |SMD| <= caliper. Poor balance → nearest_neighbor_scope_invalid."""
    smds = {}
    for feat in picked:
        if feat in controls:
            smds[feat] = standardized_mean_diff(picked[feat], controls[feat])
    worst = max((abs(v) for v in smds.values() if v is not None), default=None)
    caliper_pass = worst is not None and worst <= caliper
    return {
        "smd_per_feature": smds,
        "worst_abs_smd": worst,
        "caliper": caliper,
        "caliper_pass": caliper_pass,
        "scope_valid": bool(caliper_pass),
        "verdict": "ok" if caliper_pass else "nearest_neighbor_scope_invalid",
    }


# --------------------------------------------------------------------------
# G5 — negative controls / placebo
# --------------------------------------------------------------------------

def placebo_pass(observed_stat: float, null_stats: Sequence[float], *, alpha: float = 0.05) -> dict:
    """Is the observed statistic beyond the placebo null distribution? If a real
    'edge' is matched by shuffled/placebo runs, the battery is too permissive."""
    null = [float(x) for x in null_stats]
    if not null:
        return {"passed": False, "reason": "no_null", "empirical_p": None}
    n_ge = sum(1 for x in null if x >= observed_stat)
    emp_p = (n_ge + 1) / (len(null) + 1)
    passed = emp_p <= alpha
    return {
        "passed": passed,
        "empirical_p": emp_p,
        "n_null": len(null),
        "battery_too_permissive": not passed,
    }


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _var(xs: Sequence[float], m: float) -> float:
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1) if len(xs) > 1 else 0.0
