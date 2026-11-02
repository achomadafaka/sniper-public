"""Tests for calibration loop constants in web_settings.py."""
import pytest


def test_calibration_constants_exist():
    import sniper.settings.web as ws
    assert ws.CALIBRATION_WINDOW_DAYS == 30
    assert ws.CALIBRATION_MIN_SAMPLE == 10
    assert ws.CALIBRATION_CATASTROPHIC_THRESHOLD == 0.20
    assert ws.CALIBRATION_COVERAGE_THRESHOLD == 0.60
    assert ws.CALIBRATION_HIGH_CONF_THRESHOLD == 0.50


def test_calibration_enabled_agents_is_list_with_insider_copy():
    import sniper.settings.web as ws
    assert isinstance(ws.CALIBRATION_ENABLED_AGENTS, list)
    assert "insider_copy" in ws.CALIBRATION_ENABLED_AGENTS
