"""GeckoTerminal price client for DexScreener-blind chains (Robinhood 4663), 2026-07-16.

The 0x API key died (403 "cannot consume this service"); GT's public API indexes 4663 as network
slug ``robinhood``, free, no key. This client is the primary blind-chain price source, returning the
EXACT stats shape ``evm_price_batch`` emits so it drops into every existing seam. Spec:
docs/superpowers/specs/2026-07-16-memecoin-gt-pricing-design.md
"""
import threading
import urllib.error
import urllib.request

import pytest

from sniper.execution import geckoterminal_quote as G


def _gt_payload(prices, mcaps=None, vols=None):
    """A GeckoTerminal /simple/.../token_price response (addresses are LOWERCASE, prices are strings)."""
    mcaps = mcaps or {}
    vols = vols or {}
    return {"data": {"attributes": {
        "token_prices": {a: str(p) for a, p in prices.items()},
        "market_cap_usd": {a: (None if mcaps.get(a) is None else str(mcaps[a])) for a in prices},
        "h24_volume_usd": {a: str(vols.get(a, 0.0)) for a in prices},
    }}}


def _fake_http(payload, *, status=200, calls=None):
    def http(method, url, headers=None, body=None, timeout=10):
        if calls is not None:
            calls.append(url)
        return status, payload, None
    return http


@pytest.fixture(autouse=True)
def _clean_state():
    G.reset_gt_state()
    yield
    G.reset_gt_state()


# --------------------------------------------------------------------- batch parse

def test_batch_parses_gt_shape_into_evm_price_batch_stats():
    payload = _gt_payload({"0xaaa": 0.5, "0xbbb": 2.0},
                          mcaps={"0xaaa": 1_000_000.0, "0xbbb": None},
                          vols={"0xaaa": 42_000.0, "0xbbb": 0.0})
    out = G.gt_stats_batch_keyed([("4663", "0xAAA"), ("4663", "0xBBB")],
                                 http=_fake_http(payload), clock=lambda: 100.0)
    # keyed by (network, normalized-lowercase mint); exact evm_price_batch shape (now incl. 24h volume)
    assert out[("4663", "0xaaa")] == {"price": 0.5, "liquidity": None,
                                      "mcap": 1_000_000.0, "volume_24h": 42_000.0,
                                      "source": "geckoterminal"}
    # mcap null -> None (RPC totalSupply path remains the real mcap source); 0 volume -> None (no phantom 0)
    assert out[("4663", "0xbbb")]["price"] == 2.0
    assert out[("4663", "0xbbb")]["mcap"] is None
    assert out[("4663", "0xbbb")]["volume_24h"] is None
    assert out[("4663", "0xbbb")]["source"] == "geckoterminal"


def test_never_emits_a_zero_or_nonfinite_tick():
    """A mint priced 0/negative/inf/NaN must be OMITTED, never present with price 0.0 — a 0 reads as
    a -100% tick and fires every stop in the book (the memecoin_evm_price contract)."""
    payload = _gt_payload({"0xzero": 0.0, "0xneg": -1.0, "0xnan": float("nan"),
                           "0xinf": float("inf"), "0xok": 3.0})
    out = G.gt_stats_batch_keyed([("4663", m) for m in
                                  ("0xzero", "0xneg", "0xnan", "0xinf", "0xok")],
                                 http=_fake_http(payload), clock=lambda: 1.0)
    assert set(out) == {("4663", "0xok")}


def test_mcap_zero_string_is_none():
    """GT returns "0.0" mcap for young coins — treat <=0 as unknown, not a real zero market cap."""
    payload = _gt_payload({"0xy": 0.01}, mcaps={"0xy": 0.0})
    out = G.gt_stats_batch_keyed([("4663", "0xy")], http=_fake_http(payload), clock=lambda: 1.0)
    assert out[("4663", "0xy")]["mcap"] is None


def test_batch_skips_networks_without_a_verified_slug():
    """Only the live-verified slug {'4663':'robinhood'} is mapped. Solana (DexScreener prices it)
    and Base (8453, also DexScreener) are skipped — never guess a slug, never HTTP for them."""
    calls = []
    out = G.gt_stats_batch_keyed(
        [("solana", "SoLmint"), ("8453", "0xbase")],
        http=_fake_http(_gt_payload({}), calls=calls), clock=lambda: 1.0)
    assert out == {} and calls == []


def test_batch_chunks_at_30_addresses_per_call():
    mints = ["0x%02x" % i for i in range(65)]
    payload = _gt_payload({m: 1.0 for m in mints})
    calls = []
    out = G.gt_stats_batch_keyed([("4663", m) for m in mints],
                                 http=_fake_http(payload, calls=calls), clock=lambda: 1.0)
    assert len(calls) == 3                       # 30 + 30 + 5
    assert len(out) == 65


def test_http_failure_omits_keys_never_raises():
    def boom(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("gt 500")
    assert G.gt_stats_batch_keyed([("4663", "0xa")], http=boom, clock=lambda: 1.0) == {}
    # non-200 -> also empty, no raise
    out = G.gt_stats_batch_keyed([("4663", "0xa")],
                                 http=_fake_http(_gt_payload({"0xa": 1.0}), status=429),
                                 clock=lambda: 1.0)
    assert out == {}


def test_batch_stops_remaining_chunks_after_first_rate_limit():
    """One provider refusal must not be amplified into requests for every remaining chunk."""
    mints = ["0x%02x" % i for i in range(65)]
    calls = []

    out = G.gt_stats_batch_keyed(
        [("4663", mint) for mint in mints],
        http=_fake_http({}, status=429, calls=calls),
        clock=lambda: 100.0,
        cooldown_s=60.0,
    )

    assert out == {}
    assert len(calls) == 1


def test_rate_limit_cooldown_blocks_calls_until_window_expires():
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        if len(calls) == 1:
            return 429, {}, None
        return 200, _gt_payload({"0xb": 2.0}), None

    assert G.gt_stats_batch_keyed(
        [("4663", "0xa")], http=http, clock=lambda: 100.0, cooldown_s=60.0) == {}
    assert G.gt_stats_batch_keyed(
        [("4663", "0xb")], http=http, clock=lambda: 120.0, cooldown_s=60.0) == {}
    assert len(calls) == 1

    resumed = G.gt_stats_batch_keyed(
        [("4663", "0xb")], http=http, clock=lambda: 161.0, cooldown_s=60.0)
    assert resumed[("4663", "0xb")]["price"] == 2.0
    assert len(calls) == 2


def test_http_error_retry_after_controls_cooldown_and_keeps_cache_hits():
    calls = []
    success = _fake_http(_gt_payload({"0xcached": 3.0}), calls=calls)
    G.gt_stats_batch_keyed(
        [("4663", "0xcached")], http=success, clock=lambda: 10.0, ttl_s=300.0)

    def limited(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        raise urllib.error.HTTPError(
            url, 429, "rate limited", {"Retry-After": "120"}, None)

    assert G.gt_stats_batch_keyed(
        [("4663", "0xmiss")], http=limited, clock=lambda: 100.0,
        ttl_s=300.0, cooldown_s=60.0) == {}
    call_count = len(calls)

    during = G.gt_stats_batch_keyed(
        [("4663", "0xcached"), ("4663", "0xother")], http=limited,
        clock=lambda: 161.0, ttl_s=300.0, cooldown_s=60.0)
    assert during[("4663", "0xcached")]["price"] == 3.0
    assert ("4663", "0xother") not in during
    assert len(calls) == call_count


def test_competing_call_does_not_overlap_gt_http():
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2.0)
        return 200, _gt_payload({"0xa": 1.0, "0xb": 2.0}), None

    result = {}
    worker = threading.Thread(target=lambda: result.update(
        G.gt_stats_batch_keyed(
            [("4663", "0xa")], http=http, clock=lambda: 100.0, cooldown_s=60.0)))
    worker.start()
    assert entered.wait(1.0)

    competing = G.gt_stats_batch_keyed(
        [("4663", "0xb")], http=http, clock=lambda: 100.0, cooldown_s=60.0)
    release.set()
    worker.join(2.0)

    assert not worker.is_alive()
    assert competing == {}
    assert result[("4663", "0xa")]["price"] == 1.0
    assert len(calls) == 1


def test_sends_browser_user_agent(monkeypatch):
    """GT 403s the default Python UA (like DexScreener/Jupiter). The module _http must send a
    browser UA — verified by capturing the urllib Request the default client builds."""
    seen = {}

    class _Resp:
        status = 200
        def read(self): return b'{"data":{"attributes":{"token_prices":{}}}}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=10):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    G._http("GET", "https://api.geckoterminal.com/x")
    assert seen["ua"].startswith("Mozilla/")


# --------------------------------------------------------------------- cache + rate guard

def test_ttl_cache_serves_repeat_batches_without_rehitting():
    payload = _gt_payload({"0xa": 1.0})
    calls = []
    http = _fake_http(payload, calls=calls)
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http, clock=lambda: 100.0, ttl_s=15.0)
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http, clock=lambda: 110.0, ttl_s=15.0)  # within TTL
    assert len(calls) == 1                       # served from cache
    # after TTL expiry -> re-HTTP
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http, clock=lambda: 200.0, ttl_s=15.0)
    assert len(calls) == 2


def test_gt_price_for_hits_cache_warmed_by_the_batch():
    payload = _gt_payload({"0xa": 0.7})
    calls = []
    http = _fake_http(payload, calls=calls)
    G.gt_stats_batch_keyed([("4663", "0xA")], http=http, clock=lambda: 100.0, ttl_s=15.0)
    assert G.gt_price_for("4663", "0xA", http=http, clock=lambda: 105.0, ttl_s=15.0) == 0.7
    assert len(calls) == 1                       # single lookup served from batch's cache


def test_gt_price_for_rate_guard_blocks_a_burst_on_cache_miss():
    """The min-interval guard stops the per-mint exit-sweep path from bursting GT HTTP. Two rapid
    misses -> only the first calls; the second (within the interval) returns None without HTTP."""
    calls = []
    http = _fake_http(_gt_payload({"0xa": 1.0}), calls=calls)
    first = G.gt_price_for("4663", "0xa", http=http, clock=lambda: 100.0,
                           ttl_s=15.0, min_interval_s=3.0)
    second = G.gt_price_for("4663", "0xb", http=http, clock=lambda: 101.0,
                            ttl_s=15.0, min_interval_s=3.0)
    assert first == 1.0
    assert second is None                        # guarded, no HTTP
    assert len(calls) == 1


def test_gt_price_for_none_for_unmapped_network():
    calls = []
    assert G.gt_price_for("solana", "SoLmint",
                          http=_fake_http(_gt_payload({}), calls=calls), clock=lambda: 1.0) is None
    assert calls == []


def test_reset_clears_cache():
    payload = _gt_payload({"0xa": 1.0})
    calls = []
    http = _fake_http(payload, calls=calls)
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http, clock=lambda: 100.0, ttl_s=15.0)
    G.reset_gt_state()
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http, clock=lambda: 101.0, ttl_s=15.0)
    assert len(calls) == 2                        # cache cleared -> re-HTTP


# --------------------------------------------------------------------- shared guard for GT GETs
#
# 2026-07-17: the V4 pool-discovery lookups (evm_v4_quote._gt_pools_for_mint) hit GT via raw
# urllib — outside the shared cooldown/gate — so the marks path's 429 cooldowns intermittently
# starved them AND their calls never registered with the guard. gt_get_json is the shared-guarded
# GET every non-price GT endpoint must route through.

def test_gt_get_json_success_returns_payload_and_stamps_guard():
    calls = []
    payload = {"data": [{"id": "robinhood_0xpool"}]}
    j = G.gt_get_json("https://api.geckoterminal.com/api/v2/networks/robinhood/tokens/0xa/pools",
                      http=_fake_http(payload, calls=calls), clock=lambda: 100.0)
    assert j == payload and len(calls) == 1
    assert G._last_http[0] == 100.0                   # the shared guard saw this call


def test_gt_get_json_429_starts_shared_cooldown():
    def http(method, url, headers=None, body=None, timeout=10):
        return 429, {}, None
    assert G.gt_get_json("https://gt/x", http=http, clock=lambda: 100.0, cooldown_s=60.0) is None
    # the price batch is now cooling down too — one provider refusal, one shared cooldown
    batch_calls = []
    assert G.gt_stats_batch_keyed([("4663", "0xa")], http=_fake_http(_gt_payload({"0xa": 1.0}),
                                                                     calls=batch_calls),
                                  clock=lambda: 120.0, cooldown_s=60.0) == {}
    assert batch_calls == []


def test_research_batch_cooldown_does_not_gag_the_funded_path():
    """A price-batch (research) 429 must NOT suppress `gt_get_json`, the money path.

    This assertion is inverted from its original form on purpose. Sharing one cooldown cell was
    meant to stop callers amplifying a provider refusal, but the blind-capture batch trips 429s
    dozens of times a minute, so in practice it gagged funded entry quotes almost continuously —
    refusing live entries as `provider_rate_limit: v4 could not read: rate_limit` while the funded
    lane itself had made no failing request (2026-08-14). Suppressing the high-volume research
    caller and letting the low-volume money path through LOWERS total provider load; it does not
    raise it.
    """
    def http429(method, url, headers=None, body=None, timeout=10):
        return 429, {}, None
    calls = []
    G.gt_stats_batch_keyed([("4663", "0xa")], http=http429, clock=lambda: 100.0, cooldown_s=60.0)
    assert G.gt_get_json("https://gt/x", http=_fake_http({"ok": 1}, calls=calls),
                         clock=lambda: 130.0) == {"ok": 1}
    assert calls, "the funded path must reach the provider during a research cooldown"


def test_funded_429_suppresses_the_funded_path_without_http():
    """The amplification guard that matters: when the provider refuses US, stop asking."""
    def http429(method, url, headers=None, body=None, timeout=10):
        return 429, {}, None
    assert G.gt_get_json("https://gt/x", http=http429, clock=lambda: 100.0, cooldown_s=60.0) is None
    calls = []
    assert G.gt_get_json("https://gt/x", http=_fake_http({}, calls=calls),
                         clock=lambda: 130.0) is None
    assert calls == []                                # cooldown respected: no HTTP at all


def test_gt_get_json_transport_error_is_none():
    def boom(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("down")
    assert G.gt_get_json("https://gt/x", http=boom, clock=lambda: 100.0) is None


def test_v4_pool_lookup_respects_shared_cooldown():
    import time

    from sniper.execution import evm_v4_quote as V

    from sniper.execution import solana_quote as SQ

    G._cooldown_until[0] = time.monotonic() + 600.0   # active provider cooldown (e.g. a marks 429)
    # 2026-08-30: discovery gained a SECOND provider (DexScreener), so "refused without HTTP" is
    # now a claim about both. Suppress the DexScreener breaker too, or this test makes a live
    # call. Restored in `finally` — a leaked cooldown cell would silently blind whatever else
    # shares this xdist worker.
    ds_prev = SQ._ds_funded_cooldown_until[0]
    SQ._ds_funded_cooldown_until[0] = time.monotonic() + 600.0
    try:
        V._pool_cache.clear()
        V.begin_quote()
        # default (non-injected) path must consult the shared guards -> refused without HTTP
        assert V._gt_pools_for_mint("4663", "0xfresh") == []
        assert ("4663", "0xfresh") not in V._pool_cache   # a refusal is never cached as 'no pools'
        assert V.last_provider_blind() == "rate_limit"    # both dark = blind, not "no pools"
    finally:
        SQ._ds_funded_cooldown_until[0] = ds_prev
        V.begin_quote()


# --------------------------------------------------------------------- pool stats (liquidity)

def _pools_payload(pools):
    # GT /networks/{slug}/tokens/{mint}/pools response: data[] of pools with attributes
    return {"data": [{"id": f"robinhood_{i}", "attributes": a} for i, a in enumerate(pools)]}


def test_pool_stats_reads_liquidity_from_tokens_pools_endpoint():
    # Liquidity comes from the /tokens/{mint}/pools endpoint (ONE call — the same one V4 discovery
    # makes). Picks the DEEPEST pool by reserve_in_usd.
    calls = []
    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        assert "/tokens/" in url and "/pools" in url        # single endpoint, no /pools/{addr}
        return 200, _pools_payload([
            {"reserve_in_usd": "40000", "volume_usd": {"h24": "10"}},          # shallow pool
            {"reserve_in_usd": "125000.5", "volume_usd": {"h24": "88000.0"},   # DEEPEST -> chosen
             "market_cap_usd": "560000.0", "price_change_percentage": {"h1": "-3.2", "h24": "45.0"}},
        ]), None
    out = G.gt_pool_stats_for("4663", "0xAAA", http=http, clock=lambda: 100.0)
    assert len(calls) == 1
    assert out["liquidity"] == 125000.5
    assert out["volume_24h"] == 88000.0
    assert out["mcap"] == 560000.0
    assert out["change_h1"] == -3.2 and out["change_h24"] == 45.0


def test_pool_stats_is_cache_first_and_throttled():
    calls = []
    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(url)
        return 200, _pools_payload([{"reserve_in_usd": "1000"}]), None
    a = G.gt_pool_stats_for("4663", "0xAAA", http=http, clock=lambda: 100.0)
    assert a["liquidity"] == 1000.0 and len(calls) == 1      # one call
    # within TTL -> served from cache, no new HTTP
    b = G.gt_pool_stats_for("4663", "0xAAA", http=http, clock=lambda: 130.0)
    assert b["liquidity"] == 1000.0 and len(calls) == 1
    # a DIFFERENT token within the min-interval -> throttled to None (no burst)
    assert G.gt_pool_stats_for("4663", "0xBBB", http=http, clock=lambda: 105.0) is None
    assert len(calls) == 1


def test_pool_stats_none_for_non_gt_chain():
    assert G.gt_pool_stats_for("solana", "somemint", http=None, clock=lambda: 100.0) is None
