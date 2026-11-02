"""Venue-specific fee calculations."""
from __future__ import annotations


def polymarket_taker_fee(
    price: float,
    quantity: float,
    max_fee_rate: float = 0.02,
) -> float:
    """Polymarket taker fee: max_fee_rate × price × (1 - price) × quantity.

    Args:
        price: probability price in [0, 1]
        quantity: number of shares
        max_fee_rate: per-token fee rate (default 2%, varies by market)

    Returns:
        Fee in USDC.
    """
    if max_fee_rate <= 0 or quantity <= 0:
        return 0.0
    return max_fee_rate * price * (1.0 - price) * quantity


def kalshi_taker_fee(
    price_cents: int,
    count: int,
    taker_rate: float = 0.07,
    cap_per_contract: float = 0.50,
) -> float:
    """Kalshi taker fee: taker_rate × p × (1-p) × count, capped per contract.

    Args:
        price_cents: price in cents (1-99)
        count: number of contracts
        taker_rate: fee rate (default 7%)
        cap_per_contract: max fee per contract in dollars

    Returns:
        Fee in USD.
    """
    p = price_cents / 100.0
    per_contract = min(taker_rate * p * (1.0 - p), cap_per_contract)
    return per_contract * count
