import sqlite3
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _closed_real(c, pnl, closed_at, mode="real"):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M"+str(pnl), ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="2026-07-09T00:00:00Z")
    c.execute("UPDATE memecoin_positions SET execution_mode=? WHERE id=?", (mode, pid))
    m.close_position(c, pid, closed_at, pnl, exit_reason="x")


def test_paper_pnl_never_counts_toward_real_latch():
    c = _c()
    _closed_real(c, -50.0, "2026-07-09T05:00:00Z", mode="paper")   # paper loss
    assert m.daily_real_realized_pnl(c, "2026-07-09") == 0.0
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is False


def test_latch_trips_and_stays_tripped_after_recovery():
    c = _c()
    _closed_real(c, -40.0, "2026-07-09T05:00:00Z")                 # real loss past -30
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is True
    _closed_real(c, +20.0, "2026-07-09T06:00:00Z")                 # later real win -> net -20
    # PnL recovered above the line, but the latch STAYS tripped (no un-trip / no flap)
    assert m.check_and_latch_daily_loss(c, "2026-07-09", 30.0) is True
    assert m.is_daily_loss_tripped(c, "2026-07-09") is True
    # a new day is clean
    assert m.is_daily_loss_tripped(c, "2026-07-10") is False
