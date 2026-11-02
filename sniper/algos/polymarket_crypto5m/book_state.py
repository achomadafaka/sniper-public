"""Deterministic in-memory order book for a single Polymarket token.

Correctness rules (empirically motivated — see the spec's book-ordering trap):
  * never trust array position — keep a ``price -> size`` map and compute
    ``best_bid = max(prices)`` / ``best_ask = min(prices)``;
  * one ``BookState`` per token id — the opposite token's price is NEVER inferred by complement
    arithmetic (``1 - p``); each side has its own captured book.

Accepts both CLOB REST/WS level shapes: ``{"price": "..", "size": ".."}`` dicts and ``[price, size]``
pairs. A level with size 0 is a removal.
"""
from __future__ import annotations

from typing import Optional


def _level(item):
    """Normalize a level (dict or pair) to ``(price: float, size: float)``."""
    if isinstance(item, dict):
        return float(item["price"]), float(item["size"])
    return float(item[0]), float(item[1])


class BookState:
    def __init__(self, *, token_id: str, condition_id: str, side: Optional[str] = None):
        self.token_id = token_id
        self.condition_id = condition_id
        self.side = side
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.tick_size: Optional[float] = None
        self.min_order_size: Optional[float] = None
        self.book_hash: Optional[str] = None
        self.last_trade_price: Optional[float] = None
        self.last_trade_fee_rate_bps: Optional[float] = None
        self.exchange_ts: Optional[int] = None
        self.recv_ts: Optional[int] = None
        self.reconnect_generation: int = 0

    # --- mutations ---------------------------------------------------------------------------

    def apply_book(self, *, bids, asks, book_hash=None, tick_size=None, min_order_size=None,
                   exchange_ts=None, recv_ts=None) -> None:
        """Full snapshot — replaces the book entirely (order of the input lists is irrelevant)."""
        self.bids = {p: s for p, s in map(_level, bids or []) if s > 0}
        self.asks = {p: s for p, s in map(_level, asks or []) if s > 0}
        if book_hash is not None:
            self.book_hash = book_hash
        if tick_size is not None:
            self.tick_size = float(tick_size)
        if min_order_size is not None:
            self.min_order_size = float(min_order_size)
        self.exchange_ts, self.recv_ts = exchange_ts, recv_ts

    def apply_price_change(self, price_changes, *, exchange_ts=None, recv_ts=None) -> None:
        """Incremental level updates. ``side`` BUY -> bids, SELL -> asks. size 0 removes the level."""
        for ch in price_changes or []:
            price = float(ch["price"])
            size = float(ch["size"])
            book = self.bids if str(ch.get("side", "")).upper() in ("BUY", "BID") else self.asks
            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size
        # the stored hash is the REST/WS *snapshot* hash; it is no longer valid once levels mutate
        self.book_hash = None
        if exchange_ts is not None:
            self.exchange_ts = exchange_ts
        if recv_ts is not None:
            self.recv_ts = recv_ts

    def apply_best_bid_ask(self, *, best_bid=None, best_ask=None, spread=None) -> None:
        # top-of-book hint; the authoritative book remains the level map. Stored for cross-check only.
        self._hint_best_bid = None if best_bid is None else float(best_bid)
        self._hint_best_ask = None if best_ask is None else float(best_ask)
        self._hint_spread = None if spread is None else float(spread)

    def apply_last_trade_price(self, *, price, side=None, size=None, fee_rate_bps=None) -> None:
        self.last_trade_price = float(price)
        if fee_rate_bps is not None:
            self.last_trade_fee_rate_bps = float(fee_rate_bps)

    def apply_tick_size_change(self, *, old=None, new) -> None:
        self.tick_size = float(new)

    # --- reads -------------------------------------------------------------------------------

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        return (ba - bb) if (bb is not None and ba is not None) else None

    def taker_buy_fill(self, *, usdc: float) -> dict:
        """Walk asks ascending to estimate a $``usdc`` FOK-style taker buy of this token.

        Returns filled_usdc, shares, avg_price, fully_filled. (Phase-0 depth metric; real fee/slippage
        modeling is the taker-sim phase.)
        """
        remaining = float(usdc)
        shares = 0.0
        spent = 0.0
        for price in sorted(self.asks):
            if remaining <= 0:
                break
            level_usdc = self.asks[price] * price
            take = min(level_usdc, remaining)
            shares += take / price
            spent += take
            remaining -= take
        avg = (spent / shares) if shares > 0 else None
        return {"filled_usdc": spent, "shares": shares, "avg_price": avg,
                "fully_filled": remaining <= 1e-12}

    def snapshot_levels(self) -> dict:
        """Persistence-ready depth: bids high->low, asks low->high (sorted here, not trusted on input)."""
        return {
            "bids": [[p, self.bids[p]] for p in sorted(self.bids, reverse=True)],
            "asks": [[p, self.asks[p]] for p in sorted(self.asks)],
        }
