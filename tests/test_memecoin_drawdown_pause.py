"""Drawdown pause (operator 2026-09-15): today's realized PnL falls $100 below the day's high ->
automatic entries pause 60 min, then the high resets. Manual buys are exempt."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm
from sniper.services import memecoin_risk as sol
from sniper.settings import memecoin as cfg

DAY = "2026-09-15"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _closed(c, pnl, hhmm, mint=None, day=DAY):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint or "M%s%s" % (pnl, hhmm),
                          ticker="T", entry_price=1.0, entry_notional=175.0, entry_fill_source="chain",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at=DAY + "T00:00:00+00:00",
                          execution_mode="real")
    m.close_position(c, pid, "%sT%s:00+00:00" % (day, hhmm), realized_pnl=pnl)


def _today(c):
    """The 2026-09-15 burst: +74.5 by 09:18, then two losses of -75.6 and -59.9.

    The coin tickers are dropped deliberately. One of the two was a slur, and a ticker the market
    chose is not a name this file has to repeat to describe an arithmetic sequence.
    """
    for pnl, t in ((31.0, "09:08"), (43.5, "09:18"), (-75.6, "09:44"), (-59.9, "09:55")):
        _closed(c, pnl, t)


def _until(c, now, drop=100.0):
    return m.drawdown_pause_until(c, DAY, drop, 3600, "%sT%s:00+00:00" % (DAY, now))


def test_a_drop_from_the_day_high_pauses_for_an_hour_then_resumes():
    c = _c(); _today(c)
    assert _until(c, "09:54") is None                       # -75.6 from +74.5 is not yet $100
    assert _until(c, "09:56") == DAY + "T10:55:00+00:00"   # $135 below the high at 09:55
    assert _until(c, "10:54") is not None
    assert _until(c, "10:55") is None                       # a pause, never a rest-of-day stop


def test_the_high_resets_at_the_trigger_so_a_second_drop_is_needed():
    c = _c(); _today(c)
    _closed(c, -40.0, "11:10")                              # -40 from the reset level: no pause
    assert _until(c, "11:11") is None
    _closed(c, -61.0, "11:30")                              # -101 from the reset level: pause again
    assert _until(c, "11:31") == DAY + "T12:30:00+00:00"


def test_closes_inside_a_pause_do_not_extend_it():
    c = _c(); _today(c)
    _closed(c, -120.0, "10:10")
    assert _until(c, "10:56") is None                       # the 10:10 loss landed inside the pause
    _closed(c, -1.0, "11:00")                               # the first close after the pause re-checks
    assert _until(c, "11:01") == DAY + "T12:00:00+00:00"


def test_off_by_default_and_paper_never_counts():
    c = _c(); _today(c)
    assert _until(c, "09:56", drop=0) is None
    c2 = _c()
    pid = m.open_position(c2, source_user_id="u", source_handle="h", mint="P", ticker="T", entry_price=1.0,
                          entry_notional=175.0, entry_fill_source="chain", entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at=DAY + "T00:00:00+00:00", execution_mode="paper")
    m.close_position(c2, pid, DAY + "T09:00:00+00:00", realized_pnl=-500.0)
    assert _until(c2, "09:01") is None


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DRAWDOWN_PAUSE_S", 3600.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 1000.0)
    for mod in (sol, evm):
        monkeypatch.setattr(mod, "real_utc_now_iso", lambda: DAY + "T10:00:00+00:00")
    c = _c(); _today(c); return c


def test_both_gates_refuse_an_automatic_entry_and_name_the_resume_time(paused):
    ctx = sol.build_memecoin_risk_context(paused, usdc_balance=300.0, sol_balance=1.0, day=DAY)
    d = sol.memecoin_entry_allowed(ctx, mint="NEW", trade_size=ctx.trade_size_usd)
    assert not d.allowed and "drawdown pause" in d.reason and "10:55:00" in d.reason
    ectx = evm.build_evm_risk_context(paused, usdc_balance=300.0, native_balance=1.0, day=DAY, network_id="4663")
    e = evm.evm_entry_allowed(ectx, mint="0xnew", trade_size=ectx.trade_size_usd)
    assert not e.allowed and "drawdown pause" in e.reason


def test_a_manual_buy_is_exempt(paused):
    ctx = sol.build_memecoin_risk_context(paused, usdc_balance=300.0, sol_balance=1.0, day=DAY,
                                          enforce_concurrency=False)
    assert ctx.drawdown_pause_until is None
    ectx = evm.build_evm_risk_context(paused, usdc_balance=300.0, native_balance=1.0, day=DAY,
                                      network_id="4663", enforce_concurrency=False)
    assert ectx.drawdown_pause_until is None


def test_a_fresh_start_counts_only_later_closes_and_expires_with_the_day(monkeypatch):
    c = _c(); _today(c)
    _closed(c, -200.0, "10:19")                             # the day is now -261: the $250 limit latches
    assert m.check_and_latch_daily_loss(c, DAY, 250.0, commit=False) is True
    c.execute("DELETE FROM memecoin_daily_loss_trips")
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", DAY + "T15:30:00+00:00")
    assert m.daily_risk_realized_pnl(c, DAY) == 0.0
    assert m.check_and_latch_daily_loss(c, DAY, 250.0) is False
    _closed(c, -60.0, "15:40")
    assert _until(c, "15:41") is None                       # -60 from the fresh high of 0
    _closed(c, -50.0, "15:50")
    assert _until(c, "15:51") == DAY + "T16:50:00+00:00"    # -110 from the fresh high
    assert m.risk_day_since("2026-09-16") == "2026-09-16"   # another day ignores the value


def test_risk_reset_preserves_full_day_accounting(monkeypatch):
    c = _c()
    _closed(c, -200.0, "10:00")
    _closed(c, 30.0, "16:00")
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", DAY + "T15:30:00+00:00")
    accounting = m.real_accounting(c, price_map={}, day=DAY)
    assert accounting["realized_today"] == -170.0
    assert accounting["robot_realized_today"] + accounting["manual_realized_today"] == -170.0
    assert m.daily_risk_realized_pnl(c, DAY) == 30.0
    assert not m.check_and_latch_daily_loss(c, DAY, 100.0)


def test_midnight_preserves_the_pause_and_resets_the_daily_peak():
    c = _c()
    _closed(c, 200.0, "23:50")
    _closed(c, -110.0, "23:55")
    tomorrow = "2026-09-16"
    def until(hhmm):
        return m.drawdown_pause_until(c, tomorrow, 100.0, 3600, tomorrow + "T" + hhmm + ":00+00:00")
    assert until("00:01") == tomorrow + "T00:55:00+00:00"
    _closed(c, -60.0, "00:10", day=tomorrow)
    assert until("00:54") == tomorrow + "T00:55:00+00:00"
    assert until("00:55") is None
    _closed(c, -30.0, "01:00", day=tomorrow)
    assert until("01:01") is None
    _closed(c, -11.0, "01:10", day=tomorrow)
    assert until("01:11") == tomorrow + "T02:10:00+00:00"


def test_an_older_pause_changes_the_next_days_pause(monkeypatch):
    c = _c()
    _closed(c, -110.0, "23:55", day="2026-09-13")
    for pnl, stamp in ((-110.0, "00:10"), (-1.0, "00:56"),
                       (-100.0, "01:20"), (-1.0, "23:55")):
        _closed(c, pnl, stamp, day="2026-09-14")
    now = DAY + "T00:01:00+00:00"
    assert m.drawdown_pause_until(c, DAY, 100, 3600, now) == DAY + "T00:55:00+00:00"
    # An explicit reset clears the prior pause; midnight alone does not.
    monkeypatch.setattr(cfg, "MEMECOIN_RISK_DAY_START_AT", DAY + "T00:00:00+00:00")
    assert m.drawdown_pause_until(c, DAY, 100, 3600, now) is None
