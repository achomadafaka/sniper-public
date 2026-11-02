"""Session-local metadata reuse for sequential research capital observations.

The injected reader remains the sole owner of networking, pacing and budgets.
Call begin() before each address. Opening block headers may be shared, but all
closing headers and balances are fresh. This wrapper is not thread safe and
must not be shared with source/flow collection or execution reads.
"""
from copy import deepcopy
import re
import time

_ANCHOR_AGE_S = 240
_SOLANA_GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


def _quantity(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return int(value, 16)
    return None


def _header(value, height):
    return (isinstance(value, dict) and _quantity(value.get("number")) == height
            and isinstance(value.get("hash"), str)
            and re.fullmatch(r"0x[0-9a-fA-F]{64}", value["hash"]) is not None
            and (_quantity(value.get("timestamp")) or 0) > 0)


class CapitalRead:
    """Cache only validated identity and metadata for one capital-read session."""

    def __init__(self, chain, call, *, clock=None):
        if chain != "solana" and (not isinstance(chain, str)
                                  or not re.fullmatch(r"[1-9][0-9]*", chain)):
            raise ValueError("invalid_chain")
        self.chain = chain
        self.call = call
        self.clock = clock or time.monotonic
        self._identity = None
        self._active = False
        self._header_calls = 0
        self._clear_anchor()

    def _clear_anchor(self):
        self._head = None
        self._height = None
        self._started = None
        self._opening = None
        self._decimals = {}

    def begin(self):
        """Start an address without making provider calls; age out old anchors."""
        now = self.clock()
        if self._started is not None and not 0 <= now - self._started < _ANCHOR_AGE_S:
            self._clear_anchor()
        self._header_calls = 0
        self._active = True

    def __call__(self, method, params):
        if not self._active:
            return self.call(method, params)
        if self.chain == "solana":
            if method != "getGenesisHash" or params != []:
                return self.call(method, params)
            if self._identity is not None:
                return self._identity
            result = self.call(method, params)
            if result == _SOLANA_GENESIS:
                self._identity = result
            return result
        if method == "eth_chainId" and params == []:
            if self._identity is not None:
                return self._identity
            result = self.call(method, params)
            if _quantity(result) == int(self.chain):
                self._identity = result
            return result
        if method == "eth_blockNumber" and params == []:
            if self._head is not None:
                return self._head
            started = self.clock()
            result = self.call(method, params)
            height = _quantity(result)
            if height is not None and height > 0:
                self._head = result
                self._height = max(0, height - (12 if self.chain == "56" else 2))
                self._started = started
            return result
        if (method == "eth_getBlockByNumber" and self._height is not None
                and params == [hex(self._height), False]):
            self._header_calls += 1
            if self._header_calls == 1 and self._opening is not None:
                return deepcopy(self._opening)
            try:
                result = self.call(method, params)
            except Exception:
                self._clear_anchor()
                raise
            if not _header(result, self._height):
                self._clear_anchor()
                if self._header_calls > 1:
                    raise ValueError("wrong_block")
            elif self._header_calls == 1:
                self._opening = deepcopy(result)
            elif self._opening is not None and result["hash"] != self._opening["hash"]:
                self._clear_anchor()
            elif (self._opening is not None
                  and _quantity(result["timestamp"]) != _quantity(self._opening["timestamp"])):
                self._clear_anchor()
                raise ValueError("wrong_block")
            return result
        key = self._decimals_key(method, params)
        if key is not None and key in self._decimals:
            return self._decimals[key]
        result = self.call(method, params)
        if key is not None and isinstance(result, str):
            decimals = _quantity(result)
            if decimals is not None and decimals <= 36:
                self._decimals[key] = result
        return result

    def _decimals_key(self, method, params):
        if (method != "eth_call" or self._opening is None
                or not isinstance(params, list) or len(params) != 2
                or params[1] != hex(self._height) or not isinstance(params[0], dict)):
            return None
        transaction = params[0]
        token = transaction.get("to")
        if (set(transaction) != {"to", "data"} or transaction.get("data") != "0x313ce567"
                or not isinstance(token, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", token)):
            return None
        return token, params[1]
