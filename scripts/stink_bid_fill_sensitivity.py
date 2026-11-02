#!/usr/bin/env python3
"""Summarize strict-vs-loose maker fill sensitivity for stink-bid audits."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.simulate_stink_bid_smoke import SimConfig, run_sim


def summarize_audit_events(audit_events: list[dict]) -> dict:
    markets = len(audit_events)
    strict = [row for row in audit_events if row.get("status") == "filled"]
    loose_only = [
        row
        for row in audit_events
        if row.get("status") != "filled" and int(row.get("loose_fills_seen") or 0) > 0
    ]
    price_cross_or_loose = len(strict) + len(loose_only)
    strict_pnl = sum(float(row.get("pnl_usdc") or 0.0) for row in strict)
    return {
        "markets": markets,
        "strict_fills": len(strict),
        "loose_only_markets": len(loose_only),
        "price_cross_or_loose_markets": price_cross_or_loose,
        "strict_fill_rate": len(strict) / markets if markets else 0.0,
        "loose_or_cross_rate": price_cross_or_loose / markets if markets else 0.0,
        "loose_to_strict_ratio": price_cross_or_loose / len(strict) if strict else None,
        "strict_pnl_usdc": strict_pnl,
    }


def run_sensitivity(
    db_path: str | Path,
    cfg: SimConfig,
    *,
    since_year: Optional[int] = None,
    until_year: Optional[int] = None,
    tour: Optional[str] = None,
) -> dict:
    summary = run_sim(
        db_path,
        cfg,
        since_year=since_year,
        until_year=until_year,
        tour=tour,
        include_audit=True,
    )
    audit_summary = summarize_audit_events(summary["audit_events"])
    return {
        "config": summary["config"],
        "eligible_priced_events": summary["eligible_priced_events"],
        "tour": tour,
        "since_year": since_year,
        "until_year": until_year,
        "strict_summary": {
            key: summary[key]
            for key in (
                "fills",
                "wins",
                "losses",
                "win_rate",
                "total_pnl_usdc",
                "profit_factor",
                "max_drawdown_usdc",
            )
        },
        "fill_sensitivity": audit_summary,
    }


def parse_args(argv: Optional[list[str]] = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/backtest/stink_bid/backtest.db")
    parser.add_argument("--dip-pct", type=float, default=0.15)
    parser.add_argument("--min-favorite", type=float, default=0.65)
    parser.add_argument("--max-position-minutes", type=int, default=240)
    parser.add_argument("--min-fill-size", type=float, default=50.0)
    parser.add_argument("--since-year", type=int)
    parser.add_argument("--until-year", type=int)
    parser.add_argument("--tour", choices=["atp", "wta"])
    parser.add_argument("--output")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = parse_args(argv)
    out = run_sensitivity(
        args.db,
        SimConfig(
            fill_model="runner_parity_trade",
            dip_pct=args.dip_pct,
            min_favorite=args.min_favorite,
            max_position_minutes=args.max_position_minutes,
            min_fill_size=args.min_fill_size,
        ),
        since_year=args.since_year,
        until_year=args.until_year,
        tour=args.tour,
    )
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
