"""Closed intel paper positions must be visible with their realized result.

A closed position used to disappear from the API and UI entirely: `open_positions`
filters `status='open'` and nothing else read the table, so the realized PnL — the only
result the paper book produces — was invisible.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence import intel_paper
from sniper.routes.api_intel_paper import _realized_summary, _with_realized


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    intel_paper.ensure_schema(c)
    return c


def _open(c, idea_id, instrument, direction, entry, venue="pm"):
    return intel_paper.open_position(
        c, idea_id=idea_id, venue=venue, instrument=instrument, token_id="t",
        event_title="E", outcome_label=f"O{idea_id}", direction=direction,
        entry_price=entry, entry_ts="2026-07-29T00:00:00+00:00", size_usdc=10.0,
        reviewer_verdict="rejected", reviewer_score=88, holder_snapshot=None, neg_risk=0)


def test_closed_position_is_returned(conn):
    pid = _open(conn, 1, "0xa", "buy_yes", 0.3815)
    intel_paper.close_position(conn, pid, 0.54, "2026-07-29T21:00:00+00:00", "manual")
    assert intel_paper.open_positions(conn) == []
    rows = intel_paper.closed_positions(conn)
    assert len(rows) == 1 and rows[0]["position_id"] == pid


def test_realized_pnl_matches_the_outcome_native_convention(conn):
    """ADR-003: a buy_no stores NO prices, so the arithmetic is the same as a long."""
    pid = _open(conn, 1, "0xa", "buy_no", 0.895)
    intel_paper.close_position(conn, pid, 0.97, "2026-07-29T21:00:00+00:00", "manual")
    r = _with_realized(intel_paper.closed_positions(conn))[0]
    assert round(r["realized_pnl_pct"], 1) == 8.4
    assert round(r["realized_pnl_usdc"], 2) == 0.84


def test_profitable_hl_short_is_not_reported_as_a_loss(conn):
    """The sign convention inverts for shorts — SQL arithmetic would flip this."""
    pid = _open(conn, 2, "flx:OIL", "short", 70.0, venue="hl")
    intel_paper.close_position(conn, pid, 63.0, "2026-07-29T21:00:00+00:00", "manual")
    r = _with_realized(intel_paper.closed_positions(conn))[0]
    assert r["realized_pnl_pct"] > 0, "price fell on a short — that is a profit"
    assert round(r["realized_pnl_pct"], 1) == 10.0


def test_uncomputable_pnl_is_null_not_zero(conn):
    """A closed row with no price has no result and must not read as break-even."""
    pid = _open(conn, 3, "0xc", "buy_yes", 0.5)
    conn.execute("UPDATE intel_paper_positions SET status='closed', close_price=NULL "
                 "WHERE position_id=?", (pid,))
    conn.commit()
    r = _with_realized(intel_paper.closed_positions(conn))[0]
    assert r["realized_pnl_pct"] is None
    assert _realized_summary([r])["n_uncomputable"] == 1


def test_summary_excludes_uncomputable_rows(conn):
    a = _open(conn, 1, "0xa", "buy_yes", 0.4)
    intel_paper.close_position(conn, a, 0.5, "2026-07-29T21:00:00+00:00", "manual")
    b = _open(conn, 2, "0xb", "buy_yes", 0.4)
    conn.execute("UPDATE intel_paper_positions SET status='closed' WHERE position_id=?", (b,))
    conn.commit()
    s = _realized_summary(_with_realized(intel_paper.closed_positions(conn)))
    assert s["n"] == 1 and s["n_uncomputable"] == 1
    assert s["wins"] == 1 and s["losses"] == 0


def test_venue_filter(conn):
    a = _open(conn, 1, "0xa", "buy_yes", 0.4)
    b = _open(conn, 2, "flx:OIL", "long", 70.0, venue="hl")
    for p in (a, b):
        intel_paper.close_position(conn, p, 0.5, "2026-07-29T21:00:00+00:00", "manual")
    assert len(intel_paper.closed_positions(conn, venue="hl")) == 1
    assert len(intel_paper.closed_positions(conn, venue="pm")) == 1
