#!/usr/bin/env python3
"""WO-1 — re-verify the Solana WS hot-lane endpoint before arming. READ-ONLY, keyless.

The fastlane design measured wss://api.mainnet-beta.solana.com at median 404ms/slot (2026-07-24).
The WO requires re-verifying with a live probe before trusting it (do NOT assume the measurement
holds). This subscribes to slotNotification (no account needed) and reports the observed cadence.
Run it before enabling the hot lane; if the median has degraded, the <=1s decision target is at risk.

    python3 scripts/research/solana_ws_probe.py            # ~15s probe
    python3 scripts/research/solana_ws_probe.py --seconds 30

Verified 2026-07-25: 39 notifications / 15s, median 394ms (matches the prior 404ms).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

DEFAULT_WS = "wss://api.mainnet-beta.solana.com"


async def probe(url, seconds):
    try:
        import websockets
    except ImportError:
        print("websockets not installed"); return 2
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=3) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "slotSubscribe"}))
            n, t0, last, intervals = 0, time.time(), None, []
            while time.time() - t0 < seconds:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                d = json.loads(msg)
                if d.get("method") == "slotNotification":
                    now = time.time()
                    if last is not None:
                        intervals.append(now - last)
                    last = now
                    n += 1
    except Exception as e:  # noqa: BLE001
        print(f"PROBE FAILED: {type(e).__name__}: {str(e)[:120]}")
        return 2
    if not intervals:
        print(f"PROBE: connected, {n} notifications, no interval sample")
        return 2
    intervals.sort()
    med = intervals[len(intervals) // 2] * 1000
    p90 = intervals[int(len(intervals) * 0.9)] * 1000
    ok = med <= 1000
    print(f"PROBE {'OK' if ok else 'SLOW'}: {n} notifications in {seconds}s, "
          f"median {med:.0f}ms, p90 {p90:.0f}ms  (<=1s decision target {'viable' if ok else 'AT RISK'})")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="Solana WS hot-lane probe (read-only).")
    ap.add_argument("--url", default=DEFAULT_WS)
    ap.add_argument("--seconds", type=int, default=15)
    args = ap.parse_args()
    return asyncio.run(probe(args.url, args.seconds))


if __name__ == "__main__":
    sys.exit(main())
