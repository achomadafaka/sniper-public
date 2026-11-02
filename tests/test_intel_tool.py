import argparse
import json
from datetime import datetime, timedelta, timezone

from agents.tools import intel_tool


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_cmd_events_filters_and_touches_reviewed(monkeypatch, capsys):
    calls = []
    events = [
        {"id": 1, "slug": "geo-old-1", "last_updated": _iso(80), "status": "active"},
        {"id": 2, "slug": "fin:old-1", "last_updated": _iso(90), "status": "active"},
        {"id": 3, "slug": "geo-fresh", "last_updated": _iso(4), "status": "active"},
        {"id": 4, "slug": "geo-old-2", "last_updated": _iso(72.5), "status": "developing"},
    ]

    def fake_request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET" and path.startswith("world_events?"):
            return list(events)
        if method == "PATCH" and path.startswith("world_events?slug=in.("):
            return []
        if method == "GET" and path.startswith("industry_impacts?event_id=eq."):
            return [{"industry": "energy", "impact_direction": "bullish"}]
        raise AssertionError(f"unexpected request: {(method, path, body)}")

    monkeypatch.setattr(intel_tool, "_request", fake_request)
    args = argparse.Namespace(
        status="active,developing",
        limit=2,
        older_than_hours=48.0,
        slug_prefix=None,
        exclude_prefix="fin:",
        touch_reviewed=True,
    )

    intel_tool.cmd_events(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == 2
    assert payload["touched"] == 2
    assert [event["slug"] for event in payload["events"]] == ["geo-old-1", "geo-old-2"]
    assert payload["events"][0]["impact_count"] == 1

    patch_calls = [call for call in calls if call[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert "geo-old-1" in patch_calls[0][1]
    assert "geo-old-2" in patch_calls[0][1]
    assert "fin%3Aold-1" not in patch_calls[0][1]


def test_cmd_events_slug_prefix_filters_finance_review(monkeypatch, capsys):
    events = [
        {"id": 10, "slug": "fin:fed-risk", "last_updated": _iso(60), "status": "active"},
        {"id": 11, "slug": "geo-iran-talks", "last_updated": _iso(70), "status": "active"},
    ]

    def fake_request(method, path, body=None):
        if method == "GET" and path.startswith("world_events?"):
            return list(events)
        if method == "GET" and path.startswith("industry_impacts?event_id=eq."):
            return []
        if method == "PATCH":
            return []
        raise AssertionError(f"unexpected request: {(method, path, body)}")

    monkeypatch.setattr(intel_tool, "_request", fake_request)
    args = argparse.Namespace(
        status="active",
        limit=20,
        older_than_hours=48.0,
        slug_prefix="fin:",
        exclude_prefix=None,
        touch_reviewed=False,
    )

    intel_tool.cmd_events(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["touched"] == 0
    assert payload["events"][0]["slug"] == "fin:fed-risk"
