"""Immutable, post-capture memecoin self-signal ledger.

This module reads only already-persisted feed, episode, quote, and paper-wallet
evidence. It performs no network I/O and is never called from the funded entry
hook. One episode can produce one v1 fact record; later data cannot rewrite it.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone


_SUPPORTED_NETWORKS = {"solana", "8453", "4663", "56", "1"}
_USABLE_QUOTE_QUALITIES = {"ok", "probe_ok"}
_VERSION = "shadow_signal_v1"


def _parse_ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _finite_positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _finite_nonnegative(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _paper_alpha_b(conn, source_user_id, network_id):
    if not source_user_id:
        return None, 0
    row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl),0) AS pnl "
        "FROM memecoin_positions WHERE source_user_id=? AND network_id=? "
        "AND strategy='alpha_b' AND is_open=0 "
        "AND COALESCE(execution_mode,'paper')!='real' "
        "AND id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo')",
        (source_user_id, network_id),
    ).fetchone()
    return float(row["pnl"] or 0.0), int(row["n"] or 0)


def _first_quote(conn, network_id, mint, observed_at, deadline_at, *, usable=False):
    quality_sql = " AND price>0 AND lower(quality) IN ('ok','probe_ok')" if usable else ""
    return conn.execute(
        "SELECT id, observed_at, price, liquidity, market_cap, quality FROM "
        "memecoin_quote_observations_v2 WHERE network_id=? AND mint=? "
        "AND julianday(observed_at)>=julianday(?) "
        "AND julianday(observed_at)<=julianday(?) " + quality_sql +
        "ORDER BY julianday(observed_at), id LIMIT 1",
        (network_id, mint, observed_at, deadline_at),
    ).fetchone()


def _rating(*, network_id, identity_match, armed, lag_s, quote, market_cap, liquidity,
            runup_pct):
    reasons = []
    hard_reject = False
    if network_id not in _SUPPORTED_NETWORKS:
        reasons.append("unsupported_network")
        hard_reject = True
    if not identity_match:
        reasons.append("origin_identity_mismatch")
        hard_reject = True
    if quote is None:
        reasons.append("quote_unavailable")
        return "reject", reasons
    price = _finite_positive(quote["price"])
    if price is None or str(quote["quality"] or "").lower() not in _USABLE_QUOTE_QUALITIES:
        reasons.append("quote_unusable")
        hard_reject = True
    if market_cap is not None:
        if market_cap >= 30_000_000:
            reasons.append("entry_mcap_ge_30m")
            hard_reject = True
        elif market_cap >= 5_000_000:
            reasons.append("entry_mcap_5m_30m")
        elif market_cap >= 1_000_000:
            reasons.append("entry_mcap_1m_5m")
    else:
        reasons.append("entry_mcap_unknown")
    if runup_pct is not None and runup_pct >= 100.0:
        reasons.append("episode_runup_ge_100pct")
        hard_reject = True
    if not armed:
        reasons.append("wallet_not_armed")
    if lag_s is None:
        reasons.append("claimed_lag_unknown")
    elif lag_s >= 60.0:
        reasons.append("claimed_lag_ge_60s")
    if liquidity is None:
        reasons.append("liquidity_unknown")
    elif liquidity == 0:
        reasons.append("liquidity_zero")
        hard_reject = True
    return ("reject" if hard_reject else "watch" if reasons else "candidate"), reasons


def evaluate_shadow_signals(conn, now_iso, *, quote_window_s=90):
    """Create immutable v1 records for episodes whose quote window is resolved."""
    now = _parse_ts(now_iso)
    if now is None:
        raise ValueError("now_iso must be an aware ISO timestamp")
    try:
        quote_window_s = float(quote_window_s)
    except (TypeError, ValueError) as exc:
        raise ValueError("quote_window_s must be positive") from exc
    if not math.isfinite(quote_window_s) or quote_window_s <= 0:
        raise ValueError("quote_window_s must be positive")

    episodes = conn.execute(
        "SELECT e.episode_id, e.chain_id, e.token_address, e.started_ts, "
        "e.first_signal_id, e.first_source_price, e.first_executable_price "
        "FROM memecoin_token_episodes e LEFT JOIN memecoin_shadow_signals s "
        "ON s.episode_id=e.episode_id WHERE s.episode_id IS NULL "
        "AND e.first_signal_id IS NOT NULL ORDER BY julianday(e.started_ts), e.episode_id"
    ).fetchall()
    result = {"created": 0, "waiting": 0, "collisions": 0}
    for episode in episodes:
        network_id = str(episode["chain_id"] or "").strip().lower()
        mint = str(episode["token_address"] or "").strip()
        event_id = str(episode["first_signal_id"] or "").strip()
        feed = conn.execute(
            "SELECT id, source_user_id, source_handle, network_id, mint, their_price, "
            "their_market_cap, observed_at, lag_s, was_real_approved, clock_domain "
            "FROM memecoin_feed_observations_v2 WHERE upstream_event_id=? LIMIT 1",
            (event_id,),
        ).fetchone()
        signal = conn.execute(
            "SELECT observed_at, source_price, executable_price FROM memecoin_episode_signals "
            "WHERE episode_id=? AND upstream_event_id=? LIMIT 1",
            (episode["episode_id"], event_id),
        ).fetchone()
        observed_at = ((feed["observed_at"] if feed else None)
                       or (signal["observed_at"] if signal else None)
                       or episode["started_ts"])
        observed = _parse_ts(observed_at)
        if observed is None or not network_id or not mint or not event_id:
            # Bad episode identity cannot be assigned a trustworthy local deadline.
            continue
        deadline = observed + timedelta(seconds=quote_window_s)
        deadline_iso = deadline.isoformat()
        quote = _first_quote(
            conn, network_id, mint, observed.isoformat(), deadline_iso, usable=True,
        )
        if quote is None and now < deadline:
            result["waiting"] += 1
            continue
        if quote is None:
            # The deadline resolved without a usable observation. Preserve the first bad row
            # when one exists so terminal attribution distinguishes unusable from unavailable.
            quote = _first_quote(conn, network_id, mint, observed.isoformat(), deadline_iso)

        event = conn.execute(
            "SELECT signal_source FROM memecoin_events WHERE fomo_event_id=? LIMIT 1",
            (event_id,),
        ).fetchone()
        signal_source = str(event["signal_source"] if event and event["signal_source"] else "unknown")
        source_user_id = feed["source_user_id"] if feed else None
        source_handle = feed["source_handle"] if feed else None
        lag_s = _finite_nonnegative(feed["lag_s"] if feed else None)
        armed = bool(feed and feed["was_real_approved"] == 1)
        source_price = _finite_positive(
            (feed["their_price"] if feed else None)
            or (signal["source_price"] if signal else None)
            or episode["first_source_price"]
        )
        quote_price = _finite_positive(quote["price"] if quote else None)
        market_cap = _finite_positive(quote["market_cap"] if quote else None)
        if market_cap is None:
            market_cap = _finite_positive(feed["their_market_cap"] if feed else None)
        liquidity = _finite_nonnegative(quote["liquidity"] if quote else None)
        first_exec = _finite_positive(
            episode["first_executable_price"]
            or (signal["executable_price"] if signal else None)
        )
        runup_pct = ((quote_price / first_exec - 1.0) * 100.0
                     if quote_price is not None and first_exec is not None else None)
        quote_at = quote["observed_at"] if quote else None
        quote_dt = _parse_ts(quote_at)
        quote_age_s = ((quote_dt - observed).total_seconds() if quote_dt is not None else None)
        consensus_at_signal = conn.execute(
            "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_feed_observations_v2 "
            "WHERE network_id=? AND mint=? AND normalized_side='buy' "
            "AND julianday(observed_at)>=julianday(?) "
            "AND julianday(observed_at)<=julianday(?)",
            (network_id, mint, episode["started_ts"], observed.isoformat()),
        ).fetchone()[0]
        wallet_pnl, wallet_closes = _paper_alpha_b(conn, source_user_id, network_id)
        identity_match = bool(
            feed
            and str(feed["network_id"] or "").strip().lower() == network_id
            and str(feed["mint"] or "").strip() == mint
        )
        rating, reasons = _rating(
            network_id=network_id, identity_match=identity_match, armed=armed, lag_s=lag_s,
            quote=quote,
            market_cap=market_cap, liquidity=liquidity, runup_pct=runup_pct,
        )
        facts = {
            "version": _VERSION,
            "episode_id": episode["episode_id"],
            "origin_event_id": event_id,
            "network_id": network_id,
            "mint": mint,
            "source_user_id": source_user_id,
            "signal_source": signal_source,
            "observed_at": observed.isoformat(),
            "claimed_source_lag_s": lag_s,
            "claimed_source_lag_trust": "claimed_untrusted",
            "was_real_approved": armed,
            "origin_identity_match": identity_match,
            "source_price": source_price,
            "first_executable_price": first_exec,
            "quote_observation_id": quote["id"] if quote else None,
            "quote_price": quote_price,
            "quote_observed_at": quote_at,
            "quote_age_s": quote_age_s,
            "market_cap": market_cap,
            "liquidity": liquidity,
            "episode_executable_runup_pct": runup_pct,
            "consensus_at_signal": int(consensus_at_signal or 0),
            "wallet_ab_pnl": wallet_pnl,
            "wallet_ab_closes": wallet_closes,
            "rating": rating,
            "reason_codes": reasons,
        }
        facts_json = _canonical_json(facts)
        facts_hash = hashlib.sha256(facts_json.encode()).hexdigest()
        shadow_signal_id = "shadow_" + hashlib.sha256(
            f"{_VERSION}:{episode['episode_id']}:{network_id}:{mint}".encode()
        ).hexdigest()[:24]
        cur = conn.execute(
            "INSERT OR IGNORE INTO memecoin_shadow_signals(shadow_signal_id, episode_id, "
            "origin_event_id, network_id, mint, source_user_id, source_handle, signal_source, "
            "observed_at, claimed_source_lag_s, source_price, quote_observation_id, quote_price, "
            "quote_observed_at, quote_age_s, market_cap, liquidity, consensus_at_signal, "
            "wallet_ab_pnl, wallet_ab_closes, rating, reason_codes_json, facts_json, "
            "facts_hash, created_at, clock_domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?)",
            (shadow_signal_id, episode["episode_id"], event_id, network_id, mint,
             source_user_id, source_handle, signal_source, observed.isoformat(), lag_s,
             source_price, quote["id"] if quote else None, quote_price, quote_at, quote_age_s,
             market_cap, liquidity, int(consensus_at_signal or 0), wallet_pnl, wallet_closes,
             rating, _canonical_json(reasons), facts_json, facts_hash, now.isoformat(),
             "observed_real_utc"),
        )
        if cur.rowcount == 1:
            result["created"] += 1
        else:
            existing = conn.execute(
                "SELECT facts_hash FROM memecoin_shadow_signals WHERE episode_id=?",
                (episode["episode_id"],),
            ).fetchone()
            if existing is None or existing["facts_hash"] != facts_hash:
                result["collisions"] += 1
    conn.commit()
    return result


def list_shadow_signals(conn, *, chain=None, source=None, rating=None, limit=100):
    """Read-only newest-first shadow ledger projection."""
    where, params = [], []
    if chain:
        where.append("network_id=?")
        params.append(str(chain))
    if source:
        where.append("signal_source=?")
        params.append(str(source))
    if rating:
        where.append("rating=?")
        params.append(str(rating))
    query = "SELECT * FROM memecoin_shadow_signals"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY created_at DESC, shadow_signal_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    rows = []
    for row in conn.execute(query, params).fetchall():
        item = dict(row)
        item["reason_codes"] = json.loads(item.pop("reason_codes_json"))
        item["facts"] = json.loads(item.pop("facts_json"))
        rows.append(item)
    return rows
