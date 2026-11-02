"""Derive normalized OrderBookPoints (mid/spread/depth) from raw CLOB book snapshots."""
from __future__ import annotations

from sniper.feeds.thesis_feed_types import OrderBookPoint


def _levels(rows: list[dict], *, reverse: bool) -> list[tuple[float, float]]:
    out = []
    for row in rows or []:
        try:
            out.append((float(row["price"]), float(row["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0], reverse=reverse)


def point_from_book(msg: dict) -> OrderBookPoint:
    bids = _levels(msg.get("bids") or [], reverse=True)
    asks = _levels(msg.get("asks") or [], reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return OrderBookPoint(
        asset_id=str(msg["asset_id"]),
        ts_ms=int(msg["timestamp"]),
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        bid_depth_1=bids[0][1] if bids else 0.0,
        ask_depth_1=asks[0][1] if asks else 0.0,
        bid_depth_5=sum(size for _, size in bids[:5]),
        ask_depth_5=sum(size for _, size in asks[:5]),
        source="polymarket_clob_ws",
    )
