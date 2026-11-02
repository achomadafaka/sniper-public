#!/usr/bin/env python3
"""Alpha's test 2a, adapted to what this endpoint can actually serve: does a scattered trader's
buyer candidate still HOLD the coin shortly after the buy, or did it pass the token straight on?
Distinguishes the two live hypotheses for why 36 of Phase 1's 47 traders never reached the >=3-coin
agreement bar despite having enough clean candidate events: genuine per-trade address rotation
(each candidate is a real but short-lived beneficiary) vs a buyer-detection artifact that catches a
hop short of the terminal holder.

ADAPTATION, measured live before writing this: Alpha's literal spec was an ERC-20 balanceOf at a
later block. Tested that directly on https://rpc.mainnet.chain.robinhood.com first -- eth_call
against a historical block fails ("metadata is not found") past a narrow window. Binary search
found the boundary: state is servable roughly 6,000 blocks back (~10 minutes) and no further; every
Phase 1 target_block is hours to months old, so a literal balanceOf-at-block-N call cannot reach
ANY of them. This is a non-archive node for STATE (eth_call/eth_getBalance-at-a-block); it is NOT
pruned for LOGS -- eth_getLogs has worked at arbitrary historical depth all session (Phase 0 and
Phase 1 both queried logs from July 2026 successfully).

So this checks the same underlying question through logs instead of state: eth_getLogs for a
Transfer OUT of the mint FROM the candidate address, in the window [target_block, target_block +
HOLD_CHECK_BLOCKS] (~5 minutes). An outbound transfer in that window is direct evidence of a
pass-through (the artifact hypothesis); its absence is consistent with holding, at least that long
(rotation hypothesis, or at minimum "not an instant hop"). This is not identical to a balance
check -- a partial sell still counts as "an outbound transfer occurred" even if some balance
remains -- but it answers the same intermediary/terminal question this endpoint can actually serve
evidence for.

Scope: every clean (eoa / eip7702_delegated_eoa) buyer candidate recorded by phase1_evm_resolver.py
for a trader NOT already in phase1_evm_resolver_proposals. Writes only phase1_evm_resolver_holdcheck.
"""
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import urllib.request

DB = REPO / "sniper_tracker.db"
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
CALL_PAUSE_S = 1.15
HOLD_CHECK_BLOCKS = 3000   # ~5 minutes at the measured ~0.1009 s/block
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase1_evm_resolver_holdcheck (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT, handle TEXT, mint TEXT, buyer_address TEXT,
    target_block INTEGER, window_to_block INTEGER,
    outbound_transfer_found INTEGER, still_holds INTEGER, note TEXT, created_at TEXT NOT NULL
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _rpc(method, params, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    for _ in range(tries):
        try:
            req = urllib.request.Request(
                RPC_URL, data=body,
                headers={"content-type": "application/json", "user-agent": "sniper-research/1.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                d = json.loads(r.read().decode())
            if "error" in d:
                return None, d["error"]
            return d.get("result"), None
        except Exception:
            time.sleep(3.0)
    # A DICT, not the bare string "failed" (Codex audit #9): every caller reads the error as
    # `err.get("message", "")`, which raises AttributeError on a string and crashed the check
    # on ordinary transport exhaustion instead of falling back to its None/unknown branch.
    return None, {"message": "transport failed after retries"}


def _topic(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def outbound_transfer_exists(mint, sender, from_block, to_block, depth=0):
    """eth_getLogs for a Transfer of `mint` FROM `sender`, bisecting on the same error classes
    Phase 1's resolver handled (row-count limit, timeout)."""
    time.sleep(CALL_PAUSE_S)
    res, err = _rpc("eth_getLogs", [{
        "fromBlock": hex(from_block), "toBlock": hex(to_block), "address": mint,
        "topics": [TRANSFER_TOPIC, _topic(sender)],
    }])
    if err is not None:
        msg = str(err.get("message", "")).lower()
        if depth < 2 and ("exceeds limit" in msg or "timed out" in msg) and to_block > from_block:
            mid = (from_block + to_block) // 2
            left = outbound_transfer_exists(mint, sender, from_block, mid, depth + 1)
            right = outbound_transfer_exists(mint, sender, mid + 1, to_block, depth + 1)
            # A NEGATIVE NEEDS BOTH HALVES READ (Codex audit #9). `left or right` read a failed
            # half (None) exactly like a half with no outbound transfer (False), so a bisected
            # window that lost half its coverage could still report "still holds" -- a real
            # transfer in the unread half would have said otherwise. A found transfer in EITHER
            # half is still positive evidence regardless of what the other half could read.
            if left or right:
                return True
            if left is None or right is None:
                return None
            return False
        return None
    return bool(res)


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)

    confirmed = {r[0] for r in c.execute(
        "SELECT DISTINCT handle FROM phase1_evm_resolver_proposals").fetchall()}
    rows = c.execute(
        "SELECT source_user_id, handle, mint, buyer_address, buyer_code_class, target_block "
        "FROM phase1_evm_resolver_events WHERE note='ok'").fetchall()
    todo = [r for r in rows if r["buyer_code_class"] in ("eoa", "eip7702_delegated_eoa")
            and r["handle"] not in confirmed]
    print(f"clean candidates to check (excluding {len(confirmed)} already-confirmed traders): "
          f"{len(todo)}", flush=True)

    n_holds, n_not, n_err = 0, 0, 0
    for r in todo:
        to_block = r["target_block"] + HOLD_CHECK_BLOCKS
        found = outbound_transfer_exists(r["mint"], r["buyer_address"], r["target_block"], to_block)
        if found is None:
            note, still = "rpc_failed", None
            n_err += 1
        else:
            still = 0 if found else 1
            note = "ok"
            n_holds += still
            n_not += (1 - still)
        c.execute(
            "INSERT INTO phase1_evm_resolver_holdcheck (source_user_id, handle, mint, "
            "buyer_address, target_block, window_to_block, outbound_transfer_found, still_holds, "
            "note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["source_user_id"], r["handle"], r["mint"], r["buyer_address"], r["target_block"],
             to_block, (1 if found else 0) if found is not None else None, still, note, now_iso()))
        c.commit()
        print(f"{r['handle']:<20} mint={r['mint'][:10]} buyer={r['buyer_address']} "
              f"outbound_found={found} still_holds={still} note={note}", flush=True)

    print("\n=== TEST 2a REPORT (adapted: logs, not state) ===")
    total = n_holds + n_not
    print(f"checked: {len(todo)}  resolved: {total}  rpc_failed: {n_err}")
    if total:
        print(f"no outbound transfer in ~5min window (consistent with holding): "
              f"{n_holds}/{total} ({n_holds/total:.0%})")
        print(f"outbound transfer found (pass-through): {n_not}/{total} ({n_not/total:.0%})")


if __name__ == "__main__":
    main()
