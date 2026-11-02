import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_capture_v2 as capture


START = "2026-07-11T00:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _outcome(conn, horizons=(60, 300)):
    return capture.create_outcome_v2(
        conn, upstream_event_id="e1", source_user_id="u", handle="h",
        network_id="solana", mint="M", ticker="T", signal_observed_at=START,
        ref_price=1.0, horizons=horizons, created_at=START,
    )


def _quote(conn, observation_id, observed_at, *, price=1.2, quality="ok"):
    conn.execute(
        "INSERT INTO memecoin_quote_sweeps_v2(sweep_id, observed_at, eligible_count, requested_count, "
        "captured_count, budget_skipped_count) VALUES (?,?,?,?,?,?)",
        (f"s{observation_id}", observed_at, 1, 1, 1, 0),
    )
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(id, sweep_id, network_id, mint, observed_at, "
        "price, quality, contexts) VALUES (?,?,?,?,?,?,?,?)",
        (observation_id, f"s{observation_id}", "solana", "M", observed_at, price, quality, "signal"),
    )
    conn.commit()


def test_on_time_quote_fills_mark_with_age_and_lateness():
    conn = _conn(); outcome_id = _outcome(conn)
    _quote(conn, 1, "2026-07-11T00:01:30+00:00", price=1.25)
    result = capture.assign_due_horizons(
        conn, "2026-07-11T00:01:30+00:00", tolerance_s=90, abandon_s=1800,
    )
    mark = conn.execute(
        "SELECT * FROM memecoin_horizon_marks_v2 WHERE outcome_id=? AND target_s=60",
        (outcome_id,),
    ).fetchone()
    assert result.observed == 1
    assert mark["status"] == "observed" and mark["observation_id"] == 1
    assert mark["actual_age_s"] == 90.0 and mark["lateness_s"] == 30.0
    assert mark["price"] == 1.25


def test_early_quote_is_not_used_and_late_window_becomes_missing_forever():
    conn = _conn(); outcome_id = _outcome(conn)
    _quote(conn, 1, "2026-07-11T00:00:59+00:00")
    _quote(conn, 2, "2026-07-11T00:02:31+00:00")  # 91s late, outside tolerance
    capture.assign_due_horizons(
        conn, "2026-07-11T00:02:31+00:00", tolerance_s=90, abandon_s=1800,
    )
    mark = conn.execute(
        "SELECT status, observation_id FROM memecoin_horizon_marks_v2 "
        "WHERE outcome_id=? AND target_s=60", (outcome_id,),
    ).fetchone()
    assert tuple(mark) == ("missed", None)
    _quote(conn, 3, "2026-07-11T00:02:32+00:00")
    capture.assign_due_horizons(
        conn, "2026-07-11T00:02:32+00:00", tolerance_s=90, abandon_s=1800,
    )
    assert conn.execute(
        "SELECT status FROM memecoin_horizon_marks_v2 WHERE outcome_id=? AND target_s=60",
        (outcome_id,),
    ).fetchone()[0] == "missed"


def test_one_observation_cannot_fill_two_horizons_in_same_outcome():
    conn = _conn(); outcome_id = _outcome(conn, horizons=(60, 90))
    _quote(conn, 1, "2026-07-11T00:01:35+00:00")
    capture.assign_due_horizons(
        conn, "2026-07-11T00:01:35+00:00", tolerance_s=90, abandon_s=1800,
    )
    marks = [tuple(r) for r in conn.execute(
        "SELECT target_s, status, observation_id FROM memecoin_horizon_marks_v2 "
        "WHERE outcome_id=? ORDER BY target_s", (outcome_id,),
    )]
    assert marks == [(60, "observed", 1), (90, "pending", None)]


def test_one_observation_can_fill_same_horizon_for_two_distinct_events():
    conn = _conn()
    first = _outcome(conn, horizons=(60,))
    second = capture.create_outcome_v2(
        conn, upstream_event_id="e2", source_user_id="u2", handle="h2",
        network_id="solana", mint="M", ticker="T", signal_observed_at=START,
        ref_price=1.0, horizons=(60,), created_at=START,
    )
    _quote(conn, 1, "2026-07-11T00:01:05+00:00")

    capture.assign_due_horizons(
        conn, "2026-07-11T00:01:05+00:00", tolerance_s=90, abandon_s=1800,
    )

    marks = [tuple(r) for r in conn.execute(
        "SELECT outcome_id, status, observation_id FROM memecoin_horizon_marks_v2 "
        "WHERE outcome_id IN (?,?) ORDER BY outcome_id", (first, second),
    )]
    assert marks == sorted([(first, "observed", 1), (second, "observed", 1)])


def test_bad_quality_quote_is_excluded_then_mark_misses():
    conn = _conn(); outcome_id = _outcome(conn)
    _quote(conn, 1, "2026-07-11T00:01:10+00:00", quality="ratio_outlier")
    capture.assign_due_horizons(
        conn, "2026-07-11T00:02:31+00:00", tolerance_s=90, abandon_s=1800,
    )
    mark = conn.execute(
        "SELECT status, observation_id FROM memecoin_horizon_marks_v2 "
        "WHERE outcome_id=? AND target_s=60", (outcome_id,),
    ).fetchone()
    assert tuple(mark) == ("missed", None)


def test_final_overdue_mark_is_abandoned_and_outcome_is_partial():
    conn = _conn(); outcome_id = _outcome(conn, horizons=(60,))
    result = capture.assign_due_horizons(
        conn, "2026-07-11T00:32:00+00:00", tolerance_s=90, abandon_s=1800,
    )
    mark = conn.execute(
        "SELECT status FROM memecoin_horizon_marks_v2 WHERE outcome_id=?", (outcome_id,),
    ).fetchone()[0]
    outcome = conn.execute(
        "SELECT status, expected_marks, observed_marks, missed_marks "
        "FROM memecoin_signal_outcomes_v2 WHERE outcome_id=?", (outcome_id,),
    ).fetchone()
    assert mark == "abandoned" and result.abandoned == 1
    assert tuple(outcome) == ("partial", 1, 0, 1)


def test_all_observed_marks_complete_outcome():
    conn = _conn(); outcome_id = _outcome(conn, horizons=(60,))
    _quote(conn, 1, "2026-07-11T00:01:00+00:00")
    capture.assign_due_horizons(
        conn, "2026-07-11T00:01:00+00:00", tolerance_s=90, abandon_s=1800,
    )
    assert conn.execute(
        "SELECT status FROM memecoin_signal_outcomes_v2 WHERE outcome_id=?", (outcome_id,),
    ).fetchone()[0] == "complete"


def test_create_outcome_is_idempotent_and_requires_positive_reference():
    conn = _conn()
    first = _outcome(conn)
    second = _outcome(conn)
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes_v2").fetchone()[0] == 1
    try:
        capture.create_outcome_v2(
            conn, upstream_event_id="bad", source_user_id="u", handle="h",
            network_id="solana", mint="M", ticker="T", signal_observed_at=START,
            ref_price=0, horizons=(60,), created_at=START,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("zero reference price must be rejected")


def test_create_outcome_rejects_conflicting_retry():
    conn = _conn()
    _outcome(conn)
    try:
        capture.create_outcome_v2(
            conn, upstream_event_id="e1", source_user_id="u", handle="h",
            network_id="solana", mint="DIFFERENT", ticker="T", signal_observed_at=START,
            ref_price=1.0, horizons=(60, 300), created_at=START,
        )
    except ValueError as exc:
        assert "conflicting" in str(exc)
    else:
        raise AssertionError("conflicting event retry must be rejected")


def test_create_outcome_rolls_back_partial_insert():
    conn = _conn()
    conn.execute(
        "CREATE TRIGGER reject_horizon BEFORE INSERT ON memecoin_horizon_marks_v2 "
        "BEGIN SELECT RAISE(ABORT, 'forced horizon failure'); END",
    )
    try:
        _outcome(conn)
    except sqlite3.IntegrityError:
        pass
    else:
        raise AssertionError("forced horizon failure must surface")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_signal_outcomes_v2").fetchone()[0] == 0


def test_create_outcome_rejects_naive_time_and_fractional_horizon():
    conn = _conn()
    for signal_at, horizons in (("2026-07-11T00:00:00", (60,)), (START, (60.5,))):
        try:
            capture.create_outcome_v2(
                conn, upstream_event_id=f"bad-{signal_at}-{horizons}", source_user_id="u",
                handle="h", network_id="solana", mint="M", ticker="T",
                signal_observed_at=signal_at, ref_price=1.0, horizons=horizons,
                created_at=START,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("invalid clock or fractional horizon must be rejected")
