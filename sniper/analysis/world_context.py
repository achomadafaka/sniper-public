"""World Context Manager — maintains a knowledge graph of world events in Supabase.

Uses the `claude -p` CLI (same subscription as Claude Code, no API key needed) to:
  1. Analyze recent news against open Polymarket markets
  2. Update world event nodes in Supabase (type='reference', prefix 'world_event:')
  3. Generate trade ideas with correct causal reasoning

Fallback: If `claude` CLI is unavailable, returns empty and lets the existing
keyword-matching pipeline handle trade ideas.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sniper.markets.content_normalization import normalize_world_event_payload
from sniper.settings.models import OPUS

try:
    from sniper.markets.source_meta import get_source_meta as _get_source_meta
    _SOURCE_META_AVAILABLE = True
except ImportError:
    _SOURCE_META_AVAILABLE = False
    def _get_source_meta(key: str) -> dict:
        return {"lean": "unknown", "reliability": "medium"}

logger = logging.getLogger(__name__)

WORLD_EVENT_PREFIX = "world_event:"
WORLD_EVENT_TYPE = "reference"

# Max items sent per run — keep prompts focused
MAX_NEWS_FOR_CLAUDE = 25
MAX_MARKETS_FOR_CLAUDE = 50

# Claude CLI timeout — None = unlimited (--effort high can take 5-15 min)
CLAUDE_TIMEOUT: Optional[int] = None


# ---------------------------------------------------------------------------
# JSON schema for structured output
# ---------------------------------------------------------------------------

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "world_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug":              {"type": "string"},
                    "title":             {"type": "string"},
                    "status":            {
                        "type": "string",
                        "enum": ["active", "developing", "monitoring", "resolved"],
                    },
                    "trajectory":        {
                        "type": "string",
                        "enum": ["escalating", "de-escalating", "stable", "unknown"],
                    },
                    "status_note":       {"type": "string"},
                    "trajectory_note":   {"type": "string"},
                    "summary":           {"type": "string"},
                    "key_developments":  {"type": "array", "items": {"type": "string"}},
                    "connected_events":  {"type": "array", "items": {"type": "string"}},
                    "is_new":            {"type": "boolean"},
                    "lat":    {"type": ["number", "null"], "description": "Representative latitude for map pin (null for non-geographic events)"},
                    "lon":    {"type": ["number", "null"], "description": "Representative longitude for map pin (null for non-geographic events)"},
                    "region": {"type": "string", "enum": ["North America", "South America", "Europe", "Middle East", "Africa", "South Asia", "East Asia", "Southeast Asia", "Central Asia", "Oceania", "Global"]},
                },
                "required": ["slug", "title", "status", "trajectory", "summary", "status_note", "trajectory_note", "region"],
            },
        },
        "trade_ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "condition_id":  {"type": "string"},
                    "market_title":  {"type": "string"},
                    "direction":     {"type": "string", "enum": ["buy_yes", "buy_no"]},
                    "confidence":    {"type": "string", "enum": ["high", "medium", "low"]},
                    "causal_chain":  {"type": "string"},
                    "news_trigger":  {"type": "string"},
                    "news_source":   {"type": "string"},
                    "news_trigger_ts": {"type": ["string", "null"], "description": "ISO-8601 publication timestamp of the triggering news item, copied verbatim from the item's date. Null if unknown — do not guess."},
                    "hl_symbol": {"type": ["string", "null"], "description": "Hyperliquid symbol if the thesis is better expressed as a perp than a prediction market. Equities and commodities live on builder dexes and MUST be namespaced: flx:OIL, km:USOIL, xyz:TSLA, cash:USA500, vntl:DEFENSE, vntl:ENERGY. Crypto is bare: BTC, ETH, SOL. A bare equity name like TSLA is ambiguous across dexes and will be rejected. Null when no instrument fits — do not invent one."},
                    "driving_event":   {"type": "string"},
                    "coverage_gaps":   {"type": "array", "items": {"type": "string"}, "description": "2-3 outlet types notably absent from this story's coverage"},
                    "counter_narrative": {"type": ["string", "null"], "description": "Any source reporting against its expected bias — high-value anomaly"},
                },
                "required": ["condition_id", "market_title", "direction", "confidence", "causal_chain"],
            },
        },
        "position_reviews": {
            "type": "array",
            "description": "Reassessment of paper positions we already hold, in light of THIS news batch. Advisory only — a human decides whether to close; nothing here executes. Emit a row only for positions this batch materially bears on; omit the rest rather than padding with 'no change'.",
            "items": {
                "type": "object",
                "properties": {
                    "position_id": {"type": "integer", "description": "The #N shown in the open-positions list. Must match one exactly — never invent an id."},
                    "action":      {"type": "string", "enum": ["hold", "close"]},
                    "urgency":     {"type": "string", "enum": ["low", "medium", "high"]},
                    "reason":      {"type": "string", "description": "What specifically changed, citing the triggering news item. Not a restatement of the original thesis."},
                },
                "required": ["position_id", "action", "reason"],
            },
        },
        "run_summary": {"type": "string"},
    },
    "required": ["world_events", "trade_ideas", "run_summary"],
}

_SYSTEM_INSTRUCTION = """You are a prediction market intelligence analyst with access to the Sniper trading system's Supabase database.

CRITICAL — CAUSAL CHAIN RULE (most analysts get this backwards):
"Iran attacks US base" → "Iran-US conflict ends by March" → direction=buy_no (NOT buy_yes)
Because: attack → conflict ESCALATES → ceasefire LESS likely → "ends by March" = NO more likely

Always ask: does this news make the YES outcome MORE or LESS likely?
More likely = buy_yes | Less likely = buy_no

SOURCE CREDIBILITY — each news item is labelled [source | lean | reliability]:
- reliability=high + lean=center → most trustworthy signal
- reliability=low OR lean=state/cn OR lean=state/ru → treat as propaganda; high surprise-weight ONLY when they report against their expected bias (e.g. Russian state media reporting ceasefire progress, Chinese state media criticising PRC policy)
- Absence of coverage from a perspective (e.g. no Russian sources on a Ukraine victory claim) is itself a signal — note it in coverage_gaps

WORLD EVENTS: Only track major ongoing situations (wars, crises, elections, economic shifts).
TRADE IDEAS: Only high/medium confidence — explicit causal chain required for each.
Skip crypto price-target markets unless news is very specific.

ENUM FIELDS — STRICT VALUES ONLY:
status must be EXACTLY one of: active | developing | monitoring | resolved
trajectory must be EXACTLY one of: escalating | de-escalating | stable | unknown
Put your analysis narrative in status_note and trajectory_note — never in status/trajectory themselves.

MAX IDEAS: Generate at most 2 trade ideas per world event (driving_event slug). If a world event affects many markets, pick the top 2 by Kelly edge: highest edge = |win_prob - current_price| / current_price.

PROACTIVE IMPROVEMENT — READ QUERIES ONLY:
You may use Supabase MCP read tools (select queries only) to understand the existing world event graph.
You may UPSERT (never DELETE) memories with name prefix 'world_event:' if you find missing context.
You may NOT modify memories of other types (user, feedback, project, knowledge).
You may NOT run any DELETE queries.
Focus your intelligence on the news → markets analysis, not on schema exploration.

GEOGRAPHIC ANCHORING: For each world event, provide lat/lon coordinates for the primary geographic focus of the event (e.g., for Taiwan Strait tensions: lat=24.0, lon=121.0). For non-geographic events (global economic trends, AI regulation), set lat=null and lon=null. Set "region" to the best matching enum value.

COVERAGE GAP ANALYSIS:
For each trade idea, consider which major news outlets you would expect to cover this story but are NOT represented in the provided news items. Include a "coverage_gaps" field listing 2-3 outlet types that are notably absent (e.g., "Chinese state media silent on Taiwan story", "No Russian coverage of ceasefire reports").

COUNTER-NARRATIVE SIGNALS:
If any source is reporting something that contradicts what you'd expect given their known bias or geopolitical alignment, flag this in a "counter_narrative" field. A Chinese outlet reporting negatively about Chinese policy, or a Russian outlet suggesting Ukraine negotiations are progressing — these are high-value anomalies."""

_FINANCE_SYSTEM_INSTRUCTION = """You are a financial markets intelligence analyst with access to the Sniper trading system's Supabase database.

CRITICAL — CAUSAL CHAIN RULE (most analysts get this backwards):
"Fed signals rate hike" → "will there be a rate cut by June?" → direction=buy_no (NOT buy_yes)
Because: hawkish signal → rate cut LESS likely → "rate cut by June" = NO more likely

Always ask: does this news make the YES outcome MORE or LESS likely?
More likely = buy_yes | Less likely = buy_no

DOMAIN FOCUS: You are focused exclusively on financial/macro/crypto markets:
- Central bank policy (Fed, ECB, BoE, BoJ): rate decisions, forward guidance, inflation prints
- Equity markets: index moves, earnings, sector rotation, IPOs
- Crypto: BTC/ETH price levels, regulatory news, protocol launches, ETF flows
- Commodities: oil (OPEC+), gold, natural gas (supply/demand shocks)
- FX: major pair moves, EM stress, dollar index
- Macro data: CPI, NFP, GDP, PMI surprises

SOURCE CREDIBILITY — each news item is labelled [source | lean | reliability]:
- reliability=high + lean=center → most trustworthy signal
- Financial-specialist sources (Bloomberg, Reuters, FT, WSJ, CNBC) weighted higher for market signals
- Crypto-specialist sources (CoinDesk, CoinTelegraph, The Block) weighted higher for crypto signals

WORLD EVENTS: Only track major macro/financial events (rate cycles, market regimes, crypto cycles, commodity shocks).
TRADE IDEAS: Only high/medium confidence — explicit causal chain required for each.
Focus on price-target markets, rate markets, and crypto price markets. Skip geopolitical markets unless they have direct financial causation.

ENUM FIELDS — STRICT VALUES ONLY:
status must be EXACTLY one of: active | developing | monitoring | resolved
trajectory must be EXACTLY one of: escalating | de-escalating | stable | unknown
Put your analysis narrative in status_note and trajectory_note — never in status/trajectory themselves.

MAX IDEAS: Generate at most 2 trade ideas per world event (driving_event slug). Pick the top 2 by Kelly edge: highest edge = |win_prob - current_price| / current_price.

GEOGRAPHIC ANCHORING: For financial events without a specific geography (e.g., global rate cycle), set lat=null, lon=null, region=Global. For regional events (ECB policy → Europe), use appropriate lat/lon and region.

PROACTIVE IMPROVEMENT — READ QUERIES ONLY:
You may use Supabase MCP read tools (select queries only) to understand the existing world event graph.
You may NOT run any DELETE queries."""

_PROMPT_TEMPLATE = """Analyze these recent news items and assess their implications for open Polymarket markets.

EXISTING WORLD EVENTS (your knowledge graph to update):
{events}

RECENT NEWS ITEMS:
{news}

OPEN POLYMARKET MARKETS (conditionId | question | current YES price):
{markets}
{positions_section}
Instructions:
- world_events: only include events that were UPDATED or CREATED by this news batch
- trade_ideas: only include high/medium confidence ideas with a clear causal chain
- condition_id must exactly match a conditionId from the market list above
- If a market direction is ambiguous, skip it
- Your ideas must be mutually consistent: no two ideas in this batch may be unable to
  both win. That covers the same market twice in opposite directions, and also nested
  or mutually exclusive outcomes of one event (e.g. "X by Jul 31" vs "X by Aug 31",
  or two outcomes of a negRisk event where only one can resolve YES). If you hold two
  opposing views, emit only the one you believe more and say why in run_summary."""


# Rendered only when the book is non-empty, so a run with nothing open sends no
# empty scaffolding. Positions are shown to both agents (the reviewer sees the same
# text) so neither reasons about the book the other cannot see.
_POSITIONS_SECTION = """
OPEN PAPER POSITIONS — we already hold these:
{positions}

Reassess each against this news batch and emit position_reviews for any the batch
materially bears on. These are recommendations only: a human decides whether to close,
nothing here executes. Positions on the same event are mechanically linked — treat them
as one exposure, not several.

DO NOT propose a trade idea that:
- names a market already held above (any direction) — we cannot add to a position;
- cannot both win alongside a position above. Check implication, not just wording:
  "no ceasefire by Aug 31" already implies "no ceasefire by Jul 31", so betting the
  two opposite ways is a guaranteed offset that pays the spread twice and learns
  nothing.
If your best idea conflicts with something we already hold, say so in run_summary and
propose nothing for that market — a position_reviews entry is the right channel for
changing an existing bet.
"""


# ---------------------------------------------------------------------------
# Supabase world event storage
# ---------------------------------------------------------------------------

_MAX_WORLD_EVENTS = 30  # cap events sent to Claude to keep prompt size bounded


def _get_world_events_from_table() -> Optional[List[Dict]]:
    """Query the world_events Supabase table directly via REST API.

    Returns list of event dicts on success, or None on failure (caller falls back).
    Table schema: slug, title, region, status, trajectory, confidence, country,
    lat, lon, status_note, winners, losers, affected_industries,
    connected_events, key_developments, last_updated.
    """
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    if not supabase_url or not supabase_key:
        return None

    import urllib.request
    import urllib.error

    url = (
        f"{supabase_url}/rest/v1/world_events"
        f"?status=in.(active,developing,escalating,monitoring)"
        f"&order=last_updated.desc"
        f"&limit={_MAX_WORLD_EVENTS}"
    )
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode())
        events = []
        for row in rows:
            event = normalize_world_event_payload(dict(row))
            event["_memory_updated_at"] = row.get("last_updated")
            events.append(event)
        return events
    except Exception as exc:
        logger.warning("world_events table query failed: %s", exc)
        return None


def _get_world_events_from_memories() -> List[Dict]:
    """Legacy fallback: read world events from Supabase memories table."""
    events, _ = _get_world_events_from_memories_with_error()
    return events


def _get_world_events_from_memories_with_error() -> tuple[List[Dict], Optional[str]]:
    """Legacy fallback: read world events from Supabase memories table."""
    try:
        from bot.supabase_helper import read_memories
        memories = read_memories(type_filter=WORLD_EVENT_TYPE)
        events = []
        for m in memories:
            if not m.get("name", "").startswith(WORLD_EVENT_PREFIX):
                continue
            try:
                content = normalize_world_event_payload(json.loads(m.get("content", "{}")))
                content["_memory_updated_at"] = m.get("updated_at")
                events.append(content)
            except (json.JSONDecodeError, Exception):
                pass
        events.sort(key=lambda e: e.get("_memory_updated_at") or "", reverse=True)
        return events[:_MAX_WORLD_EVENTS], None
    except Exception as e:
        logger.warning("Failed to load world events from memories fallback: %s", e)
        return [], str(e)


def get_world_events_status() -> Dict[str, object]:
    """Return world events plus availability metadata for operator-facing APIs."""
    events = _get_world_events_from_table()
    if events is not None:
        return {
            "events": events,
            "count": len(events),
            "available": True,
            "degraded": False,
            "source": "world_events_table",
        }

    if not os.getenv("SUPABASE_URL", "").strip() or not (os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "").strip()):
        return {
            "events": [],
            "count": 0,
            "available": False,
            "degraded": True,
            "source": "unconfigured",
            "code": "supabase_not_configured",
            "message": "World context unavailable — SUPABASE_URL and SUPABASE_ANON_KEY are not configured.",
        }

    logger.info("Falling back to memories table for world events")
    events, error = _get_world_events_from_memories_with_error()
    if error is not None:
        return {
            "events": [],
            "count": 0,
            "available": False,
            "degraded": True,
            "source": "memories_fallback_failed",
            "code": "world_context_fallback_failed",
            "message": "World context unavailable — failed to load Supabase memories fallback.",
            "error": error,
        }
    return {
        "events": events,
        "count": len(events),
        "available": True,
        "degraded": True,
        "source": "memories_fallback",
        "message": "World context loaded from legacy memories fallback.",
    }


def get_world_events() -> List[Dict]:
    """Get world events from Supabase world_events table, capped at _MAX_WORLD_EVENTS.

    Falls back to reading from memories table if world_events table query fails.
    """
    return list(get_world_events_status().get("events") or [])


def get_market_opportunities(limit: int = 30) -> Dict[str, object]:
    """Read research trade ideas from the Supabase market_opportunities table (the world/finance
    analyst output). Non-terminal status only (idea/proposed/open). Read-only and display-only:
    these are RESEARCH hypotheses for the Intel tab, NOT advice / executed trades — many carry a
    NULL polymarket_condition_id (unverified market) and must not be acted on as-is."""
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    if not supabase_url or not supabase_key:
        return {"items": [], "count": 0, "available": False, "source": "unconfigured",
                "message": "Trade ideas unavailable — Supabase is not configured."}
    import urllib.request
    # Expired research must not render as current (audit F-03: 10/16 served rows were past
    # expires_at, one by 29 days). Rows with a NULL expiry are kept — undated, not expired.
    url = (f"{supabase_url}/rest/v1/market_opportunities"
           f"?status=in.(idea,proposed,open)"
           f"&or=(expires_at.is.null,expires_at.gt.now())"
           f"&order=created_at.desc&limit={max(1, min(100, int(limit)))}")
    headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            rows = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — degrade, never crash the Intel tab
        logger.warning("market_opportunities query failed: %s", exc)
        return {"items": [], "count": 0, "available": False, "source": "error", "error": str(exc)}
    # RESEARCH-ONLY projection: never ship sizing / expected-PnL fields (suggested_size_pct,
    # max_loss_pct, pnl_pct, entry_price, current_price) — CLAUDE.md forbids framing output as sizing
    # or expected PnL, and a "not advice" disclaimer is meaningless if the payload still carries the
    # trade. We surface only the qualitative thesis + a market-verified flag.
    # event_id is lineage, not trade content — it lets a research card link back to the world
    # event that drove it (audit F-21: populated 16/16 but dropped at the API boundary).
    _ALLOWED = ("instrument", "direction", "thesis", "entry_reasoning", "exit_conditions",
                "confidence", "status", "source_agent", "created_at", "expires_at", "event_id")
    items = []
    for r in (rows or []):
        item = {k: r.get(k) for k in _ALLOWED}
        item["market_verified"] = bool(r.get("polymarket_condition_id"))   # flag, not the raw id
        items.append(item)
    return {"items": items, "count": len(items), "available": True, "source": "market_opportunities_table"}


def _save_world_event_to_table(event: Dict) -> bool:
    """Upsert a world event into the Supabase world_events table.

    Returns True on success, False on failure (caller falls back to memories).
    """
    supabase_url = os.getenv("SUPABASE_URL", "")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    if not supabase_url or not supabase_key:
        return False

    import urllib.request
    import urllib.error

    normalized_event = normalize_world_event_payload(event)
    slug = normalized_event.get("slug", "").replace("/", "-").replace(" ", "-").lower()
    if not slug:
        return False

    # Map Claude output fields to world_events table columns
    row = {
        "slug": slug,
        "title": normalized_event.get("title", slug),
        "status": normalized_event.get("status", "active"),
        "trajectory": normalized_event.get("trajectory", "unknown"),
        "region": normalized_event.get("region", "Global"),
        "last_updated": normalized_event.get("last_updated", datetime.now(timezone.utc).isoformat()),
    }
    # Optional columns
    if normalized_event.get("status_note"):
        row["status_note"] = normalized_event["status_note"]
    if normalized_event.get("confidence"):
        row["confidence"] = normalized_event["confidence"]
    if normalized_event.get("country"):
        row["country"] = normalized_event["country"]
    if normalized_event.get("lat") is not None:
        row["lat"] = normalized_event["lat"]
    if normalized_event.get("lon") is not None:
        row["lon"] = normalized_event["lon"]
    if normalized_event.get("winners"):
        row["winners"] = normalized_event["winners"] if isinstance(normalized_event["winners"], list) else []
    if normalized_event.get("losers"):
        row["losers"] = normalized_event["losers"] if isinstance(normalized_event["losers"], list) else []
    if normalized_event.get("affected_industries"):
        row["affected_industries"] = normalized_event["affected_industries"] if isinstance(normalized_event["affected_industries"], list) else []
    if normalized_event.get("connected_events"):
        row["connected_events"] = normalized_event["connected_events"] if isinstance(normalized_event["connected_events"], list) else []
    if normalized_event.get("key_developments"):
        row["key_developments"] = normalized_event["key_developments"] if isinstance(normalized_event["key_developments"], list) else []

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }

    # Upsert (on conflict slug) — ?on_conflict=slug is required by PostgREST
    url = f"{supabase_url}/rest/v1/world_events?on_conflict=slug"
    data = json.dumps(row, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except Exception as exc:
        logger.warning("world_events table upsert failed for %s: %s", slug, exc)
        return False


def _save_world_event_to_memories(event: Dict) -> None:
    """Legacy fallback: persist a world event to Supabase memories."""
    try:
        from bot.supabase_helper import upsert_memory
        normalized_event = normalize_world_event_payload(event)
        slug = normalized_event.get("slug", "").replace("/", "-").replace(" ", "-").lower()
        if not slug:
            return
        name = f"{WORLD_EVENT_PREFIX}{slug}"
        title = normalized_event.get("title", slug)
        status = normalized_event.get("status", "active")
        trajectory = normalized_event.get("trajectory", "unknown")
        status_note = normalized_event.get("status_note", "")
        trajectory_note = normalized_event.get("trajectory_note", "")
        description_parts = [f"World event: {title} [{status}, {trajectory}]"]
        if status_note:
            description_parts.append(f"Status: {status_note}")
        if trajectory_note:
            description_parts.append(f"Trajectory: {trajectory_note}")
        description = " | ".join(description_parts)
        content_dict = dict(normalized_event)
        if normalized_event.get("lat") is not None:
            content_dict["lat"] = normalized_event["lat"]
        if normalized_event.get("lon") is not None:
            content_dict["lon"] = normalized_event["lon"]
        content_dict["region"] = normalized_event.get("region", "Global")
        content = json.dumps(content_dict, ensure_ascii=False)
        upsert_memory(
            type=WORLD_EVENT_TYPE,
            name=name,
            description=description,
            content=content,
            source="vps",
        )
    except Exception as e:
        logger.warning("Failed to save world event %s to memories fallback: %s", event.get("slug"), e)


def _save_world_event(event: Dict) -> None:
    """Persist a world event to Supabase world_events table.

    Falls back to memories table if the table upsert fails.
    """
    if _save_world_event_to_table(event):
        return
    # Fallback to legacy memories path
    logger.info("Falling back to memories table for saving event %s", event.get("slug"))
    _save_world_event_to_memories(event)


# ---------------------------------------------------------------------------
# Prompt formatters
# ---------------------------------------------------------------------------

def _format_news_for_prompt(news_items: List[Dict]) -> str:
    lines = []
    for i, item in enumerate(news_items[:MAX_NEWS_FOR_CLAUDE], 1):
        title = item.get("title", "")
        source_key = item.get("source", "")
        source = source_key.replace("_", " ")
        desc = (item.get("description") or "")[:200]
        pub = (item.get("published_at") or "")[:16]
        # Append source credibility metadata so Claude can weight signals appropriately
        meta = _get_source_meta(source_key)
        lean = meta.get("lean", "unknown")
        rel = meta.get("reliability", "medium")
        line = f"{i}. [{source} | {lean} | {rel}] {title}"
        if desc:
            line += f"\n   {desc}"
        if pub:
            line += f" ({pub})"
        lines.append(line)
    return "\n".join(lines)


def _get_market_yes_price(market: Dict) -> float:
    """Extract the YES price from a market dict. Returns -1.0 if unavailable."""
    prices_raw = market.get("outcomePrices")
    if isinstance(prices_raw, str):
        try:
            prices_raw = json.loads(prices_raw)
        except Exception:
            return -1.0
    if isinstance(prices_raw, list) and prices_raw:
        try:
            return float(prices_raw[0])
        except Exception:
            pass
    return -1.0


def _format_markets_for_prompt(markets: List[Dict]) -> str:
    lines = []
    count = 0
    for m in markets:
        title = m.get("question") or m.get("title") or ""
        cid = m.get("conditionId") or m.get("condition_id") or ""
        if not title or not cid:
            continue
        yes_price = _get_market_yes_price(m)
        if yes_price < 0.02 or yes_price > 0.98:
            continue
        lines.append(f"- [{cid}] {title} (YES={round(yes_price * 100)}¢)")
        count += 1
        if count >= MAX_MARKETS_FOR_CLAUDE:
            break
    if lines:
        lines.append(
            "\nIMPORTANT — similar markets: if multiple markets share the same underlying "
            "question across different date ranges (e.g. \"regime fall by March\", \"regime fall "
            "by April\", \"regime fall by June\"), pick the 1-2 with the best edge (price furthest "
            "from 0.5 on the NO side) and skip the rest. Do not generate duplicate ideas for the "
            "same thesis across date buckets."
        )
    return "\n".join(lines)


def _format_events_for_prompt(events: List[Dict]) -> str:
    if not events:
        return "None yet — create new events as needed."
    lines = []
    for e in events:
        slug = e.get("slug", "")
        title = e.get("title", slug)
        status = e.get("status", "active")
        trajectory = e.get("trajectory", "unknown")
        summary = (e.get("summary") or "")[:250]
        status_note = (e.get("status_note") or "")[:120]
        trajectory_note = (e.get("trajectory_note") or "")[:120]
        line = f"- [{slug}] {title} [{status}, {trajectory}]: {summary}"
        if status_note:
            line += f" | status_note: {status_note}"
        if trajectory_note:
            line += f" | trajectory_note: {trajectory_note}"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude CLI call
# ---------------------------------------------------------------------------

def _claude_available() -> bool:
    """Check whether the claude CLI is on PATH."""
    return shutil.which("claude") is not None


def analyze_news_with_claude(
    news_items: List[Dict],
    markets: List[Dict],
    existing_events: List[Dict],
    system_instruction: Optional[str] = None,
    positions_block: str = "",
) -> Optional[Dict]:
    """Call `claude -p` to analyze news against markets and world events.

    Uses the same Claude Code subscription — no API key required.
    Returns structured dict or None on failure.

    Args:
        system_instruction: Override the default geopolitical system prompt.
            Pass ``_FINANCE_SYSTEM_INSTRUCTION`` for the finance analyst agent.
        positions_block: Rendered open paper positions. Empty string omits the
            section entirely, so an empty book costs no prompt tokens.
    """
    if not news_items:
        return None

    news_text = _format_news_for_prompt(news_items)
    markets_text = _format_markets_for_prompt(markets)
    events_text = _format_events_for_prompt(existing_events)

    if not markets_text.strip():
        logger.info("No tradeable markets in range — skipping Claude call")
        return None

    prompt = _PROMPT_TEMPLATE.format(
        events=events_text,
        news=news_text,
        markets=markets_text,
        positions_section=(
            _POSITIONS_SECTION.format(positions=positions_block) if positions_block else ""
        ),
    )

    active_system = system_instruction if system_instruction is not None else _SYSTEM_INSTRUCTION

    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(_OUTPUT_SCHEMA),
        "--system-prompt", active_system,
        "--permission-mode", "dontAsk",
        "--disallowed-tools", "Bash,Edit,Write,Read,Glob,Grep,WebSearch,NotebookEdit",
        # NOTE: WebFetch and mcp__supabase__* are ALLOWED — agent can query/update Supabase directly
        "--model", OPUS,            # frontier tier — see sniper/settings/models.py
        "--effort", "high",         # extended thinking — best causal reasoning
        "--setting-sources", "",    # skip CLAUDE.md / project settings — saves 85K tokens per call
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT,
        )

        if proc.returncode != 0:
            logger.error("Claude CLI returned exit %d: %s", proc.returncode, proc.stderr[:300])
            return None

        response = json.loads(proc.stdout)

        # structured_output is pre-parsed JSON when --json-schema is used
        structured = response.get("structured_output")
        if structured:
            logger.info(
                "Claude analysis complete: %d world event updates, %d trade ideas — %s",
                len(structured.get("world_events", [])),
                len(structured.get("trade_ideas", [])),
                (structured.get("run_summary") or "")[:120],
            )
            return structured

        # Fallback: parse result field as JSON text
        result_text = response.get("result", "").strip()
        if result_text:
            if result_text.startswith("```"):
                lines = result_text.split("\n")
                end = len(lines) - 1 if lines[-1].strip() == "```" else len(lines)
                result_text = "\n".join(lines[1:end])
            parsed = json.loads(result_text)
            logger.info(
                "Claude analysis (text fallback): %d events, %d ideas",
                len(parsed.get("world_events", [])),
                len(parsed.get("trade_ideas", [])),
            )
            return parsed

        logger.warning("Claude returned empty result and no structured_output")
        return None

    except subprocess.TimeoutExpired:
        logger.error("Claude CLI timed out after %ds", CLAUDE_TIMEOUT)
        return None
    except json.JSONDecodeError as e:
        logger.error("Failed to parse Claude CLI output: %s", e)
        return None
    except Exception as e:
        logger.error("Claude CLI call failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main entry point (called from news_intel.py)
# ---------------------------------------------------------------------------

def _record_position_reviews(conn, reviews: List[Dict]) -> int:
    """Persist the analyst's hold/close opinions. Advisory only — nothing executes here.

    A review naming a position we do not currently hold is dropped rather than
    stored: an invented or stale id would attach model opinion to the wrong
    position, which is worse than having no opinion at all.
    """
    if not reviews:
        return 0
    from sniper.persistence import intel_paper
    try:
        open_ids = {int(p["position_id"]) for p in intel_paper.open_positions(conn)}
    except Exception:
        logger.exception("could not load open positions to validate reviews")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    written = 0
    for r in reviews:
        try:
            pid = int(r["position_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid not in open_ids:
            logger.warning("dropping review for position %s — not open", pid)
            continue
        action = str(r.get("action") or "").lower()
        if action not in ("hold", "close"):
            logger.warning("dropping review for %s — bad action %r", pid, r.get("action"))
            continue
        reason = str(r.get("reason") or "")[:2000]
        try:
            intel_paper.record_recommendation(
                conn, pid, now, action, reason=reason,
                urgency=(str(r.get("urgency") or "").lower() or None),
                source="news_intel_analyst",
            )
            written += 1
            if action == "close":
                logger.info("CLOSE recommended for position %s: %s", pid, reason[:160])
        except Exception:
            logger.exception("failed to record recommendation for position %s", pid)
    return written


def update_world_context(
    news_items: List[Dict],
    markets: List[Dict],
    domain: str = "world",
    conn=None,
) -> List[Dict]:
    """Analyze news with Claude CLI, update world events in Supabase, return trade ideas.

    Args:
        domain: ``"world"`` (default) uses the geopolitical system prompt;
                ``"finance"`` uses the finance/macro system prompt.
        conn: Optional SQLite connection to the intel paper book. When supplied,
            open positions are shown to the analyst and any close recommendations
            it returns are recorded. Omitted (the default) keeps the old
            news-only behavior for callers with no book.

    Returns list of trade idea dicts.
    Returns empty list if claude CLI unavailable or call fails.
    """
    system_instruction = _FINANCE_SYSTEM_INSTRUCTION if domain == "finance" else _SYSTEM_INSTRUCTION

    if not _claude_available():
        logger.warning("claude CLI not found on PATH — falling back to keyword matching")
        return []

    existing_events = get_world_events()
    logger.info("Loaded %d existing world events from Supabase [domain=%s]", len(existing_events), domain)

    # Filter markets to only those with meaningful edge (price not fully resolved)
    tradeable_markets = [m for m in markets if 0.05 <= _get_market_yes_price(m) <= 0.90]
    filtered_count = len(markets) - len(tradeable_markets)
    logger.info(
        "Filtered %d/%d markets (price outside [0.05,0.90])",
        filtered_count,
        len(markets),
    )

    # All news with at least a meaningful title (not just topic-tagged)
    relevant_news = [
        item for item in news_items
        if item.get("topic_tags") or len(item.get("title", "")) > 20
    ]

    # Deduplicate on normalized title (same headline from multiple feeds wastes tokens)
    _seen_titles: set = set()
    deduped_news: List[Dict] = []
    for item in relevant_news:
        _norm = item.get("title", "").lower().strip()
        if _norm and _norm not in _seen_titles:
            _seen_titles.add(_norm)
            deduped_news.append(item)
    relevant_news = deduped_news

    if not relevant_news:
        logger.info("No relevant news items for Claude analysis")
        return []

    logger.info(
        "Calling Claude CLI to analyze %d news items against %d markets",
        len(relevant_news),
        len(tradeable_markets),
    )

    positions_block = ""
    if conn is not None:
        from sniper.services.intel_positions_context import format_positions_block
        positions_block = format_positions_block(conn)
        if positions_block:
            logger.info("Including %d open paper positions in analyst context",
                        len(positions_block.splitlines()))

    result = analyze_news_with_claude(
        relevant_news, tradeable_markets, existing_events,
        system_instruction=system_instruction, positions_block=positions_block,
    )
    if result is None:
        return []

    if conn is not None:
        _record_position_reviews(conn, result.get("position_reviews") or [])

    # Persist world event updates to Supabase
    for event in (result.get("world_events") or []):
        normalized_event = normalize_world_event_payload(event)
        normalized_event["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_world_event(normalized_event)
        action = "Created" if event.get("is_new") else "Updated"
        logger.info(
            "%s world event: %s [%s, %s]",
            action, normalized_event.get("title"), normalized_event.get("status"), normalized_event.get("trajectory"),
        )

    # Codex 2026-05-27 audit: hoist trade_ideas assignment before the
    # run_summary block — the finance branch references trade_ideas in
    # two places (json payload + log line) which would have raised
    # UnboundLocalError if run_summary was non-empty.
    trade_ideas = result.get("trade_ideas") or []

    # Store run_summary as a Supabase memory for future reference
    # Finance domain → writes markets/state (owned section); world domain → world_event_run:last_summary
    if result.get("run_summary"):
        try:
            from bot.supabase_helper import upsert_memory
            now_ts = datetime.now(timezone.utc).isoformat()
            if domain == "finance":
                content = json.dumps({
                    "run_at": now_ts,
                    "run_summary": result["run_summary"],
                    "events_updated": len(result.get("world_events") or []),
                    "trade_ideas_generated": len(trade_ideas),
                }, ensure_ascii=False)
                upsert_memory(
                    type="reference",
                    name="markets/state",
                    description="Finance Analyst — macro/markets intelligence state",
                    content=content,
                    source="vps",
                )
                logger.info("Finance domain: wrote markets/state memory (%d events, %d ideas)",
                            len(result.get("world_events") or []), len(trade_ideas))
            else:
                upsert_memory(
                    type="reference",
                    name="world_event_run:last_summary",
                    description="Last world context analysis summary",
                    content=result["run_summary"],
                    source="vps",
                )
        except Exception:
            pass  # never fail the main flow

    logger.info("Claude produced %d trade ideas", len(trade_ideas))
    return trade_ideas
