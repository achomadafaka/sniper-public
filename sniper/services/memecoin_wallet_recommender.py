"""Read-only, exact-event wallet recommendation evidence."""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone


_ARMS = ("control", "real_shadow", "scalp_c2")
_PRIMARY_ARMS = ("control", "real_shadow")
_OVERLAP_MIN_PCT = 75.0


def _parse_ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _now(now_iso):
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()
    parsed = _parse_ts(now_iso)
    if parsed is None:
        raise ValueError("now_iso must be an aware ISO timestamp")
    return parsed


def _drop_top_two(values):
    return sum(sorted((float(value) for value in values), reverse=True)[2:])


def _max_drawdown(ordered_values):
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for value in ordered_values:
        equity += float(value)
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _metrics(rows):
    rows = sorted(rows, key=lambda row: (row["closed_at"], row["id"]))
    values = [float(row["realized_pnl"]) for row in rows]
    pnl_by_coin = defaultdict(float)
    for row in rows:
        pnl_by_coin[(row["network_id"], row["mint"])] += float(row["realized_pnl"])
    return {
        "n": len(values),
        "distinct_coins": len(pnl_by_coin),
        "pnl": round(sum(values), 2),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4)
        if values else None,
        "median_pnl": round(float(statistics.median(values)), 2) if values else None,
        "max_drawdown": round(_max_drawdown(values), 2) if values else None,
        "drop_top_two_pnl": round(_drop_top_two(pnl_by_coin.values()), 2)
        if values else None,
        "worst_pnl": round(min(values), 2) if values else None,
    }


def wallet_recommendations(conn, *, min_paired=5, window_days=30, chain=None,
                           now_iso=None, include_inactive=False, min_distinct_coins=3):
    """Compare exact-origin paper control/shadow closes per wallet and chain.

    C2 is reported only where it shares a primary paired event and can never
    create a recommendation row by itself. No writer is called by this module.
    """
    try:
        min_paired = int(min_paired)
        window_days = float(window_days)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_paired and window_days must be positive") from exc
    if min_paired <= 0 or not math.isfinite(window_days) or window_days <= 0:
        raise ValueError("min_paired and window_days must be positive")
    now = _now(now_iso)
    cutoff = (now - timedelta(days=window_days)).isoformat()

    try:
        min_distinct_coins = int(min_distinct_coins)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_distinct_coins must be positive") from exc
    if min_distinct_coins <= 0:
        raise ValueError("min_distinct_coins must be positive")

    params = [*_ARMS, cutoff, now.isoformat()]
    chain_sql = ""
    if chain:
        chain_sql = " AND network_id=?"
        params.append(str(chain))
    roster_sql = "" if include_inactive else (
        " AND source_user_id IN (SELECT source_user_id FROM memecoin_wallets "
        "WHERE COALESCE(excluded,0)=0)"
    )
    rows = conn.execute(
        "SELECT id, source_user_id, source_handle, network_id, mint, strategy, origin_event_id, "
        "realized_pnl, closed_at FROM memecoin_positions "
        "WHERE strategy IN (?,?,?) AND COALESCE(execution_mode,'paper')='paper' "
        "AND is_open=0 AND realized_pnl IS NOT NULL AND closed_at IS NOT NULL "
        "AND julianday(closed_at)>=julianday(?) AND julianday(closed_at)<=julianday(?) "
        "AND id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo')"
        + roster_sql + chain_sql + " ORDER BY julianday(closed_at), id",
        params,
    ).fetchall()

    legacy_counts = defaultdict(int)
    unresolved_network_closes = 0
    exact = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    handles = {}
    for row in rows:
        uid = str(row["source_user_id"] or "")
        network_id = str(row["network_id"] or "")
        if not network_id:
            unresolved_network_closes += 1
            continue
        key = (uid, network_id)
        handles[key] = row["source_handle"] or handles.get(key)
        event_id = str(row["origin_event_id"] or "").strip()
        if not event_id:
            legacy_counts[key] += 1
            continue
        exact[key][event_id][row["strategy"]].append(row)

    recommendations = []
    duplicate_rows_excluded = 0
    for key, events in exact.items():
        uid, network_id = key
        valid_by_arm = {arm: {} for arm in _ARMS}
        for event_id, by_arm in events.items():
            for arm in _ARMS:
                matches = by_arm.get(arm, [])
                if len(matches) == 1:
                    valid_by_arm[arm][event_id] = matches[0]
                elif len(matches) > 1:
                    duplicate_rows_excluded += len(matches)
        control_events = set(valid_by_arm["control"])
        shadow_events = set(valid_by_arm["real_shadow"])
        primary_events = control_events & shadow_events
        if not primary_events:
            continue
        primary_order = sorted(
            primary_events,
            key=lambda event_id: (
                valid_by_arm["control"][event_id]["closed_at"],
                valid_by_arm["control"][event_id]["id"],
            ),
        )
        all_three = primary_events & set(valid_by_arm["scalp_c2"])
        union_count = len(control_events | shadow_events)
        overlap_pct = 100.0 * len(primary_events) / union_count if union_count else 0.0
        metrics = {
            "control": _metrics([valid_by_arm["control"][event] for event in primary_order]),
            "real_shadow": _metrics([
                valid_by_arm["real_shadow"][event] for event in primary_order
            ]),
            "scalp_c2": _metrics([
                valid_by_arm["scalp_c2"][event] for event in primary_order
                if event in all_three
            ]),
        }

        paired_n = len(primary_events)
        control = metrics["control"]
        shadow = metrics["real_shadow"]
        distinct_coins = len({
            (valid_by_arm["control"][event]["network_id"],
             valid_by_arm["control"][event]["mint"])
            for event in primary_events
        })
        if paired_n >= min_paired and control["pnl"] <= 0 and shadow["pnl"] <= 0:
            recommendation = "avoid"
            reason_codes = ["both_primary_arms_nonpositive"]
        else:
            reason_codes = []
            if paired_n < min_paired:
                reason_codes.append("thin_primary_overlap")
            if overlap_pct < _OVERLAP_MIN_PCT:
                reason_codes.append("exact_overlap_below_75pct")
            if control["pnl"] <= 0:
                reason_codes.append("control_nonpositive")
            if shadow["pnl"] <= 0:
                reason_codes.append("real_shadow_nonpositive")
            if control["drop_top_two_pnl"] <= 0:
                reason_codes.append("control_tail_dependent")
            if shadow["drop_top_two_pnl"] <= 0:
                reason_codes.append("real_shadow_tail_dependent")
            recommendation = "watch" if reason_codes else "promote"
            if recommendation == "promote":
                reason_codes = ["robust_primary_evidence"]
        if distinct_coins < min_distinct_coins:
            if recommendation == "promote":
                recommendation = "watch"
                reason_codes = []
            reason_codes.append("thin_coin_diversity")
        if network_id == "evm-unknown":
            recommendation = "watch"
            reason_codes = ["unresolved_network_identity", *(
                code for code in reason_codes if code != "robust_primary_evidence"
            )]
        # Legacy summaries remain inspectable but cannot promote or demote a wallet.
        diagnostic_reason_codes = list(reason_codes)
        diagnostic_recommendation = recommendation
        recommendation = 'watch'
        reason_codes = ['protocol_missing_or_legacy']
        recommendations.append({
            "source_user_id": uid,
            "source_handle": handles.get(key),
            "network_id": network_id,
            "recommendation": recommendation,
            "diagnostic_recommendation": diagnostic_recommendation,
            "diagnostic_reason_codes": diagnostic_reason_codes,
            "research_eligible": False,
            "reason_codes": reason_codes,
            "matched_primary": paired_n,
            "distinct_coins": distinct_coins,
            "all_three_matched": len(all_three),
            "control_exact_events": len(control_events),
            "real_shadow_exact_events": len(shadow_events),
            "exact_overlap_pct": round(overlap_pct, 1),
            "legacy_unattributed_closes": legacy_counts.get(key, 0),
            "metrics": metrics,
        })

    rank = {"promote": 0, "watch": 1, "avoid": 2}
    recommendations.sort(key=lambda row: (
        rank[row["recommendation"]], -row["matched_primary"], row["network_id"],
        row["source_user_id"],
    ))
    summary = {name: 0 for name in ("promote", "watch", "avoid")}
    for row in recommendations:
        summary[row["recommendation"]] += 1
    return {
        "window_days": window_days,
        "min_paired": min_paired,
        "min_distinct_coins": min_distinct_coins,
        "exact_event_only": True,
        "honest_only": True,
        "include_inactive": bool(include_inactive),
        "overlap_min_pct": _OVERLAP_MIN_PCT,
        "recommendations": recommendations,
        "summary": summary,
        "diagnostic_summary": {name: sum(r["diagnostic_recommendation"] == name for r in recommendations) for name in summary},
        "legacy_unattributed_closes": sum(legacy_counts.values()),
        "unresolved_network_closes": unresolved_network_closes,
        "duplicate_rows_excluded": duplicate_rows_excluded,
    }
