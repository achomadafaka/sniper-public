"""Phase 1 Task 4 — realized volatility (exit brain).

Series are (t_seconds, price) on TRUE event time.
"""
import math

from sniper.analysis.exit_features import realized_vol


def test_vol_none_on_single_point():
    assert realized_vol([(0, 1.0)], window_s=300) is None
    assert realized_vol([], window_s=300) is None


def test_vol_positive_on_moving_series():
    v = realized_vol([(0, 1.0), (1, 1.1), (2, 0.95), (3, 1.05)], window_s=300)
    assert v is not None and v > 0


def test_vol_windows_out_old_points():
    # a stale point outside the window must not contribute
    series = [(0, 1.0), (1000, 1.0), (1001, 1.0)]      # only the last two are in a 300s window
    assert realized_vol(series, window_s=300) == 0.0    # flat inside window -> zero vol (not None)


def test_vol_is_continuous_in_n_and_reduces_to_abs_return_at_n1():
    """FROZEN estimator regression: realized vol is the zero-mean RMS of log returns, so adding an
    IDENTICAL return must not change it. The old sample-std-dev read 0.0953 for one +10% return and
    ~0 for two — drift cancelled to 'no volatility'."""
    one = realized_vol([(0, 1.0), (1, 1.1)], window_s=300)
    two = realized_vol([(0, 1.0), (1, 1.1), (2, 1.21)], window_s=300)   # a second identical +10%
    assert one is not None and two is not None
    assert abs(one - abs(math.log(1.1))) < 1e-12      # n=1 reduces exactly to |r|
    assert abs(two - one) < 1e-12                     # continuous: identical return -> same vol
    assert two > 0.09                                 # and emphatically NOT ~0
