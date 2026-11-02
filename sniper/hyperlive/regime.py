"""Regime classifier — decides which setups are live and provides context. Deterministic,
from the computed snapshot features. Calm regimes deliberately gate most setups OFF so the
engine doesn't force trades."""
from __future__ import annotations

from sniper.hyperlive.models import MarketSnapshot

# regime -> set of applicable setups
REGIME_SETUPS = {
    "calm":       {"cross_venue", "funding_reversion"},          # swing carry play even when quiet
    "trending":   {"continuation", "cross_venue", "breakout", "funding_reversion"},
    "ranging":    {"reversion", "cluster_magnet", "cross_venue", "basis_reversion", "funding_reversion"},
    "dislocation": {"reversion", "event_reaction", "cluster_magnet", "basis_reversion"},
    "cascade":    {"event_reaction", "continuation", "post_cascade"},
}


def classify(snapshot: MarketSnapshot) -> str:
    f = snapshot.features
    vol = float(f.get("vol_bps") or 0.0)
    ret60 = abs(float(f.get("ret60_bps") or 0.0))
    liq_rate = float(f.get("liq_rate") or 0.0)        # liquidations/min, recent
    depth_ratio = float(f.get("depth_ratio") or 1.0)  # < 1 = book thinning vs its baseline
    # A fragile (thin) book amplifies impact, so a smaller move already means stress:
    # drop the dislocation multiplier when depth has collapsed.
    k = 2.5 if depth_ratio < 0.6 else 4.0
    dislocation = ret60 >= max(25.0, k * vol) if vol > 0 else ret60 >= 25.0
    if liq_rate >= 3.0:
        return "cascade"
    if dislocation:
        return "dislocation"
    if vol < 4.0:
        return "calm"
    # trending vs ranging by persistence of the 60s vs 300s direction
    r60 = float(f.get("ret60_bps") or 0.0)
    r300 = float(f.get("ret300_bps") or 0.0)
    if r60 != 0 and r300 != 0 and (r60 > 0) == (r300 > 0) and abs(r300) >= 2 * vol:
        return "trending"
    return "ranging"


def applicable_setups(regime: str) -> set[str]:
    return REGIME_SETUPS.get(regime, set())
