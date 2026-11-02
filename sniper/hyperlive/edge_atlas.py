"""Deterministic, read-only decomposition for the Hyperliquid Edge Desk.

The Atlas reports where historical paper results concentrated, whether the fixed cell still has
recent support, and which exit path explains the copy-versus-standalone gap. It does not select
trades, mutate a ledger, or call Hyperliquid.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sniper.hyperlive.altshort_evidence import block_bootstrap_mean
from sniper.hyperlive.altshort_universe import is_alt_short, strategy_bucket
from sniper.hyperlive import intel_store as IS


DAY_MS = 86_400_000
RECENT_DAYS = 7
MIN_RECENT_CLOSES = 30
MIN_FORWARD_CLOSES = 30
CELL_ORDER = (
    ("core_long", "core", "long"),
    ("core_short", "core", "short"),
    ("alt_long", "alt", "long"),
    ("alt_short", "alt", "short"),
)
HOLD_BUCKET_ORDER = ("<1h", "1-4h", "4-12h", "12-48h", "48h+")


def _value(row: dict, key: str) -> float | None:
    raw = row.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _timestamp(row: dict, key: str = "exit_ts_ms") -> int | None:
    raw = row.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _summary(rows: list[dict], *, value_key: str) -> dict:
    scored = []
    day_rows = []
    for row in rows:
        value = _value(row, value_key)
        if value is None:
            continue
        scored.append(value)
        ts_ms = _timestamp(row)
        if ts_ms is not None:
            day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()
            day_rows.append({"day": day, "value": value})
    if not scored:
        return {"n": 0}

    day_blocks = len({row["day"] for row in day_rows})
    ci = [None, None]
    if day_blocks >= 3:
        inference = block_bootstrap_mean(
            day_rows,
            cluster_key="day",
            value_key="value",
            replications=2_000,
            seed=17,
        )
        ci = [round(float(value), 2) for value in inference["ci95"]]
    return {
        "n": len(scored),
        "mean_bps": round(statistics.fmean(scored), 2),
        "median_bps": round(statistics.median(scored), 2),
        "sum_bps": round(sum(scored), 1),
        "win_rate": round(sum(value > 0 for value in scored) / len(scored), 3),
        "day_blocks": day_blocks,
        "ci95_bps": ci,
    }


def _recent(rows: list[dict], *, now_ms: int) -> list[dict]:
    cutoff = int(now_ms) - RECENT_DAYS * DAY_MS
    return [
        row for row in rows
        if (ts_ms := _timestamp(row)) is not None and cutoff <= ts_ms <= int(now_ms)
    ]


def _week_start(ts_ms: int) -> int:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    start = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) - timedelta(days=dt.weekday())
    return int(start.timestamp() * 1000)


def _weekly(rows: list[dict], *, value_key: str) -> list[dict]:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        ts_ms = _timestamp(row)
        if ts_ms is not None and _value(row, value_key) is not None:
            grouped[_week_start(ts_ms)].append(row)
    out = []
    for start_ms in sorted(grouped)[-6:]:
        dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        out.append({
            "week": f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}",
            "week_start_ts_ms": start_ms,
            **_summary(grouped[start_ms], value_key=value_key),
        })
    return out


def _recent_status(summary: dict) -> str:
    if not summary.get("n"):
        return "no_history"
    if int(summary["n"]) < MIN_RECENT_CLOSES:
        return "insufficient_recent"
    ci_lo = (summary.get("ci95_bps") or [None])[0]
    if (
        float(summary.get("mean_bps") or 0) > 0
        and float(summary.get("median_bps") or 0) > 0
        and ci_lo is not None
        and float(ci_lo) > 0
    ):
        return "recently_supported"
    return "degraded"


def _hold_bucket(row: dict) -> str | None:
    start = _timestamp(row, "detect_ts_ms")
    end = _timestamp(row, "exit_ts_ms")
    if start is None or end is None or end < start:
        return None
    hours = (end - start) / 3_600_000
    if hours < 1:
        return "<1h"
    if hours < 4:
        return "1-4h"
    if hours < 12:
        return "4-12h"
    if hours < 48:
        return "12-48h"
    return "48h+"


def _forward(rows: list[dict] | None) -> dict:
    if rows is None:
        return {
            "open": None,
            "closed": None,
            "total": None,
            "status": "unknown",
            "confirmation": "current_arm_unverified",
        }
    open_n = sum(str(row.get("status") or "").lower() == "open" for row in rows)
    closed_n = sum(str(row.get("status") or "").lower() == "closed" for row in rows)
    total = open_n + closed_n
    if closed_n >= MIN_FORWARD_CLOSES:
        status = "sample_ready"
        confirmation = "requires_regime_replication"
    elif total:
        status = "accruing"
        confirmation = "insufficient_forward_closes"
    else:
        status = "not_started"
        confirmation = "no_forward_closes"
    return {
        "open": open_n,
        "closed": closed_n,
        "total": total,
        "status": status,
        "confirmation": confirmation,
    }


def build_edge_atlas(
    follow_rows: list[dict],
    copy_rows: list[dict],
    current_arm_rows: list[dict] | None,
    *,
    now_ms: int,
) -> dict:
    """Build the fixed Edge Atlas from already-loaded ledger rows."""
    eligible: dict[tuple[str, str], list[dict]] = defaultdict(list)
    excluded = 0
    for row in follow_rows:
        bucket = strategy_bucket(row.get("coin"))
        side = str(row.get("side") or "").strip().lower()
        if bucket not in {"core", "alt"} or side not in {"long", "short"}:
            excluded += 1
            continue
        if _value(row, "net_bps") is None:
            excluded += 1
            continue
        eligible[(bucket, side)].append(row)

    cells = []
    for cell_id, bucket, side in CELL_ORDER:
        rows = eligible[(bucket, side)]
        cells.append({
            "id": cell_id,
            "universe": bucket,
            "side": side,
            "history": _summary(rows, value_key="net_bps"),
            "recent_7d": _summary(_recent(rows, now_ms=now_ms), value_key="net_bps"),
        })

    alt_short = eligible[("alt", "short")]
    recent_alt_short = _recent(alt_short, now_ms=now_ms)
    full_summary = _summary(alt_short, value_key="net_bps")
    recent_summary = _summary(recent_alt_short, value_key="net_bps")
    data_ts = [_timestamp(row) for row in alt_short]
    data_ts = [value for value in data_ts if value is not None]

    by_reason_rows = {"mirror_exit": [], "time_cap": [], "other": []}
    hold_rows = {name: [] for name in HOLD_BUCKET_ORDER}
    for row in alt_short:
        reason = str(row.get("exit_reason") or "").strip().lower()
        reason_key = reason if reason in {"mirror_exit", "time_cap"} else "other"
        by_reason_rows[reason_key].append(row)
        if (bucket := _hold_bucket(row)) is not None:
            hold_rows[bucket].append(row)

    clean_copy = [
        row for row in copy_rows
        if is_alt_short(row.get("coin"), row.get("side"))
        and not bool(row.get("is_cold_start"))
        and _value(row, "pnl_bps") is not None
    ]
    recent_clean_copy = _recent(clean_copy, now_ms=now_ms)

    return {
        "available": True,
        "methodology": {
            "matrix": "fixed_strategy_universe_x_side",
            "pnl": "persisted_net_of_modeled_costs",
            "inference": "calendar_day_block_bootstrap",
            "recent_window_days": RECENT_DAYS,
            "warning": "Descriptive research evidence; not a trade trigger or promotion gate.",
        },
        "cells": cells,
        "excluded_rows": excluded,
        "focus": {
            "cell_id": "alt_short",
            "status": _recent_status(recent_summary if recent_summary.get("n") else {"n": 0}),
            "history": full_summary,
            "recent_7d": recent_summary,
            "weekly": _weekly(alt_short, value_key="net_bps"),
            "data_through_ts_ms": max(data_ts) if data_ts else None,
            "last_close_age_ms": int(now_ms) - max(data_ts) if data_ts else None,
        },
        "exit_gap": {
            "by_reason": {
                key: _summary(rows, value_key="net_bps")
                for key, rows in by_reason_rows.items()
            },
            "hold_buckets": {
                key: _summary(hold_rows[key], value_key="net_bps")
                for key in HOLD_BUCKET_ORDER
            },
            "hold_time_warning": (
                "Outcome attribution only; hold duration is unknown at entry and is not an entry filter."
            ),
        },
        "copy_comparison": {
            "recent_generic_follow": recent_summary,
            "recent_clean_copy": _summary(recent_clean_copy, value_key="pnl_bps"),
            "copy_definition": "alt-short, closed, non-cold-start paper copies",
            "warning": (
                "Closed-copy result excludes open positions and may be exit-censored; "
                "the retained open book is reported separately below."
            ),
        },
        "forward": _forward(current_arm_rows),
        "next_direction": {
            "priority": "exit_replication",
            "question": (
                "Can source-directed wallet exits preserve the alt-short result forward by "
                "avoiding the time-cap and 12-48 hour loss tail?"
            ),
            "milestone": (
                "Accrue current-arm closes, separate source exits from fallback exits, and require "
                "at least two distinct regime episodes before promotion."
            ),
            "warning": (
                "Wallet and coin drill-downs are multiple-tested replication candidates, not rules."
            ),
        },
    }


def read_edge_atlas(
    conn: sqlite3.Connection,
    *,
    current_arm_id: str | None,
    now_ms: int,
) -> dict:
    """Read only the fixed ledgers required by the Atlas; never migrate missing schema."""
    try:
        follow_rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM hl_follow_positions WHERE status='closed' AND net_bps IS NOT NULL"
            )
        ]
        copy_rows = [
            dict(row) for row in conn.execute(
                "SELECT * FROM hl_copy_book WHERE status='closed' AND pnl_bps IS NOT NULL"
            )
        ]
        arm_rows = None
        if current_arm_id:
            arm_ids = IS.compatible_altshort_arm_ids(conn, arm_id=current_arm_id)
            placeholders = ",".join("?" for _ in arm_ids)
            arm_rows = [
                dict(row) for row in conn.execute(
                    "SELECT status FROM hl_altshort_observations "
                    f"WHERE arm_id IN ({placeholders})",
                    arm_ids,
                )
            ]
    except sqlite3.Error:
        return {"available": False, "reason": "schema_missing"}
    return build_edge_atlas(follow_rows, copy_rows, arm_rows, now_ms=now_ms)
