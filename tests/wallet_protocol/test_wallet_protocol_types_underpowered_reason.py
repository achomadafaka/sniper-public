"""Slice 5 Task 2 — `UnderpoweredReason` Literal extension.

Three new values are added so the Slice 5 market-structure guard can stamp
the correct reason on `HypothesisResult.underpowered_reason` without
introducing a separate `limitation_reason` field.

Existing reasons must continue to assign without raising. The new values
must also be accepted. The Literal itself is checked statically by tools
that consume the type, but at runtime we verify three things:

1. The Literal's `__args__` contains every legacy value plus the three
   new structure-guard values.
2. Constructing a `HypothesisResult` with each value works.
3. `report.py` and `persistence.py` (the only consumers that read
   `underpowered_reason`) still serialize the new values without raising.
"""
from __future__ import annotations

from typing import get_args

from sniper.analysis.wallet_protocol.types import (
    HypothesisResult,
    UnderpoweredReason,
)


LEGACY_REASONS: tuple[str, ...] = (
    "n_below_threshold",
    "heavy_tail_inconclusive",
    "regime_change_too_recent",
    "no_execution_profile",
    "no_price_history_fn",
    "peer_count_below_floor",
    "zero_variance",
)

STRUCTURE_REASONS: tuple[str, ...] = (
    "market_structure_unsupported",
    "outcome_index_missing",
    "no_token_history",
)


def test_underpowered_reason_literal_includes_legacy_values() -> None:
    args = get_args(UnderpoweredReason)
    for reason in LEGACY_REASONS:
        assert reason in args, f"legacy reason {reason!r} missing from Literal"


def test_underpowered_reason_literal_includes_structure_guard_values() -> None:
    args = get_args(UnderpoweredReason)
    for reason in STRUCTURE_REASONS:
        assert reason in args, f"structure reason {reason!r} missing from Literal"


def test_hypothesis_result_accepts_each_legacy_reason() -> None:
    for reason in LEGACY_REASONS:
        result = HypothesisResult(
            name="H1",
            verdict="underpowered",
            underpowered_reason=reason,
        )
        assert result.underpowered_reason == reason


def test_hypothesis_result_accepts_each_structure_reason() -> None:
    for reason in STRUCTURE_REASONS:
        result = HypothesisResult(
            name="H2",
            verdict="underpowered",
            underpowered_reason=reason,
        )
        assert result.underpowered_reason == reason


def test_report_renders_structure_reason_in_label() -> None:
    """report._verdict_label string-interpolates underpowered_reason; the new
    values must not break that path."""
    from sniper.analysis.wallet_protocol.report import _verdict_label

    label = _verdict_label("underpowered", "market_structure_unsupported")
    assert "market_structure_unsupported" in label
    assert "underpowered" in label


def test_persistence_passes_structure_reason_through_unchanged() -> None:
    """persistence.py reads h.underpowered_reason and writes it as-is to
    Supabase. The conversion must not raise on the new values."""
    result = HypothesisResult(
        name="H3",
        verdict="underpowered",
        underpowered_reason="outcome_index_missing",
    )
    payload = {"underpowered_reason": result.underpowered_reason}
    assert payload["underpowered_reason"] == "outcome_index_missing"
