from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


class InsufficientDepthError(ValueError):
    """Raised when an ask book cannot fill the requested dollar stake."""


@dataclass(frozen=True)
class DepthWeightedFill:
    avg_price: float
    shares: float
    cost_usdc: float
    levels_consumed: int
    unfilled_usdc: float


def _as_positive_float(value: object, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def depth_weighted_buy_price(
    asks: Sequence[Mapping[str, object]],
    *,
    stake_usdc: float,
) -> DepthWeightedFill:
    """Walk ask depth for a dollar-denominated paper buy."""
    target = _as_positive_float(stake_usdc, "stake_usdc")
    remaining = target
    shares = 0.0
    cost = 0.0
    levels_consumed = 0

    sorted_asks = sorted(
        asks,
        key=lambda level: _as_positive_float(level.get("price"), "ask.price"),
    )
    for level in sorted_asks:
        price = _as_positive_float(level.get("price"), "ask.price")
        size = _as_positive_float(level.get("size"), "ask.size")
        max_level_cost = price * size
        level_cost = min(remaining, max_level_cost)
        if level_cost <= 0:
            continue
        shares += level_cost / price
        cost += level_cost
        remaining -= level_cost
        levels_consumed += 1
        if remaining <= 1e-9:
            remaining = 0.0
            break

    if remaining > 1e-9 or shares <= 0:
        raise InsufficientDepthError(
            f"insufficient ask depth for ${target:.2f}; unfilled=${remaining:.2f}"
        )

    return DepthWeightedFill(
        avg_price=cost / shares,
        shares=shares,
        cost_usdc=cost,
        levels_consumed=levels_consumed,
        unfilled_usdc=remaining,
    )


def net_edge(
    *,
    expected_payout: float,
    entry_price: float,
    modeled_cost_per_share: float,
) -> float:
    payout = float(expected_payout)
    price = float(entry_price)
    costs = float(modeled_cost_per_share)
    return payout - price - costs
