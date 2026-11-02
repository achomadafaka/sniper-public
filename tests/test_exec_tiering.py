"""WO-7 — execution tiering primitives (default-OFF, fail-safe) + measurement column round-trip."""
import sqlite3

import pytest

from sniper.execution.exec_tiering import (depth_aware_slippage_ladder, depth_impact_estimate,
                                           exit_tier_plan, priority_fee_microlamports)
from sniper.persistence import db_memecoin as m


def test_priority_fee_default_off_and_capped():
    assert priority_fee_microlamports(False, 5000, 2_000_000) == 0     # OFF -> never spend
    assert priority_fee_microlamports(True, 5000, 2_000_000) == 5000
    assert priority_fee_microlamports(True, 9_000_000, 2_000_000) == 2_000_000  # capped
    assert priority_fee_microlamports(True, -5, 2_000_000) == 0        # never negative
    assert priority_fee_microlamports(True, "x", 2_000_000) == 0       # bad input -> 0


def test_depth_impact_estimate():
    assert depth_impact_estimate(1000, 0) == 0.0
    assert abs(depth_impact_estimate(900, 100) - 0.1) < 1e-9           # 100/(900+100)
    assert depth_impact_estimate(0, 100) is None                       # unknown depth
    assert depth_impact_estimate("x", 100) is None


def test_depth_aware_ladder_off_returns_base():
    base = [0.03, 0.10, 0.25]
    assert depth_aware_slippage_ladder(900, 100, base, enabled=False) == base


def test_depth_aware_ladder_sizes_first_rung_from_depth():
    base = [0.03, 0.10, 0.25]
    # bag 100 into reserve 900 -> impact 0.10 -> first rung raised to 0.10, 0.03 dropped, 0.25 kept
    lad = depth_aware_slippage_ladder(900, 100, base, enabled=True, max_rung=0.25)
    assert lad[0] == pytest.approx(0.10) and 0.25 in lad and 0.03 not in lad
    # deep pool (tiny bag) -> impact below base_first -> first rung stays at base_first
    lad2 = depth_aware_slippage_ladder(1_000_000, 100, base, enabled=True)
    assert lad2[0] == pytest.approx(0.03)
    # unknown depth -> fail-safe to base
    assert depth_aware_slippage_ladder(0, 100, base, enabled=True) == base


def test_depth_aware_ladder_capped_by_max_rung():
    lad = depth_aware_slippage_ladder(100, 900, [0.03], enabled=True, max_rung=0.25)
    assert lad[0] == pytest.approx(0.25)                               # impact 0.9 clamped to 0.25


def test_exec_latency_records_tiering_columns():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.record_exec_latency(c, mint="MINT", pending_id=1, status="confirmed", sig="S",
                          timing={"total_ms": 420.0}, at="2026-07-25T00:00:00+00:00",
                          execution_slippage_pct=0.07, fee_paid_microlamports=5000,
                          depth_first_rung_pct=0.10, tier_variant="fee_on_depth_aware")
    r = c.execute("SELECT fee_paid_microlamports, depth_first_rung_pct, tier_variant "
                  "FROM memecoin_exec_latency").fetchone()
    assert r["fee_paid_microlamports"] == 5000 and r["tier_variant"] == "fee_on_depth_aware"
    assert r["depth_first_rung_pct"] == 0.10


def test_tier_plan_is_default_off_and_uses_exact_preview_depth_when_enabled():
    cfg = type("C", (), {
        "MEMECOIN_EXIT_PRIORITY_FEE_ENABLED": False,
        "MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS": 5000,
        "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS": 2_000_000,
        "MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED": False,
        "MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT": 0.25,
    })()
    pos = {"_exit_reserve_base": 900, "_exit_bag_base": 100}
    off = exit_tier_plan(pos, [0.03, 0.10, 0.25], chain="solana", cfg=cfg)
    assert off == {"slippage_ladder": [0.03, 0.10, 0.25], "priority_fee_microlamports": 0,
                   "tier_variant": "baseline"}
    cfg.MEMECOIN_EXIT_PRIORITY_FEE_ENABLED = True
    cfg.MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED = True
    on = exit_tier_plan(pos, [0.03, 0.10, 0.25], chain="solana", cfg=cfg)
    assert on["slippage_ladder"][0] == pytest.approx(0.10)
    assert on["priority_fee_microlamports"] == 5000
    assert on["tier_variant"] == "priority_fee+depth_aware"


def test_exit_execution_has_its_own_measurement_table():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.record_exit_execution(
        c, position_id=7, network_id="solana", exit_reason="real_trail",
        attempted_at="2026-07-25T00:00:00+00:00", status="closed", sig="S",
        total_ms=42.0, priority_fee_microlamports=5000, depth_first_rung_pct=0.10,
        tier_variant="priority_fee+depth_aware")
    row = c.execute("SELECT * FROM memecoin_exit_execution_attempts").fetchone()
    assert row["position_id"] == 7 and row["priority_fee_microlamports"] == 5000
    assert row["tier_variant"] == "priority_fee+depth_aware"
