"""Chain-1 custody reads: name the failure, back off on a rate limit, stop re-asking a refused shape.

HALCYON 2026-10-07 10:20:51-10:21:53Z (chrollo0o0, one origin, 9 retry-lane attempts): six
refusals read "quote balance unavailable" and the code that caused them was recorded nowhere.
`quote_balance_usd_or_none` swallowed the error, and the ladder tap logged a 429 and a 5xx alike as
`raise:RuntimeError`. Each no-quote ladder also sent 16-20 `0x0`->`latest` getLogs that Infura
refuses with -32602 on every call.

F1  the tap outcome and the gate read carry the provider's class (http_429, rpc_error:-32603, ...).
F2  a 429 types as provider_rpc_rate_limit, so the retry lane waits MEMECOIN_ENTRY_RETRY_RPC_429_S.
F3  a single-member endpoint that structurally refused the unbounded Initialize scan is not asked
    again for that shape until the memory's TTL ends.
"""
import json
import logging
import urllib.error

import pytest

from sniper.analysis.memecoin_quote_failures import QuoteFailure, is_infra
from sniper.execution import evm_real_seams as S
from sniper.execution import evm_v4_quote as V
from sniper.execution import evm_wallet as w
import sniper.services.memecoin_real_runner as R

URL = "https://mainnet.infura.example/v3/SECRETKEY0123456789"
OWNER = "0x" + "12" * 20
POOL = "0x" + "ab" * 32


def _http(status, body):
    def http(method, url, headers=None, body_=None, timeout=10, **_kw):
        return status, body, {}
    return http


def _raising_http(exc):
    def http(method, url, headers=None, body=None, timeout=10, **_kw):
        raise exc
    return http


RATE_LIMITED = _http(429, {"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32005, "message": "Too Many Requests"}})
OUTAGE = _http(503, {"jsonrpc": "2.0", "id": 1,
                     "error": {"code": -32603, "message": "service temporarily unavailable"}})
INTERNAL = _http(200, {"jsonrpc": "2.0", "id": 1,
                       "error": {"code": -32603, "message": "Internal error"}})
BALANCE = _http(200, {"jsonrpc": "2.0", "id": 1, "result": hex(2 * 10 ** 17)})


# --- F1: the provider's class is named --------------------------------------------------------

@pytest.mark.parametrize("http, outcome", [
    (RATE_LIMITED, "http_429"), (OUTAGE, "http_503")])
def test_the_rpc_tap_names_the_http_status_of_a_provider_failure(http, outcome):
    w.start_rpc_tap()
    with pytest.raises(RuntimeError):
        w._rpc(URL, "eth_blockNumber", [], http=http)
    [rec] = w.take_rpc_tap()
    assert rec["outcome"] == outcome


def test_the_rpc_tap_keeps_the_exception_class_for_anything_without_a_status():
    w.start_rpc_tap()
    with pytest.raises(urllib.error.URLError):
        w._rpc(URL, "eth_blockNumber", [], http=_raising_http(urllib.error.URLError("reset")))
    [rec] = w.take_rpc_tap()
    assert rec["outcome"] == "raise:URLError"


def test_an_error_body_from_eth_call_is_still_a_keyerror_and_carries_its_code():
    with pytest.raises(KeyError) as got:
        w._eth_call("0x" + "00" * 20, "70a08231", URL, http=INTERNAL)
    assert got.value.rpc_code == -32603
    assert str(got.value) == "'result'", "callers that print the error see what they always saw"


@pytest.mark.parametrize("http, cls", [
    (RATE_LIMITED, "http_429"),
    (OUTAGE, "http_503"),
    (INTERNAL, "rpc_error:-32603"),
    (_raising_http(urllib.error.URLError("reset")), "transport:URLError"),
    (_raising_http(TimeoutError("slow")), "timeout"),
])
def test_a_failed_quote_balance_read_is_none_and_names_its_class_once(http, cls, caplog):
    with caplog.at_level(logging.WARNING, logger="memecoin"):
        got = w.quote_balance_usd_or_none(OWNER, "1", URL, http=http, price_fn=lambda: 2500.0)
    assert got is None, "unknown is never 0.0"
    assert w.last_read_failure() == cls
    lines = [r.getMessage() for r in caplog.records if "quote balance read failed" in r.getMessage()]
    assert len(lines) == 1 and cls in lines[0]
    assert "SECRETKEY" not in lines[0], "the URL carries the key; log the host only"


def test_a_good_read_clears_the_previous_failure_on_this_thread():
    w.quote_balance_usd_or_none(OWNER, "1", URL, http=RATE_LIMITED, price_fn=lambda: 2500.0)
    got = w.quote_balance_usd_or_none(OWNER, "1", URL, http=BALANCE, price_fn=lambda: 2500.0)
    assert got == pytest.approx(500.0)
    assert w.last_read_failure() is None


def test_an_unknown_price_is_named_without_a_chain_read():
    calls = []

    def http(*a, **k):
        calls.append(a)
        return BALANCE(*a, **k)
    assert w.quote_balance_usd_or_none(OWNER, "1", URL, http=http, price_fn=lambda: None) is None
    assert w.last_read_failure() == "price_unavailable"
    assert calls == []


# --- F2: a rate limit backs off -----------------------------------------------------------------

def _reset_gate():
    with R._gate_balance_lock:
        R._gate_balance_cache.clear()


def test_the_gate_records_the_quote_reads_class_beside_its_none():
    _reset_gate()
    failures = {}
    quote, native, cached = R._gate_balances(
        "1", lambda: w.quote_balance_usd_or_none(OWNER, "1", URL, http=RATE_LIMITED,
                                                 price_fn=lambda: 2500.0),
        lambda: 0.01, now_ts=100.0, failures=failures)
    assert (quote, native, cached) == (None, 0.01, False)
    assert failures == {"quote": "http_429"}


def test_a_raising_gate_read_records_its_class_too():
    _reset_gate()
    failures = {}

    def boom():
        raise w._http_failure(429)
    R._gate_balances("1", boom, lambda: 0.01, now_ts=100.0, failures=failures)
    assert failures == {"quote": "http_429"}


@pytest.mark.parametrize("failures, reason", [
    ({}, "quote balance unavailable"),
    ({"quote": None}, "quote balance unavailable"),
    ({"quote": "http_429"}, "quote balance unavailable (provider_rpc_rate_limit: http_429)"),
    ({"quote": "rpc_error:-32603"}, "quote balance unavailable (rpc_error:-32603)"),
])
def test_the_refusal_keeps_its_prefix_and_names_the_class(failures, reason):
    assert R._quote_balance_unavailable_reason(failures) == reason


@pytest.mark.parametrize("reason, delay", [
    ("quote balance unavailable", 6.0),
    ("quote balance unavailable (rpc_error:-32603)", 6.0),
    ("quote balance unavailable (provider_rpc_rate_limit: http_429)", 20.0),
])
def test_every_quote_balance_refusal_retries_and_only_a_rate_limit_waits(reason, delay, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_RPC_429_S", 20.0)
    res = {"status": "rejected", "reason": reason}
    assert R._is_transient_entry_miss(res)
    assert R._entry_retry_delay(res, 6.0, attempts=1, network_id="1") == delay


def test_a_rate_limited_event_is_tried_three_times_in_the_live_window_not_nine(monkeypatch):
    """Live .env: MEMECOIN_ENTRY_RETRY_WINDOW_S=60, _INTERVAL_S=6; the lane reaps at window-interval.
    HALCYON today: 9 attempts in 62 s, 8 of them provider work."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RETRY_RPC_429_S", 20.0)
    window, interval = 60.0, 6.0

    def tries(reason):
        res = {"status": "rejected", "reason": reason}
        t, n = 0.0, 1
        while True:
            t += R._entry_retry_delay(res, interval, attempts=n - 1, network_id="1")
            if t > window - interval:
                return n
            n += 1
    assert tries(R._quote_balance_unavailable_reason({"quote": "http_429"})) == 3
    assert tries(R._quote_balance_unavailable_reason({})) == 10


def _no_store(monkeypatch):
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: None)
    V._key_cache.clear()
    V.begin_quote()


def test_a_rate_limited_pool_key_read_is_blind_as_a_rate_limit(monkeypatch):
    _no_store(monkeypatch)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a: (URL,))

    def rpc(url, method, params, http=None):
        raise w._http_failure(429)
    monkeypatch.setattr(V, "_rpc", rpc)
    assert V._pool_key("1", POOL, URL) is None
    assert V.last_provider_blind() == "rpc_rate_limit"


def test_a_refused_pool_key_read_without_a_429_keeps_its_old_reason(monkeypatch):
    _no_store(monkeypatch)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    monkeypatch.setattr(V, "_rpc", lambda *a, **k: {"error": {"code": -32603, "message": "x"}})
    assert V._pool_key("1", POOL, URL) is None
    assert V.last_provider_blind() == "poolkey_read_failed"


def test_a_rate_limited_v4_rung_codes_the_miss_as_a_rate_limit_and_stays_infra():
    code, _detail = S.classify_quote_miss([
        {"rung": "v3", "hit": False},
        {"rung": "v4", "hit": False, "blind_reason": "rpc_rate_limit"},
        {"rung": "v4_multihop", "hit": False, "blind_reason": "discovery_cooldown"}])
    assert code == "provider_rpc_rate_limit"
    failure = S.QUOTE_MISS_FAILURES[code]
    assert is_infra(failure), "it must never advance a token's unsellable count"
    assert failure is not QuoteFailure.RATE_LIMIT, "an exit must not hold its group's quote budget"


# --- F3: a refused full-range scan is not re-asked ------------------------------------------------

def _full_range(params):
    return params and params[0].get("fromBlock") == "0x0" and params[0].get("toBlock") == "latest"


@pytest.fixture
def scan(monkeypatch):
    """`_pool_key` on chain 1 with a head of 5,000 blocks: one bounded read, then the fallback."""
    _no_store(monkeypatch)
    monkeypatch.setattr(V, "_full_range_refused", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(V, "_now", lambda: clock["t"])
    asked = []
    answers = {}

    def rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(5000)}
        if not _full_range(params):
            return {"result": []}
        asked.append(url)
        return answers.get(url, {"result": []})
    monkeypatch.setattr(V, "_rpc", rpc)
    return {"clock": clock, "asked": asked, "answers": answers, "mp": monkeypatch}


def _lookup(n):
    V._key_cache.clear()
    V.begin_quote()
    return V._pool_key("1", "0x" + ("%02x" % n) * 32, URL)


def test_a_single_endpoint_that_refused_the_full_range_is_not_asked_again_until_the_ttl(scan):
    scan["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    scan["answers"][URL] = {"error": {"code": -32602, "message": "range 26139857 exceeds limit of 10000"}}
    assert _lookup(1) is None and V.last_provider_blind() == "poolkey_read_failed"
    assert _lookup(2) is None and V.last_provider_blind() == "poolkey_read_failed"
    assert scan["asked"] == [URL], "the refused shape is asked once"
    scan["clock"]["t"] += V._FULL_RANGE_REFUSAL_TTL_S + 1
    _lookup(3)
    assert scan["asked"] == [URL, URL], "a plan upgrade is not remembered forever"


def test_an_endpoint_that_serves_the_full_range_is_asked_every_time(scan):
    scan["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    _lookup(1)
    _lookup(2)
    assert scan["asked"] == [URL, URL]


def test_a_transient_error_on_the_full_range_is_not_remembered(scan):
    scan["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    scan["answers"][URL] = {"error": {"code": -32603, "message": "service temporarily unavailable"}}
    _lookup(1)
    _lookup(2)
    assert scan["asked"] == [URL, URL]


def test_an_archive_spare_is_still_asked_when_the_primary_is_remembered(scan):
    spare = "https://archive.example"
    scan["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL, spare))
    scan["mp"].setattr(V, "_archive_is_chain", lambda *a, **k: True)
    scan["answers"][URL] = {"error": {"code": -32602, "message": "range 1 exceeds limit of 10000"}}
    _lookup(1)
    _lookup(2)
    assert scan["asked"] == [URL, spare, spare]


def test_a_pooled_url_is_left_to_the_pools_own_refusal_memory(scan):
    from sniper.execution import evm_rpc_pool
    scan["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    scan["mp"].setattr(evm_rpc_pool, "pool_for", lambda url, cfg=None: [url, "https://member.example"])
    scan["answers"][URL] = {"error": {"code": -32602, "message": "range 1 exceeds limit of 10000"}}
    _lookup(1)
    _lookup(2)
    assert scan["asked"] == [URL, URL], "another member may serve it; one URL is not the pool"


def test_a_rate_limit_never_joins_the_immediate_one_shot_requote():
    """385c8b40 and F2: a provider_rpc_rate_limit miss waits for the retry lane's 20 s; evm_entry_fn's
    one-shot re-quote set (runner ~2362) must never hold it (Gamma, 2026-10-07)."""
    import inspect
    import re
    src = inspect.getsource(R._make_evm_entry_fn)
    block = src.split('result.get("quote_miss_code") in {', 1)[1].split("}", 1)[0]
    codes = set(re.findall(r'"(provider_[a-z_]+)"', block))
    assert "provider_poolkey_read_failed" in codes, "the set this test reads has moved"
    assert "provider_rpc_rate_limit" not in codes


# --- F4/F5: an old pool the endpoint cannot reach is not re-paged; a 429 parks the key scan ------
# BUNKER 2026-10-07 15:30Z (0xsneakky): the v4_multihop bridge leg asked the keys of two USDC/WETH
# pools initialised 1.8-3.2M blocks back. Six 10k pages cannot reach them and Infura refuses
# 0x0->latest, so nothing was stored and EVERY attempt re-sent ~8 getLogs (255 credits each) per pool;
# the burst 429'd the parallel v4 round-trip eth_call and the next balance reads.

HEAD = 200_000                                    # deep enough for every page to run


@pytest.fixture
def deep(monkeypatch):
    _no_store(monkeypatch)
    monkeypatch.setattr(V, "_full_range_refused", {})
    monkeypatch.setattr(V, "_scan_miss", {})
    monkeypatch.setattr(V, "_scan_429", {})
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a: (URL,))
    clock = {"t": 1000.0}
    monkeypatch.setattr(V, "_now", lambda: clock["t"])
    calls, state = [], {"born": None, "rate_limit": False}

    def rpc(url, method, params, http=None):
        calls.append((url, method, params[0] if params else None))
        if state["rate_limit"]:
            raise w._http_failure(429)
        if method == "eth_blockNumber":
            return {"result": hex(HEAD)}
        if _full_range(params):
            return {"error": {"code": -32602, "message": "range %d exceeds limit of 10000" % HEAD}}
        lo, hi = int(params[0]["fromBlock"], 16), int(params[0]["toBlock"], 16)
        born = state["born"]
        return {"result": [INIT_LOG] if born is not None and lo <= born <= hi else []}
    monkeypatch.setattr(V, "_rpc", rpc)
    return {"clock": clock, "calls": calls, "state": state, "mp": monkeypatch}


INIT_LOG = {"topics": [], "data": "0x"}          # decode is stubbed where a hit matters


def _logs(calls):
    return [c for c in calls if c[1] == "eth_getLogs"]


def test_an_old_pool_is_paged_once_then_only_its_window_is_read_until_the_ttl(deep):
    assert _lookup(7) is None
    first = len(_logs(deep["calls"]))
    assert first == 1 + V._V4_KEY_SCAN_MAX_PAGES + 1, "window, every page, the refused full range"
    deep["calls"].clear()
    assert _lookup(7) is None
    assert len(_logs(deep["calls"])) == 1, "only the window: a pool born since is still found"
    deep["calls"].clear()
    deep["clock"]["t"] += V._KEY_SCAN_MISS_TTL_S + 1
    _lookup(7)
    assert len(_logs(deep["calls"])) == first, "the memory ends: the endpoint may have changed"


def test_a_pool_born_after_the_miss_is_found_by_the_window(deep):
    pool = "0x" + "07" * 32
    _lookup(7)
    deep["state"]["born"] = HEAD - 5
    deep["mp"].setattr(V, "decode_initialize_log", lambda log: {"fee": 3000, "tick_spacing": 60})
    assert _lookup(7) == {"fee": 3000, "tick_spacing": 60}
    assert ("1", pool) in V._key_cache


def test_a_walk_cut_by_a_rate_limit_is_never_remembered_as_a_miss(deep):
    deep["mp"].setattr(V, "_KEY_SCAN_429_COOLDOWN_S", 0.0)
    deep["state"]["rate_limit"] = True
    assert _lookup(7) is None and V.last_provider_blind() == "rpc_rate_limit"
    deep["state"]["rate_limit"] = False
    deep["calls"].clear()
    _lookup(7)
    assert len(_logs(deep["calls"])) == 1 + V._V4_KEY_SCAN_MAX_PAGES + 1, "a full walk, not a memo"


def test_an_endpoint_that_serves_the_full_range_never_memoizes(deep):
    deep["mp"].setattr(V, "_rpc", lambda url, method, params, http=None: (
        {"result": hex(HEAD)} if method == "eth_blockNumber" else {"result": []}))
    _lookup(7)
    assert not V._scan_miss, "an empty full-range answer is a clean absence, asked again next time"


def test_a_429_parks_the_key_scan_on_that_url_and_sends_nothing_until_the_cooldown(deep):
    deep["state"]["rate_limit"] = True
    assert _lookup(7) is None and V.last_provider_blind() == "rpc_rate_limit"
    deep["state"]["rate_limit"] = False
    deep["calls"].clear()
    assert _lookup(8) is None and V.last_provider_blind() == "rpc_rate_limit"
    assert deep["calls"] == [], "our own scan must not keep a 429'd custody key hot"
    assert V._pool_key("1", "0x" + "08" * 32, "https://other.example") is None
    assert deep["calls"], "the cooldown is per URL"
    deep["calls"].clear()
    deep["clock"]["t"] += V._KEY_SCAN_429_COOLDOWN_S + 1
    _lookup(8)
    assert _logs(deep["calls"]), "after the cooldown the scan reads again"


def test_zero_ttls_restore_todays_behaviour(deep):
    deep["mp"].setattr(V, "_KEY_SCAN_MISS_TTL_S", 0.0)
    deep["mp"].setattr(V, "_KEY_SCAN_429_COOLDOWN_S", 0.0)
    deep["mp"].setattr(V, "_KEY_SCAN_DEPTH_TTL_S", 0.0)      # the resume memory is a TTL too
    _lookup(7)
    deep["calls"].clear()
    _lookup(7)
    assert len(_logs(deep["calls"])) == 1 + V._V4_KEY_SCAN_MAX_PAGES, "pages again (full range cooled)"
    deep["state"]["rate_limit"] = True
    _lookup(9)
    deep["state"]["rate_limit"] = False
    deep["calls"].clear()
    _lookup(9)
    assert deep["calls"], "no cooldown"


def test_a_window_read_that_errored_is_never_remembered_as_a_miss(deep):
    """Alpha's pin: only the clean shape counts. A refused or failed window proves nothing."""
    inner = V._rpc

    def rpc(url, method, params, http=None):
        p = params[0] if params else {}
        if method == "eth_getLogs" and p.get("fromBlock") == hex(HEAD - V._V4_DISCOVERY_LOOKBACK_BLOCKS):
            return {"error": {"code": -32603, "message": "Internal error"}}
        return inner(url, method, params, http=http)
    deep["mp"].setattr(V, "_rpc", rpc)
    _lookup(7)
    assert len(_logs(deep["calls"])) == V._V4_KEY_SCAN_MAX_PAGES + 1, "pages + full (window unrecorded)"
    assert not V._scan_miss


def test_the_custody_reads_never_consult_the_key_scan_cooldown(deep):
    """Alpha's pin: the guard parks only `_pool_key`; a balance or eth_call read still goes out."""
    V._scan_429[URL] = V._now()
    sent = []

    def http(method, url, headers=None, body=None, timeout=10, **_kw):
        sent.append(json.loads(body)["method"])
        return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(10 ** 17)}, {}
    assert w._rpc(URL, "eth_getBalance", [OWNER, "latest"], http=http)["result"] == hex(10 ** 17)
    assert sent == ["eth_getBalance"]


def test_chain_1_takes_an_archive_list_and_defaults_to_none():
    from sniper.settings.memecoin import _evm_archive_rpc_pool
    assert "1" not in _evm_archive_rpc_pool({})
    assert _evm_archive_rpc_pool({"MEMECOIN_EVM_ARCHIVE_RPC_URLS_1": "https://a.example, https://b.example"})[
        "1"] == ("https://a.example", "https://b.example")


def test_a_transient_full_range_error_is_never_remembered_as_a_miss(deep):
    """-32603 is not a refusal of the shape: the endpoint may serve the full range next time."""
    inner = V._rpc

    def rpc(url, method, params, http=None):
        if method == "eth_getLogs" and _full_range(params):
            deep["calls"].append((url, method, params[0]))
            return {"error": {"code": -32603, "message": "service temporarily unavailable"}}
        return inner(url, method, params, http=http)
    deep["mp"].setattr(V, "_rpc", rpc)
    _lookup(7)
    assert not V._scan_miss


ARCHIVE = "https://archive.example"


def test_an_archive_whose_identity_read_failed_keeps_the_miss_open(deep):
    """Gamma LOW A: an unreadable archive is not a wrong chain; its 3600 s must not be lost."""
    deep["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL, ARCHIVE))
    inner = V._rpc

    def rpc(url, method, params, http=None):
        if url == ARCHIVE:
            raise w._http_failure(503)
        return inner(url, method, params, http=http)
    deep["mp"].setattr(V, "_rpc", rpc)
    _lookup(7)
    assert not V._scan_miss


def test_a_window_429_sends_nothing_more_to_that_url(deep):
    """Gamma LOW B: the walk and the full range must not follow a 429'd window to the same URL."""
    inner = V._rpc
    seen = []

    def rpc(url, method, params, http=None):
        seen.append(method)
        if method == "eth_getLogs" and len(seen) == 2:
            raise w._http_failure(429)
        return inner(url, method, params, http=http)
    deep["mp"].setattr(V, "_rpc", rpc)
    assert _lookup(7) is None and V.last_provider_blind() == "rpc_rate_limit"
    assert seen == ["eth_blockNumber", "eth_getLogs"]


def test_a_parked_archive_is_not_asked_for_the_full_range(deep):
    """Gamma NIT C: an archive's own 429 parks it too."""
    deep["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL, ARCHIVE))
    deep["mp"].setattr(V, "_archive_is_chain", lambda *a, **k: True)
    V._scan_429[ARCHIVE] = V._now()
    _lookup(7)
    assert not any(u == ARCHIVE for u, _m, _p in deep["calls"])
    assert not V._scan_miss, "a parked endpoint proves nothing"


@pytest.mark.parametrize("answer, memo", [
    ({"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}, False),
    ({"jsonrpc": "2.0", "id": 1, "result": None}, False),
    ({"jsonrpc": "2.0", "id": 1, "result": "0x38"}, True)])     # a proven other chain
def test_only_a_parsed_chain_mismatch_lets_an_archive_be_skipped_into_a_memo(deep, answer, memo):
    """Gamma on 1d1a3449c: an error body at HTTP 200 is unread, not a wrong chain."""
    deep["mp"].setattr(V, "_archive_rpc_urls", lambda *a: (URL, ARCHIVE))
    inner = V._rpc

    def rpc(url, method, params, http=None):
        if url == ARCHIVE and method == "eth_chainId":
            return answer
        return inner(url, method, params, http=http)
    deep["mp"].setattr(V, "_rpc", rpc)
    _lookup(7)
    assert bool(V._scan_miss) is memo
