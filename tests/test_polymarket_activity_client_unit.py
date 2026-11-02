"""Unit tests for data.polymarket_activity_client — no network, all mocked.

Covers paths that live integration tests can't exercise:
- 5xx retry exhaustion
- 4xx non-retry (with body preview in error)
- Malformed JSON
- ActivityRow.from_dict edge cases (missing / invalid fields)
- iter_activity termination edge cases
- get_positions truncation detection

Runs in CI by default (not `integration`-marked).
"""
from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from data.polymarket_activity_client import (
    ActivityRow,
    PolymarketActivityError,
    _get_json,
    get_activity_page,
    get_positions,
    iter_activity,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_ok_response(payload):
    """Return a MagicMock resembling a successful urllib response."""
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    # Support context manager protocol.
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _mock_http_error(code, body="error body"):
    """Return an HTTPError exception with body readable."""
    return urllib.error.HTTPError(
        url="https://example/x",
        code=code,
        msg="Error",
        hdrs={},
        fp=io.BytesIO(body.encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# _get_json retry behavior
# ---------------------------------------------------------------------------

class TestGetJsonRetry:
    def test_5xx_retries_then_raises(self):
        # 3 attempts total (initial + 2 retry backoffs). All 500.
        err = _mock_http_error(503)
        with patch("data.polymarket_activity_client.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=[err, err, err]) as m_open:
            with pytest.raises(PolymarketActivityError, match="failed after retries"):
                _get_json("/x")
        assert m_open.call_count == 3

    def test_4xx_raises_immediately_with_body_preview(self):
        err = _mock_http_error(403, body="forbidden-reason-abc")
        with patch("data.polymarket_activity_client.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=err) as m_open:
            with pytest.raises(PolymarketActivityError, match="forbidden-reason-abc"):
                _get_json("/x")
        assert m_open.call_count == 1  # no retry on 4xx

    def test_5xx_then_success_returns_payload(self):
        err = _mock_http_error(502)
        ok = _mock_ok_response([{"a": 1}])
        with patch("data.polymarket_activity_client.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=[err, ok]):
            result = _get_json("/x")
        assert result == [{"a": 1}]

    def test_timeout_retries(self):
        with patch("data.polymarket_activity_client.time.sleep"), \
             patch("urllib.request.urlopen", side_effect=TimeoutError("t")) as m_open:
            with pytest.raises(PolymarketActivityError, match="failed after retries"):
                _get_json("/x")
        assert m_open.call_count == 3

    def test_malformed_json_raises(self):
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = b"not json {{"
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp):
            with pytest.raises(PolymarketActivityError, match="invalid JSON"):
                _get_json("/x")


# ---------------------------------------------------------------------------
# ActivityRow.from_dict parsing
# ---------------------------------------------------------------------------

_VALID_TRADE = {
    "proxyWallet": "0xabc",
    "timestamp": 1700000000,
    "conditionId": "0xcond",
    "type": "TRADE",
    "side": "BUY",
    "size": "5.87",
    "usdcSize": "3.17",
    "price": "0.54",
    "outcome": "Yes",
    "outcomeIndex": 0,
    "slug": "slug",
    "title": "title",
    "eventSlug": "event",
    "transactionHash": "0xtx",
    "asset": "0xasset",
}


class TestFromDict:
    def test_trade_row_parses(self):
        r = ActivityRow.from_dict(_VALID_TRADE)
        assert r.activity_type == "TRADE"
        assert r.side == "BUY"
        assert r.size == pytest.approx(5.87)
        assert r.timestamp == 1700000000
        assert r.condition_id == "0xcond"

    def test_missing_timestamp_raises(self):
        bad = dict(_VALID_TRADE); del bad["timestamp"]
        with pytest.raises(PolymarketActivityError, match="missing timestamp"):
            ActivityRow.from_dict(bad)

    def test_invalid_timestamp_raises(self):
        bad = dict(_VALID_TRADE); bad["timestamp"] = "not-a-number"
        with pytest.raises(PolymarketActivityError, match="invalid timestamp"):
            ActivityRow.from_dict(bad)

    def test_trade_missing_condition_id_raises(self):
        bad = dict(_VALID_TRADE); bad["conditionId"] = ""
        with pytest.raises(PolymarketActivityError, match="missing conditionId"):
            ActivityRow.from_dict(bad)

    def test_reward_without_condition_id_ok(self):
        # REWARD rows legitimately have no conditionId (probed 2026-04-21).
        r = {**_VALID_TRADE, "type": "REWARD", "conditionId": "", "side": None}
        row = ActivityRow.from_dict(r)
        assert row.activity_type == "REWARD"
        assert row.condition_id == ""
        assert row.side is None  # side is None for non-TRADE types

    def test_redeem_missing_condition_id_raises(self):
        bad = dict(_VALID_TRADE); bad["type"] = "REDEEM"; bad["conditionId"] = ""
        with pytest.raises(PolymarketActivityError, match="missing conditionId"):
            ActivityRow.from_dict(bad)

    def test_invalid_side_falls_back_to_none(self):
        # Side is free-text from API; only BUY/SELL are accepted, anything else → None.
        r = {**_VALID_TRADE, "side": "WEIRD"}
        row = ActivityRow.from_dict(r)
        assert row.side is None

    def test_bad_numeric_fields_become_none(self):
        # size/price/usdc_size should silently become None on bad data (downstream handles).
        r = {**_VALID_TRADE, "size": "not-a-number", "price": None, "usdcSize": "NaN"}
        row = ActivityRow.from_dict(r)
        assert row.size is None
        assert row.price is None
        # "NaN" float-casts successfully; nothing to assert, just don't raise
        assert row.usdc_size != row.usdc_size or row.usdc_size is None  # NaN check

    def test_raw_is_defensive_copy(self):
        orig = dict(_VALID_TRADE)
        row = ActivityRow.from_dict(orig)
        row.raw["poisoned"] = True
        assert "poisoned" not in orig, "mutation leaked back to original dict"


# ---------------------------------------------------------------------------
# iter_activity termination
# ---------------------------------------------------------------------------

def _make_page(timestamps):
    """Build a list of valid activity dicts at the given timestamps."""
    return [
        {**_VALID_TRADE, "timestamp": ts, "conditionId": f"0xcond_{ts}"}
        for ts in timestamps
    ]


class TestIterActivityTermination:
    def test_stops_on_empty_page(self):
        with patch(
            "data.polymarket_activity_client.get_activity_page",
            side_effect=[
                [ActivityRow.from_dict(r) for r in _make_page([1000, 999, 998])],
                [],  # true end
            ],
        ):
            rows = list(iter_activity("0xw"))
        assert len(rows) == 3

    def test_stops_on_partial_page(self):
        # Page with < PAGE_LIMIT_CAP rows → end of history.
        short_page = [ActivityRow.from_dict(r) for r in _make_page([5, 4, 3])]
        with patch(
            "data.polymarket_activity_client.get_activity_page",
            return_value=short_page,
        ):
            rows = list(iter_activity("0xw"))
        assert len(rows) == 3

    def test_since_ts_skips_rows_but_does_not_terminate_on_first(self):
        # Intra-page out-of-order: newer row after older. Naive early-return would
        # stop at first row < cutoff (ts=900) and miss ts=1100.
        pages = [
            [ActivityRow.from_dict(r) for r in _make_page([1000, 900, 1100])],
            [],
        ]
        with patch(
            "data.polymarket_activity_client.get_activity_page",
            side_effect=pages,
        ):
            rows = list(iter_activity("0xw", since_ts=950))
        yielded_ts = sorted(r.timestamp for r in rows)
        assert yielded_ts == [1000, 1100]  # 900 skipped (but not early-terminated)

    def test_since_ts_terminates_when_entire_page_pre_cutoff(self):
        pages = [
            [ActivityRow.from_dict(r) for r in _make_page([1000, 999])],  # all below cutoff
            [ActivityRow.from_dict(r) for r in _make_page([800, 700])],  # shouldn't fetch
        ]
        mock_gap = MagicMock(side_effect=pages)
        with patch(
            "data.polymarket_activity_client.get_activity_page",
            mock_gap,
        ):
            rows = list(iter_activity("0xw", since_ts=2000))
        assert rows == []
        # Critically: only one page fetched, not both — early stop worked.
        assert mock_gap.call_count == 1


# ---------------------------------------------------------------------------
# get_positions truncation detection
# ---------------------------------------------------------------------------

class TestGetPositionsTruncation:
    def test_raises_when_response_equals_limit(self):
        # Exactly `limit` rows → suspected truncation.
        fake_positions = [{"x": i} for i in range(500)]
        with patch(
            "data.polymarket_activity_client._get_json",
            return_value=fake_positions,
        ):
            with pytest.raises(PolymarketActivityError, match="suspected truncation"):
                get_positions("0xw", limit=500)

    def test_returns_when_response_below_limit(self):
        fake_positions = [{"x": i} for i in range(10)]
        with patch(
            "data.polymarket_activity_client._get_json",
            return_value=fake_positions,
        ):
            result = get_positions("0xw", limit=500)
        assert len(result) == 10

    def test_raises_on_non_list_response(self):
        with patch(
            "data.polymarket_activity_client._get_json",
            return_value={"error": "x"},
        ):
            with pytest.raises(PolymarketActivityError, match="unexpected response shape"):
                get_positions("0xw")


# ---------------------------------------------------------------------------
# get_activity_page shape validation
# ---------------------------------------------------------------------------

class TestGetActivityPage:
    def test_non_list_response_raises(self):
        with patch(
            "data.polymarket_activity_client._get_json",
            return_value={"error": "x"},
        ):
            with pytest.raises(PolymarketActivityError, match="unexpected response shape"):
                get_activity_page("0xw")

    def test_empty_list_returns_empty(self):
        with patch(
            "data.polymarket_activity_client._get_json",
            return_value=[],
        ):
            assert get_activity_page("0xw") == []
