"""Tests for Stage 1 cheap-reject gates (protocol v2 §3)."""

import time

from sniper.analysis.wallet_protocol.stage1_gates import (
    DORMANT_DAYS_THRESHOLD,
    N_CLOSED_POSITIONS_MIN,
    evaluate_stage1_gates,
)
from sniper.analysis.wallet_protocol.types import WalletData


def _make_data(
    closed: list[dict] | None = None,
    open_positions: list[dict] | None = None,
    activity: list[dict] | None = None,
    archetype: str | None = None,
    execution_profile: dict | None = None,
) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=closed or [],
        open_positions=open_positions or [],
        activity_rows=activity or [],
        activity_cap_hit=False,
        archetype_layer1=archetype,
        execution_profile=execution_profile,
        fetched_at=int(time.time()),
    )


def _now() -> int:
    return int(time.time())


# ----------------------------------------------------------------------------
# R1 — Hedge archetype
# ----------------------------------------------------------------------------


def test_r1_rejects_paired_outcome_arb():
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)],
        archetype="paired_outcome_arb",
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_hedge_archetype"
    assert "paired_outcome_arb" in (result.reason or "")


def test_r1_rejects_two_sided_inventory_trader():
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)],
        archetype="two_sided_inventory_trader",
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_hedge_archetype"


def test_r1_does_not_reject_other_archetypes():
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)],
        archetype="directional_discretionary",
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"


def test_r1_skipped_when_archetype_unknown():
    """If archetype extractor wasn't run, R1 is bypassed (no false rejections)."""
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)],
        archetype=None,
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"


# ----------------------------------------------------------------------------
# R2 — Insufficient sample
# ----------------------------------------------------------------------------


def test_r2_rejects_below_threshold():
    data = _make_data(
        closed=[{"realizedPnl": 1.0, "timestamp": _now()} for _ in range(N_CLOSED_POSITIONS_MIN - 1)],
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_insufficient_sample"
    assert str(N_CLOSED_POSITIONS_MIN - 1) in (result.reason or "")


def test_r2_passes_at_exactly_threshold():
    data = _make_data(
        closed=[{"realizedPnl": 1.0, "timestamp": _now()} for _ in range(N_CLOSED_POSITIONS_MIN)],
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"


# ----------------------------------------------------------------------------
# R3 — Dormant
# ----------------------------------------------------------------------------


def test_r3_rejects_dormant_wallet():
    old_ts = _now() - (DORMANT_DAYS_THRESHOLD + 1) * 86400
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": old_ts} for _ in range(50)],
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_dormant"


def test_r3_passes_recent_wallet():
    recent_ts = _now() - (DORMANT_DAYS_THRESHOLD - 1) * 86400
    data = _make_data(
        closed=[{"realizedPnl": 100.0, "timestamp": recent_ts} for _ in range(50)],
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"


# ----------------------------------------------------------------------------
# Gate ordering — first failing gate wins
# ----------------------------------------------------------------------------


def test_r1_takes_priority_over_r2():
    """If hedge archetype AND insufficient sample, R1 fires first."""
    data = _make_data(
        closed=[{"realizedPnl": 1.0, "timestamp": _now()}],  # n=1, would also fail R2
        archetype="paired_outcome_arb",
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_hedge_archetype"


def test_r2_takes_priority_over_r3():
    """If insufficient sample AND dormant, R2 fires first."""
    old_ts = _now() - (DORMANT_DAYS_THRESHOLD + 1) * 86400
    data = _make_data(
        closed=[{"realizedPnl": 1.0, "timestamp": old_ts} for _ in range(5)],
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "reject_insufficient_sample"


# ----------------------------------------------------------------------------
# Caution flags — do not reject, but get reported
# ----------------------------------------------------------------------------


def test_f1_sloppy_operator_flag_fires():
    """n_open_dead > 50 AND ratio > 0.50 → F1 caution."""
    closed = [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    open_pos = (
        # 60 dead positions (curPrice=0)
        [{"curPrice": 0.0, "size": 100, "avgPrice": 0.5} for _ in range(60)]
        # 40 alive positions
        + [{"curPrice": 0.5, "size": 100, "avgPrice": 0.5} for _ in range(40)]
    )
    data = _make_data(closed=closed, open_positions=open_pos)
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"
    assert any("F1_sloppy_operator" in f for f in result.caution_flags)


def test_f1_does_not_fire_with_few_dead_positions():
    closed = [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    open_pos = [{"curPrice": 0.0, "size": 100, "avgPrice": 0.5} for _ in range(5)] + [
        {"curPrice": 0.5, "size": 100, "avgPrice": 0.5} for _ in range(45)
    ]
    data = _make_data(closed=closed, open_positions=open_pos)
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"
    assert not any("F1_sloppy_operator" in f for f in result.caution_flags)


def test_f2_structured_indices_populated_when_concentrated():
    """v3 amendment B: F2 fires → f2_top3_indices and f2_top3_share populated."""
    closed = (
        [{"realizedPnl": 1_000_000.0, "timestamp": _now()} for _ in range(3)]
        + [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    )
    data = _make_data(closed=closed)
    result = evaluate_stage1_gates(data)
    assert result.f2_top3_indices is not None
    assert len(result.f2_top3_indices) == 3
    assert set(result.f2_top3_indices) == {0, 1, 2}
    assert result.f2_top3_share is not None
    assert result.f2_top3_share > 0.7


def test_f2_structured_fields_none_when_below_threshold():
    closed = [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    data = _make_data(closed=closed)
    result = evaluate_stage1_gates(data)
    assert result.f2_top3_indices is None
    assert result.f2_top3_share is None


def test_f2_single_bet_concentration_flag():
    """Top-3 positions account for >70% of net PnL → F2."""
    # 3 huge winners + 50 noise positions
    closed = (
        [{"realizedPnl": 1_000_000.0, "timestamp": _now()} for _ in range(3)]
        + [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    )
    data = _make_data(closed=closed)
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"
    assert any("F2_single_bet_concentration" in f for f in result.caution_flags)


def test_f4_heavy_ctf_flag():
    """execution_profile.ctf_redemption_share > 0.5 → F4."""
    closed = [{"realizedPnl": 100.0, "timestamp": _now()} for _ in range(50)]
    data = _make_data(
        closed=closed,
        execution_profile={"status": "populated", "ctf_redemption_share": 0.65},
    )
    result = evaluate_stage1_gates(data)
    assert result.result == "pass"
    assert any("F4_heavy_ctf" in f for f in result.caution_flags)


def test_time_relative_gates_measure_as_of_capture_not_wall_clock():
    """A frozen capture must produce a frozen verdict.

    Every time-relative gate reads `fetched_at`, not `time.time()`. When it read wall clock, a
    recorded fixture drifted across the 90-day dormancy threshold as the calendar moved and a
    deterministic calibration baseline started failing 81 days after capture, with no change to
    the wallet, the data, or the code (0xb3b50ef6: edge_detected -> data_insufficient).
    """
    captured_at = _now() - 400 * 86400          # capture is ancient in wall-clock terms...
    active_then = captured_at - 5 * 86400       # ...but the wallet was active 5 days before it
    closed = [
        {"timestamp": active_then, "pnl": 10.0, "conditionId": f"0x{i:02x}", "outcome": "Yes",
         "size": 100.0, "avgPrice": 0.5}
        for i in range(N_CLOSED_POSITIONS_MIN + 5)
    ]
    data = WalletData(
        wallet="0xabc", closed_positions=closed, open_positions=[],
        activity_rows=[{"timestamp": active_then}], activity_cap_hit=False,
        archetype_layer1=None, execution_profile=None, fetched_at=captured_at,
    )

    result = evaluate_stage1_gates(data)

    # 5 days idle as of capture — nowhere near the threshold. Measured against wall clock it
    # would be 400 days and reject_dormant.
    assert result.result != "reject_dormant", result.reason


def test_dormancy_still_rejects_a_wallet_idle_at_capture_time():
    """The as-of change must not disable the gate — only anchor it."""
    captured_at = _now()
    stale = captured_at - (DORMANT_DAYS_THRESHOLD + 30) * 86400
    closed = [
        {"timestamp": stale, "pnl": 10.0, "conditionId": f"0x{i:02x}", "outcome": "Yes",
         "size": 100.0, "avgPrice": 0.5}
        for i in range(N_CLOSED_POSITIONS_MIN + 5)
    ]
    data = WalletData(
        wallet="0xabc", closed_positions=closed, open_positions=[],
        activity_rows=[{"timestamp": stale}], activity_cap_hit=False,
        archetype_layer1=None, execution_profile=None, fetched_at=captured_at,
    )

    assert evaluate_stage1_gates(data).result == "reject_dormant"
