"""Pure normalization of caller-generated model billing proof, never model text.

SDK token usage and API-equivalent cost are estimates, not cash charges. A caller
must supply an attributable bill or explicitly freeze flat-subscription accounting.
This validates proof structure and identity; it does not authenticate a provider.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta


_REQUIRED = frozenset({"ticket_id", "provider", "model", "session_id", "result_id",
                       "response_at", "usage", "is_error", "billing_basis"})
_OPTIONAL = frozenset({"notional_api_equivalent_usd", "billed_usd", "charge_id"})
_BASES = frozenset({"unknown", "billed_receipt", "subscription_flat"})
_COUNTERS = frozenset({"input_tokens", "output_tokens", "cache_read_input_tokens",
                       "cache_creation_input_tokens", "total_tokens", "thinking_tokens",
                       "reasoning_tokens"})
_GROUPS = {
    "cache_creation": frozenset({"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}),
    "server_tool_use": frozenset({"web_search_requests", "web_fetch_requests"}),
}


def _identifier(value, field, limit=200):
    if (type(value) is not str or not 0 < len(value) <= limit
            or re.fullmatch(r"[A-Za-z0-9_.:/-]+", value) is None):
        raise ValueError("model_receipt:identifier:" + field)


def _timestamp(value):
    if type(value) is not str or len(value) > 64:
        raise ValueError("model_receipt:response_at")
    try:
        at = datetime.fromisoformat(value)
        if at.utcoffset() != timedelta(0):
            raise ValueError
    except ValueError:
        raise ValueError("model_receipt:response_at") from None


def _usage(value):
    if type(value) is not dict or value.keys() - (_COUNTERS | _GROUPS.keys()):
        raise ValueError("model_receipt:usage")
    for field, counter in value.items():
        if field in _GROUPS:
            if type(counter) is not dict or counter.keys() - _GROUPS[field]:
                raise ValueError("model_receipt:usage")
            counters = counter.values()
        else:
            counters = (counter,)
        if any(type(count) is not int or count < 0 for count in counters):
            raise ValueError("model_receipt:usage")


def _money(value):
    if value is None:
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("model_receipt:money")


def normalize_model_cost_receipt(receipt, *, ticket_id, model, response_at, billing_basis="unknown"):
    """Return exact cash USD or None, a separate estimate, and detached bounded proof.

    One receipt represents the latest final result of one call. The caller owns
    duplicate detection and bill authenticity; this function performs no I/O.
    """
    if (type(receipt) is not dict or not _REQUIRED <= receipt.keys()
            or receipt.keys() - (_REQUIRED | _OPTIONAL)):
        raise ValueError("model_receipt:schema")
    declared = receipt["billing_basis"]
    if (type(billing_basis) is not str or billing_basis not in _BASES
            or type(declared) is not str or declared not in _BASES):
        raise ValueError("model_receipt:billing_basis")
    for field in ("ticket_id", "session_id", "result_id", "charge_id"):
        if field in receipt:
            _identifier(receipt[field], field)
    _identifier(receipt["model"], "model", 80)
    _identifier(ticket_id, "ticket_id")
    _identifier(model, "model", 80)
    provider = receipt["provider"]
    if type(provider) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", provider) is None:
        raise ValueError("model_receipt:identifier:provider")
    _timestamp(receipt["response_at"])
    _timestamp(response_at)
    if (receipt["ticket_id"], receipt["model"], receipt["response_at"]) != (ticket_id, model, response_at):
        raise ValueError("model_receipt:identity")
    if type(receipt["is_error"]) is not bool:
        raise ValueError("model_receipt:is_error")
    _usage(receipt["usage"])
    estimate = receipt.get("notional_api_equivalent_usd")
    billed = receipt.get("billed_usd")
    _money(estimate)
    _money(billed)
    actual_bill = declared == "billed_receipt" and billed is not None and "charge_id" in receipt
    if ((billing_basis != "unknown" and billing_basis != declared and not actual_bill)
            or (declared == "subscription_flat" and billed is not None and billed != 0)):
        raise ValueError("model_receipt:billing_basis")
    try:
        encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("model_receipt:json") from None
    if len(encoded.encode()) > 8192:
        raise ValueError("model_receipt:receipt_size")
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    cost, basis = None, "unknown"
    if actual_bill:
        cost, basis = billed, "billed_receipt"
    elif billing_basis == declared == "subscription_flat":
        cost, basis = 0, "subscription_flat"
    return {"cost_usd": cost, "notional_api_equivalent_usd": estimate,
            "provenance": "model:" + provider + ":" + basis + ":" + digest,
            "evidence": {"receipt": json.loads(encoded), "receipt_sha256": digest}}
