"""Acceptance tests for review-patch fixes:
  - fetch_user_activity emits fetch_progress per page (Important #1)
  - fetch_wallet_data stores real activity termination reason (Important #2,
    via emitter side-channel in fetch.py)

Tests do not touch:
  tests/test_wallet_protocol_fetch_progress_emitters.py
  tests/test_wallet_protocol_time_coverage.py
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

class _ListEmitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_name: str, payload: dict) -> None:
        self.events.append((event_name, payload))

    def payloads_for(self, name: str) -> list[dict]:
        return [p for e, p in self.events if e == name]


def _activity_rows(n: int, base_ts: int = 1_700_000_000) -> list[dict]:
    return [
        {
            "conditionId": f"cid_{i}",
            "timestamp": base_ts - i,
            "transactionHash": f"tx_{i}",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Important #1 — fetch_progress emitted per page for activity endpoint
# ---------------------------------------------------------------------------

def test_fetch_user_activity_emits_fetch_progress_per_page():
    """At least 1 fetch_progress event with endpoint='activity' must be emitted."""
    from sniper.markets.polymarket_api import fetch_user_activity

    emitter = _ListEmitter()
    call_n = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_n[0]
        call_n[0] += 1
        return _activity_rows(20) if n == 0 else []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", progress_emitter=emitter)

    fp_events = emitter.payloads_for("fetch_progress")
    assert len(fp_events) >= 1, \
        f"Expected >=1 fetch_progress events from activity fetch, got {len(fp_events)}"
    assert fp_events[0]["endpoint"] == "activity", \
        f"endpoint in fetch_progress should be 'activity', got {fp_events[0]['endpoint']!r}"


def test_fetch_user_activity_fetch_progress_payload_shape():
    """fetch_progress from activity must carry {endpoint, n_rows, offset, page, elapsed_s}."""
    from sniper.markets.polymarket_api import fetch_user_activity

    emitter = _ListEmitter()
    call_n = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_n[0]
        call_n[0] += 1
        return _activity_rows(10) if n == 0 else []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", progress_emitter=emitter)

    fp_events = emitter.payloads_for("fetch_progress")
    assert fp_events, "No fetch_progress events emitted"
    payload = fp_events[0]
    required = {"endpoint", "n_rows", "offset", "page", "elapsed_s"}
    missing = required - set(payload.keys())
    assert not missing, f"fetch_progress payload missing keys: {missing}"
    assert isinstance(payload["n_rows"], int)
    assert isinstance(payload["offset"], int)
    assert isinstance(payload["page"], int)
    assert isinstance(payload["elapsed_s"], float)


def test_fetch_user_activity_fetch_progress_cumulative_n_rows_grows_monotonically():
    """n_rows in consecutive fetch_progress events must be non-decreasing."""
    from sniper.markets.polymarket_api import fetch_user_activity

    emitter = _ListEmitter()
    call_n = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_n[0]
        call_n[0] += 1
        if n < 3:
            return _activity_rows(50, base_ts=1_700_000_000 - n * 50)
        return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", page_limit=50, progress_emitter=emitter)

    fp_events = emitter.payloads_for("fetch_progress")
    assert len(fp_events) >= 2, f"Need >=2 fetch_progress events, got {len(fp_events)}"
    counts = [p["n_rows"] for p in fp_events]
    for i in range(1, len(counts)):
        assert counts[i] >= counts[i - 1], \
            f"n_rows decreased at event {i}: {counts[i-1]} -> {counts[i]}"


# ---------------------------------------------------------------------------
# Important #2 — real termination reason flows into data.fetch_termination
# via emitter side-channel in fetch.py
# ---------------------------------------------------------------------------

def test_walletdata_fetch_termination_activity_records_partial_page_reason():
    """When activity ends on a short page, fetch_termination['activity'] == 'partial_page'."""
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    from sniper.markets.polymarket_api import FetchTermination

    # Patch the positions fetchers to return nothing, then let the real
    # fetch_user_activity run with a fake _request_json that returns a
    # partial page, so the real emitter fires with reason='partial_page'.
    call_n = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_n[0]
        call_n[0] += 1
        # Single partial page (< page_limit=50) → partial_page termination
        return _activity_rows(10) if n == 0 else []

    with patch("sniper.analysis.wallet_protocol.fetch.polymarket_api") as mock_api, \
         patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        mock_api.fetch_closed_positions.return_value = []
        mock_api.fetch_user_positions.return_value = (
            [], FetchTermination("empty_page", offset=0)
        )
        # Let fetch_user_activity be the real function
        import sniper.markets.polymarket_api as _real_api
        mock_api.fetch_user_activity.side_effect = _real_api.fetch_user_activity
        mock_api.fetch_user_value.return_value = 0.0

        data = fetch_wallet_data("0xabc")

    assert data.fetch_termination is not None
    assert data.fetch_termination.get("activity") == "partial_page", \
        f"Expected 'partial_page', got {data.fetch_termination.get('activity')!r}"


def test_walletdata_fetch_termination_activity_records_api_refused_reason():
    """When activity hits a 4xx, fetch_termination['activity'] == 'api_refused'."""
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    from sniper.markets.polymarket_api import FetchTermination
    import httpx

    call_n = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        # Raise HTTPStatusError(400) → api_refused termination
        raise httpx.HTTPStatusError(
            "400 Bad Request",
            request=MagicMock(),
            response=MagicMock(status_code=400),
        )

    with patch("sniper.analysis.wallet_protocol.fetch.polymarket_api") as mock_api, \
         patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        mock_api.fetch_closed_positions.return_value = []
        mock_api.fetch_user_positions.return_value = (
            [], FetchTermination("empty_page", offset=0)
        )
        import sniper.markets.polymarket_api as _real_api
        mock_api.fetch_user_activity.side_effect = _real_api.fetch_user_activity
        mock_api.fetch_user_value.return_value = 0.0

        data = fetch_wallet_data("0xabc")

    assert data.fetch_termination is not None
    assert data.fetch_termination.get("activity") == "api_refused", \
        f"Expected 'api_refused', got {data.fetch_termination.get('activity')!r}"


def test_walletdata_fetch_termination_activity_records_empty_page_reason():
    """When activity ends on an empty page, fetch_termination['activity'] == 'empty_page'."""
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    from sniper.markets.polymarket_api import FetchTermination

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        return []  # immediate empty page

    with patch("sniper.analysis.wallet_protocol.fetch.polymarket_api") as mock_api, \
         patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        mock_api.fetch_closed_positions.return_value = []
        mock_api.fetch_user_positions.return_value = (
            [], FetchTermination("empty_page", offset=0)
        )
        import sniper.markets.polymarket_api as _real_api
        mock_api.fetch_user_activity.side_effect = _real_api.fetch_user_activity
        mock_api.fetch_user_value.return_value = 0.0

        data = fetch_wallet_data("0xabc")

    assert data.fetch_termination is not None
    assert data.fetch_termination.get("activity") == "empty_page", \
        f"Expected 'empty_page', got {data.fetch_termination.get('activity')!r}"
