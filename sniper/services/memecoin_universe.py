"""Coin Radar universe (Stage 1).

Coin-centric view over the feed captures, enriched with the latest stored quote (mcap / liquidity /
24h volume) and the known-BUYER overlap — which tracked/armed wallets bought a coin. Read-only; powers
the radar watchboard and coin search. External discovery (Stage 2) unions its rows in via
`extra_sources`. The "armed buyer" signal ("a wallet we rate bought this") is deliberately stronger and
better-covered than on-chain holder lists, which we do not track.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Sort key -> the row field it maps to. `age` = seconds since first seen (younger first when asc);
# `recent` = seconds since last seen. All numeric; None sorts last regardless of direction.
_SORT_FIELDS = {
    "vol": "volume_24h", "liq": "liquidity", "mcap": "market_cap",
    "buyers": "buyers", "armed": "armed_buyers", "age": "age_s", "recent": "recent_s",
    "v15": "buyers_15m", "v60": "buyers_60m",
}


def _now(now_iso):
    return now_iso or datetime.now(timezone.utc).isoformat()


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _coin_key(network_id, mint):
    """Canonical coin key — delegates to accounting_mark_key so radar dedup, the quote-cache overlay,
    and the money-path (radar_quote_map/socials/triage) all key coins identically. Previously this was a
    third, divergent convention (lowercased 0x but did NOT normalize the network label), which split a
    coin whenever its network was written non-canonically (e.g. 'evm-unknown' vs '4663')."""
    from sniper.persistence.db_memecoin import accounting_mark_key
    return accounting_mark_key(network_id, mint)


def _latest_quotes_batch(conn, pairs):
    """{(network_id, mint): quote_row} — the latest quote of each pair in ONE query (was an N+1: one
    point query per displayed coin, ~300/render). A per-pair correlated `ORDER BY observed_at DESC
    LIMIT 1` subquery resolves the latest row's rowid via the covering index (fast index seek, same as
    the old point lookups); a MAX(observed_at) GROUP BY instead built a temp b-tree over every quote row
    of the hot coins (1.7M rows -> ~4s)."""
    if not pairs:
        return {}
    ph = ",".join("(?,?)" for _ in pairs)
    args = [x for pr in pairs for x in pr]
    rows = conn.execute(
        f"WITH pairs(nid, mint) AS (VALUES {ph}), "
        "latest AS (SELECT p.nid, p.mint, (SELECT q.rowid FROM memecoin_quote_observations_v2 q "
        "    WHERE q.network_id=p.nid AND q.mint=p.mint ORDER BY q.observed_at DESC LIMIT 1) rid "
        "  FROM pairs p) "
        "SELECT l.nid, l.mint, q.price, q.market_cap, q.liquidity, q.volume_24h, q.observed_at "
        "FROM latest l JOIN memecoin_quote_observations_v2 q ON q.rowid=l.rid", args).fetchall()
    return {(r["nid"], r["mint"]): r for r in rows}


def _ratings_batch(conn, pairs):
    """{(network_id, mint): (rating, rating_version)} — latest rating per pair, ONE window query."""
    if not pairs:
        return {}
    ph = ",".join("(?,?)" for _ in pairs)
    args = [x for pr in pairs for x in pr]
    rows = conn.execute(
        f"WITH pairs(nid, mint) AS (VALUES {ph}), "
        "ranked AS (SELECT r.network_id nid, r.mint mint, r.rating, r.rating_version, ROW_NUMBER() OVER "
        "  (PARTITION BY r.network_id, r.mint ORDER BY r.created_at DESC) rn "
        "  FROM memecoin_radar_ratings r JOIN pairs p ON p.nid=r.network_id AND p.mint=r.mint) "
        "SELECT nid, mint, rating, rating_version FROM ranked WHERE rn=1", args).fetchall()
    return {(r["nid"], r["mint"]): (r["rating"], r["rating_version"]) for r in rows}


def _real_open_set(conn):
    """Set of (network_id, mint) with an OPEN real position — ONE query (was one per displayed coin)."""
    return {(r["network_id"], r["mint"]) for r in conn.execute(
        "SELECT network_id, mint FROM memecoin_positions WHERE execution_mode='real' AND is_open=1")}


def _armed_handles(conn, network_id, mint):
    """Handles of the tracked/armed wallets that bought this coin (for the radar tooltip)."""
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT w.handle FROM memecoin_wallets w JOIN ("
        "  SELECT source_user_id FROM memecoin_feed_observations_v2 "
        "  WHERE network_id=? AND mint=? AND normalized_side='buy' "
        "  UNION "
        "  SELECT source_user_id FROM memecoin_events "
        "  WHERE network_id=? AND mint=? AND side='buy'"
        ") buyers ON buyers.source_user_id=w.source_user_id "
        "WHERE w.real_approved=1 "
        "AND COALESCE(w.paused,0)=0 AND COALESCE(w.excluded,0)=0 AND COALESCE(w.handle,'')!='' "
        "ORDER BY w.handle", (network_id, mint, network_id, mint)) if r[0]]


def _apply_cache(row, quote_cache):
    """Overlay the daemon-refreshed quote (fresher than the stored capture) onto a row IN PLACE."""
    if not quote_cache:
        return row
    # radar_quote_map is keyed by accounting_mark_key; _coin_key now yields the same, so one canonical
    # lookup replaces the old raw-then-_coin_key fallback (which missed non-canonical network labels).
    cq = quote_cache.get(_coin_key(row.get("network_id"), row.get("mint")))
    if cq:
        for f in ("price", "market_cap", "liquidity", "volume_24h"):
            if cq.get(f) is not None:
                row[f] = cq[f]
    return row


def universe_rows(conn, *, sort="buyers", desc=True, chain=None, source=None, min_liq=None, min_vol=None,
                  min_armed=None, q=None, window_hours=72, limit=100, candidate_cap=300,
                  extra_sources=None, quote_cache=None, now_iso=None):
    """One row per coin: ticker, chain, mcap, liq, 24h vol, buyers + armed_buyers (known-BUYER overlap),
    age, rating, real-open flag. Sortable by any key in `_SORT_FIELDS`. Read-only.

    A single aggregate join computes buyers + armed_buyers per coin (armed = feed source_user_id that is
    a `real_approved`, non-paused, non-excluded wallet); the requested sort is applied before
    `candidate_cap`, then the selected rows are enriched.
    """
    now_iso = _now(now_iso)
    now_dt = _parse(now_iso)
    where, params = ["COALESCE(o.network_id,'')!=''", "COALESCE(o.mint,'')!=''"], []
    # Window filter belongs in HAVING (post-group on MAX(last_seen)), NOT the pre-group WHERE. Filtering
    # rows by last_seen before GROUP BY made MIN(first_seen) the earliest buy *within the window*, so an
    # old coin re-pumping now reported age≈minutes and masqueraded as a fresh launch. HAVING keeps a
    # coin's full history for MIN(first_seen) while still requiring recent activity to appear.
    # velocity windows: distinct buyers in the last 15m / 60m -> rank by INCOMING consensus, not the
    # 72h cumulative. NULL cutoff (no clock) -> the CASE never matches -> 0, which is honest.
    v15 = (now_dt - timedelta(minutes=15)).isoformat() if now_dt is not None else None
    v60 = (now_dt - timedelta(minutes=60)).isoformat() if now_dt is not None else None
    having, having_params = [], []
    if window_hours and now_dt is not None:
        having.append("MAX(o.last_seen) >= ?")
        having_params.append((now_dt - timedelta(hours=float(window_hours))).isoformat())
    if chain:
        where.append("o.network_id = ?")
        params.append(str(chain))
    if q:
        where.append("(LOWER(o.ticker) LIKE ? OR LOWER(o.mint) LIKE ?)")
        ql = "%" + str(q).lower() + "%"
        params += [ql, ql]
    wq = (" WHERE " + " AND ".join(where)) if where else ""
    hq = (" HAVING " + " AND ".join(having)) if having else ""
    sql_sort = {
        "buyers": "buyers", "armed": "armed_buyers", "v15": "buyers_15m",
        "v60": "buyers_60m", "age": "first_seen", "recent": "last_seen",
    }.get(sort, "buyers")
    sort_params = []
    quote_sort_field = {
        "mcap": "market_cap", "liq": "liquidity", "vol": "volume_24h",
    }.get(sort)
    if quote_sort_field:
        observed_sort = (
            f"(SELECT q.{quote_sort_field} FROM memecoin_quote_observations_v2 q WHERE "
            "q.network_id=grouped.network_id AND q.mint=grouped.mint "
            "ORDER BY q.observed_at DESC LIMIT 1)"
        )
        has_radar_cache = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memecoin_radar_quote'"
        ).fetchone() is not None
        if has_radar_cache and now_dt is not None:
            cache_cutoff = (now_dt - timedelta(minutes=10)).isoformat()
            cached_sort = (
                f"(SELECT rq.{quote_sort_field} FROM memecoin_radar_quote rq WHERE "
                "rq.chain_id=grouped.network_id AND rq.mint=grouped.mint "
                "AND julianday(rq.updated_at)>=julianday(?) LIMIT 1)"
            )
            sql_sort = f"COALESCE({cached_sort},{observed_sort})"
            sort_params = [cache_cutoff, cache_cutoff]
        else:
            sql_sort = observed_sort
    sql_direction = "DESC" if desc else "ASC"
    if sort == "age":
        sql_direction = "ASC" if desc else "DESC"
    base = conn.execute(
        "WITH observations AS ("
        "  SELECT network_id,mint,ticker,source_user_id,observed_at first_seen,observed_at last_seen "
        "  FROM memecoin_feed_observations_v2 WHERE normalized_side='buy' "
        "  UNION ALL "
        "  SELECT network_id,mint,ticker,source_user_id,"
        "         COALESCE(received_at_normalized,received_at,created_at),"
        "         COALESCE(received_at_normalized,received_at,created_at) "
        "  FROM memecoin_events WHERE side='buy' "
        "  UNION ALL "
        "  SELECT chain_id,token_address,NULL,NULL,started_ts,COALESCE(last_signal_ts,started_ts) "
        "  FROM memecoin_token_episodes"
        "), grouped AS ("
        "SELECT o.network_id, o.mint, MAX(NULLIF(o.ticker,'')) ticker, "
        "  COUNT(DISTINCT o.source_user_id) buyers, "
        "  COUNT(DISTINCT CASE WHEN w.real_approved=1 AND COALESCE(w.paused,0)=0 "
        "                       AND COALESCE(w.excluded,0)=0 THEN o.source_user_id END) armed_buyers, "
        "  COUNT(DISTINCT CASE WHEN o.last_seen >= ? THEN o.source_user_id END) buyers_15m, "
        "  COUNT(DISTINCT CASE WHEN o.last_seen >= ? THEN o.source_user_id END) buyers_60m, "
        "  MIN(o.first_seen) first_seen, MAX(o.last_seen) last_seen "
        "FROM observations o "
        "LEFT JOIN memecoin_wallets w ON w.source_user_id = o.source_user_id"
        + wq +
        " GROUP BY o.network_id, o.mint" + hq + ") SELECT * FROM grouped "
        f"ORDER BY ({sql_sort}) IS NULL, {sql_sort} {sql_direction}, last_seen DESC LIMIT ?",
        [v15, v60] + params + having_params + sort_params
        + [int(max(1, candidate_cap))]).fetchall()

    source = str(source).strip().lower() if source else None
    # batch the per-coin lookups (quote / rating / real-open) into 3 set queries instead of ~3*N point
    # queries — the board auto-refreshes and this ran ~900 queries/render against the live DB.
    pairs = [(b["network_id"], b["mint"]) for b in base]
    quotes = _latest_quotes_batch(conn, pairs)
    ratings = _ratings_batch(conn, pairs)
    real_open = _real_open_set(conn)
    rows = []
    for b in base:
        nid, mint = b["network_id"], b["mint"]
        quote = quotes.get((nid, mint))
        rating, rating_version = ratings.get((nid, mint), (None, None))
        first_dt, last_dt = _parse(b["first_seen"]), _parse(b["last_seen"])
        row = {
            "network_id": nid, "mint": mint, "ticker": b["ticker"],
            "chain": nid,
            "venue": "EVM" if str(mint).startswith("0x") else "SOL",
            "buyers": b["buyers"], "armed_buyers": b["armed_buyers"],
            "buyers_15m": b["buyers_15m"], "buyers_60m": b["buyers_60m"],
            "price": quote["price"] if quote else None,
            "market_cap": quote["market_cap"] if quote else None,
            "liquidity": quote["liquidity"] if quote else None,
            "volume_24h": quote["volume_24h"] if quote else None,
            "quote_ts": quote["observed_at"] if quote else None,
            "first_seen": b["first_seen"], "last_seen": b["last_seen"],
            "age_s": (now_dt - first_dt).total_seconds() if (now_dt and first_dt) else None,
            "recent_s": (now_dt - last_dt).total_seconds() if (now_dt and last_dt) else None,
            "rating": rating, "rating_version": rating_version,
            "real_open": (nid, mint) in real_open,
            "source": "wallet",
        }
        rows.append(_apply_cache(row, quote_cache))
    if source and source != "wallet":
        rows = []

    # Union external-discovery rows, but never duplicate a coin our feed already has (the feed row
    # carries buyers/armed and wins). A `q` filter also applies to external tickers/mints.
    seen = {_coin_key(r["network_id"], r["mint"]) for r in rows}
    ql = ("%" + str(q).lower() + "%") if q else None
    for extra in (extra_sources or []):
        extra_source = str(extra.get("source") or "external").lower()
        if source == "wallet":
            continue
        if source and source != "external" and source != extra_source:
            continue
        key = _coin_key(extra.get("network_id"), extra.get("mint"))
        if key in seen:
            continue
        if chain and str(extra.get("network_id")) != str(chain):
            continue
        if ql and ql.strip("%") not in ((extra.get("ticker") or "") + (extra.get("mint") or "")).lower():
            continue
        rows.append(_apply_cache(extra, quote_cache))
        seen.add(key)

    if min_liq is not None:
        rows = [r for r in rows if (r.get("liquidity") or 0) >= float(min_liq)]
    if min_vol is not None:
        rows = [r for r in rows if (r.get("volume_24h") or 0) >= float(min_vol)]
    if min_armed is not None:
        rows = [r for r in rows if (r.get("armed_buyers") or 0) >= int(min_armed)]

    field = _SORT_FIELDS.get(sort, "buyers")
    # None always sorts LAST regardless of direction: map it to -inf for desc / +inf for asc so a blank
    # mcap/liq/vol never jumps to the top of a "highest first" sort.
    _miss = float("-inf") if desc else float("inf")
    rows.sort(key=lambda r: (r.get(field) if r.get(field) is not None else _miss), reverse=bool(desc))
    return rows[:int(max(1, limit))]


def enrich_live_quotes(conn, rows, *, dex_batch_fn=None, gt_batch_fn=None, quote_for=None):
    """Fill missing market_cap / liquidity / volume_24h on the DISPLAYED rows from a live batch quote
    (DexScreener for priced chains, GeckoTerminal for 4663). Bounded to the rows passed in (the page),
    so it never fans out over the whole universe. Injected fns keep it unit-testable; mutates + returns
    `rows`. Coins already carrying a quote are left alone."""
    need = [r for r in rows if r.get("market_cap") is None or r.get("liquidity") is None
            or r.get("volume_24h") is None or r.get("price_change_h1") is None]
    if not need:
        return rows
    keys = list({(r["network_id"] or "solana", r["mint"]) for r in need})
    live = {}
    if dex_batch_fn is not None:
        try:
            live = dex_batch_fn(keys) or {}
        except Exception:
            live = {}
    unpriced = [k for k in keys if not (quote_for(live, k[1], k[0]) if quote_for else live.get(k))]
    gt = {}
    if gt_batch_fn is not None and unpriced:
        try:
            gt = gt_batch_fn(unpriced) or {}
        except Exception:
            gt = {}
    for r in need:
        key = (r["network_id"] or "solana", r["mint"])
        q = (quote_for(live, r["mint"], r["network_id"]) if quote_for else live.get(key)) or gt.get(key) or {}
        if r.get("market_cap") is None:
            r["market_cap"] = q.get("market_cap") or q.get("mcap")
        if r.get("liquidity") is None:
            r["liquidity"] = q.get("liquidity")
        if r.get("volume_24h") is None:
            r["volume_24h"] = q.get("volume_24h")
        if r.get("price_change_h1") is None:
            r["price_change_h1"] = q.get("price_change_h1")   # 1h momentum for the entry classifier
    return rows


def search_coins(conn, query, *, live_fn=None, chain=None, limit=25, now_iso=None):
    """Search any coin by name/ticker/mint across our whole history (no window). When our universe has
    few hits and a `live_fn(query)` is supplied (DexScreener/GT lookup), append its results — the
    in-app replacement for the Fomo coin-lookup UX. `live_fn` returns a list of row dicts (already
    normalized) or None."""
    query = (query or "").strip()
    if not query:
        return []
    local = universe_rows(conn, q=query, chain=chain, sort="recent", desc=True,
                          window_hours=None, limit=limit, candidate_cap=limit * 4, now_iso=now_iso)
    seen = {(r["network_id"], r["mint"]) for r in local}
    if live_fn is not None and len(local) < limit:
        try:
            for ext in (live_fn(query) or []):
                key = (ext.get("network_id"), ext.get("mint"))
                if key in seen:
                    continue
                ext.setdefault("source", "live")
                local.append(ext)
                seen.add(key)
                if len(local) >= limit:
                    break
        except Exception:
            pass
    return local[:limit]


def armed_buyers_for(conn, network_id, mint):
    """Public helper: which tracked/armed wallets bought this coin (handles). Used by the coin drawer."""
    return _armed_handles(conn, network_id, mint)
