"""Pure mechanics for the standalone HL exhaustion forward-paper arm."""
from __future__ import annotations

import math


HOUR_MS = 3_600_000


def _close(row: dict) -> float | None:
    try:
        value = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def regime_label(
    basket_return_bps: float | None, *, coverage: float,
    bearish_threshold_bps: float = -500.0, bullish_threshold_bps: float = 500.0,
    min_coverage: float = 0.80,
) -> str:
    """Classify a covered basket; absent and non-finite values are unknown."""
    if float(coverage) < float(min_coverage) or basket_return_bps is None:
        return "unknown"
    basket = float(basket_return_bps)
    if not math.isfinite(basket):
        return "unknown"
    if basket <= float(bearish_threshold_bps):
        return "bearish"
    if basket >= float(bullish_threshold_bps):
        return "bullish"
    return "neutral"


def assign_regime_episodes(rows: list[dict], *, separation_hours: int = 24) -> list[dict]:
    """Annotate bearish hours with deterministic episode IDs.

    Only contiguous, valid neutral/bullish hours establish separation. Unknown or
    missing hours reset the separator, so absence of data cannot manufacture a
    new episode.
    """
    annotated = []
    episode_id = 0
    separator_n = 0
    separator_last_ts = None
    for source in sorted(rows, key=lambda row: int(row["decision_ts_ms"])):
        row = dict(source)
        ts_ms = int(row["decision_ts_ms"])
        label = str(row.get("label") or "unknown")
        row["episode_id"] = None
        if label == "bearish":
            if episode_id == 0 or separator_n >= int(separation_hours):
                episode_id += 1
            row["episode_id"] = episode_id
            separator_n = 0
            separator_last_ts = None
        elif label in {"neutral", "bullish"}:
            if separator_last_ts is not None and ts_ms == separator_last_ts + HOUR_MS:
                separator_n += 1
            else:
                separator_n = 1
            separator_last_ts = ts_ms
        else:
            separator_n = 0
            separator_last_ts = None
        annotated.append(row)
    return annotated


def causal_basket_regime(
    bars_by_coin: dict[str, list[dict]], *, decision_ts_ms: int, universe,
    lookback_hours: int = 72, bearish_threshold_bps: float = -500.0,
    bullish_threshold_bps: float = 500.0, min_coverage: float = 0.80,
) -> dict:
    """Causal equal-weight basket label at one closed hourly decision bucket."""
    anchor_ts = int(decision_ts_ms) - lookback_hours * HOUR_MS
    values = []
    for coin in sorted({str(value).strip().upper() for value in universe}):
        rows = sorted(
            (row for row in bars_by_coin.get(coin, []) if int(row["ts_ms"]) <= decision_ts_ms),
            key=lambda row: int(row["ts_ms"]),
        )
        current_rows = [row for row in rows if int(row["ts_ms"]) <= decision_ts_ms]
        anchor_rows = [row for row in rows if int(row["ts_ms"]) <= anchor_ts]
        if not current_rows or not anchor_rows:
            continue
        current = _close(current_rows[-1])
        anchor = _close(anchor_rows[-1])
        if current is not None and anchor is not None:
            values.append((current - anchor) / anchor * 10_000.0)
    eligible_n = len(set(universe))
    coverage = len(values) / eligible_n if eligible_n else 0.0
    basket = sum(values) / len(values) if values else None
    label = regime_label(
        basket, coverage=coverage,
        bearish_threshold_bps=bearish_threshold_bps,
        bullish_threshold_bps=bullish_threshold_bps,
        min_coverage=min_coverage,
    )
    return {
        "decision_ts_ms": int(decision_ts_ms), "lookback_hours": int(lookback_hours),
        "eligible_n": eligible_n, "covered_n": len(values), "coverage": coverage,
        "basket_return_bps": basket, "label": label,
    }


def runup_crossing(
    bars: list[dict], *, decision_ts_ms: int, lookback_hours: int = 72,
    threshold: float = 0.30, history_hours: int = 720,
    min_history_coverage: float = 0.80,
) -> dict:
    """Return the causal upward-crossing state at one closed hourly bar."""
    eligible = sorted(
        (row for row in bars if int(row["ts_ms"]) <= int(decision_ts_ms)),
        key=lambda row: int(row["ts_ms"]),
    )
    if len(eligible) < lookback_hours + 2:
        return {"valid": False, "reason": "insufficient_history_span", "crossed": False}
    history_start = int(decision_ts_ms) - history_hours * HOUR_MS
    if int(eligible[0]["ts_ms"]) > history_start:
        return {"valid": False, "reason": "insufficient_history_span", "crossed": False}
    history_n = sum(history_start <= int(row["ts_ms"]) < decision_ts_ms for row in eligible)
    coverage = history_n / history_hours
    if coverage < min_history_coverage:
        return {
            "valid": False, "reason": "insufficient_history_coverage",
            "crossed": False, "history_coverage": coverage,
        }

    current = eligible[-1]
    previous = eligible[-2]
    if int(current["ts_ms"]) != int(decision_ts_ms):
        return {"valid": False, "reason": "missing_decision_bar", "crossed": False}

    def value_at(index: int) -> float | None:
        value = _close(eligible[index])
        anchors = [
            _close(row) for row in eligible[max(0, index - lookback_hours):index]
        ]
        anchors = [item for item in anchors if item is not None]
        if value is None or len(anchors) < int(lookback_hours * min_history_coverage):
            return None
        return value / min(anchors) - 1.0

    runup = value_at(len(eligible) - 1)
    previous_runup = value_at(len(eligible) - 2)
    if runup is None or previous_runup is None:
        return {"valid": False, "reason": "insufficient_runup_coverage", "crossed": False}
    return {
        "valid": True,
        "reason": "ok",
        "crossed": previous_runup < threshold <= runup,
        "runup": runup,
        "previous_runup": previous_runup,
        "history_coverage": coverage,
        "decision_ts_ms": int(decision_ts_ms),
        "decision_mark": _close(current),
    }


def evaluate_sampled_exit(
    *, entry_mark: float, entry_ts_ms: int, now_ts_ms: int, current: dict,
    stop_multiple: float = 1.20, take_profit_multiple: float = 0.85,
    cap_hours: int = 8,
) -> dict:
    """Evaluate one sampled mark path. Stop wins a same-sample stop/TP tie."""
    stop = float(entry_mark) * stop_multiple
    take_profit = float(entry_mark) * take_profit_multiple
    high = float(current.get("high", current.get("close")))
    low = float(current.get("low", current.get("close")))
    if high >= stop:
        return {"exit": True, "reason": "stop", "exit_mark": stop}
    if low <= take_profit:
        return {"exit": True, "reason": "take_profit", "exit_mark": take_profit}
    if int(now_ts_ms) >= int(entry_ts_ms) + cap_hours * HOUR_MS:
        return {"exit": True, "reason": "time_cap", "exit_mark": float(current["close"])}
    return {"exit": False, "reason": "hold", "exit_mark": None}


def matched_forward_basket(
    *, entries: dict[str, float], exits: dict[str, float],
    funding_bps: dict[str, float], cost_bps: float, min_coverage: float = 0.80,
) -> dict:
    values = []
    for coin, entry in entries.items():
        if coin not in exits or coin not in funding_bps or float(entry) <= 0:
            continue
        values.append(
            (float(entry) - float(exits[coin])) / float(entry) * 10_000.0
            + float(funding_bps[coin])
        )
    coverage = len(values) / len(entries) if entries else 0.0
    result = {
        "eligible_n": len(entries), "covered_n": len(values), "coverage": coverage,
        "funding_coverage": coverage,
    }
    if coverage < min_coverage:
        return {
            **result, "valid": False, "reason": "insufficient_basket_coverage",
            "basket_net_bps": None,
        }
    return {
        **result, "valid": True, "reason": "ok",
        "basket_net_bps": sum(values) / len(values) - float(cost_bps),
    }
