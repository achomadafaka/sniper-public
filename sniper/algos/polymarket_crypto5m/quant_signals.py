"""Phase 2B — unified per-window signal panel (pure).

Fuses every available tool into one record the runner persists per window: multi-source latent state
(quant_fusion), structural P(Up) under both realized and implied (DVOL) vol, order-book microstructure
(OFI / microprice / imbalance), cross-source basis + structural-vs-market residual, the realized/implied
vol regime, session/time-of-day, and the C0 (random) / C1 (favorite) benchmark sides. The component
probabilities are collected for quant_ensemble. Pure — the runner supplies the live numbers.
"""
from __future__ import annotations

import hashlib
import math

from . import quant_fusion as QF

DISAGREEMENT_DIVERGENT_BPS = 25.0


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _q_structural(now, start, vol_per_s, secs_remaining):
    if None in (now, start, vol_per_s) or start <= 0 or now <= 0 or vol_per_s <= 0 or secs_remaining <= 0:
        return None
    return _phi(math.log(now / start) / (vol_per_s * math.sqrt(secs_remaining)))


def _mid(book):
    """Midpoint of a token book, or None for a crossed/locked/degenerate/out-of-range book (so a broken
    book can never be silently read as a clean 0.5)."""
    bb, ba = (book or {}).get("best_bid"), (book or {}).get("best_ask")
    if bb is None or ba is None:
        return None
    bb, ba = float(bb), float(ba)
    if not (0.0 < bb < ba < 1.0):
        return None
    return (bb + ba) / 2.0


def _q_market(up_book, down_book):
    """P(Up) reconciled from BOTH token books: average the Up midpoint and (1 - Down midpoint). The two
    needn't agree through the spread, so using both is more robust than the Up midpoint alone."""
    up_mid = _mid(up_book)
    down_mid = _mid(down_book)
    if up_mid is not None and down_mid is not None:
        return (up_mid + (1.0 - down_mid)) / 2.0
    if up_mid is not None:
        return up_mid
    if down_mid is not None:
        return 1.0 - down_mid
    return None


def _ofi(book):
    bids, asks = (book or {}).get("bids") or [], (book or {}).get("asks") or []
    if not bids or not asks:
        return None
    b, a = float(bids[0][1]), float(asks[0][1])
    return round((b - a) / (b + a), 6) if (b + a) > 0 else None


def _microprice(book):
    bb, ba = (book or {}).get("best_bid"), (book or {}).get("best_ask")
    bids, asks = (book or {}).get("bids") or [], (book or {}).get("asks") or []
    if bb is None or ba is None or not bids or not asks:
        return None
    b, a = float(bids[0][1]), float(asks[0][1])
    return round((float(bb) * a + float(ba) * b) / (b + a), 6) if (b + a) > 0 else None


def _c0_side(condition_id):
    h = hashlib.sha256(f"c0|{condition_id}".encode()).hexdigest()
    return "Up" if int(h, 16) % 2 == 0 else "Down"


def _session(hour):
    if hour is None:
        return "unknown"
    h = int(hour) % 24
    return "asia" if h < 8 else ("eu" if h < 16 else "us")


def build_signal_panel(*, condition_id, sources, up_book, down_book, binance_start, binance_now,
                       realized_vol_per_s, seconds_remaining, implied_vol_per_s=None, q_mc=None,
                       q_residual=None, hour=None) -> dict:
    fusion = QF.fuse_latent_state(sources)
    q_market = _q_market(up_book, down_book)
    q_struct_realized = _q_structural(binance_now, binance_start, realized_vol_per_s, seconds_remaining)
    q_struct_implied = _q_structural(binance_now, binance_start, implied_vol_per_s, seconds_remaining)

    vol_ratio = None
    if realized_vol_per_s and implied_vol_per_s and implied_vol_per_s > 0:
        vol_ratio = round(realized_vol_per_s / implied_vol_per_s, 6)
    vol_regime = "unknown"
    if vol_ratio is not None:
        vol_regime = "realized_hot" if vol_ratio > 1.2 else ("realized_cold" if vol_ratio < 0.8 else "normal")

    basis_bps = fusion["source_disagreement_bps"]
    disagreement_regime = ("divergent" if (basis_bps is not None and basis_bps > DISAGREEMENT_DIVERGENT_BPS)
                           else "calm")

    # all signals — for per-component Brier evaluation
    components = {"q_market": q_market, "q_struct_realized": q_struct_realized}
    if q_struct_implied is not None:
        components["q_struct_implied"] = q_struct_implied
    if q_mc is not None:
        components["q_mc"] = q_mc
    if q_residual is not None:
        components["q_residual"] = q_residual
    components = {k: v for k, v in components.items() if v is not None}

    # decorrelated set for the ENSEMBLE: market prior + ONE structural vote (realized) + residual.
    # q_struct_implied and q_mc are Binance-move-derived (correlated with realized) -> kept as diagnostics
    # in `components`, but excluded from the ensemble so the structural family cannot out-vote the market.
    ensemble_components = {}
    if q_market is not None:
        ensemble_components["q_market"] = q_market
    if q_struct_realized is not None:
        ensemble_components["q_structural"] = q_struct_realized
    if q_residual is not None:
        ensemble_components["q_residual"] = q_residual

    # an ensemble of only the market prior carries no information beyond the price -> degraded.
    n_ensemble = len(ensemble_components)
    ensemble_degraded = "q_market" not in ensemble_components or n_ensemble < 2
    up_mid, down_mid = _mid(up_book), _mid(down_book)
    market_coherent = (abs(up_mid + down_mid - 1.0) <= 0.08
                       if (up_mid is not None and down_mid is not None) else None)

    return {
        "fusion": fusion,
        "q_market": (round(q_market, 6) if q_market is not None else None),
        "q_struct_realized": (round(q_struct_realized, 6) if q_struct_realized is not None else None),
        "q_struct_implied": (round(q_struct_implied, 6) if q_struct_implied is not None else None),
        "q_mc": q_mc,
        "q_residual": q_residual,
        "components": components,
        "ensemble_components": ensemble_components,
        "n_ensemble_components": n_ensemble,
        "ensemble_degraded": ensemble_degraded,
        "market_coherent": market_coherent,
        "structural_minus_market": (round(q_struct_realized - q_market, 6)
                                    if (q_struct_realized is not None and q_market is not None) else None),
        "cross_source_basis_bps": basis_bps,
        "up_order_flow_imbalance": _ofi(up_book),
        "down_order_flow_imbalance": _ofi(down_book),
        "up_microprice": _microprice(up_book),
        "down_microprice": _microprice(down_book),
        "realized_vol_per_s": realized_vol_per_s,
        "implied_vol_per_s": implied_vol_per_s,
        "vol_ratio": vol_ratio,
        "vol_regime": vol_regime,
        "disagreement_regime": disagreement_regime,
        "session": _session(hour),
        "c1_side": ("Up" if (q_market is not None and q_market >= 0.5) else "Down" if q_market is not None else None),
        "c0_side": _c0_side(condition_id),
    }
