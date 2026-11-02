"""Liquidity Surface v1.

Display-only proxy surface for wallet-level liquidity and execution-friction
context. The public data available to fixture replay does not include
historical order-book depth, so this module never fabricates depth. It uses
only captured wallet fills, exact captured price-history lookups, and optional
per-row volume fields when present.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


SURFACE_VERSION = "liquidity_surface_v1"
FIDELITY_TIER = "proxy_only_no_historical_depth"
NEAR_RESOLUTION_PRICE = 0.02
MAX_MODELED_COST_BPS = 10_000.0


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _condition_id(row: Mapping[str, Any]) -> str:
    return _text(row.get("conditionId") or row.get("condition_id"))


def _asset_id(row: Mapping[str, Any]) -> str:
    return _text(row.get("asset") or row.get("asset_id") or row.get("token_id"))


def _outcome(row: Mapping[str, Any]) -> str:
    return _text(row.get("outcome") or row.get("outcome_label") or row.get("side_label"))


def _side(row: Mapping[str, Any]) -> str:
    return _text(row.get("side")).upper()


def _timestamp(row: Mapping[str, Any]) -> int | None:
    val = row.get("timestamp") or row.get("ts") or row.get("fill_ts")
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _notional(row: Mapping[str, Any]) -> float:
    for key in ("usdcSize", "usdc_size", "notional", "notional_usdc"):
        val = _num(row.get(key))
        if val is not None:
            return max(val, 0.0)
    price = _num(row.get("price"))
    size = _num(row.get("size"))
    if price is None or size is None:
        return 0.0
    return max(price * size, 0.0)


def _volume_24h(row: Mapping[str, Any]) -> float | None:
    for key in (
        "market_volume_24h",
        "marketVolume24h",
        "volume_24h",
        "volume24h",
        "volume24hr",
        "volume24hrClob",
    ):
        val = _num(row.get(key))
        if val is not None and val > 0:
            return val
    return None


def _is_trade(row: Mapping[str, Any]) -> bool:
    row_type = row.get("type")
    return row_type is None or str(row_type).upper() == "TRADE"


def _is_negative_risk(row: Mapping[str, Any]) -> bool:
    return any(
        _boolish(row.get(key))
        for key in ("negRisk", "negativeRisk", "neg_risk", "enableNegRisk")
    )


def _price_lookup(
    price_history_lookups: Mapping[str, float | None] | None,
    token_id: str,
    ts: int | None,
) -> float | None:
    if not isinstance(price_history_lookups, Mapping) or not token_id or ts is None:
        return None
    return _num(price_history_lookups.get(f"{token_id}|{int(ts)}"))


def _near_resolution(*prices: float | None) -> bool:
    for price in prices:
        if price is None:
            continue
        if price <= NEAR_RESOLUTION_PRICE or price >= 1.0 - NEAR_RESOLUTION_PRICE:
            return True
    return False


def _out_of_range(*prices: float | None) -> bool:
    return any(price is not None and (price < 0.0 or price > 1.0) for price in prices)


def _slippage_cost_bps(side: str, fill_price: float, mid_price: float) -> float | None:
    if fill_price <= 0.0 or mid_price <= 0.0:
        return None
    if side == "SELL":
        cost = max(mid_price - fill_price, 0.0) / mid_price
    else:
        cost = max(fill_price - mid_price, 0.0) / mid_price
    return min(cost * 10_000.0, MAX_MODELED_COST_BPS)


def _weighted(values: list[tuple[float, float]]) -> float | None:
    denom = sum(weight for _, weight in values if weight > 0)
    if denom <= 0:
        return None
    return sum(value * weight for value, weight in values if weight > 0) / denom


def _token_observation(
    *,
    token_id: str,
    outcome: str,
    rows: Sequence[Mapping[str, Any]],
    position_rows: Sequence[Mapping[str, Any]],
    price_history_lookups: Mapping[str, float | None] | None,
) -> dict[str, Any]:
    rows_sorted = sorted(
        rows,
        key=lambda r: (_timestamp(r) or 0, _text(r.get("transactionHash") or r.get("tx_hash"))),
    )
    total_notional = sum(_notional(row) for row in rows_sorted)
    max_trade_notional = max((_notional(row) for row in rows_sorted), default=0.0)
    volume_values = [_volume_24h(row) for row in rows_sorted]
    volume_values = [v for v in volume_values if v is not None]
    volume_24h = max(volume_values) if volume_values else None
    size_ratio = (
        max_trade_notional / volume_24h
        if volume_24h is not None and volume_24h > 0
        else None
    )

    negative_risk = any(_is_negative_risk(row) for row in rows_sorted) or any(
        _is_negative_risk(row) for row in position_rows
    )
    modeled: list[tuple[float, float]] = []
    missing_history = 0
    capped = False
    caveats: set[str] = set()
    price_values: list[float] = []
    near_resolution = False

    for row in rows_sorted:
        price = _num(row.get("price"))
        ts = _timestamp(row)
        mid = _price_lookup(price_history_lookups, token_id, ts)
        if price is not None:
            price_values.append(price)
        if mid is not None:
            price_values.append(mid)
        if _out_of_range(price, mid) or negative_risk:
            caveats.add("negative_risk_guard")
            continue
        if price is None or price <= 0.0 or mid is not None and mid <= 0.0:
            caveats.add("zero_price_guard")
            continue
        if _near_resolution(price, mid):
            near_resolution = True
            caveats.add("near_resolution")
            continue
        if mid is None:
            missing_history += 1
            continue
        cost = _slippage_cost_bps(_side(row), price, mid)
        if cost is None:
            caveats.add("zero_price_guard")
            continue
        if cost >= MAX_MODELED_COST_BPS:
            capped = True
            caveats.add("estimate_capped")
        modeled.append((cost, max(_notional(row), 0.0)))

    for row in position_rows:
        for key in ("avgPrice", "curPrice", "currentPrice"):
            val = _num(row.get(key))
            if val is not None:
                price_values.append(val)
                if _out_of_range(val):
                    negative_risk = True
                    caveats.add("negative_risk_guard")
                if _near_resolution(val):
                    near_resolution = True
                    caveats.add("near_resolution")

    if size_ratio is not None and size_ratio > 1.0:
        caveats.add("trade_size_exceeds_volume_proxy")
    if negative_risk or "negative_risk_guard" in caveats or "near_resolution" in caveats:
        modeled = []
    weighted_cost = _weighted(modeled)
    max_cost = max((value for value, _ in modeled), default=None)
    if negative_risk or "negative_risk_guard" in caveats or "near_resolution" in caveats or "zero_price_guard" in caveats:
        slippage_status = "underpowered"
    elif modeled:
        slippage_status = "proxy_only"
    else:
        slippage_status = "data_unavailable"

    if size_ratio is None:
        bucket = "unknown"
    elif size_ratio > 0.10:
        bucket = "thin_proxy"
    elif size_ratio > 0.01:
        bucket = "moderate_proxy"
    else:
        bucket = "deep_proxy"

    # price_history_state — honest 3-way (plus a neutral fallback). The
    # earlier form collapsed "guard-excluded" into "missing": a token whose
    # price history was CAPTURED but deliberately discarded by the
    # near-resolution / negative-risk / zero-price guard would still report
    # "missing", implying a lookup failure that did not happen.
    _guard_caveats = {"negative_risk_guard", "near_resolution", "zero_price_guard"}
    if modeled:
        price_history_state = "available"
    elif missing_history > 0:
        price_history_state = "missing"
    elif caveats & _guard_caveats:
        price_history_state = "excluded_by_guard"
    else:
        price_history_state = "unavailable"

    return {
        "token_id": token_id,
        "outcome": outcome or "unknown",
        "n_trades": len(rows_sorted),
        "notional_usdc": round(total_notional, 6),
        "max_trade_notional_usdc": round(max_trade_notional, 6),
        "price_history_state": price_history_state,
        "volume_state": "available" if volume_24h is not None else "missing",
        "market_volume_24h_usdc": None if volume_24h is None else round(volume_24h, 6),
        "size_pressure": {
            "max_trade_to_volume_24h": None if size_ratio is None else round(size_ratio, 6),
            "capped": bool(size_ratio is not None and size_ratio > 1.0),
        },
        "near_resolution": bool(near_resolution),
        "negative_risk_guard": bool(negative_risk or "negative_risk_guard" in caveats),
        "modeled_slippage": {
            "status": slippage_status,
            "method": "trade_vs_captured_mid_proxy" if modeled else None,
            "weighted_cost_bps": None if weighted_cost is None else round(weighted_cost, 6),
            "max_cost_bps": None if max_cost is None else round(max_cost, 6),
            "capped": capped,
            "caveats": sorted(caveats),
        },
        "liquidity_bucket": bucket,
        "caveats": sorted(caveats),
    }


def _token_asymmetry(tokens: list[dict[str, Any]]) -> dict[str, Any]:
    if len(tokens) < 2:
        return {
            "state": "underpowered",
            "reason": "single_token_observed",
            "tokens": [
                {
                    "token_id": token.get("token_id"),
                    "outcome": token.get("outcome"),
                    "notional_usdc": token.get("notional_usdc", 0.0),
                    "liquidity_bucket": token.get("liquidity_bucket"),
                }
                for token in tokens
            ],
        }
    notionals = [float(token.get("notional_usdc") or 0.0) for token in tokens]
    positive = [n for n in notionals if n > 0]
    ratio = max(positive) / min(positive) if len(positive) >= 2 else None
    pressures = [
        float((token.get("size_pressure") or {}).get("max_trade_to_volume_24h") or 0.0)
        for token in tokens
    ]
    positive_pressures = [p for p in pressures if p > 0]
    pressure_ratio = (
        max(positive_pressures) / min(positive_pressures)
        if len(positive_pressures) >= 2
        else None
    )
    return {
        "state": "observed" if ratio is not None or pressure_ratio is not None else "data_unavailable",
        "max_notional_ratio": None if ratio is None else round(ratio, 6),
        "max_pressure_ratio": None if pressure_ratio is None else round(pressure_ratio, 6),
        "tokens": [
            {
                "token_id": token.get("token_id"),
                "outcome": token.get("outcome"),
                "notional_usdc": token.get("notional_usdc", 0.0),
                "liquidity_bucket": token.get("liquidity_bucket"),
            }
            for token in tokens
        ],
    }


def _empty_surface(status: str, reason: str) -> dict[str, Any]:
    return {
        "surface_version": SURFACE_VERSION,
        "status": status,
        "fidelity_tier": FIDELITY_TIER,
        "historical_depth_available": False,
        "summary": {
            "n_markets": 0,
            "n_trade_rows": 0,
            "n_modeled_trade_rows": 0,
            "total_observed_notional_usdc": 0.0,
            "weighted_execution_cost_bps": None,
            "liquidity_read": status,
        },
        "degradation": {
            "states": [status],
            "reasons": [reason, "historical_depth_unavailable"],
        },
        "market_observations": [],
    }


def compute_liquidity_surface(
    closed_positions: Sequence[Mapping[str, Any]],
    activity_rows: Sequence[Mapping[str, Any]],
    price_history_lookups: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """Build the display-only liquidity surface from captured data.

    No live I/O. No order-book reconstruction. Historical depth is explicitly
    unavailable, so all numeric friction estimates are proxy-only and sourced
    from wallet fills versus exact captured mid-price lookups.
    """
    positions = [row for row in closed_positions if isinstance(row, Mapping)]
    if not positions:
        return _empty_surface("data_unavailable", "no_closed_positions")

    trades = [
        row for row in activity_rows
        if isinstance(row, Mapping) and _is_trade(row) and _condition_id(row) and _asset_id(row)
    ]
    if not trades:
        return _empty_surface("data_unavailable", "no_trade_rows")

    positions_by_cid: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    pnl_by_cid: dict[str, float] = defaultdict(float)
    for row in positions:
        cid = _condition_id(row)
        if not cid:
            continue
        positions_by_cid[cid].append(row)
        pnl_by_cid[cid] += _num(row.get("realizedPnl") or row.get("realized_pnl")) or 0.0

    trades_by_cid_asset: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    outcome_by_cid_asset: dict[tuple[str, str], str] = {}
    # assets_by_cid is a one-pass index: without it the per-cid asset scan
    # below re-iterates every (cid, asset) key for every cid — O(markets^2).
    assets_by_cid: dict[str, list[str]] = defaultdict(list)
    for row in trades:
        cid_key = _condition_id(row)
        key = (cid_key, _asset_id(row))
        if key not in trades_by_cid_asset:
            assets_by_cid[cid_key].append(key[1])
        trades_by_cid_asset[key].append(row)
        outcome_by_cid_asset.setdefault(key, _outcome(row))

    market_observations: list[dict[str, Any]] = []
    modeled_values: list[tuple[float, float]] = []
    total_notional = 0.0
    degradation_states: set[str] = {"proxy_only"}
    degradation_reasons: set[str] = {"historical_depth_unavailable"}

    for cid in sorted({cid for cid, _ in trades_by_cid_asset} | set(positions_by_cid)):
        token_obs: list[dict[str, Any]] = []
        title = ""
        for asset in sorted(assets_by_cid.get(cid, [])):
            pos_rows = [
                pos for pos in positions_by_cid.get(cid, [])
                if _asset_id(pos) == asset or _text(pos.get("oppositeAsset")) == asset
            ]
            rows = trades_by_cid_asset[(cid, asset)]
            title = title or _text(rows[0].get("title")) or _text((pos_rows or [{}])[0].get("title"))
            obs = _token_observation(
                token_id=asset,
                outcome=outcome_by_cid_asset.get((cid, asset), ""),
                rows=rows,
                position_rows=pos_rows,
                price_history_lookups=price_history_lookups,
            )
            token_obs.append(obs)
            total_notional += float(obs["notional_usdc"] or 0.0)
            cost = obs["modeled_slippage"]["weighted_cost_bps"]
            if cost is not None:
                modeled_values.append((float(cost), float(obs["notional_usdc"] or 0.0)))
            if obs["modeled_slippage"]["status"] == "underpowered":
                degradation_states.add("underpowered")
            if obs["modeled_slippage"]["status"] == "data_unavailable" or obs["volume_state"] == "missing":
                degradation_states.add("data_unavailable")

        market_observations.append(
            {
                "condition_id": cid,
                "title": title,
                "realized_pnl": round(pnl_by_cid.get(cid, 0.0), 6),
                "tokens": token_obs,
                "token_asymmetry": _token_asymmetry(token_obs),
            }
        )

    if len(market_observations) < 2:
        status = "underpowered"
        degradation_states.add("underpowered")
        degradation_reasons.add("single_market_wallet")
    elif not modeled_values:
        status = "underpowered"
        degradation_states.add("underpowered")
        degradation_reasons.add("no_modeled_trade_rows")
    else:
        status = "proxy_only"
    if "data_unavailable" in degradation_states:
        degradation_reasons.add("missing_price_history_or_volume")
    weighted_cost = _weighted(modeled_values)
    read = (
        "proxy_only: no historical order-book depth; friction uses captured "
        "fill-vs-mid and optional volume-pressure proxies only"
    )
    return {
        "surface_version": SURFACE_VERSION,
        "status": status,
        "fidelity_tier": FIDELITY_TIER,
        "historical_depth_available": False,
        "summary": {
            "n_markets": len(market_observations),
            "n_trade_rows": len(trades),
            "n_modeled_trade_rows": len(modeled_values),
            "total_observed_notional_usdc": round(total_notional, 6),
            "weighted_execution_cost_bps": None if weighted_cost is None else round(weighted_cost, 6),
            "liquidity_read": read,
        },
        "degradation": {
            "states": sorted(degradation_states),
            "reasons": sorted(degradation_reasons),
        },
        "market_observations": market_observations,
    }


__all__ = ["compute_liquidity_surface", "SURFACE_VERSION", "FIDELITY_TIER"]
