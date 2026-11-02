import json
import sqlite3
from pathlib import Path

import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables, seed_roster
from sniper.analysis.memecoin_x import roster as R
import sniper.services.memecoin_x_receiver as receiver
from sniper.services.memecoin_x_receiver import create_app, initialize_store, process_batch

FIX = Path("tests/fixtures/memecoin_x/dom")
NOW = 1_000_000.0


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(c)
    seed_roster(c, R.INITIAL_ROSTER, now_iso="2026-07-13T00:00:00+00:00")
    return c


def _healthy_offset(tmp_path):
    p = tmp_path / "off.json"
    p.write_text(json.dumps({"offset_s": 0.0, "at": NOW - 30}))
    return str(p)


def _cards(*kinds):
    return [(FIX / f"{k}.html").read_text() for k in kinds]


def test_initialize_store_migrates_before_first_request(tmp_path):
    db_path = tmp_path / "x.db"
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE memecoin_x_capture_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            parser_version INTEGER NOT NULL,
            status TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    initialize_store(str(db_path))

    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(memecoin_x_capture_runs)")}
    assert {"cadence_bucket", "scroll_count", "dom_wait_ms", "reload_reason",
            "recovery_used", "operator_postponed"} <= columns
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_accounts").fetchone()[0] == 35


def test_process_batch_persists_and_stamps_real_clock(tmp_path):
    conn = _conn()
    res = process_batch(conn, _cards("direct", "repost", "quote"),
                        run_id="run1", now=NOW, offset_path=_healthy_offset(tmp_path))
    assert res["cards_seen"] == 3
    assert res["clock_domain"] == "observed_real_utc"
    assert res["new_posts"] >= 3
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    assert posts >= 3
    (runs,) = conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_capture_runs WHERE run_id='run1' AND status='ok'").fetchone()
    assert runs == 1
    row = conn.execute("SELECT clock_domain, author_handle FROM memecoin_x_forward_posts "
                       "WHERE canonical_post_id='2076720348171522273'").fetchone()
    assert row["clock_domain"] == "observed_real_utc" and row["author_handle"] == "memekingdom"
    # repost actor/author split persisted
    rel = conn.execute("SELECT actor_handle, relationship FROM memecoin_x_relationships "
                       "WHERE canonical_post_id='2076734724178096374'").fetchone()
    assert rel["relationship"] == "repost" and rel["actor_handle"] == "memekingdom"


def test_process_batch_idempotent(tmp_path):
    conn = _conn()
    off = _healthy_offset(tmp_path)
    process_batch(conn, _cards("direct"), run_id="r1", now=NOW, offset_path=off)
    res2 = process_batch(conn, _cards("direct"), run_id="r2", now=NOW, offset_path=off)
    assert res2["new_posts"] == 0
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    assert posts == 1


def test_process_batch_unhealthy_clock_marks_legacy(tmp_path):
    conn = _conn()
    res = process_batch(conn, _cards("direct"), run_id="ru", now=NOW,
                        offset_path=str(tmp_path / "missing.json"))
    assert res["clock_domain"] == "legacy_unknown"
    row = conn.execute("SELECT clock_domain FROM memecoin_x_forward_posts "
                       "WHERE canonical_post_id='2076720348171522273'").fetchone()
    assert row["clock_domain"] == "legacy_unknown"
    (runs,) = conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_capture_runs WHERE status='clock_unhealthy'").fetchone()
    assert runs == 1


def test_process_batch_unhealthy_coverage_gap_cannot_be_acknowledged(tmp_path):
    conn = _conn()
    result = process_batch(
        conn, _cards("direct"), run_id="unhealthy-gap", now=NOW,
        offset_path=str(tmp_path / "missing.json"),
        pass_meta={"status": "coverage_gap", "diagnostics": ["coverage_gap"],
                   "cards_scanned": 4, "stop_reason": "scroll_limit"})
    assert result["status"] == "clock_unhealthy"
    assert result["clock_domain"] == "legacy_unknown"
    assert result["cards_seen"] == 1


def test_process_batch_bad_html_counts_failure(tmp_path):
    conn = _conn()
    res = process_batch(conn, ["<div>not a tweet</div>"], run_id="rb", now=NOW,
                        offset_path=_healthy_offset(tmp_path))
    assert res["new_posts"] == 0 and res["parse_failures"] == 1
    assert res["status"] == "parser_error"


def test_process_batch_quarantines_promoted_card_without_parser_retry(tmp_path):
    conn = _conn()
    promoted = '''
      <article data-testid="tweet">
        <div data-testid="placementTracking">Promocionado</div>
        <a href="https://x.com/poptyedev/status/9001">
          <time datetime="2026-07-14T00:00:00Z">ahora</time>
        </a>
        <div data-testid="tweetText">Robinhood Chain CA:
          0x1111111111111111111111111111111111111111</div>
      </article>
    '''
    result = process_batch(
        conn, [promoted], run_id="promoted", now=NOW,
        offset_path=_healthy_offset(tmp_path))
    assert result["status"] == "ok" and result["parse_failures"] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_coin_mentions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT diagnostic_code FROM memecoin_x_capture_diagnostics"
    ).fetchone()[0] == "promoted_card"


def test_receiver_does_not_ack_all_unparseable_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "time", type("Clock", (), {"time": staticmethod(lambda: NOW)}))
    app = create_app(str(tmp_path / "x.db"))
    response = app.test_client().post("/x/cards", json={
        "run_id": "bad-dom", "cards": ["<div>not a tweet</div>"],
    })
    assert response.status_code == 422
    assert response.get_json()["status"] == "parser_error"


def test_process_batch_starts_run_before_card_persistence_and_finishes_error(tmp_path, monkeypatch):
    conn = _conn()

    def explode(*args, **kwargs):
        running = conn.execute(
            "SELECT status FROM memecoin_x_capture_runs WHERE run_id='crash'").fetchone()
        assert running["status"] == "running"
        raise RuntimeError("simulated persist failure")

    monkeypatch.setattr(receiver.X, "persist_card", explode)
    with pytest.raises(RuntimeError, match="simulated persist failure"):
        process_batch(conn, _cards("direct"), run_id="crash", now=NOW,
                      offset_path=_healthy_offset(tmp_path))
    row = conn.execute(
        "SELECT status, error_detail FROM memecoin_x_capture_runs WHERE run_id='crash'").fetchone()
    assert row["status"] == "error"
    assert "RuntimeError" in row["error_detail"]


def test_process_batch_rolls_back_partial_cards_before_error_finalization(tmp_path, monkeypatch):
    conn = _conn()
    real_persist = receiver.X.persist_card
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("second card failed")
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(receiver.X, "persist_card", fail_second)
    with pytest.raises(RuntimeError, match="second card failed"):
        process_batch(conn, _cards("direct", "repost"), run_id="partial", now=NOW,
                      offset_path=_healthy_offset(tmp_path))
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_card_observations WHERE capture_run_id='partial'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_forward_posts WHERE capture_run_id='partial'").fetchone()[0] == 0
    assert conn.execute(
        "SELECT status FROM memecoin_x_capture_runs WHERE run_id='partial'").fetchone()[0] == "error"


def test_process_batch_completed_run_retry_is_idempotent(tmp_path):
    conn = _conn()
    off = _healthy_offset(tmp_path)
    first = process_batch(conn, _cards("direct"), run_id="same", now=NOW, offset_path=off)
    second = process_batch(conn, _cards("direct"), run_id="same", now=NOW, offset_path=off)
    assert second == {**first, "duplicate": True}
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_capture_runs WHERE run_id='same'").fetchone()[0] == 1


def test_process_batch_error_run_can_retry_same_client_run_id(tmp_path, monkeypatch):
    conn = _conn()
    real_persist = receiver.X.persist_card
    monkeypatch.setattr(
        receiver.X, "persist_card",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("transient")))
    with pytest.raises(RuntimeError, match="transient"):
        process_batch(conn, _cards("direct"), run_id="retry-error", now=NOW,
                      offset_path=_healthy_offset(tmp_path))
    monkeypatch.setattr(receiver.X, "persist_card", real_persist)

    result = process_batch(conn, _cards("direct"), run_id="retry-error", now=NOW,
                           offset_path=_healthy_offset(tmp_path))
    assert result["status"] == "ok"
    assert result.get("duplicate") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_card_observations WHERE capture_run_id='retry-error'").fetchone()[0] == 1


def test_process_batch_persists_parser_diagnostics(tmp_path):
    conn = _conn()
    process_batch(conn, _cards("quote"), run_id="quote-diag", now=NOW,
                  offset_path=_healthy_offset(tmp_path))
    codes = {r[0] for r in conn.execute(
        "SELECT diagnostic_code FROM memecoin_x_capture_diagnostics WHERE capture_run_id='quote-diag'")}
    assert "missing_referenced_post_id" in codes
    rows = conn.execute(
        "SELECT canonical_post_id FROM memecoin_x_capture_diagnostics "
        "WHERE capture_run_id='quote-diag' AND diagnostic_code='missing_referenced_post_id'"
    ).fetchall()
    assert rows and all(row["canonical_post_id"] for row in rows)


def test_process_batch_persists_visible_quote_and_reply_relationships(tmp_path):
    conn = _conn()
    process_batch(conn, _cards("quote_with_status", "reply"), run_id="nested-rel",
                  now=NOW, offset_path=_healthy_offset(tmp_path))
    relationships = {
        (row["canonical_post_id"], row["actor_handle"], row["relationship"])
        for row in conn.execute("""
            SELECT canonical_post_id, actor_handle, relationship
            FROM memecoin_x_relationships
            WHERE capture_run_id='nested-rel'
        """)
    }
    assert ("2001", "zinceth", "quote") in relationships
    assert ("2002", "zinceth", "reply") in relationships


def test_receiver_rejects_oversized_or_non_string_batches(tmp_path):
    app = create_app(str(tmp_path / "x.db"))
    client = app.test_client()
    too_many = client.post("/x/cards", json={"run_id": "r-many", "cards": ["x"] * 51})
    assert too_many.status_code == 400
    non_string = client.post("/x/cards", json={"run_id": "r-type", "cards": [123]})
    assert non_string.status_code == 400
    no_run_id = client.post("/x/cards", json={"cards": []})
    assert no_run_id.status_code == 400


def test_receiver_rejects_cards_from_non_capture_feed_state(tmp_path):
    app = create_app(str(tmp_path / "x.db"))
    client = app.test_client()
    response = client.post("/x/cards", json={
        "run_id": "wrong-with-card", "cards": _cards("direct"),
        "status": "wrong_feed", "diagnostics": ["wrong_feed"],
    })
    assert response.status_code == 400
    assert not (tmp_path / "x.db").exists()


@pytest.mark.parametrize("status,diagnostic", [
    ("ok", "wrong_feed"),
    ("ok", "unauthenticated"),
    ("ok", "challenge"),
    ("ok", "rate_limited"),
    ("ok", "parser_error"),
    ("no_new", "coverage_gap"),
])
def test_receiver_rejects_incoherent_status_diagnostic_metadata(
        tmp_path, status, diagnostic):
    app = create_app(str(tmp_path / "x.db"))
    response = app.test_client().post("/x/cards", json={
        "run_id": f"bad-{diagnostic}", "cards": _cards("direct"),
        "status": status, "diagnostics": [diagnostic],
    })
    assert response.status_code == 400
    assert not (tmp_path / "x.db").exists()


@pytest.mark.parametrize("body", [
    [],
    {"run_id": "bad-cards", "cards": ""},
    {"run_id": "bad-status", "cards": [], "status": "invented"},
    {"run_id": "bad-diags", "cards": [], "diagnostics": "wrong_feed"},
    {"run_id": "unknown-diag", "cards": [], "diagnostics": ["invented"]},
    {"run_id": "bad-sentinel", "cards": [], "sentinel_found": "false"},
    {"run_id": "bad-scanned", "cards": [], "cards_scanned": -1},
    {"run_id": "bad-stop", "cards": [], "stop_reason": "invented"},
])
def test_receiver_rejects_malformed_capture_metadata(tmp_path, body):
    app = create_app(str(tmp_path / "x.db"))
    response = app.test_client().post("/x/cards", json=body)
    assert response.status_code == 400


def test_process_batch_stale_running_retry_refreshes_clock_snapshot(tmp_path):
    conn = _conn()
    receiver.X.start_capture_run(
        conn, run_id="stale-running", parser_version=1,
        started_at="1970-01-12T13:44:00+00:00",
        clock_snapshot={"clock_domain": "legacy_unknown", "offset_s": None,
                        "offset_age_s": None},
        prior_sentinel="old")
    result = process_batch(conn, _cards("direct"), run_id="stale-running", now=NOW,
                           offset_path=_healthy_offset(tmp_path))
    row = conn.execute("""
        SELECT started_at, clock_domain FROM memecoin_x_capture_runs
        WHERE run_id='stale-running'
    """).fetchone()
    assert result["status"] == "ok"
    assert row["started_at"] == "1970-01-12T13:46:40+00:00"
    assert row["clock_domain"] == "observed_real_utc"


def test_process_batch_does_not_take_over_active_running_run(tmp_path):
    conn = _conn()
    receiver.X.start_capture_run(
        conn, run_id="active", parser_version=1,
        started_at="1970-01-12T13:46:20+00:00",
        clock_snapshot={"clock_domain": "observed_real_utc", "offset_s": 0,
                        "offset_age_s": 0},
        prior_sentinel="old")
    result = process_batch(conn, _cards("direct"), run_id="active", now=NOW,
                           offset_path=_healthy_offset(tmp_path))
    assert result["status"] == "running"
    assert result["duplicate"] is True
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()[0] == 0


def test_process_batch_persists_scanned_count_and_stop_reason(tmp_path):
    conn = _conn()
    result = process_batch(
        conn, _cards("direct"), run_id="scan-meta", now=NOW,
        offset_path=_healthy_offset(tmp_path),
        pass_meta={"status": "coverage_gap", "diagnostics": ["coverage_gap"],
                   "cards_scanned": 17, "stop_reason": "scroll_limit"})
    assert result["cards_seen"] == 1
    assert result["cards_scanned"] == 17
    assert result["stop_reason"] == "scroll_limit"


def test_receiver_commits_page_unready_with_pacing_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "time", type("Clock", (), {"time": staticmethod(lambda: NOW)}))
    app = create_app(str(tmp_path / "x.db"))
    response = app.test_client().post("/x/cards", json={
        "run_id": "page-unready-1", "cards": [], "status": "page_unready",
        "diagnostics": ["page_unready"], "cards_scanned": 0,
        "stop_reason": "page_unready", "cadence_bucket": "page_unready_15m",
        "scroll_count": 0, "dom_wait_ms": 30000, "reload_reason": None,
        "recovery_used": False, "operator_postponed": False,
    })
    assert response.status_code == 200
    assert response.get_json()["status"] == "page_unready"
    conn = sqlite3.connect(tmp_path / "x.db")
    row = conn.execute("""
        SELECT cadence_bucket, scroll_count, dom_wait_ms, reload_reason,
               recovery_used, operator_postponed
        FROM memecoin_x_capture_runs WHERE run_id='page-unready-1'
    """).fetchone()
    assert row == ("page_unready_15m", 0, 30000, None, 0, 0)


def test_receiver_uses_explicit_corrected_clock_file(tmp_path, monkeypatch):
    monkeypatch.setattr(receiver, "time", type("Clock", (), {"time": staticmethod(lambda: NOW)}))
    offset_path = _healthy_offset(tmp_path)
    app = create_app(str(tmp_path / "x.db"), offset_path=offset_path)

    response = app.test_client().post("/x/cards", json={
        "run_id": "explicit-clock", "cards": _cards("direct"),
        "status": "ok", "cards_scanned": 1, "stop_reason": "initial_pass",
        "cadence_bucket": "new_5_8m", "scroll_count": 0, "dom_wait_ms": 0,
        "reload_reason": "healthy_ack", "recovery_used": False,
        "operator_postponed": False,
    })

    assert response.status_code == 200
    assert response.get_json()["clock_domain"] == "observed_real_utc"


@pytest.mark.parametrize("field,value", [
    ("cadence_bucket", "invented"),
    ("scroll_count", -1),
    ("scroll_count", 4),
    ("dom_wait_ms", -1),
    ("dom_wait_ms", 30001),
    ("reload_reason", "invented"),
    ("recovery_used", 1),
    ("operator_postponed", "false"),
])
def test_receiver_rejects_invalid_pacing_metadata(tmp_path, field, value):
    body = {
        "run_id": f"bad-pacing-{field}", "cards": [], "status": "no_new",
        "cadence_bucket": "idle_10_15m", "scroll_count": 0, "dom_wait_ms": 0,
        "reload_reason": "none", "recovery_used": False,
        "operator_postponed": False,
    }
    body[field] = value
    response = create_app(str(tmp_path / "x.db")).test_client().post("/x/cards", json=body)
    assert response.status_code == 400
