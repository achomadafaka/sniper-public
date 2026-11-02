import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_memecoin_x_api_capture.py"
FIXTURE = ROOT / "tests" / "fixtures" / "memecoin_x" / "api" / "direct.json"


def _run(args, env=None):
    clean = {key: value for key, value in os.environ.items() if not key.startswith("X_INTEL_")}
    clean.update(env or {})
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=ROOT, env=clean,
                          text=True, capture_output=True, check=False)


def test_disabled_worker_exits_before_database_or_network():
    result = _run([])
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"state": "disabled"}


def test_fixture_smoke_needs_no_credentials_and_is_deterministic(tmp_path):
    db = tmp_path / "x.db"
    result = _run(["--fixture", str(FIXTURE), "--db", str(db)])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "eligible": True, "events_created": 1, "post_id": "101", "source": "fixture"
    }
    assert db.exists()


def test_enabled_without_token_exits_two():
    result = _run([], {"X_INTEL_API_ENABLED": "1"})
    assert result.returncode == 2
    assert "bearer token" in result.stderr.lower()
