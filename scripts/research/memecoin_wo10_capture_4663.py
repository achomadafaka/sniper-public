#!/usr/bin/env python3
"""WO-10 — capture REAL funded-chain-4663 Uniswap-V3 event fixtures (read-only, one-shot).

Reproducible provenance for tests/fixtures/memecoin/evm/4663/*.json. For a canonical funded-4663 V3
pool (resolved by the SAME resolver the hot + pull lanes use), it fetches one real Swap / Mint / Burn
/ Collect via bounded read-only RPC (eth_getTransactionReceipt + eth_getBlockByNumber), taking each
raw log from the transaction RECEIPT (the authoritative source that ties log -> tx -> block) and the
block HEADER (proves block hash + timestamp + that the tx is in the block). No URL, key, or credential
is ever written to a fixture — only a provider LABEL.

Read-only: eth_* reads only. Never signs, broadcasts, or touches funded state. RPC URL comes from the
operator env (MEMECOIN_EVM_RPC_URL_4663) and is never printed or persisted.
"""
import hashlib
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from sniper.util.envfiles import load_env_files
from sniper.util.state_paths import state_root

load_env_files(str(state_root()))

from sniper.execution.evm_wallet import _rpc
from sniper.services.memecoin_tick_stream import V3_BURN, V3_COLLECT, V3_MINT, V3_SWAP
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso

CHAIN = "4663"
POOL = "0x5d25149b5710f4df0a485f075668e1649c4bb2f9"   # funded-4663 V3 pool (pid 9276), canonical resolver
PROVIDER_LABEL = "operator_read_rpc_4663"             # LABEL only — never the URL
FIXTURE_DIR = os.path.join(_ROOT, "tests", "fixtures", "memecoin", "evm", CHAIN)

# (event_kind, canonical topic, target tx hash, target log index) — discovered via bounded probe on
# 2026-07-25; final blocks, so re-running is deterministic.
TARGETS = [
    # swap tick 138848 is OUT of the burn/mint range (139400+) -> out-of-range severity case
    ("swap", V3_SWAP, "0xf905ea5bae2b4417d02a5f318746a75e95c12289a9d3a3dce7dd39c17b02810e", 118),
    # swap tick 139548 is IN the burn range [139400,140200) and mint range -> in-range severity case
    ("swap_inrange", V3_SWAP, "0xeb5db28f52ca85687de979490d5263c16aa7fc5ed8c120f4c83731a6f7c82e85", 18),
    ("mint", V3_MINT, "0xd7f522cbbb5a80e45ee90d27f8cf28cfc076da95deba1116963e40e8c292549a", 26),
    ("burn", V3_BURN, "0xa3216f2123ec63415c848364501f925ce6532e0c193e174137ee47f30e7aeb86", 19),
    ("collect", V3_COLLECT, "0xa3216f2123ec63415c848364501f925ce6532e0c193e174137ee47f30e7aeb86", 23),
]


def _call(rpc, method, params):
    r = _rpc(rpc, method, params)
    if not isinstance(r, dict) or "result" not in r or r["result"] is None:
        raise RuntimeError(f"{method} returned no result")
    return r["result"]


def _sha256_canonical(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def capture_one(rpc, kind, topic, tx_hash, log_index):
    receipt = _call(rpc, "eth_getTransactionReceipt", [tx_hash])
    block_no = int(receipt["blockNumber"], 16)
    header = _call(rpc, "eth_getBlockByNumber", [hex(block_no), False])
    logs = [lg for lg in receipt.get("logs", []) if int(lg["logIndex"], 16) == log_index]
    if len(logs) != 1:
        raise RuntimeError(f"{kind}: log_index {log_index} not unique in receipt")
    log = logs[0]
    # cross-identity proofs — the fixture is only trustworthy if these hold at capture time
    assert (log["topics"][0] or "").lower() == topic.lower(), f"{kind}: topic mismatch"
    assert log["address"].lower() == POOL.lower(), f"{kind}: pool address mismatch"
    assert log["transactionHash"].lower() == tx_hash.lower(), f"{kind}: tx mismatch"
    assert log["blockHash"].lower() == receipt["blockHash"].lower() == header["hash"].lower(), \
        f"{kind}: block hash mismatch (log/receipt/header)"
    assert any((t or "").lower() == tx_hash.lower() for t in header.get("transactions", [])), \
        f"{kind}: tx not in block header"
    return {
        "chain_id": CHAIN,
        "pool": POOL,
        "event_kind": kind,
        "expected_topic": topic,
        "log": {
            "address": log["address"],
            "topics": log["topics"],
            "data": log["data"],
            "blockNumber": log["blockNumber"],
            "blockHash": log["blockHash"],
            "transactionHash": log["transactionHash"],
            "transactionIndex": log.get("transactionIndex"),
            "logIndex": log["logIndex"],
            "removed": log.get("removed", False),
        },
        "receipt_identity": {
            "transactionHash": receipt["transactionHash"],
            "blockNumber": receipt["blockNumber"],
            "blockHash": receipt["blockHash"],
            "transactionIndex": receipt.get("transactionIndex"),
            "status": receipt.get("status"),
            "log_count": len(receipt.get("logs", [])),
        },
        "block_header": {
            "number": header["number"],
            "hash": header["hash"],
            "parentHash": header["parentHash"],
            "timestamp": header["timestamp"],
            "tx_count": len(header.get("transactions", [])),
        },
        "provider_label": PROVIDER_LABEL,
        "capture_time": real_utc_now_iso(),
        "payload_sha256": _sha256_canonical({
            "address": log["address"], "topics": log["topics"], "data": log["data"],
            "blockNumber": log["blockNumber"], "blockHash": log["blockHash"],
            "transactionHash": log["transactionHash"], "logIndex": log["logIndex"]}),
    }


def main():
    rpc = cfg.evm_rpc_for_chain(CHAIN)
    if not rpc:
        raise SystemExit("no 4663 RPC configured")
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    for kind, topic, tx_hash, log_index in TARGETS:
        fixture = capture_one(rpc, kind, topic, tx_hash, log_index)
        path = os.path.join(FIXTURE_DIR, f"v3_{kind}.json")
        with open(path, "w") as fh:
            json.dump(fixture, fh, indent=2, sort_keys=True)
        print(f"wrote {kind}: block={int(fixture['log']['blockNumber'],16)} "
              f"tx={fixture['log']['transactionHash']} logIndex={log_index} "
              f"sha256={fixture['payload_sha256'][:16]}...")


if __name__ == "__main__":
    main()
