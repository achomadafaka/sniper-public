"""Factory-authenticated source venues and exact-asset metadata, without live reads."""
import importlib.util
from copy import deepcopy

import pytest
from eth_abi import encode

from sniper.services.memecoin_bsc_volume_poll import V2_FACTORIES, V3_FACTORIES, WBNB, USDT
from sniper.services.memecoin_executed_pool import V2_SWAP, V3_SWAP, PANCAKE_V3_SWAP
from sniper.services.memecoin_evm_v4_events import V4_POOL_MANAGERS, v4_pool_id

MINT = "0x" + "11" * 20
POOL = "0x" + "22" * 20
SIG = "0x" + "ab" * 32
HASH = "0x" + "cd" * 32
ZERO = "0x" + "00" * 20
ASSETS = [(WBNB, "BNB", None), (USDT, "USDT", None)]


@pytest.fixture
def venues(monkeypatch):
    spec = importlib.util.find_spec("sniper.services.wallet_universe_venues")
    assert spec is not None, "verified venue preparation is not implemented"
    module = __import__("sniper.services.wallet_universe_venues", fromlist=["prepare_pools"])
    monkeypatch.setattr(module, "assets_for", lambda chain: list(ASSETS))
    monkeypatch.setattr(module, "native_symbol", lambda chain: "ETH" if chain == "4663" else "BNB")
    return module


def abi(kind, value):
    return "0x" + encode([kind], [value]).hex()


class Reader:
    def __init__(self, protocol="v2", quote=WBNB, chain="56"):
        self.chain = chain
        self.protocol = protocol
        self.quote = quote
        self.factory = V2_FACTORIES[0] if protocol == "v2" else V3_FACTORIES[0][0]
        self.fee = 2500
        self.token0, self.token1 = sorted([MINT, quote])
        topic = V2_SWAP if protocol == "v2" else PANCAKE_V3_SWAP
        self.receipt = dict(transactionHash=SIG, blockNumber="0x64", blockHash=HASH,
                            status="0x1", transactionIndex="0x5",
                            logs=[dict(address=POOL, transactionHash=SIG, blockNumber="0x64",
                                       blockHash=HASH, transactionIndex="0x5", logIndex="0x0",
                                       removed=False, topics=[topic, "0x" + "00" * 32, "0x" + "00" * 32],
                                       data="0x" + "00" * (128 if protocol == "v2" else 224))])
        self.calls = []
        self.fail_selector = None
        self.decimals = {MINT: 9, quote: 18}
        self.lookup = POOL
    def __call__(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method == "eth_chainId":
            return hex(int(self.chain))
        if method == "eth_getTransactionReceipt":
            assert params == [SIG]
            return deepcopy(self.receipt)
        if method == "eth_getBlockByNumber":
            assert params == ["0x64", False]
            return dict(number="0x64", hash=HASH, timestamp="0x3e8")
        assert method == "eth_call"
        request, block = params
        assert block == "0x64", "metadata must be pinned to the source block"
        target, data = request["to"], request["data"]
        selector = data[:10]
        if selector == self.fail_selector:
            raise ValueError("budget")
        if target == POOL:
            if selector == "0x0dfe1681": return abi("address", self.token0)
            if selector == "0xd21220a7": return abi("address", self.token1)
            if selector == "0xc45a0155": return abi("address", self.factory)
            if selector == "0xddca3f43": return abi("uint24", self.fee)
        if selector == "0x313ce567" and target in self.decimals:
            return abi("uint8", self.decimals[target])
        if target == self.factory and selector in ("0xe6a43905", "0x1698ee82"):
            expected = ("0xe6a43905" + encode(["address", "address"], [self.token0, self.token1]).hex()
                        if self.protocol == "v2" else
                        "0x1698ee82" + encode(["address", "address", "uint24"],
                                               [self.token0, self.token1, self.fee]).hex())
            assert data == expected
            return abi("address", self.lookup)
        pytest.fail(f"unexpected metadata read {target} {selector}")


@pytest.mark.parametrize("protocol", ["v2", "pancake_v3"])
def test_bsc_discovers_and_authenticates_source_pool_with_actual_decimals(venues, protocol):
    reader = Reader(protocol)
    pools = venues.prepare_pools("56", MINT, SIG, reader)
    assert len(pools) == 1
    p = pools[0]
    assert (p["pool"], p["token0"], p["token1"], p["quote_side"]) == (POOL, MINT, WBNB, 1)
    assert (p["token0_decimals"], p["token1_decimals"], p["quote_symbol"]) == (9, 18, "BNB")
    assert p["verified_source_signature"] == SIG and p["verified_block_hash"] == HASH
    assert p["verification"] == "trusted_factory_at_source_block"


@pytest.mark.parametrize("mutation", ["foreign_factory", "wrong_lookup", "self_pool", "unknown_quote", "bad_fee"])
def test_bsc_rejects_forged_or_self_valued_metadata(venues, mutation):
    reader = Reader("pancake_v3" if mutation == "bad_fee" else "v2")
    if mutation == "foreign_factory": reader.factory = "0x" + "99" * 20
    if mutation == "wrong_lookup": reader.lookup = "0x" + "99" * 20
    if mutation == "self_pool": reader.token1 = MINT
    if mutation == "unknown_quote": reader.token1 = "0x" + "77" * 20
    if mutation == "bad_fee": reader.fee = 123
    with pytest.raises(ValueError):
        venues.prepare_pools("56", MINT, SIG, reader)


def test_bsc_stable_decimals_are_read_and_never_assumed_six(venues):
    pools = venues.prepare_pools("56", MINT, SIG, Reader(quote=USDT))
    assert pools[0]["quote_symbol"] == "USDT" and pools[0]["token1_decimals"] == 18


def test_cached_bsc_metadata_is_revalidated_and_missing_decimals_filled(venues):
    reader = Reader()
    cached = [dict(pool=POOL, protocol="v2", token0=MINT, token1=WBNB,
                   token0_decimals=None, token1_decimals=None, quote_side=0, quote_symbol="ETH")]
    original = deepcopy(cached)
    pools = venues.prepare_pools("56", MINT, SIG, reader, cached_pools=cached)
    assert pools[0]["quote_side"] == 1 and pools[0]["quote_symbol"] == "BNB"
    assert pools[0]["token0_decimals"] == 9
    assert cached == original


def test_budget_failure_is_typed_and_not_an_empty_pool_set(venues):
    reader = Reader()
    reader.fail_selector = "0x313ce567"
    with pytest.raises(ValueError, match="budget"):
        venues.prepare_pools("56", MINT, SIG, reader)


@pytest.mark.parametrize("mutation", ["missing", "wrong_signature", "failed", "removed", "wrong_log_anchor", "wrong_chain"])
def test_discovery_rejects_invalid_source_identity(venues, mutation):
    reader = Reader()
    if mutation == "missing": reader.receipt = None
    if mutation == "wrong_signature": reader.receipt["transactionHash"] = "0x" + "ff" * 32
    if mutation == "failed": reader.receipt["status"] = "0x0"
    if mutation == "removed": reader.receipt["logs"][0]["removed"] = True
    if mutation == "wrong_log_anchor": reader.receipt["logs"][0]["blockHash"] = "0x" + "ff" * 32
    if mutation == "wrong_chain": reader.chain = "1"
    with pytest.raises(ValueError):
        venues.prepare_pools("56", MINT, SIG, reader)


def test_prepared_metadata_can_resume_without_repeating_authenticated_calls(venues):
    one = venues.prepare_pools("56", MINT, SIG, Reader())
    def only_identity(method, params):
        assert method != "eth_call", "verified same-source metadata should be reusable"
        return Reader()(method, params)
    two = venues.prepare_pools("56", MINT, SIG, only_identity, cached_pools=one)
    assert two == one


def v4_cached():
    key = dict(currency0=ZERO, currency1=MINT, fee=3000, tick_spacing=60, hooks=ZERO)
    return dict(pool=v4_pool_id(key), protocol="v4", token0=ZERO, token1=MINT,
                token0_decimals=None, token1_decimals=None, quote_side=0, **key)


def test_cached_v4_extends_manager_preserves_native_quote_and_verifies_key(venues):
    reader = Reader(chain="4663")
    reader.receipt["logs"] = []
    pools = venues.prepare_pools("4663", MINT, SIG, reader, cached_pools=[v4_cached()])
    p = pools[0]
    assert p["manager"] == V4_POOL_MANAGERS["4663"]
    assert (p["quote_side"], p["token0_decimals"], p["token1_decimals"], p["quote_symbol"]) == (0, 18, 9, "ETH")
    assert p["v4_pool_key_verified"] is True


@pytest.mark.parametrize("mutation", ["wrong_id", "wrong_manager", "wrong_currency", "unknown_protocol", "wrong_decimals"])
def test_cached_metadata_mismatches_are_typed_failures(venues, mutation):
    reader = Reader(chain="4663")
    reader.receipt["logs"] = []
    cached = v4_cached()
    if mutation == "wrong_id": cached["pool"] = "0x" + "ff" * 32
    if mutation == "wrong_manager": cached["manager"] = "0x" + "99" * 20
    if mutation == "wrong_currency": cached["currency1"] = "0x" + "77" * 20
    if mutation == "unknown_protocol": cached["protocol"] = "magic"
    if mutation == "wrong_decimals": cached["token1_decimals"] = 6
    with pytest.raises(ValueError):
        venues.prepare_pools("4663", MINT, SIG, reader, cached_pools=[cached])


def test_unknown_source_protocol_never_returns_fabricated_empty_metadata(venues):
    reader = Reader()
    reader.receipt["logs"][0]["topics"][0] = "0x" + "99" * 32
    with pytest.raises(ValueError, match="no_verified_pools"):
        venues.prepare_pools("56", MINT, SIG, reader)
