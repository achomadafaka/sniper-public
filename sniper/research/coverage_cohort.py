"""Admission-independent research cohort: what the market did after EVERY observed buy.

Our tape only covers coins we already considered entering, so it skews large and liquid: of 9,477
priced Solana buys with a known mcap, 5,682 sat above $5M where the 60s move clears a +17% bar 0-1%
of the time, while genuine sub-$250k events with any tape numbered 20. Every conclusion about small
coins was drawn from a sample that could not contain them. This cohort exists to remove that
selection, not to trade.

It lives in its OWN database file (`data/research_cohort.sqlite`) so it can never contend with the
funded tracker, and it imports nothing from the entry or exit path.

## What it fixes, and what it refuses to claim

`memecoin_feed_observations_v2` already captures every observed event before funded filters and
records `was_curated` / `was_real_approved` rather than filtering on them. What it lacks is the
identity needed to interpret a row and the EXECUTABLE prices needed to value one.

Three claims this cohort is built to avoid repeating:

1. A mid-tape move is not profit. `memecoin_quote_observations_v2` and `memecoin_quote_ledger` hold
   display mids. A 60s mid move of +17% says nothing about what a buy and a later sell would have
   returned after route impact both ways. This cohort stores EXECUTABLE buy and sell quotes at
   fixed horizons, which is the only thing that can be turned into money.
2. Missing mcap is not small mcap. An earlier read treated a missing-mcap bucket as "tiny new
   coins" and reported a 28% hit rate from it. `mcap_missing_reason` is therefore mandatory and
   typed: a row records WHY the value is absent, and absence is never evidence of size.
3. An event count is not a sample size. The band comparison that motivated this work rested on 8
   and 25 distinct mints behind 1,854 and 3,828 events. Every row carries `episode_id` so
   inference clusters by mint and by episode instead of by row.

## Typed failures are results

A quote that fails is censored, not missing: `failure_class` keeps the reason (no route, thin
liquidity, rate limit, transport) and `censored=1` marks it. Dropping failed quotes would silently
select for coins that were routable, which is the same selection this cohort exists to remove — a
coin nobody can sell is the most important row in the table.

## Frozen arms

`control`, `ceiling_1m` and `ceiling_5m` are declared once with a content digest. They are
evaluated over the cohort, never applied to funded entry. A ceiling is promoted only if the forward
cohort shows executable net value, which is a decision for the operator and not for this module.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("data/research_cohort.sqlite")

# A new episode starts when a mint has been quiet this long. Buys inside one episode are one
# market event seen several times, not several independent observations.
EPISODE_GAP_S = 900.0

# Executable quotes are taken at these offsets from our observation of the buy.
HORIZONS_S = (0, 60, 300, 900)

# Why an mcap value is absent. Never leave this NULL when the value is missing: an untyped absence
# is what let a missing-data bucket be read as a small-coin finding.
MCAP_MISSING_REASONS = (
    "feed_omitted",        # the feed carried no marketCap field for this event
    "feed_zero",           # the feed sent 0, which is not a market cap
    "no_supply",           # supply unavailable, so it cannot be derived
    "no_price",            # price unavailable, so it cannot be derived
    "provider_error",      # a lookup was attempted and failed
    "not_attempted",       # enrichment has not run for this row yet
)

ARMS = {
    "control": {"description": "every observed buy, no ceiling", "mcap_ceiling_usd": None},
    "ceiling_1m": {"description": "refuse mcap above $1M", "mcap_ceiling_usd": 1_000_000.0},
    "ceiling_5m": {"description": "refuse mcap above $5M", "mcap_ceiling_usd": 5_000_000.0},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cohort_events (
    event_id            TEXT PRIMARY KEY,
    observed_at         TEXT NOT NULL,
    chain_id            TEXT NOT NULL,
    venue               TEXT,
    mint                TEXT NOT NULL,
    source_user_id      TEXT,
    source_handle       TEXT,
    side                TEXT NOT NULL,
    their_price         REAL,
    their_size_usd      REAL,
    mcap_value          REAL,
    mcap_source         TEXT,
    mcap_missing_reason TEXT,
    supply              REAL,
    supply_source       TEXT,
    decimals            INTEGER,
    pair_address        TEXT,
    pair_source         TEXT,
    episode_id          TEXT NOT NULL,
    episode_seq         INTEGER NOT NULL,
    was_curated         INTEGER,
    was_real_approved   INTEGER,
    captured_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cohort_events_mint ON cohort_events(chain_id, mint, observed_at);
CREATE INDEX IF NOT EXISTS idx_cohort_events_episode ON cohort_events(episode_id);
CREATE INDEX IF NOT EXISTS idx_cohort_events_observed ON cohort_events(observed_at);

CREATE TABLE IF NOT EXISTS cohort_quotes (
    id                INTEGER PRIMARY KEY,
    event_id          TEXT NOT NULL,
    horizon_s         INTEGER NOT NULL,
    side              TEXT NOT NULL,
    chain_id          TEXT NOT NULL,
    venue             TEXT,
    requested_at      TEXT NOT NULL,
    observed_at       TEXT,
    in_amount_atomic  TEXT,
    out_amount_atomic TEXT,
    in_decimals       INTEGER,
    out_decimals      INTEGER,
    unit_price_usd    REAL,
    route_target      TEXT,
    quote_id          TEXT,
    failure_class     TEXT,
    censored          INTEGER NOT NULL DEFAULT 0,
    latency_ms        REAL,
    UNIQUE(event_id, horizon_s, side)
);
CREATE INDEX IF NOT EXISTS idx_cohort_quotes_event ON cohort_quotes(event_id, horizon_s);

CREATE TABLE IF NOT EXISTS cohort_arms (
    arm_id          TEXT PRIMARY KEY,
    definition_json TEXT NOT NULL,
    frozen_at       TEXT NOT NULL,
    digest          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cohort_arm_decisions (
    event_id TEXT NOT NULL,
    arm_id   TEXT NOT NULL,
    admitted INTEGER NOT NULL,
    reason   TEXT NOT NULL,
    PRIMARY KEY (event_id, arm_id)
);

CREATE TABLE IF NOT EXISTS cohort_cursor (
    name       TEXT PRIMARY KEY,
    position   TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect(path=None):
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_tables(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _iso(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# --- identity -----------------------------------------------------------------------------------

def classify_mcap(feed_market_cap, *, supply=None, price=None):
    """(value, source, missing_reason). Absence is always typed.

    A derived value is marked `derived` rather than passed off as the feed's own, because the two
    disagree often enough that an analysis has to be able to split them.
    """
    try:
        value = float(feed_market_cap)
    except (TypeError, ValueError):
        value = None
    if value is not None and value > 0:
        return value, "feed", None
    if value == 0:
        reason = "feed_zero"
    else:
        reason = "feed_omitted"
    try:
        s, p = float(supply), float(price)
        if s > 0 and p > 0:
            return s * p, "derived", None
    except (TypeError, ValueError):
        pass
    if supply in (None, 0):
        return None, None, "no_supply"
    if price in (None, 0):
        return None, None, "no_price"
    return None, None, reason


def assign_episode(conn, chain_id, mint, observed_at, gap_s=EPISODE_GAP_S):
    """(episode_id, episode_seq) for one buy on one coin.

    A burst of buys on the same mint is ONE market event observed several times. Treating each row
    as independent is how 1,854 events on 8 mints came to be read as a large sample.
    """
    moment = _iso(observed_at)
    if moment is None:
        raise ValueError("observed_at must be an ISO timestamp")
    prior = conn.execute(
        "SELECT episode_id, episode_seq, observed_at FROM cohort_events "
        "WHERE chain_id=? AND mint=? AND observed_at<=? ORDER BY observed_at DESC LIMIT 1",
        (chain_id, mint, observed_at)).fetchone()
    if prior:
        last = _iso(prior["observed_at"])
        if last is not None and (moment - last).total_seconds() <= gap_s:
            return prior["episode_id"], int(prior["episode_seq"]) + 1
    return f"{chain_id}:{mint}:{moment.strftime('%Y%m%dT%H%M%S')}", 0


def record_event(conn, *, event_id, observed_at, chain_id, mint, side, venue=None,
                 source_user_id=None, source_handle=None, their_price=None, their_size_usd=None,
                 feed_market_cap=None, supply=None, supply_source=None, decimals=None,
                 pair_address=None, pair_source=None, was_curated=None, was_real_approved=None,
                 captured_at=None, commit=True):
    """Insert one observed buy. No admission filter is applied — that is the whole point."""
    mcap, mcap_source, missing = classify_mcap(feed_market_cap, supply=supply, price=their_price)
    episode_id, episode_seq = assign_episode(conn, chain_id, mint, observed_at)
    conn.execute(
        "INSERT OR IGNORE INTO cohort_events(event_id, observed_at, chain_id, venue, mint, "
        "source_user_id, source_handle, side, their_price, their_size_usd, mcap_value, "
        "mcap_source, mcap_missing_reason, supply, supply_source, decimals, pair_address, "
        "pair_source, episode_id, episode_seq, was_curated, was_real_approved, captured_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, observed_at, str(chain_id), venue, mint, source_user_id, source_handle, side,
         their_price, their_size_usd, mcap, mcap_source, missing, supply, supply_source, decimals,
         pair_address, pair_source, episode_id, episode_seq, was_curated, was_real_approved,
         captured_at or datetime.now(timezone.utc).isoformat()))
    if commit:
        conn.commit()
    return episode_id


# --- executable quotes --------------------------------------------------------------------------

def due_quotes(conn, now, horizons=HORIZONS_S, limit=200):
    """Events whose next fixed-horizon quote is due. Both sides, oldest first."""
    now_dt = _iso(now) or datetime.now(timezone.utc)
    out = []
    for row in conn.execute(
            "SELECT event_id, observed_at, chain_id, venue, mint FROM cohort_events "
            "WHERE observed_at >= ? ORDER BY observed_at LIMIT ?",
            ((now_dt - timedelta(seconds=max(horizons) + 3600)).isoformat(), limit * 8)):
        base = _iso(row["observed_at"])
        if base is None:
            continue
        for horizon in horizons:
            if base + timedelta(seconds=horizon) > now_dt:
                continue
            for side in ("buy", "sell"):
                have = conn.execute(
                    "SELECT 1 FROM cohort_quotes WHERE event_id=? AND horizon_s=? AND side=?",
                    (row["event_id"], horizon, side)).fetchone()
                if not have:
                    out.append({"event_id": row["event_id"], "horizon_s": horizon, "side": side,
                                "chain_id": row["chain_id"], "venue": row["venue"],
                                "mint": row["mint"],
                                "due_at": (base + timedelta(seconds=horizon)).isoformat()})
                    if len(out) >= limit:
                        return out
    return out


def record_quote(conn, *, event_id, horizon_s, side, chain_id, requested_at, venue=None,
                 observed_at=None, in_amount_atomic=None, out_amount_atomic=None,
                 in_decimals=None, out_decimals=None, unit_price_usd=None, route_target=None,
                 quote_id=None, failure_class=None, latency_ms=None, commit=True):
    """One executable quote, success or typed failure.

    A failure is stored with `censored=1`, never dropped. A coin nobody could sell is the most
    important row here, and discarding it would re-introduce exactly the routable-only selection
    this cohort exists to remove.
    """
    censored = 1 if failure_class else 0
    conn.execute(
        "INSERT OR REPLACE INTO cohort_quotes(event_id, horizon_s, side, chain_id, venue, "
        "requested_at, observed_at, in_amount_atomic, out_amount_atomic, in_decimals, "
        "out_decimals, unit_price_usd, route_target, quote_id, failure_class, censored, "
        "latency_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, int(horizon_s), side, str(chain_id), venue, requested_at, observed_at,
         None if in_amount_atomic is None else str(in_amount_atomic),
         None if out_amount_atomic is None else str(out_amount_atomic),
         in_decimals, out_decimals, unit_price_usd, route_target, quote_id,
         failure_class, censored, latency_ms))
    if commit:
        conn.commit()
    return censored


def executable_round_trip(conn, event_id, horizon_s):
    """Net percent from an executable BUY at t0 and an executable SELL at the horizon.

    Returns (net_pct, status). Status is `censored` when either leg failed, which is a result and
    not a gap. This is the only number in this module that can be compared to a profit bar; the
    mid-tape response cannot.
    """
    buy = conn.execute(
        "SELECT unit_price_usd, failure_class FROM cohort_quotes "
        "WHERE event_id=? AND horizon_s=0 AND side='buy'", (event_id,)).fetchone()
    sell = conn.execute(
        "SELECT unit_price_usd, failure_class FROM cohort_quotes "
        "WHERE event_id=? AND horizon_s=? AND side='sell'", (event_id, int(horizon_s))).fetchone()
    if buy is None or sell is None:
        return None, "incomplete"
    if buy["failure_class"] or sell["failure_class"]:
        return None, "censored"
    entry, exit_ = buy["unit_price_usd"], sell["unit_price_usd"]
    if not entry or not exit_ or entry <= 0:
        return None, "unpriceable"
    return (exit_ / entry - 1.0) * 100.0, "ok"


# --- frozen arms --------------------------------------------------------------------------------

def freeze_arms(conn, arms=None, now=None):
    """Declare the arms once, with a digest. Re-freezing a changed definition is refused."""
    arms = arms or ARMS
    stamp = now or datetime.now(timezone.utc).isoformat()
    for arm_id, definition in arms.items():
        body = json.dumps(definition, sort_keys=True)
        digest = hashlib.sha256(f"{arm_id}:{body}".encode()).hexdigest()
        existing = conn.execute(
            "SELECT digest FROM cohort_arms WHERE arm_id=?", (arm_id,)).fetchone()
        if existing and existing["digest"] != digest:
            raise ValueError(
                f"arm {arm_id} is already frozen with a different definition; "
                "changing it invalidates the cohort")
        conn.execute("INSERT OR IGNORE INTO cohort_arms(arm_id, definition_json, frozen_at, "
                     "digest) VALUES(?,?,?,?)", (arm_id, body, stamp, digest))
    conn.commit()


def arm_decision(definition, event):
    """(admitted, reason) for one event under one arm. Unknown mcap is ADMITTED and said so.

    A ceiling that refuses unknown mcap would silently become a data-availability filter, and the
    coins with no mcap are disproportionately the ones the ceiling is supposed to be about.
    """
    ceiling = definition.get("mcap_ceiling_usd")
    if ceiling is None:
        return True, "control"
    value = event.get("mcap_value")
    if value is None:
        return True, f"mcap_unknown:{event.get('mcap_missing_reason') or 'untyped'}"
    return (False, f"mcap_above_ceiling:{ceiling:.0f}") if value > ceiling \
        else (True, f"mcap_within_ceiling:{ceiling:.0f}")


def record_arm_decisions(conn, event, commit=True):
    for row in conn.execute("SELECT arm_id, definition_json FROM cohort_arms"):
        admitted, reason = arm_decision(json.loads(row["definition_json"]), event)
        conn.execute("INSERT OR REPLACE INTO cohort_arm_decisions(event_id, arm_id, admitted, "
                     "reason) VALUES(?,?,?,?)",
                     (event["event_id"], row["arm_id"], 1 if admitted else 0, reason))
    if commit:
        conn.commit()
