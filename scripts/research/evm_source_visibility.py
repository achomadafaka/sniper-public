#!/usr/bin/env python3
"""Read-only BSC pending / Base preconf / Robinhood sequencer visibility benchmark.

One process, monotonic arrival clock, exact transaction hashes. General V2/V3 swaps
are a transport sample; only ledger-matched watched buys are a source-buy sample.
No signing, submission, retry, funded adapter, or provider-key use.

python3 scripts/research/evm_source_visibility.py --state-root /path/to/Sniper --seconds 180
python3 -m pytest -q -n 0 tests/test_evm_source_visibility.py
"""
import argparse
import asyncio
import base64
import json
import math
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
HASH = re.compile(r"0x[0-9a-fA-F]{64}\Z")


def stats(values):
    v = sorted(values)
    return {"n": len(v), "p50": v[len(v)//2] if v else None,
            "p90": v[min(len(v)-1, int(len(v)*.9))] if v else None,
            "min": v[0] if v else None, "max": v[-1] if v else None}


def paired(early, sealed, lo, hi):
    eligible = {h: t for h, t in sealed.items() if lo <= t <= hi}
    leads = [(t-early[h])*1000 for h, t in eligible.items() if h in early]
    return {"eligible_sealed": len(eligible), "matched": len(leads),
            "seen_before_sealed": sum(t > 0 for t in leads),
            "missing": len(eligible)-len(leads), "lead_ms": stats(leads)}


def raw_transactions(buf, depth=0):
    # shortcut: Nitro signed-tx/batch kinds only; add other kinds if observed in this feed.
    if depth > 8:
        raise ValueError("feed nesting limit")
    if not buf:
        return
    if buf[0] == 4:
        if len(buf) < 2:
            raise ValueError("empty signed transaction")
        yield buf[1:]
    elif buf[0] == 3:
        i = 1
        while i < len(buf):
            if len(buf)-i < 8:
                raise ValueError("truncated batch length")
            n = int.from_bytes(buf[i:i+8], "big")
            i += 8
            if not n or n > len(buf)-i:
                raise ValueError("truncated batch message")
            yield from raw_transactions(buf[i:i+n], depth+1)
            i += n


def feed_hashes(frame):
    from eth_hash.auto import keccak
    out = []
    for m in frame.get("messages") or []:
        body = m["message"]["message"].get("l2Msg")
        if body is None:
            raise ValueError("feed missing l2Msg")
        for raw in raw_transactions(base64.b64decode(body, validate=True)):
            out.append("0x" + keccak(raw).hex())
    return out


async def listen(name, url, chain, filters, seconds, result, sequencer=False):
    import websockets
    d = result[name] = {"chain": chain, "start": None, "end": None,
                        "errors": [], "frames": 0, "events": {}, "removed": [],
                        "idle_gaps": [], "parse_ms": []}
    start = time.monotonic()
    subscriptions, removed = {}, set()
    reason = None

    def ingest(frame, at):
        if sequencer:
            hashes, kind = feed_hashes(frame), "feed"
        else:
            params = frame.get("params") or {}
            kind = subscriptions.get(params.get("subscription"))
            payload = params.get("result")
            if kind is None:
                return
            h = payload if isinstance(payload, str) else payload.get("transactionHash")
            if kind == "heads":
                h = payload.get("hash")
            if not isinstance(h, str) or not HASH.fullmatch(h):
                raise ValueError("invalid event hash")
            h = h.lower()
            if isinstance(payload, dict) and payload.get("removed"):
                removed.add(h)
                d["removed"].append(h)
                return
            hashes = [h]
        for h in hashes:
            d["events"].setdefault(kind, {}).setdefault(h, at)

    try:
        async with websockets.connect(url, open_timeout=5, close_timeout=1,
                                      max_size=2**24, max_queue=16) as ws:
            if not sequencer:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": 0,
                                          "method": "eth_chainId", "params": []}))
                reply = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if reply.get("error") or reply.get("result") is None:
                    reason = "identity_refused"
                    d["rpc_code"] = (reply.get("error") or {}).get("code")
                    raise ValueError(reason)
                if int(reply["result"], 16) != int(chain):
                    reason = "wrong_chain"
                    raise ValueError(reason)
                for n, (kind, flt) in enumerate(filters, 1):
                    await ws.send(json.dumps({"jsonrpc": "2.0", "id": n,
                                              "method": "eth_subscribe", "params": flt}))
                    deadline = time.monotonic()+5
                    while True:
                        frame = json.loads(await asyncio.wait_for(ws.recv(),
                                                max(.001, deadline-time.monotonic())))
                        at = time.monotonic()
                        if frame.get("id") == n:
                            if frame.get("error") or not frame.get("result"):
                                reason = "subscription_refused"
                                d["refused_kind"] = kind
                                d["rpc_code"] = (frame.get("error") or {}).get("code")
                                raise ValueError(reason)
                            subscriptions[frame["result"]] = kind
                            break
                        ingest(frame, at)
            d["start"] = time.monotonic()
            d["setup_ms"] = (d["start"]-start)*1000
            print(name, "ready", flush=True)
            deadline = d["start"]+seconds
            last_receive = d["start"]
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), min(5, deadline-time.monotonic()))
                except asyncio.TimeoutError:
                    if time.monotonic() < deadline:
                        d["idle_gaps"].append({"since": last_receive, "until": time.monotonic()})
                    continue
                at = time.monotonic()
                last_receive = at
                d["frames"] += 1
                if d["frames"] > 200000:
                    raise ValueError("frame limit")
                frame = json.loads(raw)
                if frame.get("error"):
                    reason = "rpc_error"
                    d["rpc_code"] = frame["error"].get("code")
                    raise ValueError(reason)
                ingest(frame, at)
                d["parse_ms"].append((time.monotonic()-at)*1000)
    except Exception as exc:
        # Never serialize exception text: it can contain endpoint credentials.
        d["errors"].append(reason or type(exc).__name__)
        print(name, "stopped:", type(exc).__name__, flush=True)
    finally:
        d["end"] = time.monotonic()
        d["parse_ms"] = stats(d["parse_ms"])
        for events in d["events"].values():
            for h in removed:
                events.pop(h, None)


async def capture(seconds, rosters):
    from eth_hash.auto import keccak
    from sniper.services.memecoin_onchain_push import armed_transfer_filter
    swaps = {"topics": [["0x" + keccak(s.encode()).hex() for s in (
        "Swap(address,uint256,uint256,uint256,uint256,address)",
        "Swap(address,address,int256,int256,uint160,uint128,int24)")]]}
    result = {}
    jobs = []
    for name, chain, url, log_kind in (
        ("bsc_public", "56", "wss://bsc-rpc.publicnode.com", "logs"),
        ("base_preconf", "8453", "wss://mainnet-preconf.base.org", "pendingLogs"),
        ("base_sealed", "8453", "wss://base-rpc.publicnode.com", "logs"),
        ("rh_sealed", "4663", "wss://robinhood-rpc.publicnode.com", "logs")):
        filters = [("swap", [log_kind, swaps]), ("heads", ["newHeads"])]
        watched = armed_transfer_filter(rosters[chain])
        if watched:
            filters.append(("watched", [log_kind, watched[1]]))
        if chain == "56":
            filters.append(("pending", ["newPendingTransactions"]))
        jobs.append(listen(name, url, chain, filters, seconds, result))
    jobs.append(listen("rh_feed", "wss://feed.mainnet.chain.robinhood.com", "4663",
                       [], seconds, result, sequencer=True))
    await asyncio.gather(*jobs)
    return result


def comparisons(result):
    out = {}
    for name, early_name, early_kind, sealed_name in (
        ("bsc_pending", "bsc_public", "pending", "bsc_public"),
        ("base_preconf", "base_preconf", "swap", "base_sealed"),
        ("rh_feed", "rh_feed", "feed", "rh_sealed")):
        a, b = result[early_name], result[sealed_name]
        lo = max(a["start"] if a["start"] is not None else a["end"],
                 b["start"] if b["start"] is not None else b["end"])+5
        hi = min(a["end"], b["end"])-5
        retracted = set(a.get("removed", ())) | set(b.get("removed", ()))
        early = {h: t for h, t in a["events"].get(early_kind, {}).items() if h not in retracted}
        out[name] = {"complete": not (a["errors"] or b["errors"] or
                                      a.get("idle_gaps") or b.get("idle_gaps")),
                     "overlap_s": max(0, hi-lo),
                     "retracted_censored": len(retracted),
                     "v2_v3_swap_transactions": paired(early,
                         {h: t for h, t in b["events"].get("swap", {}).items() if h not in retracted}, lo, hi),
                     "watched_transfers": paired(
                         {h: t for h, t in a["events"].get("watched", {}).items()
                          if h not in retracted} if name == "base_preconf" else early,
                         {h: t for h, t in b["events"].get("watched", {}).items()
                          if h not in retracted}, lo, hi)}
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--state-root", type=Path, default=REPO)
    p.add_argument("--seconds", type=float, default=180)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if not math.isfinite(args.seconds) or not 15 <= args.seconds <= 600:
        p.error("seconds must be finite and between 15 and 600")
    if args.out.exists():
        p.error("output already exists; preserve the earlier measurement")
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.services.axiom_address_verify import evm_watched_owners
    db_path = str(args.state_root / "sniper_tracker.db")
    conn = get_readonly_connection(db_path)
    try:
        rosters = {c: sorted(evm_watched_owners(conn, c)) for c in ("56", "8453", "4663")}
        initial_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM onchain_wallet_detections").fetchone()[0]
    finally:
        conn.close()
    started = datetime.now(timezone.utc).isoformat()
    result = asyncio.run(capture(args.seconds, rosters))
    conn = get_readonly_connection(db_path)
    try:
        changed = any(rosters[c] != sorted(evm_watched_owners(conn, c)) for c in rosters)
        rows = [dict(r) for r in conn.execute(
            "SELECT id,network_id,signature,source_observation_state,source_valuation_json "
            "FROM onchain_wallet_detections WHERE id>? AND network_id IN ('56','8453','4663') "
            "ORDER BY id LIMIT 1000", (initial_id,))]
    finally:
        conn.close()
    buys = []
    for r in rows:
        hashes = [s.lower() for s in str(r["signature"]).split(":") if HASH.fullmatch(s)]
        proof = json.loads(r.pop("source_valuation_json") or "{}").get("source_initiation") or {}
        r["origin_network_id"] = proof.get("origin_network_id")
        r["visibility"] = {name: {kind: events[hashes[0]] for kind, events in d["events"].items()
                                if hashes and hashes[0] in events}
                           for name, d in result.items() if d["chain"] == r["network_id"]}
        buys.append(r)
    output = {"started_utc": started, "ended_utc": datetime.now(timezone.utc).isoformat(),
              "clock": "one-process monotonic seconds", "requested_seconds": args.seconds,
              "roster_counts": {c: len(v) for c, v in rosters.items()}, "roster_changed": changed,
              "comparison": comparisons(result), "ledger_detections": buys, "streams": result}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as f:
        f.write(json.dumps(output, indent=2)+"\n")
    print(json.dumps({k: v for k, v in output.items() if k != "streams"}, indent=2))
    return 0 if all(not d["errors"] for d in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
