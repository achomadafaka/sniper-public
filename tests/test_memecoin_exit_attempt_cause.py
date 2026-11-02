"""A stuck exit records its CAUSE, not just its status.

Position 48595 produced THIRTEEN identical `stuck` rows carrying no cause. The executor computes
one for every stuck return -- `token_balance_zero` at evm_exit_executor.py:171, the
ladder-exhausted `failure_type` at :360 -- and the call sites dropped it. Which of the two paths
fired had to be inferred from a Telegram message rather than read from the ledger.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    return c


def _rows(c):
    return [{"status": r[0], "failure_type": r[1]} for r in c.execute(
        "SELECT status, failure_type FROM memecoin_exit_execution_attempts ORDER BY id")]


def _record(c, **kw):
    base = dict(position_id=1, network_id="4663", exit_reason="real_tp",
                attempted_at="2026-09-02T19:38:29", status="stuck")
    base.update(kw)
    m.record_exit_execution(c, **base)


def test_the_column_exists():
    c = _conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_exit_execution_attempts)")}
    assert "failure_type" in cols


def test_the_two_stuck_causes_are_distinguishable():
    """The whole point: an empty balance and an exhausted ladder both write `stuck`."""
    c = _conn()
    _record(c, failure_type="token_balance_zero")
    _record(c, failure_type="route_capacity")
    got = _rows(c)
    assert [r["status"] for r in got] == ["stuck", "stuck"]
    assert [r["failure_type"] for r in got] == ["token_balance_zero", "route_capacity"]


def test_no_cause_stays_NULL_rather_than_becoming_a_string():
    """An absent cause must be absent. 'None' as text would read as a real classification."""
    c = _conn()
    _record(c)
    assert _rows(c)[0]["failure_type"] is None


def test_an_empty_cause_is_also_NULL():
    c = _conn()
    _record(c, failure_type="")
    assert _rows(c)[0]["failure_type"] is None


def test_a_database_predating_the_column_gains_it_idempotently():
    """Migration on a live ledger: the column is added once and existing rows survive."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_exit_execution_attempts ("
              "id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER NOT NULL, "
              "network_id TEXT NOT NULL, exit_reason TEXT NOT NULL, attempted_at TEXT NOT NULL, "
              "status TEXT NOT NULL, sig TEXT, total_ms REAL, "
              "priority_fee_microlamports INTEGER, fee_paid_lamports INTEGER, "
              "depth_first_rung_pct REAL, tier_variant TEXT NOT NULL)")
    c.execute("INSERT INTO memecoin_exit_execution_attempts"
              "(position_id,network_id,exit_reason,attempted_at,status,tier_variant) "
              "VALUES(48595,'4663','real_tp','2026-09-02T19:38:29','stuck','baseline')")
    c.commit()                                        # ensure_memecoin_tables refuses an open txn
    m.ensure_memecoin_tables(c)
    m.ensure_memecoin_tables(c)                       # twice: the migration must be idempotent
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_exit_execution_attempts)")}
    assert "failure_type" in cols
    old = c.execute("SELECT position_id, failure_type FROM memecoin_exit_execution_attempts").fetchone()
    assert old[0] == 48595 and old[1] is None, "the pre-existing row must survive with a NULL cause"


def test_every_call_site_passes_the_cause():
    """Four sites record exit attempts, two per chain. A cause threaded through three of them is
    a cause missing from a quarter of the tape."""
    import pathlib
    for path in ("sniper/execution/evm_real_seams.py", "sniper/execution/solana_real_seams.py"):
        src = pathlib.Path(path).read_text()
        assert src.count("m.record_exit_execution(") == src.count("failure_type=(result.get("), (
            f"{path}: a record_exit_execution call site does not pass the cause")


def test_the_cause_reads_BOTH_field_names():
    """`reason` carries the balance/route refusal and `failure_type` the exhausted ladder. Reading
    only one drops half the causes silently."""
    import pathlib
    for path in ("sniper/execution/evm_real_seams.py", "sniper/execution/solana_real_seams.py"):
        src = pathlib.Path(path).read_text()
        sites = src.count("m.record_exit_execution(")
        both = src.count('result.get("reason") or result.get("failure_type")')
        # Count, do not merely look for the substring: with two call sites per file, an `in` check
        # passes while one of them silently reads a single field name.
        assert both == sites, (
            f"{path}: {both} of {sites} call sites read both cause fields")


def test_the_exit_stage_map_is_persisted_not_only_logged():
    """WHERE the exit time went must reach the row, not only a log line.

    The map was computed for a `logger.warning` and the durable `record_exit_execution` one line
    below kept `total_ms` alone. Ten of those lines survived across every rotation against 469
    exit attempts on 4663 in 14 days, so exit latency was unattributable by construction. Same
    defect class as a `stuck` row with no cause: the value exists at the boundary and the caller
    keeps the summary.
    """
    import pathlib
    src = pathlib.Path("sniper/execution/evm_real_seams.py").read_text()
    # The main exit caller has the map in scope; derisk_fn does not, so exactly one site wires it.
    assert src.count("stage_ms_json=_stage_map_json(") == 1, (
        "the exit caller that owns _stage_ms must persist it")
    assert "_stage_map_json" in src and "json.dumps(flat, sort_keys=True)" in src


def test_an_unreached_stage_map_stores_null_not_an_empty_object():
    """`{}` would read as "measured, and every stage cost zero". An exit that reached no stage was
    not measured at all, and the two must not share a value."""
    import json
    import sqlite3

    from sniper.persistence import db_memecoin as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_exit_execution_attempts)")}
    assert "stage_ms_json" in cols, "the migration must add the column to an existing table"

    m.record_exit_execution(conn, position_id=1, network_id="4663", exit_reason="b_stop",
                            attempted_at="2026-09-03T00:00:00+00:00", status="stuck", total_ms=1.0)
    assert conn.execute("SELECT stage_ms_json FROM memecoin_exit_execution_attempts "
                        "WHERE position_id=1").fetchone()[0] is None

    payload = json.dumps({"quote_ms": 2258.3, "quote_n": 3}, sort_keys=True)
    m.record_exit_execution(conn, position_id=2, network_id="4663", exit_reason="b_stop",
                            attempted_at="2026-09-03T00:00:01+00:00", status="closed",
                            total_ms=6783.0, stage_ms_json=payload)
    stored = conn.execute("SELECT stage_ms_json FROM memecoin_exit_execution_attempts "
                          "WHERE position_id=2").fetchone()[0]
    # The retry COUNT must survive with the duration: a stage entered 3 times is a retry story
    # that a mean over durations alone hides.
    assert json.loads(stored)["quote_n"] == 3
