"""Exploratory first-passage target ladder and near-miss diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# v3 (2026-08-07, operator approved): three cells above 1,200. The v2 grid found EV
# monotone to its top cell in every cut of the v4 cohort (TP=1200@60s: +723 [613,834]
# on the retro-v6 band, n=132/121 creators) while PROFIT_FIRST winners' max favorable
# excursion ran a median +4,007 bps -- the optimum sits above what v2 can see. The
# version bump keeps pooling honest: v3 cells never mix with v2 cells in analysis.
TARGET_BPS = (200, 400, 600, 800, 1_200, 2_000, 3_000, 5_000)
HORIZON_MS = (5_000, 10_000, 20_000, 60_000, 120_000, 300_000)
LOSS_BARRIER_BPS = -800
DIAGNOSTIC_EVALUATOR_VERSION = "target-ladder-v3"


@dataclass(frozen=True)
class ExecutablePoint:
    elapsed_ms: int
    exit_kind: str
    net_return_bps: int | None
    source_healthy: bool
    migration: bool


def _evaluate(
    points: tuple[ExecutablePoint, ...], *, target_bps: int, horizon_ms: int
) -> dict[str, object]:
    best: int | None = None
    worst: int | None = None
    transient_streak = 0
    result = "pending"
    terminal_ms = points[-1].elapsed_ms if points else 0
    time_to_target_ms = None
    for point in points:
        if point.elapsed_ms > horizon_ms:
            break
        terminal_ms = point.elapsed_ms
        if not point.source_healthy:
            result = "data_gap"
            break
        if point.migration:
            result = "migration"
            break
        if point.exit_kind == "deterministic_failure":
            result = "exit_failure"
            break
        if point.exit_kind == "transient_failure":
            transient_streak += 1
            if transient_streak >= 2:
                result = "exit_failure"
                break
            continue
        transient_streak = 0
        if point.net_return_bps is None:
            continue
        value = int(point.net_return_bps)
        best = value if best is None else max(best, value)
        worst = value if worst is None else min(worst, value)
        if value <= LOSS_BARRIER_BPS:
            result = "loss"
            break
        if value >= target_bps:
            result = "hit"
            time_to_target_ms = point.elapsed_ms
            break
    if result == "pending" and points and points[-1].elapsed_ms >= horizon_ms:
        result = "timeout"
        terminal_ms = horizon_ms
    return {
        "target_bps": target_bps,
        "horizon_ms": horizon_ms,
        "result": result,
        "terminal_ms": terminal_ms,
        "time_to_target_ms": time_to_target_ms,
        "best_return_bps": best,
        "worst_return_bps": worst,
        "evaluator_version": DIAGNOSTIC_EVALUATOR_VERSION,
    }


def evaluate_target_ladder(
    observations: Iterable[ExecutablePoint],
) -> tuple[dict[str, object], ...]:
    points = tuple(sorted(observations, key=lambda point: point.elapsed_ms))
    return tuple(
        _evaluate(points, target_bps=target, horizon_ms=horizon)
        for target in TARGET_BPS
        for horizon in HORIZON_MS
    )


def near_miss(
    observations: Iterable[ExecutablePoint], *, target_bps: int = 1_200
) -> dict[str, int] | None:
    executable = tuple(
        point
        for point in observations
        if point.exit_kind == "executable" and point.net_return_bps is not None
    )
    if not executable:
        return None
    best = max(executable, key=lambda point: (int(point.net_return_bps), -point.elapsed_ms))
    value = int(best.net_return_bps)
    if value <= 0 or value >= target_bps:
        return None
    return {"best_return_bps": value, "time_to_best_ms": best.elapsed_ms}
