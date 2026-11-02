"""Per-chain minimum trade (operator 2026-10-08: chain 1 buys with 100% of its equity, never under $50).

MEMECOIN_MIN_TRADE_FLOOR_USD_<chain> overrides the global floor for one chain, everywhere a funded
EVM clip meets the floor: the risk context's scaled clip and its gate, the funded-contract
validator, and the route-capacity retry. Mainnet gas makes a small chain-1 clip -EV, so below the
chain's floor the entry refuses with the numbers named; it never trades a smaller size.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as R
from sniper.settings import memecoin as cfg


@pytest.fixture
def chain1_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 5.0)
    monkeypatch.setenv("MEMECOIN_MIN_TRADE_FLOOR_USD_1", "50")
    for table in ("ENTRY", "APPROVAL", "EXIT"):       # import-time tables: patch them in place
        name = "MEMECOIN_EVM_GAS_%s_NATIVE_BY_CHAIN" % table
        monkeypatch.setattr(cfg, name, dict(getattr(cfg, name), **{"1": 0.0001}))


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_the_override_is_per_chain(chain1_floor):
    assert cfg.min_trade_floor_usd_for("1") == 50.0
    assert cfg.min_trade_floor_usd_for("4663") == 5.0
    assert cfg.min_trade_floor_usd_for(None) == 5.0


@pytest.mark.parametrize("raw", ["", "abc", "nan", "-1"])
def test_a_malformed_override_keeps_the_global_floor(chain1_floor, monkeypatch, raw):
    monkeypatch.setenv("MEMECOIN_MIN_TRADE_FLOOR_USD_1", raw)
    assert cfg.min_trade_floor_usd_for("1") == 5.0


def test_the_chain_one_gate_refuses_a_clip_under_its_floor_and_names_it(chain1_floor):
    ctx = R.build_evm_risk_context(_c(), usdc_balance=40.0, native_balance=1.0, day="2026-10-08",
                                   network_id="1")
    assert ctx.min_trade_floor_usd == 50.0
    d = R.evm_entry_allowed(ctx, mint="0xabc", trade_size=ctx.trade_size_usd)
    assert not d.allowed and "$50.00 floor" in d.reason


def test_another_chain_keeps_the_global_floor(chain1_floor):
    ctx = R.build_evm_risk_context(_c(), usdc_balance=40.0, native_balance=1.0, day="2026-10-08",
                                   network_id="8453")
    assert ctx.min_trade_floor_usd == 5.0


def test_the_contract_validator_accepts_a_chain_floor_above_the_scaled_clip(chain1_floor):
    """risk scale 0.5 on a $63 base asks $31.50, the chain floor lifts it to $50 (never above the
    base), and the funded contract must accept exactly that clip."""
    from sniper.services.memecoin_risk import scaled_funded_clip
    clip = scaled_funded_clip(base_clip_usd=63.0, risk_scale=0.5,
                              floor_usd=cfg.min_trade_floor_usd_for("1"))
    assert clip == 50.0
    from sniper.services import memecoin_funded_policy as FP
    assert FP._floor_for_validation("1") == 50.0
