"""Funded positions: the executable top and bottom (slices A and B of
docs/superpowers/specs/2026-09-30-memecoin-funded-executable-extremes-design.md).

The high-cadence executable series already exists: `memecoin_route_series`, seam `exit_decision`,
one whole-bag sell quote per second per open funded position. Nothing recorded its top or bottom.
`memecoin_position_exec_extremes` now does, and `max_gain_pct` / `time_to_peak_s` (a display
measure) stay NULL on funded rows.

Rule 5 (CLAUDE.md):
1. Money path: no. The exit loop, the rails, `executable_peak_price` and the close statements are
   untouched. The pass writes one new table and sets two research columns to NULL on CLOSED funded
   rows.
2. Fill-path speed: the pass runs on its own thread with its own connection, never on the main
   loop, the hot-exit worker or an entry lane. Measured on the live ledger copy: see the spec.
3. Concurrency: one writer thread in the runner plus the operator's history script; both use the
   same idempotent upsert. One short write transaction per pass.
4. Absent vs zero: no positive-price sample means NO row, never zeros. A funded row's
   `max_gain_pct` is NULL, never 0.
5. Dependencies: a lost flush or a runner crash loses samples; `samples`, `first_at` and `last_at`
   show how much of the hold the series saw. A failed pass rolls back and runs again.
6. Units: `unit_price` is USD per token of the whole-bag sell quote (net of our impact and fees);
   `entry_price` is USD per token paid. Only seam `exit_decision` counts.
7. Sample: every closed funded position with at least one positive-price exit-decision quote.
8. Rollback: revert. The table can stay; nothing else reads it.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

NOW = "2026-07-09T12:00:00+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, mint, *, mode="real", price=1.0, opened="2026-07-09T11:00:00+00:00"):
    return m.open_position(c, source_user_id="u-" + mint, source_handle="h", mint=mint, ticker="T",
                           entry_price=price, entry_notional=10.0, entry_fill_source="chain",
                           entry_lag_s=1.0, their_size_usd=0.0, opened_at=opened,
                           opened_recorded_at=opened, strategy="alpha_b", execution_mode=mode,
                           network_id="solana")


def _close(c, pid, closed_at, *, max_gain=33.0, ttp=5.0):
    """A closed row that still carries a DISPLAY value, as a write-off leaves it."""
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, closed_recorded_at=?, "
              "realized_pnl=0, max_gain_pct=?, time_to_peak_s=? WHERE id=?",
              (closed_at, closed_at, max_gain, ttp, pid))
    c.commit()


def _series(c, pid, points, *, seam="exit_decision"):
    m.record_route_series(c, [
        {"position_id": pid, "mint": "x", "network_id": "solana", "seam": seam, "observed_at": at,
         "quoted_at": at, "expected_usdc": (price or 0.0) * 10.0, "open_qty": 10.0,
         "unit_price": price} for at, price in points])


def _counts(out):
    return {k: out[k] for k in ("visited", "written", "cleared")}


def _row(c, pid):
    r = c.execute("SELECT * FROM memecoin_position_exec_extremes WHERE position_id=?", (pid,)).fetchone()
    return dict(r) if r else None


def _display(c, pid):
    return tuple(c.execute("SELECT max_gain_pct, time_to_peak_s FROM memecoin_positions WHERE id=?",
                           (pid,)).fetchone())


SERIES = [("2026-07-09T11:00:01+00:00", 0.97), ("2026-07-09T11:00:02+00:00", 1.40),
          ("2026-07-09T11:00:03+00:00", 1.40), ("2026-07-09T11:00:04+00:00", 0.60),
          ("2026-07-09T11:00:05+00:00", 0.60), ("2026-07-09T11:00:06+00:00", 0.90)]


# ------------------------------------------------------------------ the aggregate

def test_the_high_and_low_carry_their_first_timestamps_and_the_sample_count(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    assert m.exec_extremes_live(conn, pid) == {
        "position_id": pid, "high_price": 1.40, "high_at": "2026-07-09T11:00:02+00:00",
        "low_price": 0.60, "low_at": "2026-07-09T11:00:04+00:00", "samples": 6,
        "first_at": "2026-07-09T11:00:01+00:00", "last_at": "2026-07-09T11:00:06+00:00",
        "basis": "exact_route:exit_decision"}


def test_other_seams_and_non_positive_prices_are_not_samples(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    _series(conn, pid, [("2026-07-09T11:00:07+00:00", 9.0)], seam="exit_audit")
    _series(conn, pid, [("2026-07-09T11:00:08+00:00", 9.0)], seam="peak_preview")
    _series(conn, pid, [("2026-07-09T11:00:09+00:00", 0.0), ("2026-07-09T11:00:10+00:00", -1.0),
                        ("2026-07-09T11:00:11+00:00", None)])
    live = m.exec_extremes_live(conn, pid)
    assert (live["high_price"], live["low_price"], live["samples"]) == (1.40, 0.60, 6)
    assert live["last_at"] == "2026-07-09T11:00:06+00:00"


def test_no_positive_sample_is_no_row_never_zeros(conn):
    pid = _open(conn, "a")
    assert m.exec_extremes_live(conn, pid) is None
    _series(conn, pid, [("2026-07-09T11:00:09+00:00", 0.0)])
    _series(conn, pid, [("2026-07-09T11:00:07+00:00", 9.0)], seam="exit_audit")
    assert m.exec_extremes_live(conn, pid) is None
    _close(conn, pid, "2026-07-09T11:59:00+00:00")
    assert m.exec_extremes_pass(conn, NOW)["written"] == 0 and _row(conn, pid) is None


def test_percentages_are_derived_at_read_time_and_a_best_gain_can_be_negative(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    pct = m.exec_extremes_pct(m.exec_extremes_live(conn, pid), 1.0,
                              opened_at="2026-07-09T11:00:00+00:00")
    assert pct["high_pct"] == pytest.approx(40.0) and pct["low_pct"] == pytest.approx(-40.0)
    assert pct["time_to_high_s"] == pytest.approx(2.0) and pct["time_to_low_s"] == pytest.approx(4.0)
    # the whole-bag sell quote is net of our own exit impact: the best gain of a hold can be < 0
    assert m.exec_extremes_pct({"high_price": 0.963, "low_price": 0.5}, 1.0)["high_pct"] == pytest.approx(-3.7)
    blank = {"high_pct": None, "low_pct": None, "time_to_high_s": None, "time_to_low_s": None}
    assert m.exec_extremes_pct(None, 1.0) == blank
    assert m.exec_extremes_pct({"high_price": 2.0, "low_price": 1.0}, 0.0) == blank
    assert m.exec_extremes_pct({"high_price": 2.0, "low_price": 1.0}, None) == blank


# ------------------------------------------------------------------ the pass

def test_a_closed_funded_position_gets_its_row_and_loses_the_display_columns(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    _close(conn, pid, "2026-07-09T11:59:00+00:00")
    assert _display(conn, pid) == (33.0, 5.0)
    assert _counts(m.exec_extremes_pass(conn, NOW)) == {"visited": 1, "written": 1, "cleared": 1}
    row = _row(conn, pid)
    assert (row["high_price"], row["low_price"], row["samples"], row["computed_at"]) == (1.40, 0.60, 6, NOW)
    assert _display(conn, pid) == (None, None)
    assert not conn.in_transaction                        # the pass commits its one transaction


def test_a_funded_close_with_no_series_is_still_cleared(conn):
    """Gamma LOW on c7481d0c3: 6 of 297 funded closes in 7 days had no series, and a written-off
    one would otherwise keep its display value forever."""
    pid = _open(conn, "a")
    _close(conn, pid, "2026-07-09T11:59:00+00:00")
    assert _counts(m.exec_extremes_pass(conn, NOW)) == {"visited": 1, "written": 0, "cleared": 1}
    assert _row(conn, pid) is None and _display(conn, pid) == (None, None)
    assert _counts(m.exec_extremes_pass(conn, NOW)) == {"visited": 1, "written": 0, "cleared": 0}


def test_a_late_flush_inside_ten_minutes_updates_the_row(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES[:2])
    _close(conn, pid, "2026-07-09T11:51:00+00:00")           # 9 minutes before NOW
    m.exec_extremes_pass(conn, NOW)
    assert _row(conn, pid)["samples"] == 2
    _series(conn, pid, SERIES[2:])                           # the flush lands late
    m.exec_extremes_pass(conn, NOW)
    assert (_row(conn, pid)["samples"], _row(conn, pid)["low_price"]) == (6, 0.60)


def test_a_row_older_than_ten_minutes_is_not_recomputed_but_a_missing_one_within_a_day_is(conn):
    old = _open(conn, "a")
    _series(conn, old, SERIES[:2])
    _close(conn, old, "2026-07-09T11:49:00+00:00")           # 11 minutes before NOW, same day
    m.upsert_exec_extremes(conn, old, "2026-07-09T11:49:30+00:00")
    conn.commit()
    _series(conn, old, SERIES[2:])
    missing = _open(conn, "b")
    _series(conn, missing, SERIES)
    _close(conn, missing, "2026-07-08T12:30:00+00:00")       # 23.5 hours before NOW: a restart gap
    assert m.exec_extremes_pass(conn, NOW)["written"] == 1
    assert _row(conn, old)["samples"] == 2 and _row(conn, missing)["samples"] == 6
    assert _display(conn, old) == (33.0, 5.0)                # not visited, so not touched


def test_a_position_closed_more_than_a_day_ago_is_left_to_the_operator_script(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    _close(conn, pid, "2026-07-08T11:00:00+00:00")           # 25 hours before NOW
    assert _counts(m.exec_extremes_pass(conn, NOW)) == {"visited": 0, "written": 0, "cleared": 0}
    assert _row(conn, pid) is None and _display(conn, pid) == (33.0, 5.0)


def test_the_pass_takes_at_most_twenty_the_ten_minute_window_first_newest_first(conn):
    recent, older = [], []
    for i in range(15):                                      # closed 1..8 minutes ago
        pid = _open(conn, "r%02d" % i)
        _series(conn, pid, SERIES)
        _close(conn, pid, "2026-07-09T11:5%d:%02d+00:00" % (2 + i // 2, 30 * (i % 2)))
        recent.append(pid)
    for i in range(15):                                      # closed 1..15 hours ago, no row yet
        pid = _open(conn, "o%02d" % i)
        _series(conn, pid, SERIES)
        _close(conn, pid, "2026-07-09T%02d:00:00+00:00" % (10 - i if i < 11 else 0))
        older.append(pid)
    assert m.exec_extremes_pass(conn, NOW)["visited"] == 20
    have = {r[0] for r in conn.execute("SELECT position_id FROM memecoin_position_exec_extremes")}
    assert set(recent) <= have                               # all 15 of the ten-minute window
    assert have - set(recent) == set(older[:5])              # then the 5 newest of the older ones
    assert m.exec_extremes_pass(conn, NOW, limit=3)["visited"] == 3
    assert m.exec_extremes_pass(conn, NOW)["visited"] == 20  # 15 recomputed + 5 more missing rows


def test_under_the_cap_the_newest_closes_of_the_ten_minute_window_come_first(conn):
    pids = []
    for minute in (52, 54, 56, 58, 59):
        pid = _open(conn, "m%d" % minute)
        _series(conn, pid, SERIES)
        _close(conn, pid, "2026-07-09T11:%d:00+00:00" % minute)
        pids.append(pid)
    assert m.exec_extremes_pass(conn, NOW, limit=3)["visited"] == 3
    have = {r[0] for r in conn.execute("SELECT position_id FROM memecoin_position_exec_extremes")}
    assert have == set(pids[-3:])


def test_the_clear_never_matches_a_paper_or_an_open_row(conn):
    paper = _open(conn, "p", mode="paper")
    _close(conn, paper, "2026-07-09T11:59:00+00:00")
    still_open = _open(conn, "o")
    conn.execute("UPDATE memecoin_positions SET max_gain_pct=7, time_to_peak_s=1 WHERE id=?", (still_open,))
    assert m.clear_funded_display_extremes(conn, paper, NOW) is False
    assert m.clear_funded_display_extremes(conn, still_open, NOW) is False
    assert _display(conn, paper) == (33.0, 5.0) and _display(conn, still_open) == (7.0, 1.0)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_position_display_extremes_backup").fetchone()[0] == 0


def test_paper_and_open_positions_are_never_touched(conn):
    paper = _open(conn, "p", mode="paper")
    _series(conn, paper, SERIES)
    _close(conn, paper, "2026-07-09T11:59:00+00:00")
    still_open = _open(conn, "o")
    _series(conn, still_open, SERIES)
    assert _counts(m.exec_extremes_pass(conn, NOW)) == {"visited": 0, "written": 0, "cleared": 0}
    assert _row(conn, paper) is None and _row(conn, still_open) is None
    assert _display(conn, paper) == (33.0, 5.0)


def test_the_real_close_statements_are_untouched_and_the_pass_owns_the_columns(conn):
    """Gamma (c): finalize_close and write_off_real_position are not edited. Both still close a
    funded row and still stamp the display value; the pass sets it back to NULL."""
    finalized = _open(conn, "f")
    conn.execute("UPDATE memecoin_positions SET peak_price=1.5, peak_ts='2026-07-09T11:00:10+00:00' "
                 "WHERE id=?", (finalized,))
    assert m.claim_close(conn, finalized)
    m.finalize_close(conn, finalized, "2026-07-09T11:59:00+00:00", 1.0, exit_reason="recovered_orphan_close")
    conn.commit()
    written_off = _open(conn, "w")
    conn.execute("UPDATE memecoin_positions SET peak_price=2.0, peak_ts='2026-07-09T11:00:20+00:00' "
                 "WHERE id=?", (written_off,))
    conn.commit()
    done = m.write_off_real_position(conn, written_off, actor="op", reason="stuck", balance_text="0",
                                     at="2026-07-09T11:59:30+00:00")
    assert done["status"] == "written_off"
    assert _display(conn, finalized) == (pytest.approx(50.0), pytest.approx(10.0, abs=0.01))
    assert _display(conn, written_off) == (pytest.approx(100.0), pytest.approx(20.0, abs=0.01))
    assert m.exec_extremes_pass(conn, NOW)["cleared"] == 2
    assert _display(conn, finalized) == (None, None) and _display(conn, written_off) == (None, None)
    closed = conn.execute("SELECT COUNT(*) FROM memecoin_positions WHERE is_open=0 AND closed_at IS NOT NULL "
                          "AND realized_pnl IS NOT NULL").fetchone()[0]
    assert closed == 2


def test_the_batch_reader_returns_rows_by_position(conn):
    a, b, c = _open(conn, "a"), _open(conn, "b"), _open(conn, "c")
    _series(conn, a, SERIES)
    _series(conn, b, SERIES[:1])
    for pid in (a, b, c):
        _close(conn, pid, "2026-07-09T11:59:00+00:00")
    m.exec_extremes_pass(conn, NOW)
    got = m.funded_exec_extremes(conn, [a, b, c, 999999])
    assert set(got) == {a, b} and got[a]["samples"] == 6 and got[b]["high_price"] == 0.97
    assert m.funded_exec_extremes(conn, []) == {}


# ------------------------------------------------------------------ where the pass runs

def test_the_pass_runs_only_on_its_own_thread_with_its_own_connection():
    """Rule 5, speed on the fill path: the pass is never on the main loop (whose phases delay the
    next poll and the periodic exit sweep), the hot-exit worker or an entry lane. It is called from
    exactly one place, a named daemon thread that opens its own connection."""
    import ast
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    loop = (root / "scripts" / "memecoin_runner_loop.py").read_text()
    assert len(re.findall(r"exec_extremes_pass\(", loop)) == 1
    worker = next(node for node in ast.walk(ast.parse(loop))
                  if isinstance(node, ast.FunctionDef) and node.name == "_exec_extremes_worker")
    body = ast.get_source_segment(loop, worker)
    assert "m.exec_extremes_pass(xconn, " in body and "xconn = _xx_connect()" in body
    assert "xconn.rollback()" in body and "time.sleep(60.0)" in body
    assert 'xconn.execute("PRAGMA busy_timeout=250")' in body     # give up, never queue on the funded writer
    assert 'xconn.execute("PRAGMA wal_autocheckpoint=0")' in body    # a checkpoint is never inside write_ms
    assert "lock_wait_ms=%.1f write_ms=%.1f" in body
    assert re.search(r"threading\.Thread\(target=_exec_extremes_worker,\s*name=\"memecoin-exec-extremes\","
                     r"\s*daemon=True\)\.start\(\)", loop)
    for other in ("sniper/services/memecoin_real_runner.py", "sniper/services/memecoin_route_series.py",
                  "sniper/services/memecoin_exits.py", "sniper/execution/evm_swap_executor.py",
                  "sniper/execution/solana_swap_executor.py"):
        assert "exec_extremes" not in (root / other).read_text(), other


def test_a_clear_always_copies_the_display_value_to_the_backup_first(conn):
    """Operator ruling 1: the old display values go to a side table first, so the clear is
    undoable. That holds for the in-runner pass too, not only for the history script; the first
    copy is never overwritten."""
    pid = _open(conn, "a")
    _close(conn, pid, "2026-07-09T11:59:00+00:00", max_gain=33.0, ttp=5.0)
    assert m.exec_extremes_pass(conn, NOW)["cleared"] == 1
    backup = "SELECT old_max_gain_pct, old_time_to_peak_s, copied_at FROM memecoin_position_display_extremes_backup"
    assert [tuple(r) for r in conn.execute(backup)] == [(33.0, 5.0, NOW)]
    conn.execute("UPDATE memecoin_positions SET max_gain_pct=99, time_to_peak_s=9 WHERE id=?", (pid,))
    conn.commit()
    assert m.exec_extremes_pass(conn, "2026-07-09T12:01:00+00:00")["cleared"] == 1
    assert [tuple(r) for r in conn.execute(backup)] == [(33.0, 5.0, NOW)]
    assert m.clear_funded_display_extremes(conn, pid, NOW) is False      # nothing left to clear
    assert conn.execute("SELECT COUNT(*) FROM memecoin_position_display_extremes_backup").fetchone()[0] == 1


# ------------------------------------------------------------------ the ledger write lock

def test_the_pass_reads_every_series_before_it_takes_the_write_lock(conn):
    """The ledger has one writer at a time and the funded exit and entry paths write to it. The
    pass therefore does all its reads first and holds the write lock for the writes only."""
    for i in range(3):
        pid = _open(conn, "p%d" % i)
        _series(conn, pid, SERIES)
        _close(conn, pid, "2026-07-09T11:59:0%d+00:00" % i)
    statements = []
    conn.set_trace_callback(statements.append)
    out = m.exec_extremes_pass(conn, NOW)
    conn.set_trace_callback(None)
    assert _counts(out) == {"visited": 3, "written": 3, "cleared": 3}
    first_write = next(i for i, s in enumerate(statements) if s.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE")))
    assert not [s for s in statements[first_write:] if "memecoin_route_series" in s]
    assert len([s for s in statements[:first_write] if "memecoin_route_series" in s]) == 9   # 3 reads x 3
    assert out["busy"] is False and out["read_ms"] >= 0.0 and out["write_ms"] >= 0.0


def test_a_busy_ledger_makes_the_pass_give_up_and_write_nothing(tmp_path):
    """While another connection holds the write lock (a funded exit booking, an entry), the pass
    does not wait it out: its connection carries a short busy timeout, it reports busy, leaves
    nothing half-written and runs again on the next pass."""
    import time
    path = str(tmp_path / "ledger.db")
    holder = sqlite3.connect(path)
    holder.row_factory = sqlite3.Row
    holder.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(holder)
    pid = _open(holder, "a")
    _series(holder, pid, SERIES)
    _close(holder, pid, "2026-07-09T11:59:00+00:00")
    worker = sqlite3.connect(path)
    worker.row_factory = sqlite3.Row
    worker.execute("PRAGMA busy_timeout=50")
    holder.execute("BEGIN IMMEDIATE")                        # the funded writer's transaction
    started = time.monotonic()
    out = m.exec_extremes_pass(worker, NOW)
    assert time.monotonic() - started < 2.0
    assert (_counts(out), out["busy"]) == ({"visited": 1, "written": 0, "cleared": 0}, True)
    assert not worker.in_transaction
    holder.commit()
    assert _row(holder, pid) is None and _display(holder, pid) == (33.0, 5.0)
    out = m.exec_extremes_pass(worker, NOW)
    assert (_counts(out), out["busy"]) == ({"visited": 1, "written": 1, "cleared": 1}, False)
    assert _row(holder, pid)["samples"] == 6


def test_an_error_that_is_not_a_held_lock_is_raised_not_reported_as_busy(conn):
    pid = _open(conn, "a")
    _series(conn, pid, SERIES)
    _close(conn, pid, "2026-07-09T11:59:00+00:00")
    conn.execute("DROP TABLE memecoin_position_display_extremes_backup")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        m.exec_extremes_pass(conn, NOW)
    assert not conn.in_transaction and _row(conn, pid) is None


def test_a_batch_backfill_reads_every_series_before_its_first_write(conn):
    """Gamma LOW on 1269b7260: the history script's batch holds the write lock from its first
    write to its commit, so its reads must come first too (12 to 64 ms per 200 positions were
    spent under the lock before)."""
    pids = []
    for i in range(3):
        pid = _open(conn, "b%d" % i)
        _series(conn, pid, SERIES)
        pids.append(pid)
    statements = []
    conn.set_trace_callback(statements.append)
    assert m.backfill_exec_extremes(conn, pids, NOW) == 3
    conn.set_trace_callback(None)
    first_write = next(i for i, s in enumerate(statements) if s.lstrip().upper().startswith(("BEGIN", "INSERT")))
    assert not [s for s in statements[first_write:] if "memecoin_route_series" in s]
    assert len([s for s in statements[:first_write] if "memecoin_route_series" in s]) == 9
    assert conn.in_transaction                              # the caller commits the batch


def test_the_pass_times_the_wait_for_the_lock_apart_from_the_time_it_holds_it(tmp_path):
    """The first live pass logged write_ms=129.2 (2026-09-30T13:37:10Z) and the second 0.2 for 19
    writes. One number could not say whether the pass WAITED 129 ms for the lock or HELD it for
    129 ms; only the second delays a funded writer. The pass now takes the lock with BEGIN IMMEDIATE
    and reports lock_wait_ms and write_ms (the hold, commit included) separately."""
    import threading
    import time
    path = str(tmp_path / "ledger.db")
    holder = sqlite3.connect(path, check_same_thread=False)
    holder.row_factory = sqlite3.Row
    holder.execute("PRAGMA journal_mode=WAL")
    m.ensure_memecoin_tables(holder)
    pid = _open(holder, "a")
    _series(holder, pid, SERIES)
    _close(holder, pid, "2026-07-09T11:59:00+00:00")
    worker = sqlite3.connect(path)
    worker.row_factory = sqlite3.Row
    worker.execute("PRAGMA busy_timeout=2000")
    holder.execute("BEGIN IMMEDIATE")
    threading.Timer(0.3, holder.commit).start()              # the other writer lets go after 300 ms
    statements = []
    worker.set_trace_callback(statements.append)
    out = m.exec_extremes_pass(worker, NOW)
    worker.set_trace_callback(None)
    assert (_counts(out), out["busy"]) == ({"visited": 1, "written": 1, "cleared": 1}, False)
    assert out["lock_wait_ms"] >= 250.0 and out["write_ms"] < 200.0
    writes = [s for s in statements if s.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE"))]
    assert writes[0].strip().upper() == "BEGIN IMMEDIATE"


def test_a_pass_with_nothing_to_visit_takes_no_lock(conn):
    statements = []
    conn.set_trace_callback(statements.append)
    out = m.exec_extremes_pass(conn, NOW)
    conn.set_trace_callback(None)
    assert _counts(out) == {"visited": 0, "written": 0, "cleared": 0}
    assert (out["lock_wait_ms"], out["write_ms"]) == (0.0, 0.0)
    assert not [s for s in statements if s.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE", "COMMIT"))]
