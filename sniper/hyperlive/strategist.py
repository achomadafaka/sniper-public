"""Strategist — flexible-A decision core. Scans every applicable setup across all market
snapshots, computes EV-after-cost with calibrated win-rates, gates on EV margin + sample,
optionally UCB-explores, and returns the single best TradeIdea or NO-TRADE. Cannot emit a
negative-EV trade — the fee discipline is structural."""
from __future__ import annotations

import math

from sniper.hyperlive import setups as S
from sniper.hyperlive.calibration import Calibration
from sniper.hyperlive.models import Candidate, MarketSnapshot, TradeIdea
from sniper.hyperlive.regime import applicable_setups, classify

EV_MARGIN_BPS = 1.0       # required EV above zero after cost
MIN_CONFIDENCE = 0.0      # P1: allow exploration of fresh cells; bandit handles balance
UCB_C = 6.0               # exploration weight (bps-scale)


def candidates_for(snapshot: MarketSnapshot, calib: Calibration,
                   allowed_setups: set | None = None) -> list[Candidate]:
    regime = classify(snapshot)
    out = []
    cost = snapshot.spread_bps + 2 * 10000 * 0.00045  # cross spread once each leg + fee
    setups = applicable_setups(regime)
    if allowed_setups is not None:
        setups = setups & allowed_setups              # horizon-track filter
    for setup in setups:
        proposal = S.propose(setup, snapshot)
        if not proposal:
            continue
        side, target_bps, stop_bps, horizon_ms, rationale = proposal
        p = calib.p_win(setup, regime)
        ev = p * target_bps - (1 - p) * stop_bps - cost
        out.append(Candidate(
            asset=snapshot.asset, setup=setup, side=side, horizon_ms=horizon_ms,
            p_win=round(p, 3), target_bps=target_bps, stop_bps=stop_bps,
            cost_bps=round(cost, 2), ev_bps=round(ev, 2),
            confidence=round(calib.confidence(setup, regime), 3), regime=regime,
            rationale=rationale,
        ))
    return out


def _ucb_score(c: Candidate, calib: Calibration) -> float:
    n = calib.count(c.setup, c.regime)
    bonus = UCB_C * math.sqrt(1.0 / (n + 1))
    return c.ev_bps + bonus


def select(snapshots: list[MarketSnapshot], calib: Calibration,
           allowed_setups: set | None = None) -> tuple[TradeIdea | None, list[Candidate], str]:
    """Return (idea | None, ranked_candidates, no_trade_reason). allowed_setups scopes a
    horizon track (fast vs swing)."""
    cands = []
    for s in snapshots:
        if s.status == "ok":
            cands.extend(candidates_for(s, calib, allowed_setups))
    viable = [c for c in cands if c.ev_bps > EV_MARGIN_BPS]
    ranked = sorted(cands, key=lambda c: -_ucb_score(c, calib))
    if not viable:
        return None, ranked, "no candidate clears EV-after-cost margin"
    best = max(viable, key=lambda c: _ucb_score(c, calib))
    entry_hint = "cluster_approach" if best.setup == "cluster_magnet" else "immediate"
    idea = TradeIdea(
        asset=best.asset, setup=best.setup, side=best.side, horizon_ms=best.horizon_ms,
        entry_hint=entry_hint, target_bps=best.target_bps, stop_bps=best.stop_bps,
        ev_bps=best.ev_bps, confidence=best.confidence, regime=best.regime,
        thesis=f"{best.asset} {best.side} [{best.setup}/{best.regime}]: {best.rationale}; "
               f"EV {best.ev_bps:+.1f}bps (p_win {best.p_win}, tgt {best.target_bps}, stop {best.stop_bps}, cost {best.cost_bps}).",
    )
    return idea, ranked, ""
