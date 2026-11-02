import json
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scripts import brain_health


def setup_function():
    brain_health.RESULTS.clear()


def test_age_uses_corrected_real_clock():
    with patch.object(
        brain_health, "_now_real",
        return_value=datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
    ):
        assert brain_health._age_s("2026-07-11T11:58:30+00:00") == 90
        assert brain_health._age_s("bad") is None


def test_runner_health_reports_fresh_auth_and_sweep(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_runner.heartbeat").write_text(json.dumps({
        "ts": 1_000.0, "auth_ok": True, "bracket_ts": 995.0,
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(brain_health.time, "time", lambda: 1_005.0)

    brain_health.check_runner()

    assert brain_health.RESULTS == [
        ("OK", "memecoin runner heartbeat", "5s"),
        ("OK", "SL/TP sweep", "fresh"),
    ]


def test_runner_health_fails_closed_on_stale_auth_and_sweep(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_runner.heartbeat").write_text(json.dumps({
        "ts": 800.0, "auth_ok": False, "bracket_ts": 850.0,
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(brain_health.time, "time", lambda: 1_005.0)

    brain_health.check_runner()

    assert [status for status, *_ in brain_health.RESULTS] == ["FAIL", "FAIL", "FAIL"]


def test_real_book_uses_corrected_real_day():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE memecoin_positions (
            id INTEGER PRIMARY KEY, is_open INTEGER, execution_mode TEXT, stuck_bag INTEGER,
            entry_notional REAL, realized_pnl REAL, closed_at TEXT, signal_source TEXT
        );
        -- The robot-performance predicate composes the fomo censor, which reads memecoin_fills.
        -- The fixture needs the table the real query joins, or it tests a narrower statement.
        CREATE TABLE memecoin_fills (position_id INTEGER, fill_source TEXT);
        CREATE TABLE memecoin_pending_entries (status TEXT);
        CREATE TABLE memecoin_wallets (real_approved INTEGER, paused INTEGER, excluded INTEGER);
        INSERT INTO memecoin_positions VALUES (1,0,'real',0,100,12.5,'2026-07-10T23:59:00Z','follow_activity');
        INSERT INTO memecoin_positions VALUES (2,0,'real',0,100,99.0,'2026-07-11T00:01:00Z','follow_activity');
        -- A manual close on the SAME day: all-money must count it, robot must not.
        INSERT INTO memecoin_positions VALUES (3,0,'real',0,100,-4.0,'2026-07-10T23:00:00Z','manual');
    """)
    with patch.object(
        brain_health, "_now_real",
        return_value=datetime(2026, 7, 10, 23, 59, tzinfo=timezone.utc),
    ):
        brain_health.check_real_book(conn)

    def _row(label):
        return next(r for r in brain_health.RESULTS if r[1] == label)
    # The day boundary is still what this test was written for: the 07-11 row must not count.
    # The split is asserted alongside it, so a future edit cannot quietly re-merge the two.
    assert _row("robot realized today")[2] == "$+12.50"
    assert _row("all-money realized today")[2].startswith("$+8.50"), \
        "all-money must include the manual -$4.00; the breaker reads this number"


def test_cron_check_marks_only_missing_entries_warn():
    result = SimpleNamespace(stdout="memecoin_watchdog\nsniper_web_watchdog\ncrypto5m_watchdog\n")
    with patch.object(brain_health.subprocess, "run", return_value=result):
        brain_health.check_crons()
    assert brain_health.RESULTS[-1] == (
        "WARN", "cron: brain sync (daily)", "missing from crontab",
    )


def test_runner_health_reports_the_onchain_volume_feed_age_at_read_time(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "memecoin_runner.heartbeat").write_text(json.dumps({
        "ts": 1_000.0, "auth_ok": True, "bracket_ts": 995.0,
        "onchain_volume_feed": {
            "4663": {"available": True, "feed_age_s": 2.0, "window_age_s": 300.0, "resets_24h": 9},
            "9999": {"available": True, "feed_age_s": 118.0, "window_age_s": None, "resets_24h": 1},
        },
    }))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(brain_health.time, "time", lambda: 1_005.0)

    brain_health.check_runner()

    feeds = [r for r in brain_health.RESULTS if r[1].startswith("on-chain m5 volume feed")]
    assert [(s, c) for s, c, _ in feeds] == [("OK", "on-chain m5 volume feed 4663"),
                                             ("WARN", "on-chain m5 volume feed 9999")]
    assert "7s" in feeds[0][2] and "resets in 24 h 9" in feeds[0][2]
