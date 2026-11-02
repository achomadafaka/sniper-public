"""Phase 1 Task 3 — causal confirmed-pivot detection, no lookahead (exit brain)."""
from sniper.analysis.exit_features import confirmed_pivots


def test_pivot_requires_confirmation_and_has_no_lookahead():
    s = [("t0", 1.0), ("t1", 0.8), ("t2", 0.9), ("t3", 1.0)]   # low at idx1
    assert confirmed_pivots(s[:3], confirm=2) == []            # idx1 not yet confirmed at t2
    piv = confirmed_pivots(s, confirm=2)
    assert any(p.kind == "low" and p.price == 0.8 for p in piv)


def test_high_pivot_symmetric():
    s = [("t0", 0.5), ("t1", 1.2), ("t2", 0.9), ("t3", 0.7)]   # high at idx1
    piv = confirmed_pivots(s, confirm=2)
    assert any(p.kind == "high" and p.price == 1.2 for p in piv)


def test_pivot_never_appears_before_enough_forward_bars():
    # Growing prefixes: a pivot at index i must not be reported until i+confirm bars exist.
    s = [("t0", 1.0), ("t1", 0.7), ("t2", 0.8), ("t3", 0.9), ("t4", 1.1)]
    seen_low_before_confirm = False
    for k in range(1, len(s) + 1):
        piv = confirmed_pivots(s[:k], confirm=2)
        if any(p.price == 0.7 for p in piv) and k < 4:   # idx1 needs bars up to idx3
            seen_low_before_confirm = True
    assert not seen_low_before_confirm


def test_flat_and_short_series_yield_no_pivots():
    assert confirmed_pivots([], confirm=2) == []
    assert confirmed_pivots([("t0", 1.0), ("t1", 1.0), ("t2", 1.0)], confirm=1) == []  # no turn
