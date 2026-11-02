"""Durable V4 PoolKey cache and archive-routed discovery."""
import sqlite3

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.persistence import db_memecoin as m


CHAIN = "4663"
POOL = "0x" + "ab" * 32
MANAGER = V.V4[CHAIN]["pool_manager"]
KEY = {
    "currency0": "0x" + "00" * 20,
    "currency1": "0x" + "11" * 20,
    "fee": 3000,
    "tick_spacing": 60,
    "hooks": "0x" + "22" * 20,
}


@pytest.fixture(autouse=True)
def _isolate_the_durable_pool_key_store(monkeypatch):
    """Every test here must see an EMPTY PoolKey store unless it builds one itself.

    Flake found 2026-09-02, ~1 in 5 at `-n 6`. `_pool_key` persists a resolved key through
    `_store_pool_key`, and conftest points SNIPER_STATE_ROOT at ONE throwaway dir per session — so
    `test_pool_key_uses_the_archive_read_after_primary_transport_failure` resolves POOL -> KEY and
    writes it, and any later test in the SAME xdist worker that calls `_pool_key(CHAIN, POOL, ...)`
    reads the sibling's leftover instead of exercising its own path.

    `V._key_cache.clear()` was not enough: it clears the in-memory cache and leaves the durable
    store. The symptom was `_pool_key` returning KEY where the test asserted None, and it moved
    with worker assignment rather than with anything the test did — which is what made it look like
    a threading problem rather than shared state on disk.

    Tests that exercise the store itself drive `db_memecoin` directly and are unaffected.
    """
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: None)
    V._key_cache.clear()
    V.begin_quote()


def test_verified_v4_pool_key_survives_a_process_cache_miss():
    """A verified immutable PoolKey must not need a second historical RPC lookup."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)

    m.put_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=POOL, pool_manager=MANAGER, key=KEY)

    assert m.get_evm_v4_pool_key(
        conn, chain_id=CHAIN, pool_id=POOL, pool_manager=MANAGER) == KEY
    assert m.get_evm_v4_pool_key(
        conn, chain_id=CHAIN, pool_id=POOL, pool_manager="0x" + "ff" * 20) is None


def test_pool_key_cache_rejects_a_malformed_currency():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    bad = dict(KEY, currency1="not-an-address")

    m.put_evm_v4_pool_key(conn, chain_id=CHAIN, pool_id=POOL, pool_manager=MANAGER, key=bad)

    assert m.get_evm_v4_pool_key(
        conn, chain_id=CHAIN, pool_id=POOL, pool_manager=MANAGER) is None


def test_empty_primary_log_response_is_a_clean_no_pool(monkeypatch):
    V._key_cache.clear()
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *_args: ("https://primary", "https://archive"),
                        raising=False)
    seen = []

    def rpc(url, _m, _params, http=None):
        if _m == "eth_blockNumber":   # leading head read; not what this test asserts
            return {"result": hex(50_000_000)}
        seen.append(url)
        return {"result": []}

    monkeypatch.setattr(V, "_rpc", rpc)
    V.begin_quote()

    assert V._pool_key(CHAIN, POOL, "https://primary") is None
    # Intent, not call count: `_pool_key` has gained a leading eth_blockNumber and a bounded
    # primary attempt since this was written, so an exact sequence rots. What matters is that an
    # empty answer ends the search without reaching the archive.
    assert "https://archive" not in seen
    assert V.last_provider_blind() is None


def test_archive_read_refuses_an_endpoint_on_the_wrong_chain(monkeypatch):
    V._key_cache.clear()
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *_args: ("https://primary", "https://archive"),
                        raising=False)
    wrong_pool = "0x" + "cd" * 32
    seen = []

    def rpc(url, method, _params, http=None):
        if method == "eth_blockNumber":   # leading head read; not asserted here
            return {"result": hex(50_000_000)}
        seen.append((url, method))
        if url == "https://primary":
            raise OSError("historical log unavailable")
        if method == "eth_chainId":
            return {"result": "0x1"}
        raise AssertionError("the wrong-chain archive endpoint read pool data")

    monkeypatch.setattr(V, "_rpc", rpc)

    assert V._pool_key(CHAIN, wrong_pool, "https://primary") is None
    assert ("https://archive", "eth_chainId") in seen, "the archive must be identity-checked"
    assert ("https://archive", "eth_getLogs") not in seen, \
        "a wrong-chain archive must never be read for pool data"


def test_pool_key_uses_the_archive_read_after_primary_transport_failure(monkeypatch):
    """A public-read failure must not discard a pool key when the archive read can prove it."""
    V._key_cache.clear()
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *_args: ("https://primary", "https://archive"),
                        raising=False)
    seen = []

    def rpc(url, method, _params, http=None):
        if method == "eth_blockNumber":   # leading head read; not asserted here
            return {"result": hex(50_000_000)}
        seen.append(url)
        if url == "https://primary":
            raise OSError("historical log unavailable")
        if method == "eth_chainId":
            return {"result": hex(int(CHAIN))}
        return {"result": [{
            "topics": [V._INIT_TOPIC, POOL, "0x" + "00" * 32,
                       "0x" + "00" * 12 + "11" * 20],
            "data": "0x" + format(3000, "064x") + format(60, "064x")
                    + "00" * 12 + "22" * 20,
        }]}

    monkeypatch.setattr(V, "_rpc", rpc)

    assert V._pool_key(CHAIN, POOL, "https://primary") == KEY
    # Intent, not call count: a primary that RAISES must fall through to the archive, and the
    # archive read must produce the key. The exact sequence rotted when `_pool_key` gained a
    # leading eth_blockNumber and a bounded primary attempt.
    assert "https://archive" in seen, "a raising primary must fall through to the archive"


@pytest.mark.parametrize("override", [None, "100000"])
def test_ethereum_old_pool_key_resolves_within_infuras_range_limit(monkeypatch, override):
    """HALCYON ref 5006a440: Initialize is older than the first 8,192 blocks."""
    monkeypatch.setattr(V, "_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN",
                        dict(V._V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN))
    if override is None:
        monkeypatch.delenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_1", raising=False)
    else:
        monkeypatch.setenv("MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_1", override)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda *a: ())
    head, initialized = 1000000, 989000
    spans = []
    log = {
        "topics": [V._INIT_TOPIC, POOL, "0x" + "00" * 32,
                   "0x" + "00" * 12 + "11" * 20],
        "data": "0x" + format(3000, "064x") + format(60, "064x")
                + "00" * 12 + "22" * 20,
    }

    def rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(head)}
        lower, upper = (int(params[0][key], 16) for key in ("fromBlock", "toBlock"))
        span = upper - lower
        spans.append(span)
        if span > 10000:
            return {"error": {"code": -32602,
                              "message": f"range {span} exceeds limit of 10000"}}
        return {"result": [log] if lower <= initialized <= upper else []}

    monkeypatch.setattr(V, "_rpc", rpc)
    assert V._pool_key("1", POOL, "https://infura.example") == KEY
    assert spans == ([8192, 10000] if override is None else [8192, 100000, 10000])
    assert V._key_scan_page_blocks("4663") == V._V4_KEY_SCAN_PAGE_BLOCKS


@pytest.mark.parametrize("limit", ["0", "99", "100000", "100001", "garbage"])
def test_infura_range_learning_ignores_invalid_or_nonshrinking_limits(monkeypatch, limit):
    monkeypatch.setattr(V, "_V4_KEY_SCAN_PAGE_BLOCKS_BY_CHAIN", {})
    response = {"error": {"code": -32602,
                          "message": f"range 100000 exceeds limit of {limit}"}}
    assert V._learn_range_limit("1", response, 100000) is None
    assert V._key_scan_page_blocks("1") == V._V4_KEY_SCAN_PAGE_BLOCKS


# --- blind-site attribution and the archive ordering fix (2026-09-02) ---------------------------
#
# Until today `_pools_for_mint` (discovery) and `_pool_key` (resolution) both wrote the SAME
# "rpc_transport" string, so 40 of 71 v4 failures on 4663 could not be attributed to a function.
# Every branch of that investigation was downstream of a label that did not name its writer.
#
# The ordering bug: `_archive_rpc_urls` returns the PRIMARY first, and an empty answer did
# `return None` from the whole function — so a primary that answers empty rather than raising meant
# no configured archive was ever asked. Buying an archive endpoint would have changed nothing.

import pytest


def _no_archives(monkeypatch):
    monkeypatch.setattr(V, "_archive_is_chain", lambda *a, **k: True)


def _run_pool_key(monkeypatch, responses, archives=()):
    """Drive _pool_key with a scripted {url: response-or-exception} and no caches."""
    V._key_cache.clear()
    V.begin_quote()
    monkeypatch.setattr(V, "_stored_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_store_pool_key", lambda *a, **k: None)
    monkeypatch.setattr(V, "_archive_rpc_urls", lambda chain, primary: (primary, *archives))
    _no_archives(monkeypatch)
    seen = []

    def fake_rpc(url, method, params, http=None):
        if method == "eth_blockNumber":
            return {"result": hex(50_000_000)}
        seen.append((url, params[0].get("fromBlock")))
        r = responses.get((url, params[0].get("fromBlock")), responses.get(url))
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(V, "_rpc", fake_rpc)
    return V._pool_key(CHAIN, POOL, "primary", http=None), seen


def test_a_refused_poolkey_read_is_named_for_ITS_OWN_site(monkeypatch):
    got, _ = _run_pool_key(monkeypatch, {"primary": RuntimeError("boom")})
    assert got is None
    assert V.last_provider_blind() == "poolkey_read_failed", \
        "resolution failure must not share a label with discovery failure"


def test_a_TIMEOUT_is_distinguished_from_a_refusal(monkeypatch):
    """Different fault, different fix: the full-range read was measured at 1.8-2.4s against a
    3.0s budget already partly spent, so a timeout here is plausible and must be visible."""
    got, _ = _run_pool_key(monkeypatch, {"primary": TimeoutError("slow")})
    assert got is None
    assert V.last_provider_blind() == "poolkey_read_timeout"
def test_every_endpoint_refusing_IS_blindness(monkeypatch):
    got, _ = _run_pool_key(
        monkeypatch, {"primary": RuntimeError("no"), "archive": RuntimeError("no")},
        archives=("archive",))
    assert got is None and V.last_provider_blind() == "poolkey_read_failed"


def test_the_renamed_labels_reach_BOTH_consumer_lists():
    """A guard cannot catch what its own list omits. Renaming without updating these would
    silently reclassify every v4 failure AND stop the provider retry."""
    import pathlib
    seams = pathlib.Path(V.__file__).with_name("evm_real_seams.py").read_text()
    runner = (pathlib.Path(V.__file__).parent.parent
              / "services" / "memecoin_real_runner.py").read_text()
    for name in ("discovery_all_blind", "poolkey_read_failed",
                 "poolkey_read_timeout", "poolkey_malformed"):
        assert f'"provider_{name}"' in seams, f"{name} missing from the QuoteFailure mapping"
        assert f'"provider_{name}"' in runner, f"{name} missing from the provider-retry set"


def test_a_malformed_log_is_named_for_the_PARSE_not_the_transport(monkeypatch):
    """A log we received but could not decode is OUR bug, not the provider's. Sharing a label with
    a transport failure sends the next investigator to the wrong system."""
    got, _ = _run_pool_key(monkeypatch, {"primary": {"result": [{"data": "0xzz", "topics": []}]}})
    assert got is None
    assert V.last_provider_blind() == "poolkey_malformed"


@pytest.mark.parametrize('refusal', [{'error': {'code': -32012, 'message': 'requested range too large'}}, {'result': None}])
def test_refused_page_stops_the_backward_scan(monkeypatch, refusal):
    calls = []
    monkeypatch.setattr(V, '_archive_rpc_urls', lambda chain, primary: (primary,))
    def rpc(url, method, params, http=None):
        if method == 'eth_blockNumber':
            return {'result': hex(50_000_000)}
        calls.append(params[0])
        return refusal
    monkeypatch.setattr(V, '_rpc', rpc)
    assert V._pool_key(CHAIN, POOL, 'primary') is None
    assert len(calls) == 3  # Recent window, one backward page, archive fallback.
    assert V.last_provider_blind() == 'poolkey_read_failed'


def test_arc_finds_an_older_key_within_the_provider_range_limit(monkeypatch):
    monkeypatch.delenv('MEMECOIN_V4_KEY_SCAN_PAGE_BLOCKS_5042', raising=False)
    monkeypatch.setattr(V, '_archive_rpc_urls', lambda chain, primary: (primary,))
    pages = []
    def rpc(url, method, params, http=None):
        if method == 'eth_blockNumber':
            return {'result': hex(50_000_000)}
        page = params[0]
        if page['toBlock'] == 'latest':
            return {'error': {'code': -32012}}
        width = int(page['toBlock'], 16) - int(page['fromBlock'], 16) + 1
        pages.append(width)
        if width > 10000:
            return {'error': {'code': -32012}}
        if len(pages) == 1:
            return {'result': []}
        return {'result': [{'topics': [V._INIT_TOPIC, POOL, '0x' + '00' * 32,
            '0x' + '00' * 12 + '11' * 20], 'data': '0x' + V._w(3000) + V._w(60) + V._wa(KEY['hooks'])}]}
    monkeypatch.setattr(V, '_rpc', rpc)
    assert V._pool_key('5042', POOL, 'primary') == KEY
    assert pages == [8193, 10000]
