"""Channel B — closing-line value (CLV) for the fresh-anomaly engine.

Was the held outcome-token's price higher near resolution than the wallet's entry VWAP?
Buying before the line moves in your favor is the ex-ante 'informed timing' tell. We work
entirely in the held outcome token's price space (outcome-native convention, ADR-003), so
`close - entry` is directly the favorable move — no sign flip.

Wraps the existing CLOB `/prices-history` adapter (keyed by CLOB ASSET id, NOT conditionId).
The asset id is recovered from the wallet's own activity rows (free, no extra API).
Returns None when the price is unavailable — the caller treats that as a data gap, never 0.
"""
from __future__ import annotations

from typing import Callable

from .types import PositionEpisode


def _asset_map(raw_activity) -> dict[tuple[str, int], str]:
    """(condition_id, outcome_index) -> CLOB asset id, from the wallet's own activity."""
    out: dict[tuple[str, int], str] = {}
    for a in raw_activity or []:
        asset = str(a.get("asset") or "")
        cond = str(a.get("conditionId") or a.get("condition_id") or "")
        oidx = a.get("outcomeIndex")
        if not asset or not cond or oidx is None:
            continue
        try:
            out[(cond, int(oidx))] = asset
        except (TypeError, ValueError):
            continue
    return out


def build_clv_fn(raw_activity, *, price_history_fn: Callable[[str, int], float | None] | None = None,
                 lead_s: int = 60) -> Callable[[PositionEpisode], float | None]:
    """Return fn(episode) -> signed CLV for the held side, or None when unavailable.

    price_history_fn(asset_id, ts) -> mid price | None. When omitted, a real CLOB-backed
    closure is built lazily (network); inject in tests.
    """
    amap = _asset_map(raw_activity)
    _phf = price_history_fn

    def _phf_default(asset: str, ts: int):
        nonlocal _phf
        if _phf is None:
            from sniper.analysis.wallet_protocol.price_history_adapter import build_price_history_fn
            _phf = build_price_history_fn()
        return _phf(asset, ts)

    fn = price_history_fn or _phf_default

    def _clv(ep: PositionEpisode) -> float | None:
        if not ep.resolution_ts or ep.entry_vwap <= 0:
            return None
        asset = amap.get((ep.condition_id, ep.outcome_index))
        if not asset:
            return None
        close = fn(asset, int(ep.resolution_ts) - int(lead_s))
        if close is None:
            return None
        return float(close) - float(ep.entry_vwap)

    return _clv
