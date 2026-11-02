"""Map Polymarket Gamma platform tags and market slugs into coarse league buckets.

Analyzer consumers use this to compare live positions against a wallet's in-sample market domains.
Tag-derived labels are preferred; slug keywords are fallback-only for scans that do not fetch tags.

ANTI-HARDCODING (operator directive): the only DURABLE hardcoded tokens are Polymarket's stable
top-level SPORTS parents; the per-league mapping is data (a dict, easy to extend), and — crucially
— any sports market whose specific tag isn't in the map AUTO-MAPS to that tag itself, so a brand-
new Polymarket league (e.g. Cricket/F1) is recognized and named WITHOUT a code change. A renamed
league tag therefore degrades to its own name (kept), never to a wrong bucket or a silent drop."""
from __future__ import annotations

# Stable top-level Polymarket sports parents (the one thing we accept as durable). A market is
# sports iff it carries one of these. If Polymarket ever renames them, validate_sports_parents()
# surfaces it loudly rather than silently classifying everything as non-sports.
SPORTS_PARENTS = ("Sports", "Games")

# Coarse-bucket aliases: league -> the set of specific Gamma tags that should collapse into it.
# Anything NOT listed here falls through to the dynamic path (the tag names itself), so this map
# only needs entries where we want to MERGE several tags (e.g. NHL+Hockey) — not an exhaustive
# enumeration of every league Polymarket will ever have.
_LEAGUE_ALIASES = {
    "NHL": {"NHL", "Hockey"},
    "MLB": {"MLB", "baseball", "World Series"},
    "CBB": {"NCAA Basketball", "March Madness"},
    "CFB": {"CFB", "NCAA"},
    "Soccer": {"Soccer", "EPL", "Premier League", "UCL", "La Liga", "Serie A", "Bundesliga", "World Cup", "FIFA"},
    "MMA/Box": {"UFC", "Boxing", "MMA"},
    "Esports": {"Esports", "CS2", "Dota", "League of Legends", "Valorant"},
}
# tags that are categories/qualifiers, never a "league" on their own (skipped in the dynamic path)
_GENERIC_TAGS = {*SPORTS_PARENTS, "All", "Recurring", "Daily", "Weekly", "Playoffs", "Finals",
                 "Championship", "Tournament", "Live", "Featured", "Trending"}


def league_from_tags(tags) -> str | None:
    """Coarse league for a market's tag list. None when not sports (no Sports/Games parent).
    Known multi-tag leagues collapse via _LEAGUE_ALIASES; an UNRECOGNIZED sports league maps to
    its own most-specific tag (auto-adapts to new Polymarket leagues); 'Other-sport' only when
    sports with no usable specific tag."""
    s = set(tags or [])
    if not (set(SPORTS_PARENTS) & s):
        return None
    for league, alias in _LEAGUE_ALIASES.items():
        if alias & s:
            return league
    # dynamic fallback: use the wallet's own specific tag(s) as the league — new leagues included
    specific = sorted(t for t in s if t not in _GENERIC_TAGS)
    return specific[0] if specific else "Other-sport"


def validate_sports_parents(client=None, *, max_scan: int = 200) -> dict:
    """Loud check that our one durable assumption — the SPORTS_PARENTS tag names — still exists on
    live Polymarket. Scans open events; returns {parent: count, '_total_events': n, '_ok': bool}.
    `_ok` is False if NONE of SPORTS_PARENTS appear across the scan (i.e. Polymarket renamed them
    and league_from_tags would now classify everything non-sports). Call this from a healthcheck;
    do NOT gate runtime on it (network), but surface a WARN when `_ok` is False."""
    own = client is None
    if own:
        import httpx
        client = httpx.Client(headers={"User-Agent": "sniper-season/1"})
    try:
        hits = {p: 0 for p in SPORTS_PARENTS}
        total = 0
        for off in range(0, max_scan, 100):
            try:
                r = client.get(GAMMA_EVENTS_URL,
                               params={"closed": "false", "active": "true", "limit": 100,
                                       "offset": off, "order": "volume24hr", "ascending": "false"},
                               timeout=25)
                evs = r.json()
            except Exception:  # noqa: BLE001
                break
            if not evs:
                break
            for ev in evs:
                total += 1
                labels = {(t.get("label") or t.get("slug")) for t in (ev.get("tags") or [])}
                for p in SPORTS_PARENTS:
                    if p in labels:
                        hits[p] += 1
        hits["_total_events"] = total
        hits["_ok"] = any(hits[p] > 0 for p in SPORTS_PARENTS)
        return hits
    finally:
        if own:
            client.close()


def build_slug_league(tags_by_slug: dict) -> dict:
    """event_slug -> league for every slug with a recognized sports league (drops non-sports)."""
    out = {}
    for slug, tags in (tags_by_slug or {}).items():
        lg = league_from_tags(tags)
        if lg is not None:
            out[slug] = lg
    return out


def is_nonsports_tagged(tags) -> bool:
    """True only when we HAVE tags and they classify the market as NON-sports (no Sports/Games
    tag). Distinguishes 'confirmed non-sports' (drop it — e.g. a Politics 'Trump vs Biden' market
    that the coarse keyword gate matched on ' vs ') from 'untagged/unknown' (keep — don't
    over-filter what we can't classify). Empty/None tags -> False (unknown, not confirmed)."""
    return bool(tags) and not ({"Sports", "Games"} & set(tags))


def nonsports_slugs(tags_by_slug: dict) -> set:
    """Set of event_slugs that are CONFIRMED non-sports by their tags (for the capture drop)."""
    return {slug for slug, tags in (tags_by_slug or {}).items() if is_nonsports_tagged(tags)}


GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

# slug-keyword league map — FALLBACK ONLY (used if the tag-based detection fails). The primary
# path derives active leagues from Polymarket's own market TAGS at runtime (see active_leagues),
# so new sports/leagues are picked up with zero code change. Sport-specific tokens only — never
# generic "-vs-"/" vs " (matches ufc/tennis/elections). Same coarse names as league_from_tags.
# Coarse-league NAMES here MUST match what league_from_tags produces (the canonical tag taxonomy),
# because the rising screen compares slug-classified positions against the tag-derived active_leagues
# set — a name mismatch silently scores in-season bettors as out-of-season. WNBA is listed BEFORE
# Basketball because "wnba" contains the "nba" substring (else WNBA mis-reads as Basketball). NBA's
# canonical tag name is "Basketball" (tags rarely carry a distinct NBA tag); college stays "CBB".
_ACTIVE_KW = {
    "WNBA": ("wnba",),
    "Basketball": ("nba", "-nba-", "basketball"),
    "NHL": ("nhl", "stanley-cup"), "MLB": ("mlb", "world-series", "baseball"),
    "Soccer": ("world-cup", "fifa", "soccer", "mls", "copa", "epl", "uefa",
               "premier-league", "la-liga", "serie-a", "bundesliga", "champions-league"),
    "Tennis": ("tennis", "wimbledon", "-atp-", "-wta-", "roland-garros"),  # "us-open" is ambiguous

    "MMA/Box": ("ufc", "-mma-", "boxing"), "CFB": ("cfb", "college-football"),
    "CBB": ("cbb", "ncaab", "march-madness"), "Esports": ("cs2", "dota", "esports", "league-of-legends"),
    "Cricket": ("cricket", "-ipl-", "-t20-", "test-match"),
    "Golf": ("golf", "-pga-", "pga-", "lpga", "ryder-cup"),
}


def slug_keywords(league: str) -> tuple:
    """Slug/title keyword tokens for a coarse league — the FALLBACK taxonomy shared by discovery
    scripts so sport keywords live in ONE place (was copy-pasted into discover_soccer_wallets.py
    etc., which drifted). Empty tuple for an unknown league. The PRIMARY taxonomy is tag-derived
    (league_from_tags); this is only for slug/title scans where tags aren't fetched."""
    return _ACTIVE_KW.get(league, ())


# Long-dated OUTRIGHT / futures markets (tournament winner, top-scorer, relegation, MVP...) carry a
# sport keyword but are not near-term per-game markets. They can make an activity screen look
# in-season when it is really futures-only. Keys on tournament-winner tokens, NOT the bare word
# "win" (a per-game "will X win" market also uses it). Single source for the screen + discovery scan
# (was duplicated). Question-phrased outrights ("will-spain-win-the-world-cup") are left to the
# endDate/season filter, not keyword detection, to avoid false-positiving per-game markets.
# NOTE: NO bare "champion" token — it's a substring of "champions(-league)", so it would drop per-game
# UCL matches ("champions-league-man-city-real-madrid"). The genuine winner-futures case
# ("champions-league-winner") is already caught by "winner", so "champion" is redundant + dangerous.
_OUTRIGHT_KW = ("winner", "to-win", "to win", "outright", "mvp", "top-scorer",
                "top scorer", "golden-boot", "golden boot", "relegat", "promotion",
                "group-winner", "to-lift", "to-reach", "to-qualify")


def is_outright(blob: str) -> bool:
    """True if a market slug/title looks like a long-dated outright/futures market (see above)."""
    b = (blob or "").lower()
    return any(k in b for k in _OUTRIGHT_KW)


def league_from_slug(blob: str) -> str | None:
    """Coarse league from a slug/title blob via the fallback keyword map — for scans where Gamma
    tags aren't fetched (discovery/screen). First matching coarse league, else None. Single source
    so callers don't reach into the private _ACTIVE_KW. Prefer league_from_tags when tags exist."""
    b = (blob or "").lower()
    for name, kw in _ACTIVE_KW.items():
        if any(k in b for k in kw):
            return name
    return None


def canonical_league_label(blob: str | None) -> str | None:
    """Normalize a displayed league/domain string onto the shared coarse taxonomy when possible."""
    s = (blob or "").strip()
    if not s:
        return None
    low = s.lower()
    for league, aliases in _LEAGUE_ALIASES.items():
        if low == league.lower() or any(low == str(a).lower() for a in aliases):
            return league
    for league in _ACTIVE_KW:
        if low == league.lower():
            return league
    return league_from_slug(s) or low


def league_matches(value: str | None, domains) -> bool:
    """True when a live position's league is in one of the analyzer's edge domains."""
    raw = (value or "").strip().lower()
    canon = canonical_league_label(value)
    if not raw or not canon:
        return False
    for d in domains or []:
        d_raw = str(d or "").strip().lower()
        d_canon = canonical_league_label(str(d or ""))
        if not d_raw:
            continue
        if d_canon == canon:
            return True
        if d_raw in raw or raw in d_raw:
            return True
    return False


def _days_until(end: str, now_ts: float) -> float | None:
    """Days from now until an ISO endDate (None if unparseable)."""
    if not end:
        return None
    try:
        import datetime as _dt
        return (_dt.datetime.fromisoformat(str(end).replace("Z", "+00:00")).timestamp() - now_ts) / 86400.0
    except Exception:  # noqa: BLE001
        return None


def active_leagues(client=None, *, min_markets: int = 1, max_scan: int = 300,
                   season_days: float = 21.0, now_ts: float | None = None) -> set:
    """Leagues actually IN SEASON right now — derived from Polymarket's own market TAGS, NOT a
    hardcoded keyword/calendar list, so new sports/leagues are detected automatically (a new
    sport's open markets carry its tag -> active; unrecognized sports fall to 'Other-sport', which
    the season gate KEEPS). "In season" = the league has open markets CLOSING within season_days
    (games/near-term events) — this excludes long-dated FUTURES (e.g. an NBA 'champion' market
    open year-round) using each market's endDate, again no hardcoded calendar. Used as a runtime
    season gate (objective external fact -> doesn't break OOS). Falls back to the slug-keyword
    scan only if the tag fetch yields nothing (network/shape change). now_ts injectable for tests."""
    import time as _time
    now = now_ts if now_ts is not None else _time.time()
    own = client is None
    if own:
        import httpx
        client = httpx.Client(headers={"User-Agent": "sniper-season/1"})
    try:
        cnt: dict[str, int] = {}
        for off in range(0, max_scan, 100):
            try:
                r = client.get(GAMMA_EVENTS_URL,
                               params={"closed": "false", "active": "true", "limit": 100,
                                       "offset": off, "order": "volume24hr", "ascending": "false"},
                               timeout=25)
                evs = r.json()
            except Exception:  # noqa: BLE001
                break
            if not evs:
                break
            for ev in evs:
                days = _days_until(ev.get("endDate") or ev.get("end_date"), now)
                if days is not None and days > season_days:    # long-dated future -> not "in season"
                    continue
                tags = [t.get("label") or t.get("slug")
                        for t in (ev.get("tags") or []) if (t.get("label") or t.get("slug"))]
                lg = league_from_tags(tags)
                if lg:
                    cnt[lg] = cnt.get(lg, 0) + 1
        active = {n for n, c in cnt.items() if c >= min_markets}
        if active:
            return active
        return _active_leagues_by_slug(client, min_markets)   # fallback
    finally:
        if own:
            client.close()


def _active_leagues_by_slug(client, min_markets: int) -> set:
    """Fallback: detect active leagues by SLUG keyword over open markets (stale-prone; only used
    if the tag-based path returns nothing)."""
    cnt: dict[str, int] = {}
    for off in range(0, 1500, 500):
        try:
            r = client.get("https://gamma-api.polymarket.com/markets",
                           params={"closed": "false", "active": "true", "limit": 500,
                                   "offset": off, "order": "volume24hr", "ascending": "false"},
                           timeout=25)
            ms = r.json()
        except Exception:  # noqa: BLE001
            break
        if not ms:
            break
        for m in ms:
            blob = ((m.get("slug") or "") + " " + (m.get("question") or "")).lower()
            for name, kw in _ACTIVE_KW.items():
                if any(k in blob for k in kw):
                    cnt[name] = cnt.get(name, 0) + 1
                    break
    return {n for n, c in cnt.items() if c >= min_markets}
