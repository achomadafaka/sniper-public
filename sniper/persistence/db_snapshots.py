"""
Snapshot table handlers for external data feeds.
Weather, commodities, news, odds, edge observations, Polymarket prices.
"""
import logging

from sniper.persistence.db_utils import _classify_news_domain, _classify_title_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Weather snapshots (used by feeds/weather.py and strategies/weather_v1.py)
# ---------------------------------------------------------------------------


def ensure_weather_snapshots_table(conn) -> None:
    """Create the ``weather_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag  TEXT,
            feed_id       TEXT NOT NULL DEFAULT 'weather',
            location_key  TEXT NOT NULL,
            forecast_ts   INTEGER NOT NULL,
            payload_json  TEXT NOT NULL DEFAULT '{}',
            captured_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_weather_snapshots_loc_ts
        ON weather_snapshots (location_key, forecast_ts DESC)
    """)
    conn.commit()


def insert_weather_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    feed_id: str = "weather",
    location_key: str,
    forecast_ts: int,
    payload_json: str = "{}",
) -> int:
    """Insert a weather snapshot row and return the new row id."""
    cur = conn.execute(
        """INSERT INTO weather_snapshots
           (instance_tag, feed_id, location_key, forecast_ts, payload_json)
           VALUES (?, ?, ?, ?, ?)""",
        (instance_tag, feed_id, location_key, forecast_ts, payload_json),
    )
    conn.commit()
    return cur.lastrowid


def recent_weather_snapshots(conn, location_key: str | None = None, hours: int = 24) -> list[dict]:
    """Return recent weather snapshots, newest first.

    If *location_key* is provided, filter to that location only.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if location_key:
        rows = conn.execute(
            """SELECT * FROM weather_snapshots
               WHERE location_key = ? AND captured_at >= ?
               ORDER BY forecast_ts DESC""",
            (location_key, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM weather_snapshots
               WHERE captured_at >= ?
               ORDER BY forecast_ts DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Commodity snapshots
# ---------------------------------------------------------------------------


def ensure_commodity_snapshots_table(conn) -> None:
    """Create the ``commodity_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS commodity_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag  TEXT,
            feed_id       TEXT NOT NULL DEFAULT 'commodities',
            provider      TEXT NOT NULL DEFAULT 'yfinance',
            symbol        TEXT NOT NULL,
            price_usd     REAL NOT NULL,
            volume        REAL,
            market_ts     TEXT NOT NULL,
            payload_json  TEXT NOT NULL DEFAULT '{}',
            captured_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_commodity_snapshots_uniq
        ON commodity_snapshots (symbol, market_ts, provider)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_commodity_snapshots_sym_ts
        ON commodity_snapshots (symbol, market_ts DESC)
    """)
    conn.commit()


def insert_commodity_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    symbol: str,
    price_usd: float,
    market_ts: str,
    provider: str = "yfinance",
    volume: float | None = None,
    payload_json: str = "{}",
) -> int | None:
    """Insert a commodity snapshot row.  Returns row id, or None if duplicate."""
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO commodity_snapshots
               (instance_tag, feed_id, provider, symbol, price_usd, volume, market_ts, payload_json)
               VALUES (?, 'commodities', ?, ?, ?, ?, ?, ?)""",
            (instance_tag, provider, symbol, price_usd, volume, market_ts, payload_json),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception("insert_commodity_snapshot failed")
        return None


def recent_commodity_snapshots(
    conn, symbol: str | None = None, hours: int = 24
) -> list[dict]:
    """Return recent commodity snapshots, newest first."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if symbol:
        rows = conn.execute(
            """SELECT * FROM commodity_snapshots
               WHERE symbol = ? AND captured_at >= ?
               ORDER BY captured_at DESC""",
            (symbol, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM commodity_snapshots
               WHERE captured_at >= ?
               ORDER BY captured_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# News snapshots
# ---------------------------------------------------------------------------


def ensure_news_snapshots_table(conn) -> None:
    """Create the ``news_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_snapshots (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag       TEXT,
            feed_id            TEXT NOT NULL DEFAULT 'news',
            source_name        TEXT NOT NULL,
            canonical_url      TEXT,
            article_hash       TEXT NOT NULL UNIQUE,
            title              TEXT NOT NULL,
            description        TEXT,
            published_at       TEXT,
            domain             TEXT,
            sentiment_keywords TEXT,
            payload_json       TEXT NOT NULL DEFAULT '{}',
            captured_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_snapshots_domain_pub
        ON news_snapshots (domain, published_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_news_snapshots_captured
        ON news_snapshots (captured_at DESC)
    """)
    conn.commit()


def insert_news_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    source_name: str,
    canonical_url: str | None = None,
    article_hash: str,
    title: str,
    description: str = "",
    published_at: str = "",
    domain: str | None = None,
    sentiment_keywords: str = "[]",
    payload_json: str = "{}",
) -> int | None:
    """Insert a news snapshot row.  Returns row id, or None if duplicate."""
    if domain is None:
        domain = _classify_news_domain(title)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO news_snapshots
               (instance_tag, feed_id, source_name, canonical_url, article_hash,
                title, description, published_at, domain, sentiment_keywords, payload_json)
               VALUES (?, 'news', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_tag, source_name, canonical_url, article_hash,
             title, description, published_at, domain, sentiment_keywords, payload_json),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception("insert_news_snapshot failed")
        return None


def recent_news_snapshots(
    conn, domain: str | None = None, hours: int = 24
) -> list[dict]:
    """Return recent news snapshots, newest first."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    if domain:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE domain = ? AND captured_at >= ?
               ORDER BY published_at DESC""",
            (domain, cutoff),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE captured_at >= ?
               ORDER BY published_at DESC""",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Context query helpers (read-only)
# ---------------------------------------------------------------------------


def find_affected_markets(
    conn, keywords: list[str], domain: str | None = None, min_volume_24h: float = 0.0
) -> list[dict]:
    """Find open markets whose titles match any of the given keywords.

    Fetches up to 500 open markets ordered by volume, then filters Python-side.
    Same approach as weather_v1.discover().
    """
    rows = conn.execute(
        """SELECT condition_id, title, end_date_iso, category, volume_24h
           FROM market_metadata
           WHERE is_open = 1
           ORDER BY volume_24h DESC
           LIMIT 500"""
    ).fetchall()

    results = []
    for row in rows:
        title = str(row["title"] or "")
        vol = float(row["volume_24h"] or 0)
        if vol < min_volume_24h:
            continue
        market_domain = _classify_title_domain(title)
        if domain and market_domain.lower() != str(domain).lower():
            continue
        lower_title = title.lower()
        matched = [kw for kw in keywords if kw.lower() in lower_title]
        if not matched:
            continue
        results.append({
            "condition_id": str(row["condition_id"] or ""),
            "title": title,
            "category": str(row["category"] or ""),
            "domain": market_domain,
            "end_date_iso": str(row["end_date_iso"] or ""),
            "volume_24h": vol,
            "matched_keywords": matched,
        })
    return results


def commodity_price_history(
    conn, symbol: str, hours: int = 168
) -> list[dict]:
    """Return commodity price time series, oldest first.

    Filters by ``market_ts`` (provider timestamp), NOT ``captured_at``
    (our clock).  This ensures the time window reflects actual market
    data coverage, not when we happened to poll.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        """SELECT price_usd, market_ts, provider, captured_at
           FROM commodity_snapshots
           WHERE symbol = ? AND market_ts >= ?
           ORDER BY market_ts ASC""",
        (symbol, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def market_price_series(
    conn, condition_id: str, hours: int = 72
) -> list[dict]:
    """Assemble whatever price history is available for a market.

    Sources: signal_history, position_signals.
    Returns list of {price, timestamp, source} ordered by timestamp ASC.
    May return empty list for markets the system hasn't tracked.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = int((_dt.now(_tz.utc) - _td(hours=hours)).timestamp())
    prices: list[dict] = []

    # Source 1: signal_history (most granular — table may not exist on older DBs)
    try:
        rows = conn.execute(
            """SELECT current_price, analyzed_ts
               FROM signal_history
               WHERE condition_id = ? AND analyzed_ts >= ?
               ORDER BY analyzed_ts ASC""",
            (condition_id, cutoff),
        ).fetchall()
        for r in rows:
            p = float(r["current_price"] or 0)
            if p > 0:
                prices.append({"price": p, "timestamp": int(r["analyzed_ts"]), "source": "signal_history"})
    except Exception:
        import logging as _log
        _log.getLogger(__name__).debug("market_price_series: signal_history not available", exc_info=True)

    # Source 2: position_signals (current state — always exists)
    rows = conn.execute(
        """SELECT current_price, analyzed_ts
           FROM position_signals
           WHERE condition_id = ? AND analyzed_ts >= ?
           ORDER BY analyzed_ts ASC""",
        (condition_id, cutoff),
    ).fetchall()
    for r in rows:
        p = float(r["current_price"] or 0)
        if p > 0:
            prices.append({"price": p, "timestamp": int(r["analyzed_ts"]), "source": "position_signals"})

    # Deduplicate by timestamp and sort
    seen: set = set()
    unique = []
    for p in sorted(prices, key=lambda x: x["timestamp"]):
        if p["timestamp"] not in seen:
            seen.add(p["timestamp"])
            unique.append(p)

    return unique


def recent_news_for_market(
    conn, condition_id: str, hours: int = 24
) -> list[dict]:
    """Find recent news articles related to a market.

    Matches news domain/keywords against the market's title.
    Returns empty list if news_snapshots table doesn't exist.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # Get market title
    row = conn.execute(
        "SELECT title FROM market_metadata WHERE condition_id = ? LIMIT 1",
        (condition_id,),
    ).fetchone()
    if row is None:
        return []

    market_title = str(row["title"] or "").lower()
    if not market_title:
        return []

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")

    try:
        rows = conn.execute(
            """SELECT * FROM news_snapshots
               WHERE captured_at >= ?
               ORDER BY published_at DESC
               LIMIT 100""",
            (cutoff,),
        ).fetchall()
    except Exception:
        # news_snapshots table may not exist
        return []

    # Python-side relevance filter
    _stop_words = {"will", "the", "a", "an", "to", "in", "of", "by", "be", "is", "it", "at", "on", "for", "and", "or"}
    results = []
    for r in rows:
        news_title = str(r["title"] or "").lower()
        # Check for keyword overlap between market title and news title
        market_words = set(market_title.split()) - _stop_words
        news_words = set(news_title.split()) - _stop_words
        overlap = market_words & news_words
        if len(overlap) >= 2:
            results.append(dict(r))
    return results


def insider_convergence_check(
    conn, condition_id: str, min_insider_score: int = 55
) -> dict:
    """Check for high-insider-score wallets on a market.

    Returns dict with insider_count, total_insider_notional, avg_insider_score.
    Annotation only — not an entry gate.
    """
    try:
        rows = conn.execute(
            """SELECT position_insider_score, position_value
               FROM position_signals
               WHERE condition_id = ? AND is_open = 1
                 AND position_insider_score >= ?""",
            (condition_id, min_insider_score),
        ).fetchall()
    except Exception:
        rows = []

    if not rows:
        return {"insider_count": 0, "total_insider_notional": 0.0, "avg_insider_score": 0.0}

    scores = [float(r["position_insider_score"] or 0) for r in rows]
    notionals = [float(r["position_value"] or 0) for r in rows]
    return {
        "insider_count": len(rows),
        "total_insider_notional": sum(notionals),
        "avg_insider_score": sum(scores) / len(scores) if scores else 0.0,
    }


# ---------------------------------------------------------------------------
# Odds snapshots (sports odds feed)
# ---------------------------------------------------------------------------

def ensure_odds_snapshots_table(conn) -> None:
    """Create the ``odds_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS odds_snapshots (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_tag         TEXT,
            provider             TEXT NOT NULL DEFAULT 'the_odds_api',
            sport_key            TEXT NOT NULL,
            sport_title          TEXT NOT NULL DEFAULT '',
            event_id             TEXT NOT NULL,
            home_team            TEXT NOT NULL,
            away_team            TEXT NOT NULL,
            commence_time        TEXT NOT NULL,
            bookmaker_key        TEXT NOT NULL,
            bookmaker_title      TEXT NOT NULL DEFAULT '',
            market_key           TEXT NOT NULL DEFAULT 'h2h',
            outcome_name         TEXT NOT NULL,
            price_decimal        REAL NOT NULL,
            implied_prob_raw     REAL NOT NULL DEFAULT 0.0,
            market_ts            TEXT NOT NULL,
            canonical_event_hash TEXT NOT NULL DEFAULT '',
            payload_json         TEXT NOT NULL DEFAULT '{}',
            captured_at          TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_odds_snapshots_uniq
        ON odds_snapshots (provider, event_id, bookmaker_key, market_key, outcome_name, market_ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_snapshots_sport_commence
        ON odds_snapshots (sport_key, commence_time)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_snapshots_event_hash
        ON odds_snapshots (canonical_event_hash, market_ts)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_odds_snapshots_captured_at
        ON odds_snapshots (captured_at DESC)
    """)
    conn.commit()


def insert_odds_snapshot(
    conn,
    *,
    instance_tag: str = "default",
    provider: str = "the_odds_api",
    sport_key: str,
    sport_title: str = "",
    event_id: str,
    home_team: str,
    away_team: str,
    commence_time: str,
    bookmaker_key: str,
    bookmaker_title: str = "",
    market_key: str = "h2h",
    outcome_name: str,
    price_decimal: float,
    implied_prob_raw: float = 0.0,
    market_ts: str,
    canonical_event_hash: str = "",
    payload_json: str = "{}",
) -> int | None:
    """Insert an odds snapshot row.  Returns rowid, or None on duplicate."""
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO odds_snapshots
               (instance_tag, provider, sport_key, sport_title, event_id,
                home_team, away_team, commence_time, bookmaker_key, bookmaker_title,
                market_key, outcome_name, price_decimal, implied_prob_raw,
                market_ts, canonical_event_hash, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (instance_tag, provider, sport_key, sport_title, event_id,
             home_team, away_team, commence_time, bookmaker_key, bookmaker_title,
             market_key, outcome_name, price_decimal, implied_prob_raw,
             market_ts, canonical_event_hash, payload_json),
        )
        conn.commit()
        return cur.lastrowid if cur.rowcount > 0 else None
    except Exception:
        import logging
        logging.getLogger(__name__).exception("insert_odds_snapshot failed")
        return None


def recent_odds_events(
    conn, sport_keys: list[str], hours_ahead: int = 72
) -> list[dict]:
    """Return the latest snapshot per event for upcoming events.

    Filters to events whose ``commence_time`` is within *hours_ahead* of now
    and whose ``sport_key`` is in *sport_keys*.  Returns only the most recent
    snapshot per (event_id, bookmaker_key, outcome_name) combo, ordered by
    commence_time ASC.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    if not sport_keys:
        return []

    now_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_iso = (_dt.now(_tz.utc) + _td(hours=hours_ahead)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    placeholders = ",".join("?" for _ in sport_keys)
    rows = conn.execute(
        f"""SELECT *
            FROM odds_snapshots
            WHERE sport_key IN ({placeholders})
              AND commence_time >= ?
              AND commence_time <= ?
            ORDER BY market_ts DESC""",
        (*sport_keys, now_iso, cutoff_iso),
    ).fetchall()

    # Keep only the latest snapshot per (event_id, bookmaker_key, outcome_name)
    seen: set[tuple] = set()
    results: list[dict] = []
    for r in rows:
        key = (r["event_id"], r["bookmaker_key"], r["outcome_name"])
        if key not in seen:
            seen.add(key)
            results.append(dict(r))

    return results


def odds_event_history(
    conn, canonical_event_hash: str, hours: int = 24
) -> list[dict]:
    """Return historical odds snapshots for an event, chronological (ASC).

    Uses canonical_event_hash for matching.  Returns snapshots from the
    last *hours* hours ordered by market_ts ASC.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    cutoff = (_dt.now(_tz.utc) - _td(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """SELECT * FROM odds_snapshots
           WHERE canonical_event_hash = ?
             AND market_ts >= ?
           ORDER BY market_ts ASC""",
        (canonical_event_hash, cutoff),
    ).fetchall()

    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Polymarket h2h matching helpers (sports context)
# ---------------------------------------------------------------------------

_H2H_SKIP_PATTERNS: tuple[str, ...] = (
    "spread",
    "over/under",
    "over under",
    "o/u ",
    "total",
    "team total",
    "series winner",
    "mvp",
    "prop",
    "first to",
    "halftime",
    "half time",
    "quarter",
    "period",
)


def find_matching_polymarket_h2h_markets(
    conn,
    home_team: str,
    away_team: str,
    commence_time: str,
    tolerance_minutes: int = 180,
) -> list[dict]:
    """Find Polymarket binary winner markets matching an odds event.

    Uses short team names (last word of each team name) for broader SQL LIKE
    matching, then filters out non-winner markets via ``_H2H_SKIP_PATTERNS``.
    Only returns open markets whose ``end_date_iso`` falls within
    *tolerance_minutes* of *commence_time*.

    Returns a list of dicts with at least ``condition_id`` and ``title``.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td

    # Extract short team names (last word) for broader matching
    home_short = home_team.strip().split()[-1]
    away_short = away_team.strip().split()[-1]

    # Time window — use date-only comparison because Polymarket's end_date_iso
    # can be date-only ("2026-03-13") or full ISO ("2026-03-13T20:00:00Z").
    # Comparing date-only against full ISO strings fails in SQLite string sort.
    try:
        ct = _dt.fromisoformat(commence_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return []
    delta = _td(minutes=tolerance_minutes)
    d_lo = (ct - delta).strftime("%Y-%m-%d")
    d_hi = (ct + delta).strftime("%Y-%m-%d")

    # SQL: both short names must appear in the title, market must be open,
    # and end_date (truncated to date) must fall within tolerance window.
    rows = conn.execute(
        """SELECT condition_id, title, end_date_iso, volume_24h
           FROM market_metadata
           WHERE is_open = 1
             AND LOWER(title) LIKE '%' || LOWER(?) || '%'
             AND LOWER(title) LIKE '%' || LOWER(?) || '%'
             AND SUBSTR(end_date_iso, 1, 10) >= ?
             AND SUBSTR(end_date_iso, 1, 10) <= ?""",
        (home_short, away_short, d_lo, d_hi),
    ).fetchall()

    results: list[dict] = []
    for r in rows:
        title_lower = r["title"].lower()
        # Skip non-winner market types
        skip = False
        for pat in _H2H_SKIP_PATTERNS:
            if pat in title_lower:
                skip = True
                break
        if skip:
            continue
        results.append(dict(r))

    return results


def _fetch_clob_outcomes(condition_id: str) -> list[dict]:
    """Fetch outcome labels and live prices from the Polymarket CLOB API.

    Hits ``/markets/{condition_id}`` and extracts ``outcome`` and ``price``
    from each token object.  Returns list of dicts with ``outcome`` (str)
    and ``price`` (float, 0.0 if unavailable), or ``[]`` on failure.
    Used as a last-resort fallback when no DB sources have outcome data
    (e.g. sports game markets with no tracked wallets).
    """
    import requests as _req

    try:
        r = _req.get(
            f"https://clob.polymarket.com/markets/{condition_id}",
            timeout=8,
        )
        if r.ok:
            tokens = r.json().get("tokens", [])
            results = []
            for tok in tokens:
                label = str(tok.get("outcome", "")).strip()
                if label:
                    price = 0.0
                    try:
                        price = max(0.0, min(1.0, float(tok.get("price", 0))))
                    except (TypeError, ValueError):
                        pass
                    results.append({"outcome": label, "price": price})
            return results
    except Exception:
        import logging as _log
        _log.debug("_fetch_clob_outcomes failed for %s", condition_id, exc_info=True)
    return []


# NOTE: polymarket_market_outcomes lives in db.py (not here) because tests
# patch db._fetch_clob_outcomes and the call must resolve in db's namespace.


# ---------------------------------------------------------------------------
# Sports edge observations (persistent logging for threshold tuning)
# ---------------------------------------------------------------------------

def ensure_edge_observations_table(conn) -> None:
    """Create the edge_observations table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS edge_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            instance_tag TEXT NOT NULL DEFAULT '',
            sport_key TEXT NOT NULL,
            event_hash TEXT NOT NULL,
            condition_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            team TEXT NOT NULL,
            outcome_label TEXT NOT NULL DEFAULT '',
            poly_price REAL NOT NULL,
            fair_prob REAL NOT NULL,
            edge_pts REAL NOT NULL,
            dispersion REAL NOT NULL DEFAULT 0.0,
            books_used INTEGER NOT NULL DEFAULT 0,
            commence_time TEXT NOT NULL DEFAULT '',
            met_threshold INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_edge_obs_event
        ON edge_observations (event_hash, team)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_edge_obs_ts
        ON edge_observations (observed_at DESC)
    """)
    conn.commit()


def insert_edge_observation(
    conn,
    *,
    instance_tag: str,
    sport_key: str,
    event_hash: str,
    condition_id: str,
    title: str,
    team: str,
    outcome_label: str,
    poly_price: float,
    fair_prob: float,
    edge_pts: float,
    dispersion: float,
    books_used: int,
    commence_time: str,
    met_threshold: bool,
) -> None:
    """Insert an edge observation row."""
    conn.execute(
        """INSERT INTO edge_observations
           (instance_tag, sport_key, event_hash, condition_id, title,
            team, outcome_label, poly_price, fair_prob, edge_pts,
            dispersion, books_used, commence_time, met_threshold)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_tag, sport_key, event_hash, condition_id, title,
            team, outcome_label, poly_price, fair_prob, edge_pts,
            dispersion, books_used, commence_time, int(met_threshold),
        ),
    )
    conn.commit()


# ── Polymarket price snapshots ───────────────────────────────────────────


def ensure_poly_price_snapshots_table(conn) -> None:
    """Create table for periodic Polymarket CLOB price captures."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poly_price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            condition_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            outcome TEXT NOT NULL,
            price REAL NOT NULL,
            market_type TEXT NOT NULL DEFAULT '',
            sport_key TEXT NOT NULL DEFAULT '',
            event_hash TEXT NOT NULL DEFAULT '',
            commence_time TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_poly_snap_cid_ts
        ON poly_price_snapshots (condition_id, captured_at DESC)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_poly_snap_ts
        ON poly_price_snapshots (captured_at DESC)
    """)
    conn.commit()


def insert_poly_price_snapshot(
    conn,
    *,
    condition_id: str,
    title: str,
    outcome: str,
    price: float,
    market_type: str = "",
    sport_key: str = "",
    event_hash: str = "",
    commence_time: str = "",
) -> None:
    """Insert a Polymarket CLOB price snapshot."""
    conn.execute(
        """INSERT INTO poly_price_snapshots
           (condition_id, title, outcome, price, market_type,
            sport_key, event_hash, commence_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (condition_id, title, outcome, price, market_type,
         sport_key, event_hash, commence_time),
    )


def poly_price_history(
    conn,
    condition_id: str,
    outcome: str = "",
    limit: int = 200,
) -> list[dict]:
    """Return price history for a Polymarket market."""
    if outcome:
        rows = conn.execute(
            "SELECT * FROM poly_price_snapshots "
            "WHERE condition_id = ? AND LOWER(outcome) = LOWER(?) "
            "ORDER BY captured_at ASC LIMIT ?",
            (condition_id, outcome, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM poly_price_snapshots "
            "WHERE condition_id = ? "
            "ORDER BY captured_at ASC LIMIT ?",
            (condition_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Wallet entry edge analysis ───────────────────────────────────────────


def wallet_sports_entry_edge(
    conn,
    wallet: str = "",
    *,
    limit: int = 200,
) -> list[dict]:
    """Cross-reference wallet sports entries against sportsbook odds.

    For each BUY in followed_wallet_activity on a sports market, finds the
    closest-in-time odds_snapshot for the same event and computes the edge
    the wallet had at entry relative to sportsbook fair value.

    Returns list of dicts with entry details + book_fair_prob + edge_at_entry.
    """
    where = "WHERE fwa.side = 'BUY'"
    params: list = []
    if wallet:
        where += " AND fwa.wallet LIKE ?"
        params.append(wallet + "%")

    rows = conn.execute(f"""
        SELECT fwa.wallet, fwa.condition_id, fwa.title, fwa.outcome,
               fwa.price AS entry_price, fwa.usdc_size, fwa.activity_ts,
               mm.title AS market_title
        FROM followed_wallet_activity fwa
        LEFT JOIN market_metadata mm ON mm.condition_id = fwa.condition_id
        {where}
        AND (LOWER(fwa.title) LIKE '%o/u %'
             OR LOWER(fwa.title) LIKE '%spread%'
             OR LOWER(fwa.title) LIKE '% vs. %')
        ORDER BY fwa.activity_ts DESC
        LIMIT ?
    """, params + [limit]).fetchall()

    results = []
    for r in rows:
        entry_price = float(r["entry_price"] or 0)
        if entry_price <= 0:
            continue

        entry = dict(r)

        # Find matching odds_snapshot by title keyword matching
        # Extract team names from the title for fuzzy matching
        title = r["title"] or ""
        outcome = r["outcome"] or ""

        # Try to find book odds at entry time
        # Match by looking for the same event hash in odds_snapshots
        # that was captured closest to the activity_ts
        book_row = conn.execute("""
            SELECT os.implied_prob_raw, os.bookmaker_key, os.market_key,
                   os.outcome_name, os.captured_at, os.canonical_event_hash
            FROM odds_snapshots os
            JOIN market_metadata mm ON mm.condition_id = ?
            WHERE os.canonical_event_hash IN (
                SELECT DISTINCT eo.event_hash FROM edge_observations eo
                WHERE eo.condition_id = ?
            )
            AND LOWER(os.outcome_name) LIKE LOWER(?)
            ORDER BY ABS(
                CAST(strftime('%s', os.captured_at) AS INTEGER) -
                CAST(strftime('%s', ?) AS INTEGER)
            ) ASC
            LIMIT 1
        """, (r["condition_id"], r["condition_id"],
              "%" + outcome[:10] + "%" if outcome else "%",
              r["activity_ts"] or "")).fetchone()

        if book_row:
            book_prob = float(book_row["implied_prob_raw"] or 0)
            # Remove vig approximation (raw prob is over-round)
            fair_prob = book_prob / 1.05  # rough vig removal
            entry["book_raw_prob"] = book_prob
            entry["book_fair_prob"] = round(fair_prob, 4)
            entry["edge_at_entry"] = round(fair_prob - entry_price, 4)
            entry["book_source"] = book_row["bookmaker_key"]
            entry["book_captured_at"] = book_row["captured_at"]
        else:
            entry["book_raw_prob"] = None
            entry["book_fair_prob"] = None
            entry["edge_at_entry"] = None
            entry["book_source"] = None
            entry["book_captured_at"] = None

        results.append(entry)

    return results


# ---------------------------------------------------------------------------
# Hyperliquid snapshots (used by feeds/hyperliquid_feed.py)
# ---------------------------------------------------------------------------


def ensure_hyperliquid_snapshots_table(conn) -> None:
    """Create the ``hyperliquid_snapshots`` table if it does not exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hyperliquid_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            asset         TEXT NOT NULL,
            mark_price    REAL NOT NULL,
            funding_rate  REAL NOT NULL,
            open_interest REAL NOT NULL,
            volume_24h    REAL NOT NULL,
            captured_at   TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(asset, captured_at)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_hl_asset_ts
        ON hyperliquid_snapshots(asset, captured_at DESC)
    """)
    conn.commit()


def insert_hyperliquid_snapshot(
    conn,
    *,
    asset: str,
    mark_price: float,
    funding_rate: float,
    open_interest: float,
    volume_24h: float,
) -> int | None:
    """Insert a single Hyperliquid snapshot row.

    Returns the rowid on success, or ``None`` if the row already exists
    (unique constraint on asset + captured_at).
    """
    try:
        cur = conn.execute(
            """INSERT INTO hyperliquid_snapshots
               (asset, mark_price, funding_rate, open_interest, volume_24h)
               VALUES (?, ?, ?, ?, ?)""",
            (asset, mark_price, funding_rate, open_interest, volume_24h),
        )
        conn.commit()
        return cur.lastrowid
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            return None
        raise


def recent_hyperliquid_snapshots(
    conn,
    *,
    asset: str | None = None,
    hours: int = 24,
    limit: int = 500,
) -> list[dict]:
    """Return recent Hyperliquid snapshots, optionally filtered by asset.

    Rows are ordered by captured_at DESC.
    """
    sql = """
        SELECT id, asset, mark_price, funding_rate, open_interest, volume_24h, captured_at
        FROM hyperliquid_snapshots
        WHERE captured_at >= datetime('now', ? || ' hours')
    """
    params: list = [str(-hours)]
    if asset:
        sql += " AND asset = ?"
        params.append(asset)
    sql += " ORDER BY captured_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
