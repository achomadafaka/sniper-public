"""The measurement-only 60 s time-cap shadow read (spec 2026-09-30-memecoin-timecap-shadow-design.md).

Pinned: a position the rail closed first changes nothing (a true zero); a position open at the cap
is priced at the rail's own exit_decision quote within 10 s after it, and at nothing else; no quote
there is unmeasured (absent, never zero); a partial exit before the cap takes the position out; the
exit-cost adjustment uses the cohort's own quote-to-fill gap.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "timecap_shadow_read", ROOT / "scripts" / "research" / "timecap_shadow_read.py")
tc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tc)

OPEN = "2026-09-30T10:00:00+00:00"          # the fill: T counts from here
DECIDED = "2026-09-30T09:59:55+00:00"       # the decision, 5 s earlier (4663-like)


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, pid, *, hold_s, pnl, notional=100.0, network="solana"):
    closed = "2026-09-30T10:%02d:%02d+00:00" % divmod(int(hold_s), 60)
    c.execute("INSERT INTO memecoin_positions (id, source_user_id, mint, ticker, entry_price, "
              "entry_notional, is_open, opened_at, opened_recorded_at, closed_at, realized_pnl, "
              "execution_mode, network_id, signal_source, strategy, exit_reason) "
              "VALUES (?, 'w1', ?, 'X', 1e-6, ?, 0, ?, ?, ?, ?, 'real', ?, 'follow_activity', "
              "'alpha_b', 'b_stop')", (pid, "M%d" % pid, notional, DECIDED, OPEN, closed, pnl,
                                       network))
    c.commit()
    return dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())


def _quote(c, pid, at_s, usd, seam="exit_decision"):
    c.execute("INSERT INTO memecoin_route_series (position_id, mint, network_id, seam, observed_at, "
              "expected_usdc) VALUES (?, ?, 'solana', ?, ?, ?)",
              (pid, "M%d" % pid, seam, "2026-09-30T10:%02d:%06.3f+00:00" % divmod(at_s, 60), usd))
    c.commit()


def test_a_position_the_rail_closed_first_is_a_true_zero():
    c = _ledger()
    p = _pos(c, 1, hold_s=45, pnl=25.0)
    assert tc.shadow(c, p, 60.0) == ("closed_before_cap", 0.0)


def test_the_cap_is_priced_at_the_first_exit_decision_quote_after_it():
    c = _ledger()
    p = _pos(c, 2, hold_s=300, pnl=-40.0)                  # actual proceeds 60
    _quote(c, 2, 59.5, 200.0)                               # before the cap: not the cap's price
    _quote(c, 2, 60.8, 90.0, seam="peak_preview")           # another seam prices another thing
    _quote(c, 2, 61.2, 95.0)
    _quote(c, 2, 65.0, 80.0)
    assert tc.shadow(c, p, 60.0) == ("measured", pytest.approx(35.0))


def test_no_quote_near_the_cap_is_unmeasured_not_zero():
    c = _ledger()
    p = _pos(c, 3, hold_s=300, pnl=-40.0)
    _quote(c, 3, 75.0, 95.0)                                # 15 s late: not "exiting at 60 s"
    assert tc.shadow(c, p, 60.0) == ("unmeasured", None)


def test_a_partial_exit_before_the_cap_takes_the_position_out():
    c = _ledger()
    p = _pos(c, 4, hold_s=300, pnl=-40.0)
    _quote(c, 4, 61.0, 95.0)
    c.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at) "
              "VALUES (4, 'derisk', 1e-6, 1, 30.0, '2026-09-30T10:00:40+00:00')")
    c.commit()
    assert tc.shadow(c, p, 60.0) == ("partial_before_cap", None)


def test_any_exit_fill_before_the_cap_is_a_partial():
    """Gamma post-review: live partials are also 'partial_exit' and 'mirror_exit', not only derisk."""
    c = _ledger()
    p = _pos(c, 6, hold_s=300, pnl=-40.0)
    _quote(c, 6, 61.0, 95.0)
    c.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at) "
              "VALUES (6, 'mirror_exit', 1e-6, 1, 30.0, '2026-09-30T10:00:40+00:00')")
    c.commit()
    assert tc.shadow(c, p, 60.0) == ("partial_before_cap", None)


def test_the_cap_counts_from_the_fill_not_the_decision():
    """A position filled at 10:00:00 (decided 09:59:55) and closed at 10:00:58 was closed before a
    60 s cap measured from the FILL, although 63 s passed since the decision."""
    c = _ledger()
    p = _pos(c, 7, hold_s=58, pnl=5.0)
    _quote(c, 7, 57.0, 110.0)
    assert tc.shadow(c, p, 60.0) == ("closed_before_cap", 0.0)


def test_a_position_without_a_fill_stamp_is_absent_not_zero():
    c = _ledger()
    p = _pos(c, 8, hold_s=300, pnl=-40.0)
    p["opened_recorded_at"] = None
    assert tc.shadow(c, p, 60.0) == ("no_fill_stamp", None)


def test_a_position_closed_in_several_sells_has_no_exit_cost_sample():
    c = _ledger()
    _pos(c, 9, hold_s=300, pnl=-40.0)
    _quote(c, 9, 295.0, 75.0)
    for kind, at in (("partial_exit", "2026-09-30T10:04:00+00:00"),
                     ("bracket_exit", "2026-09-30T10:05:00+00:00")):
        c.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at) "
                  "VALUES (9, ?, 1e-6, 1, 30.0, ?)", (kind, at))
    c.commit()
    assert tc.exit_cost_frac(c, tc.positions(c, DECIDED)) is None


def test_the_exit_cost_adjustment_uses_the_cohorts_own_quote_to_fill_gap():
    c = _ledger()
    _pos(c, 5, hold_s=300, pnl=-40.0)                      # real proceeds 60 ...
    _quote(c, 5, 61.0, 100.0)
    _quote(c, 5, 295.0, 75.0)                               # ... against a 75 quote at close: -20%
    c.execute("INSERT INTO memecoin_fills (position_id, kind, price, qty, notional, created_at) "
              "VALUES (5, 'bracket_exit', 1e-6, 1, 60.0, '2026-09-30T10:05:00+00:00')")
    c.commit()
    out = tc.read(c, tc.positions(c, DECIDED), 60.0)["solana"]
    assert out["status"] == {"measured": 1}
    assert out["shadow_delta_usd"] == pytest.approx(40.0)
    assert out["exit_cost_mean_pct"] == pytest.approx(-20.0)
    assert out["shadow_delta_after_exit_cost_usd"] == pytest.approx(100.0 * 0.8 - 60.0)
