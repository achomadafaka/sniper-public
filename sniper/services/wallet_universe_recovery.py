"""Narrow archival source reads under one research attempt budget.

This facade owns two private, sequential ReadRpc-compatible readers. It is not
thread safe: child limits are capped around each call and restored afterward.
The caller must validate recovery endpoint isolation before supplying a lazy
factory. Both readers must use research pacing and the private book guard.
There is no networking, endpoint selection, or shared/funded pool access here.
"""
import re

# Protocol addresses pinned by evm_relay_quote; no execution-module import.
# The pure relay verifier still checks returned bytecode against its code hashes.
_RELAY_CODE_TARGETS = frozenset({
    "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be",
    "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f",
})


def _quantity(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return int(value, 16)
    return None


def _hash(value):
    return value.lower() if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", value) else None


class SharedResearchRead:
    """Primary-only by default; for_unit grants only exact BSC source recovery.

    ``used`` is the sum of actual child attempts, including failures and archive
    identity checks. Factories must construct a fresh reader without making any
    calls. The injected readers retain responsibility for method allowlists and
    incrementing their counters exactly at the actual attempt boundary.
    """

    def __init__(self, primary, recovery_factory, limit):
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError("invalid_budget")
        self.primary = primary
        self.recovery_factory = recovery_factory
        self.limit = limit
        self._recovery = None
        self._verified = False
        self._identity_error = None
        self._closed = False

    @property
    def used(self):
        return self.primary.used + (self._recovery.used if self._recovery is not None else 0)

    def _call(self, child, method, params):
        if self._closed:
            raise ValueError("reader_closed")
        remaining = self.limit - self.used
        if remaining <= 0:
            raise ValueError("budget")
        old_limit = child.limit
        child.limit = min(old_limit, child.used + remaining)
        try:
            return child(method, params)
        finally:
            child.limit = old_limit

    def __call__(self, method, params):
        return self._call(self.primary, method, params)

    def _archive(self):
        if self._closed:
            raise ValueError("reader_closed")
        if self._identity_error is not None:
            raise ValueError(self._identity_error)
        if self._recovery is None:
            if self.used >= self.limit:
                raise ValueError("budget")
            recovery = self.recovery_factory()
            if recovery is self.primary:
                raise ValueError("recovery_reader_not_isolated")
            self._recovery = recovery
        if not self._verified:
            try:
                if _quantity(self._call(self._recovery, "eth_chainId", [])) != 56:
                    raise ValueError("wrong_chain")
            except ValueError as exc:
                self._identity_error = str(exc)
                raise
            self._verified = True
        return self._recovery

    def for_unit(self, unit):
        """Return a closure with its own exact source signature and learned slot."""
        signature = _hash(unit.get("signature"))
        eligible = unit.get("chain") == "56" and signature is not None
        slot = unit.get("source_slot")
        malformed_slot = slot is not None and (isinstance(slot, bool) or not isinstance(slot, int) or slot < 0)
        eligible = eligible and not malformed_slot

        def source_query(method, params):
            return (eligible and method in ("eth_getTransactionReceipt", "eth_getTransactionByHash")
                    and isinstance(params, list) and len(params) == 1 and _hash(params[0]) == signature)

        def code_query(method, params):
            return (eligible and slot is not None and method == "eth_getCode"
                    and isinstance(params, list) and len(params) == 2
                    and isinstance(params[0], str) and params[0].lower() in _RELAY_CODE_TARGETS
                    and isinstance(params[1], str) and _quantity(params[1]) == slot)

        def allowed(method, params):
            if source_query(method, params) or code_query(method, params):
                return True
            return (eligible and slot is not None and method == "eth_getBlockByNumber"
                    and isinstance(params, list) and len(params) == 2
                    and isinstance(params[0], str) and _quantity(params[0]) == slot
                    and type(params[1]) is bool)

        def read(method, params):
            nonlocal slot
            try:
                result = self(method, params)
            except ValueError as exc:
                if str(exc) != "missing_result" or not allowed(method, params):
                    raise
                result = self._call(self._archive(), method, params)
            else:
                # Some non-archive hosts return an empty historical code body.
                # Never replace nonempty code or retry an empty archive answer.
                if result == "0x" and code_query(method, params):
                    result = self._call(self._archive(), method, params)
            if slot is None and source_query(method, params) and isinstance(result, dict):
                hash_key = "transactionHash" if method == "eth_getTransactionReceipt" else "hash"
                learned = _quantity(result.get("blockNumber"))
                if (_hash(result.get(hash_key)) == signature and learned is not None
                        and _hash(result.get("blockHash")) is not None
                        and _quantity(result.get("transactionIndex")) is not None):
                    slot = learned
            return result

        return read

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.primary.close()
        finally:
            if self._recovery is not None:
                self._recovery.close()
