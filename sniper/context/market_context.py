"""Context query layer — thin composition over db.py helpers.

All raw SQL lives in ``db.py``.  This module re-exports and composes
those helpers to give strategy plugins a clean import path::

    from sniper.context import find_affected_markets, commodity_price_history
"""
from __future__ import annotations

from db import (
    commodity_price_history,
    find_affected_markets,
    insider_convergence_check,
    market_price_series,
    recent_news_for_market,
)

__all__ = [
    "commodity_price_history",
    "find_affected_markets",
    "insider_convergence_check",
    "market_price_series",
    "recent_news_for_market",
]
