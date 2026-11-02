"""Capture GT's reserve_in_usd (liquidity) from the V4 pool-discovery call the router already makes.

_gt_pools_for_mint reads reserve_in_usd to rank pools then discarded it; now it caches the deepest
pool's reserve so the runner can persist it to marks.liquidity for free (no extra GT call). The web
can't win GT's rate limit and V4 concentrated liquidity isn't summable on-chain, so this is the only
zero-cost liquidity source (2026-07-18).
"""
import time

from sniper.execution import evm_v4_quote as V


def _reset():
    V._reserve_cache.clear()
    V._pool_cache.clear()


def test_discovery_captures_deepest_pool_reserve():
    _reset()
    # GT /tokens/{mint}/pools response: two v4 pools, different reserves -> capture the deepest
    payload = {"data": [
        {"id": "robinhood_0x" + "a" * 64, "relationships": {"dex": {"data": {"id": "uniswap-v4"}}},
         "attributes": {"reserve_in_usd": "40000"}},
        {"id": "robinhood_0x" + "b" * 64, "relationships": {"dex": {"data": {"id": "uniswap-v4"}}},
         "attributes": {"reserve_in_usd": "225000.5"}},
    ]}
    V._gt_pools_for_mint("4663", "0xMINT", gt_http=lambda url: payload)
    assert V.top_pool_reserve_usd("4663", "0xMINT") == 225000.5   # deepest pool's reserve


def test_reserve_getter_is_fresh_stale_and_missing_aware():
    _reset()
    V._reserve_cache[("4663", "0xa")] = (125000.0, time.monotonic())
    assert V.top_pool_reserve_usd("4663", "0xA") == 125000.0      # case-insensitive, fresh
    V._reserve_cache[("4663", "0xold")] = (99000.0, time.monotonic() - 3600)
    assert V.top_pool_reserve_usd("4663", "0xold", max_age_s=1800) is None   # too old
    assert V.top_pool_reserve_usd("4663", "0xmissing") is None    # never discovered
    V._reserve_cache[("4663", "0xz")] = (0.0, time.monotonic())
    assert V.top_pool_reserve_usd("4663", "0xz") is None          # zero -> unknown, not $0
