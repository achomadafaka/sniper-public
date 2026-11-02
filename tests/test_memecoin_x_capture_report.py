import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables
from scripts.report_memecoin_x_capture import build_report, report_from_path


START = datetime(2026, 7, 13, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _iso(value):
    return value.isoformat()


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    return conn


def _seed_boundary(conn, *, runs=100, wall_seconds=86400, healthy=95, gaps=5):
    rows = []
    last_start = max(0, wall_seconds - 1)
    for index in range(runs):
        offset = 0 if runs == 1 else last_start * index / (runs - 1)
        started = START + timedelta(seconds=offset)
        finished = started + timedelta(seconds=1)
        rows.append((
            f"r{index}", 1, _iso(started), _iso(finished),
            "observed_real_utc" if index < healthy else "legacy_unknown",
            100, 0, 0, 1, None, f"p{index}", 1,
            "coverage_gap" if index < gaps else "ok", None,
            "gap_10_15m" if index < gaps else "new_5_8m", 1, 1500,
            "healthy_ack", 1 if index < gaps else 0, 0,
        ))
    conn.executemany("""
        INSERT INTO memecoin_x_capture_runs
            (run_id, parser_version, started_at, finished_at, clock_domain, cards_seen,
             new_posts, new_relationships, parse_failures, prior_sentinel,
             newest_sentinel, sentinel_found, status, error_detail, cadence_bucket,
             scroll_count, dom_wait_ms, reload_reason, recovery_used, operator_postponed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows)
    conn.execute("""
        INSERT INTO memecoin_x_card_observations
            (capture_run_id, canonical_post_id, author_handle, actor_handle, relationship,
             observed_at, clock_domain, parser_version, full_text, text_complete,
             extracted_urls, raw_card_json)
        VALUES ('r0','post','source','source','direct',?,'observed_real_utc',1,
                'public text says authorization',1,'[]',?)
    """, (_iso(START), json.dumps({"full_text": "public text says authorization"})))
    conn.commit()


def test_report_accepts_every_inclusive_boundary(tmp_path):
    conn = _conn()
    _seed_boundary(conn)
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert report["wall_hours"] == 24.0
    assert report["runs"] == 100
    assert report["healthy_clock_rate"] == 0.95
    assert report["coverage_gap_rate"] == 0.05
    assert report["parse_failure_rate"] == 0.01
    assert report["max_gap_seconds"] <= 960
    assert report["secret_scan_hits"] == 0
    assert report["forbidden_source_hits"] == 0
    assert report["pacing_counts"] == {"gap_10_15m": 5, "new_5_8m": 95}
    assert report["page_unready_runs"] == 0
    assert report["operator_postponed_runs"] == 0
    assert report["max_dom_wait_ms"] == 1500
    assert report["acceptance"] is True
    assert report["blocking_reasons"] == []


@pytest.mark.parametrize("mutation,reason", [
    ("short_wall", "wall_hours_below_24"),
    ("few_runs", "finished_runs_below_90"),
    ("bad_clock", "healthy_clock_rate_below_0.95"),
    ("too_many_gaps", "coverage_gap_rate_above_0.05"),
    ("long_gap", "max_gap_seconds_above_960"),
    ("parse_failures", "parse_failure_rate_above_0.01"),
    ("profile_lock", "profile_lock_present"),
    ("secret", "secret_material_present"),
    ("forbidden_source", "forbidden_source_present"),
    ("invalid_pacing", "invalid_pacing_present"),
])
def test_report_rejects_each_failed_gate(tmp_path, mutation, reason):
    conn = _conn()
    if mutation == "short_wall":
        _seed_boundary(conn, wall_seconds=86399)
    elif mutation == "few_runs":
        _seed_boundary(conn, runs=89, healthy=85, gaps=4)
    elif mutation == "bad_clock":
        _seed_boundary(conn, healthy=94)
    elif mutation == "too_many_gaps":
        _seed_boundary(conn, gaps=6)
    else:
        _seed_boundary(conn)

    if mutation == "long_gap":
        row = conn.execute("""
            SELECT run_id, started_at, finished_at FROM memecoin_x_capture_runs
            WHERE run_id='r50'
        """).fetchone()
        started = datetime.fromisoformat(row["started_at"]) + timedelta(seconds=100)
        finished = datetime.fromisoformat(row["finished_at"]) + timedelta(seconds=100)
        conn.execute("""
            UPDATE memecoin_x_capture_runs SET started_at=?, finished_at=? WHERE run_id=?
        """, (_iso(started), _iso(finished), row["run_id"]))
    elif mutation == "parse_failures":
        conn.execute("UPDATE memecoin_x_capture_runs SET parse_failures=2 WHERE run_id='r0'")
    elif mutation == "profile_lock":
        conn.execute("""
            INSERT INTO memecoin_x_capture_diagnostics
                (capture_run_id, observed_at, clock_domain, diagnostic_code, parser_version)
            VALUES ('r0',?,'observed_real_utc','profile_locked',1)
        """, (_iso(START),))
    elif mutation == "secret":
        conn.execute("""
            INSERT INTO memecoin_x_capture_diagnostics
                (capture_run_id, observed_at, clock_domain, diagnostic_code,
                 parser_version, raw_card_json)
            VALUES ('r0',?,'observed_real_utc','capture_error',1,?)
        """, (_iso(START), json.dumps({"authorization": "Bearer secret"})))
    elif mutation == "forbidden_source":
        path = tmp_path / "scripts"
        path.mkdir()
        (path / "memecoin_x_capture.user.js").write_text("button.click();")
    elif mutation == "invalid_pacing":
        conn.execute("UPDATE memecoin_x_capture_runs SET scroll_count=4 WHERE run_id='r0'")
    conn.commit()

    as_of = START + timedelta(seconds=86399 if mutation == "short_wall" else 86400)
    report = build_report(
        conn, since=_iso(START), as_of=_iso(as_of),
        source_root=tmp_path)
    assert report["acceptance"] is False
    assert reason in report["blocking_reasons"]


def test_report_from_path_uses_readonly_connection_and_does_not_mutate(tmp_path, monkeypatch):
    db_path = tmp_path / "capture.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    _seed_boundary(conn)
    before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    conn.close()

    import scripts.report_memecoin_x_capture as module
    real = module.get_readonly_connection
    calls = []

    def tracked(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(module, "get_readonly_connection", tracked)
    report = report_from_path(
        str(db_path), since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert calls == [str(db_path)]
    assert report["acceptance"] is True
    verify = sqlite3.connect(db_path)
    after = verify.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    verify.close()
    assert after == before


def test_report_rejects_dead_tail_after_last_finished_heartbeat(tmp_path):
    conn = _conn()
    _seed_boundary(conn)
    as_of = START + timedelta(hours=24, minutes=16, seconds=1)
    report = build_report(
        conn, since=_iso(START), as_of=_iso(as_of), source_root=tmp_path)
    assert report["trailing_gap_seconds"] == 16 * 60 + 1
    assert report["max_gap_seconds"] == 16 * 60 + 1
    assert report["acceptance"] is False
    assert "max_gap_seconds_above_960" in report["blocking_reasons"]


def test_report_rejects_stale_running_capture(tmp_path):
    conn = _conn()
    _seed_boundary(conn)
    conn.execute("""
        INSERT INTO memecoin_x_capture_runs
            (run_id, parser_version, started_at, clock_domain, status)
        VALUES ('stale',1,?,'observed_real_utc','running')
    """, (_iso(START + timedelta(hours=23, minutes=40)),))
    conn.commit()
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert report["stale_running_runs"] == 1
    assert report["acceptance"] is False
    assert "stale_running_run_present" in report["blocking_reasons"]


def test_report_includes_leading_gap_from_requested_window(tmp_path):
    conn = _conn()
    _seed_boundary(conn)
    for row in conn.execute(
            "SELECT run_id, started_at, finished_at FROM memecoin_x_capture_runs").fetchall():
        conn.execute("""
            UPDATE memecoin_x_capture_runs SET started_at=?, finished_at=? WHERE run_id=?
        """, (_iso(datetime.fromisoformat(row["started_at"]) + timedelta(seconds=961)),
              _iso(datetime.fromisoformat(row["finished_at"]) + timedelta(seconds=961)),
              row["run_id"]))
    conn.commit()
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24, minutes=17)),
        source_root=tmp_path)
    assert report["leading_gap_seconds"] == 961
    assert report["acceptance"] is False
    assert "max_gap_seconds_above_960" in report["blocking_reasons"]


def test_report_rejects_latest_unresolved_operator_state(tmp_path):
    conn = _conn()
    _seed_boundary(conn, gaps=4)
    conn.execute("UPDATE memecoin_x_capture_runs SET status='challenge' WHERE run_id='r99'")
    conn.commit()
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert report["coverage_gap_rate"] == 0.05
    assert report["latest_status"] == "challenge"
    assert report["acceptance"] is False
    assert "unresolved_operator_state" in report["blocking_reasons"]


def test_report_distinguishes_recovered_and_unresolved_page_unready(tmp_path):
    conn = _conn()
    _seed_boundary(conn, gaps=4)
    conn.execute("""
        UPDATE memecoin_x_capture_runs
        SET status='page_unready', cadence_bucket='page_unready_15m', scroll_count=0,
            dom_wait_ms=30000, reload_reason=NULL, recovery_used=0
        WHERE run_id='r1'
    """)
    conn.commit()
    recovered = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert recovered["page_unready_runs"] == 1
    assert "unresolved_page_unready" not in recovered["blocking_reasons"]
    assert recovered["acceptance"] is True

    conn.execute("""
        UPDATE memecoin_x_capture_runs
        SET status='page_unready', cadence_bucket='page_unready_15m', scroll_count=0,
            dom_wait_ms=30000, reload_reason=NULL, recovery_used=0
        WHERE run_id='r99'
    """)
    conn.commit()
    unresolved = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert unresolved["acceptance"] is False
    assert "unresolved_page_unready" in unresolved["blocking_reasons"]


@pytest.mark.parametrize("source", [
    "button.submit();",
    "fetch('/internal');",
    "location.assign('/profile');",
    "location.replace('/profile');",
    "location = '/profile';",
    "document.querySelector('[data-testid=like]');",
    "document.querySelector('[data-testid=follow]');",
])
def test_source_guard_rejects_browser_mutation_surfaces(tmp_path, source):
    path = tmp_path / "scripts"
    path.mkdir()
    (path / "memecoin_x_capture.user.js").write_text(source)
    conn = _conn()
    _seed_boundary(conn)
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert report["forbidden_source_hits"] >= 1
    assert "forbidden_source_present" in report["blocking_reasons"]


def test_source_guard_allows_reload_only(tmp_path):
    path = tmp_path / "scripts"
    path.mkdir()
    (path / "memecoin_x_capture.user.js").write_text("root.location.reload();")
    conn = _conn()
    _seed_boundary(conn)
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert report["forbidden_source_hits"] == 0


def test_repository_capture_sources_have_no_forbidden_actions():
    from scripts.report_memecoin_x_capture import _forbidden_source_hits

    assert _forbidden_source_hits(ROOT) == 0


def test_report_names_zero_card_window_as_insufficient_activity(tmp_path):
    conn = _conn()
    _seed_boundary(conn)
    conn.execute("UPDATE memecoin_x_capture_runs SET cards_seen=0, parse_failures=0")
    conn.commit()
    report = build_report(
        conn, since=_iso(START), as_of=_iso(START + timedelta(hours=24)),
        source_root=tmp_path)
    assert "insufficient_activity" in report["blocking_reasons"]
    assert "parse_failure_rate_above_0.01" not in report["blocking_reasons"]


def test_report_script_help_runs_without_pythonpath():
    result = subprocess.run(
        [sys.executable, "scripts/report_memecoin_x_capture.py", "--help"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--since" in result.stdout


def test_build_report_rejects_implicit_host_clock():
    conn = _conn()
    with pytest.raises(ValueError, match="as_of is required"):
        build_report(conn, since=_iso(START))


def test_report_from_path_uses_corrected_clock_when_as_of_is_omitted(
        tmp_path, monkeypatch):
    db_path = tmp_path / "capture.sqlite"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    _seed_boundary(conn)
    conn.close()

    import scripts.report_memecoin_x_capture as module
    calls = []

    def corrected(*, path):
        calls.append(path)
        return _iso(START + timedelta(hours=24)), {"healthy": True}

    monkeypatch.setattr(module, "x_observed_at_or_none", corrected)
    report = report_from_path(
        str(db_path), since=_iso(START), source_root=tmp_path,
        offset_path="/trusted/clock.json",
    )
    assert calls == ["/trusted/clock.json"]
    assert report["wall_hours"] == 24
