"""Quote latency is measured at named local boundaries, never inferred from source time."""
import json

import pytest

from sniper.services import memecoin_fill_timing as timing

QUOTE = "2026-10-10T12:00:00.200+00:00"
SUBMITTED = "2026-10-10T12:00:00.800+00:00"
CONFIRMED = "2026-10-10T12:00:01.500+00:00"


def _attempt(**changes):
    row = {
        "attempt_id": "boundary-attempt", "network_id": "56", "signal_source": "onchain_watch",
        "terminal_status": "filled", "clock_domain": "observed_real_utc", "quote_age_ms": 1300.0,
        "quote_observed_at": QUOTE, "transaction_submitted_at": SUBMITTED,
        "chain_confirmation_completed_at": CONFIRMED,
    }
    row.update(changes)
    return row


def _trace(*, quote=QUOTE, submitted=SUBMITTED, confirmed=CONFIRMED):
    return json.dumps({"stages": {
        "quote": {"completed_at": quote},
        "transaction_submitted": {"completed_at": submitted},
        "chain_confirmation": {"completed_at": confirmed},
    }})


def _boundaries(row):
    facts = timing.facts(row)
    return facts["quote_to_submission_ms"], facts["quote_to_confirmation_ms"]


def test_submission_and_confirmation_ages_are_separate_and_keep_legacy_meaning():
    row = _attempt()
    assert _boundaries(row) == pytest.approx((600.0, 1300.0))
    assert row["quote_age_ms"] == 1300.0


def test_submission_age_exists_before_confirmation():
    assert _boundaries(_attempt(chain_confirmation_completed_at=None)) == (600.0, None)


def test_confirmation_age_never_creates_a_submission_timestamp():
    assert _boundaries(_attempt(transaction_submitted_at=None)) == (None, 1300.0)


def test_equal_boundaries_are_measured_zero():
    assert _boundaries(_attempt(transaction_submitted_at=QUOTE,
                               chain_confirmation_completed_at=QUOTE)) == (0.0, 0.0)


def test_confirmation_before_measured_submission_is_unknown():
    assert _boundaries(_attempt(chain_confirmation_completed_at="2026-10-10T12:00:00.500+00:00")) == (600.0, None)


@pytest.mark.parametrize("quote", [None, "", "invalid", True, "2026-10-10T12:00:00.200"])
def test_missing_malformed_or_naive_quote_is_unknown(quote):
    assert _boundaries(_attempt(quote_observed_at=quote)) == (None, None)


@pytest.mark.parametrize("field", ["transaction_submitted_at", "chain_confirmation_completed_at"])
@pytest.mark.parametrize("stamp", ["invalid", True, "2026-10-10T12:00:00.800",
                                   "2026-10-10T12:00:00.100+00:00"])
def test_invalid_or_reversed_boundary_is_unknown(field, stamp):
    got = _boundaries(_attempt(**{field: stamp}))
    assert got[0 if field == "transaction_submitted_at" else 1] is None


@pytest.mark.parametrize("domain", [None, "legacy_unknown", "claimed_source_utc", "confirmation_only_domain"])
def test_unknown_main_row_clock_domain_does_not_make_quote_ages(domain):
    assert _boundaries(_attempt(clock_domain=domain)) == (None, None)


def test_trace_boundaries_prove_ages_even_when_source_receipt_domain_is_unknown():
    assert _boundaries(_attempt(clock_domain="legacy_unknown", trace_json=_trace())) == (600.0, 1300.0)


def test_confirmation_only_trace_does_not_prove_submission_age():
    trace = json.dumps({"stages": {"quote": {"completed_at": QUOTE},
                                 "chain_confirmation": {"completed_at": CONFIRMED}}})
    assert _boundaries(_attempt(clock_domain="confirmation_only_domain", trace_json=trace)) == (None, 1300.0)


def test_trace_proof_compares_instants_including_utc_offsets():
    assert _boundaries(_attempt(clock_domain="legacy_unknown", trace_json=_trace(),
                               quote_observed_at="2026-10-10T08:00:00.200-04:00")) == (600.0, 1300.0)


@pytest.mark.parametrize("changed", ["quote", "submitted", "confirmed"])
def test_main_row_and_trace_disagreement_cannot_supply_legacy_clock_proof(changed):
    row = _attempt(clock_domain="legacy_unknown", trace_json=_trace(**{changed: "2026-10-10T12:00:02+00:00"}))
    got = _boundaries(row)
    if changed == "quote":
        assert got == (None, None)
    else:
        assert got[0 if changed == "submitted" else 1] is None


def test_native_missing_main_boundaries_needs_exact_same_trace_quote_and_lane_stamps():
    trace = json.dumps({"stages": {"quote": {"completed_at": QUOTE}},
        "substage_events": [{"name": "rh_fast_lane_send", "completed_at": SUBMITTED},
                            {"name": "rh_fast_lane_receipt", "completed_at": CONFIRMED}]})
    assert _boundaries(_attempt(signal_source="bsc_fast_lane", quote_observed_at=None,
        transaction_submitted_at=None, chain_confirmation_completed_at=None,
        trace_json=trace, clock_domain="legacy_unknown")) == (600.0, 1300.0)


def test_native_send_and_receipt_without_quote_proof_keep_quote_latency_unknown():
    trace = json.dumps({"source_evidence": {"source_block_time": 1791633600},
        "substage_events": [{"name": "rh_fast_lane_send", "completed_at": SUBMITTED},
                            {"name": "rh_fast_lane_receipt", "completed_at": CONFIRMED}]})
    row = _attempt(signal_source="bsc_fast_lane", quote_observed_at=QUOTE,
                   transaction_submitted_at=None, chain_confirmation_completed_at=None, trace_json=trace)
    assert _boundaries(row) == (None, None)


def test_source_timestamp_trust_and_legacy_age_do_not_substitute_missing_quote():
    assert _boundaries(_attempt(quote_observed_at=None, source_created_at=QUOTE,
                               source_timestamp_trust="confirmed_chain", quote_age_ms=1300.0)) == (None, None)


@pytest.mark.parametrize("trace", ["invalid", "[]", '{"stages":[]}', '{"stages":{"quote":true}}'])
def test_malformed_trace_cannot_prove_unknown_clocks(trace):
    assert _boundaries(_attempt(clock_domain="legacy_unknown", trace_json=trace)) == (None, None)


def test_view_details_exposes_both_boundaries_without_changing_existing_stage_labels():
    detail = timing._details(None, _attempt(), None, {}, True)
    assert detail["quote_to_submission_ms"] == 600.0
    assert detail["quote_to_confirmation_ms"] == 1300.0
    assert detail["stages"]["land_ms"] == pytest.approx(700.0)


def test_cached_timings_do_not_fill_missing_quote_boundary_proof(monkeypatch):
    monkeypatch.setattr(timing, "one", lambda conn, sql, args: {
        "stages_json": '{"quote_to_submission_ms":600,"quote_to_confirmation_ms":1300}'}
        if "FROM memecoin_fill_timing WHERE" in sql else None)
    detail = timing._details(None, _attempt(quote_observed_at=None), object(), {}, True)
    assert detail["quote_to_submission_ms"] is None
    assert detail["quote_to_confirmation_ms"] is None
