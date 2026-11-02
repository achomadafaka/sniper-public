"""Mapping function tests — Phase 3 Workstream A Task A4.

Drives ``map_protocol_report_to_decision`` with the locked fixture vectors
from ``tests/fixtures/wallet_layer5_mapping_vectors.json`` and the totality /
purity / taxonomy invariants required by Phase 2 § 8 #2 and § 8 #3.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.types import (
    BranchReport,
    HypothesisResult,
    IndependenceCheckResult,
    ProtocolReport,
    StationarityResult,
)
from sniper.analysis.wallet_protocol.wallet_layer5_mapping import (
    MIN_CUSUM_N,
    Layer5Decision,
    map_protocol_report_to_decision,
)

FIXTURES_PATH = Path("tests/fixtures/wallet_layer5_mapping_vectors.json")
FIXTURES = json.loads(FIXTURES_PATH.read_text())


def _build_hypothesis(d: dict) -> HypothesisResult:
    return HypothesisResult(
        name=d["name"],
        verdict=d["verdict"],
        n=d.get("n", 0),
        underpowered_reason=d.get("underpowered_reason"),
    )


def _build_stationarity(d: dict | None) -> StationarityResult | None:
    if d is None:
        return None
    return StationarityResult(
        cusum_status=d.get("cusum_status", "calibrated"),
        h_used=d.get("h_used", 25.0),
        k=d.get("k", 0.5),
        signaled=d["signaled"],
        signal_index=d.get("signal_index"),
        pre_regime_n=d.get("pre_regime_n", 0),
        post_regime_n=d.get("post_regime_n", 0),
        inference_basis=d.get("inference_basis", "full_history"),
    )


def _build_independence(d: dict | None) -> IndependenceCheckResult | None:
    if d is None:
        return None
    return IndependenceCheckResult(
        n_clusters=d["n_clusters"],
        fraction_in_clusters_size_3plus=d["fraction_in_clusters_size_3plus"],
        flag=d["flag"],
        cluster_reduced_h2_excess_pp=d.get("cluster_reduced_h2_excess_pp"),
        cluster_reduced_h2_ci_lower=d.get("cluster_reduced_h2_ci_lower"),
        cluster_reduced_h2_ci_upper=d.get("cluster_reduced_h2_ci_upper"),
    )


def _build_branch(d: dict | None) -> BranchReport | None:
    if d is None:
        return None
    return BranchReport(
        branch=d["branch"],
        is_active=d.get("is_active", True),
        skip_reason=d.get("skip_reason"),
        hypotheses=[_build_hypothesis(h) for h in d.get("hypotheses", [])],
        stationarity=_build_stationarity(d.get("stationarity")),
        independence_check=_build_independence(d.get("independence_check")),
    )


def _build_report(d: dict) -> ProtocolReport:
    return ProtocolReport(
        wallet=d.get("wallet", "0xtest"),
        gate_result=d["gate_result"],
        trading_mode=d["trading_mode"],
        overall_verdict=d["overall_verdict"],
        primary_edge_type=d.get("primary_edge_type"),
        data_quality_flags=list(d.get("data_quality_flags", [])),
        dw_roi_point=d.get("dw_roi_point"),
        dw_roi_ci_lower=d.get("dw_roi_ci_lower"),
        dw_roi_ci_upper=d.get("dw_roi_ci_upper"),
        ew_roi_point=d.get("ew_roi_point"),
        held_analyst_branch=_build_branch(d.get("held_analyst_branch")),
        flipper_branch=_build_branch(d.get("flipper_branch")),
    )


@pytest.mark.parametrize("vec", FIXTURES, ids=lambda v: v["name"])
def test_mapping_vector(vec):
    pr = _build_report(vec["protocol_report"])
    decision = map_protocol_report_to_decision(pr)
    assert decision.action_decision == vec["expected_action_decision"], (
        f"{vec['name']}: action mismatch — got {decision.action_decision!r}, "
        f"expected {vec['expected_action_decision']!r}"
    )
    assert sorted(decision.reason_codes) == sorted(vec["expected_reason_codes"]), (
        f"{vec['name']}: reason_codes mismatch\n"
        f"  got:      {sorted(decision.reason_codes)}\n"
        f"  expected: {sorted(vec['expected_reason_codes'])}"
    )
    assert decision.confidence_band == vec["expected_confidence_band"], (
        f"{vec['name']}: band mismatch — got {decision.confidence_band!r}, "
        f"expected {vec['expected_confidence_band']!r}"
    )


def test_mapping_is_pure_fail_on_call():
    """Mapping must NOT call sqlite3.connect, os.getenv, os.environ.__getitem__,
    or time.time. Any call raises AssertionError so the test fails loudly.

    Patches are scoped to a context manager so they're released before pytest's
    own progress reporter runs (pytest reads os.environ['COLUMNS'] in -v mode
    between test-pass and fixture teardown — a monkeypatch fixture leaves the
    GuardedEnv active during that window and triggers a spurious internal
    error). Pre-building reports outside the with-block also avoids tripping
    on incidental os reads inside the dataclass machinery.
    """
    import os
    import sqlite3
    import time
    from unittest.mock import patch

    def _trip(*args, **kwargs):
        raise AssertionError(
            "mapping called a forbidden side-effecting function "
            "(sqlite3.connect / os.getenv / os.environ / time.time)"
        )

    class _GuardedEnv(dict):
        def __getitem__(self, key):
            _trip()

        def get(self, key, default=None):
            _trip()

    reports = [(_build_report(v["protocol_report"]), v["name"]) for v in FIXTURES]

    with (
        patch.object(sqlite3, "connect", _trip),
        patch.object(os, "getenv", _trip),
        patch.object(time, "time", _trip),
        patch.object(os, "environ", _GuardedEnv()),
    ):
        for pr, name in reports:
            d1 = map_protocol_report_to_decision(pr)
            d2 = map_protocol_report_to_decision(pr)
            assert d1 == d2, f"{name}: non-deterministic output"


def test_mapping_totality_5x4():
    """Phase 2 § 8 #2: mapping is total over OverallVerdict × GateResult.
    Every (verdict, gate) cell yields exactly one Layer5Decision (not a raise).
    TradingMode is pass-through and not switched on outside edge_detected."""
    overall_values = (
        "edge_detected",
        "no_edge",
        "underpowered",
        "data_insufficient",
        "operator_review",
    )
    gate_values = (
        "pass",
        "reject_hedge_archetype",
        "reject_insufficient_sample",
        "reject_dormant",
    )
    n_cells = 0
    for verdict in overall_values:
        for gate in gate_values:
            pr = ProtocolReport(
                wallet="0xtotal",
                gate_result=gate,
                trading_mode="held_analyst_only",
                overall_verdict=verdict,
                primary_edge_type=(
                    "entry_timing" if verdict == "edge_detected" else None
                ),
            )
            decision = map_protocol_report_to_decision(pr)
            assert decision.action_decision in {
                "validated_copy_candidate",
                "reverse_engineer_candidate",
                "study",
                "monitor",
                "exclude_for_copy",
                "anti_pattern",
            }, f"{verdict=} {gate=} → {decision.action_decision}"
            assert decision.confidence_band in {
                "high",
                "medium",
                "low",
                "underpowered",
                "n/a",
            }
            n_cells += 1
    assert n_cells == 5 * 4


def test_emitted_codes_in_taxonomy():
    """Phase 2 § 8 #3 (one-way): every reason code emitted by the mapping
    function across the fixture set appears in the spec § 4.4 stable list.

    NOT tested here: the reverse direction (every taxonomy code is
    reachable from at least one fixture). Codes such as
    ``tag_market_stats_unavailable``, ``payback_capitalization_warning``,
    ``activity_cap_hit_subgraph_used``, ``tag_grain_disagreement_leaf_vs_nav``
    are emitted upstream by the analyzer pipeline (Layer 3 / 4) before the
    mapping function sees a ``ProtocolReport``. Reverse coverage belongs to
    a Phase 4 test once those upstream emission paths exist.
    """
    spec = Path(
        "docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md"
    ).read_text()
    sec_start = spec.index("### 4.4 Reason-code taxonomy")
    sec_end = spec.index("\n---\n", sec_start)
    sec = spec[sec_start:sec_end]
    spec_codes = set(re.findall(r"`([a-zA-Z0-9_:<>]+)`", sec))
    stable_codes = {c for c in spec_codes if "<" not in c}

    emitted: set[str] = set()
    for vec in FIXTURES:
        pr = _build_report(vec["protocol_report"])
        d = map_protocol_report_to_decision(pr)
        for code in d.reason_codes:
            if re.match(r"^[FH]+\d*_underpowered:", code):
                continue  # parametric — covered by stem entry in spec
            emitted.add(code)
    missing_from_spec = emitted - stable_codes
    assert not missing_from_spec, f"emitted but not in § 4.4: {missing_from_spec}"


def test_min_cusum_n_locked():
    assert MIN_CUSUM_N == 30


def test_layer5_decision_shape():
    """Layer5Decision dataclass exposes the three locked fields."""
    pr = _build_report(FIXTURES[0]["protocol_report"])
    decision = map_protocol_report_to_decision(pr)
    assert isinstance(decision, Layer5Decision)
    assert isinstance(decision.action_decision, str)
    assert isinstance(decision.reason_codes, list)
    assert isinstance(decision.confidence_band, str)
