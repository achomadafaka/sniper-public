"""Tests for wallet_protocol H5 helper — confidence_flag decision rule.

These lock the methodology-review-mandated thresholds. If anyone changes them
without bumping the protocol version, these tests catch it.
"""

import pytest

from sniper.analysis.wallet_protocol import (
    PEER_COUNT_INFERENCE_FLOOR,
    PEER_COUNT_OK_THRESHOLD,
    classify_peer_confidence,
)
from sniper.analysis.wallet_protocol.h5_tag_edge import should_run_inference


def test_thresholds_match_methodology_review_lock():
    """Locked at 2026-04-25 by methodology review. Mirrors B2 wallet_tag_fit."""
    assert PEER_COUNT_OK_THRESHOLD == 30
    assert PEER_COUNT_INFERENCE_FLOOR == 10


def test_confidence_flag_ok_at_30_peers():
    assert classify_peer_confidence(30) == "ok"
    assert classify_peer_confidence(100) == "ok"


def test_confidence_flag_low_population_below_30():
    assert classify_peer_confidence(29) == "low_population"
    assert classify_peer_confidence(10) == "low_population"
    assert classify_peer_confidence(0) == "low_population"


def test_inference_refused_when_n_held_below_floor():
    """n_held < 10 → never run inference, regardless of peer_count."""
    run, reason = should_run_inference(peer_count=100, n_held=9)
    assert run is False
    assert reason == "descriptive_only"


def test_inference_refused_when_peer_count_below_floor():
    """peer_count < 10 → refuse inference even if wallet has lots of held positions."""
    run, reason = should_run_inference(peer_count=9, n_held=50)
    assert run is False
    assert reason == "inference_not_run"


def test_inference_runs_when_both_thresholds_met():
    run, reason = should_run_inference(peer_count=10, n_held=10)
    assert run is True
    assert reason == ""

    run, reason = should_run_inference(peer_count=50, n_held=200)
    assert run is True


def test_boundary_n_held_exactly_10_is_descriptive_only_below_10():
    # n_held=10 with peer_count=10 is the minimum for inference to run
    run, reason = should_run_inference(peer_count=10, n_held=10)
    assert run is True
    # n_held=9 falls below
    run, reason = should_run_inference(peer_count=10, n_held=9)
    assert run is False
    assert reason == "descriptive_only"
