"""Live event reactor — paper-trades the only HL moves that beat the ~9bps fee: forced/
dislocation events (liquidations, sharp moves, OI collapse). NOT micro-momentum (proven
~1bp, sub-fee). Executable accounting only (buy ask / sell bid + fee). Honest forward test.
"""
from __future__ import annotations

from dataclasses import dataclass

TAKER_FEE = 0.00045


@dataclass(frozen=True)
class Trigger:
    kind: str          # 'liquidation' | 'dislocation' | 'cascade'
    side: str          # 'long' | 'short'  (continuation direction)
    strength: float    # signal magnitude (bps of recent move, or liq notional weight)


def classify_trade(trade: dict) -> tuple[bool, str | None]:
    """A liquidation appears in the HL trades feed as an optional `liquidation` object.
    Returns (is_liquidation, aggressor_side). A long liquidation = forced SELL -> 'short'."""
    if not isinstance(trade, dict) or "liquidation" not in trade or trade.get("liquidation") is None:
        return False, None
    # trade `side` is the taker side of the fill; a liquidation forces the victim out, so the
    # cascade pressure continues in the fill's aggressor direction.
    s = str(trade.get("side", "")).upper()
    return True, ("long" if s.startswith("B") else "short")


def detect_dislocation(ret_window_bps: float, vol_bps: float, *, k: float = 4.0,
                       min_move_bps: float = 25.0) -> Trigger | None:
    """Sharp move = |recent return| exceeds k*vol AND an absolute floor. Continuation bias
    (forced flow tends to extend before it exhausts)."""
    if vol_bps <= 0:
        return None
    if abs(ret_window_bps) < max(min_move_bps, k * vol_bps):
        return None
    return Trigger("dislocation", "long" if ret_window_bps > 0 else "short", abs(ret_window_bps))


def exit_plan(trigger: Trigger, vol_bps: float) -> dict:
    """Targets scale with the event's own move/vol — big events, big targets (unlike the
    fee-bound micro lane). TP must clear the round-trip fee with margin."""
    rt_fee_bps = 2 * 10000 * TAKER_FEE              # round-trip taker fee in bps (~9)
    base = max(trigger.strength, 3.0 * max(vol_bps, 5.0))
    tp = max(2.5 * rt_fee_bps, 0.6 * base)          # >= ~22.5bps, scales with the move
    sl = max(1.5 * rt_fee_bps, 0.5 * base)
    return {"tp_bps": round(tp, 1), "sl_bps": round(sl, 1),
            "hold_ms": 120_000 if trigger.kind == "dislocation" else 300_000}


def executable_pnl_bps(side: str, entry_bid: float, entry_ask: float,
                       exit_bid: float, exit_ask: float, fee: float = TAKER_FEE) -> float | None:
    """Round-trip taker, marked at executable prices (+ both-leg fee), in bps."""
    eb, ea, xb, xa = (_f(entry_bid), _f(entry_ask), _f(exit_bid), _f(exit_ask))
    if side == "long":
        if ea is None or xb is None or ea <= 0:
            return None
        return 10000 * ((xb - ea) / ea - 2 * fee)
    if side == "short":
        if eb is None or xa is None or eb <= 0:
            return None
        return 10000 * ((eb - xa) / eb - 2 * fee)
    return None


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
