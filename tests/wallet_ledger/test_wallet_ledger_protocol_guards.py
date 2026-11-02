"""Slice 5 Task 3 — protocol_guards module.

Pure-logic tests for `HYPOTHESIS_STRUCTURE_CLASS` and
`market_structure_supported`. No branch wiring, no analyzer behaviour
changes — those are Task 4 and Task 5.

Spec §14 + plan §D5: 13 emitted result surfaces are classified into one
of three structure classes. The guard returns
`(supported: bool, reason: UnderpoweredReason | None)`.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.protocol_guards import (
    HYPOTHESIS_STRUCTURE_CLASS,
    market_structure_supported,
)


# ---------------------------------------------------------------------------
# Hypothesis classification table
# ---------------------------------------------------------------------------


EXPECTED_CLASSIFICATION: dict[str, str] = {
    "H1": "structure_tolerant",
    "H2": "binary_outcome_required",
    "H2_sub080": "binary_outcome_required",
    "H3": "binary_outcome_required",
    "H4": "token_price_only",
    "H5": "binary_outcome_required",
    "H6": "token_price_only",
    "FH1": "token_price_only",
    "FH2": "token_price_only",
    "FH3": "token_price_only",
    "FH4": "token_price_only",
    "FH5": "structure_tolerant",
    "FH6": "token_price_only",
}


def test_hypothesis_class_table_is_exhaustive_for_all_13_surfaces() -> None:
    assert HYPOTHESIS_STRUCTURE_CLASS == EXPECTED_CLASSIFICATION


# ---------------------------------------------------------------------------
# structure_tolerant — always True regardless of input rows
# ---------------------------------------------------------------------------


def test_structure_tolerant_returns_true_with_no_rows() -> None:
    for hid in ("H1", "FH5"):
        supported, reason = market_structure_supported(hid, [])
        assert supported is True
        assert reason is None


def test_structure_tolerant_returns_true_with_non_binary_rows() -> None:
    rows = [
        {"market_structure": "scalar", "token_id": None, "outcome_index": None},
        {"market_structure": "categorical", "token_id": "asset_a", "outcome_index": 2},
    ]
    for hid in ("H1", "FH5"):
        supported, reason = market_structure_supported(hid, rows)
        assert supported is True
        assert reason is None


# ---------------------------------------------------------------------------
# token_price_only — needs any row with non-null token_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hid", ["H4", "H6", "FH1", "FH2", "FH3", "FH4", "FH6"]
)
def test_token_price_only_supported_when_any_row_has_token_id(hid: str) -> None:
    rows = [
        {"market_structure": "categorical", "token_id": None},
        {"market_structure": "categorical", "token_id": "asset_a"},
    ]
    supported, reason = market_structure_supported(hid, rows)
    assert supported is True
    assert reason is None


@pytest.mark.parametrize(
    "hid", ["H4", "H6", "FH1", "FH2", "FH3", "FH4", "FH6"]
)
def test_token_price_only_unsupported_when_no_row_has_token_id(hid: str) -> None:
    rows = [
        {"market_structure": "scalar", "token_id": None},
        {"market_structure": "scalar", "token_id": ""},  # empty string also missing
    ]
    supported, reason = market_structure_supported(hid, rows)
    assert supported is False
    assert reason == "no_token_history"


@pytest.mark.parametrize(
    "hid", ["H4", "H6", "FH1", "FH2", "FH3", "FH4", "FH6"]
)
def test_token_price_only_unsupported_with_empty_rows(hid: str) -> None:
    supported, reason = market_structure_supported(hid, [])
    assert supported is False
    assert reason == "no_token_history"


# ---------------------------------------------------------------------------
# binary_outcome_required — needs binary rows with populated outcome_index
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hid", ["H2", "H2_sub080", "H3", "H5"])
def test_binary_outcome_required_unsupported_when_no_binary_rows(hid: str) -> None:
    rows = [
        {"market_structure": "scalar", "outcome_index": 0},
        {"market_structure": "categorical", "outcome_index": 1},
        {"market_structure": None, "outcome_index": 0},
    ]
    supported, reason = market_structure_supported(hid, rows)
    assert supported is False
    assert reason == "market_structure_unsupported"


@pytest.mark.parametrize("hid", ["H2", "H2_sub080", "H3", "H5"])
def test_binary_outcome_required_unsupported_with_empty_rows(hid: str) -> None:
    supported, reason = market_structure_supported(hid, [])
    assert supported is False
    assert reason == "market_structure_unsupported"


@pytest.mark.parametrize("hid", ["H2", "H2_sub080", "H3", "H5"])
def test_binary_outcome_required_unsupported_when_outcome_index_missing(
    hid: str,
) -> None:
    rows = [
        {"market_structure": "binary", "outcome_index": 0},
        {"market_structure": "binary", "outcome_index": None},  # offending row
    ]
    supported, reason = market_structure_supported(hid, rows)
    assert supported is False
    assert reason == "outcome_index_missing"


@pytest.mark.parametrize("hid", ["H2", "H2_sub080", "H3", "H5"])
def test_binary_outcome_required_supported_when_clean_binary(hid: str) -> None:
    rows = [
        {"market_structure": "binary", "outcome_index": 0},
        {"market_structure": "binary", "outcome_index": 1},
    ]
    supported, reason = market_structure_supported(hid, rows)
    assert supported is True
    assert reason is None


def test_binary_outcome_required_ignores_non_binary_rows_when_binary_subset_clean() -> None:
    """Mixed-structure input: presence of non-binary rows alongside clean
    binary rows must not poison the binary check, since the binary subset
    alone is enough to run a binary-only hypothesis."""
    rows = [
        {"market_structure": "scalar", "outcome_index": None},
        {"market_structure": "binary", "outcome_index": 0},
        {"market_structure": "categorical", "outcome_index": 2},
    ]
    supported, reason = market_structure_supported("H2", rows)
    assert supported is True
    assert reason is None


# ---------------------------------------------------------------------------
# Unknown hypothesis id
# ---------------------------------------------------------------------------


def test_unknown_hypothesis_id_raises_value_error() -> None:
    with pytest.raises(ValueError) as excinfo:
        market_structure_supported("Hxx_bogus", [])
    assert "Hxx_bogus" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Returned reason values are valid UnderpoweredReason members
# ---------------------------------------------------------------------------


def test_returned_reasons_are_underpoweredreason_literal_members() -> None:
    from typing import get_args

    from sniper.analysis.wallet_protocol.types import UnderpoweredReason

    valid_reasons = set(get_args(UnderpoweredReason))
    surfaced_reasons: set[str] = set()

    # Drive every guarded path through the function and collect emitted reasons.
    for hid in ("H4", "H6", "FH1", "FH2", "FH3", "FH4", "FH6"):
        _, reason = market_structure_supported(hid, [])
        if reason is not None:
            surfaced_reasons.add(reason)

    for hid in ("H2", "H2_sub080", "H3", "H5"):
        _, reason = market_structure_supported(hid, [])
        if reason is not None:
            surfaced_reasons.add(reason)
        _, reason = market_structure_supported(
            hid, [{"market_structure": "binary", "outcome_index": None}]
        )
        if reason is not None:
            surfaced_reasons.add(reason)

    assert surfaced_reasons == {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    assert surfaced_reasons.issubset(valid_reasons)
