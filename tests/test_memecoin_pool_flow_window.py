"""Pre-entry flow read off the local Solana tape (2026-08-25).

The gate this feeds must fail OPEN on absent data, so the UNKNOWN cases are tested first and
hardest: an absent pool, an absent window, and an absent reserve must each be distinguishable
from a measured zero."""
import sqlite3

import pytest

from sniper.services.memecoin_solana_tape import (
    flow_observation, pool_flow_window)


def _tape():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY, chain_id TEXT, pool TEXT, "
              "kind TEXT, amount0 TEXT, amount1 TEXT, maker TEXT, observed_at TEXT)")
    return c


def _tick(c, pool, kind, amount1, maker, ts):
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,amount0,amount1,maker,observed_at) "
              "VALUES ('solana',?,?,'0',?,?,?)", (pool, kind, str(amount1), maker, ts))


def test_absent_pool_is_unknown_not_zero():
    assert pool_flow_window(_tape(), "nope", since_iso="2026-08-25T00:00:00+00:00") is None


def test_counts_makers_and_sides():
    c = _tape()
    _tick(c, "P", "sol_swap", -100, "alice", "2026-08-25T10:00:00+00:00")
    _tick(c, "P", "sol_swap", -200, "bob", "2026-08-25T10:00:05+00:00")
    _tick(c, "P", "sol_swap", 50, "alice", "2026-08-25T10:00:09+00:00")
    f = pool_flow_window(c, "P", since_iso="2026-08-25T09:00:00+00:00")
    assert f["swaps_n"] == 3
    assert f["makers_n"] == 2            # alice twice counts once
    assert f["sell_n"] == 2 and f["buy_n"] == 1
    assert f["gross_quote"] == 350       # magnitudes
    assert f["net_quote"] == -250        # signed


def test_window_bounds_are_respected():
    c = _tape()
    _tick(c, "P", "sol_swap", -100, "a", "2026-08-25T09:00:00+00:00")   # before
    _tick(c, "P", "sol_swap", -100, "a", "2026-08-25T10:00:00+00:00")   # inside
    f = pool_flow_window(c, "P", since_iso="2026-08-25T09:30:00+00:00")
    assert f["swaps_n"] == 1


def test_reserve_comes_from_the_newest_sync_and_may_predate_the_window():
    c = _tape()
    _tick(c, "P", "sol_sync", 900, "x", "2026-08-25T08:00:00+00:00")
    _tick(c, "P", "sol_sync", 1000, "x", "2026-08-25T09:00:00+00:00")
    _tick(c, "P", "sol_swap", -250, "a", "2026-08-25T10:00:00+00:00")
    f = pool_flow_window(c, "P", since_iso="2026-08-25T09:30:00+00:00")
    assert f["reserve_quote"] == 1000                          # newest, not the first
    assert f["reserve_at"] == "2026-08-25T09:00:00+00:00"      # caller can judge staleness


def test_missing_reserve_is_none_so_turnover_stays_unknown():
    c = _tape()
    _tick(c, "P", "sol_swap", -250, "a", "2026-08-25T10:00:00+00:00")
    obs = flow_observation(pool_flow_window(c, "P", since_iso="2026-08-25T09:00:00+00:00"))
    assert obs["reserve_quote"] is None
    assert obs["turnover"] is None        # NOT 0.0 — a zero would read as "quiet pool"


def test_observation_ratios_are_scale_free():
    c = _tape()
    _tick(c, "P", "sol_sync", 10_000, "x", "2026-08-25T09:00:00+00:00")
    for i in range(4):
        _tick(c, "P", "sol_swap", -500, f"m{i}", "2026-08-25T10:00:0%d+00:00" % i)
    obs = flow_observation(pool_flow_window(c, "P", since_iso="2026-08-25T09:30:00+00:00"),
                           clip_quote=200)
    assert obs["turnover"] == pytest.approx(2000 / 10_000)
    assert obs["clip_share"] == pytest.approx(200 / 2000)
    assert obs["maker_ratio"] == pytest.approx(1.0)


def test_self_trading_shows_up_as_a_low_maker_ratio():
    c = _tape()
    for i in range(10):
        _tick(c, "P", "sol_swap", -100, "same_guy", "2026-08-25T10:00:0%d+00:00" % i)
    obs = flow_observation(pool_flow_window(c, "P", since_iso="2026-08-25T09:00:00+00:00"))
    assert obs["swaps_n"] == 10 and obs["makers_n"] == 1
    assert obs["maker_ratio"] == pytest.approx(0.1)   # vendor volume would call this real demand


def test_undecodable_leg_is_skipped_not_counted_as_zero():
    c = _tape()
    _tick(c, "P", "sol_swap", -100, "a", "2026-08-25T10:00:00+00:00")
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,amount0,amount1,maker,observed_at) "
              "VALUES ('solana','P','sol_swap','0','not_a_number','b','2026-08-25T10:00:01+00:00')")
    f = pool_flow_window(c, "P", since_iso="2026-08-25T09:00:00+00:00")
    assert f["gross_quote"] == 100
    assert f["buy_n"] == 0 and f["sell_n"] == 1


def test_flow_observation_of_none_is_none():
    assert flow_observation(None) is None
