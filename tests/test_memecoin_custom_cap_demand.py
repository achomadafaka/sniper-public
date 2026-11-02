"""Custom cap rails need fresh quotes within the existing provider ceiling."""
import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_fastlane as F, memecoin_real_runner as R
from sniper.services.memecoin_quote_demand import QuoteBudget, RAIL_REASONS, exact_quote_demand
from test_memecoin_custom_mcap_executable import ledger, position
from test_memecoin_fastlane import _c, _pos


@pytest.mark.parametrize("mid", [0, None, float("nan")])
def test_custom_cap_demands_fresh_evidence_without_a_market_mid_or_percent_rail(mid):
    verdict = exact_quote_demand(mid_pnl_pct=mid, stop_pct=0, tp_pct=0,
                                 last_exact_age_s=0, custom_cap=True)
    assert verdict.needed and verdict.reason == "custom_cap"
    assert verdict.reason in RAIL_REASONS


@pytest.mark.parametrize("leg", ["tp_target_mcap", "sl_target_mcap", "band"])
@pytest.mark.parametrize("basis", [True, False])
def test_resolved_custom_target_uses_rail_budget_before_heartbeat(ledger, leg, basis):
    pos = position(ledger, supply=1000 if basis else None)
    if leg == "band":
        m.set_exit_target(ledger, pos["id"], 1500, 1700, now_iso="2026-10-19T16:00:00+00:00")
    else:
        m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, sl_pct=0, **{leg: 1500})
    pos["_mcap_exit_target"] = m.active_exit_target(ledger, pos["id"])
    R._resolve_override_and_mcap(ledger, pos, {}, 1, {pos["id"]: object()}, R.cfg)
    clock = lambda: 0
    gate = F.build_demand_fn(ledger, always=False, heartbeat_s=600, clock=clock,
        budget=QuoteBudget(per_minute=0, burst=0, clock=clock),
        rail_budget=QuoteBudget(per_minute=0, burst=2, clock=clock))
    assert gate(pos, display_mid=1, trail_pct=0, derisk_pct=0)
    assert gate(pos, display_mid=1, trail_pct=0, derisk_pct=0)
    assert not gate(pos, display_mid=1, trail_pct=0, derisk_pct=0), "rail ceiling still applies"
    gate.rail_budget.penalize(15, group="solana:exact_quote")
    assert not gate(pos, display_mid=1, trail_pct=0, derisk_pct=0), "429 hold still applies"


def test_resetting_custom_targets_restores_default_demand(ledger):
    pos = position(ledger)
    m.set_position_exit_override(ledger, pos["id"], "operator", tp_pct=0, tp_target_mcap=1500)
    R._resolve_override_and_mcap(ledger, pos, {}, 1, {pos["id"]: object()}, R.cfg)
    gate = F.build_demand_fn(ledger, always=False, heartbeat_s=600, clock=lambda: 0)
    assert gate(pos, display_mid=1, trail_pct=0, derisk_pct=0)
    m.clear_position_exit_override(ledger, pos["id"], "operator")
    R._resolve_override_and_mcap(ledger, pos, {}, 1, {pos["id"]: object()}, R.cfg)
    assert pos["_exit_custom_cap"] is False
    assert not gate(pos, display_mid=1, trail_pct=0, derisk_pct=0)


@pytest.mark.parametrize("leg", ["tp_target_mcap", "sl_target_mcap", "band"])
@pytest.mark.parametrize("gated", [False, True])
def test_evm_batch_exposes_current_cap_metadata_before_demand_and_cold_preview(monkeypatch, leg, gated):
    conn = _c()
    pid = _pos(conn)
    if leg == "band":
        m.set_exit_target(conn, pid, 1500, 1700, now_iso="2026-10-19T16:00:00+00:00")
    else:
        m.set_position_exit_override(conn, pid, "operator", **{leg: 1500})
    seen = []
    def observe(pos):
        seen.append(bool(pos.get("_exit_custom_cap") or pos.get("_mcap_exit_target")))
        return {"expected_usdc": 50}
    monkeypatch.setattr(F, "_batched_mid_snapshot", lambda *a, **kw: ({}, {}))
    F.refresh_open_positions(conn, {"4663": {"preview_exit_fn": observe}},
        demand_fn=(lambda pos, **kw: bool(observe(pos))) if gated else None)
    assert seen and all(seen)
    conn.close()
