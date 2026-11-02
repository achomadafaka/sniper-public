#!/usr/bin/env python3
"""How early does Helius transactionSubscribe show an armed wallet's buy, against J7 and our watcher?

Operator order 2026-09-27 ~09:50Z ("run the latency probe"). The slot lane reads only pump events
whose actor is the owner (44% of armed wallets' buys); the rest reach us via the confirmed watcher
(p50 1.36 s) or J7. transactionSubscribe (accountInclude = every armed verified owner, processed,
full details) carries the owner's token-balance deltas in the frame, so any venue can be read.

  run      one socket: transactionSubscribe + slotSubscribe. Per owner BUY (owner's balance of a
           non-quote mint rose): signature, slot, recv time on the ledger clock (real_utc_now_iso),
           ms since this socket first saw the slot, and whether the slot lane's decoder reads it.
  report   join by signature to J7 (memecoin_events.received_at) and the watcher
           (onchain_wallet_detections.detected_at): lead of the stream over each, p50/p90, n.

Research key only (the first MEMECOIN_RESEARCH_HELIUS_RPC_URLS entry), read-only.

    python3 scripts/research/tx_stream_probe.py run --seconds 2700
    python3 scripts/research/tx_stream_probe.py report
    python3 scripts/research/tx_stream_probe.py --selftest
"""
import argparse
import asyncio
import contextlib
import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "research"))
OUT = REPO / "logs" / "tx_stream_probe_20260927.jsonl"
LEDGER = f"file:{REPO / 'sniper_tracker.db'}?mode=ro"
QUOTES = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"}


def owner_paid(meta, keys, owner):
    """The owner PAID in this transaction: its quote-token balance fell, or its lamports fell by more
    than the fee it signed for plus the rent of token accounts the tx created for it (> 0.001 SOL).
    A bare lamport drop is not a buy: an owner-signed claim pays fee + 2,039,280 rent per new token
    account and gets a coin for nothing (Lambda, 2026-09-27). A handout with no drop at all is not a
    buy either (the 09-27 probe counted 16 PUMP-token drops and transfers).

    Quote spend is netted PER MINT: USDC and wSOL atomic units have different decimals, so a
    cross-mint sum let 1,000,000 USDC spent be cancelled by 2,000,000 wSOL received (Codex DR-07).
    Netting within a mint still cancels a transfer between two of the owner's own accounts."""
    spent = {}
    for side, sign in (("preTokenBalances", 1), ("postTokenBalances", -1)):
        for b in meta.get(side) or []:
            if b.get("owner") == owner and b.get("mint") in QUOTES:
                spent[b["mint"]] = spent.get(b["mint"], 0) + sign * int(b["uiTokenAmount"]["amount"])
    if any(v > 0 for v in spent.values()):
        return True
    if owner not in keys:
        return False
    i = keys.index(owner)
    drop = meta["preBalances"][i] - meta["postBalances"][i]
    if keys and keys[0] == owner:
        drop -= int(meta.get("fee") or 0)
    pre_idx = {b.get("accountIndex") for b in meta.get("preTokenBalances") or []}
    drop -= sum(meta["postBalances"][b["accountIndex"]] for b in meta.get("postTokenBalances") or []
                if b.get("owner") == owner and b.get("accountIndex") not in pre_idx
                and b["accountIndex"] < len(meta["postBalances"]))
    return drop > 1_000_000


def owner_buys(meta, owners, keys=None):
    """[(owner, mint)] whose owner-held balance of a non-quote mint rose in a transaction where the
    owner paid (owner_paid); without `keys` the payment check is skipped (selftest only)."""
    delta = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for b in meta.get(side) or []:
            o = b.get("owner")
            if o in owners and b.get("mint") not in QUOTES:
                key = (o, b["mint"])
                delta[key] = delta.get(key, 0) + sign * int(b["uiTokenAmount"]["amount"])
    return [k for k, d in delta.items() if d > 0 and (keys is None or owner_paid(meta, keys, k[0]))]


def lane_reads(sig, slot, logs, owner):
    from sniper.services.memecoin_slot_signal import _owner_bytes, decode_owner_buy
    frame = {"params": {"result": {"context": {"slot": slot}, "value": {"signature": sig, "err": None,
                                                                         "logs": logs}}}}
    try:
        hit = decode_owner_buy(frame, owner, _owner_bytes(owner), "uid", recv_monotonic=0.0,
                               recv_utc="2026-01-01T00:00:00+00:00")
    except Exception as exc:  # noqa: BLE001 - a decoder raise is a layout change: record it
        return "raised:%s" % type(exc).__name__
    return "decoded" if hit and not getattr(hit, "refusal", None) else ("refused" if hit else "unread")


def armed_owners():
    from sniper.services.memecoin_address_registry import armed_verified_owners
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        return {a: h for a, (uid, h) in (armed_verified_owners(c) or {}).items()}


async def run(seconds):
    import websockets
    import slot_lane_send_monitor as mon
    from sniper.util.clock import real_utc_now_iso
    url = mon.rpc_url()
    key = re.search(r"api-key=([^&]+)", url).group(1)
    ws_url = "wss://atlas-mainnet.helius-rpc.com/?api-key=%s" % key
    owners = armed_owners()
    end = time.monotonic() + seconds
    slot_seen, stats = {}, {"frames": 0, "buys": 0, "reconnects": 0}
    with open(OUT, "a") as out:
        out.write(json.dumps({"start": real_utc_now_iso(), "owners": len(owners),
                              "owner_set": sorted(owners)}) + "\n")
        while time.monotonic() < end:
            try:
                async with websockets.connect(ws_url, open_timeout=15, ping_interval=20, max_size=None) as ws:
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "transactionSubscribe", "params": [
                        {"accountInclude": sorted(owners), "failed": False},
                        {"commitment": "processed", "encoding": "json", "transactionDetails": "full",
                         "maxSupportedTransactionVersion": 1}]}))
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "slotSubscribe"}))
                    while time.monotonic() < end:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, end - time.monotonic()))
                        mono = time.monotonic()
                        msg = json.loads(raw)
                        method = msg.get("method")
                        if "id" in msg:
                            # The subscription reply: without it an empty stream cannot be told apart
                            # from a rejected subscription (DR-07).
                            out.write(json.dumps({"sub_reply": msg["id"], "at": real_utc_now_iso(),
                                                  **({"error": msg["error"]} if "error" in msg
                                                     else {"result": msg.get("result")})}) + "\n")
                            out.flush()
                            continue
                        if method == "slotNotification":
                            s = msg["params"]["result"]["slot"]
                            slot_seen.setdefault(s, mono)
                            if len(slot_seen) > 5000:
                                for old in sorted(slot_seen)[:1000]:
                                    slot_seen.pop(old, None)
                            continue
                        if method != "transactionNotification":
                            continue
                        stats["frames"] += 1
                        res = msg["params"]["result"]
                        tx = res["transaction"]
                        meta = tx.get("meta") or {}
                        msg_keys = tx["transaction"]["message"]["accountKeys"]
                        loaded = meta.get("loadedAddresses") or {}
                        buys = owner_buys(meta, owners, msg_keys + loaded.get("writable", []) + loaded.get("readonly", []))
                        if not buys:
                            continue
                        recv_utc = real_utc_now_iso()
                        slot = res.get("slot")
                        sig = res.get("signature") or tx["transaction"]["signatures"][0]
                        for owner, mint in buys:
                            stats["buys"] += 1
                            out.write(json.dumps({
                                "sig": sig, "slot": slot, "recv_utc": recv_utc, "owner": owner,
                                "handle": owners[owner], "mint": mint,
                                "ms_after_slot_seen": (round((mono - slot_seen[slot]) * 1000, 1)
                                                       if slot in slot_seen else None),
                                "lane": lane_reads(sig, slot, meta.get("logMessages") or [], owner)}) + "\n")
                            out.flush()
            except asyncio.TimeoutError:
                break
            except Exception as exc:  # noqa: BLE001 - a dropped socket is a gap, logged, then reconnect
                stats["reconnects"] += 1
                out.write(json.dumps({"gap": real_utc_now_iso(), "error": "%s: %s" % (type(exc).__name__,
                                                                                     str(exc)[:120])}) + "\n")
                out.flush()
                await asyncio.sleep(1.0)
        out.write(json.dumps({"end": real_utc_now_iso(), **stats}) + "\n")
    print(json.dumps(stats))


async def h2h(seconds, host="atlas-mainnet.helius-rpc.com", skip=("notdecu",)):
    # mainnet.helius-rpc.com ACCEPTED transactionSubscribe but delivered 0 frames in 30 s (10:40Z)
    # while atlas delivered; delivery there is unverified, so both feeds share the atlas socket.
    """ONE socket, two feeds: transactionSubscribe (accountInclude=owners) and one logsSubscribe
    (mentions=[owner]) per owner. Per paid buy: tx-frame arrival minus the first logs frame for the
    same signature, in ms (negative: the transaction stream came first)."""
    import websockets
    import slot_lane_send_monitor as mon
    from sniper.util.clock import real_utc_now_iso
    key = re.search(r"api-key=([^&]+)", mon.rpc_url()).group(1)
    owners = {a: h for a, h in armed_owners().items() if h not in skip}
    logs_at, tx_at, rows = {}, {}, []
    end = time.monotonic() + seconds
    out_path = REPO / "logs" / "tx_stream_h2h_20260927.jsonl"
    async with websockets.connect("wss://%s/?api-key=%s" % (host, key), open_timeout=15, ping_interval=20,
                                  max_size=None) as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "transactionSubscribe", "params": [
            {"accountInclude": sorted(owners), "failed": False},
            {"commitment": "processed", "encoding": "json", "transactionDetails": "full",
             "maxSupportedTransactionVersion": 1}]}))
        for i, owner in enumerate(sorted(owners), 1):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                                      "params": [{"mentions": [owner]}, {"commitment": "processed"}]}))
        frames = {"tx": 0, "logs": 0, "sub_ok": 0, "sub_errors": []}
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
            mono = time.monotonic()
            msg = json.loads(raw)
            if "id" in msg:                       # subscription reply (DR-07): count, keep errors
                if "error" in msg:
                    frames["sub_errors"].append({"id": msg["id"], "error": msg["error"]})
                else:
                    frames["sub_ok"] += 1
            elif msg.get("method") == "logsNotification":
                frames["logs"] += 1
                v = msg["params"]["result"]["value"]
                if v.get("err") is None:
                    logs_at.setdefault(v["signature"], mono)
            elif msg.get("method") == "transactionNotification":
                frames["tx"] += 1
                res = msg["params"]["result"]
                tx = res["transaction"]
                meta = tx.get("meta") or {}
                loaded = meta.get("loadedAddresses") or {}
                keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
                buys = owner_buys(meta, owners, keys)
                if buys:
                    sig = res.get("signature") or tx["transaction"]["signatures"][0]
                    tx_at.setdefault(sig, (mono, buys[0][0]))
    for sig, (t_tx, owner) in tx_at.items():
        t_logs = logs_at.get(sig)
        rows.append({"sig": sig, "handle": owners[owner],
                     "tx_minus_logs_ms": round((t_tx - t_logs) * 1000, 1) if t_logs else None})
    with open(out_path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    d = sorted(r["tx_minus_logs_ms"] for r in rows if r["tx_minus_logs_ms"] is not None)
    q = lambda p: d[int(p * (len(d) - 1))] if d else None
    # The cohort is part of the result: notdecu is skipped by default (19,528 logs frames/h alone).
    print(json.dumps({"at": real_utc_now_iso(), "owners": len(owners), "skipped_handles": sorted(skip),
                      "frames": frames, "paid_buys": len(rows),
                      "with_logs_frame": len(d), "tx_minus_logs_ms": {"p10": q(.1), "p50": q(.5), "p90": q(.9)}}))


def _ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def report():
    import dump_forensics as dfx
    rows = [json.loads(l) for l in OUT.read_text().splitlines() if l.strip()]
    buys, not_paid, unreadable = [], 0, 0
    for r in (r for r in rows if "sig" in r):      # rows logged before owner_paid existed: re-check
        tx = dfx.get_tx(r["sig"])
        if not tx:                                 # result=null is UNKNOWN, never a handout (DR-07)
            unreadable += 1
            continue
        loaded = tx["meta"].get("loadedAddresses") or {}
        keys = tx["transaction"]["message"]["accountKeys"] + loaded.get("writable", []) + loaded.get("readonly", [])
        if owner_paid(tx["meta"], keys, r["owner"]):
            buys.append(r)
        else:
            not_paid += 1                          # handout, transfer, or a buy a relayer paid for
    with contextlib.closing(sqlite3.connect(LEDGER, uri=True, timeout=30)) as c:
        j7, watch = {}, {}
        for r in buys:
            lo = "swap:raybot:%s:" % r["sig"]
            hit = c.execute("SELECT received_at FROM memecoin_events WHERE fomo_event_id >= ? AND fomo_event_id < ? "
                            "LIMIT 1", (lo, lo[:-1] + ";")).fetchone()
            if hit and hit[0]:
                j7[r["sig"]] = _ts(hit[0])
            hit = c.execute("SELECT detected_at FROM onchain_wallet_detections WHERE signature=?", (r["sig"],)).fetchone()
            if hit and hit[0]:
                watch[r["sig"]] = _ts(hit[0])

    def q(v, p):
        v = sorted(v)
        return round(v[int(p * (len(v) - 1))], 3) if v else None
    lead_j7 = [j7[r["sig"]] - _ts(r["recv_utc"]) for r in buys if r["sig"] in j7]
    lead_w = [watch[r["sig"]] - _ts(r["recv_utc"]) for r in buys if r["sig"] in watch]
    after_slot = [r["ms_after_slot_seen"] for r in buys if r.get("ms_after_slot_seen") is not None]
    lanes = {}
    for r in buys:
        lanes[r["lane"]] = lanes.get(r["lane"], 0) + 1
    print(json.dumps({"buys": len(buys), "owner_not_paid_excluded": not_paid, "receipt_unreadable": unreadable,
                      "sub_errors": [r for r in rows if "error" in r and "sub_reply" in r],
                      "gaps": sum(1 for r in rows if "gap" in r), "lane": lanes,
                      "stream_lead_over_j7_s": {"n": len(lead_j7), "p10": q(lead_j7, .1), "p50": q(lead_j7, .5),
                                                "p90": q(lead_j7, .9)},
                      "stream_lead_over_watcher_s": {"n": len(lead_w), "p10": q(lead_w, .1), "p50": q(lead_w, .5),
                                                     "p90": q(lead_w, .9)},
                      "ms_after_slot_seen": {"n": len(after_slot), "p50": q(after_slot, .5), "p90": q(after_slot, .9)},
                      "j7_saw_share": round(len(lead_j7) / len(buys), 2) if buys else None}, indent=1))


def selftest():
    meta = {"preTokenBalances": [{"owner": "O", "mint": "M", "uiTokenAmount": {"amount": "0"}},
                                 {"owner": "O", "mint": "So11111111111111111111111111111111111111112",
                                  "uiTokenAmount": {"amount": "9"}}],
            "postTokenBalances": [{"owner": "O", "mint": "M", "uiTokenAmount": {"amount": "5"}},
                                  {"owner": "O", "mint": "So11111111111111111111111111111111111111112",
                                   "uiTokenAmount": {"amount": "1"}},
                                  {"owner": "X", "mint": "N", "uiTokenAmount": {"amount": "7"}}]}
    assert owner_buys(meta, {"O"}) == [("O", "M")], owner_buys(meta, {"O"})   # wSOL spend is not a buy
    assert owner_buys(meta, {"X"}) == [("X", "N")] and owner_buys(meta, {"Z"}) == []
    assert lane_reads("S", 1, [], "11111111111111111111111111111111") == "unread"
    free = {"fee": 5000, "preBalances": [5_000_000, 0], "postBalances": [5_000_000, 2_039_280],
            "preTokenBalances": [],
            "postTokenBalances": [{"accountIndex": 1, "owner": "O", "mint": "M", "uiTokenAmount": {"amount": "5"}}]}
    assert owner_buys(free, {"O"}, ["O", "ata"]) == []                              # a handout
    claim = {**free, "preBalances": [5_000_000, 0], "postBalances": [5_000_000 - 5000 - 2_039_280, 2_039_280]}
    assert owner_buys(claim, {"O"}, ["O", "ata"]) == []                             # a signed claim
    paid = {**free, "preBalances": [500_000_000, 0], "postBalances": [300_000_000, 2_039_280]}
    assert owner_buys(paid, {"O"}, ["O", "ata"]) == [("O", "M")]                    # paid 0.2 SOL
    usdc, wsol = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "So11111111111111111111111111111111111111112"
    bal = lambda mint, n, i: {"owner": "O", "mint": mint, "accountIndex": i, "uiTokenAmount": {"amount": str(n)}}  # noqa: E731
    flat = {"fee": 0, "preBalances": [100, 10, 10], "postBalances": [100, 10, 10]}
    mixed = {**flat, "preTokenBalances": [bal(usdc, 1_000_000, 1), bal(wsol, 0, 2)],
             "postTokenBalances": [bal(usdc, 0, 1), bal(wsol, 2_000_000, 2)]}
    assert owner_paid(mixed, ["O", "a", "b"], "O")          # USDC spent is not cancelled by wSOL received
    moved = {**flat, "preTokenBalances": [bal(usdc, 5, 1), bal(usdc, 0, 2)],
             "postTokenBalances": [bal(usdc, 0, 1), bal(usdc, 5, 2)]}
    assert not owner_paid(moved, ["O", "a", "b"], "O")      # USDC moved between two own accounts
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=("run", "report", "h2h"))
    ap.add_argument("--seconds", type=int, default=2700)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
    elif a.cmd == "run":
        asyncio.run(run(a.seconds))
    elif a.cmd == "report":
        report()
    elif a.cmd == "h2h":
        asyncio.run(h2h(a.seconds))
