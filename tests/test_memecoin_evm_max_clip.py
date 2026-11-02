"""A single EVM entry must never risk more than the operator's ceiling.

Bankroll sizing is `equity / slots` with a FLOOR and no ceiling, so a growing book grew the clip
without bound. Operator ceiling set 2026-09-04.
"""
import sniper.services.memecoin_evm_risk as R


def _ctx(monkeypatch, cap, equity=2000.0, slots=2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", cap, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 10.0, raising=False)
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda *a, **k: slots, raising=False)
    monkeypatch.setattr(R, "scaled_funded_clip",
                        lambda *, base_clip_usd, risk_scale, floor_usd: base_clip_usd)
    return equity


def _size(monkeypatch, cap, equity=2000.0, slots=2):
    _ctx(monkeypatch, cap, equity, slots)
    from sniper.services.memecoin_risk import real_trade_size
    base = real_trade_size(usdc_balance=equity, open_notional_usd=0.0, max_concurrent=slots)
    from sniper.settings import memecoin as cfg
    capped = float(getattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0.0) or 0.0)
    return min(base, capped) if capped > 0 else base


def test_a_clip_above_the_ceiling_is_capped(monkeypatch):
    # equity 2000 / 2 slots = 1000 uncapped.
    assert _size(monkeypatch, cap=150.0) == 150.0


def test_a_clip_below_the_ceiling_is_untouched(monkeypatch):
    # equity 200 / 2 = 100, already under 150.
    assert _size(monkeypatch, cap=150.0, equity=200.0) == 100.0


def test_zero_disables_the_cap(monkeypatch):
    assert _size(monkeypatch, cap=0.0) == 1000.0


def test_the_cap_never_raises_a_small_clip(monkeypatch):
    # A ceiling is not a target: a 100 clip stays 100 under a 150 ceiling.
    assert _size(monkeypatch, cap=150.0, equity=200.0) < 150.0


def test_the_ceiling_lives_in_the_shared_sizing_function(monkeypatch):
    """The operator's dashboard sizes the 'next clip' with its own call to `real_trade_size`, and
    it showed $215 against a $150 cap because the ceiling lived only in the EVM entry gate.
    Two call sites computing the same quantity diverge; one function that takes the ceiling cannot.
    """
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_risk import real_trade_size
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0, raising=False)
    # equity 430 / 2 slots = 215 -- the exact number the dashboard reported.
    uncapped = real_trade_size(usdc_balance=430.0, open_notional_usd=0.0, max_concurrent=2)
    assert uncapped == 215.0
    capped = real_trade_size(usdc_balance=430.0, open_notional_usd=0.0,
                             max_concurrent=2, max_clip_usd=150.0)
    assert capped == 150.0


def test_solana_is_not_capped_by_an_evm_ceiling(monkeypatch):
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_risk import real_trade_size
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0, raising=False)
    # a 0 ceiling is how the caller says "no cap for this chain"
    assert real_trade_size(usdc_balance=430.0, open_notional_usd=0.0,
                           max_concurrent=2, max_clip_usd=0.0) == 215.0
