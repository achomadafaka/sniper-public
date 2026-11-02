"""Tests for sniper/settings/hl_real.py env-loaded knobs."""
import importlib


def _reload(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import sniper.settings.hl_real as S
    return importlib.reload(S)


def test_defaults_fail_closed(monkeypatch):
    for k in ("HL_REAL_ENABLED", "HL_REAL_DRY_RUN", "HL_REAL_PER_POS_USD",
              "HL_REAL_TARGETS", "HL_REAL_ALT_SHORT_ONLY"):
        monkeypatch.delenv(k, raising=False)
    S = _reload(monkeypatch)
    assert S.HL_REAL_ENABLED is False          # fail-closed: disarmed by default
    assert S.HL_REAL_DRY_RUN is True           # fail-closed: dry-run by default
    assert S.HL_REAL_PER_POS_USD == 15.0
    assert S.HL_REAL_MAX_CONCURRENT == 5
    assert S.HL_REAL_MAX_TOTAL_NOTIONAL == 75.0
    assert S.HL_REAL_MAX_CHASE_BPS == 100.0
    assert S.HL_REAL_DAY_LOSS_BREAKER_USD == 20.0
    assert S.HL_REAL_ENTRY_SLIPPAGE == 0.005 and S.HL_REAL_EXIT_SLIPPAGE == 0.01
    assert S.HL_REAL_TARGETS == ["0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8"]
    assert S.HL_REAL_ALT_SHORT_ONLY is True
    assert S.HL_REAL_BASE_URL == "https://api.hyperliquid.xyz"
    assert S.HL_REAL_TAIL_SECONDS == 5.0


def test_env_overrides(monkeypatch):
    S = _reload(monkeypatch, HL_REAL_ENABLED="1", HL_REAL_TARGETS="0xAA, 0xbb")
    assert S.HL_REAL_ENABLED is True
    assert S.HL_REAL_TARGETS == ["0xaa", "0xbb"]   # lowercased, stripped
    # restore module to ambient env for later tests
    monkeypatch.delenv("HL_REAL_ENABLED")
    monkeypatch.delenv("HL_REAL_TARGETS")
    _reload(monkeypatch)


def test_alt_short_boolean_is_trimmed_and_case_insensitive(monkeypatch):
    for raw in ("0", " false ", "FALSE", "no", " No "):
        assert _reload(monkeypatch, HL_REAL_ALT_SHORT_ONLY=raw).HL_REAL_ALT_SHORT_ONLY is False
    for raw in ("1", " true ", "TRUE", "yes", " Yes "):
        assert _reload(monkeypatch, HL_REAL_ALT_SHORT_ONLY=raw).HL_REAL_ALT_SHORT_ONLY is True
    monkeypatch.delenv("HL_REAL_ALT_SHORT_ONLY")
    _reload(monkeypatch)
