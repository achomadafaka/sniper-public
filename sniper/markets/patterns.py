import threading
from typing import Any, Dict, List

from sniper.markets.polymarket_api import fetch_user_trades
from sniper.utils import (
    _format_unix,
    _normalize_tx_hash,
    _normalize_wallet,
    _safe_float,
    _safe_int,
)

_suspicious_status_cache: Dict[str, Dict[str, Any]] = {}
_suspicious_status_cache_lock = threading.Lock()
_suspicious_pattern_cache: Dict[str, Dict[str, Any]] = {}
_suspicious_pattern_cache_lock = threading.Lock()


def trade_row_id(row: Dict[str, Any]) -> str:
    tx = _normalize_tx_hash(row.get("transaction_hash"))
    if tx:
        return tx
    return (
        f"{_normalize_wallet(row.get('wallet'))}:"
        f"{str(row.get('condition_id') or '').strip().lower()}:"
        f"{str(row.get('outcome') or '').strip().lower()}:"
        f"{_safe_int(row.get('timestamp'))}:"
        f"{_safe_float(row.get('price')):.6f}:"
        f"{_safe_float(row.get('size')):.6f}"
    )


def default_order_pattern() -> Dict[str, Any]:
    return {
        "order_pattern": "Single/Few (Human-like)",
        "order_pattern_code": "single_few_human_like",
        "cluster_trade_count": 1,
        "cluster_notional": 0.0,
        "cluster_span_seconds": 0,
        "cluster_unique_sizes": 1,
        "cluster_unique_prices": 1,
        "cluster_started_ts": 0,
        "cluster_started_at": "",
        "cluster_ended_ts": 0,
        "cluster_ended_at": "",
    }


def classify_order_pattern(
    *,
    trade_count: int,
    span_seconds: int,
    unique_sizes: int,
    exec_cluster_max_span_seconds: int,
    exec_rapid_min_trades: int,
) -> tuple[str, str]:
    if trade_count <= 3:
        return ("single_few_human_like", "Single/Few (Human-like)")
    rapid_span_ceiling = min(exec_cluster_max_span_seconds, 600)
    if trade_count >= exec_rapid_min_trades and span_seconds <= rapid_span_ceiling and unique_sizes >= 3:
        return ("rapid_burst_bot_like", "Rapid Burst (Bot-like)")
    return ("scaled_multi_order", "Scaled Multi-Order")


def store_cluster_pattern(
    cluster: List[Dict[str, Any]],
    out: Dict[str, Dict[str, Any]],
    *,
    exec_cluster_max_span_seconds: int,
    exec_rapid_min_trades: int,
) -> None:
    if not cluster:
        return

    started_ts = _safe_int(cluster[0].get("timestamp"))
    ended_ts = _safe_int(cluster[-1].get("timestamp"))
    span_seconds = max(0, ended_ts - started_ts)
    trade_count = len(cluster)
    cluster_notional = sum(_safe_float(row.get("notional")) for row in cluster)
    unique_sizes = len({round(_safe_float(row.get("size")), 2) for row in cluster})
    unique_prices = len({round(_safe_float(row.get("price")), 4) for row in cluster})
    code, label = classify_order_pattern(
        trade_count=trade_count,
        span_seconds=span_seconds,
        unique_sizes=unique_sizes,
        exec_cluster_max_span_seconds=exec_cluster_max_span_seconds,
        exec_rapid_min_trades=exec_rapid_min_trades,
    )

    payload = {
        "order_pattern": label,
        "order_pattern_code": code,
        "cluster_trade_count": trade_count,
        "cluster_notional": cluster_notional,
        "cluster_span_seconds": span_seconds,
        "cluster_unique_sizes": unique_sizes,
        "cluster_unique_prices": unique_prices,
        "cluster_started_ts": started_ts,
        "cluster_started_at": _format_unix(started_ts),
        "cluster_ended_ts": ended_ts,
        "cluster_ended_at": _format_unix(ended_ts),
    }
    for row in cluster:
        out[trade_row_id(row)] = payload


def annotate_order_patterns(
    trade_rows: List[Dict[str, Any]],
    *,
    exec_cluster_max_gap_seconds: int,
    exec_cluster_max_span_seconds: int,
    exec_rapid_min_trades: int,
) -> List[Dict[str, Any]]:
    grouped: Dict[tuple[str, str, str], List[Dict[str, Any]]] = {}
    for row in trade_rows:
        key = (
            _normalize_wallet(row.get("wallet")),
            str(row.get("condition_id") or "").strip().lower(),
            str(row.get("outcome") or "").strip().lower(),
        )
        if not key[0] or not key[1]:
            continue
        grouped.setdefault(key, []).append(row)

    by_row_id: Dict[str, Dict[str, Any]] = {}
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda r: _safe_int(r.get("timestamp")))
        if not ordered:
            continue

        current_cluster: List[Dict[str, Any]] = [ordered[0]]
        for row in ordered[1:]:
            prev_ts = _safe_int(current_cluster[-1].get("timestamp"))
            start_ts = _safe_int(current_cluster[0].get("timestamp"))
            ts = _safe_int(row.get("timestamp"))
            gap = ts - prev_ts
            span_if_added = ts - start_ts
            if gap <= exec_cluster_max_gap_seconds and span_if_added <= exec_cluster_max_span_seconds:
                current_cluster.append(row)
                continue

            store_cluster_pattern(
                current_cluster,
                by_row_id,
                exec_cluster_max_span_seconds=exec_cluster_max_span_seconds,
                exec_rapid_min_trades=exec_rapid_min_trades,
            )
            current_cluster = [row]

        store_cluster_pattern(
            current_cluster,
            by_row_id,
            exec_cluster_max_span_seconds=exec_cluster_max_span_seconds,
            exec_rapid_min_trades=exec_rapid_min_trades,
        )

    for row in trade_rows:
        rid = trade_row_id(row)
        pattern = by_row_id.get(rid, default_order_pattern())
        row.update(pattern)
    return trade_rows


def extract_order_pattern_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "order_pattern": str(row.get("order_pattern") or default_order_pattern()["order_pattern"]),
        "order_pattern_code": str(row.get("order_pattern_code") or default_order_pattern()["order_pattern_code"]),
        "cluster_trade_count": int(row.get("cluster_trade_count") or 0),
        "cluster_notional": float(row.get("cluster_notional") or 0.0),
        "cluster_span_seconds": int(row.get("cluster_span_seconds") or 0),
        "cluster_unique_sizes": int(row.get("cluster_unique_sizes") or 0),
        "cluster_unique_prices": int(row.get("cluster_unique_prices") or 0),
        "cluster_started_ts": int(row.get("cluster_started_ts") or 0),
        "cluster_started_at": str(row.get("cluster_started_at") or ""),
        "cluster_ended_ts": int(row.get("cluster_ended_ts") or 0),
        "cluster_ended_at": str(row.get("cluster_ended_at") or ""),
    }


def normalize_api_trade_row(trade: Dict[str, Any]) -> Dict[str, Any]:
    price = _safe_float(trade.get("price"))
    size = _safe_float(trade.get("size"))
    return {
        "transaction_hash": _normalize_tx_hash(trade.get("transactionHash")),
        "wallet": _normalize_wallet(trade.get("proxyWallet")),
        "condition_id": str(trade.get("conditionId") or "").strip(),
        "outcome": str(trade.get("outcome") or "").strip(),
        "timestamp": _safe_int(trade.get("timestamp")),
        "price": price,
        "size": size,
        "notional": price * size,
    }


def fetch_wallet_order_pattern_map(
    wallet: str,
    *,
    http,
    limit: int,
    exec_cluster_max_gap_seconds: int,
    exec_cluster_max_span_seconds: int,
    exec_rapid_min_trades: int,
) -> Dict[str, Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for trade in fetch_user_trades(http, wallet, limit=limit):
        if not isinstance(trade, dict):
            continue
        normalized = normalize_api_trade_row(trade)
        if not normalized["condition_id"] or not normalized["wallet"]:
            continue
        rows.append(normalized)
    annotate_order_patterns(
        rows,
        exec_cluster_max_gap_seconds=exec_cluster_max_gap_seconds,
        exec_cluster_max_span_seconds=exec_cluster_max_span_seconds,
        exec_rapid_min_trades=exec_rapid_min_trades,
    )

    out: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        out[trade_row_id(row)] = extract_order_pattern_payload(row)
    return out


def get_suspicious_pattern_cache(wallet: str) -> Dict[str, Any] | None:
    with _suspicious_pattern_cache_lock:
        cached = _suspicious_pattern_cache.get(wallet)
        if cached is None:
            return None
        return {
            "patterns_by_trade_id": cached.get("patterns_by_trade_id", {}),
            "synced_ts": int(cached.get("synced_ts") or 0),
        }


def set_suspicious_pattern_cache(
    wallet: str,
    *,
    patterns_by_trade_id: Dict[str, Dict[str, Any]],
    synced_ts: int,
) -> None:
    with _suspicious_pattern_cache_lock:
        _suspicious_pattern_cache[wallet] = {
            "patterns_by_trade_id": patterns_by_trade_id,
            "synced_ts": int(synced_ts),
        }
        if len(_suspicious_pattern_cache) > 500:
            oldest_wallet = min(
                _suspicious_pattern_cache,
                key=lambda w: int(_suspicious_pattern_cache[w].get("synced_ts") or 0),
            )
            _suspicious_pattern_cache.pop(oldest_wallet, None)


def get_suspicious_status_cache(wallet: str) -> Dict[str, Any] | None:
    with _suspicious_status_cache_lock:
        cached = _suspicious_status_cache.get(wallet)
        if cached is None:
            return None
        return {
            "open_by_key": cached.get("open_by_key", {}),
            "closed_by_key": cached.get("closed_by_key", {}),
            "synced_ts": int(cached.get("synced_ts") or 0),
        }


def set_suspicious_status_cache(
    wallet: str,
    *,
    open_by_key: Dict[tuple[str, str], Dict[str, Any]],
    closed_by_key: Dict[tuple[str, str], Dict[str, Any]],
    synced_ts: int,
) -> None:
    with _suspicious_status_cache_lock:
        _suspicious_status_cache[wallet] = {
            "open_by_key": open_by_key,
            "closed_by_key": closed_by_key,
            "synced_ts": int(synced_ts),
        }
        if len(_suspicious_status_cache) > 500:
            oldest_wallet = min(
                _suspicious_status_cache,
                key=lambda w: int(_suspicious_status_cache[w].get("synced_ts") or 0),
            )
            _suspicious_status_cache.pop(oldest_wallet, None)
