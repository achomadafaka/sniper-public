"""Normalized Hyperliquid per-asset book state and conservative taker fills."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FillEstimate:
    fillable: bool
    side: str
    notional_usdc: float
    quantity: float | None
    avg_price: float | None
    worst_price: float | None
    levels_crossed: int
    reason: str = "ok"


def _float(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class BookState:
    def __init__(self, asset: str, bids: dict[float, float], asks: dict[float, float],
                 *, exchange_ts_ms: int | None, receive_ts_ms: int):
        self.asset = asset
        self.bids = {float(p): float(s) for p, s in bids.items() if float(p) > 0 and float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks.items() if float(p) > 0 and float(s) > 0}
        self.exchange_ts_ms = exchange_ts_ms
        self.receive_ts_ms = int(receive_ts_ms)

    @classmethod
    def from_l2(cls, asset: str, levels, *, exchange_ts_ms: int | None, receive_ts_ms: int) -> "BookState":
        bids_raw, asks_raw = (levels or [[], []])[:2]
        bids: dict[float, float] = {}
        asks: dict[float, float] = {}
        for level in bids_raw or []:
            px, sz = _float(level.get("px")), _float(level.get("sz"))
            if px > 0 and sz > 0:
                bids[px] = bids.get(px, 0.0) + sz
        for level in asks_raw or []:
            px, sz = _float(level.get("px")), _float(level.get("sz"))
            if px > 0 and sz > 0:
                asks[px] = asks.get(px, 0.0) + sz
        return cls(asset, bids, asks, exchange_ts_ms=exchange_ts_ms, receive_ts_ms=receive_ts_ms)

    @property
    def best_bid(self) -> float | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return min(self.asks) if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float | None:
        if self.mid in (None, 0) or self.spread is None:
            return None
        return self.spread / self.mid * 10000.0

    def depth_notional(self, side: str, *, levels: int = 5) -> float:
        ladder = self.bids if side == "bid" else self.asks
        prices = sorted(ladder, reverse=(side == "bid"))[:levels]
        return sum(px * ladder[px] for px in prices)

    def taker_fill(self, side: str, *, notional_usdc: float) -> FillEstimate:
        """Fill a target notional. ``side='long'`` buys asks; ``side='short'`` sells bids."""
        if notional_usdc <= 0:
            return FillEstimate(False, side, notional_usdc, None, None, None, 0, "nonpositive_notional")
        ladder = self.asks if side == "long" else self.bids
        prices = sorted(ladder) if side == "long" else sorted(ladder, reverse=True)
        remaining = float(notional_usdc)
        qty = 0.0
        crossed = 0
        worst = None
        for px in prices:
            available_qty = ladder[px]
            available_notional = available_qty * px
            crossed += 1
            worst = px
            if available_notional >= remaining:
                qty += remaining / px
                remaining = 0.0
                break
            qty += available_qty
            remaining -= available_notional
        if remaining > 1e-9 or qty <= 0:
            return FillEstimate(False, side, notional_usdc, None, None, None, crossed, "insufficient_depth")
        return FillEstimate(True, side, notional_usdc, qty, notional_usdc / qty, worst, crossed)

    def taker_fill_quantity(self, side: str, *, quantity: float) -> FillEstimate:
        """Fill an exact quantity. Used for closing paper positions."""
        if quantity <= 0:
            return FillEstimate(False, side, 0.0, None, None, None, 0, "nonpositive_quantity")
        ladder = self.asks if side == "long" else self.bids
        prices = sorted(ladder) if side == "long" else sorted(ladder, reverse=True)
        remaining = float(quantity)
        notional = 0.0
        filled = 0.0
        crossed = 0
        worst = None
        for px in prices:
            take = min(remaining, ladder[px])
            if take <= 0:
                continue
            crossed += 1
            worst = px
            filled += take
            notional += take * px
            remaining -= take
            if remaining <= 1e-12:
                break
        if remaining > 1e-9 or filled <= 0:
            return FillEstimate(False, side, notional, None, None, None, crossed, "insufficient_depth")
        return FillEstimate(True, side, notional, filled, notional / filled, worst, crossed)
