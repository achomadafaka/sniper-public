#!/usr/bin/env python3
"""Live order-book snapshot capture — APPEND-ONLY, no analyzer logic.

Pure data accumulation for the future `price_capture` / `liquidity_provider`
thesis cards (the true versions need order-book history we do NOT have for the
past — so we start capturing it now; every day delayed is missing evidence).
This script does ONE snapshot per invocation; schedule it (cron/timer) to
accumulate. It writes nothing but raw book/mid/spread rows; it makes NO verdicts.

Per active market (capped, volume-floored) it records, for BOTH outcome tokens:
  snapshot_ts, condition_id, token_id, side_index, best_bid, best_ask, mid,
  spread, bid_depth_top5, ask_depth_top5, n_bids, n_asks, last_trade_price,
  neg_risk, tick_size, market_volume.

Output: one parquet per run -> <out-dir>/<YYYYMMDD>/<HHMMSS>.parquet (append-only;
existing files never modified). Query later with DuckDB.

Usage (one snapshot):
  python3 scripts/capture_orderbook.py --out-dir data/orderbook_snapshots \
      --max-markets 400 --min-volume 5000
Schedule (every 10 min) via crontab:
  */10 * * * * cd /home/bigpapi/Desktop/Sniper && /usr/bin/python3 scripts/capture_orderbook.py >> runs/orderbook_capture.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

GAMMA = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"


def _f(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def active_markets(client, *, max_markets: int, min_volume: float) -> list[dict]:
    out, offset = [], 0
    while len(out) < max_markets:
        try:
            r = client.get(GAMMA, params={"active": "true", "closed": "false",
                                          "order": "volumeNum", "ascending": "false",
                                          "limit": 500, "offset": offset}, timeout=30)
        except Exception:
            break
        rows = r.json() if r.status_code < 400 else []
        if not isinstance(rows, list) or not rows:
            break
        for m in rows:
            vol = _f(m.get("volume"), 0.0)
            if vol < min_volume:
                continue
            toks = m.get("clobTokenIds")
            if isinstance(toks, str):
                try:
                    toks = json.loads(toks)
                except Exception:
                    toks = []
            if not toks or len(toks) != 2:
                continue
            out.append({"cid": str(m.get("conditionId") or ""), "tokens": [str(t) for t in toks],
                        "volume": vol})
            if len(out) >= max_markets:
                break
        offset += 500
        if len(rows) < 500:
            break
    return out


def book_snapshot(client, token: str) -> dict | None:
    try:
        r = client.get(CLOB_BOOK, params={"token_id": token}, timeout=15)
        if r.status_code >= 400:
            return None
        b = r.json()
    except Exception:
        return None
    bids = [(_f(x.get("price")), _f(x.get("size"))) for x in (b.get("bids") or [])]
    asks = [(_f(x.get("price")), _f(x.get("size"))) for x in (b.get("asks") or [])]
    bids = [(p, s) for p, s in bids if p is not None and s is not None]
    asks = [(p, s) for p, s in asks if p is not None and s is not None]
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    mid = (best_bid + best_ask) / 2 if (best_bid is not None and best_ask is not None) else None
    spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
    # top-5 depth nearest the touch
    bid_depth = sum(s for _, s in sorted(bids, key=lambda x: -x[0])[:5])
    ask_depth = sum(s for _, s in sorted(asks, key=lambda x: x[0])[:5])
    return {
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread": spread,
        "bid_depth_top5": bid_depth, "ask_depth_top5": ask_depth,
        "n_bids": len(bids), "n_asks": len(asks),
        "last_trade_price": _f(b.get("last_trade_price")),
        "neg_risk": bool(b.get("neg_risk")), "tick_size": _f(b.get("tick_size")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default="data/orderbook_snapshots")
    ap.add_argument("--max-markets", type=int, default=400)
    ap.add_argument("--min-volume", type=float, default=5000.0)
    ap.add_argument("--throttle", type=float, default=0.04, help="seconds between book calls")
    ap.add_argument("--now", type=int, default=0, help="snapshot epoch (Date.now is blocked in some envs); 0 = use time.time")
    args = ap.parse_args()

    snap_ts = args.now or int(time.time())
    from pathlib import Path
    day = time.strftime("%Y%m%d", time.gmtime(snap_ts))
    out = Path(args.out_dir) / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{time.strftime('%H%M%S', time.gmtime(snap_ts))}.parquet"

    client = httpx.Client(headers={"User-Agent": "sniper-orderbook-capture/1"})
    mkts = active_markets(client, max_markets=args.max_markets, min_volume=args.min_volume)
    print(f"snapshot {snap_ts}: {len(mkts)} active markets >= ${args.min_volume:,.0f}", file=sys.stderr)

    rows = []
    for m in mkts:
        for side, tok in enumerate(m["tokens"]):
            snap = book_snapshot(client, tok)
            if snap is None:
                continue
            rows.append({"snapshot_ts": snap_ts, "condition_id": m["cid"], "token_id": tok,
                         "side_index": side, "market_volume": m["volume"], **snap})
            time.sleep(args.throttle)
    client.close()

    if not rows:
        print("no rows captured (transient API issue?) — wrote nothing", file=sys.stderr)
        return 0
    # write parquet via duckdb (no pandas dependency assumption)
    import duckdb
    con = duckdb.connect()
    con.execute("""CREATE TEMP TABLE snap (snapshot_ts BIGINT, condition_id VARCHAR, token_id VARCHAR,
        side_index INT, market_volume DOUBLE, best_bid DOUBLE, best_ask DOUBLE, mid DOUBLE, spread DOUBLE,
        bid_depth_top5 DOUBLE, ask_depth_top5 DOUBLE, n_bids INT, n_asks INT, last_trade_price DOUBLE,
        neg_risk BOOLEAN, tick_size DOUBLE)""")
    con.executemany("INSERT INTO snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(r["snapshot_ts"], r["condition_id"], r["token_id"], r["side_index"], r["market_volume"],
                      r["best_bid"], r["best_ask"], r["mid"], r["spread"], r["bid_depth_top5"], r["ask_depth_top5"],
                      r["n_bids"], r["n_asks"], r["last_trade_price"], r["neg_risk"], r["tick_size"]) for r in rows])
    con.execute(f"COPY snap TO '{path}' (FORMAT PARQUET)")
    con.close()
    print(f"wrote {path}: {len(rows)} rows ({len(mkts)} markets x 2 sides)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
