"""Tests for scripts/execution_profile.py — Phase A (scaffold + gating)."""
from __future__ import annotations

import pytest

from scripts.execution_profile import (
    LAYER1_TERMINAL,
    MIN_BUYS_FOR_RUN,
    MIN_RESOLVED_FOR_RUN,
    _should_run,
    _stub,
    compute_execution_profile,
)
from scripts import wallet_archetype_extractor as wae


def test_layer1_terminal_set_matches_extractor_source_of_truth():
    """Spec §7.1: this module's skip set must mirror the extractor's set.

    If _LAYER1_TERMINAL in the extractor changes, this module must be
    updated in lockstep. This guards against silent drift.
    """
    assert LAYER1_TERMINAL == frozenset(wae._LAYER1_TERMINAL)


def test_stub_shape():
    s = _stub("example")
    assert s == {"status": "not_run", "reason": "example"}


def test_stub_reason_layer1_terminal():
    run, reason = _should_run(
        classification={"archetype": "paired_outcome_arb"},
        counts={"trade_records": 500},
        returns={"resolved_position_count": 100},
        force=False,
    )
    assert run is False
    assert reason == "layer1_terminal"


def test_stub_reason_layer1_terminal_two_sided_inventory():
    run, reason = _should_run(
        classification={"archetype": "two_sided_inventory_trader"},
        counts={"trade_records": 500},
        returns={"resolved_position_count": 100},
        force=False,
    )
    assert run is False
    assert reason == "layer1_terminal"


def test_multi_outcome_basket_builder_is_not_skipped():
    """Spec §7.1 correction — basket builder is NOT terminal, gets profiled."""
    run, reason = _should_run(
        classification={"archetype": "multi_outcome_basket_builder"},
        counts={"trade_records": 500},
        returns={"resolved_position_count": 100},
        force=False,
    )
    assert run is True
    assert reason == "ok"


def test_stub_reason_insufficient_activity():
    run, reason = _should_run(
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": MIN_BUYS_FOR_RUN - 1},
        returns={"resolved_position_count": MIN_RESOLVED_FOR_RUN - 1},
        force=False,
    )
    assert run is False
    assert reason == "insufficient_activity"


def test_sufficient_resolved_meets_threshold():
    run, reason = _should_run(
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 5},
        returns={"resolved_position_count": MIN_RESOLVED_FOR_RUN},
        force=False,
    )
    assert run is True
    assert reason == "ok"


def test_sufficient_buys_meets_threshold():
    run, reason = _should_run(
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": MIN_BUYS_FOR_RUN},
        returns={"resolved_position_count": 0},
        force=False,
    )
    assert run is True
    assert reason == "ok"


def test_force_flag_overrides_layer1_terminal_skip():
    run, reason = _should_run(
        classification={"archetype": "paired_outcome_arb"},
        counts={"trade_records": 5},
        returns={"resolved_position_count": 0},
        force=True,
    )
    assert run is True
    assert reason == "forced"


def test_force_flag_overrides_insufficient_activity():
    run, reason = _should_run(
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 0},
        returns={"resolved_position_count": 0},
        force=True,
    )
    assert run is True
    assert reason == "forced"


def test_missing_classification_allows_run_when_activity_sufficient():
    """Defensive: missing classification shouldn't trip the terminal-skip."""
    run, reason = _should_run(
        classification=None,
        counts={"trade_records": 100},
        returns={"resolved_position_count": 20},
        force=False,
    )
    assert run is True


def test_none_inputs_do_not_crash():
    run, reason = _should_run(None, None, None, force=False)
    assert run is False
    assert reason == "insufficient_activity"


def test_compute_execution_profile_stubs_when_layer1_terminal():
    out = compute_execution_profile(
        wallet="0xdeadbeef",
        activity=[],
        classification={"archetype": "paired_outcome_arb"},
        counts={"trade_records": 500},
        returns={"resolved_position_count": 100},
    )
    assert out["status"] == "not_run"
    assert out["reason"] == "layer1_terminal"


def test_compute_execution_profile_api_failure_when_client_missing():
    """Phase C: gate-passing wallets with no client surface api_failure."""
    out = compute_execution_profile(
        wallet="0xdeadbeef",
        activity=[],
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 100},
        returns={"resolved_position_count": 50},
    )
    assert out["status"] == "api_failure"
    assert out["reason"] == "no_client_provided"


def test_compute_execution_profile_stubs_when_insufficient():
    out = compute_execution_profile(
        wallet="0xdeadbeef",
        activity=[],
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 5},
        returns={"resolved_position_count": 0},
    )
    assert out["status"] == "not_run"
    assert out["reason"] == "insufficient_activity"
