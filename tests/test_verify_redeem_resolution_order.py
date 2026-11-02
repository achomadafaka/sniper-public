"""Tests for M5 REDEEM timestamp verification."""

from __future__ import annotations

import sqlite3

from scripts.verify_redeem_resolution_order import evaluate_redeems


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE market_outcomes (
            condition_id TEXT NOT NULL,
            outcome TEXT NOT NULL,
            resolved_price REAL NOT NULL DEFAULT 0.0,
            resolved_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (condition_id, outcome)
        )
        """
    )
    return conn


def test_evaluate_redeems_passes_when_redeems_postdate_resolution():
    conn = _conn()
    conn.execute(
        "INSERT INTO market_outcomes(condition_id, outcome, resolved_at) VALUES (?, ?, ?)",
        ("cond_a", "yes", 1000),
    )
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_a", "timestamp": "1100", "id": "r1"}],
    )
    assert result["status"] == "pass"
    assert result["n_redeems"] == 1
    assert result["n_matched_to_market_outcomes"] == 1
    assert result["n_pre_resolution_redeems"] == 0
    assert result["pre_resolution_examples"] == []


def test_evaluate_redeems_marks_inconsistent_when_redeem_predates_resolution_reference():
    conn = _conn()
    conn.execute(
        "INSERT INTO market_outcomes(condition_id, outcome, resolved_at) VALUES (?, ?, ?)",
        ("cond_a", "yes", 1000),
    )
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_a", "timestamp": "999", "id": "r1"}],
    )
    assert result["status"] == "pass"
    assert result["resolution_reference_status"] == "inconsistent"
    assert result["n_pre_resolution_redeems"] == 1
    assert result["pre_resolution_examples"][0]["redeem_id"] == "r1"


def test_evaluate_redeems_tracks_unmatched_conditions_without_failing():
    conn = _conn()
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_missing", "timestamp": "999", "id": "r1"}],
    )
    assert result["status"] == "pass"
    assert result["n_redeems"] == 1
    assert result["n_matched_to_market_outcomes"] == 0
    assert result["n_unmatched_conditions"] == 1


def test_evaluate_redeems_handles_missing_market_outcomes_table():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_missing", "timestamp": "999", "id": "r1"}],
    )
    assert result["status"] == "pass"
    assert result["n_redeems"] == 1
    assert result["n_matched_to_market_outcomes"] == 0
    assert result["n_unmatched_conditions"] == 1


def test_evaluate_redeems_fails_when_redemption_tx_appears_in_trade_rows():
    conn = _conn()
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_a", "timestamp": "1100", "id": "0xabc_0x1"}],
        trade_rows=[{"transactionHash": "0xabc"}],
    )
    assert result["status"] == "fail"
    assert result["n_redeem_trade_tx_overlaps"] == 1
    assert result["redeem_trade_tx_overlap_examples"] == ["0xabc"]


def test_evaluate_redeems_does_not_fail_on_stale_resolution_reference_only():
    conn = _conn()
    conn.execute(
        "INSERT INTO market_outcomes(condition_id, outcome, resolved_at) VALUES (?, ?, ?)",
        ("cond_a", "yes", 2000),
    )
    result = evaluate_redeems(
        conn,
        "0xwallet",
        [{"condition": "cond_a", "timestamp": "1100", "id": "0xabc_0x1"}],
        trade_rows=[{"transactionHash": "0xdef"}],
    )
    assert result["status"] == "pass"
    assert result["resolution_reference_status"] == "inconsistent"
    assert result["n_pre_resolution_redeems"] == 1
