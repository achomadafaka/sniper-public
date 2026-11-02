"""Read-only, episode-deduped grading for real memecoin entries we did not take."""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone


DIAGNOSTIC_POLICY = "diagnostic_60_60_v1"


def _number(value, *, positive=False):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def simulate_first_touch(ref_price, marks, *, tp_pct=60.0, sl_pct=60.0, clip_usd=50.0):
    """Apply one labeled diagnostic rail to observed marks in observation-time order.

    Triggers fill at the actual observed mark, including gap-through. This is a gate-comparison
    diagnostic, not a claim about an executable historical fill.
    """
    ref = _number(ref_price, positive=True)
    clip = _number(clip_usd, positive=True)
    if ref is None or clip is None:
        return None
    usable = []
    for mark in marks or ():
        price = _number(mark.get("price"), positive=True)
        observed = _parse(mark.get("observed_at"))
        if price is not None and observed is not None:
            usable.append((observed, price))
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    returns = [(price / ref - 1.0) * 100.0 for _, price in usable]
    exit_reason = "last_observed"
    exit_return = returns[-1]
    exit_at = usable[-1][0]
    for (observed, _price), ret in zip(usable, returns):
        if ret <= -abs(float(sl_pct)):
            exit_reason, exit_return, exit_at = "stop_loss", ret, observed
            break
        if ret >= abs(float(tp_pct)):
            exit_reason, exit_return, exit_at = "take_profit", ret, observed
            break
    return {
        "policy": DIAGNOSTIC_POLICY,
        "exit_reason": exit_reason,
        "exit_return_pct": round(exit_return, 4),
        "pnl_usd": round(clip * exit_return / 100.0, 2),
        "mfe_pct": round(max(returns), 4),
        "mae_pct": round(min(returns), 4),
        "exit_observed_at": exit_at.isoformat(),
        "marks_used": len(usable),
    }


def _now(now_iso):
    if now_iso is not None:
        parsed = _parse(now_iso)
    else:
        from sniper.util.clock import real_utc_now
        parsed = datetime.fromtimestamp(real_utc_now(), tz=timezone.utc)
    if parsed is None:
        raise ValueError("now_iso must be timezone-aware ISO-8601")
    return parsed


def _event_at(row):
    return (_parse(row.get("real_decision_at")) or
            _parse(row.get("received_at_normalized")) or
            _parse(row.get("created_at")) or datetime.max.replace(tzinfo=timezone.utc))


def grade_missed_entries(conn, *, window_hours=24, chain=None, source=None, now_iso=None):
    """Return one missed opportunity per coin episode plus reason-level aggregates.

    All SQL is SELECT-only. Exact upstream-event outcomes are required for money totals; repeated
    refusals in the same episode never receive repeated counterfactual PnL.
    """
    now = _now(now_iso)
    cutoff = (now - timedelta(hours=max(0.0, float(window_hours)))).isoformat()
    sql = (
        "SELECT e.*, s.episode_id FROM memecoin_events e "
        "LEFT JOIN memecoin_episode_signals s ON s.upstream_event_id=e.fomo_event_id "
        "WHERE e.real_decision IS NOT NULL "
        "AND julianday(COALESCE(e.received_at_normalized,e.real_decision_at,e.created_at))"
        ">=julianday(?) "
        "AND julianday(COALESCE(e.received_at_normalized,e.real_decision_at,e.created_at))"
        "<=julianday(?)"
    )
    params = [cutoff, now.isoformat()]
    if chain:
        sql += " AND e.network_id=?"
        params.append(str(chain))
    if source:
        sql += " AND e.signal_source=?"
        params.append(str(source))
    sql += " ORDER BY COALESCE(e.real_decision_at,e.received_at_normalized,e.created_at),e.id"
    events = [dict(row) for row in conn.execute(sql, params)]

    grouped = defaultdict(list)
    for event in events:
        network = str(event.get("network_id") or "")
        mint = event.get("mint")
        episode_id = event.get("episode_id")
        fallback = f"legacy:{event.get('fomo_event_id') or event.get('id')}"
        grouped[(network, mint, episode_id or fallback)].append(event)

    episodes = []
    later_filled_excluded = 0
    for (network, mint, episode_key), rows in grouped.items():
        rows.sort(key=_event_at)
        if any(str(row.get("real_decision")) == "filled" for row in rows):
            later_filled_excluded += 1
            continue
        event = rows[0]
        event_id = event.get("fomo_event_id")
        outcome = conn.execute(
            "SELECT * FROM memecoin_signal_outcomes_v2 WHERE upstream_event_id=?",
            (event_id,),
        ).fetchone() if event_id else None
        marks = []
        if outcome is not None:
            marks = [dict(mark) for mark in conn.execute(
                "SELECT observed_at,price,quality,target_s,actual_age_s FROM "
                "memecoin_horizon_marks_v2 WHERE outcome_id=? AND status='observed' "
                "AND price IS NOT NULL ORDER BY observed_at,id",
                (outcome["outcome_id"],),
            )]
        simulation = simulate_first_touch(
            outcome["ref_price"] if outcome is not None else event.get("their_price"), marks,
        ) if marks else None
        coverage_status = (str(outcome["status"]) if outcome is not None and marks
                           else "coverage_missing")
        mfe = simulation["mfe_pct"] if simulation else None
        final_return = None
        if outcome is not None and marks:
            ref = _number(outcome["ref_price"], positive=True)
            last = _number(marks[-1]["price"], positive=True)
            if ref is not None and last is not None:
                final_return = round((last / ref - 1.0) * 100.0, 4)
        episodes.append({
            "episode_id": event.get("episode_id") or episode_key,
            "network_id": network,
            "mint": mint,
            "ticker": event.get("ticker"),
            "signal_source": event.get("signal_source") or "unknown",
            "source_user_id": event.get("source_user_id"),
            "source_handle": event.get("source_handle"),
            "origin_event_id": event_id,
            "terminal_status": event.get("real_decision"),
            "terminal_reason": event.get("real_decision_reason") or event.get("real_decision"),
            "decided_at": event.get("real_decision_at"),
            "entry_mcap": event.get("their_market_cap"),
            "coverage_status": coverage_status,
            "observed_marks": len(marks),
            "mfe_pct": mfe,
            "final_return_pct": final_return,
            "hit20": (mfe is not None and mfe >= 20.0),
            "hit50": (mfe is not None and mfe >= 50.0),
            "simulation": simulation,
        })

    aggregates = {}
    avoided = forgone = 0.0
    covered = 0
    for row in episodes:
        key = (row["network_id"], row["signal_source"], row["terminal_reason"])
        bucket = aggregates.setdefault(key, {
            "network_id": row["network_id"], "signal_source": row["signal_source"],
            "terminal_reason": row["terminal_reason"], "episodes": 0,
            "covered": 0, "avoided_loss_usd": 0.0, "forgone_gain_usd": 0.0,
        })
        bucket["episodes"] += 1
        simulation = row["simulation"]
        if simulation is None:
            continue
        covered += 1
        bucket["covered"] += 1
        pnl = float(simulation["pnl_usd"])
        if pnl < 0:
            loss = -pnl
            avoided += loss
            bucket["avoided_loss_usd"] += loss
        elif pnl > 0:
            forgone += pnl
            bucket["forgone_gain_usd"] += pnl
    by_reason = []
    for bucket in aggregates.values():
        bucket["avoided_loss_usd"] = round(bucket["avoided_loss_usd"], 2)
        bucket["forgone_gain_usd"] = round(bucket["forgone_gain_usd"], 2)
        by_reason.append(bucket)
    by_reason.sort(key=lambda row: (
        row["network_id"], row["signal_source"], row["terminal_reason"],
    ))
    return {
        "policy": DIAGNOSTIC_POLICY,
        "window_hours": float(window_hours),
        "summary": {
            "deduped_episodes": len(episodes),
            "covered_episodes": covered,
            "coverage_pct": round(100.0 * covered / len(episodes), 1) if episodes else None,
            "later_filled_excluded": later_filled_excluded,
            "avoided_loss_usd": round(avoided, 2),
            "forgone_gain_usd": round(forgone, 2),
        },
        "by_reason": by_reason,
        "episodes": episodes,
    }
