#!/usr/bin/env python3
"""Sandwich read over landed slot-lane fills (Alpha GO 2026-10-15; standing check, read-only).

Two signs that someone priced us to our limit:
1. NEAR LIMIT: the fill received at most 1% more tokens than the signed minimum
   (receipt_execution_evidence.acquired_atomic vs slot_price_limit.min_tokens_atomic).
2. BRACKET: in our landing block, one trader bought the coin before our transaction and sold it
   after. "adjacent" = those are the nearest coin trades on each side of us; "any" = anywhere in
   the block.

Off the fill path. The ledger is opened read-only. Blocks come from MEMECOIN_RESEARCH_HELIUS_RPC_URL
(research endpoint, never custody), one getBlock per fill, sequential, and the read stops at the
first RPC error: blocks not read are counted as unknown, never as clean.

    python3 scripts/memecoin_slot_sandwich_read.py [--since ISO] [--no-blocks] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

NEAR_LIMIT_PCT = 1.0
PACE_S = 0.2


def coin_deltas(tx, mint):
    """{owner: atomic change of `mint`} for one getBlock (transactionDetails=accounts) entry."""
    meta = tx.get("meta") or {}
    if meta.get("err") is not None:
        return {}                                   # a failed transaction moved nothing
    out = {}
    for side, sign in (("preTokenBalances", -1), ("postTokenBalances", 1)):
        for bal in meta.get(side) or ():
            if bal.get("mint") == mint and bal.get("owner"):
                out[bal["owner"]] = out.get(bal["owner"], 0) + sign * int(bal["uiTokenAmount"]["amount"])
    return {owner: d for owner, d in out.items() if d}


def trades(tx, mint):
    """[(trader, +1 buy | -1 sell)] in one transaction: owners of the coin that also signed it.
    ponytail: a sandwich that buys and sells from two different wallets reads as two traders, and
    a pool or relayer that signs is counted as a trader; widen to fee-payer clusters if it matters."""
    signers = {k["pubkey"] for k in tx["transaction"]["accountKeys"] if k.get("signer")}
    return [(owner, 1 if d > 0 else -1) for owner, d in coin_deltas(tx, mint).items() if owner in signers]


def classify_block(txs, our_sig, mint):
    """Bracket verdict for our transaction in one block. None when our signature is not in it.
    Our custody is our transaction's fee payer; `acquired` is its own coin change in this block."""
    ours = next((i for i, tx in enumerate(txs) if tx["transaction"]["signatures"][0] == our_sig), None)
    if ours is None:
        return None
    custody = txs[ours]["transaction"]["accountKeys"][0]["pubkey"]
    seq = [(i, who, side) for i, tx in enumerate(txs) if i != ours
           for who, side in trades(tx, mint) if who != custody]
    before = [t for t in seq if t[0] < ours]
    after = [t for t in seq if t[0] > ours]
    buyers_before = {who for _i, who, side in before if side > 0}
    sellers_after = {who for _i, who, side in after if side < 0}
    near_b = before[-1] if before else None
    near_a = after[0] if after else None
    adjacent = bool(near_b and near_a and near_b[1] == near_a[1] and near_b[2] > 0 and near_a[2] < 0)
    return {"index": ours, "block_txs": len(txs), "coin_trades_before": len(before),
            "acquired": coin_deltas(txs[ours], mint).get(custody),
            "coin_trades_after": len(after), "adjacent": adjacent,
            "any": sorted(buyers_before & sellers_after)}


def near_limit(acquired, min_tokens):
    """(headroom_pct, within_1pct); (None, None) when either side is unknown."""
    if not acquired or not min_tokens or int(min_tokens) <= 0:
        return None, None
    acquired, min_tokens = int(acquired), int(min_tokens)
    head = (acquired / min_tokens - 1.0) * 100.0
    return head, acquired * 100 <= min_tokens * (100 + NEAR_LIMIT_PCT)     # exact, no float edge


def landed_fills(conn, since):
    rows = conn.execute(
        "SELECT attempt_id, source_created_at, source_handle, mint, tx_sig, trace_json "
        "FROM memecoin_entry_attempts WHERE network_id='solana' AND signal_source='slot_lane' "
        "AND terminal_status='filled' AND tx_sig IS NOT NULL "
        "AND julianday(source_created_at) >= julianday(?) ORDER BY source_created_at", (since,))
    for row in rows:
        events = {e.get("name"): e for e in json.loads(row["trace_json"] or "{}").get("substage_events", [])}
        evidence = (events.get("receipt_execution_evidence") or {}).get("evidence") or {}
        limit = events.get("slot_price_limit") or {}
        yield {"attempt_id": row["attempt_id"], "at": row["source_created_at"], "handle": row["source_handle"],
               "mint": row["mint"], "sig": row["tx_sig"],
               "slot": evidence.get("receipt_slot") or (events.get("slot_resend") or {}).get("landing_slot"),
               "acquired": evidence.get("acquired_atomic"), "min_tokens": limit.get("min_tokens_atomic")}


def _rpc(url, method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        answer = json.loads(resp.read())
    if "error" in answer or answer.get("result") is None:
        raise RuntimeError("%s: %s" % (method, (answer.get("error") or {}).get("message", "no result")))
    return answer["result"]


def get_block(url, slot):
    return _rpc(url, "getBlock", [int(slot), {"encoding": "json", "transactionDetails": "accounts",
                                              "maxSupportedTransactionVersion": 1, "rewards": False}])["transactions"]


def landing_slot(url, sig):
    """Old traces (before 2026-09-28) carry no landing slot: read it from the transaction."""
    return _rpc(url, "getTransaction", [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1}])["slot"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-09-26T00:00:00+00:00")
    ap.add_argument("--no-blocks", action="store_true", help="near-limit half only, no RPC")
    ap.add_argument("--db", default=None)
    args = ap.parse_args(argv)
    from sniper.persistence.db_core import get_readonly_connection
    fills = list(landed_fills(get_readonly_connection(args.db), args.since))
    url = None
    if not args.no_blocks:
        url = (os.environ.get("MEMECOIN_RESEARCH_HELIUS_RPC_URL") or "").strip()
        if not url:
            from dotenv import dotenv_values
            url = (dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
                   .get("MEMECOIN_RESEARCH_HELIUS_RPC_URL") or "").strip()
        if not url:
            sys.exit("refused: MEMECOIN_RESEARCH_HELIUS_RPC_URL is not set")
    near = unknown_near = adjacent = anywhere = read = 0
    stopped = None
    for f in fills:
        verdict = None
        if url and stopped is None:
            try:
                slot = f["slot"] or landing_slot(url, f["sig"])
                verdict = classify_block(get_block(url, slot), f["sig"], f["mint"])
                read += verdict is not None
                time.sleep(PACE_S)
            except Exception as exc:                 # stop on the first error: the rest is unknown
                stopped = "%s at %s" % (exc, f["attempt_id"][:8])
        head, within = near_limit(f["acquired"] or (verdict or {}).get("acquired"), f["min_tokens"])
        unknown_near += within is None
        near += bool(within)
        if verdict is not None:
            adjacent += verdict["adjacent"]
            anywhere += bool(verdict["any"])
        if within or (verdict and (verdict["adjacent"] or verdict["any"])):
            print("FLAG %s %s %-16s headroom=%s adjacent=%s any=%s idx=%s" % (
                f["attempt_id"][:8], f["at"][:19], (f["handle"] or "")[:16],
                None if head is None else "%.2f%%" % head, verdict and verdict["adjacent"],
                verdict and [w[:8] for w in verdict["any"]], verdict and verdict["index"]))
    n = len(fills)
    print("landed slot-lane fills since %s: %d" % (args.since, n))
    print("near limit (<= %.0f%% over min_tokens): %d of %d known (%d unknown)"
          % (NEAR_LIMIT_PCT, near, n - unknown_near, unknown_near))
    if url:
        print("blocks read: %d of %d; adjacent bracket: %d; same trader before and after anywhere: %d"
              % (read, n, adjacent, anywhere))
        if stopped:
            print("STOPPED (rest unknown, never clean): %s" % stopped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
