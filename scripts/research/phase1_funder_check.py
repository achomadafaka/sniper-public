#!/usr/bin/env python3
"""Alpha's test 2b: do a scattered trader's disagreeing buyer candidates share a common FUNDER? If
so, the funder -- not the buyer -- is the stable per-trader identity, because it is one address per
trader by construction rather than by luck. Pattern read from sniper/analysis/winning_wallet_thesis/
funding_graph.py's Channel E (earliest-inbound-transfer funder identification with explicit
data_gap handling) -- that file is wired to Polygon USDC; this is the same eth_getLogs shape
(TRANSFER_TOPIC, None, topic_to=candidate) retargeted to chain 4663 and WETH, its quote asset.

ADAPTATION, measured live before writing this: the natural "funder" signal -- native ETH sent to a
fresh account to cover its own gas -- cannot be traced here. Finding it needs either logs (native
transfers emit none) or a historical eth_getBalance/eth_call, and test 2a already proved this
endpoint's state window is only ~6,000 blocks (~10 minutes) -- every Phase 1 target_block is older
than that. An unfiltered "any inbound ERC-20 transfer" fallback was tested and rejected: for one
sample candidate its "earliest inbound transfer" was just ANOTHER purchase of the same mint at a
block tens of millions apart, not a funding event -- too noisy to trust as "the funder". WETH
specifically (CHAIN_QUOTE["4663"]["token"], the swap quote asset) is the one interpretable signal
left: a WETH credit to a fresh account, before its own trading activity, is a real funding event if
it exists. Its absence is a governed data_gap, not a claim of "no funder" -- fully consistent with
funding_graph.py's own philosophy (return None + data_gap, never guess).

Scope: same 153 (trader, candidate) pairs test 2a covered -- every distinct clean buyer candidate
for a trader NOT already in phase1_evm_resolver_proposals. Writes only phase1_evm_resolver_funder.
"""
import json
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
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
CALL_PAUSE_S = 1.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS phase1_evm_resolver_funder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT, handle TEXT, candidate_address TEXT,
    weth_inbound_count INTEGER, earliest_funder TEXT, earliest_block INTEGER,
    note TEXT, created_at TEXT NOT NULL
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
    return None, "failed"


def _topic(address):
    return "0x" + address.lower().replace("0x", "").rjust(64, "0")


def _topic_address(topic):
    return "0x" + str(topic or "")[-40:].lower() if topic else None


def earliest_weth_funder(candidate):
    time.sleep(CALL_PAUSE_S)
    res, err = _rpc("eth_getLogs", [{
        "fromBlock": "0x0", "toBlock": "latest", "address": WETH,
        "topics": [TRANSFER_TOPIC, None, _topic(candidate)],
    }])
    if err is not None:
        return None, None, None, "rpc_failed"
    if not res:
        return 0, None, None, "no_weth_inbound_found"
    earliest = min(res, key=lambda l: int(l["blockNumber"], 16))
    sender = _topic_address((earliest.get("topics") or [None, None])[1])
    return len(res), sender, int(earliest["blockNumber"], 16), "ok"


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)

    confirmed = {r[0] for r in c.execute(
        "SELECT DISTINCT handle FROM phase1_evm_resolver_proposals").fetchall()}
    rows = c.execute(
        "SELECT DISTINCT source_user_id, handle, buyer_address FROM phase1_evm_resolver_events "
        "WHERE note='ok' AND buyer_code_class IN ('eoa','eip7702_delegated_eoa') "
        "AND handle NOT IN (SELECT DISTINCT handle FROM phase1_evm_resolver_proposals)").fetchall()
    print(f"distinct (trader, candidate) pairs: {len(rows)}", flush=True)

    for r in rows:
        cnt, funder, block, note = earliest_weth_funder(r["buyer_address"])
        c.execute(
            "INSERT INTO phase1_evm_resolver_funder (source_user_id, handle, candidate_address, "
            "weth_inbound_count, earliest_funder, earliest_block, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (r["source_user_id"], r["handle"], r["buyer_address"], cnt, funder, block, note, now_iso()))
        c.commit()
        print(f"{r['handle']:<20} candidate={r['buyer_address']} weth_inbound={cnt} "
              f"funder={funder} note={note}", flush=True)

    print("\n=== TEST 2b REPORT ===")
    has_funder = c.execute(
        "SELECT COUNT(*) FROM phase1_evm_resolver_funder WHERE note='ok'").fetchone()[0]
    no_funder = c.execute(
        "SELECT COUNT(*) FROM phase1_evm_resolver_funder WHERE note='no_weth_inbound_found'").fetchone()[0]
    failed = c.execute(
        "SELECT COUNT(*) FROM phase1_evm_resolver_funder WHERE note='rpc_failed'").fetchone()[0]
    print(f"candidates with a WETH funder found: {has_funder}")
    print(f"candidates with NO WETH inbound ever (data_gap, not 'no funder'): {no_funder}")
    print(f"rpc_failed: {failed}")

    print("\nper-trader: do >=2 of a trader's candidates share the same earliest_funder?")
    tr = c.execute(
        "SELECT handle, earliest_funder, COUNT(DISTINCT candidate_address) n "
        "FROM phase1_evm_resolver_funder WHERE note='ok' AND earliest_funder IS NOT NULL "
        "GROUP BY handle, earliest_funder HAVING n >= 2").fetchall()
    for row in tr:
        print(f"  {row['handle']}: funder {row['earliest_funder']} shared by {row['n']} candidates")
    if not tr:
        print("  none")


if __name__ == "__main__":
    main()
