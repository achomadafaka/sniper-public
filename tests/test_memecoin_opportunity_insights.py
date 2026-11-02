import json
import sqlite3
import zlib

from sniper.research.memecoin_opportunity.insights import (
    _arm_comparison,
    _confirmation_microscope,
    _creator_prior,
    _decision_latency,
    _launch_mode_cost,
    _confirmation_microscope_v3,
    _headline_alert_targets,
    connect_insights,
    is_forward_measurement,
    is_scored_outcome,
    project_matured_insights,
    prune_insight_samples,
    read_live_insights,
)
from sniper.research.memecoin_opportunity.diagnostics import (
    HORIZON_MS, TARGET_BPS, ExecutablePoint)


def _v3_alert(
    index,
    *,
    return_bps=200,
    passes=True,
    mode=None,
    status="SCORED",
    creator=None,
):
    launch_mode = mode or ("cashback" if index % 2 == 0 else "standard")
    return {
        "alert_id": f"v3-{index}",
        "launch_id": f"launch-{index}",
        "rule_version": "pump-confirmation-truth-v3",
        "score_status": status,
        "outcome_kind": "PROFIT_FIRST" if return_bps >= 1_200 else "TIMEOUT",
        "terminal_return_bps": return_bps if status == "SCORED" else None,
        "creator_id": creator or f"creator-{index}",
        "features": {
            "confirmation_independent_pass": passes,
            "confirmation_clean_pass": passes,
            "confirmation_low_friction_pass": passes,
            "launch_mode": launch_mode,
        },
        "targets": [],
    }


def _v4_alert(index, *, control=False, return_bps=200, mode=None, creator=None):
    launch_mode = mode or ("cashback" if index % 2 == 0 else "standard")
    return {
        "alert_id": f"{'control' if control else 'truth'}-{index}",
        "launch_id": f"{'control' if control else 'truth'}-launch-{index}",
        "venue_id": "pumpfun_solana",
        "rule_version": "route-control-v2" if control else "pump-confirmation-truth-v6",
        "evaluator_version": "alert-outcome-v4",
        "score_status": "SCORED",
        "outcome_kind": "TIMEOUT",
        "terminal_return_bps": return_bps,
        "creator_id": creator or f"creator-{index}",
        "observed_at": f"2026-07-29T12:{index // 60:02d}:{index % 60:02d}Z",
        "features": {
            "launch_mode": launch_mode,
            "quote_mint": "So11111111111111111111111111111111111111112",
            "decision_age_ms": 20_000,
            "progress_bps": 4_000,
            "modeled_round_trip_cost_bps": -250,
        },
        "targets": [],
    }


def _realistic_gate_hot(outcomes):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE op_paper_fill_requests(
          request_id TEXT PRIMARY KEY, alert_id TEXT, rule_version TEXT);
        CREATE TABLE op_paper_outcomes(
          request_id TEXT, lifecycle_version TEXT, status TEXT,
          result_code TEXT, return_bps INTEGER);
        """
    )
    for outcome in outcomes:
        _insert_realistic_gate_outcome(conn, *outcome)
    return conn


def _insert_realistic_gate_outcome(
    conn, alert_id, rule_version, return_bps, status, result_code
):
    request_id = f"request-{alert_id}"
    conn.execute(
        "INSERT INTO op_paper_fill_requests VALUES(?,?,?)",
        (request_id, alert_id, rule_version),
    )
    conn.execute(
        "INSERT INTO op_paper_outcomes VALUES(?,?,?,?,?)",
        (
            request_id,
            "radar-realistic-v2",
            status,
            result_code,
            return_bps,
        ),
    )


def test_realistic_gate_uses_only_valid_v2_outcomes():
    from sniper.research.memecoin_opportunity import insights as ins

    truth = _v4_alert(0, return_bps=900, mode="standard")
    control = _v4_alert(0, control=True, return_bps=400, mode="standard")
    hot = _realistic_gate_hot([
        (truth["alert_id"], truth["rule_version"], -1_300, "valid", "stop_first"),
        (control["alert_id"], control["rule_version"], 200, "valid", "time_exit"),
    ])

    gate_alerts = ins._realistic_gate_evidence(hot, [truth, control])
    result = ins._confirmation_microscope([truth, control], gate_alerts=gate_alerts)

    assert result["outcome_source"] == "radar-realistic-v2"
    assert result["forward_primary"]["mean_return_bps"] == -1_300
    assert result["control"]["mean_return_bps"] == 200
    assert result["matched_control"]["mean_arm_minus_control_bps"] == -1_500


def test_realistic_gate_does_not_use_legacy_labels_when_v2_is_missing():
    from sniper.research.memecoin_opportunity import insights as ins

    alerts = [_v4_alert(index, return_bps=300) for index in range(26)] + [
        _v4_alert(index, control=True, return_bps=100) for index in range(26)
    ]

    gate_alerts = ins._realistic_gate_evidence(_realistic_gate_hot([]), alerts)
    result = ins._confirmation_microscope(alerts, gate_alerts=gate_alerts)

    assert result["forward_primary"]["valid"] == 0
    assert result["control"]["valid"] == 0
    assert result["matched_control"]["pairs"] == 0


def test_realistic_gate_excludes_an_alert_with_two_valid_v2_outcomes():
    from sniper.research.memecoin_opportunity import insights as ins

    truth = _v4_alert(0, return_bps=900, mode="standard")
    hot = _realistic_gate_hot([
        (truth["alert_id"], truth["rule_version"], 300, "valid", "time_exit"),
    ])
    hot.execute(
        "INSERT INTO op_paper_fill_requests VALUES(?,?,?)",
        ("request-duplicate", truth["alert_id"], truth["rule_version"]),
    )
    hot.execute(
        "INSERT INTO op_paper_outcomes VALUES(?,?,?,?,?)",
        ("request-duplicate", "radar-realistic-v2", "valid", "time_exit", -500),
    )

    assert ins._realistic_gate_evidence(hot, [truth]) == []


def test_live_report_uses_realistic_gate_evidence(monkeypatch):
    from sniper.research.memecoin_opportunity import insights as ins

    truth = _v4_alert(0, return_bps=900, mode="standard")
    control = _v4_alert(0, control=True, return_bps=400, mode="standard")
    hot = _hot()
    _insert_realistic_gate_outcome(
        hot, truth["alert_id"], truth["rule_version"], -1_300, "valid", "stop_first"
    )
    _insert_realistic_gate_outcome(
        hot, control["alert_id"], control["rule_version"], 200, "valid", "time_exit"
    )
    monkeypatch.setattr(
        ins, "_merged_alert_evidence", lambda current, historical: [truth, control]
    )

    gate = ins.read_live_insights(hot)["confirmation_microscope"]

    assert gate["forward_primary"]["mean_return_bps"] == -1_300
    assert gate["matched_control"]["mean_arm_minus_control_bps"] == -1_500


def test_confirmation_v4_requires_disjoint_matched_ev_not_resolved_hit_rate():
    alerts = [
        _v4_alert(index, return_bps=300)
        for index in range(10)
    ] + [
        _v4_alert(index, control=True, return_bps=100)
        for index in range(10)
    ]

    result = _confirmation_microscope(alerts)

    assert result["status"] == "INSUFFICIENT_MATCHED_CONTROL"
    assert result["best_arm"] is None
    assert result["forward_primary"]["mean_return_bps"] == 300
    assert result["forward_primary"]["time_exits"] == 10
    assert result["matched_control"]["pairs"] == 10
    assert result["matched_control"]["mean_arm_minus_control_bps"] == 200


def test_confirmation_v4_promotes_only_when_creator_clustered_ev_interval_is_positive():
    alerts = [
        _v4_alert(index, return_bps=200)
        for index in range(26)
    ] + [
        _v4_alert(index, control=True, return_bps=100)
        for index in range(26)
    ]

    result = _confirmation_microscope(alerts)

    assert result["status"] == "PROMISING_DIRECTION"
    # The arm identity must track the rows actually selected, not the function name.
    assert result["best_arm"] == "truth_v6"
    assert result["matched_control"]["pairs"] == 26
    assert result["matched_control"]["mean_arm_minus_control_bps"] == 100
    assert result["matched_control"]["creator_cluster_ci95_bps"] == [100, 100]
    assert result["blocker_codes"] == []


def test_confirmation_v4_matching_is_one_to_one_and_reports_unmatched_rows():
    truth = [_v4_alert(0), _v4_alert(1)]
    one_control = [_v4_alert(0, control=True, return_bps=100)]

    result = _confirmation_microscope(truth + one_control)

    assert result["matched_control"]["pairs"] == 1
    assert result["matched_control"]["unmatched_truth"] == 1
    assert result["matched_control"]["unmatched_control"] == 0


def test_confirmation_v4_reports_a_positive_mode_specific_direction_instead_of_generic_block():
    # Machinery test for the mode splitter. v6 fires on `standard` only, so its own
    # forward population has one mode; the splitter still applies to the control arm and
    # to the pre-v6 cohorts. The fixture is synthetic on purpose.
    alerts = [
        _v4_alert(index, mode="cashback", return_bps=300)
        for index in range(30)
    ] + [
        _v4_alert(index + 30, mode="standard", return_bps=50)
        for index in range(5)
    ] + [
        _v4_alert(index, control=True, mode="cashback", return_bps=100)
        for index in range(30)
    ] + [
        _v4_alert(index + 30, control=True, mode="standard", return_bps=100)
        for index in range(5)
    ]

    result = _confirmation_microscope(alerts)

    assert result["status"] == "MODE_SPECIFIC_DIRECTION"
    assert result["best_arm"] == "truth_v6:cashback"
    cashback = next(row for row in result["modes"] if row["mode"] == "cashback")
    assert cashback["pairs"] == 30
    assert cashback["creator_cluster_ci95_bps"][0] > 0


def test_confirmation_v3_uses_pass_vs_excluded_and_reaches_promising_at_exact_gates():
    alerts = [
        _v3_alert(index, passes=index < 25, return_bps=300 if index < 25 else 100)
        for index in range(50)
    ]

    result = _confirmation_microscope_v3(alerts)

    assert result["status"] == "PROMISING_DIRECTION"
    assert result["checkpoint"] == 50
    assert result["forward_primary"]["valid"] == 50
    independent = next(row for row in result["conditions"] if row["key"] == "independent")
    assert independent["pass"]["valid"] == 25
    assert independent["excluded"]["valid"] == 25
    assert independent["mean_advantage_bps"] == 200
    assert independent["median_advantage_bps"] == 200
    assert independent["incremental_samples"] == 25
    assert result["blocker_codes"] == []


def test_confirmation_v3_collects_until_condition_and_mode_cohorts_are_real():
    separation = _confirmation_microscope_v3([
        _v3_alert(index, passes=True) for index in range(25)
    ])
    assert separation["status"] == "SEPARATION_COLLECTING"

    mode = _confirmation_microscope_v3([
        _v3_alert(
            index,
            passes=index < 25,
            mode="cashback" if index < 40 else "standard",
            return_bps=300 if index < 25 else 100,
        )
        for index in range(50)
    ])
    assert mode["status"] == "MODE_REPLICATION_COLLECTING"
    assert {row["mode"]: row["valid"] for row in mode["modes"]} == {
        "cashback": 40,
        "standard": 10,
    }


def test_confirmation_v3_blocks_negative_separation_and_creator_concentration():
    negative_separation = _confirmation_microscope_v3([
        _v3_alert(index, passes=index < 25, return_bps=100 if index < 25 else 300)
        for index in range(50)
    ])
    assert negative_separation["status"] == "EVIDENCE_BLOCKED"
    assert "INDEPENDENT_NOT_IMPROVED" in negative_separation["blocker_codes"]

    concentrated = _confirmation_microscope_v3([
        _v3_alert(
            index,
            passes=index < 25,
            return_bps=300 if index < 25 else 100,
            creator="dominant" if index < 30 else f"creator-{index}",
        )
        for index in range(50)
    ])
    assert concentrated["status"] == "EVIDENCE_BLOCKED"
    assert "CREATOR_CONCENTRATION_OVER_50" in concentrated["blocker_codes"]


def test_confirmation_v3_excludes_invalid_and_keeps_v2_pilot_out_of_verdict():
    alerts = [
        _v3_alert(index, passes=index < 25, return_bps=300 if index < 25 else 100)
        for index in range(50)
    ]
    alerts.append(_v3_alert(50, status="DATA_GAP_INVALID", return_bps=99_999))
    alerts.append({
        **_v3_alert(51, return_bps=-9_999),
        "alert_id": "v2-loss",
        "rule_version": "pump-confirmation-truth-v2",
    })

    result = _confirmation_microscope_v3(alerts)

    assert result["status"] == "PROMISING_DIRECTION"
    assert result["forward_primary"]["valid"] == 50
    assert result["forward_primary"]["invalid"] == 1
    assert result["forward_primary"]["total"] == 51
    truth_v2 = next(row for row in result["historical_pilot"]["arms"] if row["key"] == "truth")
    assert truth_v2["sum_return_bps"] == -9_999


def test_confirmation_v3_delayed_pair_reports_mean_and_median():
    alerts = [
        _v3_alert(0, return_bps=500),
        _v3_alert(1, return_bps=300),
        {
            **_v3_alert(0, return_bps=100),
            "alert_id": "delayed-0",
            "rule_version": "pump-confirmation-delayed-v2",
        },
        {
            **_v3_alert(1, return_bps=400),
            "alert_id": "delayed-1",
            "rule_version": "pump-confirmation-delayed-v2",
        },
    ]

    result = _confirmation_microscope_v3(alerts)

    assert result["paired_delay"] == {
        "pairs": 2,
        "immediate_better": 1,
        "delayed_better": 1,
        "ties": 0,
        "mean_immediate_minus_delayed_bps": 150,
        "median_immediate_minus_delayed_bps": 150,
        "checkpoint_status": "COLLECTING",
    }


def test_arm_comparison_accounts_for_retained_outcomes_and_invalid_alerts():
    alerts = [
        {
            "rule_version": "pump-confirmation-v1",
            "score_status": "SCORED",
            "terminal_return_bps": 1_500,
            "targets": [{"target_bps": 1_200, "horizon_ms": 60_000, "result": "hit"}],
        },
        {
            "rule_version": "pump-confirmation-v1",
            "score_status": "SCORED",
            "terminal_return_bps": 200,
            "targets": [{"target_bps": 1_200, "horizon_ms": 60_000, "result": "timeout"}],
        },
        {
            "rule_version": "pump-confirmation-v1",
            "score_status": "DATA_GAP_INVALID",
            "terminal_return_bps": 9_999,
            "targets": [{"target_bps": 1_200, "horizon_ms": 60_000, "result": "hit"}],
        },
        {
            "rule_version": "pump-confirmation-v1",
            "score_status": "PENDING",
            "terminal_return_bps": None,
            "targets": [],
        },
    ]

    cell = _arm_comparison(alerts)["pump-confirmation-v1"]["60000"]

    assert cell == {
        "hits": 1,
        "losses": 0,
        "timeouts": 1,
        "other": 0,
        "invalid": 1,
        "pending": 1,
        "coverage_missing": 0,
        "scored": 2,
        "samples": 2,
        "hit_rate_bps": 5_000,
        "final_return_samples": 2,
        "avg_final_return_bps": 850,
    }


def test_arm_comparison_registers_forward_v3_receipts():
    comparison = _arm_comparison([])

    assert "pump-confirmation-truth-v3" in comparison
    assert "pump-confirmation-delayed-v2" in comparison


def test_alert_evidence_only_embeds_headline_target_cells():
    targets = _headline_alert_targets([
        ExecutablePoint(0, "executable", -500, True, False),
        ExecutablePoint(60_000, "executable", 1_300, True, False),
        ExecutablePoint(300_000, "executable", 1_500, True, False),
    ])

    assert {(row["target_bps"], row["horizon_ms"]) for row in targets} == {
        (1_200, 60_000),
        (1_200, 300_000),
    }


def test_censored_and_reorg_outcomes_are_never_scored():
    assert is_scored_outcome("PROFIT_FIRST") is True
    assert is_scored_outcome("timeout") is True
    assert is_scored_outcome("CENSORED_DATA_GAP") is False
    assert is_scored_outcome("chain_reorg") is False
    assert is_scored_outcome(None) is False


def test_censored_alert_is_invalid_across_summary_microscope_and_patterns():
    hot = _hot()
    hot.execute(
        "UPDATE op_competing_outcomes SET outcome_kind='CENSORED_DATA_GAP',"
        "terminal_net_return_bps=NULL,last_executable_return_bps=NULL"
    )

    result = read_live_insights(hot)

    alert = result["alert_evidence"][0]
    assert alert["score_status"] == "DATA_GAP_INVALID"
    assert result["alert_summary"]["scored"] == 0
    ignition = next(
        row for row in result["confirmation_microscope"]["arms"]
        if row["key"] == "truth"
    )
    assert ignition["completed"] == 0
    assert result["patterns"] == []


def _hot() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE op_launches(launch_id TEXT PRIMARY KEY, network_id TEXT, venue_id TEXT,
          token_id TEXT, creator_id TEXT, launched_at TEXT, row_fingerprint TEXT);
        CREATE TABLE op_state_snapshots(snapshot_id TEXT PRIMARY KEY, launch_id TEXT,
          observed_at TEXT, state_json TEXT, row_fingerprint TEXT, observed_event_id TEXT);
        CREATE TABLE op_predictions(prediction_id TEXT PRIMARY KEY, snapshot_id TEXT,
          zone TEXT, would_fire INTEGER, rule_version TEXT, distance_bps INTEGER,
          dominant_risk TEXT, observed_at TEXT, row_fingerprint TEXT);
        CREATE TABLE op_landmark_states(landmark_id TEXT PRIMARY KEY, launch_id TEXT,
          observed_at TEXT, observed_event_id TEXT);
        CREATE TABLE op_counterfactual_entries(entry_id TEXT PRIMARY KEY, landmark_id TEXT,
          entry_observed_at TEXT);
        CREATE TABLE op_competing_outcomes(outcome_id TEXT PRIMARY KEY, entry_id TEXT,
          outcome_kind TEXT, elapsed_ms INTEGER, terminal_net_return_bps INTEGER,
          last_executable_return_bps INTEGER, evaluator_version TEXT, inserted_at TEXT);
        CREATE TABLE op_executable_observations(observation_id TEXT PRIMARY KEY,
          entry_id TEXT, elapsed_ms INTEGER, exit_kind TEXT, net_return_bps INTEGER,
          source_healthy INTEGER, migration INTEGER);
        CREATE TABLE op_forward_alerts(alert_id TEXT PRIMARY KEY, launch_id TEXT,
          network_id TEXT, venue_id TEXT, token_id TEXT, snapshot_id TEXT,
          observed_at TEXT, clip_usd_micros INTEGER, rule_version TEXT,
          quote_provider TEXT, round_trip_return_bps INTEGER, truth_label TEXT);
        CREATE TABLE op_forward_alert_entries(alert_id TEXT PRIMARY KEY, entry_id TEXT,
          snapshot_id TEXT, quote_observed_at TEXT, quote_completed_at TEXT,
          quote_age_ms INTEGER, entry_effective_at TEXT, provider TEXT,
          evaluator_version TEXT);
        CREATE TABLE op_forward_alert_features(alert_id TEXT PRIMARY KEY,
          feature_version TEXT, feature_json TEXT);
        CREATE TABLE op_paper_fill_requests(
          request_id TEXT PRIMARY KEY, alert_id TEXT, rule_version TEXT);
        CREATE TABLE op_paper_outcomes(
          request_id TEXT, lifecycle_version TEXT, status TEXT,
          result_code TEXT, return_bps INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO op_launches VALUES(?,?,?,?,?,?,?)",
        ("l1", "solana", "pumpfun_solana", "mint", "creator", "2026-07-20T00:00:00Z", "lfp"),
    )
    for suffix, distance, fire, zone in (("a", 8000, 0, "OUTER"), ("b", 900, 1, "INNER")):
        conn.execute(
            "INSERT INTO op_state_snapshots VALUES(?,?,?,?,?,?)",
            (f"s{suffix}", "l1", f"2026-07-20T00:00:0{1 if suffix == 'a' else 2}Z", json.dumps({"progress_bps": 700, "effective_buyer_count": 12, "capital_acceleration_atomic_per_s2": 1500, "buyer_acceleration_per_s2_micros": 250000, "sell_pressure_bps": 500, "effective_buyer_hhi_change_bps": -800, "creator_sale_since_prior": False}), f"sfp{suffix}", f"ev-{suffix}"),
        )
        conn.execute(
            "INSERT INTO op_predictions VALUES(?,?,?,?,?,?,?,?,?)",
            (f"p{suffix}", f"s{suffix}", zone, fire, "pump-ignition-v1" if fire else "radar-forward-v3", distance, "jump" if fire else "timeout", f"2026-07-20T00:00:0{1 if suffix == 'a' else 2}Z", f"pfp{suffix}"),
        )
    conn.execute(
        "INSERT INTO op_landmark_states VALUES('lm','l1','2026-07-20T00:00:02Z','ev-b')"
    )
    conn.execute("INSERT INTO op_counterfactual_entries VALUES('e','lm','2026-07-20T00:00:02Z')")
    conn.execute("INSERT INTO op_competing_outcomes VALUES('o','e','PROFIT_FIRST',9000,1450,1450,'opportunity-outcome-v2','2026-07-20T00:01:00Z')")
    conn.execute("INSERT INTO op_executable_observations VALUES('x1','e',0,'executable',-200,1,0)")
    conn.execute("INSERT INTO op_executable_observations VALUES('x2','e',9000,'executable',1450,1,0)")
    conn.execute(
        "INSERT INTO op_forward_alerts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("a1", "l1", "solana", "pumpfun_solana", "mint", "sb",
         "2026-07-20T00:00:02Z", 25_000_000, "pump-ignition-v1", "fixture", -200,
         "EXPLORATORY PAPER ALERT"),
    )
    conn.execute(
        "INSERT INTO op_forward_alert_entries VALUES(?,?,?,?,?,?,?,?,?)",
        ("a1", "e", "sb", "2026-07-20T00:00:02Z",
         "2026-07-20T00:00:02.100000Z", 25,
         "2026-07-20T00:00:02.500000Z", "fixture", "alert-outcome-v3"),
    )
    conn.execute(
        "INSERT INTO op_forward_alert_features VALUES(?,?,?)",
        ("a1", "confirmation-feature-v1", json.dumps({"independent_buyer_count": 7})),
    )
    return conn


def test_projection_is_compact_and_idempotent(tmp_path):
    hot = _hot()
    insights = connect_insights(tmp_path / "insights.db")
    assert insights.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    assert project_matured_insights(hot, insights, now_iso="2026-07-26T00:00:00Z") == 1
    assert project_matured_insights(hot, insights, now_iso="2026-07-26T00:00:00Z") == 0

    samples = insights.execute(
        "SELECT sample_role,would_fire,feature_blob FROM op_insight_samples ORDER BY sample_role"
    ).fetchall()
    assert [row[0] for row in samples] == ["control", "fired"]
    assert json.loads(zlib.decompress(samples[1][2]))["progress_bps"] == 700
    assert insights.execute("SELECT COUNT(*) FROM op_insight_daily").fetchone()[0] == 2
    assert insights.execute("SELECT SUM(jump_count) FROM op_insight_daily").fetchone()[0] == 1
    control = insights.execute(
        "SELECT outcome_kind,terminal_net_return_bps FROM op_insight_samples "
        "WHERE sample_role='control'"
    ).fetchone()
    assert tuple(control) == (None, None)
    assert insights.execute("SELECT COUNT(*) FROM op_projected_launches").fetchone()[0] == 1
    assert insights.execute("SELECT COUNT(*) FROM op_insight_targets").fetchone()[0] == 0
    assert insights.execute(
        "SELECT SUM(sample_count) FROM op_insight_target_daily"
    ).fetchone()[0] == len(TARGET_BPS) * len(HORIZON_MS)
    alert = insights.execute(
        "SELECT alert_id,entry_id,rule_version,score_status,outcome_kind,terminal_return_bps,mfe_bps,mae_bps "
        "FROM op_insight_alerts"
    ).fetchone()
    assert tuple(alert) == (
        "a1", "e", "pump-ignition-v1", "SCORED", "PROFIT_FIRST", 1450, 1450, -200
    )
    retained_features = insights.execute(
        "SELECT feature_version,feature_json FROM op_insight_alerts WHERE alert_id='a1'"
    ).fetchone()
    assert retained_features[0] == "confirmation-feature-v1"
    assert json.loads(retained_features[1])["independent_buyer_count"] == 7
    # The whole 5 x 6 exit ladder is persisted; the headline cell is one of thirty.
    assert insights.execute("SELECT COUNT(*) FROM op_insight_alert_targets").fetchone()[0] \
        == len(TARGET_BPS) * len(HORIZON_MS)
    assert insights.execute(
        "SELECT COUNT(*) FROM op_insight_alert_targets WHERE target_bps=1200 AND horizon_ms=60000"
    ).fetchone()[0] == 1


def test_projection_labels_non_firing_sample_from_exact_existing_entry(tmp_path):
    hot = _hot()
    hot.execute(
        "INSERT INTO op_landmark_states "
        "VALUES('lm-control','l1','2026-07-20T00:00:01Z','ev-a')"
    )
    hot.execute(
        "INSERT INTO op_counterfactual_entries VALUES('e-control','lm-control','2026-07-20T00:00:01Z')"
    )
    hot.execute(
        "INSERT INTO op_competing_outcomes VALUES(?,?,?,?,?,?,?,?)",
        (
            "o-control", "e-control", "LOSS_FIRST", 4_000, -900, -900,
            "opportunity-outcome-v3", "2026-07-20T00:01:01Z",
        ),
    )
    hot.execute(
        "INSERT INTO op_executable_observations VALUES(?,?,?,?,?,?,?)",
        ("x-control", "e-control", 4_000, "executable", -900, 1, 0),
    )
    insights = connect_insights(tmp_path / "insights.db")

    assert project_matured_insights(
        hot, insights, now_iso="2026-07-26T00:00:00Z"
    ) == 1

    control = insights.execute(
        "SELECT outcome_kind,terminal_net_return_bps,label_version "
        "FROM op_insight_samples WHERE sample_role='control'"
    ).fetchone()
    assert tuple(control) == ("LOSS_FIRST", -900, "selected-outcome-v2")


def test_quarantine_projection_marks_incomplete_alert_invalid_without_training_sample(tmp_path):
    hot = _hot()
    hot.execute("DELETE FROM op_competing_outcomes")
    insights = connect_insights(tmp_path / "insights.db")

    assert project_matured_insights(
        hot,
        insights,
        now_iso="2026-07-26T00:00:00Z",
        allow_incomplete=True,
    ) == 1

    assert insights.execute(
        "SELECT score_status FROM op_insight_alerts WHERE alert_id='a1'"
    ).fetchone()[0] == "DATA_GAP_INVALID"
    assert insights.execute("SELECT COUNT(*) FROM op_insight_samples").fetchone()[0] == 0
    assert insights.execute("SELECT COUNT(*) FROM op_projected_launches").fetchone()[0] == 1


def test_insight_schema_v3_adds_forward_identity_features_and_target_version(tmp_path):
    path = tmp_path / "insights.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE op_insight_schema_version(version INTEGER NOT NULL)")
    legacy.execute("INSERT INTO op_insight_schema_version VALUES(3)")
    legacy.execute(
        "CREATE TABLE op_insight_alerts(alert_id TEXT PRIMARY KEY, observed_at TEXT)"
    )
    legacy.commit()
    legacy.close()

    migrated = connect_insights(path)

    assert migrated.execute("SELECT version FROM op_insight_schema_version").fetchone()[0] == 10
    columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(op_insight_alerts)")
    }
    assert {"rule_version", "feature_version", "feature_json", "creator_id"} <= columns
    for table in ("op_insight_targets", "op_insight_alert_targets"):
        assert "evaluator_version" in {
            row[1] for row in migrated.execute(f"PRAGMA table_info({table})")
        }
    sample_columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(op_insight_samples)")
    }
    assert "label_version" in sample_columns


def test_live_insights_are_launch_clustered_and_report_non_degenerate_patterns():
    hot = _hot()

    result = read_live_insights(hot)

    assert result["status"] == "LEARNING — EDGE NOT YET ESTABLISHED"
    assert result["receipt"]["launches"] == 1
    assert result["receipt"]["matured_launches"] == 1
    assert result["receipt"]["return_span_bps"] == 1_650
    assert result["outcomes"]["PROFIT_FIRST"] == 1
    # venue rows are derived from the authority registry, so assert by identity rather
    # than by position: the order is not a contract.
    venue_ids = {row["venue_id"] for row in result["venues"]}
    assert "pumpfun_solana" in venue_ids
    assert not any("bags" in vid for vid in venue_ids)
    assert result["patterns"] == []
    assert result["pattern_status"] == "BLOCKED_UNTIL_MATCHED_CONTROLS"
    ten_second = next(
        row for row in result["target_ladder"]
        if row["venue_id"] == "pumpfun_solana"
        and row["target_bps"] == 1_200 and row["horizon_ms"] == 10_000
    )
    assert ten_second["launch_samples"] == 1
    assert ten_second["hit_rate_bps"] == 10_000
    assert result["arm_comparison_historical_pilot"]["arms"]["pump-ignition-v1"]["60000"] == {
        "hits": 1,
        "losses": 0,
        "timeouts": 0,
        "other": 0,
        "invalid": 0,
        "pending": 0,
        "coverage_missing": 0,
        "scored": 1,
        "samples": 1,
        "hit_rate_bps": 10_000,
        "final_return_samples": 1,
        "avg_final_return_bps": 1_450,
    }
    assert result["near_misses"] == []


def test_live_insights_block_pattern_claims_when_returns_are_fee_only():
    hot = _hot()
    hot.execute("DELETE FROM op_executable_observations")
    hot.executemany(
        "INSERT INTO op_executable_observations VALUES(?,?,?,?,?,?,?)",
        [
            (f"flat-{index}", "e", index * 10, "executable", -200 + index % 2, 1, 0)
            for index in range(20)
        ],
    )

    result = read_live_insights(hot)

    assert result["status"] == "DEGENERATE — NO PRICE INFORMATION"
    assert result["degenerate"] is True
    assert result["receipt"]["return_span_bps"] == 1
    assert result["patterns"] == []


def test_alert_results_use_the_exact_linked_entry_not_latest_launch_outcome():
    hot = _hot()
    hot.execute(
        "INSERT INTO op_landmark_states "
        "VALUES('lm2','l1','2026-07-20T00:00:03Z','ev-c')"
    )
    hot.execute("INSERT INTO op_counterfactual_entries VALUES('e2','lm2','2026-07-20T00:00:03Z')")
    hot.execute(
        "INSERT INTO op_competing_outcomes VALUES(?,?,?,?,?,?,?,?)",
        ("o2", "e2", "LOSS_FIRST", 5000, -900, -900,
         "opportunity-outcome-v2", "2026-07-20T00:02:00Z"),
    )
    hot.execute("INSERT INTO op_executable_observations VALUES('y1','e2',0,'executable',-200,1,0)")
    hot.execute("INSERT INTO op_executable_observations VALUES('y2','e2',5000,'executable',-900,1,0)")

    result = read_live_insights(hot)

    alert = result["alert_evidence"][0]
    assert alert["alert_id"] == "a1"
    assert alert["score_status"] == "SCORED"
    assert alert["outcome_kind"] == "PROFIT_FIRST"
    assert alert["terminal_return_bps"] == 1450
    assert alert["mfe_bps"] == 1450
    assert alert["mae_bps"] == -200
    assert alert["time_to_best_ms"] == 9000
    assert alert["quote_drag_bps"] == -200
    assert result["alert_summary"] == {
        "alerts": 1,
        "scored": 1,
        "pending": 0,
        "unscored_legacy": 0,
        "profit_first": 1,
        "loss_first": 0,
        "other": 0,
    }


def test_timeout_alert_exposes_late_quote_without_rewriting_terminal_result():
    hot = _hot()
    hot.execute(
        "UPDATE op_competing_outcomes SET outcome_kind='TIMEOUT',elapsed_ms=60000,"
        "terminal_net_return_bps=NULL,last_executable_return_bps=NULL WHERE entry_id='e'"
    )
    hot.execute(
        "INSERT INTO op_executable_observations VALUES(?,?,?,?,?,?,?)",
        ("x3", "e", 60_742, "executable", 18_496, 1, 0),
    )

    alert = read_live_insights(hot)["alert_evidence"][0]

    assert alert["outcome_kind"] == "TIMEOUT"
    assert alert["terminal_return_bps"] is None
    assert alert["latest_observed_return_bps"] == 18_496
    assert alert["latest_observed_elapsed_ms"] == 60_742


def test_unlinked_forward_alert_is_explicitly_unscored_legacy():
    hot = _hot()
    hot.execute("DELETE FROM op_forward_alert_entries")

    result = read_live_insights(hot)

    assert result["alert_evidence"][0]["score_status"] == "UNSCORED_LEGACY"
    assert result["alert_evidence"][0]["outcome_kind"] is None
    assert result["alert_summary"]["unscored_legacy"] == 1


def test_pruning_keeps_daily_aggregates_and_projection_receipt(tmp_path):
    hot = _hot()
    insights = connect_insights(tmp_path / "insights.db")
    project_matured_insights(hot, insights, now_iso="2026-07-26T00:00:00Z")

    # 5 sample/alert rows + every non-headline exit-ladder cell now persisted
    # (the ladder keeps two headline cells: 1200@60s and 1200@300s).
    assert prune_insight_samples(
        insights, cutoff_iso="2026-07-21T00:00:00Z", max_rows=100
    ) == 5 + len(TARGET_BPS) * len(HORIZON_MS) - 2
    assert insights.execute("SELECT COUNT(*) FROM op_insight_samples").fetchone()[0] == 0
    assert insights.execute("SELECT COUNT(*) FROM op_insight_alerts").fetchone()[0] == 0
    assert insights.execute("SELECT COUNT(*) FROM op_insight_alert_targets").fetchone()[0] == 0
    assert insights.execute("SELECT COUNT(*) FROM op_insight_daily").fetchone()[0] == 2
    assert insights.execute("SELECT COUNT(*) FROM op_projected_launches").fetchone()[0] == 1
    assert insights.execute("SELECT COUNT(*) FROM op_insight_targets").fetchone()[0] == 0
    assert insights.execute(
        "SELECT SUM(sample_count) FROM op_insight_target_daily"
    ).fetchone()[0] == len(TARGET_BPS) * len(HORIZON_MS)


def test_live_insights_can_include_retained_target_receipts(tmp_path):
    source = _hot()
    historical = connect_insights(tmp_path / "insights.db")
    project_matured_insights(source, historical, now_iso="2026-07-26T00:00:00Z")
    empty = _hot()
    for table in (
        "op_executable_observations", "op_competing_outcomes", "op_counterfactual_entries",
        "op_landmark_states", "op_predictions", "op_state_snapshots", "op_launches",
    ):
        empty.execute(f"DELETE FROM {table}")

    result = read_live_insights(empty, historical=historical)
    cell = next(
        row for row in result["target_ladder"]
        if row["venue_id"] == "pumpfun_solana"
        and row["target_bps"] == 1_200 and row["horizon_ms"] == 10_000
    )
    assert cell["launch_samples"] == 1
    assert cell["hit_rate_bps"] == 10_000
    assert result["alert_evidence"][0]["alert_id"] == "a1"
    assert result["alert_evidence"][0]["outcome_kind"] == "PROFIT_FIRST"


def test_arm_comparison_quarantines_pre_v4_measurement_from_forward_results():
    """A rule name that survived the v4 cutover must not pool the two regimes."""
    alerts = [
        {
            "rule_version": "pump-ignition-v1",
            "evaluator_version": "alert-outcome-v3",
            "score_status": "SCORED",
            "terminal_return_bps": 4_000,
            "targets": [{"target_bps": 1_200, "horizon_ms": 60_000, "result": "hit"}],
        },
        {
            "rule_version": "pump-ignition-v1",
            "evaluator_version": "alert-outcome-v4",
            "score_status": "SCORED",
            "terminal_return_bps": -200,
            "targets": [{"target_bps": 1_200, "horizon_ms": 60_000, "result": "timeout"}],
        },
    ]

    assert [is_forward_measurement(row) for row in alerts] == [False, True]
    forward = _arm_comparison(
        [row for row in alerts if is_forward_measurement(row)]
    )["pump-ignition-v1"]["60000"]
    historical = _arm_comparison(
        [row for row in alerts if not is_forward_measurement(row)]
    )["pump-ignition-v1"]["60000"]

    assert forward["samples"] == 1
    assert forward["avg_final_return_bps"] == -200
    assert historical["samples"] == 1
    assert historical["avg_final_return_bps"] == 4_000


def test_live_insights_split_forward_and_historical_measurement():
    hot = _hot()

    result = read_live_insights(hot)

    assert result["arm_comparison"]["pump-ignition-v1"]["60000"]["samples"] == 0
    historical = result["arm_comparison_historical_pilot"]
    assert historical["label"] == "Withdrawn pre-v4 measurement pilots"
    assert historical["arms"]["pump-ignition-v1"]["60000"]["samples"] == 1


def test_v4_matched_control_reports_band_sensitivity():
    """The matched advantage must be published against band width, not alone."""
    alerts = [_v4_alert(index, return_bps=1_000) for index in range(20)]
    alerts += [_v4_alert(index, control=True, return_bps=900) for index in range(10)]
    far = [_v4_alert(100 + index, control=True, return_bps=9_000) for index in range(10)]
    for row in far:
        row["features"]["decision_age_ms"] = 35_000
    alerts += far

    sensitivity = _confirmation_microscope(alerts)["matched_control"]["band_sensitivity"]

    assert [row["scale"] for row in sensitivity] == [1, 2, 4]
    assert sensitivity[0]["pairs"] == 10
    assert sensitivity[0]["mean_arm_minus_control_bps"] == 100
    assert sensitivity[1]["pairs"] == 20
    assert sensitivity[1]["mean_arm_minus_control_bps"] < 0


def test_alert_evidence_retains_the_full_exit_grid_not_only_the_headline_cell():
    """Exit-policy questions need every target; the headline cell answers none of them."""
    hot = _hot()

    result = read_live_insights(hot)

    cells = {
        (row["target_bps"], row["horizon_ms"])
        for row in result["alert_evidence"][0]["targets"]
    }
    assert (200, 5_000) in cells
    assert (1_200, 60_000) in cells
    assert len(cells) == len(TARGET_BPS) * len(HORIZON_MS)


def test_exit_realism_reports_how_far_losses_run_past_the_barrier():
    """A -800 stop that realizes -2600 is not a stop. Publish the overshoot."""
    hot = _hot()

    realism = read_live_insights(hot)["exit_realism"]

    assert realism["loss_barrier_bps"] == -800
    assert realism["profit_barrier_bps"] == 1_200
    assert "median_realized_loss_bps" in realism
    assert "loss_overshoot_bps" in realism
    assert realism["note"]


def test_decision_latency_separates_rule_wait_from_engineering_wait():
    """26.6% of decision age is the quote round trip; 73.4% is the rule's own bands.

    Conflating them invites 'make it faster' work on the share that is a strategy
    choice, and 'loosen the rule' work on the share that is plumbing.
    """
    alerts = [
        {
            "evaluator_version": "alert-outcome-v4",
            "features": {
                "decision_age_ms": 10_000,
                "chain_state_age_ms": 2_500,
                "quote_latency_ms": 2_400,
            },
        },
        {
            "evaluator_version": "alert-outcome-v3",  # historical, excluded
            "features": {
                "decision_age_ms": 99_000,
                "chain_state_age_ms": 99_000,
                "quote_latency_ms": 99_000,
            },
        },
    ]

    latency = _decision_latency(alerts)

    assert latency["samples"] == 1
    assert latency["median_decision_age_ms"] == 10_000
    assert latency["median_engineering_ms"] == 2_500
    assert latency["median_rule_wait_ms"] == 7_500
    assert latency["engineering_share_bps"] == 2_500
    assert latency["median_quote_latency_ms"] == 2_400


def _creator_alert(index, *, creator, return_bps, hour=12):
    return {
        "alert_id": f"c{index}",
        "launch_id": f"cl{index}",
        "creator_id": creator,
        "evaluator_version": "alert-outcome-v4",
        "score_status": "SCORED",
        "observed_at": f"2026-07-29T{hour:02d}:{index % 60:02d}:00Z",
        "entry_effective_at": f"2026-07-29T{hour:02d}:{index % 60:02d}:00Z",
        "outcome_elapsed_ms": 1_000,
        "terminal_return_bps": return_bps,
        "targets": [],
    }


def test_creator_prior_is_causal_and_market_adjusted():
    """Only a creator's EARLIER alerts may inform the current one, and the hour's
    own mean is removed so a good market window cannot manufacture the split."""
    alerts = []
    # good creator: first two alerts positive, third should land in the positive-prior arm
    for n, r in enumerate([1_000, 1_200, 900]):
        alerts.append(_creator_alert(n, creator="good", return_bps=r))
    # bad creator: first two negative, third in the negative-prior arm
    for n, r in enumerate([-900, -1_100, -800]):
        alerts.append(_creator_alert(10 + n, creator="bad", return_bps=r))

    prior = _creator_prior(alerts)

    assert prior["pairs"] == 4
    assert prior["distinct_creators"] == 2
    # the first alert of each creator has no prior and must be excluded
    assert prior["positive_prior"]["n"] == 2
    assert prior["negative_prior"]["n"] == 2
    assert prior["separation_bps"] > 0
    assert prior["market_adjusted_separation_bps"] is not None


def test_creator_prior_reports_nothing_without_repeat_creators():
    alerts = [_creator_alert(n, creator=f"c{n}", return_bps=500) for n in range(5)]

    assert _creator_prior(alerts)["pairs"] == 0


def test_launch_mode_cost_proves_the_fee_is_known_before_entry():
    """The cheap fee tier is deterministic and known at launch, so it is free to use."""
    alerts = [
        {
            "evaluator_version": "alert-outcome-v4",
            "score_status": "SCORED",
            "terminal_return_bps": -190,
            "features": {
                "launch_mode": "cashback",
                "effective_fee_bps": 95,
                "modeled_round_trip_cost_bps": -191,
            },
        },
        {
            "evaluator_version": "alert-outcome-v4",
            "score_status": "SCORED",
            "terminal_return_bps": -243,
            "features": {
                "launch_mode": "standard",
                "effective_fee_bps": 125,
                "modeled_round_trip_cost_bps": -250,
            },
        },
    ]

    modes = _launch_mode_cost(alerts)

    cashback = next(row for row in modes["modes"] if row["launch_mode"] == "cashback")
    standard = next(row for row in modes["modes"] if row["launch_mode"] == "standard")
    assert cashback["modeled_cost_bps"] == [-191]
    assert standard["modeled_cost_bps"] == [-250]
    assert cashback["cost_deterministic"] is True
    assert modes["cheapest_mode"] == "cashback"
    assert modes["cost_spread_bps"] == 59


def _leak_alert(index, *, creator, return_bps, observed, elapsed_ms=60_000):
    return {
        "alert_id": f"lk{index}",
        "launch_id": f"lkl{index}",
        "creator_id": creator,
        "evaluator_version": "alert-outcome-v4",
        "score_status": "SCORED",
        "observed_at": observed,
        "entry_effective_at": observed,
        "outcome_elapsed_ms": elapsed_ms,
        "terminal_return_bps": return_bps,
        "targets": [],
    }


def test_creator_prior_excludes_outcomes_that_had_not_resolved_yet():
    """An earlier ALERT is not an earlier RESULT.

    A prior alert's outcome resolves up to 60s after it fires. Counting it at a
    decision that happened before it resolved is target leakage -- it uses information
    that did not exist yet. On live data this inflated the measured separation from
    +392 bps (t=0.70) to +1,530 (t=4.11), which is the whole apparent finding.
    """
    alerts = [
        # resolves 12:01:00 -- known in time for the 12:02:00 decision
        _leak_alert(0, creator="c", return_bps=2_000, observed="2026-07-29T12:00:00Z"),
        # resolves 12:02:30 -- NOT known at the 12:02:00 decision
        _leak_alert(1, creator="c", return_bps=2_000, observed="2026-07-29T12:01:30Z"),
        _leak_alert(2, creator="c", return_bps=-500, observed="2026-07-29T12:02:00Z"),
    ]

    prior = _creator_prior(alerts)

    # alert 1 pairs off alert 0 (resolved 12:01:00). alert 2 pairs off alert 0 only --
    # alert 1's outcome lands at 12:02:30, after the 12:02:00 decision, so it is dropped.
    assert prior["pairs"] == 2
    assert prior["unresolved_priors_excluded"] == 1


def test_creator_prior_forms_no_pair_when_every_prior_is_still_open():
    alerts = [
        _leak_alert(0, creator="c", return_bps=2_000, observed="2026-07-29T12:00:00Z"),
        _leak_alert(1, creator="c", return_bps=900, observed="2026-07-29T12:00:10Z"),
    ]

    prior = _creator_prior(alerts)

    assert prior["pairs"] == 0
    assert prior["unresolved_priors_excluded"] == 1


def test_alert_creator_survives_sample_expiry(tmp_path):
    """The matched-control gate clusters by creator. Creator identity used to be readable
    only through op_insight_samples, so pruning samples silently destroyed the gate's own
    evidence. The alert row must carry it."""
    hot = _hot()
    insights = connect_insights(tmp_path / "insights.db")
    project_matured_insights(hot, insights, now_iso="2026-07-26T00:00:00Z")

    stored = insights.execute(
        "SELECT creator_id FROM op_insight_alerts ORDER BY alert_id"
    ).fetchall()
    assert stored, "expected projected alert rows"
    assert all(row[0] for row in stored), "alert rows must persist creator identity"

    # Expire every sample while keeping the alert evidence.
    pruned = prune_insight_samples(
        insights,
        cutoff_iso="2026-07-21T00:00:00Z",
        alert_cutoff_iso="2000-01-01T00:00:00Z",
        max_rows=100,
    )
    assert pruned > 0
    assert insights.execute("SELECT COUNT(*) FROM op_insight_samples").fetchone()[0] == 0
    assert insights.execute("SELECT COUNT(*) FROM op_insight_alerts").fetchone()[0] > 0

    # With samples gone the creator must still resolve, from the stored column.
    evidence = read_live_insights(_hot(), historical=insights)["alert_evidence"]
    historical = [row for row in evidence if row.get("creator_id")]
    assert historical, "creator identity must survive sample expiry"


def test_separate_alert_retention_keeps_gate_evidence(tmp_path):
    """Samples and alert evidence are different clocks; the shorter one must not take
    the gate rows with it."""
    hot = _hot()
    insights = connect_insights(tmp_path / "insights.db")
    project_matured_insights(hot, insights, now_iso="2026-07-26T00:00:00Z")
    before = insights.execute("SELECT COUNT(*) FROM op_insight_alerts").fetchone()[0]

    prune_insight_samples(
        insights,
        cutoff_iso="2026-07-27T00:00:00Z",
        alert_cutoff_iso="2026-05-01T00:00:00Z",
        max_rows=100,
    )
    assert insights.execute("SELECT COUNT(*) FROM op_insight_samples").fetchone()[0] == 0
    assert insights.execute(
        "SELECT COUNT(*) FROM op_insight_alerts"
    ).fetchone()[0] == before


def test_microscope_labels_the_arm_it_actually_selected():
    """Publishing "Confirmation v4" while selecting v5 rows is the identity confusion the
    measurement-integrity design exists to prevent."""
    from sniper.research.memecoin_opportunity.insights import (
        FORWARD_CONFIRMATION_LABEL,
        FORWARD_CONFIRMATION_RULE,
    )

    alerts = [
        {
            "rule_version": FORWARD_CONFIRMATION_RULE,
            "score_status": "SCORED",
            "terminal_return_bps": 900,
            "outcome_kind": "PROFIT_FIRST",
            "observed_at": "2026-07-31T00:00:00Z",
            "alert_id": "a1",
            "launch_id": "L1",
            "creator_id": "c1",
            "features": {},
            "targets": [],
        }
    ]
    result = _confirmation_microscope(alerts)
    assert result["forward_rule_version"] == FORWARD_CONFIRMATION_RULE
    assert result["best_label"] == FORWARD_CONFIRMATION_LABEL
    assert "v4" not in str(result["best_label"])


def test_gate_publishes_every_prerequisite_not_only_pair_count():
    """`/25` is the first gate, not the only one."""
    result = _confirmation_microscope([])
    codes = {item["code"] for item in result["gate_prerequisites"]}
    assert {"MATCHED_PAIRS", "MODE_CONCENTRATION", "CREATOR_CONCENTRATION"} <= codes
    # Nothing is satisfied on an empty cohort, and the count is reported, not just pairs.
    assert result["gate_prerequisites_unmet"]


def test_the_funding_gate_matches_at_the_promoted_scale_two_band():
    """Promotion recorded 2026-08-07 (operator approved). At scale=1 the decision-age
    band was the binding constraint (~2-3 pairs/day, 25-pair verdict 8-13 days out);
    scale=2 was already a PUBLISHED band-sensitivity cell, so promoting it is not a
    post-hoc widening. A truth/control pair 15s apart in decision age must pair under
    the gate (fails the old +/-10s, passes +/-20s); the arm predicate and outcome
    scoring are untouched."""
    truth = _v4_alert(0, return_bps=300, mode="standard")
    control = _v4_alert(0, control=True, return_bps=100, mode="standard")
    control["features"]["decision_age_ms"] = truth["features"]["decision_age_ms"] + 15_000

    result = _confirmation_microscope([truth, control])

    assert result["matched_control"]["pairs"] == 1
    assert result["matched_control"]["mean_arm_minus_control_bps"] == 200
    # scale=1 stays visible in the sensitivity table and still reports zero pairs here.
    narrow = next(r for r in result["matched_control"]["band_sensitivity"] if r["scale"] == 1)
    assert narrow["pairs"] == 0


def test_slow_capital_split_is_a_labelled_diagnostic_not_a_gate():
    """Anti-blitz (2026-08-07): the strongest clean-universe discriminator, published
    as a split of the existing cohorts at a FROZEN threshold (clean-universe median
    velocity) -- never a gate, never a new arm, never tuned on outcomes."""
    slow = _v4_alert(0, return_bps=500, mode="standard")
    fast = _v4_alert(1, return_bps=-300, mode="standard")
    slow["features"]["eligible_capital_velocity_atomic_per_s"] = 1_000_000_000
    fast["features"]["eligible_capital_velocity_atomic_per_s"] = 9_000_000_000
    control = _v4_alert(2, control=True, return_bps=100, mode="standard")
    control["features"]["eligible_capital_velocity_atomic_per_s"] = 2_000_000_000

    result = _confirmation_microscope([slow, fast, control])

    split = result["matched_control"]["slow_capital_split"]
    assert split["threshold_atomic_per_s"] == 3_500_000_000
    assert split["truth"]["slow"] == {"n": 1, "mean_return_bps": 500}
    assert split["truth"]["fast"] == {"n": 1, "mean_return_bps": -300}
    assert split["control"]["slow"] == {"n": 1, "mean_return_bps": 100}
    assert split["control"]["fast"]["n"] == 0
    # A row without the velocity feature is excluded, not guessed.
    bare = _v4_alert(3, return_bps=50, mode="standard")
    bare_result = _confirmation_microscope([bare])
    assert bare_result["matched_control"]["slow_capital_split"]["truth"]["slow"]["n"] == 0


def test_ladder_cells_never_pool_across_evaluator_versions():
    """REGRESSION (audit 2026-08-07). The merge key dropped evaluator_version, so v1/v2/v3
    rows pooled into one cell -- the exact cross-version pooling the ladder version bump
    exists to prevent. A v2 and a v3 row at the same (venue, target, horizon) must stay
    two cells."""
    from sniper.research.memecoin_opportunity.insights import _merge_diagnostics

    v2 = {"venue_id": "pumpfun_solana", "target_bps": 1_200, "horizon_ms": 60_000,
          "evaluator_version": "target-ladder-v2", "launch_samples": 10, "hits": 4,
          "losses": 6, "exit_failures": 0, "migrations": 0, "data_gaps": 0}
    v3 = {**v2, "evaluator_version": "target-ladder-v3", "launch_samples": 5, "hits": 5,
          "losses": 0}
    ladder, _near = _merge_diagnostics([v3], [], None)
    assert len(ladder) == 1
    ladder2, _near = _merge_diagnostics([v2, v3], [], None)
    assert len(ladder2) == 2
    by_version = {row["evaluator_version"]: row for row in ladder2}
    assert by_version["target-ladder-v2"]["launch_samples"] == 10
    assert by_version["target-ladder-v3"]["launch_samples"] == 5
    assert by_version["target-ladder-v3"]["hit_rate_bps"] == 10_000


def test_gate_checkpoints_seal_once_and_never_reseal(monkeypatch):
    """Audit 2026-08-07 finding 5: the funding contract promises verdicts at 25/50/100
    pairs, but the live pair set moves -- a later recomputation cannot prove what the
    gate saw at the crossing. The first crossing seals pair identities + stats; a
    second sealing attempt with different data must change NOTHING."""
    from sniper.research.memecoin_opportunity import insights as ins

    alerts = [_v4_alert(i, return_bps=300, mode="standard") for i in range(26)] + [
        _v4_alert(i, control=True, return_bps=100, mode="standard") for i in range(26)
    ]
    monkeypatch.setattr(ins, "_merged_alert_evidence", lambda hot, historical: alerts)
    hot = _realistic_gate_hot([
        (
            alert["alert_id"],
            alert["rule_version"],
            alert["terminal_return_bps"],
            "valid",
            "time_exit",
        )
        for alert in alerts
    ])
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    assert ins.seal_gate_checkpoints(hot, db, now_iso="2026-08-07T10:00:00Z") == 1
    row = db.execute("SELECT * FROM op_insight_gate_checkpoints").fetchone()
    assert row["checkpoint"] == 25
    assert row["match_scale"] == ins.GATE_MATCH_SCALE
    assert row["pairs_at_seal"] == 26
    assert row["mean_arm_minus_control_bps"] == 200
    manifest = json.loads(row["pair_manifest_json"])
    assert len(manifest) == 25
    first_hash = row["pair_manifest_sha256"]

    # The world moves on -- more pairs, different returns. The seal must not.
    alerts2 = [_v4_alert(i, return_bps=-500, mode="standard") for i in range(40)] + [
        _v4_alert(i, control=True, return_bps=900, mode="standard") for i in range(40)
    ]
    monkeypatch.setattr(ins, "_merged_alert_evidence", lambda hot, historical: alerts2)
    hot2 = _realistic_gate_hot([
        (
            alert["alert_id"],
            alert["rule_version"],
            alert["terminal_return_bps"],
            "valid",
            "time_exit",
        )
        for alert in alerts2
    ])
    assert ins.seal_gate_checkpoints(hot2, db, now_iso="2026-08-08T10:00:00Z") == 0
    row2 = db.execute(
        "SELECT * FROM op_insight_gate_checkpoints WHERE checkpoint=25").fetchone()
    assert row2["pair_manifest_sha256"] == first_hash
    assert row2["mean_arm_minus_control_bps"] == 200
    assert row2["sealed_at"] == "2026-08-07T10:00:00Z"


def test_gate_checkpoint_seal_closes_its_write_transaction(monkeypatch):
    """A checkpoint seal must not block the next projection transaction."""
    from sniper.research.memecoin_opportunity import insights as ins

    alerts = [_v4_alert(i, return_bps=300, mode="standard") for i in range(26)] + [
        _v4_alert(i, control=True, return_bps=100, mode="standard") for i in range(26)
    ]
    monkeypatch.setattr(ins, "_merged_alert_evidence", lambda hot, historical: alerts)
    hot = _realistic_gate_hot([
        (
            alert["alert_id"],
            alert["rule_version"],
            alert["terminal_return_bps"],
            "valid",
            "time_exit",
        )
        for alert in alerts
    ])
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    assert ins.seal_gate_checkpoints(hot, db, now_iso="2026-08-07T10:00:00Z") == 1
    assert not db.in_transaction
    db.execute("BEGIN IMMEDIATE")
    db.execute("ROLLBACK")


def test_gate_checkpoint_does_not_seal_legacy_outcomes(monkeypatch):
    from sniper.research.memecoin_opportunity import insights as ins

    alerts = [_v4_alert(i, return_bps=300, mode="standard") for i in range(26)] + [
        _v4_alert(i, control=True, return_bps=100, mode="standard") for i in range(26)
    ]
    monkeypatch.setattr(ins, "_merged_alert_evidence", lambda hot, historical: alerts)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row

    assert ins.seal_gate_checkpoints(
        _realistic_gate_hot([]), db, now_iso="2026-08-07T10:00:00Z"
    ) == 0
    assert db.execute("SELECT COUNT(*) FROM op_insight_gate_checkpoints").fetchone()[0] == 0
