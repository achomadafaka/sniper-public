"""Queue expiry is measured before attempt creation, entirely on monotonic clocks."""
import json
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_slot_lane as L
from test_memecoin_slot_lane import conn, _buy, _live_lane


def test_queue_keeps_original_receipt_clock_and_records_each_enqueue():
    clock = SimpleNamespace(t=100., monotonic=lambda: clock.t)
    q = L.LaneBuyQueue(clock=clock)
    buy = _buy(recv_monotonic=99.8)
    q.put(buy)
    clock.t = 100.4
    assert q.get() == (buy, 100.)
    q.put(buy)
    assert q.get() == (buy, 100.4)
    assert buy.recv_monotonic == 99.8


def test_queue_loss_denominator_includes_pre_attempt_refusals_and_distinguishes_already_old():
    stats = L.QueueTiming()
    stats.record({"reason": "slot_lane_signal_too_old", "queue_timing": {
        "queue_ms": 600., "receipt_to_enqueue_ms": 50., "receipt_to_decision_ms": 650.}})
    stats.record({"reason": "slot_lane_signal_too_old", "queue_timing": {
        "queue_ms": 10., "receipt_to_enqueue_ms": 600., "receipt_to_decision_ms": 610.}})
    stats.record({"reason": "slot_lane_wallet_busy", "queue_timing": {
        "queue_ms": 0., "receipt_to_enqueue_ms": 1., "receipt_to_decision_ms": 1.}})
    report = stats.snapshot()
    assert report["handled"] == 3
    assert report["queue_expired"] == 1
    assert report["already_old"] == 1
    assert report["wallet_busy"] == 1
    assert report["queue_ms"]["p50"] == 10.
    stats.record({"reason": "slot_lane_signal_too_old", "queue_timing": {
        "queue_ms": None, "receipt_to_enqueue_ms": None, "receipt_to_decision_ms": None}})
    assert stats.snapshot()["unknown"] == 1
    assert stats.snapshot()["queue_expired"] == 1


def test_trace_normalizes_receipt_from_corrected_anchor_and_monotonic_age(conn, tmp_path, monkeypatch):
    ln, _ = _live_lane(conn, tmp_path, monkeypatch)
    import time
    now = time.monotonic()
    # Raw receipt intentionally disagrees by 20 seconds. Wall UTC cannot grade elapsed time.
    buy = _buy(recv_monotonic=now-.1, recv_utc="2026-09-26T20:30:20+00:00")
    result = ln.handle(buy, enqueued_mono=now-.05)
    assert result["status"] == "filled"
    row = dict(conn.execute("SELECT * FROM memecoin_entry_attempts").fetchone())
    assert row["observed_at"] < row["decision_started_at"]
    receipt = next(e for e in json.loads(row["trace_json"])["substage_events"]
                   if e["name"] == "slot_receipt")
    assert receipt["receipt_to_decision_ms"] == pytest.approx(100., abs=5.)
    assert receipt["queue_ms"] == pytest.approx(50., abs=5.)
    assert receipt["timing_basis"] == "monotonic"
    assert ln._boundary(buy)["receipt_to_dispatch_ms"] >= 100.


def test_pre_attempt_age_refusal_carries_queue_measurement_without_writing(conn, tmp_path, monkeypatch):
    import time
    ln, _ = _live_lane(conn, tmp_path, monkeypatch)
    now = time.monotonic()
    result = ln.handle(_buy(recv_monotonic=now-.7), enqueued_mono=now-.65)
    assert result["reason"] == "slot_lane_signal_too_old"
    assert result["queue_timing"]["queue_ms"] >= 650.
    assert conn.execute("SELECT COUNT(*) FROM memecoin_entry_attempts").fetchone()[0] == 0


def test_missing_or_backwards_intervals_are_unknown_never_zero():
    assert L.monotonic_ms(10., None) is None
    assert L.monotonic_ms(10., 11.) is None
    assert L.monotonic_ms(10., float("nan")) is None
    assert L.monotonic_ms(10., 10.) == 0.


def test_expiry_inside_handler_does_not_claim_buy_was_old_before_enqueue():
    stats = L.QueueTiming()
    stats.record({'reason': 'slot_lane_signal_too_old', 'waited_ms': 500.1,
                  'queue_timing': {'queue_ms': .9, 'receipt_to_enqueue_ms': 499.,
                                   'receipt_to_decision_ms': 499.9}})
    assert stats.snapshot()['already_old'] == 0
    assert stats.snapshot()['queue_expired'] == 0
    assert stats.snapshot()['unknown'] == 1
