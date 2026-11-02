"""Timing Surface v1 — captured-travel-only timing classification.

Spec: docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
      (commit d598cf2, r2)
Audit: docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md

Test layout mirrors tests/test_wallet_protocol_completeness_surface.py.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.timing import held_resolution_travel


# ---------------------------------------------------------------------------
# Section A — held_resolution_travel: raw-token rule, label-agnostic
# ---------------------------------------------------------------------------


def test_held_resolution_travel_yes_win():
    """YES bought at 0.30 resolved at 1.0 → captured travel +0.70."""
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 1.0,
           "realizedPnl": 70.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.70)


def test_held_resolution_travel_no_win_locks_audit_r1_pattern():
    """NO bought at 0.40, NO won → universal payout: 1.0 - 0.40 = +0.60.

    Locks audit-R1's NO-token sign correctness. A buggy implementation
    that flipped the NO side (entry - later or similar) would yield
    -0.60 here.
    """
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 0.0,
           "realizedPnl": 60.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.60)


def test_held_resolution_travel_other_categorical_win():
    """Categorical winning outcome (Trump bought at 0.30 → wins) gets
    the same +0.70 favorable travel as a YES bought at 0.30. The rule
    is label-agnostic.
    """
    pos = {"outcome": "Trump", "avgPrice": 0.30, "curPrice": 1.0,
           "realizedPnl": 70.0, "size": 100, "conditionId": "cid_x"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.70)


def test_held_resolution_travel_yes_loss():
    """YES bought at 0.30, lost → -0.30 (lost entry premium)."""
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 0.0,
           "realizedPnl": -30.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_loss")
    assert travel == pytest.approx(-0.30)


def test_held_resolution_travel_no_loss():
    """NO bought at 0.40, lost (YES won, NO token redeems at $0) → -0.40."""
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 1.0,
           "realizedPnl": -40.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_loss")
    assert travel == pytest.approx(-0.40)


def test_held_resolution_travel_rejects_non_held_category():
    pos = {"outcome": "Yes", "avgPrice": 0.30, "curPrice": 1.0}
    with pytest.raises(ValueError, match="non-held category"):
        held_resolution_travel(pos, "pre_resolution_profitable_exit")
    with pytest.raises(ValueError, match="non-held category"):
        held_resolution_travel(pos, "insufficient_data")


# ---------------------------------------------------------------------------
# Section B — sw_sell_move: size-weighted SELL with explicit method tracking
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.timing import sw_sell_move


def test_t1_sw_sell_move_no_token_raw_movement_sign():
    """T1 (audit oracle): NO bought at 0.40, sold at 0.65 → +0.25 favorable.

    Locks the raw-owned-token rule: long-token favorable = later − entry,
    no asymmetric NO flip.
    """
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 25.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move == pytest.approx(0.25)
    assert result.sell_price_method == "size_weighted"
    assert result.weighted_sell_price == pytest.approx(0.65)
    assert result.last_sell_price == pytest.approx(0.65)
    assert result.n_sell_fills == 1


def test_t2_sw_sell_move_categorical_outcome_label_agnostic():
    """T2 (audit oracle): outcome="Trump" bought at 0.30, sold at 0.55
    → +0.25 favorable. Same rule as YES/NO; label-agnostic."""
    pos = {"conditionId": "cid_x", "outcome": "Trump",
           "avgPrice": 0.30, "realizedPnl": 25.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_x", "outcome": "Trump",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_trump"},
        {"side": "SELL", "conditionId": "cid_x", "outcome": "Trump",
         "price": 0.55, "timestamp": 2000, "size": 100, "asset": "tok_trump"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move == pytest.approx(0.25)
    assert result.sell_price_method == "size_weighted"


def test_t3_sw_sell_move_dust_close_does_not_dominate():
    """T3 (audit oracle): BUY 100 NO at 0.40. SELL 99 at 0.65, then
    SELL 1 at 0.05.

    Latest-SELL would have given last_sell_price=0.05 → sw_move=−0.35.
    Size-weighted gives weighted ≈ 0.6440 → sw_move ≈ +0.2440.

    The size-weighted path must run (all SELLs have valid size) and the
    dust SELL must NOT dominate.
    """
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 24.40}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 99,  "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.05, "timestamp": 3000, "size": 1,   "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    # (0.65*99 + 0.05*1) / 100 = 64.40/100 = 0.6440
    assert result.weighted_sell_price == pytest.approx(0.6440, abs=1e-4)
    assert result.sw_move == pytest.approx(0.2440, abs=1e-4)
    # And explicitly not the dust value.
    assert result.last_sell_price == pytest.approx(0.05)
    assert result.n_sell_fills == 2


def test_sw_sell_move_falls_back_to_latest_when_size_invalid():
    """When ANY SELL has missing/zero size, fall back to latest SELL
    by timestamp and stamp sell_price_method="latest_sell_fallback"."""
    pos = {"conditionId": "cid_a", "outcome": "Yes",
           "avgPrice": 0.30, "realizedPnl": 10.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_yes"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.40, "timestamp": 2000, "size": 0,   "asset": "tok_yes"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.45, "timestamp": 3000, "size": 50,  "asset": "tok_yes"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "latest_sell_fallback"
    assert result.weighted_sell_price is None
    assert result.last_sell_price == pytest.approx(0.45)
    assert result.sw_move == pytest.approx(0.45 - 0.30)
    assert result.n_sell_fills == 2


def test_sw_sell_move_unavailable_when_no_sells():
    """No SELL events for (cid, outcome) → unavailable, sw_move=None.

    Won't normally happen for pre_resolution_* categories (those by
    definition have a SELL match), but the helper is robust.
    """
    pos = {"conditionId": "cid_a", "outcome": "Yes",
           "avgPrice": 0.30, "realizedPnl": 0.0}
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.30, "timestamp": 1000, "size": 100, "asset": "tok_yes"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sw_move is None
    assert result.sell_price_method == "unavailable"
    assert result.weighted_sell_price is None
    assert result.last_sell_price is None
    assert result.n_sell_fills == 0


def test_sw_sell_move_filters_by_cid_and_outcome():
    """Activity for a different (cid, outcome) must not be matched
    when the position has no asset id (fallback path)."""
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 0.0}    # no `asset` on pos
    activity = [
        # Wrong outcome
        {"side": "SELL", "conditionId": "cid_a", "outcome": "Yes",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_yes"},
        # Wrong cid
        {"side": "SELL", "conditionId": "cid_b", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "unavailable"
    assert result.sw_move is None


def test_sw_sell_move_prefers_token_identity_when_position_has_asset():
    """When position carries `asset`, match SELLs by asset token id —
    NOT by (cid, outcome) label. Locks the audit's preference for
    stable identity over label-based matching."""
    pos = {
        "conditionId": "cid_a", "outcome": "No",
        "asset": "tok_no_a",                     # stable token identity
        "avgPrice": 0.40, "realizedPnl": 25.0,
    }
    activity = [
        # Same (cid, outcome) but DIFFERENT token id — must NOT match.
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.05, "timestamp": 2000, "size": 100, "asset": "tok_no_b"},
        # Matching token id — this is the SELL we want.
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 3000, "size": 100, "asset": "tok_no_a"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    assert result.weighted_sell_price == pytest.approx(0.65)
    assert result.sw_move == pytest.approx(0.25)
    assert result.n_sell_fills == 1


def test_sw_sell_move_falls_back_to_cid_outcome_when_position_has_no_asset():
    """When position has no `asset`, fall back to (cid, outcome) match."""
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 25.0}
    activity = [
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 100, "asset": "tok_no_a"},
    ]
    result = sw_sell_move(pos, activity)
    assert result.sell_price_method == "size_weighted"
    assert result.sw_move == pytest.approx(0.25)


# ---------------------------------------------------------------------------
# Section C — compute_timing_surface: classification + gating + reserved fields
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.timing import compute_timing_surface


def _build_held_win_pos(*, cid, avg_price=0.40, realized_pnl=60.0,
                        outcome="Yes", ts=1000):
    """Held-to-resolution win fixture (curPrice at resolution, no SELL match)."""
    return {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": 1.0,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": ts, "eventSlug": f"slug-{cid}",
    }


def _build_held_loss_pos(*, cid, avg_price=0.40, realized_pnl=-40.0,
                         outcome="Yes", ts=1000):
    """Held-to-resolution loss fixture."""
    return {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": 0.0,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": ts, "eventSlug": f"slug-{cid}",
    }


def _build_preres_profitable_pos(*, cid, avg_price=0.40,
                                  sell_price=0.60, realized_pnl=20.0,
                                  outcome="Yes", buy_ts=1000, sell_ts=1500):
    """Position fixture + matching activity rows for a profitable pre-res exit."""
    pos = {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": sell_price,    # not at resolution
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": buy_ts, "eventSlug": f"slug-{cid}",
    }
    activity = [
        {"side": "BUY",  "conditionId": cid, "outcome": outcome,
         "price": avg_price, "timestamp": buy_ts, "size": 100,
         "asset": f"tok_{cid}"},
        {"side": "SELL", "conditionId": cid, "outcome": outcome,
         "price": sell_price, "timestamp": sell_ts, "size": 100,
         "asset": f"tok_{cid}"},
    ]
    return pos, activity


def _build_preres_loss_cut_pos(*, cid, avg_price=0.40,
                                sell_price=0.20, realized_pnl=-20.0,
                                outcome="Yes", buy_ts=1000, sell_ts=1500):
    pos = {
        "conditionId": cid, "outcome": outcome,
        "avgPrice": avg_price, "curPrice": sell_price,
        "realizedPnl": realized_pnl, "size": 100,
        "timestamp": buy_ts, "eventSlug": f"slug-{cid}",
    }
    activity = [
        {"side": "BUY",  "conditionId": cid, "outcome": outcome,
         "price": avg_price, "timestamp": buy_ts, "size": 100,
         "asset": f"tok_{cid}"},
        {"side": "SELL", "conditionId": cid, "outcome": outcome,
         "price": sell_price, "timestamp": sell_ts, "size": 100,
         "asset": f"tok_{cid}"},
    ]
    return pos, activity


def test_compute_timing_surface_returns_none_on_empty_input():
    """Mirrors exit_attribution.compute_attribution_summary's contract."""
    result = compute_timing_surface(
        closed_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result is None


def test_classification_holding_driven_with_dominant_held_bucket():
    """25 held wins + 5 pre-res profitable exits → holding_driven.
    Locks the dominant-rule-before-mixed ordering: even though both
    buckets are profitable and each has n >= 5 (mixed-eligible), the
    held bucket's 0.83 share clears the 0.70 dominance threshold first.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "holding_driven"
    assert result["confidence_band"] == "high"
    assert result["confidence_downgraded"] is False
    assert result["coverage_downgraded"] is False
    assert result["coverage_caveats"] == []
    assert result["n_attributed"] == 30
    assert result["held_to_resolution"]["n"] == 25
    assert result["pre_resolution"]["n"] == 5


def test_classification_trading_driven_with_dominant_preres_bucket():
    """5 held losses + 25 pre-res profitable → trading_driven."""
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "trading_driven"


def test_classification_mixed_when_buckets_split_and_both_profitable():
    """12 held profitable + 12 pre-res profitable, neither dominant → mixed."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(12)]
    activity = []
    for i in range(12):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "mixed"


def test_classification_unclear_when_neither_bucket_dominates_and_pnl_negative():
    """6 held losses + 6 pre-res loss cuts + 8 held wins + 10 pre-res winners.
    Held = 14 (50% of n_attributed=28), pre-res = 14 (50%) — neither
    dominates. Both buckets net positive on PnL? Need to check.

    Use a fixture with both buckets net negative to force `unclear`.
    """
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(14)]
    activity = []
    for i in range(14):
        pos, act = _build_preres_loss_cut_pos(cid=f"pre_l_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "unclear"


def test_classification_underpowered_when_n_attributed_below_floor():
    """n_attributed=15 < 20 floor → underpowered, regardless of strong
    composition.

    Per spec §5, n_attributed = held_n + preres_n + n_unresolved
    (excludes insufficient_data). To produce 15 ATTRIBUTED positions we
    need a non-empty activity stream so exit_attribution.classify_position
    doesn't short-circuit to `insufficient_data` on its first guard.
    A single sentinel BUY for an unrelated cid (timestamp before all
    held_w positions) provides activity_min_ts without introducing any
    SELL match.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(15)]
    sentinel_activity = [{
        "side": "BUY", "conditionId": "cid_unrelated", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_unrelated",
    }]
    result = compute_timing_surface(
        closed, sentinel_activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["n_attributed"] == 15           # spec: excludes insufficient_data
    assert result["classification"] == "underpowered"
    assert "n_attributed=15 below floor 20" in result["classification_reason"]
    assert result["confidence_band"] == "underpowered"
    assert result["coverage_downgraded"] is False     # n < floor, not coverage


def test_future_repricing_fields_always_literal_in_v1():
    """The four reserved fields MUST be literal false/null in v1 and never
    populated. Locks the v1.5 reservation contract."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["future_repricing_available"] is False
    assert result["future_repricing_signal"] is None
    assert result["future_repricing_window"] is None
    assert result["price_history_source"] is None


# ---------------------------------------------------------------------------
# Section D — coverage gating + caveats (audit T5 anchor)
# ---------------------------------------------------------------------------


def test_t5_unmapped_fills_alone_does_not_force_underpowered():
    """T5 (audit oracle): one unmapped fill in an otherwise strong
    sample fires SUBGRAPH_UNMAPPED_FILLS caveat + drops confidence to
    "low" but classification stands. Critical regression-lock against
    the r1-era behavior that forced underpowered on any unmapped count.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=1,    # the lone unmapped fill
        attribution_coverage_low=False,
    )
    # Classification stands.
    assert result["classification"] == "holding_driven"
    # Confidence drops; coverage caveat fires.
    assert result["confidence_band"] == "low"
    assert result["confidence_downgraded"] is True
    # Not forced-underpowered.
    assert result["coverage_downgraded"] is False
    assert result["coverage_caveats"] == ["SUBGRAPH_UNMAPPED_FILLS"]


def test_attribution_coverage_low_forces_underpowered_with_coverage_downgraded():
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=True,
    )
    assert result["classification"] == "underpowered"
    assert result["confidence_band"] == "underpowered"
    assert result["coverage_downgraded"] is True
    assert "ATTRIBUTION_COVERAGE_LOW" in result["coverage_caveats"]


def test_activity_truncated_without_fallback_forces_underpowered():
    """activity_cap_hit AND not (subgraph_fallback OR walk_full_history)
    → ACTIVITY_TRUNCATED caveat + force_underpowered + coverage_downgraded.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result["classification"] == "underpowered"
    assert result["coverage_downgraded"] is True
    assert "ACTIVITY_TRUNCATED" in result["coverage_caveats"]


def test_activity_truncated_with_subgraph_fallback_does_not_fire_caveat():
    """Cap hit but subgraph fallback ran → no ACTIVITY_TRUNCATED caveat,
    and classification stands.

    A sentinel BUY (unrelated cid, ts before all held_w positions)
    provides a non-empty activity_rows so exit_attribution.classify_position
    doesn't short-circuit to `insufficient_data` on its first guard.
    Same pattern as test_classification_underpowered_when_n_attributed_below_floor.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    sentinel_activity = [{
        "side": "BUY", "conditionId": "cid_unrelated", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_unrelated",
    }]
    result = compute_timing_surface(
        closed, sentinel_activity,
        activity_cap_hit=True, subgraph_fallback_used=True,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert "ACTIVITY_TRUNCATED" not in result["coverage_caveats"]
    assert result["classification"] == "holding_driven"
    assert result["confidence_band"] == "high"


def test_activity_truncated_with_walk_full_history_does_not_fire_caveat():
    """Cap hit but walk_full_history ran → no ACTIVITY_TRUNCATED caveat,
    and classification stands. Same sentinel pattern as the
    subgraph-fallback case above."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    sentinel_activity = [{
        "side": "BUY", "conditionId": "cid_unrelated", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_unrelated",
    }]
    result = compute_timing_surface(
        closed, sentinel_activity,
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=True, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert "ACTIVITY_TRUNCATED" not in result["coverage_caveats"]
    assert result["classification"] == "holding_driven"


def test_multiple_caveats_aggregate_into_caveat_list():
    """Multiple coverage issues → all caveats fire; classification forced
    underpowered; coverage_downgraded=True."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    result = compute_timing_surface(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=3,
        attribution_coverage_low=True,
    )
    assert result["classification"] == "underpowered"
    assert result["coverage_downgraded"] is True
    # Order must match the spec: ACTIVITY_TRUNCATED, SUBGRAPH_UNMAPPED_FILLS,
    # ATTRIBUTION_COVERAGE_LOW.
    assert result["coverage_caveats"] == [
        "ACTIVITY_TRUNCATED", "SUBGRAPH_UNMAPPED_FILLS", "ATTRIBUTION_COVERAGE_LOW",
    ]


# ---------------------------------------------------------------------------
# Section E — plumbing: ProtocolReport.timing_surface + analyze_wallet wiring
# ---------------------------------------------------------------------------


import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _now() -> int:
    return int(time.time())


def _wallet_data(
    *,
    closed_positions,
    activity_rows,
    activity_cap_hit=False,
    subgraph_fallback_used=False,
    walk_full_history=False,
    n_unmapped_fills=0,
):
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions),
        open_positions=[],
        activity_rows=list(activity_rows),
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        n_unmapped_fills=n_unmapped_fills,
    )


def test_protocol_report_has_timing_surface_field_default_none():
    """A bare ProtocolReport has timing_surface=None until analyze_wallet
    populates it. Display-only field; never feeds verdict."""
    r = ProtocolReport(wallet="0xabc")
    assert r.timing_surface is None


def _build_strong_holding_data():
    """30 attributed closed positions, all held wins, no caveats.

    Includes a sentinel BUY row for an unrelated cid (ts=500, before all
    held_w positions at ts=1000) so exit_attribution.classify_position
    doesn't short-circuit on empty activity_rows. Pattern mirrors Task 4
    sentinel-BUY convention.
    """
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(30)]
    sentinel_activity = [{
        "side": "BUY", "conditionId": "cid_unrelated_e", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_unrelated_e",
    }]
    return _wallet_data(
        closed_positions=closed, activity_rows=sentinel_activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
    )


def test_analyze_wallet_populates_timing_surface_on_strong_data():
    data = _build_strong_holding_data()
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert isinstance(report, ProtocolReport)
    assert report.timing_surface is not None
    assert report.timing_surface["classification"] == "holding_driven"
    assert report.timing_surface["confidence_band"] == "high"
    assert report.timing_surface["coverage_caveats"] == []
    # Future-reserved fields literal in v1.
    assert report.timing_surface["future_repricing_available"] is False
    assert report.timing_surface["future_repricing_signal"] is None


def test_t4_timing_surface_invariant_under_curprice_coordinate_flip():
    """T4 (audit oracle): proves Timing Surface v1 does NOT depend on
    closed_positions[].curPrice's coordinate.

    Build a held-to-resolution win set. Build a "flipped" twin where
    every position's curPrice is replaced (1.0 ↔ 0.0). All other
    fields identical (especially realizedPnl — the source of truth for
    the held_to_resolution_win/_loss split via exit_attribution's
    PnL-sign-based classifier).

    Expected: timing_surface dict is identical between the two runs.
    Locks audit-R2 routing.
    """
    # Sentinel BUY at ts=500 (before all held_w positions at ts=1000) so
    # exit_attribution.classify_position doesn't short-circuit on empty
    # activity_rows. Same pattern as Task 4 sentinel-BUY convention.
    sentinel_activity = [{
        "side": "BUY", "conditionId": "cid_unrelated_t4", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_unrelated_t4",
    }]

    base_closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    base = _wallet_data(closed_positions=base_closed, activity_rows=sentinel_activity)
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)

    # Flipped twin: curPrice 1.0 → 0.0 on every position. realizedPnl
    # preserved (still > 0 → exit_attribution classifies as held_win
    # because curPrice ≈ 0 still trips _at_resolution AND PnL > 0).
    flipped_closed = []
    for p in base_closed:
        q = dict(p)
        q["curPrice"] = 0.0 if q["curPrice"] == 1.0 else 1.0
        flipped_closed.append(q)
    flipped = _wallet_data(closed_positions=flipped_closed, activity_rows=sentinel_activity)
    r_flip = analyze_wallet("0xabc", pre_fetched_data=flipped, n_iter=200, seed=1)

    # The timing_surface dict must be identical — Timing v1 doesn't
    # touch curPrice for magnitude.
    assert r_base.timing_surface == r_flip.timing_surface


# ---------------------------------------------------------------------------
# Section F — render: ## Timing surface block
# ---------------------------------------------------------------------------


from sniper.analysis.wallet_protocol.report import render_report


def test_timing_surface_section_renders_after_edge_surface_before_stage1():
    """Section ordering: ## Timing surface comes after ## Edge surface
    and before ### Stage 1 result."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    data = _wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    timing_pos = md.find("## Timing surface")
    edge_pos = md.find("## Edge surface")
    stage1_pos = md.find("### Stage 1 result")
    assert timing_pos != -1, "## Timing surface must appear in render"
    if edge_pos != -1:
        assert edge_pos < timing_pos
    assert stage1_pos != -1
    assert timing_pos < stage1_pos


def _render_for_classification(closed, activity, **kwargs):
    data = _wallet_data(
        closed_positions=closed, activity_rows=activity, **kwargs,
    )
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    return render_report(report)


def test_render_holding_driven_uses_allowed_phrasing():
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity)
    assert "Classification: holding_driven" in md
    assert "predominantly from holding positions" in md
    # Coverage caveats line absent on a clean run.
    assert "Coverage caveats:" not in md


def test_render_trading_driven_uses_allowed_phrasing():
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity)
    assert "Classification: trading_driven" in md
    assert "predominantly from pre-resolution exits" in md


def test_render_underpowered_with_coverage_downgrade_references_completeness():
    """coverage_downgraded path must reference `## Data completeness`."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    md = _render_for_classification(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False,
    )
    assert "Classification: underpowered" in md
    assert "Completeness caveats fired" in md
    assert "`## Data completeness`" in md


def test_render_unmapped_fills_alone_renders_caveat_with_holding_classification():
    """Locks T5 in the render path: classification=holding_driven,
    confidence: low, coverage caveat present."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    md = _render_for_classification(closed, activity, n_unmapped_fills=1)
    assert "Classification: holding_driven" in md
    assert "Confidence: low" in md
    assert "SUBGRAPH_UNMAPPED_FILLS" in md
    assert "(see `## Data completeness`)" in md


# ---------------------------------------------------------------------------
# Section G — regression locks: forbidden phrasing, no external lookup,
# verdict invariance
# ---------------------------------------------------------------------------


# Spec §8.7 + audit forbidden-phrase list. Anything here in a rendered
# Timing Surface v1 report is a regression — these claims require strict
# repricing enrichment (v1.5 / H6) which v1 does not implement.
_FORBIDDEN_PHRASES = (
    "entered before repricing",
    "acted before the market",
    "had information advantage",
    "predicted the move early",
    "front-ran",
    "knew before the market",
    "pre-news edge",
    "early information edge",
    "latency edge",
)


def _all_classifications_render_corpus() -> list[str]:
    """Build a rendered-report corpus covering every classification path."""
    corpus: list[str] = []

    # holding_driven
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # trading_driven
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(5)]
    activity = []
    for i in range(25):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # mixed
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(12)]
    activity = []
    for i in range(12):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # unclear (all losses both buckets)
    closed = [_build_held_loss_pos(cid=f"held_l_{i}") for i in range(14)]
    activity = []
    for i in range(14):
        pos, act = _build_preres_loss_cut_pos(cid=f"pre_l_{i}")
        closed.append(pos); activity.extend(act)
    corpus.append(_render_for_classification(closed, activity))

    # underpowered (n < floor)
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(15)]
    corpus.append(_render_for_classification(closed, []))

    # underpowered + coverage_downgraded
    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    corpus.append(_render_for_classification(
        closed, [],
        activity_cap_hit=True, subgraph_fallback_used=False,
        walk_full_history=False,
    ))
    return corpus


def test_no_forbidden_phrases_appear_in_any_rendered_report():
    """Lock §8.7 against accidental over-claiming. If any rendered
    report contains a forbidden phrase, the test fails with a precise
    pointer to which phrase + which classification."""
    corpus = _all_classifications_render_corpus()
    for i, md in enumerate(corpus):
        lower = md.lower()
        for phrase in _FORBIDDEN_PHRASES:
            assert phrase.lower() not in lower, (
                f"Forbidden phrase {phrase!r} appeared in rendered "
                f"report #{i} (classification corpus index)."
            )


def test_compute_timing_surface_makes_no_http_calls(monkeypatch):
    """compute_timing_surface MUST NOT call requests.Session.get or any
    HTTP method. Locks the no-external-lookup contract."""
    import requests
    calls: list[tuple] = []

    def _forbidden_get(self, *args, **kwargs):
        calls.append(("get", args, kwargs))
        raise AssertionError("requests.Session.get called inside compute_timing_surface")

    def _forbidden_post(self, *args, **kwargs):
        calls.append(("post", args, kwargs))
        raise AssertionError("requests.Session.post called inside compute_timing_surface")

    monkeypatch.setattr(requests.Session, "get", _forbidden_get)
    monkeypatch.setattr(requests.Session, "post", _forbidden_post)

    closed = [_build_held_win_pos(cid=f"held_w_{i}") for i in range(25)]
    activity = []
    for i in range(5):
        pos, act = _build_preres_profitable_pos(cid=f"pre_w_{i}")
        closed.append(pos); activity.extend(act)
    result = compute_timing_surface(
        closed, activity,
        activity_cap_hit=False, subgraph_fallback_used=False,
        walk_full_history=False, n_unmapped_fills=0,
        attribution_coverage_low=False,
    )
    assert result is not None
    assert calls == [], f"Unexpected HTTP calls: {calls}"


def _verdict_signature(r: ProtocolReport) -> tuple:
    return (
        r.overall_verdict,
        r.gate_result,
        r.primary_edge_type,
        r.copyability_note,
    )


def test_timing_surface_does_not_alter_verdict_signature():
    """Verdict invariance: same closed-positions universe → identical
    (overall_verdict, gate_result, primary_edge_type, copyability_note)
    regardless of timing_surface presence/content. Mirrors Completeness
    Surface v1's invariance contract."""
    closed = [_build_held_win_pos(cid=f"held_w_{i}", realized_pnl=20.0)
              for i in range(40)]
    base = _wallet_data(closed_positions=closed, activity_rows=[])
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)

    # Decorated twin: introduce coverage caveats + unmapped fills + cap_hit
    # → forces timing_surface into underpowered + coverage_downgraded.
    decorated = _wallet_data(
        closed_positions=closed, activity_rows=[],
        activity_cap_hit=True, subgraph_fallback_used=True,
        walk_full_history=True, n_unmapped_fills=11,
    )
    r_dec = analyze_wallet("0xabc", pre_fetched_data=decorated, n_iter=200, seed=1)

    # Verdicts must be identical even though timing_surface differs.
    assert _verdict_signature(r_base) == _verdict_signature(r_dec)
    # Sanity: timing_surface really did differ between runs.
    assert r_base.timing_surface != r_dec.timing_surface


def test_r10_timing_surface_classification_invariant_under_resolved_coverage_change():
    """R10 Option A: Timing Surface classification must NOT change behavior
    when ATTRIBUTION_COVERAGE_LOW gets a new (stricter) trigger driven by
    n_with_resolved_attribution. A wallet dominated by unresolved_or_unknown
    still lands `unclear`, not `underpowered` — preserving the §5 contract.

    Fixture: 5 held wins + 25 unresolved_or_unknown.
      Timing-side metrics:
        n_attributed = held(5) + preres(0) + unresolved(25) = 30 (sample floor)
        held_share = 5/30 ≈ 17%; preres_share = 0%; unresolved dominates.
        attribution_coverage_low (kwarg) → 30/30 = 100% → False.
        force_underpowered → False (n>=20, kwarg false, no truncation).
      → classification = "unclear" (neither bucket dominates, no clear
        positive concentration).
    Completeness-side (verified separately in completeness_surface tests):
      n_with_resolved_attribution / n_total = 5/30 < 50% → caveat fires
      at the report layer.
    """
    closed = [
        _build_held_win_pos(cid=f"h_{i}", ts=2000) for i in range(5)
    ] + [
        {
            "conditionId": f"u_{i}", "outcome": "Yes",
            "avgPrice": 0.30, "curPrice": 0.50,
            "realizedPnl": 0.0, "size": 100, "timestamp": 2000,
            "eventSlug": f"slug-u_{i}",
        }
        for i in range(25)
    ]
    # Sentinel BUY for an unrelated cid BEFORE all closed-position
    # timestamps gives activity_min_ts < every position ts, so no
    # position is "outside the activity window" (which would short-circuit
    # to insufficient_data). The sentinel introduces no SELL match,
    # so the unresolved positions stay `unresolved_or_unknown`.
    activity = [{
        "side": "BUY", "conditionId": "sentinel", "outcome": "Yes",
        "price": 0.50, "timestamp": 500, "size": 100, "asset": "tok_x",
    }]
    data = _wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    ts = report.timing_surface
    assert ts is not None
    # Sample-size floor includes unresolved → 5 + 25 = 30.
    assert ts["n_attributed"] == 30
    # Classification stands as `unclear` (held_share = 17%, well below 70%;
    # unresolved dominates; no clear positive concentration).
    assert ts["classification"] == "unclear"
    # Timing's coverage_caveats list does NOT carry ATTRIBUTION_COVERAGE_LOW
    # because the (preserved) kwarg formula yields 30/30 = 100% > 50%.
    assert "ATTRIBUTION_COVERAGE_LOW" not in (ts.get("coverage_caveats") or [])
    # Not forced underpowered.
    assert ts["confidence_band"] != "underpowered"
