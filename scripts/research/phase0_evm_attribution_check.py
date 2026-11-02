#!/usr/bin/env python3
"""Phase 0 of the fomo-to-on-chain attribution design (docs/superpowers/specs/
2026-09-07-fomo-account-to-onchain-wallet-attribution-design.md). Pure measurement. It writes no
production code path and changes no runtime behaviour.

For each of the 47 traders in memecoin_evm_wallet_addresses (a fomo-profile-scraped address, NOT
proven against the chain), take that trader's most recent chain-4663 buy event from
memecoin_events. Read chain-4663 ERC-20 Transfer logs in a time window around that event from
https://rpc.mainnet.chain.robinhood.com (the chain-owner endpoint named in the design doc, never
the custody RPC). Find, per transaction, the address whose balance of the bought mint rose the
most (the buyer candidate for that trade). Then answer the three Phase 0 questions:

  1. Does the profile address appear as a buyer of that trader's coin?
  2. Does the same candidate buyer address recur across more than one trader (a shared executor)?
  3. Can an EVM buyer be read from this endpoint at all (the feasibility gate)?

REQUEST BUDGET, stated before the run: one trader = one event = one eth_getBlockByNumber calibration
read plus one eth_getLogs call, with a bounded bisection fallback (max 2 extra eth_getLogs calls)
only if the endpoint refuses a window for exceeding its 10,000-row match limit. 47 traders -> <=94
calibration calls + <=141 eth_getLogs calls, plus 3 one-off startup calibration calls (block
number, two blocks for the seconds-per-block ratio). A 1.2s pause runs between every call; the
endpoint rate-limited an unpaced burst during dry-run testing (HTTP 429, then a transient 403) and
calmed down once calls were paced and carried a User-Agent.

A per-log `blockTimestamp` field looked usable in an initial capability probe (a "latest"-only
query returns it populated), but a historical range query returns it as 0x0 on this endpoint
(measured this run). Time filtering below uses block NUMBER distance from a calibrated target
block instead, converted through a measured seconds-per-block ratio — see candidate_buyer()'s
docstring.

Writes ONLY its own table, phase0_evm_fresh_buy_check. All other reads are read-only against
sniper_tracker.db (get_readonly_connection).
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from sniper.persistence.db_core import get_readonly_connection
from sniper.services.memecoin_evm_onchain_watch import TRANSFER_TOPIC, _integer, _topic_address

DB = REPO / "sniper_tracker.db"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"   # chain-owner endpoint, never custody RPC
CHAIN_NETWORK_ID = "4663"
ZERO = "0x0000000000000000000000000000000000000000"
WINDOW_S = 150            # margin around the fomo-reported buy time
CALL_PAUSE_S = 1.2        # endpoint rate-limits an unpaced burst (measured this session)

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase0_evm_fresh_buy_check (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT NOT NULL, handle TEXT, mint TEXT NOT NULL,
    event_created_at TEXT, event_epoch INTEGER,
    window_from_block INTEGER, window_to_block INTEGER,
    logs_returned INTEGER, kept_logs INTEGER, distinct_txs INTEGER,
    nearest_tx_hash TEXT, nearest_tx_buyer TEXT, nearest_tx_time_delta_s REAL,
    profile_address TEXT NOT NULL,
    strict_match INTEGER NOT NULL, loose_match INTEGER NOT NULL,
    note TEXT, created_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _rpc(method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last_err = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                RPC_URL, data=body,
                headers={"content-type": "application/json", "user-agent": "sniper-research/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                return None, d["error"]
            return d.get("result"), None
        except Exception as exc:
            last_err = str(exc)
            time.sleep(3.0)
    return None, last_err


def _epoch(iso_ts):
    try:
        return int(datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, AttributeError):
        return None


def get_logs_windowed(mint, from_block, to_block, depth=0):
    """eth_getLogs for one mint's Transfer topic, bisecting on the endpoint's 10k-row cap."""
    time.sleep(CALL_PAUSE_S)
    result, err = _rpc("eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "address": mint, "topics": [TRANSFER_TOPIC],
    }])
    if err is not None:
        if depth < 2 and "exceeds limit" in str(err.get("message", "")) and to_block > from_block:
            mid = (from_block + to_block) // 2
            left = get_logs_windowed(mint, from_block, mid, depth + 1)
            right = get_logs_windowed(mint, mid + 1, to_block, depth + 1)
            return (left or []) + (right or [])
        return None
    return result or []


def candidate_buyer(logs, mint, target_block, span_blocks, spb):
    """Group Transfer logs by tx; for each tx, the address whose balance of `mint` rose the most
    is that tx's buyer candidate. Return the candidate for the tx nearest target_block.

    Ranks by BLOCK NUMBER, not the log's own `blockTimestamp` field: that field reads 0x0 on a
    historical (non-head) eth_getLogs range on this endpoint (measured this run — it is populated
    only for a "latest" query). Block distance converts to a time distance through the calibrated
    seconds-per-block ratio instead, which measurement showed accurate to ~9s over a 100k-block
    span (see main()'s per-event calibration read)."""
    mint_l = mint.lower()
    by_tx = {}
    for log in logs:
        bn = _integer(log.get("blockNumber"))
        if bn is None or abs(bn - target_block) > span_blocks:
            continue
        topics = log.get("topics") or []
        if len(topics) < 3:
            continue
        sender, receiver = _topic_address(topics[1]), _topic_address(topics[2])
        amount = _integer(log.get("data"))
        if not sender or not receiver or amount <= 0:
            continue
        tx = log.get("transactionHash")
        entry = by_tx.setdefault(tx, {"deltas": {}, "block_number": bn})
        if receiver != mint_l and receiver != ZERO:
            entry["deltas"][receiver] = entry["deltas"].get(receiver, 0) + amount
        if sender != mint_l and sender != ZERO:
            entry["deltas"][sender] = entry["deltas"].get(sender, 0) - amount
    if not by_tx:
        return None, None, None, 0
    nearest_tx, nearest_entry = min(by_tx.items(), key=lambda kv: abs(kv[1]["block_number"] - target_block))
    positives = {a: d for a, d in nearest_entry["deltas"].items() if d > 0}
    buyer = max(positives, key=positives.get) if positives else None
    delta_s = (nearest_entry["block_number"] - target_block) * spb
    return nearest_tx, buyer, delta_s, len(by_tx)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None,
                     help="check only the first N traders (smoke-test the run before the full 47)")
    args = ap.parse_args()

    ro = get_readonly_connection(str(DB))
    ro.row_factory = sqlite3.Row
    traders = ro.execute(
        "SELECT source_user_id, handle, address FROM memecoin_evm_wallet_addresses ORDER BY handle"
    ).fetchall()
    if args.limit:
        traders = traders[:args.limit]
    print(f"traders to check: {len(traders)}", flush=True)

    latest_num, err = _rpc("eth_blockNumber", [])
    if err:
        sys.exit(f"REFUSE: cannot read latest block ({err}) — endpoint failed the feasibility gate")
    latest_num = int(latest_num, 16)
    time.sleep(CALL_PAUSE_S)
    ref_num = latest_num - 100_000
    ref_block, err = _rpc("eth_getBlockByNumber", [hex(ref_num), False])
    if err:
        sys.exit(f"REFUSE: cannot calibrate block time ({err})")
    latest_block, err = _rpc("eth_getBlockByNumber", [hex(latest_num), False])
    if err:
        sys.exit(f"REFUSE: cannot calibrate block time ({err})")
    spb = (int(latest_block["timestamp"], 16) - int(ref_block["timestamp"], 16)) / (latest_num - ref_num)
    now_epoch = int(latest_block["timestamp"], 16)
    print(f"calibration: latest_block={latest_num} seconds/block={spb:.5f}", flush=True)

    wdb = sqlite3.connect(DB, timeout=30)
    wdb.executescript(SCHEMA)

    rows_out = []
    for t in traders:
        uid, handle, profile_addr = t["source_user_id"], t["handle"], t["address"].lower()
        ev = ro.execute(
            "SELECT mint, created_at FROM memecoin_events WHERE source_user_id=? AND side='buy' "
            "AND network_id=? ORDER BY created_at DESC LIMIT 1", (uid, CHAIN_NETWORK_ID)
        ).fetchone()
        if not ev:
            rows_out.append((uid, handle, None, None, None, None, None, 0, 0, 0,
                              None, None, None, profile_addr, 0, 0, "no_4663_buy_event", now_iso()))
            continue
        mint, created_at = ev["mint"], ev["created_at"]
        epoch = _epoch(created_at)
        if epoch is None:
            rows_out.append((uid, handle, mint, created_at, None, None, None, 0, 0, 0,
                              None, None, None, profile_addr, 0, 0, "bad_timestamp", now_iso()))
            continue
        target_block = latest_num - int(round((now_epoch - epoch) / spb))
        # One calibration read per event: the linear model measured ~9s accurate over a 100k-block
        # span in this session's dry run, but a real block read removes that drift instead of
        # trusting it, at the cost of one extra bounded RPC call.
        time.sleep(CALL_PAUSE_S)
        cal_block, cal_err = _rpc("eth_getBlockByNumber", [hex(target_block), False])
        if not cal_err and cal_block:
            actual_ts = _integer(cal_block.get("timestamp"))
            target_block += int(round((epoch - actual_ts) / spb))
        span_kept = int(round(WINDOW_S / spb))
        span_fetch = span_kept + 50
        lo, hi = target_block - span_fetch, target_block + span_fetch
        logs = get_logs_windowed(mint, lo, hi)
        if logs is None:
            rows_out.append((uid, handle, mint, created_at, epoch, lo, hi, 0, 0, 0,
                              None, None, None, profile_addr, 0, 0, "rpc_failed", now_iso()))
            print(f"{handle:<20} mint={mint[:10]} RPC FAILED", flush=True)
            continue
        nearest_tx, buyer, delta_s, distinct_txs = candidate_buyer(logs, mint, target_block, span_kept, spb)
        strict = 1 if (buyer and buyer == profile_addr) else 0
        kept = sum(1 for l in logs if abs((_integer(l.get("blockNumber")) or 0) - target_block) <= span_kept)
        loose = 1 if any(
            _topic_address((l.get("topics") or [None, None, None])[2]) == profile_addr
            for l in logs if len(l.get("topics") or []) >= 3
            and abs((_integer(l.get("blockNumber")) or 0) - target_block) <= span_kept
        ) else 0
        note = "ok" if buyer else ("no_logs_in_window" if kept == 0 else "no_positive_delta_found")
        rows_out.append((uid, handle, mint, created_at, epoch, lo, hi, len(logs), kept, distinct_txs,
                          nearest_tx, buyer, delta_s, profile_addr, strict, loose, note, now_iso()))
        print(f"{handle:<20} mint={mint[:10]} logs={len(logs):5d} kept={kept:4d} txs={distinct_txs:3d} "
              f"buyer={buyer or '-'} strict={strict} loose={loose} note={note}", flush=True)

    wdb.executemany(
        "INSERT INTO phase0_evm_fresh_buy_check (source_user_id, handle, mint, event_created_at, "
        "event_epoch, window_from_block, window_to_block, logs_returned, kept_logs, distinct_txs, "
        "nearest_tx_hash, nearest_tx_buyer, nearest_tx_time_delta_s, profile_address, strict_match, "
        "loose_match, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_out)
    wdb.commit()

    # ---- report the three Phase 0 questions ----
    n = len(rows_out)
    reached = [r for r in rows_out if r[16] not in ("no_4663_buy_event", "bad_timestamp", "rpc_failed")]
    with_buyer = [r for r in reached if r[11]]  # nearest_tx_buyer present
    strict_hits = [r for r in rows_out if r[14] == 1]
    loose_hits = [r for r in rows_out if r[15] == 1]
    print("\n=== PHASE 0 REPORT ===")
    print(f"n traders checked: {n}")
    print(f"Q3 feasibility — endpoint reached and returned logs for: {len(reached)}/{n}; "
          f"a buyer candidate was extracted for: {len(with_buyer)}/{n}")
    print(f"Q1 profile-address match — strict (nearest-tx buyer == profile address): "
          f"{len(strict_hits)}/{n}; loose (profile address appears as any receiver in window): "
          f"{len(loose_hits)}/{n}")
    buyer_to_traders = {}
    for r in with_buyer:
        buyer_to_traders.setdefault(r[11], set()).add(r[1])
    shared = {b: hs for b, hs in buyer_to_traders.items() if len(hs) > 1}
    print(f"Q2 shared executors — distinct candidate-buyer addresses seen: {len(buyer_to_traders)}; "
          f"addresses seen for >1 trader: {len(shared)}")
    for b, hs in shared.items():
        print(f"    {b} -> {sorted(hs)}")


if __name__ == "__main__":
    main()
