#!/usr/bin/env python3
"""Phase 1 of the fomo-to-on-chain attribution design (docs/superpowers/specs/
2026-09-07-fomo-account-to-onchain-wallet-attribution-design.md, sections 4 and 7), amended by
Alpha after Phase 0 (session message 2026-09-07): fresh-buy attribution with a code-class check
before the shared-address reject, both tx.from and the token beneficiary recorded on every
transaction, and a raised confirmation bar of >=3 agreeing distinct coins (not the spec's >=2).

This writes ONLY its own three tables (phase1_evm_resolver_events, _rejects, _proposals). It does
NOT write memecoin_wallet_addresses -- Alpha reviews the proposals table and promotes by hand. No
production code path changes and no funded behaviour changes.

METHOD, per trader:
  1. Take up to MAX_MINTS_PER_TRADER distinct chain-4663 mints the trader bought (most recent buy
     per distinct mint -- rebuys of the same coin do not count as a second agreeing coin).
  2. For each (trader, mint) event: calibrate a target block from the event's timestamp (one real
     eth_getBlockByNumber read per event -- Phase 0 proved a single global linear model drifts by
     hours over a 2-month span, so this cannot be skipped), then eth_getLogs the mint's Transfer
     topic in a window around it. The address with the largest NET POSITIVE token-balance change
     within the nearest transaction is the buyer candidate (same method as Phase 0).
  3. eth_getTransactionReceipt on that transaction gives BOTH `from` (the signer) and its logs, so
     one extra call answers both "who signed" and "did the buyer spend the quote asset" (a WETH
     Transfer FROM the buyer in the same receipt). Record both; never infer one from the other --
     Phase 0 found 28 of 41 events were relayed (signer != beneficiary).
  4. eth_getCode-classify the buyer address: 'contract' (ordinary deployed bytecode) is an
     automatic reject -- Phase 0 found a 24KB contract that nets positive inside one transaction
     purely as router/pool plumbing. 'eoa' and 'eip7702_delegated_eoa' (bytecode 0xef0100 + a
     20-byte delegate -- a real per-user account on this chain, not shared infrastructure) both
     stay in play.
  5. Aggregate per trader: an address that is the buyer candidate for >=CONFIRM_MIN_COINS DISTINCT
     mints is proposed. Then, across ALL traders' proposals, any candidate address proposed for
     more than one trader is rejected as a shared executor (this only fires on the non-contract
     survivors of step 4 -- a true collision here is the real signal, not router noise).

RETENTION AND WINDOW, measured live before this run: this endpoint retains full history back to at
least 2026-07-12 for eth_getBlockByNumber (binary search converged to the correct block within 1s),
but a WIDE eth_getLogs window (+/-2000 blocks, ~404s) timed out at that same historical depth while
a narrower one (+/-500 blocks, ~101s) returned successfully. WINDOW_S is kept smaller than Phase
0's here for this reason, with a bisection fallback on either a row-count-limit error or a timeout.

AMENDMENT 3 (Alpha's hypothesis: use the local data/memecoin_ticks.db `pool_ticks` tape to avoid
eth_getLogs) -- TESTED, NOT USED. `pool_meta` returned 96 candidate pool rows for one single mint
address (many with 66-hex-char pool ids, i.e. V4 PoolIds, not deployed addresses -- there is no
cheap way to pick the right one), and the pool actually queried had zero tick rows anywhere near
the target block for a mid-July event (its covered range starts at block 50,270,979, long after
July). Combined with the two caveats Alpha already named (`maker` is the router, not the trader;
the tape is frozen at 2026-09-04T20:56), the savings did not clear the complexity bar. eth_getLogs,
correctly windowed, already reaches the full needed history.

REQUEST BUDGET, stated before running: MAX_MINTS_PER_TRADER=6, up to 47 traders => <=282 events.
Calibration is now an ITERATED correction (up to 3 eth_getBlockByNumber reads, converging fast --
see main()'s comment on why one shot was not enough at a 2-month distance), plus 1 getLogs plus 1
getTransactionReceipt: worst case 5 calls/event, 1 (recent events converge on the first read) to 5
in practice. Upper bound 282*5 = 1410 calls, plus one eth_getCode call per DISTINCT address
encountered (cached, so a repeat costs nothing) -- estimated <=250 more. Bound: <=1700 calls for
this run. Paced >=1.15s apart. Chain-owner endpoint only, never custody RPC or QuickNode.
"""
import argparse
import json
import re
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
WINDOW_S = 90              # narrower than Phase 0's 150s -- avoids the getLogs timeout at depth
CALL_PAUSE_S = 1.15
MAX_MINTS_PER_TRADER = 6
CONFIRM_MIN_COINS = 3      # amendment 1: raised from the spec's rule-5 bar of >=2
EIP7702_RE = re.compile(r"0xef0100[0-9a-fA-F]{40}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase1_evm_resolver_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT NOT NULL, handle TEXT, mint TEXT NOT NULL,
    event_created_at TEXT, target_block INTEGER, window_from_block INTEGER, window_to_block INTEGER,
    nearest_tx_hash TEXT, buyer_address TEXT, buyer_code_class TEXT,
    signer_address TEXT, signer_code_class TEXT, quote_spent_confirmed INTEGER,
    note TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase1_evm_resolver_rejects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT, handle TEXT, candidate_address TEXT, code_class TEXT,
    rule TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase1_evm_resolver_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT NOT NULL, handle TEXT, candidate_address TEXT NOT NULL, code_class TEXT,
    agreeing_distinct_mints INTEGER, mints_json TEXT, first_event_at TEXT, last_event_at TEXT,
    status TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _epoch(iso_ts):
    try:
        return int(datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, AttributeError):
        return None


def _rpc(method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for _attempt in range(tries):
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
            time.sleep(3.0)
    # A DICT, not the bare string "failed" (Codex audit #9): every caller reads the error as
    # `err.get("message", "")`, which raises AttributeError on a string and crashed the resolver
    # on ordinary transport exhaustion instead of falling back to its None/unknown branch.
    return None, {"message": "transport failed after retries"}


def get_logs_windowed(mint, from_block, to_block, depth=0):
    """eth_getLogs for one mint's Transfer topic. Bisects on either the endpoint's row-count cap
    or a timeout -- both were observed live this session at historical depth."""
    time.sleep(CALL_PAUSE_S)
    result, err = _rpc("eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block),
        "address": mint, "topics": [TRANSFER_TOPIC],
    }])
    if err is not None:
        msg = str(err.get("message", "")).lower()
        retryable = "exceeds limit" in msg or "timed out" in msg
        if depth < 2 and retryable and to_block > from_block:
            mid = (from_block + to_block) // 2
            left = get_logs_windowed(mint, from_block, mid, depth + 1)
            right = get_logs_windowed(mint, mid + 1, to_block, depth + 1)
            # A FAILED HALF IS UNKNOWN, NOT EMPTY (Codex audit #9). `(left or []) + (right or [])`
            # read a failed half exactly like a half with zero transfers, so a bisected window that
            # genuinely lost coverage under-counted logs instead of reporting the read as unknown.
            if left is None or right is None:
                return None
            return left + right
        return None
    return result or []


def candidate_buyer(logs, mint, target_block, span_blocks):
    """Group Transfer logs by tx; the address with the largest net-positive balance change of
    `mint`, in the transaction nearest target_block, is that event's buyer candidate."""
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
        return None, None
    nearest_tx, nearest_entry = min(by_tx.items(), key=lambda kv: abs(kv[1]["block_number"] - target_block))
    positives = {a: d for a, d in nearest_entry["deltas"].items() if d > 0}
    buyer = max(positives, key=positives.get) if positives else None
    return nearest_tx, buyer


def classify_code(address, cache):
    """eoa / eip7702_delegated_eoa / contract_<N>B / read_failed. Cached -- a repeat address (a
    common signer relaying many trades, say) costs nothing on the second call."""
    if address in cache:
        return cache[address]
    time.sleep(CALL_PAUSE_S)
    code, err = _rpc("eth_getCode", [address, "latest"])
    if err or code is None:
        cls = "read_failed"
    elif code == "0x":
        cls = "eoa"
    elif EIP7702_RE.fullmatch(code):
        cls = "eip7702_delegated_eoa"
    else:
        cls = f"contract_{len(code)//2 - 1}B"
    cache[address] = cls
    return cls


def quote_spent_by(receipt, buyer, weth):
    """True if `buyer` appears as the sender of a WETH Transfer inside this receipt -- confirms
    the buyer paid the quote asset, not just received the memecoin (design doc step 3)."""
    buyer_l, weth_l = (buyer or "").lower(), (weth or "").lower()
    for log in (receipt or {}).get("logs") or []:
        topics = log.get("topics") or []
        if len(topics) < 3 or str(topics[0]).lower() != TRANSFER_TOPIC:
            continue
        if str(log.get("address") or "").lower() != weth_l:
            continue
        if _topic_address(topics[1]) == buyer_l:
            return True
    return False


def classify_evidence(buyer_cls, spent):
    """(note, eligible) for one candidate observation (Codex audit #10). `spent` is a tri-state:
    True (confirmed quote-asset spend), False (receipt read fine, no such spend), or None (the
    receipt itself could not be read -- unknown, never treated as False). `buyer_cls` is never
    "contract_*" here; that class is auto-rejected before this runs. Eligible (note == "ok")
    requires BOTH an accepted account class and a CONFIRMED spend -- everything else is kept as an
    observation, not silently dropped or silently counted, with the note naming which evidence is
    missing."""
    if buyer_cls == "read_failed":
        return "buyer_class_unread", False
    if spent is None:
        return "receipt_unread", False
    if not spent:
        return "quote_spend_not_confirmed", False
    return "ok", True


def collisions_from(per_trader_candidates):
    """Addresses shared across more than one trader, from EVERY eligible per-trader candidate
    (Codex audit #10) -- not only the ones that individually cleared CONFIRM_MIN_COINS. Built from
    `proposals` alone, a router used by trader A for 3 coins and trader B for only 1 was never
    flagged shared: B's candidate never reached `proposals` to be counted, so the collision was
    invisible from the one side that actually had it."""
    addr_to_traders = {}
    for uid, addrs in per_trader_candidates.items():
        for addr in addrs:
            addr_to_traders.setdefault(addr, []).append(uid)
    return {a for a, uids in addr_to_traders.items() if len(set(uids)) > 1}


def distinct_mints_for_trader(ro, uid, limit):
    """Most recent buy event per distinct mint, newest mints first."""
    rows = ro.execute(
        "SELECT mint, MAX(created_at) AS created_at FROM memecoin_events "
        "WHERE source_user_id=? AND side='buy' AND network_id=? GROUP BY mint "
        "ORDER BY created_at DESC LIMIT ?", (uid, CHAIN_NETWORK_ID, limit)
    ).fetchall()
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None, help="check only the first N traders")
    ap.add_argument("--handles", default=None,
                     help="comma-separated handles to check instead of all traders (deep-sample mode)")
    ap.add_argument("--max-mints", type=int, default=MAX_MINTS_PER_TRADER,
                     help=f"distinct mints to sample per trader (default {MAX_MINTS_PER_TRADER})")
    args = ap.parse_args()
    max_mints = args.max_mints

    ro = get_readonly_connection(str(DB))
    ro.row_factory = sqlite3.Row
    traders = ro.execute(
        "SELECT source_user_id, handle, address FROM memecoin_evm_wallet_addresses ORDER BY handle"
    ).fetchall()
    if args.handles:
        wanted = {h.strip() for h in args.handles.split(",")}
        traders = [t for t in traders if t["handle"] in wanted]
    elif args.limit:
        traders = traders[:args.limit]
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    weth = CHAIN_QUOTE[CHAIN_NETWORK_ID]["token"]
    print(f"traders: {len(traders)}  max mints/trader: {max_mints}  "
          f"confirm bar: >={CONFIRM_MIN_COINS} distinct coins", flush=True)

    wdb = sqlite3.connect(DB, timeout=30)
    wdb.executescript(SCHEMA)
    code_cache = {}
    per_trader_candidates = {}   # uid -> {address: set(mints)}
    trader_meta = {}             # uid -> (handle, first_event_at, last_event_at) per address later

    for t in traders:
        uid, handle = t["source_user_id"], t["handle"]
        mints = distinct_mints_for_trader(ro, uid, max_mints)
        per_trader_candidates.setdefault(uid, {})
        for m in mints:
            mint, created_at = m["mint"], m["created_at"]
            epoch = _epoch(created_at)
            if epoch is None:
                continue
            time.sleep(CALL_PAUSE_S)
            latest_num_hex, err = _rpc("eth_blockNumber", [])
            if err:
                wdb.execute(
                    "INSERT INTO phase1_evm_resolver_events (source_user_id, handle, mint, "
                    "event_created_at, note, created_at) VALUES (?,?,?,?,?,?)",
                    (uid, handle, mint, created_at, "rpc_failed", now_iso()))
                wdb.commit()
                print(f"{handle:<20} mint={mint[:10]} RPC FAILED (blockNumber)", flush=True)
                continue
            latest_num = int(latest_num_hex, 16)
            time.sleep(CALL_PAUSE_S)
            latest_block, err = _rpc("eth_getBlockByNumber", [hex(latest_num), False])
            if err:
                continue
            now_epoch = _integer(latest_block.get("timestamp"))
            spb = 0.1009   # measured this session near "now"; NOT assumed constant over time --
            # a single-shot correction using this rate left ~143s of residual error at a 2-month
            # distance during this session's retention probe (the true average rate over that span
            # measured ~0.44% different). Iterate the correction instead of trusting one shot; each
            # further iteration corrects a much smaller residual, so it converges fast for old
            # events and costs only the one call for recent ones (already within WINDOW_S/2).
            target_block = latest_num - int(round((now_epoch - epoch) / spb))
            for _cal_iter in range(3):
                time.sleep(CALL_PAUSE_S)
                cal_block, cal_err = _rpc("eth_getBlockByNumber", [hex(target_block), False])
                if cal_err or not cal_block:
                    break
                actual_ts = _integer(cal_block.get("timestamp"))
                delta = epoch - actual_ts
                if abs(delta) <= WINDOW_S // 2:
                    break
                target_block += int(round(delta / spb))
            span_kept = int(round(WINDOW_S / spb))
            span_fetch = span_kept + 50
            lo, hi = target_block - span_fetch, target_block + span_fetch
            logs = get_logs_windowed(mint, lo, hi)
            if logs is None:
                wdb.execute(
                    "INSERT INTO phase1_evm_resolver_events (source_user_id, handle, mint, "
                    "event_created_at, target_block, window_from_block, window_to_block, note, "
                    "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (uid, handle, mint, created_at, target_block, lo, hi, "rpc_failed", now_iso()))
                wdb.commit()
                print(f"{handle:<20} mint={mint[:10]} RPC FAILED (getLogs)", flush=True)
                continue
            nearest_tx, buyer = candidate_buyer(logs, mint, target_block, span_kept)
            if not buyer:
                note = "no_logs_in_window" if not logs else "no_positive_delta_found"
                wdb.execute(
                    "INSERT INTO phase1_evm_resolver_events (source_user_id, handle, mint, "
                    "event_created_at, target_block, window_from_block, window_to_block, "
                    "nearest_tx_hash, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uid, handle, mint, created_at, target_block, lo, hi, nearest_tx, note, now_iso()))
                wdb.commit()
                print(f"{handle:<20} mint={mint[:10]} note={note}", flush=True)
                continue

            buyer_cls = classify_code(buyer, code_cache)
            time.sleep(CALL_PAUSE_S)
            receipt, rerr = _rpc("eth_getTransactionReceipt", [nearest_tx])
            signer = str((receipt or {}).get("from") or "").lower() if not rerr else None
            signer_cls = classify_code(signer, code_cache) if signer else None
            # UNKNOWN IS NOT "NO" (Codex audit #10, same class as #9). A failed receipt read must
            # not read the same as a receipt that came back clean and showed no quote-asset spend.
            spent = quote_spent_by(receipt, buyer, weth) if not rerr else None

            if buyer_cls.startswith("contract_"):
                wdb.execute(
                    "INSERT INTO phase1_evm_resolver_events (source_user_id, handle, mint, "
                    "event_created_at, target_block, window_from_block, window_to_block, "
                    "nearest_tx_hash, buyer_address, buyer_code_class, signer_address, "
                    "signer_code_class, quote_spent_confirmed, note, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, handle, mint, created_at, target_block, lo, hi, nearest_tx, buyer,
                     buyer_cls, signer, signer_cls, 1 if spent else 0, "ok", now_iso()))
                wdb.commit()
                print(f"{handle:<20} mint={mint[:10]} buyer={buyer} cls={buyer_cls} "
                      f"signer={signer} spent={int(bool(spent))}", flush=True)
                wdb.execute(
                    "INSERT INTO phase1_evm_resolver_rejects (source_user_id, handle, "
                    "candidate_address, code_class, rule, detail, created_at) VALUES (?,?,?,?,?,?,?)",
                    (uid, handle, buyer, buyer_cls, "router_contract_reject",
                     f"nets positive within tx {nearest_tx} on mint {mint}", now_iso()))
                wdb.commit()
                continue   # never enters aggregation -- automatic disqualification (amendment 2a)

            note, eligible = classify_evidence(buyer_cls, spent)

            wdb.execute(
                "INSERT INTO phase1_evm_resolver_events (source_user_id, handle, mint, "
                "event_created_at, target_block, window_from_block, window_to_block, "
                "nearest_tx_hash, buyer_address, buyer_code_class, signer_address, "
                "signer_code_class, quote_spent_confirmed, note, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, handle, mint, created_at, target_block, lo, hi, nearest_tx, buyer, buyer_cls,
                 signer, signer_cls, 1 if spent else 0, note, now_iso()))
            wdb.commit()
            print(f"{handle:<20} mint={mint[:10]} buyer={buyer} cls={buyer_cls} "
                  f"signer={signer} spent={int(bool(spent))} note={note}", flush=True)

            if not eligible:
                continue   # observed, but not evidence the confirm-bar or collision check may use

            per_trader_candidates[uid].setdefault(buyer, set()).add(mint)
            trader_meta[uid] = (handle, buyer_cls)

    # ---- aggregate: propose per trader, then reject cross-trader collisions ----
    proposals = []   # (uid, handle, addr, cls, mints:set)
    for uid, addrs in per_trader_candidates.items():
        handle = trader_meta.get(uid, (None, None))[0]
        for addr, mints in addrs.items():
            if len(mints) >= CONFIRM_MIN_COINS:
                cls = code_cache.get(addr, "unknown")
                proposals.append((uid, handle, addr, cls, mints))

    shared = collisions_from(per_trader_candidates)

    for uid, handle, addr, cls, mints in proposals:
        status = "rejected_shared_address" if addr in shared else "proposed"
        if status == "rejected_shared_address":
            other = sorted(other_uid for other_uid, addrs in per_trader_candidates.items()
                           if other_uid != uid and addr in addrs)
            wdb.execute(
                "INSERT INTO phase1_evm_resolver_rejects (source_user_id, handle, "
                "candidate_address, code_class, rule, detail, created_at) VALUES (?,?,?,?,?,?,?)",
                (uid, handle, addr, cls, "cross_trader_shared_address",
                 f"also proposed for source_user_id(s): {other}", now_iso()))
        wdb.execute(
            "INSERT INTO phase1_evm_resolver_proposals (source_user_id, handle, candidate_address, "
            "code_class, agreeing_distinct_mints, mints_json, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, handle, addr, cls, len(mints), json.dumps(sorted(mints)), status, now_iso()))
    wdb.commit()

    # ---- report ----
    print("\n=== PHASE 1 REPORT ===")
    n_traders = len(traders)
    proposed = [p for p in proposals if p[2] not in shared]
    print(f"traders sampled: {n_traders}")
    print(f"proposals clearing >={CONFIRM_MIN_COINS} agreeing distinct coins: {len(proposals)} "
          f"({len(proposed)} survive the cross-trader check, {len(proposals) - len(proposed)} "
          f"rejected as shared)")
    traders_with_proposal = len({uid for uid, *_ in proposed})
    print(f"distinct traders with >=1 surviving proposal: {traders_with_proposal}/{n_traders}")
    dist = {}
    for uid, addrs in per_trader_candidates.items():
        best = max((len(m) for m in addrs.values()), default=0)
        dist[best] = dist.get(best, 0) + 1
    print("distribution of BEST agreeing-coin-count achieved per trader (all traders, incl. those "
          "under the bar):")
    for k in sorted(dist):
        print(f"    {k} coins: {dist[k]} trader(s)")


if __name__ == "__main__":
    main()
