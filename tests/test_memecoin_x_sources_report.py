import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from sniper.analysis.memecoin_x.read_models import build_source_graph
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables


ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2026-07-14T00:00:00+00:00"


def _db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    conn.execute(
        """
        INSERT INTO memecoin_x_capture_runs
          (run_id,parser_version,started_at,finished_at,clock_domain,status)
        VALUES ('r1',1,'2026-07-13T01:00:00+00:00','2026-07-13T01:01:00+00:00',
                'observed_real_utc','ok')
        """
    )
    conn.commit()
    return conn


def test_help_runs_without_pythonpath(tmp_path):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/report_memecoin_x_sources.py"), "--help"],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--db" in result.stdout and "--as-of" in result.stdout


def test_report_uses_corrected_clock_and_readonly_connection(tmp_path, monkeypatch):
    path = tmp_path / "x.sqlite"
    _db(path).close()
    import scripts.report_memecoin_x_sources as module

    calls = []
    monkeypatch.setattr(module, "x_observed_at_or_none", lambda *, path: (
        calls.append(path) or AS_OF, {"healthy": True}))
    result = module.report_from_path(
        path, as_of=None, offset_path="/trusted/offset.json")
    assert calls == ["/trusted/offset.json"]
    assert result["as_of"] == AS_OF
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        module._open_report_connection(path).execute(
            "INSERT INTO memecoin_x_accounts(handle,role) VALUES ('x','suggested')")


def test_report_rejects_missing_healthy_clock(tmp_path, monkeypatch):
    import scripts.report_memecoin_x_sources as module

    monkeypatch.setattr(module, "x_observed_at_or_none",
                        lambda *, path: (None, {"reason": "stale_offset"}))
    with pytest.raises(ValueError, match="healthy corrected clock required: stale_offset"):
        module.report_from_path(tmp_path / "missing.sqlite", as_of=None,
                                offset_path="offset.json")


def test_report_json_equals_read_model_for_fixture(tmp_path):
    path = tmp_path / "x.sqlite"
    conn = _db(path)
    expected = build_source_graph(conn, as_of=AS_OF)
    conn.close()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/report_memecoin_x_sources.py"),
         "--db", str(path), "--as-of", AS_OF, "--json"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize("setup", ["missing", "no_tables"])
def test_missing_database_or_tables_returns_bounded_unavailable_error(tmp_path, setup):
    path = tmp_path / "x.sqlite"
    if setup == "no_tables":
        sqlite3.connect(path).close()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/report_memecoin_x_sources.py"),
         "--db", str(path), "--as-of", AS_OF, "--json"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.startswith("source graph unavailable:")
    assert len(result.stderr) <= 300
    assert "Traceback" not in result.stderr
