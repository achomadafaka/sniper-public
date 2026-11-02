"""market_semantics_enricher — resolution (r), platform tags, parent category, specific
tag, and series/slug-derived templates. No hardcoded entities; transient fetch failures
stay unknown (never cached as resolved=False / untagged)."""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Iterable, Mapping

from sniper.markets import polymarket_api as P
from sniper.analysis.winning_wallet_thesis.ledger import CACHE_DIR
from sniper.analysis.winning_wallet_thesis.types import (
    EnrichedLedger, RESOLUTION_WIN, WalletEventLedger,
)

# Stable parent rollup keyed on platform PARENT tags (order = priority; geopolitics
# before politics). No entity names — those are ephemeral platform sub-tags.
_PARENT_RULES = [
    ("sports",      {"sports", "esports"}),
    ("crypto",      {"crypto", "bitcoin", "ethereum", "solana"}),
    ("geopolitics", {"geopolitics"}),
    ("macro",       {"economy", "fed", "macro", "inflation", "economic policy", "rates"}),
    ("weather",     {"weather", "climate"}),
    ("culture",     {"pop culture", "mentions", "awards", "celebrities", "movies", "music",
                     "tv", "entertainment"}),
    ("politics",    {"politics", "elections", "world", "world elections", "global elections"}),
]
_GENERIC_TAGS = {"sports", "games", "world", "politics", "all", "main election", "weekly",
                 "recurring", "hide from new", "main"}


def parent_category(tags: Iterable[str]) -> str:
    low = {str(t).strip().lower() for t in tags if t}
    if not low:
        return "untagged"
    for name, parents in _PARENT_RULES:
        if low & parents:
            return name
    return "other-tagged"


def specific_tag(tags: list[str]) -> str:
    """Most-specific platform tag (durable, platform-owned) for finer slicing."""
    for t in tags:
        if str(t).strip().lower() not in _GENERIC_TAGS:
            return str(t).strip()
    return tags[0] if tags else "untagged"


# slug template = leading series tokens. Polymarket slugs are "<series>-<matchup>-<date>";
# strip trailing date and the vs-matchup so recurring market families collapse
# (series/slug-derived, never title regex). `_KNOWN_MULTI` keys on slug STRUCTURE
# (hyphenated series prefixes), not market subject — acceptable under spec §7.
_DATE_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_TRAIL_YEAR_RE = re.compile(r"-\d{4}$")
_KNOWN_MULTI = ("ufc-fight-night", "noche-ufc", "blast-open")


def event_template(event_slug: str) -> str:
    s = (event_slug or "").strip().lower()
    if not s:
        return ""
    for k in _KNOWN_MULTI:
        if s.startswith(k):
            return k
    s = _DATE_RE.sub("", s)
    s = _TRAIL_YEAR_RE.sub("", s)
    parts = s.split("-")
    if "vs" in parts:
        parts = parts[:parts.index("vs")]
    return parts[0] if parts else ""


def _json_list(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return []


def _to_epoch(s) -> int | None:
    """Parse a Gamma timestamp ('2026-05-30 15:52:32+00' | ISO 'Z') to epoch seconds."""
    if not s:
        return None
    from datetime import datetime
    t = str(s).strip().replace(" ", "T").replace("Z", "+00:00")
    if t.endswith("+00"):
        t += ":00"
    try:
        return int(datetime.fromisoformat(t).timestamp())
    except ValueError:
        return None


def _parse_resolution(m: Mapping) -> dict:
    status = (m.get("umaResolutionStatus") or "").lower()
    closed = bool(m.get("closed"))
    prices = _json_list(m.get("outcomePrices"))
    try:
        fp = [float(x) for x in prices]
    except (TypeError, ValueError):
        fp = []
    resolved = (status == "resolved" or closed) and bool(fp)
    winner = None
    push = False
    if fp:
        mx = max(fp)
        if mx >= RESOLUTION_WIN:
            winner = fp.index(mx)
        elif 0.4 <= mx <= 0.6 and len(fp) >= 2 and all(0.4 <= x <= 0.6 for x in fp):
            push = True
    # actual market close/resolution time — the real position close-time for risk timeline
    resolved_ts = _to_epoch(m.get("closedTime") or m.get("umaEndDate") or m.get("endDate"))
    return {"resolved": resolved, "winner": winner, "push": push, "prices": fp,
            "resolved_ts": resolved_ts}


def resolve_conditions(condition_ids: Iterable[str], *, wallet: str,
                       refresh: bool = False, batch_size: int = 40) -> dict[str, dict]:
    """condition_id -> {resolved, winner, push, prices}. Cached. A transient batch error
    leaves those ids unknown (retried next run); a successful response that simply omits
    a market marks it resolved=False (genuinely open)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_resolutions.json")
    cache: dict[str, dict] = {}
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            cache = json.load(f)
    wanted = {str(c).strip() for c in condition_ids if c}
    unknown = sorted(wanted - cache.keys())
    if unknown:
        s = P.build_session()
        for i in range(0, len(unknown), batch_size):
            batch = unknown[i:i + batch_size]
            try:
                data = P._request_json(s, f"{P.GAMMA_BASE_URL}/markets",
                                       params={"condition_ids": batch, "closed": "true",
                                               "limit": 500})
            except Exception as e:  # noqa: BLE001
                print(f"  resolve batch error: {type(e).__name__}", file=sys.stderr)
                continue
            got = set()
            for m in data or []:
                cid = str(m.get("conditionId") or "").strip()
                if cid not in wanted:
                    continue
                got.add(cid)
                cache[cid] = _parse_resolution(m)
            for cid in batch:
                if cid not in got:
                    cache.setdefault(cid, {"resolved": False})
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    return cache


def load_event_tags(event_slugs: Iterable[str], *, wallet: str, refresh: bool = False,
                    batch_size: int = 40) -> dict[str, list[str]]:
    """event_slug -> [platform tag labels] from Gamma /events (scalar-slug). Cached;
    transient errors leave the slug unknown (retried), a successful miss caches []."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_tags.json")
    cache: dict[str, list[str]] = {}
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            cache = json.load(f)
    wanted = {str(s).strip() for s in event_slugs if s}
    unknown = sorted(wanted - cache.keys())
    if unknown:
        s = P.build_session()
        for i in range(0, len(unknown), batch_size):
            for slug in unknown[i:i + batch_size]:
                try:
                    # No closed= filter: OPEN markets (live forward-harness bets) must return tags
                    # too — closed:true dropped them, caching [] so specialist league-filtering
                    # silently discarded valid open markets (review #HIGH 2026-06-17). Slug is
                    # unique, so this still matches closed (historical) events for the analyzer.
                    data = P._request_json(s, f"{P.GAMMA_BASE_URL}/events",
                                           params={"slug": slug, "limit": 500})
                except Exception as e:  # noqa: BLE001
                    print(f"  tags slug error: {slug} {type(e).__name__}", file=sys.stderr)
                    continue
                matched = next((ev for ev in (data or [])
                                if str(ev.get("slug") or "").strip() == slug), None)
                if matched is None:
                    cache.setdefault(slug, [])
                    continue
                cache[slug] = [t.get("label") or t.get("slug")
                               for t in (matched.get("tags") or [])
                               if (t.get("label") or t.get("slug"))]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f)
        os.replace(tmp, path)
    return cache


def enrich_ledger(ledger: WalletEventLedger, *, refresh: bool = False) -> EnrichedLedger:
    conds = {r.condition_id for r in ledger.rows if r.condition_id}
    slugs = {r.event_slug for r in ledger.rows if r.event_slug}
    resolutions = resolve_conditions(conds, wallet=ledger.wallet, refresh=refresh)
    tags = load_event_tags(slugs, wallet=ledger.wallet, refresh=refresh)
    templates = {s: event_template(s) for s in slugs}
    # negRisk conversions (no-op + no network when the wallet has none); the minted-YES
    # sub-markets need resolution too, so refresh those condition_ids before building.
    from sniper.analysis.winning_wallet_thesis.conversions import load_conversions
    conversions = load_conversions(ledger.raw_activity, wallet=ledger.wallet, refresh=refresh)
    if conversions:
        conv_conds = {leg.condition_id
                      for _ts, eff in conversions
                      for leg in (eff.yes_minted + eff.no_burned) if leg.condition_id}
        missing = conv_conds - resolutions.keys()
        if missing:
            resolutions.update(resolve_conditions(missing, wallet=ledger.wallet, refresh=refresh))
    return EnrichedLedger(ledger=ledger, resolutions=resolutions, tags=tags,
                          templates=templates, conversions=conversions)
