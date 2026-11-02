"""Minimal on-chain redemption executor for resolved real positions.

This module is intentionally dependency-lazy:
- Importing it does not require web3/eth-account.
- Heavy deps load only when live redemption is explicitly armed.
"""

from __future__ import annotations

import os
from typing import Any, Dict


POLYGON_CHAIN_ID = int(os.getenv("POLYGON_CHAIN_ID", "137"))
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
CTF_CONTRACT = os.getenv("CTF_CONTRACT", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
USDC_POLYGON = os.getenv("USDC_CONTRACT", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
ZERO_BYTES32 = "0x" + "00" * 32

CTF_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "collateralToken", "type": "address"},
            {"internalType": "bytes32", "name": "parentCollectionId", "type": "bytes32"},
            {"internalType": "bytes32", "name": "conditionId", "type": "bytes32"},
            {"internalType": "uint256[]", "name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def redeem_condition_position(
    *,
    wallet: str,
    condition_id: str,
    outcome: str,
    private_key: str,
    wait_for_receipt: bool = True,
) -> Dict[str, Any]:
    """Submit CTF redeemPositions for a single condition.

    Returns dict with tx_hash and optional receipt metadata.
    """
    from eth_account import Account
    from web3 import Web3

    if not private_key:
        raise RuntimeError("Missing private key for live redemption")

    wallet_norm = str(wallet or "").strip().lower()
    if not wallet_norm:
        raise RuntimeError("wallet is required")

    account = Account.from_key(private_key)
    signer = str(account.address or "").strip().lower()
    if signer != wallet_norm:
        raise RuntimeError(f"Signer mismatch: key derives to {account.address}, expected {wallet}")

    condition_hex = str(condition_id or "").strip().lower()
    if condition_hex.startswith("0x"):
        condition_hex = condition_hex[2:]
    if len(condition_hex) != 64:
        raise RuntimeError(f"Invalid condition_id bytes32: {condition_id}")

    # CTF redemption for this condition. Index set is intentionally broad to
    # mirror the safe CLI behavior and avoid outcome-index mismatches.
    index_sets = [1, 2]

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError(f"Failed to connect to Polygon RPC: {POLYGON_RPC_URL}")

    ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI)
    nonce = w3.eth.get_transaction_count(account.address)
    fn = ctf.functions.redeemPositions(
        w3.to_checksum_address(USDC_POLYGON),
        bytes.fromhex(ZERO_BYTES32[2:]),
        bytes.fromhex(condition_hex),
        index_sets,
    )

    gas_estimate = int(fn.estimate_gas({"from": account.address, "nonce": nonce, "chainId": POLYGON_CHAIN_ID}))
    gas_price = int(w3.eth.gas_price)
    tx = fn.build_transaction(
        {
            "from": account.address,
            "nonce": nonce,
            "chainId": POLYGON_CHAIN_ID,
            "gas": int(gas_estimate * 1.2),
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": max(w3.to_wei(30, "gwei"), gas_price),
        }
    )
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    payload: Dict[str, Any] = {
        "tx_hash": tx_hash.hex(),
        "nonce": nonce,
        "chain_id": POLYGON_CHAIN_ID,
        "gas_estimate": gas_estimate,
    }
    if wait_for_receipt:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
        payload["receipt_status"] = int(receipt.status or 0)
        payload["gas_used"] = int(receipt.gasUsed or 0)
    return payload
