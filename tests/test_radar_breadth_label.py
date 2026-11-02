"""`radar-buyer-breadth-v1` is a frozen, immutable, slot-free decision label.

The tests that matter here are not the happy path. They are the three ways this label
could quietly destroy the measurement it exists to serve:
  1. the thresholds move,
  2. it becomes an arm and eats the control's slot,
  3. retention or a rewrite mutates a frozen class.
"""

from __future__ import annotations

import sqlite3

import pytest

from sniper.research.memecoin_opportunity import breadth_label
from sniper.research.memecoin_opportunity.breadth_label import (
    CENSOR_UNKNOWN_BUYER_COUNT,
    CLASS_CENSORED,
    CLASS_CONTROL,
    CLASS_MIDDLE,
    CLASS_PASS,
    LABELLED_VENUE_ID,
    RULE_VERSION,
    classify,
    record_breadth_label,
)
from sniper.research.memecoin_opportunity.store import _BREADTH_LABEL_STATEMENTS


OBSERVED = "2026-08-19T10:00:00.000000Z"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for statement in _BREADTH_LABEL_STATEMENTS:
        conn.execute(statement)
    return conn


def _features(buyers, **over):
    base = {
        "eligible_buyer_count": buyers,
        "launch_mode": "standard",
        "decision_age_ms": 8_000,
        "progress_bps": 3_000,
        "modeled_round_trip_cost_bps": -191,
    }
    base.update(over)
    return base


def _write(conn, buyers, launch_id="L1", venue_id=LABELLED_VENUE_ID, **over):
    return record_breadth_label(
        conn,
        launch_id=launch_id,
        network_id="solana",
        venue_id=venue_id,
        decision_snapshot_id="S1",
        features=_features(buyers, **over),
        observed_at=OBSERVED,
    )


def test_thresholds_are_frozen_at_the_preregistered_values():
    """A silent retune is the failure this label exists to prevent."""
    assert breadth_label.PASS_MIN_BUYERS == 9
    assert breadth_label.CONTROL_MAX_BUYERS == 3
    assert RULE_VERSION == "radar-buyer-breadth-v1"


@pytest.mark.parametrize(
    "buyers,expected",
    [
        (0, CLASS_CONTROL), (3, CLASS_CONTROL),
        (4, CLASS_MIDDLE), (8, CLASS_MIDDLE),
        (9, CLASS_PASS), (147, CLASS_PASS),
    ],
)
def test_three_classes_partition_the_buyer_range(buyers, expected):
    assert classify(buyers) == expected


def test_unknown_buyer_count_writes_an_explicit_censor_row():
    """An absent row is ambiguous. It could be an old schema or a lost write.

    The gate has to measure censor coverage, so the censor must be a stored fact.
    """
    assert classify(None) == CLASS_CENSORED
    conn = _conn()
    assert _write(conn, None) == CLASS_CENSORED
    row = conn.execute(
        "SELECT breadth_class,censor_reason,eligible_buyer_count FROM op_breadth_labels"
    ).fetchone()
    assert row["breadth_class"] == CLASS_CENSORED
    assert row["censor_reason"] == CENSOR_UNKNOWN_BUYER_COUNT
    assert row["eligible_buyer_count"] is None


def test_a_censor_row_cannot_carry_a_buyer_count():
    """The CHECK constraints keep a censor and a measured class mutually exclusive."""
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO op_breadth_labels(launch_id,network_id,venue_id,"
            "decision_snapshot_id,rule_version,breadth_class,censor_reason,"
            "eligible_buyer_count,observed_at,row_fingerprint,inserted_at) "
            "VALUES('L9','solana','pumpfun_solana','S9',?,'censored','x',7,?,'f',?)",
            (RULE_VERSION, OBSERVED, OBSERVED),
        )


def test_other_venues_are_not_labelled_by_this_rule_version():
    """The venue scoring design forbids a shared control across venues."""
    conn = _conn()
    assert _write(conn, 12, launch_id="L-pons", venue_id="pons_v1_4663") is None
    assert _write(conn, 12, launch_id="L-bags", venue_id="bags_robinhood_4663") is None
    assert conn.execute("SELECT COUNT(*) FROM op_breadth_labels").fetchone()[0] == 0


def test_one_row_per_launch_and_first_write_wins():
    conn = _conn()
    assert _write(conn, 12) == CLASS_PASS
    # A later decision on the same launch must not restate the frozen class.
    assert _write(conn, 2) == CLASS_CONTROL
    rows = conn.execute("SELECT breadth_class,eligible_buyer_count FROM op_breadth_labels").fetchall()
    assert len(rows) == 1
    assert rows[0]["breadth_class"] == CLASS_PASS
    assert rows[0]["eligible_buyer_count"] == 12


def test_label_row_is_immutable():
    conn = _conn()
    _write(conn, 10)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE op_breadth_labels SET breadth_class='control'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM op_breadth_labels")


def test_launch_mode_is_stored_because_results_never_pool_across_modes():
    conn = _conn()
    _write(conn, 9, launch_id="L-cash", launch_mode="cashback")
    _write(conn, 9, launch_id="L-std", launch_mode="standard")
    modes = {
        row["launch_id"]: row["launch_mode"]
        for row in conn.execute("SELECT launch_id,launch_mode FROM op_breadth_labels")
    }
    assert modes == {"L-cash": "cashback", "L-std": "standard"}


def test_entity_count_is_a_shadow_field_and_stays_null():
    """Point-in-time entity edges do not exist; a whole-corpus map would look ahead."""
    conn = _conn()
    _write(conn, 20)
    assert conn.execute("SELECT entity_buyer_count FROM op_breadth_labels").fetchone()[0] is None


def test_the_labelled_venue_is_frozen():
    assert LABELLED_VENUE_ID == "pumpfun_solana"


def test_matching_covariates_are_frozen_with_the_label():
    conn = _conn()
    _write(conn, 11, decision_age_ms=7_500, progress_bps=2_750)
    row = conn.execute(
        "SELECT decision_age_ms,progress_bps,modeled_round_trip_cost_bps FROM op_breadth_labels"
    ).fetchone()
    assert (row["decision_age_ms"], row["progress_bps"]) == (7_500, 2_750)
    assert row["modeled_round_trip_cost_bps"] == -191


def test_label_is_not_an_arm_and_registers_no_rule_version():
    """The label must not take an arm slot.

    `pump-creator-prior-v1` took the control slot. The breadth label must stay outside
    the arm registry.
    """
    from sniper.research.memecoin_opportunity import directional

    source = (directional.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        assert RULE_VERSION not in handle.read()


def test_breadth_label_table_is_outside_the_retention_delete_set():
    from sniper.research.memecoin_opportunity import retention

    with open(retention.__file__, encoding="utf-8") as handle:
        body = handle.read()
    assert "op_breadth_labels" not in body


def test_the_control_receipt_writes_a_label_and_the_arms_list_is_unchanged(tmp_path):
    """End-to-end: a real route-control-v2 receipt must leave exactly one label row.

    Reuses the engine suite's own fixture so this exercises the production write path,
    not a hand-built stand-in.
    """
    from test_memecoin_opportunity_engine import (
        test_qualifying_quote_completion_records_control_and_ignition_once as scenario,
    )
    from sniper.research.memecoin_opportunity.store import connect

    scenario(tmp_path)

    conn = connect(tmp_path / "op.db")
    rules = [r[0] for r in conn.execute("SELECT rule_version FROM op_forward_alerts")]
    assert any(r.startswith("route-control-") for r in rules)
    # the label never appears as an arm
    assert RULE_VERSION not in rules

    labels = conn.execute(
        "SELECT launch_id,rule_version,breadth_class,eligible_buyer_count,launch_mode "
        "FROM op_breadth_labels"
    ).fetchall()
    assert len(labels) == 1, "exactly one label per launch"
    assert labels[0]["rule_version"] == RULE_VERSION
    assert labels[0]["breadth_class"] in {CLASS_PASS, CLASS_MIDDLE, CLASS_CONTROL}
    conn.close()


def test_a_confirmation_launch_with_no_control_arm_still_gets_a_label(tmp_path):
    """The defect this ordering fixes.

    The engine builds no control arm when a confirmation arm fires, and a confirmation
    arm needs a high buyer count. A label bound to the control receipt therefore dropped
    exactly the `pass` rows the gate exists to count.
    """
    from test_memecoin_opportunity_engine import (
        test_v6_alert_appends_one_paper_fill_request_when_enabled as scenario,
    )
    from sniper.research.memecoin_opportunity.store import connect

    scenario(tmp_path)

    conn = connect(tmp_path / "op.db")
    rules = [r[0] for r in conn.execute("SELECT rule_version FROM op_forward_alerts")]
    assert rules, "the fixture must emit at least one alert"
    # no control arm was created on this launch ...
    assert not any(r.startswith("route-control-") for r in rules)
    # ... and the label still exists
    labels = conn.execute(
        "SELECT breadth_class,eligible_buyer_count FROM op_breadth_labels"
    ).fetchall()
    assert len(labels) == 1, f"expected one label, got {len(labels)} (rules={rules})"
    assert labels[0]["breadth_class"] == CLASS_PASS
    assert labels[0]["eligible_buyer_count"] >= 9
    conn.close()


def test_a_version_9_insights_database_migrates_and_can_be_read_only(tmp_path):
    """A read-only open cannot create a table, so the version must advance with it."""
    from sniper.research.memecoin_opportunity.insights import (
        INSIGHT_SCHEMA_VERSION,
        connect_existing_insights,
        connect_insights,
    )

    path = tmp_path / "insights.db"
    old = connect_insights(path)
    old.execute("UPDATE op_insight_schema_version SET version=9")
    old.execute("DROP TABLE IF EXISTS op_insight_breadth_labels")
    old.close()

    migrated = connect_insights(path)
    assert INSIGHT_SCHEMA_VERSION == 10
    assert migrated.execute(
        "SELECT version FROM op_insight_schema_version"
    ).fetchone()[0] == 10
    migrated.close()

    # the read-only path is the one that used to raise
    readonly = connect_existing_insights(path)
    assert readonly.execute(
        "SELECT COUNT(*) FROM op_insight_breadth_labels"
    ).fetchone()[0] == 0
    readonly.close()
