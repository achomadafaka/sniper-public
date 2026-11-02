"""Position Building Surface v1 — pure core tests.

Spec: docs/superpowers/specs/2026-05-10-wallet-position-builder-design.md
Plan: docs/superpowers/plans/2026-05-10-wallet-position-builder-phase1-core.md
"""
from __future__ import annotations

import dataclasses
import inspect
import ast
import json
import time
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.position_building import (
    compute_position_building_surface,
)
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


def _row(
    *,
    cid: str = "cid1",
    asset: str | None = "tok1",
    outcome: str | None = "Yes",
    side: str = "BUY",
    price: float | None = 0.40,
    size: float | None = 10.0,
    ts: int | None = 1000,
    **extra,
) -> dict:
    row = {
        "condition_id": cid,
        "asset": asset,
        "outcome": outcome,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": ts,
    }
    row.update(extra)
    return row


def _closed(
    *,
    cid: str = "cid1",
    asset: str | None = "tok1",
    outcome: str | None = "Yes",
    size: float | None = 10.0,
    **extra,
) -> dict:
    row = {
        "conditionId": cid,
        "asset": asset,
        "outcome": outcome,
        "size": size,
    }
    row.update(extra)
    return row


def test_compute_position_building_surface_signature_lock():
    sig = inspect.signature(compute_position_building_surface)
    params = sig.parameters
    assert list(params) == [
        "activity_rows",
        "closed_positions",
        "wallet",
        "activity_cap_hit",
        "subgraph_fallback_used",
        "walk_full_history",
    ]
    assert params["activity_rows"].default is inspect.Parameter.empty
    assert params["closed_positions"].default is None
    assert params["wallet"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["wallet"].default is None
    assert params["activity_cap_hit"].default is False
    assert params["subgraph_fallback_used"].default is False
    assert params["walk_full_history"].default is False


def test_returns_none_when_no_activity_rows_and_no_closed_positions():
    assert compute_position_building_surface([]) is None
    assert compute_position_building_surface([], None) is None


def test_phase2_position_building_surface_field_present_and_default_none():
    """Phase 2 wires the position_building_surface field on ProtocolReport.

    Field is structurally present and defaults to None until Phase 3 wires
    analyze_wallet to populate it. The field itself does not feed verdict,
    gate, primary_edge_type, or copyability_note logic — display-only per
    the W2 spec.
    """
    fields_by_name = {f.name: f for f in dataclasses.fields(ProtocolReport)}
    assert "position_building_surface" in fields_by_name
    field = fields_by_name["position_building_surface"]
    # Default is None; type annotation is `dict | None` (string at runtime
    # under PEP 563 / __future__ annotations, but verifiable by default).
    assert field.default is None


def test_wallet_parameter_round_trip():
    result = compute_position_building_surface([_row()], wallet="0xabc")
    assert result is not None
    assert result["wallet"] == "0xabc"

    result_no_wallet = compute_position_building_surface([_row()])
    assert result_no_wallet is not None
    assert result_no_wallet["wallet"] is None


def _only_position(result: dict) -> dict:
    assert result["wallet_summary"]["n_positions"] == 1
    return result["positions"][0]


def test_single_buy_reconstructs_one_position():
    result = compute_position_building_surface([
        _row(size=12.5, price=0.42, event_uid="ev1")
    ])
    pos = _only_position(result)
    assert pos["position_key"] == {
        "type": "asset",
        "condition_id": "cid1",
        "asset": "tok1",
        "outcome": "Yes",
    }
    assert pos["n_buy_fills"] == 1
    assert pos["n_sell_fills"] == 0
    assert pos["first_buy_ts"] == 1000
    assert pos["first_buy_price"] == pytest.approx(0.42)
    assert pos["final_observed_size"] == pytest.approx(12.5)
    assert pos["peak_observed_size"] == pytest.approx(12.5)
    assert pos["peak_observed_size_ts"] == 1000
    assert len(pos["cumulative_size_path"]) == 1


def test_alias_resolution_for_protocol_rows():
    result = compute_position_building_surface([
        {
            "conditionId": "cid_alias",
            "token_id": "tok_alias",
            "outcome": "No",
            "side": "buy",
            "price": 0.25,
            "size": 8,
            "ts": 2222,
            "usdcSize": 2.0,
            "event_uid": "ev-alias",
        }
    ])
    pos = _only_position(result)
    assert pos["condition_id"] == "cid_alias"
    assert pos["asset"] == "tok_alias"
    assert pos["first_buy_ts"] == 2222
    assert pos["first_buy_notional"] == pytest.approx(2.0)
    assert pos["cumulative_size_path"][0]["notional"] == pytest.approx(2.0)


def test_side_normalization_and_malformed_side_counter():
    rows = [
        _row(side="Buy", event_uid="buy1"),
        _row(side="SELL", size=3, ts=1010, event_uid="sell1"),
        _row(side="long", ts=1020, event_uid="bad1"),
    ]
    result = compute_position_building_surface(rows)
    pos = _only_position(result)
    assert pos["n_buy_fills"] == 1
    assert pos["n_sell_fills"] == 1
    assert pos["row_quality"]["malformed_rows"] == 1
    assert result["row_quality_summary"]["malformed_rows"] == 1


def test_missing_required_fields_increment_row_quality_counters():
    rows = [
        _row(price=None, event_uid="missing-price"),
        _row(size=None, ts=1010, event_uid="missing-size"),
        _row(ts=None, event_uid="missing-ts"),
    ]
    result = compute_position_building_surface(rows)
    pos = _only_position(result)
    assert pos["row_quality"]["missing_price_rows"] == 1
    assert pos["row_quality"]["missing_size_rows"] == 1
    assert pos["row_quality"]["missing_timestamp_rows"] == 1
    assert pos["row_quality"]["ignored_rows"] == 3
    assert pos["accumulation_pattern"] == "unavailable"


def test_notional_derived_and_mismatch_caveat():
    result = compute_position_building_surface([
        _row(size=10, price=0.40, event_uid="ok"),
        _row(size=10, price=0.40, ts=1010, event_uid="bad", notional=9.0),
    ])
    pos = _only_position(result)
    assert pos["cumulative_size_path"][0]["notional"] == pytest.approx(4.0)
    assert "SIZE_NOTIONAL_MISMATCH" in pos["caveats"]
    assert pos["row_quality"]["size_notional_mismatch_rows"] == 1


def test_identity_derivation_order():
    cases = [
        (_row(event_uid="ev1"), "stable_event_uid"),
        (_row(event_uid=None, raw_event_id="raw1"), "stable_event_uid"),
        (_row(event_uid=None, transaction_hash="tx1", logIndex=3), "stable_transaction"),
        (_row(event_uid=None, source_ref="batch1", row_index=1), "source_bounded_ordinal"),
        (_row(event_uid=None, row_index=1), "local_order_only"),
        (_row(event_uid=None), "unavailable"),
    ]
    for row, expected in cases:
        result = compute_position_building_surface([row])
        pos = _only_position(result)
        assert pos["identity_quality"] == expected


def test_same_second_distinct_event_uids_remain_separate():
    result = compute_position_building_surface([
        _row(size=1, event_uid="ev1"),
        _row(size=2, event_uid="ev2"),
    ])
    pos = _only_position(result)
    assert pos["n_buy_fills"] == 2
    assert [p["event_uid"] for p in pos["cumulative_size_path"]] == ["ev1", "ev2"]
    assert "ORDER_AMBIGUOUS_SAME_SECOND" not in pos["caveats"]


def test_same_second_no_discriminator_marked_ambiguous_same_second():
    result = compute_position_building_surface([
        _row(size=1),
        _row(size=2),
    ])
    pos = _only_position(result)
    assert pos["n_buy_fills"] == 2
    assert pos["identity_quality"] == "ambiguous_same_second"
    assert "ORDER_AMBIGUOUS_SAME_SECOND" in pos["caveats"]
    assert pos["row_quality"]["ambiguous_rows"] == 2


def test_position_key_asset_wins_over_outcome_text():
    result = compute_position_building_surface([
        _row(cid="cidA", asset="tokA", outcome="Candidate A", event_uid="ev1"),
        _row(cid="cidA", asset="tokA", outcome="Different Text", ts=1010, event_uid="ev2"),
    ])
    pos = _only_position(result)
    assert pos["position_key"]["type"] == "asset"
    assert pos["position_key"]["asset"] == "tokA"
    assert "LABEL_FALLBACK_IDENTITY" not in pos["caveats"]


def test_position_key_outcome_fallback_marks_label_fallback():
    result = compute_position_building_surface([
        _row(cid="cidA", asset=None, outcome="Candidate A", event_uid="ev1"),
    ])
    pos = _only_position(result)
    assert pos["position_key"]["type"] == "label_fallback"
    assert pos["identity_quality"] == "label_fallback"
    assert "LABEL_FALLBACK_IDENTITY" in pos["caveats"]


def test_activity_cap_hit_without_fallback_caps_quality_and_emits_top_level_caveat():
    result = compute_position_building_surface(
        [_row(event_uid="ev1")],
        activity_cap_hit=True,
        subgraph_fallback_used=False,
        walk_full_history=False,
    )
    pos = _only_position(result)
    assert pos["source_quality"] == "activity_rows_partial"
    assert "ACTIVITY_CAP_HIT" in result["top_level_caveats"]
    assert "ACTIVITY_TRUNCATED_POSITION_PATH" in pos["caveats"]


def test_activity_cap_hit_with_fallback_does_not_emit_top_level_caveat():
    result = compute_position_building_surface(
        [_row(event_uid="ev1")],
        activity_cap_hit=True,
        subgraph_fallback_used=True,
        walk_full_history=False,
    )
    assert "ACTIVITY_CAP_HIT" not in result["top_level_caveats"]


def test_repeated_buys_produce_cumulative_size_and_average_entry_path():
    rows = [
        _row(size=10, price=0.40, ts=1000, event_uid="b1"),
        _row(size=20, price=0.50, ts=1010, event_uid="b2"),
        _row(size=30, price=0.60, ts=1020, event_uid="b3"),
    ]
    pos = _only_position(compute_position_building_surface(rows))
    assert [e["observed_size"] for e in pos["cumulative_size_path"]] == [10, 30, 60]
    assert pos["final_observed_size"] == pytest.approx(60)
    assert pos["peak_observed_size"] == pytest.approx(60)
    assert pos["average_entry_path"][-1]["avg_entry_price"] == pytest.approx(
        (0.40 * 10 + 0.50 * 20 + 0.60 * 30) / 60
    )
    assert {e["provenance"] for e in pos["average_entry_path"]} == {"factual"}


def test_milestones_use_peak_not_final_after_sell():
    rows = [
        _row(size=40, ts=1000, event_uid="b1"),
        _row(size=60, ts=1100, event_uid="b2"),
        _row(side="SELL", size=40, ts=1200, event_uid="s1"),
    ]
    pos = _only_position(compute_position_building_surface(rows))
    assert pos["peak_observed_size"] == pytest.approx(100)
    assert pos["final_observed_size"] == pytest.approx(60)
    assert pos["time_to_50pct_size_ms"] == 100_000
    assert pos["time_to_75pct_size_ms"] == 100_000
    assert pos["n_buys_before_50pct"] == 2
    assert pos["n_buys_before_75pct"] == 2


def test_average_entry_inferred_after_first_sell():
    rows = [
        _row(size=10, price=0.40, ts=1000, event_uid="b1"),
        _row(side="SELL", size=5, price=0.50, ts=1010, event_uid="s1"),
        _row(size=10, price=0.60, ts=1020, event_uid="b2"),
    ]
    pos = _only_position(compute_position_building_surface(rows))
    assert "AVERAGE_ENTRY_INFERRED_AFTER_SELL" in pos["caveats"]
    assert pos["average_entry_path"][-1]["provenance"] == "inferred"
    assert pos["fields_provenance"]["average_entry_path"] == "inferred"


def test_partial_sell_ladder_and_summary_preserve_all_sells():
    rows = [
        _row(size=100, ts=1000, event_uid="b1"),
        _row(side="SELL", size=10, price=0.50, ts=1010, event_uid="s1"),
        _row(side="SELL", size=20, price=0.60, ts=1020, event_uid="s2"),
        _row(side="SELL", size=30, price=0.70, ts=1030, event_uid="s3"),
    ]
    pos = _only_position(compute_position_building_surface(rows))
    assert [e["event_uid"] for e in pos["partial_sell_ladder"]] == ["s1", "s2", "s3"]
    assert pos["sell_ladder_summary"]["n_sell_fills"] == 3
    assert pos["sell_ladder_summary"]["total_sell_size"] == pytest.approx(60)
    assert pos["sell_ladder_summary"]["size_weighted_sell_price"] == pytest.approx(
        (0.50 * 10 + 0.60 * 20 + 0.70 * 30) / 60
    )
    assert pos["sell_ladder_summary"]["sell_price_method"] == "size_weighted"


def test_sell_exceeds_observed_size_caveats_and_mid_position_rule():
    no_prior = _only_position(compute_position_building_surface([
        _row(side="SELL", size=10, ts=1000, event_uid="s1"),
    ]))
    assert no_prior["final_observed_size"] == pytest.approx(0)
    assert "SELL_EXCEEDS_OBSERVED_SIZE" in no_prior["caveats"]
    assert "LIKELY_STARTED_MID_POSITION" in no_prior["caveats"]

    with_prior = _only_position(compute_position_building_surface([
        _row(size=5, ts=1000, event_uid="b1"),
        _row(side="SELL", size=10, ts=1010, event_uid="s1"),
    ]))
    assert "SELL_EXCEEDS_OBSERVED_SIZE" in with_prior["caveats"]
    assert "LIKELY_STARTED_MID_POSITION" not in with_prior["caveats"]


def test_reaccumulation_detected_with_cumulative_material_delta_and_dust_ignored():
    material = _only_position(compute_position_building_surface([
        _row(size=100, ts=1000, event_uid="b1"),
        _row(side="SELL", size=5, ts=1010, event_uid="s1"),
        _row(side="SELL", size=5, ts=1020, event_uid="s2"),
        _row(side="SELL", size=5, ts=1030, event_uid="s3"),
        _row(size=5, ts=1040, event_uid="b2"),
        _row(size=5, ts=1050, event_uid="b3"),
        _row(size=5, ts=1060, event_uid="b4"),
    ]))
    assert material["reaccumulation_detected"] is True

    dust = _only_position(compute_position_building_surface([
        _row(size=100, ts=1000, event_uid="b1"),
        _row(side="SELL", size=0.5, ts=1010, event_uid="s1"),
        _row(side="SELL", size=0.5, ts=1020, event_uid="s2"),
        _row(size=0.5, ts=1030, event_uid="b2"),
        _row(size=0.5, ts=1040, event_uid="b3"),
    ]))
    assert dust["reaccumulation_detected"] is False
    assert len(dust["cumulative_size_path"]) == 5


def test_pattern_front_loaded_gradual_small_buy_and_repeated_buy():
    front = _only_position(compute_position_building_surface([
        _row(size=20, ts=1000, event_uid="b1"),
        _row(size=20, ts=1010, event_uid="b2"),
        _row(size=20, ts=1020, event_uid="b3"),
        _row(size=20, ts=1030, event_uid="b4"),
    ]))
    assert front["accumulation_pattern"] == "front_loaded"
    assert front["small_buy_accumulation_detected"] is True

    gradual = _only_position(compute_position_building_surface([
        _row(size=20, ts=1000, event_uid="g1"),
        _row(size=20, ts=1200, event_uid="g2"),
        _row(size=20, ts=1400, event_uid="g3"),
        _row(size=20, ts=1600, event_uid="g4"),
    ]))
    assert gradual["accumulation_pattern"] == "gradual_accumulation"
    assert gradual["small_buy_accumulation_detected"] is True

    fallback = _only_position(compute_position_building_surface([
        _row(size=20, ts=1000, event_uid="f1"),
        _row(size=20, ts=1000, event_uid="f2"),
        _row(size=20, ts=1000, event_uid="f3"),
    ]))
    assert fallback["accumulation_pattern"] == "small_buy_accumulation"

    repeated = _only_position(compute_position_building_surface([
        _row(size=60, ts=1000, event_uid="r1"),
        _row(size=40, ts=1400, event_uid="r2"),
    ]))
    assert repeated["accumulation_pattern"] == "repeated_buy"


def test_closed_position_only_emits_no_usable_buy_path_and_context_counts():
    result = compute_position_building_surface([], closed_positions=[_closed()])
    pos = _only_position(result)
    assert pos["position_key"]["type"] == "asset"
    assert pos["identity_quality"] == "unavailable"
    assert pos["accumulation_pattern"] == "unavailable"
    assert "NO_USABLE_BUY_PATH" in pos["caveats"]
    assert pos["fields_provenance"]["first_buy_ts"] == "unavailable"
    assert result["wallet_summary"]["n_context_only_positions"] == 1
    assert result["wallet_summary"]["n_unavailable_positions"] == 1


def test_context_only_falls_back_to_label_when_no_asset():
    result = compute_position_building_surface([], closed_positions=[
        _closed(asset=None, outcome="Candidate A")
    ])
    pos = _only_position(result)
    assert pos["position_key"]["type"] == "label_fallback"
    assert pos["identity_quality"] == "label_fallback"
    assert "LABEL_FALLBACK_IDENTITY" in pos["caveats"]
    assert "NO_USABLE_BUY_PATH" in pos["caveats"]


def test_observation_quality_good_partial_and_source_bounded_cap():
    good = _only_position(compute_position_building_surface([
        _row(event_uid="ev1", capture_run_id="run1", source="ledger", source_ref="ref1")
    ]))
    assert good["source_quality"] == "ledger_enriched"
    assert good["identity_quality"] == "stable_event_uid"
    assert good["observation_window_quality"] == "good"

    bounded = _only_position(compute_position_building_surface([
        _row(event_uid=None, source_ref="ref1", row_index=1)
    ]))
    assert bounded["identity_quality"] == "source_bounded_ordinal"
    assert bounded["observation_window_quality"] == "partial"

    label = _only_position(compute_position_building_surface([
        _row(asset=None, outcome="A", event_uid="ev1")
    ]))
    assert label["observation_window_quality"] == "partial"


def test_wallet_summary_denominators_and_decision_rules():
    rows = [
        _row(cid="a", asset="tokA", size=10, event_uid="a1"),
        _row(cid="b", asset="tokB", size=10, event_uid="b1"),
        _row(cid="b", asset="tokB", size=10, ts=1010, event_uid="b2"),
        _row(cid="c", asset="tokC", size=10, event_uid="c1"),
        _row(cid="c", asset="tokC", side="SELL", size=3, ts=1010, event_uid="c2"),
        _row(cid="c", asset="tokC", side="SELL", size=3, ts=1020, event_uid="c3"),
    ]
    result = compute_position_building_surface(rows, closed_positions=[
        _closed(cid="d", asset="tokD")
    ])
    summary = result["wallet_summary"]
    assert summary["n_positions"] == 4
    assert summary["n_reconstructed_positions"] == 3
    assert summary["n_context_only_positions"] == 1
    assert summary["n_unavailable_positions"] == 1
    assert summary["share_sell_ladder_positions"] == pytest.approx(1 / 3)
    assert summary["share_positions_with_no_usable_buy_path"] == pytest.approx(1 / 4)
    assert summary["dominant_accumulation_pattern"] is not None
    assert summary["high_confidence_position_count"] >= 0
    assert result["source_quality_distribution"]
    assert result["identity_quality_distribution"]
    assert result["row_quality_summary"]["malformed_rows"] == 0


def test_no_forbidden_output_keys():
    forbidden = {
        "recommended_trigger",
        "trigger_threshold",
        "copy_after_first_buy",
        "copy_after_n_buys",
        "expected_copy_pnl",
        "simulated_copy_result",
        "copy_strategy",
        "copy_signal",
    }
    result = compute_position_building_surface([
        _row(event_uid="ev1"),
        _row(side="SELL", size=5, ts=1010, event_uid="ev2"),
    ])

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in forbidden
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)


# ---------------------------------------------------------------------------
# §2.6 poor label tests
# ---------------------------------------------------------------------------

def _only_position_poor_tests(result):
    return result["positions"][0]


def test_poor_when_first_buy_ts_missing():
    """Trigger 1a: first entry missing — usable rows exist but no BUY fill
    (SELL-only), so first_buy_ts is None → poor. Spec §2.6.

    Note: rows with ts=None or price=None fail the usable check entirely and
    produce 'unavailable', not 'poor'.  The 'poor' floor fires when usable
    rows exist (position reconstructable) but no BUY fill was captured.
    """
    pos = _only_position_poor_tests(compute_position_building_surface([
        _row(side="SELL", size=10.0, ts=1000, event_uid="ev1")
    ]))
    assert pos["n_buy_fills"] == 0
    assert pos["n_sell_fills"] == 1
    assert pos["first_buy_ts"] is None
    assert pos["observation_window_quality"] == "poor"


def test_poor_when_first_buy_price_missing():
    """Trigger 1b: first entry missing — no usable BUY fills → first_buy_price
    is None → poor. Spec §2.6.  Same root cause as 1a (SELL-only path).
    """
    pos = _only_position_poor_tests(compute_position_building_surface([
        _row(side="SELL", size=5.0, ts=1000, event_uid="ev2"),
        _row(side="SELL", size=5.0, ts=1010, event_uid="ev3"),
    ]))
    assert pos["n_buy_fills"] == 0
    assert pos["first_buy_price"] is None
    assert pos["observation_window_quality"] == "poor"


def test_poor_when_milestone_path_mostly_missing():
    """Trigger 2: >=3 buy fills but all milestone timestamps None → poor. Spec §2.6.

    Milestone timestamps are None when peak_size<=0 (all buy sizes are 0.0,
    so no size accumulates and the milestone cross never happens).
    first_buy_ts and first_buy_price are both set (trigger 1 does not fire).
    """
    pos = _only_position_poor_tests(compute_position_building_surface([
        _row(size=0.0, ts=1000, event_uid="ev1"),
        _row(size=0.0, ts=1001, event_uid="ev2"),
        _row(size=0.0, ts=1002, event_uid="ev3"),
    ]))
    assert pos["n_buy_fills"] == 3
    assert pos["first_buy_ts"] is not None
    assert pos["first_buy_price"] is not None
    assert pos["time_to_50pct_size_ms"] is None
    assert pos["time_to_75pct_size_ms"] is None
    assert pos["time_to_peak_size_ms"] is None
    assert pos["observation_window_quality"] == "poor"


def test_poor_when_sell_ladder_ambiguous_despite_multiple_sells():
    """Trigger 3: >=2 SELL fills but all have zero size → partial sell_price_method → poor. Spec §2.6.

    Usable rows require size is not None; size=0.0 is usable but causes
    sell_price_method='partial' (the all-(r.size or 0) > 0 guard fails),
    which maps to 'sell ladder mostly missing/ambiguous' per spec §2.6.
    """
    pos = _only_position_poor_tests(compute_position_building_surface([
        _row(size=10, ts=1000, event_uid="ev1"),
        _row(side="SELL", size=0.0, ts=1010, event_uid="ev2"),
        _row(side="SELL", size=0.0, ts=1020, event_uid="ev3"),
    ]))
    assert pos["n_sell_fills"] == 2
    assert pos["sell_ladder_summary"]["sell_price_method"] == "partial"
    assert pos["observation_window_quality"] == "poor"


def test_unavailable_not_replaced_by_poor():
    """No usable BUY path → unavailable (not poor). Spec §2.6 order: unavailable first."""
    pos = _only_position_poor_tests(compute_position_building_surface(
        [], closed_positions=[_closed()]
    ))
    assert pos["observation_window_quality"] == "unavailable"


def test_partial_not_overridden_by_poor_for_capped_but_complete_reconstruction():
    """Cap-hit + no fallback + complete first-entry + milestones present + valid sell ladder → partial, not poor.

    Guards against over-aggressive poor rule (spec §2.6 floor must not
    swallow cases that are legitimately only partial due to cap).
    """
    pos = _only_position_poor_tests(compute_position_building_surface(
        [
            _row(size=10, ts=1000, event_uid="ev1"),
            _row(side="SELL", size=5, ts=1010, event_uid="ev2"),
            _row(side="SELL", size=5, ts=1020, event_uid="ev3"),
        ],
        activity_cap_hit=True,
    ))
    # first_buy_ts and first_buy_price both set; sell ladder has size → size_weighted
    assert pos["first_buy_ts"] is not None
    assert pos["first_buy_price"] is not None
    assert pos["sell_ladder_summary"]["sell_price_method"] == "size_weighted"
    assert pos["observation_window_quality"] == "partial"


def test_ambiguous_same_second_identity_is_partial_not_poor():
    """ambiguous_same_second identity with complete first-entry → partial, not poor. Spec §2.6."""
    pos = _only_position_poor_tests(compute_position_building_surface([
        _row(size=10, ts=1000, event_uid=None),
        _row(size=10, ts=1000, event_uid=None),
    ]))
    assert pos["identity_quality"] == "ambiguous_same_second"
    assert pos["first_buy_ts"] is not None
    assert pos["first_buy_price"] is not None
    assert pos["observation_window_quality"] == "partial"


def test_low_confidence_position_count_includes_poor():
    """low_confidence_position_count must count poor positions. Spec §2.6 / wallet_summary."""
    result = compute_position_building_surface([
        # position A: good (ledger_enriched + stable_event_uid + BUY fill present)
        _row(cid="a", asset="tokA", size=10, ts=1000, event_uid="a1",
             source="ledger", source_ref="sref1", capture_run_id="run1"),
        # position B: poor (SELL-only → first_buy_ts is None, trigger 1a)
        _row(cid="b", asset="tokB", side="SELL", size=5, ts=1000, event_uid="b1"),
    ], closed_positions=[_closed(cid="c", asset="tokC")])
    summary = result["wallet_summary"]
    poor_count = sum(
        1 for p in result["positions"]
        if p["observation_window_quality"] == "poor"
    )
    assert poor_count >= 1
    assert summary["low_confidence_position_count"] == sum(
        1 for p in result["positions"]
        if p["observation_window_quality"] in {"poor", "unavailable"}
    )


def test_w2_does_not_import_forbidden_modules():
    path = Path("sniper/analysis/wallet_protocol/position_building.py")
    tree = ast.parse(path.read_text())
    forbidden_roots = {"requests", "httpx", "urllib", "aiohttp"}
    forbidden_contains = {
        "sniper.analysis.wallet_protocol.ledger",
        "sniper.analysis.wallet_protocol.timing",
        "sniper.analysis.wallet_protocol.exit_attribution",
        "sniper.analysis.wallet_protocol.analyze_wallet",
        "sniper.analysis.wallet_protocol.report",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden_roots
                assert not any(alias.name.startswith(s) for s in forbidden_contains)
        elif isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in forbidden_roots
            assert not any(node.module.startswith(s) for s in forbidden_contains)


def test_no_http_calls_during_compute(monkeypatch):
    calls = {"n": 0}

    def record(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("HTTP call forbidden")

    requests = pytest.importorskip("requests")
    monkeypatch.setattr(requests.Session, "get", record)
    monkeypatch.setattr(requests.Session, "post", record)
    monkeypatch.setattr(requests, "get", record)
    monkeypatch.setattr(requests, "post", record)

    compute_position_building_surface([_row(event_uid="ev1")])
    assert calls["n"] == 0


def test_fixture_replay_smoke_swissmiss_or_yatsen():
    candidates = [
        Path("tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-04-30.json"),
        Path("tests/calibration/fixtures/0x5bffcf561bcae83af680ad600cb99f1184d6ffbe_2026-04-30.json"),
        Path("tests/replay/fixtures/swissmiss_fetch_snapshot.json"),
    ]
    fixture = next((p for p in candidates if p.exists()), None)
    if fixture is None:
        pytest.skip("No replayable SwissMiss/YatSen fixture with activity_rows and closed_positions available locally")

    data = json.loads(fixture.read_text())
    if not data.get("activity_rows") or not data.get("closed_positions"):
        pytest.skip("No replayable SwissMiss/YatSen fixture with activity_rows and closed_positions available locally")

    result = compute_position_building_surface(
        data["activity_rows"],
        data["closed_positions"],
        wallet=data.get("wallet"),
        activity_cap_hit=bool(data.get("activity_cap_hit")),
        subgraph_fallback_used=bool(data.get("subgraph_fallback_used")),
        walk_full_history=bool(data.get("walk_full_history")),
    )

    assert result is not None
    assert result["surface_version"] == "position_building_v1"
    assert result["wallet_summary"]["n_positions"] > 0
    allowed = {
        "unavailable",
        "fragmented_or_ambiguous",
        "single_fill",
        "front_loaded",
        "gradual_accumulation",
        "small_buy_accumulation",
        "repeated_buy",
    }
    assert {p["accumulation_pattern"] for p in result["positions"]} <= allowed

    forbidden = {
        "recommended_trigger",
        "trigger_threshold",
        "copy_after_first_buy",
        "copy_after_n_buys",
        "expected_copy_pnl",
        "simulated_copy_result",
        "copy_strategy",
        "copy_signal",
    }

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in forbidden
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(result)


# ---------------------------------------------------------------------------
# Phase 3 — analyze_wallet wiring + report render
# ---------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _phase3_pos(
    cid="cid_a", outcome="Yes", curPrice=1.0, avgPrice=0.50, size=100,
    realizedPnl=50.0, ts=None, eventSlug=None,
):
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts,
        "eventSlug": eventSlug or f"slug-{cid}",
    }


def _phase3_activity(cid="cid_a", outcome="Yes", side="BUY", size=10.0,
                     price=0.40, ts=None, asset="tok_a", event_uid=None):
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
    row = {
        "conditionId": cid,
        "outcome": outcome,
        "asset": asset,
        "side": side,
        "size": size,
        "price": price,
        "timestamp": ts,
        "type": "TRADE",
    }
    if event_uid is not None:
        row["event_uid"] = event_uid
    return row


def _phase3_wallet_data(
    *,
    closed_positions=None,
    activity_rows=None,
    activity_cap_hit=False,
    subgraph_fallback_used=False,
    walk_full_history=False,
    n_unmapped_fills=0,
):
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions or []),
        open_positions=[],
        activity_rows=list(activity_rows or []),
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=subgraph_fallback_used,
        walk_full_history=walk_full_history,
        n_unmapped_fills=n_unmapped_fills,
    )


def test_phase3_analyze_wallet_populates_position_building_surface():
    """W2 Phase 3: analyze_wallet must call compute_position_building_surface
    and populate the typed field with the canonical surface_version."""
    activity = [
        _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=10.0,
                         price=0.40, ts=1000, event_uid="ev_b1"),
        _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=20.0,
                         price=0.50, ts=2000, event_uid="ev_b2"),
    ]
    closed = [_phase3_pos(cid="m1", realizedPnl=15.0, ts=2500)]
    data = _phase3_wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.position_building_surface is not None
    surface = report.position_building_surface
    assert surface["surface_version"] == "position_building_v1"
    assert surface["wallet"] == "0xabc"
    assert surface["wallet_summary"]["n_positions"] >= 1


def test_phase3_position_building_surface_none_when_no_activity_or_closed():
    """compute_position_building_surface returns None when there are no
    activity rows AND no closed positions; analyze_wallet preserves that
    contract on the typed field."""
    data = _phase3_wallet_data(closed_positions=[], activity_rows=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.position_building_surface is None


def test_phase3_position_building_surface_built_from_closed_only():
    """Even with no activity rows, closed positions alone produce a surface
    (context-only positions, NO_USABLE_BUY_PATH caveat)."""
    closed = [_phase3_pos(cid="m1", realizedPnl=10.0, ts=2000)]
    data = _phase3_wallet_data(closed_positions=closed, activity_rows=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.position_building_surface is not None
    summary = report.position_building_surface["wallet_summary"]
    # closed-only -> context-only positions counted
    assert summary["n_context_only_positions"] >= 1


def test_phase3_render_position_building_section_when_populated():
    """When the surface is populated, the render section appears with the
    spec §5.4 compact summary fields. Display-only language only — no
    triggers, no copy recommendations."""
    activity = [
        _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=10.0,
                         price=0.40, ts=1000, event_uid="ev_b1"),
        _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=20.0,
                         price=0.50, ts=2000, event_uid="ev_b2"),
        _phase3_activity(cid="m2", asset="tokm2", side="BUY", size=15.0,
                         price=0.30, ts=3000, event_uid="ev_b3"),
    ]
    closed = [
        _phase3_pos(cid="m1", realizedPnl=10.0, ts=2500),
        _phase3_pos(cid="m2", realizedPnl=-3.0, ts=3500),
    ]
    data = _phase3_wallet_data(closed_positions=closed, activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    md = render_report(report)
    assert "## Position building" in md
    assert "dominant_accumulation_pattern:" in md
    assert "dominant_limitation:" in md
    assert "observation_window_coverage:" in md
    assert "high_confidence_position_count:" in md
    assert "low_confidence_position_count:" in md
    # Display-only discipline — no triggers, no recommendations.
    forbidden_phrases = [
        "recommended trigger",
        "copy after",
        "expected copy",
        "simulated copy",
        "deploy this strategy",
    ]
    for phrase in forbidden_phrases:
        assert phrase not in md.lower()


def test_phase3_render_position_building_unavailable_when_field_none():
    """When position_building_surface is None (no usable inputs), the render
    section MUST print a clean 'unavailable' line, NOT silently omit."""
    data = _phase3_wallet_data(closed_positions=[], activity_rows=[])
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    assert report.position_building_surface is None
    md = render_report(report)
    assert "## Position building" in md
    assert "unavailable" in md.lower()


def _verdict_signature_pb(r: ProtocolReport) -> tuple:
    return (
        r.overall_verdict,
        r.gate_result,
        r.primary_edge_type,
        r.copyability_note,
    )


def test_phase3_position_building_does_not_alter_verdict_signature():
    """Hard-guard: populating position_building_surface must NEVER mutate
    overall_verdict / gate_result / primary_edge_type / copyability_note
    relative to the same closed-position universe with no activity rows."""
    closed = [_phase3_pos(cid="m1", realizedPnl=20.0, ts=1000 + i)
              for i in range(40)]
    base = _phase3_wallet_data(closed_positions=closed, activity_rows=[])
    enriched = _phase3_wallet_data(
        closed_positions=closed,
        activity_rows=[
            _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=10.0,
                             price=0.40, ts=900, event_uid="ev_b1"),
        ],
    )
    r_base = analyze_wallet("0xabc", pre_fetched_data=base, n_iter=200, seed=1)
    r_enr = analyze_wallet("0xabc", pre_fetched_data=enriched, n_iter=200, seed=1)
    # Both should have the surface populated (closed_only -> context-only path).
    assert r_base.position_building_surface is not None
    assert r_enr.position_building_surface is not None
    # The verdict signature must NOT differ.
    assert _verdict_signature_pb(r_base) == _verdict_signature_pb(r_enr)


def test_phase3_position_building_no_forbidden_keys_in_surface():
    """Walk the surface dict; assert no copy/trigger keys leaked from upstream."""
    activity = [
        _phase3_activity(cid="m1", asset="tokm1", side="BUY", size=10.0,
                         price=0.40, ts=1000, event_uid="ev_b1"),
        _phase3_activity(cid="m1", asset="tokm1", side="SELL", size=5.0,
                         price=0.55, ts=2000, event_uid="ev_s1"),
    ]
    data = _phase3_wallet_data(activity_rows=activity)
    report = analyze_wallet("0xabc", pre_fetched_data=data, n_iter=200, seed=1)
    surface = report.position_building_surface
    assert surface is not None
    forbidden = {
        "recommended_trigger",
        "trigger_threshold",
        "copy_after_first_buy",
        "copy_after_n_buys",
        "expected_copy_pnl",
        "simulated_copy_result",
        "copy_strategy",
        "copy_signal",
    }

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                assert k not in forbidden
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(surface)


def test_phase3_no_ledger_imports_in_analyze_wallet_or_report():
    """Wiring must not introduce ledger-module imports into analyze_wallet.py
    or report.py — preserves the doctrine separation between core surfaces
    and ledger enrichment."""
    forbidden_root = "sniper.analysis.wallet_protocol.ledger"
    for path in (
        Path("sniper/analysis/wallet_protocol/analyze_wallet.py"),
        Path("sniper/analysis/wallet_protocol/report.py"),
    ):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_root), (
                    f"forbidden ledger import in {path}: {node.module}"
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_root), (
                        f"forbidden ledger import in {path}: {alias.name}"
                    )
