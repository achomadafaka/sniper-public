#!/usr/bin/env python3
"""Tag analytics CLI — Phase 1 (tag_market_stats).

Runs per-tag resolve-distribution + volume + co-occurrence over
`market_tags ⋈ market_outcomes ⋈ market_metadata` and writes the output
to `data/tag_stats/tag_market_stats.json`.

Usage:
    python3 scripts/tag_analytics.py
    python3 scripts/tag_analytics.py --db sniper_tracker.db --out-dir data/tag_stats --min-markets 5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sniper.analysis.tag_analytics import (  # noqa: E402
    compute_tag_market_stats,
    summarize_coverage,
)

DEFAULT_DB = ROOT / "sniper_tracker.db"
DEFAULT_OUT_DIR = ROOT / "data" / "tag_stats"


def _serializable(stats: dict) -> list[dict]:
    rows = []
    for tag, row in sorted(
        stats.items(),
        key=lambda kv: (-(kv[1]["n_binary_resolved"] or 0), -(kv[1]["n_markets"] or 0)),
    ):
        out = dict(row)
        out["distinct_co_occurring_tags_top5"] = [
            {"tag": t, "count": c}
            for t, c in row["distinct_co_occurring_tags_top5"]
        ]
        rows.append(out)
    return rows


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=str(DEFAULT_DB))
    p.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    p.add_argument("--min-markets", type=int, default=1)
    p.add_argument("--print-top", type=int, default=20)
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    try:
        stats = compute_tag_market_stats(conn, min_markets=args.min_markets)
        coverage = summarize_coverage(conn, stats)
    finally:
        conn.close()

    payload = {
        "coverage": coverage,
        "tags": _serializable(stats),
    }
    out_path = out_dir / "tag_market_stats.json"
    out_path.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"wrote {out_path}")
    print(
        f"  tags={coverage['distinct_tags']}  resolved_markets="
        f"{coverage['resolved_markets_total']}  tagged_resolved="
        f"{coverage['resolved_markets_with_tags']}  binary_tagged="
        f"{coverage['binary_markets_with_tags']}"
    )
    pct = coverage["tag_coverage_pct_of_resolved"]
    if pct is not None:
        print(f"  tag_coverage_pct_of_resolved={pct * 100:.1f}%")

    if args.print_top:
        print()
        print(
            f"Top {args.print_top} tags by n_binary_resolved "
            "(min_markets applied):"
        )
        print(
            f"{'tag':<30} {'n_mkts':>7} {'n_bin':>6} {'yes':>5} {'no':>5} "
            f"{'yes_share':>10} {'med_vol':>10}"
        )
        for row in payload["tags"][: args.print_top]:
            yes_share = row["yes_share"]
            med_vol = row["median_market_volume_24h"]
            print(
                f"{row['tag']:<30} "
                f"{row['n_markets']:>7} "
                f"{row['n_binary_resolved']:>6} "
                f"{row['resolved_yes_count']:>5} "
                f"{row['resolved_no_count']:>5} "
                f"{(f'{yes_share:.2f}' if yes_share is not None else '-'):>10} "
                f"{(f'{med_vol:.0f}' if med_vol is not None else '-'):>10}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
