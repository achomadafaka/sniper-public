"""Robinhood above-nano chase ceiling (2026-09-12, operator "Do both")."""
import inspect

import sniper.services.memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def test_the_ceiling_caps_4663_above_nano_and_nothing_else(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", 30.0)
    assert R.over_nano_chase_cap(50.0, "4663", False) == 30.0
    assert R.over_nano_chase_cap(20.0, "4663", False) == 20.0, "a tighter cap stays"
    assert R.over_nano_chase_cap(0.0, "4663", False) == 30.0, "a waived (0 = none) cap is capped too"
    assert R.over_nano_chase_cap(50.0, "4663", True) == 50.0, "nano keeps its own caps"
    assert R.over_nano_chase_cap(50.0, "solana", False) == 50.0
    assert R.over_nano_chase_cap(0.0, "56", False) == 0.0


def test_off_by_default(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_4663_OVER_NANO_QUOTE_CHASE_MAX_PCT", 0.0)
    assert R.over_nano_chase_cap(0.0, "4663", False) == 0.0


def test_the_hook_applies_it_after_the_waiver():
    src = inspect.getsource(R.real_entry_hook)
    i = src.index('if _waived_early("chase_past_entry") and not is_nano:')
    j = src.index("quote_chase_cap = over_nano_chase_cap(quote_chase_cap, identity.network_id, is_nano)")
    assert i < j < src.index("chase_max_pct=quote_chase_cap")
