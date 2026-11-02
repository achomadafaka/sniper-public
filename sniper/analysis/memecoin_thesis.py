"""memecoin_thesis.py — read-side helpers for captured trader theses.

A thesis is DISPLAY/CONTEXT only. The live sample skews underwater-cope (e.g. −49% "round-trip
6 figures to make 7 figures"), so the text alone is anti-signal-prone. `thesis_stance` reports
the OBJECTIVE position bucket (their unrealized PnL % at post time) so the operator reads the
words against reality — it makes no claim to parse tone/sentiment.
"""
from __future__ import annotations

from sniper.settings import memecoin as _cfg


def thesis_stance(their_pnl_pct) -> str:
    """underwater | flat | winning | unknown — from their unrealized PnL % (percent, not fraction).
    Thresholds MEMECOIN_THESIS_UNDERWATER_PCT (default -10) / _WINNING_PCT (default +20)."""
    if their_pnl_pct is None:
        return "unknown"
    try:
        p = float(their_pnl_pct)
    except (TypeError, ValueError):
        return "unknown"
    if p != p:          # NaN
        return "unknown"
    if p <= _cfg.MEMECOIN_THESIS_UNDERWATER_PCT:
        return "underwater"
    if p >= _cfg.MEMECOIN_THESIS_WINNING_PCT:
        return "winning"
    return "flat"


def decorate_thesis(row: dict) -> dict:
    """Attach the computed stance to a thesis row (dict from list_theses)."""
    row = dict(row)
    row["stance"] = thesis_stance(row.get("their_pnl_pct"))
    return row
