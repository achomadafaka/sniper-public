#!/usr/bin/env python3
"""Liquidation cluster-map lane. Public HL data only; read-only; no keys/orders.
Harvests active addresses from the trades feed, polls clearinghouseState for their
liquidationPx, aggregates per-asset cluster magnets, and writes them for the engine."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import cluster_map as CM  # noqa: E402
from sniper.hyperlive import persistence as P  # noqa: E402

HL_WS = "wss://api.hyperliquid.xyz/ws"
HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_engine.sqlite"


def _info(payload: dict) -> dict:
    req = urllib.request.Request(HL_INFO, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def _mids_and_assets(max_assets: int):
    meta, ctxs = _info({"type": "metaAndAssetCtxs"})
    names = [m["name"] for m in meta.get("universe") or []]
    pairs = sorted(zip(names, ctxs or []), key=lambda x: -float(x[1].get("dayNtlVlm") or 0))
    assets = [a for a, _ in pairs[:max_assets]]
    mids = {a.upper(): float(c.get("midPx") or 0) for a, c in pairs if c.get("midPx")}
    return assets, mids


def _harvest_ws_addresses(assets, seconds, cap):
    """Active accounts seen on the trades WS over `seconds` (recency-biased)."""
    import websocket
    ws = websocket.create_connection(HL_WS, timeout=15)
    for a in assets:
        ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": a}}))
    addrs, start = set(), time.monotonic()
    ws.settimeout(2)
    while time.monotonic() - start < seconds and len(addrs) < cap:
        try:
            msg = json.loads(ws.recv())
        except Exception:
            continue
        if msg.get("channel") == "trades":
            for tr in msg.get("data") or []:
                for u in tr.get("users") or []:
                    if isinstance(u, str) and u.startswith("0x") and set(u[2:]) != {"0"}:
                        addrs.add(u)
    try:
        ws.close()
    except Exception:
        pass
    return addrs


def _harvest_leaderboard_addresses(top_n):
    """Top-N leaderboard wallets by account value — the persistent whales, not time-biased."""
    try:
        from sniper.markets import hyperliquid_api as HL
        rows = HL.fetch_leaderboard()
    except Exception:
        return set()
    rows = sorted(rows, key=lambda r: -float(r.get("account_value") or 0))
    out = set()
    for r in rows[:top_n]:
        w = r.get("wallet")
        if isinstance(w, str) and w.startswith("0x"):
            out.add(w)
    return out


def _harvest_addresses(assets, seconds, cap, *, leaderboard_top=200):
    """Multi-source, de-duplicated harvest: leaderboard whales UNION trades-WS active accounts.
    Whales are listed first so the cap can't starve them — widens + de-time-biases the sample
    (the WS alone over-weights whoever happened to trade in the last `seconds`)."""
    whales = _harvest_leaderboard_addresses(leaderboard_top)
    ws_addrs = _harvest_ws_addresses(assets, seconds, cap)
    combined = list(whales) + [a for a in ws_addrs if a not in whales]
    return combined[:cap]


def _round(conn, args) -> int:
    from concurrent.futures import ThreadPoolExecutor
    assets, mids = _mids_and_assets(args.max_assets)
    addrs = _harvest_addresses(assets, args.harvest_seconds, args.max_addresses)

    def fetch(a):
        try:
            return CM.positions_from_clearinghouse(_info({"type": "clearinghouseState", "user": a}))
        except Exception:
            return []
    positions = []
    with ThreadPoolExecutor(max_workers=24) as ex:   # weight-2 endpoint, ~600/min budget
        for ps in ex.map(fetch, addrs):
            positions.extend(ps)
    heatmap = CM.build_heatmap(positions)
    sigs = {}
    for asset, levels in heatmap.items():
        mid = mids.get(asset)
        if not mid:
            continue
        sig = CM.cluster_signal(levels, mid, min_notional=args.min_notional)
        if sig:
            sigs[asset] = sig
    ts_ms = int(time.time() * 1000)
    P.save_clusters(conn, sigs, ts_ms=ts_ms)                    # latest snapshot (engine reads this)
    P.save_cluster_history(conn, sigs, mids, ts_ms=ts_ms)      # append-only time series (cascade audit)
    print(f"cluster_map: assets={len(assets)} addresses={len(addrs)} signals={len(sigs)} -> {dict(list(sigs.items())[:6])}",
          file=sys.stderr)
    return len(sigs)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HL liquidation cluster-map lane (read-only)")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--max-assets", type=int, default=12)
    ap.add_argument("--max-addresses", type=int, default=300)
    ap.add_argument("--harvest-seconds", type=int, default=20)
    ap.add_argument("--min-notional", type=float, default=50_000.0)
    ap.add_argument("--interval-seconds", type=int, default=180)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    conn = P.connect(args.db_path)
    P.ensure_schema(conn)
    try:
        if args.once:
            _round(conn, args)
            return 0
        while True:
            try:
                _round(conn, args)
            except Exception as e:
                print(f"cluster_map round failed: {e}", file=sys.stderr)
            time.sleep(args.interval_seconds)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
