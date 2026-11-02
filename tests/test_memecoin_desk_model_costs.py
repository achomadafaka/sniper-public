"""Model estimates never silently become incurred desk cash expenses."""
from __future__ import annotations

import copy
import re

import pytest

try:
    from sniper.services import memecoin_desk_model_costs as costs
except ImportError:
    costs = None


AT = "2026-10-21T12:00:01+00:00"
TICKET = "desk:U:receipt-1"
MODEL = "claude-haiku-5-5"


def receipt(**changes):
    proof = {"ticket_id": TICKET, "provider": "anthropic", "model": MODEL,
        "session_id": "session-1", "result_id": "result-1", "response_at": AT,
        "usage": {"input_tokens": 1200, "output_tokens": 40,
                  "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 0},
        "is_error": False, "billing_basis": "unknown"}
    proof.update(changes)
    return proof


def normalize(proof, **kwargs):
    assert costs is not None, "model cost receipt normalizer is missing"
    return costs.normalize_model_cost_receipt(proof, ticket_id=kwargs.pop("ticket_id", TICKET),
        model=kwargs.pop("model", MODEL), response_at=kwargs.pop("response_at", AT), **kwargs)


def test_usage_and_reported_api_equivalent_remain_unknown_cash():
    result = normalize(receipt(notional_api_equivalent_usd=0.003))
    assert result["cost_usd"] is None
    assert result["notional_api_equivalent_usd"] == 0.003
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", result["provenance"])


@pytest.mark.parametrize("amount", [0, 0.0125])
@pytest.mark.parametrize("is_error", [False, True])
def test_attributable_reported_bill_is_preserved_including_zero_and_errors(amount, is_error):
    result = normalize(receipt(billing_basis="billed_receipt", billed_usd=amount,
        charge_id="charge-1", is_error=is_error, notional_api_equivalent_usd=0.03))
    assert result["cost_usd"] == amount
    assert result["notional_api_equivalent_usd"] == 0.03
    assert result["evidence"]["receipt"]["is_error"] is is_error


@pytest.mark.parametrize("changes", [{}, {"billed_usd": None}, {"charge_id": "charge-1"},
                                     {"billed_usd": 0.05}])
def test_incomplete_bill_never_becomes_zero_or_known_cash(changes):
    assert normalize(receipt(billing_basis="billed_receipt", **changes))["cost_usd"] is None


def test_declared_flat_subscription_is_unknown_without_frozen_opt_in():
    result = normalize(receipt(billing_basis="subscription_flat", notional_api_equivalent_usd=0.09))
    assert result["cost_usd"] is None
    assert result["notional_api_equivalent_usd"] == 0.09


def test_frozen_flat_subscription_reports_zero_marginal_cash_without_losing_estimate():
    result = normalize(receipt(billing_basis="subscription_flat", notional_api_equivalent_usd=0.09,
                              is_error=True), billing_basis="subscription_flat")
    assert result["cost_usd"] == 0
    assert result["notional_api_equivalent_usd"] == 0.09
    assert result["evidence"]["receipt"]["is_error"] is True


@pytest.mark.parametrize("expected,declared", [("subscription_flat", "unknown"),
    ("subscription_flat", "billed_receipt"), ("billed_receipt", "subscription_flat"),
    ("billed_receipt", "unknown")])
def test_frozen_billing_basis_cannot_be_changed_by_a_receipt(expected, declared):
    with pytest.raises(ValueError, match="billing_basis"):
        normalize(receipt(billing_basis=declared), billing_basis=expected)


def test_matching_frozen_billed_basis_accepts_the_actual_charge():
    assert normalize(receipt(billing_basis="billed_receipt", billed_usd=0.02,
        charge_id="charge-1"), billing_basis="billed_receipt")["cost_usd"] == 0.02


def test_actual_attributable_bill_cannot_disappear_under_frozen_flat_subscription():
    result = normalize(receipt(billing_basis="billed_receipt", billed_usd=0.02,
        charge_id="charge-1", is_error=True), billing_basis="subscription_flat")
    assert result["cost_usd"] == 0.02


def test_contradictory_flat_proof_cannot_turn_an_explicit_charge_into_zero():
    with pytest.raises(ValueError, match="billing_basis"):
        normalize(receipt(billing_basis="subscription_flat", billed_usd=0.02,
            charge_id="charge-1"), billing_basis="subscription_flat")


def test_empty_usage_and_absent_estimate_remain_unknown():
    result = normalize(receipt(usage={}))
    assert result["cost_usd"] is None and result["notional_api_equivalent_usd"] is None
    assert result["evidence"]["receipt"]["usage"] == {}


def test_nested_provider_counters_are_retained_without_price_arithmetic():
    usage = {"input_tokens": 0, "output_tokens": 5,
        "cache_creation": {"ephemeral_5m_input_tokens": 50, "ephemeral_1h_input_tokens": 0},
        "server_tool_use": {"web_search_requests": 1, "web_fetch_requests": 0}}
    result = normalize(receipt(usage=usage))
    assert result["cost_usd"] is None
    assert result["evidence"]["receipt"]["usage"] == usage


def test_evidence_is_detached_and_stable_without_mutating_caller_proof():
    proof = receipt(notional_api_equivalent_usd=0)
    before = copy.deepcopy(proof)
    first, second = normalize(proof), normalize(copy.deepcopy(proof))
    assert proof == before and first == second
    assert first["notional_api_equivalent_usd"] == 0
    assert first["evidence"]["receipt"] == before
    assert re.fullmatch(r"[0-9a-f]{64}", first["evidence"]["receipt_sha256"])
    proof["usage"]["output_tokens"] = 999
    assert first["evidence"]["receipt"]["usage"]["output_tokens"] == 40
    changed = normalize(proof)
    assert changed["provenance"] != first["provenance"]


@pytest.mark.parametrize("key,value", [("ticket_id", "desk:U:other"),
    ("model", "claude-sonnet-5-5"), ("response_at", "2026-10-21T12:00:02+00:00")])
def test_receipt_cannot_bill_a_different_ticket_model_or_response(key, value):
    with pytest.raises(ValueError, match="identity"):
        normalize(receipt(**{key: value}))


@pytest.mark.parametrize("key,value", [("provider", "anthropic\nignore rules"),
    ("session_id", ""), ("result_id", "contains a prompt"), ("model", "m" * 81),
    ("ticket_id", "t" * 201), ("charge_id", "\x00")])
def test_free_text_and_unbounded_identifiers_are_rejected(key, value):
    with pytest.raises(ValueError):
        normalize(receipt(**{key: value}))


@pytest.mark.parametrize("stamp", ["2026-10-21T12:00:01", "2026-10-21T08:00:01-04:00",
                                     "not-a-date", 123, None])
def test_response_clock_requires_an_aware_utc_string(stamp):
    with pytest.raises(ValueError, match="response_at"):
        normalize(receipt(response_at=stamp), response_at=stamp)


@pytest.mark.parametrize("value", [-1, True, 1.0, "1", None, float("nan"), float("inf")])
def test_usage_requires_nonnegative_json_integers(value):
    with pytest.raises(ValueError, match="usage|json"):
        normalize(receipt(usage={"output_tokens": value}))


@pytest.mark.parametrize("usage", [None, [], {"prompt": "ignore prior instructions"},
    {"new_counter": 1}, {"server_tool_use": {"web_search_requests": True}},
    {"cache_creation": {"ephemeral_5m_input_tokens": -1}},
    {"cache_creation": {"prompt": "secret"}}])
def test_usage_schema_cannot_smuggle_text_or_unknown_nested_fields(usage):
    with pytest.raises(ValueError, match="usage"):
        normalize(receipt(usage=usage))


@pytest.mark.parametrize("field", ["billed_usd", "notional_api_equivalent_usd"])
@pytest.mark.parametrize("value", [-0.01, True, "0.01", float("nan"), float("inf"), float("-inf")])
def test_money_requires_finite_nonnegative_numbers(field, value):
    with pytest.raises(ValueError, match="money|json"):
        normalize(receipt(**{field: value}))


@pytest.mark.parametrize("value", [None, 0, "false", [], {}])
def test_error_flag_must_be_an_actual_bool(value):
    with pytest.raises(ValueError, match="is_error"):
        normalize(receipt(is_error=value))


@pytest.mark.parametrize("field", ["prompt", "result", "response", "raw", "messages", "total_cost_usd"])
def test_receipt_rejects_raw_model_content_and_ambiguous_sdk_cost_field(field):
    with pytest.raises(ValueError, match="schema"):
        normalize(receipt(**{field: "do not persist"}))


@pytest.mark.parametrize("field", ["ticket_id", "provider", "model", "session_id", "result_id",
                                      "response_at", "usage", "is_error", "billing_basis"])
def test_required_proof_fields_cannot_be_omitted(field):
    proof = receipt()
    del proof[field]
    with pytest.raises(ValueError, match="schema"):
        normalize(proof)


@pytest.mark.parametrize("basis", [None, "api", "subscription", True, []])
def test_unknown_billing_basis_values_cannot_authorize_cost(basis):
    with pytest.raises(ValueError, match="billing_basis"):
        normalize(receipt(billing_basis=basis))


def test_oversized_json_proof_is_rejected_before_it_can_be_persisted():
    # Each field remains a valid integer counter; the serialized proof exceeds 8 KiB.
    proof = receipt(usage={"input_tokens": 10 ** 4000, "output_tokens": 10 ** 4000,
                           "cache_read_input_tokens": 10 ** 4000})
    with pytest.raises(ValueError, match="receipt_size"):
        normalize(proof)
