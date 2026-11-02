"""Maker-entry execution for the crypto5m SCALPER (engine maker-entry-v1).

The forward campaign's verdict: every TAKER rule pays ~19.5% ROI of cost (C0 random baseline) and no
signal in the stack recovers it. The scalper's read (confirmed direction + EV gate) stays; this module
swaps the execution: instead of paying the ask the moment a direction confirms, POST a resting paper bid
on that side and let the market trade INTO us — entering at bid-side price (capturing ~the spread) or
not entering at all. The fill-vs-miss asymmetry (fills cluster when the move fails — adverse selection)
is exactly what the paper campaign measures; do not assume the savings survive it.

Fills are print-verified like the maker runner: a resting bid fills only when a trade PRINTS at/below it
on the tape; the book merely quoting through is not a fill. Pure decisions here (unit-tested); the runner
does I/O and bookkeeping.
"""
from __future__ import annotations

from dataclasses import dataclass

TICK = 0.01
QUOTE_TTL_S = 12          # a quote that hasn't filled in this long is stale — the move left without us
MAX_REPRICES = 3          # chase the top of book at most this many times (then rest where we are)
MIN_SECONDS_LEFT = 20     # mirror scalp_trader.MIN_SECONDS_LEFT — don't quote into the close
MAX_ENTRY_PRICE = 0.92    # mirror scalp_trader.MAX_ENTRY_PRICE — no room to move above this


@dataclass
class PendingQuote:
    side: str            # 'Up' | 'Down'
    price: float         # our resting bid on that side's token
    posted_ts: int
    decision_ask: float | None   # the touch at decision time — the taker counterfactual for fill_slip
    reprices: int = 0


def _touch(book, side):
    if side == "Up":
        return book.get("up_bid"), book.get("up_ask")
    return book.get("down_bid"), book.get("down_ask")


def desired_entry_bid(side, book, max_entry=MAX_ENTRY_PRICE):
    """Where to rest our entry bid for `side`: join-or-improve the best bid by one tick, never cross the
    ask (that would be a taker), never above max_entry. None when the book gives nothing to quote against."""
    bid, ask = _touch(book, side)
    if bid is None and ask is None:
        return None
    px = (bid + TICK) if bid is not None else (ask - TICK)
    if ask is not None:
        px = min(px, ask - TICK)
    px = min(px, max_entry)
    px = round(px, 4)
    return px if px > 0 else None


def update_quote(pq: PendingQuote, *, book, confirm, now_ts, seconds_left, prints=None):
    """One tick of quote management. Returns an action dict:
      {'action': 'fill', 'price', 'fill_model', 'slip'}   — we were traded into; open the position
      {'action': 'cancel', 'reason'}                      — pull the quote, no trade
      {'action': 'reprice', 'price'}                      — lost the top of book; join it again (capped)
      {'action': 'rest'}                                  — leave the quote where it is
    prints = tape trades since last tick in THIS side's price space [(price, size), ...];
    None = tape unreachable -> NO fill this tick (print-only contract; no book-through fail-open)."""
    conf = confirm or {}
    if seconds_left is not None and seconds_left < MIN_SECONDS_LEFT:
        return {"action": "cancel", "reason": "window closing — pull the entry quote"}
    if not conf.get("confirmed") or conf.get("confirmed_side") != pq.side:
        return {"action": "cancel", "reason": "read no longer confirmed — pull the entry quote"}
    if now_ts - pq.posted_ts >= QUOTE_TTL_S:
        return {"action": "cancel", "reason": f"quote stale ({QUOTE_TTL_S}s unfilled) — the move left without us"}

    bid, ask = _touch(book, pq.side)
    # fill check on the PREVIOUSLY-resting price. PRINT-ONLY: a fill requires a trade on the tape at/through
    # our price. prints=None (tape unreachable) means NO fill — never fail-open to book-through, which counts
    # the book merely QUOTING at our price as a trade (codex review 2026-07-04: weak contract).
    if prints is not None:
        hit = sum(sz for px, sz in prints if px <= pq.price + 1e-9)
        if hit > 0:
            return {"action": "fill", "price": pq.price, "fill_model": "print",
                    "slip": (round(pq.price - pq.decision_ask, 4) if pq.decision_ask is not None else None)}

    # lost the top of book while the read still holds -> join the new best bid (bounded chase)
    if bid is not None and bid > pq.price + 1e-9 and pq.reprices < MAX_REPRICES:
        new_px = desired_entry_bid(pq.side, book)
        # never chase past what the taker would have paid at decision time — at that point maker adds nothing
        if new_px is not None and pq.decision_ask is not None:
            new_px = min(new_px, round(pq.decision_ask - TICK, 4))
        if new_px is not None and new_px > pq.price + 1e-9:
            return {"action": "reprice", "price": new_px}
    return {"action": "rest"}
