"""Signal copy-quote capture — MEASUREMENT ONLY, for one frozen wallet cohort.

Why this exists: no statistic we can compute today observes the price OUR OWN $50 order would
get. `their_price` carries the source wallet's market impact, `wouldbe_price` is populated on
0.9% of buys, and horizon marks are nominal quotes with no quantity, route or depth. So the
screens can reject a wallet but can never fund one. This captures the missing field.

For every distinct FIRST BUY by the frozen cohort it records:
  * one executable $50 entry quote taken immediately after the funded hook has decided, and
  * exit quotes for the EXACT expected token quantity every 5s through 90s, plus 120/300/900s.

The dense early schedule is the point. PUMP20_POST60 baselines at 60 seconds and is structurally
blind to the first minute, which is exactly where a fast exit operates. This measures it.

HARD CONSTRAINTS, all load-bearing:
  * It NEVER signs, submits, or touches a wallet key. Only `jupiter_entry_quote` and
    `_typed_jupiter_exit_quote` are imported — both are plain HTTP GETs. `jupiter_swap_tx` and
    the signing seams beside them must never be imported here.
  * It reads `sniper_tracker.db` through `get_readonly_connection()` only, so it can never pin
    the WAL or write to the funded ledger.
  * It writes to a SEPARATE measurement database. `memecoin_quote_ledger` cannot host this:
    its `(mint, ts_minute)` identity cannot represent an event-specific quantity quote.
  * It requires `real_decision_at IS NOT NULL` before quoting, so capture always trails the
    funded decision and can never race it.
  * The funded-hook delay is INSIDE the entry quote's latency. Every result therefore carries
    both `delay_source_to_quote_ms` and `delay_decision_to_quote_ms` so the hook's contribution
    is never invisible inside a headline number.

A quote is not a fill. Everything derived from this table is modelled, not realized.
"""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

# THE COMMON GRID. Every cohort member quotes these exact legs, always. Cross-wallet comparison
# happens on this grid and nowhere else — a per-wallet grid would make the sampling schedule a
# confounder, so a wallet could "win" by being measured more often near its own peak.
FAST_LEGS_S = tuple(range(5, 95, 5))          # 5,10,...,90
# The tail runs to SIX HOURS because that is where the label lives. A 300s ceiling made the
# outcome we actually care about — "did an exact sell of the acquired quantity ever reach 1.30x
# the executable entry cost" — structurally unresolvable: no dataset anywhere holds an exact
# whole-bag quote at a six-hour horizon, so every feature question inherited that gap and the
# study was NOT ESTIMABLE regardless of how many episodes accrued. Five extra legs per episode
# fix it. This is the cheapest change in the system by a wide margin.
COMMON_TAIL_S = (120, 300, 900, 1800, 3600, 21600)
EXIT_LEGS_S = FAST_LEGS_S + COMMON_TAIL_S

# Per-wallet tail extensions are now EMPTY and should stay that way. They existed to give two
# wallets a longer tail than the common grid, which made the sampling schedule differ by wallet —
# a confounder in any cross-wallet comparison. Now that the common grid itself reaches six hours
# there is nothing left to extend, and every episode is measured on an identical schedule.
TAIL_EXTENSIONS_S: dict[str, tuple] = {}
ENTRY_USD = 50.0
USDC = "USDC"

# --- safety envelope ------------------------------------------------------------------------
# This process is measurement, so every limit below fails toward LESS capture, never toward more.
# Raised with the six-hour tail: an episode now holds pending legs for 6h instead of 5 minutes,
# so at ~20 qualifying mints/chain/day the steady-state concurrency is ~5 and a burst can exceed
# the old cap of 12 without anything being wrong. The cap still exists to bound a runaway.
MAX_ACTIVE_EPISODES = 30          # concurrent episodes with unfinished legs
MAX_QUOTES_PER_DAY = 3000         # hard ceiling on provider calls in a rolling UTC day
MIN_QUOTE_INTERVAL_S = 0.2        # spacing between provider calls, so a burst cannot self-429


def legs_for(member_key):
    """Common grid + this wallet's tail extension. Deduplicated and sorted, so a mistaken
    extension that repeats a common leg cannot double-schedule it."""
    return tuple(sorted(set(EXIT_LEGS_S) | set(TAIL_EXTENSIONS_S.get(member_key, ()))))

SCHEMA = """
CREATE TABLE IF NOT EXISTS copy_quote_schedule (
    origin_event_id   TEXT NOT NULL,
    source_user_id    TEXT NOT NULL,
    network_id        TEXT NOT NULL,
    mint              TEXT NOT NULL,
    leg               TEXT NOT NULL,          -- 'entry' | 'exit'
    horizon_s         INTEGER NOT NULL,       -- 0 for entry
    due_at            TEXT NOT NULL,
    anchor_at         TEXT,                   -- entry-quote completion; horizons measure from it
    status            TEXT NOT NULL DEFAULT 'pending',   -- pending|done|failed|abandoned
    attempts          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    cohort            TEXT,                   -- 'wallet' | 'coin_filter' - never pool the two
    prior_buyers      INTEGER,                -- distinct wallets in STRICTLY BEFORE the signal
    mcap_at_signal    REAL,
    spec_version      TEXT,                   -- which freeze this episode ran under
    PRIMARY KEY (origin_event_id, leg, horizon_s)
);
CREATE INDEX IF NOT EXISTS idx_cqs_due ON copy_quote_schedule(status, due_at);

CREATE TABLE IF NOT EXISTS copy_quote_result (
    origin_event_id   TEXT NOT NULL,
    leg               TEXT NOT NULL,
    horizon_s         INTEGER NOT NULL,
    source_user_id    TEXT NOT NULL,
    network_id        TEXT NOT NULL,
    mint              TEXT NOT NULL,
    scheduled_at      TEXT,
    started_at        TEXT NOT NULL,
    quoted_at         TEXT,
    quote_duration_ms REAL,
    actual_elapsed_s  REAL,                   -- from anchor_at; the REAL horizon, not the target
    input_asset       TEXT,
    input_amount_atomic  TEXT,                -- TEXT: atomic amounts exceed SQLite INTEGER range
    output_asset      TEXT,
    output_amount_atomic TEXT,
    route             TEXT,
    provider          TEXT,
    quote_id          TEXT,
    status            TEXT NOT NULL,          -- 'ok' | 'failed'
    failure_reason    TEXT,
    source_received_at   TEXT,
    real_decision_at     TEXT,
    delay_source_to_quote_ms   REAL,
    delay_decision_to_quote_ms REAL,
    PRIMARY KEY (origin_event_id, leg, horizon_s)
);
"""


def utc_now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat() if dt is not None else None


def parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def open_measurement_db(path):
    """The measurement store. Separate file: it must never share a failure domain, a lock, or a
    schema with the funded ledger."""
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    # Idempotent column adds, so a database created by an earlier build keeps its rows instead
    # of needing a wipe. CREATE TABLE IF NOT EXISTS never alters an existing table.
    have = {r[1] for r in conn.execute("PRAGMA table_info(copy_quote_schedule)")}
    for col, decl in (("cohort", "TEXT"), ("prior_buyers", "INTEGER"),
                      ("mcap_at_signal", "REAL"), ("spec_version", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE copy_quote_schedule ADD COLUMN {col} {decl}")
    return conn


def due_episodes(ro_conn, source_user_id, network_id, since_iso, limit=200):
    """New first-buy episodes for the cohort that the funded hook has ALREADY decided.

    `real_decision_at IS NOT NULL` is the ordering guarantee: capture trails the funded decision
    and can never contend with it. One row per mint — a re-buy on a coin already captured is not
    a new episode.
    """
    rows = ro_conn.execute(
        """
        SELECT fomo_event_id, source_user_id, network_id, mint, created_at, received_at,
               real_decision_at
        FROM memecoin_events
        WHERE source_user_id = ? AND network_id = ? AND side = 'buy'
          AND COALESCE(is_first_buy, 0) = 1
          AND real_decision_at IS NOT NULL
          AND created_at > ?
          AND COALESCE(mint,'') != ''
        ORDER BY created_at
        LIMIT ?
        """,
        (source_user_id, network_id, since_iso, limit),
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        if r["mint"] in seen:
            continue
        seen.add(r["mint"])
        out.append(dict(r))
    return out


# --- coin-type cohort --------------------------------------------------------------------------
# The best-supported effect found on this tape is a COIN property, not a wallet property:
# market cap under $1M with 4-8 distinct wallets already in produces a 19.2% rate of >= +20%
# within 5 minutes against a 5.6% base (3.4x, n=512), and the whole sub-$1M row beats base at
# every consensus level. Both inputs are known before the buy.
#
# It is NOT yet an edge: the same cohort loses -0.97%/coin under its best TP/stop pair, and a
# time split ran +1.23% then -3.16%. It is captured because the losing verdict rests on modelled
# 450bps costs and four-point paths — exactly the two things this capture replaces with measured
# quantities. Volume is ~11 coins/day, so 30 coins arrive in about three days.
# Stamped on every scheduled episode. A cohort whose rows do not say which freeze they ran under
# cannot be split from a later version after the fact — the 10:21:31Z pilot episode landed under
# v1 and only a stored version distinguishes it.
SPEC_VERSION = "coin-type-fast-pump-v4"
COIN_MAX_MCAP = 1_000_000.0
COIN_CONSENSUS_MIN, COIN_CONSENSUS_MAX = 4, 8


def prior_buyer_count(ro_conn, mint, before_received_iso, network_id):
    """Distinct wallets whose buy of `mint` on `network_id` we had RECEIVED before
    `before_received_iso`.

    Two separate leaks are closed here, both found by review:

    * **Receipt time, not trade time.** An earlier version compared `created_at`, the trader's own
      buy time. Events arrive late — measured lag runs to 403s at p99 — so a buy created before
      our reference could still be unreceived when we decide, and counting it credits the strategy
      with knowledge it did not have. Switching to `received_at` moved the retrospective cell from
      10 mints to 6.
    * **Chain scope.** The count had no `network_id`, so the same mint string on another chain
      contributed buyers. Chains are never pooled anywhere else in this system.

    Strictly before remains the point: buyers arriving after the reference moment are the coin's
    own outcome, which is the leak that retracted `creator_prior` (+1,453bps to +392).
    """
    row = ro_conn.execute(
        "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_events "
        "WHERE mint = ? AND network_id = ? AND side = 'buy' "
        "AND received_at IS NOT NULL AND received_at < ?",
        (mint, network_id, before_received_iso)).fetchone()
    return int(row[0] or 0)


def coin_filter_episodes(ro_conn, network_id, since_iso, *, max_mcap=COIN_MAX_MCAP,
                         cons_min=COIN_CONSENSUS_MIN, cons_max=COIN_CONSENSUS_MAX, limit=200):
    """First buys on `network_id` matching the coin-type filter, from ANY wallet.

    Wallet identity is deliberately ignored here: this cohort tests a coin property, and mixing
    in a wallet screen would make the two inseparable in the results.
    """
    # The feed is GLOBAL: 6,446 distinct wallets appear in memecoin_events, of which only 221 are
    # on our roster. `real_decision_at` is set exclusively for roster wallets (4,053 decided, 0
    # off-roster), because only they can reach the funded hook.
    #
    # Requiring it unconditionally would silently restrict a COIN-type cohort to a 221-wallet
    # sample — reintroducing precisely the wallet selection this cohort exists to remove. So the
    # requirement is conditional: it applies to roster wallets, where a funded decision exists and
    # capture must trail it, and is waived for off-roster wallets, which the funded path can never
    # touch and therefore cannot be raced.
    rows = ro_conn.execute(
        """
        SELECT e.fomo_event_id, e.source_user_id, e.network_id, e.mint, e.created_at,
               e.received_at, e.real_decision_at, e.their_market_cap,
               (w.source_user_id IS NOT NULL) AS on_roster
        FROM memecoin_events e
        LEFT JOIN memecoin_wallets w ON w.source_user_id = e.source_user_id
        WHERE e.network_id = ? AND e.side = 'buy' AND COALESCE(e.is_first_buy, 0) = 1
          AND e.created_at > ? AND COALESCE(e.mint,'') != ''
          AND e.their_market_cap > 0 AND e.their_market_cap < ?
          AND (w.source_user_id IS NULL OR e.real_decision_at IS NOT NULL)
        ORDER BY e.created_at LIMIT ?
        """,
        (network_id, since_iso, max_mcap, limit),
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["mint"] in seen:
            continue
        # our receipt of THIS signal is the decision moment, not the trader's buy time
        n = prior_buyer_count(ro_conn, r["mint"], str(r["received_at"] or r["created_at"]),
                              r["network_id"])
        if not (cons_min <= n <= cons_max):
            continue
        seen.add(r["mint"])
        d = dict(r)
        d["prior_buyers"] = n
        out.append(d)
    return out


def schedule_entry(mdb, ep, now=None, cohort="wallet"):
    """Queue the entry quote. Idempotent on (origin_event_id, 'entry', 0).

    `cohort` records WHY this episode was admitted — a wallet in the frozen roster, or the
    coin-type filter. The two populations answer different questions and must never be pooled
    in analysis, so the reason is stored rather than inferred later.
    """
    now = now or utc_now()
    mdb.execute(
        "INSERT OR IGNORE INTO copy_quote_schedule"
        "(origin_event_id, source_user_id, network_id, mint, leg, horizon_s, due_at,"
        " anchor_at, status, attempts, created_at, cohort, prior_buyers, mcap_at_signal,"
        " spec_version) VALUES (?,?,?,?,'entry',0,?,NULL,'pending',0,?,?,?,?,?)",
        (ep["fomo_event_id"], ep["source_user_id"], ep["network_id"], ep["mint"],
         iso(now), iso(now), cohort, ep.get("prior_buyers"), ep.get("their_market_cap"),
         SPEC_VERSION),
    )


def schedule_exits(mdb, row, anchor, legs=None):
    """Queue every exit leg once the entry quote lands.

    Horizons are measured from the ENTRY QUOTE COMPLETION, not from the source event. The source
    delay and the funded-hook delay are recorded separately on each result rather than folded
    into the horizon, so a slow hook shifts the whole curve visibly instead of silently.
    """
    legs = EXIT_LEGS_S if legs is None else legs
    # Provenance is INHERITED from the entry row, never re-derived. Exit legs written without it
    # carried cohort=NULL, so a coin-filter episode's 20 legs pooled with the wallet cohort's —
    # the one thing the cohort column exists to prevent.
    src = mdb.execute(
        "SELECT cohort, prior_buyers, mcap_at_signal, spec_version FROM copy_quote_schedule "
        "WHERE origin_event_id=? AND leg='entry'", (row["origin_event_id"],)).fetchone()
    cohort = src["cohort"] if src else None
    prior = src["prior_buyers"] if src else None
    mcap = src["mcap_at_signal"] if src else None
    version = src["spec_version"] if src else SPEC_VERSION
    for h in legs:
        due = anchor.timestamp() + h
        mdb.execute(
            "INSERT OR IGNORE INTO copy_quote_schedule"
            "(origin_event_id, source_user_id, network_id, mint, leg, horizon_s, due_at,"
            " anchor_at, status, attempts, created_at, cohort, prior_buyers, mcap_at_signal,"
            " spec_version) VALUES (?,?,?,?,'exit',?,?,?,'pending',0,?,?,?,?,?)",
            (row["origin_event_id"], row["source_user_id"], row["network_id"], row["mint"],
             int(h), iso(datetime.fromtimestamp(due, timezone.utc)), iso(anchor),
             iso(utc_now()), cohort, prior, mcap, version),
        )


def claim_due(mdb, now=None, limit=32):
    """Schedule rows whose time has come, oldest first."""
    now = now or utc_now()
    return [dict(r) for r in mdb.execute(
        "SELECT * FROM copy_quote_schedule WHERE status='pending' AND due_at <= ? "
        "ORDER BY due_at LIMIT ?", (iso(now), limit))]


def active_episode_count(mdb):
    """Episodes still holding unfinished legs. The cap on this is what stops a burst of signals
    from turning into an unbounded quote fan-out."""
    return int(mdb.execute(
        "SELECT COUNT(DISTINCT origin_event_id) FROM copy_quote_schedule "
        "WHERE status='pending'").fetchone()[0])


def quotes_today(mdb, now=None):
    """Provider calls recorded in the current UTC day, successes and failures alike — a failed
    call cost the provider exactly as much as a successful one."""
    now = now or utc_now()
    day = iso(now)[:10]
    return int(mdb.execute(
        "SELECT COUNT(*) FROM copy_quote_result WHERE substr(started_at,1,10)=?",
        (day,)).fetchone()[0])


def admission_block(mdb, now=None, *, max_active=MAX_ACTIVE_EPISODES,
                    max_daily=MAX_QUOTES_PER_DAY):
    """Reason to refuse a NEW episode, or None. Checked before scheduling an entry, never before
    an exit leg: an episode already under way must be allowed to finish, because a curve missing
    its middle is worse than an episode that never started.

    Returns a string so the refusal is logged as a fact rather than a silent skip.
    """
    active = active_episode_count(mdb)
    if active >= max_active:
        return f"max_active_episodes({active}/{max_active})"
    used = quotes_today(mdb, now)
    if used >= max_daily:
        return f"daily_quote_budget({used}/{max_daily})"
    return None


def record(mdb, row, *, status, started, quoted=None, quote=None, failure=None,
           ep=None, anchor=None):
    """Write one result and close its schedule row. A failure is a terminal row, never a gap —
    a missing row and a failed quote are different facts and must stay distinguishable."""
    duration = ((quoted - started).total_seconds() * 1000.0) if quoted else None
    elapsed = ((quoted or started) - anchor).total_seconds() if anchor else None
    src = parse_iso((ep or {}).get("received_at"))
    dec = parse_iso((ep or {}).get("real_decision_at"))
    end = quoted or started
    mdb.execute(
        "INSERT OR REPLACE INTO copy_quote_result VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?)",
        (row["origin_event_id"], row["leg"], int(row["horizon_s"]), row["source_user_id"],
         row["network_id"], row["mint"], row.get("due_at"), iso(started), iso(quoted),
         duration, elapsed,
         (quote or {}).get("input_asset"), (quote or {}).get("input_amount_atomic"),
         (quote or {}).get("output_asset"), (quote or {}).get("output_amount_atomic"),
         (quote or {}).get("route"), "jupiter", (quote or {}).get("quote_id"),
         status, failure, iso(src), iso(dec),
         ((end - src).total_seconds() * 1000.0) if src else None,
         ((end - dec).total_seconds() * 1000.0) if dec else None),
    )
    mdb.execute(
        "UPDATE copy_quote_schedule SET status=?, attempts=attempts+1 "
        "WHERE origin_event_id=? AND leg=? AND horizon_s=?",
        ("done" if status == "ok" else "failed", row["origin_event_id"], row["leg"],
         int(row["horizon_s"])),
    )


def normalise_quote(raw, *, input_asset, output_asset):
    """Jupiter response -> the fields we store. Atomic amounts stay TEXT: they exceed SQLite's
    signed 64-bit INTEGER for high-decimal tokens, and a silently overflowed amount is worse
    than a missing one."""
    if not isinstance(raw, dict):
        return None
    out = raw.get("outAmount")
    if out in (None, ""):
        return None
    labels = []
    for step in raw.get("routePlan") or []:
        info = (step or {}).get("swapInfo") or {}
        if info.get("label"):
            labels.append(str(info["label"]))
    return {
        "input_asset": input_asset,
        "input_amount_atomic": str(raw.get("inAmount") or ""),
        "output_asset": output_asset,
        "output_amount_atomic": str(out),
        "route": ">".join(labels) or None,
        "quote_id": str(raw.get("contextSlot") or "") or None,
    }


def classify_quote_failure(observed):
    """Turn an observed HTTP outcome into a typed `QuoteFailure` value.

    The exit path gets typed failures from `_typed_jupiter_exit_quote`; the entry path had only
    an untyped 'entry_quote_unavailable' string, so a rate limit, a dead route and a transport
    fault were indistinguishable on exactly the leg that decides whether an episode exists at all.
    An episode with no entry quote has no true entry, and every exit leg is refused for it — so
    the reason that entry failed is the difference between "this coin was unquotable" and "our
    provider was throttled", which are opposite conclusions.

    `observed` is {'exception': exc} and/or {'response': (status, payload, ...)}.
    Lives here rather than in the execution module: this is measurement, and the funded seam must
    not be edited for it.
    """
    from sniper.analysis.memecoin_quote_failures import QuoteFailure

    exc = observed.get("exception")
    if isinstance(exc, TimeoutError):
        return QuoteFailure.PROVIDER_TIMEOUT.value
    if exc is not None:
        return QuoteFailure.RPC_TRANSPORT.value
    response = observed.get("response")
    if not isinstance(response, tuple) or len(response) < 2:
        return QuoteFailure.MALFORMED_RESPONSE.value
    status, payload = response[0], response[1]
    if status == 429:
        return QuoteFailure.RATE_LIMIT.value
    if isinstance(status, int) and status >= 500:
        return QuoteFailure.RPC_TRANSPORT.value
    if status != 200 or not isinstance(payload, dict):
        return QuoteFailure.MALFORMED_RESPONSE.value
    message = str(payload.get("error") or payload.get("message") or "").lower()
    if "route" in message:
        return QuoteFailure.NO_ROUTE.value
    if "liquidity" in message:
        return QuoteFailure.INSUFFICIENT_LIQUIDITY.value
    return QuoteFailure.MALFORMED_RESPONSE.value


def expected_tokens(mdb, origin_event_id):
    """The exact token quantity the entry quote said $50 would buy. Every exit leg quotes THIS
    amount — a round number would measure a different trade than the one we could have made."""
    r = mdb.execute(
        "SELECT output_amount_atomic FROM copy_quote_result "
        "WHERE origin_event_id=? AND leg='entry' AND status='ok'", (origin_event_id,)).fetchone()
    if not r or not r["output_amount_atomic"]:
        return None
    try:
        amount = int(r["output_amount_atomic"])
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None
