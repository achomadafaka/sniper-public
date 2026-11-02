"""`stuck_bag` must clear when the bag is demonstrably routable again.

RATBRAIN 64081 (2026-09-23) sat flagged at -13.4% against a -35% stop while a whole-bag preview
priced it at $26.91, because nothing in the tree ever set the column back to 0. The flag drives the
operator page, the DEAD verdict in stuck_bag_report, and stuck_backoff's retry delay, so a stale 1
delays the exit of a position that can now exit.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _position(c, **fields):
    base = dict(source_user_id="u1", mint="0xabc", ticker="T", network_id="4663",
                entry_price=1e-05, entry_notional=40.0, execution_mode="real")
    base.update(fields)
    cols = ",".join(base)
    c.execute("INSERT INTO memecoin_positions(%s) VALUES(%s)" % (cols, ",".join("?" * len(base))),
              tuple(base.values()))
    c.commit()
    return c.execute("SELECT MAX(id) FROM memecoin_positions").fetchone()[0]


def _flag(c, pid):
    return c.execute("SELECT stuck_bag FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0]


def test_mark_then_clear_round_trips():
    c = _conn()
    pid = _position(c)
    m.mark_stuck_bag(c, pid)
    assert _flag(c, pid) == 1
    assert m.clear_stuck_bag(c, pid) is True, "a flagged position must clear"
    assert _flag(c, pid) == 0


def test_clearing_an_unflagged_position_changes_nothing_and_reports_false():
    """The caller logs on True, so a no-op must not claim it cleared something."""
    c = _conn()
    pid = _position(c)
    assert _flag(c, pid) == 0
    assert m.clear_stuck_bag(c, pid) is False
    assert _flag(c, pid) == 0


def test_clear_is_idempotent():
    c = _conn()
    pid = _position(c)
    m.mark_stuck_bag(c, pid)
    assert m.clear_stuck_bag(c, pid) is True
    assert m.clear_stuck_bag(c, pid) is False, "second call must report no change"


def test_clear_touches_only_the_named_position():
    c = _conn()
    keep = _position(c, mint="0xkeep")
    other = _position(c, mint="0xother")
    m.mark_stuck_bag(c, keep)
    m.mark_stuck_bag(c, other)
    m.clear_stuck_bag(c, keep)
    assert _flag(c, keep) == 0
    assert _flag(c, other) == 1, "clearing one bag must not un-flag the rest of the book"


def test_the_runner_clears_only_on_a_positive_whole_bag_quote():
    """The guard the sweep applies: a mark or a missing quote must NEVER clear the flag."""
    c = _conn()
    pid = _position(c)
    m.mark_stuck_bag(c, pid)
    for bag_quote in (None, 0, 0.0, -1.0):
        pos = {"id": pid, "mint": "0xabc", "stuck_bag": 1, "_exit_bag_quote": bag_quote}
        if pos.get("stuck_bag") and (pos.get("_exit_bag_quote") or 0) > 0:
            m.clear_stuck_bag(c, pid)
        assert _flag(c, pid) == 1, "no routable evidence -> the flag must stand (%r)" % bag_quote
    pos = {"id": pid, "mint": "0xabc", "stuck_bag": 1, "_exit_bag_quote": 26.91}
    if pos.get("stuck_bag") and (pos.get("_exit_bag_quote") or 0) > 0:
        m.clear_stuck_bag(c, pid)
    assert _flag(c, pid) == 0, "a $26.91 whole-bag route quote clears it"


def test_a_recent_stuck_attempt_keeps_the_flag_and_an_old_one_does_not():
    """An execution failure outranks a quote: the same route that quotes can be the one that
    reverts (RATBRAIN 64081), so the flag stands while a stuck attempt is younger than the backoff
    ceiling and clears once it is older."""
    c = _conn()
    pid = _position(c)
    m.mark_stuck_bag(c, pid)
    c.execute("INSERT INTO memecoin_exit_execution_attempts(position_id, network_id, exit_reason, "
              "attempted_at, status, tier_variant) VALUES (?,?,?,?,?,?)",
              (pid, "4663", "b_stop", "2026-09-23T08:22:05+00:00", "stuck", "baseline"))
    c.commit()
    assert m.clear_stuck_bag(c, pid, now_iso="2026-09-23T08:23:00+00:00", quiet_s=120) is False
    assert _flag(c, pid) == 1
    assert m.clear_stuck_bag(c, pid, now_iso="2026-09-23T08:25:00+00:00", quiet_s=120) is True
    assert _flag(c, pid) == 0
