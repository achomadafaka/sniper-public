"""Stored source venue explains missing BSC decisions without inventing a verdict."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_fill_timing as timing


def facts(post, fast=None):
    return timing.facts(
        {"attempt_id": "fixture", "network_id": "56", "source_slot": 100,
         "landing_slot": 104, "signal_source": "onchain_watch"},
        detection={"source_valuation_json": json.dumps({"evm_post": post})}, fast=fast)


def test_missing_bsc_decision_retains_venue_without_false_v2_only_limit():
    cause, text = timing.classify(facts({"venue": "v3", "pool": "0x" + "aa" * 20}))
    assert cause == "fast_lane_skipped"
    assert 'BSC V3 source;' in text
    assert 'supports V2' not in text
    assert "no fast-lane decision" in text


def test_missing_source_venue_reports_the_gap():
    _cause, text = timing.classify(facts({"reason": "no_venue_leg"}))
    assert "source venue unresolved (no_venue_leg)" in text


def test_actual_lane_verdict_remains_authoritative():
    _cause, text = timing.classify(facts(
        {"venue": "v3"}, {"lane": "bsc lane", "reason": "inline_unsafe:safety_deadline_exceeded"}))
    assert "inline_unsafe:safety_deadline_exceeded" in text
    assert "supports V2" not in text


def test_malformed_metadata_stays_unknown():
    _cause, text = timing.classify(facts(["v3"]))
    assert "no fast-lane decision" in text
    assert "supports V2" not in text


@pytest.mark.parametrize("written_at,late", [
    ("2026-10-07T15:45:54.171553+00:00", True),
    (None, False), ("bad", False), ("2026-10-07T15:45:54.171553", False),
    ("2026-10-07T15:45:54.100000+00:00", False),
])
def test_proven_late_duplicate_is_not_fast_lane_skipped(written_at, late):
    f = facts({}, {"lane": "bsc lane", "reason": "hook:rejected:unresolved signed transaction on 56",
                   "decision_started_at": "2026-10-07T15:45:54.165138+00:00", "written_at": written_at})
    f["submitted_at"] = "2026-10-07T15:45:54.036452+00:00"
    cause, text = timing.classify(f)
    assert (cause != "fast_lane_skipped") == late
    assert ("BSC duplicate suppressed after broadcast" in text) == late


@pytest.mark.parametrize("late", [False, True])
def test_cached_completed_row_refreshes_why_but_preserves_measurements(monkeypatch, late):
    stored = {"state": "complete", "network_id": "56", "source_slot": 100,
              "landing_slot": 104, "gap": 4, "cause": "fast_lane_skipped",
              "cause_detail": "no fast-lane decision",
              "stages_json": '{"decide_ms":1184,"land_ms":668}'}
    monkeypatch.setattr(timing, "one", lambda *_args: stored)
    monkeypatch.setattr(timing, "detection_of", lambda *_args:
                        {"source_valuation_json": '{"evm_post":{"venue":"v3"}}'})
    monkeypatch.setattr(timing, "slot_attempt_of", lambda *_args: None)
    monkeypatch.setattr(timing, "skip_of", lambda *_args: None)
    fast = {"reason": "hook:rejected:unresolved signed transaction on 56",
            "decision_started_at": "2026-10-07T15:45:54.165138+00:00",
            "written_at": "2026-10-07T15:45:54.171553+00:00"} if late else None
    monkeypatch.setattr(timing, "fast_of", lambda *_args, **_kwargs: fast)
    attempt = {"attempt_id": "fixture", "network_id": "56", "terminal_status": "filled",
               "transaction_submitted_at": "2026-10-07T15:45:54.036452+00:00"}
    board = timing._details(None, attempt, object(), {}, True)
    alert = timing._details(None, attempt, object(), {}, False)
    assert board["line"] == alert["line"]
    assert "Landed +4 blocks" in board["line"]
    assert ('BSC duplicate suppressed after broadcast' if late else 'BSC V3 source;') in board['line']
    assert 'supports V2' not in board['line']
    assert "largest: detect-to-submit 1184 ms" in board["line"]
    assert board["stages"]["land_ms"] == 668


@pytest.mark.parametrize("decision,submitted,late", [
    ("2026-10-07T15:45:54.165138+00:00", "2026-10-07T15:45:54.036452+00:00", True),
    ("2026-10-07T15:45:54.200000+00:00", "2026-10-07T15:45:54.036452+00:00", False),
    ("2026-10-07T15:45:53.999999+00:00", "2026-10-07T15:45:54.036452+00:00", False),
    ("2026-10-07T15:45:54.036452+00:00", "2026-10-07T15:45:54.036452+00:00", False),
    (None, "2026-10-07T15:45:54.036452+00:00", False),
    ("bad", "2026-10-07T15:45:54.036452+00:00", False),
    ("2026-10-07T15:45:54.165138", "2026-10-07T15:45:54.036452+00:00", False),
    ("2026-10-07T15:45:54.165138+00:00", None, False),
    ("2026-10-07T15:45:54.165138+00:00", "2026-10-07T15:45:54.036452", False),
])
def test_yh_duplicate_after_broadcast_is_not_the_delay_cause(decision, submitted, late):
    ref, mint = "0xsource", "0xmint"
    with sqlite3.connect(":memory:") as lane, sqlite3.connect(":memory:") as ledger:
        lane.execute("CREATE TABLE bsc_lane_events (id INTEGER, tx_hash TEXT, mint TEXT, reason TEXT, written_at TEXT)")
        lane.execute("INSERT INTO bsc_lane_events VALUES (1, ?, ?, ?, ?)",
                     (ref, mint, "hook:rejected:unresolved signed transaction on 56",
                      "2026-10-07T15:45:54.171553+00:00"))
        ledger.execute("CREATE TABLE memecoin_entry_attempts (origin_event_id TEXT, decision_started_at TEXT, "
                       "terminal_status TEXT, terminal_reason TEXT)")
        ledger.execute("CREATE INDEX idx_mc_entry_attempt_origin ON memecoin_entry_attempts(origin_event_id)")
        ledger.execute("INSERT INTO memecoin_entry_attempts VALUES (?, ?, 'refused', ?)",
                       ("onchain:56:" + ref + ":" + mint, decision, "unresolved signed transaction on 56"))
        fast = timing.fast_of({"bsc": lane}, "56", ref, mint=mint, ledger=ledger)
        assert timing.fast_of({"bsc": lane}, "56", ref, mint="0xother", ledger=ledger) is None
    f = timing.facts({"network_id": "56", "signal_source": "follow_activity",
                      "terminal_status": "filled", "source_slot": 126278347, "landing_slot": 126278351,
                      "observed_at": "2026-10-07T15:45:53.096867+00:00",
                      "transaction_submitted_at": submitted,
                      "chain_confirmation_completed_at": "2026-10-07T15:45:54.409511+00:00"},
                     detection={"block_time": 1791387952}, fast=fast)
    cause, text = timing.classify(f)
    assert timing.landed_text(f).startswith("Landed +4 blocks")
    if late:
        assert cause == "decide_slow"
        assert "BSC duplicate suppressed after broadcast" in text
        assert "source block-to-receipt 1.1 s (coarse)" in text
        assert "largest: detect-to-submit 940 ms" in text
    else:
        assert cause == "fast_lane_skipped"
        assert "hook:rejected:unresolved signed transaction on 56" in text
