#!/usr/bin/env python3
"""Forward-only Hyperliquid cascade-observation capture.

Subscribes to the HL trades WS for the top-N assets, polls metaAndAssetCtxs for market mid + OI
each bucket, and writes raw per-bucket rows to hl_cascade_obs (the detector derives ret/sigma/oi
deltas downstream). No backfill. Read-only w.r.t. exchange. clearinghouseState is NOT used here
(market OI comes only from asset context). See the cascade detector design + plan."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import persistence as P  # noqa: E402

HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"
HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "cascade_capture.heartbeat")
BACKOFF_CAP_S = 60.0        # cap reconnect backoff — a 1s retry storm gets the WS IP-throttled
STALL_MS = 120_000         # no trades across all top-N assets for this long => reconnect (half-open)


def aggregate_buckets(trades, bucket_ms, *, now_ms):
    """Pure: {coin: (n_trades, traded_notional)} for trades in [now_ms - bucket_ms, now_ms)."""
    lo = now_ms - bucket_ms
    out = {}
    for tr in trades:
        t = int(tr.get("time") or 0)
        if t < lo or t >= now_ms:
            continue
        coin = str(tr.get("coin") or "").upper()
        if not coin:
            continue
        try:
            notl = float(tr.get("px") or 0) * float(tr.get("sz") or 0)
        except (TypeError, ValueError):
            continue
        n, s = out.get(coin, (0, 0.0))
        out[coin] = (n + 1, s + notl)
    return out


def bucket_boundary(ts_ms, bucket_ms):
    """Floor a timestamp to its bucket start."""
    return (int(ts_ms) // bucket_ms) * bucket_ms


def update_clock_offset(offset_ms, trades, recv_ms):
    """Track local_wall - exchange offset from the trade stream.

    This host's wall clock runs ~7h ahead of real/exchange time (env skew), so bucketing
    trade timestamps against the local clock drops every trade. `recv_ms - trade.time`
    equals the true offset plus a non-negative network/processing delay, so the running
    minimum converges to the true offset. Returns the updated offset (or the prior value)."""
    for tr in trades:
        t = int(tr.get("time") or 0)
        if not t:
            continue
        off = recv_ms - t
        offset_ms = off if offset_ms is None else min(offset_ms, off)
    return offset_ms


def resolve_bucket(now_local_ms, offset_ms, bucket_ms, last_ex_flush):
    """Exchange-time bucket boundary to flush, or None if the offset is unknown or this is
    not a new bucket. Flushing is driven by the local wall clock but the boundary (and the
    trade window) live in exchange time so the +7h host skew can't exclude real trades."""
    if offset_ms is None:
        return None
    ex_flush = bucket_boundary(now_local_ms - offset_ms, bucket_ms)
    if last_ex_flush is not None and ex_flush <= last_ex_flush:
        return None
    return ex_flush


def extract_trade_updates(msg):
    """Return trade dicts from known HL trade WS frame shapes.

    Official frames use data=[WsTrade]. Some clients/relays wrap that as
    data={coin, trades:[...]}; keep the coin so bucket aggregation does not
    silently lose volume.
    """
    if msg.get("channel") != "trades":
        return []
    data = msg.get("data")
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    if not isinstance(data, dict):
        return []
    coin = data.get("coin")
    if "px" in data and "sz" in data:
        trades = [data]
    else:
        raw = data.get("trades") or data.get("data") or []
        trades = raw if isinstance(raw, list) else []
    out = []
    for tr in trades:
        if not isinstance(tr, dict):
            continue
        if coin and not tr.get("coin"):
            tr = {**tr, "coin": coin}
        out.append(tr)
    return out


def _info(payload):
    import requests
    r = requests.post(HL_INFO, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _assets_mid_oi(max_assets):
    """Returns (assets, {COIN: (mid, oi)}) from metaAndAssetCtxs, top-N by 24h notional volume."""
    data = _info({"type": "metaAndAssetCtxs"})
    meta, ctxs = data[0], data[1]
    names = [m["name"] for m in meta.get("universe") or []]
    pairs = sorted(zip(names, ctxs or []), key=lambda x: -float(x[1].get("dayNtlVlm") or 0))
    pairs = pairs[:max_assets]
    assets = [a for a, _ in pairs]
    mid_oi = {}
    for a, c in pairs:
        mid = c.get("midPx") or c.get("markPx")
        oi = c.get("openInterest")
        mid_oi[a.upper()] = (float(mid) if mid else None, float(oi) if oi else None)
    return assets, mid_oi


def _subscribe(assets):
    import websocket  # websocket-client
    ws = websocket.create_connection(HL_WS, timeout=15)
    for a in assets:
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": a}}))
    ws.settimeout(1)
    return ws


def next_backoff(cur):
    """Exponential reconnect backoff, capped at BACKOFF_CAP_S. A fixed 1s retry storms HL's WS
    and gets the IP throttled (the root cause of the 7h zombie); backing off lets it recover."""
    return min(max(float(cur), 1.0) * 2.0, BACKOFF_CAP_S)


def feed_stalled(now_ms, last_trade_ms, stall_ms=STALL_MS):
    """True when no trade has arrived for stall_ms — a half-open socket delivers nothing yet
    raises nothing, so we must force a reconnect on silence, not only on exceptions."""
    return (now_ms - last_trade_ms) >= stall_ms


def _write_heartbeat(path=HEARTBEAT_PATH):
    """Liveness beacon written on each obs flush (host wall clock). The watchdog checks its age;
    obs ts_ms is exchange-time (+7h host skew) so it can't be compared to the wall clock."""
    try:
        with open(path, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def _reconnect(assets, old_ws):
    """Close any prior socket (avoid fd leaks / duplicate subs), then open + subscribe fresh."""
    try:
        if old_ws is not None:
            old_ws.close()
    except Exception:
        pass
    return _subscribe(assets)


def main(argv=None):
    ap = argparse.ArgumentParser(description="HL cascade observation capture")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=20)
    ap.add_argument("--bucket-seconds", type=int, default=10)
    args = ap.parse_args(argv)

    import websocket  # websocket-client (imported for the timeout exception type)
    conn = P.connect(args.db)
    P.ensure_schema(conn)
    bucket_ms = args.bucket_seconds * 1000

    assets, _ = _assets_mid_oi(args.max_assets)
    ws = None
    backoff = 1.0

    buf = []
    offset_ms = None          # local_wall - exchange (~ +7h env skew; see update_clock_offset)
    last_ex_flush = None
    last_trade_ms = int(time.time() * 1000)
    next_local_flush = (int(time.time() * 1000) // bucket_ms + 1) * bucket_ms
    print(f"capturing {len(assets)} assets, bucket={args.bucket_seconds}s -> {args.db}", flush=True)
    while True:
        if ws is None:                              # (re)connect with capped exponential backoff
            try:
                ws = _reconnect(assets, None)
                backoff = 1.0
                last_trade_ms = int(time.time() * 1000)
            except Exception as e:  # noqa: BLE001
                print(f"ws connect failed: {type(e).__name__}: {e}; retry in {backoff:.0f}s", flush=True)
                time.sleep(backoff)
                backoff = next_backoff(backoff)
                continue
        if feed_stalled(int(time.time() * 1000), last_trade_ms):   # half-open socket, no data
            print("ws feed stalled (no trades) -> reconnect", flush=True)
            try:
                ws = _reconnect(assets, ws)
            except Exception:  # noqa: BLE001
                ws = None
            last_trade_ms = int(time.time() * 1000)
            continue
        try:
            raw = ws.recv()
            if not raw:                             # empty frame == socket closed/half-open
                raise ConnectionError("empty ws frame")
            trades = extract_trade_updates(json.loads(raw))
            if trades:
                offset_ms = update_clock_offset(offset_ms, trades, int(time.time() * 1000))
                buf.extend(trades)
                last_trade_ms = int(time.time() * 1000)
        except websocket.WebSocketTimeoutException:
            pass
        except Exception as e:  # noqa: BLE001
            print(f"ws error: {type(e).__name__}: {e}; retry in {backoff:.0f}s", flush=True)
            try:
                ws.close()
            except Exception:
                pass
            ws = None
            time.sleep(backoff)
            backoff = next_backoff(backoff)
            continue

        now_local = int(time.time() * 1000)
        if now_local < next_local_flush:
            continue
        next_local_flush += bucket_ms
        ex_flush = resolve_bucket(now_local, offset_ms, bucket_ms, last_ex_flush)
        if ex_flush is None:
            continue          # no trade seen yet -> no exchange clock, or not a new bucket
        agg = aggregate_buckets(buf, bucket_ms, now_ms=ex_flush)
        try:
            _, mid_oi = _assets_mid_oi(args.max_assets)
        except Exception as e:  # noqa: BLE001
            print(f"ctx fetch error: {type(e).__name__}", flush=True)
            mid_oi = {}
        rows = {}
        for a in assets:
            au = a.upper()
            mid, oi = mid_oi.get(au, (None, None))
            if mid is None:
                continue          # never poison the obs series with a None mid (detector reads it)
            n, notl = agg.get(au, (0, 0.0))
            rows[au] = (mid, oi, n, notl)
        if rows:
            P.save_cascade_obs(conn, rows, ts_ms=ex_flush)
            _write_heartbeat()
        buf = [t for t in buf if int(t.get("time") or 0) >= ex_flush]
        last_ex_flush = ex_flush


if __name__ == "__main__":
    raise SystemExit(main())
