import copy
import json
import sqlite3
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin_x as X


FIX = Path(__file__).parent / "fixtures" / "memecoin_x" / "api"
NOW = "2026-07-26T12:00:05+00:00"


@dataclass(frozen=True)
class Settings:
    max_post_reads_per_day: int = 500
    recovery_max_pages: int = 10


SETTINGS = Settings()


def _load(name="direct.json"):
    return json.loads((FIX / name).read_text())


def _conn(frontier=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    conn.execute(
        "INSERT INTO memecoin_x_accounts(handle,display_handle,x_user_id,role,active) "
        "VALUES ('alpha','Alpha','1001','tier1_trader',1)")
    if frontier:
        state = X.api_capture_state(conn)
        X.update_api_capture_state(
            conn, expected_revision=state["revision"], frontier_post_id=frontier,
            updated_at=NOW)
    conn.commit()
    return conn


def _clock():
    return NOW


def test_stream_event_commits_event_outbox_resource_and_frontier_atomically():
    from sniper.services.memecoin_x_api_capture import ingest_payload

    conn = _conn()
    result = ingest_payload(
        conn, _load(), source="stream", settings=SETTINGS, observed_at_fn=_clock)
    assert result["events_created"] == 1
    assert X.api_capture_state(conn)["frontier_post_id"] == "101"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_notification_outbox "
        "WHERE channel='telegram' AND state='pending'").fetchone()[0] == 1
    assert X.api_resource_count(conn, utc_day="2026-07-26") == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()[0] == 1


def test_persistence_failure_does_not_advance_frontier(monkeypatch):
    from sniper.services.memecoin_x_api_capture import ingest_payload

    conn = _conn()

    def fail(*_args, **_kwargs):
        raise RuntimeError("injected")

    monkeypatch.setattr(X, "persist_signal_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        ingest_payload(
            conn, _load(), source="stream", settings=SETTINGS, observed_at_fn=_clock)
    assert X.api_capture_state(conn)["frontier_post_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_signal_events").fetchone()[0] == 0
    assert X.api_resource_count(conn, utc_day="2026-07-26") == 0


class RecentClient:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.calls = []

    def recent_search(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.pages)


def _page(ids, *, next_token=None, newest_id=None):
    base = _load()
    data = []
    for post_id in ids:
        post = copy.deepcopy(base["data"])
        post["id"] = str(post_id)
        post["edit_history_tweet_ids"] = [str(post_id)]
        data.append(post)
    meta = {"result_count": len(data)}
    if next_token:
        meta["next_token"] = next_token
    if newest_id:
        meta["newest_id"] = str(newest_id)
    return {"data": data, "includes": base["includes"], "meta": meta}


def test_bounded_recovery_resumes_without_advancing_frontier_until_complete():
    from sniper.services.memecoin_x_api_capture import run_recovery

    conn = _conn(frontier="100")
    client = RecentClient([
        _page([105, 104], next_token="page-2", newest_id="105"),
        _page([103, 102, 101], newest_id="105"),
    ])
    first = run_recovery(
        conn, client, settings=replace(SETTINGS, recovery_max_pages=1),
        observed_at_fn=_clock)
    assert first["complete"] is False
    assert X.api_capture_state(conn)["frontier_post_id"] == "100"
    assert X.api_capture_state(conn)["recovery_next_token"] == "page-2"
    second = run_recovery(
        conn, client, settings=replace(SETTINGS, recovery_max_pages=1),
        observed_at_fn=_clock)
    assert second["complete"] is True
    assert X.api_capture_state(conn)["frontier_post_id"] == "105"
    assert X.api_capture_state(conn)["recovery_next_token"] is None
    assert [row[0] for row in conn.execute(
        "SELECT post_id FROM memecoin_x_signal_events ORDER BY id")] == ["104", "105", "101", "102", "103"]


class StreamClient:
    def __init__(self, payloads):
        self.payloads = payloads

    def stream(self):
        yield from self.payloads


def test_resource_cap_stops_after_resource_that_reaches_cap():
    from sniper.services.memecoin_x_api_capture import run_stream

    conn = _conn()
    second = _load()
    second["data"]["id"] = "102"
    second["data"]["edit_history_tweet_ids"] = ["102"]
    result = run_stream(
        conn, StreamClient([_load(), second]),
        settings=replace(SETTINGS, max_post_reads_per_day=1),
        observed_at_fn=_clock, stop_event=threading.Event())
    assert result["state"] == "budget_exhausted"
    assert result["payloads_consumed"] == 1
    assert X.api_capture_state(conn)["frontier_post_id"] == "101"


def test_stream_recovery_overlap_is_idempotent():
    from sniper.services.memecoin_x_api_capture import ingest_payload

    conn = _conn()
    ingest_payload(conn, _load(), source="stream", settings=SETTINGS, observed_at_fn=_clock)
    ingest_payload(conn, _load(), source="recovery", settings=SETTINGS,
                   observed_at_fn=_clock, advance_frontier=False)
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_signal_events").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_notification_outbox WHERE channel='telegram'").fetchone()[0] == 1


def test_second_distinct_actor_creates_one_consensus_alert_not_one_per_later_actor():
    from sniper.services.memecoin_x_api_capture import ingest_payload

    conn = _conn()
    ingest_payload(conn, _load(), source="stream", settings=SETTINGS, observed_at_fn=_clock)
    for user_id, handle, post_id in (("1002", "beta", "102"), ("1003", "gamma", "103")):
        conn.execute(
            "INSERT INTO memecoin_x_accounts(handle,display_handle,x_user_id,role,active) "
            "VALUES (?,?,?,?,1)", (handle, handle.title(), user_id, "tier1_trader"))
        conn.commit()
        payload = _load()
        payload["data"]["id"] = post_id
        payload["data"]["author_id"] = user_id
        payload["data"]["edit_history_tweet_ids"] = [post_id]
        payload["includes"]["users"] = [{
            "id": user_id, "username": handle, "name": handle.title(),
            "public_metrics": {},
        }]
        ingest_payload(
            conn, payload, source="stream", settings=SETTINGS, observed_at_fn=_clock)
    consensus = conn.execute(
        "SELECT ticker,consensus_count,actor_handles_json FROM memecoin_x_signal_events "
        "WHERE event_type='cashtag_consensus'").fetchall()
    assert len(consensus) == 1
    assert consensus[0]["ticker"] == "MEME"
    assert consensus[0]["consensus_count"] == 3
    assert json.loads(consensus[0]["actor_handles_json"]) == ["alpha", "beta", "gamma"]
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_notification_outbox o "
        "JOIN memecoin_x_signal_events e ON e.id=o.signal_event_id "
        "WHERE o.channel='telegram' AND e.event_type='cashtag_consensus'").fetchone()[0] == 1
