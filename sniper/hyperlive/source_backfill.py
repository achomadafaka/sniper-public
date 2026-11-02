"""Strict reconstruction of legacy copy entry event times from public provider fills.

Historical exchange event time can sometimes be recovered. Historical receive time cannot.
This module is pure except for the injected fetch callable and never creates latency samples.
"""
from __future__ import annotations


PROVIDER_PAGE_LIMIT = 2_000
PROVIDER_HISTORY_LIMIT = 10_000
MAX_CAUSAL_WINDOW_MS = 300_000


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def reconstruct_entry_event(copy_row: dict, fills: list[dict], *, after_ts_ms: int) -> dict:
    """Match one unique fill whose resulting signed position equals the captured target size."""
    if bool(copy_row.get("is_cold_start")):
        return {"status": "censored_cold_start", "reason": "preexisting_position"}
    target_size = _float(copy_row.get("target_szi"))
    if target_size is None or target_size == 0:
        return {"status": "unavailable_target_size", "reason": "target_size_missing"}
    side = str(copy_row.get("side") or "").lower()
    if side not in {"long", "short"} or (target_size > 0) != (side == "long"):
        return {"status": "unmatched", "reason": "target_side_size_mismatch"}
    detect_ts_ms = _int(copy_row.get("detect_ts_ms"))
    if detect_ts_ms is None:
        return {"status": "unmatched", "reason": "detect_time_missing"}
    lower = max(int(after_ts_ms), detect_ts_ms - MAX_CAUSAL_WINDOW_MS)
    direction = "Open Long" if side == "long" else "Open Short"
    provider_side = "B" if side == "long" else "A"
    tolerance = max(1e-9, abs(target_size) * 0.02)
    completions = []
    for fill in fills:
        event_ts = _int(fill.get("time"))
        start = _float(fill.get("startPosition"))
        size = _float(fill.get("sz"))
        if (
            fill.get("coin") != copy_row.get("coin")
            or fill.get("dir") != direction
            or fill.get("side") != provider_side
            or event_ts is None
            or not lower < event_ts <= detect_ts_ms
            or start is None
            or size is None
            or size <= 0
        ):
            continue
        final_size = start + size if side == "long" else start - size
        if abs(final_size - target_size) <= tolerance:
            completions.append((event_ts, str(fill.get("tid"))))
    if not completions:
        return {"status": "unmatched", "reason": "no_exact_position_completion"}
    unique = sorted(set(completions))
    if len(unique) != 1 or unique[0][1] in {"", "None"}:
        return {
            "status": "ambiguous",
            "reason": "multiple_exact_position_completions",
            "tids": [tid for _, tid in unique],
        }
    event_ts, tid = unique[0]
    return {
        "status": "matched",
        "reason": "unique_exact_position_completion",
        "source_event_ts_ms": event_ts,
        "tids": [tid],
    }


def fetch_wallet_history(wallet: str, *, start_ms: int, end_ms: int, fetch) -> dict:
    """Page newest-to-oldest within the provider's documented latest-10,000 ceiling."""
    wallet = str(wallet).lower()
    cursor_end = int(end_ms)
    rows_by_tid: dict[str, dict] = {}
    ceiling_reached = False
    while cursor_end >= int(start_ms) and len(rows_by_tid) < PROVIDER_HISTORY_LIMIT:
        page = fetch(wallet, int(start_ms), cursor_end) or []
        valid = [row for row in page if isinstance(row, dict) and _int(row.get("time")) is not None]
        if not valid:
            break
        for row in valid:
            tid = str(row.get("tid"))
            if tid not in {"", "None"}:
                rows_by_tid.setdefault(tid, row)
        if len(rows_by_tid) >= PROVIDER_HISTORY_LIMIT:
            ceiling_reached = True
            break
        if len(valid) < PROVIDER_PAGE_LIMIT:
            break
        next_end = min(int(row["time"]) for row in valid) - 1
        if next_end >= cursor_end:
            ceiling_reached = True
            break
        cursor_end = next_end
    fills = sorted(
        list(rows_by_tid.values()),
        key=lambda row: (int(row["time"]), str(row.get("tid"))),
    )[:PROVIDER_HISTORY_LIMIT]
    return {"fills": fills, "ceiling_reached": ceiling_reached, "count": len(fills)}
