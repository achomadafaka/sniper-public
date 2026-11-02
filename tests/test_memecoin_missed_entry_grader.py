import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_capture_v2 as capture


NOW = "2026-07-17T12:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(conn, event_id, *, mint="M", network="4663", decision="skipped_mcap",
           reason="over_ceiling", at="2026-07-17T11:00:00+00:00",
           source="global_feed", price=1.0, mcap=500_000.0):
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,ticker,side,"
        "their_price,their_market_cap,created_at,received_at_normalized,clock_domain,signal_source,"
        "network_id,real_decision,real_decision_reason,real_decision_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, "u", "alice", mint, "T", "buy", price, mcap, at, at,
         "observed_real_utc", source, network, decision, reason, at),
    )


def _episode(conn, episode_id, event_ids, *, network="4663", mint="M"):
    conn.execute(
        "INSERT INTO memecoin_token_episodes(episode_id,chain_id,token_address,started_ts,status,"
        "last_signal_ts) VALUES(?,?,?,?,?,?)",
        (episode_id, network, mint, "2026-07-17T11:00:00+00:00", "open",
         "2026-07-17T11:01:00+00:00"),
    )
    conn.executemany(
        "INSERT INTO memecoin_episode_signals(episode_id,upstream_event_id,observed_at,source_price) "
        "VALUES(?,?,?,?)",
        [(episode_id, event_id, "2026-07-17T11:00:00+00:00", 1.0)
         for event_id in event_ids],
    )


def _observed_outcome(conn, event_id, prices, *, network="4663", mint="M"):
    outcome_id = capture.create_outcome_v2(
        conn, upstream_event_id=event_id, source_user_id="u", handle="alice",
        network_id=network, mint=mint, ticker="T",
        signal_observed_at="2026-07-17T11:00:00+00:00", ref_price=1.0,
        horizons=[60 * (index + 1) for index in range(len(prices))],
        created_at="2026-07-17T11:00:00+00:00",
    )
    marks = conn.execute(
        "SELECT id,target_s FROM memecoin_horizon_marks_v2 WHERE outcome_id=? ORDER BY target_s",
        (outcome_id,),
    ).fetchall()
    for mark, price in zip(marks, prices):
        observed = f"2026-07-17T11:{int(mark['target_s'] / 60):02d}:00+00:00"
        conn.execute(
            "UPDATE memecoin_horizon_marks_v2 SET observed_at=?,actual_age_s=?,lateness_s=0,"
            "price=?,quality='probe_ok',status='observed' WHERE id=?",
            (observed, float(mark["target_s"]), price, mark["id"]),
        )
    conn.execute(
        "UPDATE memecoin_signal_outcomes_v2 SET observed_marks=?,missed_marks=0,status='complete',"
        "completed_at=? WHERE outcome_id=?", (len(prices), NOW, outcome_id),
    )


def test_simulate_first_touch_uses_time_order_and_gap_through_fill():
    from sniper.services.memecoin_missed_entry_grader import simulate_first_touch

    result = simulate_first_touch(1.0, [
        {"observed_at": "2026-07-17T11:02:00+00:00", "price": 1.8},
        {"observed_at": "2026-07-17T11:01:00+00:00", "price": 0.35},
    ])

    assert result["exit_reason"] == "stop_loss"
    assert result["exit_return_pct"] == -65.0
    assert result["pnl_usd"] == -32.5
    assert result["mfe_pct"] == 80.0


def test_grader_deduplicates_episode_and_uses_first_terminal_event_exact_outcome():
    from sniper.services.memecoin_missed_entry_grader import grade_missed_entries

    conn = _conn()
    _event(conn, "e1", decision="skipped_mcap", reason="over_ceiling")
    _event(conn, "e2", decision="skipped_mcap", reason="over_ceiling",
           at="2026-07-17T11:01:00+00:00")
    _episode(conn, "ep1", ["e1", "e2"])
    _observed_outcome(conn, "e1", [1.7, 1.8])
    _observed_outcome(conn, "e2", [0.5, 0.4])

    report = grade_missed_entries(conn, now_iso=NOW)

    assert len(report["episodes"]) == 1
    row = report["episodes"][0]
    assert row["origin_event_id"] == "e1" and row["episode_id"] == "ep1"
    assert row["terminal_reason"] == "over_ceiling"
    assert row["mfe_pct"] == 80.0
    assert row["simulation"]["exit_reason"] == "take_profit"
    assert report["summary"]["deduped_episodes"] == 1
    assert report["summary"]["forgone_gain_usd"] == 35.0


def test_grader_excludes_episode_that_later_filled():
    from sniper.services.memecoin_missed_entry_grader import grade_missed_entries

    conn = _conn()
    _event(conn, "e1", decision="not_approved", reason="wallet not armed")
    _event(conn, "e2", decision="filled", reason="filled $50",
           at="2026-07-17T11:01:00+00:00")
    _episode(conn, "ep1", ["e1", "e2"])
    _observed_outcome(conn, "e1", [2.0])

    report = grade_missed_entries(conn, now_iso=NOW)

    assert report["episodes"] == []
    assert report["summary"]["later_filled_excluded"] == 1


def test_grader_keeps_missing_coverage_out_of_money_totals_and_lanes_separate():
    from sniper.services.memecoin_missed_entry_grader import grade_missed_entries

    conn = _conn()
    _event(conn, "g", mint="G", source="global_feed", decision="route_failed",
           reason="no route")
    _episode(conn, "global", ["g"], mint="G")
    _event(conn, "f", mint="F", source="follow_activity", decision="unsafe",
           reason="honeypot")
    _episode(conn, "follow", ["f"], mint="F")
    _observed_outcome(conn, "f", [0.2], mint="F")

    report = grade_missed_entries(conn, now_iso=NOW)

    rows = {row["episode_id"]: row for row in report["episodes"]}
    assert rows["global"]["coverage_status"] == "coverage_missing"
    assert rows["global"]["simulation"] is None
    assert rows["follow"]["coverage_status"] == "complete"
    assert report["summary"]["avoided_loss_usd"] == 40.0
    assert {group["signal_source"] for group in report["by_reason"]} == {
        "global_feed", "follow_activity",
    }


class _SpyConn:
    def __init__(self, conn):
        self.conn = conn
        self.writes = []

    def execute(self, sql, *args, **kwargs):
        if sql.lstrip().split(None, 1)[0].upper() not in {"SELECT", "WITH", "PRAGMA"}:
            self.writes.append(sql)
        return self.conn.execute(sql, *args, **kwargs)


def test_grader_is_read_only():
    from sniper.services.memecoin_missed_entry_grader import grade_missed_entries

    spy = _SpyConn(_conn())
    grade_missed_entries(spy, now_iso=NOW)
    assert spy.writes == []


def test_grader_excludes_future_clock_rows_and_accepts_z_suffix_in_window():
    from sniper.services.memecoin_missed_entry_grader import grade_missed_entries

    conn = _conn()
    _event(conn, "in-window", mint="A", at="2026-07-17T11:00:00Z")
    _episode(conn, "ep-a", ["in-window"], mint="A")
    _event(conn, "future", mint="B", at="2026-07-18T11:00:00+00:00")
    _episode(conn, "ep-b", ["future"], mint="B")
    conn.commit()

    report = grade_missed_entries(conn, now_iso=NOW)

    assert [row["origin_event_id"] for row in report["episodes"]] == ["in-window"]
