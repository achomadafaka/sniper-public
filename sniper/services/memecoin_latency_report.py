"""Read-only aggregation of monotonic memecoin entry stages and EVM substages."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone


TOP_LEVEL_STAGES = (
    "chain_resolution_ms", "market_data_ms", "reservation_ms", "safety_checks_ms",
    "quote_ms", "signing_ms", "transaction_submitted_ms", "chain_confirmation_ms",
    "balance_delta_ms", "persistence_ms", "total_ms",
)


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _percentile(values, quantile):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(float(quantile) * len(values)) - 1))
    return round(values[index], 3)


def _summary(key, values, *, substage=False, measured=None):
    network_id, terminal_status, name = key
    out = {
        "network_id": network_id,
        "terminal_status": terminal_status,
        ("substage" if substage else "stage"): name,
        "n": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "thin": len(values) < 5,
    }
    if not substage:
        # Whether these durations are the stage's own work or the gap since the previous mark.
        # Nine of the ten funded-entry stages infer, so a p50 here is NOT comparable to a measured
        # one and the stages do NOT sum to a budget. Substage events always carry real durations.
        out["measured"] = measured
    return out


def latency_report(conn, *, window_hours=168, chain=None, now_iso=None):
    if now_iso is None:
        from sniper.util.clock import real_utc_now_iso
        now_iso = real_utc_now_iso()
    now = _parse(now_iso)
    if now is None:
        raise ValueError("now_iso must be timezone-aware ISO-8601")
    cutoff = (now - timedelta(hours=max(0.0, float(window_hours)))).isoformat()
    sql = ("SELECT * FROM memecoin_entry_attempts WHERE "
           "julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?)")
    params = [cutoff, now.isoformat()]
    if chain:
        sql += " AND network_id=?"
        params.append(str(chain))
    rows = [dict(row) for row in conn.execute(sql, params)]
    top = defaultdict(list)
    substages = defaultdict(list)
    measured_flags = {}
    invalid = 0
    for row in rows:
        group = (str(row.get("network_id") or "unknown"),
                 str(row.get("terminal_status") or "open"))
        for stage in TOP_LEVEL_STAGES:
            value = row.get(stage)
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value >= 0:
                top[(*group, stage)].append(value)
        raw = row.get("trace_json")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid += 1
            continue
        for stage_name, stage_data in (payload.get("stages") or {}).items():
            flag = (stage_data or {}).get("measured")
            if not isinstance(flag, bool):
                continue                      # pre-2026-09-03 rows carry no flag -> stays unknown
            key = (*group, "%s_ms" % stage_name)
            measured_flags[key] = flag if key not in measured_flags else (
                measured_flags[key] and flag)
        for event in payload.get("substage_events") or ():
            name = str((event or {}).get("name") or "").strip()
            try:
                duration = float((event or {}).get("duration_ms"))
            except (TypeError, ValueError):
                continue
            if name and math.isfinite(duration) and duration >= 0:
                substages[(*group, name)].append(duration)
    return {
        "window_hours": float(window_hours),
        "attempts": len(rows),
        "invalid_trace_json": invalid,
        "top_level": [_summary(key, values, measured=measured_flags.get(key))
                      for key, values in sorted(top.items())],
        "substages": [_summary(key, values, substage=True)
                      for key, values in sorted(substages.items())],
    }
