"""Tests for predict_tool.cmd_start_run — opens agent_runs row with calibration snapshot."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_start_run_posts_row_with_running_status_and_calibration_context(capsys):
    from agents.tools import predict_tool

    stats_stub = {
        "agent_type": "insider_copy",
        "total": 10, "graded": 10, "accuracy_pct": 60.0,
        "red_flags": [], "sample_warning": False,
    }
    inserted = {}

    def fake_request(method, path, body=None):
        if method == "POST" and path == "agent_runs":
            inserted["body"] = body
            return [{"id": "run-uuid-abc"}]
        pytest.fail(f"unexpected request: {method} {path}")

    with patch.object(predict_tool, "cmd_calibration_stats_fn", return_value=stats_stub), \
         patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            model = "claude-sonnet-4-6"
            window_days = 30
        predict_tool.cmd_start_run(Args())

    body = inserted["body"]
    assert body["agent_type"] == "insider_copy"
    assert body["status"] == "running"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["calibration_context"] == stats_stub

    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["run_id"] == "run-uuid-abc"
    # Formatted calibration block must be present on stdout for wrapper capture
    assert "CALIBRATION STATE" in out["calibration_block"]


def test_start_run_block_lists_red_flags_when_present(capsys):
    from agents.tools import predict_tool

    stats_stub = {
        "agent_type": "insider_copy",
        "total": 18, "graded": 18, "accuracy_pct": 5.6,
        "red_flags": ["catastrophic_accuracy", "low_verification_coverage"],
        "sample_warning": False, "by_confidence": {},
        "verification_coverage_pct": 37.0, "graded_coverage_pct": 100.0,
    }

    def fake_request(method, path, body=None):
        return [{"id": "run-uuid-xyz"}]

    with patch.object(predict_tool, "cmd_calibration_stats_fn", return_value=stats_stub), \
         patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            model = None
            window_days = 30
        predict_tool.cmd_start_run(Args())

    out = json.loads(capsys.readouterr().out)
    block = out["calibration_block"]
    assert "catastrophic_accuracy" in block
    assert "low_verification_coverage" in block
    assert "RED FLAGS:" in block
    assert "none" not in block.lower()  # "RED FLAGS: none" must NOT appear when flags exist
