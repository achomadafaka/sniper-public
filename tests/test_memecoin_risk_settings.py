import importlib

import pytest


def test_risk_envelope_defaults(monkeypatch):
    for key in (
        "MEMECOIN_REAL_ENABLED", "MEMECOIN_TRADE_SIZE_USD", "MEMECOIN_MAX_CONCURRENT",
        "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", "MEMECOIN_DAILY_LOSS_LIMIT_USD",
        "MEMECOIN_MIN_TRADE_FLOOR_USD", "MEMECOIN_MAX_ROUNDTRIP_COST_PCT",
        "MEMECOIN_GAS_RETRY_FACTOR",
    ):
        monkeypatch.delenv(key, raising=False)
    import sniper.settings.memecoin as s
    # RELOAD IT BACK, ALWAYS (2026-09-23). `monkeypatch` restores the ENVIRONMENT at teardown and
    # nothing restores the MODULE, so a reload here left the shared settings object holding the
    # code defaults for every later test in this xdist worker. conftest pins
    # MEMECOIN_MAX_CONCURRENT=6 and the default is 7, so
    # test_memecoin_risk_context::test_context_folds_confirmed_and_pending_into_effective failed
    # whenever the two files landed in the same worker in this order -- an intermittent red under
    # -n 6 and deterministic under -n 0. Same try/finally shape test_memecoin_evm_v3_settings
    # already uses.
    try:
        importlib.reload(s)
        assert s.MEMECOIN_REAL_ENABLED is False      # armed only by explicit opt-in
        assert s.MEMECOIN_TRADE_SIZE_USD == 10.0
        assert s.MEMECOIN_MAX_CONCURRENT == 7
        assert s.MEMECOIN_MAX_TOTAL_EXPOSURE_USD == 0.0
        assert s.MEMECOIN_DAILY_LOSS_LIMIT_USD == 30.0
        assert s.MEMECOIN_MIN_TRADE_FLOOR_USD == 5.0  # provisional; shadow-measured later
        assert s.MEMECOIN_MAX_ROUNDTRIP_COST_PCT == 3.0
        assert s.MEMECOIN_GAS_RETRY_FACTOR == 2.0
    finally:
        monkeypatch.undo()
        importlib.reload(s)


def test_the_defaults_probe_leaves_the_shared_settings_module_as_it_found_it():
    """The restore above is invisible unless something reads it, and pytest runs a file's tests in
    order, so this is the check that fails if it is ever removed: the module must reflect the
    PINNED environment from conftest, not the code defaults the probe reloaded."""
    import os

    import sniper.settings.memecoin as s
    assert s.MEMECOIN_MAX_CONCURRENT == int(os.environ["MEMECOIN_MAX_CONCURRENT"]) == 6
    assert s.MEMECOIN_TRADE_SIZE_USD == float(os.environ["MEMECOIN_TRADE_SIZE_USD"]) == 10.0


def test_real_exit_policy_validation_is_noop_when_disarmed(monkeypatch):
    import sniper.settings.memecoin as s
    monkeypatch.setattr(s, "MEMECOIN_REAL_ENABLED", False)
    monkeypatch.setattr(s, "MEMECOIN_REAL_MAX_HOLD_S", 0.0)
    assert s.validate_real_exit_policy() is None


@pytest.mark.parametrize("field,value", [
    ("MEMECOIN_REAL_MAX_HOLD_S", 0.0),
    ("MEMECOIN_REAL_MAX_HOLD_S", float("nan")),
    ("MEMECOIN_EXIT_SLIPPAGE_LADDER", []),
    ("MEMECOIN_EXIT_SLIPPAGE_LADDER", [3.0, 0.0]),
    ("MEMECOIN_EXIT_SLIPPAGE_LADDER", [10.0, 3.0]),
    ("MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 0),
    ("MEMECOIN_REAL_STOP_PCT", -1.0),
    ("MEMECOIN_REAL_STOP_PCT", 101.0),
    ("MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S", 0.0),
    ("MEMECOIN_REAL_MIRROR_HOLDER_WINDOW_S", float("nan")),
    ("MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD", 0.0),
    ("MEMECOIN_REAL_MIRROR_MIN_REMAINING_USD", float("inf")),
])
def test_real_exit_policy_rejects_nonterminal_or_malformed_config(monkeypatch, field, value):
    import sniper.settings.memecoin as s
    monkeypatch.setattr(s, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(s, "MEMECOIN_REAL_MAX_HOLD_S", 86400.0)
    monkeypatch.setattr(s, "MEMECOIN_EXIT_SLIPPAGE_LADDER", [3.0, 10.0, 25.0])
    monkeypatch.setattr(s, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 4)
    monkeypatch.setattr(s, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(s, field, value)
    with pytest.raises(RuntimeError):
        s.validate_real_exit_policy()


def test_real_exit_policy_accepts_positive_terminal_config(monkeypatch):
    import sniper.settings.memecoin as s
    monkeypatch.setattr(s, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(s, "MEMECOIN_REAL_MAX_HOLD_S", 86400.0)
    monkeypatch.setattr(s, "MEMECOIN_EXIT_SLIPPAGE_LADDER", [3.0, 10.0, 25.0])
    monkeypatch.setattr(s, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 4)
    monkeypatch.setattr(s, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(s, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(s, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    assert s.validate_real_exit_policy() is None
