#!/usr/bin/env python3
"""Phase 0, step 1 of the lane's transaction stream (design section 13): capture RAW Meteora DLMM and
DAMM v2 buy frames from Helius transactionSubscribe, and count the load under the tight filter.

One socket on the research key (the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry, atlas host: the
mainnet host accepted the subscription and delivered nothing on 09-27). ONE subscription per armed
owner: {accountRequired: [owner], accountInclude: [DLMM, DAMM v2], failed: false}, so only that
owner's DLMM or DAMM v2 transactions arrive. Read-only; nothing is sent or signed.

Every frame is counted (per owner, per venue). The first --keep frames per venue in which the owner's
balance of a non-quote mint ROSE are written whole to --out (JSONL) for the decode check: does
meta.innerInstructions carry the program's own swap event? Stops when both venues have --keep
frames, or at --seconds, or when a real Solana position opens (checked at start and each idle
minute; the research key is flat-book only).

    python3 scripts/research/tx_stream_venue_capture.py --seconds 3600 --keep 3 --out <path>
"""
import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "scripts" / "research")]
STATE_ROOT = "/home/bigpapi/Desktop/Sniper"      # .env and the ledger live in the main checkout

DLMM = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
DAMM_V2 = "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG"
VENUE_OF = {DLMM: "dlmm", DAMM_V2: "damm_v2"}


def solana_book_open():
    """True while a real Solana position is open: the research key is used only on a flat book."""
    import sqlite3
    conn = sqlite3.connect("file:%s/sniper_tracker.db?mode=ro" % STATE_ROOT, uri=True, timeout=5)
    try:
        return conn.execute("SELECT 1 FROM memecoin_positions WHERE execution_mode='real' "
                            "AND network_id='solana' AND closed_at IS NULL AND COALESCE(tracking_paused_at,'')='' LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def venues_in(tx):
    """The venues whose program the transaction touches (top level or inner)."""
    msg = tx["transaction"]["message"]
    meta = tx.get("meta") or {}
    loaded = meta.get("loadedAddresses") or {}
    keys = msg["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
    return sorted({VENUE_OF[k] for k in keys if k in VENUE_OF})


async def run(seconds, keep, out_path):
    import websockets
    import slot_lane_send_monitor as mon
    import tx_stream_probe
    from tx_stream_probe import armed_owners, owner_buys
    tx_stream_probe.LEDGER = "file:%s/sniper_tracker.db?mode=ro" % STATE_ROOT
    from sniper.util.clock import real_utc_now_iso
    key = re.search(r"api-key=([^&]+)", mon.rpc_url()).group(1)
    owners = armed_owners()
    end = time.monotonic() + seconds
    kept = {"dlmm": 0, "damm_v2": 0}
    stats = {"frames": 0, "by_venue": {}, "by_owner": {}, "subs_ok": 0, "subs_err": 0,
             "reconnects": 0, "started": real_utc_now_iso(), "owners": len(owners)}
    if solana_book_open():
        print(json.dumps({"stopped": "solana_position_open_at_start"}))
        return
    with open(out_path, "a") as out:
        while time.monotonic() < end and min(kept.values()) < keep:
            try:
                async with websockets.connect("wss://atlas-mainnet.helius-rpc.com/?api-key=%s" % key,
                                              open_timeout=15, ping_interval=20, max_size=None) as ws:
                    for i, owner in enumerate(sorted(owners), 1):
                        await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "transactionSubscribe",
                                                  "params": [{"accountInclude": [DLMM, DAMM_V2],
                                                              "accountRequired": [owner], "failed": False},
                                                             {"commitment": "processed", "encoding": "json",
                                                              "transactionDetails": "full",
                                                              "maxSupportedTransactionVersion": 1}]}))
                    while time.monotonic() < end and min(kept.values()) < keep:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(60.0, max(1.0, end - time.monotonic())))
                        except asyncio.TimeoutError:
                            if solana_book_open():
                                stats["stopped"] = "solana_position_open"
                                end = time.monotonic()
                            continue
                        msg = json.loads(raw)
                        if "id" in msg:
                            stats["subs_err" if "error" in msg else "subs_ok"] += 1
                            if "error" in msg:
                                out.write(json.dumps({"sub_error": msg["error"], "id": msg["id"]}) + "\n")
                            continue
                        if msg.get("method") != "transactionNotification":
                            continue
                        res = msg["params"]["result"]
                        tx = res["transaction"]
                        stats["frames"] += 1
                        venues = venues_in(tx)
                        for v in venues:
                            stats["by_venue"][v] = stats["by_venue"].get(v, 0) + 1
                        meta = tx.get("meta") or {}
                        buys = owner_buys(meta, owners)
                        for owner, _mint in buys:
                            stats["by_owner"][owners[owner]] = stats["by_owner"].get(owners[owner], 0) + 1
                        for v in venues:
                            if buys and kept[v] < keep:
                                kept[v] += 1
                                out.write(json.dumps({"venue": v, "recv_utc": real_utc_now_iso(),
                                                      "buys": buys, "frame": res}) + "\n")
                                out.flush()
            except asyncio.TimeoutError:
                break
            except Exception as exc:  # noqa: BLE001 - a dropped socket is a gap: logged, then reconnect
                stats["reconnects"] += 1
                out.write(json.dumps({"gap": real_utc_now_iso(), "error": "%s: %s" % (type(exc).__name__, str(exc)[:160])}) + "\n")
                out.flush()
                await asyncio.sleep(2.0)
        stats.update(ended=real_utc_now_iso(), kept=kept)
        out.write(json.dumps({"stats": stats}) + "\n")
    print(json.dumps(stats))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=3600)
    ap.add_argument("--keep", type=int, default=3)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(Path(STATE_ROOT) / ".env")
    asyncio.run(run(args.seconds, args.keep, args.out))


if __name__ == "__main__":
    main()
