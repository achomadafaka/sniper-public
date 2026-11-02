"""WO-5 part 1 — persisted controller-state substrate.

Six columns on memecoin_positions carry the exit-decision hysteresis (structure-break persistence,
negative-flow / route-degradation onset, route/infra failure streaks) so it survives a runner
restart by construction. All default NULL/0 -> the engine's behavior is UNCHANGED until WO-7
registers the persistence-window + sizing constants that read them.
"""
import sqlite3

import pytest

from sniper.persistence.db_memecoin import (
    CONTROLLER_STATE_COLS, ensure_memecoin_tables, set_position_controller_state)

_WANTED = ("structure_break_since", "consecutive_break_events", "negative_flow_since",
           "route_degradation_since", "route_failure_streak", "infra_failure_streak")


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    return c


def _open(c):
    return c.execute(
        "INSERT INTO memecoin_positions(source_user_id, mint, entry_price, opened_at) "
        "VALUES('u','M',1.0,'t')").lastrowid


def test_migration_adds_all_six_columns_defaulting_inert():
    c = _db()
    cols = {r["name"]: r["dflt_value"] for r in c.execute("PRAGMA table_info(memecoin_positions)")}
    for w in _WANTED:
        assert w in cols, f"{w} missing from migration"
    pid = _open(c)
    row = c.execute(
        "SELECT structure_break_since, consecutive_break_events, negative_flow_since, "
        "route_degradation_since, route_failure_streak, infra_failure_streak "
        "FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    # inert by default: timestamps NULL, streaks 0 -> engine behavior unchanged
    assert row["structure_break_since"] is None and row["negative_flow_since"] is None
    assert row["route_degradation_since"] is None
    assert row["consecutive_break_events"] == 0 and row["route_failure_streak"] == 0
    assert row["infra_failure_streak"] == 0


def test_set_controller_state_persists_a_subset():
    c = _db()
    pid = _open(c)
    set_position_controller_state(
        c, pid, structure_break_since="2026-07-25T00:00:00Z", consecutive_break_events=2,
        route_failure_streak=3)
    row = c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["structure_break_since"] == "2026-07-25T00:00:00Z"
    assert row["consecutive_break_events"] == 2 and row["route_failure_streak"] == 3
    # untouched columns stay inert
    assert row["negative_flow_since"] is None and row["infra_failure_streak"] == 0


def test_set_controller_state_rejects_unknown_field():
    c = _db()
    pid = _open(c)
    with pytest.raises(ValueError, match="unknown controller-state"):
        set_position_controller_state(c, pid, exact_quote_misses=5)   # not a controller-state col


def test_controller_state_cols_constant_matches():
    # WO-5A grew the writable set (event id, executable_peak_at/price, route_quote_value/at). The six
    # part-1 columns remain part of it; the exact new members are pinned in the WO-5A reducer tests.
    assert set(_WANTED) <= set(CONTROLLER_STATE_COLS)
