import sqlite3

import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    return conn


def _observation(conn, pid, actor, observed):
    obs = conn.execute(
        """
        INSERT INTO memecoin_x_card_observations
          (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
           observed_at,clock_domain,parser_version,raw_card_json)
        VALUES ('r',?,'poptyedev',?,'repost',?,'observed_real_utc',1,'{}')
        """, (pid, actor, observed)).lastrowid
    conn.execute(
        """
        INSERT INTO memecoin_x_forward_posts
          (canonical_post_id,author_handle,author_role_at_capture,first_observed_at,
           last_observed_at,parser_version,clock_domain)
        VALUES (?,'poptyedev','tier2_source',?,?,1,'observed_real_utc')
        """, (pid, observed, observed))
    conn.execute(
        """
        INSERT INTO memecoin_x_coin_mentions
          (canonical_post_id,network_id,mint,extraction_method,eligible,parser_version,
           source_observation_id)
        VALUES (?,'4663','0x1111111111111111111111111111111111111111',
                'contract_text',1,1,?)
        """, (pid, obs))
    conn.execute(
        """
        INSERT INTO memecoin_x_relationships
          (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
           relationship,observed_at,clock_domain,source_observation_id)
        VALUES (?,?,'tier1_trader',1,'repost',?,'observed_real_utc',?)
        """, (pid, actor, observed, obs))
    conn.commit()
    return obs


def test_runner_persists_events_before_dashboard_and_selected_telegram_rows():
    from sniper.services.memecoin_x_signal_builder import run_once

    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    _observation(conn, "p2", "goyimpnl", "2026-07-13T00:01:00+00:00")
    result = run_once(conn, run_id="run1", observed_at="2026-07-13T00:02:00+00:00")
    assert result["status"] == "ok" and result["through_observation_id"] == 2
    event_types = [row[0] for row in conn.execute(
        "SELECT event_type FROM memecoin_x_signal_events ORDER BY id")]
    assert event_types == ["capture_tag", "tier2_scout", "capture_tag",
                           "tier2_scout", "fast_consensus"]
    channels = [tuple(row) for row in conn.execute(
        """
        SELECT e.event_type, o.channel FROM memecoin_x_notification_outbox o
        JOIN memecoin_x_signal_events e ON e.id=o.signal_event_id ORDER BY o.id
        """)]
    assert channels.count(("capture_tag", "dashboard")) == 2
    assert channels.count(("tier2_scout", "telegram")) == 2
    assert channels.count(("fast_consensus", "telegram")) == 1


def test_delayed_rerun_is_idempotent_and_cursor_advances():
    from sniper.services.memecoin_x_signal_builder import run_once

    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    first = run_once(conn, run_id="run1", observed_at="2026-07-13T02:00:00+00:00")
    second = run_once(conn, run_id="run2", observed_at="2026-07-13T03:00:00+00:00")
    assert first["events_created"] == 2
    assert second["events_created"] == 0 and second["after_observation_id"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_signal_events").fetchone()[0] == 2


def test_runner_failure_records_error_without_advancing_cursor(monkeypatch):
    import sniper.services.memecoin_x_signal_builder as builder

    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    monkeypatch.setattr(builder, "events_for_observation",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        builder.run_once(conn, run_id="bad", observed_at="2026-07-13T00:01:00+00:00")
    row = conn.execute("SELECT status, through_observation_id FROM memecoin_x_signal_runs").fetchone()
    assert tuple(row) == ("error", 0)
    assert builder.last_completed_observation_id(conn) == 0


def test_runner_rejects_invalid_limit_and_duplicate_run_id():
    from sniper.services.memecoin_x_signal_builder import run_once

    conn = _conn()
    with pytest.raises(ValueError, match="limit"):
        run_once(conn, run_id="x", observed_at="2026-07-13T00:00:00+00:00", limit=0)
    run_once(conn, run_id="once", observed_at="2026-07-13T00:00:00+00:00")
    with pytest.raises(ValueError, match="already exists"):
        run_once(conn, run_id="once", observed_at="2026-07-13T00:00:00+00:00")
