"""Canonical Hyperliquid funding accounting.

``funding_pnl_bps`` is always signed as PnL: positive helps the position, negative hurts it.
Funding prints apply at hourly boundaries, so a hold covers ``(entry_ts_ms, exit_ts_ms]``.
"""
from __future__ import annotations

from dataclasses import dataclass


HOUR_MS = 3_600_000
MAX_PRINT_SKEW_MS = 60_000
COMPLETE_STATUSES = frozenset({"complete", "not_applicable"})


@dataclass(frozen=True, slots=True)
class FundingResult:
    value_bps: float | None
    status: str
    expected_prints: int
    observed_prints: int


def expected_funding_times(entry_ts_ms: int, exit_ts_ms: int) -> tuple[int, ...]:
    entry = int(entry_ts_ms)
    exit_ = int(exit_ts_ms)
    if exit_ <= entry:
        return ()
    first = (entry // HOUR_MS + 1) * HOUR_MS
    return tuple(range(first, exit_ + 1, HOUR_MS))


def funding_pnl(
    rows: list[dict] | None,
    *,
    side: str,
    entry_ts_ms: int,
    exit_ts_ms: int,
    fetch_failed: bool = False,
    exit_after_funding: bool | None = None,
) -> FundingResult:
    """Return complete funding contribution or an explicit censoring status."""
    expected = expected_funding_times(entry_ts_ms, exit_ts_ms)
    if fetch_failed:
        return FundingResult(None, "fetch_failed", len(expected), 0)
    if not expected:
        return FundingResult(0.0, "not_applicable", 0, 0)
    if int(exit_ts_ms) % HOUR_MS == 0 and exit_after_funding is None:
        return FundingResult(None, "ambiguous_boundary", len(expected), 0)
    if int(exit_ts_ms) % HOUR_MS == 0 and exit_after_funding is False:
        expected = expected[:-1]
        if not expected:
            return FundingResult(0.0, "not_applicable", 0, 0)

    by_time: dict[int, float] = {}
    for row in rows or []:
        try:
            ts_ms = int(row["time"])
            rate = float(row["rate"])
        except (KeyError, TypeError, ValueError):
            continue
        print_time = ((ts_ms + HOUR_MS // 2) // HOUR_MS) * HOUR_MS
        if (abs(ts_ms - print_time) <= MAX_PRINT_SKEW_MS and
                print_time in expected and print_time not in by_time):
            by_time[print_time] = rate
    if any(ts_ms not in by_time for ts_ms in expected):
        return FundingResult(None, "missing", len(expected), len(by_time))
    raw_bps = sum(by_time[ts_ms] for ts_ms in expected) * 10_000.0
    contribution = raw_bps if str(side).lower() == "short" else -raw_bps
    return FundingResult(round(contribution, 3), "complete", len(expected), len(by_time))


def legacy_copy_cost_bps(result: FundingResult) -> float | None:
    """Legacy ``hl_copy_book.funding_bps`` is cost-signed."""
    return -result.value_bps if result.value_bps is not None else None


def legacy_altshort_contribution_bps(result: FundingResult) -> float | None:
    """Legacy paper ``funding_bps`` is already contribution-signed."""
    return result.value_bps
