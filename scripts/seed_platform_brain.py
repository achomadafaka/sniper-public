#!/usr/bin/env python3
"""Seed Supabase brain with multi-engine platform architecture knowledge.

Run on VPS after deploying Phase 1:
    python3 scripts/seed_platform_brain.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_env_path = os.path.join(ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

from bot.supabase_helper import upsert_memory

now = datetime.now(timezone.utc).isoformat()

ENTRIES = [
    {
        "name": "architecture/multi_engine_platform",
        "description": "Multi-engine strategy platform: 3 lanes (copy, native PM, Hyperliquid) + Algorithms UI",
        "content": {
            "timestamp": now,
            "version": "1.0",
            "status": "phase_1_complete",
            "lanes": {
                "copy": "Improved Polymarket copy — real-time ingest, latency, viability scoring",
                "native_polymarket": "Standardized research→paper→promote lifecycle",
                "native_crypto": "Hyperliquid perps — liquidation reaction, momentum, wallet follow",
            },
            "ui": "Algorithms tab: Live Engines, Signal Queue, Execution Monitor, Edge Board",
            "hard_boundary": "Paper/research only. Live HL execution gated on paper evidence.",
            "new_tables": ["hl_trades", "hl_liquidations", "hl_positions", "hl_fills", "signal_queue", "copy_viability"],
            "new_files": ["db_hyperliquid.py", "feeds/hyperliquid.py", "risk/models.py (venue field)", "tests/test_venue_risk.py"],
        },
    },
    {
        "name": "architecture/venue_risk_model",
        "description": "Venue-aware risk: Polymarket (binary) vs Hyperliquid (leveraged perps) with separate limits",
        "content": {
            "timestamp": now,
            "polymarket": "7 ordered checks, price [0,1], leverage=1",
            "hyperliquid": {
                "additional_checks": ["venue_enabled", "venue_exposure", "leverage_limit", "venue_concurrent", "asset_concentration"],
                "env_vars": ["INSTANCE_HL_MAX_EXPOSURE_USDC=0", "INSTANCE_HL_MAX_LEVERAGE=3.0", "INSTANCE_HL_MAX_CONCURRENT=5"],
            },
        },
    },
    {
        "name": "architecture/signal_queue",
        "description": "Unified cross-venue signal instrumentation: discovery→entry/rejection tracking",
        "content": {
            "timestamp": now,
            "lifecycle": {"pending": "discovered", "entered": "position opened", "rejected": "gate blocked", "expired": "aged out"},
            "reject_reasons": ["cross_strategy_dedup", "plugin_enter", "adversarial_gate", "conviction_gate", "world_boost_negative", "risk:*"],
            "use_cases": ["Algorithms UI", "copy viability scoring", "signal quality metrics"],
        },
    },
    {
        "name": "strategy/hl_liquidation_proximity",
        "description": "HL liquidation proximity strategy: track whale positions, detect near-liquidation, counter-trade cascades",
        "content": {
            "timestamp": now,
            "status": "phase_5a_deployed",
            "concept": "Track whale positions via public clearinghouseState API. When close to liquidation, enter counter-position.",
            "api": "POST https://api.hyperliquid.xyz/info — no auth, fully public",
            "distance_formula": {
                "isolated": "abs(mark - liqPx) / mark * 100",
                "cross": "(accountValue - crossMMUsed) / accountValue * 100",
            },
            "tables": ["hl_tracked_wallets", "hl_whale_positions"],
            "feed": "hl_whale (poll-mode, 60s)",
            "next": "Phase 5B: detection engine + hl_liquidation_v1 plugin",
        },
    },
    {
        "name": "architecture/ws_reconciliation",
        "description": "WS followed-wallet reconciliation: synthetic ws: hashes with strict matching",
        "content": {
            "timestamp": now,
            "status": "deployed",
            "synthetic_hash": "ws:<sha1(wallet|cond|outcome|side|type|raw_ms|price|size)>",
            "reconciliation": "Strict match: same wallet/cond/outcome/side/type, +/-2s, price/size at 6 decimals",
            "ambiguity": "Multiple matches -> no merge, insert canonical, log warning",
        },
    },
]

for entry in ENTRIES:
    content = json.dumps(entry["content"], indent=2) if isinstance(entry["content"], dict) else entry["content"]
    result = upsert_memory(
        type="knowledge",
        name=entry["name"],
        description=entry["description"],
        content=content,
        source="local",
    )
    print(f"Stored: {entry['name']}")

print(f"\nDone — {len(ENTRIES)} brain entries seeded.")
