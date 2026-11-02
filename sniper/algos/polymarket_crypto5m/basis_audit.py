"""Binance↔Chainlink basis audit — ``binance_chainlink_settlement_proxy_audit`` (Lane B, research).

Answers the first Phase-0 research question: **how often would a Binance-based Up/Down direction
label disagree with the actual Chainlink-settled Polymarket winner**, overall and near the threshold?
This is the hard floor on any Binance-driven edge. Binance is NEVER settlement truth and never drives
a paper decision — this lane is strictly for measuring proxy danger. Not a Chainlink reconstruction.
"""
from __future__ import annotations

from typing import Optional


def binance_direction(*, start: Optional[float], end: Optional[float]) -> Optional[str]:
    """Proxy direction, mirroring the Chainlink settlement rule (end >= start -> Up)."""
    if start is None or end is None:
        return None
    return "Up" if float(end) >= float(start) else "Down"


def audit_row(*, condition_id: str, price_to_beat: Optional[float], binance_start: Optional[float],
              binance_end: Optional[float], winner: Optional[str], window_start_ts: int) -> dict:
    """One window's proxy-audit record. ``usable`` is False when any input needed is missing."""
    direction = binance_direction(start=binance_start, end=binance_end)
    basis = (binance_start - price_to_beat) if (binance_start is not None and price_to_beat is not None) else None
    move = (binance_end - binance_start) if (binance_start is not None and binance_end is not None) else None
    # "near threshold" = how close the FINAL price is to the Chainlink settlement threshold
    # (price_to_beat). Small distance => coin-flip window where a tiny basis flips the outcome. A flat
    # window far from the threshold is NOT near-threshold (that was the abs_move bug).
    distance_to_threshold = (abs(binance_end - price_to_beat)
                             if (binance_end is not None and price_to_beat is not None) else None)
    usable = direction is not None and winner is not None
    agrees = (direction == winner) if usable else None
    return {
        "condition_id": condition_id,
        "window_start_ts": window_start_ts,
        "binance_direction": direction,
        "winner": winner,
        "agrees_with_winner": agrees,
        "basis_binance_minus_chainlink": basis,
        "binance_move": move,
        "abs_binance_move": abs(move) if move is not None else None,
        "distance_to_threshold": distance_to_threshold,
        "usable": usable,
    }


def aggregate(rows: list, *, near_threshold_abs: float = 5.0) -> dict:
    """Overall + near-threshold disagreement rates and missing count over audit rows.

    Near-threshold uses ``distance_to_threshold`` (|binance_end - price_to_beat|), not the raw move.
    """
    usable = [r for r in rows if r.get("usable")]
    missing = sum(1 for r in rows if not r.get("usable"))
    disagree = [r for r in usable if r["agrees_with_winner"] is False]
    near = [r for r in usable if (r.get("distance_to_threshold") is not None
                                  and r["distance_to_threshold"] <= near_threshold_abs)]
    near_disagree = [r for r in near if r["agrees_with_winner"] is False]
    return {
        "n": len(rows),
        "n_usable": len(usable),
        "missing": missing,
        "disagreement_rate": (len(disagree) / len(usable)) if usable else None,
        "near_threshold_n": len(near),
        "near_threshold_disagreement_rate": (len(near_disagree) / len(near)) if near else None,
    }
