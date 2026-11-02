#!/usr/bin/env python3
"""What a chain-direct roster lane would COST in wire bytes on the research Helius key.

WHY THIS RUNS BEFORE THE LANE. The research WS beat the J7 relay by a median 0.45 s on 7 of 7
events, and forward verification proved 23 of 27 roster addresses (2026-09-22). The lane is worth
building -- but the free tier is 1M credits, 10 RPS and roughly 50 MB a month, and nobody had
measured what the roster costs. A latency win that exhausts the quota in a week is an outage with
extra steps.

ONE SOCKET, MANY SUBSCRIPTIONS. Opening one WebSocket per wallet fails: 27 concurrent connections
returned HTTP 429 on all but one. A single socket carrying 27 `logsSubscribe` subscriptions acked
27 of 27 with zero errors, so that is both the correct architecture and the only one measured here.

THE SHORT CAPTURE CANNOT PRICE A SPARSE WALLET, so this reports two instruments:

  --capture  the live socket, which prices BYTES PER FRAME honestly and catches a firehose wallet
             immediately, but sees nothing from a trader who trades twice a day.
  --project  `getSignaturesForAddress` over each wallet's own history (a 6-day window for a normal
             trader) times the measured frame size. This is the defensible monthly number.

`logsSubscribe` frames carry only {signature, err, logs} -- NO ACCOUNT KEYS. Do not "verify" the
`mentions` filter by looking for the address inside a frame; it is never there, and that false
negative reads exactly like a broken filter. The honest control is a never-used pubkey, which
returns zero frames while a real one returns thousands.

    python3 scripts/research/memecoin_ws_byte_budget.py --project
    python3 scripts/research/memecoin_ws_byte_budget.py --capture --seconds 120
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FREE_TIER_MB_MONTH = 50.0
# Mean non-bot logsSubscribe frame, measured 2026-09-22 (bot frames run ~674 B, trader frames
# 1.4-5.4 kB). Sampled from FIVE frames, so treat the projection as an order of magnitude.
DEFAULT_FRAME_BYTES = 3380.0
HOURS_PER_MONTH = 730.0


def _sol_book_open():
    conn = sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % ROOT, uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE execution_mode='real' "
            "AND network_id='solana' AND closed_at IS NULL AND COALESCE(tracking_paused_at,'')=''").fetchone()[0]
    finally:
        conn.close()


def targets():
    conn = sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % ROOT, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [(r["h"], r["a"]) for r in conn.execute(
            "SELECT DISTINCT d.x_handle h, d.address a FROM axiom_trader_directory d "
            "JOIN memecoin_wallets w ON LOWER(w.handle)=LOWER(d.x_handle) "
            "WHERE d.chain='solana' AND w.real_approved=1 ORDER BY d.x_handle")]
    finally:
        conn.close()


def _research_url():
    from sniper.util.envfiles import load_env_files
    load_env_files(str(ROOT))
    url = os.getenv("MEMECOIN_RESEARCH_HELIUS_RPC_URL")
    custody = (os.getenv("MEMECOIN_SOLANA_RPC_URL") or "").strip()
    if not url:
        raise SystemExit("MEMECOIN_RESEARCH_HELIUS_RPC_URL unset; refusing a funded endpoint")
    if custody and url.strip() == custody:
        raise SystemExit("research URL equals the funded custody URL; refusing")
    return url


def project(url, frame_bytes):
    """Monthly bytes from each wallet's OWN transaction rate. The defensible number."""
    def rpc(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.load(resp)

    rows = []
    for handle, addr in targets():
        try:
            res = rpc("getSignaturesForAddress", [addr, {"limit": 1000}]).get("result") or []
        except Exception as exc:
            print("  %-20s READ FAILED %r (not a zero)" % (handle, exc))
            continue
        span = ((res[0].get("blockTime") or 0) - (res[-1].get("blockTime") or 0)
                if len(res) >= 2 else 0)
        rate = (len(res) / span) if span > 0 else 0.0
        rows.append((handle, addr, rate, rate * frame_bytes * 3600 / 1e6))
        time.sleep(0.25)

    print("%-20s %12s %12s %12s" % ("handle", "tx/s", "MB/hour", "MB/month"))
    for handle, _a, rate, mbh in sorted(rows, key=lambda r: -r[2]):
        print("%-20s %12.4f %12.3f %12.1f" % (handle, rate, mbh, mbh * HOURS_PER_MONTH))
    total = sum(r[3] for r in rows) * HOURS_PER_MONTH
    # A wallet transacting faster than once a second is a bot, not a trader to mirror.
    bots = [r for r in rows if r[2] >= 1.0]
    quiet = sum(r[3] for r in rows if r[2] < 1.0) * HOURS_PER_MONTH
    print("\n=== PROJECTION, %d verified Solana addresses ===" % len(rows))
    print("  all wallets          %.0f MB/month" % total)
    if bots:
        print("  bot-rate wallets     %s" % ", ".join("%s (%.0f tx/s)" % (b[0], b[2]) for b in bots))
        print("  excluding them       %.1f MB/month" % quiet)
    verdict = "WITHIN" if quiet <= FREE_TIER_MB_MONTH else "OVER by %.1fx" % (
        quiet / FREE_TIER_MB_MONTH)
    print("  free tier ~%.0f MB     -> %s" % (FREE_TIER_MB_MONTH, verdict))
    return rows


async def _capture(url, secs):
    import websockets
    tgt = targets()
    byid, stats = {}, {h: [0, 0] for h, _ in tgt}
    async with websockets.connect(url.replace("https://", "wss://", 1),
                                  max_size=None, ping_interval=20) as ws:
        for i, (_h, addr) in enumerate(tgt, start=1):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                      "params": [{"mentions": [addr]},
                                                 {"commitment": "processed"}]}))
        acked = errors = 0
        start = time.monotonic()
        while time.monotonic() - start < secs:
            if _sol_book_open():
                print("  ABORT: a real Solana position opened mid-capture")
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("id") is not None:
                if "error" in msg:
                    errors += 1
                else:
                    acked += 1
                    byid[msg["result"]] = tgt[msg["id"] - 1][0]
                continue
            handle = byid.get((msg.get("params") or {}).get("subscription"))
            if handle:
                stats[handle][0] += 1
                stats[handle][1] += len(raw.encode() if isinstance(raw, str) else raw)
    print("  one socket, %d subscriptions: acked=%d errors=%d" % (len(tgt), acked, errors))
    hours = secs / 3600.0
    for handle, (frames, nbytes) in sorted(stats.items(), key=lambda kv: -kv[1][1]):
        if frames:
            print("  %-20s frames %7d  %10d B  %8.1f MB/hour  (%.0f B/frame)"
                  % (handle, frames, nbytes, nbytes / hours / 1e6, nbytes / frames))
    silent = [h for h, v in stats.items() if not v[0]]
    print("  silent in this window (%d): %s" % (len(silent), ", ".join(silent)))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--capture", action="store_true", help="live socket, prices bytes per frame")
    ap.add_argument("--project", action="store_true", help="monthly bytes from signature rates")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--frame-bytes", type=float, default=DEFAULT_FRAME_BYTES)
    args = ap.parse_args(argv)
    if not (args.capture or args.project):
        args.project = True

    url = _research_url()
    # OPERATOR RULE: no research read on the Helius key while the funded Solana lane is live.
    if _sol_book_open():
        print("REFUSED: a real Solana position is open.")
        return 1
    if args.capture:
        asyncio.run(_capture(url, args.seconds))
    if args.project:
        project(url, args.frame_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
