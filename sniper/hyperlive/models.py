"""Immutable carriers for the Hyperlive engine. Paper-only; executable accounting."""
from __future__ import annotations

from dataclasses import dataclass, field

# Pipeline states (shown on the UI, exactly one active at a time).
STATES = ("SCANNING", "SELECTING", "DESIGNING", "ARMED", "MANAGING", "CLOSING", "EVALUATING")

TAKER_FEE = 0.00045
ROUND_TRIP_FEE_BPS = 2 * 10000 * TAKER_FEE  # ~9.0


@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the engine computed for one asset at one decision time."""
    asset: str
    ts_ms: int
    bid: float
    ask: float
    mid: float
    spread_bps: float
    features: dict = field(default_factory=dict)   # ofi, vol_bps, ret_*, oi_delta, funding, basis, cluster_*, catalyst
    status: str = "ok"


@dataclass(frozen=True)
class Candidate:
    """One setup's proposed trade on one asset/horizon, with its EV math."""
    asset: str
    setup: str            # 'continuation' | 'reversion' | 'cluster_magnet' | 'event_reaction' | 'cross_venue' | 'breakout'
    side: str             # 'long' | 'short'
    horizon_ms: int
    p_win: float
    target_bps: float
    stop_bps: float
    cost_bps: float
    ev_bps: float
    confidence: float     # 0..1 from sample size
    regime: str
    rationale: str = ""


@dataclass(frozen=True)
class TradeIdea:
    asset: str
    setup: str
    side: str
    horizon_ms: int
    entry_hint: str       # how ARMED→ENTER should time it (e.g. 'immediate' | 'cluster_approach')
    target_bps: float
    stop_bps: float
    ev_bps: float
    confidence: float
    regime: str
    thesis: str


@dataclass(frozen=True)
class Position:
    asset: str
    side: str
    entry_bid: float
    entry_ask: float
    entry_ts_ms: int
    target_bps: float
    stop_bps: float
    horizon_ms: int
    setup: str


@dataclass(frozen=True)
class CycleRecord:
    cycle_id: int
    selected_asset: str | None
    idea: TradeIdea | None
    decision: str          # 'TRADE' | 'NO_TRADE'
    no_trade_reason: str = ""
