import json
import sqlite3

from sniper.persistence import db_memecoin as m


NOW = "2026-07-17T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_latency_report_aggregates_substages_by_chain_and_terminal_status():
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    for index, duration in enumerate((1, 2, 3, 4, 5), start=1):
        trace = {"substage_events": [
            {"name": "evm.approval", "duration_ms": duration, "elapsed_ms": duration},
            {"name": "evm.confirmation", "duration_ms": duration * 10,
             "elapsed_ms": duration * 11},
        ]}
        conn.execute(
            "INSERT INTO memecoin_entry_attempts(attempt_id,network_id,mint,observed_at,"
            "decision_started_at,terminal_status,terminal_reason,completed_at,trace_json,total_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (f"a{index}", "4663", f"0x{index}", "2026-07-17T11:00:00+00:00",
             "2026-07-17T11:00:00+00:00", "filled", "filled",
             "2026-07-17T11:00:01+00:00", json.dumps(trace), duration * 100),
        )

    report = latency_report(conn, now_iso=NOW)
    approval = next(row for row in report["substages"] if row["substage"] == "evm.approval")
    assert approval == {
        "network_id": "4663", "terminal_status": "filled", "substage": "evm.approval",
        "n": 5, "p50_ms": 3.0, "p95_ms": 5.0, "thin": False,
    }
    confirmation = next(
        row for row in report["substages"] if row["substage"] == "evm.confirmation")
    assert confirmation["p50_ms"] == 30.0 and confirmation["p95_ms"] == 50.0
    assert report["attempts"] == 5


def test_latency_report_labels_samples_under_five_as_thin_and_ignores_bad_json():
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    conn.executemany(
        "INSERT INTO memecoin_entry_attempts(attempt_id,network_id,mint,observed_at,"
        "decision_started_at,terminal_status,terminal_reason,completed_at,trace_json) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        [
            ("good", "solana", "M", "2026-07-17T11:00:00+00:00",
             "2026-07-17T11:00:00+00:00", "filled", "filled",
             "2026-07-17T11:00:01+00:00",
             json.dumps({"substage_events": [{"name": "solana.sign", "duration_ms": 7}]})),
            ("bad", "solana", "N", "2026-07-17T11:00:00+00:00",
             "2026-07-17T11:00:00+00:00", "refused", "no",
             "2026-07-17T11:00:01+00:00", "{"),
        ],
    )

    report = latency_report(conn, now_iso=NOW)
    assert report["substages"][0]["thin"] is True
    assert report["substages"][0]["n"] == 1
    assert report["invalid_trace_json"] == 1


def test_latency_report_excludes_future_clock_rows():
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    for attempt, observed in (
        ("current", "2026-07-17T11:00:00Z"),
        ("future", "2026-07-18T11:00:00+00:00"),
    ):
        conn.execute(
            "INSERT INTO memecoin_entry_attempts(attempt_id,network_id,mint,observed_at,"
            "decision_started_at,terminal_status,terminal_reason,completed_at,total_ms) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (attempt, "4663", "0x" + attempt, observed, observed, "filled", "filled",
             observed, 10),
        )
    report = latency_report(conn, now_iso=NOW)
    assert report["attempts"] == 1


def _insert(conn, index, *, stages, total_ms=100.0):
    conn.execute(
        "INSERT INTO memecoin_entry_attempts(attempt_id,network_id,mint,observed_at,"
        "decision_started_at,terminal_status,terminal_reason,completed_at,trace_json,total_ms,"
        "safety_checks_ms,reservation_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"m{index}", "4663", f"0x{index}", "2026-07-17T11:00:00+00:00",
         "2026-07-17T11:00:00+00:00", "filled", "filled", "2026-07-17T11:00:01+00:00",
         json.dumps({"stages": stages}), total_ms, 1500.0, 500.0))


def test_the_report_says_whether_a_stage_duration_was_measured_or_inferred():
    """Nine of the ten funded-entry stages record the gap since the previous mark rather than their
    own work, and all ten land in identically-named `*_ms` columns. Presenting their p50s side by
    side without saying which is which is what produced a phantom budget residual, a wrong stage
    order, and safety reading 1,534 ms for 2,835 ms of work."""
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    for index in range(1, 6):
        _insert(conn, index, stages={
            "reservation": {"completed_at": "2026-07-17T11:00:01+00:00",
                            "duration_ms": 500.0, "measured": True},
            "safety_checks": {"completed_at": "2026-07-17T11:00:01+00:00",
                              "duration_ms": 1500.0, "measured": False},
        })
    report = latency_report(conn, now_iso=NOW)
    by_stage = {row["stage"]: row for row in report["top_level"]}
    assert by_stage["reservation_ms"]["measured"] is True
    assert by_stage["safety_checks_ms"]["measured"] is False, (
        "an inferred inter-mark interval must not be presented as a measured stage duration")


def test_one_inferred_row_makes_the_whole_aggregate_inferred():
    """A p50 over a mix is not a measured duration. The aggregate degrades to the weaker claim."""
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    for index in range(1, 5):
        _insert(conn, index, stages={"reservation": {
            "completed_at": "2026-07-17T11:00:01+00:00", "duration_ms": 500.0, "measured": True}})
    _insert(conn, 9, stages={"reservation": {
        "completed_at": "2026-07-17T11:00:01+00:00", "duration_ms": 500.0, "measured": False}})
    report = latency_report(conn, now_iso=NOW)
    row = next(r for r in report["top_level"] if r["stage"] == "reservation_ms")
    assert row["measured"] is False


def test_a_row_without_the_flag_leaves_the_stage_unknown_rather_than_claiming_measured():
    """Rows written before 2026-09-03 carry no flag. Unknown must not read as measured."""
    from sniper.services.memecoin_latency_report import latency_report

    conn = _conn()
    for index in range(1, 6):
        _insert(conn, index, stages={"reservation": {
            "completed_at": "2026-07-17T11:00:01+00:00", "duration_ms": 500.0}})
    report = latency_report(conn, now_iso=NOW)
    row = next(r for r in report["top_level"] if r["stage"] == "reservation_ms")
    assert row["measured"] is None
