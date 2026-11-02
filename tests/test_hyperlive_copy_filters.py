"""Tests for entry-quality filters (sniper/hyperlive/copy_filters.py)."""
from sniper.hyperlive.copy_filters import EntryPolicy, RECOMMENDED, should_open


def test_default_policy_filters_nothing():
    p = EntryPolicy()
    assert p.active is False
    ok, _ = should_open(lag_bps=9999, target_notional=1, consensus_n=99, policy=p)
    assert ok is True


def test_lag_filter_rejects_chase():
    p = EntryPolicy(max_lag_bps=150.0)
    assert should_open(lag_bps=740, policy=p)[0] is False
    assert should_open(lag_bps=-740, policy=p)[0] is False    # magnitude, either direction
    assert should_open(lag_bps=3, policy=p)[0] is True


def test_lag_filter_fails_open_on_missing():
    p = EntryPolicy(max_lag_bps=150.0)
    assert should_open(lag_bps=None, policy=p)[0] is True      # no data -> don't block


def test_conviction_floor():
    p = EntryPolicy(min_conviction_usd=20000.0)
    assert should_open(target_notional=5000, policy=p)[0] is False
    assert should_open(target_notional=25000, policy=p)[0] is True
    assert should_open(target_notional=None, policy=p)[0] is True   # missing -> allow


def test_consensus_bounds():
    p = EntryPolicy(min_consensus=2, max_consensus=3)
    assert should_open(consensus_n=1, policy=p)[0] is False    # too few
    assert should_open(consensus_n=2, policy=p)[0] is True
    assert should_open(consensus_n=3, policy=p)[0] is True
    assert should_open(consensus_n=5, policy=p)[0] is False    # crowded


def test_recommended_is_active_and_reasonable():
    assert RECOMMENDED.active is True
    assert RECOMMENDED.max_lag_bps == 150.0 and RECOMMENDED.max_consensus == 3
    # a clean small-consensus, high-conviction, low-lag entry passes
    ok, why = should_open(lag_bps=5, target_notional=50000, consensus_n=2, policy=RECOMMENDED)
    assert ok is True and why == "ok"
