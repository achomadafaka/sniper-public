"""Continuous active paper-trading decision engine for the Polymarket BTC Up/Down 5-minute binary.

It thinks every few seconds across the WHOLE window (not a fixed checkpoint): given our probability P(Up)
(where BTC already is, projected to close) and the LIVE book on both sides, it ENTERS dynamically the
moment a side is underpriced vs our probability — at second 1, second 4:59, or never — and then MANAGES the
position: take profit by selling into a good bid, or cut when the edge reverses, otherwise hold to
settlement. Both sides. Pure decisions here (unit-tested); the runner does the I/O and paper bookkeeping.

This is data-collection, not a profit claim: whether OUR probability beats the market's price is exactly
what running it reveals.
"""
from __future__ import annotations

from dataclasses import dataclass

# --- policy knobs (tunable; the engine is meant to be ACTIVE, not frozen) -----------------------
ENTRY_EDGE = 0.04            # enter a side when our prob beats (ask + entry fee) by this much
TAKE_PROFIT_PRICE = 0.90     # if we can SELL the held side at >= this bid, lock the gain
TAKE_PROFIT_MULT = 1.35      # ...or if the sell bid is >= this multiple of our entry price
STOP_EDGE = -0.08           # if our edge on the held side falls below this, cut the loss. REVERTED from a
                            # tightened -0.05: the backtest favored -0.05 but LIVE it whipsaw-stopped positions
                            # 5-20s after entry (-$152 in stops) — the sampled capture hid the intra-tick noise.
MIN_SECONDS_LEFT_ENTRY = 8  # don't open a new position with less than this left (can't manage it)
TAKER_FEE_RATE = 0.07
# smarter-exit knobs (finding-backed: Binance leads the book ~1s, so our fair value leads the price) ---
CONVERGENCE_TAKE_MARGIN = 0.02   # take profit once the BOOK has caught up to our entry fair value (edge
                                 # realized) — bank it instead of riding resolution variance
FAIR_REVERSAL_STOP = 0.12        # cut once OUR fair value for the held side falls this far below our entry
                                 # conviction (BTC reversed). REVERTED from 0.08 (whipsaw — see STOP_EDGE)
FRESH_PROB_EPS = 0.005           # our prob must have moved at least this much toward a side to call the
                                 # divergence "fresh" (book likely still stale) vs a stale standing gap
# direction-AGREEMENT gate (soft): the driftless fair value ignores trend; this avoids fighting a strong
# move. Data (n~28 windows) mildly supports it — used as a bar-raise, not a hard veto except on extremes.
MOMENTUM_ADVERSE_BPS = 12.0      # BTC moving this many bps AGAINST the side raises the entry bar
MOMENTUM_PENALTY = 0.10          # ...by this much (demand a bigger mispricing to fight the trend). Raised from
                                 # 0.03 — replay showed one-adverse entries lose; a stiffer bar nets more (R3)
MOMENTUM_VETO_BPS = 40.0         # ...and beyond this (a real falling knife) we skip the entry entirely
MOMENTUM_MIN_AGREEMENT = 0.66    # only trust the trend (apply the gate) when >=2/3 of the time scales agree;
                                 # an incoherent trend (scales disagree) is noise — don't gate on it
# order-flow (book imbalance) — the SECOND directional signal. R3 (momentum AND imbalance agreeing) is the
# only forward rule that beat the random baseline, so the gate uses BOTH: it takes both to veto or to lean in.
IMBALANCE_THRESHOLD = 0.20       # |top-of-book imbalance| above this counts as an order-flow direction (R2)
MOMENTUM_LEAN = 0.015            # when BOTH signals favor the side, LOWER the entry bar by this (lean into it)
MIN_ENTRY_BAR = 0.02             # ...but never below this floor (keep a cost buffer even with full conviction)
# --- disciplined R3-bound entry (v2): bind to the winning forward rule (momentum AND order-flow agree),
#     enter at a sane price, HOLD TO SETTLEMENT (no whipsaw stops). ----------------------------------------
CONVICTION_MOM_BPS = 0.5         # momentum "favors" a side when its signed trend exceeds this (and is coherent).
                                 # R3's momentum rule (R1) is just the SIGN (threshold 0.0); 1bp avoids acting
                                 # on dead-flat noise while staying close to the winning rule.
MAX_ENTRY_PRICE = 0.90          # don't buy a side already near-certain — no payoff left, only downside
PRICE_SLACK = 0.05             # don't overpay: skip if the ask is worse than our fair value by more than this
ENTER_WHEN_SECONDS_LEFT_BELOW = 90  # only enter LATE, when the direction has firmed up — the winning forward
                                    # rule R3 enters at 240s (60s left); entering early rides reversals


@dataclass
class Position:
    side: str               # 'Up' or 'Down'
    entry_price: float
    entry_ts: int
    shares: float
    entry_prob: float | None = None   # OUR fair value for the side at entry — powers convergence/reversal exits


def _fee(price):
    return TAKER_FEE_RATE * price * (1.0 - price)


def _entry_edge(prob_side, ask):
    """Our probability for a side minus the cost to buy it (ask + taker fee). +ve = underpriced."""
    if prob_side is None or ask is None or not (0.0 < ask < 1.0):
        return None
    return prob_side - ask - _fee(ask)


def decide(*, prob_up, book, position, seconds_left, now_ts, stake_usdc=10.0, prob_up_prev=None,
           momentum_bps=None, momentum_agreement=None, book_imbalance=None):
    """Return an action dict for THIS tick.
    book = {up_ask, up_bid, down_ask, down_bid} (any may be None).
    position = Position or None.   prob_up_prev = our P(Up) on the previous tick (for entry FRESHNESS).
    momentum_bps = signed BTC trend (+ve up), horizon scaled to time-left. momentum_agreement = cross-scale
    coherence. book_imbalance = signed top-of-book size imbalance for the UP token (+ve = order-flow leans Up).
    The CONVICTION gate uses momentum AND order-flow together (R3): both confirm to veto/lean, one to nudge.
    action ∈ {'enter','exit','hold','wait'}; for enter/exit also: side, price, kind, reason, (shares)."""
    up_ask, up_bid = book.get("up_ask"), book.get("up_bid")
    down_ask, down_bid = book.get("down_ask"), book.get("down_bid")
    prob_down = (1.0 - prob_up) if prob_up is not None else None
    trend_coherent = momentum_agreement is None or momentum_agreement >= MOMENTUM_MIN_AGREEMENT
    mom_up = momentum_bps if (momentum_bps is not None and trend_coherent) else None   # +ve favors Up

    def _conv(side):
        """(favors, against): do momentum AND order-flow BOTH point the same way for this side? (R3)."""
        m = None if mom_up is None else (mom_up if side == "Up" else -mom_up)
        i = None if book_imbalance is None else (book_imbalance if side == "Up" else -book_imbalance)
        favors = (m is not None and m >= CONVICTION_MOM_BPS) and (i is not None and i >= IMBALANCE_THRESHOLD)
        against = (m is not None and m <= -CONVICTION_MOM_BPS) and (i is not None and i <= -IMBALANCE_THRESHOLD)
        return favors, against

    if position is None:
        if seconds_left < MIN_SECONDS_LEFT_ENTRY:
            return {"action": "wait", "reason": "too little time left to open and settle"}
        if seconds_left > ENTER_WHEN_SECONDS_LEFT_BELOW:
            return {"action": "wait", "reason": f"too early — wait until <{ENTER_WHEN_SECONDS_LEFT_BELOW}s left "
                    f"for the direction to firm up (R3 enters late)"}
        # BIND TO R3: only trade the side momentum AND order-flow agree on (the one forward rule that beat random)
        conv_side = "Up" if _conv("Up")[0] else ("Down" if _conv("Down")[0] else None)
        if conv_side is None:
            return {"action": "wait", "reason": "no dual conviction — need momentum AND order-flow to agree"}
        ask = up_ask if conv_side == "Up" else down_ask
        if ask is None or not (0.0 < ask < MAX_ENTRY_PRICE):
            return {"action": "wait", "best_side": conv_side,
                    "reason": f"conviction {conv_side} but ask {'—' if ask is None else f'{ask:.2f}'} "
                              f"unusable (empty or >{MAX_ENTRY_PRICE:.0%}, no payoff)"}
        # buy the conviction side at market (like R3 — no fair-value price filter, which biased toward
        # favorites and skipped the cheap underdog-reversal plays where R3's payoff comes from).
        fair_side = prob_up if conv_side == "Up" else prob_down
        return {"action": "enter", "side": conv_side, "price": ask, "kind": "entry",
                "shares": round(stake_usdc / ask, 4),
                "edge": (round(_entry_edge(fair_side, ask), 4) if fair_side is not None else None),
                "entry_prob": (round(fair_side, 4) if fair_side is not None else None),
                "momentum_bps": momentum_bps, "book_imbalance": book_imbalance,
                "reason": f"R3 conviction — momentum & order-flow BOTH favor {conv_side}; buy @ {ask:.2f}, "
                          f"hold to settlement"}

    # holding: HOLD TO SETTLEMENT (no whipsaw stops). Lock big wins; exit early only if the conviction FLIPS.
    side = position.side
    sell_bid = up_bid if side == "Up" else down_bid
    if sell_bid is not None and 0.0 < sell_bid < 1.0:
        # take-profit: near-certain win — lock it before settlement risk (don't give back an ~$8 winner).
        # Otherwise PURE HOLD TO SETTLEMENT — no stop, no conviction-flip exit (both whipsaw and cut winners);
        # R3 holds its directional bet to the close and lets the payoff distribution play out.
        if sell_bid >= TAKE_PROFIT_PRICE:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "take_profit",
                    "reason": f"near-certain — lock {side} @ {sell_bid:.2f}"}
    return {"action": "hold", "reason": (f"holding {side} to settlement — conviction intact"
            + (f", mark {sell_bid:.2f}" if sell_bid is not None else ", no bid"))}


def settle_pnl(position: Position, winning_outcome: str, stake_usdc=10.0) -> dict:
    """PnL if a held position runs to settlement (binary pays 1 if our side won, else 0)."""
    won = winning_outcome == position.side
    payout = position.shares * (1.0 if won else 0.0)
    return {"won": won, "exit_kind": "settlement", "exit_price": (1.0 if won else 0.0),
            "pnl": round(payout - stake_usdc, 4)}


def exit_pnl(position: Position, sell_price: float, stake_usdc=10.0) -> dict:
    """PnL when we SELL the position into the book at sell_price (before settlement)."""
    return {"exit_price": sell_price, "pnl": round(position.shares * sell_price - stake_usdc, 4)}
