"""A J7 UUID cannot identify a source transaction; retained local timings still explain it."""
import pytest
import json

from sniper.services import memecoin_fill_timing as ft


def _attempt(origin):
    return dict(network_id="56", signal_source="follow_activity", terminal_status="filled",
                origin_event_id=origin, tx_sig="0x" + "ab" * 32,
                observed_at="2026-10-09T14:40:47.272137+00:00",
                transaction_submitted_at="2026-10-09T14:40:49.706421+00:00",
                chain_confirmation_completed_at="2026-10-09T14:40:50.210000+00:00",
                trace_json=json.dumps({"submission_events": [
                    {"stage": "chain_confirmation", "block": 126653487}]}))


def test_uuid_keeps_unknown_gap_and_exposes_missing_identity_and_local_stages():
    attempt = _attempt("swap:20521386-197f-4673-b88e-4277401fc8c0")
    f = ft.facts(attempt)
    assert ft.source_ref(attempt) is None and ft.gap_of(f) is None
    cause, detail = ft.classify(f)
    assert cause == "source_unknown" and "source transaction unavailable" in detail
    line = ft.landed_text(f)
    assert line.startswith("Landed block 126653487 · block gap unavailable")
    assert "source transaction unavailable" in line and "largest: detect-to-submit 2434 ms" in line
    assert "+8" not in line and "resolving" not in line


def test_stored_uuid_source_does_not_promise_a_resolvable_gap():
    line = ft.line_from_row(dict(network_id="56", source_sig=None, gap=None,
                                 landing_slot=126653487, cause="source_unknown",
                                 cause_detail="source transaction unavailable", state="unsupported"))
    assert line == "Landed block 126653487 · block gap unavailable · source transaction unavailable"


def test_exact_reference_with_missing_receipt_has_a_different_explanation():
    attempt = _attempt("onchain:56:0x" + "12" * 32 + ":0x" + "34" * 20)
    line = ft.landed_text(ft.facts(attempt))
    assert line.startswith("Landed +? blocks (source block resolving)")
    assert "source block unresolved" in line and "source transaction unavailable" not in line


def test_stored_exact_reference_still_resolves_its_source_block():
    line = ft.line_from_row(dict(network_id="56", source_sig="0x" + "12" * 32, gap=None,
                                 landing_slot=126653487, cause="source_unknown",
                                 cause_detail="source block unresolved", state="pending"))
    assert line == "Landed +? blocks (source block resolving) · source block unresolved"


def test_manual_entry_keeps_its_confirmed_block_without_a_source():
    attempt = dict(_attempt("manual:operator"), signal_source="manual")
    assert ft.landed_text(ft.facts(attempt)) == "Landed block 126653487 · operator entry"


def test_failed_native_minimum_output_keeps_its_reason_without_a_landing_block():
    attempt = dict(_attempt("bscfast:56:0x" + "12" * 32 + ":0x" + "34" * 20),
                   signal_source="bsc_fast_lane", terminal_status="failed",
                   terminal_reason="reverted_min_out", trace_json=None)
    assert "reverted_min_out" in ft.landed_text(ft.facts(attempt))


@pytest.mark.parametrize("parallel", ["used", "intent_used"])     # an adopted intent read too (Gamma LOW-1)
def test_parallel_safety_latency_is_labelled_as_remaining_wait(parallel):
    f = ft.facts(dict(_attempt("bscfast:56:0x" + "12" * 32 + ":0x" + "34" * 20),
                      signal_source="bsc_fast_lane", source_slot=126653479),
                 live={"safety_path": "inline", "safety_ms": 600, "parallel_safety": parallel})
    assert "safety wait inline 600 ms" in ft.landed_text(f)
