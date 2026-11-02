#!/usr/bin/env python3
"""Live shadow orderbook capture for stink-bid candidates.

This module does not place orders. It records the live book state around a
candidate bid and evaluates whether a later trade would have cleared the depth
ahead of a paper resting bid.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.polymarket_client import PolymarketClient
from sniper.services.paper_limit_engine import compute_bid_price


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float


@dataclass(frozen=True)
class OrderbookSnapshot:
    condition_id: str
    token_id: str
    timestamp: str
    hash: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    min_order_size: Optional[float]
    tick_size: Optional[float]
    last_trade_price: Optional[float]

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 10)


@dataclass(frozen=True)
class ShadowOrder:
    condition_id: str
    token_id: str
    bid_price: float
    min_fill_size: float
    position_size_usdc: float


@dataclass(frozen=True)
class TradePrint:
    token_id: str
    price: float
    size: float
    side: str
    timestamp_sec: int
    transaction_hash: str = ""


@dataclass(frozen=True)
class ShadowFillEvaluation:
    price_crossed: bool
    size_qualified: bool
    queue_fillable: bool
    depth_ahead: float
    required_trade_size: float
    reason: str


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    return float(value)


def _levels(raw_levels: list[dict]) -> list[BookLevel]:
    return [
        BookLevel(price=float(level["price"]), size=float(level["size"]))
        for level in raw_levels or []
        if level.get("price") is not None and level.get("size") is not None
    ]


def parse_orderbook_snapshot(raw: dict) -> OrderbookSnapshot:
    """Normalize Polymarket `/book` payload into typed floats."""
    return OrderbookSnapshot(
        condition_id=str(raw.get("market") or ""),
        token_id=str(raw.get("asset_id") or ""),
        timestamp=str(raw.get("timestamp") or ""),
        hash=str(raw.get("hash") or ""),
        bids=_levels(raw.get("bids") or []),
        asks=_levels(raw.get("asks") or []),
        min_order_size=_to_float(raw.get("min_order_size")),
        tick_size=_to_float(raw.get("tick_size")),
        last_trade_price=_to_float(raw.get("last_trade_price")),
    )


def depth_ahead_for_bid(bids: list[BookLevel], bid_price: float) -> float:
    """Return visible resting size at better or equal bid prices.

    Equal-price depth is counted as ahead because this shadow system assumes our
    hypothetical order is placed after the current snapshot.
    """
    return sum(level.size for level in bids if level.price >= bid_price)


def evaluate_shadow_fill(
    order: ShadowOrder,
    trade: TradePrint,
    *,
    depth_ahead: float,
) -> ShadowFillEvaluation:
    """Evaluate whether a trade plausibly reaches our paper resting bid.

    Conservative rule: a trade must cross our bid, meet the live min-fill rule,
    and be large enough to clear visible depth ahead plus `min_fill_size`.
    """
    price_crossed = trade.token_id == order.token_id and trade.price <= order.bid_price
    size_qualified = trade.size >= order.min_fill_size
    required_trade_size = depth_ahead + order.min_fill_size
    queue_fillable = price_crossed and size_qualified and trade.size >= required_trade_size
    if not price_crossed:
        reason = "price_not_crossed"
    elif not size_qualified:
        reason = "trade_below_min_fill_size"
    elif not queue_fillable:
        reason = "visible_queue_ahead_not_cleared"
    else:
        reason = "queue_fillable"
    return ShadowFillEvaluation(
        price_crossed=price_crossed,
        size_qualified=size_qualified,
        queue_fillable=queue_fillable,
        depth_ahead=depth_ahead,
        required_trade_size=required_trade_size,
        reason=reason,
    )


def evaluate_queue_models(
    order: ShadowOrder,
    trade: TradePrint,
    *,
    depth_ahead: float,
) -> list[dict]:
    """Evaluate the same trade under multiple queue assumptions.

    `pessimistic`: we are behind all visible same/better bid depth.
    `midpoint`: half visible depth is ahead.
    `optimistic`: no visible depth is ahead.
    `price_cross`: ignores queue and checks only price/size.
    """
    models = {
        "pessimistic": depth_ahead,
        "midpoint": depth_ahead / 2.0,
        "optimistic": 0.0,
        "price_cross": 0.0,
    }
    rows = []
    for name, model_depth in models.items():
        if name == "price_cross":
            price_crossed = trade.token_id == order.token_id and trade.price <= order.bid_price
            size_qualified = trade.size >= order.min_fill_size
            rows.append(
                {
                    "queue_model": name,
                    "price_crossed": price_crossed,
                    "size_qualified": size_qualified,
                    "queue_fillable": price_crossed and size_qualified,
                    "depth_ahead": 0.0,
                    "required_trade_size": order.min_fill_size,
                    "reason": "price_cross_fillable" if price_crossed and size_qualified else "not_price_cross_fillable",
                }
            )
            continue
        evaluation = evaluate_shadow_fill(order, trade, depth_ahead=model_depth)
        row = asdict(evaluation)
        row["queue_model"] = name
        rows.append(row)
    return rows


def evaluate_trade_against_snapshot(
    order: ShadowOrder,
    snapshot: OrderbookSnapshot,
    trade: TradePrint,
) -> ShadowFillEvaluation:
    depth_ahead = depth_ahead_for_bid(snapshot.bids, order.bid_price)
    return evaluate_shadow_fill(order, trade, depth_ahead=depth_ahead)


def build_shadow_order(
    *,
    condition_id: str,
    token_id: str,
    favorite_price: float,
    dip_pct: float,
    min_bid: float,
    min_fill_size: float,
    position_size_usdc: float,
) -> ShadowOrder:
    return ShadowOrder(
        condition_id=condition_id,
        token_id=token_id,
        bid_price=compute_bid_price(favorite_price, dip_pct=dip_pct, min_bid=min_bid),
        min_fill_size=min_fill_size,
        position_size_usdc=position_size_usdc,
    )


def write_shadow_observation(
    output_path: str | Path,
    order: ShadowOrder,
    *,
    depth_ahead: float,
    evaluation: Optional[ShadowFillEvaluation] = None,
    snapshot: Optional[OrderbookSnapshot] = None,
    queue_models: Optional[list[dict]] = None,
) -> None:
    row = {
        "event_type": "trade_evaluation" if evaluation is not None else "book_snapshot",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        **asdict(order),
        "depth_ahead": depth_ahead,
    }
    if snapshot is not None:
        row.update(
            {
                "book_timestamp": snapshot.timestamp,
                "book_hash": snapshot.hash,
                "best_bid": snapshot.best_bid,
                "best_ask": snapshot.best_ask,
                "spread": snapshot.spread,
                "min_order_size": snapshot.min_order_size,
                "tick_size": snapshot.tick_size,
                "last_trade_price": snapshot.last_trade_price,
            }
        )
    if evaluation is not None:
        row.update(asdict(evaluation))
    if queue_models is not None:
        row["queue_models"] = queue_models
    with open(output_path, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


class ShadowOrderMonitor:
    """Maintain latest book snapshot and evaluate incoming trade prints."""

    def __init__(self, *, order: ShadowOrder, output_path: str | Path) -> None:
        self.order = order
        self.output_path = Path(output_path)
        self.snapshot: Optional[OrderbookSnapshot] = None

    def set_snapshot(self, snapshot: OrderbookSnapshot) -> None:
        if snapshot.token_id != self.order.token_id:
            raise ValueError(
                f"snapshot token {snapshot.token_id!r} does not match order token {self.order.token_id!r}"
            )
        self.snapshot = snapshot

    def on_trade(self, trade: TradePrint) -> Optional[ShadowFillEvaluation]:
        if trade.token_id != self.order.token_id or self.snapshot is None:
            return None
        evaluation = evaluate_trade_against_snapshot(self.order, self.snapshot, trade)
        queue_rows = evaluate_queue_models(
            self.order,
            trade,
            depth_ahead=evaluation.depth_ahead,
        )
        write_shadow_observation(
            self.output_path,
            self.order,
            depth_ahead=evaluation.depth_ahead,
            evaluation=evaluation,
            snapshot=self.snapshot,
            queue_models=queue_rows,
        )
        return evaluation


def capture_once(
    *,
    token_id: str,
    condition_id: str,
    favorite_price: float,
    output_path: str | Path,
    dip_pct: float = 0.15,
    min_bid: float = 0.05,
    min_fill_size: float = 50.0,
    position_size_usdc: float = 25.0,
    client: Optional[PolymarketClient] = None,
) -> dict:
    client = client or PolymarketClient()
    raw_book = client.get_orderbook(token_id)
    if not raw_book:
        raise RuntimeError(f"failed to fetch orderbook for token_id={token_id}")
    snapshot = parse_orderbook_snapshot(raw_book)
    order = build_shadow_order(
        condition_id=condition_id or snapshot.condition_id,
        token_id=token_id,
        favorite_price=favorite_price,
        dip_pct=dip_pct,
        min_bid=min_bid,
        min_fill_size=min_fill_size,
        position_size_usdc=position_size_usdc,
    )
    depth_ahead = depth_ahead_for_bid(snapshot.bids, order.bid_price)
    write_shadow_observation(output_path, order, depth_ahead=depth_ahead, snapshot=snapshot)
    return {
        "condition_id": order.condition_id,
        "token_id": order.token_id,
        "bid_price": order.bid_price,
        "depth_ahead": depth_ahead,
        "best_bid": snapshot.best_bid,
        "best_ask": snapshot.best_ask,
        "spread": snapshot.spread,
        "output_path": str(output_path),
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition-id", required=True)
    parser.add_argument("--token-id", required=True)
    parser.add_argument("--favorite-price", type=float, required=True)
    parser.add_argument("--output", default="data/live_shadow/stink_bid_orderbook.jsonl")
    parser.add_argument("--dip-pct", type=float, default=0.15)
    parser.add_argument("--min-bid", type=float, default=0.05)
    parser.add_argument("--min-fill-size", type=float, default=50.0)
    parser.add_argument("--position-size-usdc", type=float, default=25.0)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    row = capture_once(
        condition_id=args.condition_id,
        token_id=args.token_id,
        favorite_price=args.favorite_price,
        output_path=output,
        dip_pct=args.dip_pct,
        min_bid=args.min_bid,
        min_fill_size=args.min_fill_size,
        position_size_usdc=args.position_size_usdc,
    )
    print(json.dumps(row, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
