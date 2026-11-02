"""Directional SCALPER for the Polymarket BTC 5m binary.

Acts on the live directional thesis (direction.directional_thesis): when enough signals agree that BTC is
moving, it takes the market price, rides the move, and banks the profit — "if it thinks the price is moving,
take whatever price it is, wait for the move, take the profit." Short holds (seconds), both sides.

The economics it must beat: a scalp crosses the spread (buy near ask, sell near bid), so the captured move
must exceed the round-trip spread. Realized fee is 0 on the tape, so cost ≈ the spread (~1c) — a 2c read
nets ~+1c. Whether the thesis predicts moves > spread often enough is an empirical question for FORWARD
PAPER (this is not faithfully backtestable — intra-tick exits are the sampled-book blind spot).

Pure decisions here (unit-tested); the runner does I/O + bookkeeping.
"""
from __future__ import annotations
from dataclasses import dataclass

# ENTRY: the engine no longer enters on an instantaneous read — it enters only on a CONFIRMED direction from
# direction_state.DirectionState (persistence + multi-horizon + order-flow agreement). The confirmation gate
# (conviction bar, order-flow agreement) lives there now; decide() just consumes its `confirm` dict.
# EXIT philosophy v2 — gap-immune. The binary GAPS 5-12c per 2s poll, so a tight price-stop bails into the
# gap (audit: 95% of stops recover, median +26c, ~25s later — the stop was 117 trades / -$249 of bleed).
# Exits are now PERSISTENCE-based: cut only on a move that HELD against us, not a single gappy tick.
TRAIL_SPREAD_MULT = 1.5     # lock the win once the bid gives back > this many spreads from its peak
TRAIL_FLOOR = 0.005         # ...but at least 0.5c
PATIENCE_STOP = 0.10        # an adverse move only counts once the bid is this deep against us...
PATIENCE_S = 12             # ...AND it has PERSISTED this long (a 2s gap that recovers is ignored)
FLIP_PERSIST_S = 6          # a reversal must HOLD this long to count (not a single-tick opposite read)
CATASTROPHE = 0.25          # hard cut only on a genuine collapse
EXIT_FLIP_CONVICTION = 0.15  # min conviction for an opposite read to START the flip timer (in update_exit_state)
FLIP_FIRE_CONVICTION = 0.30  # ...and the opposite read must still be THIS convicted when the timer fires —
                             # matches the exit replay that validated the design (a flip that weakened to a
                             # low-conviction wiggle by fire time is not worth banking out for)
HOLD_SECONDS = 40           # safety timeout (rides can last; the 5m window caps it anyway)
TAKE_PROFIT_PRICE = 0.90    # lock a runaway win before settlement risk
MIN_SECONDS_LEFT = 20       # don't open a scalp with less than this left
MAX_ENTRY_PRICE = 0.92      # don't scalp a near-decided side (no room to move)
MIN_ENTRY_PRICE = 0.08


@dataclass
class Position:
    side: str               # 'Up' | 'Down'
    entry_price: float
    entry_ts: int
    shares: float
    entry_prob: float | None = None
    peak_bid: float | None = None     # highest sell-bid seen since entry (for the trailing exit) — runner updates
    adverse_since: int | None = None  # ts the bid first went > PATIENCE_STOP against us (runner updates)
    flip_since: int | None = None     # ts the read first flipped against us (runner updates)


def _bid_ask(book, side):
    if side == "Up":
        return book.get("up_bid"), book.get("up_ask")
    return book.get("down_bid"), book.get("down_ask")


def update_exit_state(position, book, thesis, now_ts):
    """Maintain the gap-immune exit timers on the position each tick (the runner calls this before decide(),
    just like it updates peak_bid). adverse_since = when the bid first went > PATIENCE_STOP against us;
    flip_since = when the computed direction first flipped against us. Cleared the moment the condition lifts,
    so a transient 2s gap can't trip the persistence timers."""
    sell_bid, _ = _bid_ask(book, position.side)
    if sell_bid is not None and 0.0 < sell_bid < 1.0:
        adverse = (sell_bid - position.entry_price) <= -PATIENCE_STOP
        position.adverse_since = (position.adverse_since or now_ts) if adverse else None
    opp = (thesis.get("side") and thesis.get("side") != position.side
           and thesis.get("conviction", 0) >= EXIT_FLIP_CONVICTION)
    position.flip_since = (position.flip_since or now_ts) if opp else None


def decide(*, thesis, book, position, now_ts, seconds_left, stake_usdc=10.0, confirm=None, ev=None):
    """Scalp action for THIS tick. confirm = direction_state.DirectionState.update(...) dict (the memory/
    confirmation gate); thesis = directional_thesis(...) for display/score; book = {up/down ask/bid}."""
    if position is None:
        if seconds_left < MIN_SECONDS_LEFT:
            return {"action": "wait", "reason": "too little time left to scalp"}
        conf = confirm or {}
        side = conf.get("confirmed_side")
        if not conf.get("confirmed") or side is None:
            return {"action": "wait", "score": thesis.get("score"),
                    "reason": f"no confirmed direction (persist {conf.get('persistence_ticks', 0)}, "
                              f"horizons={conf.get('horizons_agree')}, flow={conf.get('flow_agree')})"}
        _bid, ask = _bid_ask(book, side)
        if ask is None or not (MIN_ENTRY_PRICE < ask < MAX_ENTRY_PRICE):
            return {"action": "wait", "best_side": side,
                    "reason": f"confirmed {side} but ask {'—' if ask is None else f'{ask:.2f}'} unusable"}
        if ev is not None and not ev.get("passed"):       # reachability EV gate (off when ev is None)
            _ev007 = ev.get("ev_007")
            _th = ev.get("t_hold")
            return {"action": "wait", "best_side": side, "score": thesis.get("score"),
                    "reason": f"confirmed {side} but EV gate blocks ({ev.get('reject_reason')}): "
                              f"ev={'—' if _ev007 is None else f'{_ev007:+.4f}'} "
                              f"t_hold={'—' if _th is None else f'{_th:.0f}s'}",
                    "ev": ev}
        return {"action": "enter", "side": side, "price": ask, "kind": "entry",
                "shares": round(stake_usdc / ask, 4), "score": thesis.get("score"),
                "conviction": thesis.get("conviction"), "entry_prob": thesis.get("score"),
                "persistence": conf.get("persistence_ticks"), "ev": ev,
                "reason": f"CONFIRMED {side} (held {conf.get('persistence_ticks')} ticks, horizons+flow agree); "
                          f"take @ {ask:.2f}, ride until the direction turns"}

    # holding a scalp — RIDE through micro-noise; exit only on a PERSISTENT move (gap-immune)
    side = position.side
    sell_bid, our_ask = _bid_ask(book, side)
    held = now_ts - position.entry_ts
    if sell_bid is not None and 0.0 < sell_bid < 1.0:
        gain = sell_bid - position.entry_price
        peak = position.peak_bid if position.peak_bid is not None else sell_bid
        peak_gain = peak - position.entry_price
        spread = (our_ask - sell_bid) if (our_ask is not None and our_ask > sell_bid) else None
        trail = max(TRAIL_FLOOR, TRAIL_SPREAD_MULT * spread) if spread is not None else TRAIL_FLOOR
        if sell_bid >= TAKE_PROFIT_PRICE:                                  # runaway win — lock it
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "take_profit",
                    "reason": f"near-certain — lock {side} @ {sell_bid:.2f} (+{100*gain:.1f}c)"}
        # TRAILING: rode up then gave back more than ~1.5 spreads from the peak → the move stalled, lock it
        if peak_gain > 0 and gain <= peak_gain - trail:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "take_profit",
                    "reason": f"gave back {100*(peak_gain-gain):.1f}c from +{100*peak_gain:.1f}c peak "
                              f"(> {100*trail:.1f}c) — lock {side} @ {sell_bid:.2f} ({'+' if gain >= 0 else ''}{100*gain:.1f}c)"}
        # PATIENCE stop: cut only if the adverse move PERSISTED (gap-immune — a 2s spike that recovers is ignored)
        if position.adverse_since is not None and (now_ts - position.adverse_since) >= PATIENCE_S:
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "stop",
                    "reason": f"adverse {100*gain:.1f}c held {now_ts - position.adverse_since}s — cut {side} @ {sell_bid:.2f}"}
        # PERSISTENT flip: the direction turned, STAYED turned, and is STILL convicted (not a faded wiggle)
        if (position.flip_since is not None and (now_ts - position.flip_since) >= FLIP_PERSIST_S
                and thesis.get("conviction", 0) >= FLIP_FIRE_CONVICTION):
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "reversal",
                    "reason": f"read flipped & held {now_ts - position.flip_since}s — bank {side} @ {sell_bid:.2f} "
                              f"({'+' if gain >= 0 else ''}{100*gain:.1f}c)"}
        if gain <= -CATASTROPHE:                                           # genuine collapse — hard cut
            return {"action": "exit", "side": side, "price": sell_bid, "kind": "stop",
                    "reason": f"collapse {100*gain:.1f}c (> {100*CATASTROPHE:.0f}c) — cut {side} @ {sell_bid:.2f}"}
    if held >= HOLD_SECONDS:                                               # safety timeout
        px = sell_bid if (sell_bid is not None and 0.0 < sell_bid < 1.0) else None
        return {"action": "exit", "side": side, "price": px, "kind": "timeout",
                "reason": f"held {HOLD_SECONDS}s — close {side}" + (f" @ {px:.2f}" if px else "")}
    gtxt = f"+{100*(sell_bid-position.entry_price):.1f}c" if sell_bid is not None else "no bid"
    return {"action": "hold", "reason": f"riding {side} ({gtxt}, {held}s) — direction holds"}


def settle_pnl(position, winning_outcome, stake_usdc=10.0):
    won = winning_outcome == position.side
    return {"won": won, "exit_kind": "settlement", "exit_price": (1.0 if won else 0.0),
            "pnl": round(position.shares * (1.0 if won else 0.0) - stake_usdc, 4)}


def exit_pnl(position, sell_price, stake_usdc=10.0):
    if sell_price is None:
        return {"exit_price": None, "pnl": 0.0}
    return {"exit_price": sell_price, "pnl": round(position.shares * sell_price - stake_usdc, 4)}
