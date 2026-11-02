"""The entry card's Why names the slot lane's own verdict on a fast-lane-only refusal (operator
2026-10-06 ~23:3xZ: jpegkite RULR paged "Why: native_fast_lane_unavailable" while the slot lane had
refused the buy at the source-buy floor). Display only."""
import json

from sniper.services import memecoin_entry_view as V
from sniper.services.memecoin_telegram import render_entry_notification

FLOOR = "source buy $1499 (0.384%) under mcap floor 0.800% ($3125)"


def _view(outcome=None, reason=None):
    subs = [{"name": "attempt_row"}]
    if outcome is not None:
        subs.append({"name": "slot_lane_skip", "outcome": outcome, "reason": reason})
    trace = json.dumps({"substage_events": subs})
    return {"status": "rejected", "reason": "native_fast_lane_unavailable", "network_id": "solana",
            "ticker": "RULR", "source_handle": "jpegkite", "source_size_usd": 1499.0,
            "signal_source": "onchain_watch", "origin_event_id": "onchain:SIG",
            "gate_evidence": V._gate_evidence({"trace_json": trace})}


def _why(view):
    return next(l for l in render_entry_notification(view).splitlines() if l.startswith("Why: "))


def test_the_card_says_what_the_slot_lane_decided():
    assert _why(_view("rejected", FLOOR)) == "Why: slot lane refused: " + FLOOR


def test_an_intermediate_hand_off_note_is_not_shown_as_a_verdict():
    for outcome in ("lane", "handoff", "pair_expired"):
        assert _why(_view(outcome, None)) == _why(_view())


def test_without_a_slot_lane_decision_the_card_is_unchanged():
    assert "native" in _why(_view()).lower() or "fast lane" in _why(_view()).lower()


def test_a_missing_receipt_proof_card_carries_the_slot_lane_verdict_too():
    """STC BiR7qY3f 2dab63c2 (2026-10-07): the on-chain lane refused missing_receipt_proof while the
    slot lane had refused slot_lane_price_unreadable; the card said only the first."""
    view = dict(_view("rejected", "slot_lane_price_unreadable"), reason="missing_receipt_proof",
                status="source_initiation_unknown")
    why = _why(view)
    assert why == ("Why: no proof the wallet signed and paid for this buy; "
                   "slot lane refused: slot_lane_price_unreadable"), why
    plain = dict(_view(), reason="missing_receipt_proof", status="source_initiation_unknown")
    assert "slot lane" not in _why(plain)


def test_the_runner_records_the_slot_lane_decision_on_a_solana_receipt_refusal(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_slot_skips as skips
    monkeypatch.setattr(skips, "decision_for", lambda sig: ("rejected", "slot_lane_price_unreadable")
                        if sig == "SIGX" else None)
    marks = []
    trace = SimpleNamespace(mark_substage=lambda name, **kw: marks.append((name, kw)))
    res = R._with_slot_lane_decision({"status": "source_initiation_unknown", "reason": "missing_receipt_proof"},
                                     SimpleNamespace(fomo_event_id="onchain:SIGX"), trace)
    assert res["reason"] == "missing_receipt_proof"
    assert res["slot_lane_skip"] == "rejected: slot_lane_price_unreadable"
    assert marks == [("slot_lane_skip", {"outcome": "rejected", "reason": "slot_lane_price_unreadable"})]
