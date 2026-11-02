"""Smoke tests for the Phase 0 import-graph audit script."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


REPO = Path(__file__).parent.parent


def test_audit_script_exists():
    assert (REPO / "scripts/audit_import_graph.py").is_file()


def test_audit_script_has_main():
    spec = importlib.util.spec_from_file_location(
        "audit_import_graph",
        REPO / "scripts/audit_import_graph.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


def test_audit_returns_zero_on_clean_repo():
    result = subprocess.run(
        ["python3", "scripts/audit_import_graph.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
