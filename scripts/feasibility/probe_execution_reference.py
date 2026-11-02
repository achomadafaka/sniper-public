#!/usr/bin/env python3
"""MVE — can we measure execution quality on Polymarket wallet fills?

Viability experiment for the execution-layer scope memo
(docs/research/scopes/2026-04-22-execution-layer-scope.md).

Answers three questions:

  1. Does a PRECEDING minute-bar from prices-history produce a meaningful
     skew distribution vs actual fill prices, or is the self-reference problem
     so dominant that every wallet looks like zero skew?
  2. Does trade-tape midpoint (other wallets' trades in the 5 minutes before
     the fill) agree with the minute-bar reference?
  3. Do two mechanically distinct wallets produce distinguishable execution
     signatures? (SwissMiss = rotating accumulator; kch123 = two-sided
     inventory trader.)

NOT production code. Write-once research probe. Output is stdout + a markdown
memo under docs/research/. Do not import from this script elsewhere.

Usage:
  python3 scripts/feasibility/probe_execution_reference.py \\
      --wallet 0xdbade4... --wallet 0x6a72f6... --max-trades 25
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.polymarket_activity_client import iter_activity
from data.polymarket_client import PolymarketClient


@dataclass
class FillProbe:
    wallet: str
    condition_id: str
    asset: str
    title: str
    trade_ts: int
    fill_price: float
    ref_bar_preceding: float | None
    ref_bar_covering: float | None
    ref_tape_5min: float | None
    tape_trade_count: int
    drift_1h: float | None
    drift_6h: float | None
    drift_24h: float | None


def _preceding_bar(history: list[dict], trade_ts: int) -> float | None:
    """Last bar with ts strictly before trade_ts, within a 30-minute window."""
    best = None
    for point in history:
        ts = point["timestamp"]
        if ts < trade_ts and ts >= trade_ts - 1800:
            if best is None or ts > best["timestamp"]:
                best = point
    return best["price"] if best else None


def _covering_bar(history: list[dict], trade_ts: int) -> float | None:
    """Closest bar in absolute time to trade_ts, within 120 seconds.

    Self-referential on liquid markets: the minute containing the wallet's fill
    is heavily influenced by that fill. Kept here so we can *measure* the
    self-reference bias instead of asserting it.
    """
    best = None
    best_gap = 121
    for point in history:
        gap = abs(point["timestamp"] - trade_ts)
        if gap < best_gap:
            best = point
            best_gap = gap
    return best["price"] if best else None


def _bar_at_offset(history: list[dict], trade_ts: int, offset_sec: int) -> float | None:
    """Closest bar to trade_ts + offset_sec, within a half-window of tolerance."""
    target = trade_ts + offset_sec
    tolerance = max(300, offset_sec // 12)
    best = None
    best_gap = tolerance + 1
    for point in history:
        gap = abs(point["timestamp"] - target)
        if gap < best_gap:
            best = point
            best_gap = gap
    return best["price"] if best else None


def _tape_midpoint(
    client: PolymarketClient,
    condition_id: str,
    asset: str,
    trade_ts: int,
    exclude_wallet: str,
) -> tuple[float | None, int]:
    """Mean fill price from OTHER wallets on the same asset in [t-5m, t)."""
    start = trade_ts - 300
    end = trade_ts - 1
    trades = client.get_trades_window(condition_id, start, end, page_size=200, max_pages=5)
    prices: list[float] = []
    for trade in trades:
        if str(trade.get("asset") or "") != asset:
            continue
        if str(trade.get("proxyWallet") or "").lower() == exclude_wallet.lower():
            continue
        price = trade.get("price")
        if price is None:
            continue
        try:
            prices.append(float(price))
        except (TypeError, ValueError):
            continue
    if not prices:
        return None, 0
    return mean(prices), len(prices)


def probe_wallet(wallet: str, max_trades: int) -> list[FillProbe]:
    client = PolymarketClient()
    buys: list[tuple[int, str, str, str, float]] = []
    for row in iter_activity(wallet):
        if row.activity_type != "TRADE" or row.side != "BUY":
            continue
        if not row.asset or not row.condition_id or row.price is None or row.timestamp <= 0:
            continue
        buys.append((row.timestamp, row.condition_id, row.asset, row.title, float(row.price)))
        if len(buys) >= max_trades:
            break

    probes: list[FillProbe] = []
    for trade_ts, condition_id, asset, title, fill_price in buys:
        # One prices-history call covering pre-window + drift windows.
        history = client.get_prices_series(
            token_id=asset,
            start_ts=trade_ts - 1800,
            end_ts=trade_ts + 24 * 3600 + 300,
            fidelity=1,
        )
        ref_prec = _preceding_bar(history, trade_ts)
        ref_cov = _covering_bar(history, trade_ts)
        drift_1h = _bar_at_offset(history, trade_ts, 3600)
        drift_6h = _bar_at_offset(history, trade_ts, 6 * 3600)
        drift_24h = _bar_at_offset(history, trade_ts, 24 * 3600)

        ref_tape, tape_n = _tape_midpoint(client, condition_id, asset, trade_ts, wallet)

        probes.append(
            FillProbe(
                wallet=wallet.lower(),
                condition_id=condition_id,
                asset=asset,
                title=title,
                trade_ts=trade_ts,
                fill_price=fill_price,
                ref_bar_preceding=ref_prec,
                ref_bar_covering=ref_cov,
                ref_tape_5min=ref_tape,
                tape_trade_count=tape_n,
                drift_1h=(drift_1h - fill_price) if drift_1h is not None else None,
                drift_6h=(drift_6h - fill_price) if drift_6h is not None else None,
                drift_24h=(drift_24h - fill_price) if drift_24h is not None else None,
            )
        )
        # Gentle pace; the client has retries but doesn't throttle.
        time.sleep(0.25)

    return probes


def _dist_summary(values: list[float]) -> dict[str, Any]:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0, "mean": None, "median": None, "stdev": None, "min": None, "max": None}
    return {
        "n": len(values),
        "mean": round(mean(values), 5),
        "median": round(median(values), 5),
        "stdev": round(pstdev(values), 5) if len(values) >= 2 else 0.0,
        "min": round(min(values), 5),
        "max": round(max(values), 5),
    }


def summarize(probes: list[FillProbe]) -> dict[str, Any]:
    if not probes:
        return {"n_trades": 0}

    skew_bar_prec = [
        p.fill_price - p.ref_bar_preceding
        for p in probes
        if p.ref_bar_preceding is not None
    ]
    skew_bar_cov = [
        p.fill_price - p.ref_bar_covering
        for p in probes
        if p.ref_bar_covering is not None
    ]
    skew_tape = [
        p.fill_price - p.ref_tape_5min
        for p in probes
        if p.ref_tape_5min is not None
    ]
    drift_1h = [p.drift_1h for p in probes if p.drift_1h is not None]
    drift_6h = [p.drift_6h for p in probes if p.drift_6h is not None]
    drift_24h = [p.drift_24h for p in probes if p.drift_24h is not None]

    # Coverage — how often do we actually *have* each reference?
    n = len(probes)
    coverage = {
        "preceding_bar": sum(1 for p in probes if p.ref_bar_preceding is not None) / n,
        "covering_bar": sum(1 for p in probes if p.ref_bar_covering is not None) / n,
        "tape_5min": sum(1 for p in probes if p.ref_tape_5min is not None) / n,
        "drift_1h": len(drift_1h) / n,
        "drift_6h": len(drift_6h) / n,
        "drift_24h": len(drift_24h) / n,
    }

    return {
        "n_trades": n,
        "coverage": {k: round(v, 3) for k, v in coverage.items()},
        "skew_vs_preceding_bar": _dist_summary(skew_bar_prec),
        "skew_vs_covering_bar": _dist_summary(skew_bar_cov),
        "skew_vs_tape_5min": _dist_summary(skew_tape),
        "drift_1h": _dist_summary(drift_1h),
        "drift_6h": _dist_summary(drift_6h),
        "drift_24h": _dist_summary(drift_24h),
        "tape_trade_count_median": int(median([p.tape_trade_count for p in probes])),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="MVE: execution-reference viability probe")
    parser.add_argument("--wallet", action="append", required=True, help="Wallet address (repeatable)")
    parser.add_argument("--max-trades", type=int, default=25)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    results: dict[str, Any] = {}
    for wallet in args.wallet:
        print(f"=== probing {wallet} (up to {args.max_trades} trades) ===", file=sys.stderr)
        probes = probe_wallet(wallet, args.max_trades)
        summary = summarize(probes)
        results[wallet.lower()] = {
            "summary": summary,
            "probes": [p.__dict__ for p in probes],
        }
        print(json.dumps({wallet.lower(): summary}, indent=2))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json_out}", file=sys.stderr)


if __name__ == "__main__":
    main()
