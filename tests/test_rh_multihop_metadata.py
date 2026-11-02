"""Robinhood entry metadata uses owned connections and one decision's successful DS read."""
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import threading
import time
import urllib.error

import pytest

from sniper.execution import evm_v4_multihop as MH
from sniper.execution import evm_v4_quote as Q
from sniper.execution import evm_wallet as W
from sniper.execution import geckoterminal_quote as GT
from sniper.execution import solana_quote as DS

CHAIN = "4663"
CASH = "0x" + "00" * 20
COIN = "0x" + "22" * 20
BRIDGE = "0x" + "33" * 20
POOL = "0x" + "44" * 32


def headers(**values):
    out = Message()
    for key, value in values.items():
        out[key.replace("_", "-")] = value
    return out


@pytest.fixture(autouse=True)
def providers(monkeypatch):
    monkeypatch.setattr(GT, "_funded_cooldown_until", [0.0])
    monkeypatch.setattr(GT, "_cooldown_until", [0.0])
    monkeypatch.setattr(GT, "_gt_disabled", lambda: False)
    monkeypatch.setattr(DS, "_ds_funded_cooldown_until", [0.0])
    monkeypatch.setattr(DS, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(Q, "_stored_pools_for_mint", lambda *a: [])


@pytest.mark.parametrize("ranked", [[], [(9000.0, POOL)]])
def test_successful_ds_pair_read_is_reused_in_fallback(monkeypatch, ranked):
    calls = []
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda *a, **k: (calls.append(a) or ranked, False))
    kw = {"ds_pool_cache": {}, "provider_budget": MH._SearchBudget(1, 100)}
    MH._direct_bridge_pools(CHAIN, CASH, BRIDGE, **kw)
    assert MH._pools_with_depth(CHAIN, BRIDGE, include_store=False, pair_token=CASH, **kw) == [
        (pid, depth) for depth, pid in ranked]
    assert len(calls) == 1, "direct and fallback DS discovery are the same successful read"


def test_failed_ds_read_is_not_reused(monkeypatch):
    calls = []
    def read(*a, **k):
        calls.append(a)
        return ([], True) if len(calls) == 1 else ([(9000.0, POOL)], False)
    monkeypatch.setattr(Q, "_gt_pools_ranked", lambda *a, **k: ([], False))
    monkeypatch.setattr(Q, "_ds_pools_ranked", read)
    kw = {"ds_pool_cache": {}, "provider_budget": MH._SearchBudget(1, 100)}
    assert MH._direct_bridge_pools(CHAIN, CASH, BRIDGE, **kw) is None
    assert MH._pools_with_depth(CHAIN, BRIDGE, include_store=False, pair_token=CASH, **kw) == [(POOL, 9000)]
    assert len(calls) == 2


def test_provider_cache_is_local_and_pair_and_callback_bound(monkeypatch):
    calls = []
    monkeypatch.setattr(Q, "_ds_pools_ranked", lambda *a, **k: (calls.append((a, k)) or [], False))
    barrier = threading.Barrier(2)
    def decision():
        cache = {}
        kw = {"ds_pool_cache": cache, "provider_budget": MH._SearchBudget(1, 100)}
        barrier.wait()
        for _ in range(2):
            MH._direct_bridge_pools(CHAIN, CASH, BRIDGE, **kw)
        MH._direct_bridge_pools(CHAIN, COIN, BRIDGE, **kw)
        MH._direct_bridge_pools(CHAIN, CASH, BRIDGE, ds_http=lambda url: {}, **kw)
        return cache
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: decision(), range(2)))
    assert len(calls) == 6
    assert results[0] is not results[1]


def test_cut_provider_read_does_not_enter_cache():
    cache = {}
    def slow(*a, **k):
        time.sleep(0.04)
        return [], False
    budget = MH._SearchBudget(0.01, 100)
    assert MH._provider_pools(slow, CHAIN, COIN, budget=budget, cache=cache) == ([], False)
    assert budget.discovery_cut
    assert cache == {}


@pytest.mark.parametrize("status", [403, 429, 500])
def test_pooled_metadata_http_errors_keep_headers_and_hide_body(status):
    response_headers = headers(retry_after="37")
    budget = MH._SearchBudget(1, 100)
    seen = []
    def pooled(method, url, **kw):
        seen.append(kw)
        return status, {"secret": "response must not reach the exception"}, response_headers
    request = MH._metadata_http(pooled, budget, GT._UA)
    with pytest.raises(urllib.error.HTTPError) as raised:
        request("GET", "https://api.example.test/pools?secret=hidden", timeout=10)
    assert raised.value.code == status
    assert raised.value.headers.get("Retry-After") == "37"
    assert "secret" not in str(raised.value)
    assert budget.used == 1
    assert 0 < seen[0]["timeout"] <= 1
    assert seen[0]["headers"] == {"user-agent": GT._UA, "accept": "*/*"}


def test_pooled_metadata_redirects_count_every_send_and_keep_deadline():
    seen = []
    budget = MH._SearchBudget(1, 2)
    def pooled(method, url, **kw):
        seen.append((url, kw["timeout"]))
        return 302, {}, headers(location="/redirected")
    request = MH._metadata_http(pooled, budget, DS._UA)
    with pytest.raises(MH.RequestNotSent):
        request("GET", "https://api.example.test/start")
    assert [url for url, _ in seen] == ["https://api.example.test/start", "https://api.example.test/redirected"]
    assert seen[1][1] <= seen[0][1] <= 1
    assert budget.used == 2 and budget.discovery_cut


@pytest.mark.parametrize("location", ["file:///tmp/no", "ftp://unavailable.test/file"])
def test_pooled_metadata_rejects_unsupported_redirects(location):
    seen = []
    def pooled(*a, **k):
        seen.append(a)
        return 302, {}, headers(location=location)
    request = MH._metadata_http(pooled, MH._SearchBudget(1, 100), GT._UA)
    with pytest.raises(urllib.error.HTTPError):
        request("GET", "https://api.example.test/start")
    assert len(seen) == 1


def test_pooled_metadata_redirect_success_and_loop_bound():
    seen = []
    def pooled(method, url, **kw):
        seen.append(url)
        return (302, {}, headers(location="/final")) if len(seen) == 1 else (200, {"data": []}, headers())
    request = MH._metadata_http(pooled, MH._SearchBudget(1, 100), GT._UA)
    assert request("GET", "https://api.example.test/start")[1] == {"data": []}
    assert seen == ["https://api.example.test/start", "https://api.example.test/final"]
    count = []
    def loop(*a, **k):
        count.append(1)
        return 302, {}, headers(location="/loop")
    with pytest.raises(urllib.error.HTTPError):
        MH._metadata_http(loop, MH._SearchBudget(1, 100), GT._UA)("GET", "https://api.example.test/loop")
    assert len(count) <= 10


def test_pooled_metadata_429_uses_both_existing_provider_guards():
    def pooled(*a, **k):
        return 429, {}, headers(retry_after="37")
    request = MH._metadata_http(pooled, MH._SearchBudget(1, 100), GT._UA)
    now = time.monotonic()
    assert Q._gt_pools_ranked(CHAIN, COIN, request_http=request) == ([], True)
    assert GT._funded_cooldown_until[0] >= now + 36
    assert Q._ds_pools_ranked(CHAIN, COIN, request_http=request) == ([], True)
    assert DS.dexscreener_cooldown_active(funded=True)


@pytest.mark.parametrize("payload", ["not json", None, ["wrong shape"]])
def test_pooled_metadata_malformed_body_stays_provider_failure(payload):
    request = MH._metadata_http(lambda *a, **k: (200, payload, headers()), MH._SearchBudget(1, 100), GT._UA)
    assert Q._gt_pools_ranked(CHAIN, COIN, request_http=request) == ([], True)
    assert Q._ds_pools_ranked(CHAIN, COIN, request_http=request) == ([], True)


@pytest.mark.parametrize("chain,is_buy,owned", [("4663", True, True), ("4663", True, False),
                                              ("4663", False, True), ("8453", True, True)])
def test_owned_metadata_selection_is_only_rh_entry(monkeypatch, chain, is_buy, owned):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    captures = []
    callback = lambda *a, **k: (200, {}, headers())
    def http(*a, **k):
        return 200, {}, None
    if owned:
        http.owned_metadata_headers = callback
    monkeypatch.setattr(MH, "multihop_quote", lambda *a, **k: captures.append(k))
    monkeypatch.setattr(MH, "dex_v4_multihop_exit_quote", lambda *a, **k: captures.append(k))
    weth = W.CHAIN_QUOTE[chain]["token"]
    sell, buy = (weth, COIN) if is_buy else (COIN, weth)
    MH.dex_v4_multihop_quote(chain, sell, buy, 100, "0xtaker", http=http, rpc_url="http://rpc", clip_usd=50)
    assert captures[0].get("metadata_http") is (callback if chain == "4663" and is_buy and owned else None)


def test_full_rh_entry_elides_one_metadata_send_with_same_route_and_fresh_quotes(monkeypatch):
    from sniper.execution import evm_v4_multihop_tx as TX
    from sniper.settings import memecoin as cfg
    bridge_pool = "0x" + "55" * 32
    keys = {POOL: {"currency0": COIN, "currency1": BRIDGE, "fee": 3000,
                   "tick_spacing": 60, "hooks": CASH},
            bridge_pool: {"currency0": CASH, "currency1": BRIDGE, "fee": 100,
                          "tick_spacing": 1, "hooks": CASH}}
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_STORE_FIRST", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_MULTIHOP_MAX_LEGS", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGES", {CHAIN: (BRIDGE,)})
    # This test counts metadata SENDS across two identical searches; the bridge-leg memo
    # (2026-10-17) would serve the second search's bridge lookups and the repeated DS bridge read.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MULTIHOP_BRIDGE_MEMO_MAX_AGE_S", 0.0)
    monkeypatch.setattr(MH, "_pool_key_for", lambda chain, pid, rpc, **kw: keys[pid])
    monkeypatch.setattr(MH, "_onchain_pair_options", lambda *a, **k: [])
    monkeypatch.setattr(TX, "as_zerox_quote", lambda chain, cash, mint, route, *a, **k: {"route": dict(route)})
    monkeypatch.setattr(Q, "_pair_hint", {})
    monkeypatch.setattr(MH, "_BRIDGE_LAST_GOOD", {})
    metadata, quotes, block_reads = [], [], []

    def gt_pool(pid, depth, first, second):
        return {"id": "robinhood_" + pid, "attributes": {"reserve_in_usd": str(depth)},
                "relationships": {name: {"data": {"id": "robinhood_" + token}} for name, token in
                                  (("base_token", first), ("quote_token", second))}}

    def transport(method, url, **kw):
        if method == "POST":
            return 200, {"result": "0xabc"}, headers()
        metadata.append(url)
        if "geckoterminal" in url:
            rows = [gt_pool(POOL, 10000, COIN, BRIDGE)] if COIN in url else [
                gt_pool(bridge_pool, 20000, CASH, BRIDGE)]
            return 200, {"data": rows}, headers()
        rows = [{"chainId": "robinhood", "pairAddress": POOL,
                 "baseToken": {"address": COIN}, "quoteToken": {"address": BRIDGE},
                 "liquidity": {"usd": 9000}}] if COIN in url else []
        return 200, {"pairs": rows}, headers()

    def decision_block(chain, rpc, **kw):
        kw["http"]("POST", rpc, body="{}")
        block_reads.append(1)
        return hex(0xABC + len(block_reads))

    def quote_path(chain, currency, path, amount, rpc, block="latest", **kw):
        kw["http"]("POST", rpc, body="{}")
        quotes.append((currency, amount, block, tuple(key["fee"] for _, key in path)))
        return 5000 if currency == CASH else 970

    monkeypatch.setattr(MH, "_decision_block", decision_block)
    monkeypatch.setattr(MH, "_quote_path", quote_path)
    monkeypatch.setattr(GT, "_http", transport)
    monkeypatch.setattr(DS, "_http", transport)
    weth = W.CHAIN_QUOTE[CHAIN]["token"]
    def entry():
        return MH.dex_v4_multihop_quote(CHAIN, weth, COIN, 1000, "0xtaker", http=transport,
                                        rpc_url="http://rpc", clip_usd=50)

    baseline = entry()
    assert baseline is not None and len(metadata) == 5
    transport.owned_metadata_headers = transport
    improved = entry()
    assert improved is not None and len(metadata) == 9
    for field in ("coin_pool_id", "bridge_pool_id", "coin_depth_usd", "bridge_depth_usd",
                  "round_trip_pct", "amount_out", "legs_path", "paths_measured"):
        assert improved["route"][field] == baseline["route"][field]
    assert improved["route"]["search_requests"] == baseline["route"]["search_requests"] - 1
    assert quotes == [(CASH, 1000, "0xabd", (100, 3000)), (COIN, 5000, "0xabd", (3000, 100)),
                      (CASH, 1000, "0xabe", (100, 3000)), (COIN, 5000, "0xabe", (3000, 100))]
