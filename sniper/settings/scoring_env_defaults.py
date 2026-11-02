"""
Canonical scoring env defaults for deterministic test execution.

These values are sourced from analyze_users.py defaults and used by test
infrastructure to avoid drift across conftest/replay harness copies.
"""

from __future__ import annotations

import os

SCORING_ENV_DEFAULTS: dict[str, str] = {
    # Classification gates
    "MAX_POSITIONS": "10",
    "INSIDER_MAX_POSITIONS": "5",
    "MIN_PNL": "-100000",
    "MAX_PNL": "250000",
    "MIN_POSITION_VALUE": "2000",
    "MIN_ORDER_NOTIONAL": "2000",
    "SCORE_FLAG_THRESHOLD": "70",
    "SPECULATOR_SCORE_HIGH": "65",
    # Activity / frequency
    "LOOKBACK_DAYS": "14",
    "FRESH_ACCOUNT_DAYS": "90",
    "MIN_REPEAT_HITS_14D": "3",
    # Insider thresholds
    "INSIDER_MIN_TOTAL_PNL": "20000",
    "INSIDER_MIN_TOP_OPEN_VALUE": "3000",
    "INSIDER_MIN_TOTAL_OPEN_VALUE": "10000",
    # Stream ingestion thresholds (stream_low_buys.py) — must match MIN_ORDER_NOTIONAL above
    "STREAM_LARGE_MIN_NOTIONAL": "5000",
    # Fresh account stream trigger (7 days) vs scorer window (FRESH_ACCOUNT_DAYS=90, capped 30).
    # These are intentionally different: trigger catches signal in real-time,
    # scorer window determines how long the historical bonus applies.
    "FRESH_ACCOUNT_MAX_AGE_DAYS": "7",
    # analyze_users default for this key resolves via MIN_TRADE_NOTIONAL->MIN_ORDER_NOTIONAL
    "INSIDER_MIN_SERIOUS_TRADE_NOTIONAL": "2000",
    "INSIDER_MIN_SERIOUS_TRADES_14D": "3",
    # Emerging insider thresholds
    "EMERGING_SCORE_THRESHOLD": "65",
    "EMERGING_MIN_POSITION_INSIDER_SCORE": "68",
    "EMERGING_MIN_TOP_OPEN_VALUE": "2500",
    "EMERGING_MIN_TOTAL_OPEN_VALUE": "5000",
    "EMERGING_MAX_POSITION_SPECULATOR_SCORE": "70",
    "EMERGING_MAX_POSITION_SPECULATOR_COUNT": "1",
    # Position scoring
    "POSITION_MIN_OPEN_VALUE": "2000",
    "POSITION_SCORE_THRESHOLD": "65",
    "POSITION_SPECULATOR_THRESHOLD": "65",
    # Overlay penalty caps
    "INSIDER_OVERLAY_MANY_POSITIONS": "8",
    "INSIDER_OVERLAY_VERY_MANY_POSITIONS": "12",
    "INSIDER_OVERLAY_MAX_PENALTY": "30",
}


def pin_scoring_env(*, force: bool) -> None:
    """Apply canonical scoring env defaults.

    force=False: uses setdefault (non-destructive to shell overrides)
    force=True : overwrites existing env to enforce deterministic replay
    """
    if force:
        for key, value in SCORING_ENV_DEFAULTS.items():
            os.environ[key] = value
        return
    for key, value in SCORING_ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)

