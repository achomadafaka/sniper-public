"""Target-slice source-position anatomy for copy-paper setup."""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sniper.strategies.source_position_copy_v1 import (
    DEFAULTS,
    SourceObservation,
    SourcePositionState,
    _build_source_positions,
    _detect_threshold_crossing,
    _matches_target_filter,
    _parse_params,
)


@dataclass(frozen=True)
class TargetAnatomyConfig:
    thresholds_usdc: tuple[int, ...] = (5000, 10000, 25000, 50000, 100000)
    windows_hours: tuple[int, ...] = (1, 6, 24)
    min_crossings_for_default: int = 20
    min_crossings_for_fallback: int = 20
    params: dict[str, Any] = field(default_factory=dict)


def _first_buy(state: SourcePositionState) -> SourceObservation | None:
    buy_rows = [
        obs for obs in (state.observations or [])
        if str(obs.side).upper().strip() == "BUY"
    ]
    if not buy_rows:
        return None
    return min(buy_rows, key=lambda obs: (int(obs.observed_ts), str(obs.activity_key)))


def _position_outcome(state: SourcePositionState) -> bool | None:
    for obs in reversed(state.observations or []):
        if "won" in (obs.raw_json or {}):
            return bool(obs.raw_json["won"])
    return None


def _position_economics(state: SourcePositionState) -> tuple[float, float, int | None]:
    won = _position_outcome(state)
    if won is None:
        return state.buy_cost, 0.0, None
    held_shares = max(state.buy_shares - state.sell_shares, 0.0)
    payout = held_shares if won else 0.0
    pnl = state.sell_proceeds + payout - state.buy_cost
    return state.buy_cost, pnl, int(won)


def _included_positions(
    observations: list[SourceObservation],
    params: dict[str, Any],
) -> tuple[list[SourcePositionState], Counter[str]]:
    included: list[SourcePositionState] = []
    excluded: Counter[str] = Counter()
    for state in _build_source_positions(observations).values():
        first_buy = _first_buy(state)
        if first_buy is None:
            excluded["no_buy"] += 1
            continue
        passes, reason = _matches_target_filter(first_buy, params)
        if passes:
            included.append(state)
        else:
            excluded[reason] += 1
    return included, excluded


def _threshold_grid(
    positions: list[SourcePositionState],
    *,
    config: TargetAnatomyConfig,
    params: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    grid: dict[str, dict[str, Any]] = {}
    for threshold in config.thresholds_usdc:
        for window in config.windows_hours:
            wins = 0
            losses = 0
            crossed_positions = 0
            total_cost = 0.0
            total_pnl = 0.0
            times_to_cross: list[int] = []
            grid_params = {
                **params,
                "source_commitment_threshold_usdc": threshold,
                "source_commitment_window_hours": window,
                "max_threshold_age_minutes": 10**12,
            }
            for state in positions:
                trigger = _detect_threshold_crossing(state, grid_params, now_ts=10**12)
                if not trigger.should_enter:
                    continue
                crossed_positions += 1
                first_buy = _first_buy(state)
                if first_buy is not None and trigger.cross_ts is not None:
                    times_to_cross.append(int(trigger.cross_ts) - int(first_buy.observed_ts))
                cost, pnl, won = _position_economics(state)
                total_cost += cost
                total_pnl += pnl
                if won == 1:
                    wins += 1
                elif won == 0:
                    losses += 1
            grid[f"{threshold}|{window}h"] = {
                "threshold_usdc": threshold,
                "window_hours": window,
                "crossed_positions": crossed_positions,
                "wins": wins,
                "losses": losses,
                "roi_pct": total_pnl / total_cost * 100.0 if total_cost else 0.0,
                "median_time_to_cross_seconds": (
                    int(statistics.median(times_to_cross)) if times_to_cross else None
                ),
            }
    return grid


def _recommended_trigger(
    grid: dict[str, dict[str, Any]],
    *,
    config: TargetAnatomyConfig,
    params: dict[str, Any],
) -> dict[str, Any]:
    default = grid.get("25000|6h", {})
    if int(default.get("crossed_positions") or 0) >= config.min_crossings_for_default:
        threshold = 25000
        window = 6
        reason = "default_supported"
    else:
        feasible = [
            row for row in grid.values()
            if int(row["window_hours"]) == 6
            and int(row["crossed_positions"]) >= config.min_crossings_for_fallback
        ]
        feasible.sort(key=lambda row: int(row["threshold_usdc"]))
        if feasible:
            threshold = int(feasible[0]["threshold_usdc"])
            window = int(feasible[0]["window_hours"])
            reason = "fallback_lowest_feasible_threshold"
        else:
            threshold = 25000
            window = 6
            reason = "default_insufficient_sample"

    return {
        "source_commitment_threshold_usdc": threshold,
        "source_commitment_window_hours": window,
        "source_commitment_mode": "net_buy_cost",
        "entry_price_min": float(params.get("entry_price_min") or DEFAULTS["entry_price_min"]),
        "max_threshold_age_minutes": int(params.get("max_threshold_age_minutes") or DEFAULTS["max_threshold_age_minutes"]),
        "max_price_drift_from_source_vwap_pct": float(
            params.get("max_price_drift_from_source_vwap_pct")
            or DEFAULTS["max_price_drift_from_source_vwap_pct"]
        ),
        "selection_reason": reason,
    }


def build_target_position_anatomy(
    observations: list[SourceObservation],
    *,
    config: TargetAnatomyConfig,
) -> dict[str, Any]:
    """Build a JSON-serializable target-slice anatomy report.

    This is development-data anatomy only. It picks a paper trigger before
    forward results exist; it does not validate the trigger.
    """
    params = _parse_params({"params": {**DEFAULTS, **config.params}})
    positions, excluded = _included_positions(observations, params)
    grid = _threshold_grid(positions, config=config, params=params)
    return {
        "target_universe": {
            "included_positions": len(positions),
            "excluded_by_reason": dict(excluded),
        },
        "threshold_grid": grid,
        "recommended_trigger": _recommended_trigger(grid, config=config, params=params),
        "validation_note": "historical target anatomy is development data, not validation",
    }
