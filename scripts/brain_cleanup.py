#!/usr/bin/env python3
"""Brain cleanup — move static documentation from knowledge to reference type.

These entries were bulk-seeded as 'knowledge' on March 16-17 but are actually
system documentation, not live trading intelligence. Moving them to 'reference'
keeps the active knowledge section focused on live intel.

Usage:
    python3 scripts/brain_cleanup.py           # run the cleanup
    python3 scripts/brain_cleanup.py --dry-run  # preview without changes
"""
import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()

from bot.supabase_helper import read_memories, _request  # noqa: E402

# Entries that are static documentation, not live trading intel
STATIC_DOCS = {
    "app_architecture_map",
    "signal_pipeline_architecture",
    "paper_trader_position_sizing",
    "copy_trading_information_cascade_theory",
    "market_resolution_timing_edge",
    "known_issues_and_watchlist",
    "data_quality_and_known_gaps",
    "app_thesis_and_goal",
    "geopolitics_market_warning",
    "agent_pipeline_priority_scoring",
    "component_interconnections",
    "wallet_scoring_system",
    "prediction_market_microstructure",
    "kelly_criterion_implementation",
    "bankroll_management_system",
    "strategy_inventory_live",
    "strategy_news_intel_v1",
    "strategy_insider_copy_v1",
    "historical_backtest_results",
    "param_sensitivity_report",
    "sports_matchup_opportunity_guide",
    "market_exploitability_scores",
}

PM_PREFIX = "pm_"


def main():
    parser = argparse.ArgumentParser(description="Move static docs from knowledge to reference type")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    mems = read_memories(type_filter="knowledge")
    print(f"Found {len(mems)} knowledge entries total")

    moved = 0
    skipped = 0
    for m in mems:
        name = m.get("name", "")
        if name in STATIC_DOCS or name.startswith(PM_PREFIX):
            if args.dry_run:
                print(f"  [DRY RUN] would move to reference: {name}")
                moved += 1
            else:
                try:
                    _request(
                        "PATCH",
                        f"memories?id=eq.{m['id']}",
                        body={"type": "reference"},
                        extra_headers={"Prefer": "return=representation"},
                    )
                    moved += 1
                    print(f"  moved to reference: {name}")
                except Exception as e:
                    print(f"  FAILED: {name}: {e}")
                    skipped += 1

    action = "Would move" if args.dry_run else "Moved"
    print(f"\n{action} {moved} entries from knowledge -> reference")
    if skipped:
        print(f"  ({skipped} failed)")


if __name__ == "__main__":
    main()
