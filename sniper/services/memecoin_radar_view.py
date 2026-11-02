"""Read-only projection of memecoin entry-radar telemetry for the dashboard.

Every function issues SELECT statements only. It never writes and is never
called from the funded entry/exit path. It renders the tables written by the
approved 2026-07-16 entry-radar-coin-state telemetry into a live
per-(network_id, mint) view. See
docs/superpowers/specs/2026-07-16-memecoin-live-radar-visualization-design.md.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_entry_radar import rate_entry_facts

_CHAIN_LABELS = {
    "solana": "SOL", "8453": "Base", "4663": "Robinhood", "56": "BSC", "1": "ETH",
}
GIVEBACK_PCT = 40.0  # display-only: latest quote >= this % below peak -> giveback flag
HEAT_HOT_WINDOW_S = 300.0  # last buy within 5 min (and not giving back) reads as "hot"
QUOTE_FRESH_S = 90.0  # display-only; capture horizons use the same minimum lateness tolerance

# real_decision values that mean "we saw it and chose/were unable to enter"
_PASS_REASONS = {
    "skipped_mcap": "mcap gate",
    "unroutable_chain": "chain not supported",
    "route_failed": "entry route unavailable",
    "unsafe": "safety check",
    "rejected": "already held",
}
# not_approved / disarmed just mean the buyer isn't an armed wallet -> still "watching"


def _hold_detail(pnl_pct, exec_high_pct):
    """`exec_high_pct` is the best whole-bag sell quote against the fill (the executable series),
    never the display peak: a funded row carries no display extreme."""
    parts = []
    if pnl_pct is not None:
        parts.append(f"{pnl_pct:+.0f}%")
    if exec_high_pct is not None:
        parts.append(f"sell-quote high {exec_high_pct:+.0f}%")
    return " · ".join(parts) or None


def chain_label(network_id):
    if network_id is None:
        return "?"
    return _CHAIN_LABELS.get(str(network_id), str(network_id))


def _parse_ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_s(start_iso, now_iso):
    a, b = _parse_ts(start_iso), _parse_ts(now_iso)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _pct_change(a, b):
    """(b - a) / a * 100, or None if a is missing/zero or b is missing."""
    try:
        a = float(a); b = float(b)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return (b - a) / a * 100.0


def drop_top_two(pnls):
    """Sum after removing the two largest values (tail-dependence check)."""
    vals = sorted((float(x) for x in pnls), reverse=True)
    return round(sum(vals[2:]), 2)


def advisory_rating(episode, latest_quote, first_source_ts, now_iso, *, chase_cap_pct=None):
    """Display-only green/yellow/red for a watch-only coin (no real attempt).

    Reuses the immutable v1 rule against observable episode + latest-quote facts.
    NEVER persisted. Returns (rating, reason_codes).
    """
    ep = dict(episode) if episode is not None else {}
    q = dict(latest_quote) if latest_quote is not None else {}
    first_exec = ep.get("first_executable_price")
    runup = _pct_change(first_exec, q.get("price")) if first_exec else None
    facts = {
        "terminal_status": None,   # no attempt -> never an existing-refusal red
        "terminal_reason": None,
        "network_id": ep.get("chain_id"),
        "mint": ep.get("token_address"),
        "source_price": ep.get("first_source_price"),
        "quote_price": q.get("price"),
        "quote_age_ms": None,
        "quote_id": q.get("id"),
        "entry_mcap": q.get("market_cap"),
        "entry_liquidity": q.get("liquidity"),
        "observed_at": q.get("observed_at"),
        "claimed_source_age_s": _age_s(first_source_ts, now_iso),
        "episode_executable_runup_pct": runup,
    }
    qsrc, qobs = q.get("source_ts"), q.get("observed_at")
    a, b = _parse_ts(qsrc), _parse_ts(qobs)
    if a is not None and b is not None:
        facts["quote_age_ms"] = max(0.0, (b - a).total_seconds() * 1000.0)
    return rate_entry_facts(facts, chase_cap_pct=chase_cap_pct)


def _now(now_iso):
    if now_iso:
        return now_iso
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()


def radar_rows(conn, *, window_hours=24, chain=None, source=None, rating=None,
               fast=False, phase=None, limit=200, now_iso=None):
    now_iso = _now(now_iso)
    now_dt = _parse_ts(now_iso)
    cutoff_iso = None
    if now_dt is not None and window_hours:
        cutoff_iso = (now_dt - timedelta(hours=float(window_hours))).isoformat()

    q = ("SELECT episode_id, chain_id, token_address, started_ts, ended_ts, status, "
         "consensus_cluster_count, first_source_price, first_executable_price, "
         "last_signal_ts FROM memecoin_token_episodes")
    where, params = [], []
    if cutoff_iso:
        where.append("COALESCE(last_signal_ts, started_ts) >= ?"); params.append(cutoff_iso)
    if chain:
        where.append("chain_id = ?"); params.append(str(chain))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY COALESCE(last_signal_ts, started_ts) DESC LIMIT ?"
    params.append(int(max(1, min(int(limit), 500))))

    rows = []
    for ep in conn.execute(q, params).fetchall():
        nid, mint = ep["chain_id"], ep["token_address"]
        latest_q = conn.execute(
            "SELECT id, price, market_cap, liquidity, volume_24h, quality, dex_id, "
            "observed_at, source_ts "
            "FROM memecoin_quote_observations_v2 WHERE network_id=? AND mint=? "
            "ORDER BY observed_at DESC LIMIT 1", (nid, mint)).fetchone()
        buyers = conn.execute(
            "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_feed_observations_v2 "
            "WHERE network_id=? AND mint=?", (nid, mint)).fetchone()[0]
        first_obs = conn.execute(
            "SELECT ticker, source_ts, source_user_id, source_handle "
            "FROM memecoin_feed_observations_v2 "
            "WHERE network_id=? AND mint=? ORDER BY observed_at ASC LIMIT 1",
            (nid, mint)).fetchone()
        ev = conn.execute(
            "SELECT signal_source FROM memecoin_events WHERE network_id=? AND mint=? "
            "AND signal_source IS NOT NULL ORDER BY created_at ASC LIMIT 1",
            (nid, mint)).fetchone()
        rating_row = conn.execute(
            "SELECT rating FROM memecoin_radar_ratings WHERE episode_id=? "
            "ORDER BY created_at DESC LIMIT 1", (ep["episode_id"],)).fetchone()
        if rating_row is None:
            rating_row = conn.execute(
                "SELECT rating FROM memecoin_radar_ratings WHERE network_id=? AND mint=? "
                "ORDER BY created_at DESC LIMIT 1", (nid, mint)).fetchone()
        # "wallets in" = distinct buyers across feed captures and events (episodes
        # can have 0 feed-obs rows but the buys are recorded in events).
        ev_buyers = conn.execute(
            "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_events "
            "WHERE network_id=? AND mint=?", (nid, mint)).fetchone()[0]
        buyers = max(buyers, ev_buyers)

        real_open = conn.execute(
            "SELECT id, entry_price, last_quote_price, price_attribution "
            "FROM memecoin_positions "
            "WHERE network_id=? AND mint=? AND execution_mode='real' AND is_open=1 "
            "ORDER BY opened_at DESC LIMIT 1", (nid, mint)).fetchone()
        real_closed = None
        if real_open is None:
            real_closed = conn.execute(
                "SELECT realized_pnl, price_attribution FROM memecoin_positions "
                "WHERE network_id=? AND mint=? AND execution_mode='real' AND is_open=0 "
                "ORDER BY closed_at DESC LIMIT 1", (nid, mint)).fetchone()
        last_decision = conn.execute(
            "SELECT real_decision, real_decision_reason FROM memecoin_events "
            "WHERE network_id=? AND mint=? AND real_decision IS NOT NULL "
            "ORDER BY COALESCE(real_decision_at, created_at) DESC, id DESC LIMIT 1",
            (nid, mint)).fetchone()
        latest_attempt = conn.execute(
            "SELECT price_attribution FROM memecoin_entry_attempts "
            "WHERE network_id=? AND mint=? ORDER BY completed_at DESC LIMIT 1",
            (nid, mint)).fetchone()

        source_user_id = first_obs["source_user_id"] if first_obs else None
        wallet_ab = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(realized_pnl),0) AS pnl "
            "FROM memecoin_positions WHERE source_user_id=? AND network_id=? "
            "AND strategy='alpha_b' AND is_open=0 "
            "AND COALESCE(execution_mode,'paper')!='real' "
            "AND id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo')",
            (source_user_id, nid),
        ).fetchone() if source_user_id else None

        attempt_rating = rating_row["rating"] if rating_row else None
        adv, adv_reasons = advisory_rating(
            ep, latest_q, first_obs["source_ts"] if first_obs else None, now_iso)

        if real_open is not None:
            ph = "owned"
        elif ep["ended_ts"] or str(ep["status"] or "").lower() == "closed":
            ph = "cooldown"
        else:
            ph = "watching"

        latest_price = latest_q["price"] if latest_q else None
        quote_observed_at = latest_q["observed_at"] if latest_q else None
        quote_age_s = _age_s(quote_observed_at, now_iso)
        quote_state = ("unknown" if quote_age_s is None else
                       "fresh" if quote_age_s <= QUOTE_FRESH_S else "stale")
        first_exec = ep["first_executable_price"]
        runup = _pct_change(first_exec, latest_price) if first_exec else None
        src_quote_drift = _pct_change(ep["first_source_price"], ep["first_executable_price"])
        peak = conn.execute(
            "SELECT MAX(price) FROM memecoin_quote_observations_v2 "
            "WHERE network_id=? AND mint=?", (nid, mint)).fetchone()[0]
        giveback = bool(
            peak and latest_price is not None
            and float(latest_price) <= float(peak) * (1 - GIVEBACK_PCT / 100.0)
            and ph in ("owned", "watching"))
        claimed_age = _age_s(first_obs["source_ts"] if first_obs else None, now_iso)
        last_activity_age_s = _age_s(ep["last_signal_ts"], now_iso)
        eff_rating = attempt_rating or adv

        # plain-language "did we act?" status
        decision = str(last_decision["real_decision"]) if last_decision else ""
        if decision in ("unroutable_chain", "route_failed"):
            route_state = "blocked"
        elif decision == "filled":
            route_state = "ready"
        else:
            route_state = "unknown"
        position_attribution = None
        if real_open is not None:
            position_attribution = real_open["price_attribution"]
        elif real_closed is not None:
            position_attribution = real_closed["price_attribution"]
        attempt_attribution = latest_attempt["price_attribution"] if latest_attempt else None
        attribution = position_attribution or attempt_attribution
        if attribution == "exact_event":
            attribution_state = "exact_event"
        elif real_open is not None or real_closed is not None or latest_attempt is not None:
            attribution_state = "incomplete"
        else:
            attribution_state = "not_attempted"
        if real_open is not None:
            us_status = "hold"
            us_detail = _hold_detail(
                _pct_change(real_open["entry_price"], real_open["last_quote_price"]),
                m.exec_extremes_pct(m.exec_extremes_live(conn, real_open["id"]),
                                    real_open["entry_price"])["high_pct"])
        elif real_closed is not None:
            us_status = "exited"
            pnl = real_closed["realized_pnl"]
            us_detail = f"{pnl:+.0f}$" if pnl is not None else None
        elif decision in _PASS_REASONS:
            us_status = "passed"
            us_detail = _PASS_REASONS[decision]
        elif decision == "filled":
            us_status = "entered"
            us_detail = None
        else:
            us_status = "watching"
            us_detail = None

        # heat: only meaningful when we're not holding
        heat = "hot" if (
            last_activity_age_s is not None and last_activity_age_s < HEAT_HOT_WINDOW_S
            and not giveback and (runup is None or runup > -20)) else "cooling"

        rows.append({
            "episode_id": ep["episode_id"], "network_id": nid, "mint": mint,
            "chain_label": chain_label(nid),
            "ticker": (first_obs["ticker"] if first_obs else None),
            "phase": ph, "giveback": giveback,
            "us_status": us_status, "us_detail": us_detail, "heat": heat,
            "age_s": _age_s(ep["started_ts"], now_iso),
            "last_activity_age_s": last_activity_age_s,
            "buyers": buyers, "consensus_cluster_count": ep["consensus_cluster_count"],
            "source_quote_drift_pct": src_quote_drift, "runup_pct": runup,
            "latest_price": latest_price, "quote_observed_at": quote_observed_at,
            "quote_age_s": quote_age_s, "quote_state": quote_state,
            "quote_quality": (latest_q["quality"] if latest_q else None),
            "quote_source": (latest_q["dex_id"] if latest_q else None),
            "liquidity": (latest_q["liquidity"] if latest_q else None),
            "volume_24h": (latest_q["volume_24h"] if latest_q else None),
            "mcap": (latest_q["market_cap"] if latest_q else None),
            "route_state": route_state, "attribution_state": attribution_state,
            "source_user_id": source_user_id,
            "source_handle": (first_obs["source_handle"] if first_obs else None),
            "wallet_ab_pnl": (float(wallet_ab["pnl"]) if wallet_ab else None),
            "wallet_ab_closes": (int(wallet_ab["n"]) if wallet_ab else 0),
            "attempt_rating": attempt_rating, "advisory_rating": adv,
            "advisory_reasons": adv_reasons, "effective_rating": eff_rating,
            "signal_source": (ev["signal_source"] if ev else None),
            "claimed_source_age_s": claimed_age,
        })

    def keep(r):
        if source and r["signal_source"] != source:
            return False
        if rating and r["effective_rating"] != rating:
            return False
        if phase and r["phase"] != phase:
            return False
        if fast and not (r["claimed_source_age_s"] is not None
                         and r["claimed_source_age_s"] < 60):
            return False
        return True

    return [r for r in rows if keep(r)]


def coin_timeline(conn, network_id, mint, *, episode_id=None, now_iso=None):
    ep = None
    if episode_id:
        ep = conn.execute(
            "SELECT * FROM memecoin_token_episodes WHERE episode_id=?",
            (episode_id,)).fetchone()
    if ep is None:
        ep = conn.execute(
            "SELECT * FROM memecoin_token_episodes WHERE chain_id=? AND token_address=? "
            "ORDER BY started_ts DESC LIMIT 1", (network_id, mint)).fetchone()

    signals = [{
        "kind": "signal", "side": r["normalized_side"],
        "user": r["source_handle"] or r["source_user_id"],
        "their_price": r["their_price"], "their_size_usd": r["their_size_usd"],
        "claimed_source_ts": r["source_ts"], "observed_at": r["observed_at"],
        "lag_s": r["lag_s"], "curated": bool(r["was_curated"]),
        "real_approved": bool(r["was_real_approved"]),
    } for r in conn.execute(
        "SELECT normalized_side, source_user_id, source_handle, their_price, "
        "their_size_usd, source_ts, observed_at, lag_s, was_curated, was_real_approved "
        "FROM memecoin_feed_observations_v2 WHERE network_id=? AND mint=? "
        "ORDER BY observed_at ASC", (network_id, mint))]

    decisions = [dict(r) for r in conn.execute(
        "SELECT signal_source, action, real_decision, real_decision_reason, "
        "real_decision_at, created_at FROM memecoin_events "
        "WHERE network_id=? AND mint=? ORDER BY created_at ASC", (network_id, mint))]

    holders = [dict(r) for r in conn.execute(
        "SELECT source_user_id, source_handle, evidence_state, remaining_usd, "
        "observed_at, confirmation_state, invalidated_at, invalidation_reason "
        "FROM memecoin_holder_evidence WHERE network_id=? AND mint=? "
        "ORDER BY observed_at ASC", (network_id, mint))]

    arm_positions = [dict(r) for r in conn.execute(
        "SELECT id, strategy, execution_mode, entry_price, last_quote_price, peak_price, "
        "max_gain_pct, realized_pnl, exit_reason, opened_at, closed_at, is_open "
        "FROM memecoin_positions WHERE network_id=? AND mint=? ORDER BY opened_at ASC",
        (network_id, mint))]
    # ONE BASIS PER FIELD. `max_gain_pct` is a display measure and exists on paper rows only; a
    # funded row gets None there, whatever the column holds, and its executable top and bottom
    # from the extremes table instead.
    funded = m.funded_exec_extremes(
        conn, [a["id"] for a in arm_positions if a["execution_mode"] == "real"])
    for arm in arm_positions:
        real = arm["execution_mode"] == "real"
        pct = m.exec_extremes_pct(funded.get(arm["id"]), arm["entry_price"])
        arm["max_gain_pct"] = None if real else arm["max_gain_pct"]
        arm["max_gain_basis"] = None if real else "display"
        arm["exec_high_pct"], arm["exec_low_pct"] = pct["high_pct"], pct["low_pct"]

    rating_row = None
    if ep is not None:
        rating_row = conn.execute(
            "SELECT rating, reason_codes_json, decided_at FROM memecoin_radar_ratings "
            "WHERE episode_id=? ORDER BY created_at DESC LIMIT 1",
            (ep["episode_id"],)).fetchone()
    if rating_row is None:
        rating_row = conn.execute(
            "SELECT rating, reason_codes_json, decided_at FROM memecoin_radar_ratings "
            "WHERE network_id=? AND mint=? ORDER BY created_at DESC LIMIT 1",
            (network_id, mint)).fetchone()

    price_path = [{
        "observed_at": r["observed_at"], "price": r["price"], "market_cap": r["market_cap"],
    } for r in conn.execute(
        "SELECT observed_at, price, market_cap FROM memecoin_quote_observations_v2 "
        "WHERE network_id=? AND mint=? ORDER BY observed_at ASC LIMIT 2000",
        (network_id, mint))]
    if len(price_path) > 300:
        step = len(price_path) // 300 + 1
        price_path = price_path[::step]

    return {
        "network_id": network_id, "mint": mint, "chain_label": chain_label(network_id),
        "episode": (dict(ep) if ep is not None else None),
        "signals": signals, "decisions": decisions, "holders": holders,
        "arm_positions": arm_positions,
        "attempt_rating": (rating_row["rating"] if rating_row else None),
        "attempt_reason_codes": (rating_row["reason_codes_json"] if rating_row else None),
        "price_path": price_path,
    }


def radar_summary(conn, *, window_hours=24, chain=None, source=None, now_iso=None):
    now_iso = _now(now_iso)
    now_dt = _parse_ts(now_iso)
    cutoff_iso = None
    if now_dt is not None and window_hours:
        cutoff_iso = (now_dt - timedelta(hours=float(window_hours))).isoformat()

    ep_q = "SELECT chain_id, token_address FROM memecoin_token_episodes"
    ep_where, ep_params = [], []
    if cutoff_iso:
        ep_where.append("COALESCE(last_signal_ts, started_ts) >= ?"); ep_params.append(cutoff_iso)
    if chain:
        ep_where.append("chain_id = ?"); ep_params.append(str(chain))
    if ep_where:
        ep_q += " WHERE " + " AND ".join(ep_where)
    coins = {(r["chain_id"], r["token_address"]) for r in conn.execute(ep_q, ep_params)}

    r_q = "SELECT network_id, signal_source, rating, COUNT(*) n FROM memecoin_radar_ratings"
    r_where, r_params = [], []
    if cutoff_iso:
        r_where.append("created_at >= ?"); r_params.append(cutoff_iso)
    if chain:
        r_where.append("network_id = ?"); r_params.append(str(chain))
    if source:
        r_where.append("signal_source = ?"); r_params.append(source)
    if r_where:
        r_q += " WHERE " + " AND ".join(r_where)
    r_q += " GROUP BY network_id, signal_source, rating"
    outcomes = {}
    for row in conn.execute(r_q, r_params):
        key = f"{chain_label(row['network_id'])}/{row['signal_source'] or '?'}"
        bucket = outcomes.setdefault(key, {"green": 0, "yellow": 0, "red": 0})
        rk = str(row["rating"] or "").lower()
        if rk in bucket:
            bucket[rk] += row["n"]

    arm_q = ("SELECT strategy, realized_pnl, network_id, mint FROM memecoin_positions "
             "WHERE is_open=0 AND realized_pnl IS NOT NULL")
    arm_params = []
    if cutoff_iso:
        arm_q += " AND closed_at >= ?"; arm_params.append(cutoff_iso)
    if chain:
        arm_q += " AND network_id = ?"; arm_params.append(str(chain))
    per_arm = {}
    for row in conn.execute(arm_q, arm_params):
        if coins and (row["network_id"], row["mint"]) not in coins:
            continue
        per_arm.setdefault(row["strategy"] or "?", []).append(float(row["realized_pnl"]))
    paper_arms = [{
        "arm": name, "closed": len(pnls), "pnl": round(sum(pnls), 2),
        "pnl_drop_top2": drop_top_two(pnls),
    } for name, pnls in sorted(per_arm.items())]

    att_q = ("SELECT COUNT(*) total, SUM(CASE WHEN price_attribution IS NOT NULL "
             "AND quote_id IS NOT NULL AND source_drift_pct IS NOT NULL "
             "AND execution_slippage_pct IS NOT NULL THEN 1 ELSE 0 END) complete "
             "FROM memecoin_positions WHERE execution_mode='real'")
    att_params = []
    if cutoff_iso:
        att_q += " AND opened_at >= ?"; att_params.append(cutoff_iso)
    att = conn.execute(att_q, att_params).fetchone()
    total = att["total"] or 0
    attribution_pct = round(100.0 * (att["complete"] or 0) / total, 1) if total else None

    # Candidate tables are created by the research runner
    # (sniper.research.memecoin_strategy.store.ensure_strategy_tables), not the
    # web conn. If absent, degrade to no candidates rather than error — never
    # CREATE here, so the read-only guarantee holds.
    candidates = []
    try:
        cand_rows = conn.execute(
            "SELECT candidate_id, name, status, gates_json "
            "FROM memecoin_strategy_candidates").fetchall()
    except sqlite3.OperationalError:
        cand_rows = []
    for cand in cand_rows:
        try:
            gates = json.loads(cand["gates_json"] or "{}")
        except (TypeError, ValueError):
            gates = {}
        closed_by_chain = {}
        for row in conn.execute(
            "SELECT network_id, COUNT(*) n FROM memecoin_candidate_positions "
            "WHERE candidate_id=? AND status='closed' GROUP BY network_id",
            (cand["candidate_id"],)):
            closed_by_chain[chain_label(row["network_id"])] = row["n"]
        candidates.append({
            "candidate_id": cand["candidate_id"], "name": cand["name"],
            "status": cand["status"],
            "paired_closes_target": gates.get("paired_closes_per_chain", "30-50"),
            "closed_by_chain": closed_by_chain,
        })

    return {
        "window_hours": window_hours,
        "outcomes_by_chain_source": outcomes,
        "paper_arms": paper_arms,
        "attribution_complete_pct": attribution_pct,
        "candidates": candidates,
    }
