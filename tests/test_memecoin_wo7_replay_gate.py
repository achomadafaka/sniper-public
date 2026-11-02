"""WO-7 — official replay acceptance gate. Pure scoring logic on SYNTHETIC data (this cycle's real
empirical floors are UNMET, so nothing arms). What is tested: the gate REFUSES to arm on a mid-only
(uncalibrated) metric or when evidence floors are unmet, and only a calibrated + floors-met + paired
LB>=0 + no-catastrophic-regression combination is arm-eligible. The gate manufactures nothing.
"""
from sniper.services import memecoin_v5_replay_gate as g


# --- fill model --------------------------------------------------------------------------------

def test_uncalibrated_fill_model_is_mid_labeled_and_ineligible():
    fm = g.FillModel(impact_cap_pct=15.0, haircut_frac=0.10, calibrated=False)   # cap above haircut
    assert fm.metric_name == "net_mid_exit_value" and fm.arm_eligible is False
    assert fm.exit_value(100.0) == 90.0                       # 10% p75 haircut, not capped


def test_fill_model_applies_whole_bag_impact_cap():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.50, calibrated=False)
    assert fm.exit_value(100.0) == 94.0                       # haircut 50% capped to 6%


def test_calibrate_requires_min_real_closes():
    closes = [{"notional": 100.0, "realized_impact_frac": 0.03} for _ in range(19)]
    assert g.calibrate_fill_model(closes, impact_cap_pct=6.0, haircut_frac=0.1).calibrated is False
    closes += [{"notional": 500.0, "realized_impact_frac": 0.05}]   # now 20
    fm = g.calibrate_fill_model(closes, impact_cap_pct=6.0, haircut_frac=0.1)
    assert fm.calibrated is True and fm.metric_name == "net_fill_modeled_exit_value"


# --- time-ordered OOS split --------------------------------------------------------------------

def test_time_ordered_split_is_by_time_not_random():
    eps = [{"closed_ts": f"2026-07-{d:02d}"} for d in (5, 1, 3, 2, 4)]
    train, hold = g.time_ordered_split(eps, train_frac=0.6, time_key="closed_ts")
    assert [e["closed_ts"] for e in train] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert [e["closed_ts"] for e in hold] == ["2026-07-04", "2026-07-05"]


# --- evidence floors ---------------------------------------------------------------------------

def test_floors_require_all_metrics_and_calibration():
    full = {"position_days": 14, "closed_overlapping": 20, "decoded_lp_removals": 10}
    met, reasons = g.evidence_floors_met(full, fill_model_calibrated=True)
    assert met and reasons == []
    # calibration missing -> unmet even with every count satisfied
    met2, r2 = g.evidence_floors_met(full, fill_model_calibrated=False)
    assert not met2 and "fill_model_uncalibrated" in r2
    # a short count -> unmet
    met3, r3 = g.evidence_floors_met({**full, "decoded_lp_removals": 3}, fill_model_calibrated=True)
    assert not met3 and any("decoded_lp_removals" in x for x in r3)


# --- acceptance verdict ------------------------------------------------------------------------

_FLOORS_OK = {"position_days": 14, "closed_overlapping": 20, "decoded_lp_removals": 10}


def _ep(value, n):
    return {f"ep{i}": float(value) for i in range(n)}


def test_uncalibrated_never_arms_even_if_candidate_wins():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.1, calibrated=False)
    v = g.acceptance_verdict(_ep(10, 30), _ep(20, 30), fill_model=fm, floor_metrics=_FLOORS_OK,
                             catastrophic_baseline=0.1, catastrophic_candidate=0.1)
    assert v.arm_eligible is False and v.metric_name == "net_mid_exit_value"
    assert "fill_model_uncalibrated" in v.reasons


def test_calibrated_floors_met_positive_lb_arms():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.05, calibrated=True,
                     slippage_curve=((100.0, 0.05),))
    v = g.acceptance_verdict(_ep(10.0, 40), _ep(12.0, 40), fill_model=fm, floor_metrics=_FLOORS_OK,
                             catastrophic_baseline=0.10, catastrophic_candidate=0.08)
    assert v.arm_eligible is True and v.bootstrap_lower_bound >= 0 and v.reasons == ()


def test_catastrophic_regression_blocks_arming():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.05, calibrated=True,
                     slippage_curve=((100.0, 0.05),))
    v = g.acceptance_verdict(_ep(10.0, 40), _ep(12.0, 40), fill_model=fm, floor_metrics=_FLOORS_OK,
                             catastrophic_baseline=0.05, catastrophic_candidate=0.20)
    assert v.arm_eligible is False and v.catastrophic_regression is True


def test_negative_lower_bound_blocks_arming():
    fm = g.FillModel(impact_cap_pct=6.0, haircut_frac=0.05, calibrated=True,
                     slippage_curve=((100.0, 0.05),))
    # candidate is worse on average -> paired LB < 0
    v = g.acceptance_verdict(_ep(12.0, 40), _ep(10.0, 40), fill_model=fm, floor_metrics=_FLOORS_OK,
                             catastrophic_baseline=0.1, catastrophic_candidate=0.1)
    assert v.arm_eligible is False and v.bootstrap_lower_bound < 0


def test_full_policy_replay_tracks_independent_inventory_per_arm():
    from sniper.services.memecoin_exit_brain import CandidateDecision, CUT, DERISK, RIDE
    fm = g.FillModel(impact_cap_pct=100, haircut_frac=0, calibrated=True,
                     slippage_curve=((10_000.0, 0.0),))
    episodes = [{
        "episode_id": "ep1", "entry_notional": 100.0,
        "events": [
            {"event_id": "a", "whole_position_mid_value": 120.0},
            {"event_id": "b", "whole_position_mid_value": 80.0},
        ],
    }]
    baseline_seen = []
    candidate_seen = []
    def baseline(_event, inventory):
        baseline_seen.append(inventory.remaining_fraction)
        return CandidateDecision(CUT)
    def candidate(event, inventory):
        candidate_seen.append((event["event_id"], inventory.remaining_fraction,
                               inventory.partial_used))
        return (CandidateDecision(DERISK, proposed_fraction=0.5)
                if event["event_id"] == "a" else CandidateDecision(RIDE))

    paired = g.replay_paired_policies(
        episodes, baseline_decide=baseline, candidate_decide=candidate, fill_model=fm)

    assert baseline_seen == [1.0]
    assert candidate_seen == [("a", 1.0, False), ("b", 0.5, True)]
    assert paired["baseline_by_episode"] == {"ep1": 20.0}
    # candidate sells half at 120 (=60), tail at 80 (=40), exactly breaks even.
    assert paired["candidate_by_episode"] == {"ep1": 0.0}
    assert paired["candidate_results"]["ep1"].actions == ("DERISK:0.500000", "TERMINAL")


def test_full_policy_replay_rejects_duplicate_or_unpriced_episode_identity():
    from sniper.services.memecoin_exit_brain import CandidateDecision, RIDE
    fm = g.FillModel(impact_cap_pct=100, haircut_frac=0, calibrated=True,
                     slippage_curve=((10_000.0, 0.0),))
    decide = lambda _event, _inventory: CandidateDecision(RIDE)
    import pytest
    with pytest.raises(g.EvidencePairingError):
        g.replay_paired_policies(
            [{"episode_id": "x", "entry_notional": 100, "events": []}],
            baseline_decide=decide, candidate_decide=decide, fill_model=fm)
    with pytest.raises(g.EvidencePairingError):
        g.replay_paired_policies(
            [{"episode_id": "x", "entry_notional": 100,
              "events": [{"whole_position_mid_value": 100}]},
             {"episode_id": "x", "entry_notional": 100,
              "events": [{"whole_position_mid_value": 100}]}],
            baseline_decide=decide, candidate_decide=decide, fill_model=fm)
