"""Conviction-scaled sizing: bet ~ sqrt(their_size/pivot), clamped. Off by default
(returns the flat base) so it's forward-test-gated, not a blind flat->scaled flip."""
from sniper.services.memecoin_copy_trader import conviction_size

CFG = dict(enabled=True, pivot=20000.0, min_mult=0.5, max_mult=3.0)


def test_disabled_returns_flat_base():
    assert conviction_size(1_000_000, 100.0, enabled=False) == 100.0


def test_pivot_is_1x():
    assert conviction_size(20000, 100.0, **CFG) == 100.0


def test_scales_up_sqrt_and_caps():
    assert conviction_size(80000, 100.0, **CFG) == 200.0          # sqrt(4)=2x
    assert conviction_size(10_000_000, 100.0, **CFG) == 300.0     # capped at max_mult=3x


def test_scales_down_and_floors():
    assert conviction_size(5000, 100.0, **CFG) == 50.0            # sqrt(0.25)=0.5x
    assert conviction_size(100, 100.0, **CFG) == 50.0            # floored at min_mult=0.5x


def test_missing_or_zero_size_returns_base():
    assert conviction_size(None, 100.0, **CFG) == 100.0
    assert conviction_size(0, 100.0, **CFG) == 100.0
    assert conviction_size(-5, 100.0, **CFG) == 100.0
