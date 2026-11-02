"""The Robinhood sizing ladder (operator, 2026-09-22): 100% of equity up to $50; one $50 clip while
equity sits between $50 and $100; two clips of equity/2 from $100, capped at $100 each from $200;
three clips of equity/3 from $300, capped at $170 each from $510. Bankroll mode supplies the
division by active slots; MEMECOIN_EVM_TIER_CLIP_USD_<chain> supplies the ceiling per tier."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as R
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


@pytest.fixture
def ladder(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_EQUITY_SLOT_TIERS_ENABLED_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_TWO_SLOT_EQUITY_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_THREE_SLOT_EQUITY_USD", 300.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_BY_CHAIN", {"4663": 3})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN", {"4663": (50.0, 100.0, 170.0)})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD", 0.0)


@pytest.mark.parametrize("equity,slots,clip", [
    (30.0, 1, 30.0),      # 100% of equity below $50
    (80.0, 1, 50.0),      # one $50 clip, $30 stays in the wallet
    (150.0, 2, 75.0),     # two clips of equity/2
    (250.0, 2, 100.0),    # two $100 clips, $50 stays
    (400.0, 3, 133.33),   # three clips of equity/3
    (600.0, 3, 170.0),    # three $170 clips, $90 stays
])
def test_robinhood_ladder(ladder, equity, slots, clip):
    ctx = R.build_evm_risk_context(_c(), usdc_balance=equity, native_balance=1.0,
                                   day="2026-09-22", network_id="4663")
    assert ctx.chain_max_concurrent == slots
    assert ctx.trade_size_usd == pytest.approx(clip, abs=0.01)


def test_a_chain_without_tier_caps_keeps_its_flat_ceiling(ladder, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_TIER_CLIP_USD_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_MAX_CLIP_USD_BY_CHAIN", {"4663": 150.0})
    ctx = R.build_evm_risk_context(_c(), usdc_balance=600.0, native_balance=1.0,
                                   day="2026-09-22", network_id="4663")
    assert (ctx.chain_max_concurrent, ctx.trade_size_usd) == (3, 150.0)
    assert cfg.evm_max_clip_usd_for("4663") == 150.0 and cfg.evm_max_clip_usd_for("4663", slots=2) == 150.0
