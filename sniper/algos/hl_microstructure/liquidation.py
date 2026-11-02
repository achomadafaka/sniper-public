"""Liquidation/whale stress adapter for the Hyperliquid fusion lane."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class LiquidationStress:
    asset: str
    direction: str | None
    score: float
    notional_at_risk: float
    nearest_distance_pct: float
    reason: str
    stale: bool = False


def _f(value, default: float | None = None) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _ts(row: dict) -> int | None:
    for key in ("updated_ts_ms", "last_seen_ts_ms", "timestamp_ms", "time_ms"):
        if row.get(key) is not None:
            try:
                return int(row[key])
            except (TypeError, ValueError):
                return None
    return None


def _side(row: dict) -> str:
    return str(row.get("side") or row.get("position_side") or "").lower()


def stress_from_positions(
    rows: list[dict],
    *,
    now_ms: int,
    max_age_ms: int = 300_000,
) -> dict[str, LiquidationStress]:
    """Aggregate isolated whale liquidation stress by asset."""
    by_asset: dict[str, LiquidationStress] = {}
    for row in rows:
        asset = str(row.get("asset") or row.get("coin") or "").upper()
        if not asset:
            continue
        seen = _ts(row)
        if seen is None or int(now_ms) - seen > int(max_age_ms):
            continue
        margin_mode = str(row.get("margin_mode") or row.get("marginMode") or "isolated").lower()
        if "cross" in margin_mode:
            continue
        distance = _f(row.get("distance_to_liquidation_pct") or row.get("distance_pct"))
        notional = _f(row.get("notional_usdc") or row.get("notional") or row.get("position_value"), 0.0) or 0.0
        if distance is None or distance <= 0 or notional <= 0:
            continue
        side = _side(row)
        if side not in ("long", "short"):
            continue
        direction = "short" if side == "long" else "long"
        distance_score = max(0.0, min(1.0, (5.0 - distance) / 5.0))
        notional_score = min(1.0, math.log10(notional + 1.0) / 7.0)
        score = min(1.0, 0.65 * distance_score + 0.35 * notional_score)
        if score <= 0:
            continue
        stress = LiquidationStress(
            asset=asset,
            direction=direction,
            score=score,
            notional_at_risk=notional,
            nearest_distance_pct=distance,
            reason="isolated_whale_near_liquidation",
            stale=False,
        )
        prev = by_asset.get(asset)
        if prev is None or stress.score > prev.score:
            by_asset[asset] = stress
    return by_asset
