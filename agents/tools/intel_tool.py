#!/usr/bin/env python3
"""
World intelligence tool for strategy agents.

Query geographic context, manage world events, track industry impacts,
and create market opportunities across Polymarket and Hyperliquid.

Usage:
    # Get geographic context for a region (neighbors, industries, resources)
    python3 agents/tools/intel_tool.py geo --region middle_east

    # List all regions and their neighbors
    python3 agents/tools/intel_tool.py geo --all

    # Find which regions are affected by an event in a given region
    python3 agents/tools/intel_tool.py cascade --region middle_east
      → returns: neighbors + their key industries + trade routes

    # Upsert a world event
    python3 agents/tools/intel_tool.py event \
        --slug iran-us-tensions-2026 \
        --title "Iran-US Military Tensions" \
        --region middle_east \
        --country Iran \
        --status escalating \
        --trajectory escalating \
        --winners '["defense","oil_producers"]' \
        --losers '["airlines","tourism","shipping"]' \
        --industries '["energy","defense","shipping","insurance"]'

    # Add industry impact for an event
    python3 agents/tools/intel_tool.py impact \
        --event-slug iran-us-tensions-2026 \
        --industry energy \
        --sector oil_and_gas \
        --direction bullish \
        --magnitude major \
        --reasoning "Strait of Hormuz risk premium — 20% of global oil transits here" \
        --tickers '["OIL","XLE","USO"]' \
        --hyperliquid '["OIL-USD","ETH-USD"]' \
        --polymarket '["0xabc123"]' \
        --horizon days

    # Create a market opportunity
    python3 agents/tools/intel_tool.py opportunity \
        --event-slug iran-us-tensions-2026 \
        --platform hyperliquid \
        --instrument OIL-USD \
        --direction long \
        --thesis "Strait of Hormuz disruption risk drives oil above $85" \
        --entry-reasoning "Oil at $72, historical Hormuz premium is $8-15" \
        --confidence high \
        --size-pct 3.0 \
        --expires-hours 168

    # List active events with their impacts
    python3 agents/tools/intel_tool.py events --status active,escalating

    # List open market opportunities
    python3 agents/tools/intel_tool.py opportunities --status idea,active

Outputs JSON to stdout.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from sniper.markets.content_normalization import normalize_event_title

# Backward-compatible alias for older tests/callers.
_normalize_event_title = normalize_event_title

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")


def _request(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode() if e.fp else ""
        print(json.dumps({"error": f"HTTP {e.code}: {err_body[:300]}"}))
        sys.exit(1)


def _parse_iso8601(value):
    """Parse a Supabase ISO timestamp into a timezone-aware datetime."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _touch_world_events(slugs):
    """Refresh last_updated for a batch of existing event slugs."""
    slugs = [str(slug).strip() for slug in (slugs or []) if str(slug).strip()]
    if not slugs:
        return 0
    encoded = ",".join(urllib.parse.quote(slug, safe=":-_") for slug in slugs)
    _request(
        "PATCH",
        f"world_events?slug=in.({encoded})",
        {"last_updated": datetime.now(timezone.utc).isoformat()},
    )
    return len(slugs)


def cmd_geo(args):
    """Get geographic context for a region or all regions."""
    if args.all:
        rows = _request("GET", "geo_context?order=region")
    else:
        rows = _request("GET", f"geo_context?region=eq.{args.region}")

    if not rows:
        print(json.dumps({"error": f"Region '{args.region}' not found. Use --all to see available regions."}))
        return

    print(json.dumps({"ok": True, "regions": rows}, indent=2))


def cmd_cascade(args):
    """Find cascade impacts — what regions and industries are affected by events in a region."""
    # Get the source region
    source = _request("GET", f"geo_context?region=eq.{args.region}")
    if not source:
        print(json.dumps({"error": f"Region '{args.region}' not found."}))
        return

    source = source[0]
    neighbors = source.get("neighbors", [])

    # Get all neighbor regions
    if neighbors:
        neighbor_filter = ",".join(neighbors)
        neighbor_data = _request("GET", f"geo_context?region=in.({neighbor_filter})")
    else:
        neighbor_data = []

    # Build cascade map
    cascade = {
        "source_region": args.region,
        "source_industries": source.get("key_industries", []),
        "source_resources": source.get("key_resources", []),
        "source_alliances": source.get("geopolitical_alliances", []),
        "affected_regions": [],
    }

    for n in neighbor_data:
        # Find shared industries (cross-exposure)
        shared = set(source.get("key_industries", [])) & set(n.get("key_industries", []))
        cascade["affected_regions"].append({
            "region": n["region"],
            "countries": n.get("countries", []),
            "shared_industries": list(shared),
            "their_industries": n.get("key_industries", []),
            "their_resources": n.get("key_resources", []),
            "their_alliances": n.get("geopolitical_alliances", []),
        })

    print(json.dumps({"ok": True, "cascade": cascade}, indent=2))


def cmd_event(args):
    """Upsert a world event."""
    normalized_title = normalize_event_title(args.title)
    row = {
        "slug": args.slug,
        "title": normalized_title,
        "region": args.region,
        "status": args.status,
        "trajectory": args.trajectory,
        "confidence": args.confidence,
    }
    if args.country:
        row["country"] = args.country
    if args.lat and args.lon:
        row["lat"] = args.lat
        row["lon"] = args.lon
    if args.status_note:
        row["status_note"] = args.status_note
    if args.winners:
        row["winners"] = json.loads(args.winners)
    if args.losers:
        row["losers"] = json.loads(args.losers)
    if args.industries:
        row["affected_industries"] = json.loads(args.industries)
    if args.connected:
        row["connected_events"] = json.loads(args.connected)
    if args.developments:
        row["key_developments"] = json.loads(args.developments)

    row["last_updated"] = datetime.now(timezone.utc).isoformat()

    # Upsert by slug
    existing = _request("GET", f"world_events?slug=eq.{args.slug}")
    if existing:
        _request("PATCH", f"world_events?slug=eq.{args.slug}", row)
        result = {"ok": True, "action": "updated", "slug": args.slug, "title": normalized_title}
    else:
        _request("POST", "world_events", row)
        result = {"ok": True, "action": "created", "slug": args.slug, "title": normalized_title}
    if normalized_title != args.title:
        result["title_normalized"] = True
        result["raw_title"] = args.title
    print(json.dumps(result))


def cmd_impact(args):
    """Add an industry impact for a world event."""
    # Resolve event_id from slug
    events = _request("GET", f"world_events?slug=eq.{args.event_slug}&select=id")
    if not events:
        print(json.dumps({"error": f"Event '{args.event_slug}' not found."}))
        return

    row = {
        "event_id": events[0]["id"],
        "industry": args.industry,
        "impact_direction": args.direction,
        "impact_magnitude": args.magnitude,
        "reasoning": args.reasoning,
        "confidence": args.confidence,
        "time_horizon": args.horizon,
    }
    if args.sector:
        row["sector"] = args.sector
    if args.tickers:
        row["affected_tickers"] = json.loads(args.tickers)
    if args.hyperliquid:
        row["affected_hyperliquid"] = json.loads(args.hyperliquid)
    if args.polymarket:
        row["affected_polymarket"] = json.loads(args.polymarket)

    result = _request("POST", "industry_impacts", row)
    print(json.dumps({"ok": True, "id": result[0]["id"] if result else "unknown"}))


def cmd_opportunity(args):
    """Create a market opportunity."""
    event_id = None
    if args.event_slug:
        events = _request("GET", f"world_events?slug=eq.{args.event_slug}&select=id")
        if events:
            event_id = events[0]["id"]

    row = {
        "platform": args.platform,
        "instrument": args.instrument,
        "direction": args.direction,
        "thesis": args.thesis,
        "entry_reasoning": args.entry_reasoning,
        "confidence": args.confidence,
        "suggested_size_pct": args.size_pct,
        "source_agent": "world_analyst",
    }
    if event_id:
        row["event_id"] = event_id
    if args.exit_conditions:
        row["exit_conditions"] = args.exit_conditions
    if args.polymarket_id:
        row["polymarket_condition_id"] = args.polymarket_id
    if args.hyperliquid_ticker:
        row["hyperliquid_ticker"] = args.hyperliquid_ticker
    if args.expires_hours:
        row["expires_at"] = (datetime.now(timezone.utc) + timedelta(hours=args.expires_hours)).isoformat()

    result = _request("POST", "market_opportunities", row)
    print(json.dumps({"ok": True, "id": result[0]["id"] if result else "unknown"}))


def cmd_events(args):
    """List world events."""
    statuses = args.status.split(",")
    status_filter = ",".join(statuses)
    fetch_limit = args.limit
    if args.slug_prefix or args.exclude_prefix or args.older_than_hours is not None:
        fetch_limit = max(fetch_limit, 200)
    path = f"world_events?status=in.({status_filter})&order=last_updated.desc&limit={fetch_limit}"
    events = _request("GET", path)

    if args.slug_prefix:
        events = [ev for ev in events if str(ev.get("slug", "")).startswith(args.slug_prefix)]
    if args.exclude_prefix:
        events = [ev for ev in events if not str(ev.get("slug", "")).startswith(args.exclude_prefix)]
    if args.older_than_hours is not None:
        now = datetime.now(timezone.utc)
        filtered = []
        for ev in events:
            last_updated = _parse_iso8601(ev.get("last_updated"))
            if last_updated is None:
                continue
            age_h = (now - last_updated).total_seconds() / 3600.0
            if age_h > args.older_than_hours:
                filtered.append(ev)
        events = filtered
    events = events[:args.limit]

    touched = 0
    if args.touch_reviewed:
        touched = _touch_world_events([ev.get("slug") for ev in events])

    # AUDIT — temporary diagnostic (2026-04-18): capture exactly what the analyst
    # agent is invoking, so we can distinguish "agent skipped flags" vs
    # "flags present but touch path failed" vs "prompt path is fine". Remove
    # after 20:02 UTC world run is verified clean.
    try:
        _audit_path = Path("/tmp/intel_tool_events_audit.log")
        _audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _audit_path.open("a", encoding="utf-8") as _af:
            _af.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "argv": sys.argv,
                "status": args.status,
                "limit": args.limit,
                "older_than_hours": args.older_than_hours,
                "slug_prefix": args.slug_prefix,
                "exclude_prefix": args.exclude_prefix,
                "touch_reviewed": args.touch_reviewed,
                "count": len(events),
                "touched": touched,
                "sample_slugs": [ev.get("slug") for ev in events[:5]],
            }) + "\n")
    except Exception:
        pass  # never block the tool on audit-log write

    # Enrich with impact counts
    for ev in events:
        impacts = _request("GET", f"industry_impacts?event_id=eq.{ev['id']}&select=industry,impact_direction")
        ev["impact_count"] = len(impacts)
        ev["impacts_summary"] = impacts

    print(json.dumps({"ok": True, "count": len(events), "touched": touched, "events": events}, indent=2))


def cmd_opportunities(args):
    """List market opportunities."""
    statuses = args.status.split(",")
    status_filter = ",".join(statuses)
    path = f"market_opportunities?status=in.({status_filter})&order=created_at.desc&limit=20"
    opps = _request("GET", path)
    print(json.dumps({"ok": True, "count": len(opps), "opportunities": opps}, indent=2))


def cmd_trade_idea(args):
    """Write a Polymarket trade idea to the SQLite trade_ideas table (feeds news_intel pipeline).

    This is how the world_analyst injects ideas into the news_intel strategy (#28).
    Uses Brain.add_trade_idea() with source_agent="world_analyst" so strategy #28
    can filter by source_agent and correctly track world_analyst performance.

    For Hyperliquid/non-Polymarket opportunities, use 'intel_tool.py opportunity' instead.
    """
    ROOT_PATH = Path(__file__).resolve().parent.parent.parent
    db_path = os.getenv("DB_PATH", str(ROOT_PATH / "sniper_tracker.db"))

    # Load .env if not already loaded
    env_path = ROOT_PATH / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

    try:
        import re as _re
        _VALID_CID = _re.compile(r"^0x[0-9a-f]{64}$")
        _NULL_CID = "0x" + "0" * 64
        _cid = args.condition_id or ""
        if not _VALID_CID.match(_cid) or _cid == _NULL_CID:
            print(json.dumps({
                "ok": False,
                "error": f"REJECTED: condition_id {_cid!r} is not a valid Polymarket condition_id. "
                         f"Must be '0x' + 64 lowercase hex chars. "
                         f"Find the real condition_id via 'intel_tool.py opportunity --search' "
                         f"or the Polymarket API before calling trade-idea.",
                "condition_id": _cid,
            }))
            sys.exit(1)

        sys.path.insert(0, str(ROOT_PATH))
        from brain import Brain

        direction = args.direction
        outcome = "Yes" if direction in ("buy_yes",) else "No"

        brain = Brain(db_path=db_path)
        idea_id = brain.add_trade_idea(
            condition_id=args.condition_id,
            market_title=args.market_title or "",
            outcome=outcome,
            direction=direction,
            reasoning=args.thesis,
            current_price=args.current_price if args.current_price is not None else 0.5,
            confidence=args.confidence,
            news_trigger=args.news_trigger or "",
            news_source=args.news_source or "world_analyst",
            driving_event=args.event_slug or "",
            source_agent=args.source_agent or "world_analyst",
        )
        actual_source = args.source_agent or "world_analyst"

        if idea_id:
            print(json.dumps({
                "ok": True,
                "idea_id": idea_id,
                "source_agent": actual_source,
                "condition_id": args.condition_id,
                "direction": direction,
            }))
        else:
            print(json.dumps({
                "ok": False,
                "note": "idea skipped — likely duplicate condition_id already open",
                "condition_id": args.condition_id,
            }))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="World intelligence tool")
    sub = parser.add_subparsers(dest="command")

    # geo
    p = sub.add_parser("geo", help="Geographic context")
    p.add_argument("--region", default=None)
    p.add_argument("--all", action="store_true")

    # cascade
    p = sub.add_parser("cascade", help="Cascade impact analysis")
    p.add_argument("--region", required=True)

    # event
    p = sub.add_parser("event", help="Upsert a world event")
    p.add_argument("--slug", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--country", default=None)
    p.add_argument("--lat", type=float, default=None)
    p.add_argument("--lon", type=float, default=None)
    p.add_argument("--status", default="monitoring")
    p.add_argument("--trajectory", default="unknown")
    p.add_argument("--status-note", default=None)
    p.add_argument("--confidence", default="medium")
    p.add_argument("--winners", default=None)
    p.add_argument("--losers", default=None)
    p.add_argument("--industries", default=None)
    p.add_argument("--connected", default=None)
    p.add_argument("--developments", default=None)

    # impact
    p = sub.add_parser("impact", help="Add industry impact")
    p.add_argument("--event-slug", required=True)
    p.add_argument("--industry", required=True)
    p.add_argument("--sector", default=None)
    p.add_argument("--direction", required=True, choices=["bullish", "bearish", "neutral", "mixed"])
    p.add_argument("--magnitude", default="moderate")
    p.add_argument("--reasoning", required=True)
    p.add_argument("--tickers", default=None)
    p.add_argument("--hyperliquid", default=None)
    p.add_argument("--polymarket", default=None)
    p.add_argument("--confidence", default="medium")
    p.add_argument("--horizon", default="days")

    # opportunity
    p = sub.add_parser("opportunity", help="Create market opportunity")
    p.add_argument("--event-slug", default=None)
    p.add_argument("--platform", required=True, choices=["polymarket", "hyperliquid", "both"])
    p.add_argument("--instrument", required=True)
    p.add_argument("--direction", required=True, choices=["long", "short", "buy_yes", "buy_no"])
    p.add_argument("--thesis", required=True)
    p.add_argument("--entry-reasoning", required=True)
    p.add_argument("--exit-conditions", default=None)
    p.add_argument("--confidence", default="medium")
    p.add_argument("--size-pct", type=float, default=2.0)
    p.add_argument("--polymarket-id", default=None)
    p.add_argument("--hyperliquid-ticker", default=None)
    p.add_argument("--expires-hours", type=int, default=None)

    # list events
    p = sub.add_parser("events", help="List world events")
    p.add_argument("--status", default="active,escalating,developing")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--older-than-hours", type=float, default=None)
    p.add_argument("--slug-prefix", default=None)
    p.add_argument("--exclude-prefix", default=None)
    p.add_argument("--touch-reviewed", action="store_true")

    # list opportunities
    p = sub.add_parser("opportunities", help="List market opportunities")
    p.add_argument("--status", default="idea,active")

    # trade-idea — write to SQLite trade_ideas (feeds news_intel strategy pipeline)
    p = sub.add_parser(
        "trade-idea",
        help="Write a Polymarket trade idea to SQLite trade_ideas (feeds news_intel #28 pipeline)",
    )
    p.add_argument("--condition-id", required=True, help="Polymarket condition_id (hex)")
    p.add_argument("--market-title", required=True, help="Market question/title")
    p.add_argument("--direction", required=True, choices=["buy_yes", "buy_no"],
                   help="buy_yes = bet YES, buy_no = bet NO")
    p.add_argument("--thesis", required=True, help="Causal reasoning for this trade idea")
    p.add_argument("--confidence", default="medium", choices=["high", "medium", "low"])
    p.add_argument("--current-price", type=float, default=None,
                   help="Current YES price (0.0–1.0). Defaults to 0.5 if unknown.")
    p.add_argument("--news-trigger", default=None, help="The specific news headline that triggered this idea")
    p.add_argument("--news-source", default="world_analyst", help="News source name")
    p.add_argument("--event-slug", default=None, help="Related world event slug (driving_event field)")
    p.add_argument("--source-agent", default=None,
                   help="Agent that generated this idea (world_analyst, finance_analyst, news_intel). "
                        "Determines which strategy picks it up. Defaults to 'world_analyst'.")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    {
        "geo": cmd_geo,
        "cascade": cmd_cascade,
        "event": cmd_event,
        "impact": cmd_impact,
        "opportunity": cmd_opportunity,
        "events": cmd_events,
        "opportunities": cmd_opportunities,
        "trade-idea": cmd_trade_idea,
    }[args.command](args)


if __name__ == "__main__":
    main()
