"""Performance splits by who decided; risk counts every dollar.

The operator's manual picks were reading as system losses — 08-26 robot +$247.25 against manual
-$24.42, 08-27 robot +$74.18 against manual -$64.08, with manual the majority of closes by count on
the 27th. So performance surfaces split. The ledger is immutable: nothing is deleted or hidden, and
robot + manual must always reconcile to the all-money total.

THE TRAP, and it was live in the order as written. `daily_real_realized_pnl` is a bare
SUM(realized_pnl) and it feeds the daily loss BREAKER. Applying the performance filter to it would
have stopped the breaker counting $64.08 of real losses on 08-27, leaving it green through a
drawdown it exists to stop. The first test here is that trap, written behaviourally so it fails on
what the function DOES rather than on how the SQL is spelled.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


DAY = "2026-08-27"


def _closed(c, *, pnl, source, mint="M1", mode="real"):
    c.execute(
        "INSERT INTO memecoin_positions(mint, network_id, execution_mode, signal_source, is_open, "
        "opened_at, closed_at, realized_pnl, entry_notional) "
        "VALUES (?,'solana',?,?,0,?,?,?,100.0)",
        (mint, mode, source, DAY + "T09:00:00", DAY + "T10:00:00", pnl))
    c.commit()


# ---------------------------------------------------- the breaker must not be blinded

def test_the_daily_breaker_still_counts_a_manual_loss(conn):
    """THE regression this whole split could have caused. A manual loss is a real loss."""
    _closed(conn, pnl=+74.18, source="follow_activity")
    _closed(conn, pnl=-64.08, source="manual", mint="M2")
    assert m.daily_real_realized_pnl(conn, DAY) == pytest.approx(10.10)


def test_the_breaker_latches_on_a_manual_only_drawdown(conn):
    """The end-to-end shape: nothing but manual losses must still trip the breaker."""
    _closed(conn, pnl=-250.0, source="manual")
    assert m.check_and_latch_daily_loss(conn, DAY, 200.0) is True


def test_a_manual_only_drawdown_would_NOT_trip_under_the_performance_filter(conn):
    """Proves the previous test is load-bearing rather than incidentally true — under the
    robot filter the same losses vanish and the breaker stays green."""
    _closed(conn, pnl=-250.0, source="manual")
    robot = conn.execute("SELECT COALESCE(SUM(realized_pnl),0) FROM memecoin_positions "
                         "WHERE is_open=0 AND " + m.robot_performance_sql()).fetchone()[0]
    assert robot == 0.0
    assert m.daily_real_realized_pnl(conn, DAY) == pytest.approx(-250.0)


# ---------------------------------------------------- the split itself

def test_robot_and_manual_reconcile_to_all_money(conn):
    _closed(conn, pnl=+74.18, source="follow_activity")
    _closed(conn, pnl=-64.08, source="manual", mint="M2")
    a = m.real_accounting(conn, price_map={}, day=DAY)
    assert a["robot_realized_today"] == pytest.approx(74.18)
    assert a["manual_realized_today"] == pytest.approx(-64.08)
    assert a["realized_today"] == pytest.approx(10.10)
    assert a["robot_realized_today"] + a["manual_realized_today"] == pytest.approx(a["realized_today"])
    assert a["robot_realized_total"] + a["manual_realized_total"] == pytest.approx(a["realized_total"])


def test_paper_rows_reach_neither_number(conn):
    """Both predicates are funded-only; a paper loss must not appear in either."""
    _closed(conn, pnl=-500.0, source="global_feed", mode="paper")
    a = m.real_accounting(conn, price_map={}, day=DAY)
    assert a["realized_today"] == 0.0 and a["robot_realized_today"] == 0.0


def test_a_fomo_priced_row_leaves_robot_performance_but_stays_in_all_money(conn):
    """Three censors, composed: fomo is a dishonest PRICE, manual is a different DECIDER. A fomo
    row is not the operator's fault and its dollars are still real."""
    _closed(conn, pnl=+40.0, source="follow_activity")
    pid = conn.execute("SELECT id FROM memecoin_positions").fetchone()[0]
    conn.execute("INSERT INTO memecoin_fills(position_id, kind, qty, notional, fill_source, "
                 "created_at) VALUES (?, 'entry', 1.0, 1.0, 'fomo', ?)", (pid, DAY))
    conn.commit()
    a = m.real_accounting(conn, price_map={}, day=DAY)
    assert a["robot_realized_today"] == 0.0, "fomo-priced rows leave the performance surface"
    assert a["realized_today"] == pytest.approx(40.0), "but the money still moved"


# ---------------------------------------------------- the names must stay separable

def test_manual_is_not_folded_into_the_fomo_predicate():
    """HONEST_COPY_SQL means 'this row has a fomo-priced fill'. Folding manual in would make the
    name lie and tell a future reader that manual rows have fill problems."""
    assert "signal_source" not in m.HONEST_COPY_SQL
    assert "fomo" in m.HONEST_COPY_SQL


def test_real_money_removes_nothing_but_paper():
    sql = m.real_money_sql()
    assert "execution_mode" in sql
    for forbidden in ("signal_source", "fomo"):
        assert forbidden not in sql, "the risk predicate must not filter anything but mode"


def test_daily_real_realized_pnl_does_not_read_the_robot_predicate():
    """Source guard, secondary to the behavioural tests above. Uses code_of so the docstring — which
    NAMES the predicate it forbids — cannot match itself (AGENTS.md, source-scanning guards)."""
    from _source_guard import code_of
    code = code_of(m.daily_real_realized_pnl)
    for forbidden in ("robot_performance_sql", "robot_decision_sql", "signal_source"):
        assert forbidden not in code, (
            f"the daily loss breaker's input reads {forbidden}; a manual loss would stop counting")
