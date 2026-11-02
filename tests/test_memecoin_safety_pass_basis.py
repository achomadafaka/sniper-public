"""A safety PASS records its basis (Gamma 2026-10-07; spec 2026-10-07 EVM entry safety, section 3).

349 of 372 EVM fills since 09-04 stored no pass reason, so AICAT's admit had to be replayed on the
chain to be seen. The fast lanes' caches still dropped it: an inline pass was cached as
(True, None), and a cache hit answered a bare "rh_fast_safety_cache" / "bsc_fast_safety_cache".
"""
import json

import pytest

from sniper.services import memecoin_rh_fast_lane as lane

PASS = "ok: sell route reverted but the token pull succeeded — route-attributed; detail=x"


def _cache(safety=lambda mint, usd=None: (True, PASS)):
    return lane.SafetyCache(safety, ttl_s=300, usd=20.0, clock=lambda: 1000.0)


def test_a_background_pass_keeps_its_reason_for_the_cache_hit():
    cache = _cache()
    cache.run_one("0xmint", now=1000.0)
    assert cache.cached_answer("0xmint", "rh_fast_safety_cache") == (
        True, "rh_fast_safety_cache: " + PASS)


def test_an_inline_pass_keeps_its_reason_too():
    cache = _cache()
    cache.put("0xMint", now=1000.0, reason="ok")
    assert cache.cached_answer("0xmint", "bsc_fast_safety_cache") == (True, "bsc_fast_safety_cache: ok")


def test_a_pass_without_a_reason_reads_unknown_never_ok():
    cache = _cache(lambda mint, usd=None: (True, None))
    cache.run_one("0xmint", now=1000.0)
    assert cache.cached_answer("0xmint", "rh_fast_safety_cache") == (
        True, "rh_fast_safety_cache: unknown")
    cache.put("0xother", now=1000.0)
    assert cache.cached_answer("0xother", "x")[1] == "x: unknown"


def test_the_lane_inline_check_hands_its_reason_to_the_cache():
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.inline_host, live.inline_deadline_s = None, 2.0
    live.inline_safety_fn = lambda mint, usd=None: (True, "ok")
    live.inline_facts_fn = lambda mint: (18, 10 ** 24)
    live.stats_fn = None
    live.host_clock = lambda: 1000.0
    live.cache = _cache()
    assert live._inline("0xmint", {}) is None
    assert live.cache.cached_answer("0xmint", "rh_fast_safety_cache")[1] == "rh_fast_safety_cache: ok"


def test_the_4663_lane_context_answers_with_the_cached_basis():
    live = lane.LiveLane.__new__(lane.LiveLane)
    live.contexts, live.ctx = {}, {}
    live.cache = _cache()
    live.cache.run_one("0xmint", now=1000.0)
    payload = {"snapshot": {"weth_wei": str(10 ** 18), "native_wei": str(10 ** 17)}, "eth_usd": 2500}
    safety = live._lane_context(payload, execute_fn=None)["4663"]["token_safety_fn"]
    assert safety("0xmint") == (True, "rh_fast_safety_cache: " + PASS)


def test_the_destination_lookup_is_timed_on_the_failure_path():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "_safety_fakes", Path(__file__).with_name("test_memecoin_evm_token_safety.py"))
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    _router_reverts_http, _safety_for, _V2_ROUTE = (
        fakes._router_reverts_http, fakes._safety_for, fakes._V2_ROUTE)
    timings = {}
    hook = {"code": 3, "message": "execution reverted", "data": "0x90bfb865"}
    safety = _safety_for(_router_reverts_http(router_error=hook), _V2_ROUTE)
    safety("0xtok", timings=timings)
    assert "destination_ms" in timings and timings["destination_ms"] >= 0.0
