"""Pure, budget-transparent recovery of an EVM source observation.

Some read providers have full blocks and logs but no receipt index. Committed
swap logs prove successful execution; the returned observation deliberately
retains its provenance and is not presented as a provider receipt. The flow
collector still validates the venue, quantities and buy direction.
"""
from __future__ import annotations

from copy import deepcopy
import re

from sniper.services.wallet_universe_flow import SWAP_TOPICS

_LABEL = "canonical_full_block_and_logs"
_HASH_LABEL = "canonical_block_transaction_and_logs"
SOURCE_VIEW_VERSION = "wallet-universe-source-view-v2"


def _hex(value, size, field):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{%d}" % (size * 2), value):
        raise ValueError("source_invalid_" + field)
    return value.lower()


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("source_invalid_" + field)
    if isinstance(value, str) and not re.fullmatch(r"(?:0x[0-9a-fA-F]+|0|[1-9][0-9]*)", value):
        raise ValueError("source_invalid_" + field)
    number = int(value, 16 if isinstance(value, str) and value.startswith("0x") else 10) if isinstance(value, str) else value
    if number < 0:
        raise ValueError("source_invalid_" + field)
    return number


def _recipient(transaction):
    # Explicit JSON null is canonical contract creation; an absent key is a
    # partial response. Neither may be replaced with the measured beneficiary.
    if "to" not in transaction:
        raise ValueError("source_missing_recipient")
    return None if transaction["to"] is None else _hex(transaction["to"], 20, "recipient")


def _source_body(transaction, signature):
    """Validate a confirmed transaction body without asserting successful execution."""
    if not isinstance(transaction, dict):
        raise ValueError("source_missing_transaction_body")
    if _hex(transaction.get("hash"), 32, "transaction") != signature:
        raise ValueError("source_transaction_identity_mismatch")
    data = transaction.get("input")
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data):
        raise ValueError("source_invalid_transaction_input")
    return dict(blockNumber=_number(transaction.get("blockNumber"), "transaction_block_number"),
                blockHash=_hex(transaction.get("blockHash"), 32, "transaction_block_hash"),
                transactionIndex=_number(transaction.get("transactionIndex"), "transaction_index"),
                **{"from": _hex(transaction.get("from"), 20, "sender")},
                to=_recipient(transaction), input=data.lower())


def resolve_source_clock(unit, call):
    """Resolve a provisional event clock, never a mint-buy or flow proof.

    Solana supplies an exact successful transaction header. An EVM slot supplies
    only a canonical block header until the collector independently binds the
    source transaction to that block. All requests use the caller's read budget.
    """
    if not isinstance(unit, dict):
        raise ValueError("source_invalid_unit")
    chain, signature, slot = unit.get("chain"), unit.get("signature"), unit.get("source_slot")
    if not isinstance(signature, str) or not signature:
        raise ValueError("source_invalid_signature")
    if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0):
        raise ValueError("source_invalid_slot")
    result = dict(method="wallet-universe-source-clock-v1", chain=chain, signature=signature)
    if chain == "solana":
        transaction = call("getTransaction", [signature, dict(
            encoding="json", commitment="confirmed", maxSupportedTransactionVersion=1)])
        if not isinstance(transaction, dict):
            raise ValueError("source_missing_transaction")
        meta, body = transaction.get("meta"), transaction.get("transaction")
        if not isinstance(meta, dict) or "err" not in meta or meta["err"] is not None:
            raise ValueError("source_unsuccessful_transaction")
        signatures = body.get("signatures") if isinstance(body, dict) else None
        if (not isinstance(signatures, list) or any(not isinstance(value, str) or not value for value in signatures)
                or signatures.count(signature) != 1 or len(set(signatures)) != len(signatures)):
            raise ValueError("source_wrong_signature")
        observed_slot, timestamp = transaction.get("slot"), transaction.get("blockTime")
        if isinstance(observed_slot, bool) or not isinstance(observed_slot, int) or observed_slot < 0:
            raise ValueError("source_invalid_slot")
        if slot is not None and slot != observed_slot:
            raise ValueError("source_wrong_slot")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
            raise ValueError("source_invalid_time")
        result.update(t0=timestamp, source_slot=observed_slot, observed_from="successful_transaction_header")
    else:
        if not isinstance(chain, str) or _number(chain, "chain") < 1:
            raise ValueError("source_invalid_chain")
        signature = _hex(signature, 32, "signature")
        result["signature"] = signature
        receipt_hash = None
        if slot is None:
            try:
                receipt = call("eth_getTransactionReceipt", [signature])
            except ValueError as exc:
                if str(exc) != "missing_result":
                    raise
                transaction = _source_body(call("eth_getTransactionByHash", [signature]), signature)
                slot, receipt_hash = transaction["blockNumber"], transaction["blockHash"]
            else:
                if not isinstance(receipt, dict):
                    raise ValueError("source_missing_receipt")
                if (_hex(receipt.get("transactionHash"), 32, "signature") != signature
                        or _number(receipt.get("status"), "status") != 1):
                    raise ValueError("source_wrong_receipt")
                slot = _number(receipt.get("blockNumber"), "slot")
                receipt_hash = _hex(receipt.get("blockHash"), 32, "block_hash")
        header = call("eth_getBlockByNumber", [hex(slot), False])
        if not isinstance(header, dict):
            raise ValueError("source_missing_header")
        if _number(header.get("number"), "slot") != slot:
            raise ValueError("source_wrong_slot")
        block_hash = _hex(header.get("hash"), 32, "block_hash")
        if receipt_hash is not None and receipt_hash != block_hash:
            raise ValueError("source_reorg")
        timestamp = _number(header.get("timestamp"), "time")
        if timestamp <= 0:
            raise ValueError("source_invalid_time")
        result.update(t0=timestamp, source_slot=slot, block_hash=block_hash,
                      observed_from="canonical_block_header")
    return result


class SourceRead:
    """Wrap one unit's injected reader, with an optional persisted observation.

    ``cached`` returns a detached JSON-compatible derived observation, or None.
    A restored observation is bound to the exact measurement unit and checked
    against the current canonical header on its first use. The transaction's
    sender may be a relay; beneficiary attribution belongs to the collector.
    Every actual read goes through ``call`` and its budget.
    """

    def __init__(self, unit, call, cached=None):
        self.unit = deepcopy(unit)
        self.call = call
        self._cached = deepcopy(cached)
        # Unversioned views omitted the recipient. Rebuild their evidence via
        # the same budgeted reader instead of repeatedly returning a bad cache.
        if isinstance(self._cached, dict) and "source_view_version" not in self._cached:
            self._cached = None
        self._memo = None

    @property
    def cached(self):
        return deepcopy(self._cached)

    def __call__(self, method, params):
        signature = self.unit.get("signature")
        exact = (method == "eth_getTransactionReceipt" and self.unit.get("chain") != "solana"
                 and isinstance(signature, str) and isinstance(params, (list, tuple))
                 and len(params) == 1 and isinstance(params[0], str)
                 and params[0].lower() == signature.lower())
        if not exact:
            return self.call(method, params)
        slot = self.unit.get("source_slot")
        if slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0):
            raise ValueError("source_invalid_slot")
        if self._memo is not None:
            return deepcopy(self._memo)
        if self._cached is not None:
            self._identity()
            self._validate_observation(self._cached)
            try:
                self._canonical(self._cached["blockHash"])
            except ValueError as exc:
                if str(exc) != "missing_result" or self.unit.get("source_slot") is not None:
                    raise
                # A per-unit archive reader cannot trust a cached slot alone.
                # An independent exact-source lookup grants header recovery;
                # its identity must also match the previously proved view.
                transaction = _source_body(self.call("eth_getTransactionByHash", [self.signature]), self.signature)
                if (transaction["blockNumber"] != self.slot
                        or transaction["blockHash"] != _hex(self._cached["blockHash"], 32, "block_hash")
                        or transaction["transactionIndex"] != _number(self._cached["transactionIndex"], "transaction_index")
                        or transaction["from"] != _hex(self._cached["from"], 20, "sender")
                        or transaction["to"] != _recipient(self._cached)):
                    raise ValueError("source_cached_identity_mismatch")
                self._canonical(self._cached["blockHash"])
            self._memo = deepcopy(self._cached)
            return deepcopy(self._memo)
        try:
            result = self.call(method, params)
        except ValueError as exc:
            if str(exc) != "missing_result":
                raise
            self._identity()
            result = self._recover()
            self._cached = deepcopy(result)
        self._memo = deepcopy(result)
        return deepcopy(result)

    def _identity(self):
        slot = self.unit.get("source_slot")
        self.slot = slot
        self.signature = _hex(self.unit.get("signature"), 32, "signature")
        _hex(self.unit.get("address"), 20, "address")
        _hex(self.unit.get("mint"), 20, "mint")
        if not isinstance(self.unit.get("uid"), str) or not self.unit["uid"]:
            raise ValueError("source_invalid_uid")
        if not isinstance(self.unit.get("chain"), str) or _number(self.unit["chain"], "chain") < 1:
            raise ValueError("source_invalid_chain")
        self.timestamp = _number(self.unit.get("t0"), "time")
        self.binding = {field: deepcopy(self.unit.get(field)) for field in
                        ("chain", "uid", "address", "mint", "signature", "source_slot", "t0")}

    def _header(self, block):
        if not isinstance(block, dict):
            raise ValueError("source_invalid_block")
        if _number(block.get("number"), "block_number") != self.slot:
            raise ValueError("source_wrong_slot")
        if _number(block.get("timestamp"), "block_time") != self.timestamp:
            raise ValueError("source_wrong_time")
        return _hex(block.get("hash"), 32, "block_hash")

    def _canonical(self, expected):
        block_hash = self._header(self.call("eth_getBlockByNumber", [hex(self.slot), False]))
        if block_hash != expected:
            raise ValueError("source_reorg")

    def _logs(self, logs, block_hash, index, *, source_only=False):
        if not isinstance(logs, list):
            raise ValueError("source_invalid_logs")
        found, seen, swap = [], set(), False
        for log in logs:
            if not isinstance(log, dict):
                raise ValueError("source_invalid_log")
            tx_hash = _hex(log.get("transactionHash"), 32, "log_transaction")
            if tx_hash != self.signature:
                if source_only:
                    raise ValueError("source_foreign_log")
                continue
            if (_hex(log.get("blockHash"), 32, "log_block") != block_hash
                    or _number(log.get("blockNumber"), "log_block_number") != self.slot
                    or _number(log.get("transactionIndex"), "log_transaction_index") != index
                    or log.get("removed") is not False):
                raise ValueError("source_wrong_log_anchor")
            log_index = _number(log.get("logIndex"), "log_index")
            if log_index in seen:
                raise ValueError("source_duplicate_log")
            seen.add(log_index)
            _hex(log.get("address"), 20, "log_address")
            topics = log.get("topics")
            if not isinstance(topics, list) or len(topics) > 4:
                raise ValueError("source_invalid_topics")
            topics = [_hex(topic, 32, "topic") for topic in topics]
            data = log.get("data")
            if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", data):
                raise ValueError("source_invalid_log_data")
            swap |= bool(topics and topics[0] in SWAP_TOPICS)
            found.append(deepcopy(log))
        if not found or not swap:
            raise ValueError("source_missing_swap_logs")
        return sorted(found, key=lambda row: _number(row["logIndex"], "log_index"))

    def _validate_observation(self, observation):
        if not isinstance(observation, dict) or observation.get("observation_source") not in (_LABEL, _HASH_LABEL):
            raise ValueError("source_invalid_cached_observation")
        if observation.get("source_view_version") != SOURCE_VIEW_VERSION:
            raise ValueError("source_invalid_cached_version")
        if (observation.get("source_unit_binding") != self.binding
                or _hex(observation.get("transactionHash"), 32, "transaction") != self.signature
                or observation.get("status") != "0x1"):
            raise ValueError("source_cached_identity_mismatch")
        observed_slot = _number(observation.get("blockNumber"), "block_number")
        if self.slot is not None and observed_slot != self.slot:
            raise ValueError("source_cached_identity_mismatch")
        self.slot = observed_slot
        _hex(observation.get("from"), 20, "sender")
        _recipient(observation)
        block_hash = _hex(observation.get("blockHash"), 32, "block_hash")
        index = _number(observation.get("transactionIndex"), "transaction_index")
        self._logs(observation.get("logs"), block_hash, index, source_only=True)

    def _recover(self):
        prefetched = None
        if self.slot is None:
            prefetched = _source_body(self.call("eth_getTransactionByHash", [self.signature]), self.signature)
            self.slot = prefetched["blockNumber"]
        block = self.call("eth_getBlockByNumber", [hex(self.slot), True])
        block_hash = self._header(block)
        transactions = block.get("transactions")
        if not isinstance(transactions, list) or not transactions:
            raise ValueError("source_missing_full_transactions")
        hash_only = all(isinstance(tx, str) for tx in transactions)
        matched, hashes = [], set()
        for index, tx in enumerate(transactions):
            if not hash_only and not isinstance(tx, dict):
                raise ValueError("source_missing_full_transactions")
            tx_hash = _hex(tx if hash_only else tx.get("hash"), 32, "transaction")
            if tx_hash in hashes:
                raise ValueError("source_duplicate_transaction")
            hashes.add(tx_hash)
            if hash_only:
                if tx_hash == self.signature:
                    matched.append(index)
                continue
            if (_number(tx.get("transactionIndex"), "transaction_index") != index
                    or _number(tx.get("blockNumber"), "transaction_block_number") != self.slot
                    or _hex(tx.get("blockHash"), 32, "transaction_block_hash") != block_hash):
                raise ValueError("source_transaction_identity_mismatch")
            sender = _hex(tx.get("from"), 20, "sender")
            if tx_hash != self.signature:
                continue
            matched.append((index, sender, _recipient(tx), tx))
        if len(matched) != 1:
            raise ValueError("source_missing_transaction")
        if hash_only:
            index = matched[0]
            transaction = prefetched or _source_body(
                self.call("eth_getTransactionByHash", [self.signature]), self.signature)
            if (transaction["blockNumber"] != self.slot or transaction["blockHash"] != block_hash
                    or transaction["transactionIndex"] != index):
                raise ValueError("source_transaction_identity_mismatch")
            sender, recipient = transaction["from"], transaction["to"]
        else:
            index, sender, recipient, transaction = matched[0]
            if prefetched is not None and _source_body(transaction, self.signature) != prefetched:
                raise ValueError("source_transaction_identity_mismatch")
        logs = self._logs(self.call("eth_getLogs", [{"blockHash": block_hash}]), block_hash, index)
        self._canonical(block_hash)
        return dict(transactionHash=self.signature, blockNumber=hex(self.slot), blockHash=block_hash,
                    transactionIndex=hex(index), **{"from": sender}, to=recipient, logs=logs, status="0x1",
                    observation_source=_HASH_LABEL if hash_only else _LABEL,
                    source_view_version=SOURCE_VIEW_VERSION,
                    source_unit_binding=deepcopy(self.binding))
