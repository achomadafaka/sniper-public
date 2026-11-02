"""A stop may only be held by complete, current reserves from the bound pool."""

import pytest

from sniper.services.memecoin_solana_amounts import WSOL
from sniper.services.memecoin_solana_swap_marks import PoolBinding, SolanaSwapMarkCache


BINDING = PoolBinding("MintA", "PoolA", "CoinV", "QuoteV", WSOL)


def _sync(slot=100, **changes):
    row = {"kind": "sol_sync", "pool": "PoolA", "block_no": slot,
           "amount0": "1000000", "amount1": "2000000000", "decimals0": 6, "decimals1": 9}
    return dict(row, **changes)


def _swap(slot=101, **changes):
    row = {"kind": "sol_swap", "pool": "PoolA", "block_no": slot,
           "amount0": "-1000", "amount1": "1000000", "decimals0": 6, "decimals1": 9}
    return dict(row, **changes)


def _record(cache, row, at):
    return cache.record(row, BINDING, now_ts=at, observed_at="2026-10-17T17:00:00+00:00")


def _rung(cache, at):
    return cache.exit_rung_pair("MintA", now_ts=at, max_age_s=60.0)


@pytest.fixture
def cache():
    value = SolanaSwapMarkCache()
    assert _record(value, _sync(), 0.0)
    yield value
    value._onchain_pool.shutdown(wait=True)


@pytest.mark.parametrize("slot", [100, 101])
def test_swap_without_post_balances_invalidates_the_stop_witness(cache, slot):
    assert _record(cache, _swap(slot), 100.0)
    assert _rung(cache, 100.0) is None
    # Display receipt freshness retains its existing meaning.
    assert cache._display_seen[("MintA", "PoolA")] == 100.0


def test_out_of_order_complete_reserves_cannot_refresh_the_stop_witness(cache):
    assert _record(cache, _sync(99), 100.0)
    assert _rung(cache, 100.0) is None, "the accepted slot100 reserves are100s old"


@pytest.mark.parametrize("row", [
    _sync(101, amount1="0"),
    _sync(101, amount0="bad"),
    _swap(101, balance0="bad", balance1="1000"),
    _swap(None, balance0="1000000", balance1="2000000000"),
])
def test_unreadable_new_movement_cannot_leave_a_healthy_stop_witness(cache, row):
    _record(cache, row, 1.0)
    assert _rung(cache, 1.0) is None


def test_unknown_movement_needs_a_strictly_later_complete_slot_to_recover(cache):
    assert _record(cache, _swap(101), 1.0)
    for slot in (99, 100, 101):
        assert _record(cache, _sync(slot), 2.0)
        assert _rung(cache, 2.0) is None, "same-slot transaction order is unknown"
    assert _record(cache, _sync(102, amount1="1000000000"), 3.0)
    got = _rung(cache, 4.0)
    assert got["slot"] == 102 and got["quote_atoms"] == 1000000000
    assert got["seen_age_s"] == 1.0


def test_rollover_requires_fresh_complete_reserves_even_when_swaps_resume(cache):
    cache.invalidate_prices(now_ts=50.0)
    assert _record(cache, _swap(101), 100.0)
    assert _rung(cache, 100.0) is None
    assert _record(cache, _sync(102), 101.0)
    assert _rung(cache, 101.0)["seen_age_s"] == 0.0


def test_fresh_complete_pairs_keep_their_own_age_despite_old_frames(cache):
    assert _record(cache, _sync(101, amount1="1000000000"), 2.0)
    assert _record(cache, _sync(100), 3.0)
    got = _rung(cache, 4.0)
    assert got["quote_atoms"] == 1000000000 and got["slot"] == 101
    assert got["seen_age_s"] == 2.0


def test_reserve_time_in_the_future_is_unreadable(cache):
    assert _record(cache, _sync(101), 10.0)
    assert _rung(cache, 9.0) is None


def test_full_invalidation_drops_stop_evidence(cache):
    cache.invalidate()
    assert _rung(cache, 1.0) is None


def test_same_slot_duplicate_cannot_freshen_accepted_reserves(cache):
    assert _record(cache, _sync(100), 30.0)
    assert _rung(cache, 61.0) is None, "a repeated frame does not re-observe the pool"


def test_conflicting_complete_pairs_in_one_slot_are_unordered(cache):
    assert _record(cache, _sync(100, amount1="1000000000"), 1.0)
    assert _rung(cache, 1.0) is None
    assert _record(cache, _sync(100), 2.0)
    assert _rung(cache, 2.0) is None, "an older healthy transaction must not win by arrival order"
    assert _record(cache, _sync(101, amount1="1000000000"), 3.0)
    assert _rung(cache, 3.0)["quote_atoms"] == 1000000000
