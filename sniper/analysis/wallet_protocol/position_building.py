"""Position Building Surface v1 — pure accumulation reconstruction.

Spec: docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md
Plan: docs/superpowers/plans/2026-05-10-wallet-position-builder-phase1-core.md

No ledger imports. No DB. No I/O. No live API. No price history.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any


SURFACE_VERSION = "position_building_v1"
SIZE_NOTIONAL_EPSILON = 1e-3
FRONT_LOADED_WINDOW_MS = 5 * 60 * 1000

_ROW_QUALITY_KEYS = (
    "malformed_rows",
    "ignored_rows",
    "ambiguous_rows",
    "missing_price_rows",
    "missing_size_rows",
    "missing_timestamp_rows",
    "size_notional_mismatch_rows",
)

_IDENTITY_RANK = {
    "stable_event_uid": 0,
    "stable_transaction": 1,
    "source_bounded_ordinal": 2,
    "local_order_only": 3,
    "ambiguous_same_second": 4,
    "label_fallback": 5,
    "unavailable": 6,
}

_PATTERN_PRECEDENCE = (
    "unavailable",
    "fragmented_or_ambiguous",
    "single_fill",
    "front_loaded",
    "gradual_accumulation",
    "small_buy_accumulation",
    "repeated_buy",
)

_FORBID_GOOD_CAVEATS = {
    "LABEL_FALLBACK_IDENTITY",
    "ORDER_AMBIGUOUS_SAME_SECOND",
    "SELL_EXCEEDS_OBSERVED_SIZE",
    "STARTS_MID_POSITION",
    "PARTIAL_FILL_PATH",
    "ACTIVITY_TRUNCATED_POSITION_PATH",
}


@dataclass
class _NormRow:
    raw: Mapping[str, Any]
    condition_id: str | None
    asset: str | None
    outcome: str | None
    side: str | None
    price: float | None
    size: float | None
    timestamp: int | None
    notional: float | None
    event_uid: str | None
    raw_event_id: str | None
    transaction_hash: str | None
    log_index: Any
    capture_run_id: str | None
    source: str | None
    source_ref: str | None
    source_batch_id: str | None
    row_index: int | None
    input_order: int
    identity_quality: str
    identity_value: str | None
    row_quality: Counter = field(default_factory=Counter)
    caveats: set[str] = field(default_factory=set)
    usable: bool = False

    @property
    def key(self) -> tuple[str, str, str] | None:
        if not self.condition_id:
            return None
        if self.asset:
            return ("asset", self.condition_id, self.asset)
        if self.outcome:
            return ("label_fallback", self.condition_id, self.outcome)
        return None


def _blank_row_quality() -> Counter:
    return Counter({k: 0 for k in _ROW_QUALITY_KEYS})


def _get(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row:
            return row.get(name)
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_side(value: Any) -> str | None:
    side = _str_or_none(value)
    if side is None:
        return None
    side_up = side.upper()
    if side_up in {"BUY", "SELL"}:
        return side_up
    return None


def _derive_identity(row: Mapping[str, Any]) -> tuple[str, str | None]:
    event_uid = _str_or_none(row.get("event_uid"))
    if event_uid:
        return "stable_event_uid", event_uid
    raw_event_id = _str_or_none(row.get("raw_event_id"))
    if raw_event_id:
        return "stable_event_uid", raw_event_id
    tx = _str_or_none(row.get("transaction_hash") or row.get("transactionHash"))
    log_index = _get(row, "logIndex", "log_index")
    if tx and log_index is not None:
        return "stable_transaction", f"{tx}:{log_index}"
    row_index = _get(row, "row_index", "event_row_ordinal")
    source_scope = (
        _str_or_none(row.get("source_batch_id"))
        or _str_or_none(row.get("source_ref"))
        or _str_or_none(row.get("capture_run_id"))
    )
    if source_scope is not None and row_index is not None:
        return "source_bounded_ordinal", f"{source_scope}:{row_index}"
    if row_index is not None:
        return "local_order_only", str(row_index)
    return "unavailable", None


def _normalize_row(raw: Mapping[str, Any], input_order: int) -> _NormRow:
    rq = _blank_row_quality()
    condition_id = _str_or_none(_get(raw, "condition_id", "conditionId", "cid"))
    asset = _str_or_none(_get(raw, "asset", "token_id"))
    outcome = _str_or_none(raw.get("outcome"))
    side = _normalize_side(raw.get("side"))
    price = _float_or_none(raw.get("price"))
    size = _float_or_none(raw.get("size"))
    timestamp = _int_or_none(_get(raw, "timestamp", "ts"))
    notional_raw = _get(raw, "notional", "usdcSize", "usd_equivalent_amount")
    notional = _float_or_none(notional_raw)

    if side is None:
        rq["malformed_rows"] += 1
    if price is None:
        rq["missing_price_rows"] += 1
    if size is None:
        rq["missing_size_rows"] += 1
    if timestamp is None:
        rq["missing_timestamp_rows"] += 1

    if notional is None and price is not None and size is not None:
        notional = abs(price * size)

    caveats: set[str] = set()
    if price is not None and size is not None and notional is not None:
        mismatch = abs(size * price - notional) / max(abs(notional), 1.0)
        if mismatch > SIZE_NOTIONAL_EPSILON:
            rq["size_notional_mismatch_rows"] += 1
            caveats.add("SIZE_NOTIONAL_MISMATCH")

    identity_quality, identity_value = _derive_identity(raw)
    usable = (
        condition_id is not None
        and (asset is not None or outcome is not None)
        and side in {"BUY", "SELL"}
        and price is not None
        and size is not None
        and timestamp is not None
    )
    if not usable:
        rq["ignored_rows"] += 1

    return _NormRow(
        raw=raw,
        condition_id=condition_id,
        asset=asset,
        outcome=outcome,
        side=side,
        price=price,
        size=size,
        timestamp=timestamp,
        notional=notional,
        event_uid=_str_or_none(raw.get("event_uid")),
        raw_event_id=_str_or_none(raw.get("raw_event_id")),
        transaction_hash=_str_or_none(raw.get("transaction_hash") or raw.get("transactionHash")),
        log_index=_get(raw, "logIndex", "log_index"),
        capture_run_id=_str_or_none(raw.get("capture_run_id")),
        source=_str_or_none(raw.get("source")),
        source_ref=_str_or_none(raw.get("source_ref")),
        source_batch_id=_str_or_none(raw.get("source_batch_id")),
        row_index=_int_or_none(_get(raw, "row_index", "event_row_ordinal")),
        input_order=input_order,
        identity_quality=identity_quality,
        identity_value=identity_value,
        row_quality=rq,
        caveats=caveats,
        usable=usable,
    )


def _worst_identity(rows: Sequence[_NormRow], *, label_fallback: bool) -> str:
    if label_fallback:
        return "label_fallback"
    if not rows:
        return "unavailable"
    return max((r.identity_quality for r in rows), key=lambda q: _IDENTITY_RANK[q])


def _source_quality(
    rows: Sequence[_NormRow],
    *,
    identity_quality: str,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
    has_usable_rows: bool,
) -> str:
    if not has_usable_rows:
        return "unavailable"
    if identity_quality == "ambiguous_same_second":
        return "activity_rows_ambiguous"
    has_ledger = any(
        r.event_uid and r.capture_run_id and r.source and r.source_ref
        for r in rows
    )
    if has_ledger:
        return "ledger_enriched"
    if activity_cap_hit and not (subgraph_fallback_used or walk_full_history):
        return "activity_rows_partial"
    return "activity_rows_complete"


def _sort_rows(rows: Sequence[_NormRow]) -> list[_NormRow]:
    return sorted(
        rows,
        key=lambda r: (
            r.timestamp if r.timestamp is not None else 0,
            r.row_index if r.row_index is not None else r.input_order,
            r.identity_value or "",
        ),
    )


def _path_entry(row: _NormRow, size_delta: float, observed_size: float, order_ambiguous: bool) -> dict:
    observed_notional = None
    if row.price is not None:
        observed_notional = abs(observed_size * row.price)
    return {
        "ts": row.timestamp,
        "event_uid": row.event_uid,
        "side": row.side,
        "size_delta": size_delta,
        "observed_size": observed_size,
        "price": row.price,
        "notional": row.notional,
        "observed_notional": observed_notional,
        "order_ambiguous": order_ambiguous,
    }


def _distribution(values: Sequence[str]) -> dict[str, dict[str, float | int]]:
    total = len(values)
    if total == 0:
        return {}
    counts = Counter(values)
    return {
        key: {"n": n, "share": n / total}
        for key, n in sorted(counts.items())
    }


def _safe_median(values: Sequence[int | float | None]) -> int | float | None:
    clean = [v for v in values if v is not None]
    return median(clean) if clean else None


def _build_position(
    key: tuple[str, str, str],
    rows: Sequence[_NormRow],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
) -> dict:
    key_type, condition_id, key_value = key
    sorted_rows = _sort_rows(rows)
    caveats: set[str] = set()
    row_quality = _blank_row_quality()
    for row in sorted_rows:
        row_quality.update(row.row_quality)
        caveats.update(row.caveats)
    label_fallback = key_type == "label_fallback"
    if label_fallback:
        caveats.add("LABEL_FALLBACK_IDENTITY")

    same_second = defaultdict(list)
    for row in sorted_rows:
        same_second[(row.condition_id, row.asset, row.timestamp)].append(row)
    for group in same_second.values():
        if len(group) <= 1:
            continue
        identities = [r.identity_value for r in group if r.identity_value]
        if len(set(identities)) < len(group):
            caveats.add("ORDER_AMBIGUOUS_SAME_SECOND")
            row_quality["ambiguous_rows"] += len(group)
            for row in group:
                row.identity_quality = "ambiguous_same_second"

    usable_rows = [r for r in sorted_rows if r.usable]
    buy_rows = [r for r in usable_rows if r.side == "BUY"]
    sell_rows = [r for r in usable_rows if r.side == "SELL"]
    identity_quality = _worst_identity(usable_rows or sorted_rows, label_fallback=label_fallback)
    source_quality = _source_quality(
        usable_rows or sorted_rows,
        identity_quality=identity_quality,
        activity_cap_hit=activity_cap_hit,
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        has_usable_rows=bool(usable_rows),
    )
    if activity_cap_hit and not (subgraph_fallback_used or walk_full_history):
        if usable_rows:
            caveats.add("ACTIVITY_TRUNCATED_POSITION_PATH")

    observed_size = 0.0
    peak_size = 0.0
    peak_ts = None
    cumulative_path = []
    avg_entry_path = []
    buy_cost = 0.0
    buy_size = 0.0
    seen_sell = False
    first_buy_ts = None
    first_buy_price = None
    first_buy_notional = None
    partial_sell_ladder = []
    for row in usable_rows:
        size = float(row.size or 0.0)
        if row.side == "BUY":
            delta = size
            if first_buy_ts is None:
                first_buy_ts = row.timestamp
                first_buy_price = row.price
                first_buy_notional = row.notional
            buy_cost += (row.price or 0.0) * size
            buy_size += size
        else:
            delta = -size
            seen_sell = True
        observed_size += delta
        if observed_size < 0:
            caveats.add("SELL_EXCEEDS_OBSERVED_SIZE")
            if not buy_rows or all((b.timestamp or 0) >= (row.timestamp or 0) for b in buy_rows):
                caveats.add("LIKELY_STARTED_MID_POSITION")
            observed_size = 0.0
        if observed_size > peak_size:
            peak_size = observed_size
            peak_ts = row.timestamp
        ambiguous = "ORDER_AMBIGUOUS_SAME_SECOND" in caveats
        cumulative_path.append(_path_entry(row, delta, observed_size, ambiguous))
        if row.side == "SELL":
            partial_sell_ladder.append({
                "ts": row.timestamp,
                "event_uid": row.event_uid,
                "price": row.price,
                "size": size,
                "notional": row.notional,
                "observed_size_after": observed_size,
                "order_ambiguous": ambiguous,
            })
        if row.side == "BUY":
            avg_price = (buy_cost / buy_size) if buy_size else None
            avg_entry_path.append({
                "ts": row.timestamp,
                "avg_entry_price": avg_price,
                "observed_inventory": observed_size,
                "method": "average_cost_observed",
                "provenance": "inferred" if seen_sell else "factual",
            })
    if sell_rows:
        caveats.add("AVERAGE_ENTRY_INFERRED_AFTER_SELL")

    def _time_to_share(share: float) -> int | None:
        if not cumulative_path or peak_size <= 0 or first_buy_ts is None:
            return None
        target = peak_size * share
        for entry in cumulative_path:
            if entry["observed_size"] >= target:
                return int((entry["ts"] - first_buy_ts) * 1000)
        return None

    def _buys_before_share(share: float) -> int | None:
        if peak_size <= 0:
            return None
        target = peak_size * share
        n_buys = 0
        for entry in cumulative_path:
            if entry["side"] == "BUY":
                n_buys += 1
            if entry["observed_size"] >= target:
                return n_buys
        return None

    time_to_75 = _time_to_share(0.75)
    time_to_peak = None if peak_ts is None or first_buy_ts is None else int((peak_ts - first_buy_ts) * 1000)
    total_buy_size = sum(float(r.size or 0.0) for r in buy_rows)
    total_buy_notional = sum(float(r.notional or 0.0) for r in buy_rows if r.notional is not None)
    largest_buy = max((float(r.size or 0.0) for r in buy_rows), default=0.0)
    largest_buy_share = (largest_buy / peak_size) if peak_size > 0 else None
    total_sell_size = sum(float(r.size or 0.0) for r in sell_rows)
    total_sell_notional = sum(float(r.notional or 0.0) for r in sell_rows if r.notional is not None)
    if sell_rows and all((r.size or 0) > 0 for r in sell_rows):
        sw_sell = sum((r.price or 0.0) * (r.size or 0.0) for r in sell_rows) / total_sell_size
        sell_method = "size_weighted"
    elif sell_rows:
        sw_sell = None
        sell_method = "partial"
    else:
        sw_sell = None
        sell_method = "unavailable"

    dust_delta = max(1e-9, 0.01 * peak_size) if peak_size > 0 else 1e-9
    material_delta = max(1e-9, 0.10 * peak_size) if peak_size > 0 else 1e-9
    sell_accum = 0.0
    buy_after_sell_accum = 0.0
    in_sell_down = False
    reaccumulation_detected = False
    for entry in cumulative_path:
        delta = abs(float(entry["size_delta"]))
        if delta <= dust_delta:
            continue
        if entry["side"] == "SELL":
            sell_accum += delta
            if sell_accum >= material_delta:
                in_sell_down = True
        elif in_sell_down:
            buy_after_sell_accum += delta
            if buy_after_sell_accum >= material_delta:
                reaccumulation_detected = True
                break

    buy_timestamps = {r.timestamp for r in buy_rows if r.timestamp is not None}
    split_buys = len(buy_timestamps) > 1 or len({r.identity_value for r in buy_rows if r.identity_value}) > 1
    small_buy_detected = (
        len(buy_rows) >= 3
        and (largest_buy_share is not None and largest_buy_share < 0.70)
        and split_buys
        and identity_quality not in {"ambiguous_same_second", "unavailable"}
    )
    if not buy_rows:
        accumulation_pattern = "unavailable"
    elif identity_quality in {"ambiguous_same_second", "unavailable"} or source_quality == "activity_rows_ambiguous":
        accumulation_pattern = "fragmented_or_ambiguous"
    elif len(buy_rows) == 1:
        accumulation_pattern = "single_fill"
    elif len(buy_timestamps) == 1 and small_buy_detected:
        accumulation_pattern = "small_buy_accumulation"
    elif time_to_75 is not None and time_to_75 <= FRONT_LOADED_WINDOW_MS:
        accumulation_pattern = "front_loaded"
    elif (
        len(buy_rows) >= 3
        and time_to_75 is not None
        and time_to_75 > FRONT_LOADED_WINDOW_MS
        and (largest_buy_share is not None and largest_buy_share < 0.70)
        and identity_quality not in {"ambiguous_same_second", "unavailable"}
    ):
        accumulation_pattern = "gradual_accumulation"
    elif small_buy_detected:
        accumulation_pattern = "small_buy_accumulation"
    else:
        accumulation_pattern = "repeated_buy"

    # Observation quality — evaluation order per spec §2.6:
    # 1. unavailable (no usable fill path)
    # 2. poor floor (usable path exists but critically incomplete)
    # 3. good / partial (max-quality caps + caveats)
    observation_quality = "partial" if usable_rows else "unavailable"
    if usable_rows:
        # poor floor: spec §2.6 — "first entry, milestone path, or sell ladder
        # is mostly missing/ambiguous."
        _first_entry_missing = first_buy_ts is None or first_buy_price is None
        _milestone_path_missing = (
            len(buy_rows) >= 3
            and _time_to_share(0.50) is None
            and time_to_75 is None
            and time_to_peak is None
        )
        _sell_ladder_ambiguous = len(sell_rows) >= 2 and sell_method == "partial"
        if _first_entry_missing or _milestone_path_missing or _sell_ladder_ambiguous:
            observation_quality = "poor"
        elif (
            source_quality in {"ledger_enriched", "activity_rows_complete"}
            and identity_quality in {"stable_event_uid", "stable_transaction"}
            and not (_FORBID_GOOD_CAVEATS & caveats)
            and row_quality["size_notional_mismatch_rows"] <= 1
        ):
            observation_quality = "good"
        # ORDER_AMBIGUOUS_SAME_SECOND caps good→partial but does not override poor
        if "ORDER_AMBIGUOUS_SAME_SECOND" in caveats and observation_quality == "good":
            observation_quality = "partial"

    fields_provenance = {
        "first_buy_ts": "factual" if first_buy_ts is not None else "unavailable",
        "cumulative_size_path": "inferred" if ("ACTIVITY_TRUNCATED_POSITION_PATH" in caveats or "ORDER_AMBIGUOUS_SAME_SECOND" in caveats) else ("factual" if cumulative_path else "unavailable"),
        "average_entry_path": "inferred" if sell_rows else ("factual" if avg_entry_path else "unavailable"),
        "sell_ladder_summary": "factual" if sell_rows else "unavailable",
        "observation_window_quality": "inferred" if usable_rows else "unavailable",
    }

    return {
        "position_key": {
            "type": key_type,
            "condition_id": condition_id,
            "asset": key_value if key_type == "asset" else None,
            "outcome": next((r.outcome for r in sorted_rows if r.outcome), key_value if key_type == "label_fallback" else None),
        },
        "condition_id": condition_id,
        "asset": key_value if key_type == "asset" else None,
        "outcome": next((r.outcome for r in sorted_rows if r.outcome), key_value if key_type == "label_fallback" else None),
        "source_quality": source_quality,
        "identity_quality": identity_quality,
        "caveats": sorted(caveats),
        "first_buy_ts": first_buy_ts,
        "first_buy_price": first_buy_price,
        "first_buy_notional": first_buy_notional,
        "n_buy_fills": len(buy_rows),
        "n_sell_fills": len(sell_rows),
        "peak_observed_size": peak_size if usable_rows else None,
        "peak_observed_size_ts": peak_ts,
        "peak_observed_notional": None if peak_size <= 0 else abs(peak_size * (first_buy_price or 0.0)),
        "final_observed_size": observed_size if usable_rows else None,
        "final_observed_notional": None if not usable_rows else abs(observed_size * (first_buy_price or 0.0)),
        "cumulative_size_path": cumulative_path,
        "average_entry_path": avg_entry_path,
        "buy_ladder_summary": {
            "n_buy_fills": len(buy_rows),
            "total_buy_size": total_buy_size if buy_rows else None,
            "total_buy_notional": total_buy_notional if buy_rows else None,
            "first_buy_ts": first_buy_ts,
            "last_buy_ts": buy_rows[-1].timestamp if buy_rows else None,
            "size_weighted_entry_price": (buy_cost / buy_size) if buy_size else None,
            "largest_buy_share_of_peak": largest_buy_share,
        },
        "partial_sell_ladder": partial_sell_ladder,
        "sell_ladder_summary": {
            "n_sell_fills": len(sell_rows),
            "total_sell_size": total_sell_size if sell_rows else None,
            "total_sell_notional": total_sell_notional if sell_rows else None,
            "first_sell_ts": sell_rows[0].timestamp if sell_rows else None,
            "last_sell_ts": sell_rows[-1].timestamp if sell_rows else None,
            "size_weighted_sell_price": sw_sell,
            "sell_price_method": sell_method,
        },
        "time_to_25pct_size_ms": _time_to_share(0.25),
        "time_to_50pct_size_ms": _time_to_share(0.50),
        "time_to_75pct_size_ms": time_to_75,
        "time_to_peak_size_ms": time_to_peak,
        "time_to_final_observed_size_ms": None if not cumulative_path or first_buy_ts is None else int((cumulative_path[-1]["ts"] - first_buy_ts) * 1000),
        "n_buys_before_50pct": _buys_before_share(0.50),
        "n_buys_before_75pct": _buys_before_share(0.75),
        "accumulation_pattern": accumulation_pattern,
        "small_buy_accumulation_detected": small_buy_detected,
        "reaccumulation_detected": reaccumulation_detected,
        "observation_window_quality": observation_quality,
        "row_quality": dict(row_quality),
        "reconciliation": {
            "closed_position_seen": False,
            "closed_position_size": None,
            "observed_final_size": observed_size if usable_rows else None,
            "size_delta": None,
            "status": "not_checked",
        },
        "fields_provenance": fields_provenance,
    }


def _closed_key(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    condition_id = _str_or_none(_get(row, "condition_id", "conditionId", "cid"))
    asset = _str_or_none(_get(row, "asset", "token_id"))
    outcome = _str_or_none(row.get("outcome"))
    if not condition_id:
        return None
    if asset:
        return ("asset", condition_id, asset)
    if outcome:
        return ("label_fallback", condition_id, outcome)
    return None


def _context_position(
    key: tuple[str, str, str],
    row: Mapping[str, Any],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
) -> dict:
    key_type, condition_id, key_value = key
    outcome = _str_or_none(row.get("outcome"))
    size = _float_or_none(_get(row, "size", "size_tokens"))
    caveats = {"NO_USABLE_BUY_PATH"}
    if key_type == "label_fallback":
        caveats.add("LABEL_FALLBACK_IDENTITY")
        identity_quality = "label_fallback"
    else:
        identity_quality = "unavailable"
    source_quality = (
        "activity_rows_partial"
        if activity_cap_hit and not (subgraph_fallback_used or walk_full_history)
        else "unavailable"
    )
    fields_provenance = {
        "first_buy_ts": "unavailable",
        "cumulative_size_path": "unavailable",
        "average_entry_path": "unavailable",
        "sell_ladder_summary": "unavailable",
        "observation_window_quality": "unavailable",
    }
    return {
        "position_key": {
            "type": key_type,
            "condition_id": condition_id,
            "asset": key_value if key_type == "asset" else None,
            "outcome": outcome or (key_value if key_type == "label_fallback" else None),
        },
        "condition_id": condition_id,
        "asset": key_value if key_type == "asset" else None,
        "outcome": outcome or (key_value if key_type == "label_fallback" else None),
        "source_quality": source_quality,
        "identity_quality": identity_quality,
        "caveats": sorted(caveats),
        "first_buy_ts": None,
        "first_buy_price": None,
        "first_buy_notional": None,
        "n_buy_fills": 0,
        "n_sell_fills": 0,
        "peak_observed_size": None,
        "peak_observed_size_ts": None,
        "peak_observed_notional": None,
        "final_observed_size": None,
        "final_observed_notional": None,
        "cumulative_size_path": [],
        "average_entry_path": [],
        "buy_ladder_summary": {
            "n_buy_fills": 0,
            "total_buy_size": None,
            "total_buy_notional": None,
            "first_buy_ts": None,
            "last_buy_ts": None,
            "size_weighted_entry_price": None,
            "largest_buy_share_of_peak": None,
        },
        "partial_sell_ladder": [],
        "sell_ladder_summary": {
            "n_sell_fills": 0,
            "total_sell_size": None,
            "total_sell_notional": None,
            "first_sell_ts": None,
            "last_sell_ts": None,
            "size_weighted_sell_price": None,
            "sell_price_method": "unavailable",
        },
        "time_to_25pct_size_ms": None,
        "time_to_50pct_size_ms": None,
        "time_to_75pct_size_ms": None,
        "time_to_peak_size_ms": None,
        "time_to_final_observed_size_ms": None,
        "n_buys_before_50pct": None,
        "n_buys_before_75pct": None,
        "accumulation_pattern": "unavailable",
        "small_buy_accumulation_detected": False,
        "reaccumulation_detected": False,
        "observation_window_quality": "unavailable",
        "row_quality": dict(_blank_row_quality()),
        "reconciliation": {
            "closed_position_seen": True,
            "closed_position_size": size,
            "observed_final_size": None,
            "size_delta": None,
            "status": "context_only",
        },
        "fields_provenance": fields_provenance,
    }


def _base_surface(
    activity_rows: Sequence[Mapping],
    closed_positions: Sequence[Mapping] | None,
    *,
    wallet: str | None,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
) -> dict:
    return {
        "wallet": wallet,
        "surface_version": SURFACE_VERSION,
        "generated_from": {
            "activity_rows": len(activity_rows),
            "closed_positions": len(closed_positions or []),
            "activity_cap_hit": activity_cap_hit,
            "subgraph_fallback_used": subgraph_fallback_used,
            "walk_full_history": walk_full_history,
        },
        "source_quality_distribution": {},
        "identity_quality_distribution": {},
        "row_quality_summary": dict(_blank_row_quality()),
        "wallet_summary": {},
        "positions": [],
        "top_level_caveats": [],
    }


def _wallet_summary(positions: Sequence[dict]) -> dict:
    n_positions = len(positions)
    reconstructed = [p for p in positions if p["n_buy_fills"] > 0 and p["first_buy_ts"] is not None]
    n_reconstructed = len(reconstructed)
    n_unavailable = sum(1 for p in positions if p["observation_window_quality"] == "unavailable")
    n_context = sum(1 for p in positions if p["reconciliation"]["status"] == "context_only")
    def _shape_share(pred) -> float | None:
        if n_reconstructed == 0:
            return None
        return sum(1 for p in reconstructed if pred(p)) / n_reconstructed

    pattern_counts = Counter(p["accumulation_pattern"] for p in reconstructed)
    if pattern_counts:
        dominant_pattern = min(
            pattern_counts,
            key=lambda p: (-pattern_counts[p], _PATTERN_PRECEDENCE.index(p)),
        )
    else:
        dominant_pattern = None
    caveat_counts = Counter(c for p in positions for c in p["caveats"])
    if caveat_counts:
        dominant_limitation = min(caveat_counts, key=lambda c: (-caveat_counts[c], c))
    else:
        dominant_limitation = None
    return {
        "n_positions": n_positions,
        "n_reconstructed_positions": n_reconstructed,
        "n_unavailable_positions": n_unavailable,
        "n_context_only_positions": n_context,
        "share_repeated_buy_positions": _shape_share(lambda p: p["accumulation_pattern"] == "repeated_buy"),
        "share_small_buy_accumulation": _shape_share(lambda p: p["small_buy_accumulation_detected"]),
        "share_sell_ladder_positions": _shape_share(lambda p: p["n_sell_fills"] >= 2),
        "share_reaccumulation_positions": _shape_share(lambda p: p["reaccumulation_detected"]),
        "median_time_to_50pct_size_ms": _safe_median([p["time_to_50pct_size_ms"] for p in positions]),
        "median_time_to_75pct_size_ms": _safe_median([p["time_to_75pct_size_ms"] for p in positions]),
        "median_time_to_peak_size_ms": _safe_median([p["time_to_peak_size_ms"] for p in positions]),
        "median_peak_observed_notional": _safe_median([p["peak_observed_notional"] for p in positions]),
        "median_final_observed_notional": _safe_median([p["final_observed_notional"] for p in positions]),
        "share_positions_with_notional_available": (sum(1 for p in positions if p["first_buy_notional"] is not None) / n_positions) if n_positions else None,
        "observation_window_coverage": (sum(1 for p in positions if p["observation_window_quality"] in {"good", "partial"}) / n_positions) if n_positions else None,
        "observation_window_quality_distribution": _distribution([p["observation_window_quality"] for p in positions]),
        "share_positions_with_stable_identity": (sum(1 for p in positions if p["identity_quality"] in {"stable_event_uid", "stable_transaction"}) / n_positions) if n_positions else None,
        "share_positions_with_ambiguous_same_second": (sum(1 for p in positions if p["identity_quality"] == "ambiguous_same_second") / n_positions) if n_positions else None,
        "share_positions_with_no_usable_buy_path": (sum(1 for p in positions if "NO_USABLE_BUY_PATH" in p["caveats"]) / n_positions) if n_positions else None,
        "dominant_accumulation_pattern": dominant_pattern,
        "dominant_limitation": dominant_limitation,
        "high_confidence_position_count": sum(1 for p in positions if p["observation_window_quality"] == "good"),
        "low_confidence_position_count": sum(1 for p in positions if p["observation_window_quality"] in {"poor", "unavailable"}),
        "major_caveats": [],
    }


def compute_position_building_surface(
    activity_rows: Sequence[Mapping],
    closed_positions: Sequence[Mapping] | None = None,
    *,
    wallet: str | None = None,
    activity_cap_hit: bool = False,
    subgraph_fallback_used: bool = False,
    walk_full_history: bool = False,
) -> dict | None:
    """Compute a pure position-building surface from normalized rows."""
    if not activity_rows and not closed_positions:
        return None
    surface = _base_surface(
        activity_rows,
        closed_positions,
        wallet=wallet,
        activity_cap_hit=activity_cap_hit,
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
    )
    normalized = [_normalize_row(row, i) for i, row in enumerate(activity_rows)]
    groups: dict[tuple[str, str, str], list[_NormRow]] = defaultdict(list)
    global_quality = _blank_row_quality()
    for row in normalized:
        global_quality.update(row.row_quality)
        key = row.key
        if key is not None:
            groups[key].append(row)

    positions = [
        _build_position(
            key,
            rows,
            activity_cap_hit=activity_cap_hit,
            subgraph_fallback_used=subgraph_fallback_used,
            walk_full_history=walk_full_history,
        )
        for key, rows in sorted(groups.items())
    ]
    seen_keys = set(groups)
    for closed in closed_positions or []:
        key = _closed_key(closed)
        if key is None or key in seen_keys:
            continue
        positions.append(
            _context_position(
                key,
                closed,
                activity_cap_hit=activity_cap_hit,
                subgraph_fallback_used=subgraph_fallback_used,
                walk_full_history=walk_full_history,
            )
        )
        seen_keys.add(key)
    surface["positions"] = positions
    for pos in positions:
        for key, value in pos["row_quality"].items():
            global_quality[key] = max(global_quality[key], value)
    surface["row_quality_summary"] = dict(global_quality)
    caveats = set()
    if activity_cap_hit and not (subgraph_fallback_used or walk_full_history):
        caveats.add("ACTIVITY_CAP_HIT")
    for pos in positions:
        caveats.update(pos["caveats"])
    surface["top_level_caveats"] = sorted(caveats)
    surface["source_quality_distribution"] = _distribution([p["source_quality"] for p in positions])
    surface["identity_quality_distribution"] = _distribution([p["identity_quality"] for p in positions])
    surface["wallet_summary"] = _wallet_summary(positions)
    return surface


__all__ = ["SURFACE_VERSION", "compute_position_building_surface"]
