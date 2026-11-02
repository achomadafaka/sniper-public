from __future__ import annotations

import hashlib

import pytest

from sniper.research.memecoin_opportunity.identity import (
    canonical_json,
    content_fingerprint,
    normalize_identity,
    require_utc_iso,
)
from sniper.research.memecoin_opportunity.types import (
    CanonicalEvent,
    ChainOrder,
    ConfirmationState,
    EventKind,
    LifecycleState,
    NetworkId,
    RawEvent,
    VenueId,
)


NOW = "2026-07-26T14:00:00Z"


def _raw_event(**overrides) -> RawEvent:
    payload = overrides.pop("raw_payload_json", '{"x":1}')
    values = {
        "raw_event_id": "raw-1",
        "network_id": NetworkId.ROBINHOOD_4663,
        "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        "source_key": "4663:bags:provider-a",
        "source_provider": "provider-a",
        "source_cursor": "101:0:2",
        "token_id": "0xAbCd",
        "transaction_id": "0xDeF0",
        "chain_order": ChainOrder(101, 0, 2, "0xBlock"),
        "chain_timestamp": NOW,
        "received_at": "2026-07-26T14:00:00.100000Z",
        "confirmation": ConfirmationState.TENTATIVE,
        "raw_payload_json": payload,
        "raw_payload_hash": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    }
    values.update(overrides)
    return RawEvent(**values)


def _canonical_event(**overrides) -> CanonicalEvent:
    values = {
        "canonical_event_id": "event-1",
        "raw_event_id": "raw-1",
        "launch_id": "launch-1",
        "network_id": NetworkId.ROBINHOOD_4663,
        "venue_id": VenueId.BAGS_ROBINHOOD_4663,
        "token_id": "0xAbCd",
        "lifecycle": LifecycleState.PRE_MIGRATION,
        "event_kind": EventKind.BUY,
        "transaction_id": "0xDeF0",
        "chain_order": ChainOrder(101, 0, 2, "0xBlock"),
        "chain_timestamp": NOW,
        "received_at": "2026-07-26T14:00:00.100000Z",
        "decoded_at": "2026-07-26T14:00:00.200000Z",
        "state_applied_at": "2026-07-26T14:00:00.250000Z",
        "confirmation": ConfirmationState.TENTATIVE,
        "actor_id": "0xFEED",
        "quote_delta_atomic": 25_000_000,
        "token_delta_atomic": 9_000_000_000_000_000_000_000,
        "progress_bps": 500,
        "creator_linked": False,
        "decoder_version": "bags-4663-v1",
    }
    values.update(overrides)
    return CanonicalEvent(**values)


def test_evm_identity_is_lowercased_but_solana_is_preserved():
    assert normalize_identity("4663", "0xAbCd") == "0xabcd"
    assert normalize_identity("solana", "AbCd") == "AbCd"


def test_canonical_json_and_fingerprint_ignore_dict_insertion_order():
    left = {"b": 2, "a": 1}
    right = {"a": 1, "b": 2}

    assert canonical_json(left) == '{"a":1,"b":2}'
    assert canonical_json(left) == canonical_json(right)
    assert content_fingerprint(left) == content_fingerprint(right)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError):
        canonical_json({"value": value})


def test_require_utc_iso_canonicalizes_zero_offset():
    assert require_utc_iso("2026-07-26T10:00:00+00:00") == "2026-07-26T10:00:00Z"


@pytest.mark.parametrize(
    "value",
    ["2026-07-26T10:00:00", "2026-07-26T10:00:00-04:00", "bad", ""],
)
def test_require_utc_iso_rejects_naive_non_utc_or_invalid_values(value):
    with pytest.raises(ValueError):
        require_utc_iso(value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"block_or_slot": -1, "transaction_index": 0, "event_index": 0, "block_hash": None},
        {"block_or_slot": 1, "transaction_index": -1, "event_index": 0, "block_hash": None},
        {"block_or_slot": 1, "transaction_index": 0, "event_index": -1, "block_hash": None},
    ],
)
def test_chain_order_rejects_negative_coordinates(kwargs):
    with pytest.raises(ValueError):
        ChainOrder(**kwargs)


def test_raw_event_rejects_payload_hash_mismatch():
    with pytest.raises(ValueError, match="raw_payload_hash"):
        _raw_event(raw_payload_hash="wrong")


def test_raw_event_normalizes_evm_identity_at_construction():
    event = _raw_event()

    assert event.token_id == "0xabcd"
    assert event.transaction_id == "0xdef0"
    assert event.chain_order.block_hash == "0xblock"


def test_canonical_event_normalizes_actor_and_handles_unbounded_atomic_integer():
    event = _canonical_event()

    assert event.actor_id == "0xfeed"
    assert event.token_delta_atomic == 9_000_000_000_000_000_000_000


def test_canonical_event_rejects_cross_venue_network_pair():
    with pytest.raises(ValueError, match="venue_id"):
        _canonical_event(
            network_id=NetworkId.ROBINHOOD_4663,
            venue_id=VenueId.PUMPFUN_SOLANA,
        )


@pytest.mark.parametrize("progress_bps", [-1, 10_001])
def test_canonical_event_rejects_progress_outside_curve(progress_bps):
    with pytest.raises(ValueError, match="progress_bps"):
        _canonical_event(progress_bps=progress_bps)


@pytest.mark.parametrize("field", ["canonical_event_id", "raw_event_id", "launch_id", "decoder_version"])
def test_canonical_event_rejects_blank_required_identity(field):
    with pytest.raises(ValueError, match=field):
        _canonical_event(**{field: "  "})


@pytest.mark.parametrize("field", ["quote_delta_atomic", "token_delta_atomic"])
def test_canonical_event_rejects_boolean_atomic_values(field):
    with pytest.raises(ValueError, match=field):
        _canonical_event(**{field: True})


def test_correction_event_requires_correction_target():
    with pytest.raises(ValueError, match="correction_of"):
        _canonical_event(event_kind=EventKind.CORRECTION, correction_of=None)


def test_observability_timestamps_must_be_monotonic():
    with pytest.raises(ValueError, match="decoded_at"):
        _canonical_event(decoded_at="2026-07-26T14:00:00.050000Z")
    with pytest.raises(ValueError, match="state_applied_at"):
        _canonical_event(state_applied_at="2026-07-26T14:00:00.150000Z")
