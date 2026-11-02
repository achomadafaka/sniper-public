"""Tests for predict_tool.cmd_finish_run — closes agent_runs row with terminal status."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_finish_run_patches_row_with_terminal_status(capsys):
    from agents.tools import predict_tool

    patched = {}
    def fake_request(method, path, body=None):
        patched["method"] = method
        patched["path"] = path
        patched["body"] = body
        return [{"id": "run-uuid-abc"}]

    # Pretend the parent row exists and was created recently
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            run_id = "run-uuid-abc"
            status = "success"
            turns = 28
            cost_usd = 0.95
            findings_summary = "3 strategies healthy"
        predict_tool.cmd_finish_run(Args())

    assert patched["method"] == "PATCH"
    assert "agent_runs" in patched["path"]
    assert "id=eq.run-uuid-abc" in patched["path"]
    body = patched["body"]
    assert body["status"] == "success"
    assert body["turns"] == 28
    assert body["cost_usd"] == 0.95
    assert body["findings_summary"] == "3 strategies healthy"
    # duration_seconds is computed server-side by finish-run; must be present and >= 0
    assert "duration_seconds" in body
    assert body["duration_seconds"] is None or body["duration_seconds"] >= 0
    # Must NOT write accuracy_pct (spec §6.4)
    assert "accuracy_pct" not in body


def test_finish_run_rejects_invalid_status():
    from agents.tools import predict_tool

    class Args:
        run_id = "run-uuid-abc"
        status = "partial"  # not in allow-list
        turns = None
        cost_usd = None
        findings_summary = None

    with pytest.raises(SystemExit) as exc_info:
        predict_tool.cmd_finish_run(Args())
    assert exc_info.value.code == 2


def test_finish_run_accepts_all_terminal_statuses():
    """success, error, timeout, budget_exceeded are all valid. 'running' is NOT."""
    from agents.tools import predict_tool

    def fake_request(method, path, body=None):
        return [{"id": "r"}]

    with patch.object(predict_tool, "_request", side_effect=fake_request):
        for status in ("success", "error", "timeout", "budget_exceeded"):
            class Args:
                pass
            a = Args()
            a.run_id = "r"
            a.status = status
            a.turns = None
            a.cost_usd = None
            a.findings_summary = None
            # Should not raise
            predict_tool.cmd_finish_run(a)

        # 'running' must be rejected
        class RunningArgs:
            run_id = "r"; status = "running"; turns = None; cost_usd = None; findings_summary = None
        with pytest.raises(SystemExit):
            predict_tool.cmd_finish_run(RunningArgs())
