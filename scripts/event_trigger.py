#!/usr/bin/env python3
"""
event_trigger.py — RSS keyword watcher that fires agents on breaking news.

Runs every 10 minutes via systemd timer. Polls a curated set of fast-breaking
RSS feeds; when an entry's urgency score clears the threshold it immediately
fires world_analyst or finance_analyst in --quick mode — bypassing the
4-hour schedule for genuinely breaking events.

State lives in logs/event_trigger_state.json:
  seen_ids     — {guid: unix_ts}   dedup window (pruned at 24 h)
  last_trigger — {agent: unix_ts}  per-agent cooldown gate
  etags        — {src: etag}       HTTP conditional-GET validators
  modified     — {src: modified}   HTTP conditional-GET validators

Design notes:
  - feedparser etag/modified support → conditional GETs, ~304 most cycles
  - feeds fetched in parallel (ThreadPoolExecutor) — total latency = slowest feed
  - score = Σ topic_weight + Σ headline_boost
  - cooldown stops the same agent firing twice in a 3-hour window
  - subprocess.Popen + start_new_session so the timer's cgroup isn't held
  - one Telegram ping before launch so you know WHY the agent ran early
"""

import hashlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sniper.services.news_intel import TOPIC_KEYWORDS  # noqa: E402 — needs sys.path set first

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("event-trigger")

# ── Config ────────────────────────────────────────────────────────────────────
STATE_FILE      = ROOT / "logs" / "event_trigger_state.json"
SCORE_THRESHOLD = 5        # minimum combined score to fire an agent
COOLDOWN_S      = 3 * 3600 # don't retrigger the same agent within 3 hours
SEEN_TTL_S      = 24 * 3600
FETCH_TIMEOUT   = 8        # seconds per feed (applied via socket default timeout)

VENV_PY = str(ROOT / ".venv" / "bin" / "python")

# Curated subset: fast-updating, high signal-to-noise, geographically diverse.
# Full list lives in news_intel.RSS_FEEDS — we only poll the fastest breakers here.
WATCH_FEEDS = {
    "bbc_world":      "https://feeds.bbci.co.uk/news/world/rss.xml",
    "guardian_world": "https://www.theguardian.com/world/rss",
    "axios":          "https://api.axios.com/feed/",
    "aljazeera":      "https://www.aljazeera.com/xml/rss/all.xml",
    "france24":       "https://www.france24.com/en/rss",
    "cnbc_economy":   "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "marketwatch":    "https://feeds.marketwatch.com/marketwatch/topstories/",
    "meduza":         "https://meduza.io/rss/en/all",
}

# topic → (urgency_weight, agent_key, --domains flag for world_analyst)
# These topic keys must exist in news_intel.TOPIC_KEYWORDS
URGENCY_MAP: dict[str, tuple[int, str, str]] = {
    "military":   (5, "world_analyst",   "--domains geo"),
    "iran":       (4, "world_analyst",   "--domains geo"),
    "russia":     (4, "world_analyst",   "--domains geo"),
    "china":      (4, "world_analyst",   "--domains geo"),
    "israel":     (4, "world_analyst",   "--domains geo"),
    "northkorea": (4, "world_analyst",   "--domains geo"),
    "trump":      (3, "world_analyst",   "--domains politics"),
    "election":   (3, "world_analyst",   "--domains politics"),
    "greenland":  (3, "world_analyst",   "--domains politics"),
    "fed":        (4, "finance_analyst", ""),
    "oil":        (3, "finance_analyst", ""),
    "bitcoin":    (3, "finance_analyst", ""),
}

# Exact-phrase boosts applied on top of topic scores (lowercase match)
HEADLINE_BOOSTS: list[tuple[int, str]] = [
    (5, "declares war"),
    (5, "nuclear launch"),
    (5, "coup"),
    (5, "assassinated"),
    (4, "nuclear strike"),
    (4, "nuclear test"),
    (4, "invades"),
    (4, "invaded"),
    (3, "emergency session"),
    (3, "breaking:"),
    (2, "airstrike"),
    (2, "missile strike"),
    (2, "sanctions"),
    (2, "emergency"),
]

AGENT_CMDS: dict[str, list[str]] = {
    "world_analyst":   [VENV_PY, str(ROOT / "scripts" / "world_analyst.py"), "--quick"],
    "finance_analyst": [VENV_PY, str(ROOT / "sniper" / "services" / "finance_analyst.py"),            "--quick"],
}

_KW_CACHE: dict[tuple[str, ...], re.Pattern[str]] = {}


# ── State helpers ─────────────────────────────────────────────────────────────
def _load() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": {}, "last_trigger": {}, "etags": {}, "modified": {}}


def _save(state: dict) -> None:
    """Atomic write: persist to .tmp then rename so a mid-write kill can't corrupt state."""
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_FILE)


def _prune(seen: dict[str, float], now: float) -> dict[str, float]:
    cutoff = now - SEEN_TTL_S
    return {k: v for k, v in seen.items() if v > cutoff}


def _derive_eid(entry) -> str:
    """Stable per-entry identifier. Falls back to sha1(title|published) when the
    feed omits both id and link — guarantees dedup even for malformed entries.

    Uses hashlib.sha1 (not builtin hash()) because hash() is randomized per
    interpreter invocation, which would defeat cross-run deduplication.
    """
    eid = entry.get("id") or entry.get("link", "")
    if eid:
        return eid
    fp = (entry.get("title", "") + "|" + entry.get("published", "")).encode("utf-8", "replace")
    return "sha1:" + hashlib.sha1(fp).hexdigest()[:16]


def _kw_pattern(words: list[str]) -> re.Pattern[str]:
    key = tuple(words)
    pattern = _KW_CACHE.get(key)
    if pattern is None:
        # Match whole keywords/phrases only; avoids false positives like
        # "war" in "reward" while preserving multi-word topic phrases.
        pattern = re.compile(
            r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b",
            re.IGNORECASE,
        )
        _KW_CACHE[key] = pattern
    return pattern


# ── Feed fetcher (used by ThreadPoolExecutor) ─────────────────────────────────
def _fetch(src: str, url: str, etag: str | None, modified: str | None) -> tuple[str, object]:
    """Fetch one feed; returns (src, parsed_feed). Timeout enforced via socket default."""
    feed = feedparser.parse(
        url,
        etag=etag,
        modified=modified,
        request_headers={"User-Agent": "Sniper/1.0 (+event-trigger)"},
    )
    return src, feed


# ── Scoring ───────────────────────────────────────────────────────────────────
def _score(title: str, summary: str) -> tuple[int, str, str]:
    """
    Returns (score, best_agent, domain_flag).
    score = Σ matched topic weights + Σ matched headline boosts.
    best_agent is chosen by highest per-agent topic score; headline boosts are
    additive to the total but don't influence agent selection.
    """
    text = (title + " " + (summary or "")).lower()
    agent_scores: dict[str, int] = {}
    agent_domains: dict[str, str] = {}

    for topic, keywords in TOPIC_KEYWORDS.items():
        if topic not in URGENCY_MAP:
            continue
        weight, agent, domains = URGENCY_MAP[topic]
        if _kw_pattern(keywords).search(text):
            agent_scores[agent] = agent_scores.get(agent, 0) + weight
            if domains:
                agent_domains[agent] = domains

    total = sum(agent_scores.values())
    for boost, phrase in HEADLINE_BOOSTS:
        if phrase in text:
            total += boost

    best = max(agent_scores, key=lambda a: agent_scores[a]) if agent_scores else "world_analyst"
    return total, best, agent_domains.get(best, "--domains geo,politics")


# ── Telegram ping ─────────────────────────────────────────────────────────────
def _notify(agent: str, score: int, headline: str, source: str) -> None:
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    label = "🌍 World Analyst" if agent == "world_analyst" else "💹 Finance Analyst"
    text  = f"⚡ *Breaking trigger* — {label} fired early\n\nScore: `{score}` · Source: `{source}`\n📰 _{headline[:120]}_"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=8,
        )
    except Exception as e:
        log.warning("Telegram notify failed: %s", e)


# ── Main ──────────────────────────────────────────────────────────────────────
def run() -> None:
    load_dotenv(ROOT / ".env")  # no-op if vars already set (systemd EnvironmentFile)

    # Apply socket timeout so feedparser can't block indefinitely on a hung feed
    socket.setdefaulttimeout(FETCH_TIMEOUT)

    now          = time.time()
    state        = _load()
    seen         = _prune(state.get("seen_ids", {}), now)
    last_trigger = state.get("last_trigger", {})
    etags        = state.get("etags", {})
    modified_hdrs = state.get("modified", {})
    triggered: set[str] = set()

    def _persist() -> None:
        """Snapshot in-memory state → disk. Safe to call anywhere."""
        state.update({
            "seen_ids": seen,
            "last_trigger": last_trigger,
            "etags": etags,
            "modified": modified_hdrs,
        })
        try:
            _save(state)
        except Exception as e:
            log.error("state save failed: %s", e)

    try:
        # Fetch all feeds in parallel — total latency = slowest feed, not sum of all
        fetch_args = [(src, url, etags.get(src), modified_hdrs.get(src))
                      for src, url in WATCH_FEEDS.items()]
        with ThreadPoolExecutor(max_workers=len(WATCH_FEEDS)) as pool:
            futures = {pool.submit(_fetch, *args): args[0] for args in fetch_args}
            for future in as_completed(futures):
                src = futures[future]
                try:
                    src, feed = future.result()
                except Exception as e:
                    log.warning("Feed %s: %s", src, e)
                    continue

                if getattr(feed, "status", 0) == 304:
                    log.debug("%s: 304 not modified", src)
                else:
                    if feed.get("etag"):
                        etags[src] = feed.etag
                    if feed.get("modified"):
                        modified_hdrs[src] = feed.modified

                for entry in feed.entries[:15]:
                    try:
                        eid = _derive_eid(entry)
                        if eid in seen:
                            continue
                        seen[eid] = now

                        title   = entry.get("title", "")
                        summary = entry.get("summary", "")
                        score, agent, domains = _score(title, summary)

                        if score < SCORE_THRESHOLD:
                            continue
                        if agent in triggered:
                            continue
                        if now - last_trigger.get(agent, 0) < COOLDOWN_S:
                            log.info("Score=%d [%s] cooldown active — skip '%s'", score, src, title[:60])
                            continue

                        log.info("🚨 score=%d [%s] → %s %s | '%s'", score, src, agent, domains, title[:80])
                        _notify(agent, score, title, src)

                        log_file = ROOT / "logs" / f"{agent}_event_trigger.log"
                        log_file.parent.mkdir(parents=True, exist_ok=True)
                        cmd = AGENT_CMDS[agent] + (domains.split() if domains else []) + ["--quiet"]
                        try:
                            with open(log_file, "a") as lf:
                                subprocess.Popen(
                                    cmd,
                                    cwd=str(ROOT),
                                    start_new_session=True,
                                    stdout=lf,
                                    stderr=subprocess.STDOUT,
                                )
                            last_trigger[agent] = now
                            triggered.add(agent)
                            # Checkpoint state IMMEDIATELY after fire so an OOM kill
                            # of the parent cgroup (or any subsequent crash) cannot
                            # replay this same eid on the next cycle.
                            _persist()
                            log.info("state checkpointed after fire: agent=%s eid=%s", agent, eid[:40])
                        except Exception as e:
                            log.error("Launch failed for %s: %s", agent, e)
                    except Exception:
                        # One malformed entry must never kill the whole scan.
                        log.exception("entry processing failed [%s] — skipping", src)
                        continue
    finally:
        # Defensive final persist — covers early exits, exceptions in the fetch
        # loop, and any code path that didn't hit a per-fire checkpoint.
        _persist()
        log.info("Scanned %d feeds — triggered: %s", len(WATCH_FEEDS), triggered or "none")


if __name__ == "__main__":
    run()
