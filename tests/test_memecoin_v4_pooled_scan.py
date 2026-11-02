"""Physical endpoint attribution for pooled Initialize scans; no network or ledger writes."""
import json

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_v4_quote as v4
from sniper.settings import memecoin as cfg

PRIMARY = "https://custody.example"
BACKUP = "https://backup.example"
POOL_ID = "0x" + "ab" * 32
REFUSAL = {"error": {"code": -32602, "message": "range 200000 exceeds limit of 10000"}}


@pytest.fixture
def scan(monkeypatch):
    pool.reset_for_tests()
    v4.begin_quote()
    clock = [1000.0]
    monkeypatch.setattr(pool, "_now", lambda: clock[0])
    monkeypatch.setattr(v4, "_now", lambda: clock[0])
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, BACKUP)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ARCHIVE_RPC_POOL_URLS", {})
    monkeypatch.setattr(v4, "_key_cache", {})
    monkeypatch.setattr(v4, "_full_range_refused", {})
    monkeypatch.setattr(v4, "_scan_429", {})
    monkeypatch.setattr(v4, "_scan_miss", {})
    monkeypatch.setattr(v4, "_stored_pool_key", lambda *a: None)
    monkeypatch.setattr(v4, "_store_pool_key", lambda *a: None)
    monkeypatch.setattr(v4, "_archive_rpc_urls", lambda *a: (PRIMARY,))
    for url, ms in ((PRIMARY, 150), (BACKUP, 80)):
        pool.mark_identity(url, True)
        pool.record(url, ms, True)
    calls = []

    def http(_method, url, *, body, **kwargs):
        request = json.loads(body)
        method, params = request["method"], request["params"]
        calls.append((url, method, params))
        if method == "eth_blockNumber":
            return 200, {"result": hex(200_000)}, {}
        if method == "eth_chainId":
            return 200, {"result": "0x1"}, {}
        if params[0].get("fromBlock") == "0x0":
            return 200, dict(REFUSAL), {}
        return 200, {"result": []}, {}

    yield clock, calls, http
    pool.reset_for_tests()
    v4.begin_quote()


def test_two_members_memoize_a_clean_miss_and_keep_bounded_reads(scan):
    _clock, calls, http = scan
    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert ("1", POOL_ID) in v4._scan_miss
    full = [u for u, m, p in calls if m == "eth_getLogs" and p[0]["fromBlock"] == "0x0"]
    assert set(full) == {PRIMARY, BACKUP}, "each physical member proves its own refusal"
    calls.clear()
    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert [m for _u, m, _p in calls] == ["eth_blockNumber", "eth_getLogs"]
    assert not pool.refuses(PRIMARY, "eth_getLogs"), "only the full-range shape is refused"


def test_a_backup_refusal_does_not_silence_an_unasked_primary(scan):
    _clock, calls, http = scan
    pool.penalize(PRIMARY, "test", quarantine_s=60)
    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert v4._full_range_refused_recently(BACKUP)
    assert not v4._full_range_refused_recently(PRIMARY)
    assert not v4._scan_miss, "a quarantined primary has not refused the shape"
    assert not any(url == PRIMARY for url, _m, _p in calls)


def test_a_backup_429_parks_only_that_member_and_outlasts_pool_quarantine(scan):
    clock, calls, inner = scan
    pool.penalize(PRIMARY, "test", quarantine_s=1)

    def http(method, url, *, body, **kwargs):
        if url == BACKUP:
            calls.append((url, json.loads(body)["method"], json.loads(body)["params"]))
            return 429, {"error": {"code": -32005, "message": "rate limited"}}, {}
        return inner(method, url, body=body, **kwargs)

    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert set(v4._scan_429) == {BACKUP}
    assert v4.last_provider_blind() == "rpc_rate_limit"
    clock[0] += 16
    assert pool.available(PRIMARY) and pool.available(BACKUP)
    calls.clear()
    assert v4._pool_key("1", "0x" + "cd" * 32, PRIMARY, http=http) is None
    assert calls and all(url == PRIMARY for url, _m, _p in calls)
    assert not v4._scan_miss, "the parked backup proves nothing about a clean miss"


def test_another_member_can_serve_the_full_range_after_a_refusal(scan, monkeypatch):
    _clock, calls, inner = scan
    monkeypatch.setattr(v4, "decode_initialize_log", lambda _log: {"fee": 500})

    def http(method, url, *, body, **kwargs):
        request = json.loads(body)
        if (url == BACKUP and request["method"] == "eth_getLogs"
                and request["params"][0]["fromBlock"] == "0x0"):
            calls.append((url, request["method"], request["params"]))
            return 200, {"result": [{"data": "0x"}]}, {}
        return inner(method, url, body=body, **kwargs)

    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) == {"fee": 500}
    assert v4._full_range_refused_recently(PRIMARY)
    assert not v4._full_range_refused_recently(BACKUP)
    assert not v4._scan_miss


def test_a_recovered_429_still_types_an_incomplete_scan_as_rate_limited(scan):
    _clock, _calls, inner = scan

    def http(method, url, *, body, **kwargs):
        if url == PRIMARY:
            return 429, {"error": {"code": -32005, "message": "rate limited"}}, {}
        return inner(method, url, body=body, **kwargs)

    # The backup recovers the head, but custody's parked full range remains unknown.
    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert set(v4._scan_429) == {PRIMARY}
    assert v4.last_provider_blind() == "rpc_rate_limit"
    assert not v4._scan_miss


def test_full_range_refusal_expiry_and_zero_ttl_apply_to_each_member(scan, monkeypatch):
    clock, calls, http = scan
    v4._pool_key("1", POOL_ID, PRIMARY, http=http)
    clock[0] += v4._FULL_RANGE_REFUSAL_TTL_S + 1
    calls.clear()
    v4._pool_key("1", "0x" + "cd" * 32, PRIMARY, http=http)
    assert {u for u, m, p in calls if m == "eth_getLogs" and p[0]["fromBlock"] == "0x0"} == {
        PRIMARY, BACKUP}
    monkeypatch.setattr(v4, "_FULL_RANGE_REFUSAL_TTL_S", 0)
    calls.clear()
    v4._pool_key("1", "0x" + "ef" * 32, PRIMARY, http=http)
    assert {u for u, m, p in calls if m == "eth_getLogs" and p[0]["fromBlock"] == "0x0"} == {
        PRIMARY, BACKUP}


def test_archive_identity_429_parks_its_physical_endpoint_before_the_next_scan(scan, monkeypatch):
    _clock, calls, inner = scan
    archive = "https://archive.example"
    monkeypatch.setattr(v4, "_archive_rpc_urls", lambda *a: (PRIMARY, archive))

    def http(method, url, *, body, **kwargs):
        if url == archive:
            calls.append((url, json.loads(body)["method"], json.loads(body)["params"]))
            return 429, {"error": {"code": -32005, "message": "rate limited"}}, {}
        return inner(method, url, body=body, **kwargs)

    assert v4._pool_key("1", POOL_ID, PRIMARY, http=http) is None
    assert set(v4._scan_429) == {archive}
    assert v4.last_provider_blind() == "rpc_rate_limit"
    calls.clear()
    v4._pool_key("1", "0x" + "99" * 32, PRIMARY, http=http)
    assert not any(url == archive for url, _method, _params in calls)
    assert not v4._scan_miss
