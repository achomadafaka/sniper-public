"""Tests for the pure capture-assembly helpers (sniper/hyperlive/copy_capture.py)."""
import json

from sniper.hyperlive import copy_capture as CC
from sniper.hyperlive import funding as FUND


def _book():
    return {"mid": 2000.0, "asks": [{"px": 2001.0, "sz": 10.0}], "bids": [{"px": 1999.0, "sz": 10.0}]}


def test_lag_bps_long_positive_when_we_enter_higher():
    # target filled 1999, we fill 2000.5 (mark moved up before we detected) -> we paid ~7.5 bps more
    assert CC.lag_bps("long", 2000.5, 1999.0) > 0


def test_lag_bps_short_positive_when_we_enter_lower():
    # short: worse = selling cheaper. target sold 2001, we sold 2000 -> positive cost
    assert CC.lag_bps("short", 2000.0, 2001.0) > 0


def test_lag_bps_none_without_target_px():
    assert CC.lag_bps("long", 2000.0, None) is None
    assert CC.lag_bps("long", 2000.0, 0.0) is None


def test_entry_capture_full():
    cap = CC.entry_capture("long", 2000.5,
                           {"entry_px": 1999.0, "szi": 8.0, "side": "long"}, _book())
    assert cap["target_entry_px"] == 1999.0 and cap["target_szi"] == 8.0
    assert cap["target_notional"] == round(8.0 * 1999.0, 2)
    assert cap["entry_spread_bps"] is not None and cap["lag_bps"] > 0
    assert json.loads(cap["entry_depth_json"])["asks"][0] == [2001.0, 10.0]


def test_entry_capture_degrades_without_book_or_target():
    cap = CC.entry_capture("long", 2000.0, None, None)
    assert cap["target_entry_px"] is None and cap["entry_depth_json"] is None
    assert cap["entry_spread_bps"] is None and cap["lag_bps"] is None


def test_funding_cost_long_pays_positive():
    rows = [{"time": 100, "rate": 0.00001}, {"time": 200, "rate": 0.00002}, {"time": 999, "rate": 0.1}]
    # window [50,300] captures first two: sum 0.00003 -> long cost +0.3 bps
    c = CC.funding_cost_bps(rows, "long", 50, 300)
    assert c == round(0.00003 * 1e4, 3) and c > 0


def test_funding_cost_short_receives():
    rows = [{"time": 100, "rate": 0.00003}]
    assert CC.funding_cost_bps(rows, "short", 50, 300) < 0     # short receives positive funding


def test_funding_cost_none_when_no_rows_in_window():
    assert CC.funding_cost_bps([{"time": 9999, "rate": 0.1}], "long", 0, 100) is None


def test_canonical_funding_uses_open_closed_interval_and_pnl_sign():
    hour = 3_600_000
    rows = [
        {"time": hour, "rate": 0.00001},
        {"time": 2 * hour, "rate": 0.00002},
        {"time": 3 * hour, "rate": 0.5},
    ]
    short = FUND.funding_pnl(rows, side="short", entry_ts_ms=hour, exit_ts_ms=2 * hour + 1)
    long = FUND.funding_pnl(rows, side="long", entry_ts_ms=hour, exit_ts_ms=2 * hour + 1)
    assert short.status == "complete" and short.value_bps == 0.2
    assert long.status == "complete" and long.value_bps == -0.2
    assert short.expected_prints == 1 and short.observed_prints == 1


def test_canonical_funding_censors_missing_and_exact_exit_boundary():
    hour = 3_600_000
    missing = FUND.funding_pnl([], side="short", entry_ts_ms=1, exit_ts_ms=hour + 1)
    assert missing.status == "missing" and missing.value_bps is None
    boundary = FUND.funding_pnl(
        [{"time": hour, "rate": 0.00001}], side="short",
        entry_ts_ms=1, exit_ts_ms=hour,
    )
    assert boundary.status == "ambiguous_boundary" and boundary.value_bps is None
    known = FUND.funding_pnl(
        [{"time": hour, "rate": 0.00001}], side="short",
        entry_ts_ms=1, exit_ts_ms=hour, exit_after_funding=True,
    )
    assert known.status == "complete" and known.value_bps == 0.1


def test_canonical_funding_distinguishes_no_boundary_from_fetch_failure():
    no_boundary = FUND.funding_pnl([], side="short", entry_ts_ms=1, exit_ts_ms=100)
    assert no_boundary.status == "not_applicable" and no_boundary.value_bps == 0.0
    failed = FUND.funding_pnl(
        None, side="short", entry_ts_ms=1, exit_ts_ms=100, fetch_failed=True
    )
    assert failed.status == "fetch_failed" and failed.value_bps is None


def test_canonical_funding_normalizes_only_bounded_provider_timestamp_skew():
    hour = 3_600_000
    observed = FUND.funding_pnl(
        [{"time": hour + 121, "rate": 0.00001}], side="short",
        entry_ts_ms=1, exit_ts_ms=hour + 1_000,
    )
    outside_tolerance = FUND.funding_pnl(
        [{"time": hour + 60_001, "rate": 0.00001}], side="short",
        entry_ts_ms=1, exit_ts_ms=hour + 120_000,
    )

    assert observed.status == "complete" and observed.value_bps == 0.1
    assert outside_tolerance.status == "missing" and outside_tolerance.value_bps is None


def test_is_holdout_boundary():
    assert CC.is_holdout(1000, None) == 0            # no freeze -> in-sample
    assert CC.is_holdout(999, 1000) == 0             # before freeze
    assert CC.is_holdout(1000, 1000) == 1            # at/after freeze -> OOS
    assert CC.is_holdout(1001, 1000) == 1
