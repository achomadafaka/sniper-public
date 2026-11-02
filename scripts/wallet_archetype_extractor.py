#!/usr/bin/env python3
"""Wallet archetype feature extraction from live Polymarket activity.

This is intentionally separate from the legacy insider/speculator scoring path.
Its job is to answer a narrower question:

  "What kind of wallet behavior is this, based on observable trade structure?"

The output is designed for research and human review, not automatic strategy
creation. It extracts activity/position features, assigns a coarse archetype,
and emits enough evidence to support a deep read.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median, pstdev
from typing import Any, Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Activity + positions come from our typed analysis-layer client (retries,
# UA workaround, safe casts). Gate0 lifetime stats + keyword lists stay with
# the legacy profiler since they are independent of the raw-activity path.
from data.polymarket_activity_client import (
    PolymarketActivityError,
    get_activity_page,
    get_positions as _pm_get_positions,
    iter_activity as _pm_iter_activity,
)
from data.polymarket_client import PolymarketClient
from sniper.markets.polymarket_api import build_session, fetch_wallet_profile_enrichment
from scripts.execution_profile import compute_execution_profile
from scripts.wallet_profiler import fetch_wallet_lifetime_stats


def _fetch_activity_dicts(wallet: str, limit: int = 500) -> List[Dict[str, Any]]:
    """Pull raw Polymarket activity via the typed client, return list of dicts.

    Preserves the legacy extractor API (list of row dicts) while switching
    the underlying fetch to the hardened client — we get retries, typed
    parsing, and UA handling for free without churning the feature code.
    """
    rows: List[Dict[str, Any]] = []
    for row in _pm_iter_activity(wallet):
        rows.append(row.raw)
        if len(rows) >= limit:
            break
    return rows


def _fetch_positions_dicts(wallet: str) -> List[Dict[str, Any]]:
    """Positions via the typed client; returns raw dicts for legacy feature code."""
    return _pm_get_positions(wallet, size_threshold=0.0)


def _fetch_wallet_lifetime_span(wallet: str, max_pages: int = 6) -> Dict[str, Any]:
    """Paginate full Polymarket activity to find true wallet-level lifespan.

    This is intentionally separate from `_timing_metrics`, which measures
    intra-sample cadence (regularity within the 500-row activity window).
    Wallet-level tenure requires walking the full history — otherwise any
    prolific wallet looks "fresh" because 500 rows can cover only days.

    API constraint (verified 2026-04-22): Polymarket Data API caps `offset`
    at 3000. Offsets beyond that return HTTP 400 with "max historical
    activity offset of 3000 exceeded". At 500 rows/page this means max 6
    reachable pages (3000 rows of history) — anything beyond is not
    retrievable via the public activity endpoint. `max_pages=6` default
    aligns with this cap; raising it would trigger HTTP 400 on prolific
    wallets without yielding more data.

    Stops on first empty page, partial page (< 500 rows = true end), the
    API-level offset cap raising 400, or after max_pages calls. If the
    cap hits before seeing a partial page, `pagination_complete=False`
    and the caller must treat `wallet_lifetime_days` as a LOWER bound.

    Returns:
      wallet_first_activity_ts, wallet_last_activity_ts, wallet_lifetime_days,
      pagination_complete, pages_fetched
    """
    oldest: int | None = None
    newest = 0
    offset = 0
    pages_fetched = 0
    complete = True
    hit_api_cap = False
    for _ in range(max_pages):
        try:
            page = get_activity_page(wallet, limit=500, offset=offset)
        except PolymarketActivityError as exc:
            # Defensive: if the API returns 400 'max historical activity offset
            # exceeded', treat it as an end-of-history signal rather than a fatal
            # error. Other 4xx/5xx still propagate.
            if "max historical activity offset" in str(exc):
                hit_api_cap = True
                break
            raise
        pages_fetched += 1
        if not page:
            break
        for row in page:
            ts = row.timestamp
            if ts > 0:
                if oldest is None or ts < oldest:
                    oldest = ts
                if ts > newest:
                    newest = ts
        if len(page) < 500:
            break
        offset += 500
    else:
        # Loop exhausted without break → we hit max_pages, history may exceed what we walked
        complete = False

    # If we hit the API offset cap, the true history extends further than we could walk
    if hit_api_cap:
        complete = False

    lifetime_days = 0.0
    if oldest and newest and newest >= oldest:
        lifetime_days = round((newest - oldest) / 86400.0, 2)

    return {
        "wallet_first_activity_ts": oldest or 0,
        "wallet_last_activity_ts": newest,
        "wallet_lifetime_days": lifetime_days,
        "pagination_complete": complete,
        "pages_fetched": pages_fetched,
    }


def _fetch_wallet_profile_tenure(wallet: str) -> Dict[str, Any]:
    """Fetch profile-level tenure metadata from Polymarket profile hydration.

    This complements `_fetch_wallet_lifetime_span`, which is bounded by the
    public activity endpoint's offset cap. For hyper-active wallets, profile
    `joinDate` / `createdAt` provides an orthogonal lower-noise age signal.
    """
    session = build_session()
    try:
        enriched = fetch_wallet_profile_enrichment(session, wallet)
    finally:
        session.close()

    joined_ts = int(enriched.get("profile_joined_ts") or 0)
    profile_lifetime_days = 0.0
    if joined_ts > 0:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        if now_ts >= joined_ts:
            profile_lifetime_days = round((now_ts - joined_ts) / 86400.0, 2)

    return {
        "profile_joined_ts": joined_ts,
        "profile_lifetime_days": profile_lifetime_days,
        "profile_tenure_source": str(enriched.get("profile_enrichment_source") or ""),
    }


def _fetch_wallet_tenure_metadata(wallet: str) -> Dict[str, Any]:
    """Merge activity-span tenure with profile-derived tenure.

    `wallet_lifetime_days` is the best available tenure estimate:
    - activity span if that is all we have
    - profile age if history is API-capped or older than reachable activity
    - the max of both when both exist

    `pagination_complete` remains load-bearing even when profile tenure exists:
    it tells the reviewer whether the public activity history is complete.
    """
    activity_tenure = _fetch_wallet_lifetime_span(wallet)
    profile_tenure = _fetch_wallet_profile_tenure(wallet)

    activity_days = _float(activity_tenure.get("wallet_lifetime_days"))
    profile_days = _float(profile_tenure.get("profile_lifetime_days"))

    merged_days = max(activity_days, profile_days)
    if profile_days > activity_days:
        lifetime_source = "profile_joined"
    elif activity_days > 0:
        lifetime_source = "activity_span"
    else:
        lifetime_source = "unknown"

    return {
        **activity_tenure,
        **profile_tenure,
        "wallet_lifetime_days": round(merged_days, 2),
        "wallet_lifetime_source": lifetime_source,
    }


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _approx_share_size(row: Dict[str, Any]) -> float:
    size = _float(row.get("size"))
    if size > 0:
        return size
    usdc = _float(row.get("usdcSize"))
    price = _float(row.get("price"))
    if usdc > 0 and 0 < price <= 1.0:
        return usdc / price
    return 0.0


def _entry_price_bucket(price: float) -> str:
    if price <= 0:
        return "unknown"
    if price < 0.25:
        return "0.00-0.25"
    if price < 0.40:
        return "0.25-0.40"
    if price < 0.60:
        return "0.40-0.60"
    if price < 0.75:
        return "0.60-0.75"
    return "0.75-1.00"


def _trade_events(activity: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in activity if row.get("type") == "TRADE" and row.get("conditionId")]


def _position_events(positions: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [row for row in positions if row.get("conditionId")]


def _pair_metrics_by_condition(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        condition_id = row.get("conditionId")
        if not condition_id:
            continue
        item = grouped.setdefault(
            condition_id,
            {
                "outcomes": set(),
                "title": row.get("title") or "?",
                "negative_risk": bool(row.get("negativeRisk")),
                "value": 0.0,
                "count": 0,
                "outcome_values": defaultdict(float),
            },
        )
        outcome = row.get("outcome")
        if outcome not in ("", None):
            item["outcomes"].add(str(outcome))
        item["count"] += 1
        value = _float(
            row.get("currentValue")
            or row.get("initialValue")
            or row.get("usdcSize")
            or row.get("size")
        )
        item["value"] += value
        if outcome not in ("", None):
            item["outcome_values"][str(outcome)] += value
        item["negative_risk"] = item["negative_risk"] or bool(row.get("negativeRisk"))

    total = len(grouped)
    paired = [item for item in grouped.values() if len(item["outcomes"]) >= 2]
    binary_paired = [item for item in paired if not item["negative_risk"]]
    paired_value = sum(item["value"] for item in paired)
    total_value = sum(item["value"] for item in grouped.values())
    negative_risk_total = sum(1 for item in grouped.values() if item["negative_risk"])
    negative_risk_paired = sum(1 for item in paired if item["negative_risk"])
    paired_secondary_leg_value = sum(
        max(item["value"] - max(item["outcome_values"].values(), default=0.0), 0.0)
        for item in paired
    )
    binary_secondary_leg_value = sum(
        max(item["value"] - max(item["outcome_values"].values(), default=0.0), 0.0)
        for item in binary_paired
    )
    negative_risk_secondary_leg_value = paired_secondary_leg_value - binary_secondary_leg_value

    return {
        "total_conditions": total,
        "paired_conditions": len(paired),
        "paired_condition_ratio": round(len(paired) / max(total, 1), 3),
        "binary_paired_conditions": len(binary_paired),
        "binary_paired_condition_ratio": round(len(binary_paired) / max(total, 1), 3),
        "paired_value_share": round(paired_value / max(total_value, 1.0), 3),
        "paired_secondary_leg_value_share": round(paired_secondary_leg_value / max(total_value, 1.0), 3),
        "binary_secondary_leg_value_share": round(binary_secondary_leg_value / max(total_value, 1.0), 3),
        "negative_risk_secondary_leg_value_share": round(negative_risk_secondary_leg_value / max(total_value, 1.0), 3),
        "negative_risk_condition_ratio": round(negative_risk_total / max(total, 1), 3),
        "negative_risk_paired_ratio": round(negative_risk_paired / max(negative_risk_total, 1), 3),
        "paired_examples": [item["title"] for item in paired[:3]],
    }


def _event_family_metrics(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Group rows by shared event family (`eventSlug`) to catch multi-outcome baskets.

    Polymarket negative-risk / multi-outcome events often appear as many
    conditionIds under one event family. Condition-level pairing misses this.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        family_key = row.get("eventSlug") or row.get("slug") or row.get("conditionId") or row.get("title")
        if not family_key:
            continue
        item = grouped.setdefault(
            str(family_key),
            {
                "titles": set(),
                "conditions": set(),
                "negative_risk": bool(row.get("negativeRisk")),
                "value": 0.0,
            },
        )
        if row.get("title"):
            item["titles"].add(str(row.get("title")))
        if row.get("conditionId"):
            item["conditions"].add(str(row.get("conditionId")))
        item["negative_risk"] = item["negative_risk"] or bool(row.get("negativeRisk"))
        item["value"] += _float(
            row.get("currentValue")
            or row.get("initialValue")
            or row.get("usdcSize")
            or row.get("size")
        )

    families = list(grouped.values())
    total = len(families)
    total_value = sum(item["value"] for item in families)
    multi_condition = [item for item in families if len(item["conditions"]) >= 2]
    negative_risk_families = [item for item in families if item["negative_risk"]]
    negative_risk_multi = [item for item in multi_condition if item["negative_risk"]]
    multi_value = sum(item["value"] for item in multi_condition)
    negative_risk_multi_value = sum(item["value"] for item in negative_risk_multi)

    return {
        "total_families": total,
        "multi_condition_families": len(multi_condition),
        "multi_condition_family_ratio": round(len(multi_condition) / max(total, 1), 3),
        "multi_condition_family_value_share": round(multi_value / max(total_value, 1.0), 3),
        "negative_risk_family_ratio": round(len(negative_risk_families) / max(total, 1), 3),
        "negative_risk_family_value_share": round(negative_risk_multi_value / max(total_value, 1.0), 3),
        "max_family_value_share": round(max((item["value"] for item in families), default=0.0) / max(total_value, 1.0), 3),
        "family_examples": [
            sorted(item["titles"])[0]
            for item in sorted(multi_condition, key=lambda item: item["value"], reverse=True)[:3]
            if item["titles"]
        ],
    }


# ---------------------------------------------------------------------------
# Tag-based domain metrics (Stage 2 of wallet-taxonomy migration)
# ---------------------------------------------------------------------------
# Replaces the coarse _classify_title → {sports, political, crypto, other}
# pipeline with opaque gamma tag slugs read from market_tags.
# Both coexist during the shadow period (Stages 2–3).
# Opaque-string handling: no function in this block branches on a specific
# tag value.


def _fetch_tag_map(
    condition_ids: Iterable[str],
    *,
    db_path: str | None = None,
) -> Dict[str, frozenset[str]]:
    """Pre-fetch tags for a batch of conditions in one SQL pass.

    Returns `{condition_id: frozenset(tags)}`. Conditions with no rows in
    market_tags are simply absent from the returned dict; callers treat
    absence as unclassified.
    """
    cid_list = list({str(c).strip().lower() for c in condition_ids if c})
    if not cid_list:
        return {}
    try:
        import sqlite3
        from db import get_db_path
        path = db_path or get_db_path()
        conn = sqlite3.connect(path, timeout=5)
        try:
            placeholders = ",".join("?" for _ in cid_list)
            rows = conn.execute(
                f"SELECT condition_id, tag FROM market_tags WHERE condition_id IN ({placeholders})",
                cid_list,
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive
        logging.warning("_fetch_tag_map failed: %s", exc)
        return {}

    out: Dict[str, set[str]] = defaultdict(set)
    for cid, tag in rows:
        if cid and tag:
            out[str(cid).strip().lower()].add(str(tag))
    return {k: frozenset(v) for k, v in out.items()}


def _tags_for_row(row: Dict[str, Any], tag_map: Dict[str, frozenset[str]]) -> frozenset[str]:
    """Look up tags for a row's conditionId; empty frozenset on miss."""
    cid = row.get("conditionId")
    if not cid:
        return frozenset()
    return tag_map.get(str(cid).strip().lower(), frozenset())


def _tag_distribution(
    rows: Iterable[Dict[str, Any]],
    tag_map: Dict[str, frozenset[str]],
    *,
    weight_fn: Any = None,
) -> Dict[str, Any]:
    """Build the §4.2 tag envelope from a row iterable.

    `weight_fn(row) -> float` optionally produces a per-row weight. Default
    (None) is equal-weight. For PnL-weighted views, pass
    `lambda r: abs(float(r.get("realized_pnl") or 0))`.

    A row with multiple tags contributes its full weight to EACH tag's
    count (multi-tag membership), but its denominator contribution (total
    rows) stays 1. A row with zero tags is counted under
    `unclassified_weight`.
    """
    tag_weights: Dict[str, float] = defaultdict(float)
    titles: set[str] = set()
    total_weight = 0.0
    unclassified_weight = 0.0

    for row in rows:
        w = 1.0 if weight_fn is None else float(weight_fn(row) or 0)
        if w <= 0:
            continue
        title = str(row.get("title") or "")
        if title:
            titles.add(title)
        tags = _tags_for_row(row, tag_map)
        total_weight += w
        if not tags:
            unclassified_weight += w
            continue
        for tag in tags:
            tag_weights[tag] += w

    if total_weight <= 0:
        return {
            "tag_fill_counts": {},
            "primary_tag": None,
            "primary_tag_share": 0.0,
            "distinct_tag_count": 0,
            "distinct_titles": len(titles),
            "unclassified_share": 0.0,
            "top_tags": [],
            "sample_size": 0,
        }

    ordered = sorted(tag_weights.items(), key=lambda kv: (-kv[1], kv[0]))
    primary_tag, primary_weight = (ordered[0] if ordered else (None, 0.0))
    top_tags = [
        {
            "tag": tag,
            "count": round(w, 3) if weight_fn else int(w),
            "share": round(w / total_weight, 3),
        }
        for tag, w in ordered[:5]
    ]

    return {
        "tag_fill_counts": {
            tag: round(w, 3) if weight_fn else int(w) for tag, w in ordered
        },
        "primary_tag": primary_tag,
        "primary_tag_share": round(primary_weight / total_weight, 3) if primary_tag else 0.0,
        "distinct_tag_count": len(ordered),
        "distinct_titles": len(titles),
        "unclassified_share": round(unclassified_weight / total_weight, 3),
        "top_tags": top_tags,
        "sample_size": int(total_weight) if weight_fn is None else round(total_weight, 2),
    }


def _domain_tags_horizon_metrics(
    trades: Iterable[Dict[str, Any]],
    positions: Iterable[Dict[str, Any]],
    resolved: List[Dict[str, Any]],
    tag_map: Dict[str, frozenset[str]],
) -> Dict[str, Any]:
    """Three-horizon tag-based domain split, mirror of _domain_horizon_metrics.

    Same motivation as the legacy function (SwissMiss rotation detection)
    but without the hardcoded keyword classifier. Each horizon reports the
    full §4.2 envelope; alignment + rotation flags compare primary_tag.

    - `recent_trade` weights by trade row count
    - `live_book` weights by open position row count
    - `realized_pnl` weights by absolute realized PnL
    """
    recent = _tag_distribution(trades, tag_map)
    live = _tag_distribution(positions, tag_map)
    # resolved rows from _resolved_position_summaries carry condition_id
    # (snake_case), not conditionId. Adapt without mutating the caller's list.
    adapted_resolved = [
        {**r, "conditionId": r.get("conditionId") or r.get("condition_id")}
        for r in resolved
    ]
    realized = _tag_distribution(
        adapted_resolved,
        tag_map,
        weight_fn=lambda r: abs(_float(r.get("realized_pnl"))),
    )

    primary_recent = recent["primary_tag"]
    primary_live = live["primary_tag"]
    primary_realized = realized["primary_tag"]

    def _agree(a: Any, b: Any) -> bool | None:
        if a is None or b is None:
            return None
        return a == b

    # Overlap: Jaccard on top-tag sets where both sides have tags.
    def _overlap(dist_a: Dict[str, Any], dist_b: Dict[str, Any]) -> float | None:
        a = {t["tag"] for t in dist_a.get("top_tags", [])}
        b = {t["tag"] for t in dist_b.get("top_tags", [])}
        if not a or not b:
            return None
        inter = len(a & b)
        union = len(a | b)
        return round(inter / union, 3) if union else 0.0

    recent_live = _agree(primary_recent, primary_live)
    recent_realized = _agree(primary_recent, primary_realized)
    live_realized = _agree(primary_live, primary_realized)
    top_overlap = _overlap(recent, live)

    rotation_signal = (
        recent_live is False
        or (top_overlap is not None and top_overlap < 0.3)
    )

    return {
        "recent_trade": recent,
        "live_book": live,
        "realized_pnl": realized,
        "alignment": {
            "recent_live_primary_match": recent_live,
            "recent_realized_primary_match": recent_realized,
            "live_realized_primary_match": live_realized,
            "top_tag_overlap_recent_live_jaccard": top_overlap,
        },
        "rotation_signal": rotation_signal,
    }


def _trade_shape_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    buy_trades = [row for row in trades if row.get("side") == "BUY"]
    sell_trades = [row for row in trades if row.get("side") == "SELL"]

    price_counts = Counter(round(_float(row.get("price")), 2) for row in buy_trades if row.get("price") is not None)
    condition_counts = Counter(row.get("conditionId") for row in trades if row.get("conditionId"))
    buy_usdc = [_float(row.get("usdcSize")) for row in buy_trades if row.get("usdcSize") is not None]
    buy_usdc = [v for v in buy_usdc if v > 0]  # drop zero/negatives so CV is meaningful

    midpoint_fills = sum(1 for row in buy_trades if 0.49 <= _float(row.get("price")) <= 0.51)
    mean_buy_usdc = (sum(buy_usdc) / len(buy_usdc)) if buy_usdc else 0.0
    buy_usdc_stddev = pstdev(buy_usdc) if len(buy_usdc) >= 2 else 0.0
    # CV = stddev / mean. Low CV = consistent sizing (algo signature); high CV = conviction-sized (discretionary).
    usdc_size_cv = round(buy_usdc_stddev / mean_buy_usdc, 3) if mean_buy_usdc > 0 else 0.0
    return {
        "trade_count": len(trades),
        "buy_count": len(buy_trades),
        "sell_count": len(sell_trades),
        "sell_ratio": round(len(sell_trades) / max(len(trades), 1), 3),
        "avg_fills_per_condition": round(len(trades) / max(len(condition_counts), 1), 2),
        "top_condition_fill_share": round((condition_counts.most_common(1)[0][1] if condition_counts else 0) / max(len(trades), 1), 3),
        "price_repetition_ratio": round((price_counts.most_common(1)[0][1] if price_counts else 0) / max(len(buy_trades), 1), 3),
        "midpoint_buy_ratio": round(midpoint_fills / max(len(buy_trades), 1), 3),
        "median_buy_usdc": round(median(buy_usdc), 2) if buy_usdc else 0.0,
        "mean_buy_usdc": round(mean_buy_usdc, 2),
        "usdc_size_cv": usdc_size_cv,
    }


def _timing_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Inter-trade regularity + time-of-day signature.

    inter_trade_interval_cv distinguishes algorithmic (low CV, regular cadence)
    from discretionary (high CV, bursty) actors. Time-of-day histogram surfaces
    24/7 algo patterns vs US-hours retail patterns.

    Both fall through to safe defaults on tiny samples (< 5 trades) — CV on a
    2-row series is noise.
    """
    ts_values = sorted(int(row.get("timestamp") or 0) for row in trades if row.get("timestamp"))
    ts_values = [t for t in ts_values if t > 0]

    inter_trade_cv: float = 0.0
    if len(ts_values) >= 5:
        gaps = [ts_values[i] - ts_values[i - 1] for i in range(1, len(ts_values))]
        mean_gap = sum(gaps) / len(gaps)
        if mean_gap > 0:
            gap_stddev = pstdev(gaps)
            inter_trade_cv = round(gap_stddev / mean_gap, 3)

    # 24-bin hour-of-day histogram (UTC).
    hour_histogram = [0] * 24
    for ts in ts_values:
        try:
            hour_histogram[datetime.fromtimestamp(ts, tz=timezone.utc).hour] += 1
        except (ValueError, OSError):
            continue
    total = sum(hour_histogram) or 1
    # Concentration in "US market hours" (13-21 UTC ~= 9a-5p ET) vs rest.
    us_hours_share = round(sum(hour_histogram[13:22]) / total, 3) if total else 0.0

    first_ts = ts_values[0] if ts_values else 0
    last_ts = ts_values[-1] if ts_values else 0
    lifespan_days = round((last_ts - first_ts) / 86400.0, 2) if ts_values else 0.0

    return {
        "inter_trade_interval_cv": inter_trade_cv,
        "time_of_day_histogram": hour_histogram,
        "us_hours_share": us_hours_share,
        "lifespan_days": lifespan_days,
        "first_trade_ts": first_ts,
        "last_trade_ts": last_ts,
    }


def _per_position_pnl_metrics(activity: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Realized PnL distribution from observed BUY/SELL/REDEEM cashflows.

    This intentionally shares the same closure logic as Layer 4:
    - REDEEM closes a condition
    - fully flattened SELL path also counts as realized

    Without SELL-aware realization, active discretionary wallets look much
    worse than they are because sell proceeds are ignored.
    """
    pnl_values = [_float(item.get("realized_pnl")) for item in _resolved_position_summaries(activity)]

    if not pnl_values:
        return {
            "resolved_position_count": 0,
            "median_pnl": 0.0,
            "mean_pnl": 0.0,
            "pnl_stddev": 0.0,
            "pnl_cv_abs": 0.0,
            "positive_share": 0.0,
        }

    mean_pnl = sum(pnl_values) / len(pnl_values)
    pnl_stddev = pstdev(pnl_values) if len(pnl_values) >= 2 else 0.0
    # CV on abs(mean) so we don't flip sign; insiders / conviction bettors = high dispersion, MM/arb = low.
    pnl_cv_abs = round(pnl_stddev / abs(mean_pnl), 3) if abs(mean_pnl) > 0 else 0.0
    positive_count = sum(1 for v in pnl_values if v > 0)
    return {
        "resolved_position_count": len(pnl_values),
        "median_pnl": round(median(pnl_values), 2),
        "mean_pnl": round(mean_pnl, 2),
        "pnl_stddev": round(pnl_stddev, 2),
        "pnl_cv_abs": pnl_cv_abs,
        "positive_share": round(positive_count / len(pnl_values), 3),
    }


def _redeem_metrics(activity: List[Dict[str, Any]]) -> Dict[str, Any]:
    redeem_events = [row for row in activity if row.get("type") == "REDEEM"]
    return {
        "redeem_count": len(redeem_events),
        "redeem_ratio": round(len(redeem_events) / max(len(activity), 1), 3),
    }


def classify_archetype(features: Dict[str, Any]) -> Dict[str, Any]:
    live_pairs = features["position_pairing"]["paired_condition_ratio"]
    paired_value_share = features["position_pairing"]["paired_value_share"]
    binary_live_pairs = _float(features["position_pairing"].get("binary_paired_condition_ratio", live_pairs))
    binary_secondary_leg_value_share = _float(
        features["position_pairing"].get("binary_secondary_leg_value_share", paired_value_share)
    )
    trade_pairs = features["trade_pairing"]["paired_condition_ratio"]
    sell_ratio = features["trade_shape"]["sell_ratio"]
    # `primary_domain` / `concentration` removed Stage 5 — their only reader
    # was the deprecated sports_specialist_accumulator rule (now tag-based).
    price_repetition_ratio = features["trade_shape"]["price_repetition_ratio"]
    live_family_ratio = _float(features.get("position_event_families", {}).get("multi_condition_family_ratio"))
    live_family_value_share = _float(features.get("position_event_families", {}).get("multi_condition_family_value_share"))
    live_negative_risk_family_value_share = _float(
        features.get("position_event_families", {}).get("negative_risk_family_value_share")
    )

    if binary_live_pairs >= 0.45 or binary_secondary_leg_value_share >= 0.25:
        reasons = [
            f"binary live paired-condition ratio={binary_live_pairs:.1%}",
            f"binary secondary-leg value share={binary_secondary_leg_value_share:.1%}",
        ]
        return {
            "archetype": "paired_outcome_arb",
            "confidence": "high" if binary_live_pairs >= 0.6 or binary_secondary_leg_value_share >= 0.4 else "medium",
            "copy_policy": "exclude",
            "rationale": reasons,
        }

    if trade_pairs >= 0.2 or sell_ratio >= 0.35:
        reasons = [
            f"trade paired-condition ratio={trade_pairs:.1%}",
            f"sell ratio={sell_ratio:.1%}",
        ]
        return {
            "archetype": "two_sided_inventory_trader",
            "confidence": "medium",
            "copy_policy": "exclude",
            "rationale": reasons,
        }

    if live_negative_risk_family_value_share >= 0.3 and live_family_ratio >= 0.08 and trade_pairs < 0.15 and sell_ratio < 0.2:
        reasons = [
            f"negative-risk family value share={live_negative_risk_family_value_share:.1%}",
            f"multi-condition event-family ratio={live_family_ratio:.1%}",
            f"multi-condition event-family value share={live_family_value_share:.1%}",
        ]
        return {
            "archetype": "multi_outcome_basket_builder",
            "confidence": "medium" if live_negative_risk_family_value_share >= 0.45 or live_family_ratio >= 0.15 else "low",
            "copy_policy": "manual_review",
            "rationale": reasons,
        }

    # Stage 4 cutover (wallet-taxonomy migration): tag-based specialist rule
    # replaces the legacy keyword-based sports_specialist_accumulator.
    # Fires on any wallet with single-tag concentration on live_book OR
    # recent_trade horizon — not restricted to a hardcoded tag slug.
    tag_specialist = classify_archetype_tag_shadow(features)
    if tag_specialist is not None:
        return tag_specialist

    if trade_pairs < 0.1 and price_repetition_ratio < 0.2:
        reasons = [
            f"trade paired-condition ratio={trade_pairs:.1%}",
            f"price repetition={price_repetition_ratio:.1%}",
        ]
        return {
            "archetype": "directional_discretionary",
            "confidence": "low",
            "copy_policy": "study_only",
            "rationale": reasons,
        }

    return {
        "archetype": "mixed_unresolved",
        "confidence": "low",
        "copy_policy": "manual_review",
        "rationale": [
            f"live paired-condition ratio={live_pairs:.1%}",
            f"trade paired-condition ratio={trade_pairs:.1%}",
        ],
    }


_LAYER1_TERMINAL = {"paired_outcome_arb", "two_sided_inventory_trader"}


# ---------------------------------------------------------------------------
# Stage 3 of wallet-taxonomy migration — SHADOW classifier
# ---------------------------------------------------------------------------
# Emits alongside classify_archetype. No consumer reads this yet. Purpose:
# collect a side-by-side diff of legacy vs tag-based classifications so
# Stage 4 cutover can be reviewed before merge (spec §6).
#
# Rule shape matches the legacy `sports_specialist_accumulator` but is
# fully data-driven: the rule fires on any wallet whose live book OR
# recent trade tape shows concentration in a single gamma tag with
# reliable tag coverage. The tag slug is reported as evidence but NEVER
# used in the rule logic. Future tag vocabularies work unchanged.


def _tag_specialist_horizon_passes(horizon: Dict[str, Any]) -> bool:
    """True iff this horizon's tag distribution looks like specialist behaviour.

    Rule shape:
    - primary_tag_share >= 0.45 — wallet concentrates on ONE tag
    - unclassified_share < 0.15 — tag data is reliable for this horizon
    - sample_size >= 10 — not a noise read

    `distinct_tag_count` is NOT used as a gate. Polymarket tags individual
    markets granularly (a single politics market carries politics +
    us-elections + trump + republicans + primaries + … as separate tags),
    so even a focused politics-only wallet has 50–150 distinct tag slugs
    across its fills. The meaningful specialization signal is the TOP
    tag's fill share, not the count of distinct tags. (`distinct_tag_count`
    remains in the envelope for transparency / reviewer inspection.)
    """
    # `or`-fallbacks treat 0.0 as falsy — use dict.get default (explicit None
    # check) to avoid unclassified_share=0.0 being silently replaced by 1.0.
    try:
        s_raw = horizon.get("sample_size")
        if s_raw is None or float(s_raw) < 10:
            return False
        pts_raw = horizon.get("primary_tag_share")
        if pts_raw is None or float(pts_raw) < 0.45:
            return False
        us_raw = horizon.get("unclassified_share")
        if us_raw is None:
            return False  # absent coverage signal → don't promote
        if float(us_raw) >= 0.15:
            return False
        return True
    except (TypeError, ValueError):
        return False


def classify_archetype_tag_shadow(features: Dict[str, Any]) -> Dict[str, Any] | None:
    """Tag-based Layer 1 classification mirror. Returns None when the rule
    doesn't apply (wallet doesn't exhibit single-tag specialisation on any
    horizon, or tag coverage is too sparse to claim anything).

    Reads from `domain_tags_horizons` (Stage 2 output). No dependency on
    the legacy `_classify_title` keyword classifier.
    """
    horizons = features.get("domain_tags_horizons") or {}
    if not horizons:
        return None

    trade_pairs = _float(features.get("trade_pairing", {}).get("paired_condition_ratio"))
    sell_ratio = _float(features.get("trade_shape", {}).get("sell_ratio"))
    if trade_pairs >= 0.15 or sell_ratio >= 0.15:
        return None  # wallet isn't holding — not a specialist_accumulator

    live = horizons.get("live_book") or {}
    recent = horizons.get("recent_trade") or {}
    live_pass = _tag_specialist_horizon_passes(live)
    recent_pass = _tag_specialist_horizon_passes(recent)
    if not (live_pass or recent_pass):
        return None

    # Use the horizon that passed with higher confidence (higher primary_tag_share,
    # lower unclassified_share). If both pass, live_book wins — current positions
    # are the authoritative view of what the wallet IS doing.
    chosen = live if live_pass else recent
    chosen_name = "live_book" if live_pass else "recent_trade"

    primary_tag = chosen.get("primary_tag")
    primary_share = float(chosen.get("primary_tag_share") or 0)
    distinct = int(chosen.get("distinct_tag_count") or 0)
    unclassified = float(chosen.get("unclassified_share") or 0)
    n = int(chosen.get("sample_size") or 0)

    # Confidence: strong share + low tag diversity + low unclassified + both horizons agreeing
    both_pass = live_pass and recent_pass
    strong_share = primary_share >= 0.70
    confidence = "high" if (both_pass and strong_share) else ("medium" if both_pass or strong_share else "low")

    rationale = [
        f"tag_specialist via {chosen_name}: primary_tag={primary_tag}, share={primary_share:.1%}",
        f"distinct_tag_count={distinct}, unclassified_share={unclassified:.1%}, n={n}",
        f"trade_paired_ratio={trade_pairs:.1%}, sell_ratio={sell_ratio:.1%}",
    ]
    if both_pass:
        rationale.append("both live_book and recent_trade horizons confirm specialisation")

    return {
        "archetype": "tag_specialist_accumulator",
        "confidence": confidence,
        "copy_policy": "study_only",
        "rationale": rationale,
        "primary_tag": primary_tag,   # evidence field; callers MUST NOT branch on value
        "chosen_horizon": chosen_name,
    }


def classify_archetype_layer2(features: Dict[str, Any]) -> Dict[str, Any] | None:
    """Layer 2 — intent classification. Returns None if Layer 1 already excluded.

    Runs only on wallets that pass Layer 1 (i.e., not arb, not MM). Layer 2
    distinguishes INTENT among survivors: insider-shape, thesis specialist,
    structural-bias exploiter, noise, or directional_unknown.

    Conservative by design: per constraint set 2026-04-21, no automatic
    upgrade beyond `validated_copy` on the first pass. Every positive
    Layer 2 outcome flags for human review before any copy strategy is created.

    Thresholds are initial and judgment-based; re-tune once labeled samples accumulate.
    """
    layer1 = features.get("classification", {})
    if layer1.get("archetype") in _LAYER1_TERMINAL:
        return None

    timing = features.get("timing", {})
    # domain → domain_tags (Stage 5 migration). distinct_titles is preserved
    # in domain_tags; concentration is equivalent to primary_tag_share.
    domain_tags = features.get("domain_tags", {})
    trade_shape = features.get("trade_shape", {})
    redeems = features.get("redeems", {})
    returns_ = features.get("returns", {})

    # Wallet-level tenure — comes from full-history pagination (not the
    # 500-row sample's `lifespan_days`, which measures intra-sample window).
    # Fall back to sample lifespan only if wallet-level lookup failed.
    wallet_lifetime_days = _float(timing.get("wallet_lifetime_days"))
    lifespan_days = wallet_lifetime_days if wallet_lifetime_days > 0 else _float(timing.get("lifespan_days"))
    distinct_titles = int(domain_tags.get("distinct_titles") or 0)
    concentration = _float(domain_tags.get("primary_tag_share"))
    top_condition_fill = _float(trade_shape.get("top_condition_fill_share"))
    price_rep = _float(trade_shape.get("price_repetition_ratio"))
    sell_ratio = _float(trade_shape.get("sell_ratio"))
    usdc_cv = _float(trade_shape.get("usdc_size_cv"))
    redeem_ratio = _float(redeems.get("redeem_ratio"))
    resolved_count = int(returns_.get("resolved_position_count") or 0)
    pnl_cv_abs = _float(returns_.get("pnl_cv_abs"))

    # insider-shape: fresh wallet, narrow focus, heavy concentration in one market
    if 0 < lifespan_days < 30 and distinct_titles <= 3 and top_condition_fill > 0.5:
        return {
            "archetype": "insider_shape",
            "confidence": "low",  # Always low — real insider detection requires pre-event timing proof we don't have yet.
            "copy_policy": "exclude",
            "notes": "flag for specialist insider research; not safe to copy on these features alone",
            "rationale": [
                f"lifespan_days={lifespan_days:.0f}",
                f"distinct_titles={distinct_titles}",
                f"top_condition_fill_share={top_condition_fill:.1%}",
            ],
        }

    # structural_bias_exploiter: systematic pattern across many resolutions.
    # Checked BEFORE thesis_specialist because its conditions are a strict
    # superset (same tenure/concentration signals PLUS consistent sizing and
    # price repetition — the algorithmic signatures). Without this ordering,
    # systematic exploiters get mislabeled as high-end thesis specialists.
    if (price_rep > 0.2 and concentration > 0.6 and resolved_count > 10
            and usdc_cv < 0.5 and sell_ratio < 0.15):
        return {
            "archetype": "structural_bias_exploiter",
            "confidence": "medium",
            "copy_policy": "reverse_engineer_candidate",
            "notes": "candidate for human review; potential plugin extraction target",
            "rationale": [
                f"price_repetition_ratio={price_rep:.1%}",
                f"domain_concentration={concentration:.1%}",
                f"usdc_size_cv={usdc_cv}",
                f"resolved_positions={resolved_count}",
            ],
        }

    # thesis_specialist: tenured, domain-focused, holds to resolution, conviction-sized
    if (lifespan_days > 90 and concentration > 0.5 and sell_ratio < 0.1
            and redeem_ratio > 0.05 and resolved_count >= 5):
        return {
            "archetype": "thesis_specialist",
            "confidence": "medium" if pnl_cv_abs > 0.3 else "low",
            "copy_policy": "validated_copy",
            "notes": "requires human review + thesis check before any copy strategy",
            "rationale": [
                f"lifespan_days={lifespan_days:.0f}",
                f"domain_concentration={concentration:.1%}",
                f"redeem_ratio={redeem_ratio:.1%}",
                f"sell_ratio={sell_ratio:.1%}",
                f"resolved_positions={resolved_count}",
            ],
        }

    # noise: scattered across many markets with no concentration and tiny sample
    if distinct_titles > 20 and concentration < 0.3 and resolved_count <= 3:
        return {
            "archetype": "noise",
            "confidence": "low",
            "copy_policy": "exclude",
            "notes": "scattered activity with no discernible pattern",
            "rationale": [
                f"distinct_titles={distinct_titles}",
                f"domain_concentration={concentration:.1%}",
                f"resolved_positions={resolved_count}",
            ],
        }

    # directional_unknown: passed Layer 1 but doesn't fit any named intent cleanly
    return {
        "archetype": "directional_unknown",
        "confidence": "low",
        "copy_policy": "manual_review",
        "notes": "passed mechanism exclusion but intent unclear; human review required",
        "rationale": [
            f"lifespan_days={lifespan_days:.0f}",
            f"domain_concentration={concentration:.1%}",
            f"resolved_positions={resolved_count}",
        ],
    }


_OBSERVATION_BLOCKS = (
    "counts",
    "trade_pairing",
    "position_pairing",
    "trade_event_families",
    "position_event_families",
    "domain_tags",
    "domain_tags_horizons",
    "trade_shape",
    "redeems",
    "timing",
    "returns",
)


def _build_observations(features: Dict[str, Any]) -> Dict[str, Any]:
    """Schema-stable Layer 1 payload.

    The extractor still emits legacy top-level blocks for backward
    compatibility, but `observations` is the forward path for the wallet
    understanding brain: explicitly observational, assumption-light, and
    free of policy/action labels.
    """
    return {name: features[name] for name in _OBSERVATION_BLOCKS}


def _build_behavior_pattern(features: Dict[str, Any]) -> Dict[str, Any]:
    """Schema-stable Layer 2 payload.

    `primary_pattern` is the stable mechanical pattern. The current second
    classifier remains useful, but it carries more interpretation than the
    design ultimately wants, so we keep it under `secondary_pattern` and mark
    it explicitly provisional.
    """
    primary = features.get("classification", {})
    secondary = features.get("classification_layer2")
    payload: Dict[str, Any] = {
        "primary_pattern": {
            "label": primary.get("archetype", "unknown"),
            "confidence": primary.get("confidence", "low"),
            "rationale": list(primary.get("rationale") or []),
            "terminal": primary.get("archetype") in _LAYER1_TERMINAL,
        },
        "secondary_pattern": None,
    }
    if secondary is not None:
        payload["secondary_pattern"] = {
            "label": secondary.get("archetype", "unknown"),
            "confidence": secondary.get("confidence", "low"),
            "rationale": list(secondary.get("rationale") or []),
            "notes": secondary.get("notes", ""),
            "provisional": True,
        }
    return payload


def _edge_signal(level: str, rationale: List[str], notes: str = "") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "level": level,
        "rationale": rationale,
    }
    if notes:
        payload["notes"] = notes
    return payload


def _build_edge_hypotheses(features: Dict[str, Any]) -> Dict[str, Any]:
    """First-pass Layer 3 edge vector using only currently measurable signals.

    This stays conservative on purpose. We only elevate a dimension when the
    existing observations meaningfully support it; otherwise we return
    `unknown` with a reason rather than inventing a narrative.
    """
    primary = (features.get("classification") or {}).get("archetype", "unknown")
    secondary = (features.get("classification_layer2") or {}).get("archetype", "")

    counts = features.get("counts", {})
    trade_pairing = features.get("trade_pairing", {})
    position_pairing = features.get("position_pairing", {})
    position_event_families = features.get("position_event_families", {})
    domain_tags = features.get("domain_tags", {})
    trade_shape = features.get("trade_shape", {})
    redeems = features.get("redeems", {})
    timing = features.get("timing", {})
    returns_ = features.get("returns", {})

    trade_count = int(trade_shape.get("trade_count") or counts.get("trade_records") or 0)
    live_positions = int(counts.get("live_positions") or 0)
    distinct_titles = int(domain_tags.get("distinct_titles") or 0)
    concentration = _float(domain_tags.get("primary_tag_share"))
    midpoint_buy_ratio = _float(trade_shape.get("midpoint_buy_ratio"))
    top_condition_fill = _float(trade_shape.get("top_condition_fill_share"))
    price_repetition = _float(trade_shape.get("price_repetition_ratio"))
    sell_ratio = _float(trade_shape.get("sell_ratio"))
    usdc_size_cv = _float(trade_shape.get("usdc_size_cv"))
    redeem_ratio = _float(redeems.get("redeem_ratio"))
    inter_trade_cv = _float(timing.get("inter_trade_interval_cv"))
    wallet_lifetime_days = _float(timing.get("wallet_lifetime_days"))
    positive_share = _float(returns_.get("positive_share"))
    resolved_count = int(returns_.get("resolved_position_count") or 0)
    pnl_cv_abs = _float(returns_.get("pnl_cv_abs"))
    trade_pair_ratio = _float(trade_pairing.get("paired_condition_ratio"))
    live_pair_ratio = _float(position_pairing.get("paired_condition_ratio"))
    negative_risk_ratio = _float(position_pairing.get("negative_risk_condition_ratio"))
    live_family_ratio = _float(position_event_families.get("multi_condition_family_ratio"))
    live_negative_risk_family_value_share = _float(position_event_families.get("negative_risk_family_value_share"))
    max_family_value_share = _float(position_event_families.get("max_family_value_share"))

    if resolved_count < 5:
        market_selection = _edge_signal(
            "unknown",
            [f"resolved_positions={resolved_count}", "resolved sample too small to infer repeatable selection edge"],
        )
    elif secondary == "noise" or positive_share <= 0.4:
        market_selection = _edge_signal(
            "low",
            [f"positive_share={positive_share:.1%}", f"secondary_pattern={secondary or 'none'}"],
        )
    elif resolved_count >= 10 and positive_share >= 0.6 and concentration >= 0.45:
        market_selection = _edge_signal(
            "medium",
            [
                f"positive_share={positive_share:.1%}",
                f"resolved_positions={resolved_count}",
                f"domain_concentration={concentration:.1%}",
            ],
        )
    else:
        market_selection = _edge_signal(
            "unknown",
            [f"positive_share={positive_share:.1%}", "selection quality not separable from execution with current data"],
        )

    if resolved_count < 10:
        entry_timing = _edge_signal(
            "unknown",
            [f"resolved_positions={resolved_count}", "no path-level repricing data yet"],
            notes="current extractor sees entry prices, not subsequent market path",
        )
    elif midpoint_buy_ratio <= 0.15 and positive_share >= 0.6:
        entry_timing = _edge_signal(
            "medium",
            [f"midpoint_buy_ratio={midpoint_buy_ratio:.1%}", f"positive_share={positive_share:.1%}"],
            notes="suggestive only until path-aware replay exists",
        )
    elif midpoint_buy_ratio >= 0.4 and positive_share <= 0.5:
        entry_timing = _edge_signal(
            "low",
            [f"midpoint_buy_ratio={midpoint_buy_ratio:.1%}", f"positive_share={positive_share:.1%}"],
        )
    else:
        entry_timing = _edge_signal(
            "unknown",
            [f"midpoint_buy_ratio={midpoint_buy_ratio:.1%}", "entry quality not attributable without later price path"],
            notes="needs price-path replay or line-move context",
        )

    if trade_count < 10:
        sizing = _edge_signal(
            "unknown",
            [f"trade_count={trade_count}", "not enough trade observations to assess sizing behavior"],
        )
    elif usdc_size_cv >= 4.0 and pnl_cv_abs >= 0.7 and resolved_count >= 10 and positive_share >= 0.6:
        sizing = _edge_signal(
            "high",
            [
                f"usdc_size_cv={usdc_size_cv:.2f}",
                f"pnl_cv_abs={pnl_cv_abs:.2f}",
                f"resolved_positions={resolved_count}",
            ],
        )
    elif usdc_size_cv >= 1.5 and resolved_count >= 5:
        sizing = _edge_signal(
            "medium",
            [f"usdc_size_cv={usdc_size_cv:.2f}", f"resolved_positions={resolved_count}"],
        )
    elif usdc_size_cv <= 0.3:
        sizing = _edge_signal(
            "low",
            [f"usdc_size_cv={usdc_size_cv:.2f}", "size pattern is closer to uniform than conviction-scaled"],
        )
    else:
        sizing = _edge_signal(
            "unknown",
            [f"usdc_size_cv={usdc_size_cv:.2f}", "sizing variance exists but outcome linkage is weak"],
        )

    if resolved_count < 10:
        exit_management = _edge_signal(
            "unknown",
            [f"resolved_positions={resolved_count}", "exit skill cannot be separated from simple hold behavior yet"],
            notes="needs naive-hold comparison before stronger claims",
        )
    elif 0.03 <= sell_ratio <= 0.2 and positive_share >= 0.6:
        exit_management = _edge_signal(
            "medium",
            [f"sell_ratio={sell_ratio:.1%}", f"positive_share={positive_share:.1%}"],
            notes="suggests meaningful discretionary exits, not just pure redeem behavior",
        )
    elif sell_ratio < 0.02 and redeem_ratio > 0.03:
        exit_management = _edge_signal(
            "low",
            [f"sell_ratio={sell_ratio:.1%}", f"redeem_ratio={redeem_ratio:.1%}"],
        )
    elif sell_ratio >= 0.35:
        exit_management = _edge_signal(
            "low",
            [f"sell_ratio={sell_ratio:.1%}", "high churn makes good exits hard to distinguish from inventory management"],
        )
    else:
        exit_management = _edge_signal(
            "unknown",
            [f"sell_ratio={sell_ratio:.1%}", f"redeem_ratio={redeem_ratio:.1%}"],
            notes="needs condition-level hold-vs-sell attribution",
        )

    if primary == "paired_outcome_arb":
        structural_exploitation = _edge_signal(
            "high",
            [f"primary_pattern={primary}", f"live_paired_condition_ratio={live_pair_ratio:.1%}"],
        )
    elif secondary == "structural_bias_exploiter":
        structural_exploitation = _edge_signal(
            "high",
            [
                f"secondary_pattern={secondary}",
                f"price_repetition_ratio={price_repetition:.1%}",
                f"usdc_size_cv={usdc_size_cv:.2f}",
            ],
        )
    elif price_repetition >= 0.25 and usdc_size_cv <= 0.5 and inter_trade_cv <= 1.0:
        structural_exploitation = _edge_signal(
            "medium",
            [
                f"price_repetition_ratio={price_repetition:.1%}",
                f"usdc_size_cv={usdc_size_cv:.2f}",
                f"inter_trade_interval_cv={inter_trade_cv:.2f}",
            ],
        )
    elif usdc_size_cv > 1.0 and inter_trade_cv > 1.5 and trade_pair_ratio < 0.1:
        structural_exploitation = _edge_signal(
            "low",
            [
                f"usdc_size_cv={usdc_size_cv:.2f}",
                f"inter_trade_interval_cv={inter_trade_cv:.2f}",
                f"trade_paired_condition_ratio={trade_pair_ratio:.1%}",
            ],
        )
    else:
        structural_exploitation = _edge_signal(
            "unknown",
            [
                f"price_repetition_ratio={price_repetition:.1%}",
                f"negative_risk_condition_ratio={negative_risk_ratio:.1%}",
            ],
        )

    if live_positions >= 10 and concentration >= 0.45 and top_condition_fill <= 0.35 and live_pair_ratio < 0.15:
        portfolio_construction = _edge_signal(
            "medium",
            [
                f"live_positions={live_positions}",
                f"domain_concentration={concentration:.1%}",
                f"top_condition_fill_share={top_condition_fill:.1%}",
            ],
        )
    elif live_negative_risk_family_value_share >= 0.25 and live_family_ratio >= 0.08:
        portfolio_construction = _edge_signal(
            "medium",
            [
                f"negative_risk_family_value_share={live_negative_risk_family_value_share:.1%}",
                f"multi_condition_family_ratio={live_family_ratio:.1%}",
                f"max_family_value_share={max_family_value_share:.1%}",
            ],
            notes="event-family baskets are meaningful in the live book; condition-level views understate this",
        )
    elif live_positions <= 2 and top_condition_fill >= 0.5:
        portfolio_construction = _edge_signal(
            "low",
            [f"live_positions={live_positions}", f"top_condition_fill_share={top_condition_fill:.1%}"],
        )
    else:
        portfolio_construction = _edge_signal(
            "unknown",
            [f"live_positions={live_positions}", "current features do not yet model cross-market book coherence directly"],
        )

    if secondary == "insider_shape" or (0 < wallet_lifetime_days < 30 and distinct_titles <= 3 and top_condition_fill > 0.5):
        informational_signature = _edge_signal(
            "medium",
            [
                f"wallet_lifetime_days={wallet_lifetime_days:.0f}",
                f"distinct_titles={distinct_titles}",
                f"top_condition_fill_share={top_condition_fill:.1%}",
            ],
            notes="signature only, not proof of privileged information",
        )
    elif wallet_lifetime_days >= 180 and distinct_titles >= 10:
        informational_signature = _edge_signal(
            "low",
            [f"wallet_lifetime_days={wallet_lifetime_days:.0f}", f"distinct_titles={distinct_titles}"],
        )
    else:
        informational_signature = _edge_signal(
            "unknown",
            [f"wallet_lifetime_days={wallet_lifetime_days:.0f}", f"distinct_titles={distinct_titles}"],
            notes="pre-event timing evidence is not yet modeled",
        )

    # Layer 3 consumes execution_profile drift evidence to promote
    # entry_timing_edge out of `unknown` per spec §6.6. Only runs when the
    # execution profile is populated (shortlist wallets only).
    entry_timing, exit_management = _apply_execution_profile_to_edges(
        features, entry_timing, exit_management
    )

    # Stage 3 of taxonomy migration: append tag-based rationale to
    # concentration-sensitive edges (market_selection, portfolio_construction)
    # WITHOUT changing their level. Consumers can read this for transparency
    # before Stage 4 cutover flips to tag-based rules.
    market_selection, portfolio_construction = _append_tag_rationale_to_edges(
        features, market_selection, portfolio_construction
    )

    return {
        "market_selection_edge": market_selection,
        "entry_timing_edge": entry_timing,
        "sizing_edge": sizing,
        "exit_management_edge": exit_management,
        "structural_exploitation_edge": structural_exploitation,
        "portfolio_construction_edge": portfolio_construction,
        "informational_edge_signature": informational_signature,
    }


def _append_tag_rationale_to_edges(
    features: Dict[str, Any],
    market_selection: Dict[str, Any],
    portfolio_construction: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Augment edge rationales with tag-based evidence. Levels unchanged.

    Stage 3 of the wallet-taxonomy migration: makes domain_tags evidence
    visible to reviewers without yet changing promotion logic. Stage 4
    replaces the legacy `domain.concentration`-based reads.
    """
    dt = features.get("domain_tags") or {}
    if not dt or not dt.get("primary_tag"):
        return market_selection, portfolio_construction

    primary_tag = dt.get("primary_tag")
    primary_share = float(dt.get("primary_tag_share") or 0)
    distinct = int(dt.get("distinct_tag_count") or 0)
    unclassified = float(dt.get("unclassified_share") or 0)

    line = (
        f"tag-based (shadow): primary_tag={primary_tag} share={primary_share:.1%} "
        f"distinct={distinct} unclassified={unclassified:.1%}"
    )

    def _with_rationale(edge: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(edge)
        out["rationale"] = list(edge.get("rationale") or []) + [line]
        return out

    return _with_rationale(market_selection), _with_rationale(portfolio_construction)


def _apply_execution_profile_to_edges(
    features: Dict[str, Any],
    entry_timing: Dict[str, Any],
    exit_management: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Spec §6.6. Consumes features['execution_profile'] to upgrade
    `entry_timing_edge` when drift evidence is strong.

    v1: `exit_management_edge` only gets drift_1h appended to rationale for
    reviewer cross-reference. A dedicated sell-side execution measurement
    lands in v2.
    """
    ep = features.get("execution_profile") or {}
    if ep.get("status") != "populated":
        return entry_timing, exit_management

    drift_1h = ep.get("drift_1h") or {}
    n_drift = int(drift_1h.get("n") or 0)
    tier = str(ep.get("confidence_tier") or "")

    if n_drift >= 10:
        dmean = float(drift_1h.get("mean") or 0.0)
        dmed = float(drift_1h.get("median") or 0.0)
        n_skew = int((ep.get("skew_vs_preceding_bar") or {}).get("n") or 0)

        if dmean >= 0.05 and dmed >= 0.03 and tier == "high":
            entry_timing = _edge_signal(
                "high",
                [
                    f"drift_1h_mean={dmean:+.3f}",
                    f"drift_1h_median={dmed:+.3f}",
                    f"n_drift={n_drift}",
                    f"n_skew={n_skew}",
                    f"confidence_tier={tier}",
                ],
            )
        elif dmean >= 0.02 and dmed >= 0.01:
            entry_timing = _edge_signal(
                "medium",
                [
                    f"drift_1h_mean={dmean:+.3f}",
                    f"drift_1h_median={dmed:+.3f}",
                    f"n_drift={n_drift}",
                ],
                # No path-replay caveat anymore — execution profile IS path evidence.
                notes="",
            )
        elif dmean <= -0.02:
            entry_timing = _edge_signal(
                "low",
                [
                    f"drift_1h_mean={dmean:+.3f}",
                    f"drift_1h_median={dmed:+.3f}",
                    f"n_drift={n_drift}",
                    "negative subsequent drift — fills are fading, not leading",
                ],
            )
        else:
            # Near-zero drift: keep heuristic level but append rationale.
            new_rationale = list(entry_timing.get("rationale") or [])
            new_rationale.append(
                f"drift_1h_mean={dmean:+.3f} (near-zero; execution data does not promote)"
            )
            entry_timing = dict(entry_timing)
            entry_timing["rationale"] = new_rationale

        # Exit-management cross-ref only (v1). Append for reviewer context.
        exit_rationale = list(exit_management.get("rationale") or [])
        exit_rationale.append(f"execution_profile.drift_1h_mean={dmean:+.3f}")
        exit_management = dict(exit_management)
        exit_management["rationale"] = exit_rationale

    return entry_timing, exit_management


def _aggregate_realized_pnl(rows: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        label = str(row.get(key) or "unknown")
        item = grouped.setdefault(
            label,
            {"positions": 0, "winner_count": 0, "loser_count": 0, "realized_pnl": 0.0},
        )
        pnl = _float(row.get("realized_pnl"))
        item["positions"] += 1
        item["realized_pnl"] += pnl
        if pnl > 0:
            item["winner_count"] += 1
        elif pnl < 0:
            item["loser_count"] += 1

    return {
        label: {
            "positions": item["positions"],
            "winner_count": item["winner_count"],
            "loser_count": item["loser_count"],
            "realized_pnl": round(item["realized_pnl"], 2),
        }
        for label, item in sorted(grouped.items())
    }


def _primary_tag_for_cid(
    condition_id: str, tag_map: Dict[str, frozenset[str]] | None
) -> str:
    """Deterministic per-condition tag for Layer 4 grouping.

    Returns `"unclassified"` when no tag_map or no tags for the condition.
    Otherwise returns the alphabetically first tag slug (deterministic,
    no global rarity lookup required for Stage 5). Stage 6 or Tag Analytics
    §3 can replace this with rarity-weighted specificity when needed.
    """
    if not tag_map:
        return "unclassified"
    tags = tag_map.get(str(condition_id or "").strip().lower())
    if not tags:
        return "unclassified"
    return sorted(tags)[0]


def _resolved_position_summaries(
    activity: List[Dict[str, Any]],
    tag_map: Dict[str, frozenset[str]] | None = None,
) -> List[Dict[str, Any]]:
    """Build realized condition summaries from BUY/SELL/REDEEM cashflows.

    Resolution is conservative:
    - any row with REDEEM is realized
    - a fully flattened buy/sell path with ~zero remaining shares is realized
    Open positions are excluded from the realized attribution set.

    We aggregate at condition level, not condition+outcome level. Live
    Polymarket REDEEM rows frequently omit outcome/asset on resolved payouts,
    so outcome-level joins undercount real realized history on live wallets.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in activity:
        condition_id = row.get("conditionId")
        if not condition_id:
            continue
        item = grouped.setdefault(
            str(condition_id),
            {
                "condition_id": str(condition_id),
                "title": row.get("title") or "?",
                "buy_usdc": 0.0,
                "sell_usdc": 0.0,
                "redeem_usdc": 0.0,
                "open_shares": 0.0,
                "has_sell": False,
                "has_redeem": False,
                "entry_price_numerator": 0.0,
                "entry_price_denominator": 0.0,
            },
        )

        row_type = row.get("type")
        if row_type == "TRADE" and row.get("side") == "BUY":
            usdc = _float(row.get("usdcSize"))
            shares = _approx_share_size(row)
            price = _float(row.get("price"))
            item["buy_usdc"] += usdc
            item["open_shares"] += shares
            weight = shares if shares > 0 else usdc
            if price > 0 and weight > 0:
                item["entry_price_numerator"] += price * weight
                item["entry_price_denominator"] += weight
        elif row_type == "TRADE" and row.get("side") == "SELL":
            item["sell_usdc"] += _float(row.get("usdcSize"))
            item["open_shares"] -= _approx_share_size(row)
            item["has_sell"] = True
        elif row_type == "REDEEM":
            redeem_usdc = _float(row.get("usdcSize") or row.get("size"))
            item["redeem_usdc"] += redeem_usdc
            item["open_shares"] -= _approx_share_size(row) or redeem_usdc
            item["has_redeem"] = True

    resolved: List[Dict[str, Any]] = []
    for item in grouped.values():
        if item["buy_usdc"] <= 0:
            continue
        closed_via_redeem = item["has_redeem"]
        closed_via_sell = item["has_sell"] and abs(item["open_shares"]) <= 1e-6
        if not (closed_via_redeem or closed_via_sell):
            continue

        avg_entry_price = 0.0
        if item["entry_price_denominator"] > 0:
            avg_entry_price = item["entry_price_numerator"] / item["entry_price_denominator"]

        if item["has_sell"] and item["has_redeem"]:
            hold_path = "sold_then_redeemed"
        elif item["has_sell"]:
            hold_path = "sold_before_resolution"
        else:
            hold_path = "held_to_resolution"

        title = str(item["title"] or "?")
        realized_pnl = item["sell_usdc"] + item["redeem_usdc"] - item["buy_usdc"]
        resolved.append(
            {
                "condition_id": item["condition_id"],
                "title": title,
                # `category` now comes from market_tags (per spec §4.4) — deterministic
                # first-alphabetical tag for stable Layer 4 grouping. Stage 6 /
                # Tag Analytics will refine with rarity-weighted specificity.
                "category": _primary_tag_for_cid(item["condition_id"], tag_map),
                "entry_price_bucket": _entry_price_bucket(avg_entry_price),
                "hold_path": hold_path,
                "avg_entry_price": round(avg_entry_price, 3),
                "realized_pnl": round(realized_pnl, 2),
            }
        )
    return resolved


def _live_current_value_by_category(
    positions: List[Dict[str, Any]],
    tag_map: Dict[str, frozenset[str]] | None = None,
) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in positions:
        condition_id = row.get("conditionId")
        if not condition_id:
            continue
        category = _primary_tag_for_cid(condition_id, tag_map)
        item = grouped.setdefault(category, {"positions": 0, "current_value": 0.0})
        item["positions"] += 1
        item["current_value"] += _float(row.get("currentValue"))

    return {
        label: {
            "positions": item["positions"],
            "current_value": round(item["current_value"], 2),
        }
        for label, item in sorted(grouped.items())
    }


def _build_outcome_attribution(
    features: Dict[str, Any],
    activity: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    resolved: List[Dict[str, Any]] | None = None,
    tag_map: Dict[str, frozenset[str]] | None = None,
) -> Dict[str, Any]:
    """Layer 4 — realized outcome attribution plus live-book context.

    `resolved` may be passed in by the caller to avoid recomputing the
    condition-level realization pass; falls back to computing it here so
    existing callers/tests keep working. `tag_map` is the per-wallet
    market_tags lookup for tag-based category grouping (Stage 5).
    """
    if resolved is None:
        resolved = _resolved_position_summaries(activity, tag_map=tag_map)
    live_value_by_category = _live_current_value_by_category(positions, tag_map=tag_map)
    live_value_total = round(
        sum(item["current_value"] for item in live_value_by_category.values()),
        2,
    )
    resolved_count = len(resolved)
    total_pnl = round(sum(_float(row["realized_pnl"]) for row in resolved), 2)
    total_gains = round(sum(max(_float(row["realized_pnl"]), 0.0) for row in resolved), 2)
    positive_count = sum(1 for row in resolved if _float(row["realized_pnl"]) > 0)
    pnl_values = [_float(row["realized_pnl"]) for row in resolved]
    pnl_cv_abs = 0.0
    if resolved_count >= 2:
        mean_pnl = sum(pnl_values) / resolved_count
        if abs(mean_pnl) > 0:
            pnl_cv_abs = round(pstdev(pnl_values) / abs(mean_pnl), 3)

    flags: List[str] = []
    reasons: List[str] = []
    if resolved_count < 3:
        flags.append("insufficient_resolved_sample")
        reasons.append(f"resolved_positions={resolved_count}")
    if live_value_total > 0:
        flags.append("open_book_present")
        reasons.append(f"live_current_value_total=${live_value_total:,.2f}")

    top_gain = max((max(_float(row["realized_pnl"]), 0.0) for row in resolved), default=0.0)
    top_two_gains = sorted((max(_float(row["realized_pnl"]), 0.0) for row in resolved), reverse=True)[:2]
    top_gain_share = round(top_gain / total_gains, 3) if total_gains > 0 else 0.0
    top_two_gain_share = round(sum(top_two_gains) / total_gains, 3) if total_gains > 0 else 0.0

    if resolved_count < 4 or total_gains <= 0:
        breadth_label = "insufficient"
        breadth_rationale = [
            f"resolved_positions={resolved_count}",
            f"total_realized_gains=${total_gains:,.2f}",
        ]
    elif top_gain_share >= 0.6 or top_two_gain_share >= 0.85:
        breadth_label = "episodic"
        breadth_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"top_two_gain_share={top_two_gain_share:.1%}",
        ]
    elif top_gain_share <= 0.35 and positive_count >= 4:
        breadth_label = "broad"
        breadth_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"positive_positions={positive_count}",
        ]
    else:
        breadth_label = "mixed"
        breadth_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"positive_positions={positive_count}",
        ]

    positive_share = round(positive_count / resolved_count, 3) if resolved_count > 0 else 0.0
    if resolved_count < 5:
        stability_label = "insufficient"
        stability_rationale = [
            f"resolved_positions={resolved_count}",
            "need a larger realized sample before calling returns stable or fragile",
        ]
    elif top_gain_share >= 0.65 or pnl_cv_abs >= 1.5 or positive_share < 0.45:
        stability_label = "fragile"
        stability_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"pnl_cv_abs={pnl_cv_abs:.2f}",
            f"positive_share={positive_share:.1%}",
        ]
    elif top_gain_share <= 0.4 and pnl_cv_abs <= 1.0 and positive_share >= 0.55:
        stability_label = "stable"
        stability_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"pnl_cv_abs={pnl_cv_abs:.2f}",
            f"positive_share={positive_share:.1%}",
        ]
    else:
        stability_label = "mixed"
        stability_rationale = [
            f"top_gain_share={top_gain_share:.1%}",
            f"pnl_cv_abs={pnl_cv_abs:.2f}",
            f"positive_share={positive_share:.1%}",
        ]

    status = "ok" if resolved_count >= 3 else "insufficient"
    return {
        "status": status,
        "reasons": reasons,
        "flags": flags,
        "resolved_position_count": resolved_count,
        "realized_pnl_total": total_pnl,
        "realized_pnl_by_category": _aggregate_realized_pnl(resolved, "category"),
        # Stage 5 removed subcategory field (came from deleted _classify_title_subcategory).
        # Tag-level breakdown is in realized_pnl_by_category (now tag-keyed).
        "realized_pnl_by_entry_price_bucket": _aggregate_realized_pnl(resolved, "entry_price_bucket"),
        "realized_pnl_by_hold_path": _aggregate_realized_pnl(resolved, "hold_path"),
        "gain_concentration": {
            "top_position_gain_share": top_gain_share,
            "top_two_position_gain_share": top_two_gain_share,
        },
        "outcome_structure": {
            "breadth": breadth_label,
            "breadth_rationale": breadth_rationale,
            "stability": stability_label,
            "stability_rationale": stability_rationale,
        },
        "live_current_value_total": live_value_total,
        "live_current_value_by_category": live_value_by_category,
    }


def _build_data_quality(features: Dict[str, Any], *, skip_gate0: bool, activity_limit: int) -> Dict[str, Any]:
    """Surface missingness and sampling constraints as first-class signals."""
    timing = features.get("timing", {})
    counts = features.get("counts", {})
    activity_records = int(counts.get("activity_records") or 0)
    return {
        "activity_limit": activity_limit,
        "activity_sample_truncated": activity_limit > 0 and activity_records >= activity_limit,
        "pagination_complete": bool(timing.get("pagination_complete", False)),
        "pages_fetched": int(timing.get("pages_fetched") or 0),
        "wallet_lifetime_source": str(timing.get("wallet_lifetime_source") or "unknown"),
        "profile_tenure_source": str(timing.get("profile_tenure_source") or ""),
        "profile_joined_present": int(timing.get("profile_joined_ts") or 0) > 0,
        "wallet_lifetime_days": round(_float(timing.get("wallet_lifetime_days")), 2),
        "sample_lifespan_days": round(_float(timing.get("lifespan_days")), 2),
        "gate0_skipped": skip_gate0,
        "layer2_evaluated": features.get("classification_layer2") is not None,
    }


def classify_wallet_archetype_from_snapshot(
    activity: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
    *,
    tag_map: Dict[str, frozenset] | None = None,
    tenure_metadata: Dict[str, Any] | None = None,
    wallet: str = "",
    skip_gate0: bool = True,
    with_execution: bool = False,
    force_execution: bool = False,
    activity_limit: int = 500,
) -> Dict[str, Any]:
    """Offline archetype classifier. Takes already-fetched activity and
    positions; returns the same `features` dict that extract_wallet_archetype
    returns.

    No NETWORK and no DB calls when ALL FOUR offline-mode conditions hold:

      * `tag_map` is a dict (`{}` is acceptable)
      * `tenure_metadata` is a dict
      * `skip_gate0=True` (default) — otherwise the body calls live
        `fetch_wallet_lifetime_stats(wallet)` to populate the gate0 field
      * `with_execution=False` (default) — otherwise the body builds a
        live `PolymarketClient()` and runs `compute_execution_profile()`,
        which hits Polymarket HTTP

    Replay paths (capture script + parity test + runner under
    `--from-fixture`) MUST satisfy all four. The legacy
    `extract_wallet_archetype` wrapper passes `skip_gate0=False` by
    default and `with_execution=False` by default; it relies on the None
    fallbacks for `tag_map`/`tenure_metadata` which DO hit producers.

    `tag_map`:
      * `tag_map={...}` (or `tag_map={}` for an explicit no-DB run) →
        no SQLite contact. Replay paths and the parity test pass this.
      * `tag_map=None` → falls back to `_fetch_tag_map(condition_ids)`,
        which opens a SQLite connection against `db.get_db_path()`. NOT pure.

    `tenure_metadata`:
      * `tenure_metadata={...}` → use directly. Required keys mirror
        `_fetch_wallet_tenure_metadata` output: `wallet_first_activity_ts`,
        `wallet_last_activity_ts`, `wallet_lifetime_days`,
        `pagination_complete`, `pages_fetched`, plus optional
        `profile_joined_ts`, `profile_lifetime_days`,
        `profile_tenure_source`, `wallet_lifetime_source`. Replay paths
        and the parity test pass this.
      * `tenure_metadata=None` → falls back to
        `_fetch_wallet_tenure_metadata(wallet)`, which makes live HTTP
        calls (`get_activity_page` + `build_session` +
        `fetch_wallet_profile_enrichment`). NOT pure.

    `wallet` is informational (stamped into the features dict). Default
    empty string is acceptable for callers that don't need the address in
    the output (e.g. the parity test). Note: `tenure_metadata=None` with
    an empty `wallet` will hit network for an empty address, which the
    public producers will likely reject — pass tenure_metadata explicitly
    when wallet is empty.

    Spec: docs/superpowers/specs/2026-05-01-fixture-v2-schema.md.
    """
    now = datetime.now(timezone.utc)
    trades = _trade_events(activity)
    position_rows = _position_events(positions)
    # Pre-fetch tag map for every condition this wallet touches in ONE query.
    # Trades + position rows cover the union — resolved conditions are a
    # subset of trades (resolved comes from activity TRADE rows).
    tag_condition_ids = (
        [str(r.get("conditionId") or "") for r in trades]
        + [str(r.get("conditionId") or "") for r in position_rows]
    )
    if tag_map is None:
        tag_map = _fetch_tag_map(tag_condition_ids)
    # Resolved summaries need tag_map to stamp each row's `category` via
    # _primary_tag_for_cid; compute after the map is built.
    resolved = _resolved_position_summaries(activity, tag_map=tag_map)

    if tenure_metadata is None:
        tenure_metadata = _fetch_wallet_tenure_metadata(wallet)

    features = {
        "wallet": wallet.lower(),
        "as_of_ts": int(now.timestamp()),
        "extracted_at": now.isoformat(),
        "gate0": {} if skip_gate0 else fetch_wallet_lifetime_stats(wallet),
        "counts": {
            "activity_records": len(activity),
            "trade_records": len(trades),
            "live_positions": len(position_rows),
        },
        "trade_pairing": _pair_metrics_by_condition(trades),
        "position_pairing": _pair_metrics_by_condition(position_rows),
        "trade_event_families": _event_family_metrics(trades),
        "position_event_families": _event_family_metrics(position_rows),
        "domain_tags": _tag_distribution([*trades, *position_rows], tag_map),
        "domain_tags_horizons": _domain_tags_horizon_metrics(
            trades, position_rows, resolved, tag_map
        ),
        "trade_shape": _trade_shape_metrics(trades),
        "redeems": _redeem_metrics(activity),
        # `timing` merges intra-sample cadence (CV, histogram, sample lifespan)
        # with wallet-level tenure (wallet_lifetime_days). That tenure now
        # comes from the best available source: full activity span when
        # reachable, or profile join-date when public activity is API-capped.
        # Layer 2 uses the wallet-level number; intra-sample remains a cadence
        # signal.
        "timing": {
            **_timing_metrics(trades),
            **tenure_metadata,
        },
        "returns": _per_position_pnl_metrics(activity),
    }
    features["classification"] = classify_archetype(features)
    # Layer 2 only runs if Layer 1 did not terminate with an excluded archetype.
    layer2 = classify_archetype_layer2(features)
    if layer2 is not None:
        features["classification_layer2"] = layer2
    features["observations"] = _build_observations(features)
    features["behavior_pattern"] = _build_behavior_pattern(features)
    # Execution profile runs BEFORE edge hypotheses so Layer 3 mapping can
    # consume drift evidence to promote entry_timing_edge out of `unknown`.
    if with_execution:
        features["execution_profile"] = compute_execution_profile(
            wallet,
            activity,
            client=PolymarketClient(),
            classification=features.get("classification"),
            counts=features.get("counts"),
            returns=features.get("returns"),
            force=force_execution,
        )
    else:
        features["execution_profile"] = {
            "status": "not_run",
            "reason": "shortlist_gate_not_met",
        }
    features["edge_hypotheses"] = _build_edge_hypotheses(features)
    features["outcome_attribution"] = _build_outcome_attribution(
        features, activity, position_rows, resolved=resolved, tag_map=tag_map,
    )
    features["operational_implications"] = {}
    features["data_quality"] = _build_data_quality(
        features,
        skip_gate0=skip_gate0,
        activity_limit=activity_limit,
    )
    return features


def extract_wallet_archetype(
    wallet: str,
    skip_gate0: bool = False,
    activity_limit: int = 500,
    *,
    with_execution: bool = False,
    force_execution: bool = False,
) -> Dict[str, Any]:
    activity = _fetch_activity_dicts(wallet, limit=activity_limit)
    positions = _fetch_positions_dicts(wallet)
    # tag_map=None and tenure_metadata=None let the snapshot classifier
    # fall back to live producers (the legacy behavior). Replay/capture
    # callers MUST pass both explicitly to avoid the DB side channel and
    # the live HTTP path through _fetch_wallet_tenure_metadata.
    return classify_wallet_archetype_from_snapshot(
        activity, positions,
        tag_map=None,
        tenure_metadata=None,
        wallet=wallet,
        skip_gate0=skip_gate0,
        with_execution=with_execution,
        force_execution=force_execution,
        activity_limit=activity_limit,
    )


def format_markdown(report: Dict[str, Any]) -> str:
    cls = report["classification"]
    layer2 = report.get("classification_layer2")
    edge_hypotheses = report.get("edge_hypotheses") or {}
    domain_tags = report.get("domain_tags") or {}
    trade_shape = report["trade_shape"]
    trade_pairing = report["trade_pairing"]
    pos_pairing = report["position_pairing"]
    pos_event_families = report.get("position_event_families") or {}
    gate0 = report["gate0"]

    lines = [
        f"# Wallet Archetype — `{report['wallet']}`",
        "",
        f"- Layer 1 (mechanism): `{cls['archetype']}` ({cls['confidence']}) → `{cls['copy_policy']}`",
    ]
    if layer2 is not None:
        lines.append(
            f"- Layer 2 (intent): `{layer2['archetype']}` ({layer2['confidence']}) → `{layer2['copy_policy']}`"
        )
    else:
        lines.append("- Layer 2 (intent): _not evaluated — Layer 1 terminal_")
    lines.extend(
        [
            f"- Primary tag: `{domain_tags.get('primary_tag') or 'unknown'}` "
            f"({domain_tags.get('primary_tag_share', 0.0):.0%} share, "
            f"{domain_tags.get('distinct_tag_count', 0)} distinct tags, "
            f"{domain_tags.get('unclassified_share', 0.0):.0%} unclassified)",
            f"- Trade paired-condition ratio: {trade_pairing['paired_condition_ratio']:.0%}",
            f"- Live paired-condition ratio: {pos_pairing['paired_condition_ratio']:.0%}",
            f"- Live multi-condition family ratio: {pos_event_families.get('multi_condition_family_ratio', 0.0):.0%}",
            f"- Sell ratio: {trade_shape['sell_ratio']:.0%}",
            f"- Midpoint buy ratio: {trade_shape['midpoint_buy_ratio']:.0%}",
        ]
    )
    horizons = report.get("domain_tags_horizons") or {}
    if horizons:
        recent = horizons.get("recent_trade") or {}
        live = horizons.get("live_book") or {}
        realized = horizons.get("realized_pnl") or {}
        alignment = horizons.get("alignment") or {}
        lines.extend(
            [
                "",
                "## Tag horizons",
                "",
                f"- Recent tape: `{recent.get('primary_tag') or 'unknown'}` "
                f"({recent.get('primary_tag_share', 0.0):.0%} share, n={recent.get('sample_size', 0)})",
                f"- Live book: `{live.get('primary_tag') or 'unknown'}` "
                f"({live.get('primary_tag_share', 0.0):.0%} share, n={live.get('sample_size', 0)})",
                f"- Realized PnL: `{realized.get('primary_tag') or 'unknown'}` "
                f"({realized.get('primary_tag_share', 0.0):.0%} |PnL| share, n={realized.get('sample_size', 0)})",
                f"- Alignment: recent↔live={alignment.get('recent_live_primary_match')}, "
                f"recent↔realized={alignment.get('recent_realized_primary_match')}, "
                f"live↔realized={alignment.get('live_realized_primary_match')}, "
                f"rotation_signal={horizons.get('rotation_signal')}",
            ]
        )
    if gate0:
        lines.append(f"- Gate0 source: `{gate0.get('source', 'unknown')}`")
        if gate0.get("lifetime_pnl") is not None:
            lines.append(f"- Lifetime PnL: `${gate0['lifetime_pnl']:+,.0f}`")
    lines.extend(["", "## Layer 1 rationale (mechanism)", ""])
    for reason in cls["rationale"]:
        lines.append(f"- {reason}")
    if layer2 is not None:
        lines.extend(["", "## Layer 2 rationale (intent)", ""])
        for reason in layer2["rationale"]:
            lines.append(f"- {reason}")
        if layer2.get("notes"):
            lines.extend(["", f"**Note:** {layer2['notes']}"])
    if edge_hypotheses:
        lines.extend(["", "## Layer 3 edge hypotheses", ""])
        for edge_name, payload in edge_hypotheses.items():
            lines.append(f"- `{edge_name}`: `{payload.get('level', 'unknown')}`")
            for reason in payload.get("rationale") or []:
                lines.append(f"  - {reason}")
            if payload.get("notes"):
                lines.append(f"  - note: {payload['notes']}")
    outcome_attribution = report.get("outcome_attribution") or {}
    if outcome_attribution:
        lines.extend(["", "## Layer 4 outcome attribution", ""])
        lines.append(
            f"- Status: `{outcome_attribution.get('status', 'unknown')}`"
            f" | realized positions: {outcome_attribution.get('resolved_position_count', 0)}"
            f" | realized PnL: ${outcome_attribution.get('realized_pnl_total', 0.0):+,.2f}"
        )
        structure = outcome_attribution.get("outcome_structure") or {}
        lines.append(
            f"- Breadth: `{structure.get('breadth', 'unknown')}`"
            f" | Stability: `{structure.get('stability', 'unknown')}`"
        )
        gain_concentration = outcome_attribution.get("gain_concentration") or {}
        if gain_concentration:
            lines.append(
                f"- Top gain share: {gain_concentration.get('top_position_gain_share', 0.0):.0%}"
                f" | Top-two gain share: {gain_concentration.get('top_two_position_gain_share', 0.0):.0%}"
            )
    if pos_pairing["paired_examples"]:
        lines.extend(["", "## Example paired markets", ""])
        for title in pos_pairing["paired_examples"]:
            lines.append(f"- {title}")
    if pos_event_families.get("family_examples"):
        lines.extend(["", "## Example multi-condition event families", ""])
        for title in pos_event_families["family_examples"]:
            lines.append(f"- {title}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract live wallet archetype features")
    parser.add_argument("wallet", help="Wallet address")
    parser.add_argument("--activity-limit", type=int, default=500)
    parser.add_argument("--skip-gate0", action="store_true")
    parser.add_argument("--markdown", action="store_true", help="Emit markdown instead of JSON")
    parser.add_argument(
        "--with-execution",
        action="store_true",
        help="Compute per-fill execution profile (API-expensive; shortlist only)",
    )
    parser.add_argument(
        "--force-execution",
        action="store_true",
        help="Run execution profile even on Layer 1 terminal wallets (calibration/QA)",
    )
    args = parser.parse_args()

    report = extract_wallet_archetype(
        wallet=args.wallet,
        skip_gate0=args.skip_gate0,
        activity_limit=args.activity_limit,
        with_execution=args.with_execution,
        force_execution=args.force_execution,
    )
    if args.markdown:
        print(format_markdown(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
