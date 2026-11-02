"""Tests for predict_tool.cmd_calibration_stats — JIT calibration metrics."""
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _run(agent="insider_copy", window_days=30, mock_rows=None):
    """Helper: call the stats function with a mocked _request response."""
    from agents.tools import predict_tool

    captured = {}
    def fake_request(method, path, body=None):
        captured["method"] = method
        captured["path"] = path
        return mock_rows or []

    with patch.object(predict_tool, "_request", side_effect=fake_request):
        result = predict_tool.cmd_calibration_stats_fn(agent, window_days)
    return result, captured


def test_empty_returns_sample_warning():
    result, _ = _run(mock_rows=[])
    assert result["total"] == 0
    assert result["graded"] == 0
    assert result["sample_warning"] is True
    assert "small_sample" in result["red_flags"]


def test_all_graded_no_red_flags():
    # 15 predictions, all graded, 9 correct = 60% accuracy; all have verification_query
    rows = []
    for i in range(15):
        rows.append({
            "id": f"uuid-{i}",
            "was_correct": i < 9,          # first 9 correct
            "confidence": "high" if i < 5 else ("medium" if i < 10 else "low"),
            "verification_query": "SELECT 1",
            "created_at": "2026-04-15T00:00:00+00:00",
        })
    result, _ = _run(mock_rows=rows)
    assert result["total"] == 15
    assert result["graded"] == 15
    assert result["graded_coverage_pct"] == 100.0
    assert result["verification_coverage_pct"] == 100.0
    assert 59.0 <= result["accuracy_pct"] <= 61.0
    assert result["sample_warning"] is False
    assert "small_sample" not in result["red_flags"]
    assert "catastrophic_accuracy" not in result["red_flags"]
    assert "low_verification_coverage" not in result["red_flags"]


def test_catastrophic_accuracy_flag():
    # 10 predictions, 1 correct = 10% accuracy, all have queries
    rows = [
        {"id": f"uuid-{i}", "was_correct": i == 0, "confidence": "medium",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
        for i in range(10)
    ]
    result, _ = _run(mock_rows=rows)
    assert result["accuracy_pct"] == 10.0
    assert "catastrophic_accuracy" in result["red_flags"]


def test_low_verification_coverage_flag():
    # 12 predictions, half with query, half without
    rows = []
    for i in range(12):
        rows.append({
            "id": f"uuid-{i}", "was_correct": i % 2 == 0, "confidence": "medium",
            "verification_query": "SELECT 1" if i < 4 else None,  # only 4/12 = 33%
            "created_at": "2026-04-15T00:00:00+00:00",
        })
    result, _ = _run(mock_rows=rows)
    assert result["verification_coverage_pct"] < 60.0
    assert "low_verification_coverage" in result["red_flags"]


def test_high_confidence_miscalibrated_flag():
    # high 0/5 = 0% ; medium 5/10 = 50%  → high.accuracy < 50 AND <= medium
    rows = []
    for i in range(5):
        rows.append({"id": f"h-{i}", "was_correct": False, "confidence": "high",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    for i in range(10):
        rows.append({"id": f"m-{i}", "was_correct": i < 5, "confidence": "medium",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    result, _ = _run(mock_rows=rows)
    assert "high_confidence_miscalibrated" in result["red_flags"]


def test_high_confidence_beats_medium_no_flag():
    # high 5/5 = 100%; medium 5/10 = 50%. Monotonic → no flag even though overall is 10/15 = 66.7
    rows = []
    for i in range(5):
        rows.append({"id": f"h-{i}", "was_correct": True, "confidence": "high",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    for i in range(10):
        rows.append({"id": f"m-{i}", "was_correct": i < 5, "confidence": "medium",
                     "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"})
    result, _ = _run(mock_rows=rows)
    assert "high_confidence_miscalibrated" not in result["red_flags"]


def test_small_sample_flag_even_if_accuracy_high():
    # 4 graded, all correct = 100% accuracy but below MIN_SAMPLE=10
    rows = [
        {"id": f"uuid-{i}", "was_correct": True, "confidence": "high",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
        for i in range(4)
    ]
    result, _ = _run(mock_rows=rows)
    assert result["sample_warning"] is True
    assert "small_sample" in result["red_flags"]


def test_request_path_applies_eligibility_filter():
    """The Supabase query must filter to run_id NOT NULL and inner-join successful runs with turns>0."""
    rows = [
        {"id": "uuid-0", "was_correct": True, "confidence": "medium",
         "verification_query": "SELECT 1", "created_at": "2026-04-15T00:00:00+00:00"}
    ]
    _, captured = _run(mock_rows=rows)
    path = captured["path"]
    # Must include run_id NOT NULL filter (run_id=not.is.null) and join-filter on agent_runs.
    assert "run_id=not.is.null" in path
    assert "agent_runs.status=eq.success" in path
    assert "agent_runs.turns=gt.0" in path
