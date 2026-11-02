"""Route a RawEvent to one or more (table_name, row_dict) pairs."""
from __future__ import annotations

import json
from typing import Optional

from sniper.sim.schema import RawEvent, EventType


def _safe_float(val, default=None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=None) -> Optional[int]:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _common_fields(evt: RawEvent) -> dict:
    return {
        "market_id": evt.market_id,
        "asset_id": evt.asset_id,
        "capture_ts": evt.capture_ts,
        "exchange_ts": evt.exchange_ts,
        "ingest_seq": evt.ingest_seq,
        "venue": evt.venue,
    }


def _route_trade(evt: RawEvent) -> tuple[str, dict]:
    p = evt.payload
    row = {
        **_common_fields(evt),
        "price": _safe_float(p.get("price")),
        "size": _safe_float(p.get("size")),
        "side": p.get("side", ""),
        "fee_rate_bps": _safe_int(p.get("fee_rate_bps")),
        "transaction_hash": p.get("transaction_hash"),
        "market_type": evt.market_type,
    }
    return ("trades", row)


def _route_book_delta(evt: RawEvent) -> list[tuple[str, dict]]:
    p = evt.payload
    common = _common_fields(evt)
    price = _safe_float(p.get("price"))
    size = _safe_float(p.get("size"))
    side = p.get("side", "")
    best_bid = _safe_float(p.get("best_bid"))
    best_ask = _safe_float(p.get("best_ask"))

    delta_row = {
        **common,
        "price": price,
        "size": size,
        "side": side,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }

    # Derive bid_size/ask_size only when delta is at the top price
    bid_size = None
    ask_size = None
    if side == "BUY" and price is not None and best_bid is not None and abs(price - best_bid) < 1e-9:
        bid_size = size
    if side == "SELL" and price is not None and best_ask is not None and abs(price - best_ask) < 1e-9:
        ask_size = size

    quote_row = {
        **common,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "source_event": "book_delta",
    }

    return [("book_deltas", delta_row), ("quotes_top", quote_row)]


def _route_best_bba(evt: RawEvent) -> tuple[str, dict]:
    p = evt.payload
    return ("quotes_top", {
        **_common_fields(evt),
        "best_bid": _safe_float(p.get("best_bid")),
        "best_ask": _safe_float(p.get("best_ask")),
        "bid_size": None,
        "ask_size": None,
        "source_event": "best_bba",
    })


def _route_book_snapshot(evt: RawEvent) -> list[tuple[str, dict]]:
    p = evt.payload
    common = _common_fields(evt)
    bids = p.get("bids", [])
    asks = p.get("asks", [])

    results: list[tuple[str, dict]] = []

    # Top-of-book quote
    top_bid = _safe_float(bids[0]["price"]) if bids else None
    top_ask = _safe_float(asks[0]["price"]) if asks else None
    top_bid_size = _safe_float(bids[0]["size"]) if bids else None
    top_ask_size = _safe_float(asks[0]["size"]) if asks else None

    results.append(("quotes_top", {
        **common,
        "best_bid": top_bid,
        "best_ask": top_ask,
        "bid_size": top_bid_size,
        "ask_size": top_ask_size,
        "source_event": "book_snapshot",
    }))

    # All levels
    for i, bid in enumerate(bids):
        results.append(("book_levels", {
            "market_id": evt.market_id,
            "asset_id": evt.asset_id,
            "capture_ts": evt.capture_ts,
            "ingest_seq": evt.ingest_seq,
            "side": "bid",
            "price": _safe_float(bid["price"]),
            "size": _safe_float(bid["size"]),
            "level_index": i,
            "venue": evt.venue,
        }))

    for i, ask in enumerate(asks):
        results.append(("book_levels", {
            "market_id": evt.market_id,
            "asset_id": evt.asset_id,
            "capture_ts": evt.capture_ts,
            "ingest_seq": evt.ingest_seq,
            "side": "ask",
            "price": _safe_float(ask["price"]),
            "size": _safe_float(ask["size"]),
            "level_index": i,
            "venue": evt.venue,
        }))

    return results


def _route_lifecycle(evt: RawEvent) -> tuple[str, dict]:
    return ("market_lifecycle", {
        "market_id": evt.market_id,
        "capture_ts": evt.capture_ts,
        "ingest_seq": evt.ingest_seq,
        "event_type": evt.event_type.value,
        "market_type": evt.market_type,
        "payload": json.dumps(evt.payload),
        "venue": evt.venue,
    })


def route_event(evt: RawEvent) -> tuple[str, dict] | list[tuple[str, dict]]:
    """Route a RawEvent to (table_name, row_dict) or list of them.

    Returns a single tuple for events that map 1:1 to a table row.
    Returns a list for events that produce multiple rows (book_delta -> delta + quote,
    book_snapshot -> quote + N levels).
    """
    if evt.event_type == EventType.TRADE:
        return _route_trade(evt)
    if evt.event_type == EventType.BOOK_DELTA:
        return _route_book_delta(evt)
    if evt.event_type == EventType.BEST_BBA:
        return _route_best_bba(evt)
    if evt.event_type == EventType.BOOK_SNAPSHOT:
        return _route_book_snapshot(evt)
    if evt.event_type in (EventType.MARKET_METADATA, EventType.MARKET_RESOLVED):
        return _route_lifecycle(evt)
    return []
