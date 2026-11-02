"""The Base sell mirror reads a sale through a second host when the first refuses (2026-10-07, P1(b)).

On 8453 the sell shadow's only host is the watcher URL, base-rpc.publicnode.com, read by direct POST
(never the funded pool: Gamma's 09-29 blocker). publicnode's HTTP endpoint answers 403 to the
receipt read, so every sell cooled it 60 s ("no fallback: sells go unread meanwhile", 13 times
10-06..10-07), and the mirror has NEVER decoded a Base sale: 0 'sell 0x' lines in the whole 8453
log, sell_shadow {candidates 2, read_failed 2}.

Now a second host, the chain's backfill log-read host (_backfill_url: explicit, else its Alchemy
pool member), is asked on any miss, by its own direct POST: never custody, never the funded pool,
never the primary itself.
"""
import importlib

import pytest

PRIMARY = "https://base-rpc.publicnode.com"
ALCHEMY = "https://base-mainnet.g.alchemy.com/v2/KEY"
CUSTODY = "https://mainnet.base.org"
RECEIPT = {"status": "0x1", "logs": []}


@pytest.fixture
def mod(monkeypatch):
    import scripts.research.onchain_evm_wallet_watch as w
    w._configure("8453")
    w._SELL_HOSTS.clear()
    monkeypatch.setattr(w, "evm_rpc_for_chain", lambda chain: CUSTODY)
    yield w
    w._SELL_HOSTS.clear()
    w._configure("4663")


def _posts(mod, monkeypatch, answers):
    posts = []

    def post(url, body, timeout):
        posts.append((url, body["method"]))
        return answers[url](body["method"])
    monkeypatch.setattr(mod, "_http_post", post)
    return posts


def test_a_refusing_primary_falls_back_and_the_sale_is_read(mod, monkeypatch):
    """THE regression: publicnode 403s the receipt, and the sell went unread."""
    posts = _posts(mod, monkeypatch, {
        PRIMARY: lambda method: (403, None),
        ALCHEMY: lambda method: (200, {"jsonrpc": "2.0", "id": 1, "result": dict(RECEIPT)})})
    clock = [0.0]
    read = mod._sell_read_fn(PRIMARY, clock=lambda: clock[0], fallback=ALCHEMY)
    assert read("eth_getTransactionReceipt", ["0xt"]) == RECEIPT
    assert read("eth_getTransactionReceipt", ["0xt"]) == RECEIPT, "the cooled primary is skipped"
    assert posts == [(PRIMARY, "eth_getTransactionReceipt"), (ALCHEMY, "eth_getTransactionReceipt"),
                     (ALCHEMY, "eth_getTransactionReceipt")]


def test_a_null_from_the_primary_is_asked_of_the_fallback_too(mod, monkeypatch):
    _posts(mod, monkeypatch, {
        PRIMARY: lambda method: (200, {"jsonrpc": "2.0", "id": 1, "result": None}),
        ALCHEMY: lambda method: (200, {"jsonrpc": "2.0", "id": 1, "result": dict(RECEIPT)})})
    assert mod._sell_read_fn(PRIMARY, fallback=ALCHEMY)("eth_getTransactionReceipt", ["0xt"]) == RECEIPT


def test_without_a_fallback_the_read_is_todays(mod, monkeypatch):
    posts = _posts(mod, monkeypatch, {PRIMARY: lambda method: (403, None)})
    assert mod._sell_read_fn(PRIMARY)("eth_getTransactionReceipt", ["0xt"]) is None
    assert posts == [(PRIMARY, "eth_getTransactionReceipt")]


def test_the_fallback_never_touches_the_funded_pool(mod, monkeypatch):
    from sniper.execution import evm_rpc_pool, evm_wallet

    def forbidden(*_a, **_k):
        raise AssertionError("the sell path reached the funded pool")
    for name in ("pool_for", "choose", "penalize", "record", "note_refusal", "chain_of"):
        monkeypatch.setattr(evm_rpc_pool, name, forbidden)
    monkeypatch.setattr(evm_wallet, "_rpc", forbidden)
    monkeypatch.setattr(mod, "_counted_rpc", forbidden)
    _posts(mod, monkeypatch, {
        PRIMARY: lambda method: (403, None),
        ALCHEMY: lambda method: (200, {"jsonrpc": "2.0", "id": 1, "result": dict(RECEIPT)})})
    assert mod._sell_read_fn(PRIMARY, fallback=ALCHEMY)("eth_getTransactionReceipt", ["0xt"]) == RECEIPT


def test_the_fallback_host_is_the_backfill_host_never_custody_or_the_primary(mod, monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_8453", PRIMARY)
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_8453", raising=False)
    monkeypatch.setattr(mod, "_backfill_url", lambda chain: ALCHEMY)
    assert mod.sell_fallback_url("8453") == ALCHEMY
    monkeypatch.setattr(mod, "_backfill_url", lambda chain: CUSTODY + "/")
    assert mod.sell_fallback_url("8453") is None, "never custody"
    monkeypatch.setattr(mod, "_backfill_url", lambda chain: PRIMARY)
    assert mod.sell_fallback_url("8453") is None, "never the primary again"
    monkeypatch.setattr(mod, "_backfill_url", lambda chain: None)
    assert mod.sell_fallback_url("8453") is None


def test_the_cooling_line_names_the_fallback_and_its_reads_are_counted(mod, monkeypatch):
    said = []
    monkeypatch.setattr(mod, "_say", said.append)
    _posts(mod, monkeypatch, {
        PRIMARY: lambda method: (403, None),
        ALCHEMY: lambda method: (200, {"jsonrpc": "2.0", "id": 1, "result": dict(RECEIPT)})})
    before = mod._SELL_STATS.get("fallback_reads", 0)
    mod._sell_read_fn(PRIMARY, fallback=ALCHEMY)("eth_getTransactionReceipt", ["0xt"])
    assert "sell" in said[0] and "fallback host meanwhile" in said[0], said
    assert mod._SELL_STATS.get("fallback_reads", 0) == before + 1


def test_the_sell_handler_passes_the_fallback():
    import inspect
    import scripts.research.onchain_evm_wallet_watch as w
    src = inspect.getsource(w.run_push)
    assert "read_fn=_sell_read_fn(read_url, fallback=sell_fallback_url())" in src


def test_only_8453_gets_a_fallback(mod, monkeypatch):
    """Alpha: 56 and 4663 decode on their primary; their Alchemy member is the funded pool's."""
    monkeypatch.setattr(mod, "_backfill_url", lambda chain: ALCHEMY)
    for chain in ("56", "4663", "1"):
        assert mod.sell_fallback_url(chain) is None, chain


def test_a_fallback_429_is_counted(mod, monkeypatch):
    """Gamma LOW: the 8453 fallback's key is the funded pool's; a burst that draws its 429 shows."""
    _posts(mod, monkeypatch, {PRIMARY: lambda method: (403, None), ALCHEMY: lambda method: (429, None)})
    before = mod._SELL_STATS.get("fallback_429", 0)
    assert mod._sell_read_fn(PRIMARY, fallback=ALCHEMY)("eth_getTransactionReceipt", ["0xt"]) is None
    assert mod._SELL_STATS.get("fallback_429", 0) == before + 1
