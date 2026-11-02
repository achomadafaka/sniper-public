"""Pattern A entrypoint guards — running legacy daemons as scripts under
LEGACY_PIPELINE_DISABLED=1 must exit non-zero before any DB writes.

Spec ref: docs/superpowers/specs/2026-04-30-analyzer-completion-and-archive-design.md §5.1
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent


def _run_under_guard(
    script: str, env_var: str = "LEGACY_PIPELINE_DISABLED"
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env[env_var] = "1"
    # Defensive: when testing one var alone, ensure the other isn't also set.
    if env_var == "LEGACY_PIPELINE_DISABLED":
        env.pop("LEGACY_STREAM_FROZEN", None)
    else:
        env.pop("LEGACY_PIPELINE_DISABLED", None)
    return subprocess.run(
        [sys.executable, str(REPO / script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_stream_low_buys_blocks_under_legacy_pipeline_disabled():
    result = _run_under_guard("stream_low_buys.py", "LEGACY_PIPELINE_DISABLED")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
    assert "stream_low_buys" in result.stderr


def test_stream_low_buys_blocks_under_legacy_stream_frozen():
    result = _run_under_guard("stream_low_buys.py", "LEGACY_STREAM_FROZEN")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()


def test_analyze_users_blocks_under_legacy_pipeline_disabled():
    result = _run_under_guard("analyze_users.py", "LEGACY_PIPELINE_DISABLED")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
    assert "analyze_users" in result.stderr


def test_analyze_users_blocks_under_legacy_stream_frozen():
    result = _run_under_guard("analyze_users.py", "LEGACY_STREAM_FROZEN")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "blocked" in result.stderr.lower()
