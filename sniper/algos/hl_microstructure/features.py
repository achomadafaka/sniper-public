"""Decision-time feature builder for Hyperliquid live paper policies."""
from __future__ import annotations

import math
from collections import defaultdict, deque

from sniper.algos.hl_microstructure import FEATURE_VERSION
from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.models import FeatureSnapshot


def _norm_side(side: str | None) -> str | None:
    s = str(side or "").lower()
    if s in ("b", "buy", "bid"):
        return "buy"
    if s in ("a", "sell", "ask"):
        return "sell"
    return None


class RollingMarketState:
    def __init__(self, *, max_history_ms: int = 120_000):
        self.max_history_ms = int(max_history_ms)
        self.books: dict[str, BookState] = {}
        self.trades = defaultdict(deque)
        self.mids = defaultdict(deque)
        self.asset_ctx: dict[str, dict] = {}

    def _trim(self, asset: str, now_ms: int) -> None:
        floor = now_ms - self.max_history_ms
        for q in (self.trades[asset], self.mids[asset]):
            while q and q[0]["receive_ts_ms"] < floor:
                q.popleft()

    def update_book(self, book: BookState) -> None:
        self.books[book.asset] = book
        if book.mid is not None:
            self.mids[book.asset].append({
                "receive_ts_ms": book.receive_ts_ms,
                "exchange_ts_ms": book.exchange_ts_ms,
                "mid": book.mid,
            })
        self._trim(book.asset, book.receive_ts_ms)

    def ingest_trade(self, asset: str, *, side: str, price: float, size: float,
                     exchange_ts_ms: int | None, receive_ts_ms: int) -> None:
        norm = _norm_side(side)
        if norm is None or price <= 0 or size <= 0:
            return
        self.trades[asset].append({
            "side": norm,
            "price": float(price),
            "size": float(size),
            "notional": float(price) * float(size),
            "exchange_ts_ms": exchange_ts_ms,
            "receive_ts_ms": int(receive_ts_ms),
        })
        self._trim(asset, int(receive_ts_ms))

    def update_asset_ctx(self, asset: str, ctx: dict, *, receive_ts_ms: int) -> None:
        self.asset_ctx[asset] = {**(ctx or {}), "receive_ts_ms": int(receive_ts_ms)}

    def _mid_at_or_before(self, asset: str, ts_ms: int) -> float | None:
        vals = [m for m in self.mids[asset] if m["receive_ts_ms"] <= ts_ms]
        return vals[-1]["mid"] if vals else None

    def _return(self, asset: str, decision_ts_ms: int, window_ms: int) -> float | None:
        now = self._mid_at_or_before(asset, decision_ts_ms)
        prev = self._mid_at_or_before(asset, decision_ts_ms - window_ms)
        if now is None or prev in (None, 0):
            return None
        return math.log(now / prev)

    def _realized_vol(self, asset: str, decision_ts_ms: int, window_ms: int) -> float | None:
        points = [m for m in self.mids[asset]
                  if decision_ts_ms - window_ms <= m["receive_ts_ms"] <= decision_ts_ms]
        if len(points) < 3:
            return None
        rets = []
        for a, b in zip(points, points[1:]):
            if a["mid"] > 0 and b["mid"] > 0:
                rets.append(math.log(b["mid"] / a["mid"]))
        if len(rets) < 2:
            return None
        mu = sum(rets) / len(rets)
        return math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))

    def _flow(self, asset: str, decision_ts_ms: int, window_ms: int) -> dict:
        recent = [t for t in self.trades[asset]
                  if decision_ts_ms - window_ms <= t["receive_ts_ms"] <= decision_ts_ms]
        buy = sum(t["notional"] for t in recent if t["side"] == "buy")
        sell = sum(t["notional"] for t in recent if t["side"] == "sell")
        total = buy + sell
        return {
            "buy": buy,
            "sell": sell,
            "count": len(recent),
            "imbalance": ((buy - sell) / total) if total > 0 else 0.0,
        }

    def snapshot(self, asset: str, *, decision_ts_ms: int) -> FeatureSnapshot:
        book = self.books.get(asset)
        if book is None or book.receive_ts_ms > decision_ts_ms:
            return FeatureSnapshot(asset, int(decision_ts_ms), FEATURE_VERSION, {}, "book_missing", ["book_missing"])

        flow_5 = self._flow(asset, decision_ts_ms, 5_000)
        flow_10 = self._flow(asset, decision_ts_ms, 10_000)
        flow_30 = self._flow(asset, decision_ts_ms, 30_000)
        flow_60 = self._flow(asset, decision_ts_ms, 60_000)
        bid_depth = book.depth_notional("bid", levels=5)
        ask_depth = book.depth_notional("ask", levels=5)
        depth_total = bid_depth + ask_depth
        ctx = self.asset_ctx.get(asset) or {}
        ctx_fresh = ctx.get("receive_ts_ms") is not None and ctx.get("receive_ts_ms") <= decision_ts_ms

        features = {
            "mid": book.mid,
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "spread_bps": book.spread_bps,
            "bid_depth_5": bid_depth,
            "ask_depth_5": ask_depth,
            "book_imbalance_5": ((bid_depth - ask_depth) / depth_total) if depth_total > 0 else None,
            "buy_notional_5s": flow_5["buy"],
            "sell_notional_5s": flow_5["sell"],
            "flow_imbalance_5s": flow_5["imbalance"],
            "trade_count_5s": flow_5["count"],
            "buy_notional_10s": flow_10["buy"],
            "sell_notional_10s": flow_10["sell"],
            "flow_imbalance_10s": flow_10["imbalance"],
            "trade_count_10s": flow_10["count"],
            "buy_notional_30s": flow_30["buy"],
            "sell_notional_30s": flow_30["sell"],
            "flow_imbalance_30s": flow_30["imbalance"],
            "trade_count_30s": flow_30["count"],
            "buy_notional_60s": flow_60["buy"],
            "sell_notional_60s": flow_60["sell"],
            "flow_imbalance_60s": flow_60["imbalance"],
            "trade_count_60s": flow_60["count"],
            "return_5s": self._return(asset, decision_ts_ms, 5_000),
            "return_15s": self._return(asset, decision_ts_ms, 15_000),
            "return_30s": self._return(asset, decision_ts_ms, 30_000),
            "return_60s": self._return(asset, decision_ts_ms, 60_000),
            "realized_vol_10s": self._realized_vol(asset, decision_ts_ms, 10_000),
            "realized_vol_30s": self._realized_vol(asset, decision_ts_ms, 30_000),
            "realized_vol_60s": self._realized_vol(asset, decision_ts_ms, 60_000),
            "funding": _to_float(ctx.get("funding")) if ctx_fresh else None,
            "open_interest": _to_float(ctx.get("openInterest")) if ctx_fresh else None,
            "premium": _to_float(ctx.get("premium")) if ctx_fresh else None,
            "mark_price": _to_float(ctx.get("markPx")) if ctx_fresh else None,
            "oracle_price": _to_float(ctx.get("oraclePx")) if ctx_fresh else None,
            "source_age_ms": max(0, int(decision_ts_ms) - int(book.receive_ts_ms)),
        }
        return FeatureSnapshot(asset, int(decision_ts_ms), FEATURE_VERSION, features, "ok", [])


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
