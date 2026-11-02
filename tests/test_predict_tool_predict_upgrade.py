"""Tests for the upgraded cmd_predict — adds --run-id + --calibration-note."""
import json
import pytest
from unittest.mock import patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def _valid_envelope(red_flags=None, rationale=None):
    cal = {
        "saw_red_flags": red_flags or [],
        "verification_query_provided": True,
    }
    if rationale is not None:
        cal["confidence_rationale"] = rationale
    return {"schema_version": 1, "calibration": cal}


def test_predict_with_run_id_writes_run_id_column():
    from agents.tools import predict_tool

    posted = {}
    def fake_request(method, path, body=None):
        if method == "POST" and path == "agent_predictions":
            posted["body"] = body
            return [{"id": "pred-uuid-1"}]
        return []

    env = _valid_envelope()
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"
            prediction = "Wallet X score stays >=70 for 3d"
            condition = "Check user_analysis"
            confidence = "medium"
            verify_hours = 48
            verify_at = None
            query = "SELECT insider_score FROM user_analysis WHERE wallet='0xabc'"
            context = None
            run_id = "run-uuid-abc"
            calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())

    assert posted["body"]["run_id"] == "run-uuid-abc"
    # Envelope lands in context_snapshot as JSON text
    snap = json.loads(posted["body"]["context_snapshot"])
    assert snap["schema_version"] == 1
    assert snap["calibration"]["verification_query_provided"] is True


def test_predict_rejects_calibration_note_missing_schema_version():
    from agents.tools import predict_tool

    bad = json.dumps({"calibration": {"saw_red_flags": [], "verification_query_provided": True}})
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = bad
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_rejects_high_confidence_without_rationale():
    from agents.tools import predict_tool

    # High confidence + no rationale = invalid per spec §6.3
    env = _valid_envelope()  # no rationale
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "high"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_rejects_red_flags_present_without_rationale():
    from agents.tools import predict_tool

    env = _valid_envelope(red_flags=["catastrophic_accuracy"])  # flags but no rationale
    with pytest.raises(SystemExit) as exc_info:
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        predict_tool.cmd_predict(Args())
    assert exc_info.value.code == 2


def test_predict_accepts_high_confidence_with_rationale():
    from agents.tools import predict_tool

    env = _valid_envelope(rationale="first high-conf call this week")
    def fake_request(method, path, body=None):
        return [{"id": "p"}]
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "high"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = "r"; calibration_note = json.dumps(env)
        # Should not raise
        predict_tool.cmd_predict(Args())


def test_predict_without_run_id_still_works_legacy_mode():
    """Backward compat: CLI calls without --run-id keep working (writes NULL)."""
    from agents.tools import predict_tool

    posted = {}
    def fake_request(method, path, body=None):
        if method == "POST":
            posted["body"] = body
            return [{"id": "p"}]
        return []
    with patch.object(predict_tool, "_request", side_effect=fake_request):
        class Args:
            agent = "insider_copy"; prediction = "P"; condition = "C"
            confidence = "medium"; verify_hours = 48; verify_at = None
            query = "SELECT 1"; context = None
            run_id = None
            calibration_note = None
        predict_tool.cmd_predict(Args())
    # Body should not contain run_id (or should contain None)
    assert posted["body"].get("run_id") in (None, "")
