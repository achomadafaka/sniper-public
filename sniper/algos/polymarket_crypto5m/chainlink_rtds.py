"""Chainlink BTC/USD collector over Polymarket's Real-Time Data Socket (RTDS).

RTDS is public / no-auth (verified 2026-06-20). This is the *live* source for the Price-to-Beat
(Gamma only backfills ``priceToBeat`` at resolution). Pure ``parse_frame`` is unit-tested from
recorded frames; the thin WS loop (``stream`` / ``collect``) takes an injected socket so no test
touches the live API.

Two incoming shapes on ``topic=crypto_prices_chainlink``:
  * snapshot on subscribe — ``payload.data[]`` backfill items ``{timestamp, value}`` (no exact value);
  * update — ``payload`` ``{full_accuracy_value, symbol, timestamp, value}`` + outer ``timestamp``.

``full_accuracy_value`` (price * 10**18) is parsed as an exact ``Decimal`` (see ``ChainlinkTick``).
"""
from __future__ import annotations

import json
import time
from typing import Callable, Iterable, Optional

from .models import RtdsDeliveryMode

from .models import ChainlinkTick

RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_chainlink"
DEFAULT_SYMBOL = "btc/usd"
PING_INTERVAL_S = 5


def subscribe_message(symbol: str = DEFAULT_SYMBOL, *, type_filter: str = "*") -> dict:
    """The exact RTDS subscribe frame. ``type_filter`` is probed (``"*"`` vs ``"update"``) so the
    observed delivery mode is recorded, not assumed."""
    return {"action": "subscribe",
            "subscriptions": [{"topic": TOPIC, "type": type_filter,
                               "filters": json.dumps({"symbol": symbol})}]}


def classify_delivery(ticks) -> RtdsDeliveryMode:
    """What actually arrived for a window. A live ``update`` tick -> live_updates; only snapshot
    backfill -> snapshot_reconstruction; nothing -> unavailable. Snapshot-only is NEVER 'live'."""
    ticks = list(ticks or [])
    if any(not t.is_snapshot for t in ticks):
        return RtdsDeliveryMode.LIVE_UPDATES
    if ticks:
        return RtdsDeliveryMode.SNAPSHOT_RECONSTRUCTION
    return RtdsDeliveryMode.UNAVAILABLE


def staleness_ms(ticks, *, now_ms: int) -> Optional[int]:
    """Age of the freshest Chainlink observation in ms (watchdog input). None if no ticks."""
    ts = [t.source_ts_ms for t in (ticks or []) if t.source_ts_ms is not None]
    return (int(now_ms) - max(ts)) if ts else None


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_frame(raw, *, recv_ts: Optional[int] = None) -> list[ChainlinkTick]:
    """Parse one raw RTDS text frame into ``ChainlinkTick``s. Control/non-data frames -> ``[]``.

    Never raises on a malformed frame and NEVER synthesizes a price: a tick with a missing/unparseable
    ``timestamp`` or ``value`` is skipped, not turned into a fake 0.0 observation.
    """
    if not raw or not isinstance(raw, str):
        return []
    raw = raw.strip()
    if not raw.startswith("{"):
        return []  # PONG / control text
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(msg, dict):
        return []
    payload = msg.get("payload")
    if not isinstance(payload, dict):
        return []
    server_ts = _to_int(msg.get("timestamp"))

    # snapshot backfill: payload.data[] of {timestamp, value}
    if isinstance(payload.get("data"), list):
        out = []
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            src = _to_int(item.get("timestamp"))
            val = _to_float(item.get("value"))
            if src is None or val is None:        # never fabricate a 0.0 Chainlink tick
                continue
            out.append(ChainlinkTick(
                symbol=item.get("symbol") or DEFAULT_SYMBOL,
                source_ts_ms=src, server_ts_ms=server_ts, recv_ts=recv_ts,
                value=val, full_accuracy_value=item.get("full_accuracy_value"), is_snapshot=True,
            ))
        return out

    # live update: payload = {full_accuracy_value, symbol, timestamp, value}
    src = _to_int(payload.get("timestamp"))
    val = _to_float(payload.get("value"))
    if src is None or val is None:
        return []
    return [ChainlinkTick(
        symbol=payload.get("symbol") or DEFAULT_SYMBOL,
        source_ts_ms=src, server_ts_ms=server_ts, recv_ts=recv_ts,
        value=val, full_accuracy_value=payload.get("full_accuracy_value"), is_snapshot=False,
    )]


# --- thin WS loop (socket injected; not unit-tested against live) ----------------------------

def stream(ws, *, recv_frames: int, now_fn: Callable[[], int], symbol: str = DEFAULT_SYMBOL,
           on_tick: Optional[Callable[[ChainlinkTick], None]] = None) -> list[ChainlinkTick]:
    """Subscribe on ``ws`` and read up to ``recv_frames`` frames, parsing each into ticks.

    ``ws`` must expose ``send(str)`` and ``recv()->str`` (websocket-client compatible). ``now_fn``
    returns server-time unix seconds (never the local +7h clock). PING cadence is the caller's job in
    a real loop; here we read a bounded number of frames for determinism/testability.
    """
    ws.send(json.dumps(subscribe_message(symbol)))
    ticks: list[ChainlinkTick] = []
    for _ in range(recv_frames):
        try:
            raw = ws.recv()
        except Exception:
            break
        for t in parse_frame(raw, recv_ts=now_fn()):
            ticks.append(t)
            if on_tick is not None:
                on_tick(t)
    return ticks


# RTDS only delivers a backfill batch ON SUBSCRIBE; it does NOT stream in-window updates (verified
# 2026-06-21: a single subscribe captures only the pre-subscribe history, so chainlink_now goes stale by
# the 120-270s checkpoints). Re-subscribing periodically pulls fresh batches that include the latest
# in-window ticks; overlapping ticks across batches dedup downstream by dedup_key.
RESUBSCRIBE_INTERVAL_S = 15


def collect_resubscribing(connect_fn, *, stop_fn, on_tick, now_fn, clock_fn,
                          interval_s: int = RESUBSCRIBE_INTERVAL_S, symbol: str = DEFAULT_SYMBOL,
                          sleep_fn: Callable[[float], None] = time.sleep, connect_backoff_s: float = 2.0) -> None:
    """Long-lived RTDS collector that RE-SUBSCRIBES every ``interval_s`` to pull fresh backfill batches
    (RTDS does not stream in-window). Socket-agnostic for testability:
      ``connect_fn()`` -> ws with send/recv/close (recv raises on timeout/EOF — tolerated between batches);
      ``stop_fn()`` -> True to stop; ``now_fn()`` -> server unix-secs for recv_ts; ``clock_fn()`` ->
      monotonic secs for cadence; ``on_tick(tick)`` persists each parsed tick.
    A recv exception is a normal inter-batch gap and does NOT force a reconnect — only the interval does."""
    ws = None
    last_sub = None
    while not stop_fn():
        if ws is None or last_sub is None or (clock_fn() - last_sub) >= interval_s:
            if ws is not None:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                ws = connect_fn()
                ws.send(json.dumps(subscribe_message(symbol)))
                last_sub = clock_fn()
            except Exception:  # noqa: BLE001 — transient connect failure; back off so an RTDS outage
                ws, last_sub = None, None      # can't busy-loop / storm reconnects (pegs a core otherwise)
                sleep_fn(connect_backoff_s)
                continue
        try:
            raw = ws.recv()
        except Exception:  # noqa: BLE001 — recv timeout between batches is normal; keep the connection
            continue
        for t in parse_frame(raw, recv_ts=now_fn()):
            on_tick(t)
    if ws is not None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
