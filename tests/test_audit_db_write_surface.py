"""Smoke tests for the Phase 0 DB write-surface audit script."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).parent.parent


def test_script_exists():
    assert (REPO / "scripts/audit_db_write_surface.py").is_file()


def test_script_runs_and_produces_output(tmp_path):
    out_path = tmp_path / "audit_test.md"
    result = subprocess.run(
        ["python3", "scripts/audit_db_write_surface.py", "--out", str(out_path)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = out_path.read_text()
    for table in (
        "low_buy_trades",
        "wallet_analysis_queue",
        "discovery_events",
        "user_analysis",
        "wallet_layer5_decision",
        "analyzer_runs",
        "wallet_analysis_batch_item",
        "wallet_profiles",
        "tag_nav_category",
        "market_outcomes",
    ):
        assert f"`{table}`" in out, f"table {table} missing from audit output"
