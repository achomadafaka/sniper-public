#!/usr/bin/env python3
"""Read-only: how late the swap-mark lane hears a vault change, at `processed` versus `confirmed`.

The funded swap-mark lane subscribes each held pool's vaults with `accountSubscribe` at
`confirmed` on the read RPC's WebSocket (MEMECOIN_SOLANA_READ_RPC_URL, keyless). This opens ONE
socket to that same endpoint for `--duration` seconds and subscribes:
  - `slotSubscribe`, whose first notification for a slot is the node's clock for that slot;
  - each vault twice, once at `processed` and once at `confirmed`.
For every (vault, slot) change it prints the receipt lag behind the slot clock at each
commitment, and confirmed-minus-processed where both arrived. It never touches the custody RPC,
the on-chain watcher's endpoint, or any keyed URL, and it prints hosts only.

    python3 scripts/research/swap_mark_notify_lag.py --duration 180
    python3 scripts/research/swap_mark_notify_lag.py --selftest
"""
import argparse
import asyncio
import json
import os
import sqlite3
import statistics
import sys
import time
from urllib.parse import urlsplit

REPO = "/home/bigpapi/Desktop/Sniper"
TAPE = os.path.join(REPO, "data", "memecoin_sol_tape.db")


def busy_vaults(limit, window_min):
    """Vaults of the Solana pools with the most tape swaps in the tape's newest `window_min`."""
    t = sqlite3.connect("file:%s?mode=ro" % TAPE, uri=True, timeout=5)
    try:
        rows = t.execute(
            "SELECT v.coin_vault, v.quote_vault FROM pool_ticks p JOIN sol_pool_vaults v "
            "ON v.pool = p.pool WHERE p.chain_id = 'solana' AND p.observed_at >= "
            "(SELECT strftime('%Y-%m-%dT%H:%M:%S', MAX(observed_at), ?) FROM pool_ticks) "
            "GROUP BY p.pool ORDER BY COUNT(*) DESC LIMIT ?",
            ("-%d minutes" % window_min, limit)).fetchall()
    finally:
        t.close()
    return [v for r in rows for v in r if v]


def summarize(slot_first, seen):
    """seen: {(vault, slot, commitment): receipt_ts}. Returns {name: [lags_ms]}."""
    out = {"processed": [], "confirmed": [], "confirmed_minus_processed": []}
    for (vault, slot, commitment), ts in seen.items():
        if slot in slot_first:
            out[commitment].append((ts - slot_first[slot]) * 1000.0)
        if commitment == "confirmed" and (vault, slot, "processed") in seen:
            out["confirmed_minus_processed"].append(
                (ts - seen[(vault, slot, "processed")]) * 1000.0)
    return out


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


async def run(ws_url, vaults, duration):
    import websockets
    slot_first, seen, req = {}, {}, {}
    async with websockets.connect(ws_url, max_size=2 ** 22) as ws:
        rid = 1
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "slotSubscribe"}))
        req[rid] = ("slot", None)
        for v in vaults:
            for commitment in ("processed", "confirmed"):
                rid += 1
                req[rid] = (v, commitment)
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": "accountSubscribe",
                                          "params": [v, {"encoding": "base64",
                                                         "commitment": commitment}]}))
        subs = {}
        deadline = time.time() + duration
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.time()))
            except asyncio.TimeoutError:
                break
            now = time.time()
            msg = json.loads(raw)
            if "id" in msg and "result" in msg:
                subs[msg["result"]] = req.get(msg["id"])
                continue
            params = msg.get("params") or {}
            what = subs.get(params.get("subscription"))
            if what is None:
                continue
            result = params.get("result") or {}
            if what[0] == "slot":
                slot_first.setdefault(int(result.get("slot")), now)
            else:
                slot = int((result.get("context") or {}).get("slot"))
                seen.setdefault((what[0], slot, what[1]), now)
    return slot_first, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=180.0)
    ap.add_argument("--pools", type=int, default=6)
    ap.add_argument("--window-min", type=int, default=360)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    sys.path.insert(0, REPO)
    from sniper.services.memecoin_solana_swap_marks import ws_url_from_read_rpc
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    load_env_files(str(state_root()))
    read = os.environ.get("MEMECOIN_SOLANA_READ_RPC_URL", "")
    forbidden = {urlsplit(os.environ.get(k, "")).netloc for k in
                 ("MEMECOIN_SOLANA_RPC_URL", "ONCHAIN_WATCH_HELIUS_RPC_URL")} - {""}
    host = urlsplit(read).netloc
    if not host or host in forbidden or urlsplit(read).query:
        raise SystemExit("read RPC missing, keyed, or shared with custody/watcher: refusing")
    vaults = busy_vaults(args.pools, args.window_min)
    print("endpoint=%s vaults=%d duration=%.0fs" % (host, len(vaults), args.duration))
    slot_first, seen = asyncio.run(run(ws_url_from_read_rpc(read), vaults, args.duration))
    for name, vals in summarize(slot_first, seen).items():
        print("%s n=%d p50=%s p90=%s ms" % (name, len(vals), pct(vals, 0.5), pct(vals, 0.9)))


def selftest():
    slots = {100: 10.0, 101: 10.4}
    seen = {("A", 100, "processed"): 10.5, ("A", 100, "confirmed"): 11.1,
            ("A", 101, "confirmed"): 11.5, ("B", 99, "processed"): 9.9}
    got = summarize(slots, seen)
    assert [round(x) for x in got["processed"]] == [500]
    assert sorted(round(x) for x in got["confirmed"]) == [1100, 1100]
    assert [round(x) for x in got["confirmed_minus_processed"]] == [600]
    assert pct([], 0.5) is None and pct([1, 2, 3, 4], 0.9) == 4
    print("selftest ok")


if __name__ == "__main__":
    main()
