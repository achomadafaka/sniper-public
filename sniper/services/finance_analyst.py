"""Finance Analyst Agent — macro / markets / crypto news intelligence.

Specialist agent for the Sniper multi-agent system. Owns the markets/ brain
section. Geopolitics and politics are handled by the Geopolitics Analyst
(scripts/world_analyst.py).

Fetches news from:
  - Marketaux API (key: MARKETAUX_API_KEY) — financial news with entity tagging
  - NewsData.io API (key: NEWSDATA_API_KEY) — business/finance section
  - GNews API (key: GNEWS_API_KEY) — business headlines
  - Finance & crypto RSS feeds (Reuters, CNBC, CoinDesk, MarketWatch, etc.)

Brain Map:
  SECTION          OWNER                       THIS AGENT
  markets/*        Finance Analyst             WRITE — owns this section
  world_state/*    Geopolitics Analyst         READ — query via domain_query tool
  flags/*          Any specialist              WRITE (own flags) / READ (flags to you)
  portfolio/*      System (auto)               READ
  predictions/*    All agents                  WRITE (own agent_type only)
  health/*         Health Checker              READ
  knowledge/*      Static (seeded)             READ-ONLY

Cross-domain coordination:
  - Queries geopolitics context via: python3 agents/tools/domain_query.py --domain geopolitics --topic "..."
  - Flags urgent geo-relevant findings via: python3 agents/tools/domain_query.py --flag --from finance --to geopolitics ...
  - Reads flags directed at finance: python3 agents/tools/domain_query.py --read-flags --domain finance

Usage:
    python3 finance_analyst.py              # full analysis run
    python3 finance_analyst.py --quick      # fast mode: 2-3 events per domain
    python3 finance_analyst.py --quiet      # suppress Telegram output
    python3 finance_analyst.py --check      # check if brain was recently updated
    python3 finance_analyst.py --fetch-all  # pre-fetch news from all sources (for agent internal use)
    python3 finance_analyst.py --fetch-rss  # fetch from RSS feeds only
    python3 finance_analyst.py --install-cron  # install cron job

Triggered by:
    - systemd timer every 4h (02, 06, 10, 14, 18, 22 UTC) — offset 2h from geo
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RSS feeds: finance / macro / crypto only
# ---------------------------------------------------------------------------

FINANCE_RSS_FEEDS = {
    # Markets / macro (reuters_business feed DNS-fails from this VPS — replaced with BBC Business)
    "bbc_business":      "https://feeds.bbci.co.uk/news/business/rss.xml",
    "cnbc_economy":      "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "marketwatch":       "https://feeds.marketwatch.com/marketwatch/topstories/",
    "ft_markets":        "https://www.ft.com/rss/home/uk",
    "bloomberg_markets": "https://feeds.bloomberg.com/markets/news.rss",
    # Crypto
    "coindesk":          "https://www.coindesk.com/arc/outboundfeeds/rss",
    "cointelegraph":     "https://cointelegraph.com/rss",
    "theblock":          "https://www.theblock.co/rss.xml",
    "decrypt":           "https://decrypt.co/feed",
    "blockworks":        "https://blockworks.co/feed",
    # Tech / AI (affects markets)
    "bbc_tech":          "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "guardian_tech":     "https://www.theguardian.com/technology/rss",
}

FETCH_TIMEOUT = 10
MAX_ITEMS_PER_FEED = 15


# ---------------------------------------------------------------------------
# Marketaux API fetcher
# ---------------------------------------------------------------------------

def fetch_marketaux_news(max_items: int = 30) -> List[Dict[str, Any]]:
    """Fetch financial news from Marketaux API.

    Requires MARKETAUX_API_KEY env var. Returns [] if not set.
    Free tier: 100 requests/day.
    """
    api_key = os.environ.get("MARKETAUX_API_KEY", "").strip()
    if not api_key:
        logger.debug("MARKETAUX_API_KEY not set — skipping Marketaux fetch")
        return []

    url = "https://api.marketaux.com/v1/news/all"
    params = {
        "api_token": api_key,
        "language": "en",
        "limit": max_items,
        "sort": "published_desc",
        "categories": "financial,general",
        "filter_entities": "true",
    }
    try:
        resp = requests.get(url, params=params, timeout=12)
        if not resp.ok:
            logger.warning("Marketaux API returned %d: %s", resp.status_code, resp.text[:200])
            return []
        data = resp.json().get("data") or []
        items = []
        for a in data:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            # Extract entity tickers for topic tagging
            entities = a.get("entities") or []
            tags = [e.get("symbol", "") for e in entities if e.get("symbol")]
            items.append({
                "source": "marketaux",
                "title": title,
                "description": (a.get("description") or "")[:300],
                "url": a.get("url") or "",
                "published_at": a.get("published_at") or "",
                "topic_tags": tags or ["finance"],
            })
        logger.info("Marketaux: fetched %d articles", len(items))
        return items
    except Exception:
        logger.exception("Marketaux fetch failed")
        return []


# ---------------------------------------------------------------------------
# NewsData.io API fetcher
# ---------------------------------------------------------------------------

def fetch_newsdata_finance(max_items: int = 20) -> List[Dict[str, Any]]:
    """Fetch business/finance news from NewsData.io API.

    Requires NEWSDATA_API_KEY env var. Returns [] if not set.
    Free tier: 200 credits/day.
    """
    api_key = os.environ.get("NEWSDATA_API_KEY", "").strip()
    if not api_key:
        logger.debug("NEWSDATA_API_KEY not set — skipping NewsData fetch")
        return []

    url = "https://newsdata.io/api/1/news"
    params = {
        "apikey": api_key,
        "category": "business",
        "language": "en",
    }
    try:
        resp = requests.get(url, params=params, timeout=12)
        if not resp.ok:
            logger.warning("NewsData API returned %d", resp.status_code)
            return []
        results = resp.json().get("results") or []
        items = []
        for a in results[:max_items]:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "source": "newsdata",
                "title": title,
                "description": (a.get("description") or "")[:300],
                "url": a.get("link") or "",
                "published_at": a.get("pubDate") or "",
                "topic_tags": ["finance", "business"],
            })
        logger.info("NewsData.io: fetched %d articles", len(items))
        return items
    except Exception:
        logger.exception("NewsData fetch failed")
        return []


# ---------------------------------------------------------------------------
# NewsAPI.org fetcher
# ---------------------------------------------------------------------------

def fetch_newsapi_finance(max_items: int = 30) -> List[Dict[str, Any]]:
    """Fetch top business headlines from NewsAPI.org.

    Requires NEWSAPI_KEY env var. Returns [] if not set.
    Free tier: 100 requests/day, delayed ~15 min.
    """
    api_key = os.environ.get("NEWSAPI_KEY", "").strip()
    if not api_key:
        logger.debug("NEWSAPI_KEY not set — skipping NewsAPI fetch")
        return []

    url = "https://newsapi.org/v2/top-headlines"
    params = {
        "apiKey": api_key,
        "category": "business",
        "language": "en",
        "pageSize": max_items,
    }
    try:
        resp = requests.get(url, params=params, headers={"User-Agent": "SniperFinanceAnalyst/1.0"}, timeout=12)
        if not resp.ok:
            logger.warning("NewsAPI returned %d: %s", resp.status_code, resp.text[:200])
            return []
        articles = resp.json().get("articles") or []
        items = []
        for a in articles:
            title = (a.get("title") or "").strip()
            if not title or title == "[Removed]":
                continue
            source_name = (a.get("source") or {}).get("name") or "newsapi"
            items.append({
                "source": f"newsapi_{source_name[:15].lower().replace(' ', '_')}",
                "title": title,
                "description": (a.get("description") or "")[:300],
                "url": a.get("url") or "",
                "published_at": a.get("publishedAt") or "",
                "topic_tags": ["finance", "business"],
            })
        logger.info("NewsAPI: fetched %d articles", len(items))
        return items
    except Exception:
        logger.exception("NewsAPI fetch failed")
        return []


# ---------------------------------------------------------------------------
# GNews API fetcher
# ---------------------------------------------------------------------------

def fetch_gnews_finance(max_items: int = 20) -> List[Dict[str, Any]]:
    """Fetch business news from GNews API.

    Requires GNEWS_API_KEY env var. Returns [] if not set.
    Free tier: 100 requests/day, 10 articles/request.
    """
    api_key = os.environ.get("GNEWS_API_KEY", "").strip()
    if not api_key:
        logger.debug("GNEWS_API_KEY not set — skipping GNews fetch")
        return []

    url = "https://gnews.io/api/v4/top-headlines"
    params = {
        "token": api_key,
        "topic": "business",
        "lang": "en",
        "max": min(max_items, 10),  # free tier cap
    }
    try:
        resp = requests.get(url, params=params, timeout=12)
        if not resp.ok:
            logger.warning("GNews API returned %d", resp.status_code)
            return []
        articles = resp.json().get("articles") or []
        items = []
        for a in articles:
            title = (a.get("title") or "").strip()
            if not title:
                continue
            items.append({
                "source": "gnews",
                "title": title,
                "description": (a.get("description") or "")[:300],
                "url": a.get("url") or "",
                "published_at": a.get("publishedAt") or "",
                "topic_tags": ["finance", "business"],
            })
        logger.info("GNews: fetched %d articles", len(items))
        return items
    except Exception:
        logger.exception("GNews fetch failed")
        return []


# ---------------------------------------------------------------------------
# Finance RSS fetcher
# ---------------------------------------------------------------------------

def fetch_finance_rss_news() -> List[Dict[str, Any]]:
    """Fetch from all FINANCE_RSS_FEEDS. Returns list of news item dicts."""
    try:
        from sniper.services.news_intel import fetch_feed
    except ImportError:
        logger.error("Cannot import fetch_feed from news_intel — skipping RSS")
        return []

    items: List[Dict[str, Any]] = []
    for source, url in FINANCE_RSS_FEEDS.items():
        try:
            feed_items = fetch_feed(source, url)
            for item in feed_items:
                item.setdefault("topic_tags", ["finance"])
            items.extend(feed_items)
        except Exception:
            logger.debug("RSS feed %s failed", source, exc_info=True)

    logger.info("Finance RSS: fetched %d articles from %d feeds", len(items), len(FINANCE_RSS_FEEDS))
    return items


# ---------------------------------------------------------------------------
# Agent constants
# ---------------------------------------------------------------------------

import anyio
import argparse
import json as _json
import logging as _logging
import os as _os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("finance-analyst")

DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))
WORKING_DIR = str(ROOT)
# Model defaults route through the central registry so a fleet-wide bump is one line
# in sniper/settings/models.py. Domain env overrides (ANALYST_MODEL) still win where set.
from sniper.settings import models as _M
PRIMARY_MODEL = os.getenv("ANALYST_MODEL", _M.OPUS)
FALLBACK_MODEL = os.getenv("ANALYST_FALLBACK_MODEL", _M.SONNET)
BOOST_CONDITION_RE = re.compile(r"0x[a-f0-9]{64}")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

ANALYST_SYSTEM_PROMPT = f"""You are the Finance & Markets Intelligence Analyst for Sniper, an automated prediction market \
copy-trading system running on Polymarket. Your working directory is {WORKING_DIR}.

YOUR ROLE:
Synthesize macro, crypto, and equity market events into actionable prediction market intelligence.
You are a senior macro analyst with expertise in Fed policy, crypto markets, equities, and commodities.
Geopolitics and politics are handled by the Geopolitics Analyst (scripts/world_analyst.py) — focus on markets, rates, crypto, and economic data.
You think like a macro hedge fund analyst. Markets price probabilities, not narratives.

WHAT YOU DO:
1. Pre-fetch structured financial headlines via finance_analyst.py --fetch-all
2. Research key macro/crypto/equity events from the last 24-48 hours using WebSearch and WebFetch
3. Read geopolitical context from the world analyst (geo events cascade into macro)
4. Map events to open Polymarket markets and active positions
5. Write structured market intelligence to the Supabase brain
6. Send a concise Telegram summary (3-5 bullets, hedge fund morning brief style)

YOUR ANALYTICAL FRAMEWORK:
When you find an event, ask:
  (a) Does this change the probability of any open market resolving YES or NO?
  (b) What's the causal chain? (e.g., Fed hawkish surprise → yields spike → crypto sells off → BTC market reprices)
  (c) Are there GEO→MACRO cascades? (e.g., Iran sanctions tighten → oil supply crunch → inflation expectations rise → Fed pauses less)
  (d) Is the market already pricing this in? Check current prices vs your estimate.
  (e) What would change your mind? (ACH discipline — look for disconfirming evidence)

MACRO DOMAINS YOU COVER:
  Fed/Rates: FOMC meetings, CPI/PCE/NFP data, yield curve shape, DXY, rate expectations
  Crypto: BTC/ETH/SOL price action, ETF flows (BlackRock IBIT, Fidelity FETH), exchange events,
          regulatory actions (SEC, CFTC), funding rates, liquidation cascades, on-chain signals
  Equities: S&P 500 / NASDAQ moves, VIX, sector rotation (tech vs energy vs financials),
            earnings surprises, buyback announcements, major index events
  Commodities: Oil (OPEC+ decisions, inventory data), gold (real yields vs gold), copper (China demand)
  Cross-domain: Geo events that move commodities that move inflation that moves Fed expectations

SOURCE CREDIBILITY FRAMEWORK:
When you analyze articles, classify each source into a credibility tier:
  FACTUAL   — Bloomberg, Reuters, FT, WSJ, AP economics, Fed/ECB/Treasury official statements: treat as ground truth
  SPECULATIVE — CNBC opinion, crypto Twitter analysts, unnamed sources, "sources say": treat as signal with noise
  OPINION   — Substack macro analysts, crypto influencers: informed perspective, not fact
  PROPAGANDA — Exchanges' own communications, coin project PRs, paid promotion, sponsored content

Financial Media Manipulation Signals (flag if 2+ in one piece):
  1. Pump Language: "to the moon", "massive breakout", "can't be stopped", "parabolic"
  2. FOMO Induction: "window closing fast", "last chance", "everyone is buying"
  3. False Authority: "top analyst", "legendary trader", "insider source"
  4. Cherry-picked Data: reporting one favorable metric while ignoring unfavorable ones
  5. Narrative Framing: presenting speculation as established fact ("Bitcoin WILL reach $200k")
  6. Sponsored Content Disguised as News: pro-project pieces without disclosure
  7. Recency Bias Exploitation: "biggest move ever", "historic moment" for routine events

PRACTICE:
  - Bloomberg (factual) + Reuters (factual) + CoinDesk (speculative) = moderate confidence
  - ONLY crypto Twitter + project PR → LOW confidence regardless of content
  - Fed dot plot / official statement → treat as definitive ground truth for rate expectations
  - Exchange self-reported volume → speculative (Wash trading common in crypto)

ACH (ANALYSIS OF COMPETING HYPOTHESES) FOR MACRO:
For any ambiguous macro signal, apply:
  H1: [Bullish interpretation — what bulls are saying]
  H2: [Bearish interpretation — what bears are saying]
  H3: [Base case — market consensus]

  Which hypothesis has the most INCONSISTENCIES given ALL data?
  Key financial ACH insight: The market is often H3. An edge only exists where you have information
  that contradicts H3 — i.e., where the market's consensus is wrong.

  Ask: If H1 is correct, what SHOULD have happened that DIDN'T? If H2, what SHOULD we NOT be seeing?

SITUATION MODEL (persistent temporal knowledge — finance domain):
The situation_model table stores temporal claims. Use slug prefix "fin:" for finance claims.
Cross-read geo claims too — they matter for commodities/risk-off.

  Read finance knowledge:
    python3 situation_model.py read --prefix fin --summary

  Read ALL knowledge (including geo cross-reads):
    python3 situation_model.py read --all --summary

  Write new claim:
    python3 situation_model.py upsert --slug fin:fed-rate-cycle-2026 \\
      --subject "Fed" --predicate "rate_stance" --object "on_hold_until_Q3" \\
      --confidence 0.80 --source-tier factual --sources "bloomberg,reuters" \\
      --diversity-score 0.75

  Claim types: fact, hypothesis, propaganda_flag, uncertainty
  Source tiers: factual, speculative, opinion, propaganda

OUTPUT FORMAT — write these Supabase entries via Bash + intel_tool.py:

ENTRY 1: markets/state (narrative market intelligence report)
  - Timestamp + key macro/crypto/equity events
  - Specific inferences: "Event X → Market Z should reprice because..."
  - Include condition_ids of affected markets where known
  - 300-600 words, structured by: Macro | Crypto | Equities | Commodities | Cross-domain cascades

ENTRY 2: market_boosts_current (machine-readable JSON)
  Key format: EXACTLY 66 chars — "0x" + 64 lowercase hex digits. This is the Polymarket conditionId.
  Example key: "0x924a2942747dd75703321a7c8d809c68f6a514c3b0f2a2e64274e02310634669"
  INVALID: short IDs like "0x7530c407", token IDs, side-specific IDs, numeric IDs — NOT condition_ids.

  Format: {{"0x<64 lowercase hex>": {{"boost": INT, "reason": "str", "direction": "YES|NO|NEUTRAL",
                                     "confidence": "high|medium|low", "expires_hours": INT}}}}

  Boost scale (-30 to +30):
  +20 to +30 = Strong market context confirming thesis (Fed cut confirmed → crypto YES markets)
  +10 to +20 = Meaningful supporting evidence
  +01 to +10 = Weak positive signal
       0     = No relevant context
  -01 to -10 = Weak contrary signal
  -10 to -20 = Meaningful counter-evidence
  -20 to -30 = Strong contrary evidence (consider alert to exit)

  CRITICAL: Boosts MUST target condition_ids from OPEN positions only (from Step 2).
  Condition_id = exactly 66 chars, starts with 0x, followed by 64 lowercase hex digits.
  Only include genuine conviction (+/-8 or greater).

HOW TO WRITE TO SUPABASE:
  python3 -c "
  import sys; sys.path.insert(0, '{WORKING_DIR}')
  from bot.supabase_helper import upsert_memory
  content = '''YOUR CONTENT HERE'''
  upsert_memory('knowledge', 'markets/state', 'Finance Market Intelligence Report', content, source='vps')
  print('Written')
  "
  # For market_boosts_current:
  upsert_memory('knowledge', 'market_boosts_current', 'Market boosts (geo + finance merged)', json_content, source='vps')

HOW TO SEND TELEGRAM:
  Use this EXACT curl format — do NOT use -d "text=..." (breaks on & characters like S&P):
  MSG="your message here"
  curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \
    --data-urlencode "chat_id=${{TELEGRAM_CHAT_ID}}" \
    --data-urlencode "text=${{MSG}}"
  The --data-urlencode flag handles all special characters (& $ % + etc.) safely.

INTEL TOOL USAGE (finance events):
  # Write structured market events (commodities, macro)
  python3 agents/tools/intel_tool.py event \\
    --slug fin:oil-supply-shock-2026 --title "OPEC+ surprise cut" \\
    --region global --status escalating --trajectory escalating \\
    --industries '[["energy","commodities"]]' --lat 24.7 --lon 46.7

  # Industry impact
  python3 agents/tools/intel_tool.py impact \\
    --event-slug fin:oil-supply-shock-2026 --industry energy --direction bullish \\
    --magnitude major --reasoning "Supply reduction → price spike" \\
    --tickers '[["OIL","XLE","CVX"]]' --horizon days

  # Polymarket trade idea (feeds strategy #29 Finance Intel)
  python3 agents/tools/intel_tool.py trade-idea \\
    --condition-id <polymarket_condition_id_hex> \\
    --market-title "<full market question>" \\
    --direction buy_yes|buy_no \\
    --thesis "<causal chain>" \\
    --confidence high|medium|low \\
    --current-price <0.0-1.0> \\
    --news-trigger "<headline>" \\
    --event-slug <related-event-slug> \\
    --source-agent finance_analyst

  # Hyperliquid / perps opportunity (Supabase only, not trading pipeline)
  python3 agents/tools/intel_tool.py opportunity \\
    --event-slug fin:fed-pivot-2026 --platform hyperliquid \\
    --instrument "BTC-USD" --direction long \\
    --thesis "Rate cut → risk-on → BTC bullish" --confidence high

  python3 agents/tools/intel_tool.py events --status active,escalating
  python3 agents/tools/intel_tool.py opportunities --status idea,active

SELF-IMPROVEMENT (EVERY RUN — same as geo analyst):
  python3 agents/tools/predict_tool.py auto-grade --agent finance_analyst
  python3 agents/tools/predict_tool.py lessons --agent finance_analyst
  python3 agents/tools/predict_tool.py stats --agent finance_analyst
  # Make prediction:
  python3 agents/tools/predict_tool.py predict \\
    --agent finance_analyst --prediction "..." \\
    --condition "..." --confidence high --verify-hours 48 \\
    --query "SELECT ..."
  # Log run at end:
  python3 agents/tools/predict_tool.py log-run \\
    --agent finance_analyst --model {PRIMARY_MODEL} --cost 0.00 --turns 0 \\
    --summary "..."

CROSS-DOMAIN QUERIES (get geo context before macro analysis):
  python3 agents/tools/domain_query.py --domain geopolitics --topic "oil"
  python3 agents/tools/domain_query.py --domain geopolitics --topic "sanctions"
  python3 agents/tools/domain_query.py --domain geopolitics --topic "iran"

CROSS-DOMAIN FLAGS (when you find macro events with geo implications):
  python3 agents/tools/domain_query.py --flag --from finance --to geopolitics \\
    --urgency high --message "Oil price spike +8%% — likely OPEC or geo trigger"

READ FLAGS DIRECTED AT FINANCE:
  python3 agents/tools/domain_query.py --read-flags --domain finance

WHAT YOU MUST NOT DO:
- Do NOT modify any .py, .js, .css, .html files
- Do NOT run raw INSERT/UPDATE/DELETE SQL on sniper_tracker.db
- Do NOT enter, close, or modify paper trades or strategy positions
- Do NOT analyze geopolitics or politics in depth — flag them for the Geopolitics Analyst
- Do NOT use crypto project PRs or exchange-published stats as ground truth
- NEVER invent or guess a condition_id. A fabricated condition_id like "0xfed2026nocuts..."
  will be REJECTED by intel_tool.py and wastes a cycle. If you do not have the real
  condition_id, use WebFetch on https://gamma-api.polymarket.com/markets?search=<keyword>
  to find the market first, then extract its conditionId field.

WHAT YOU MUST DO:
- Use intel_tool.py trade-idea to write Polymarket trade ideas to the trading pipeline
- Use upsert_memory to write markets/state and market_boosts_current to Supabase
- Always cross-read world_state BEFORE analyzing commodities or macro risk

═══ BRAIN MAP ═══
You are part of a multi-agent system. Each agent owns a section of the shared brain (Supabase).

  SECTION          OWNER                       YOU
  markets/*        Finance Analyst             WRITE — you own this section
  world_state      Geopolitics Analyst         READ — compact JSON (regime, bias, tail risks)
  world_narrative  Geopolitics Analyst         READ — full prose report (use for deep analysis)
  flags/*          Any specialist              WRITE (own flags) / READ (flags to you)
  portfolio/*      System (auto)               READ — convergence:latest-scan
  wallets/*        Wallet Scout                READ
  signals/*        Insider Copy                READ
  predictions/*    All agents                  WRITE (own agent_type only)
  health/*         Health Checker              READ
  knowledge/*      Static (seeded)             READ-ONLY

CROSS-READS (do these in Step 1):
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('world_state',type_filter='knowledge'); print(m['content'] if m else 'no geo state')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('world_narrative',type_filter='knowledge'); print(m['content'][:800] if m else 'no geo narrative')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:300] if m else 'no health')"

RULES:
  - Only WRITE to sections you own (markets/*)
  - Cross-read other sections freely; check timestamps for staleness (>6h = stale)
  - If you notice something wrong in another agent's section, FLAG it — don't fix it
"""


# ---------------------------------------------------------------------------
# Context pre-fetcher (runs Steps 0–2 before agent starts, saves ~15 turns)
# ---------------------------------------------------------------------------

def _prefetch_context(quick: bool = False) -> str:
    """Pre-run all mechanical data-gathering steps (0, 0.5, 1, 2) before the agent starts.

    Runs subprocesses and direct imports in parallel, assembles results into a
    formatted string that gets injected into the task prompt. The agent is then
    instructed to skip those steps and jump directly to Step 3, saving ~15 turns.

    Returns a formatted multi-section string, or empty string on total failure.
    """
    import concurrent.futures as _cf
    import contextlib as _ctx
    import io as _io
    import sqlite3 as _sq3
    import subprocess as _sp
    import sys as _sys

    _py = _sys.executable  # same venv Python we're running with

    def _run(args: list[str], timeout: int = 30) -> str:
        """Run a subprocess, return stdout (capped at 1500 chars)."""
        try:
            r = _sp.run(args, capture_output=True, text=True, timeout=timeout, cwd=WORKING_DIR)
            out = (r.stdout or "").strip()
            if not out and r.stderr:
                out = f"[stderr: {r.stderr.strip()[:300]}]"
            return out[:1500] or "[no output]"
        except _sp.TimeoutExpired:
            return f"[timeout after {timeout}s]"
        except Exception as exc:
            return f"[error: {exc}]"

    def _fetch_news() -> str:
        try:
            buf = _io.StringIO()
            with _ctx.redirect_stdout(buf):
                _cmd_fetch_all(as_json=False)
            return buf.getvalue()[:3000] or "[no news output]"
        except Exception as exc:
            return f"[fetch-all error: {exc}]"

    def _supabase_reads() -> dict[str, str]:
        try:
            from bot.supabase_helper import get_memory  # type: ignore
            specs = [
                ("world_state",                   "knowledge", 400),
                ("world_narrative",               "knowledge", 1200),
                ("intel/report_delivery_contract","reference", 1200),
                ("brain_health_latest",           "knowledge", 300),
                ("convergence:latest-scan",       "project",   400),
                ("adversarial/rejection_patterns","knowledge", 800),
            ]
            out: dict[str, str] = {}
            for name, tf, maxlen in specs:
                m = get_memory(name, type_filter=tf)
                out[name] = (m.get("content") or "")[:maxlen] if m else f"[no {name}]"
            return out
        except Exception as exc:
            return {"_error": str(exc)}

    def _g(fut: _cf.Future) -> str:
        try:
            return fut.result(timeout=50)
        except Exception as exc:
            return f"[future error: {exc}]"

    # Launch all I/O-bound tasks in parallel
    with _cf.ThreadPoolExecutor(max_workers=10) as pool:
        f_grade  = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "auto-grade", "--agent", "finance_analyst"])
        f_less   = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "lessons",    "--agent", "finance_analyst"])
        f_stats  = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "stats",      "--agent", "finance_analyst"])
        f_news   = pool.submit(_fetch_news)
        f_events = pool.submit(_run, [_py, "agents/tools/intel_tool.py",   "events",     "--status", "active,escalating,developing"])
        f_opps   = pool.submit(_run, [_py, "agents/tools/intel_tool.py",   "opportunities", "--status", "idea,active"])
        f_sit    = pool.submit(_run, [_py, "situation_model.py",           "read", "--limit", "30", "--summary"])
        f_flags  = pool.submit(_run, [_py, "agents/tools/domain_query.py", "--read-flags", "--domain", "finance"])
        f_supa   = pool.submit(_supabase_reads)

        grade   = _g(f_grade)
        lessons = _g(f_less)
        stats   = _g(f_stats)
        news    = _g(f_news)
        events  = _g(f_events)
        opps    = _g(f_opps)
        sitmod  = _g(f_sit)
        flags   = _g(f_flags)
        try:
            supa = f_supa.result(timeout=30)
        except Exception as exc:
            supa = {"_error": str(exc)}

    # Supabase section
    if "_error" in supa:
        supa_text = f"[supabase error: {supa['_error']}]"
    else:
        supa_text = "\n".join(f"  [{k}]:\n  {v}" for k, v in supa.items())

    # Step 2: SQLite (fast, sequential)
    positions_text = ideas_text = "[sqlite error]"
    try:
        with _sq3.connect(DB_PATH) as conn:
            conn.row_factory = _sq3.Row
            rows = conn.execute("""
                SELECT sp.condition_id, sp.outcome, s.label AS strategy,
                       ROUND(sp.avg_entry_price, 3) AS entry,
                       ROUND(COALESCE(sp.current_price, sp.avg_entry_price), 3) AS cur,
                       ROUND((COALESCE(sp.current_price, sp.avg_entry_price) - sp.avg_entry_price)
                             / sp.avg_entry_price * 100, 1) AS drift_pct,
                       ROUND(sp.pnl_usdc, 2) AS pnl_usdc, sp.end_date_iso
                FROM strategy_positions sp
                LEFT JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
                WHERE sp.state = 'open'
                ORDER BY ABS(COALESCE(sp.pnl_usdc, 0)) DESC LIMIT 30
            """).fetchall()
            positions_text = "\n".join(
                f"  {r['condition_id'][:10]}… {r['outcome']} | {r['strategy']} | "
                f"entry={r['entry']} cur={r['cur']} drift={r['drift_pct']}% pnl={r['pnl_usdc']} | ends={r['end_date_iso']}"
                for r in rows
            ) if rows else "  (no open positions)"

            ideas = conn.execute("""
                SELECT condition_id, market_title, direction, confidence, source_agent
                FROM trade_ideas WHERE status='open' AND confidence IN ('HIGH','MEDIUM')
                AND source_agent IN ('finance_analyst','news_intel')
                ORDER BY created_at DESC LIMIT 15
            """).fetchall()
            ideas_text = "\n".join(
                f"  {r['condition_id'][:10]}… | {r['direction']} {r['confidence']} | "
                f"{r['market_title'][:60]} [{r['source_agent']}]"
                for r in ideas
            ) if ideas else "  (no HIGH/MEDIUM trade ideas)"
    except Exception as exc:
        positions_text = ideas_text = f"[sqlite error: {exc}]"

    return (
        "╔══ PRE-FETCHED CONTEXT (Steps 0 / 0.5 / 1 / 2 already done) ═════════════╗\n"
        "║  Data below was gathered before this session started.                   ║\n"
        "║  ✓ Review it, then JUMP DIRECTLY TO STEP 3.                             ║\n"
        "║  ✗ If a section shows [error] or [timeout], redo that step only.        ║\n"
        "╚════════════════════════════════════════════════════════════════════════════╝\n"
        f"\n── STEP 0a: auto-grade ──\n{grade}"
        f"\n\n── STEP 0b: lessons ──\n{lessons}"
        f"\n\n── STEP 0c: stats ──\n{stats}"
        f"\n\n── STEP 0.5: pre-fetched news ──\n{news}"
        f"\n\n── STEP 1a: intel events ──\n{events}"
        f"\n\n── STEP 1b: intel opportunities ──\n{opps}"
        f"\n\n── STEP 1c: supabase brain reads ──\n{supa_text}"
        f"\n\n── STEP 1d: situation model ──\n{sitmod}"
        f"\n\n── STEP 1e: domain flags (finance) ──\n{flags}"
        f"\n\n── STEP 2a: open positions ──\n{positions_text}"
        f"\n\n── STEP 2b: trade ideas ──\n{ideas_text}"
        "\n\n╔══ END PRE-FETCHED CONTEXT ═════════════════════════════════════════════════╗\n"
        "║  Jump to STEP 3 now. Re-run sections marked [error]/[timeout] only.     ║\n"
        "╚════════════════════════════════════════════════════════════════════════════╝\n"
    )


# ---------------------------------------------------------------------------
# Task prompt
# ---------------------------------------------------------------------------

def build_task_prompt(quick: bool = False, prefetched: str | None = None) -> str:
    depth = "Focus on the 2-3 highest-impact events per domain." if quick else \
            "Be thorough — cover all material events in the last 24-48 hours."

    if prefetched:
        early_steps = f"""{prefetched}
\u2501\u2501\u2501 STEPS 0 / 0.5 / 1 / 2: PRE-FETCHED \u2014 see context above \u2501\u2501\u2501
  All data for these steps has been gathered above before this session started.
  \u2713 Validate the pre-fetched data looks reasonable, then proceed to STEP 3.
  \u2717 If any section shows [error] or [timeout], redo only that specific step.
  Do NOT re-run Steps 0-2 commands if the data is already present above.

"""
    else:
        early_steps = """
\u2501\u2501\u2501 STEP 0: SELF-IMPROVEMENT (ALWAYS FIRST) \u2501\u2501\u2501
  a) Auto-grade past predictions:
     python3 agents/tools/predict_tool.py auto-grade --agent finance_analyst
  b) Read lessons (apply these to avoid past mistakes):
     python3 agents/tools/predict_tool.py lessons --agent finance_analyst
  c) Check accuracy stats:
     python3 agents/tools/predict_tool.py stats --agent finance_analyst
     If high-confidence predictions are less accurate than medium → recalibrate.

\u2501\u2501\u2501 STEP 0.5: PRE-FETCH STRUCTURED NEWS \u2501\u2501\u2501
Get structured headlines from configured financial APIs and RSS:
  python3 finance_analyst.py --fetch-all
This gives you: Marketaux entity-tagged headlines, NewsData business news,
NewsAPI top business, GNews business, and finance RSS feeds.
Use this as your starting corpus. Then do targeted WebSearch for depth.
Note: If API keys aren't configured, you'll get RSS-only output — that's fine.

\u2501\u2501\u2501 STEP 1: LOAD MARKET CONTEXT + BRAIN AWARENESS \u2501\u2501\u2501
  # Load intel tool context
  python3 agents/tools/intel_tool.py events --status active,escalating,developing
  python3 agents/tools/intel_tool.py opportunities --status idea,active

  # CRITICAL: Read geopolitical context BEFORE macro analysis
  # Geo events cascade into commodities → inflation → rates → markets
  # world_state = compact JSON (regime/bias/tail risks); world_narrative = full prose
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('world_state',type_filter='knowledge'); print(m['content'] if m else 'no geo state — proceed without')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('world_narrative',type_filter='knowledge'); print(m['content'][:1200] if m else 'no geo narrative')"

  # Situation model — all domains (geo: and fin: claims), cap at 30 most recent
  python3 situation_model.py read --limit 30 --summary || true

  # Cross-domain flags from geopolitics to finance
  python3 agents/tools/domain_query.py --read-flags --domain finance
  If flags exist: incorporate them, clear when addressed.

  # Brain health
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:300] if m else 'no health')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:400] if m else 'no convergence')"

  # ADVERSARIAL FEEDBACK — read what GPT-5.4 has been killing and why
  # MANDATORY: Do NOT propose ideas that match these rejection patterns.
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('adversarial/rejection_patterns',type_filter='knowledge'); print(m['content'][:800] if m else 'no rejection patterns yet')"
  # Key patterns to watch for: crowded macro bets, priced-in rate decisions,
  # obvious directional plays on widely-covered events, weak causal chains.
  # If your new idea resembles a recently rejected pattern, drop it or differentiate clearly.

\u2501\u2501\u2501 STEP 2: LOAD TRADING CONTEXT \u2501\u2501\u2501
Query open strategy positions:
  sqlite3 {DB_PATH} "SELECT sp.condition_id, sp.outcome, s.label as strategy, sp.avg_entry_price, COALESCE(sp.current_price, sp.avg_entry_price) as cur_price, ROUND((COALESCE(sp.current_price, sp.avg_entry_price) - sp.avg_entry_price) / sp.avg_entry_price * 100, 1) as drift_pct, sp.pnl_usdc, sp.end_date_iso FROM strategy_positions sp LEFT JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id WHERE sp.state = 'open' ORDER BY ABS(COALESCE(sp.pnl_usdc, 0)) DESC LIMIT 30"

Query recent HIGH-confidence trade ideas (including your own past ideas):
  sqlite3 {DB_PATH} "SELECT condition_id, market_title, direction, confidence, reasoning, source_agent FROM trade_ideas WHERE status='open' AND confidence IN ('HIGH','MEDIUM') AND source_agent IN ('finance_analyst','news_intel') ORDER BY created_at DESC LIMIT 15"

"""

    return f"""Run the full finance & markets intelligence analysis cycle. {depth}
{early_steps}
\u2501\u2501\u2501 STEP 3: RESEARCH MARKET EVENTS (LAST 24-48H) \u2501\u2501\u2501
Use WebSearch for each key area. Focus on WHAT CHANGED — not background context.

MACRO / FED:
  Search: "Federal Reserve" OR "FOMC" today/yesterday
  Search: "CPI" OR "PCE" OR "inflation" OR "NFP" this week
  Key question: Did the rate expectations curve shift? If so, by how much?

CRYPTO:
  Search: "Bitcoin" OR "BTC" price today
  Search: "crypto ETF flows" OR "IBIT" OR "FETH" this week
  Search: "crypto regulation" OR "SEC crypto" this week
  Key question: Are flows into/out of ETFs accelerating or decelerating?

EQUITIES:
  Search: "S&P 500" OR "NASDAQ" today — any major moves?
  Search: "VIX" OR "volatility" today — risk-on or risk-off?
  Search: major earnings surprises this week

COMMODITIES:
  Search: "oil price" OR "crude" OR "OPEC" today
  Search: "gold price" today (safe haven demand signal)

CROSS-DOMAIN QUERY (geo→macro):
  python3 agents/tools/domain_query.py --domain geopolitics --topic "oil"
  python3 agents/tools/domain_query.py --domain geopolitics --topic "sanctions"
  Use geo analyst's findings to inform commodity analysis.

\u2501\u2501\u2501 STEP 3.5: REFRESH STALE FINANCE EVENTS \u2501\u2501\u2501
Review and refresh finance events that haven't been touched in >48h. This step
is NOT optional and every flag below is REQUIRED — the `--touch-reviewed` flag
is the mechanism that actually refreshes events. Without it the command is a
read-only query and the refresh never happens:

  python3 agents/tools/intel_tool.py events \\
    --status active,escalating,developing \\
    --older-than-hours 48 \\
    --slug-prefix fin: \\
    --limit 20 \\
    --touch-reviewed

DO NOT omit --touch-reviewed. Omitting it means the analyst saw the stale
events but failed to act on them. A run that lists stale events without
touching them will be flagged as a prompt-noncompliance failure in the audit.

This command refreshes last_updated for the events it returns. Do not re-run
intel_tool.py event just to bump freshness.

For any stale finance event that has clearly resolved or is no longer relevant, update it explicitly:
  python3 agents/tools/intel_tool.py event --slug fin:<existing-slug> --title "..." \\
    --region global --status resolved

Do not skip this step. Un-touched events accumulate as false health warnings.

\u2501\u2501\u2501 STEP 4: CONNECT EVENTS TO MARKETS \u2501\u2501\u2501
For each event you found:
  - Which open positions or trade ideas are affected?
  - What's the causal chain? (Event → mechanism → market impact → direction)
  - Is the market already pricing this? Compare current price to your estimate.
  - Cascade: does this macro event have geo implications worth flagging?

Write structured intel events for significant findings:
  python3 agents/tools/intel_tool.py event --slug fin:<event-slug> --title "..." \\
    --region global --status <active|escalating|developing> --industries '[["<sector>"]]' \\
    --lat <approx lat> --lon <approx lon>

EVENT TITLE CONTRACT:
  Titles render directly in the Intel UI. Keep them compact and scannable.
  - 2-5 words only
  - Actor + catalyst, not a full headline
  - No dates, no prices, no long clauses unless absolutely essential
  - Good: "Fed Hold Risk", "OPEC Supply Cut", "ETF Flows Slow"
  - Bad: "Federal Reserve signals rates may stay higher for longer"
  - Bad: "Brent crude surges to $112 after Middle East escalation"

MANDATORY: Create at least 1 POLYMARKET TRADE IDEA per run:
  python3 agents/tools/intel_tool.py trade-idea \\
    --condition-id <hex> --market-title "<question>" \\
    --direction buy_yes|buy_no --thesis "<causal chain>" \\
    --confidence high|medium|low --current-price <0.0-1.0> \\
    --news-trigger "<headline>" --event-slug fin:<slug> \\
    --source-agent finance_analyst

  WHY: This writes to trade_ideas table. Strategy #29 (Finance Intel) reads and trades this.
  Focus on: crypto markets, Fed/rate markets, economic indicator markets.
  If nothing is compelling, create one at --confidence low.

\u2501\u2501\u2501 STEP 4.5: SOURCE AUDIT + ACH + SITUATION MODEL \u2501\u2501\u2501

A) SOURCE TIER AUDIT:
   Note: "Bloomberg (factual) + Reuters (factual) + CoinDesk (speculative) = medium-high confidence"
   If only crypto Twitter / project PRs → downgrade to LOW regardless of content.

B) FINANCIAL MANIPULATION SCAN: check each non-factual source article for:
   - Pump language, FOMO induction, false authority, cherry-picked data, sponsored disguised as news
   - 2+ signals → flag as propaganda_tier, note in markets/state

C) ACH FOR TOP 2 MACRO EVENTS:
   H1: [Bullish interpretation]
   H2: [Bearish interpretation]
   Evidence matrix: which has more inconsistencies?
   Conclusion: state most supported hypothesis and why

D) UPDATE SITUATION MODEL (fin: namespace):
   For each significant new finding:
   python3 situation_model.py upsert \\
     --slug fin:<event-slug> \\
     --subject "<entity: Fed|BTC|Oil|S&P500>" \\
     --predicate "<status/relationship>" \\
     --object "<value>" \\
     --confidence <0.0-1.0> --source-tier <factual|speculative|opinion|propaganda> \\
     --sources "<comma-separated>" --diversity-score <0.0-1.0>

   Write 2-3 claims per major event. Examples:
     subject="Fed" predicate="rate_stance" object="on_hold_pause_signaled"
     subject="BTC" predicate="etf_flow_7d" object="net_inflow_2.1B"
     subject="Oil" predicate="price_driver" object="geo_risk_premium_elevated"

\u2501\u2501\u2501 STEP 5: WRITE markets/state TO SUPABASE \u2501\u2501\u2501
Write structured market intelligence report. Include:
  - Timestamp
  - Short heading labels only — the UI parses these headings directly
  - Keep the section label EXACT and the title after the colon SHORT
  - Prefer bullets and short paragraphs over dense prose
  - Use this exact heading pattern:
    ## EXECUTIVE SUMMARY: What changed
    ## MACRO: Rates and inflation
    ## CRYPTO: Flows and regulation
    ## EQUITIES: Risk and breadth
    ## CROSS-DOMAIN: Geo to macro
    ## MARKET IMPLICATIONS: Position impact
    ## SOURCE CREDIBILITY: Source audit
    ## ACH: Competing hypotheses
  - The text after the colon should be 2-4 words
  - Open each section with 1 short lead sentence, then bullets if useful
  - Omit a section if you truly have nothing material; do not fill space
  - Write for dashboard delivery, not essay completeness

  Before any Supabase upsert, write the final report to /tmp/markets_state_report.txt so the wrapper can recover it if you hit the tool limit:
  python3 -c "
  from pathlib import Path
  Path('/tmp/markets_state_report.txt').write_text('''PASTE YOUR REPORT HERE''', encoding='utf-8')
  print('Temp report written')
  "

  python3 -c "
  import sys; sys.path.insert(0, '.')
  from bot.supabase_helper import upsert_memory
  content = '''PASTE YOUR REPORT HERE'''
  upsert_memory('knowledge', 'markets/state', 'Finance Market Intelligence Report', content, source='vps')
  print('Written markets/state')
  "

\u2501\u2501\u2501 STEP 6: WRITE market_boosts_current TO SUPABASE \u2501\u2501\u2501
Write JSON boost dict for OPEN POSITIONS ONLY:
  - Only boost condition_ids from Step 2 open positions
  - Only genuine conviction (+/-8 or greater)
  - If no material view, write empty {{}}
  - Every entry you write must include `"source": "finance_analyst"`
  - Before upsert, drop malformed keys, token IDs, and any condition_id not present in open positions or market_metadata

  python3 -c "
  import sys, json, re, sqlite3; sys.path.insert(0, '.')
  from bot.supabase_helper import upsert_memory
  boosts = {{}}  # your boost dict — keys MUST be full 66-char condition_ids
  conn = sqlite3.connect('{DB_PATH}')
  valid_ids = {{
      str(row[0]).strip().lower()
      for row in conn.execute(\"SELECT condition_id FROM market_metadata WHERE condition_id IS NOT NULL AND condition_id != ''\")
  }}
  valid_ids |= {{
      str(row[0]).strip().lower()
      for row in conn.execute(\"SELECT condition_id FROM strategy_positions WHERE state='open' AND condition_id IS NOT NULL AND condition_id != ''\")
  }}
  _valid = {{}}
  for k, v in boosts.items():
      key = str(k or '').strip().lower()
      if not re.fullmatch(r'0x[a-f0-9]{{64}}', key):
          continue
      if key not in valid_ids:
          continue
      entry = dict(v or {{}})
      entry['source'] = 'finance_analyst'
      _valid[key] = entry
  if len(_valid) < len(boosts):
      print(f'Dropped {{len(boosts) - len(_valid)}} invalid boost keys (malformed/token/unknown IDs)')
  upsert_memory('knowledge', 'market_boosts_current', 'Finance Market Boosts', json.dumps(_valid), source='vps')
  print('Written market_boosts_current')
  "

\u2501\u2501\u2501 STEP 7: MAKE PREDICTION (exactly 1) \u2501\u2501\u2501
Quality > quantity. Must be specific, time-bound, and verifiable.

IMPORTANT -- two valid prediction types:

  Type A -- Polymarket binary (auto-gradeable via SQLite):
    Predict whether a contract YES price will cross a PROBABILITY threshold (0-1).
    Threshold in --prediction must be 0-1 (e.g. 0.75), never a dollar amount.
    --prediction "BTC above $70k contract (0x...) will trade above 0.75 YES by [date]"
    --query "SELECT captured_at FROM poly_price_snapshots WHERE condition_id='0x...' ORDER BY captured_at DESC LIMIT 1"
    NOTE: poly_price_snapshots.price is a 0-1 probability, NOT a USD price.

  Type B -- Real-world macro call (manual grading, omit --query):
    Predict a real-world outcome. Grade manually each run via `grade` + `mark`.
    --prediction "Fed will hold rates at May FOMC"  (binary, checkable from news)
    --prediction "Oil Brent will stay below $90 through April"  (check real price manually)
    Do NOT attach poly_price_snapshots to a dollar prediction -- values are incomparable.

  python3 agents/tools/predict_tool.py predict \\
    --agent finance_analyst \\
    --prediction "..." \\
    --condition "..." \\
    --confidence medium --verify-hours 48 \\
    --query "..."   # omit entirely for Type B predictions

Good: "Fed will not cut in March FOMC meeting" (Type B -- omit --query, grade manually)
Good: "BTC above $72k contract (0x...) will price above 0.6 YES by [date]" (Type A -- 0-1 threshold)
If your prediction says "by [date]", "on [date]", "through [month]", or "EOD", you MUST use `--verify-at` with an exact UTC ISO timestamp after the true resolution window.
For those calendar predictions, replace `--verify-hours 48` with `--verify-at 2026-03-30T04:15:00Z`.
Bad:  "BTC will close above $72000" with poly_price_snapshots query (USD vs probability mismatch)
Bad:  "Markets will be volatile" (vague, not falsifiable)
\u2501\u2501\u2501 STEP 8: CROSS-DOMAIN FLAGS \u2501\u2501\u2501
This step is MANDATORY — do not skip. Either create a flag or confirm none are needed.

Create a flag to geopolitics if ANY of these are true based on your analysis this run:
  • Oil (WTI/Brent) moved >5%% in 24h with no obvious demand explanation (likely supply/geo trigger)
  • USD DXY moved >1.5%% — affects sanction effectiveness and commodity pricing
  • VIX above 28 (equity fear level that historically precedes or follows political shocks)
  • S&P/NASDAQ moved >2%% on a single session — systemic risk event
  • BTC dropped >10%% — often correlated with macro risk-off or regulatory shock
  • Unexpected Fed action (surprise hike, emergency cut, or strong forward guidance change)
  • Major bank failure or systemic credit event

If any condition is met:
  python3 agents/tools/domain_query.py --flag --from finance --to geopolitics \\
    --urgency high|medium \\
    --message "[one precise sentence: asset, move size, date, why it may have geo roots]"

  Example: "WTI +8.3%% Mar 28 with no demand catalyst — likely Hormuz/supply disruption, check Middle East"

If no conditions apply, write exactly: "Step 8: No finance→geopolitics flags required this run."

Then read and clear any finance-directed flags from geopolitics you've already addressed:
  python3 agents/tools/domain_query.py --read-flags --domain finance
  # For each addressed flag: python3 agents/tools/domain_query.py --clear-flag --flag-name "flag:..."

\u2501\u2501\u2501 STEP 9: TELEGRAM SUMMARY \u2501\u2501\u2501
Send exactly ONE Telegram message. Use this template (fill in every field):

  \U0001f4b9 Finance Analyst — [YYYY-MM-DD HH:MM UTC]
  \u2022 [Most important macro development — one sentence, specific: event, number, direction]
  \u2022 [Key crypto/equities signal — one sentence, include price/level if relevant]
  \u2022 [Market implication or trade idea — which market(s), which direction, why]
  [OPTIONAL 4th bullet only if a wrong-sided position was detected]
  \u26a0\ufe0f WRONG SIDE: [condition_id short] [outcome] entry=[entry_price] cur=[current_price] — [reason]

  \U0001f4ca [N] boosts written | \U0001f4a1 [N] trade ideas | \U0001f3af [your prediction in \u22648 words] | [\u26a0\ufe0f N flags OR \u2705 no flags]

RULES:
  - Header MUST include UTC time — use $(date -u '+%Y-%m-%d %H:%M UTC')
  - Exactly 3 bullets unless a wrong-sided position was found (then 4)
  - One sentence per bullet. No sub-clauses. No "and also". Ruthlessly cut.
  - Numbers must be EXACT: "$112" not "elevated", "7 boosts" not "several", "$0.87\u2192$0.94" not "rose"
  - Footer line is MANDATORY — never omit it
  - If you boosted 0 markets, write "\U0001f4ca 0 boosts written" (not "N/A" or blank)
  - If you created 0 trade ideas, write "\U0001f4a1 0 trade ideas"
  - Wrong-sided = open position in strategy #29 where boost direction OPPOSES the position outcome
  - NEVER send two Telegram messages — combine everything into one
  - Keep total message under 800 chars — trim bullets if needed

Format: Hedge fund morning brief, not a news article. Every word earns its place.

\u2501\u2501\u2501 STEP 10: REVIEW OWNED STRATEGY (#29) \u2501\u2501\u2501
  sqlite3 {DB_PATH} "SELECT s.id, s.label, s.status, \\
    COUNT(CASE WHEN sp.state='open' THEN 1 END) as open_ct, \\
    COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed_ct, \\
    SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins, \\
    ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl \\
  FROM strategies s \\
  LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id \\
  WHERE s.id = 29 GROUP BY s.id"

  - Finance Intel (#29): trades YOUR trade ideas. Poor win rate or PnL < -$50 → flag.
  python3 agents/tools/action_tool.py flag --strategy-id 29 --reason "..."
  python3 agents/tools/action_tool.py recommend --strategy-id 29 --strategy finance_intel_v1 \\
    --recommendation "..." --reason "Based on [market analysis]"

\u2501\u2501\u2501 STEP 11: LOG RUN \u2501\u2501\u2501
Write a run_summary observation (required for brain health B5/E5 checks):
  python3 agents/tools/obs_tool.py write \\
    --strategy finance_analyst \\
    --type run_summary \\
    --confidence high \\
    --content "Brief: key macro events, markets boosted, trade idea(s) created, prediction made"
  (Fill in actual content)

Then log the run metadata:
  python3 agents/tools/predict_tool.py log-run \\
    --agent finance_analyst --model {PRIMARY_MODEL} --cost 0.00 --turns 0 \\
    --summary "Brief: what macro events found, how many markets boosted, trade idea created"
  (Fill in actual cost/turns from this run)
"""


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------

# Per-message inactivity timeout for the SDK async iterator. See world_analyst.py
# for rationale. A hung tool call (e.g. WebFetch on a slow URL) stops the stream;
# we abort before the outer systemd TimeoutStartSec (600s) kills us.
SDK_INACTIVITY_TIMEOUT_S = int(os.getenv("ANALYST_INACTIVITY_TIMEOUT_S", "180"))


async def _iter_with_inactivity_timeout(async_gen, timeout_s: int):
    """Yield items from ``async_gen``; raise TimeoutError if more than
    ``timeout_s`` seconds elapse between consecutive items.

    The first ``__anext__`` call is NOT wrapped in any timeout. That call
    triggers claude_agent_sdk's subprocess connect, which includes its own
    internal ``anyio.fail_after(2)`` version check and an ``anyio.TaskGroup``
    for stderr handling. Wrapping that startup in an outer ``anyio`` cancel
    scope entangles scope stacks across task boundaries and produces either
    ``CancelledError: Cancelled via cancel scope`` or ``Attempted to exit a
    cancel scope that isn't the current task's current cancel scope``.

    Subsequent ``__anext__`` calls use ``asyncio.wait_for`` (native asyncio
    cancellation — does not push onto anyio's cancel-scope stack) so the
    watchdog still catches hung tool calls (WebFetch on slow URLs, etc.)
    without breaking SDK startup.
    """
    import asyncio as _asyncio
    try:
        # First message: unbounded — let the SDK finish its connect + version
        # check + stderr task group spin-up without outer-scope interference.
        try:
            first = await async_gen.__anext__()
        except StopAsyncIteration:
            return
        yield first

        # Subsequent messages: inactivity watchdog via asyncio.wait_for.
        while True:
            try:
                item = await _asyncio.wait_for(async_gen.__anext__(), timeout=timeout_s)
            except StopAsyncIteration:
                return
            except _asyncio.TimeoutError:
                raise TimeoutError(
                    f"SDK stream inactive for >{timeout_s}s — likely a hung tool call"
                )
            yield item
    finally:
        try:
            await async_gen.aclose()
        except Exception:
            pass


async def run_analyst(task: str, quiet: bool = False) -> bool:
    """Run the finance analyst as a Claude Code agent session. Returns True on success."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import ResultMessage
    except ImportError:
        logger.error("claude_agent_sdk not installed — run: pip install claude-agent-sdk")
        return False

    # Inject calibration track record
    try:
        from sniper.analysis.agent_calibration import get_calibration_blurb
        _calib = get_calibration_blurb("finance_analyst")
        _system_prompt = ANALYST_SYSTEM_PROMPT + _calib if _calib else ANALYST_SYSTEM_PROMPT
        if _calib:
            logger.info("finance_analyst: injected calibration blurb")
    except Exception:
        _system_prompt = ANALYST_SYSTEM_PROMPT

    opts = ClaudeAgentOptions(
        cwd=WORKING_DIR,
        model=PRIMARY_MODEL,
        permission_mode="bypassPermissions",
        system_prompt=_system_prompt,
        setting_sources=[],
        max_turns=55,  # runaway guard; Max-plan auth means no billable spend
    )

    logger.info("💹 Finance Analyst starting (model=%s)", PRIMARY_MODEL)
    start = datetime.now(timezone.utc)

    try:
        msg_count = 0
        tool_count = 0

        async for message in _iter_with_inactivity_timeout(
            query(prompt=task, options=opts), SDK_INACTIVITY_TIMEOUT_S
        ):
            msg_count += 1

            if isinstance(message, ResultMessage):
                stop = message.stop_reason
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                logger.info(
                    "✅ Finance Analyst done: stop=%s msgs=%d tools=%d elapsed=%.0fs",
                    stop, msg_count, tool_count, elapsed,
                )
                if not quiet and message.result:
                    logger.info("Result preview: %s", (message.result or "")[:500])
                return True

            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    if "ToolUse" in type(block).__name__:
                        tool_count += 1
                        name = getattr(block, "name", "?")
                        if name in ("WebSearch", "WebFetch"):
                            inp = getattr(block, "input", {}) or {}
                            logger.info("  🌐 %s: %s", name,
                                        str(inp.get("query") or inp.get("url", ""))[:80])
                        elif name == "Bash":
                            inp = getattr(block, "input", {}) or {}
                            logger.info("  💻 %s", str(inp.get("command", ""))[:80])

    except Exception as e:
        msg = str(e).lower()
        is_rate_limit = "rate" in msg or "overload" in msg
        is_inactivity_timeout = isinstance(e, TimeoutError)
        # Retry on rate-limit OR inactivity timeout — both are transient runtime
        # conditions where a fresh attempt on the fallback model is worth the
        # tokens. Any other exception (genuine bug, auth, etc.) is logged and
        # left for the caller to handle.
        if is_rate_limit or is_inactivity_timeout:
            reason = "rate limit" if is_rate_limit else "inactivity timeout"
            logger.warning("Primary failed (%s) — falling back to %s", reason, FALLBACK_MODEL)
            opts_fb = ClaudeAgentOptions(
                cwd=WORKING_DIR, model=FALLBACK_MODEL,
                permission_mode="bypassPermissions",
                system_prompt=_system_prompt,
                setting_sources=[],
                max_turns=55,
            )
            try:
                async for message in _iter_with_inactivity_timeout(
                    query(prompt=task, options=opts_fb), SDK_INACTIVITY_TIMEOUT_S
                ):
                    if isinstance(message, ResultMessage):
                        logger.info("✅ Finance Analyst done (fallback model)")
                        return True
                # C10 fix: log when fallback stream ends without ResultMessage
                logger.warning("Finance Analyst fallback ended without ResultMessage (no result produced)")
            except Exception as e2:
                logger.error("Fallback also failed: %s", e2)
        else:
            logger.error("Finance Analyst error (no retry): %s", e)

    logger.warning("Finance Analyst returning False — no successful result produced")
    return False


# ---------------------------------------------------------------------------
# Boost validation guardrails
# ---------------------------------------------------------------------------

def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _load_known_boost_target_ids(db_path: str = DB_PATH) -> set[str]:
    known: set[str] = set()
    try:
        with sqlite3.connect(db_path) as conn:
            queries: list[str] = []
            if _sqlite_table_exists(conn, "market_metadata"):
                queries.append(
                    "SELECT condition_id FROM market_metadata WHERE condition_id IS NOT NULL AND condition_id != ''"
                )
            if _sqlite_table_exists(conn, "strategy_positions"):
                queries.append(
                    "SELECT condition_id FROM strategy_positions WHERE state='open' AND condition_id IS NOT NULL AND condition_id != ''"
                )
            if _sqlite_table_exists(conn, "paper_trades"):
                queries.append(
                    "SELECT condition_id FROM paper_trades WHERE status='open' AND condition_id IS NOT NULL AND condition_id != ''"
                )
            for query in queries:
                for row in conn.execute(query):
                    cid = str(row[0] or "").strip().lower()
                    if cid:
                        known.add(cid)
    except Exception as exc:
        logger.warning("Failed to load known boost targets from DB: %s", exc)
    return known


def _filter_market_boost_targets(
    boosts: dict[str, Any],
    valid_condition_ids: set[str] | None = None,
) -> tuple[dict[str, Any], int]:
    filtered: dict[str, Any] = {}
    removed = 0
    allowed = {str(cid or "").strip().lower() for cid in (valid_condition_ids or set()) if str(cid or "").strip()}
    enforce_membership = bool(allowed)

    for raw_key, raw_value in (boosts or {}).items():
        key = str(raw_key or "").strip().lower()
        if not BOOST_CONDITION_RE.fullmatch(key):
            removed += 1
            continue
        if enforce_membership and key not in allowed:
            removed += 1
            continue
        if not isinstance(raw_value, dict):
            removed += 1
            continue
        filtered[key] = raw_value
    return filtered, removed


def _prune_invalid_market_boost_targets(source_hint: str = "finance_analyst") -> int:
    try:
        from bot.supabase_helper import get_memory, upsert_memory

        entry = get_memory("market_boosts_current", type_filter="knowledge")
        if not entry:
            return 0
        raw_content = entry.get("content", "{}")
        raw_boosts = _json.loads(raw_content) if isinstance(raw_content, str) else raw_content
        if not isinstance(raw_boosts, dict):
            logger.warning("%s wrote non-dict market_boosts_current; skipping prune", source_hint)
            return 0

        known_ids = _load_known_boost_target_ids(DB_PATH)
        if not known_ids:
            logger.warning("No known condition IDs available; skipping market boost pruning")
            return 0

        filtered, removed = _filter_market_boost_targets(raw_boosts, known_ids)
        if removed <= 0:
            return 0

        upsert_memory(
            "knowledge",
            "market_boosts_current",
            "Market boosts (validated)",
            _json.dumps(filtered),
            source="vps",
        )
        logger.warning("%s pruned %d invalid market boost target(s)", source_hint, removed)
        return removed
    except Exception as exc:
        logger.warning("Boost target pruning failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Brain freshness check
# ---------------------------------------------------------------------------

def _check_brain_updated() -> tuple[bool, bool]:
    """Returns (markets_state_updated, markets_boosts_updated) within last 2 hours."""
    try:
        from bot.supabase_helper import get_memory
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)

        def _fresh(name):
            m = get_memory(name, type_filter="knowledge")
            if not m:
                return False
            ts_raw = m.get("updated_at") or ""
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                return ts > cutoff
            except Exception:
                return False

        return _fresh("markets/state"), _fresh("market_boosts_current")
    except Exception:
        return False, False


# ---------------------------------------------------------------------------
# CLI fetch helpers (used by agent via Bash tool)
# ---------------------------------------------------------------------------

def _cmd_fetch_all(as_json: bool = False) -> None:
    """Fetch from all configured sources and print structured output."""
    import concurrent.futures

    items: list[dict] = []
    sources_used: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futs = {
            pool.submit(fetch_marketaux_news): "marketaux",
            pool.submit(fetch_newsdata_finance): "newsdata",
            pool.submit(fetch_newsapi_finance): "newsapi",
            pool.submit(fetch_gnews_finance): "gnews",
            pool.submit(fetch_finance_rss_news): "rss",
        }
        for fut, src in futs.items():
            try:
                result = fut.result(timeout=20)
                if result:
                    items.extend(result)
                    sources_used.append(src)
            except Exception as exc:
                logger.debug("Fetch %s failed: %s", src, exc)

    if as_json:
        print(_json.dumps({"sources": sources_used, "count": len(items), "items": items[:80]}, indent=2))
    else:
        print(f"=== Finance News Pre-fetch: {len(items)} articles from {sources_used} ===")
        for item in items[:60]:
            src = item.get("source", "?")
            title = item.get("title", "")
            pub = item.get("published_at", "")[:10]
            print(f"  [{src}] {pub} — {title}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main() -> None:
    # Load .env
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

    parser = argparse.ArgumentParser(description="Finance & Markets Intelligence Analyst")
    parser.add_argument("--quick", action="store_true",
                        help="Fast mode: 2-3 events per domain")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress Telegram output")
    parser.add_argument("--check", action="store_true",
                        help="Check if brain was recently updated (exit 0=yes 1=no)")
    parser.add_argument("--fetch-all", action="store_true",
                        help="Pre-fetch news from all configured sources (for agent internal use)")
    parser.add_argument("--fetch-rss", action="store_true",
                        help="Fetch from RSS feeds only")
    parser.add_argument("--as-json", action="store_true",
                        help="Output --fetch-all as JSON")
    parser.add_argument("--install-cron", action="store_true",
                        help="Install cron job (offset 2h from geo analyst)")
    args = parser.parse_args()

    if args.check:
        ms, mb = _check_brain_updated()
        status = "✅ fresh" if (ms and mb) else f"⚠️  stale (markets/state={'✅' if ms else '❌'}, boosts={'✅' if mb else '❌'})"
        print(f"Brain status: {status}")
        import sys as _sys
        _sys.exit(0 if (ms and mb) else 1)

    if args.fetch_all:
        _cmd_fetch_all(as_json=args.as_json)
        return

    if args.fetch_rss:
        items = fetch_finance_rss_news()
        if args.as_json:
            print(_json.dumps({"count": len(items), "items": items}, indent=2))
        else:
            for item in items:
                print(f"  [{item.get('source')}] {item.get('title','')}")
        return

    if args.install_cron:
        import subprocess as _sp
        # Offset 2h from geo analyst (which runs at 00, 04, 08, 12, 16, 20 UTC)
        cron_line = (
            f"0 2,6,10,14,18,22 * * * cd {ROOT} && "
            f"{ROOT}/.venv/bin/python3 {ROOT}/finance_analyst.py --quiet "
            f">> {ROOT}/logs/finance_analyst.log 2>&1"
        )
        result = _sp.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        if "finance_analyst.py" in existing:
            print("✅ finance_analyst cron already installed")
        else:
            new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
            _sp.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            print(f"✅ Installed cron: {cron_line}")
        return

    # ── Concurrency guard ────────────────────────────────────────────────────
    # Prevents a second instance (e.g. event_trigger Popen) from running while
    # the scheduled systemd run is still active.  Uses a PID-file so a crash
    # that skips cleanup is still detected on the next launch.
    _lock_path = ROOT / "logs" / "finance_analyst.lock"

    def _lock_stale(path: Path) -> bool:
        """Return True if lock file exists but the recorded PID is dead."""
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)   # signal 0 = existence check, no actual signal sent
            return False      # process alive → lock is live
        except (ValueError, ProcessLookupError, PermissionError):
            return True       # file missing/corrupt or PID gone → stale

    if _lock_path.exists() and not _lock_stale(_lock_path):
        existing_pid = _lock_path.read_text().strip()
        logger.warning("finance_analyst already running (PID %s) — exiting to avoid overlap", existing_pid)
        sys.exit(0)

    _lock_path.write_text(str(os.getpid()))

    import atexit as _atexit
    _atexit.register(lambda: _lock_path.unlink(missing_ok=True))
    # ── End concurrency guard ─────────────────────────────────────────────────

    logger.info("Pre-fetching context for Steps 0–2 (saves ~15 agent turns)…")
    _prefetched = _prefetch_context(quick=args.quick)
    logger.info("Pre-fetch complete (%d chars)", len(_prefetched))
    task = build_task_prompt(quick=args.quick, prefetched=_prefetched)

    import asyncio as _asyncio

    def _quiet_exception_handler(loop, ctx):
        exc = ctx.get("exception")
        exc_str = str(exc) if exc else ""
        msg = ctx.get("message", "")
        if (
            "cancel scope" in exc_str.lower()
            or "event loop is closed" in exc_str.lower()
            or "cancel scope" in msg.lower()
        ):
            return
        loop.default_exception_handler(ctx)

    async def _main():
        _asyncio.get_event_loop().set_exception_handler(_quiet_exception_handler)
        return await run_analyst(task=task, quiet=args.quiet)

    # Also suppress "Exception ignored in __del__" noise that comes through
    # sys.unraisablehook (a separate path from the asyncio exception handler).
    # This fires when BaseSubprocessTransport.__del__ runs after loop teardown.
    import sys as _sysmod
    _orig_unraisable = _sysmod.unraisablehook
    def _quiet_unraisable(u):
        if isinstance(u.exc_value, RuntimeError) and (
            "event loop is closed" in str(u.exc_value).lower()
            or "cancel scope" in str(u.exc_value).lower()
        ):
            return
        _orig_unraisable(u)
    _sysmod.unraisablehook = _quiet_unraisable

    try:
        ok = anyio.run(_main)
    except RuntimeError as e:
        if "Event loop is closed" in str(e) or "cancel scope" in str(e):
            ok = True
        else:
            raise

    run_failed = not ok
    if run_failed:
        logger.error("Finance analyst run failed")
        # Do NOT exit here — fall through so the shell-fallback heartbeat
        # at the end of main() still writes an agent_runs row with
        # status='error'. Exiting here leaves B1 silent on failure.

    # --- Fallback recovery: agent may have written /tmp/markets_state_report.txt
    # but run out of turns before executing the Supabase upsert. Recover here.
    _tmp_report = Path("/tmp/markets_state_report.txt")
    ms_ok, mb_ok = _check_brain_updated()
    if not ms_ok and _tmp_report.exists():
        try:
            _age_s = (datetime.now(timezone.utc).timestamp() - _tmp_report.stat().st_mtime)
            if _age_s < 1800 and _tmp_report.stat().st_size > 200:  # fresh + non-empty
                from bot.supabase_helper import upsert_memory
                _content = _tmp_report.read_text(encoding="utf-8")
                upsert_memory("knowledge", "markets/state",
                              "Finance Market Intelligence Report", _content, source="vps")
                logger.info("✅ markets/state recovered from temp file (agent wrote report but ran out of turns)")
                ms_ok = True
        except Exception as _e:
            logger.warning("Temp file recovery failed: %s", _e)

    removed_boosts = _prune_invalid_market_boost_targets(source_hint="finance_analyst")
    if removed_boosts:
        ms_ok, mb_ok = _check_brain_updated()

    if ms_ok and mb_ok:
        logger.info("✅ Brain updated: markets/state ✅  market_boosts_current ✅")
    else:
        logger.warning(
            "⚠️  Brain may not have been updated: markets/state=%s  boosts=%s",
            "✅" if ms_ok else "❌", "✅" if mb_ok else "❌",
        )

    # Always write an agent_runs heartbeat at the shell level so B1 never
    # misses a run — even on failure (status='error' surfaces in the audit)
    # and even when Claude hit the tool limit before the analyst's own log-run.
    heartbeat_status = "error" if run_failed else "success"
    heartbeat_summary = (
        "[shell-fallback] run failed"
        if run_failed
        else "[shell-fallback] run completed"
    )
    try:
        import subprocess as _sp
        _sp.run(
            [
                str(ROOT / ".venv" / "bin" / "python3"),
                "agents/tools/predict_tool.py", "log-run",
                "--agent", "finance_analyst",
                "--model", PRIMARY_MODEL,
                "--cost", "0.00",
                "--turns", "0",
                "--summary", heartbeat_summary,
                "--status", heartbeat_status,
            ],
            cwd=str(ROOT),
            capture_output=True,
            timeout=15,
        )
        logger.info("✅ agent_runs heartbeat written (status=%s)", heartbeat_status)
    except Exception as _e:
        logger.warning("shell log-run fallback failed: %s", _e)

    sys.exit(1 if run_failed else 0)


if __name__ == "__main__":
    main()
