#!/usr/bin/env python3
"""Paper-only live shadow daemon for tennis stink bids.

Discovers active tennis H2H markets, watches the live favorite token, captures
visible CLOB depth, and evaluates incoming trade prints against that depth.
It never places or cancels exchange orders.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from data.polymarket_client import PolymarketClient
from sniper.feeds.polymarket_trade_tap import TradeTapState, TradeTick, run_trade_tap
from sniper.services.paper_limit_engine import compute_bid_price
from scripts.stink_bid_live_shadow import (
    ShadowOrder,
    ShadowOrderMonitor,
    TradePrint,
    depth_ahead_for_bid,
    parse_orderbook_snapshot,
    write_shadow_observation,
)
from sniper.services.stink_bid_runner import _fetch_gamma_tennis_h2h

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShadowCandidate:
    condition_id: str
    title: str
    outcome: str
    token_id: str
    favorite_price: float
    bid_price: float


def _parse_json_or_list(value) -> list:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return value if isinstance(value, list) else []


def parse_market_tokens(market: dict) -> list[tuple[str, str]]:
    outcomes = _parse_json_or_list(market.get("outcomes"))
    token_ids = _parse_json_or_list(market.get("clobTokenIds"))
    return [
        (str(outcome), str(token_id))
        for outcome, token_id in zip(outcomes, token_ids)
        if outcome and token_id
    ]


def _book_reference_price(raw_book: dict) -> Optional[float]:
    snapshot = parse_orderbook_snapshot(raw_book)
    if snapshot.best_bid is not None and snapshot.best_ask is not None:
        return round((snapshot.best_bid + snapshot.best_ask) / 2.0, 4)
    return snapshot.last_trade_price


def discover_shadow_candidates(
    pairs: list[dict],
    *,
    client: PolymarketClient,
    dip_pct: float,
    min_favorite: float,
    max_favorite: float,
    min_bid: float,
    min_fill_size: float,
    position_size_usdc: float,
) -> list[ShadowCandidate]:
    del min_fill_size, position_size_usdc
    candidates: list[ShadowCandidate] = []
    for pair in pairs:
        market = pair.get("market") or {}
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        title = str(market.get("question") or pair.get("event", {}).get("title") or "")
        priced_tokens = []
        for outcome, token_id in parse_market_tokens(market):
            raw_book = client.get_orderbook(token_id)
            if not raw_book:
                continue
            price = _book_reference_price(raw_book)
            if price is None:
                continue
            priced_tokens.append((price, outcome, token_id))
        if not priced_tokens:
            continue
        favorite_price, outcome, token_id = max(priced_tokens, key=lambda item: item[0])
        if favorite_price < min_favorite or favorite_price > max_favorite:
            continue
        candidates.append(
            ShadowCandidate(
                condition_id=condition_id,
                title=title,
                outcome=outcome,
                token_id=token_id,
                favorite_price=favorite_price,
                bid_price=compute_bid_price(favorite_price, dip_pct=dip_pct, min_bid=min_bid),
            )
        )
    return candidates


class ShadowDaemon:
    def __init__(
        self,
        *,
        client: PolymarketClient,
        output_path: str | Path,
        dip_pct: float,
        min_fill_size: float,
        position_size_usdc: float,
    ) -> None:
        self.client = client
        self.output_path = Path(output_path)
        self.dip_pct = dip_pct
        self.min_fill_size = min_fill_size
        self.position_size_usdc = position_size_usdc
        self.tap_state = TradeTapState()
        self.monitors: dict[str, ShadowOrderMonitor] = {}

    def set_candidates(self, candidates: list[ShadowCandidate]) -> None:
        next_tokens = {candidate.token_id for candidate in candidates}
        for stale in set(self.monitors) - next_tokens:
            self.tap_state.unwatch(stale)
            del self.monitors[stale]
        for candidate in candidates:
            order = ShadowOrder(
                condition_id=candidate.condition_id,
                token_id=candidate.token_id,
                bid_price=candidate.bid_price,
                min_fill_size=self.min_fill_size,
                position_size_usdc=self.position_size_usdc,
            )
            self.monitors[candidate.token_id] = ShadowOrderMonitor(
                order=order,
                output_path=self.output_path,
            )
            self.tap_state.watch(candidate.token_id)
            self.refresh_snapshot(candidate.token_id)

    def refresh_snapshot(self, token_id: str) -> bool:
        monitor = self.monitors.get(token_id)
        if monitor is None:
            return False
        raw_book = self.client.get_orderbook(token_id)
        if not raw_book:
            return False
        snapshot = parse_orderbook_snapshot(raw_book)
        monitor.set_snapshot(snapshot)
        depth_ahead = depth_ahead_for_bid(snapshot.bids, monitor.order.bid_price)
        write_shadow_observation(
            self.output_path,
            monitor.order,
            depth_ahead=depth_ahead,
            snapshot=snapshot,
        )
        return True

    def refresh_all_snapshots(self) -> None:
        for token_id in list(self.monitors):
            self.refresh_snapshot(token_id)

    def on_tick(self, tick: TradeTick):
        monitor = self.monitors.get(tick.token_id)
        if monitor is None:
            return None
        trade = TradePrint(
            token_id=tick.token_id,
            price=tick.price,
            size=tick.size,
            side=tick.side,
            timestamp_sec=int(tick.timestamp_ms // 1000),
        )
        return monitor.on_trade(trade)


async def run_shadow_daemon(
    *,
    output_path: str | Path,
    dip_pct: float = 0.15,
    min_favorite: float = 0.65,
    max_favorite: float = 0.90,
    min_bid: float = 0.05,
    min_fill_size: float = 50.0,
    position_size_usdc: float = 25.0,
    discover_seconds: int = 900,
    snapshot_seconds: int = 60,
) -> None:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    client = PolymarketClient(timeout=10.0)
    daemon = ShadowDaemon(
        client=client,
        output_path=output,
        dip_pct=dip_pct,
        min_fill_size=min_fill_size,
        position_size_usdc=position_size_usdc,
    )
    session = requests.Session()

    async def discover_loop() -> None:
        while True:
            pairs = _fetch_gamma_tennis_h2h(session)
            candidates = discover_shadow_candidates(
                pairs,
                client=client,
                dip_pct=dip_pct,
                min_favorite=min_favorite,
                max_favorite=max_favorite,
                min_bid=min_bid,
                min_fill_size=min_fill_size,
                position_size_usdc=position_size_usdc,
            )
            daemon.set_candidates(candidates)
            LOG.info("shadow discovered candidates=%d watched=%d", len(candidates), len(daemon.monitors))
            await asyncio.sleep(discover_seconds)

    async def snapshot_loop() -> None:
        while True:
            daemon.refresh_all_snapshots()
            await asyncio.sleep(snapshot_seconds)

    await asyncio.gather(
        discover_loop(),
        snapshot_loop(),
        run_trade_tap(daemon.tap_state, daemon.on_tick),
    )


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Discover candidates, capture snapshots, print summary, and exit.")
    parser.add_argument("--output", default="data/live_shadow/stink_bid_shadow.jsonl")
    parser.add_argument("--dip-pct", type=float, default=0.15)
    parser.add_argument("--min-favorite", type=float, default=0.65)
    parser.add_argument("--max-favorite", type=float, default=0.90)
    parser.add_argument("--min-bid", type=float, default=0.05)
    parser.add_argument("--min-fill-size", type=float, default=50.0)
    parser.add_argument("--position-size-usdc", type=float, default=25.0)
    parser.add_argument("--discover-seconds", type=int, default=900)
    parser.add_argument("--snapshot-seconds", type=int, default=60)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    if args.once:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        client = PolymarketClient(timeout=10.0)
        session = requests.Session()
        pairs = _fetch_gamma_tennis_h2h(session)
        candidates = discover_shadow_candidates(
            pairs,
            client=client,
            dip_pct=args.dip_pct,
            min_favorite=args.min_favorite,
            max_favorite=args.max_favorite,
            min_bid=args.min_bid,
            min_fill_size=args.min_fill_size,
            position_size_usdc=args.position_size_usdc,
        )
        daemon = ShadowDaemon(
            client=client,
            output_path=output,
            dip_pct=args.dip_pct,
            min_fill_size=args.min_fill_size,
            position_size_usdc=args.position_size_usdc,
        )
        daemon.set_candidates(candidates)
        print(
            json.dumps(
                {
                    "pairs_seen": len(pairs),
                    "candidates": [candidate.__dict__ for candidate in candidates],
                    "watched_tokens": sorted(daemon.monitors),
                    "output": str(output),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    asyncio.run(
        run_shadow_daemon(
            output_path=args.output,
            dip_pct=args.dip_pct,
            min_favorite=args.min_favorite,
            max_favorite=args.max_favorite,
            min_bid=args.min_bid,
            min_fill_size=args.min_fill_size,
            position_size_usdc=args.position_size_usdc,
            discover_seconds=args.discover_seconds,
            snapshot_seconds=args.snapshot_seconds,
        )
    )


if __name__ == "__main__":
    main()
