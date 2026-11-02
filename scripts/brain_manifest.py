#!/usr/bin/env python3
"""Create/update the brain manifest and clean up legacy entries."""
import os, sys, json
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        if k.strip() and k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()

from bot.supabase_helper import upsert_memory, read_memories, _request

MANIFEST = {
    "version": 1,
    "description": "Sniper brain structure — who owns what, who reads what, how data flows",
    "sections": {
        "world_state": {
            "owner": "geopolitics_analyst (scripts/world_analyst.py)",
            "purpose": "Geopolitics & political intelligence — conflicts, sanctions, elections, alliances, regime changes",
            "writes_to": {
                "memories": ["world_state", "market_boosts_current"],
                "tables": ["world_events", "industry_impacts", "market_opportunities"]
            },
            "read_by": ["paper_trader (boosts)", "strategy_runner (boosts)", "intel_tab (events)", "finance_analyst (cross-domain query)"],
            "schedule": "every 4h (00, 04, 08, 12, 16, 20 UTC)"
        },
        "markets": {
            "owner": "finance_analyst (finance_analyst.py)",
            "purpose": "Financial / macro / crypto intelligence — Fed, rates, equities, commodities, crypto",
            "writes_to": {
                "memories": ["markets/state", "markets/boosts"],
                "tables": ["trade_ideas"]
            },
            "read_by": ["paper_trader (boosts)", "strategy_runner (boosts)", "news_intel (trade ideas)", "geopolitics_analyst (cross-domain query)"],
            "schedule": "every 4h (02, 06, 10, 14, 18, 22 UTC)"
        },
        "flags": {
            "owner": "any specialist agent",
            "purpose": "Cross-domain urgent alerts — geo→finance, finance→geo flags with expiry",
            "writes_to": {
                "memories": ["flag:geo→finance:*", "flag:finance→geo:*"]
            },
            "read_by": ["target domain specialist (read + clear after processing)"],
            "schedule": "on-demand (created during agent runs, read at start of target agent runs)",
            "tool": "agents/tools/domain_query.py --flag / --read-flags / --clear-flag"
        },
        "portfolio": {
            "owner": "system (sync-state)",
            "purpose": "Live portfolio state — strategy performance, convergence, conflicts",
            "writes_to": {
                "memories": ["Strategy Performance", "Wallet Analysis State", "VPS Services Status",
                             "convergence:latest-scan", "conflict:latest-scan", "strategy_regimes_current"]
            },
            "read_by": ["all agents (context loading)", "health_checker"],
            "schedule": "every 30min (sync-state timer)"
        },
        "wallets": {
            "owner": "wallet_scout",
            "purpose": "Wallet discovery, vetting, strategy lifecycle",
            "writes_to": {
                "observations": "strategy_type=wallet_scout",
                "memories": ["Wallet Analysis State"]
            },
            "read_by": ["insider_copy (reads scout run_summary for context)"],
            "schedule": "twice daily (09:00 + 21:00 UTC)"
        },
        "signals": {
            # Owner archived 2026-07-14: insider_scoring_analyst retired with the insider_score
            # formula per ADR-006 (inversely correlated with lifetime PnL). Historical writes
            # remain queryable under strategy_type=insider_analyst; no live agent produces new
            # ones. Kept in the manifest so those legacy rows still resolve to a named section.
            "owner": "archived:insider_scoring_analyst",
            "purpose": "Historical insider_score calibration — retired 2026-07-14 alongside the score itself.",
            "writes_to": {
                "observations": "strategy_type=insider_analyst (historical only)",
                "memories": ["signals/scoring_accuracy_by_band", "signals/threshold_sweep_latest",
                             "signals/false_positives"]
            },
            "read_by": [],
            "schedule": "archived"
        },
        "strategies": {
            "owner": "strategy_reviewer",
            "purpose": "Plugin strategy performance analysis, win_prob calibration, bleeding detection",
            "writes_to": {
                "observations": "strategy_type=strategy_reviewer",
                "memories": ["strategies/plugin_performance_snapshot", "strategies/winprob_calibration",
                             "strategies/bleeding_watchlist"]
            },
            "read_by": ["all agents (cross-read in Step 1)"],
            "schedule": "twice weekly (Wed + Sat 15:00 UTC)"
        },
        "predictions": {
            "owner": "all agents",
            "purpose": "Self-improvement — verifiable predictions, lessons, accuracy tracking",
            "writes_to": {
                "tables": ["agent_predictions", "agent_lessons", "agent_runs"]
            },
            "read_by": ["each agent reads own predictions/lessons", "health_checker (grading backlog)"],
            "schedule": "every agent run"
        },
        "fast_ooda": {
            "owner": "fast_ooda (scripts/fast_ooda.py)",
            "purpose": "Deterministic fast-path event matcher — keyword news-to-position matching, micro-boosts",
            "writes_to": {
                "memories": ["market_boosts_current (source=fast_ooda entries)", "fast_ooda_last_run"]
            },
            "read_by": ["paper_trader (boosts)", "strategy_runner (boosts)", "health_checker"],
            "schedule": "every 15min via cron"
        },
        "health": {
            "owner": "brain_health_checker",
            "purpose": "System validation — staleness, heartbeats, coherence, integrity",
            "writes_to": {
                "memories": ["brain_health_latest"]
            },
            "read_by": ["all agents (Step 1 context loading)"],
            "schedule": "every 6h (00:30, 06:30, 12:30, 18:30 UTC)"
        },
        "knowledge": {
            "owner": "static (seeded once)",
            "purpose": "Domain knowledge — scoring theory, market microstructure, strategy frameworks",
            "writes_to": {
                "memories": "~30 knowledge entries seeded by enrich_brain.py, inject_*.py"
            },
            "read_by": ["agents as needed (browsing context)"],
            "schedule": "manual seeding only"
        },
        "meta": {
            "owner": "human",
            "purpose": "Behavioral instructions for Claude, user preferences",
            "writes_to": {
                "memories": "feedback + user type entries"
            },
            "read_by": ["Claude Code sessions"],
            "schedule": "manual only"
        }
    },
    "data_flows": {
        "working": [
            "geopolitics_analyst -> market_boosts_current -> paper_trader (entry gate + sizing)",
            "geopolitics_analyst -> market_boosts_current -> strategy_runner (entry gate + sizing)",
            "fast_ooda -> market_boosts_current (source=fast_ooda, +/-15, 2h expiry) -> paper_trader + strategy_runner",
            "finance_analyst -> trade_ideas (SQLite) -> news_intel_v1 plugin -> strategy_positions",
            "news_intel -> trade_ideas (SQLite) -> news_intel_v1 plugin -> strategy_positions"
        ],
        "cross_domain": [
            "geopolitics_analyst --domain_query--> finance (reads markets/ brain section)",
            "finance_analyst --domain_query--> geopolitics (reads world_state/ brain section)",
            "geopolitics_analyst --flag--> finance (urgent: oil/sanctions/supply disruptions)",
            "finance_analyst --flag--> geopolitics (urgent: economic pressure affecting political outcomes)"
        ],
        "cross_agent_reads": [
            "wallet_scout reads insider_copy latest diagnosis (position health context)",
            "insider_copy reads wallet_scout latest run_summary (active strategy roster)",
            "insider_analyst reads insider_copy diagnosis (position context for calibration)",
            "strategy_reviewer reads insider_analyst diagnosis (scoring accuracy context)",
            "all agents read brain_health_latest (system status awareness)",
            "all agents read convergence:latest-scan (market alignment)",
            "all agents read conflict:latest-scan (position risk)"
        ]
    }
}

def main():
    # 1. Upsert the manifest
    upsert_memory(
        type="reference",
        name="brain_manifest",
        description="Brain structure manifest — sections, ownership, data flows",
        content=json.dumps(MANIFEST, indent=2),
        source="vps",
    )
    print("brain_manifest upserted")

    # 2. Clean up legacy world_event:* memories (now in world_events table)
    mems = read_memories(type_filter="reference")
    deleted = 0
    for m in mems:
        if m.get("name", "").startswith("world_event:"):
            try:
                _request("DELETE", f"memories?id=eq.{m['id']}")
                deleted += 1
            except Exception as e:
                print(f"  WARNING: Failed to delete {m['name']}: {e}")
    print(f"Cleaned up {deleted} legacy world_event:* memories")

if __name__ == "__main__":
    main()
