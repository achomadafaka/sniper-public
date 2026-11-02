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
        "memory_keys": ["world_state", "world_narrative", "market_boosts_current"],
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
            # Parse JSON content directly (world_state is compact JSON); fall back to truncation
            raw_content = mem.get("content", "")
            try:
                parsed = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
                content_preview: object = parsed
            except (json.JSONDecodeError, TypeError):
                content_preview = str(raw_content)[:500]
            memories.append({
                "name": mem["name"],
                "updated": str(mem.get("updated_at", ""))[:19],
                "age_hours": round(age_h, 1),
                "stale": age_h > 6,
                "content_preview": content_preview,
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
        directed = [f for f in flags if f.get("name", "").startswith(f"flag:") and f"\u2192{domain}" in f.get("name", "")]
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
    flag_name = f"flag:{from_domain}\u2192{to_domain}:{slug}"

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
        description=f"Cross-domain flag: {from_domain}\u2192{to_domain} ({urgency})",
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
        if not name.startswith("flag:") or f"\u2192{domain}" not in name:
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
