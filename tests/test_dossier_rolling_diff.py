"""Tests for scripts/dossier_rolling_diff.py — drift detector on copy targets."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.dossier_rolling_diff import (  # noqa: E402
    _classify_drift,
    _compute_deltas,
    _grain_quality_dist,
    _mechanism_evidence_dist,
    _thesis_candidate_count,
    diff_window_dossiers,
    main as dossier_diff_main,
)


def _stub_tables(
    *,
    schema_version: str = "v5_behavioral_descriptors",
    wallet: str = "0xtest",
    n_positions: int = 100,
    n_grains: int = 4,
    since_days_filter: str = "since_days=30;cutoff_ts=1000;fetched_at=2000",
    grain_qualities: list[str] | None = None,
    mechanism_rows: list[dict] | None = None,
    thesis_candidates: list[dict] | None = None,
    descriptor_hour_rows: int = 0,
    descriptor_cadence_rows: int = 0,
    descriptor_sizing_rows: int = 0,
    descriptor_time_in_position_rows: int = 0,
    descriptor_fills_per_position_rows: int = 0,
):
    """Build a minimal duck-typed DossierTables stand-in."""

    class _Tables:
        pass

    t = _Tables()
    t.result_semantics = [
        {"grain_id": f"g{i}", "grain_quality": q}
        for i, q in enumerate(grain_qualities or ["interpretable"] * 3 + ["underpowered"])
    ]
    t.mechanism_evidence = mechanism_rows or [
        {"mechanism": "prediction_driven", "evidence_strength": "metric_supported"},
        {"mechanism": "trading_driven", "evidence_strength": "inconclusive"},
    ]
    t.thesis_candidates = thesis_candidates or []
    t.descriptor_hour_distribution = [{} for _ in range(descriptor_hour_rows)]
    t.descriptor_cadence = [{} for _ in range(descriptor_cadence_rows)]
    t.descriptor_sizing = [{} for _ in range(descriptor_sizing_rows)]
    t.descriptor_time_in_position = [{} for _ in range(descriptor_time_in_position_rows)]
    t.descriptor_fills_per_position = [{} for _ in range(descriptor_fills_per_position_rows)]
    t.dossier_meta = [
        {"key": "schema_version", "value": schema_version},
        {"key": "wallet", "value": wallet},
        {"key": "n_positions", "value": str(n_positions)},
        {"key": "n_grains", "value": str(n_grains)},
        {"key": "since_days_filter", "value": since_days_filter},
    ]
    return t


def test_grain_quality_dist_counts_correctly():
    t = _stub_tables(grain_qualities=["interpretable", "interpretable", "underpowered"])
    assert _grain_quality_dist(t) == {"interpretable": 2, "underpowered": 1}


def test_mechanism_evidence_dist_uses_dotted_key():
    t = _stub_tables(
        mechanism_rows=[
            {"mechanism": "prediction_driven", "evidence_strength": "metric_supported"},
            {"mechanism": "prediction_driven", "evidence_strength": "inconclusive"},
        ]
    )
    assert _mechanism_evidence_dist(t) == {
        "prediction_driven.metric_supported": 1,
        "prediction_driven.inconclusive": 1,
    }


def test_thesis_candidate_count_returns_len():
    t = _stub_tables(thesis_candidates=[{"id": 1}, {"id": 2}])
    assert _thesis_candidate_count(t) == 2


def test_compute_deltas_thesis_disappeared():
    recent = _stub_tables(thesis_candidates=[], grain_qualities=["underpowered"] * 4)
    baseline = _stub_tables(
        thesis_candidates=[{"id": 1}],
        grain_qualities=["interpretable", "underpowered", "underpowered"],
    )
    deltas = _compute_deltas(recent, baseline)
    assert deltas["thesis_candidate_delta"] == -1
    assert deltas["interpretable_grain_delta"] == -1
    assert deltas["drift_flag"] == "thesis_disappeared"


def test_compute_deltas_thesis_strengthened():
    recent = _stub_tables(
        thesis_candidates=[{"id": 1}, {"id": 2}],
        grain_qualities=["interpretable"] * 3,
    )
    baseline = _stub_tables(
        thesis_candidates=[{"id": 1}],
        grain_qualities=["interpretable"] * 1,
    )
    deltas = _compute_deltas(recent, baseline)
    assert deltas["thesis_candidate_delta"] == 1
    assert deltas["interpretable_grain_delta"] == 2
    assert deltas["drift_flag"] == "thesis_strengthened"


def test_compute_deltas_no_thesis_either_window():
    recent = _stub_tables(thesis_candidates=[], grain_qualities=["underpowered"] * 4)
    baseline = _stub_tables(thesis_candidates=[], grain_qualities=["underpowered"] * 4)
    deltas = _compute_deltas(recent, baseline)
    assert deltas["drift_flag"] == "no_thesis_either_window"


def test_classify_drift_grain_quality_degraded():
    # interpretable shrank but baseline had a thesis
    assert _classify_drift(interp_delta=-2, rec_thesis=1, base_thesis=1) == "grain_quality_degraded"


def test_classify_drift_stable():
    # No change in any flag input
    assert _classify_drift(interp_delta=0, rec_thesis=1, base_thesis=1) == "stable"


def test_diff_window_dossiers_top_level_shape():
    recent = _stub_tables(
        n_positions=50,
        grain_qualities=["underpowered"] * 4,
        thesis_candidates=[],
    )
    baseline = _stub_tables(
        n_positions=200,
        grain_qualities=["interpretable", "interpretable", "underpowered"],
        thesis_candidates=[{"id": 1}],
    )
    diff = diff_window_dossiers(recent, baseline)
    assert diff["schema_version"] == "v5_behavioral_descriptors"
    assert diff["wallet"] == "0xtest"
    assert diff["recent"]["n_positions"] == 50
    assert diff["baseline"]["n_positions"] == 200
    assert diff["deltas"]["n_positions_delta"] == -150
    assert diff["deltas"]["thesis_candidate_delta"] == -1
    assert diff["deltas"]["drift_flag"] == "thesis_disappeared"


def test_cli_rejects_baseline_less_than_recent(tmp_path: Path):
    fix = tmp_path / "f.json"
    fix.write_text("{}")
    rep = tmp_path / "r.json"
    rep.write_text("{}")
    rc = dossier_diff_main([
        "--wallet", "0xtest",
        "--fixture", str(fix),
        "--protocol-report", str(rep),
        "--recent-days", "60",
        "--baseline-days", "30",
    ])
    assert rc == 2


def test_cli_rejects_non_positive_days(tmp_path: Path):
    fix = tmp_path / "f.json"
    fix.write_text("{}")
    rep = tmp_path / "r.json"
    rep.write_text("{}")
    rc = dossier_diff_main([
        "--wallet", "0xtest",
        "--fixture", str(fix),
        "--protocol-report", str(rep),
        "--recent-days", "0",
        "--baseline-days", "60",
    ])
    assert rc == 2


def test_cli_rejects_missing_fixture(tmp_path: Path):
    rep = tmp_path / "r.json"
    rep.write_text("{}")
    rc = dossier_diff_main([
        "--wallet", "0xtest",
        "--fixture", str(tmp_path / "missing.json"),
        "--protocol-report", str(rep),
        "--recent-days", "30",
        "--baseline-days", "60",
    ])
    assert rc == 2


def test_cli_end_to_end_with_synthetic_fixture(tmp_path: Path):
    """Smoke: build minimal fixture, run CLI, verify output structure."""
    SEC = 86400
    fetched_at = 2_000_000_000
    fixture = {
        "wallet": "0xdeadbeef" + "0" * 32,
        "fetched_at": fetched_at,
        "schema_version": 2,
        "closed_positions": [
            {
                "conditionId": "0x" + "a" * 64,
                "asset": "1001",
                "outcome": "Yes",
                "title": "P1",
                "avgPrice": 0.5,
                "totalBought": 10.0,
                "realizedPnl": 1.0,
                "timestamp": fetched_at - 10 * SEC,
            }
        ],
        "open_positions": [],
        "activity_rows": [
            {
                "conditionId": "0x" + "a" * 64,
                "asset": "1001",
                "type": "TRADE",
                "side": "BUY",
                "size": 10,
                "price": 0.5,
                "timestamp": fetched_at - 10 * SEC,
            }
        ],
        "leafs_by_cid": {},
        "nav_lookup": {},
    }
    fix_path = tmp_path / "fix.json"
    fix_path.write_text(json.dumps(fixture))
    rep_path = tmp_path / "rep.json"
    rep_path.write_text(json.dumps({
        "protocol_version": "v2",
        "wallet": fixture["wallet"],
    }))
    out = tmp_path / "diff.json"

    rc = dossier_diff_main([
        "--wallet", fixture["wallet"],
        "--fixture", str(fix_path),
        "--protocol-report", str(rep_path),
        "--recent-days", "30",
        "--baseline-days", "60",
        "--out-json", str(out),
    ])
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "deltas" in payload
    assert "drift_flag" in payload["deltas"]
    # Recent + baseline windows both contain the position (10 days back)
    assert payload["recent"]["n_positions"] == 1
    assert payload["baseline"]["n_positions"] == 1
