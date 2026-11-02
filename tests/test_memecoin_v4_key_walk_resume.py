"""A retried PoolKey walk resumes where the last one stopped (2026-10-07, Alpha P0).

0xsneakky laddering 0x24c2c6f7 on chain 1, 5 attempts 17:37:02-47Z (Gamma): every ladder pass
re-walked the same pool ids from the head -- 0x2e3e9b65 window + 6 pages, 0xcbca9e82 3-4 reads, the
mint's Initialize scan 11 reads -- because a walk is remembered only on a CLEAN miss and any 429 in
the burst disqualified it. The burst drew Infura 429s and quarantined both pool members, and the
next reads failed NoEligibleReadEndpoint.

Now the contiguous range a walk ANSWERED empty below its first window survives for
MEMECOIN_V4_KEY_SCAN_DEPTH_TTL_S (0 = today). A retry re-reads the top window (a pool initialised
since is found there; it also covers log-index lag) and pages on below the remembered bottom with
what is left of the same page cap. An error never extends the memory. The mint discovery's full
range joins the full-range refusal memory.
"""
import json

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_v4_quote as v4
from sniper.settings import memecoin as cfg

PRIMARY = "https://custody.example"
BACKUP = "https://backup.example"
POOL_ID = "0x" + "ab" * 32
MINT = "0x" + "cd" * 20
HEAD = 30_000_000
WINDOW = 8192                       # chain 1: min(8192, its 10,000-block page)
PAGE = 10_000
REFUSAL = {"error": {"code": -32602, "message": "range 30000000 exceeds limit of 10000"}}
INIT_LOG = {"data": "0x", "topics": []}


class Chain:
    """Two pool members; head and per-range answers scripted by the test."""

    def __init__(self):
        self.head, self.calls, self.fail_pages, self.found_above = HEAD, [], set(), None
        self.full_429 = False

    def http(self, _method, url, *, body, **_kw):
        request = json.loads(body)
        method, params = request["method"], request["params"]
        self.calls.append((url, method, params))
        if method == "eth_blockNumber":
            return 200, {"result": hex(self.head)}, {}
        if method == "eth_chainId":
            return 200, {"result": "0x1"}, {}
        flt = params[0]
        if flt.get("fromBlock") == "0x0":
            if self.full_429:
                return 429, {"error": {"code": -32005, "message": "Too Many Requests"}}, {}
            return 200, dict(REFUSAL), {}
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        if (lo, hi) in self.fail_pages:
            return 200, {"error": {"code": -32603, "message": "Internal error"}}, {}
        if self.found_above is not None and hi >= self.found_above:
            return 200, {"result": [dict(INIT_LOG)]}, {}
        return 200, {"result": []}, {}

    def pages(self):
        """(from, to) of every bounded Initialize read, in order."""
        return [(int(p[0]["fromBlock"], 16), int(p[0]["toBlock"], 16)) for _u, m, p in self.calls
                if m == "eth_getLogs" and p[0].get("fromBlock") != "0x0"]


@pytest.fixture
def chain(monkeypatch):
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
    monkeypatch.setattr(v4, "_scan_depth", {}, raising=False)
    monkeypatch.setattr(v4, "_stored_pool_key", lambda *a: None)
    monkeypatch.setattr(v4, "_store_pool_key", lambda *a: None)
    monkeypatch.setattr(v4, "_archive_rpc_urls", lambda *a: (PRIMARY,))
    monkeypatch.setattr(v4, "decode_initialize_log", lambda _log: {"fee": 500})
    monkeypatch.delenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_1", raising=False)
    for url, ms in ((PRIMARY, 150), (BACKUP, 80)):
        pool.mark_identity(url, True)
        pool.record(url, ms, True)
    c = Chain()
    c.clock = clock
    yield c
    pool.reset_for_tests()
    v4.begin_quote()


def _ask(chain):
    return v4._pool_key("1", POOL_ID, PRIMARY, http=chain.http)


def _all_pages_from(head):
    """The window and six pages today's walk reads from `head`."""
    out, upper = [(head - WINDOW, head)], head - WINDOW
    for _ in range(6):
        out.append((upper - PAGE, upper))
        upper -= PAGE
    return out


def test_a_fully_walked_but_unclean_scan_is_not_re_paged(chain):
    """THE regression (Gamma's 0x2e3e9b65): all six pages answered [], but a 429 on the full range
    kept the walk from being a clean miss, so every retry re-read all seven ranges."""
    chain.full_429 = True
    assert _ask(chain) is None
    assert chain.pages() == _all_pages_from(HEAD)
    assert not v4._scan_miss                        # unclean: today's clean-miss memo does not apply
    chain.calls.clear()
    chain.clock[0] += 61                            # past the 429 park, inside the depth TTL
    chain.head += 5
    assert _ask(chain) is None
    assert chain.pages() == [(HEAD + 5 - WINDOW, HEAD + 5)], "only the top window is re-read"


def test_a_page_error_keeps_what_was_answered_and_the_retry_resumes_there(chain):
    failed = (HEAD - WINDOW - 3 * PAGE, HEAD - WINDOW - 2 * PAGE)        # the third page
    chain.fail_pages.add(failed)
    assert _ask(chain) is None
    assert chain.pages() == _all_pages_from(HEAD)[:4]
    chain.fail_pages.clear()
    chain.calls.clear()
    chain.clock[0] += 5
    assert _ask(chain) is None
    # The window, then the failed page again (an error never extends the memory) and the rest of
    # the same six-page cap: 2 answered before, 4 now. Never deeper than today's walk.
    assert chain.pages() == [(HEAD - WINDOW, HEAD)] + _all_pages_from(HEAD)[3:]


def test_a_pool_initialised_after_the_first_walk_is_found_in_the_top_window(chain):
    chain.full_429 = True
    assert _ask(chain) is None
    chain.calls.clear()
    chain.clock[0] += 61
    chain.head += 40
    chain.found_above = HEAD + 20                   # its Initialize landed after the first walk
    assert _ask(chain) == {"fee": 500}
    assert len(chain.pages()) == 1


def test_a_stale_memory_is_dropped_and_the_walk_runs_as_today(chain):
    chain.full_429 = True
    assert _ask(chain) is None
    chain.calls.clear()
    chain.clock[0] += 61
    chain.head += WINDOW // 2 + 2                   # the new window no longer meets the trusted range
    assert _ask(chain) is None
    assert chain.pages() == _all_pages_from(chain.head)


def test_the_memory_expires_after_its_ttl(chain, monkeypatch):
    monkeypatch.setattr(v4, "_KEY_SCAN_DEPTH_TTL_S", 600.0)
    chain.full_429 = True
    assert _ask(chain) is None
    chain.calls.clear()
    chain.clock[0] += 601
    assert _ask(chain) is None
    assert chain.pages() == _all_pages_from(HEAD)


def test_ttl_zero_is_todays_walk(chain, monkeypatch):
    monkeypatch.setattr(v4, "_KEY_SCAN_DEPTH_TTL_S", 0.0)
    chain.full_429 = True
    assert _ask(chain) is None
    chain.calls.clear()
    chain.clock[0] += 61
    assert _ask(chain) is None
    assert chain.pages() == _all_pages_from(HEAD)


def test_a_found_key_clears_the_memory(chain):
    chain.full_429 = True
    assert _ask(chain) is None
    assert ("1", POOL_ID) in v4._scan_depth
    chain.clock[0] += 61
    chain.found_above = HEAD - 1
    assert _ask(chain) == {"fee": 500}
    assert ("1", POOL_ID) not in v4._scan_depth


@pytest.mark.parametrize("stop_type", [pool.RequestNotSent, pool.NoEligibleReadEndpoint])
@pytest.mark.parametrize("answered", [0, 2, 4])
def test_caller_stop_keeps_clean_pages_and_never_starts_archive_fallback(
        chain, monkeypatch, stop_type, answered):
    calls, stopped = [], [True]
    upper = HEAD - WINDOW - answered * PAGE
    failed = (upper - PAGE, upper)

    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(HEAD)}
        flt = params[0]
        span = (int(flt["fromBlock"], 16), int(flt["toBlock"], 16))
        calls.append(span)
        if stopped[0] and span == failed:
            raise stop_type("offline caller stop")
        return {"result": []}

    monkeypatch.setattr(v4, "_rpc", rpc)
    assert _ask(chain) is None
    memo = v4._scan_depth.get(("1", POOL_ID))
    assert memo is not None and memo[2:4] == (upper, answered)
    assert calls == _all_pages_from(HEAD)[:answered + 2]
    assert not v4._scan_miss and v4.last_provider_blind() is None
    calls.clear()
    stopped[0] = False
    assert _ask(chain) is None
    assert calls[0] == (HEAD - WINDOW, HEAD)
    assert calls[1] == failed, "the refused page is retried; clean deeper pages are not"


@pytest.mark.parametrize("stop_type", [pool.RequestNotSent, pool.NoEligibleReadEndpoint])
def test_caller_stop_on_narrowed_page_keeps_only_the_answered_original_pages(
        chain, monkeypatch, stop_type):
    monkeypatch.setattr(v4, "_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN", {"1": PAGE})
    calls, stopped = [], [True]
    upper = HEAD - WINDOW - 2 * PAGE

    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(HEAD)}
        flt = params[0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        calls.append((lo, hi))
        if hi == upper and hi - lo == PAGE:
            return {"error": {"code": -32602, "message": "range 10000 exceeds limit of 5000"}}
        if stopped[0] and hi == upper:
            raise stop_type("offline narrowed request stopped")
        return {"result": []}

    monkeypatch.setattr(v4, "_rpc", rpc)
    assert _ask(chain) is None
    memo = v4._scan_depth.get(("1", POOL_ID))
    assert memo is not None and memo[2:4] == (upper, 2)
    assert calls[-2:] == [(upper - PAGE, upper), (upper - 5000, upper)]
    assert not v4._scan_miss and v4.last_provider_blind() is None
    calls.clear()
    stopped[0] = False
    assert _ask(chain) is None
    assert calls[:2] == [(HEAD - 5000, HEAD), (upper - 5000, upper)]


@pytest.mark.parametrize("stop_type", [pool.RequestNotSent, pool.NoEligibleReadEndpoint])
def test_unserved_initial_window_never_creates_scan_coverage(chain, monkeypatch, stop_type):
    calls = []
    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(HEAD)}
        calls.append(params[0])
        raise stop_type("offline initial window stopped")
    monkeypatch.setattr(v4, "_rpc", rpc)
    assert _ask(chain) is None
    assert len(calls) == 1 and not v4._scan_depth and not v4._scan_miss


def test_caller_stop_below_an_unanswered_top_window_cannot_claim_contiguous_coverage(
        chain, monkeypatch):
    calls = []
    def rpc(_url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(HEAD)}
        flt = params[0]
        lo, hi = int(flt["fromBlock"], 16), int(flt["toBlock"], 16)
        calls.append((lo, hi))
        if (lo, hi) == (HEAD - WINDOW, HEAD):
            return {"error": {"code": -32603, "message": "unreadable top"}}
        if len(calls) == 4:
            raise pool.RequestNotSent("offline caller stop")
        return {"result": []}
    monkeypatch.setattr(v4, "_rpc", rpc)
    assert _ask(chain) is None
    assert len(calls) == 4 and not v4._scan_depth and not v4._scan_miss


# --- the mint's Initialize discovery joins the full-range refusal memory -------------------------

def test_mint_discovery_does_not_re_ask_a_full_range_both_members_refused(chain):
    assert v4._onchain_pools_for_mint("1", MINT, PRIMARY, http=chain.http) == ([], True)
    full = [u for u, m, p in chain.calls if m == "eth_getLogs" and p[0].get("fromBlock") == "0x0"]
    assert full, "the first pass asks"
    assert v4._full_range_pool_refused_recently(PRIMARY)
    chain.calls.clear()
    # Still blind, never absent: a refused full range proves nothing about the coin.
    assert v4._onchain_pools_for_mint("1", MINT, PRIMARY, http=chain.http) == ([], True)
    assert not [1 for _u, m, p in chain.calls
                if m == "eth_getLogs" and p[0].get("fromBlock") == "0x0"]


def test_the_memory_holds_while_the_new_window_still_meets_the_trusted_range(chain):
    """The boundary of the stale rule: the new window starts exactly one block above the trusted
    range (blocks at least half a window below the old head), so nothing between them is unread."""
    chain.full_429 = True
    assert _ask(chain) is None
    chain.calls.clear()
    chain.clock[0] += 61
    chain.head += WINDOW // 2 + 1
    assert _ask(chain) is None
    assert chain.pages() == [(chain.head - WINDOW, chain.head)]


def test_mint_discovery_does_not_re_ask_a_single_endpoints_refused_full_range(chain, monkeypatch):
    """One member: the pool has nobody to exclude, so the explicit skip is what stops the re-ask."""
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY,)})
    assert v4._onchain_pools_for_mint("1", MINT, PRIMARY, http=chain.http) == ([], True)
    assert v4._full_range_pool_refused_recently(PRIMARY)
    chain.calls.clear()
    assert v4._onchain_pools_for_mint("1", MINT, PRIMARY, http=chain.http) == ([], True)
    assert not [1 for _u, m, p in chain.calls
                if m == "eth_getLogs" and p[0].get("fromBlock") == "0x0"]


def test_base_mint_discovery_window_fits_chain_page_limit(chain):
    spans = []

    def rpc(_url, method, params, http=None):
        if method == 'eth_blockNumber':
            return {'result': hex(HEAD)}
        flt = params[0]
        if flt['fromBlock'] != '0x0':
            spans.append(int(flt['toBlock'], 16) - int(flt['fromBlock'], 16))
            return {'result': [{'topics': [v4._INIT_TOPIC, POOL_ID]}]}
        return {'result': []}

    from unittest.mock import patch
    with patch.object(v4, '_rpc', rpc):
        assert v4._onchain_pools_for_mint('8453', MINT, PRIMARY)[0] == [POOL_ID]
    assert spans and max(spans) <= v4._key_scan_page_blocks('8453')
