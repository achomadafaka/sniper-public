"""Tests for scripts/backfill_agent_predictions_run_id.py — one-shot linkage backfill."""
import json
import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_match_exactly_one_updates_row():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_ts = now - timedelta(hours=1, minutes=2)

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [{"id": "r-1", "created_at": run_ts.isoformat()}]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH" and "agent_predictions" in path:
            patched.append((path, body))
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["matched"] == 1
    assert result["ambiguous"] == 0
    assert result["orphaned"] == 0
    assert len(patched) == 1
    assert "id=eq.p-1" in patched[0][0]
    assert patched[0][1] == {"run_id": "r-1"}


def test_match_run_written_after_prediction_updates_row():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_ts = pred_ts + timedelta(minutes=2)

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [{"id": "r-1", "created_at": run_ts.isoformat()}]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH" and "agent_predictions" in path:
            patched.append((path, body))
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["matched"] == 1
    assert result["ambiguous"] == 0
    assert result["orphaned"] == 0
    assert len(patched) == 1
    assert patched[0][1] == {"run_id": "r-1"}


def test_multi_match_skips_as_ambiguous():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_a = now - timedelta(hours=1, minutes=2)
    run_b = now - timedelta(hours=1, minutes=30)  # still within 1h window

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [
        {"id": "r-a", "created_at": run_a.isoformat()},
        {"id": "r-b", "created_at": run_b.isoformat()},
    ]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH":
            patched.append(True)
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["matched"] == 0
    assert result["ambiguous"] == 1
    assert len(patched) == 0  # no UPDATE issued


def test_zero_match_skips_as_orphaned():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_too_old = now - timedelta(hours=10)  # outside 1h window

    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": None}]
    runs = [{"id": "r-old", "created_at": run_too_old.isoformat()}]

    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert result["orphaned"] == 1
    assert result["matched"] == 0


def test_rejects_agent_not_in_allow_list(capsys):
    import scripts.backfill_agent_predictions_run_id as bf

    with pytest.raises(SystemExit) as exc_info:
        bf.backfill("wallet_scout")  # not in CALIBRATION_ENABLED_AGENTS for v1
    assert exc_info.value.code == 2
    err = capsys.readouterr().out + capsys.readouterr().err
    assert "wallet_scout" in err or "not in CALIBRATION_ENABLED_AGENTS" in err


def test_does_not_overwrite_existing_run_id():
    import scripts.backfill_agent_predictions_run_id as bf

    now = datetime.now(timezone.utc)
    pred_ts = now - timedelta(hours=1)
    run_ts = now - timedelta(hours=1, minutes=2)

    # Prediction already has a run_id — must be skipped
    predictions = [{"id": "p-1", "created_at": pred_ts.isoformat(), "run_id": "existing-uuid"}]
    runs = [{"id": "r-1", "created_at": run_ts.isoformat()}]

    patched = []
    def fake_request(method, path, body=None):
        if method == "GET" and "agent_predictions" in path:
            return predictions
        if method == "GET" and "agent_runs" in path:
            return runs
        if method == "PATCH":
            patched.append(True)
            return [{}]
        return []

    with patch.object(bf, "_request", side_effect=fake_request):
        result = bf.backfill("insider_copy")

    assert len(patched) == 0
    assert result["already_linked"] == 1
