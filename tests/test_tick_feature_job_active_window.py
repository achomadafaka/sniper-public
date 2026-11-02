"""The active-pool window must keep its row set AND seek an index.

`tick_feature_job` picked the busiest pools with `datetime(observed_at) > datetime(?, '-N seconds')`.
The datetime() call on the COLUMN made the predicate non-sargable, so idx_pool_ticks_chain_time
went unused and every run scanned the whole chain: 30.2s on the live 15GB tape (2026-08-12), which
is why one invocation could not finish inside its 5-minute cron interval.

The rewrite compares the bare column against a precomputed bound. datetime(x) > datetime(t) is a
SECOND-granularity compare, so floor(x) > floor(t) <=> x >= floor(t) + 1s. These tests pin that
equality (including the sub-second boundary rows, where a naive bare-string compare differs) and
pin that the plan seeks rather than scans.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from sniper.persistence.db_ticks import ensure_tick_tables

WINDOW_S = 300.0


def _bound_iso(now_iso, window_s=WINDOW_S):
    """The job's bound, kept in lockstep with scripts/tick_feature_job.py."""
    cutoff = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")) - timedelta(
        seconds=int(window_s * 2))
    return (cutoff.replace(microsecond=0)
            + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"


OLD = ("SELECT pool, COUNT(*) AS n FROM pool_ticks WHERE chain_id=? "
       "AND kind IN ('v2_swap','v3_swap') "
       "AND datetime(observed_at) > datetime(?, '-' || ? || ' seconds') "
       "GROUP BY pool ORDER BY n DESC")
NEW = ("SELECT pool, COUNT(*) AS n FROM pool_ticks INDEXED BY idx_pool_ticks_chain_time "
       "WHERE chain_id=? AND kind IN ('v2_swap','v3_swap') AND observed_at >= ? "
       "GROUP BY pool ORDER BY n DESC")


@pytest.fixture
def tc(tmp_path):
    c = sqlite3.connect(tmp_path / "ticks.db")
    c.row_factory = sqlite3.Row
    ensure_tick_tables(c)
    return c


def _add(c, pool, observed_at, kind="v3_swap", chain="4663"):
    c.execute("INSERT INTO pool_ticks (chain_id, pool, kind, observed_at, block_no) "
              "VALUES (?,?,?,?,1)", (chain, pool, kind, observed_at))


def test_rewrite_matches_the_datetime_form_across_the_boundary_second(tc):
    """The cutoff second itself is the case a naive bare-string compare gets wrong."""
    now = "2026-08-12T14:10:00.500000+00:00"          # window opens at 14:00:00.500000
    for us in ("000000", "400000", "499999", "500000", "500001", "900000"):
        _add(tc, "0xboundary", f"2026-08-12T14:00:00.{us}+00:00")
    _add(tc, "0xbefore", "2026-08-12T13:59:59.999999+00:00")
    _add(tc, "0xafter", "2026-08-12T14:00:01.000000+00:00")
    tc.commit()
    old = tc.execute(OLD, ("4663", now, int(WINDOW_S * 2))).fetchall()
    new = tc.execute(NEW, ("4663", _bound_iso(now))).fetchall()
    assert [(r["pool"], r["n"]) for r in old] == [(r["pool"], r["n"]) for r in new]
    # and the boundary second is excluded by BOTH, which is the 2026-07-23 semantics
    assert "0xboundary" not in {r["pool"] for r in new}
    assert {r["pool"] for r in new} == {"0xafter"}


def test_rewrite_matches_when_the_cutoff_lands_on_an_exact_second(tc):
    now = "2026-08-12T14:10:00+00:00"
    for iso in ("2026-08-12T13:59:59.999999+00:00", "2026-08-12T14:00:00.000001+00:00",
                "2026-08-12T14:00:00.999999+00:00", "2026-08-12T14:00:01.000000+00:00",
                "2026-08-12T14:05:00.123456+00:00"):
        _add(tc, "0xp", iso)
    tc.commit()
    old = tc.execute(OLD, ("4663", now, int(WINDOW_S * 2))).fetchone()
    new = tc.execute(NEW, ("4663", _bound_iso(now))).fetchone()
    assert old["n"] == new["n"] == 2      # 14:00:01 and 14:05:00 only


def test_other_kinds_and_chains_stay_excluded(tc):
    now = "2026-08-12T14:10:00+00:00"
    _add(tc, "0xp", "2026-08-12T14:05:00.000000+00:00", kind="v2_sync")
    _add(tc, "0xp", "2026-08-12T14:05:00.000000+00:00", kind="v3_burn")
    _add(tc, "0xp", "2026-08-12T14:05:00.000000+00:00", chain="8453")
    _add(tc, "0xp", "2026-08-12T14:05:00.000000+00:00", kind="v2_swap")
    tc.commit()
    assert tc.execute(NEW, ("4663", _bound_iso(now))).fetchone()["n"] == 1


def test_rewrite_seeks_on_time_while_the_old_form_cannot(tc):
    """The time bound must become a SEEK BOUND. Only a bare column can do that.

    The INDEXED BY pin makes this deterministic at any table size. Without it the planner prefers
    a covering scan of idx_pool_ticks_pool_kind_time, which reads every row of the chain: 1,502ms
    against 29.9ms on the live tape, and ANALYZE did not change the choice.
    """
    new_plan = " ".join(r[-1] for r in tc.execute(
        "EXPLAIN QUERY PLAN " + NEW, ("4663", "2026-08-12T14:00:01+00:00")))
    assert "observed_at>" in new_plan, "the rewrite must seek on time: " + new_plan
    assert "SCAN" not in new_plan.upper(), new_plan

    old_plan = " ".join(r[-1] for r in tc.execute(
        "EXPLAIN QUERY PLAN " + OLD, ("4663", "2026-08-12T14:10:00+00:00", 600)))
    assert "observed_at>" not in old_plan, "the old form must not seek on time: " + old_plan


def test_the_pinned_index_exists_so_the_pin_cannot_break(tc):
    names = {r[0] for r in tc.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pool_ticks'")}
    assert "idx_pool_ticks_chain_time" in names


def test_per_pool_time_index_exists(tc):
    """compute_pool_windows pins a pool AND a time range, and reads kind='v2_sync' newest-first.

    Without kind before observed_at, a V3 pool (no v2_sync row at all) walked its whole history
    to find nothing: 777ms per call on the live tape, 400 calls per run.
    """
    names = {r[0] for r in tc.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pool_ticks'")}
    assert "idx_pool_ticks_pool_kind_time" in names

    for q, args in (
        ("SELECT maker FROM pool_ticks WHERE chain_id=? AND pool=? "
         "AND kind IN ('v2_swap','v3_swap') AND observed_at > ? AND observed_at <= ?",
         ("4663", "0xp", "a", "b")),
        ("SELECT amount0 FROM pool_ticks WHERE chain_id=? AND pool=? AND kind='v2_sync' "
         "AND observed_at <= ? ORDER BY observed_at DESC LIMIT 1", ("4663", "0xp", "b")),
    ):
        plan = " ".join(r[-1] for r in tc.execute("EXPLAIN QUERY PLAN " + q, args))
        # pool AND kind AND the time bound must all be seek terms — that is what turns the
        # reserve read on a v2_sync-free V3 pool from a full-history walk into an empty seek.
        assert "pool=?" in plan and "kind=?" in plan, plan
        assert "observed_at" in plan, plan
        assert "SCAN" not in plan.upper(), plan
