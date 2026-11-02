"""The card must never be more optimistic than the rails on the same position.

2026-08-24: once the exact quote aged past the 300s window, `_select_display_mark` DISCARDED it and
showed a fresh modeled fill or the raw mid instead. On a thin pool the mid is roughly double what
the bag actually fetches, so the card read healthier than the funded sweep — which keeps its last
resolved exact and takes the WORSE of it and nominal. That gap is the direction that talks an
operator out of a sell which was already correct.
"""
from sniper.routes.api_memecoin import _select_display_mark

NOW = "2026-08-24T15:00:00+00:00"
FRESH = "2026-08-24T14:59:30+00:00"      # 30s  — inside the 300s window
STALE = "2026-08-24T14:50:00+00:00"      # 600s — outside it


def test_a_fresh_exact_quote_wins_outright():
    price, basis, _obs, age = _select_display_mark(
        mid=2.0, mid_observed_at=NOW, liquidity=100_000.0, qty=1.0, now_iso=NOW,
        exact_price=1.0, exact_observed_at=FRESH)
    assert (price, basis) == (1.0, "measured_route")
    assert age == 30.0


def test_a_stale_exact_still_floors_an_optimistic_mid():
    """The whole point: 1.0 was really quoted, 2.0 is a mid nobody can trade at."""
    price, basis, _obs, _age = _select_display_mark(
        mid=2.0, mid_observed_at=NOW, liquidity=0.0, qty=1.0, now_iso=NOW,
        exact_price=1.0, exact_observed_at=STALE)
    assert (price, basis) == (1.0, "measured_route_stale")


def test_a_stale_exact_does_NOT_flatter_a_worse_current_mark():
    """Conservative in one direction only. If the live mark is worse than our last real quote, the
    worse number stands — otherwise this would hide a real deterioration behind an old quote."""
    price, basis, _obs, _age = _select_display_mark(
        mid=0.5, mid_observed_at=NOW, liquidity=0.0, qty=1.0, now_iso=NOW,
        exact_price=1.0, exact_observed_at=STALE)
    assert (price, basis) == (0.5, "mid")


def test_the_modeled_fill_is_deliberately_left_alone():
    """Scope is the raw mid only. `modeled_slippage` already haircuts, and the 300s window is a
    real policy saying an older exact is not trusted — this floor must not quietly overrule it."""
    _price, basis, _obs, _age = _select_display_mark(
        mid=2.0, mid_observed_at=NOW, liquidity=1_000_000.0, qty=1.0, now_iso=NOW,
        exact_price=1.0, exact_observed_at=STALE)
    assert basis == "modeled_slippage"


def test_no_exact_ever_leaves_the_old_behaviour_untouched():
    price, basis, _obs, _age = _select_display_mark(
        mid=2.0, mid_observed_at=NOW, liquidity=0.0, qty=1.0, now_iso=NOW,
        exact_price=None, exact_observed_at=None)
    assert (price, basis) == (2.0, "mid")
