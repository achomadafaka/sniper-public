"""Offline attempt-count and reorg regressions for the capital-only wrapper."""
from copy import deepcopy
import importlib

import pytest

BLOCK = "0x" + "aa" * 32
TOKEN = "0x" + "11" * 20
OWNER = "0x" + "22" * 20
GENESIS = "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdpKuc147dw2N9d"


class Reader:
    def __init__(self):
        self.calls = []
        self.values = dict(eth_chainId="0x38", eth_blockNumber="0x70", eth_getBalance="0x1",
                           eth_getBlockByNumber=dict(number="0x64", hash=BLOCK, timestamp="0x3e8"),
                           eth_call="0x12", getGenesisHash=GENESIS, getBalance={"value": 1})
        self.error = None

    def __call__(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if self.error == method:
            raise ValueError("provider_timeout")
        return deepcopy(self.values[method])


def wrap(reader, chain="56", clock=lambda: 0):
    return importlib.import_module("sniper.services.wallet_universe_capital_reads").CapitalRead(chain, reader, clock=clock)


def start(read):
    read.begin()
    assert read("eth_chainId", []) == "0x38"
    assert read("eth_blockNumber", []) == "0x70"
    return read("eth_getBlockByNumber", ["0x64", False])


def decimals(read, token=TOKEN, block="0x64"):
    return read("eth_call", [dict(to=token, data="0x313ce567"), block])


def test_wallets_share_identity_start_header_and_decimals_but_not_end_headers_or_balances():
    provider = Reader()
    read = wrap(provider)
    for _ in range(2):
        start(read)
        decimals(read)
        read("eth_getBalance", [OWNER, "0x64"])
        read("eth_call", [dict(to=TOKEN, data="0x70a08231" + "00" * 32), "0x64"])
        read("eth_getBlockByNumber", ["0x64", False])
    methods = [method for method, _ in provider.calls]
    assert methods.count("eth_chainId") == methods.count("eth_blockNumber") == 1
    assert methods.count("eth_getBlockByNumber") == 3
    assert methods.count("eth_getBalance") == 2
    assert methods.count("eth_call") == 3


def test_closing_header_cannot_hide_a_reorg():
    provider = Reader()
    read = wrap(provider)
    assert start(read)["hash"] == BLOCK
    provider.values["eth_getBlockByNumber"]["hash"] = "0x" + "bb" * 32
    assert read("eth_getBlockByNumber", ["0x64", False])["hash"] != BLOCK
    # A changed anchor also invalidates metadata from its old state.
    start(read)
    assert [method for method, _ in provider.calls].count("eth_blockNumber") == 2


def test_end_header_failure_is_not_replaced_with_cached_success():
    provider = Reader()
    read = wrap(provider)
    start(read)
    provider.error = "eth_getBlockByNumber"
    with pytest.raises(ValueError, match="provider_timeout"):
        read("eth_getBlockByNumber", ["0x64", False])


def test_anchor_and_decimals_expire_at_240_seconds_identity_can_remain():
    now = [0]
    provider = Reader()
    read = wrap(provider, clock=lambda: now[0])
    for second in (0, 239, 240):
        now[0] = second
        start(read)
        decimals(read)
    methods = [method for method, _ in provider.calls]
    assert methods.count("eth_chainId") == 1
    assert methods.count("eth_blockNumber") == 2
    assert methods.count("eth_getBlockByNumber") == 2
    assert methods.count("eth_call") == 2


def test_no_cache_shared_across_instances_and_returned_headers_are_detached():
    provider = Reader()
    first, second = wrap(provider), wrap(provider)
    start(first)["hash"] = "modified"
    assert start(first)["hash"] == BLOCK
    start(second)
    assert [method for method, _ in provider.calls].count("eth_chainId") == 2


@pytest.mark.parametrize("method,value", [("eth_chainId", "0x1"), ("eth_chainId", None),
    ("eth_blockNumber", True), ("eth_blockNumber", "garbage"),
    ("eth_getBlockByNumber", dict(number="0x63", hash=BLOCK, timestamp="0x3e8")),
    ("eth_getBlockByNumber", dict(number="0x64", hash="bad", timestamp="0x3e8")),
    ("eth_getBlockByNumber", dict(number="0x64", hash=BLOCK, timestamp=0)),
    ("eth_call", "0x25"), ("eth_call", None)])
def test_invalid_results_are_never_cached(method, value):
    provider = Reader()
    read = wrap(provider)
    start(read)
    # Use a fresh wrapper to avoid the deliberately successful setup cache.
    read = wrap(provider)
    read.begin()
    if method in ("eth_getBlockByNumber", "eth_call"):
        read("eth_blockNumber", [])
    if method == "eth_call": read("eth_getBlockByNumber", ["0x64", False])
    provider.values[method] = value
    params = {"eth_chainId": [], "eth_blockNumber": [], "eth_getBlockByNumber": ["0x64", False],
              "eth_call": [dict(to=TOKEN, data="0x313ce567"), "0x64"]}[method]
    before = len(provider.calls)
    for _ in range(2):
        read(method, params)
        read.begin()
    assert len(provider.calls) == before + 2


def test_decimals_cache_is_exact_token_and_fixed_block_only():
    provider = Reader()
    read = wrap(provider)
    start(read)
    for token, block in [(TOKEN, "0x64"), (TOKEN, "0x64"), (OWNER, "0x64"),
                         (TOKEN, "0x63"), (TOKEN, "0x63"), (TOKEN, "latest"), (TOKEN, "latest")]:
        decimals(read, token, block)
    assert [method for method, _ in provider.calls].count("eth_call") == 6


def test_provider_errors_are_not_cached_or_retried():
    provider = Reader()
    read = wrap(provider)
    provider.error = "eth_chainId"
    for _ in range(2):
        read.begin()
        with pytest.raises(ValueError, match="provider_timeout"):
            read("eth_chainId", [])
    assert len(provider.calls) == 2


def test_solana_only_genesis_is_cached():
    provider = Reader()
    read = wrap(provider, chain="solana")
    for _ in range(2):
        read.begin()
        assert read("getGenesisHash", []) == GENESIS
        read("getBalance", [OWNER, {"commitment": "finalized"}])
    assert [method for method, _ in provider.calls] == ["getGenesisHash", "getBalance", "getBalance"]


def test_wrong_solana_genesis_is_not_cached():
    provider = Reader()
    provider.values["getGenesisHash"] = "wrong"
    read = wrap(provider, chain="solana")
    for _ in range(2):
        read.begin()
        assert read("getGenesisHash", []) == "wrong"
    assert len(provider.calls) == 2


def test_observe_capital_still_reports_reorg_after_cached_opening_header():
    from sniper.services.wallet_universe_metrics import observe_capital
    provider = Reader()
    provider.values.update(eth_chainId=hex(5042), eth_blockNumber=hex(102), eth_getBalance=hex(10**18))
    read = wrap(provider, chain="5042")
    read.begin()
    row = observe_capital("5042", OWNER, read, {}, now="1970-01-01T00:16:40+00:00")
    assert row["state"] == "ok"
    provider.values["eth_getBlockByNumber"]["hash"] = "0x" + "bb" * 32
    read.begin()
    row = observe_capital("5042", OWNER, read, {}, now="1970-01-01T00:16:40+00:00")
    assert row["state"] == "block_reorg" and row["usd"] is None


@pytest.mark.parametrize("field,value", [("number", "0x63"), ("timestamp", None), ("timestamp", "0x3e9")])
def test_same_hash_malformed_or_conflicting_closing_header_cannot_be_success(field, value):
    provider = Reader()
    read = wrap(provider)
    start(read)
    provider.values["eth_getBlockByNumber"][field] = value
    with pytest.raises(ValueError, match="wrong_block"):
        read("eth_getBlockByNumber", ["0x64", False])


def test_provider_error_during_decimals_is_not_cached():
    provider = Reader()
    read = wrap(provider)
    start(read)
    provider.error = "eth_call"
    with pytest.raises(ValueError, match="provider_timeout"):
        decimals(read)
    provider.error = None
    assert decimals(read) == "0x12"
    assert [method for method, _ in provider.calls].count("eth_call") == 2
