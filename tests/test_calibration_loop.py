"""Integration test for calibration loop v1 — exercises the full linkage path.

Not run in CI (tagged `integration`). Validates the machine-checkable success
criterion from spec §8.1 against live Supabase state after a real insider_copy
run has completed.

Usage:
    python3 -m pytest tests/test_calibration_loop.py -v -m integration
"""
import json
import os
import pytest
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

pytestmark = pytest.mark.integration


SUPABASE_URL = os.getenv("CALIBRATION_LIVE_SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("CALIBRATION_LIVE_SUPABASE_KEY", "")


@pytest.fixture(autouse=True)
def _require_live_supabase_creds():
    if not SUPABASE_URL or not SUPABASE_KEY:
        pytest.skip(
            "set CALIBRATION_LIVE_SUPABASE_URL and CALIBRATION_LIVE_SUPABASE_KEY "
            "to run calibration-loop integration tests"
        )


def _sb_get(path: str):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _deploy_cutoff_iso() -> str:
    cutoff = os.getenv("CALIBRATION_DEPLOY_TS")
    if cutoff:
        # Fail fast on malformed override rather than silently querying wrong rows.
        datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        return cutoff
    return (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()


def test_recent_insider_copy_runs_have_calibration_context():
    """§8.1(1): For all migrated-agent agent_runs rows after deploy cutoff,
    calibration_context IS NOT NULL."""
    cutoff = _deploy_cutoff_iso()
    runs = _sb_get(
        f"agent_runs?select=id,created_at,calibration_context"
        f"&agent_type=eq.insider_copy"
        f"&created_at=gte.{urllib.parse.quote(cutoff)}"
    )
    assert runs, "no insider_copy runs after deploy cutoff — trigger a run before this test"
    missing = [r for r in runs if r.get("calibration_context") is None]
    assert not missing, f"{len(missing)} insider_copy runs after deploy cutoff have NULL calibration_context"


def test_linked_predictions_have_valid_envelope():
    """§8.1(2): For any agent_predictions.run_id = <recent run>, context_snapshot
    parses as JSON AND contains calibration.saw_red_flags."""
    cutoff = _deploy_cutoff_iso()
    preds = _sb_get(
        f"agent_predictions?select=id,run_id,context_snapshot"
        f"&agent_type=eq.insider_copy"
        f"&run_id=not.is.null"
        f"&created_at=gte.{urllib.parse.quote(cutoff)}"
    )
    assert preds, "no linked insider_copy predictions after deploy cutoff — the integration path was not exercised"
    bad = []
    for p in preds:
        snap = p.get("context_snapshot")
        try:
            env = json.loads(snap) if isinstance(snap, str) else snap
            assert isinstance(env, dict)
            assert "calibration" in env
            assert "saw_red_flags" in env["calibration"]
        except Exception as exc:
            bad.append((p["id"], str(exc)))
    assert not bad, f"{len(bad)} predictions have invalid calibration envelopes: {bad[:3]}"
