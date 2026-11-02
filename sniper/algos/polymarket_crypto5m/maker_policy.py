"""Quoting policy for a Binance-reactive MAKER on the Polymarket BTC 5m binary.

Why a maker: the verified edge is that Binance LEADS the Polymarket book ~1s (and settlement ~2s).
As a taker the 1s mispricing (~1.3c) is smaller than the spread+fee cost (~2.25c) so it's dead; as a
maker we EARN the spread and use the Binance lead to keep our resting quotes ahead of where the book is
about to go (centering on the book mid instead LOSES — adverse selection). See
finding-crypto5m-binance-leads-book-maker-edge. n=25, sign solid / magnitude optimistic. PAPER only.

This module is PURE compute (no network, no orders) so it is unit-tested deterministically. One call per
token: pass fair_up for the UP token and (1-fair_up) for the DOWN token — the binary is symmetric.

Native flexibility (the asks in the user's request, all falling out of continuous re-quoting):
  - RE-ENTRY:        the engine re-quotes every tick, so it re-enters a window whenever an edge reappears.
  - PARTIAL EXITS:   inventory skew posts asks that scale us out gradually as we get long (not all-or-none).
  - CUT LOSSES:      when the Binance-led fair value moves through our position, the skewed quotes flip to
                     unwind it; `should_unwind()` flags a hard cut when fair crosses far against us.
"""
from __future__ import annotations

from dataclasses import dataclass

TICK = 0.01


@dataclass(frozen=True)
class Quote:
    side: str      # "bid" (we buy this token) or "ask" (we sell this token)
    price: float
    size: float


def _round_tick(x: float) -> float:
    return round(round(x / TICK) * TICK, 4)


def desired_quotes(*, fair, best_bid, best_ask, inventory, max_inventory,
                   seconds_left, base_margin=0.02, base_size=50.0,
                   inv_skew=0.5, min_seconds_quote=12, band_lo=0.05, band_hi=0.95,
                   min_quote_edge=None, min_bid_depth=0.0):
    """Desired (bid, ask) for ONE outcome token of the 5m binary. LONG-ONLY per token.

    fair          : our Binance-led fair value for THIS token's outcome (0..1). The alpha — leads the book.
    best_bid/ask  : current top of THIS token's book (None if unknown).
    inventory     : shares we HOLD of this token (>=0). Drives skew, bid taper, and the ask size.
    max_inventory : per-token cap.
    seconds_left  : time to settlement; we PULL quotes in the final seconds (near-settlement fills at the
                    extremes are max adverse selection — 12s, widened from 4s).
    base_margin   : half-width of our quote around the (inventory-skewed) center.
    inv_skew      : how hard inventory pulls the center (0=no inventory management, 1=aggressive unwind).
    band_lo/hi    : no quotes at all when fair is outside this band — a near-certain token has ~no payoff
                    left on one side and unlimited downside on a late flip.
    min_quote_edge: minimum distance from fair for both bid and ask. Defaults to base_margin. Raising this
                    is the main anti-pickoff knob when the fair estimate is noisy or the quote loop is stale.

    Returns {"bid": Quote|None, "ask": Quote|None, "reason": str}. A None side = don't quote it.
    Guarantees: never bid above fair, never ask below fair (no knowingly -EV quote); quotes stay in (0,1),
    top-of-book competitive; the ASK exists only against HELD inventory and never exceeds it — no naked
    synthetic shorts (selling tokens we don't hold is impossible on Polymarket without minting a set;
    negative-inventory fills also corrupted vwap/cut-loss in the first campaign — codex review 2026-07-04).
    Two-sidedness on the BINARY comes from bidding both tokens (buying Down IS selling Up).
    """
    if fair is None or not (0.0 < fair < 1.0):
        return {"bid": None, "ask": None, "reason": "no fair value"}
    if seconds_left is not None and seconds_left < min_seconds_quote:
        return {"bid": None, "ask": None, "reason": f"settling (<{min_seconds_quote}s) — pulled"}
    if not (band_lo <= fair <= band_hi):
        return {"bid": None, "ask": None,
                "reason": f"fair {fair:.2f} outside quote band [{band_lo:.2f}, {band_hi:.2f}]"}

    inv = max(0.0, float(inventory or 0.0))              # long-only: negative inventory cannot exist
    cap = max(1.0, float(max_inventory))
    inv_frac = min(1.0, inv / cap)                       # 0 (flat) .. 1 (max long)
    # skew the center against our inventory: long -> shift center DOWN (more eager to sell, less to buy)
    center = fair - inv_skew * inv_frac * base_margin

    raw_bid = center - base_margin
    raw_ask = center + base_margin

    # hard no-loss guards: buy below fair, sell above fair, with an optional edge floor
    edge_floor = max(TICK, float(min_quote_edge if min_quote_edge is not None else base_margin))
    raw_bid = min(raw_bid, fair - edge_floor)
    raw_ask = max(raw_ask, fair + edge_floor)

    bid = ask = None
    bid_reason = ask_reason = ""

    # ---- BID (we buy this token) — only if we have room to get longer ----
    if inv_frac < 0.999:
        b = _round_tick(raw_bid)
        if best_ask is not None:
            b = min(b, _round_tick(best_ask - TICK))     # cannot cross the ask
        if min_bid_depth and min_bid_depth > 0:
            # DEEP-ONLY (maker-deep-only-v1): never lead the book. The near-touch/top-of-book bids are the
            # proven profit-eater (adverse selection); only rest a bid at least min_bid_depth below the touch,
            # where the edge lives (stale-seller capture). Too-shallow -> no bid this tick.
            if best_bid is not None and (best_bid - b) < min_bid_depth - 1e-9:
                b = 0.0                                   # skip: within min_bid_depth of the touch
                bid_reason = f"deep-only: bid within {min_bid_depth:.2f} of touch — skipped"
            else:
                bid_reason = "deep-only resting bid"
        elif best_bid is not None and raw_bid >= best_bid - 1e-9:
            b = max(b, best_bid)                          # improve to / sit at top of book to actually fill
            bid_reason = "top-of-book bid (we lead the book)"
        else:
            bid_reason = "resting bid below book"
        if 0 < b < fair:                                  # valid, still below fair
            size = base_size * (1.0 - inv_frac)           # post less as we approach the long cap
            if size > 0:
                bid = Quote("bid", round(b, 4), round(size, 2))

    # ---- ASK (we SELL what we HOLD — scale-out only, never a naked short) ----
    if inv > 0:
        a = _round_tick(raw_ask)
        if best_bid is not None:
            a = max(a, _round_tick(best_bid + TICK))      # cannot cross the bid
        if best_ask is not None and raw_ask <= best_ask + 1e-9:
            a = min(a, best_ask)                          # improve to / sit at top of book
            ask_reason = "top-of-book ask"
        else:
            ask_reason = "resting ask above book"
        if fair < a < 1:                                  # valid, still above fair
            size = min(inv, base_size)                    # never offer more than we hold
            if size > 0:
                ask = Quote("ask", round(a, 4), round(size, 2))

    reason = "; ".join(r for r in (bid_reason if bid else "", ask_reason if ask else "") if r) or "no valid quote"
    return {"bid": bid, "ask": ask, "reason": reason}


def should_unwind(*, fair, inventory, entry_price, hard_cut=0.10):
    """Hard cut-loss: if the Binance-led fair value has moved `hard_cut` THROUGH our average entry against
    us, stop providing liquidity on the losing side and unwind. Returns (unwind: bool, reason).
    Long and fair collapsed below entry-hard_cut, or short and fair spiked above entry+hard_cut."""
    if not inventory or entry_price is None or fair is None:
        return (False, "")
    if inventory > 0 and fair < entry_price - hard_cut:
        return (True, f"long but fair {fair:.2f} fell {hard_cut:.0%} through entry {entry_price:.2f} — cut")
    if inventory < 0 and fair > entry_price + hard_cut:
        return (True, f"short but fair {fair:.2f} rose {hard_cut:.0%} through entry {entry_price:.2f} — cut")
    return (False, "")
