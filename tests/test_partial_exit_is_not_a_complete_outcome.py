"""A close that priced only PART of the bag is not a complete outcome.

`observed_exit_sql` asks whether an executable exit leg EXISTS. That is satisfied by the sold half
of a partial exit whose remainder never got a quote, so 71 rows sat inside the measurable
population at mean +45.20 against a whole-population mean of 2.44 — half the bag presented as the
outcome of the whole, with the unpriced remainder a median 50% of the exited quantity.

The stored realized_pnl is NOT touched: it is true for the portion that sold.
"""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m

DDL = """
CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, is_open INTEGER, realized_pnl REAL,
  exit_reason TEXT, entry_fill_source TEXT, last_quote_price REAL);
CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER, kind TEXT, qty REAL,
  fill_source TEXT);
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(DDL)
    return c


def _pos(c, pid, legs, *, reason="real_tp", pnl=45.0):
    c.execute("INSERT INTO memecoin_positions (id,is_open,realized_pnl,exit_reason,"
              "entry_fill_source,last_quote_price) VALUES (?,0,?,?,'route_quote',0.5)",
              (pid, pnl, reason))
    c.execute("INSERT INTO memecoin_fills (position_id,kind,qty,fill_source) "
              "VALUES (?,'entry',100.0,'route_quote')", (pid,))
    for kind, source in legs:
        c.execute("INSERT INTO memecoin_fills (position_id,kind,qty,fill_source) "
                  "VALUES (?,?,50.0,?)", (pid, kind, source))
    c.commit()


def _measurable(c):
    return [r[0] for r in c.execute(
        "SELECT id FROM memecoin_positions p WHERE %s ORDER BY id" % m.measurable_outcome_sql("p"))]


def test_a_partial_exit_with_an_unpriced_remainder_is_censored(conn):
    """THE 71. A real partial sell, then a rail fires on that part, and an earlier leg was never
    priced. The reason says `real_tp` — it names the final rail, not the composition of the legs."""
    _pos(conn, 1, [("partial_exit", "route_quote"), ("bracket_exit", "noquote")])
    assert _measurable(conn) == []


def test_a_fully_priced_exit_stays_in_the_population(conn):
    """NEGATIVE CONTROL. The censor must not swallow complete outcomes to catch partial ones."""
    _pos(conn, 2, [("partial_exit", "route_quote"), ("bracket_exit", "route_quote")])
    assert _measurable(conn) == [2]


def test_a_cleanup_remainder_is_censored_too(conn):
    """`cleanup` is the other non-executable source and leaves the same unpriced remainder."""
    _pos(conn, 3, [("partial_exit", "dexscreener"), ("manual_close", "cleanup")])
    assert _measurable(conn) == []


def test_the_stored_pnl_is_NOT_modified(conn):
    """The fix belongs in the population, not the row. Destroying a true number to correct a
    framing problem is the mirror of the bug this change exists to fix."""
    _pos(conn, 4, [("partial_exit", "route_quote"), ("bracket_exit", "noquote")], pnl=45.2)
    assert conn.execute("SELECT realized_pnl FROM memecoin_positions WHERE id=4").fetchone()[0] == 45.2


def test_it_keys_on_the_LEG_not_the_reason(conn):
    """Both rows carry an unpriced leg; only one has a reason that admits it. The reason-string
    condition catches the second and always missed the first — that gap IS the 71."""
    _pos(conn, 5, [("partial_exit", "route_quote"), ("bracket_exit", "noquote")], reason="real_tp")
    _pos(conn, 6, [("partial_exit", "route_quote"), ("bracket_exit", "noquote")],
         reason="a_timecap_noquote")
    assert _measurable(conn) == []


def test_a_close_with_no_exit_legs_is_censored_by_a_DIFFERENT_rule(conn):
    """CORRECTED. I expected this row to survive because `observed_exit_sql` tolerates rows with no
    exit legs — but a later, pre-existing condition fails a closed position with no exit fill as an
    orphan close, so it never reaches the new clause.

    Verified by isolating my clause and re-running: censored with it and without it. The
    distinction matters because "my change censored this" and "this was already censored" lead to
    opposite decisions, and the test as first written would have blamed the wrong rule.
    """
    _pos(conn, 7, [])
    assert _measurable(conn) == []
