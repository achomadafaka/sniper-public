"""The two trading breakers are operator-switchable from the cockpit (2026-09-18).

The drawdown pause and the daily loss breaker are CAPITAL rails, so `memecoin_entry_knobs` would
normally lock them. They are adjustable at the operator's request: a breaker that can only be moved
by editing `.env` and restarting a release-pinned real-money process is a breaker nobody turns off
on the day it is wrong.

The trap these tests exist for: the daily breaker LATCHES into `memecoin_daily_loss_trips`, and the
entry context read that latch without consulting the limit. Setting the limit to 0 while today was
already latched therefore left entries blocked by a breaker that had just been switched off.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.services import memecoin_entry_knobs as knobs

BREAKERS = ("MEMECOIN_DRAWDOWN_PAUSE_USD", "MEMECOIN_DRAWDOWN_PAUSE_S",
            "MEMECOIN_DAILY_LOSS_LIMIT_USD")


@pytest.mark.parametrize("name", BREAKERS)
def test_the_breaker_is_in_the_registry_and_adjustable(name):
    assert name in knobs.KNOBS, "the cockpit cannot move a knob it does not know"
    assert name in knobs.ADJUSTABLE
    assert knobs.KNOBS[name].adjustable is True


@pytest.mark.parametrize("name", BREAKERS)
def test_each_breaker_has_an_off_switch(name):
    """The cockpit's disable writes `off_value`, so 'off' is an audited decision, not a missing row."""
    assert knobs.KNOBS[name].off_value == 0


@pytest.mark.parametrize("name", BREAKERS)
def test_zero_is_inside_the_declared_bounds(name):
    """An override outside its bounds is dropped, so a floor above 0 would make the switch inert."""
    knob = knobs.KNOBS[name]
    assert knob.minimum is not None and knob.minimum <= 0
    assert knob.coerce(0) == 0


def test_zero_disables_the_drawdown_pause():
    from sniper.persistence import db_memecoin as m
    assert m.drawdown_pause_until(None, "2026-09-18", 0, 2640, "2026-09-18T10:00:00+00:00") is None
    assert m.drawdown_pause_until(None, "2026-09-18", None, 2640, "2026-09-18T10:00:00+00:00") is None


def test_zero_disables_the_daily_breaker():
    from sniper.persistence import db_memecoin as m
    assert m.check_and_latch_daily_loss(None, "2026-09-18", 0) is False
    assert m.check_and_latch_daily_loss(None, "2026-09-18", None) is False


def _conn_with_a_real_latch(day, limit_usd):
    """A REAL closed position losing enough to actually trip check_and_latch_daily_loss -- not a
    monkeypatched return value standing in for one. Returns the connection with the latch already
    written."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(conn, source_user_id="u", source_handle="h", mint="M1", ticker="T",
                          entry_price=1.0, entry_notional=175.0, entry_fill_source="chain",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at=day + "T00:00:00+00:00",
                          execution_mode="real")
    m.close_position(conn, pid, day + "T09:00:00+00:00", realized_pnl=-(limit_usd + 25.0))
    assert m.check_and_latch_daily_loss(conn, day, limit_usd) is True
    assert m.is_daily_loss_tripped(conn, day) is True     # the latch is real and present
    return conn


def test_a_disabled_breaker_releases_a_latch_that_already_fired(monkeypatch):
    """THE DEFECT THIS FILE EXISTS FOR, and the B3 audit fix: build the REAL context (not repeat
    its formula) at a zero and a positive limit against the SAME already-fired latch."""
    from sniper.services import memecoin_risk as risk
    day = "2026-09-18"
    conn = _conn_with_a_real_latch(day, 50.0)

    monkeypatch.setattr(risk.cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    ctx_off = risk.build_memecoin_risk_context(conn, usdc_balance=100.0, sol_balance=1.0, day=day)
    assert ctx_off.daily_tripped is False, "the breaker was switched off; the context must say so"

    monkeypatch.setattr(risk.cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 50.0)
    ctx_on = risk.build_memecoin_risk_context(conn, usdc_balance=100.0, sol_balance=1.0, day=day)
    assert ctx_on.daily_tripped is True, "the same latch, breaker back on, must still block"


def test_both_risk_modules_guard_the_latch_the_same_way(monkeypatch):
    """Solana and EVM build their own context; a fix in one is not a fix in the other. Calls both
    real builders against a real latch, rather than grepping source text for a formula string."""
    from sniper.services import memecoin_evm_risk as evm
    from sniper.services import memecoin_risk as risk
    day = "2026-09-18"
    conn = _conn_with_a_real_latch(day, 50.0)

    for mod in (risk, evm):
        monkeypatch.setattr(mod.cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    sol_off = risk.build_memecoin_risk_context(conn, usdc_balance=100.0, sol_balance=1.0, day=day)
    evm_off = evm.build_evm_risk_context(conn, usdc_balance=100.0, native_balance=1.0, day=day,
                                         network_id="4663")
    assert sol_off.daily_tripped is False
    assert evm_off.daily_tripped is False

    for mod in (risk, evm):
        monkeypatch.setattr(mod.cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 50.0)
    sol_on = risk.build_memecoin_risk_context(conn, usdc_balance=100.0, sol_balance=1.0, day=day)
    evm_on = evm.build_evm_risk_context(conn, usdc_balance=100.0, native_balance=1.0, day=day,
                                        network_id="4663")
    assert sol_on.daily_tripped is True
    assert evm_on.daily_tripped is True


def test_the_capital_locks_are_still_locked():
    """Switching these two on purpose must not quietly unlock the rest of the safety group."""
    for name in ("MEMECOIN_ENTRY_IMPACT_CAP_PCT", "MEMECOIN_CHASE_SOURCE_IMPACT_MAX_PCT",
                 "MEMECOIN_REAL_MAX_EXEC_PREMIUM", "MEMECOIN_MIN_TRADE_FLOOR_USD"):
        assert knobs.KNOBS[name].adjustable is False
        assert name not in knobs.ADJUSTABLE
