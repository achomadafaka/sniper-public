"""Sports context query layer — thin re-export over db.py helpers.

All raw SQL lives in ``db.py``.  This module re-exports the sports-specific
helpers so strategy plugins have a clean import path::

    from sniper.context.sports_context import find_matching_polymarket_h2h_markets
"""
from __future__ import annotations

from db import (
    find_matching_polymarket_h2h_markets,
    insider_convergence_check,
    odds_event_history,
    polymarket_market_outcomes,
    recent_odds_events,
)

__all__ = [
    "find_matching_polymarket_h2h_markets",
    "insider_convergence_check",
    "odds_event_history",
    "polymarket_market_outcomes",
    "recent_odds_events",
]
