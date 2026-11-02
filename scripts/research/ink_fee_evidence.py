#!/usr/bin/env python3
"""Does our OP-stack fee code equal what Ink actually charges? (Ink plan Task 8, fee_accounting.)

Read-only on Ink's official public RPC. For recent blocks, every non-deposit transaction whose sender
sent exactly one transaction in that block, with value 0 and an ERC-20 transfer/approve selector (no
native refunds can reach it): the chain's charge is balance(sender, n-1) - balance(sender, n). Ours is
evm_op_fees.read_tx_fee: gasUsed x effectiveGasPrice + l1Fee + the GasPriceOracle's operator fee at n.
Writes one evidence JSON; a mismatch is reported with both numbers, never dropped.

    python3 scripts/research/ink_fee_evidence.py [--blocks 40] [--out PATH]
"""
import argparse
import collections
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
URL = "https://rpc-gel.inkonchain.com"
ERC20_SELECTORS = ("0xa9059cbb", "0x095ea7b3")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--blocks", type=int, default=40)
    ap.add_argument("--out", default=str(ROOT / "docs/superpowers/research/2026-09-29-ink-evidence/"
                                         "fee-accounting-balance-delta.json"))
    args = ap.parse_args(argv)
    from sniper.execution import evm_op_fees as F
    from sniper.execution.evm_wallet import _rpc

    def call(method, params):
        answer = _rpc(URL, method, params)
        if "result" not in answer:
            raise RuntimeError("%s: %s" % (method, answer.get("error")))
        return answer["result"]

    head = int(call("eth_blockNumber", []), 16)
    checked, rows = collections.Counter(), []
    for n in range(head - args.blocks, head):
        block = call("eth_getBlockByNumber", [hex(n), True])
        senders = collections.Counter(tx["from"].lower() for tx in block["transactions"])
        for tx in block["transactions"]:
            if (tx.get("type") == "0x7e" or senders[tx["from"].lower()] != 1
                    or int(tx.get("value") or "0x0", 16) != 0
                    or not str(tx.get("input") or "").startswith(ERC20_SELECTORS)):
                continue
            fee = F.read_tx_fee(tx["hash"], URL)
            if fee is None or fee["status"] != "complete":
                checked["fee_unavailable"] += 1
                continue
            before = int(call("eth_getBalance", [tx["from"], hex(n - 1)]), 16)
            after = int(call("eth_getBalance", [tx["from"], hex(n)]), 16)
            charged = before - after
            ours = int(fee["total_wei"])
            checked["match" if charged == ours else "mismatch"] += 1
            rows.append({"block": n, "tx": tx["hash"], "charged_wei": str(charged), "ours_wei": str(ours),
                         "l2_execution_wei": fee["l2_execution_wei"], "l1_data_wei": fee["l1_data_wei"],
                         "operator_wei": fee["operator_wei"], "match": charged == ours})
            time.sleep(0.05)
    out = {"observed_at": datetime.now(timezone.utc).isoformat(), "rpc": URL, "chain_id": 57073,
           "blocks": [head - args.blocks, head - 1], "counts": dict(checked), "transactions": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
    print("blocks %d..%d  %s  -> %s" % (head - args.blocks, head - 1, dict(checked), args.out))
    return 0 if checked["mismatch"] == 0 and checked["match"] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
