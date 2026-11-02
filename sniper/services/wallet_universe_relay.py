"""Injected read-only historical Relay destination-beneficiary evidence.

This proves a paid destination purchase, never the origin wallet or funded
authority. Third-party-funded gifts remain possible. The caller first verifies
the current canonical receipt block/time and a registered pool quote-in buy.
This helper binds the exact unit, raw transaction and all receipt log anchors,
then reuses only the watcher's pure envelope classifier. It verifies proxy/router
bytecode at that receipt block; empty/pruned code stays unknown, without a head
fallback. Timeout, rate limit and budget errors propagate through the injected
reader. Completed reads survive in caller-owned JSON state; cached booleans,
changed anchors, obsolete versions and altered proof fields cannot grant proof.
There are no network calls, database writes, global caches or execution actions.
"""
from __future__ import annotations

from copy import deepcopy
import re

from eth_utils import keccak
from sniper.services.memecoin_evm_onchain_watch import relayer_purchase_from_receipt

VERSION = "wallet-universe-relay-beneficiary-v1"
PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
# Pure pinned protocol data, matching evm_relay_quote without its live code reader.
CODE_HASHES = ((PROXY,"0xa3b36a6aa3c2fb588b92336d88b0735b40dea0282c110d2034edf688ca661b5b"),
               (ROUTER,"0xde894e5c12e9513d50613c8fff375ecbf19b5d9a53bec0662e2b9667e3ec8f15"))


class RelayResearchError(ValueError):
    def __init__(self,reason,*,retryable=False):
        self.reason=reason
        self.retryable=retryable
        super().__init__(reason)


def _fail(reason,*,retryable=False):
    raise RelayResearchError(reason,retryable=retryable)


def _hex(value,size):
    if not isinstance(value,str) or not re.fullmatch(r"0x[0-9a-fA-F]{"+str(size*2)+r"}",value):
        _fail("research_relay_invalid_identity")
    return value.lower()


def _number(value):
    if type(value) is int and value>=0:
        return value
    if isinstance(value,str) and re.fullmatch(r"(?:0|[1-9]\d*|0x[0-9a-fA-F]+)",value):
        return int(value,16 if value.startswith("0x") else 10)
    _fail("research_relay_invalid_number")


def _binding(unit,receipt):
    if not isinstance(unit,dict) or not isinstance(receipt,dict):
        _fail("research_relay_missing_identity")
    chain=unit.get("chain")
    if chain not in ("4663","56","8453") or unit.get("source_initiation") is not None:
        _fail("research_relay_unit_not_eligible")
    uid=unit.get("uid")
    if not isinstance(uid,str) or not uid:
        _fail("research_relay_missing_uid")
    identity=dict(chain=chain,uid=uid,address=_hex(unit.get("address"),20),
        mint=_hex(unit.get("mint"),20),signature=_hex(unit.get("signature"),32),
        t0=_number(unit.get("t0")),source_slot=unit.get("source_slot"))
    block=_number(receipt.get("blockNumber"));index=_number(receipt.get("transactionIndex"))
    if (identity["signature"]!=_hex(receipt.get("transactionHash"),32)
            or _number(receipt.get("status"))!=1
            or identity["source_slot"] is not None and _number(identity["source_slot"])!=block):
        _fail("research_relay_receipt_identity_mismatch")
    sender=_hex(receipt.get("from"),20);target=_hex(receipt.get("to"),20)
    if sender==identity["address"] or target!=PROXY:
        _fail("research_relay_not_proxy_purchase")
    block_hash=_hex(receipt.get("blockHash"),32)
    logs=receipt.get("logs")
    if not isinstance(logs,list):
        _fail("research_relay_missing_logs")
    seen=set()
    for log in logs:
        if (not isinstance(log,dict) or log.get("removed") is not False
                or _hex(log.get("transactionHash"),32)!=identity["signature"]
                or _hex(log.get("blockHash"),32)!=block_hash
                or _number(log.get("blockNumber"))!=block
                or _number(log.get("transactionIndex"))!=index):
            _fail("research_relay_log_anchor_mismatch")
        li=_number(log.get("logIndex"))
        if li in seen:
            _fail("research_relay_duplicate_log")
        seen.add(li)
    return dict(unit=identity,receipt=dict(block_number=block,block_hash=block_hash,
        transaction_index=index,sender=sender,target=target))


def _transaction(transaction,binding):
    if not isinstance(transaction,dict):
        _fail("research_relay_transaction_unavailable",retryable=True)
    unit,receipt=binding["unit"],binding["receipt"]
    if (_hex(transaction.get("hash"),32)!=unit["signature"]
            or _number(transaction.get("chainId"))!=int(unit["chain"])
            or _number(transaction.get("blockNumber"))!=receipt["block_number"]
            or _hex(transaction.get("blockHash"),32)!=receipt["block_hash"]
            or _number(transaction.get("transactionIndex"))!=receipt["transaction_index"]
            or _hex(transaction.get("from"),20)!=receipt["sender"]
            or _hex(transaction.get("to"),20)!=receipt["target"]
            or _number(transaction.get("value"))!=0):
        _fail("research_relay_transaction_identity_mismatch")
    raw=transaction.get("input")
    if not isinstance(raw,str) or not re.fullmatch(r"0x0a2b8f36(?:[0-9a-fA-F]{2})+",raw):
        _fail("research_relay_envelope_unproved")


def _code_hash(code):
    if code in (None,"","0x"):
        _fail("research_relay_historical_code_unavailable",retryable=True)
    if not isinstance(code,str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})+",code):
        _fail("research_relay_malformed_code")
    raw=bytes.fromhex(code[2:])
    if not any(raw):
        _fail("research_relay_historical_code_unavailable",retryable=True)
    return "0x"+keccak(raw).hex()


def recover_beneficiary(unit,receipt,call,*,state=None):
    """Return typed beneficiary-only evidence, with durable caller-owned reads.

    ``call`` receives only exact transaction and exact receipt-block code reads.
    The flow collector always revalidates the canonical header, source receipt,
    net target transfer and pool buy; this helper recomputes envelope attribution
    from that fresh receipt even when its raw transaction/code reads are cached.
    """
    binding=_binding(unit,receipt)
    if state is None:
        state={}
    if not isinstance(state,dict):
        _fail("research_relay_invalid_cache")
    if state:
        if state.get("version")!=VERSION or state.get("binding")!=binding:
            _fail("research_relay_cache_identity_mismatch")
    else:
        state.update(version=VERSION,binding=deepcopy(binding),code={})
    transaction=state.get("transaction")
    cached_transaction=transaction is not None
    if transaction is None:
        transaction=call("eth_getTransactionByHash",[binding["unit"]["signature"]])
        _transaction(transaction,binding)
        state["transaction"]=deepcopy(transaction)
        state["transaction_input_hash"]="0x"+keccak(bytes.fromhex(transaction["input"][2:])).hex()
    else:
        _transaction(transaction,binding)
        actual="0x"+keccak(bytes.fromhex(transaction["input"][2:])).hex()
        if state.get("transaction_input_hash")!=actual:
            _fail("research_relay_transaction_cache_mismatch")
    # Preliminary classifier is pure: contracts_verified is a provisional flag;
    # the proof is never returned until both actual exact-block hashes verify.
    decoded=relayer_purchase_from_receipt(receipt,transaction,binding["unit"]["address"],
        chain=binding["unit"]["chain"],contracts_verified=True,any_route=True)
    if (not isinstance(decoded,dict) or decoded.get("mint")!=binding["unit"]["mint"]
            or decoded.get("beneficiary")!=binding["unit"]["address"]
            or decoded.get("relayer")!=binding["receipt"]["sender"]):
        _fail("research_relay_envelope_unproved")
    code=state.get("code")
    if not isinstance(code,dict) or set(code)-set(dict(CODE_HASHES)):
        _fail("research_relay_invalid_code_cache")
    if (cached_transaction and binding["unit"]["source_slot"] is None
            and any(code.get(address) is None for address,_ in CODE_HASHES)):
        # A fresh per-unit archive reader cannot grant historical code reads
        # from cache alone. Reprove the exact transaction once so it learns the
        # source block; never widen the grant from our persisted slot hint.
        fresh=call("eth_getTransactionByHash",[binding["unit"]["signature"]])
        _transaction(fresh,binding)
        fresh_input_hash="0x"+keccak(bytes.fromhex(fresh["input"][2:])).hex()
        if fresh_input_hash!=state["transaction_input_hash"]:
            _fail("research_relay_transaction_cache_mismatch")
    hashes={}
    for address,expected in CODE_HASHES:
        value=code.get(address)
        if value is None:
            value=call("eth_getCode",[address,hex(binding["receipt"]["block_number"])])
        actual=_code_hash(value)
        if actual!=expected:
            _fail("research_relay_code_hash_mismatch")
        code[address]=value
        hashes[address]=actual
    initiation=dict(schema_version=1,proof_version=decoded["proof_version"],
        network_id=binding["unit"]["chain"],tx_hash=binding["unit"]["signature"],
        block_hash=binding["receipt"]["block_hash"],block_number=binding["receipt"]["block_number"],
        mint=decoded["mint"],beneficiary=decoded["beneficiary"],payer=decoded["payer"],
        source_user_id=binding["unit"]["uid"])
    proof=dict(version=VERSION,kind="research_relay_destination_purchase",binding=deepcopy(binding),
        code_basis="receipt_block",beneficiary_only=True,initiating_wallet_verified=False,
        third_party_payment_possible=True,origin_authority="unknown",code_hashes=hashes,
        transaction_input_hash=state["transaction_input_hash"],
        output_atomic=str(decoded["output_atomic"]),payment_atomic=str(decoded["payment_atomic"]),
        payment_token=decoded["payment_token"],source_initiation=initiation)
    if state.get("proof") is not None and state["proof"]!=proof:
        _fail("research_relay_cache_proof_mismatch")
    state["proof"]=deepcopy(proof)
    return proof
