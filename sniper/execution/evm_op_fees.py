"""OP-stack transaction fees from chain evidence (Ink plan Task 8, 2026-09-29).

A funded OP-stack transaction pays three fees in native ETH, all read from the chain:

  L2 execution   gasUsed x effectiveGasPrice                        (receipt)
  L1 data        l1Fee                                              (receipt, OP extension)
  operator fee   GasPriceOracle.getOperatorFee(gasUsed) at the transaction's own block

The operator fee comes from the oracle, not the receipt. Live Ink receipts (2026-09-29) carry no
operatorFeeScalar/operatorFeeConstant fields, and the formula changed at the Jovian fork. The
oracle applies the formula that was in force at that block. On Ink both forks are active and the
oracle answers 0 today: a zero that was READ, not assumed.

Every amount is an exact integer in wei. A missing receipt field or a failed read is UNKNOWN,
never 0: `status` names what is missing, and the total stays None.
"""
from __future__ import annotations

OP_STACK_FEE_CHAINS = frozenset({"57073"})
# Ethereum mainnet pays ONE fee: gasUsed x effectiveGasPrice. No L1 data fee, no operator fee.
L1_FEE_CHAINS = frozenset({"1"})
GAS_PRICE_ORACLE = "0x420000000000000000000000000000000000000F"
SEL_GET_OPERATOR_FEE = "0x275aedd2"        # getOperatorFee(uint256)


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    try:
        text = str(value)
        out = int(text, 16) if text.startswith("0x") else int(text)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def operator_fee_wei(gas_used, block_number, rpc_url, *, http=None):
    """The operator fee the oracle charged `gas_used` at `block_number`, or None (unknown)."""
    from sniper.execution.evm_wallet import _rpc
    if _int(gas_used) is None or _int(block_number) is None:
        return None
    try:
        answer = _rpc(rpc_url, "eth_call",
                      [{"to": GAS_PRICE_ORACLE,
                        "data": SEL_GET_OPERATOR_FEE + format(_int(gas_used), "064x")},
                       hex(_int(block_number))], http=http)
    except Exception:
        return None
    result = (answer or {}).get("result")
    if not isinstance(result, str) or not result.startswith("0x") or len(result) != 66:
        return None
    return int(result, 16)


def tx_fee(receipt, *, operator_wei):
    """{l2_execution_wei, l1_data_wei, operator_wei, total_wei, status}: decimal strings or None."""
    receipt = receipt if isinstance(receipt, dict) else {}
    gas, price = _int(receipt.get("gasUsed")), _int(receipt.get("effectiveGasPrice"))
    parts = {"l2_execution": gas * price if gas is not None and price is not None else None,
             "l1_data": _int(receipt.get("l1Fee")),
             "operator": _int(operator_wei) if operator_wei is not None else None}
    missing = [name for name, value in parts.items() if value is None]
    return {"l2_execution_wei": None if parts["l2_execution"] is None else str(parts["l2_execution"]),
            "l1_data_wei": None if parts["l1_data"] is None else str(parts["l1_data"]),
            "operator_wei": None if parts["operator"] is None else str(parts["operator"]),
            "total_wei": None if missing else str(sum(parts.values())),
            "status": "complete" if not missing else "unknown:" + ",".join(missing)}


def l1_tx_fee(receipt):
    """The fee of a mainnet (L1) transaction, in `tx_fee`'s shape. The execution fee is stored in
    `l2_execution_wei` (the column is the execution fee on every chain); `l1_data_wei` and
    `operator_wei` are None because those fees do not exist on L1, which is not "unknown": the
    status is `complete` when gasUsed and effectiveGasPrice were both read. A missing field is
    UNKNOWN, never 0."""
    receipt = receipt if isinstance(receipt, dict) else {}
    gas, price = _int(receipt.get("gasUsed")), _int(receipt.get("effectiveGasPrice"))
    total = gas * price if gas is not None and price is not None else None
    return {"l2_execution_wei": None if total is None else str(total), "l1_data_wei": None,
            "operator_wei": None, "total_wei": None if total is None else str(total),
            "status": "complete" if total is not None else "unknown:execution"}


def read_l1_tx_fee(tx_hash, rpc_url, *, http=None):
    """The fee of one L1 transaction from its CANONICAL receipt, or None when the receipt is absent
    or not canonical yet (nothing is recorded, and a later pass asks again)."""
    from sniper.execution.evm_real_seams import _canonical_receipt_on_rpc
    from sniper.execution.evm_wallet import _rpc
    try:
        receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result")
    except Exception:
        return None
    if not isinstance(receipt, dict) or not _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url,
                                                                        http=http):
        return None
    return {**l1_tx_fee(receipt), "block_number": _int(receipt.get("blockNumber"))}


def read_tx_fee(tx_hash, rpc_url, *, http=None):
    """The fee of one transaction, from its CANONICAL receipt plus the oracle at its block. None
    when the receipt is absent or not canonical yet: nothing is recorded, and a later pass asks
    again."""
    from sniper.execution.evm_real_seams import _canonical_receipt_on_rpc
    from sniper.execution.evm_wallet import _rpc
    try:
        receipt = _rpc(rpc_url, "eth_getTransactionReceipt", [tx_hash], http=http).get("result")
    except Exception:
        return None
    if not isinstance(receipt, dict) or not _canonical_receipt_on_rpc(receipt, tx_hash, rpc_url,
                                                                        http=http):
        return None
    block = _int(receipt.get("blockNumber"))
    fee = tx_fee(receipt, operator_wei=operator_fee_wei(receipt.get("gasUsed"), block, rpc_url,
                                                        http=http))
    return {**fee, "block_number": block}


def reconcile_fees(conn, network_id, fee_fn, *, limit=20, now_iso=None):
    """Record the fee of every funded transaction on `network_id` that has none yet (fills with a
    tx hash), at most `limit` per pass. Off the fill path: it reads receipts of transactions
    already booked. Returns {recorded, unknown, pending}."""
    from sniper.persistence import db_memecoin as m
    out = {"recorded": 0, "unknown": 0, "pending": 0}
    for tx_hash, kind in m.evm_fee_backlog(conn, network_id, limit=limit):
        fee = fee_fn(tx_hash)
        if fee is None:
            out["pending"] += 1
            continue
        m.record_evm_tx_fee(conn, network_id=network_id, tx_hash=tx_hash, kind=kind, fee=fee,
                            recorded_at=now_iso)
        out["recorded" if fee["status"] == "complete" else "unknown"] += 1
    return out
