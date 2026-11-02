"""Phase 7.2 Task 4 — lint the systemd unit + timer for the ledger
pipeline cron path.

Pure offline. Parses the .service and .timer files and asserts the
contract elements per plan v2 D5.
"""
from __future__ import annotations

import configparser
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = REPO_ROOT / "systemd"
SERVICE_PATH = SYSTEMD_DIR / "sniper-ledger-pipeline.service"
TIMER_PATH = SYSTEMD_DIR / "sniper-ledger-pipeline.timer"


def _parse_unit(path: Path) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(strict=False, interpolation=None)
    cp.optionxform = str  # preserve case on keys
    cp.read(path)
    return cp


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def test_service_unit_exists() -> None:
    assert SERVICE_PATH.is_file(), f"missing: {SERVICE_PATH}"


def test_service_is_oneshot() -> None:
    cp = _parse_unit(SERVICE_PATH)
    assert cp["Service"]["Type"] == "oneshot"


def test_service_execstart_matches_cli_invocation() -> None:
    cp = _parse_unit(SERVICE_PATH)
    cmd = cp["Service"]["ExecStart"]
    assert "scripts/run_ledger_pipeline.py" in cmd
    assert "--from-followed" in cmd
    assert "--auto-window" in cmd
    assert "--write" in cmd


def test_service_working_directory_is_repo_root() -> None:
    cp = _parse_unit(SERVICE_PATH)
    wd = cp["Service"]["WorkingDirectory"]
    assert wd == "/home/sniper/sniper"


def test_service_logs_to_journald() -> None:
    cp = _parse_unit(SERVICE_PATH)
    assert cp["Service"]["StandardOutput"] == "journal"
    assert cp["Service"]["StandardError"] == "journal"


def test_service_has_install_section_with_default_target() -> None:
    cp = _parse_unit(SERVICE_PATH)
    assert "Install" in cp
    assert cp["Install"]["WantedBy"] == "default.target"


# ---------------------------------------------------------------------------
# Timer
# ---------------------------------------------------------------------------


def test_timer_unit_exists() -> None:
    assert TIMER_PATH.is_file(), f"missing: {TIMER_PATH}"


def test_timer_oncalendar_is_hourly() -> None:
    cp = _parse_unit(TIMER_PATH)
    assert cp["Timer"]["OnCalendar"] == "hourly"


def test_timer_is_persistent() -> None:
    cp = _parse_unit(TIMER_PATH)
    assert cp["Timer"]["Persistent"].lower() == "true"


def test_timer_has_300s_random_delay() -> None:
    cp = _parse_unit(TIMER_PATH)
    assert cp["Timer"]["RandomizedDelaySec"] == "300"


def test_timer_targets_the_service_unit() -> None:
    cp = _parse_unit(TIMER_PATH)
    assert cp["Timer"]["Unit"] == "sniper-ledger-pipeline.service"


def test_timer_install_section_uses_timers_target() -> None:
    cp = _parse_unit(TIMER_PATH)
    assert "Install" in cp
    assert cp["Install"]["WantedBy"] == "timers.target"


# ---------------------------------------------------------------------------
# Operator-enable command pinned
# ---------------------------------------------------------------------------


def test_unit_files_pin_operator_enable_command() -> None:
    """Exact-string pin so a future silent rename surfaces. Either
    unit may carry the comment, or both may; at least one must."""
    expected = (
        "systemctl --user enable --now "
        "sniper-ledger-pipeline.timer"
    )
    bodies = SERVICE_PATH.read_text() + "\n" + TIMER_PATH.read_text()
    assert expected in bodies, (
        "operator-enable command not pinned in either unit comment"
    )
