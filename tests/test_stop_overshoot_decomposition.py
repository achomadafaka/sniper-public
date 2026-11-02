"""The b_stop overshoot builder attributes from the CONFIRMATION streak, not the first dip.

The runner resets its confirmation streak whenever a read recovers above the trigger
(`memecoin_real_runner` stop/grace logic), so only the final uninterrupted run below the
trigger produces the exit. A dip that recovered hours earlier belongs to neither stage.

Offline: an in-memory database and a frozen clock. No production read, no provider.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest

from scripts.research import stop_overshoot_decomposition as stop

T0 = dt.datetime(2026, 9, 23, 12, tzinfo=dt.timezone.utc)


def _at(seconds):
    return (T0 + dt.timedelta(seconds=seconds)).isoformat()


def _db(series, decision_pct=-42.0, decision_at=101, fill_price=0.58, entry_price=1.0):
    """One closed b_stop position, its exit_decision route series and its sell fill."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE memecoin_positions(id INTEGER, ticker TEXT, network_id TEXT,"
        " entry_price REAL, entry_notional REAL, realized_pnl REAL, opened_recorded_at TEXT,"
        " opened_at TEXT, closed_at TEXT, closed_recorded_at TEXT, exit_timing_json TEXT,"
        " exit_intent_json TEXT, source_handle TEXT, execution_mode TEXT, is_open INTEGER,"
        " exit_reason TEXT);"
        "CREATE TABLE memecoin_exit_trigger_audit(position_id INTEGER, reason TEXT, lane TEXT,"
        " allowed INTEGER, preview_usdc REAL, display_price REAL, detail TEXT, checked_at TEXT);"
        "CREATE TABLE memecoin_route_series(position_id INTEGER, id INTEGER, observed_at TEXT,"
        " expected_usdc REAL, open_qty REAL, unit_price REAL, seam TEXT);"
        "CREATE TABLE memecoin_fills(position_id INTEGER, kind TEXT, price REAL, qty REAL,"
        " notional REAL, created_at TEXT);"
        "CREATE TABLE memecoin_exit_execution_attempts(position_id INTEGER, status TEXT,"
        " tier_variant TEXT, depth_first_rung_pct REAL, total_ms REAL, stage_ms_json TEXT,"
        " failure_type TEXT, attempted_at TEXT, exit_reason TEXT);"
    )
    conn.execute(
        "INSERT INTO memecoin_positions(id, ticker, network_id, entry_price, entry_notional,"
        " realized_pnl, opened_at, opened_recorded_at, closed_at, execution_mode, is_open,"
        " exit_reason) VALUES(1,'M','solana',?,100,-42,?,?,?, 'real',0,'b_stop')",
        (entry_price, _at(0), _at(0), _at(120)),
    )
    conn.execute(
        "INSERT INTO memecoin_exit_trigger_audit(position_id, reason, lane, allowed,"
        " display_price, detail, checked_at) VALUES(1,'b_stop','test',1,?,?,?)",
        (fill_price,
         json.dumps({"code": "threshold_confirmed", "executable_unit_pnl_pct": decision_pct}),
         _at(decision_at)),
    )
    for i, (second, price) in enumerate(series):
        conn.execute(
            "INSERT INTO memecoin_route_series(position_id, id, observed_at, expected_usdc,"
            " open_qty, unit_price, seam) VALUES(1,?,?,?,100,?, 'exit_decision')",
            (i, _at(second), (price or 0) * 100, price),
        )
    conn.execute(
        "INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, created_at)"
        " VALUES(1,'bracket_exit',?,100,?,?)", (fill_price, fill_price * 100, _at(120)))
    conn.commit()
    return conn


def _row(monkeypatch, conn):
    """Build against `conn`.

    NOTE `stop.sqlite3` IS the sqlite3 module, so patching its `connect` patches it for every
    caller in the process, `_db` included. Build every fixture database BEFORE calling this.
    """
    from sniper.util import clock
    monkeypatch.setattr(clock, "real_utc_now", lambda: (T0 + dt.timedelta(seconds=200)).timestamp())
    with monkeypatch.context() as patched:
        patched.setattr(stop.sqlite3, "connect", lambda *a, **k: conn)
        rows, _now, _cut = stop.build(30)
    assert len(rows) == 1
    return rows[0]


def test_a_recovered_dip_is_not_part_of_the_confirmation_streak(monkeypatch):
    """C1: 0.64 at +1s recovers at +2s, so the streak that fired the stop starts at +100s."""
    row = _row(monkeypatch, _db([(0, 0.90), (1, 0.64), (2, 0.90), (100, 0.60), (101, 0.58)]))

    assert row["first_breach_at"] == _at(100)
    assert row["first_breach_pct"] == pytest.approx(-40.0)
    assert row["debounce_s"] == 1
    assert row["g_observe"] == pytest.approx(-5.0)
    assert row["g_debounce"] == pytest.approx(-2.0)
    # The recovered dip is still reported, as its own count, not folded into a stage.
    assert row["recovered_dips"] == 1


def test_the_read_gap_is_measured_to_the_streak_that_fired(monkeypatch):
    """The clean read before the final streak is the one a faster cadence would have moved."""
    row = _row(monkeypatch, _db([(0, 0.90), (1, 0.64), (2, 0.90), (100, 0.60), (101, 0.58)]))
    assert row["prev_read_pct"] == pytest.approx(-10.0)
    assert row["read_gap_s"] == 98


def test_an_uninterrupted_run_is_attributed_from_its_own_start(monkeypatch):
    """With no recovery the answer is unchanged: the streak is the whole breach run."""
    row = _row(monkeypatch, _db([(0, 0.90), (99, 0.62), (100, 0.60), (101, 0.58)]))
    assert row["first_breach_at"] == _at(99)
    assert row["first_breach_pct"] == pytest.approx(-38.0)
    assert row["debounce_s"] == 2
    assert row["recovered_dips"] == 0


def test_an_unknown_price_breaks_the_streak_without_counting_as_a_recovery(monkeypatch):
    """A read with no price cannot confirm a stop and cannot clear one either."""
    row = _row(monkeypatch, _db([(0, 0.90), (98, 0.62), (99, None), (100, 0.60), (101, 0.58)]))
    assert row["first_breach_at"] == _at(100), "an unpriced read must not be read as -100%"
    assert row["debounce_s"] == 1
    assert row["recovered_dips"] == 0, "an unknown read is not a recovery"
    assert row["unknown_reads"] == 1


def test_the_total_does_not_depend_on_where_the_streak_starts(monkeypatch):
    """The overshoot is realised minus the trigger, so only the per-stage split moves."""
    without_dip_db = _db([(0, 0.90), (100, 0.60), (101, 0.58)])
    with_dip_db = _db([(0, 0.90), (1, 0.64), (2, 0.90), (100, 0.60), (101, 0.58)])
    plain = _row(monkeypatch, without_dip_db)
    with_dip = _row(monkeypatch, with_dip_db)
    assert plain["overshoot"] == pytest.approx(with_dip["overshoot"])
    assert plain["overshoot"] == pytest.approx(-7.0)
