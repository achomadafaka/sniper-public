"""Acceptance tests for the buyer-breadth scorer and its immutable receipt.

Contract: docs/superpowers/specs/2026-08-19-radar-buyer-breadth-preregistration.md
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sniper.research.memecoin_opportunity.insights import connect_insights
from sniper.research.memecoin_opportunity.breadth_scorer import (
    BREADTH_CHECKPOINTS,
    BREADTH_GATE_VERSION,
    BREADTH_MATCH_SCALE,
    BREADTH_OUTCOME_SOURCE,
    FORWARD_CLOCK_ISO,
    REQUIRED_MODES,
    validate_cli,
    score_breadth,
    seal_breadth_checkpoints,
)

CLOCK = "2026-08-20T13:55:02Z"
NEW_CLOCK = "2026-08-21T00:00:00Z"
NEW_POST_CLOCK = "2026-08-21T00:01:00Z"


def _insights(tmp_path):
    return connect_insights(tmp_path / "insights.db")


def _label(conn, launch_id, *, klass, mode="standard", age=1_000, progress=0,
           cost=-250, censor=None, observed="2026-08-20T14:00:00Z"):
    conn.execute(
        "INSERT INTO op_insight_breadth_labels(launch_id,network_id,venue_id,"
        "decision_snapshot_id,rule_version,breadth_class,censor_reason,"
        "eligible_buyer_count,entity_buyer_count,launch_mode,decision_age_ms,"
        "progress_bps,modeled_round_trip_cost_bps,observed_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (launch_id, "solana", "pumpfun_solana", f"snap-{launch_id}",
         "radar-buyer-breadth-v1", klass, censor, None, None, mode,
         age, progress, cost, observed, observed),
    )


def _alert(conn, launch_id, *, ret, creator="creator-x", mint="SOL",
           observed="2026-08-20T14:00:00Z", scored=True, rule="route-control-v2",
           evaluator="alert-outcome-v4", suffix="", provider="local_curve_math",
           quote_age_ms=50, effective_at=None):
    effective_at = effective_at or observed
    conn.execute(
        "INSERT INTO op_insight_alerts(alert_id,launch_id,venue_id,token_id,"
        "entry_id,observed_at,evaluator_version,score_status,terminal_return_bps,"
        "source_fingerprint,created_at,rule_version,feature_version,feature_json,"
        "creator_id,entry_effective_at,provider,quote_age_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"alert-{launch_id}{suffix}", launch_id, "pumpfun_solana", f"tok-{launch_id}",
         f"entry-{launch_id}{suffix}", observed, evaluator,
         "SCORED" if scored else "PENDING", ret if scored else None,
         f"fp-{launch_id}{suffix}", observed, rule, "v1",
         json.dumps({"quote_mint": mint}), creator, effective_at, provider,
         quote_age_ms),
    )


def test_pairs_form_only_inside_one_launch_mode(tmp_path):
    """A cashback pass must never match a standard control."""
    conn = _insights(tmp_path)
    _label(conn, "L1", klass="pass", mode="cashback")
    _alert(conn, "L1", ret=500)
    _label(conn, "L2", klass="control", mode="standard")
    _alert(conn, "L2", ret=100)

    result = score_breadth(conn, since_iso=CLOCK)

    assert result["cashback"]["matched_pairs"] == 0
    assert result["standard"]["matched_pairs"] == 0


def test_label_censor_rate_is_censored_over_all_labels(tmp_path):
    """Denominator is ALL labels, middle included."""
    conn = _insights(tmp_path)
    _label(conn, "L1", klass="censored", censor="unknown_buyer_count")
    _label(conn, "L2", klass="middle")
    _label(conn, "L3", klass="pass")
    _alert(conn, "L3", ret=10)
    _label(conn, "L4", klass="control")
    _alert(conn, "L4", ret=10)

    rate = score_breadth(conn, since_iso=CLOCK)["standard"]["label_censor_rate"]

    assert rate == pytest.approx(0.25)


def test_outcome_censor_rate_is_reported_per_class_and_excludes_middle(tmp_path):
    """pass and control get separate rates; middle enters no denominator."""
    conn = _insights(tmp_path)
    _label(conn, "P1", klass="pass")
    _alert(conn, "P1", ret=500)
    _label(conn, "P2", klass="pass")            # no alert row -> censored outcome
    _label(conn, "C1", klass="control")
    _alert(conn, "C1", ret=100)
    _label(conn, "M1", klass="middle")          # must not move either rate

    stats = score_breadth(conn, since_iso=CLOCK)["standard"]

    assert stats["outcome_censor_rate_pass"] == pytest.approx(0.5)
    assert stats["outcome_censor_rate_control"] == pytest.approx(0.0)


def test_scored_but_unmatched_row_is_not_censored(tmp_path):
    """A caliper rejection is gate 1's business, never a censor."""
    conn = _insights(tmp_path)
    _label(conn, "P1", klass="pass", progress=0)
    _alert(conn, "P1", ret=500)
    # control sits far outside the progress caliper, so it cannot match
    _label(conn, "C1", klass="control", progress=999_999)
    _alert(conn, "C1", ret=100)

    stats = score_breadth(conn, since_iso=CLOCK)["standard"]

    assert stats["matched_pairs"] == 0
    assert stats["outcome_censor_rate_pass"] == pytest.approx(0.0)
    assert stats["outcome_censor_rate_control"] == pytest.approx(0.0)


def test_seal_writes_once_and_never_moves(tmp_path):
    """The first seal is the only seal, even when later data changes the number."""
    conn = _insights(tmp_path)
    for i in range(BREADTH_CHECKPOINTS[0]):
        _label(conn, f"P{i}", klass="pass", observed=NEW_POST_CLOCK)
        _alert(conn, f"P{i}", ret=1_000, creator=f"c{i}", observed=NEW_POST_CLOCK)
        _label(conn, f"C{i}", klass="control", observed=NEW_POST_CLOCK)
        _alert(conn, f"C{i}", ret=0, creator=f"c{i}", observed=NEW_POST_CLOCK)

    assert seal_breadth_checkpoints(conn, now_iso="2026-08-21T01:00:00Z") == 1
    first = conn.execute(
        "SELECT sealed_at,pairs_at_seal,mean_pass_minus_control_bps "
        "FROM op_insight_breadth_checkpoints"
    ).fetchall()

    # more pairs arrive, then a second seal runs
    for i in range(100, 110):
        _label(conn, f"P{i}", klass="pass", observed=NEW_POST_CLOCK)
        _alert(conn, f"P{i}", ret=-5_000, creator=f"c{i}", observed=NEW_POST_CLOCK)
        _label(conn, f"C{i}", klass="control", observed=NEW_POST_CLOCK)
        _alert(conn, f"C{i}", ret=0, creator=f"c{i}", observed=NEW_POST_CLOCK)
    assert seal_breadth_checkpoints(conn, now_iso="2026-08-21T02:00:00Z") == 0

    assert conn.execute(
        "SELECT sealed_at,pairs_at_seal,mean_pass_minus_control_bps "
        "FROM op_insight_breadth_checkpoints"
    ).fetchall() == first


def test_seal_records_the_frozen_outcome_source(tmp_path):
    """A receipt that does not name its source cannot be audited later."""
    conn = _insights(tmp_path)
    for i in range(BREADTH_CHECKPOINTS[0]):
        _label(conn, f"P{i}", klass="pass", observed=NEW_POST_CLOCK)
        _alert(conn, f"P{i}", ret=1_000, creator=f"c{i}", observed=NEW_POST_CLOCK)
        _label(conn, f"C{i}", klass="control", observed=NEW_POST_CLOCK)
        _alert(conn, f"C{i}", ret=0, creator=f"c{i}", observed=NEW_POST_CLOCK)

    seal_breadth_checkpoints(conn, now_iso="2026-08-21T01:00:00Z")

    row = conn.execute(
        "SELECT outcome_source,match_scale,launch_mode FROM op_insight_breadth_checkpoints"
    ).fetchone()
    assert row[0] == BREADTH_OUTCOME_SOURCE
    assert row[1] == BREADTH_MATCH_SCALE
    assert row[2] == "standard"
    assert BREADTH_GATE_VERSION == "radar-buyer-breadth-gate-v2"
    assert "all-rules" in BREADTH_OUTCOME_SOURCE


# --- seal-safety blockers (2026-08-20) -------------------------------------

PRE_CLOCK = "2026-08-20T12:00:00Z"
POST_CLOCK = "2026-08-20T14:00:00Z"


def test_scoring_defaults_to_the_frozen_clock_and_drops_pre_clock_labels(tmp_path):
    """A None clock must mean the frozen clock, never "all time"."""
    conn = _insights(tmp_path)
    _label(conn, "OLD", klass="pass", observed=PRE_CLOCK)
    _alert(conn, "OLD", ret=9_000, observed=PRE_CLOCK)
    _label(conn, "OLDC", klass="control", observed=PRE_CLOCK)
    _alert(conn, "OLDC", ret=0, observed=PRE_CLOCK)

    scored = score_breadth(conn)

    # the mode is still reported, but it carries nothing from before the clock
    assert scored["standard"]["pass_labels"] == 0
    assert scored["standard"]["matched_pairs"] == 0


def test_pre_clock_launch_projected_after_the_clock_is_still_excluded(tmp_path):
    """The filter is observed_at, not the projection's created_at."""
    conn = _insights(tmp_path)
    conn.execute(
        "INSERT INTO op_insight_breadth_labels(launch_id,network_id,venue_id,"
        "decision_snapshot_id,rule_version,breadth_class,censor_reason,"
        "eligible_buyer_count,entity_buyer_count,launch_mode,decision_age_ms,"
        "progress_bps,modeled_round_trip_cost_bps,observed_at,created_at) "
        "VALUES('BACKFILL','solana','pumpfun_solana','s','radar-buyer-breadth-v1',"
        "'pass',NULL,NULL,NULL,'standard',1000,0,-250,?,?)",
        (PRE_CLOCK, POST_CLOCK),
    )
    _alert(conn, "BACKFILL", ret=9_000, observed=PRE_CLOCK)

    assert score_breadth(conn).get("standard", {}).get("pass_labels", 0) == 0


def test_seal_refuses_a_clock_override(tmp_path):
    """The seal path must not accept a caller-supplied window."""
    conn = _insights(tmp_path)
    with pytest.raises(TypeError):
        seal_breadth_checkpoints(conn, since_iso=PRE_CLOCK, now_iso=POST_CLOCK)


def test_receipt_rejects_update(tmp_path):
    conn = _insights(tmp_path)
    _seed_sealable(conn)
    seal_breadth_checkpoints(conn, now_iso="2026-08-21T01:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE op_insight_breadth_checkpoints SET mean_pass_minus_control_bps=1"
        )


def test_receipt_rejects_delete(tmp_path):
    conn = _insights(tmp_path)
    _seed_sealable(conn)
    seal_breadth_checkpoints(conn, now_iso="2026-08-21T01:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM op_insight_breadth_checkpoints")


def test_a_different_evaluator_version_never_pools(tmp_path):
    """route-control-v2 alone is not enough. The evaluator must match too."""
    conn = _insights(tmp_path)
    _label(conn, "P1", klass="pass", observed=POST_CLOCK)
    _alert(conn, "P1", ret=500, observed=POST_CLOCK, evaluator="alert-outcome-v3")
    _label(conn, "C1", klass="control", observed=POST_CLOCK)
    _alert(conn, "C1", ret=100, observed=POST_CLOCK, evaluator="alert-outcome-v3")

    stats = score_breadth(conn)["standard"]

    assert stats["matched_pairs"] == 0
    assert stats["pass_scored"] == 0
    assert "alert-outcome-v4" in BREADTH_OUTCOME_SOURCE


def _seed_sealable(conn):
    for i in range(BREADTH_CHECKPOINTS[0]):
        _label(conn, f"P{i}", klass="pass", observed=NEW_POST_CLOCK)
        _alert(conn, f"P{i}", ret=1_000, creator=f"c{i}", observed=NEW_POST_CLOCK)
        _label(conn, f"C{i}", klass="control", observed=NEW_POST_CLOCK)
        _alert(conn, f"C{i}", ret=0, creator=f"c{i}", observed=NEW_POST_CLOCK)


# --- admission-independent outcome source (2026-08-21) ----------------------


def test_all_v4_rules_form_one_admission_independent_source(tmp_path):
    conn = _insights(tmp_path)
    _label(conn, "P", klass="pass", observed=NEW_POST_CLOCK)
    _alert(
        conn, "P", ret=900, rule="pump-confirmation-truth-v6",
        observed=NEW_POST_CLOCK,
    )
    _label(conn, "C", klass="control", observed=NEW_POST_CLOCK)
    _alert(conn, "C", ret=100, observed=NEW_POST_CLOCK)

    stats = score_breadth(conn)["standard"]

    assert stats["pass_scored"] == 1
    assert stats["control_scored"] == 1
    assert stats["matched_pairs"] == 1


def test_same_launch_alerts_count_once_when_the_economic_outcome_agrees(tmp_path):
    conn = _insights(tmp_path)
    _label(conn, "P", klass="pass", observed=NEW_POST_CLOCK)
    for suffix, rule in (("-v6", "pump-confirmation-truth-v6"),
                         ("-adaptive", "radar-adaptive-v3")):
        _alert(
            conn, "P", ret=900, rule=rule, suffix=suffix,
            observed=NEW_POST_CLOCK,
        )
    _label(conn, "C", klass="control", observed=NEW_POST_CLOCK)
    _alert(conn, "C", ret=100, observed=NEW_POST_CLOCK)

    stats = score_breadth(conn)["standard"]

    assert stats["pass_scored"] == 1
    assert stats["matched_pairs"] == 1
    assert stats["_pairs"][0][0]["alert_id"] == "P"


def test_conflicting_launch_outcomes_are_censored(tmp_path):
    conn = _insights(tmp_path)
    _label(conn, "P", klass="pass", observed=NEW_POST_CLOCK)
    _alert(conn, "P", ret=900, suffix="-control", observed=NEW_POST_CLOCK)
    _alert(
        conn, "P", ret=-900, rule="pump-confirmation-truth-v6", suffix="-v6",
        observed=NEW_POST_CLOCK,
    )

    stats = score_breadth(conn)["standard"]

    assert stats["pass_scored"] == 0
    assert stats["outcome_censor_rate_pass"] == pytest.approx(1.0)


def test_an_alert_from_another_decision_time_is_censored(tmp_path):
    conn = _insights(tmp_path)
    _label(conn, "P", klass="pass", observed=NEW_POST_CLOCK)
    _alert(conn, "P", ret=900, observed="2026-08-21T00:02:00Z")

    stats = score_breadth(conn)["standard"]

    assert stats["pass_scored"] == 0
    assert stats["outcome_censor_rate_pass"] == pytest.approx(1.0)


def test_default_gate2_clock_starts_after_the_source_contract(tmp_path):
    conn = _insights(tmp_path)
    _label(conn, "OLD", klass="pass", observed=POST_CLOCK)
    _alert(conn, "OLD", ret=900, observed=POST_CLOCK)

    stats = score_breadth(conn)["standard"]

    assert FORWARD_CLOCK_ISO == NEW_CLOCK
    assert stats["pass_labels"] == 0


def test_required_modes_always_appear_even_with_no_labels(tmp_path):
    """The contract reports standard and cashback separately. A silent mode is a
    missing row, not an absent question. Its rates are None, never zero.
    """
    conn = _insights(tmp_path)

    scored = score_breadth(conn)

    for mode in REQUIRED_MODES:
        assert mode in scored
        assert scored[mode]["matched_pairs"] == 0
        assert scored[mode]["label_censor_rate"] is None
        assert scored[mode]["outcome_censor_rate_pass"] is None


def test_a_seal_may_not_be_asked_for_a_different_window_than_it_prints():
    """--seal --since would display one cohort and seal another."""
    with pytest.raises(ValueError):
        validate_cli(seal=True, since="2026-08-01T00:00:00Z")
    validate_cli(seal=True, since=None)
    validate_cli(seal=False, since="2026-08-01T00:00:00Z")
