"""Null-Strategy Baseline surface — `null_baseline_surface`.

Spec: docs/superpowers/specs/2026-05-22-null-baseline-surface-design.md

Compares a wallet's hold-to-resolution side-selection return, on the
markets it traded, against always-favorite / always-underdog / random
null strategies — separating side-selection skill from riding the chalk.

For a resolved binary position the ROI is pure arithmetic: bought at
`avgPrice`, resolves to 1 (win) or 0 (loss), so `roi = (1-avgPrice)/
avgPrice` if the side won, `-1` if it lost. The favorite is the side
priced > 0.50. `wallet_ew_roi` scores the wallet's CHOSEN side on this
same hold-to-resolution basis — a side-selection measure, not the
wallet's realized return (they differ for early-exited positions).

HARD WALL — display-only. `compute_null_baseline_surface` is read ONLY by
the report renderer. It MUST NEVER feed `overall_verdict`, `gate_result`,
`primary_edge_type`, `copyability_note`, branch/hypothesis logic, or
append to the top-level `data_quality_flags`. Caveats live in the
surface's own `caveats` list. Pure function — no I/O. Reports whether the
wallet beat/matched/underperformed the chalk, never "skill".
"""
from __future__ import annotations

from typing import Any, Mapping

SURFACE_VERSION = 1
RESOLVED_WIN = 0.97        # curPrice ≥ this → the wallet's side won
RESOLVED_LOSS = 0.03       # curPrice ≤ this → the wallet's side lost
PRICE_BAND_LO = 0.02       # eligible avgPrice strictly within (lo, hi)
PRICE_BAND_HI = 0.98
MIN_ELIGIBLE = 20          # below this → underpowered
COVERAGE_FLOOR = 0.50      # eligible/total below this → proxy_only
FAVORITE_DOMINANCE = 0.90  # wallet on the favorite above this share → proxy_only
EDGE_BAND = 0.03           # |edge_vs_favorite| below this → matches_chalk

_SEVERITY = {"ok": 0, "proxy_only": 1, "underpowered": 2, "data_unavailable": 3}


def _num_or_none(x: Any) -> float | None:
    """Coerce to float, or None when absent/unparseable. None must NOT be
    silently treated as 0.0 — a missing curPrice would become a phantom
    resolved loss."""
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _roi(price: float, side_won: bool) -> float:
    """Hold-to-resolution ROI of a $1 bet on a side at `price`."""
    return (1.0 - price) / price if side_won else -1.0


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _empty(data_state: str, reason: str, *, n_total: int) -> dict:
    return {
        "surface_version": SURFACE_VERSION,
        "data_state": data_state,
        "comparison": "inconclusive",
        "n_total_closed": n_total,
        "n_eligible": 0,
        "coverage": None,
        "wallet_ew_roi": None,
        "favorite_ew_roi": None,
        "underdog_ew_roi": None,
        "random_ew_roi": None,
        "edge_vs_favorite": None,
        "favorite_hit_rate": None,
        "n_on_favorite": 0,
        "n_on_underdog": 0,
        "caveats": [reason],
    }


def compute_null_baseline_surface(
    closed_positions: list[Mapping[str, Any]],
) -> dict:
    """Build the Null-Strategy Baseline surface. Pure, deterministic,
    display-only."""
    closed = list(closed_positions or [])
    n_total = len(closed)
    if n_total == 0:
        return _empty("data_unavailable", "no_closed_positions", n_total=0)

    # ---- §5.1 eligibility + §5.2 per-market ROI -------------------------
    wallet_rois: list[float] = []
    favorite_rois: list[float] = []
    underdog_rois: list[float] = []
    favorite_won_flags: list[bool] = []
    n_on_favorite = 0
    for p in closed:
        if p.get("negRisk") or p.get("negativeRisk"):
            continue
        avg = _num_or_none(p.get("avgPrice"))
        cur = _num_or_none(p.get("curPrice"))
        if avg is None or cur is None:
            continue
        if not (PRICE_BAND_LO < avg < PRICE_BAND_HI):
            continue
        if not (cur >= RESOLVED_WIN or cur <= RESOLVED_LOSS):
            continue
        # eligible.
        won = cur >= RESOLVED_WIN
        p_w = avg
        p_fav = max(p_w, 1.0 - p_w)
        p_dog = 1.0 - p_fav
        wallet_on_favorite = p_w >= 0.50
        favorite_won = won if wallet_on_favorite else (not won)
        wallet_rois.append(_roi(p_w, won))
        favorite_rois.append(_roi(p_fav, favorite_won))
        underdog_rois.append(_roi(p_dog, not favorite_won))
        favorite_won_flags.append(favorite_won)
        if wallet_on_favorite:
            n_on_favorite += 1

    n_eligible = len(wallet_rois)
    if n_eligible == 0:
        return _empty("data_unavailable", "no_eligible_positions",
                      n_total=n_total)

    # ---- §5.3 aggregation ----------------------------------------------
    wallet_ew = sum(wallet_rois) / n_eligible
    favorite_ew = sum(favorite_rois) / n_eligible
    underdog_ew = sum(underdog_rois) / n_eligible
    # random_ew = mean of per-market midpoints = midpoint of the two
    # aggregates, valid only because both are equal-weighted over the
    # identical eligible set (keep this if the aggregation ever changes).
    random_ew = (favorite_ew + underdog_ew) / 2.0
    edge = wallet_ew - favorite_ew
    favorite_hit_rate = sum(favorite_won_flags) / n_eligible
    n_on_underdog = n_eligible - n_on_favorite
    coverage = n_eligible / n_total

    # ---- §6 data_state + caveats ---------------------------------------
    caveats: list[str] = []
    state = "ok"
    if n_eligible < MIN_ELIGIBLE:
        state = _worse(state, "underpowered")
    if coverage < COVERAGE_FLOOR:
        caveats.append(
            f"LOW_COVERAGE: only {coverage:.0%} of closed positions were "
            "eligible (resolved, binary-priced) — the baseline sees a slice.")
        state = _worse(state, "proxy_only")
    favorite_share = n_on_favorite / n_eligible
    if favorite_share > FAVORITE_DOMINANCE:
        caveats.append(
            f"FAVORITE_DOMINANCE: the wallet bet the favorite on "
            f"{favorite_share:.0%} of markets — too little deviation from "
            "chalk to judge side selection.")
        state = _worse(state, "proxy_only")

    # ---- §5.4 comparison label -----------------------------------------
    if state != "ok":
        comparison = "inconclusive"
    elif edge > EDGE_BAND:
        comparison = "beats_chalk"
    elif edge < -EDGE_BAND:
        comparison = "underperforms_chalk"
    else:
        comparison = "matches_chalk"

    return {
        "surface_version": SURFACE_VERSION,
        "data_state": state,
        "comparison": comparison,
        "n_total_closed": n_total,
        "n_eligible": n_eligible,
        "coverage": round(coverage, 4),
        "wallet_ew_roi": round(wallet_ew, 6),
        "favorite_ew_roi": round(favorite_ew, 6),
        "underdog_ew_roi": round(underdog_ew, 6),
        "random_ew_roi": round(random_ew, 6),
        "edge_vs_favorite": round(edge, 6),
        "favorite_hit_rate": round(favorite_hit_rate, 4),
        "n_on_favorite": n_on_favorite,
        "n_on_underdog": n_on_underdog,
        "caveats": caveats,
    }
