"""A held exotic leg reports its terminal result before the on-chain lane wakes."""
import collections
import dataclasses
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_fill_timing as ft
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_skips as skips
from test_slot_lane_exotic_live import _bare_lane, _buy


@pytest.fixture(autouse=True)
def isolated_decisions(monkeypatch):
    monkeypatch.setattr(skips, "_RING", collections.deque(maxlen=skips.RING_MAX))
    monkeypatch.setattr(skips, "_LAST", collections.OrderedDict())


def _held(*, signature="OTTO", deadline=101.0):
    buy = SimpleNamespace(signature=signature, routed=None)
    legs = L.PendingLegs()
    legs.add(buy, deadline=deadline)
    observed = []
    wake = SimpleNamespace(set=lambda: observed.append(skips.decision_for(signature)))
    lane = _bare_lane(pending_legs=legs, wake=wake,
                      clock=SimpleNamespace(monotonic=lambda: 100.0))
    skips.note(signature, "rejected", "slot_lane_exotic_leg_pending")
    return lane, buy, observed


def test_worker_policy_refusal_reaches_card_and_timing_before_wake(tmp_path):
    from sniper.services import memecoin_real_runner as R
    from sniper.services.memecoin_telegram import _slot_lane_words

    lane, buy, observed = _held()
    reason = "mcap $311151 over ceiling $250000"
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(buy, reason))
    assert observed == [("rejected", reason)]
    assert lane.pending_legs.release_kind("OTTO", 100.0) == "policy"
    evidence = []
    trace = SimpleNamespace(mark_substage=lambda name, **fields: evidence.append(dict(name=name, **fields)))
    result = R._fast_lane_refusal(SimpleNamespace(fomo_event_id="onchain:OTTO"), "solana", trace)
    assert result["reason"] == "native_fast_lane_unavailable"
    assert result["slot_lane_skip"] == "rejected: mcap $311151 over ceiling $250000"
    assert _slot_lane_words(dict(result, gate_evidence=evidence)) == "slot lane refused: " + reason
    conn = ft.connect(tmp_path / "timing.db")
    try:
        skips.flush(conn, skips.drain())
        assert ft.skip_of(conn, "solana", "OTTO") == {"outcome": "rejected", "reason": reason}
    finally:
        conn.close()


def test_worker_duplicate_does_not_erase_later_result():
    lane, buy, observed = _held()
    assert lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "slot_lane_cpmm_leg_failed"))
    skips.note("OTTO", "filled", None)
    assert not lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "mcap $1 over ceiling $0"))
    assert lane.expire_exotic(102.0) == 0
    assert skips.decision_for("OTTO") == ("filled", None)
    assert len(observed) == 1


def test_expiry_reports_aged_out_once_before_wake_and_ignores_late_worker():
    lane, buy, observed = _held(deadline=99.0)
    assert lane.expire_exotic(100.0) == 1
    assert observed == [("rejected", "slot_lane_exotic_aged_out")]
    assert lane.pending_legs.release_kind("OTTO", 100.0) == "expired"
    skips.note("OTTO", "filled", None)
    assert lane.expire_exotic(101.0) == 0
    assert not lane.resolve_exotic_refusal(L.ExoticRefusal(buy, "slot_lane_cpmm_leg_failed"))
    assert skips.decision_for("OTTO") == ("filled", None)


@pytest.mark.parametrize("outcome, reason", [
    ("rejected", "mcap $311151 over ceiling $250000"),
    ("filled", None), ("submitted", None), ("submitted_unknown", "timeout"),
    ("unknown", None), ("anomaly", "receipt_unreadable"), ("error", "worker_error"),
])
def test_unowned_late_requeue_cannot_erase_terminal_decision(outcome, reason):
    lane, _buy_unused, _observed = _held()
    lane.pending_legs.claim("OTTO", now=100.0)
    skips.note("OTTO", outcome, reason)
    late = dataclasses.replace(_buy(recv=99.9), signature="OTTO", ondemand={"leg": {}, "quote": (6, 1, 0, 0)})
    assert lane.handle(late) == {"status": "rejected", "reason": "slot_lane_exotic_aged_out"}
    assert skips.decision_for("OTTO") == (outcome, reason)


@pytest.mark.parametrize("outcome, reason", [
    (None, None), ("lane", "pump_amm"), ("handoff", "pool_unknown"), ("pair_expired", None),
    ("rejected", "slot_lane_exotic_leg_pending"),
])
def test_unowned_late_requeue_replaces_only_routing_or_pending_notes(outcome, reason):
    lane, _buy_unused, _observed = _held()
    lane.pending_legs.claim("OTTO", now=100.0)
    skips._LAST.pop("OTTO")
    skips.note("OTTO", outcome, reason)
    late = dataclasses.replace(_buy(recv=99.9), signature="OTTO", ondemand={"leg": {}, "quote": (6, 1, 0, 0)})
    assert lane.handle(late) == {"status": "rejected", "reason": "slot_lane_exotic_aged_out"}
    assert skips.decision_for("OTTO") == ("rejected", "slot_lane_exotic_aged_out")


@pytest.mark.parametrize("reason", ["mcap $311151 over ceiling $250000", "slot_lane_exotic_aged_out"])
def test_timing_replaces_pending_placeholder_with_terminal_skip(reason):
    f = ft.facts(dict(network_id="solana", signal_source="onchain_watch"),
                 slot_attempt=dict(terminal_status="rejected", terminal_reason="slot_lane_exotic_leg_pending"),
                 skip=dict(outcome="rejected", reason=reason))
    assert ft.classify(f)[1].startswith("slot lane refused: " + reason)


@pytest.mark.parametrize("slot_status, slot_reason, outcome, reason", [
    ("rejected", "slot_lane_exotic_leg_pending", "lane", "pump_amm"),
    ("rejected", "slot_lane_exotic_leg_pending", "handoff", "pool_unknown"),
    ("rejected", "slot_lane_exotic_leg_pending", "rejected", None),
    ("source_initiation_unknown", "missing_receipt_proof", "rejected", "mcap $1 over ceiling $0"),
    ("failed", "chain_error:BuySlippageBelowMinBaseAmountOut", "rejected", "mcap $1 over ceiling $0"),
])
def test_timing_preserves_real_attempts_and_does_not_promote_routing_hints(slot_status, slot_reason,
                                                                       outcome, reason):
    f = ft.facts(dict(network_id="solana", signal_source="onchain_watch",
                     delivery_lane="onchain_fallback_after_slot_lane" if slot_status == "failed" else None),
                 slot_attempt=dict(terminal_status=slot_status, terminal_reason=slot_reason),
                 skip=dict(outcome=outcome, reason=reason))
    assert slot_reason in ft.classify(f)[1]
