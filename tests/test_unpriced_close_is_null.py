"""An exit we could not price must book NULL, not a fabricated breakeven.

When no quote came back the ledger booked the exit at the ENTRY price. The arithmetic then yielded
exactly 0.0 realized PnL and stored it as a fact: 2,823 paper closes indistinguishable from a real
round trip at flat. NULL is the unknown; 0 is a known zero.

FORWARD ONLY from 2026-08-29. Rows closed earlier keep the fabricated value and carry no marker.
"""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m
from sniper.services.memecoin_copy_trader import _realized_from_exits

ENTRY = 2.0


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE memecoin_fills (id INTEGER PRIMARY KEY, position_id INTEGER,
      kind TEXT, price REAL, qty REAL, fill_source TEXT, price_attribution TEXT)""")
    c.execute("""CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, strategy TEXT,
      execution_mode TEXT, is_open INTEGER, closed_at TEXT)""")
    return c


def _leg(c, pid, price, qty, source, attribution=None, kind="bracket_exit"):
    c.execute("INSERT INTO memecoin_fills (position_id,kind,price,qty,fill_source,"
              "price_attribution) VALUES (?,?,?,?,?,?)", (pid, kind, price, qty, source, attribution))
    c.commit()


def test_an_unpriced_close_books_NULL(conn):
    _leg(conn, 1, ENTRY, 10.0, "noquote", m.PLACEHOLDER_PRICE_ATTRIBUTION)
    assert _realized_from_exits(conn, 1, ENTRY) is None


def test_a_cleanup_fallback_also_books_NULL(conn):
    """`cleanup` with no last quote falls back to the entry price, same fabrication."""
    _leg(conn, 2, ENTRY, 10.0, "cleanup", m.PLACEHOLDER_PRICE_ATTRIBUTION, kind="manual_close")
    assert _realized_from_exits(conn, 2, ENTRY) is None


def test_a_cleanup_with_a_REAL_stale_quote_keeps_its_number(conn):
    """The other half of `cleanup`. A stale quote is a poor observation, not a fabricated one, and
    erasing it would delete a real outcome — the mirror of the bug being fixed."""
    _leg(conn, 3, 1.5, 10.0, "cleanup", None, kind="manual_close")
    assert _realized_from_exits(conn, 3, ENTRY) == pytest.approx((1.5 - ENTRY) * 10.0)


def test_a_partial_leg_close_keeps_its_number(conn):
    """A real partial sell plus an unpriced remainder keeps the realized figure for what SOLD."""
    _leg(conn, 4, 3.0, 4.0, "route_quote", None, kind="partial_exit")
    _leg(conn, 4, ENTRY, 6.0, "noquote", m.PLACEHOLDER_PRICE_ATTRIBUTION)
    assert _realized_from_exits(conn, 4, ENTRY) == pytest.approx((3.0 - ENTRY) * 4.0)


def test_a_GENUINE_breakeven_is_not_erased(conn):
    """NEGATIVE CONTROL, and the reason this keys on the structured field rather than on
    `price == entry_price`. An exit that really filled at the entry price is a true zero."""
    _leg(conn, 5, ENTRY, 10.0, "route_quote", None)
    assert _realized_from_exits(conn, 5, ENTRY) == 0.0


def test_a_pre_marker_noquote_row_is_still_recognised(conn):
    """Rows closed before the marker existed carry `fill_source='noquote'` and nothing else. They
    are not backfilled, but the reader must still not treat them as observations."""
    _leg(conn, 6, ENTRY, 10.0, "noquote", None)
    assert _realized_from_exits(conn, 6, ENTRY) is None


def test_no_exit_legs_at_all_is_also_unknown(conn):
    """`sum([])` is 0.0 in Python, which is the same fabrication by a different route."""
    assert _realized_from_exits(conn, 99, ENTRY) is None


def test_the_per_arm_fabrication_rate_is_reportable(conn):
    """The censor protects the estimand; it does not protect a human reading an arm table. If two
    paired arms ever diverge on this rate, the fabrication becomes a differential bias."""
    for pid, arm, src in ((10, "a", "noquote"), (11, "a", "route_quote"), (12, "b", "route_quote")):
        conn.execute("INSERT INTO memecoin_positions (id,strategy,execution_mode,is_open) "
                     "VALUES (?,?,'paper',0)", (pid, arm))
        _leg(conn, pid, ENTRY, 1.0, src,
             m.PLACEHOLDER_PRICE_ATTRIBUTION if src == "noquote" else None)
    out = m.placeholder_exit_rate_by_arm(conn)
    assert out["a"] == {"closed": 2, "placeholder": 1, "rate": 0.5}
    assert out["b"]["rate"] == 0.0
