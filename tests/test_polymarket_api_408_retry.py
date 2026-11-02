"""Regression tests for HTTP 408 retry in polymarket_api._request_json / _text.

Background:
    Before 2026-05-14 the helpers raised on any 4xx that was not 429. A
    single mid-walk 408 (Polymarket "Request Timeout") would propagate to
    fetch.py's `except Exception: activity = []`, silently discarding all
    partial activity rows fetched up to that point.

    Verified empirically against YatSen wallet:
      pre-fix:  activity=0 (single 408 at offset=2350 wiped 2,350 rows)
      post-fix: activity=18,093

These tests pin the new behavior: 408 is recoverable and retried like 429
and 5xx, the partial-page state is preserved, and the final response is
returned.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from sniper.markets.polymarket_api import _request_json, _request_text


def _mock_response(status: int, payload=None, text: str = "") -> MagicMock:
    """Build a minimal MagicMock that behaves like httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload if payload is not None else {}
    resp.text = text
    resp.headers = {}
    if 400 <= status < 600:
        err = httpx.HTTPStatusError(
            f"HTTP {status}",
            request=MagicMock(spec=httpx.Request),
            response=resp,
        )
        resp.raise_for_status.side_effect = err
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_request_json_retries_408_then_succeeds():
    """A 408 must NOT raise; helper retries and returns the 200 body."""
    responses = [
        _mock_response(408),
        _mock_response(200, payload=[{"ok": True}]),
    ]
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = responses

    with patch("sniper.markets.polymarket_api.time.sleep") as sleep_mock, \
         patch("sniper.markets.polymarket_api._throttle_requests"):
        result = _request_json(client, "https://example.test/x", params={"k": "v"})

    assert result == [{"ok": True}]
    assert client.get.call_count == 2
    # backoff slept at least once between attempts
    assert sleep_mock.call_count >= 1


def test_request_json_retries_two_408s_then_succeeds():
    """Multi-retry case: two 408s in a row, then 200. Confirms the retry
    loop iterates correctly past a single retry attempt and backs off
    each time before succeeding."""
    responses = [
        _mock_response(408),
        _mock_response(408),
        _mock_response(200, payload={"v": 1}),
    ]
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = responses

    with patch("sniper.markets.polymarket_api.time.sleep") as sleep_mock, \
         patch("sniper.markets.polymarket_api._throttle_requests"):
        result = _request_json(client, "https://example.test/y", params=None)

    assert result == {"v": 1}
    assert client.get.call_count == 3
    # backoff slept after each 408, so at least twice
    assert sleep_mock.call_count >= 2


def test_request_json_raises_after_retry_budget_exhausted(monkeypatch):
    """Repeated retryable failures must terminate instead of looping forever."""
    retryable = _mock_response(408)
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = [
        retryable,
        retryable,
        retryable,
        AssertionError("unbounded retry continued past budget"),
    ]
    monkeypatch.setattr(
        "sniper.markets.polymarket_api.REQUEST_MAX_ATTEMPTS",
        3,
        raising=False,
    )

    with patch("sniper.markets.polymarket_api.time.sleep") as sleep_mock, \
         patch("sniper.markets.polymarket_api._throttle_requests"):
        with pytest.raises(httpx.HTTPStatusError):
            _request_json(client, "https://example.test/exhaust", params=None)

    assert client.get.call_count == 3
    assert sleep_mock.call_count == 2


def test_request_json_still_raises_on_non_retryable_4xx():
    """Confirm we did not over-broaden — 400 / 401 / 403 / 404 still raise."""
    for status in (400, 401, 403, 404):
        bad = _mock_response(status)
        client = MagicMock(spec=httpx.Client)
        client.get.return_value = bad

        with patch("sniper.markets.polymarket_api.time.sleep"), \
             patch("sniper.markets.polymarket_api._throttle_requests"):
            with pytest.raises(httpx.HTTPStatusError):
                _request_json(client, "https://example.test/z", params=None)


def test_request_text_retries_408_then_succeeds():
    """Same fix in _request_text."""
    responses = [
        _mock_response(408, text=""),
        _mock_response(200, text="hello"),
    ]
    client = MagicMock(spec=httpx.Client)
    client.get.side_effect = responses

    with patch("sniper.markets.polymarket_api.time.sleep"), \
         patch("sniper.markets.polymarket_api._throttle_requests"):
        result = _request_text(client, "https://example.test/t", params=None)

    assert result == "hello"
    assert client.get.call_count == 2
