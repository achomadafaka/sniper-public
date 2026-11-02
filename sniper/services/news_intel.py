"""
News Intelligence — fetch headlines, match to open Polymarket markets,
surface potential trade ideas.

Run as a standalone script (cron/service) or import NewsIntel class.

Flow:
  1. Fetch RSS headlines from 12 sources (BBC, Guardian, NPR, etc.)
  2. Match headlines against open Polymarket markets (keyword overlap)
  3. Score the match: does the news make YES or NO more likely?
  4. Store news in Supabase; create trade_idea records for strong matches
  5. Optionally send Telegram alert for high-confidence ideas

Usage:
    python3 news_intel.py              # one-shot run
    python3 news_intel.py --loop 30   # run every 30 minutes
"""

import argparse
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

import requests

from sniper.markets.hl_universe import load_universe
from sniper.markets.pm_resolver import resolve_token, snapshot_prices
from sniper.services.intel_paper_open import open_for_idea

logger = logging.getLogger(__name__)

try:
    from sniper.analysis.entity_clusters import ENTITY_CLUSTERS, detect_entity_clusters, KEYWORD_INDEX
    from sniper.markets.source_meta import SOURCE_META, compute_perspective_diversity, compute_surprise_weight, get_source_meta
    _ENTITY_CLUSTERS_AVAILABLE = True
except ImportError:
    _ENTITY_CLUSTERS_AVAILABLE = False
    logger.debug("entity_clusters/source_meta not available; multilingual cluster detection disabled")

# ---------------------------------------------------------------------------
# RSS Feed sources — all verified accessible from this server
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    # --- Core geopolitics (original) ---
    "bbc_world":     "https://feeds.bbci.co.uk/news/world/rss.xml",
    "bbc_politics":  "https://feeds.bbci.co.uk/news/politics/rss.xml",
    "bbc_tech":      "https://feeds.bbci.co.uk/news/technology/rss.xml",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "guardian_us":   "https://www.theguardian.com/us-news/rss",
    "guardian_tech": "https://www.theguardian.com/technology/rss",
    "npr_world":     "https://feeds.npr.org/1004/rss.xml",
    "foreignpolicy": "https://foreignpolicy.com/feed/",
    "the_hill":      "https://thehill.com/news/feed/",
    "politico":      "https://rss.politico.com/politics-news.xml",
    "coindesk":      "https://www.coindesk.com/arc/outboundfeeds/rss",
    "axios":         "https://api.axios.com/feed/",
    # --- Conflict / Middle East / OSINT (new) ---
    "aljazeera":     "https://www.aljazeera.com/xml/rss/all.xml",
    "france24":      "https://www.france24.com/en/rss",
    "dw_world":      "https://rss.dw.com/rdf/rss-en-world",
    "middleeasteye": "https://www.middleeasteye.net/rss",
    # --- Crypto / DeFi / token launches (new) ---
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock":      "https://www.theblock.co/rss.xml",
    "decrypt":       "https://decrypt.co/feed",
    "blockworks":    "https://blockworks.co/feed",
    # --- Economics / Fed / markets (new) ---
    "cnbc_economy":  "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "marketwatch":   "https://feeds.marketwatch.com/marketwatch/topstories/",
    # --- Asia-Pacific ---
    "scmp":          "https://www.scmp.com/rss/91/feed",
    "nikkei_asia":   "https://asia.nikkei.com/rss/feed/nar",
    "nhk_world":     "https://www3.nhk.or.jp/rss/news/cat0.xml",
    "times_india":   "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "the_hindu":     "https://www.thehindu.com/news/international/?service=rss",
    # --- Europe multilingual ---
    "euractiv":      "https://www.euractiv.com/feed/",
    "rfi_english":   "https://www.rfi.fr/en/rss",
    "dw_arabic":     "https://rss.dw.com/rdf/rss-ar-all",
    # --- Russia/Ukraine ---
    "ukr_pravda_en": "https://www.pravda.com.ua/eng/rss/view_news/",  # Ukrainska Pravda (English) — replaced dead kyiv_independent feed
    "meduza":        "https://meduza.io/rss/en/all",
    # --- Middle East perspective ---
    "almonitor":     "https://www.al-monitor.com/rss",
    "asharq_awsat":  "https://english.aawsat.com/rss",
    # --- Latin America ---
    "mercopress":    "https://en.mercopress.com/rss",
    # --- Strategic/state perspective (read, don't trust) ---
    "global_times":  "https://www.globaltimes.cn/rss/outbrain.xml",
    "tass_english":  "https://tass.com/rss/v2.xml",
}

FETCH_TIMEOUT = 10  # seconds per feed
MAX_ITEMS_PER_FEED = 15

# Repo root is three levels up from sniper/services/news_intel.py. Before this was
# hoisted out of the __main__ block it resolved to sniper/services/logs/, which does
# not exist, so `open(lock, "w")` raised FileNotFoundError before NewsIntel() was ever
# constructed — the entrypoint has been unrunnable since the module left the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOCK_PATH = os.path.join(_REPO_ROOT, "logs", "news_intel.lock")


def _detect_feed_language(source_key: str) -> str:
    """Return the primary language code for a feed source."""
    if _ENTITY_CLUSTERS_AVAILABLE:
        from sniper.markets.source_meta import get_source_meta
        return get_source_meta(source_key).get("lang", "en")
    return "en"

# ---------------------------------------------------------------------------
# Topic keyword → market category mapping
# Used to score how relevant a news headline is to a Polymarket question
# ---------------------------------------------------------------------------
TOPIC_KEYWORDS = {
    # --- Geopolitical conflicts ---
    "iran":       ["iran", "iranian", "tehran", "khamenei", "pahlavi", "irgc", "persian gulf", "strait of hormuz"],
    "russia":     ["russia", "russian", "putin", "kremlin", "moscow", "ukraine", "zelensky", "donbas", "crimea"],
    "china":      ["china", "chinese", "beijing", "xi jinping", "taiwan", "pla", "south china sea"],
    "northkorea": ["north korea", "kim jong", "pyongyang", "dprk"],
    "israel":     ["israel", "israeli", "netanyahu", "gaza", "hamas", "idf", "hezbollah", "west bank"],
    "military":   ["invasion", "attack", "strike", "missile", "ceasefire", "war", "conflict", "troops",
                   "bombing", "airstrike", "regime change", "coup", "military operation"],
    # --- US politics ---
    "trump":      ["trump", "white house", "executive order", "tariff", "maga", "oval office", "vance"],
    "election":   ["election", "ballot", "vote", "candidate", "polling", "primary", "midterm"],
    "taiwan":     ["taiwan", "strait", "invasion", "pla", "republic of china"],
    "greenland":  ["greenland", "denmark", "arctic"],
    # --- Crypto / blockchain / DeFi ---
    "bitcoin":    ["bitcoin", "btc", "crypto", "ethereum", "eth", "coinbase", "binance", "blockchain",
                   "solana", "sol", "xrp", "ripple", "defi"],
    "token":      ["token launch", "airdrop", "fdv", "ipo", "listing", "opensea", "metamask",
                   "uniswap", "token sale", "fair launch"],
    # --- Economics / markets ---
    "fed":        ["federal reserve", "fed rate", "interest rate", "powell", "inflation", "cpi",
                   "rate cut", "rate hike", "fomc", "monetary policy", "recession"],
    "oil":        ["crude oil", "opec", "brent", "wti", "petroleum", "oil price", "energy crisis",
                   "oil embargo", "barrel"],
    # --- Tech / AI ---
    "ai":         ["openai", "anthropic", "gpt", "claude", "gemini", "artificial intelligence", "llm",
                   "gpt-6", "agi", "ai regulation"],
    "spacex":     ["spacex", "elon", "starship", "rocket", "nasa", "mars mission"],
    # --- Other ---
    "climate":    ["climate", "temperature record", "carbon", "emissions"],
}

# Sentiment signals: these words in a headline suggest YES is more likely
YES_SIGNALS = [
    "confirmed", "announces", "launches", "invades", "attacks", "wins",
    "passes", "approves", "signs", "declares", "breaks", "reaches", "achieves",
    "hits record", "all-time high", "surges", "soars", "escalates", "escalation",
    "breakthrough", "deal reached", "agreement signed", "ceasefire agreed",
]
# These suggest YES is less likely (NO more likely)
NO_SIGNALS = [
    "denies", "rejects", "refuses", "cancels", "postpones", "delays",
    "fails", "collapses", "unlikely", "no plans", "rules out",
    "drops", "falls", "plunges", "crashes", "not ready", "not planning",
    "no deal", "no agreement", "no ceasefire", "dismissed", "ruled out",
    "backs away", "walks back", "reverses", "retreats from",
]


# ---------------------------------------------------------------------------
# RSS Fetcher
# ---------------------------------------------------------------------------

def _parse_event_ts(raw: Optional[str]):
    """Parse an ISO-8601 news timestamp to an aware UTC datetime, or None.

    Falls back to None (caller uses 'now') rather than guessing — an invented
    event time would silently corrupt the repricing-capture measurement.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_rss_date(date_str: Optional[str]) -> Optional[str]:
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return None


def fetch_feed(source: str, url: str) -> List[Dict[str, Any]]:
    """Fetch and parse one RSS feed. Returns list of article dicts."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "SniperNewsBot/1.0"},
            timeout=FETCH_TIMEOUT
        )
        if not resp.ok:
            logger.warning("Feed %s returned %d", source, resp.status_code)
            return []
        root = ET.fromstring(resp.content)
        # Handle both RSS <channel><item> and Atom <entry>
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        items = root.findall(".//item") or root.findall(".//atom:entry", ns)
        results = []
        for item in items[:MAX_ITEMS_PER_FEED]:
            title = (item.findtext("title") or item.findtext("atom:title", namespaces=ns) or "").strip()
            desc = (item.findtext("description") or item.findtext("summary") or item.findtext("atom:summary", namespaces=ns) or "").strip()
            # Strip HTML tags from description
            desc = re.sub(r"<[^>]+>", " ", desc).strip()[:500]
            link = item.findtext("link") or item.findtext("atom:link", namespaces=ns) or ""
            pub_date = _parse_rss_date(item.findtext("pubDate") or item.findtext("published") or "")
            if not title:
                continue
            results.append({
                "source": source,
                "title": title,
                "description": desc or None,
                "url": link or None,
                "published_at": pub_date,
            })
        return results
    except ET.ParseError as e:
        logger.warning("XML parse error for %s: %s", source, e)
        return []
    except Exception as e:
        logger.warning("Feed fetch failed %s: %s", source, e)
        return []


def fetch_all_feeds() -> List[Dict[str, Any]]:
    """Fetch all RSS feeds concurrently. Returns merged list."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    all_items = []
    with ThreadPoolExecutor(max_workers=min(22, len(RSS_FEEDS))) as pool:
        futures = {pool.submit(fetch_feed, src, url): src for src, url in RSS_FEEDS.items()}
        for future in as_completed(futures):
            src = futures[future]
            try:
                items = future.result()
                logger.debug("Feed %s: %d items", src, len(items))
                all_items.extend(items)
            except Exception as e:
                logger.debug("Feed %s exception: %s", src, e)
    return all_items


# ---------------------------------------------------------------------------
# Topic extraction + sentiment
# ---------------------------------------------------------------------------

def extract_topics(title: str, description: str = "") -> List[str]:
    """Extract topic tags from headline text."""
    text = (title + " " + (description or "")).lower()
    tags = []
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            tags.append(topic)
    return tags


def score_sentiment(title: str, description: str = "") -> str:
    """Rough sentiment: does this news make some event MORE or LESS likely?"""
    text = (title + " " + (description or "")).lower()
    yes_score = sum(1 for s in YES_SIGNALS if s in text)
    no_score = sum(1 for s in NO_SIGNALS if s in text)
    if yes_score > no_score:
        return "bullish_yes"
    elif no_score > yes_score:
        return "bullish_no"
    return "neutral"


def enrich_news_items(items: List[Dict]) -> List[Dict]:
    """Add topic_tags and sentiment to each news item."""
    for item in items:
        title = item.get("title", "")
        desc = item.get("description", "") or ""
        item["topic_tags"] = extract_topics(title, desc)
        item["sentiment"] = score_sentiment(title, desc)
        item["processed"] = False
        # Entity cluster detection (multilingual-aware, additive signal)
        if _ENTITY_CLUSTERS_AVAILABLE:
            source_lang = _detect_feed_language(item.get("source", ""))
            item_clusters = detect_entity_clusters(
                (title + " " + desc).strip(),
                lang=source_lang
            )
            if item_clusters:
                item["entity_clusters"] = item_clusters
                # Use the first matched cluster as the driving_event hint
                if not item.get("driving_event"):
                    item["driving_event"] = item_clusters[0]
    return items


# ---------------------------------------------------------------------------
# Market matching — cross-reference news with open Polymarket markets
# ---------------------------------------------------------------------------

def fetch_polymarket_markets(limit: int = 500) -> List[Dict]:
    """Fetch open markets from Polymarket Gamma API."""
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"active": "true", "closed": "false", "limit": limit, "order": "volume24hr", "ascending": "false"},
            timeout=15,
        )
        if resp.ok:
            return resp.json()
        return []
    except Exception as e:
        logger.warning("Polymarket fetch failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def mark_news_processed(conn, titles) -> None:
    """Mark this run's analysed news rows processed, in ONE short write transaction.

    news_items lives in the funded ledger. This was `executemany(... WHERE title = ?)` over every
    title of the run (457), and the only index holding `title` is UNIQUE(source, title), so each
    update scanned all ~38k rows inside one write transaction: the ledger's write lock was held
    for at least 2.5 s every 4 h while a funded entry or exit booking waited (lock sampler,
    2026-09-27 00:07:33Z). `processed` is NOT NULL 0/1 and earlier rows are already 1, so
    restricting to `processed = 0` walks idx_ni_processed to this run's new rows only. Rows
    already marked stay 1, so the end state is the one the per-title update left, including a
    same-title row from another source.
    """
    import json
    conn.execute(
        "UPDATE news_items SET processed = 1 "
        "WHERE processed = 0 AND title IN (SELECT value FROM json_each(?))",
        (json.dumps(list(titles)),))
    conn.commit()


class NewsIntel:
    def __init__(self, db_conn=None):
        from brain import Brain
        self.brain = Brain()
        self.db_conn = db_conn

    def run_once(self) -> Dict[str, int]:
        """Full pipeline: fetch → enrich → store → analyze → create ideas.

        Analysis strategy:
          1. Try Claude API (world_context.update_world_context) — causal reasoning
          2. Fall back to keyword matching if ANTHROPIC_API_KEY not set or API fails
        """
        # 1. Fetch all feeds
        logger.info("Fetching news feeds...")
        raw_items = fetch_all_feeds()
        logger.info("Fetched %d raw items from %d feeds", len(raw_items), len(RSS_FEEDS))

        # 2. Enrich with topics + sentiment (kept for fallback + news feed display)
        enriched = enrich_news_items(raw_items)
        topic_items = [i for i in enriched if i.get("topic_tags")]
        logger.info("Enriched: %d items with topics", len(topic_items))

        # 3. Store in Supabase
        stored = self.brain.store_news(enriched)
        logger.info("Stored %d news items to Supabase", stored)

        # 4. Fetch open Polymarket markets
        markets = fetch_polymarket_markets()
        logger.info("Fetched %d Polymarket markets", len(markets))

        # 5. Try Claude-powered analysis (updates world events + returns trade ideas)
        claude_ideas: List[Dict] = []
        used_claude = False
        try:
            from sniper.analysis.world_context import update_world_context
            # conn lets the analyst see the open paper book and recommend closes.
            claude_ideas = update_world_context(
                enriched, markets, conn=self.brain._get_conn()
            )
            used_claude = True
            logger.info("Claude analysis returned %d trade ideas", len(claude_ideas))
        except Exception as e:
            logger.warning("World context update failed, using keyword fallback: %s", e)

        # 5b. Mark analyzed news items as processed (enables 3-day cleanup vs 7-day)
        if used_claude and enriched:
            try:
                titles = [item.get("title", "") for item in enriched if item.get("title")]
                mark_news_processed(self.brain._get_conn(), titles)
                logger.info("Marked %d news items as processed", len(titles))
            except Exception as e:
                logger.debug("Failed to mark news as processed: %s", e)

        # 6a. Claude path — store Claude's trade ideas
        ideas_created = 0
        seen_conditions: set = set()
        # Every candidate opens a paper position; the reviewer verdict is a tag,
        # not a gate. Fetched once per run rather than per candidate.
        paper_counts = {"pm": 0, "hl": 0, "no_instrument": 0, "conflict": 0}
        hl_universe_set = load_universe()
        matches_count = 0

        if used_claude and claude_ideas:
            matches_count = len(claude_ideas)
            # Build a quick lookup: conditionId → market data for price extraction
            market_by_cid: Dict[str, Dict] = {}
            for m in markets:
                cid = m.get("conditionId") or m.get("condition_id", "")
                if cid:
                    market_by_cid[cid] = m

            # Build end_date lookup: conditionId → endDateIso
            market_end_dates = {m.get("conditionId", ""): m.get("endDateIso") or m.get("end_date_iso") for m in markets}

            for idea in claude_ideas:
                cid = idea.get("condition_id", "")
                direction = idea.get("direction", "").lower()
                dedup_key = (cid, direction)
                if not cid or not direction or dedup_key in seen_conditions:
                    continue
                seen_conditions.add(dedup_key)

                # Determine outcome from direction
                outcome = "YES" if direction == "buy_yes" else "NO"

                # Extract current market price
                market_price = None
                m = market_by_cid.get(cid)
                if m:
                    prices_raw = m.get("outcomePrices")
                    if isinstance(prices_raw, str):
                        try:
                            import json as _json
                            prices_raw = _json.loads(prices_raw)
                        except Exception:
                            pass
                    if isinstance(prices_raw, list) and prices_raw:
                        try:
                            yes_p = float(prices_raw[0])
                            market_price = yes_p if direction == "buy_yes" else round(1 - yes_p, 3)
                        except Exception:
                            pass

                reasoning = idea.get("causal_chain") or idea.get("reasoning") or ""

                # Resolve to a real CLOB asset id. This replaces the old edge guard,
                # which read idea["current_price"] — a field the LLM never returns, so
                # it defaulted to 0.50 and both branches were unreachable (audit F-14).
                # Resolution doubles as instrument validation: if the venue does not
                # know the market, there is nothing to trade (F-01).
                token_id = resolve_token(cid, outcome)
                if token_id is None:
                    self.brain.add_trade_idea(
                        condition_id=cid, market_title=idea.get("market_title", ""),
                        outcome=outcome, direction=direction, reasoning=reasoning,
                        source_agent="news_intel", reject_reason="unresolvable_market",
                    )
                    logger.info("Rejected unresolvable market %s (%s)", cid, idea.get("market_title", "")[:50])
                    continue

                # Two price snapshots: what the market knew when the event happened,
                # and what we would pay now. Their ratio against the eventual move is
                # the "already priced" measurement this system has never had (§8).
                event_dt = _parse_event_ts(idea.get("news_trigger_ts")) or datetime.now(timezone.utc)
                decision_dt = datetime.now(timezone.utc)
                price_at_event, price_at_decision = snapshot_prices(
                    token_id, int(event_dt.timestamp()), int(decision_dt.timestamp())
                )
                if price_at_decision is None:
                    self.brain.add_trade_idea(
                        condition_id=cid, market_title=idea.get("market_title", ""),
                        outcome=outcome, direction=direction, reasoning=reasoning,
                        source_agent="news_intel", token_id=token_id,
                        reject_reason="no_decision_price",
                    )
                    logger.info("Rejected %s — no executable price at decision time", cid)
                    continue

                # The decision price IS the price. Prefer it over the Gamma snapshot.
                market_price = price_at_decision if price_at_decision is not None else market_price

                # Perspective diversity scoring across sources for this topic cluster
                if _ENTITY_CLUSTERS_AVAILABLE:
                    _driving_event = idea.get("driving_event")
                    _event_sources = [
                        i.get("source", "") for i in enriched
                        if _driving_event and _driving_event in i.get("entity_clusters", [])
                    ]
                    _div_score, _div_label = compute_perspective_diversity(_event_sources)
                    idea["perspective_diversity_score"] = _div_score
                    idea["perspective_diversity_label"] = _div_label
                    # Echo-chamber gate: cap confidence to "medium" when coverage is
                    # from a single perspective cluster (diversity < 0.3).
                    # This prevents single-source narratives from triggering high-confidence trades.
                    if _div_score < 0.3 and idea.get("confidence") == "high":
                        logger.info(
                            "Downgrading idea confidence high→medium (echo chamber): "
                            "diversity=%.2f (%s) for %s",
                            _div_score, _div_label, idea.get("market_title", "")[:50],
                        )
                        idea["confidence"] = "medium"

                # Append coverage_gaps and counter_narrative to reasoning
                if idea.get("coverage_gaps"):
                    reasoning += f"\n\nCoverage gaps: {', '.join(idea['coverage_gaps'])}"
                if idea.get("counter_narrative"):
                    reasoning += f"\n\nCounter-narrative: {idea['counter_narrative']}"

                idea_id = self.brain.add_trade_idea(
                    condition_id=cid,
                    market_title=idea.get("market_title", ""),
                    outcome=outcome,
                    direction=direction,
                    reasoning=reasoning,
                    current_price=market_price,
                    confidence=idea.get("confidence", "medium"),
                    news_trigger=idea.get("news_trigger"),
                    news_source=idea.get("news_source"),
                    expires_hours=48,
                    end_date_iso=market_end_dates.get(cid),
                    driving_event=idea.get("driving_event"),
                    source_agent="news_intel",
                    token_id=token_id,
                    event_ts=event_dt.isoformat(),
                    price_at_event=price_at_event,
                    price_at_decision=price_at_decision,
                )
                if idea_id:
                    ideas_created += 1
                    _disposition = open_for_idea(
                        self.brain._get_conn(),
                        {"id": idea_id, "condition_id": cid, "direction": direction,
                         "hl_symbol": idea.get("hl_symbol"),
                         "adversarial_approved": None, "adversarial_score": None},
                        hl_universe_set=hl_universe_set,
                    )["disposition"]
                    paper_counts[_disposition] = paper_counts.get(_disposition, 0) + 1
                    logger.info(
                        "Trade idea #%d [%s %s]: %s → %s @ %.2f",
                        idea_id,
                        "claude",
                        idea.get("confidence", "?").upper(),
                        idea.get("market_title", "")[:50],
                        direction,
                        market_price or 0,
                    )
                    if idea.get("confidence") == "high":
                        _send_telegram_idea(
                            idea.get("market_title", ""),
                            direction,
                            market_price,
                            idea.get("news_trigger") or "",
                            idea.get("news_source") or "",
                            reasoning,
                        )

        # No fallback path. The old keyword branch fired whenever Claude returned zero
        # ideas, turning a >=3-word title overlap into a confidence='high' candidate and a
        # Telegram alert (audit §12.1/§12.2). Zero candidates is a valid, honest outcome.
        elif not claude_ideas:
            logger.info("Claude returned no ideas — no candidates this run")

        # 7. Expire stale ideas
        expired = self.brain.expire_stale_ideas()
        if expired:
            logger.info("Expired %d stale trade ideas", expired)

        return {
            "news_fetched": len(raw_items),
            "news_with_topics": len(topic_items),
            "news_stored": stored,
            "markets_scanned": len(markets),
            "trade_matches": matches_count,
            "ideas_created": ideas_created,
            "paper_pm": paper_counts["pm"],
            "paper_hl": paper_counts["hl"],
            "paper_none": paper_counts["no_instrument"],
            "paper_conflict": paper_counts["conflict"],
            "ideas_expired": expired,
            "used_claude": used_claude,
        }

    def get_recent_ideas(self, limit: int = 20) -> List[Dict]:
        return self.brain.get_open_trade_ideas(limit=limit)

    def get_recent_news(self, limit: int = 30) -> List[Dict]:
        return self.brain.get_recent_news(limit=limit)


def _send_telegram_idea(
    market_title: str,
    direction: str,
    price: Optional[float],
    news_headline: str,
    news_source: str,
    reasoning: str,
) -> None:
    """Send a high-confidence trade idea to Telegram."""
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        action = "BUY YES" if direction == "buy_yes" else "BUY NO"
        price_str = f" @ ¢{round(price*100)}" if price else ""
        text = (
            f"💡 *Trade Idea* \\[news\\]\n\n"
            f"*Market:* {_esc(market_title)}\n"
            f"*Direction:* `{action}{price_str}`\n\n"
            f"📰 *Trigger:* {_esc(news_headline)}\n"
            f"*Source:* {_esc(news_source.replace('_', ' '))}\n\n"
            f"_{_esc(reasoning[:200])}_"
        )
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "MarkdownV2"},
            timeout=10,
        )
    except Exception as e:
        logger.debug("Telegram idea alert failed: %s", e)


def _esc(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    for c in r"_*[]()~`>#+-=|{}.!":
        text = text.replace(c, f"\\{c}")
    return text


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    parser = argparse.ArgumentParser(description="Sniper News Intelligence")
    parser.add_argument("--loop", type=int, metavar="MINUTES",
                        help="Run continuously every N minutes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and match but don't write to Supabase")
    args = parser.parse_args()

    # ── Concurrency guard ────────────────────────────────────────────────────
    # news_intel is Type=oneshot fired by a timer. event_trigger.py can also
    # spawn it via Popen. Without a lockfile, a slow run and a timer fire can
    # overlap, writing duplicate trade ideas and brain entries to Supabase.
    _lock_path = LOCK_PATH
    os.makedirs(os.path.dirname(_lock_path), exist_ok=True)

    def _lock_stale(path: str) -> bool:
        """Return True if the lockfile is missing, unreadable, or its PID is dead."""
        try:
            pid = int(open(path).read().strip())
            os.kill(pid, 0)   # signal 0 = existence check, no actual signal sent
            return False      # process alive → lock is live
        except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
            return True       # corrupt / gone PID → stale lock

    if os.path.exists(_lock_path) and not _lock_stale(_lock_path):
        existing_pid = open(_lock_path).read().strip()
        logger.warning("news_intel already running (PID %s) — exiting to avoid overlap", existing_pid)
        sys.exit(0)

    with open(_lock_path, "w") as _lf:
        _lf.write(str(os.getpid()))
    import atexit as _atexit
    _atexit.register(lambda: os.unlink(_lock_path) if os.path.exists(_lock_path) else None)
    # ── End concurrency guard ─────────────────────────────────────────────────

    intel = NewsIntel()

    def run():
        stats = intel.run_once()
        logger.info("Run complete: %s", stats)
        ideas = intel.get_recent_ideas(limit=10)
        if ideas:
            logger.info("\n=== Open Trade Ideas ===")
            for idea in ideas:
                logger.info(
                    "[%s] %s → %s @ %.2f — %s",
                    idea.get("confidence", "?").upper(),
                    idea.get("market_title", "")[:60],
                    idea.get("direction"),
                    idea.get("current_price") or 0,
                    (idea.get("news_trigger") or "")[:60],
                )
        return stats

    if args.loop:
        logger.info("Starting news intel loop every %d minutes", args.loop)
        while True:
            try:
                run()
            except Exception:
                logger.exception("Loop iteration failed")
            time.sleep(args.loop * 60)
    else:
        run()
