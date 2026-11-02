"""Failing acceptance tests for A.7 — time_coverage metadata on ProtocolReport.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §A.7,
      acceptance criteria 23-24.

These tests target the NOT-YET-IMPLEMENTED fields:
  - ProtocolReport.time_coverage: dict | None (new field, default None)
  - WalletData.fetch_termination: dict | None (or however the plumbing is done)
  - analyze_wallet populates time_coverage from WalletData

Current state on main (ede8e1d):
  - ProtocolReport has NO time_coverage field.
  - WalletData has NO fetch_termination field.
  - analyze_wallet does NOT compute or assign time_coverage.

All tests except the last two baseline checks must fail until implementation.
"""
from __future__ import annotations

import time
from dataclasses import fields as dc_fields

import pytest

from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


# ---------------------------------------------------------------------------
# A.7.1 — ProtocolReport.time_coverage field exists and defaults to None
# ---------------------------------------------------------------------------

def test_protocol_report_has_time_coverage_field():
    """ProtocolReport must have a time_coverage: dict | None field defaulting to None.

    Fails with AttributeError on main because the field doesn't exist yet.
    """
    report = ProtocolReport(wallet="0xtest")
    # Will raise AttributeError if field doesn't exist
    assert hasattr(report, "time_coverage"), \
        "ProtocolReport missing time_coverage field (spec A.7)"


def test_protocol_report_time_coverage_defaults_to_none():
    """time_coverage must default to None when not explicitly set."""
    report = ProtocolReport(wallet="0xtest")
    assert report.time_coverage is None, \
        f"Expected time_coverage=None by default, got {report.time_coverage!r}"


# ---------------------------------------------------------------------------
# A.7.2 — time_coverage populated from closed_positions timestamps
# ---------------------------------------------------------------------------

def _make_wallet_data(
    closed_positions: list[dict],
    open_positions: list[dict] | None = None,
    activity_rows: list[dict] | None = None,
    fetch_termination: dict | None = None,
) -> WalletData:
    """Minimal WalletData for testing time_coverage computation."""
    return WalletData(
        wallet="0xtest",
        closed_positions=closed_positions,
        open_positions=open_positions or [],
        activity_rows=activity_rows or [],
        activity_cap_hit=False,
        archetype_layer1="speculator",
        execution_profile=None,
        fetched_at=int(time.time()),
        fetch_termination=fetch_termination,
    )


def test_time_coverage_none_when_no_closed_positions():
    """time_coverage must be None when closed_positions == [].

    Spec: 'closed_positions == [] → time_coverage = None (no temporal extent)'
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    data = _make_wallet_data(closed_positions=[])
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is None, \
        f"Expected time_coverage=None when no closed positions, got {report.time_coverage!r}"


def test_time_coverage_first_position_ts_is_min_timestamp():
    """first_position_ts must equal min(p['timestamp'] for p in closed_positions)."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_oldest = 1700000000
    ts_newest = 1705000000
    closed = [
        {"conditionId": "cid_a", "timestamp": ts_newest, "realizedPnl": 100.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 100, "outcome": "Yes"},
        {"conditionId": "cid_b", "timestamp": ts_oldest, "realizedPnl": -50.0,
         "avgPrice": 0.6, "curPrice": 0.0, "size": 80, "outcome": "Yes"},
    ]
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None, "time_coverage must be populated"
    assert report.time_coverage["first_position_ts"] == ts_oldest, \
        f"first_position_ts should be {ts_oldest}, got {report.time_coverage['first_position_ts']}"


def test_time_coverage_last_position_ts_is_max_timestamp():
    """last_position_ts must equal max(p['timestamp'] for p in closed_positions)."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_oldest = 1700000000
    ts_newest = 1705000000
    closed = [
        {"conditionId": "cid_a", "timestamp": ts_newest, "realizedPnl": 100.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 100, "outcome": "Yes"},
        {"conditionId": "cid_b", "timestamp": ts_oldest, "realizedPnl": -50.0,
         "avgPrice": 0.6, "curPrice": 0.0, "size": 80, "outcome": "Yes"},
    ]
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    assert report.time_coverage["last_position_ts"] == ts_newest, \
        f"last_position_ts should be {ts_newest}, got {report.time_coverage['last_position_ts']}"


def test_time_coverage_duration_days_computed_correctly():
    """duration_days must be (last - first) / 86400, rounded (int or float).

    Spec: 'duration_days: int'
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_first = 1700000000
    # 60 days later
    ts_last = ts_first + 60 * 86400
    closed = [
        {"conditionId": "cid_a", "timestamp": ts_last, "realizedPnl": 10.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"},
        {"conditionId": "cid_b", "timestamp": ts_first, "realizedPnl": -5.0,
         "avgPrice": 0.6, "curPrice": 0.0, "size": 8, "outcome": "Yes"},
    ]
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    assert report.time_coverage["duration_days"] == 60, \
        f"Expected duration_days=60, got {report.time_coverage['duration_days']}"


def test_time_coverage_n_positions_per_month_computed():
    """n_positions_per_month == len(closed) / (duration_days / 30.0) when duration > 0."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_first = 1700000000
    ts_last = ts_first + 90 * 86400  # 90 days = 3 months
    closed = [
        {"conditionId": f"cid_{i}", "timestamp": ts_first + i * 86400,
         "realizedPnl": 5.0, "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"}
        for i in range(30)  # 30 positions over 90 days = 10/month
    ]
    # Override first/last to be exact
    closed[0]["timestamp"] = ts_first
    closed[-1]["timestamp"] = ts_last
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    n_per_month = report.time_coverage["n_positions_per_month"]
    assert n_per_month is not None, "n_positions_per_month must be populated when duration > 0"
    # 30 positions / 3.0 months ≈ 10.0
    expected = 30.0 / (90.0 / 30.0)
    assert abs(n_per_month - expected) < 0.1, \
        f"n_positions_per_month expected ~{expected:.1f}, got {n_per_month}"


def test_time_coverage_n_positions_per_month_zero_duration():
    """n_positions_per_month is None (or 0.0) when duration_days == 0.

    Spec: 'n_positions_per_month is None (or 0.0 — implementer's choice,
    document the choice) when duration_days == 0'.
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts = 1700000000
    # All same timestamp → duration = 0
    closed = [
        {"conditionId": "cid_a", "timestamp": ts, "realizedPnl": 10.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"},
    ]
    data = _make_wallet_data(closed_positions=closed)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    n_per_month = report.time_coverage["n_positions_per_month"]
    assert n_per_month is None or n_per_month == 0.0, \
        f"n_positions_per_month should be None or 0.0 when duration=0, got {n_per_month!r}"


# ---------------------------------------------------------------------------
# A.7.3 — first_fill_ts / last_fill_ts from activity_rows
# ---------------------------------------------------------------------------

def test_time_coverage_fill_ts_from_activity_rows():
    """first_fill_ts and last_fill_ts must come from activity_rows timestamps."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_first_fill = 1699000000
    ts_last_fill = 1704000000
    closed = [
        {"conditionId": "cid_a", "timestamp": 1700000000, "realizedPnl": 10.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"},
    ]
    activity = [
        {"conditionId": "cid_a", "timestamp": ts_last_fill, "transactionHash": "tx1"},
        {"conditionId": "cid_a", "timestamp": ts_first_fill, "transactionHash": "tx2"},
    ]
    data = _make_wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    assert report.time_coverage["first_fill_ts"] == ts_first_fill, \
        f"first_fill_ts expected {ts_first_fill}, got {report.time_coverage['first_fill_ts']}"
    assert report.time_coverage["last_fill_ts"] == ts_last_fill, \
        f"last_fill_ts expected {ts_last_fill}, got {report.time_coverage['last_fill_ts']}"


def test_time_coverage_fill_ts_none_when_no_activity():
    """first_fill_ts and last_fill_ts must be None when activity_rows is empty.

    Spec: 'activity_rows == [] → first_fill_ts / last_fill_ts are None;
    closed-position fields still populated if closed_positions present'
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    closed = [
        {"conditionId": "cid_a", "timestamp": 1700000000, "realizedPnl": 10.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"},
    ]
    data = _make_wallet_data(closed_positions=closed, activity_rows=[])
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    assert report.time_coverage["first_fill_ts"] is None, \
        f"first_fill_ts should be None when no activity, got {report.time_coverage['first_fill_ts']}"
    assert report.time_coverage["last_fill_ts"] is None, \
        f"last_fill_ts should be None when no activity, got {report.time_coverage['last_fill_ts']}"
    # Closed position fields still populated
    assert "first_position_ts" in report.time_coverage
    assert report.time_coverage["first_position_ts"] == 1700000000


# ---------------------------------------------------------------------------
# A.7.4 — fetch_termination sub-dict per endpoint
# ---------------------------------------------------------------------------

def test_wallet_data_has_fetch_termination_field():
    """WalletData must have a fetch_termination field so analyze_wallet can read it.

    Spec: 'FetchTermination objects that split #1 added need to flow from
    fetch_user_positions / fetch_user_activity through fetch_wallet_data
    (likely stored on WalletData)'.
    """
    data = _make_wallet_data(closed_positions=[])
    assert hasattr(data, "fetch_termination"), \
        "WalletData missing fetch_termination field (spec A.7 plumbing)"


def test_time_coverage_fetch_termination_per_endpoint():
    """time_coverage.fetch_termination must carry one entry per endpoint.

    Spec shape:
        'fetch_termination': {
            'closed_positions': 'empty_page' | 'partial_page' | 'api_refused',
            'open_positions': ...,
            'activity': ...,
        }
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    closed = [
        {"conditionId": "cid_a", "timestamp": 1700000000, "realizedPnl": 10.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 10, "outcome": "Yes"},
    ]
    termination_data = {
        "closed_positions": "partial_page",
        "open_positions": "empty_page",
        "activity": "empty_page",
    }
    data = _make_wallet_data(closed_positions=closed,
                             fetch_termination=termination_data)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    ft = report.time_coverage.get("fetch_termination")
    assert ft is not None, \
        "time_coverage['fetch_termination'] missing from report"
    assert "closed_positions" in ft, \
        "fetch_termination missing 'closed_positions' key"
    assert "open_positions" in ft, \
        "fetch_termination missing 'open_positions' key"
    assert "activity" in ft, \
        "fetch_termination missing 'activity' key"
    valid_reasons = {"empty_page", "partial_page", "api_refused"}
    for ep, reason in ft.items():
        assert reason in valid_reasons, \
            f"fetch_termination['{ep}'] = {reason!r} not in vocabulary {valid_reasons}"
    assert ft["closed_positions"] == "partial_page"


# ---------------------------------------------------------------------------
# A.7.5 — All 7 required keys present in time_coverage
# ---------------------------------------------------------------------------

def test_time_coverage_has_all_required_keys():
    """time_coverage dict must contain all 7 keys defined in spec A.7."""
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    ts_first = 1700000000
    ts_last = ts_first + 30 * 86400
    closed = [
        {"conditionId": "cid_a", "timestamp": ts_last, "realizedPnl": 100.0,
         "avgPrice": 0.5, "curPrice": 0.0, "size": 100, "outcome": "Yes"},
        {"conditionId": "cid_b", "timestamp": ts_first, "realizedPnl": -10.0,
         "avgPrice": 0.6, "curPrice": 0.0, "size": 50, "outcome": "Yes"},
    ]
    activity = [
        {"conditionId": "cid_a", "timestamp": ts_last - 100, "transactionHash": "tx1"},
    ]
    termination_data = {
        "closed_positions": "empty_page",
        "open_positions": "empty_page",
        "activity": "empty_page",
    }
    data = _make_wallet_data(closed_positions=closed, activity_rows=activity,
                             fetch_termination=termination_data)
    report = analyze_wallet(pre_fetched_data=data)
    assert report.time_coverage is not None
    required_keys = {
        "first_position_ts",
        "last_position_ts",
        "duration_days",
        "n_positions_per_month",
        "first_fill_ts",
        "last_fill_ts",
        "fetch_termination",
    }
    missing = required_keys - set(report.time_coverage.keys())
    assert not missing, \
        f"time_coverage missing required keys: {missing}"
