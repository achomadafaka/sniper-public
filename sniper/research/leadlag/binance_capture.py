"""Append-only raw capture of Binance USD-M futures market data for the lead-lag study.

Public market data only. No keys, no orders. Stores source-event, transaction, and
local-receive timestamps separately — the local clock carries a large constant skew, so
NEVER mix an exchange ts with a receive ts in one delta (see the spec's clock discipline).
"""
from __future__ import annotations

import json
import os
import sqlite3


VENUE = "binance_um"


def connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS binance_raw_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            venue TEXT NOT NULL,
            symbol TEXT,
            stream TEXT NOT NULL,
            source_event_ts INTEGER,
            transaction_ts INTEGER,
            local_receive_ts INTEGER NOT NULL,
            best_bid REAL,
            best_ask REAL,
            bid_qty REAL,
            ask_qty REAL,
            price REAL,
            quantity REAL,
            is_buyer_maker INTEGER,
            update_id INTEGER,
            capture_lag_ms INTEGER,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_binance_raw_sym_ts
            ON binance_raw_events(symbol, source_event_ts);
        CREATE INDEX IF NOT EXISTS idx_binance_raw_stream
            ON binance_raw_events(stream, local_receive_ts);
    """)
    conn.commit()


def parse_message(msg: dict, local_receive_ts: int) -> dict | None:
    """Normalize one Binance combined-stream message into a raw row. None if unrecognized."""
    if not isinstance(msg, dict):
        return None
    stream = msg.get("stream")
    data = msg.get("data") if "data" in msg else msg
    if not isinstance(data, dict):
        return None
    kind = _kind(stream, data)
    if kind is None:
        return None
    event_ts = _int(data.get("E"))
    row = {
        "venue": VENUE,
        "symbol": (data.get("s") or _symbol_from_stream(stream) or "").upper() or None,
        "stream": stream or f"{(data.get('s') or '').lower()}@{kind}",
        "source_event_ts": event_ts,
        "transaction_ts": _int(data.get("T")),
        "local_receive_ts": int(local_receive_ts),
        "best_bid": None, "best_ask": None, "bid_qty": None, "ask_qty": None,
        "price": None, "quantity": None, "is_buyer_maker": None,
        "update_id": _int(data.get("u")),
        "capture_lag_ms": (int(local_receive_ts) - event_ts) if event_ts is not None else None,
        "payload_json": json.dumps(data, sort_keys=True),
    }
    if kind == "bookTicker":
        row["best_bid"] = _float(data.get("b"))
        row["bid_qty"] = _float(data.get("B"))
        row["best_ask"] = _float(data.get("a"))
        row["ask_qty"] = _float(data.get("A"))
    elif kind == "trade":
        row["price"] = _float(data.get("p"))
        row["quantity"] = _float(data.get("q"))
        row["is_buyer_maker"] = 1 if data.get("m") else 0
        row["transaction_ts"] = _int(data.get("T"))
    elif kind == "depth":
        bids = data.get("b") or []
        asks = data.get("a") or []
        if bids:
            row["best_bid"], row["bid_qty"] = _float(bids[0][0]), _float(bids[0][1])
        if asks:
            row["best_ask"], row["ask_qty"] = _float(asks[0][0]), _float(asks[0][1])
    return row


def insert_rows(conn: sqlite3.Connection, rows: list[dict]) -> int:
    rows = [r for r in rows if r]
    if not rows:
        return 0
    conn.executemany(
        """INSERT INTO binance_raw_events
           (venue, symbol, stream, source_event_ts, transaction_ts, local_receive_ts,
            best_bid, best_ask, bid_qty, ask_qty, price, quantity, is_buyer_maker,
            update_id, capture_lag_ms, payload_json)
           VALUES (:venue,:symbol,:stream,:source_event_ts,:transaction_ts,:local_receive_ts,
                   :best_bid,:best_ask,:bid_qty,:ask_qty,:price,:quantity,:is_buyer_maker,
                   :update_id,:capture_lag_ms,:payload_json)""",
        rows,
    )
    conn.commit()
    return len(rows)


def _kind(stream, data) -> str | None:
    s = (stream or "").lower()
    if s.endswith("@bookticker") or (data.get("b") is not None and data.get("a") is not None
                                     and data.get("B") is not None and data.get("A") is not None):
        return "bookTicker"
    if s.endswith("@aggtrade") or s.endswith("@trade") or data.get("e") in ("aggTrade", "trade"):
        return "trade"  # @aggTrade is dead on some endpoints; @trade carries the same fields
    if "@depth" in s or data.get("e") == "depthUpdate":
        return "depth"
    return None


def _symbol_from_stream(stream) -> str | None:
    if not stream:
        return None
    return stream.split("@", 1)[0] or None


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None
