#!/usr/bin/env python3
"""Cross-asset lead-lag capture for the Hyperliquid `xyz` HIP-3 dex (equities/indices/
commodities/FX). Every refresh: snapshot the HL perp mids + a free real-world reference
(Yahoo), and log the perp-vs-real basis when the reference is LIVE (its own timestamp
advanced -> market open, actively price-discovering). Reuses the scan_log schema so the
existing basis_direction_edge audit runs over it unchanged.

Public data only. Research capture; paper only; no orders, no keys. Basis is perp-vs-real,
never PnL. Reference is logged only when live, so weekend/after-hours stale prints are
dropped (clock-offset-proof: see xyz_leadlag.is_live)."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.hyperlive import scan_log as SL  # noqa: E402
from sniper.hyperlive import xyz_leadlag as XL  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
XYZ_DEX = "xyz"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hyperlive_xyz_scan_log.sqlite"
MIN_VOLUME_USD = 5_000_000.0
YF_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d"


def _post(payload: dict, timeout=20):
    req = urllib.request.Request(HL_INFO, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "curl/8"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def _fetch_xyz_contexts() -> list[dict]:
    meta, ctxs = _post({"type": "metaAndAssetCtxs", "dex": XYZ_DEX})
    return [{"name": u["name"], "ctx": c} for u, c in zip(meta["universe"], ctxs)]


def _yahoo(symbol: str, timeout=15):
    """(regularMarketPrice, regularMarketTime) for a Yahoo symbol, or (None, None)."""
    url = YF_CHART.format(urllib.parse.quote(symbol))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=timeout))
        m = d["chart"]["result"][0]["meta"]
        return m.get("regularMarketPrice"), m.get("regularMarketTime")
    except Exception:
        return None, None


def _mid(ctx: dict):
    for k in ("midPx", "markPx", "oraclePx"):
        try:
            v = float(ctx.get(k))
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return None


def _one_refresh(conn, prev_reg_time: dict, refresh_s: int) -> tuple[int, int]:
    """Log one xyz lead-lag refresh. Returns (rows_logged, live_basis_count)."""
    contexts = _fetch_xyz_contexts()
    referenceable = XL.select_referenceable(contexts, MIN_VOLUME_USD)
    # fetch each unique reference symbol once
    refs = {}
    for sym in {r["ref_symbol"] for r in referenceable}:
        px, t = _yahoo(sym)
        refs[sym] = (px, t)
    scan_ts = int(time.time() * 1000)
    rows, live = [], 0
    for r in referenceable:
        asset, ctx, sym = r["asset"], r["ctx"], r["ref_symbol"]
        hl_mid = _mid(ctx)
        ref_px, reg_t = refs.get(sym, (None, None))
        basis = None
        if XL.is_live(prev_reg_time.get(sym), reg_t):
            basis = XL.basis_bps(ref_px, hl_mid)
            if basis is not None:
                live += 1
        if reg_t is not None:
            prev_reg_time[sym] = reg_t
        try:
            vol = float(ctx.get("dayNtlVlm") or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        rows.append({"asset": asset, "selected_bool": True, "selected_rank": None,
                     "mid_px": hl_mid, "day_ntl_vlm": vol, "binance_basis_bps": basis,
                     "lead_reason": sym})
    SL.log_refresh(conn, scan_ts, rows, refresh_interval_s=refresh_s,
                   watchlist_state=",".join(r["asset"] for r in referenceable))
    return len(rows), live


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="xyz cross-asset lead-lag capture (public data only)")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--interval-seconds", type=int, default=150)
    ap.add_argument("--duration-seconds", type=int, default=0, help="0 = run until interrupted")
    ap.add_argument("--once", action="store_true", help="single refresh then exit (smoke)")
    args = ap.parse_args(argv)

    conn = SL.connect(args.db_path)
    SL.ensure_schema(conn)
    prev_reg_time: dict = {}
    start = time.time()
    try:
        while True:
            try:
                n, live = _one_refresh(conn, prev_reg_time, args.interval_seconds)
                print(f"{time.strftime('%FT%TZ', time.gmtime())} xyz refresh: {n} referenceable, "
                      f"{live} live basis logged", file=sys.stderr)
            except Exception as e:
                print(f"xyz refresh failed: {e}", file=sys.stderr)
            if args.once:
                break
            if args.duration_seconds and time.time() - start >= args.duration_seconds:
                break
            time.sleep(args.interval_seconds)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
