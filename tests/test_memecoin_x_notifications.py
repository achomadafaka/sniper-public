from pathlib import Path
import sqlite3

from sniper.persistence import db_memecoin_x as X


NOW = "2026-07-14T00:00:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    event_id = X.persist_signal_event(conn, {
        "logical_key": "tier2_scout:4663:mint:1", "event_type": "tier2_scout",
        "network_id": "4663", "mint": "0x1111111111111111111111111111111111111111",
        "triggered_at": NOW, "source_handle": "poptyedev",
        "evidence_observation_ids": [1], "actor_handles": ["zinceth"],
        "relationship_counts": {"repost": 1},
    }, created_at=NOW)
    X.enqueue_signal_notification(
        conn, logical_key="telegram:tier2_scout:4663:mint:1",
        signal_event_id=event_id, channel="telegram", created_at=NOW)
    X.enqueue_signal_notification(
        conn, logical_key="dashboard:tier2_scout:4663:mint:1",
        signal_event_id=event_id, channel="dashboard", created_at=NOW)
    return conn


def test_disabled_mode_calls_nothing_and_mutates_nothing():
    from sniper.services.memecoin_x_notifications import deliver_pending

    conn = _conn()
    calls = []
    result = deliver_pending(
        conn, sender=lambda text: calls.append(text), observed_at=NOW, enabled=False)
    assert result == {"enabled": False, "selected": 0, "sent": 0, "failed": 0}
    assert calls == []
    assert [tuple(row) for row in conn.execute(
        "SELECT channel,state,attempts FROM memecoin_x_notification_outbox ORDER BY id")] == [
            ("telegram", "pending", 0), ("dashboard", "pending", 0)]


def test_dry_run_preview_renders_pending_telegram_without_mutation():
    from sniper.services.memecoin_x_notifications import preview_pending

    conn = _conn()
    before = [tuple(row) for row in conn.execute(
        "SELECT id,state,attempts FROM memecoin_x_notification_outbox ORDER BY id")]
    previews = preview_pending(conn, limit=20)
    assert len(previews) == 1
    assert previews[0]["logical_key"] == "telegram:tier2_scout:4663:mint:1"
    assert "display-only" in previews[0]["text"].lower()
    assert [tuple(row) for row in conn.execute(
        "SELECT id,state,attempts FROM memecoin_x_notification_outbox ORDER BY id")] == before


def test_success_sends_once_and_deduped_rerun_does_not_call_sender():
    from sniper.services.memecoin_x_notifications import deliver_pending

    conn = _conn()
    calls = []
    sender = lambda text: (calls.append(text) or (True, 12.5, None))
    first = deliver_pending(conn, sender=sender, observed_at=NOW, enabled=True)
    second = deliver_pending(conn, sender=sender, observed_at=NOW, enabled=True)
    assert first == {"enabled": True, "selected": 1, "sent": 1, "failed": 0}
    assert second == {"enabled": True, "selected": 0, "sent": 0, "failed": 0}
    assert len(calls) == 1
    row = conn.execute(
        "SELECT state,attempts,sent_at FROM memecoin_x_notification_outbox WHERE channel='telegram'"
    ).fetchone()
    assert tuple(row) == ("sent", 1, NOW)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_notification_attempts").fetchone()[0] == 1


def test_failures_stay_pending_until_third_attempt_then_stop():
    from sniper.services.memecoin_x_notifications import deliver_pending

    conn = _conn()
    sender = lambda text: (False, 4.0, "provider unavailable")
    for expected_state, expected_attempts in (("pending", 1), ("pending", 2), ("failed", 3)):
        result = deliver_pending(conn, sender=sender, observed_at=NOW, enabled=True)
        assert result["failed"] == 1
        row = conn.execute(
            "SELECT state,attempts FROM memecoin_x_notification_outbox WHERE channel='telegram'"
        ).fetchone()
        assert tuple(row) == (expected_state, expected_attempts)
    assert deliver_pending(conn, sender=sender, observed_at=NOW, enabled=True)["selected"] == 0


def test_sender_exception_is_bounded_and_audited():
    from sniper.services.memecoin_x_notifications import deliver_pending

    conn = _conn()
    def broken(_text):
        raise RuntimeError("x" * 900)
    deliver_pending(conn, sender=broken, observed_at=NOW, enabled=True)
    row = conn.execute(
        "SELECT bounded_error FROM memecoin_x_notification_attempts").fetchone()
    assert len(row[0]) == 500


def test_telegram_transport_error_never_returns_bot_token(monkeypatch):
    from scripts import run_memecoin_x_notifications as cli

    token = "super-secret-bot-token"

    def fail(_request, timeout):
        assert timeout == 15
        raise RuntimeError(f"transport failed for bot{token}")

    monkeypatch.setattr(cli.request, "urlopen", fail)
    ok, _latency_ms, error = cli._telegram_sender(token, "chat-id")("test")
    assert ok is False
    assert token not in error
    assert error == "RuntimeError"


def test_notification_is_cited_display_only_observation_language():
    from sniper.services.memecoin_x_notifications import render_notification

    text = render_notification({
        "event_type": "fast_consensus", "source_handle": "poptyedev",
        "network_id": "4663", "mint": "0x1111111111111111111111111111111111111111",
        "triggered_at": NOW, "actor_handles": ["zinceth", "goyimpnl"],
    }, {"logical_key": "fast_consensus:4663:mint:2"})
    lowered = text.lower()
    assert "observed correlation" in lowered and "display-only" in lowered
    assert "robinhood 4663" in lowered and "fast_consensus:4663:mint:2" in text
    for forbidden in ("alpha", "entry", " buy", "safe", "veto", "size", "return"):
        assert forbidden not in lowered


def test_worker_source_has_no_cross_lane_queries_or_imports():
    source = (Path(__file__).resolve().parents[1] /
              "sniper/services/memecoin_x_notifications.py").read_text().lower()
    for forbidden in (
        "memecoin_positions", "real_trader", "trade_executor", "funded_runner",
        "manual_buy", "api_memecoin",
    ):
        assert forbidden not in source


def test_cashtag_alert_contains_actor_activity_identity_and_exact_post_link():
    from sniper.services.memecoin_x_notifications import render_notification

    text = render_notification({
        "event_type": "cashtag_mention", "source_handle": "alpha",
        "ticker": "MEME", "identity_state": "ticker_only",
        "relationship": "direct", "cashtag_source": "actor_text",
        "excerpt": "Watching $MEME, not endorsing it.",
        "post_url": "https://x.com/alpha/status/101",
        "triggered_at": "2026-07-26T12:00:05+00:00", "consensus_count": 1,
        "details": {"cashtags": ["MEME", "OTHER"]},
    }, {"logical_key": "x-api:1001:101:direct:MEME,OTHER"})
    assert text.startswith("X CASHTAG — @alpha · DIRECT")
    assert "$MEME $OTHER" in text
    assert "mentioned in actor text" in text
    assert "Identity: ticker only" in text
    assert "Open Post: https://x.com/alpha/status/101" in text
    assert "promotion" not in text.lower()
