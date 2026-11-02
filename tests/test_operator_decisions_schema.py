"""W6.1 — operator_decisions schema acceptance tests.

The I3 operator-review SOP records human decisions as an audit overlay,
not as analyzer-owned verdict mutations. Schema invariants:

* Composite primary key (wallet, analyzer_run_id) — one decision per
  (wallet, run).
* CHECK constraint on ``decision`` ∈ {accept, reject, defer}.
* Mandatory: wallet, analyzer_run_id, decided_at_ts, decision.
* Defaults: reasoning='', operator='unknown'.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §I3.
Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W6.1.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.persistence.db_operator_decisions import ensure_operator_decisions


def _new_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_ensure_operator_decisions_creates_table():
    conn = _new_db()
    ensure_operator_decisions(conn)
    rows = list(conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='operator_decisions'"
    ))
    assert len(rows) == 1


def test_ensure_operator_decisions_is_idempotent():
    conn = _new_db()
    ensure_operator_decisions(conn)
    ensure_operator_decisions(conn)  # must not raise
    ensure_operator_decisions(conn)
    rows = list(conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='operator_decisions'"
    ))
    assert len(rows) == 1


def test_decision_check_constraint_allows_valid_values():
    conn = _new_db()
    ensure_operator_decisions(conn)
    for decision in ("accept", "reject", "defer"):
        conn.execute(
            "INSERT INTO operator_decisions "
            "(wallet, analyzer_run_id, decided_at_ts, decision) "
            "VALUES (?, ?, ?, ?)",
            (f"0x{decision}", f"run-{decision}", 1700000000, decision),
        )
    rows = list(conn.execute("SELECT decision FROM operator_decisions"))
    assert {r[0] for r in rows} == {"accept", "reject", "defer"}


def test_decision_check_constraint_rejects_invalid():
    conn = _new_db()
    ensure_operator_decisions(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operator_decisions "
            "(wallet, analyzer_run_id, decided_at_ts, decision) "
            "VALUES (?, ?, ?, ?)",
            ("0xbad", "run-1", 1700000000, "approved"),  # not in {accept,reject,defer}
        )


def test_primary_key_is_wallet_run():
    conn = _new_db()
    ensure_operator_decisions(conn)
    conn.execute(
        "INSERT INTO operator_decisions "
        "(wallet, analyzer_run_id, decided_at_ts, decision) "
        "VALUES (?, ?, ?, ?)",
        ("0xabc", "run-1", 1700000000, "accept"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operator_decisions "
            "(wallet, analyzer_run_id, decided_at_ts, decision) "
            "VALUES (?, ?, ?, ?)",
            ("0xabc", "run-1", 1700000001, "reject"),  # same composite key
        )

    # Different wallet, same run_id — allowed.
    conn.execute(
        "INSERT INTO operator_decisions "
        "(wallet, analyzer_run_id, decided_at_ts, decision) "
        "VALUES (?, ?, ?, ?)",
        ("0xdef", "run-1", 1700000002, "defer"),
    )
    # Same wallet, different run_id — allowed.
    conn.execute(
        "INSERT INTO operator_decisions "
        "(wallet, analyzer_run_id, decided_at_ts, decision) "
        "VALUES (?, ?, ?, ?)",
        ("0xabc", "run-2", 1700000003, "reject"),
    )


def test_required_columns_not_null():
    conn = _new_db()
    ensure_operator_decisions(conn)
    # Missing wallet → NOT NULL violation
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operator_decisions "
            "(analyzer_run_id, decided_at_ts, decision) VALUES (?, ?, ?)",
            ("run-1", 1700000000, "accept"),
        )
    # Missing decision → NOT NULL violation
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operator_decisions "
            "(wallet, analyzer_run_id, decided_at_ts) VALUES (?, ?, ?)",
            ("0xabc", "run-1", 1700000000),
        )


def test_optional_columns_have_defaults():
    conn = _new_db()
    ensure_operator_decisions(conn)
    conn.execute(
        "INSERT INTO operator_decisions "
        "(wallet, analyzer_run_id, decided_at_ts, decision) "
        "VALUES (?, ?, ?, ?)",
        ("0xabc", "run-1", 1700000000, "accept"),
    )
    row = conn.execute(
        "SELECT reasoning, operator FROM operator_decisions"
    ).fetchone()
    assert row[0] == ""           # reasoning default
    assert row[1] == "unknown"    # operator default


def test_init_db_wires_operator_decisions():
    """db.init_db must ensure_operator_decisions on a fresh DB."""
    import db
    conn = sqlite3.connect(":memory:")
    db.init_db(conn)
    rows = list(conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='operator_decisions'"
    ))
    assert len(rows) == 1
