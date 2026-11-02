"""Saved retries explain elapsed time without replacing the terminal transaction's clock."""
import json

import pytest

from sniper.services import memecoin_fill_timing as ft
from test_memecoin_fill_timing import _okt_attempt

FIRST, LAST = "0x" + "11" * 32, "0x" + "22" * 32


def _retry():
    attempt = _okt_attempt()
    trace = json.loads(attempt["trace_json"])
    anchor = trace["receipt_clock"]["monotonic_ns"]
    boot = trace["receipt_clock"]["boot_id"]
    trace["submission_events"] = [
        dict(stage="transaction_submitted", attempt_no=1, boot_id=boot, outcome="accepted",
             monotonic_ns=anchor + 1_585_550_260),
        dict(stage="chain_confirmation", attempt_no=1, boot_id=boot, tx_sig=FIRST, outcome="failed",
             monotonic_ns=anchor + 1_807_347_108),
        dict(stage="transaction_submitted", attempt_no=2, boot_id=boot, outcome="accepted",
             monotonic_ns=anchor + 2_489_464_602),
        dict(stage="chain_confirmation", attempt_no=2, boot_id=boot, tx_sig=LAST, outcome="confirmed",
             block=125, monotonic_ns=anchor + 2_648_425_201)]
    trace["substage_events"] = [
        dict(name="evm.sender_dispatch", attempt_no=number, boot_id=boot, tx_sig=sig,
             monotonic_ns=anchor + elapsed)
        for number, sig, elapsed in ((1, FIRST, 1_500_000_000), (2, LAST, 2_400_000_000))]
    attempt.update(network_id="4663", tx_sig=LAST, trace_json=json.dumps(trace))
    return attempt, trace


def _facts(attempt, trace):
    return ft.facts(dict(attempt, trace_json=json.dumps(trace)))


@pytest.mark.parametrize("duplicate", [False, True])
def test_retry_note_counts_saved_submissions_and_keeps_terminal_metrics(duplicate):
    attempt, trace = _retry()
    if duplicate:
        trace["submission_events"] += [dict(trace["submission_events"][0]), dict(trace["submission_events"][2])]
    facts = _facts(attempt, trace)
    assert facts["receipt_submission_count"] == 2
    assert facts["first_receipt_submit_ms"] == pytest.approx(1585.550260)
    assert facts["receipt_submit_ms"] == pytest.approx(2489.464602)
    assert facts["receipt_confirmation_ms"] == pytest.approx(2648.425201)
    assert ft.stages(facts)["land_ms"] == pytest.approx(158.960599)
    detail = ft.classify(facts)[1]
    assert "2 submitted attempts; first receipt-to-submit observed 1586 ms" in detail
    assert "receipt-to-submit observed 2489 ms" in detail
    assert detail.endswith("largest: receipt-to-submit 2489 ms")


def test_retry_note_preserves_an_unknown_source_gap():
    attempt, trace = _retry()
    attempt["origin_event_id"] = "swap:20521386-197f-4673-b88e-4277401fc8c0"
    facts = _facts(attempt, trace)
    assert ft.gap_of(facts) is None and facts["source_ref"] is None
    line = ft.landed_text(facts)
    assert "block gap unavailable" in line and "source transaction unavailable" in line
    assert "2 submitted attempts; first receipt-to-submit observed 1586 ms" in line


@pytest.mark.parametrize("outcome", ["not_sent", "explicit_rejected", None, "submitted"])
def test_non_submitted_outcomes_do_not_count(outcome):
    attempt, trace = _retry()
    if outcome is None:
        trace["submission_events"][0].pop("outcome")
    else:
        trace["submission_events"][0]["outcome"] = outcome
    assert "submitted attempts" not in ft.classify(_facts(attempt, trace))[1]


def test_ambiguous_submission_is_an_attempt_without_claiming_acceptance():
    attempt, trace = _retry()
    trace["submission_events"][0]["outcome"] = "ambiguous"
    assert "2 submitted attempts" in ft.classify(_facts(attempt, trace))[1]


@pytest.mark.parametrize("bad_clock", ["missing", "cross_boot"])
def test_identity_cannot_come_only_from_an_unclocked_confirmation(bad_clock):
    attempt, trace = _retry()
    trace["substage_events"][0].pop("tx_sig")
    if bad_clock == "missing":
        trace["submission_events"][1].pop("monotonic_ns")
    else:
        trace["submission_events"][1]["boot_id"] = "different-boot"
    assert "submitted attempts" not in ft.classify(_facts(attempt, trace))[1]


def test_valid_dispatch_identity_survives_an_unclocked_confirmation():
    attempt, trace = _retry()
    trace["submission_events"][1].pop("monotonic_ns")
    assert "2 submitted attempts" in ft.classify(_facts(attempt, trace))[1]


@pytest.mark.parametrize("bad", ["single", "signing_only", "missing_clock", "invalid_clock", "missing_no",
                                 "bool_no", "missing_boot", "cross_boot", "negative", "nan", "infinite",
                                 "string_clock", "conflict", "wrong_identity", "unbound", "same_tx", "unordered"])
def test_incomplete_or_conflicting_history_does_not_add_a_retry_note(bad):
    attempt, trace = _retry()
    first = trace["submission_events"][0]
    if bad == "single":
        trace["submission_events"].pop(0)
    elif bad == "signing_only":
        first["stage"] = "signing"
    elif bad == "missing_clock":
        trace.pop("receipt_clock")
    elif bad == "invalid_clock":
        trace["receipt_clock"]["correction_s"] = float("inf")
    elif bad == "missing_no":
        first.pop("attempt_no")
    elif bad == "bool_no":
        first["attempt_no"] = True
    elif bad == "missing_boot":
        first.pop("boot_id")
    elif bad == "cross_boot":
        first["boot_id"] = "another-boot"
    elif bad in ("negative", "nan", "infinite", "string_clock"):
        first["monotonic_ns"] = {"negative": -1, "nan": float("nan"), "infinite": float("inf"),
                                  "string_clock": str(first["monotonic_ns"])}[bad]
    elif bad == "conflict":
        trace["submission_events"].append(dict(first, monotonic_ns=first["monotonic_ns"] + 1))
    elif bad == "wrong_identity":
        trace["submission_events"][1]["tx_sig"] = "0x" + "33" * 32
    elif bad == "unbound":
        trace["submission_events"][1].pop("tx_sig")
        trace["substage_events"][0].pop("tx_sig")
    elif bad == "same_tx":
        trace["submission_events"][1]["tx_sig"] = LAST
        trace["substage_events"][0]["tx_sig"] = LAST
    else:
        first["monotonic_ns"] = trace["submission_events"][2]["monotonic_ns"] + 1
    facts = _facts(attempt, trace)
    assert "receipt_submission_count" not in facts
    assert "submitted attempts" not in ft.classify(facts)[1]


def test_legacy_and_solana_timings_have_no_retry_note():
    attempt, trace = _retry()
    trace.pop("receipt_clock")
    for event in trace["submission_events"] + trace["substage_events"]:
        event.pop("monotonic_ns")
        event.pop("boot_id")
    assert "submitted attempts" not in ft.classify(_facts(attempt, trace))[1]
    attempt["network_id"] = "solana"
    assert "submitted attempts" not in ft.classify(_facts(attempt, trace))[1]
