from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping


@dataclass(frozen=True)
class PathQuote:
    observation_id: int | str
    observed_at: datetime
    price: float


@dataclass(frozen=True)
class PathMetrics:
    complete: bool
    entry_price: float
    signal_at: datetime
    observations: tuple[PathQuote, ...]
    observation_count: int
    quality_excluded: int
    input_excluded: int
    max_gain_pct: float | None
    max_drawdown_pct: float | None
    terminal_return_pct: float | None
    max_peak_to_later_drawdown_pct: float | None
    recovery_from_trough_pct: float | None
    time_to_peak_s: float | None
    time_to_trough_s: float | None
    observed_duration_s: float
    reclaimed_entry: bool
    new_high_after_trough: bool
    reached_20: bool
    reached_60: bool
    drew_down_30: bool
    drew_down_50: bool


def _time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("path timestamps must be timezone aware")
    return parsed


def _positive(value, *, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be finite and positive") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return parsed


def _normalize_quotes(
    signal_at: datetime,
    rows: Iterable[Mapping],
) -> tuple[tuple[PathQuote, ...], int, int]:
    quotes: list[PathQuote] = []
    quality_excluded = 0
    input_excluded = 0
    for ordinal, row in enumerate(rows):
        if row.get("quality") != "ok":
            quality_excluded += 1
            continue
        try:
            observed_at = _time(row.get("observed_at"))
            price = float(row.get("price"))
        except (TypeError, ValueError):
            input_excluded += 1
            continue
        if observed_at < signal_at or not math.isfinite(price) or price <= 0:
            input_excluded += 1
            continue
        observation_id = row.get("id")
        if observation_id is None:
            observation_id = ordinal
        quotes.append(PathQuote(observation_id, observed_at, price))
    quotes.sort(key=lambda quote: (quote.observed_at, quote.price, str(quote.observation_id)))
    return tuple(quotes), quality_excluded, input_excluded


def analyze_path(entry_price, signal_at, quotes, *, complete: bool) -> PathMetrics:
    entry = _positive(entry_price, name="entry_price")
    signal = _time(signal_at)
    normalized, quality_excluded, input_excluded = _normalize_quotes(signal, quotes)
    if not normalized:
        return PathMetrics(
            complete=bool(complete), entry_price=entry, signal_at=signal, observations=(),
            observation_count=0, quality_excluded=quality_excluded,
            input_excluded=input_excluded, max_gain_pct=None, max_drawdown_pct=None,
            terminal_return_pct=None, max_peak_to_later_drawdown_pct=None,
            recovery_from_trough_pct=None, time_to_peak_s=None, time_to_trough_s=None,
            observed_duration_s=0.0, reclaimed_entry=False,
            new_high_after_trough=False, reached_20=False, reached_60=False,
            drew_down_30=False, drew_down_50=False,
        )

    quote_returns = [(quote.price / entry - 1.0) * 100.0 for quote in normalized]
    returns = [0.0, *quote_returns]
    peak_index = max(range(len(returns)), key=lambda index: (returns[index], -index))
    trough_index = min(range(len(returns)), key=lambda index: (returns[index], index))
    max_gain = returns[peak_index]
    max_drawdown = returns[trough_index]

    running_peak = returns[0]
    max_peak_to_later_drawdown = 0.0
    for value in returns[1:]:
        max_peak_to_later_drawdown = max(max_peak_to_later_drawdown, running_peak - value)
        running_peak = max(running_peak, value)

    later_returns = returns[trough_index + 1:]
    recovery = (max(later_returns) - max_drawdown) if later_returns else 0.0
    reclaimed_entry = max_drawdown < 0 and any(value >= 0 for value in later_returns)
    pre_trough_peak = max(returns[:trough_index + 1])
    new_high = max_drawdown <= -30 and bool(later_returns) and max(later_returns) > pre_trough_peak

    last = normalized[-1].observed_at
    peak_at = signal if peak_index == 0 else normalized[peak_index - 1].observed_at
    trough_at = signal if trough_index == 0 else normalized[trough_index - 1].observed_at
    return PathMetrics(
        complete=bool(complete), entry_price=entry, signal_at=signal,
        observations=normalized, observation_count=len(normalized),
        quality_excluded=quality_excluded, input_excluded=input_excluded,
        max_gain_pct=max_gain, max_drawdown_pct=max_drawdown,
        terminal_return_pct=quote_returns[-1],
        max_peak_to_later_drawdown_pct=max_peak_to_later_drawdown,
        recovery_from_trough_pct=recovery,
        time_to_peak_s=(peak_at - signal).total_seconds(),
        time_to_trough_s=(trough_at - signal).total_seconds(),
        observed_duration_s=(last - signal).total_seconds(),
        reclaimed_entry=reclaimed_entry, new_high_after_trough=new_high,
        reached_20=max_gain >= 20, reached_60=max_gain >= 60,
        drew_down_30=max_drawdown <= -30, drew_down_50=max_drawdown <= -50,
    )


def classify_path(metrics: PathMetrics) -> str:
    if not metrics.complete:
        return "incomplete"
    if metrics.observation_count == 0:
        return "slow_or_unresolved"

    returns = [
        (quote.price / metrics.entry_price - 1.0) * 100.0
        for quote in metrics.observations
    ]
    first_runner = next((index for index, value in enumerate(returns) if value >= 60), None)
    first_deep_drawdown = next((index for index, value in enumerate(returns) if value <= -30), None)
    if first_runner is not None and (
        first_deep_drawdown is None or first_runner < first_deep_drawdown
    ):
        return "immediate_runner"
    if metrics.drew_down_30 and metrics.reclaimed_entry:
        if metrics.new_high_after_trough:
            return "deep_recovery_new_high"
        return "deep_recovery"
    if (
        metrics.reached_20
        and (metrics.max_peak_to_later_drawdown_pct or 0) >= 40
        and not metrics.reclaimed_entry
    ):
        return "pump_fade"
    if not metrics.reached_20 and (metrics.terminal_return_pct or 0) <= -30:
        return "terminal_failure"
    return "slow_or_unresolved"


def classify_observed_path(metrics: PathMetrics) -> str:
    """Describe an observed partial shape without making a terminal-path claim."""
    if metrics.observation_count == 0:
        return "observed_insufficient"
    returns = [
        (quote.price / metrics.entry_price - 1.0) * 100.0
        for quote in metrics.observations
    ]
    first_runner = next((index for index, value in enumerate(returns) if value >= 60), None)
    first_deep_drawdown = next((index for index, value in enumerate(returns) if value <= -30), None)
    if first_runner is not None and (
        first_deep_drawdown is None or first_runner < first_deep_drawdown
    ):
        return "observed_immediate_runner"
    if metrics.drew_down_30 and metrics.reclaimed_entry:
        if metrics.new_high_after_trough:
            return "observed_deep_recovery_new_high"
        return "observed_deep_recovery"
    if metrics.reached_20 and (metrics.max_peak_to_later_drawdown_pct or 0) >= 40:
        return "observed_pump_fade_unresolved"
    if metrics.drew_down_30:
        return "observed_deep_drawdown_unresolved"
    return "observed_unresolved"
