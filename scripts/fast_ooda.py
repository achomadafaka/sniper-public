#!/usr/bin/env python3
"""Fast OODA loop — deterministic event-to-boost mapper.

Runs every 15min. Matches recent news headlines against open positions
using keyword overlap. Writes micro-boosts to market_boosts_current.
No LLM calls. Cost: $0. Latency: <5 seconds.

This is the fast-path complement to the 4h analyst cycle. It catches
obvious event->position connections in 15min instead of 4h.

Usage:
    python3 scripts/fast_ooda.py              # full run
    python3 scripts/fast_ooda.py --quiet      # no Telegram output
    python3 scripts/fast_ooda.py --dry-run    # print matches, don't write boosts
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Project root bootstrapping
# ---------------------------------------------------------------------------
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Load .env
_env_path = os.path.join(ROOT, ".env")
if os.path.isfile(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                if _k.strip() and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()

logger = logging.getLogger("fast_ooda")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", os.path.join(ROOT, "sniper_tracker.db"))

# Micro-boost cap — analyst boosts go up to +/-30; fast OODA is capped lower
MAX_BOOST = 15
MIN_BOOST = -15

# How many hours until a fast_ooda micro-boost expires
MICRO_BOOST_EXPIRY_HOURS = 2

# Minimum keyword overlap for a news-position match
MIN_KEYWORD_OVERLAP = 2

# How many minutes of recent news to scan
NEWS_WINDOW_MINUTES = 30

# Telegram alert threshold — only alert if abs(boost) >= this
TELEGRAM_ALERT_THRESHOLD = 15

# Stopwords for keyword extraction (short common words to skip)
STOP_WORDS = frozenset({
    "will", "the", "a", "an", "in", "on", "by", "of", "to", "be", "is",
    "for", "and", "or", "before", "after", "during", "end", "this", "that",
    "with", "from", "have", "has", "been", "are", "was", "were", "not",
    "but", "its", "more", "than", "what", "over", "into", "could", "would",
    "about", "says", "said", "also", "new", "news", "report", "reports",
    "2024", "2025", "2026", "2027",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
})

# Keywords that suggest the event HAPPENED or is PROGRESSING (positive momentum)
HAPPENED_POSITIVE = frozenset({
    "confirmed", "approved", "passed", "signed", "enacted", "launched",
    "strikes", "invaded", "deployed", "wins", "elected", "resolved",
    "announces", "reveals", "breakthrough", "deal", "agreement",
    "ratified", "awarded", "surges", "soars", "rallies", "gains",
    "advances", "escalates", "expands", "achieves", "succeeds",
})

# Keywords that suggest the event FAILED or is RETREATING (negative momentum)
HAPPENED_NEGATIVE = frozenset({
    "rejected", "failed", "collapsed", "withdrawn", "cancelled", "vetoed",
    "loses", "defeated", "suspended", "blocked", "denied", "abandoned",
    "retreats", "ceasefire", "surrender", "negotiating", "stalls",
    "delays", "postponed", "halted", "downgrades", "slumps", "plunges",
    "declines", "drops", "falls", "reverses", "opposes", "rejects",
})


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

def extract_keywords(text: str, min_length: int = 4) -> set:
    """Extract significant keywords from text.

    Returns a set of lowercase words with length >= min_length,
    excluding stopwords.
    """
    words = set(re.findall(r"\b[a-z]+\b", text.lower()))
    return {w for w in words if len(w) >= min_length and w not in STOP_WORDS}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_match(headline_lower: str, position_outcome: str, overlap_count: int) -> int:
    """Score a news-position match. Returns boost value clamped to [MIN_BOOST, MAX_BOOST].

    Scoring logic:
    - Count sentiment keywords in the headline (positive and negative)
    - Net sentiment = pos_count - neg_count
    - For YES positions: net positive = event progressing = bullish = positive boost
    - For NO positions:  net positive = event progressing = bearish = negative boost
    - Each net sentiment point = 5 boost, capped at +/-15
    - Overlap count adds a small bonus (1 per extra keyword beyond 2)
    """
    pos_count = sum(1 for w in HAPPENED_POSITIVE if w in headline_lower)
    neg_count = sum(1 for w in HAPPENED_NEGATIVE if w in headline_lower)

    if pos_count == 0 and neg_count == 0:
        return 0  # Neutral news -- no boost

    # Net sentiment: positive = event happening/progressing
    net = pos_count - neg_count

    # For YES positions: event happening = good (boost positive)
    # For NO positions: event happening = bad (boost negative)
    outcome_upper = (position_outcome or "YES").upper()
    if outcome_upper == "NO":
        net = -net

    # Scale: each net sentiment word = +/-5, capped
    raw_boost = net * 5

    # Small bonus for strong keyword overlap (beyond the minimum 2)
    overlap_bonus = max(0, overlap_count - MIN_KEYWORD_OVERLAP)
    if raw_boost > 0:
        raw_boost += overlap_bonus
    elif raw_boost < 0:
        raw_boost -= overlap_bonus

    return max(MIN_BOOST, min(MAX_BOOST, raw_boost))


# ---------------------------------------------------------------------------
# DB readers
# ---------------------------------------------------------------------------

def _get_db_conn():
    """Get a read-only SQLite connection."""
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_recent_news(conn, window_minutes: int = NEWS_WINDOW_MINUTES) -> List[Dict]:
    """Fetch news items from the last N minutes."""
    try:
        rows = conn.execute(
            """
            SELECT title, source, published_at, description
            FROM news_items
            WHERE fetched_at >= datetime('now', ? || ' minutes')
            ORDER BY published_at DESC
            LIMIT 50
            """,
            (str(-window_minutes),),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Failed to fetch news_items: %s", e)
        return []


def fetch_open_positions(conn) -> List[Dict]:
    """Fetch all open positions from paper_trades and strategy_positions."""
    positions = []

    # Paper trades
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT pt.condition_id, pt.market_title, pt.outcome, pt.entry_price
            FROM paper_trades pt
            WHERE pt.status = 'open' AND pt.duplicate_of IS NULL
            """
        ).fetchall()
        for r in rows:
            positions.append({
                "condition_id": r["condition_id"],
                "market_title": r["market_title"] or "",
                "outcome": r["outcome"] or "YES",
                "entry_price": r["entry_price"] or 0.0,
                "source": "paper_trades",
            })
    except Exception as e:
        logger.warning("Failed to fetch paper_trades: %s", e)

    # Strategy positions
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT sp.condition_id, mm.title, sp.outcome, sp.entry_price
            FROM strategy_positions sp
            LEFT JOIN market_metadata mm ON sp.condition_id = mm.condition_id
            WHERE sp.state = 'open'
            """
        ).fetchall()
        for r in rows:
            positions.append({
                "condition_id": r["condition_id"],
                "market_title": r["title"] or "",
                "outcome": r["outcome"] or "YES",
                "entry_price": r["entry_price"] or 0.0,
                "source": "strategy_positions",
            })
    except Exception as e:
        logger.warning("Failed to fetch strategy_positions: %s", e)

    # Deduplicate by condition_id (keep first occurrence)
    seen = set()
    unique = []
    for p in positions:
        cid = p["condition_id"]
        if cid and cid not in seen:
            seen.add(cid)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Matching engine
# ---------------------------------------------------------------------------

def find_matches(
    news_items: List[Dict],
    positions: List[Dict],
    min_overlap: int = MIN_KEYWORD_OVERLAP,
) -> List[Dict]:
    """Match news headlines against open position titles via keyword overlap.

    Returns list of match dicts with:
        - news_title, news_source
        - condition_id, market_title, outcome
        - overlap_keywords, overlap_count
        - boost (signed integer)
    """
    # Pre-compute position keyword sets
    position_cache: List[Tuple[Dict, frozenset]] = []
    for pos in positions:
        kw = extract_keywords(pos["market_title"])
        if kw:
            position_cache.append((pos, frozenset(kw)))

    matches = []
    for news in news_items:
        title = news.get("title", "")
        desc = news.get("description", "") or ""
        headline_text = title + " " + desc
        news_kw = extract_keywords(headline_text)
        if not news_kw:
            continue

        headline_lower = headline_text.lower()

        for pos, pos_kw in position_cache:
            overlap = news_kw & pos_kw
            if len(overlap) < min_overlap:
                continue

            boost = score_match(headline_lower, pos["outcome"], len(overlap))
            if boost == 0:
                continue  # Neutral -- skip

            matches.append({
                "news_title": title,
                "news_source": news.get("source", ""),
                "condition_id": pos["condition_id"],
                "market_title": pos["market_title"],
                "outcome": pos["outcome"],
                "overlap_keywords": sorted(overlap),
                "overlap_count": len(overlap),
                "boost": boost,
            })

    return matches


# ---------------------------------------------------------------------------
# Supabase integration
# ---------------------------------------------------------------------------

def read_current_boosts() -> Dict[str, Any]:
    """Read current market_boosts_current from Supabase. Returns raw dict."""
    try:
        from bot.supabase_helper import get_memory
        m = get_memory("market_boosts_current", type_filter="knowledge")
        if not m:
            return {}
        content = m.get("content", "{}")
        return json.loads(content) if isinstance(content, str) else (content or {})
    except Exception as e:
        logger.warning("Failed to read market_boosts_current: %s", e)
        return {}


def merge_micro_boosts(
    existing: Dict[str, Any],
    matches: List[Dict],
) -> Dict[str, Any]:
    """Merge new fast_ooda micro-boosts into existing boosts.

    Rules:
    - Only add/update entries with source == "fast_ooda"
    - Never overwrite entries from other sources (analyst, etc.)
    - If multiple news items match the same condition_id, use the strongest boost
    """
    from datetime import datetime, timezone

    now_iso = datetime.now(timezone.utc).isoformat()
    now_dt = datetime.now(timezone.utc)

    # Prune expired fast_ooda entries before merging so stale boosts don't accumulate.
    # Each fast_ooda entry stores written_at + expires_hours; anything past deadline is dropped.
    pruned = {}
    for k, v in existing.items():
        if isinstance(v, dict) and v.get("source") == "fast_ooda":
            written_at_str = v.get("written_at")
            exp_h = float(v.get("expires_hours", MICRO_BOOST_EXPIRY_HOURS))
            if written_at_str:
                try:
                    from datetime import datetime as _dt
                    written_dt = _dt.fromisoformat(written_at_str)
                    if (now_dt - written_dt).total_seconds() > exp_h * 3600:
                        continue  # expired — drop it
                except Exception:
                    pass  # unparseable timestamp — keep the entry
        pruned[k] = v
    merged = pruned

    # Group matches by condition_id, pick strongest boost per position
    best_per_cid: Dict[str, Dict] = {}
    for m in matches:
        cid = m["condition_id"].lower()
        if cid not in best_per_cid or abs(m["boost"]) > abs(best_per_cid[cid]["boost"]):
            best_per_cid[cid] = m

    for cid, match in best_per_cid.items():
        existing_entry = merged.get(cid)

        # Only overwrite if existing entry is from fast_ooda or doesn't exist
        if existing_entry and isinstance(existing_entry, dict):
            if existing_entry.get("source") != "fast_ooda":
                # Analyst boost exists — defer to the analyst, skip micro-boost.
                # (Previous __fast_ooda namespaced-key approach created ghost keys
                # that broke brain health E4 check and couldn't be used by strategies.)
                continue

        # Write or overwrite fast_ooda entry; include written_at for expiry pruning
        merged[cid] = {
            "boost": match["boost"],
            "reason": f"[fast_ooda] {match['news_title'][:80]}",
            "direction": match["outcome"].upper(),
            "confidence": "medium" if abs(match["boost"]) >= 10 else "low",
            "source": "fast_ooda",
            "expires_hours": MICRO_BOOST_EXPIRY_HOURS,
            "written_at": now_iso,
            "overlap_keywords": match["overlap_keywords"],
        }

    return merged


def write_boosts(merged: Dict[str, Any]) -> bool:
    """Write merged boosts back to Supabase."""
    try:
        from bot.supabase_helper import upsert_memory
        upsert_memory(
            type="knowledge",
            name="market_boosts_current",
            description="Market boosts (analyst + fast_ooda micro-boosts)",
            content=json.dumps(merged),
            source="vps",
        )
        return True
    except Exception as e:
        logger.error("Failed to write market_boosts_current: %s", e)
        return False


def write_run_summary(matches: List[Dict], boosts_written: int) -> bool:
    """Write a run summary to knowledge/fast_ooda_last_run."""
    try:
        from bot.supabase_helper import upsert_memory
        now = datetime.now(timezone.utc).isoformat()
        summary = {
            "timestamp": now,
            "matches_found": len(matches),
            "boosts_written": boosts_written,
            "matches": [
                {
                    "news_title": m["news_title"][:80],
                    "market_title": m["market_title"][:60],
                    "boost": m["boost"],
                    "overlap_keywords": m["overlap_keywords"],
                }
                for m in matches[:10]  # Cap at 10 for readability
            ],
        }
        upsert_memory(
            type="knowledge",
            name="fast_ooda_last_run",
            description=f"Fast OODA run at {now}: {len(matches)} matches, {boosts_written} boosts",
            content=json.dumps(summary, indent=2),
            source="vps",
        )
        return True
    except Exception as e:
        logger.warning("Failed to write fast_ooda_last_run: %s", e)
        return False


# ---------------------------------------------------------------------------
# Telegram notification
# ---------------------------------------------------------------------------

def send_telegram_alert(matches: List[Dict], quiet: bool = False) -> None:
    """Send Telegram notification for strong boosts (>= threshold)."""
    if quiet:
        return

    strong_matches = [m for m in matches if abs(m["boost"]) >= TELEGRAM_ALERT_THRESHOLD]
    if not strong_matches:
        return

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.debug("Telegram not configured, skipping alert")
        return

    lines = ["*Fast OODA Alert*"]
    for m in strong_matches[:5]:  # Cap at 5 alerts
        direction = "+" if m["boost"] > 0 else ""
        lines.append(
            f"  {direction}{m['boost']} | {m['market_title'][:40]}"
            f"\n  News: {m['news_title'][:60]}"
            f"\n  Keywords: {', '.join(m['overlap_keywords'][:5])}"
        )

    text = "\n\n".join(lines)
    # Escape Markdown special chars for MarkdownV2
    # Use plain text to avoid escaping headaches in a cron script
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        logger.info("Telegram alert sent (%d strong matches)", len(strong_matches))
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


# ---------------------------------------------------------------------------
# Hyperliquid funding micro-boosts
# ---------------------------------------------------------------------------

# Keywords that link BTC/ETH to Polymarket position titles
_HL_ASSET_KEYWORDS = {
    "BTC": {"bitcoin", "btc"},
    "ETH": {"ethereum", "eth", "ether"},
}

# Funding rate threshold for micro-boost (same as liquidation_monitor)
_HL_FUNDING_THRESHOLD = 0.0005  # 0.05% per 8h


def _check_hyperliquid_funding(positions: List[Dict], conn=None) -> List[Dict]:
    """Check Hyperliquid funding rates and generate micro-boosts for matching positions.

    High positive funding = longs overleveraged = bearish for YES positions.
    High negative funding = shorts overleveraged = bullish for YES positions.

    If *conn* is provided it is used directly (for testing); otherwise a new
    connection to DB_PATH is opened and closed.

    Returns list of match dicts compatible with the news-match format.
    """
    import sqlite3

    boosts = []
    try:
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
        try:
            hl_rows = conn.execute(
                """SELECT asset, funding_rate, open_interest
                   FROM hyperliquid_snapshots
                   WHERE asset IN ('BTC', 'ETH')
                     AND captured_at >= datetime('now', '-15 minutes')
                   ORDER BY captured_at DESC""",
            ).fetchall()
        finally:
            if own_conn:
                conn.close()

        if not hl_rows:
            return boosts

        # Deduplicate: keep latest per asset
        seen_assets = set()
        hl_latest = []
        for row in hl_rows:
            asset = row["asset"]
            if asset not in seen_assets:
                seen_assets.add(asset)
                hl_latest.append(dict(row))

        for hl in hl_latest:
            asset = hl["asset"]
            funding = float(hl["funding_rate"])

            if abs(funding) <= _HL_FUNDING_THRESHOLD:
                continue

            # High funding = longs overleveraged = bearish
            # For YES positions on BTC/ETH markets: bearish = negative boost
            # For NO positions on BTC/ETH markets: bearish = positive boost
            keywords = _HL_ASSET_KEYWORDS.get(asset, set())

            for pos in positions:
                title_lower = pos["market_title"].lower()
                if not any(kw in title_lower for kw in keywords):
                    continue

                # Direction: high positive funding is bearish for the asset
                outcome_upper = (pos.get("outcome") or "YES").upper()
                if funding > 0:
                    # Longs overleveraged: bearish for asset
                    raw_boost = -5 if outcome_upper == "YES" else 5
                else:
                    # Shorts overleveraged: bullish for asset
                    raw_boost = 5 if outcome_upper == "YES" else -5

                # Scale by magnitude (stronger funding = stronger boost)
                magnitude = abs(funding) / _HL_FUNDING_THRESHOLD
                if magnitude > 2:
                    raw_boost = int(raw_boost * 1.5)

                raw_boost = max(MIN_BOOST, min(MAX_BOOST, raw_boost))

                if raw_boost == 0:
                    continue

                direction_label = "bearish" if funding > 0 else "bullish"
                funding_pct = funding * 100

                boosts.append({
                    "news_title": f"[HL] {asset} funding {funding_pct:+.4f}%/8h ({direction_label})",
                    "news_source": "hyperliquid",
                    "condition_id": pos["condition_id"],
                    "market_title": pos["market_title"],
                    "outcome": pos.get("outcome", "YES"),
                    "overlap_keywords": [asset.lower(), "funding", "hyperliquid"],
                    "overlap_count": 3,
                    "boost": raw_boost,
                })

    except Exception as e:
        logger.debug("Hyperliquid funding check skipped: %s", e)

    return boosts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    *,
    quiet: bool = False,
    dry_run: bool = False,
    window_minutes: int = NEWS_WINDOW_MINUTES,
) -> Dict[str, Any]:
    """Execute the fast OODA loop.

    Returns a summary dict with matches_found, boosts_written, etc.
    """
    start = time.time()
    result: Dict[str, Any] = {
        "matches_found": 0,
        "boosts_written": 0,
        "elapsed_seconds": 0.0,
        "matches": [],
    }

    # 1. OBSERVE: read recent news
    conn = _get_db_conn()
    try:
        news = fetch_recent_news(conn, window_minutes=window_minutes)
        positions = fetch_open_positions(conn)
    finally:
        conn.close()

    if not news:
        logger.info("No recent news in last %d minutes", window_minutes)
        result["elapsed_seconds"] = round(time.time() - start, 2)
        return result

    if not positions:
        logger.info("No open positions to match against")
        result["elapsed_seconds"] = round(time.time() - start, 2)
        return result

    logger.info("Scanning %d news items against %d open positions", len(news), len(positions))

    # 2. ORIENT + DECIDE: match and score
    matches = find_matches(news, positions)
    result["matches_found"] = len(matches)
    result["matches"] = matches

    if not matches:
        logger.info("No matches found")
        if not dry_run:
            write_run_summary(matches, 0)
        result["elapsed_seconds"] = round(time.time() - start, 2)
        return result

    for m in matches:
        direction = "+" if m["boost"] > 0 else ""
        logger.info(
            "MATCH: boost=%s%d  market=%s  news=%s  keywords=%s",
            direction, m["boost"],
            m["market_title"][:40],
            m["news_title"][:50],
            ",".join(m["overlap_keywords"][:4]),
        )

    # 2b. ORIENT: Hyperliquid funding/OI micro-boosts for BTC/ETH positions
    hl_boosts = _check_hyperliquid_funding(positions)
    if hl_boosts:
        matches.extend(hl_boosts)
        result["matches_found"] = len(matches)
        result["matches"] = matches
        for m in hl_boosts:
            direction = "+" if m["boost"] > 0 else ""
            logger.info(
                "HL_MATCH: boost=%s%d  market=%s  reason=%s",
                direction, m["boost"],
                m["market_title"][:40],
                m.get("news_title", "")[:50],
            )

    if dry_run:
        print(f"\n[DRY RUN] {len(matches)} matches found:")
        for m in matches:
            direction = "+" if m["boost"] > 0 else ""
            print(f"  {direction}{m['boost']:>3d}  {m['market_title'][:50]}")
            print(f"        news: {m.get('news_title', '')[:60]}")
            print(f"        keywords: {', '.join(m.get('overlap_keywords', []))}")
        result["elapsed_seconds"] = round(time.time() - start, 2)
        return result

    # 3. ACT: write micro-boosts to Supabase
    existing_boosts = read_current_boosts()
    merged = merge_micro_boosts(existing_boosts, matches)

    # Count how many new fast_ooda entries we're writing
    new_fo_count = sum(
        1 for k, v in merged.items()
        if isinstance(v, dict) and v.get("source") == "fast_ooda"
    )
    result["boosts_written"] = new_fo_count

    if write_boosts(merged):
        logger.info("Wrote %d micro-boosts to market_boosts_current", new_fo_count)
    else:
        logger.error("Failed to write boosts")

    # 4. RECORD: write run summary
    write_run_summary(matches, new_fo_count)

    # 5. ALERT: Telegram for strong boosts
    send_telegram_alert(matches, quiet=quiet)

    result["elapsed_seconds"] = round(time.time() - start, 2)
    logger.info(
        "Fast OODA complete: %d matches, %d boosts written, %.1fs",
        len(matches), new_fo_count, result["elapsed_seconds"],
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Fast OODA loop — deterministic event-to-boost mapper")
    parser.add_argument("--quiet", action="store_true", help="Suppress Telegram notifications")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without writing boosts")
    parser.add_argument("--window", type=int, default=NEWS_WINDOW_MINUTES,
                        help=f"Minutes of recent news to scan (default: {NEWS_WINDOW_MINUTES})")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    result = run(quiet=args.quiet, dry_run=args.dry_run, window_minutes=args.window)

    if not args.quiet:
        print(f"Fast OODA: {result['matches_found']} matches, "
              f"{result['boosts_written']} boosts written, "
              f"{result['elapsed_seconds']:.1f}s")


if __name__ == "__main__":
    main()
