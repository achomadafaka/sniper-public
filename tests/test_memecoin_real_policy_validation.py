import math

import pytest

from sniper.settings import memecoin as cfg


@pytest.mark.parametrize("daily", [0.0, 260.0, -1.0, 1.0, math.nan, math.inf])
def test_sol_buffer_daily_cap_zero_is_unlimited(monkeypatch, daily):
    for key, value in {
        "MEMECOIN_REAL_ENABLED": True,
        "MEMECOIN_SOL_EXEC_BUFFER_CAP_USD": 175.0,
        "MEMECOIN_SOL_EXEC_BUFFER_TOPUP_USD": 130.0,
        "MEMECOIN_SOL_EXEC_BUFFER_DAILY_CAP_USD": daily,
        "MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD": 175.0,
        "MEMECOIN_SOL_EXEC_BUFFER_PERIOD_S": 30.0,
    }.items():
        monkeypatch.setattr(cfg, key, value)
    if daily == 0 or daily == 260:
        assert cfg.validate_real_exit_policy() is None
    else:
        with pytest.raises(RuntimeError, match="SOL buffer top-up"):
            cfg.validate_real_exit_policy()


@pytest.mark.parametrize(("field", "value"), [
    ("MEMECOIN_FUNDED_CLASSIFICATION_VERSION", True),
    ("MEMECOIN_FUNDED_CLASSIFICATION_VERSION", 0),
    ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.0),
    ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.01),
    ("MEMECOIN_FUNDED_REGIME_RISK_SCALE", math.nan),
    ("MEMECOIN_FUNDED_TAPE_WINDOW_S", 0),
    ("MEMECOIN_FUNDED_TAPE_MIN_OBSERVATIONS", 2),
    ("MEMECOIN_FUNDED_TAPE_MIN_SPAN_S", -1),
    ("MEMECOIN_FUNDED_POST_PEAK_RUNUP_PCT", -1),
    ("MEMECOIN_FUNDED_POST_PEAK_GIVEBACK_PCT", 101),
    ("MEMECOIN_FUNDED_REGIME_BUCKET_S", 0),
    ("MEMECOIN_FUNDED_REGIME_BASELINE_BUCKETS", 0),
    ("MEMECOIN_FUNDED_REGIME_MIN_NONEMPTY_BUCKETS", 0),
    ("MEMECOIN_FUNDED_REGIME_MIN_BASELINE_BUYS", -1),
    ("MEMECOIN_FUNDED_REGIME_MAX_STALENESS_S", math.inf),
    ("MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT", "not-a-timestamp"),
    ("MEMECOIN_REAL_NANO_DERISK_PCT", math.nan),
    ("MEMECOIN_REAL_NANO_TRAIL_PCT", 101),
    ("MEMECOIN_REAL_NANO_SL_PCT", -1),
    ("MEMECOIN_REAL_MICRO_DERISK_PCT", -1),
    ("MEMECOIN_REAL_MICRO_TRAIL_PCT", math.inf),
    ("MEMECOIN_REAL_MICRO_SL_PCT", 101),
    ("MEMECOIN_REAL_MID_DERISK_PCT", math.nan),
    ("MEMECOIN_REAL_MID_TRAIL_PCT", -1),
    ("MEMECOIN_REAL_MID_SL_PCT", 101),
    ("MEMECOIN_MAX_CONCURRENT", 0),
    ("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", math.nan),
    ("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", -1),
    ("MEMECOIN_DERISK_PENDING_STALE_S", 0),
    ("MEMECOIN_DERISK_PENDING_STALE_S", math.inf),
    ("MEMECOIN_HOT_DECODE_MAX_SIGS", 0),
    ("MEMECOIN_HOT_DECODE_MAX_PAGES", 0),
    ("MEMECOIN_HOT_DECODE_MAX_PAGES", 101),
    ("MEMECOIN_HOT_EVM_POLL_S", 0),
    ("MEMECOIN_HOT_EVM_POLL_S", math.inf),
    ("MEMECOIN_HOT_PREWARM_WORKERS", 0),
    ("MEMECOIN_HOT_PREWARM_WORKERS", True),
    ("MEMECOIN_HOT_PREWARM_DEADLINE_S", 0),
    ("MEMECOIN_HOT_PREWARM_DEADLINE_S", math.inf),
    ("MEMECOIN_HOT_STRUCTURE_WINDOW_S", "typo"),
    ("MEMECOIN_HOT_STRUCTURE_WINDOW_S", math.inf),
    ("MEMECOIN_HOT_STRUCTURE_WINDOW_S", 0),
    ("MEMECOIN_ROUTE_GONE_STREAK", 1.5),
    ("MEMECOIN_ROUTE_GONE_STREAK", math.inf),
    ("MEMECOIN_NO_PROGRESS_WINDOW_S", math.inf),
    ("MEMECOIN_NO_PROGRESS_WINDOW_S", 0),
    ("MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS", 10001),
])
def test_funded_classifier_settings_fail_closed(monkeypatch, field, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, field, value)
    with pytest.raises(RuntimeError, match="invalid armed memecoin exit policy"):
        cfg.validate_real_exit_policy()


def test_funded_classifier_settings_accept_deployed_defaults(monkeypatch):
    assert cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS == 300
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(
        cfg, "MEMECOIN_FUNDED_CONTRACT_MIN_RESERVED_AT", "2026-07-18T12:00:00+00:00")
    assert cfg.validate_real_exit_policy() is None


def test_band_policy_selection_rejects_bands_without_a_band_policy(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ("nano", "out_of_scope"))

    with pytest.raises(RuntimeError, match="MEMECOIN_REAL_BAND_POLICY_BANDS"):
        cfg.validate_real_exit_policy()


def test_present_invalid_optional_registration_is_not_treated_as_unset(monkeypatch):
    monkeypatch.setenv("BAD_OPTIONAL_FLOAT", "typo")
    monkeypatch.setenv("BAD_OPTIONAL_INT", "0")
    assert cfg._opt_float("BAD_OPTIONAL_FLOAT") == "typo"
    assert cfg._opt_int("BAD_OPTIONAL_INT") == "0"


def test_exit_engine_version_defaults_v5_and_non_v5_fails_armed_startup(monkeypatch):
    assert cfg.MEMECOIN_EXIT_ENGINE_VERSION == "v5"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    for retired in ("v3", "v2", "v6"):
        monkeypatch.setattr(cfg, "MEMECOIN_EXIT_ENGINE_VERSION", retired)
        with pytest.raises(RuntimeError, match="MEMECOIN_EXIT_ENGINE_VERSION must be v5"):
            cfg.validate_real_exit_policy()
