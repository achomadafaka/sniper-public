"""Pure Ink Subblocks sequencer. Provisional observations never certify fills."""

import json

from eth_account import Account
from eth_utils import keccak


MAX_FRAME_BYTES = 4_000_000


def _result(status, observations=(), invalidated=()):
    return {"status": status, "observations": list(observations),
            "invalidated_payload_ids": list(invalidated)}


def _number(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not a block number")
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


class InkSubblockState:
    def __init__(self):
        self.payload_id = None
        self.block_number = None
        self.block_timestamp = None
        self.next_index = 0
        self.last_frame = None
        self.gapped = False

    def accept(self, frame: dict, *, received_mono_ns: int, received_utc: str) -> dict:
        try:
            if isinstance(frame, str):
                if len(frame.encode()) > MAX_FRAME_BYTES:
                    return _result("malformed")
                frame = json.loads(frame)
            if not isinstance(frame, dict) or len(json.dumps(frame).encode()) > MAX_FRAME_BYTES:
                return _result("malformed")
            payload = frame["payload_id"]
            index = frame["index"]
            diff = frame["diff"]
            meta = frame["metadata"]
            if not isinstance(payload, str) or not payload.startswith("0x") or not isinstance(index, int) or index < 0:
                return _result("malformed")
            if not isinstance(diff, dict) or not isinstance(meta, dict):
                return _result("malformed")
            block = _number(meta["block_number"])
            if index == 0:
                base = frame["base"]
                if not isinstance(base, dict) or _number(base["block_number"]) != block:
                    return _result("malformed")
            elif self.payload_id is None or self.gapped:
                return _result("waiting_base")
        except (KeyError, TypeError, ValueError, OverflowError):
            return _result("malformed")

        fingerprint = json.dumps(frame, sort_keys=True, separators=(",", ":"))
        if payload == self.payload_id and index == self.next_index - 1 and fingerprint == self.last_frame:
            return _result("accepted")
        if index != 0 and (payload != self.payload_id or block != self.block_number or index != self.next_index):
            self.gapped = True
            return _result("sequence_gap")
        if index == 0 and self.payload_id == payload and self.next_index > 0:
            self.gapped = True
            return _result("sequence_gap")

        invalidated = []
        status = "accepted"
        if index == 0:
            if self.payload_id and self.block_number == block and self.payload_id != payload:
                invalidated = [self.payload_id]
                status = "payload_replaced"
            self.payload_id, self.block_number = payload, block
            self.block_timestamp = _number(frame["base"]["timestamp"])
            self.gapped = False
        self.next_index = index + 1
        self.last_frame = fingerprint

        try:
            txs = diff.get("transactions") or []
            receipts = meta.get("receipts") or {}
            if not isinstance(txs, list) or not isinstance(receipts, dict):
                raise ValueError("invalid transaction or receipt container")
            observations = []
            for raw in txs:
                if not isinstance(raw, str) or not raw.startswith("0x"):
                    raise ValueError("invalid raw transaction")
                tx_hash = "0x" + keccak(hexstr=raw).hex()
                receipt = receipts.get(tx_hash)
                if not isinstance(receipt, dict):
                    raise ValueError("missing matching receipt")
                tx_type = _number(receipt.get("type", "0x0"))
                if tx_type == 0x7e or _number(receipt.get("status", "0x0")) != 1:
                    continue  # system deposit or failed transaction is not a candidate
                owner = Account.recover_transaction(raw)
                logs = receipt.get("logs")
                if not isinstance(logs, list):
                    raise ValueError("invalid provisional logs")
                observations.append({
                    "chain_id": "57073", "payload_id": payload, "subblock_index": index,
                    "block_number": block, "tx_hash": tx_hash, "owner": owner.lower(),
                    "raw_transaction": raw, "receipt": receipt,
                    "observation_key": f"57073:{payload}:{index}:{tx_hash}",
                    "state": "provisional", "canonical_block_hash": None,
                    "canonical_log_index": None, "received_monotonic_ns": received_mono_ns,
                    "received_utc": received_utc,
                    "source_time_basis": "block_timestamp",
                    "source_timestamp": self.block_timestamp,
                    "coverage_complete": True,
                })
        except Exception:
            self.gapped = True
            return _result("malformed", invalidated=invalidated)
        return _result(status, observations, invalidated)
