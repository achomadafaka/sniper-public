from pathlib import Path

import pytest


def test_disabled_settings_need_no_secret():
    from sniper.settings.x_intel import XIntelSettings

    cfg = XIntelSettings.from_env({})
    assert cfg.enabled is False
    assert cfg.max_post_reads_per_day == 500


def test_enabled_settings_require_secret_and_strict_bounds(tmp_path):
    from sniper.settings.x_intel import XIntelSettings

    with pytest.raises(ValueError, match="bearer token"):
        XIntelSettings.from_env({"X_INTEL_API_ENABLED": "1"})
    with pytest.raises(ValueError, match="0 or 1"):
        XIntelSettings.from_env({"X_INTEL_API_ENABLED": "yes"})
    with pytest.raises(ValueError, match="positive"):
        XIntelSettings.from_env({
            "X_INTEL_API_ENABLED": "1", "X_INTEL_BEARER_TOKEN": "token",
            "X_INTEL_DB_PATH": str(tmp_path / "x.db"),
            "X_INTEL_MAX_POST_READS_PER_DAY": "0",
        })


def test_enabled_settings_redact_token_and_reject_funded_database(tmp_path):
    from sniper.settings.x_intel import XIntelSettings

    env = {
        "X_INTEL_API_ENABLED": "1", "X_INTEL_BEARER_TOKEN": "secret-token",
        "X_INTEL_DB_PATH": str(tmp_path / "x.db"),
    }
    cfg = XIntelSettings.from_env(env)
    assert "secret-token" not in repr(cfg)
    with pytest.raises(ValueError, match="isolated X database"):
        XIntelSettings.from_env({**env, "X_INTEL_DB_PATH": "sniper_tracker.db"})
