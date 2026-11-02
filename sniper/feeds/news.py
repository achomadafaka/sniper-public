"""News feed adapter (NewsAPI + RSS).

Polls RSS feeds on a 15-minute cadence and optionally enriches with NewsAPI.
Stores normalized article snapshots in the ``news_snapshots`` table.
RSS-only mode works without any API key.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from .base import FeedAdapter
from sniper.settings.web import INSTANCE_TAG

logger = logging.getLogger(__name__)

# Tracking params to strip from URLs for canonicalization
_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid",
})

# Default RSS feeds (verified working as of 2026-03-11)
DEFAULT_RSS_FEEDS = (
    # feeds.reuters.com no longer resolves (checked 2026-10-27); removed
    "https://rss.app/feeds/v1.1/tsWbSnBk1ym9aGdv.xml",  # AP Top Headlines
    "https://www.aljazeera.com/xml/rss/all.xml",
)

# Desk news capture (docs/superpowers/specs/2026-10-27-desk-news-capture-design.md): (source id, kind, url, poll every s). Public, no key.
DESK_NEWS_SOURCES = (
    ("aljazeera", "rss", "https://www.aljazeera.com/xml/rss/all.xml", 900),
    ("google_trends_us", "rss", "https://trends.google.com/trending/rss?geo=US", 900),
    ("reddit_memes", "rss", "https://www.reddit.com/r/memes/hot.rss", 600),
    ("reddit_popular", "rss", "https://www.reddit.com/r/popular/hot.rss", 600),
    ("knowyourmeme", "rss", "https://knowyourmeme.com/newsfeed.rss", 1800),
    ("hackernews", "rss", "https://news.ycombinator.com/rss", 900),
    ("wikipedia_top", "wiki_top", "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/{date}", 21600),
)

# Sentiment keywords to extract from news titles/descriptions
SENTIMENT_KEYWORDS = (
    "oil", "crude", "brent", "wti", "petroleum", "opec", "barrel",
    "sanctions", "war", "military", "ceasefire", "invasion", "troops",
    "missile", "airstrike", "escalation", "conflict", "embargo",
    "inflation", "recession", "rally", "crash", "surge", "plunge",
    "bitcoin", "crypto",
)


def _canonicalize_url(url: str | None) -> str:
    """Strip tracking parameters from a URL for dedup."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        cleaned = {k: v for k, v in params.items() if k.lower() not in _STRIP_PARAMS}
        new_query = urlencode(cleaned, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url or ""


def _compute_article_hash(canonical_url: str | None, title: str, published_at: str) -> str:
    """Compute a dedup hash for an article.

    Primary: hash of canonical URL.
    Fallback: hash of title + published_at (for articles without URLs).
    """
    if canonical_url:
        source = canonical_url
    else:
        source = f"{title}|{published_at}"
    return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:32]


def _extract_sentiment_keywords(text: str) -> List[str]:
    """Extract known sentiment keywords from text."""
    lower = text.lower()
    return [kw for kw in SENTIMENT_KEYWORDS if kw in lower]


class NewsFeedAdapter(FeedAdapter):
    """NewsAPI + RSS feed adapter."""

    feed_id = "news"
    display_name = "News (NewsAPI + RSS)"
    default_poll_seconds = 900  # 15 minutes
    mode = "poll"

    def validate_config(self) -> bool:
        """Always True — RSS works without API key."""
        return True

    def warmup(self, conn) -> None:
        from db import ensure_news_snapshots_table
        ensure_news_snapshots_table(conn)

    def tick(self, conn) -> None:
        from db import insert_news_snapshot

        # Collect articles from all sources
        articles: List[Dict[str, Any]] = []
        articles.extend(self._fetch_rss())
        if os.environ.get("NEWS_API_KEY", "").strip():
            articles.extend(self._fetch_newsapi())

        stored = 0
        for article in articles:
            title = str(article.get("title") or "")
            if not title:
                continue
            canonical_url = _canonicalize_url(article.get("url"))
            published_at = str(article.get("published_at") or "")
            article_hash = _compute_article_hash(canonical_url, title, published_at)
            sentiment_kws = _extract_sentiment_keywords(
                f"{title} {article.get('description', '')}"
            )

            row_id = insert_news_snapshot(
                conn,
                instance_tag=INSTANCE_TAG,
                source_name=str(article.get("source_name") or "unknown"),
                canonical_url=canonical_url,
                article_hash=article_hash,
                title=title,
                description=str(article.get("description") or ""),
                published_at=published_at,
                domain=None,  # let db.py classify internally
                sentiment_keywords=json.dumps(sentiment_kws),
                payload_json=json.dumps(article.get("payload", {})),
            )
            if row_id is not None:
                stored += 1

        if stored:
            logger.info("news snapshots stored: %d new articles", stored)
        else:
            logger.debug("news tick: no new articles")

    # -- internal helpers -------------------------------------------------------

    def _get_rss_urls(self) -> List[str]:
        env_feeds = os.environ.get("NEWS_RSS_FEEDS", "").strip()
        if env_feeds:
            return [u.strip() for u in env_feeds.split(",") if u.strip()]
        return list(DEFAULT_RSS_FEEDS)

    def _fetch_rss(self) -> List[Dict[str, Any]]:
        """Parse RSS feeds and return normalized article dicts."""
        articles: List[Dict[str, Any]] = []
        try:
            import feedparser
        except ImportError:
            logger.error("feedparser not installed — cannot fetch RSS feeds")
            return articles

        for feed_url in self._get_rss_urls():
            try:
                feed = feedparser.parse(feed_url)
                source_name = f"rss_{feed.feed.get('title', 'unknown')[:20]}".lower().replace(" ", "_")
                for entry in feed.entries[:20]:  # cap per feed
                    articles.append({
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "description": entry.get("summary", entry.get("description", "")),
                        "published_at": entry.get("published", ""),
                        "source_name": source_name,
                        "payload": {"feed_url": feed_url},
                    })
            except Exception:
                logger.exception("news: failed to parse RSS feed %s", feed_url)

        return articles

    def _fetch_newsapi(self) -> List[Dict[str, Any]]:
        """Fetch top headlines from NewsAPI (if key is set)."""
        import urllib.request
        import urllib.error

        api_key = os.environ.get("NEWS_API_KEY", "").strip()
        if not api_key:
            return []

        articles: List[Dict[str, Any]] = []
        url = "https://newsapi.org/v2/top-headlines?category=business&language=en&pageSize=20"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Sniper/1.0",
                "X-Api-Key": api_key,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for item in data.get("articles", []):
                    articles.append({
                        "title": item.get("title", ""),
                        "url": item.get("url", ""),
                        "description": item.get("description", ""),
                        "published_at": item.get("publishedAt", ""),
                        "source_name": "newsapi",
                        "payload": {"source": item.get("source", {})},
                    })
        except Exception:
            logger.exception("news: failed to fetch from NewsAPI")

        return articles
