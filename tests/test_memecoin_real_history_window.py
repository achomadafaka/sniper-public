"""Funded history window (2026-08-25, operator: the live funded path reads nothing older than a week).

The cutoff is built in Python, NOT with SQLite datetime('now','-N days'): that renders a SPACE
separator while our stamps use 'T', and ' ' < 'T', so a string comparison reaches further back than
asked. That bug overcounted a '24h' window by 69% on 2026-08-23."""
import datetime
import sqlite3

import sniper.settings.memecoin as cfg
from sniper.persistence.db_memecoin import losing_real_coin_ids


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions (network_id TEXT, mint TEXT, execution_mode TEXT, "
              "is_open INT, closed_at TEXT, realized_pnl REAL)")
    return c


def _close(c, mint, pnl, closed_at):
    c.execute("INSERT INTO memecoin_positions VALUES ('solana',?,'real',0,?,?)",
              (mint, closed_at, pnl))


def test_window_off_reads_everything():
    c = _conn()
    _close(c, "OLD", -5.0, "2026-01-01T00:00:00+00:00")
    assert any("OLD" in x for x in losing_real_coin_ids(c))


def test_window_excludes_a_loss_outside_it():
    c = _conn()
    _close(c, "OLD", -5.0, "2026-01-01T00:00:00+00:00")
    _close(c, "NEW", -5.0, "2026-08-24T00:00:00+00:00")
    got = losing_real_coin_ids(c, since_iso="2026-08-18T00:00:00+00:00")
    assert any("NEW" in x for x in got)
    assert not any("OLD" in x for x in got)      # the permanent block becomes expiring


def test_only_the_most_recent_close_decides_inside_the_window():
    c = _conn()
    _close(c, "M", -9.0, "2026-08-20T00:00:00+00:00")
    _close(c, "M", +4.0, "2026-08-24T00:00:00+00:00")   # newest close WON
    assert losing_real_coin_ids(c, since_iso="2026-08-18T00:00:00+00:00") == frozenset()


def test_cutoff_uses_T_separator_not_a_space():
    iso = cfg.real_history_since_iso.__doc__
    assert "space" in iso.lower()
    prev = cfg.MEMECOIN_REAL_HISTORY_DAYS
    try:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = 7.0
        out = cfg.real_history_since_iso("2026-08-25T12:00:00+00:00")
        assert out.startswith("2026-08-18T"), out      # 'T', and exactly 7 days back
        assert " " not in out
    finally:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = prev


def test_zero_days_means_unbounded():
    prev = cfg.MEMECOIN_REAL_HISTORY_DAYS
    try:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = 0
        assert cfg.real_history_since_iso("2026-08-25T12:00:00+00:00") is None
    finally:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = prev


def test_losing_reentry_hours_knob_wins_over_the_day_window():
    prev_h = cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS
    prev_d = cfg.MEMECOIN_REAL_HISTORY_DAYS
    try:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = 7.0
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = 24.0
        out = cfg.losing_reentry_since_iso("2026-08-25T12:00:00+00:00")
        assert out.startswith("2026-08-24T12:00:00"), out    # 24h, not 7 days
        assert " " not in out
    finally:
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = prev_h
        cfg.MEMECOIN_REAL_HISTORY_DAYS = prev_d


def test_losing_reentry_falls_back_to_the_day_window_when_hours_unset():
    prev_h = cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS
    prev_d = cfg.MEMECOIN_REAL_HISTORY_DAYS
    try:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = 7.0
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = 0
        assert cfg.losing_reentry_since_iso("2026-08-25T12:00:00+00:00").startswith("2026-08-18T")
    finally:
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = prev_h
        cfg.MEMECOIN_REAL_HISTORY_DAYS = prev_d


def test_losing_reentry_unbounded_when_both_are_off():
    prev_h = cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS
    prev_d = cfg.MEMECOIN_REAL_HISTORY_DAYS
    try:
        cfg.MEMECOIN_REAL_HISTORY_DAYS = 0
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = 0
        assert cfg.losing_reentry_since_iso("2026-08-25T12:00:00+00:00") is None
    finally:
        cfg.MEMECOIN_REAL_LOSING_REENTRY_HOURS = prev_h
        cfg.MEMECOIN_REAL_HISTORY_DAYS = prev_d


# --- m5 turnover BAND (2026-08-25) -----------------------------------------------------------
# Both bounds are required. Measured over 62 funded closes: floor alone keeps 48 for +$0.63,
# ceiling alone keeps 56 for +$6.54, both together keep 42 for +$57.66 while refusing 20 that lost
# -$108.14. The tests below pin the band's edges and, more importantly, its fail-OPEN behaviour:
# a missing volume or a missing mcap must never become a refusal.
from sniper.services.memecoin_real_runner import volume_alive_decision as vad


def _band(vol_m5, mcap=100_000.0):
    return vad(None, mcap, abs_floor=0, turnover_floor=0,
               vol_m5=vol_m5, m5_floor=0.10, m5_ceiling=1.50)


def test_inside_the_band_is_allowed():
    assert _band(50_000)[0] is True          # 0.50 turnover
    assert _band(10_000)[0] is True          # 0.10 exactly — floor is inclusive
    assert _band(149_000)[0] is True         # just under the ceiling


def test_below_the_floor_is_refused_with_its_own_reason():
    allow, need, why = _band(9_000)          # 0.09
    assert allow is False and why == "m5_turnover_under_floor"
    assert need == 10_000                    # the floor is reported in DOLLARS, not a ratio


def test_above_the_ceiling_is_refused_with_a_DIFFERENT_reason():
    allow, need, why = _band(200_000)        # 2.00 turnover
    assert allow is False and why == "m5_turnover_over_ceiling"
    assert need == 150_000
    # the two causes must never blur — they are opposite problems
    assert why != "m5_turnover_under_floor"


def test_ceiling_off_by_default_leaves_the_floor_alone():
    allow, _, why = vad(None, 100_000.0, abs_floor=0, turnover_floor=0,
                        vol_m5=500_000, m5_floor=0.10)   # 5.0 turnover, no ceiling passed
    assert allow is True and why == "ok"


def test_missing_volume_fails_OPEN_not_closed():
    allow, _, _ = vad(None, 100_000.0, abs_floor=0, turnover_floor=0,
                      vol_m5=None, m5_floor=0.10, m5_ceiling=1.50)
    assert allow is True                      # absent data is not evidence of a dead coin


def test_missing_mcap_cannot_trigger_the_band():
    allow, _, _ = vad(None, None, abs_floor=0, turnover_floor=0,
                      vol_m5=1, m5_floor=0.10, m5_ceiling=1.50)
    assert allow is True                      # no mcap -> no ratio -> no opinion


def test_band_is_silent_above_the_mcap_it_was_fitted_on():
    """The band's measured population is p10 $9,936 / median $31,432 / p90 $153,663, with one row
    of 62 above $250k. With the mcap filter off, an unscoped band would extrapolate a ~$31k-coin
    threshold onto any size. Above the bound it must fail OPEN, not switch to another rule."""
    inside = vad(None, 100_000.0, abs_floor=0, turnover_floor=0, vol_m5=1_000,
                 m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    assert inside[0] is False and inside[2] == "m5_turnover_under_floor"

    # same starved coin, but a $5M mcap — outside the fitted range, so no opinion
    outside = vad(None, 5_000_000.0, abs_floor=0, turnover_floor=0, vol_m5=1_000,
                  m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    assert outside[0] is True

    # and the ceiling is equally silent up there
    hot = vad(None, 5_000_000.0, abs_floor=0, turnover_floor=0, vol_m5=50_000_000,
              m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=250_000.0)
    assert hot[0] is True


def test_band_scope_zero_means_every_size():
    hot = vad(None, 5_000_000.0, abs_floor=0, turnover_floor=0, vol_m5=50_000_000,
              m5_floor=0.10, m5_ceiling=1.50, m5_band_max_mcap=0)
    assert hot[0] is False and hot[2] == "m5_turnover_over_ceiling"
