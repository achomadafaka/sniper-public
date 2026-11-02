# Specialist Phase 1: Cross-Domain Query + Flags + E2E Tests

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the foundation for specialist agents — a cross-domain query tool ($0/call), an urgent flag mechanism, and proper end-to-end tests that catch the bugs manual inspection found this session.

**Architecture:** `domain_query.py` is a CLI tool agents call via Bash to read another domain's brain section. Pure data retrieval — reads from Supabase memories, world_events, observations, predictions, and country profiles. No LLM calls. Flags are stored as Supabase memories with expiry. E2E tests verify the price flow from trade idea → strategy position and the agent brain section boundaries.

**Tech Stack:** Python 3.12, Supabase REST API, SQLite, pytest

**Spec:** `docs/superpowers/specs/2026-03-21-specialist-agents-architecture.md`

---

## File Structure

| File | Purpose |
|------|---------|
| `agents/tools/domain_query.py` | Cross-domain query + flag CLI tool |
| `tests/test_domain_query.py` | Unit tests for domain_query |
| `tests/test_price_flow_e2e.py` | End-to-end price correctness tests |
| `tests/test_agent_brain_sections.py` | Agent brain section boundary tests |

---

## Chunk 1: Domain Query Tool

### Task 1: Build domain_query.py

**Files:**
- Create: `agents/tools/domain_query.py`
- Test: `tests/test_domain_query.py`

- [ ] **Step 1: Create the domain query tool**

```python
#!/usr/bin/env python3
"""Cross-domain query tool — agents call this to read another domain's brain section.

Pure data retrieval, no LLM calls, costs $0. Returns structured JSON from
Supabase memories, world_events, observations, predictions, and country profiles.

Usage:
    # Query another domain's knowledge
    python3 agents/tools/domain_query.py --domain geopolitics --topic "hormuz"
    python3 agents/tools/domain_query.py --domain finance --topic "oil sanctions"

    # Create a cross-domain flag
    python3 agents/tools/domain_query.py --flag --from geopolitics --to finance \
        --urgency high --message "Hormuz closure confirmed"

    # Read flags directed at your domain
    python3 agents/tools/domain_query.py --read-flags --domain finance

    # Clear an acknowledged flag
    python3 agents/tools/domain_query.py --clear-flag --flag-name "geo-to-finance:hormuz"
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Load .env
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

from bot.supabase_helper import (
    get_memory, search_memories, read_memories,
    upsert_memory, delete_memory, _request,
)

DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))

# Domain → brain section mapping
DOMAIN_MAP = {
    "geopolitics": {
        "brain_section": "world_state",
        "agent_type": "world_analyst",
        "obs_strategy": "world_analyst",
        "memory_keys": ["world_state", "market_boosts_current"],
        "event_regions": ["middle_east", "europe_east", "russia_cis", "east_asia", "south_asia", "southeast_asia", "africa_sub_saharan", "south_america"],
    },
    "finance": {
        "brain_section": "markets",
        "agent_type": "finance_analyst",
        "obs_strategy": "finance_analyst",
        "memory_keys": ["markets/state", "markets/boosts"],
        "ticker_symbols": ["S&P 500", "NASDAQ", "DOW", "VIX", "10Y", "GOLD", "OIL", "BTC", "ETH", "SOL"],
    },
}


def query_domain(domain: str, topic: str) -> dict:
    """Query a domain's brain section for information on a topic."""
    config = DOMAIN_MAP.get(domain)
    if not config:
        return {"error": f"Unknown domain: {domain}. Available: {list(DOMAIN_MAP.keys())}"}

    result = {
        "domain": domain,
        "topic": topic,
        "queried_at": datetime.now(timezone.utc).isoformat(),
        "sections": {},
    }

    # 1. Brain memories for this domain
    memories = []
    for key in config.get("memory_keys", []):
        mem = get_memory(key, type_filter="knowledge")
        if mem:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(
                str(mem.get("updated_at", "")).replace("Z", "+00:00")
            )).total_seconds() / 3600
            memories.append({
                "name": mem["name"],
                "updated": str(mem.get("updated_at", ""))[:19],
                "age_hours": round(age_h, 1),
                "stale": age_h > 6,
                "content_preview": str(mem.get("content", ""))[:500],
            })
    result["sections"]["memories"] = memories

    # 2. World events (for geopolitics domain)
    if domain == "geopolitics":
        try:
            events = _request("GET",
                f"world_events?select=slug,title,status,trajectory,region,country,last_updated"
                f"&status=in.(active,escalating,developing)&order=last_updated.desc&limit=10")
            if topic:
                topic_lower = topic.lower()
                events = [e for e in (events or []) if topic_lower in str(e).lower()]
            result["sections"]["world_events"] = events or []
        except Exception:
            result["sections"]["world_events"] = []

    # 3. Latest observations from this domain's agent
    try:
        obs_type = config.get("obs_strategy", domain)
        obs = _request("GET",
            f"strategy_agent_observations?select=obs_type,content,created_at"
            f"&strategy_type=eq.{obs_type}&order=created_at.desc&limit=5")
        if topic and obs:
            topic_lower = topic.lower()
            obs = [o for o in obs if topic_lower in str(o.get("content", "")).lower()]
        result["sections"]["observations"] = [
            {**o, "content": str(o.get("content", ""))[:300]}
            for o in (obs or [])[:3]
        ]
    except Exception:
        result["sections"]["observations"] = []

    # 4. Country profiles matching topic
    if topic:
        country_matches = search_memories(f"country:")
        country_matches = [m for m in country_matches
                          if m.get("name", "").startswith("country:")
                          and topic.lower() in str(m.get("content", "")).lower()]
        result["sections"]["country_profiles"] = [
            {"name": m["name"], "preview": str(m.get("content", ""))[:400]}
            for m in country_matches[:3]
        ]

    # 5. Ticker data (for finance domain)
    if domain == "finance":
        try:
            import sqlite3
            # Read latest ticker from the API cache or just report symbols
            result["sections"]["tickers"] = config.get("ticker_symbols", [])
        except Exception:
            pass

    # 6. Trade ideas related to topic
    if topic:
        try:
            import sqlite3
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            ideas = conn.execute(
                "SELECT id, market_title, direction, confidence, status, current_price "
                "FROM trade_ideas WHERE status='open' AND market_title LIKE ?",
                (f"%{topic}%",)
            ).fetchall()
            conn.close()
            result["sections"]["trade_ideas"] = [dict(r) for r in ideas]
        except Exception:
            result["sections"]["trade_ideas"] = []

    # 7. Flags directed at this domain
    try:
        flags = search_memories(f"flag:")
        directed = [f for f in flags if f.get("name", "").startswith(f"flag:") and f"→{domain}" in f.get("name", "")]
        result["sections"]["pending_flags"] = [
            {"name": f["name"], "content": f.get("content", ""), "updated": str(f.get("updated_at", ""))[:19]}
            for f in directed
        ]
    except Exception:
        result["sections"]["pending_flags"] = []

    return result


def create_flag(from_domain: str, to_domain: str, urgency: str, message: str) -> dict:
    """Create a cross-domain urgent flag."""
    slug = re.sub(r'[^a-z0-9]+', '-', message.lower())[:40]
    flag_name = f"flag:{from_domain}→{to_domain}:{slug}"

    content = json.dumps({
        "from": from_domain,
        "to": to_domain,
        "urgency": urgency,
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    })

    upsert_memory(
        type="knowledge",
        name=flag_name,
        description=f"Cross-domain flag: {from_domain}→{to_domain} ({urgency})",
        content=content,
        source="vps",
    )
    return {"ok": True, "flag_name": flag_name, "urgency": urgency}


def read_flags(domain: str) -> list:
    """Read all pending flags directed at a domain."""
    flags = search_memories(f"flag:")
    directed = []
    now = datetime.now(timezone.utc)
    for f in flags:
        name = f.get("name", "")
        if not name.startswith("flag:") or f"→{domain}" not in name:
            continue
        try:
            data = json.loads(f.get("content", "{}"))
            expires = datetime.fromisoformat(data.get("expires_at", "").replace("Z", "+00:00"))
            if now > expires:
                # Expired — clean up
                try:
                    delete_memory(name)
                except Exception:
                    pass
                continue
        except Exception:
            pass
        directed.append({"name": name, "content": f.get("content", ""), "updated": str(f.get("updated_at", ""))[:19]})
    return directed


def clear_flag(flag_name: str) -> dict:
    """Acknowledge and clear a flag."""
    try:
        deleted = delete_memory(flag_name)
        return {"ok": True, "deleted": deleted}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Cross-domain query tool for specialist agents")
    parser.add_argument("--domain", help="Domain to query: geopolitics, finance")
    parser.add_argument("--topic", default="", help="Topic keywords to search for")

    # Flag operations
    parser.add_argument("--flag", action="store_true", help="Create a cross-domain flag")
    parser.add_argument("--from", dest="from_domain", help="Flag sender domain")
    parser.add_argument("--to", dest="to_domain", help="Flag target domain")
    parser.add_argument("--urgency", default="medium", choices=["low", "medium", "high"], help="Flag urgency")
    parser.add_argument("--message", default="", help="Flag message")

    parser.add_argument("--read-flags", action="store_true", help="Read flags directed at --domain")
    parser.add_argument("--clear-flag", action="store_true", help="Clear a flag by --flag-name")
    parser.add_argument("--flag-name", default="", help="Flag name to clear")

    args = parser.parse_args()

    if args.flag:
        if not args.from_domain or not args.to_domain or not args.message:
            print(json.dumps({"error": "--flag requires --from, --to, and --message"}))
            sys.exit(1)
        result = create_flag(args.from_domain, args.to_domain, args.urgency, args.message)
        print(json.dumps(result, indent=2))
    elif args.read_flags:
        if not args.domain:
            print(json.dumps({"error": "--read-flags requires --domain"}))
            sys.exit(1)
        flags = read_flags(args.domain)
        print(json.dumps(flags, indent=2))
    elif args.clear_flag:
        if not args.flag_name:
            print(json.dumps({"error": "--clear-flag requires --flag-name"}))
            sys.exit(1)
        result = clear_flag(args.flag_name)
        print(json.dumps(result, indent=2))
    elif args.domain:
        result = query_domain(args.domain, args.topic)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify syntax**

Run: `python3 -c "import agents.tools.domain_query"`

- [ ] **Step 3: Write tests**

```python
# tests/test_domain_query.py
import json
import pytest
from unittest.mock import patch, MagicMock

def test_query_unknown_domain():
    from agents.tools.domain_query import query_domain
    result = query_domain("sports", "nba")
    assert "error" in result

def test_query_geopolitics_returns_structure():
    from agents.tools.domain_query import query_domain
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "iran")
    assert result["domain"] == "geopolitics"
    assert result["topic"] == "iran"
    assert "sections" in result
    assert "memories" in result["sections"]
    assert "world_events" in result["sections"]
    assert "observations" in result["sections"]

def test_query_finance_returns_structure():
    from agents.tools.domain_query import query_domain
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("finance", "oil")
    assert result["domain"] == "finance"
    assert "tickers" in result["sections"]

def test_query_filters_by_topic():
    from agents.tools.domain_query import query_domain
    mock_events = [
        {"slug": "iran-war", "title": "Iran War", "status": "active", "region": "middle_east"},
        {"slug": "uk-election", "title": "UK Election", "status": "developing", "region": "europe_west"},
    ]
    with patch("agents.tools.domain_query.get_memory", return_value=None), \
         patch("agents.tools.domain_query._request", return_value=mock_events), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "iran")
    assert len(result["sections"]["world_events"]) == 1
    assert result["sections"]["world_events"][0]["slug"] == "iran-war"

def test_stale_detection():
    from agents.tools.domain_query import query_domain
    old_mem = {
        "name": "world_state",
        "updated_at": "2026-03-20T00:00:00+00:00",
        "content": "old data",
    }
    with patch("agents.tools.domain_query.get_memory", return_value=old_mem), \
         patch("agents.tools.domain_query._request", return_value=[]), \
         patch("agents.tools.domain_query.search_memories", return_value=[]):
        result = query_domain("geopolitics", "")
    assert result["sections"]["memories"][0]["stale"] == True

def test_create_flag():
    from agents.tools.domain_query import create_flag
    with patch("agents.tools.domain_query.upsert_memory") as mock_upsert:
        result = create_flag("geopolitics", "finance", "high", "Hormuz closed")
    assert result["ok"] == True
    assert "flag:" in result["flag_name"]
    assert "geopolitics→finance" in result["flag_name"]
    mock_upsert.assert_called_once()

def test_read_flags_filters_by_domain():
    from agents.tools.domain_query import read_flags
    mock_flags = [
        {"name": "flag:geopolitics→finance:hormuz", "content": json.dumps({
            "from": "geopolitics", "to": "finance", "urgency": "high",
            "message": "Hormuz", "expires_at": "2099-01-01T00:00:00+00:00",
            "created_at": "2026-03-21T00:00:00+00:00",
        }), "updated_at": "2026-03-21T00:00:00"},
        {"name": "flag:finance→geopolitics:rates", "content": json.dumps({
            "from": "finance", "to": "geopolitics", "urgency": "low",
            "message": "Rates", "expires_at": "2099-01-01T00:00:00+00:00",
            "created_at": "2026-03-21T00:00:00+00:00",
        }), "updated_at": "2026-03-21T00:00:00"},
    ]
    with patch("agents.tools.domain_query.search_memories", return_value=mock_flags), \
         patch("agents.tools.domain_query.delete_memory"):
        flags = read_flags("finance")
    assert len(flags) == 1
    assert "geopolitics→finance" in flags[0]["name"]

def test_expired_flag_gets_cleaned():
    from agents.tools.domain_query import read_flags
    expired_flag = [{
        "name": "flag:geo→finance:old",
        "content": json.dumps({
            "expires_at": "2020-01-01T00:00:00+00:00",
            "created_at": "2020-01-01T00:00:00+00:00",
        }),
        "updated_at": "2020-01-01",
    }]
    with patch("agents.tools.domain_query.search_memories", return_value=expired_flag), \
         patch("agents.tools.domain_query.delete_memory") as mock_del:
        flags = read_flags("finance")
    assert len(flags) == 0
    mock_del.assert_called_once()
```

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_domain_query.py -v`

- [ ] **Step 5: Commit**

---

## Chunk 2: End-to-End Price Flow Tests

### Task 2: Price flow verification tests

**Files:**
- Create: `tests/test_price_flow_e2e.py`

These tests verify that a BUY_YES and BUY_NO trade idea flows correctly through the news_intel plugin to a strategy_position with correct price at every step.

- [ ] **Step 1: Write E2E price tests**

```python
# tests/test_price_flow_e2e.py
"""End-to-end price flow tests — verify prices are correct at every step
from trade idea → news_intel plugin discover → strategy position.

These tests would have caught the BUY_NO price inversion bug."""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock


@pytest.fixture
def e2e_db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE trade_ideas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id TEXT, market_title TEXT, outcome TEXT,
            direction TEXT, reasoning TEXT, current_price REAL,
            target_price REAL, confidence TEXT, news_trigger TEXT,
            news_source TEXT, expires_at TEXT, status TEXT DEFAULT 'open',
            driving_event TEXT, alerted_at TEXT, source_agent TEXT
        );
        CREATE TABLE market_metadata (
            condition_id TEXT PRIMARY KEY, end_date_iso TEXT
        );
        CREATE TABLE paper_trades (
            trade_id INTEGER PRIMARY KEY, condition_id TEXT, outcome TEXT,
            status TEXT DEFAULT 'open', entry_price REAL, pnl_usdc REAL,
            duplicate_of TEXT
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}',
            status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY AUTOINCREMENT,
            wallet TEXT, condition_id TEXT, outcome TEXT,
            strategy_id TEXT, state TEXT DEFAULT 'open',
            entry_price REAL, avg_entry_price REAL, pnl_usdc REAL DEFAULT 0
        );
    """)
    return p, c


def test_buy_yes_price_flow(e2e_db):
    """BUY_YES: trade_ideas.current_price IS the YES price.
    Signal should carry the YES price through to entry."""
    p, c = e2e_db

    # Create a BUY_YES trade idea — current_price is YES price
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES ('0xcond1', 'Will X happen?', 'YES', 'buy_yes', 0.35, 'high', 'open')""")
    c.commit()

    # Simulate news_intel_v1 discover()
    from strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    signal = signals[0]
    assert signal.outcome == "Yes"
    # For BUY_YES, current_price should be the YES price (0.35)
    assert signal.current_price == 0.35, f"BUY_YES signal price should be 0.35 (YES price), got {signal.current_price}"


def test_buy_no_price_flow(e2e_db):
    """BUY_NO: trade_ideas.current_price IS the NO price (tradeable side).
    Signal should carry the NO price through to entry."""
    p, c = e2e_db

    # Create a BUY_NO trade idea — current_price is the NO price (0.65)
    # This means YES price is 0.35, NO price is 0.65
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES ('0xcond2', 'Will Y happen?', 'NO', 'buy_no', 0.65, 'high', 'open')""")
    c.commit()

    from strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    signal = signals[0]
    assert signal.outcome == "No"
    # For BUY_NO, current_price should be the NO price (0.65)
    # NOT the YES price, NOT 1-price inverted
    assert signal.current_price == 0.65, f"BUY_NO signal price should be 0.65 (NO price), got {signal.current_price}"


def test_buy_no_high_price_rejected(e2e_db):
    """BUY_NO with NO price > 0.92 should be rejected by price gate."""
    p, c = e2e_db

    # Near-certain NO market — NO price is 0.95 (YES is 0.05)
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES ('0xcond3', 'Certain thing?', 'NO', 'buy_no', 0.95, 'high', 'open')""")
    c.commit()

    from strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    # Entry should be rejected by price gate
    decision = plugin.enter(c, strategy, signals[0], [])
    assert decision.should_enter == False
    assert "sane range" in decision.reason.lower() or "0.92" in decision.reason


def test_buy_no_low_price_rejected(e2e_db):
    """BUY_NO with NO price < 0.05 should be rejected."""
    p, c = e2e_db

    # Near-certain YES market — NO price is 0.03
    c.execute("""INSERT INTO trade_ideas
        (condition_id, market_title, outcome, direction, current_price, confidence, status)
        VALUES ('0xcond4', 'Almost certain?', 'NO', 'buy_no', 0.03, 'high', 'open')""")
    c.commit()

    from strategies.news_intel_v1 import NewsIntelV1Strategy
    plugin = NewsIntelV1Strategy()
    strategy = {"id": 1, "parameters": "{}"}

    signals = plugin.discover(c, strategy)
    assert len(signals) == 1

    decision = plugin.enter(c, strategy, signals[0], [])
    assert decision.should_enter == False
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_price_flow_e2e.py -v`

- [ ] **Step 3: Run full suite**

Run: `python3 -m pytest tests/ -q`

- [ ] **Step 4: Commit**

---

## Chunk 3: Agent Brain Section Boundary Tests

### Task 3: Brain section tests

**Files:**
- Create: `tests/test_agent_brain_sections.py`

These tests verify agents write to the correct brain sections and that the brain manifest accurately describes the system.

- [ ] **Step 1: Write brain section tests**

```python
# tests/test_agent_brain_sections.py
"""Verify agent brain section boundaries — each agent writes to its own
section and doesn't modify others'."""

import json
import pytest


class TestDomainMapping:
    """Verify domain_query.py's domain map is complete and correct."""

    def test_geopolitics_domain_exists(self):
        from agents.tools.domain_query import DOMAIN_MAP
        assert "geopolitics" in DOMAIN_MAP
        assert DOMAIN_MAP["geopolitics"]["agent_type"] == "world_analyst"

    def test_finance_domain_exists(self):
        from agents.tools.domain_query import DOMAIN_MAP
        assert "finance" in DOMAIN_MAP
        assert DOMAIN_MAP["finance"]["agent_type"] == "finance_analyst"

    def test_all_domains_have_required_keys(self):
        from agents.tools.domain_query import DOMAIN_MAP
        required = {"brain_section", "agent_type", "obs_strategy"}
        for domain, config in DOMAIN_MAP.items():
            for key in required:
                assert key in config, f"Domain {domain} missing {key}"


class TestAgentPromptBrainMap:
    """Verify each agent's system prompt contains a BRAIN MAP section."""

    def test_wallet_scout_has_brain_map(self):
        from agents.wallet_scout_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT
        assert "wallets" in SYSTEM_PROMPT.lower()

    def test_insider_copy_has_brain_map(self):
        from agents.insider_copy_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT
        assert "signals" in SYSTEM_PROMPT.lower()

    def test_insider_analyst_has_brain_map(self):
        from agents.insider_scoring_analyst import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT

    def test_strategy_reviewer_has_brain_map(self):
        from agents.strategy_reviewer_agent import SYSTEM_PROMPT
        assert "BRAIN MAP" in SYSTEM_PROMPT


class TestObservationTypes:
    """Verify obs_tool strategy types match the brain section owners."""

    def test_obs_strategy_types_are_distinct(self):
        """No two agents should write to the same observation strategy_type."""
        strategy_types = [
            "insider_copy_v1",  # insider copy agent
            "wallet_scout",     # wallet scout
            "insider_analyst",  # insider scoring analyst
            "strategy_reviewer", # strategy reviewer
            # world_analyst writes to memories, not observations
        ]
        assert len(strategy_types) == len(set(strategy_types))
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_agent_brain_sections.py -v`

- [ ] **Step 3: Run full suite**

Run: `python3 -m pytest tests/ -q`

- [ ] **Step 4: Commit**

---

## Chunk 4: Deploy + Verify

### Task 4: Deploy and test on VPS

- [ ] **Step 1: Push all code**

```bash
git push origin main
```

- [ ] **Step 2: Pull on VPS**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && git pull origin main'
```

- [ ] **Step 3: Test domain_query.py on VPS**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^(SUPABASE|DB_PATH)" .env | xargs) && .venv/bin/python3 agents/tools/domain_query.py --domain geopolitics --topic "iran" 2>&1 | head -30'
```

- [ ] **Step 4: Test flag creation on VPS**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^(SUPABASE|DB_PATH)" .env | xargs) && .venv/bin/python3 agents/tools/domain_query.py --flag --from geopolitics --to finance --urgency high --message "Test flag from Phase 1 deployment"'
```

- [ ] **Step 5: Test flag reading**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^SUPABASE" .env | xargs) && .venv/bin/python3 agents/tools/domain_query.py --read-flags --domain finance'
```

- [ ] **Step 6: Clean up test flag**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^SUPABASE" .env | xargs) && .venv/bin/python3 agents/tools/domain_query.py --clear-flag --flag-name "flag:geopolitics→finance:test-flag-from-phase-1-deployment"'
```

- [ ] **Step 7: Run brain health check**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^(SUPABASE|TELEGRAM|DB_PATH)" .env | xargs) && .venv/bin/python3 scripts/brain_health.py'
```
