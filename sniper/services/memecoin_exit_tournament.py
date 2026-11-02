"""Read-only exact-origin exit-policy tournament."""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone


_ARMS = ("control", "real_shadow", "scalp_c2", "peak_lock_e")
_CHALLENGERS = _ARMS[1:]
_WINNER_SWALLOW_MAX_PCT = 10.0


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


def _metrics(rows):
    values = [float(row["realized_pnl"]) for row in rows]
    return {
        "n": len(values),
        "pnl": round(sum(values), 2),
        "win_rate": round(sum(value > 0 for value in values) / len(values), 4)
        if values else None,
        "median_pnl": round(float(statistics.median(values)), 2) if values else None,
        "worst_pnl": round(min(values), 2) if values else None,
        "drop_top_two_pnl": round(_drop_top_two(values), 2) if values else None,
    }


def _mcap_band(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(value) or value <= 0:
        return "unknown"
    if value < 250_000:
        return "under_250k"
    if value < 1_000_000:
        return "250k_1m"
    if value < 5_000_000:
        return "1m_5m"
    if value < 30_000_000:
        return "5m_30m"
    return "30m_or_more"


def _lag_band(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if not math.isfinite(value) or value < 0:
        return "unknown"
    return "under_60s" if value < 60 else "60s_or_more"


def _consensus_band(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if value < 0:
        return "unknown"
    return "consensus" if value >= 2 else "solo"


def _readiness(control_metrics, challenger_metrics, paired_rows, min_paired):
    paired_n = len(paired_rows)
    control_winners = sum(control["realized_pnl"] > 0 for control, _ in paired_rows)
    swallowed = sum(
        control["realized_pnl"] > 0 and challenger["realized_pnl"] <= 0
        for control, challenger in paired_rows
    )
    swallow_rate = 100.0 * swallowed / control_winners if control_winners else 0.0
    if paired_n < min_paired:
        return ("insufficient", [f"paired_closes_below_{min_paired}"], swallowed,
                swallow_rate)
    pnl_delta = challenger_metrics["pnl"] - control_metrics["pnl"]
    drop_delta = (challenger_metrics["drop_top_two_pnl"]
                  - control_metrics["drop_top_two_pnl"])
    reasons = []
    if pnl_delta <= 0:
        reasons.append("pnl_delta_nonpositive")
    if drop_delta <= 0:
        reasons.append("drop_top_two_delta_nonpositive")
    if swallow_rate > _WINNER_SWALLOW_MAX_PCT:
        reasons.append("winner_swallow_over_10pct")
    if reasons:
        return "rejected", reasons, swallowed, swallow_rate
    return "shadow_candidate", [
        "positive_paired_delta", "positive_drop_top_two_delta", "winner_swallow_within_10pct",
    ], swallowed, swallow_rate


def _protected_hold_counter(conn, cutoff, now_iso, chain):
    params = [cutoff, now_iso]
    chain_sql = ""
    if chain:
        chain_sql = " AND p.network_id=?"
        params.append(str(chain))
    rows = conn.execute(
        "SELECT a.allowed, a.detail, p.network_id FROM memecoin_exit_trigger_audit a "
        "JOIN memecoin_positions p ON p.id=a.position_id "
        "WHERE a.reason='protected_hold_shadow' "
        "AND julianday(a.checked_at)>=julianday(?) AND julianday(a.checked_at)<=julianday(?)"
        + chain_sql + " ORDER BY a.checked_at, a.id",
        params,
    ).fetchall()
    by_chain = defaultdict(lambda: {"evaluations": 0, "would_protect": 0})
    would_protect = 0
    funded_suppressed = 0
    for row in rows:
        network_id = str(row["network_id"] or "unresolved")
        allowed = bool(row["allowed"])
        would_protect += int(allowed)
        by_chain[network_id]["evaluations"] += 1
        by_chain[network_id]["would_protect"] += int(allowed)
        try:
            detail = json.loads(row["detail"] or "{}")
        except (TypeError, ValueError):
            detail = {}
        funded_suppressed += int(bool(detail.get("funded_stop_suppressed")))
    return {
        "evaluations": len(rows),
        "would_protect": would_protect,
        "would_not_protect": len(rows) - would_protect,
        "funded_stops_suppressed": funded_suppressed,
        "by_chain": dict(sorted(by_chain.items())),
    }


def exit_policy_tournament(conn, *, window_days=30, chain=None, min_paired=30,
                           now_iso=None):
    """Pair closed paper policies by exact ``origin_event_id`` and segment entry facts."""
    try:
        window_days = float(window_days)
        min_paired = int(min_paired)
    except (TypeError, ValueError) as exc:
        raise ValueError("window_days and min_paired must be positive") from exc
    if not math.isfinite(window_days) or window_days <= 0 or min_paired <= 0:
        raise ValueError("window_days and min_paired must be positive")
    now = _now(now_iso)
    cutoff = (now - timedelta(days=window_days)).isoformat()
    params = [*_ARMS, cutoff, now.isoformat()]
    chain_sql = ""
    if chain:
        chain_sql = " AND p.network_id=?"
        params.append(str(chain))
    rows = conn.execute(
        "SELECT p.id, p.network_id, p.strategy, p.origin_event_id, p.realized_pnl, "
        "p.entry_mcap, p.entry_lag_s, p.closed_at FROM memecoin_positions p "
        "WHERE p.strategy IN (?,?,?,?) AND COALESCE(p.execution_mode,'paper')='paper' "
        "AND p.is_open=0 AND p.realized_pnl IS NOT NULL AND p.closed_at IS NOT NULL "
        "AND julianday(p.closed_at)>=julianday(?) AND julianday(p.closed_at)<=julianday(?)"
        + chain_sql + " ORDER BY julianday(p.closed_at), p.id",
        params,
    ).fetchall()

    legacy_unattributed = 0
    unresolved_network = 0
    attribution = defaultdict(lambda: {"total_closes": 0, "exact_origin_closes": 0})
    raw_events = defaultdict(lambda: defaultdict(list))
    for row in rows:
        network_id = str(row["network_id"] or "")
        if not network_id:
            unresolved_network += 1
            continue
        attr = attribution[(network_id, row["strategy"])]
        attr["total_closes"] += 1
        event_id = str(row["origin_event_id"] or "").strip()
        if not event_id:
            legacy_unattributed += 1
            continue
        attr["exact_origin_closes"] += 1
        raw_events[(network_id, event_id)][row["strategy"]].append(row)

    exact_events = {}
    duplicate_rows_excluded = 0
    for event_key, by_arm in raw_events.items():
        clean = {}
        for arm, matches in by_arm.items():
            if len(matches) == 1:
                clean[arm] = matches[0]
            else:
                duplicate_rows_excluded += len(matches)
        if clean:
            exact_events[event_key] = clean

    segments = defaultdict(dict)
    for (network_id, event_id), by_arm in exact_events.items():
        control = by_arm.get("control")
        if control is None:
            continue
        snapshot = conn.execute(
            "SELECT mcap, consensus_n_60m FROM memecoin_entry_snapshots "
            "WHERE upstream_event_id=? LIMIT 1",
            (event_id,),
        ).fetchone()
        mcap = snapshot["mcap"] if snapshot and snapshot["mcap"] is not None else control["entry_mcap"]
        consensus = snapshot["consensus_n_60m"] if snapshot else None
        segment_key = (
            network_id, _mcap_band(mcap), _lag_band(control["entry_lag_s"]),
            _consensus_band(consensus),
        )
        segments[segment_key][event_id] = by_arm

    out_segments = []
    for segment_key in sorted(segments):
        events = segments[segment_key]
        comparisons = []
        for challenger in _CHALLENGERS:
            paired = [
                (by_arm["control"], by_arm[challenger])
                for _, by_arm in sorted(events.items())
                if "control" in by_arm and challenger in by_arm
            ]
            if not paired:
                continue
            control_metrics = _metrics([pair[0] for pair in paired])
            challenger_metrics = _metrics([pair[1] for pair in paired])
            readiness, reasons, swallowed, swallow_rate = _readiness(
                control_metrics, challenger_metrics, paired, min_paired,
            )
            if segment_key[0] == "evm-unknown":
                readiness, reasons = "insufficient", ["unresolved_network_identity"]
            comparisons.append({
                "challenger": challenger,
                "paired_closes": len(paired),
                "control": control_metrics,
                "challenger_metrics": challenger_metrics,
                "pnl_delta": round(challenger_metrics["pnl"] - control_metrics["pnl"], 2),
                "drop_top_two_delta": round(
                    challenger_metrics["drop_top_two_pnl"]
                    - control_metrics["drop_top_two_pnl"], 2,
                ),
                "winner_swallow_count": swallowed,
                "winner_swallow_rate": round(swallow_rate, 1),
                "readiness": readiness,
                "reason_codes": reasons,
            })
        if not comparisons:
            continue
        all_four = sum(all(arm in by_arm for arm in _ARMS) for by_arm in events.values())
        out_segments.append({
            "segment": {
                "network_id": segment_key[0], "mcap_band": segment_key[1],
                "entry_lag_band": segment_key[2], "consensus_band": segment_key[3],
            },
            "exact_control_events": len(events),
            "exact_attribution_pct": 100.0,
            "all_four_matched": all_four,
            "comparisons": comparisons,
        })

    attribution_rows = []
    for (network_id, arm), counts in sorted(attribution.items()):
        total = counts["total_closes"]
        attribution_rows.append({
            "network_id": network_id,
            "arm": arm,
            **counts,
            "exact_attribution_pct": round(100.0 * counts["exact_origin_closes"] / total, 1)
            if total else None,
        })
    return {
        "window_days": window_days,
        "min_paired": min_paired,
        "exact_origin_only": True,
        "legacy_matching_used": False,
        "winner_swallow_max_pct": _WINNER_SWALLOW_MAX_PCT,
        "segments": out_segments,
        "attribution_by_chain_arm": attribution_rows,
        "legacy_unattributed_closes": legacy_unattributed,
        "unresolved_network_closes": unresolved_network,
        "duplicate_rows_excluded": duplicate_rows_excluded,
        "protected_hold_shadow": _protected_hold_counter(
            conn, cutoff, now.isoformat(), chain,
        ),
    }
