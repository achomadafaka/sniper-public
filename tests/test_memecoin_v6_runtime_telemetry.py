from datetime import datetime, timedelta, timezone

from sniper.persistence import db_memecoin_v6_paper as db
from sniper.services.memecoin_v6_events import V6FeedCursor, V6FeedHealth
from sniper.services.memecoin_v6_paper_report import runtime_health


BASE = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _conn(tmp_path):
    return db.connect(str(tmp_path / "v6.sqlite"))


def test_unchanged_heartbeats_update_one_health_row(tmp_path):
    conn = _conn(tmp_path)
    cursor = V6FeedCursor("4663", "0xpool", 10, "event-10")
    for i in range(5_000):
        now = (BASE + timedelta(seconds=i)).isoformat()
        health = V6FeedHealth("poll_500ms", True, False, now, 0)
        db.upsert_feed_health(
            conn, "4663", "0xpool", health, cursor=cursor,
            accumulator_freshness_s=0.2, updated_at=now)
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_feed_health").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM v6_paper_observations").fetchone()[0] == 0
    row = conn.execute("SELECT updated_at FROM v6_paper_feed_health").fetchone()
    assert row[0] == (BASE + timedelta(seconds=4_999)).isoformat()


def test_latency_report_keeps_provider_and_local_stages_separate(tmp_path):
    conn = _conn(tmp_path)
    at = BASE.isoformat()
    db.append_latency_sample(conn, "provider_to_receive", 240_000, network_id="4663",
                             provider="rpc", feed_mode="poll_500ms", recorded_at=at)
    db.append_latency_sample(conn, "receive_to_observation", 80_000, network_id="4663",
                             provider="rpc", feed_mode="poll_500ms", recorded_at=at)
    db.append_latency_sample(conn, "observation_to_decision", 20_000, network_id="4663",
                             provider="local", feed_mode="poll_500ms", recorded_at=at)
    data = runtime_health(conn)
    assert data["provider_to_receive_ms"]["p95"] == 240.0
    assert data["receive_to_observation_ms"]["p95"] == 80.0
    assert data["observation_to_decision_ms"]["p99"] == 20.0
    assert data["by_provider"]["rpc"]["provider_to_receive_ms"]["p50"] == 240.0


def test_latency_retention_removes_samples_older_than_24_hours(tmp_path):
    conn = _conn(tmp_path)
    old = (BASE - timedelta(days=2)).isoformat()
    current = BASE.isoformat()
    db.append_latency_sample(conn, "step", 1_000, network_id="solana",
                             provider="local", feed_mode="subscription", recorded_at=old)
    db.append_latency_sample(conn, "step", 2_000, network_id="solana",
                             provider="local", feed_mode="subscription", recorded_at=current)
    rows = conn.execute("SELECT duration_us FROM v6_paper_latency_samples").fetchall()
    assert [row[0] for row in rows] == [2_000]


def test_runtime_health_reports_feed_gap_and_cursor(tmp_path):
    conn = _conn(tmp_path)
    health = V6FeedHealth("subscription", False, True, BASE.isoformat(), 2)
    cursor = V6FeedCursor("solana", "Vault", 123, "event-123")
    db.upsert_feed_health(conn, "solana", "Vault", health, cursor=cursor,
                          accumulator_freshness_s=1.5, updated_at=BASE.isoformat())
    data = runtime_health(conn)
    feed = data["feed_health"]["solana"]["Vault"]
    assert feed["gap"] is True and feed["cursor_height"] == 123
    assert feed["accumulator_freshness_s"] == 1.5
