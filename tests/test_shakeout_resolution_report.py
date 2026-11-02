"""WO-3 — the shakeout-resolution report's ordinal + resolution logic (the DB read is exercised by
running the script; this pins the pure math)."""
from sniper.analysis.exit_features import confirmed_pivots
import scripts.research.shakeout_resolution_report as R


def test_shakeout_recoveries_orders_and_numbers():
    # two shakeouts: low@8 broken(6)+recovered(15); later low@11 broken(9)+recovered(20)
    series = [(0, 10), (10, 8), (20, 12), (30, 6), (40, 15), (50, 11), (60, 14), (70, 9), (80, 20)]
    events = R._shakeout_recoveries(series, confirmed_pivots(series, confirm=1))
    assert [ordinal for _t, _px, ordinal in events] == [1, 2]     # first, then second
    assert events[0][0] < events[1][0]                            # in time order


def test_resolution_up_down_and_censored():
    series = [(0, 10), (900, 12), (1800, 8)]
    assert R._resolution(series, 0, 10, 900) is True     # +900s price 12 > 10
    assert R._resolution(series, 0, 10, 1800) is False   # +1800s price 8 < 10
    assert R._resolution(series, 0, 10, 3600) is None     # tape ends before +3600s -> censored


def test_epoch_ts_parses_iso_and_int():
    assert R._epoch_ts(1700000000) == 1700000000
    assert R._epoch_ts("2026-07-24T16:50:33+00:00") > 0
    assert R._epoch_ts(None) is None and R._epoch_ts("garbage") is None
