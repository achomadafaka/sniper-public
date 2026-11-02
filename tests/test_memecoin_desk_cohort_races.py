"""Bounded, real-SQLite regressions for immutable cohort and expense races."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier

import pytest

from sniper.persistence import db_memecoin as m

NOW = "2026-10-21T12:00:00+00:00"
POLICY = {"clip_usd": 25, "max_open": 5}


class StaleReadConnection(sqlite3.Connection):
    """Hold completed reads together only when the helper has not acquired its writer transaction."""
    def execute(self, sql, parameters=()):
        in_transaction = self.in_transaction
        cursor = super().execute(sql, parameters)
        if not in_transaction and sql.startswith(self.pause_sql):
            self.stale_reads.wait(timeout=5)
        return cursor


@pytest.fixture
def path(tmp_path):
    target = tmp_path / "ledger.sqlite"
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    return target


def register(conn, *, cohort_id=None, policy=POLICY, commit=True):
    return m.register_desk_cohort(conn, producer="H", policy=policy, prompt_version="desk-judge-v6",
                                  created_at=NOW, cohort_id=cohort_id, commit=commit)


def provision(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return register(conn)


def cost(conn, cohort, usd, *, commit=True):
    return m.record_desk_cohort_cost(conn, cohort_id=cohort, event_id="one-expense", usd=usd,
                                    kind="model", occurred_at=NOW,
                                    provenance="bill:pending" if usd is None else "bill:exact",
                                    commit=commit)


def race(path, values, callback, *, pause_sql, isolation_level):
    start, stale_reads = Barrier(2), Barrier(2)

    def run(value):
        conn = sqlite3.connect(path, timeout=5, factory=StaleReadConnection, isolation_level=isolation_level)
        conn.row_factory = sqlite3.Row
        conn.pause_sql, conn.stale_reads = pause_sql, stale_reads
        try:
            start.wait(timeout=5)
            return {"value": value, "result": callback(conn, value), "error": None}
        except Exception as error:
            return {"value": value, "result": None, "error": error}
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(run, value) for value in values]
        return [future.result(timeout=8) for future in futures]


@pytest.mark.parametrize("isolation_level", ["", None])
def test_concurrent_policy_alias_registration_returns_only_the_stored_winner(path, isolation_level):
    outcomes = race(path, ["alias-a", "alias-b"], lambda conn, alias: register(conn, cohort_id=alias),
                    pause_sql="SELECT cohort_id FROM memecoin_desk_cohorts", isolation_level=isolation_level)
    success = [row for row in outcomes if row["error"] is None]
    failure = [row for row in outcomes if row["error"] is not None]
    assert len(success) == len(failure) == 1, outcomes
    assert isinstance(failure[0]["error"], ValueError)
    assert "alias" in str(failure[0]["error"])
    with sqlite3.connect(path) as conn:
        rows = conn.execute("SELECT cohort_id FROM memecoin_desk_cohorts").fetchall()
    assert rows == [(success[0]["result"],)]


@pytest.mark.parametrize("isolation_level", ["", None])
def test_concurrent_same_id_cannot_register_two_different_policies(path, isolation_level):
    outcomes = race(path, [25, 26],
                    lambda conn, clip: register(conn, cohort_id="one-cohort", policy={"clip_usd": clip}),
                    pause_sql="SELECT cohort_id FROM memecoin_desk_cohorts", isolation_level=isolation_level)
    success = [row for row in outcomes if row["error"] is None]
    failure = [row for row in outcomes if row["error"] is not None]
    assert len(success) == len(failure) == 1, outcomes
    assert isinstance(failure[0]["error"], ValueError)
    assert "different policy" in str(failure[0]["error"])
    with sqlite3.connect(path) as conn:
        stored = conn.execute("SELECT policy_json FROM memecoin_desk_cohorts").fetchone()[0]
    assert json.loads(stored)["policy"] == {"clip_usd": success[0]["value"]}


@pytest.mark.parametrize("isolation_level", ["", None])
@pytest.mark.parametrize("seed_unknown", [False, True])
def test_concurrent_identical_expenses_are_idempotent(path, isolation_level, seed_unknown):
    cohort = provision(path)
    if seed_unknown:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            cost(conn, cohort, None)
    outcomes = race(path, [12.5, 12.5], lambda conn, usd: cost(conn, cohort, usd),
                    pause_sql="SELECT kind,usd,occurred_at,provenance,position_id FROM memecoin_desk_cohort_costs",
                    isolation_level=isolation_level)
    assert all(row["error"] is None for row in outcomes), outcomes
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs").fetchone()[0] == 1
        assert m.desk_cohort_accounting(conn, cohort)["net_usd"] == -12.5


@pytest.mark.parametrize("isolation_level", ["", None])
def test_concurrent_unknown_expense_resolutions_cannot_accept_conflicting_values(path, isolation_level):
    cohort = provision(path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        cost(conn, cohort, None)
    outcomes = race(path, [1.0, 200.0], lambda conn, usd: cost(conn, cohort, usd),
                    pause_sql="SELECT kind,usd,occurred_at,provenance,position_id FROM memecoin_desk_cohort_costs",
                    isolation_level=isolation_level)
    success = [row for row in outcomes if row["error"] is None]
    failure = [row for row in outcomes if row["error"] is not None]
    assert len(success) == len(failure) == 1, outcomes
    assert isinstance(failure[0]["error"], ValueError)
    assert "immutable" in str(failure[0]["error"])
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        assert m.desk_cohort_accounting(conn, cohort)["known_cost_usd"] == success[0]["value"]
        assert m.desk_cohort_killed(conn, cohort) is (success[0]["value"] == 200.0)


@pytest.mark.parametrize("isolation_level", ["", None])
def test_expense_and_trip_roll_back_together_when_latching_fails(path, isolation_level):
    cohort = provision(path)
    conn = sqlite3.connect(path, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TRIGGER fail_trip BEFORE INSERT ON memecoin_desk_cohort_trips "
                 "BEGIN SELECT RAISE(ABORT,'injected_trip_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected_trip_failure"):
        cost(conn, cohort, 200)
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs").fetchone()[0] == 0
    assert not m.desk_cohort_killed(conn, cohort)
    conn.close()


def test_autocommit_commit_false_requires_an_explicit_caller_transaction(path):
    cohort = provision(path)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    with pytest.raises(ValueError, match="transaction"):
        register(conn, cohort_id="new-cohort", policy={"clip_usd": 26}, commit=False)
    with pytest.raises(ValueError, match="transaction"):
        cost(conn, cohort, 200, commit=False)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs").fetchone()[0] == 0
    conn.close()


@pytest.mark.parametrize("isolation_level", ["", None])
def test_commit_false_keeps_cohort_cost_and_latch_in_the_callers_transaction(path, isolation_level):
    conn = sqlite3.connect(path, isolation_level=isolation_level)
    conn.row_factory = sqlite3.Row
    if isolation_level is None:
        conn.execute("BEGIN IMMEDIATE")
    cohort = register(conn, commit=False)
    cost(conn, cohort, 200, commit=False)
    assert conn.in_transaction is True and m.desk_cohort_killed(conn, cohort)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_costs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_desk_cohort_trips").fetchone()[0] == 0
    conn.close()
