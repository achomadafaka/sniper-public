"""Tests for Becker CSE context metadata normalization."""
from __future__ import annotations

from scripts.build_cse_pit_index import _resolution_fields


def test_resolution_fields_emit_real_per_side_metadata():
    fields = _resolution_fields(["Yes", "No"], ["0", "1"], closed=True)

    assert fields["outcomes"] == ["Yes", "No"]
    assert fields["winning_index"] == 1
    assert fields["resolved_yes"] == 0


def test_resolution_fields_keep_unresolved_none():
    fields = _resolution_fields(["Yes", "No"], ["0.4", "0.6"], closed=False)

    assert fields["outcomes"] == ["Yes", "No"]
    assert fields["winning_index"] is None
    assert fields["resolved_yes"] is None
