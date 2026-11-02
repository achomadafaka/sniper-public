"""Pure research mechanics for the frozen HL cross-sectional rollover study."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from collections import deque
import hashlib
import json
import math
import random
import statistics
from typing import Any

import numpy as np
import pandas as pd

from sniper.hyperlive.altshort_universe import (
    coin_comparison_key,
    provider_coin,
    provider_coin_index,
)


HOUR_MS = 3_600_000


@dataclass(frozen=True)
class RolloverPolicy:
    policy_version: str
    excluded_coins: tuple[str, ...]
    min_history_hours: int
    min_history_coverage: float
    return_percentile_min: float
    runup_percentile_min: float
    rollover_6h_bps_max: float
    max_entries_per_24h: int
    take_profit_bps: float
    cap_hours: int
    fee_bps: float
    slippage_each_bps: float
    min_benchmark_coverage: float
    bootstrap_replications: int
    bootstrap_seed: int

    _V1 = {
        "policy_version": "rollover-v1",
        "excluded_coins": ("BTC", "ETH", "SOL"),
        "min_history_hours": 720,
        "min_history_coverage": 0.8,
        "return_percentile_min": 0.95,
        "runup_percentile_min": 0.95,
        "rollover_6h_bps_max": 0.0,
        "max_entries_per_24h": 5,
        "take_profit_bps": 200.0,
        "cap_hours": 96,
        "fee_bps": 9.0,
        "slippage_each_bps": 15.0,
        "min_benchmark_coverage": 0.8,
        "bootstrap_replications": 5000,
        "bootstrap_seed": 20260727,
    }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RolloverPolicy":
        expected = {field.name for field in fields(cls)}
        supplied = set(value)
        if supplied != expected:
            missing = sorted(expected - supplied)
            unknown = sorted(supplied - expected)
            detail = missing[0] if missing else unknown[0]
            raise ValueError(f"invalid policy field: {detail}")
        normalized = dict(value)
        excluded = normalized.get("excluded_coins")
        if not isinstance(excluded, (list, tuple)):
            raise ValueError("excluded_coins must be a list")
        normalized["excluded_coins"] = tuple(str(item) for item in excluded)
        for field, expected_value in cls._V1.items():
            actual = normalized[field]
            if isinstance(expected_value, float):
                try:
                    finite = math.isfinite(float(actual))
                except (TypeError, ValueError):
                    finite = False
                if not finite or float(actual) != expected_value:
                    raise ValueError(f"{field} differs from frozen rollover-v1")
            elif actual != expected_value:
                raise ValueError(f"{field} differs from frozen rollover-v1")
        return cls(**normalized)

    @property
    def round_trip_cost_bps(self) -> float:
        return self.fee_bps + 2.0 * self.slippage_each_bps

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["excluded_coins"] = list(self.excluded_coins)
        return value


def policy_sha256(policy: RolloverPolicy) -> str:
    payload = json.dumps(policy.to_dict(), sort_keys=True, separators=(",", ":")) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CausalFrames:
    index: pd.Index
    coins: tuple[str, ...]
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    eligible: pd.DataFrame
    signal: pd.DataFrame
    features: dict[str, pd.DataFrame]


def _wide(
    candles: dict[str, pd.DataFrame], *, field: str, index: pd.Index, coins: tuple[str, ...]
) -> pd.DataFrame:
    series = {}
    for coin in coins:
        frame = candles[coin]
        if field not in frame or "t" not in frame:
            raise ValueError(f"missing candle field {field}: {coin}")
        values = frame[["t", field]].drop_duplicates("t", keep="last").set_index("t")[field]
        series[coin] = pd.to_numeric(values, errors="coerce")
    return pd.concat(series, axis=1).reindex(index).astype(float)


def build_causal_frames(
    candles: dict[str, pd.DataFrame], policy: RolloverPolicy
) -> CausalFrames:
    provider_coin_index(candles)
    normalized = {
        provider_coin(coin): frame.copy()
        for coin, frame in candles.items()
        if coin_comparison_key(coin) not in policy.excluded_coins
    }
    normalized = {coin: frame for coin, frame in normalized.items() if coin}
    if not normalized:
        raise ValueError("no eligible candle inputs")
    coins = tuple(sorted(normalized))
    timestamps = [
        int(value)
        for frame in normalized.values()
        for value in frame.get("t", pd.Series(dtype=np.int64)).dropna()
    ]
    if not timestamps:
        raise ValueError("candle inputs have no timestamps")
    index = pd.Index(
        range(min(timestamps) // HOUR_MS * HOUR_MS, max(timestamps) // HOUR_MS * HOUR_MS + HOUR_MS, HOUR_MS),
        name="ts_ms",
        dtype=np.int64,
    )
    open_ = _wide(normalized, field="o", index=index, coins=coins)
    high = _wide(normalized, field="h", index=index, coins=coins)
    low = _wide(normalized, field="l", index=index, coins=coins)
    close = _wide(normalized, field="c", index=index, coins=coins)
    volume = _wide(normalized, field="v", index=index, coins=coins)

    history_n = close.shift(1).notna().rolling(
        policy.min_history_hours, min_periods=1
    ).sum()
    eligible = history_n.ge(
        math.ceil(policy.min_history_hours * policy.min_history_coverage)
    )
    for coin in coins:
        first = close[coin].first_valid_index()
        if first is None:
            eligible[coin] = False
        else:
            eligible.loc[index < int(first) + policy.min_history_hours * HOUR_MS, coin] = False

    returns = {
        hours: (close / close.shift(hours) - 1.0) * 10_000.0
        for hours in (1, 3, 6, 12, 24, 72)
    }
    trailing_low = close.rolling(25, min_periods=20).min()
    trailing_high = close.rolling(25, min_periods=20).max()
    runup = (close / trailing_low - 1.0) * 10_000.0
    off_high = (close / trailing_high - 1.0) * 10_000.0
    hourly = close.pct_change(fill_method=None) * 10_000.0
    realized_vol = hourly.rolling(24, min_periods=19).std()
    volume_median = volume.shift(1).rolling(24, min_periods=19).median()
    volume_ratio = volume / volume_median
    candle_range = (high - low).replace(0.0, np.nan)
    close_location = (close - low) / candle_range
    upper_wick = (high - close) / candle_range
    return_percentile = returns[24].where(eligible).rank(axis=1, pct=True)
    runup_percentile = runup.where(eligible).rank(axis=1, pct=True)
    vol_percentile = realized_vol.where(eligible).rank(axis=1, pct=True)
    basket_72h = returns[72].where(eligible).mean(axis=1)
    dispersion_24h = returns[24].where(eligible).std(axis=1)
    signal = (
        eligible
        & return_percentile.ge(policy.return_percentile_min)
        & runup_percentile.ge(policy.runup_percentile_min)
        & returns[6].le(policy.rollover_6h_bps_max)
    )
    features = {
        **{f"return_{hours}h_bps": frame for hours, frame in returns.items()},
        "runup_from_low_24h_bps": runup,
        "distance_from_high_24h_bps": off_high,
        "realized_vol_24h_bps": realized_vol,
        "volume_ratio_24h": volume_ratio,
        "close_location": close_location,
        "upper_wick_share": upper_wick,
        "return_24h_percentile": return_percentile,
        "runup_24h_percentile": runup_percentile,
        "vol_24h_percentile": vol_percentile,
        "basket_return_72h_bps": pd.DataFrame(
            np.repeat(basket_72h.to_numpy()[:, None], len(coins), axis=1),
            index=index,
            columns=coins,
        ),
        "dispersion_24h_bps": pd.DataFrame(
            np.repeat(dispersion_24h.to_numpy()[:, None], len(coins), axis=1),
            index=index,
            columns=coins,
        ),
    }
    return CausalFrames(
        index=index,
        coins=coins,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        eligible=eligible,
        signal=signal,
        features=features,
    )


def _funding_frames(
    funding: dict[str, pd.DataFrame], frames: CausalFrames
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rates = pd.DataFrame(np.nan, index=frames.index, columns=frames.coins, dtype=float)
    frame_symbols = provider_coin_index(frames.coins)
    provider_coin_index(funding)
    for raw_coin, source in funding.items():
        coin = frame_symbols.get(coin_comparison_key(raw_coin))
        if coin not in rates or source is None or source.empty:
            continue
        if not {"time", "rate"} <= set(source.columns):
            raise ValueError(f"missing funding fields: {coin}")
        values = source[["time", "rate"]].copy()
        values["bucket"] = pd.to_numeric(values["time"], errors="coerce") // HOUR_MS * HOUR_MS
        values["rate"] = pd.to_numeric(values["rate"], errors="coerce")
        hourly = values.dropna().groupby("bucket")["rate"].sum()
        common = frames.index.intersection(hourly.index.astype(np.int64))
        rates.loc[common, coin] = hourly.loc[common].to_numpy()
    return rates, rates.fillna(0.0).cumsum()


def _funding_interval(
    rates: pd.DataFrame,
    cumulative: pd.DataFrame,
    *,
    entry_ts_ms: int,
    exit_ts_ms: int,
) -> tuple[pd.Series, pd.Series]:
    entry_i = rates.index.get_loc(entry_ts_ms)
    exit_i = rates.index.get_loc(exit_ts_ms)
    expected = max(0, exit_i - entry_i)
    if expected == 0:
        return pd.Series(0.0, index=rates.columns), pd.Series(True, index=rates.columns)
    total = cumulative.iloc[exit_i - 1].copy()
    if entry_i > 0:
        total -= cumulative.iloc[entry_i - 1]
    coverage = rates.iloc[entry_i:exit_i].notna().sum(axis=0).eq(expected)
    return total * 10_000.0, coverage


def _decision_features(frames: CausalFrames, *, ts_ms: int, coin: str) -> dict[str, float | None]:
    result = {}
    for name, frame in frames.features.items():
        value = frame.at[ts_ms, coin]
        result[name] = float(value) if pd.notna(value) and math.isfinite(float(value)) else None
    return result


def simulate_rollover(
    frames: CausalFrames,
    funding: dict[str, pd.DataFrame],
    policy: RolloverPolicy,
    *,
    max_open: int | None = None,
    candidate_gate: pd.DataFrame | None = None,
    execution_gate: pd.DataFrame | None = None,
) -> list[dict]:
    for name, gate in (
        ("candidate_gate", candidate_gate),
        ("execution_gate", execution_gate),
    ):
        if gate is None:
            continue
        if not gate.index.equals(frames.index):
            raise ValueError(f"{name} index differs from causal frames")
        if set(gate.columns) != set(frames.coins):
            raise ValueError(f"{name} columns differ from causal frames")
    if candidate_gate is not None:
        candidate_gate = candidate_gate.reindex(columns=frames.coins)
    if execution_gate is not None:
        execution_gate = execution_gate.reindex(columns=frames.coins)
    rates, cumulative = _funding_frames(funding, frames)
    previous = pd.Series(False, index=frames.coins)
    previous_eligible = pd.Series(False, index=frames.coins)
    pending: dict[int, list[tuple[str, float, int]]] = {}
    open_positions: dict[str, dict] = {}
    recent_entries: deque[int] = deque()
    events: list[dict] = []
    max_open_n = int(max_open) if max_open is not None else len(frames.coins)
    last_ts = int(frames.index.max())

    for offset, raw_ts in enumerate(frames.index):
        ts_ms = int(raw_ts)
        current = frames.close.loc[ts_ms]
        for coin, position in list(open_positions.items()):
            mark = current.get(coin)
            if pd.isna(mark) or float(mark) <= 0:
                continue
            gross = (position["entry_mark"] - float(mark)) / position["entry_mark"] * 10_000.0
            reason = None
            if gross >= policy.take_profit_bps:
                reason = "take_profit"
            elif ts_ms >= position["entry_ts_ms"] + policy.cap_hours * HOUR_MS:
                reason = "time_cap"
            if reason is None:
                continue
            if position.get("shadow"):
                del open_positions[coin]
                continue
            funding_bps, complete = _funding_interval(
                rates,
                cumulative,
                entry_ts_ms=position["entry_ts_ms"],
                exit_ts_ms=ts_ms,
            )
            valid_funding = bool(complete[coin])
            event = {
                **position,
                "status": "closed" if valid_funding else "invalid",
                "exclusion_reason": None if valid_funding else "missing_funding",
                "exit_ts_ms": ts_ms,
                "exit_mark": float(mark),
                "exit_reason": reason,
                "gross_bps": gross,
                "funding_bps": float(funding_bps[coin]) if valid_funding else None,
                "net_bps": (
                    gross + float(funding_bps[coin]) - policy.round_trip_cost_bps
                    if valid_funding
                    else None
                ),
            }
            events.append(event)
            del open_positions[coin]

        while recent_entries and recent_entries[0] <= ts_ms - 24 * HOUR_MS:
            recent_entries.popleft()
        slots = min(
            policy.max_entries_per_24h - len(recent_entries),
            max_open_n - len(open_positions),
        )
        for coin, score, decision_ts_ms in pending.pop(ts_ms, []):
            if slots <= 0:
                break
            if coin in open_positions or not bool(frames.eligible.at[ts_ms, coin]):
                continue
            entry = frames.open.at[ts_ms, coin]
            if pd.isna(entry) or float(entry) <= 0:
                continue
            recent_entries.append(ts_ms)
            slots -= 1
            if execution_gate is not None and not bool(
                execution_gate.at[int(decision_ts_ms), coin]
            ):
                if ts_ms <= last_ts - policy.cap_hours * HOUR_MS:
                    open_positions[coin] = {
                        "coin": coin,
                        "entry_ts_ms": ts_ms,
                        "entry_mark": float(entry),
                        "shadow": True,
                    }
                continue
            base = {
                "coin": coin,
                "decision_ts_ms": int(decision_ts_ms),
                "entry_ts_ms": ts_ms,
                "entry_mark": float(entry),
                "decision_features": _decision_features(
                    frames, ts_ms=int(decision_ts_ms), coin=coin
                ),
            }
            decision_rate = rates.at[int(decision_ts_ms), coin]
            prior_rate = (
                rates.at[int(decision_ts_ms) - 24 * HOUR_MS, coin]
                if int(decision_ts_ms) - 24 * HOUR_MS in rates.index
                else np.nan
            )
            base["decision_features"]["funding_rate_bps"] = (
                float(decision_rate) * 10_000.0 if pd.notna(decision_rate) else None
            )
            base["decision_features"]["funding_change_24h_bps"] = (
                (float(decision_rate) - float(prior_rate)) * 10_000.0
                if pd.notna(decision_rate) and pd.notna(prior_rate)
                else None
            )
            if ts_ms > last_ts - policy.cap_hours * HOUR_MS:
                events.append(
                    {
                        **base,
                        "status": "censored",
                        "exclusion_reason": "insufficient_exit_horizon",
                        "exit_ts_ms": None,
                        "exit_mark": None,
                        "exit_reason": None,
                        "gross_bps": None,
                        "funding_bps": None,
                        "net_bps": None,
                    }
                )
            else:
                open_positions[coin] = base

        current_signal = frames.signal.loc[ts_ms].fillna(False).astype(bool)
        crossed = current_signal & ~previous & previous_eligible
        if candidate_gate is not None:
            crossed &= candidate_gate.loc[ts_ms].fillna(False).astype(bool)
        if offset + 1 < len(frames.index):
            next_ts_ms = int(frames.index[offset + 1])
            candidates = [
                (
                    coin,
                    float(frames.features["return_24h_bps"].at[ts_ms, coin]),
                    ts_ms,
                )
                for coin in frames.coins
                if bool(crossed.get(coin, False)) and coin not in open_positions
            ]
            pending[next_ts_ms] = sorted(candidates, key=lambda item: item[1], reverse=True)
        previous = current_signal
        previous_eligible = frames.eligible.loc[ts_ms].fillna(False).astype(bool)

    for coin, position in open_positions.items():
        if position.get("shadow"):
            continue
        events.append(
            {
                **position,
                "status": "censored",
                "exclusion_reason": "missing_exit_mark",
                "exit_ts_ms": None,
                "exit_mark": None,
                "exit_reason": None,
                "gross_bps": None,
                "funding_bps": None,
                "net_bps": None,
            }
        )
    return events


def replay_sampled_exits(
    events: list[dict],
    frames: CausalFrames,
    funding: dict[str, pd.DataFrame],
    policy: RolloverPolicy,
    *,
    cap_hours: int,
    stop_loss_bps: float | None = None,
) -> list[dict]:
    """Replay causal sampled-close exits without changing admitted entries."""
    if not isinstance(cap_hours, int) or not 1 <= cap_hours <= policy.cap_hours:
        raise ValueError("cap_hours must be within the frozen horizon")
    if stop_loss_bps is not None and (
        not math.isfinite(float(stop_loss_bps)) or float(stop_loss_bps) <= 0
    ):
        raise ValueError("stop_loss_bps must be finite and positive")
    rates, cumulative = _funding_frames(funding, frames)
    last_ts_ms = int(frames.index.max())
    exit_fields = {
        "status",
        "exclusion_reason",
        "exit_ts_ms",
        "exit_mark",
        "exit_reason",
        "gross_bps",
        "funding_bps",
        "net_bps",
        "benchmark_valid",
        "benchmark_reason",
        "benchmark_eligible_n",
        "benchmark_covered_n",
        "benchmark_coverage",
        "benchmark_net_bps",
        "residual_bps",
    }
    replayed = []
    for source in events:
        base = {key: value for key, value in source.items() if key not in exit_fields}
        coin = str(base["coin"])
        entry_ts_ms = int(base["entry_ts_ms"])
        entry_mark = float(base["entry_mark"])
        cap_ts_ms = entry_ts_ms + cap_hours * HOUR_MS
        if cap_ts_ms > last_ts_ms:
            replayed.append(
                {
                    **base,
                    "status": "censored",
                    "exclusion_reason": "insufficient_exit_horizon",
                    "exit_ts_ms": None,
                    "exit_mark": None,
                    "exit_reason": None,
                    "gross_bps": None,
                    "funding_bps": None,
                    "net_bps": None,
                }
            )
            continue
        closed = None
        for ts_ms in range(entry_ts_ms + HOUR_MS, cap_ts_ms + 1, HOUR_MS):
            mark = frames.close.at[ts_ms, coin]
            if pd.isna(mark) or float(mark) <= 0:
                continue
            gross = (entry_mark - float(mark)) / entry_mark * 10_000.0
            reason = None
            if gross >= policy.take_profit_bps:
                reason = "take_profit"
            elif stop_loss_bps is not None and gross <= -float(stop_loss_bps):
                reason = "stop_loss"
            elif ts_ms >= cap_ts_ms:
                reason = "time_cap"
            if reason is None:
                continue
            funding_bps, complete = _funding_interval(
                rates,
                cumulative,
                entry_ts_ms=entry_ts_ms,
                exit_ts_ms=ts_ms,
            )
            valid_funding = bool(complete[coin])
            closed = {
                **base,
                "status": "closed" if valid_funding else "invalid",
                "exclusion_reason": None if valid_funding else "missing_funding",
                "exit_ts_ms": ts_ms,
                "exit_mark": float(mark),
                "exit_reason": reason,
                "gross_bps": gross,
                "funding_bps": float(funding_bps[coin]) if valid_funding else None,
                "net_bps": (
                    gross + float(funding_bps[coin]) - policy.round_trip_cost_bps
                    if valid_funding
                    else None
                ),
            }
            break
        replayed.append(
            closed
            if closed is not None
            else {
                **base,
                "status": "censored",
                "exclusion_reason": "missing_exit_mark",
                "exit_ts_ms": None,
                "exit_mark": None,
                "exit_reason": None,
                "gross_bps": None,
                "funding_bps": None,
                "net_bps": None,
            }
        )
    return replayed


def attach_matched_basket(
    events: list[dict],
    frames: CausalFrames,
    funding: dict[str, pd.DataFrame],
    policy: RolloverPolicy,
) -> list[dict]:
    rates, cumulative = _funding_frames(funding, frames)
    attached = []
    for source in events:
        event = dict(source)
        if event.get("status") != "closed" or event.get("net_bps") is None:
            attached.append(
                {
                    **event,
                    "benchmark_valid": False,
                    "benchmark_reason": event.get("exclusion_reason") or "event_not_closed",
                    "benchmark_eligible_n": 0,
                    "benchmark_covered_n": 0,
                    "benchmark_coverage": 0.0,
                    "benchmark_net_bps": None,
                    "residual_bps": None,
                }
            )
            continue
        entry_ts_ms = int(event["entry_ts_ms"])
        exit_ts_ms = int(event["exit_ts_ms"])
        entry = frames.open.loc[entry_ts_ms]
        exit_ = frames.close.loc[exit_ts_ms]
        eligible = frames.eligible.loc[entry_ts_ms] & entry.notna() & entry.gt(0)
        eligible.loc[event["coin"]] = False
        funding_bps, funding_complete = _funding_interval(
            rates,
            cumulative,
            entry_ts_ms=entry_ts_ms,
            exit_ts_ms=exit_ts_ms,
        )
        covered = eligible & exit_.notna() & exit_.gt(0) & funding_complete
        eligible_n = int(eligible.sum())
        covered_n = int(covered.sum())
        coverage = covered_n / eligible_n if eligible_n else 0.0
        if coverage < policy.min_benchmark_coverage:
            attached.append(
                {
                    **event,
                    "benchmark_valid": False,
                    "benchmark_reason": "insufficient_benchmark_coverage",
                    "benchmark_eligible_n": eligible_n,
                    "benchmark_covered_n": covered_n,
                    "benchmark_coverage": coverage,
                    "benchmark_net_bps": None,
                    "residual_bps": None,
                }
            )
            continue
        gross = (entry[covered] - exit_[covered]) / entry[covered] * 10_000.0
        benchmark_net = float(
            (gross + funding_bps[covered]).mean() - policy.round_trip_cost_bps
        )
        attached.append(
            {
                **event,
                "benchmark_valid": True,
                "benchmark_reason": "ok",
                "benchmark_eligible_n": eligible_n,
                "benchmark_covered_n": covered_n,
                "benchmark_coverage": coverage,
                "benchmark_net_bps": benchmark_net,
                "residual_bps": float(event["net_bps"]) - benchmark_net,
            }
        )
    return attached


def _entry_day(event: dict) -> str:
    return pd.Timestamp(int(event["entry_ts_ms"]), unit="ms", tz="UTC").date().isoformat()


def _closed_events(events: list[dict], field: str | None = None) -> list[dict]:
    rows = [event for event in events if event.get("status") == "closed"]
    if field is not None:
        rows = [event for event in rows if event.get(field) is not None]
    return rows


def block_bootstrap(
    events: list[dict],
    field: str,
    *,
    seed: int,
    replications: int,
) -> dict:
    rows = _closed_events(events, field)
    by_day: dict[str, list[float]] = {}
    for event in rows:
        by_day.setdefault(_entry_day(event), []).append(float(event[field]))
    days = sorted(by_day)
    if not days:
        return {"n": 0, "clusters": 0, "mean": None, "ci95": [None, None]}
    rng = random.Random(int(seed))
    values = []
    for _ in range(int(replications)):
        sampled = [rng.choice(days) for _ in days]
        flat = [value for day in sampled for value in by_day[day]]
        values.append(statistics.fmean(flat))
    low, high = np.percentile(values, [2.5, 97.5])
    observed = [float(event[field]) for event in rows]
    return {
        "n": len(rows),
        "clusters": len(days),
        "mean": statistics.fmean(observed),
        "ci95": [float(low), float(high)],
    }


def _summary(
    events: list[dict], *, seed: int, replications: int
) -> dict:
    rows = _closed_events(events, "net_bps")
    residual_rows = [event for event in rows if event.get("residual_bps") is not None]
    net = [float(event["net_bps"]) for event in rows]
    residual = [float(event["residual_bps"]) for event in residual_rows]
    net_inference = block_bootstrap(
        rows, "net_bps", seed=seed, replications=replications
    )
    residual_inference = block_bootstrap(
        residual_rows, "residual_bps", seed=seed + 1, replications=replications
    )
    by_coin: dict[str, list[dict]] = {}
    for event in rows:
        by_coin.setdefault(str(event["coin"]), []).append(event)
    top_coin = None
    if by_coin:
        top_coin = max(
            by_coin,
            key=lambda coin: abs(sum(float(row["net_bps"]) for row in by_coin[coin])),
        )
    without_top = [event for event in rows if event["coin"] != top_coin]
    without_top_residual = [
        event for event in without_top if event.get("residual_bps") is not None
    ]
    cap = [event for event in rows if event.get("exit_reason") == "time_cap"]
    coverages = [
        float(event["benchmark_coverage"])
        for event in rows
        if event.get("benchmark_coverage") is not None
    ]
    return {
        "n": len(rows),
        "entry_days": len({_entry_day(event) for event in rows}),
        "mean_net_bps": statistics.fmean(net) if net else None,
        "median_net_bps": statistics.median(net) if net else None,
        "win_rate": sum(value > 0 for value in net) / len(net) if net else None,
        "ci95_net_bps": net_inference["ci95"],
        "mean_residual_bps": statistics.fmean(residual) if residual else None,
        "ci95_residual_bps": residual_inference["ci95"],
        "cap_exit_n": len(cap),
        "cap_exit_mean_net_bps": (
            statistics.fmean(float(event["net_bps"]) for event in cap) if cap else None
        ),
        "worst_net_bps": min(net) if net else None,
        "top_coin": top_coin,
        "leave_top_coin_mean_net_bps": (
            statistics.fmean(float(event["net_bps"]) for event in without_top)
            if without_top
            else None
        ),
        "leave_top_coin_mean_residual_bps": (
            statistics.fmean(
                float(event["residual_bps"]) for event in without_top_residual
            )
            if without_top_residual
            else None
        ),
        "benchmark_coverage": statistics.fmean(coverages) if coverages else 0.0,
        "funding_coverage": (
            sum(event.get("funding_bps") is not None for event in events if event.get("status") in {"closed", "invalid"})
            / sum(event.get("status") in {"closed", "invalid"} for event in events)
            if any(event.get("status") in {"closed", "invalid"} for event in events)
            else 0.0
        ),
    }


def summarize_events(
    events: list[dict], *, seed: int, replications: int
) -> dict:
    """Public stable summary used by the cache-only runner."""
    return _summary(events, seed=seed, replications=replications)


def period_report(
    events: list[dict],
    *,
    period: str,
    seed: int,
    replications: int,
) -> list[dict]:
    if period not in {"month", "week"}:
        raise ValueError("period must be month or week")
    grouped: dict[str, list[dict]] = {}
    for event in _closed_events(events, "net_bps"):
        stamp = pd.Timestamp(int(event["entry_ts_ms"]), unit="ms", tz="UTC")
        if period == "month":
            key = stamp.strftime("%Y-%m")
        else:
            iso = stamp.isocalendar()
            key = f"{iso.year}-W{iso.week:02d}"
        grouped.setdefault(key, []).append(event)
    output = []
    for offset, key in enumerate(sorted(grouped)):
        summary = _summary(
            grouped[key], seed=seed + offset * 17, replications=replications
        )
        output.append(
            {
                "period": key,
                **summary,
                "inference_status": (
                    "inferential" if summary["entry_days"] >= 5 else "descriptive"
                ),
            }
        )
    return output


def causal_regime_report(
    events: list[dict], *, seed: int, replications: int
) -> list[dict]:
    grouped = {"bearish": [], "neutral": [], "bullish": []}
    for event in _closed_events(events, "net_bps"):
        value = (event.get("decision_features") or {}).get("basket_return_72h_bps")
        if value is None or not math.isfinite(float(value)):
            continue
        label = "bearish" if float(value) <= -500 else "bullish" if float(value) >= 500 else "neutral"
        grouped[label].append(event)
    output = []
    for offset, label in enumerate(("bearish", "neutral", "bullish")):
        if grouped[label]:
            output.append(
                {
                    "regime": label,
                    **_summary(
                        grouped[label],
                        seed=seed + offset * 31,
                        replications=replications,
                    ),
                }
            )
    return output


_CONTINUATION_FEATURES = (
    "close_location",
    "upper_wick_share",
    "return_1h_bps",
    "return_3h_bps",
    "return_6h_bps",
    "return_12h_bps",
    "return_24h_bps",
    "return_72h_bps",
    "distance_from_high_24h_bps",
    "realized_vol_24h_bps",
    "volume_ratio_24h",
    "funding_rate_bps",
    "funding_change_24h_bps",
    "basket_return_72h_bps",
    "dispersion_24h_bps",
)

_MISSING_CONTINUATION_FEEDS = (
    "open_interest",
    "liquidation_volume",
    "spot_perp_basis",
    "orderbook_replenishment",
)


def continuation_feature_report(events: list[dict]) -> list[dict]:
    rows = _closed_events(events, "net_bps")
    output = []
    for feature in _CONTINUATION_FEATURES:
        available = [
            event
            for event in rows
            if (event.get("decision_features") or {}).get(feature) is not None
            and math.isfinite(float(event["decision_features"][feature]))
        ]
        if len(available) < 2:
            output.append(
                {"feature": feature, "available": False, "reason": "insufficient_data"}
            )
            continue
        split = statistics.median(
            float(event["decision_features"][feature]) for event in available
        )
        lower = [
            event
            for event in available
            if float(event["decision_features"][feature]) <= split
        ]
        upper = [
            event
            for event in available
            if float(event["decision_features"][feature]) > split
        ]

        def side(prefix: str, values: list[dict]) -> dict:
            cap = [event for event in values if event.get("exit_reason") == "time_cap"]
            return {
                f"{prefix}_n": len(values),
                f"{prefix}_mean_net_bps": (
                    statistics.fmean(float(event["net_bps"]) for event in values)
                    if values
                    else None
                ),
                f"{prefix}_cap_exit_n": len(cap),
                f"{prefix}_cap_exit_mean_net_bps": (
                    statistics.fmean(float(event["net_bps"]) for event in cap)
                    if cap
                    else None
                ),
            }

        output.append(
            {
                "feature": feature,
                "available": True,
                "split_value": float(split),
                **side("lower", lower),
                **side("upper", upper),
            }
        )
    output.extend(
        {"feature": feature, "available": False, "reason": "missing_feed"}
        for feature in _MISSING_CONTINUATION_FEEDS
    )
    return output


def historical_verdict(report: dict) -> dict:
    failed = []
    overall = report.get("overall") or {}
    residual_ci = overall.get("ci95_residual_bps") or [None, None]
    if residual_ci[0] is None or float(residual_ci[0]) <= 0:
        failed.append("residual_ci")
    positive_months = sum(
        (row.get("ci95_net_bps") or [None])[0] is not None
        and float(row["ci95_net_bps"][0]) > 0
        for row in report.get("months", [])
    )
    if positive_months < 2:
        failed.append("positive_months")
    leave_top = overall.get("leave_top_coin_mean_residual_bps")
    if leave_top is None or float(leave_top) <= 0:
        failed.append("leave_top_coin_residual")
    if float(report.get("double_slippage_mean_net_bps") or 0.0) <= 0:
        failed.append("double_slippage_net")
    if float(overall.get("benchmark_coverage") or 0.0) < 0.8:
        failed.append("benchmark_coverage")
    if float(overall.get("funding_coverage") or 0.0) < 0.8:
        failed.append("funding_coverage")
    baseline = (report.get("baselines") or {}).get(
        "extreme_winner_without_rollover"
    ) or {}
    incremental_pairs = (
        (overall.get("mean_net_bps"), baseline.get("mean_net_bps")),
        (overall.get("mean_residual_bps"), baseline.get("mean_residual_bps")),
    )
    if any(
        signal is None
        or control is None
        or float(signal) <= float(control)
        for signal, control in incremental_pairs
    ):
        failed.append("rollover_incremental_value")
    if not report.get("provenance_valid"):
        failed.append("provenance")
    if failed:
        return {
            "status": "candidate_reject",
            "passed": False,
            "blockers": [],
            "failed": failed,
        }
    blockers = []
    if report.get("older_history") == "blocked_paid_archive":
        blockers.append("older_history_blocked_paid_archive")
    return {
        "status": "candidate_continue",
        "passed": True,
        "blockers": blockers,
        "failed": [],
    }
