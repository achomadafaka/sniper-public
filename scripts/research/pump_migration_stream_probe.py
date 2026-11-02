#!/usr/bin/env python3
"""How soon after a pump.fun migration can the slot lane know the new PumpSwap pool?

Question 2026-09-27 (Alpha task 1, prewarm): 20 of the 27 lane-buildable pools the slot lane missed
`pool_unknown` since 2026-09-26 21:07Z were migrated within 24 h of the buy, both S+7 incidents
included (scripts/research/slot_lane_pool_miss_census.py), and none of those had been seen in the
ledger. Every migration mentions pump's migration account 39azUYFW..., and its pAMM CreatePoolEvent
(`Program data:`) names the pool (byte 173), the base mint (50), the quote mint (82), the index (16)
and the creator (18, the ["pool-authority", mint] PDA for a pump migration), all in the logs frame.

This subscribes ONE logsSubscribe {mentions: [39azUYFW...]} at `confirmed` on the keyless
wss://api.mainnet-beta.solana.com (the socket a runtime listener would use; one WS subscription, no
HTTP request against the IP's funded read budget) and records, per frame: bytes, decode time, the
decoded event, and whether a canonical wSOL pool is readable at `confirmed` right after receipt
(research key, one getMultipleAccounts each). At the end the pools' first-transaction blockTime is
read (research key) for receipt lag. Refuses to start while a real Solana position is open.

    python3 -u scripts/research/pump_migration_stream_probe.py --seconds 600
    python3 scripts/research/pump_migration_stream_probe.py --selftest
"""
import argparse
import asyncio
import contextlib
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
import funded_exit_truth as fet  # noqa: E402
from sniper.execution.pumpswap_buy import create_pool_event  # noqa: E402

fet.SPACING_S = 0.25
WS = "wss://api.mainnet-beta.solana.com"
MIGRATION_ACCOUNT = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"
WSOL = "So11111111111111111111111111111111111111112"
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
FRAMES = REPO / "tests" / "fixtures" / "memecoin" / "pump_migrate_frames.json"


def open_real_solana():
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        return c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=1 "
                         "AND execution_mode='real' AND network_id='solana'").fetchone()[0]


def readable(pool):
    t = time.monotonic()
    res = fet.rpc("getMultipleAccounts", [[pool], {"encoding": "base64", "commitment": "confirmed"}])
    return bool(((res or {}).get("value") or [None])[0]), (time.monotonic() - t - fet.SPACING_S) * 1000


async def run(seconds):
    import websockets
    rows, t_end = [], time.monotonic() + seconds
    async with websockets.connect(WS, max_size=None, ping_interval=20, open_timeout=10) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe", "params": [
            {"mentions": [MIGRATION_ACCOUNT]}, {"commitment": "confirmed"}]}))
        print("subscribe reply:", await asyncio.wait_for(ws.recv(), 10), flush=True)
        while time.monotonic() < t_end:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=max(0.1, t_end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            recv = time.time()
            t0 = time.perf_counter()
            value = json.loads(msg)["params"]["result"]["value"]
            ev = None if value.get("err") else create_pool_event(value.get("logs"))
            parse_us = (time.perf_counter() - t0) * 1e6
            row = {"recv": recv, "bytes": len(msg), "parse_us": parse_us, "err": bool(value.get("err")),
                   "sig": value.get("signature"), "event": ev}
            if ev and ev["canonical"]:
                row["readable"], row["read_ms"] = await asyncio.to_thread(readable, ev["pool"])
            rows.append(row)
            print(json.dumps(row), flush=True)
    return rows


def summarize(rows):
    canon = [r for r in rows if r["event"] and r["event"]["canonical"]]
    for r in canon:
        res = fet.rpc("getTransactionsForAddress", [r["event"]["pool"], {
            "transactionDetails": "signatures", "sortOrder": "asc", "limit": 1}]) or {}
        first = (res.get("data") or [{}])[0].get("blockTime")
        r["recv_minus_block_s"] = (r["recv"] - first) if first else None
    lags = sorted(r["recv_minus_block_s"] for r in canon if r.get("recv_minus_block_s") is not None)
    pct = lambda xs, q: xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None  # noqa: E731
    print(json.dumps({
        "frames": len(rows), "failed": sum(r["err"] for r in rows),
        "create_pool_events": sum(1 for r in rows if r["event"]), "canonical_wsol": len(canon),
        "bytes_total": sum(r["bytes"] for r in rows),
        "parse_us_p50": statistics.median([r["parse_us"] for r in rows]) if rows else None,
        "readable_at_receipt": sum(1 for r in canon if r.get("readable")),
        "read_ms_p50": statistics.median([r["read_ms"] for r in canon]) if canon else None,
        "recv_minus_blocktime_s_p50_p90": (pct(lags, 0.5), pct(lags, 0.9)),
        "research_429s": fet.RATE_429[0]}))


def selftest():
    frame = json.loads(FRAMES.read_text())["wsol"]
    ev = create_pool_event(frame["logs"])
    assert ev == {"index": 0, "creator": ev["creator"], "base_mint":
                  "6UTgBWACzB1zX2GA2uEvVn6325XE99wNvkVTXv5qpump", "quote_mint": WSOL,
                  "pool": "qNadZjDebjUwHS5ZRcLTLtoGk4wAU15kydQLFSJGUUe", "canonical": True}, ev
    assert create_pool_event([l for l in frame["logs"] if not l.startswith("Program data:")]) is None
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        sys.exit(0)
    if open_real_solana():
        sys.exit("a real Solana position is open: no research read now")
    out = asyncio.run(run(a.seconds))
    if open_real_solana():
        print("NOTE: a real Solana position opened during the probe", flush=True)
    summarize(out)
