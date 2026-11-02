"""MissLearner carries exact DAMM read targets only when every current waiter agrees."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sniper.execution import meteora_damm_v2_buy as D
from sniper.services import memecoin_slot_lane as L
from test_slot_lane_miss_learn import Timers
from test_solana_damm_read_hint import COIN, FIX, OWNER


def setup_misses(hints, *, venue="meteora_damm_v2", owners=None):
    calls = []
    cache = SimpleNamespace(learn=lambda coin, **kw: calls.append((coin, kw)) or False)
    clock = SimpleNamespace(monotonic=lambda: 10.)
    learner = L.MissLearner(pools={venue: cache}, put=lambda *a: None,
        handoff_fn=lambda *a: None, log=lambda *a: None, clock=clock, timer=Timers())
    for n, hint in enumerate(hints):
        routed = SimpleNamespace(mint=COIN, owner=(owners or [OWNER] * len(hints))[n],
            signature="SIG%d" % n, handle="test", slot=456215414, damm_read_hint=hint)
        buy = L.RoutedDammMiss(routed, cache)
        if venue != "meteora_damm_v2":
            buy.venue = venue
        assert learner.offer(buy)
    return learner, calls


def test_damm_first_sight_passes_source_bound_hint_to_existing_background_learner():
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    learner, calls = setup_misses([hint])
    learner.learn_once(COIN)
    assert calls == [(COIN, {"read_hint": hint})]


def test_same_canonical_targets_from_two_owners_share_one_hint_read():
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    owner2 = "second-owner"
    learner, calls = setup_misses([hint, replace(hint, owner=owner2)], owners=[OWNER, owner2])
    learner.learn_once(COIN)
    assert calls == [(COIN, {"read_hint": hint})]


@pytest.mark.parametrize("bad", ["missing", "owner", "coin", "pool", "vault", "program", "untyped"])
def test_incomplete_or_conflicting_waiters_keep_legacy_scan(bad):
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    other = (None if bad == "missing" else SimpleNamespace(**vars(hint)) if bad == "untyped"
             else replace(hint, **{dict(owner="owner", coin="coin", pool="pool",
                                        vault="vault_a", program="program_a")[bad]: "different"}))
    learner, calls = setup_misses([hint, other])
    learner.learn_once(COIN)
    assert calls == [(COIN, {})]


def test_other_venue_cache_receives_no_new_keyword():
    hint = D.pool_read_hint(FIX, COIN, OWNER)
    learner, calls = setup_misses([hint], venue="meteora_dbc")
    learner.learn_once(COIN)
    assert calls == [(COIN, {})]
