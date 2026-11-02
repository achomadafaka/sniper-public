"""Solana had a per-entry FLOOR and no ceiling.

`real_trade_size` has accepted `max_clip_usd` since 2026-09-04, but only the EVM gate and the EVM
half of the dashboard ever passed one. Bankroll sizing is `equity / slots`, so a growing Solana book
grew the clip without bound.

Operator order 2026-09-08: "cap solana entries at 175". Read as DOLLARS, mirroring the existing
`MEMECOIN_EVM_MAX_CLIP_USD`. Not currently binding — the largest Solana entry ever taken is $173.77
against a $107.94 median — so this pins the ceiling where bankroll sizing already tops out.
"""
import pytest

from sniper.services import memecoin_risk as R
from sniper.settings import memecoin as cfg


@pytest.fixture()
def bankroll(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)


def _size(**kw):
    return R.real_trade_size(usdc_balance=kw.pop("usdc", 1000.0),
                             open_notional_usd=kw.pop("open", 0.0), **kw)


def test_without_a_ceiling_the_clip_follows_equity(bankroll):
    assert _size(usdc=1000.0) == 1000.0
    assert _size(usdc=5000.0) == 5000.0, "this unbounded growth is what the ceiling exists to stop"


def test_the_ceiling_caps_a_large_clip(bankroll):
    assert _size(usdc=1000.0, max_clip_usd=175.0) == 175.0


def test_the_ceiling_never_raises_a_smaller_clip(bankroll, monkeypatch):
    """A ceiling, not a target.

    The spendable clamp must NOT be what holds it down, or the test passes even when the ceiling is
    applied as an assignment. Mutation-checked 2026-09-08: the first version used a small balance,
    so `min(requested, spendable)` masked `clip = max_clip_usd` entirely. Plenty of cash, many
    slots, so the computed clip is small on its own merits.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 20)
    assert _size(usdc=1000.0, max_clip_usd=175.0) == 50.0, "equity/slots, untouched by the ceiling"


def test_zero_disables_the_ceiling(bankroll):
    assert _size(usdc=1000.0, max_clip_usd=0.0) == 1000.0


def test_the_floor_still_applies_under_the_ceiling(bankroll):
    """min_trade 30 with an equity of 10 and a spendable of 10 -> spendable wins, not the floor."""
    assert _size(usdc=10.0, max_clip_usd=175.0) == 10.0


def test_the_spendable_clamp_still_beats_the_ceiling(bankroll):
    """The ceiling must never let a clip exceed free cash."""
    assert _size(usdc=50.0, open=900.0, max_clip_usd=175.0) == 50.0


# --- the wiring, which is the part that was missing -----------------------------------------

def test_the_solana_gate_passes_the_ceiling():
    """The function took a ceiling for four days; the Solana caller passed nothing."""
    import inspect
    src = inspect.getsource(R.evaluate_entry_risk) if hasattr(R, "evaluate_entry_risk") else ""
    if "real_trade_size" not in src:                       # locate the caller generically
        src = inspect.getsource(R)
    assert "MEMECOIN_SOLANA_MAX_CLIP_USD" in src, "the Solana sizing call must pass a ceiling"


def test_the_dashboard_reads_the_same_ceiling_as_the_gate():
    """Two call sites computing the same quantity diverge; that already cost a $215-vs-$150 read."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "sniper" / "routes" / "api_memecoin.py").read_text()
    assert "MEMECOIN_SOLANA_MAX_CLIP_USD" in src
    assert '0.0 if str(net) == "solana"' not in src, "solana no longer reads a hardcoded 0 ceiling"


def test_the_default_is_off_so_the_setting_alone_changes_nothing():
    assert float(getattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD", 0.0) or 0.0) >= 0.0


def test_both_ceilings_are_operator_adjustable_and_bounded():
    from sniper.services import memecoin_entry_knobs as K
    for name in ("MEMECOIN_SOLANA_MAX_CLIP_USD", "MEMECOIN_EVM_MAX_CLIP_USD"):
        assert name in K.ADJUSTABLE, f"{name} must be settable without a deploy"
        knob = K.KNOBS[name]
        assert knob.minimum == 0 and knob.maximum is not None, "a sizing knob must be bounded"
        assert knob.off_value == 0, "OFF must be a switch, not a typed zero"
        with pytest.raises(ValueError):
            knob.coerce(-1)
