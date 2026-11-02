"""Directional thesis engine for the Polymarket BTC 5m binary — the engine's live read of where BTC is
headed in the NEXT FEW SECONDS, fused from every short-horizon signal we can compute.

This is NOT the 5-minute outcome forecast (that horizon is efficient — proven). It's a seconds-scale
microstructure read used by a SCALPER: when enough independent signals agree on a direction, the engine
takes the market price, rides the move, and banks the profit. Pure compute — unit-tested, no I/O.

ADAPTIVE thresholds (not hardcoded bps): a move only counts as "direction" if it is large RELATIVE to the
current noise. Returns are scored as z = return ÷ (σ·√horizon) — a 1σ move, not a fixed 3bps. The microprice
gap is scored relative to the SPREAD (the book's natural wobble). This way the read tightens in calm markets
(won't call a direction from noise) and loosens when BTC is actually moving. σ and the spread are passed in;
if absent it falls back to the absolute floors below.

Each signal casts a vote in {-1, 0, +1}; the thesis fuses them into score / agreement / conviction / side,
and returns each signal's z-score (for display + tuning).
"""
from __future__ import annotations
import math

# z-thresholds (in units of measured noise) — a signal votes when |z| clears these
RET_Z = 1.0              # a return counts as a move only if it's >= 1σ over its horizon
EMA_Z = 0.6             # EMA slope vs its ~15s vol scale
MICRO_Z = 0.5           # microprice gap >= half the spread (clear book pressure)
IMB_EPS = 0.15          # top-of-book size imbalance (already a ratio, no vol scaling)
VOL_DIR_EPS = 0.10      # signed-volume share away from 0.5
OFI_EPS = 0.15          # signed trade-tape order-flow imbalance (buy−sell)/total, already a ratio
# absolute fallbacks when σ / spread are unavailable (degrade gracefully, never crash)
RET_EPS = 0.00003
EMA_EPS = 0.00002
MICRO_EPS = 0.004

# weights REALIGNED to data-driven predictiveness (crypto5m_forecast_fit.py, n=2263): OOS corr with the next-8s
# book move is momentum-15s +0.21 (strongest) > imbalance +0.13 > microprice +0.11. The old hand-set weights
# over-weighted microprice (was 1.2, the highest, despite being the WEAKEST). A fitted model didn't beat equal
# weights, so this is a moderate realignment toward predictiveness, not an overfit.
WEIGHTS = {
    "mom_5s": 0.7, "mom_15s": 1.3, "mom_30s": 1.1,     # 15s is the workhorse; 5s too noisy, 30s too slow
    "ema_slope": 1.1,                                  # trend, ~momentum strength
    "microprice": 0.9,                                 # weakest predictor — down-weighted from 1.2
    "imbalance": 1.0,                                  # top-of-book size imbalance
    "order_flow": 1.4,                                 # signed TRADE-TAPE flow — the STRONGEST predictor in the
                                                       # walk-forward (finding-crypto5m-walkforward-tape-model);
                                                       # the engine previously ignored it. Highest weight.
    "volume_dir": 0.6,                                 # kline taker-buy share (coarser proxy than the tape)
    "basis": 1.0,                                      # Binance↔Chainlink settlement lead — DELIBERATELY None:
                                                       # tested (n=2652) only +0.09 incremental beyond momentum
                                                       # (Chainlink tracks Binance 0.73) + live ~15s stale. Not wired.
}


def _ret_z(ret, vol_per_s, horizon_s):
    """z-score of an h-second return vs its expected stdev σ·√h. Falls back to abs/RET_EPS without σ."""
    if ret is None:
        return None
    if vol_per_s and vol_per_s > 0:
        return ret / (vol_per_s * math.sqrt(horizon_s))
    return ret / RET_EPS                               # fallback: scale to the absolute floor


def _vote(z, thresh):
    if z is None:
        return None
    return 1 if z >= thresh else (-1 if z <= -thresh else 0)


def directional_thesis(*, ret_5s=None, ret_15s=None, ret_30s=None, ema_slope=None,
                       microprice_gap=None, imbalance=None, volume_dir=None,
                       vol_per_s=None, spread=None, basis_dir=None, order_flow=None):
    """Fuse live signals into a directional read, with thresholds adaptive to measured noise.
    vol_per_s = BTC realized per-second vol (scales the return signals). spread = current Up-book spread
    (scales the microprice signal). Returns side/score/agreement/conviction + per-signal votes and z-scores."""
    zr5, zr15, zr30 = _ret_z(ret_5s, vol_per_s, 5), _ret_z(ret_15s, vol_per_s, 15), _ret_z(ret_30s, vol_per_s, 30)
    z_ema = (ema_slope / (vol_per_s * math.sqrt(15)) if (ema_slope is not None and vol_per_s)
             else (ema_slope / EMA_EPS if ema_slope is not None else None))
    z_micro = (microprice_gap / spread if (microprice_gap is not None and spread and spread > 0)
               else (microprice_gap / MICRO_EPS if microprice_gap is not None else None))
    zscores = {"mom_5s": zr5, "mom_15s": zr15, "mom_30s": zr30, "ema_slope": z_ema, "microprice": z_micro}
    votes = {
        "mom_5s": _vote(zr5, RET_Z), "mom_15s": _vote(zr15, RET_Z), "mom_30s": _vote(zr30, RET_Z),
        "ema_slope": _vote(z_ema, EMA_Z), "microprice": _vote(z_micro, MICRO_Z),
        "imbalance": (None if imbalance is None else (1 if imbalance >= IMB_EPS else (-1 if imbalance <= -IMB_EPS else 0))),
        "order_flow": (None if order_flow is None else (1 if order_flow >= OFI_EPS else (-1 if order_flow <= -OFI_EPS else 0))),
        "volume_dir": (None if volume_dir is None else (1 if volume_dir >= VOL_DIR_EPS else (-1 if volume_dir <= -VOL_DIR_EPS else 0))),
        "basis": (basis_dir if basis_dir in (-1, 0, 1) else None),
    }
    active = {k: v for k, v in votes.items() if v is not None}
    nonzero = {k: v for k, v in active.items() if v != 0}
    if not nonzero:
        return {"score": 0.0, "agreement": 0.0, "conviction": 0.0, "side": None,
                "votes": votes, "zscores": zscores, "n_active": len(active), "n_agree": 0}
    wsum = sum(WEIGHTS[k] for k in active)
    score = sum(WEIGHTS[k] * v for k, v in active.items()) / wsum if wsum else 0.0
    s = 1 if score > 0 else (-1 if score < 0 else 0)
    n_agree = sum(1 for v in nonzero.values() if v == s)
    agreement = n_agree / len(nonzero)
    conviction = abs(score) * agreement
    side = "Up" if s > 0 else ("Down" if s < 0 else None)
    return {"score": round(score, 4), "agreement": round(agreement, 3), "conviction": round(conviction, 4),
            "side": side, "votes": votes, "zscores": {k: (round(v, 2) if v is not None else None) for k, v in zscores.items()},
            "n_active": len(active), "n_agree": n_agree}
