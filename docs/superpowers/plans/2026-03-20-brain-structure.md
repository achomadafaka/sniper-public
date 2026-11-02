# Brain Section Structure — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Structure the Supabase brain with clear namespaces, automate stale data flows, wire up dead-end reads, add selective cross-agent awareness, and create a manifest that makes every agent conscious of its place in the system.

**Architecture:** No schema changes. Naming convention + brain manifest entry + agent prompt updates + one new timer for sync-state. The brain already has natural sections — we formalize them and connect the dead wires.

**Tech Stack:** Python 3.12, Supabase REST API, systemd timers

---

## Key Findings from Data Flow Audit

**3 working flows** (affect trading): market_boosts → paper trader/strategy runner, trade_ideas → news_intel plugin
**3 dead-end writes** (nobody reads): brain feedback, health results, convergence/conflict scans
**1 siloed system** (no cross-reads): agent observations
**1 never automated** (manual only): sync-state

This plan fixes all of these.

---

## Chunk 1: Brain Manifest + Agent Awareness

### Task 1: Create brain_manifest and clean up legacy

**Files:**
- Create: `scripts/brain_manifest.py` (one-time script to upsert the manifest + clean up)

- [ ] **Step 1: Write the manifest script**

The manifest is a JSON memory entry that documents the entire brain structure. It also cleans up the 11 stale `world_event:*` memories.

```python
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
            "owner": "world_analyst",
            "purpose": "World intelligence — events, market boosts, geopolitical context",
            "writes_to": {
                "memories": ["world_state", "market_boosts_current"],
                "tables": ["world_events", "industry_impacts", "market_opportunities"]
            },
            "read_by": ["paper_trader (boosts)", "strategy_runner (boosts)", "intel_tab (events)"],
            "schedule": "every 2h (news_intel timer) + every 4h (world_analyst cron)"
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
            "owner": "insider_copy",
            "purpose": "Position quality analysis, PnL patterns, parameter tuning",
            "writes_to": {
                "observations": "strategy_type=insider_copy_v1"
            },
            "read_by": ["wallet_scout (reads insider_copy diagnosis for position context)"],
            "schedule": "twice daily (06:00 + 18:00 UTC)"
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
            "world_analyst → market_boosts_current → paper_trader (entry gate + sizing)",
            "world_analyst → market_boosts_current → strategy_runner (entry gate + sizing)",
            "news_intel → trade_ideas (SQLite) → news_intel_v1 plugin → strategy_positions"
        ],
        "cross_agent_reads": [
            "wallet_scout reads insider_copy latest diagnosis (position health context)",
            "insider_copy reads wallet_scout latest run_summary (active strategy roster)",
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
    print("✅ brain_manifest upserted")

    # 2. Clean up legacy world_event:* memories (now in world_events table)
    mems = read_memories(type_filter="reference")
    deleted = 0
    for m in mems:
        if m.get("name", "").startswith("world_event:"):
            try:
                _request("DELETE", f"memories?id=eq.{m['id']}")
                deleted += 1
            except Exception as e:
                print(f"  ⚠️ Failed to delete {m['name']}: {e}")
    print(f"✅ Cleaned up {deleted} legacy world_event:* memories")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it on VPS**
- [ ] **Step 3: Commit**

---

### Task 2: Automate sync-state (portfolio section)

**Files:**
- Create: `systemd/sniper-sync-state.service`
- Create: `systemd/sniper-sync-state.timer`

The sync-state command writes Strategy Performance, Wallet Analysis, convergence scans, and conflict scans. Currently manual-only. Put it on a 30-min timer.

- [ ] **Step 1: Create service**

```ini
[Unit]
Description=Sniper Brain Sync State (portfolio + cross-layer intelligence)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/home/sniper/sniper
EnvironmentFile=-/home/sniper/sniper/.env
ExecStart=/home/sniper/sniper/.venv/bin/python3 /home/sniper/sniper/bot/supabase_helper.py sync-state
StandardOutput=append:/home/sniper/sniper/logs/sync_state.log
StandardError=append:/home/sniper/sniper/logs/sync_state.log
TimeoutStartSec=60
Restart=no

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create timer**

```ini
[Unit]
Description=Timer — sync brain portfolio state every 30 minutes
Requires=sniper-sync-state.service

[Timer]
OnCalendar=*-*-* *:00,30:00
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Deploy to VPS**
- [ ] **Step 4: Commit**

---

### Task 3: Add Brain Map + cross-reads to agent prompts

**Files:**
- Modify: `agents/wallet_scout_agent.py` (system prompt)
- Modify: `agents/insider_copy_agent.py` (system prompt)
- Modify: `scripts/world_analyst.py` (system prompt)

Add a BRAIN MAP section to each agent's system prompt and update Step 1 to read cross-agent data.

**For ALL three agents, add this block after the existing tool sections in the system prompt:**

```
═══ BRAIN MAP ═══
You are part of a multi-agent system. Each agent owns a section of the shared brain (Supabase).

  SECTION          OWNER             YOU
  world_state/*    World Analyst      {READ/WRITE depending on agent}
  portfolio/*      System (auto)      READ — convergence:latest-scan, conflict:latest-scan, Strategy Performance
  wallets/*        Wallet Scout       {READ/WRITE depending on agent}
  signals/*        Insider Copy       {READ/WRITE depending on agent}
  predictions/*    All agents         WRITE (own agent_type only)
  health/*         Health Checker     READ — brain_health_latest
  knowledge/*      Static (seeded)    READ-ONLY
  meta/*           Human              READ-ONLY

CROSS-READS (do these in Step 1):
  python3 -c "from bot.supabase_helper import get_memory; import json; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; import json; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no convergence data')"
  python3 -c "from bot.supabase_helper import get_memory; import json; m=get_memory('conflict:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no conflict data')"
```

**Agent-specific additions to the Brain Map:**

For **Wallet Scout**: add read of insider_copy's latest diagnosis:
```
  python3 agents/tools/obs_tool.py read --strategy insider_copy_v1 --type diagnosis --limit 1
```

For **Insider Copy**: add read of wallet_scout's latest run_summary:
```
  python3 agents/tools/obs_tool.py read --strategy wallet_scout --type run_summary --limit 1
```

For **World Analyst**: add read of portfolio convergence + conflict data (already has access via Bash).

**Update each agent's Step 1 (Load memory)** to include these cross-reads. The agent should note any health warnings, convergence signals, or conflicts relevant to its work.

- [ ] **Step 1: Update wallet_scout_agent.py**
- [ ] **Step 2: Update insider_copy_agent.py**
- [ ] **Step 3: Update world_analyst.py**
- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

---

## Chunk 2: Verification

### Task 4: Verify the full structure works

- [ ] **Step 1: Push and deploy all changes**
- [ ] **Step 2: Run brain_manifest.py on VPS**
- [ ] **Step 3: Verify manifest exists in Supabase**
- [ ] **Step 4: Verify legacy world_event:* memories deleted**
- [ ] **Step 5: Verify sync-state timer is running**
- [ ] **Step 6: Run brain health check — E1 dual-path should improve**
- [ ] **Step 7: Verify agent prompts are updated**
