#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.analysis.public_truth_stale.manual_log import load_manual_observations
from sniper.analysis.public_truth_stale.pricing import (
    InsufficientDepthError,
    depth_weighted_buy_price,
    net_edge,
)
from sniper.analysis.public_truth_stale.report import PaperEntry, build_markdown_report
from sniper.analysis.public_truth_stale.types import SourceLane


MIN_NET_EDGE = 0.04
FIXED_STAKE_USDC = 20.0


def _load_book(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _book_path(books_dir: Path, orderbook_file: Path) -> Path:
    if orderbook_file.is_absolute():
        return orderbook_file
    return books_dir / orderbook_file


def _entry_rejection_reason(
    *,
    source_lane: SourceLane,
    rule_match_status: str,
    finality_status: str,
    stake_usdc: float,
    edge: float,
) -> str:
    if source_lane is SourceLane.AMBIGUITY_DISPUTE:
        return "lane_d_excluded"
    if abs(stake_usdc - FIXED_STAKE_USDC) > 1e-9:
        return "non_fixed_stake"
    if rule_match_status != "exact_match":
        return "rule_match_not_exact"
    if (
        source_lane is SourceLane.RESOLUTION_AUTHORITATIVE
        and finality_status != "official_final"
    ):
        return "lane_a_requires_official_final"
    if edge < MIN_NET_EDGE:
        return "net_edge_below_threshold"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score manual Public-Truth Stale paper observations offline."
    )
    parser.add_argument("--signals", required=True, type=Path)
    parser.add_argument("--books-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    observations = load_manual_observations(args.signals)
    entries: list[PaperEntry] = []

    for observation in observations:
        book = _load_book(_book_path(args.books_dir, observation.orderbook_file))
        try:
            fill = depth_weighted_buy_price(
                book.get("asks", []),
                stake_usdc=observation.stake_usdc,
            )
            edge = net_edge(
                expected_payout=1.0,
                entry_price=fill.avg_price,
                modeled_cost_per_share=observation.modeled_cost_per_share,
            )
            rejection_reason = _entry_rejection_reason(
                source_lane=observation.source_lane,
                rule_match_status=observation.rule_match_status,
                finality_status=observation.finality_status,
                stake_usdc=observation.stake_usdc,
                edge=edge,
            )
            avg_entry_price = fill.avg_price
            shares = fill.shares
        except InsufficientDepthError:
            edge = 0.0
            rejection_reason = "insufficient_depth"
            avg_entry_price = 0.0
            shares = 0.0
        entries.append(
            PaperEntry(
                signal_id=observation.signal_id,
                source_lane=observation.source_lane,
                confirmation_status=observation.confirmation_status,
                stake_usdc=observation.stake_usdc,
                avg_entry_price=avg_entry_price,
                shares=shares,
                net_edge=edge,
                entered=not rejection_reason,
                rejection_reason=rejection_reason,
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        build_markdown_report(entries, source_path=args.signals),
        encoding="utf-8",
    )
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
