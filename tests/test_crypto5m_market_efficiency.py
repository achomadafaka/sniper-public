"""Phase 1C #3 — market baseline metrics for the efficiency gate.

Pure forecast metrics (Brier, log loss, reliability, ECE, logistic calibration intercept/slope) over
(predicted Up prob, up_won) pairs, plus a per-checkpoint report over the captured DB. Midpoint (displayed
implied prob) is kept SEPARATE from executable entry. Primary baseline = raw Up midpoint.
"""
import math
import os

import scripts.crypto5m_efficiency_report as CLI
from sniper.algos.polymarket_crypto5m import features as FE
from sniper.algos.polymarket_crypto5m import market_efficiency as ME
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import sim_persistence as SP


def test_midpoint_and_normalized():
    assert ME.midpoint({"best_bid": 0.60, "best_ask": 0.62}) == 0.61
    assert ME.midpoint({"best_bid": None, "best_ask": 0.62}) is None
    assert abs(ME.normalized_up_midpoint(0.61, 0.39) - 0.61) < 1e-9
    assert ME.normalized_up_midpoint(None, 0.39) is None


def test_brier_and_log_loss_known_values():
    assert ME.brier_score([0.5, 0.5], [1, 0]) == 0.25
    assert ME.brier_score([1.0, 0.0], [1, 0]) == 0.0
    assert abs(ME.log_loss([0.5], [1]) - math.log(2)) < 1e-9
    assert ME.log_loss([1.0], [1]) < 1e-6                  # near-perfect (clipped), not inf


def test_reliability_and_ece():
    # two perfectly-calibrated buckets -> ECE ~ 0
    preds = [0.3] * 100 + [0.7] * 100
    outs = [1] * 30 + [0] * 70 + [1] * 70 + [0] * 30
    ece = ME.expected_calibration_error(preds, outs, n_bins=10)
    assert ece < 0.02


def test_calibration_fit_recovers_slope_one_on_calibrated_data():
    preds = [0.3] * 100 + [0.7] * 100
    outs = [1] * 30 + [0] * 70 + [1] * 70 + [0] * 30
    fit = ME.calibration_fit(preds, outs)
    assert fit is not None
    assert abs(fit["slope"] - 1.0) < 0.3 and abs(fit["intercept"]) < 0.3


def test_calibration_fit_none_on_degenerate():
    assert ME.calibration_fit([0.4, 0.6], [1, 1]) is None          # all outcomes same
    assert ME.calibration_fit([0.5] * 50, [1, 0] * 25) is None     # no variance in predictor


def test_forecast_metrics_bundle():
    m = ME.forecast_metrics([0.3, 0.7], [0, 1])
    assert m["n"] == 2
    assert abs(m["mean_pred"] - 0.5) < 1e-9
    assert m["actual_freq"] == 0.5
    assert "brier" in m and "log_loss" in m and "ece" in m and "reliability_bins" in m


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "me.sqlite"))
    P.ensure_schema(conn)
    SP.ensure_sim_schema(conn)
    return conn


def _seed(conn, cid, cp, *, up_mid, winner, feature_status="ok", integrity="usable",
         recv_missing=False):
    SP.upsert_feature_snapshot(conn, dict(
        condition_id=cid, market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=cp,
        decision_ts=1000 + cp, feature_cutoff_ts=1000 + cp, feature_version=FE.FEATURE_VERSION,
        feature_status=feature_status, price_to_beat_quality="unavailable",
        book_integrity_status=integrity, receive_time_missing=recv_missing, market_rule_hash="h",
        features={"up_midpoint_probability": up_mid, "down_midpoint_probability": (1 - up_mid)}), now_ts=1)
    conn.execute("INSERT OR IGNORE INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES (?,?,?)", (cid, winner, "gamma_only"))
    conn.commit()


def test_stale_or_leaky_books_excluded_reconciliation_quarantine_is_diagnostic(tmp_path):
    # 2026-06-21: the per-snapshot book_integrity_status is the real gate. A condition-level
    # crypto5m_book_reconciliation quarantine (fires on benign WS-vs-REST movement) is NO LONGER an
    # eligibility gate, so a quarantined window with a usable decision book IS observed.
    conn = _conn(tmp_path)
    _seed(conn, "ok", 60, up_mid=0.7, winner="Up")
    _seed(conn, "stale", 60, up_mid=0.5, winner="Up", integrity="stale")
    _seed(conn, "leak", 60, up_mid=0.5, winner="Up", recv_missing=True)
    _seed(conn, "badfs", 60, up_mid=0.5, winner="Up", feature_status="feature_window_incomplete")
    _seed(conn, "quar", 60, up_mid=0.5, winner="Up")          # usable book + a quarantined interval
    conn.execute("INSERT INTO crypto5m_book_reconciliation (condition_id, token_id, "
                 "quarantined_interval) VALUES ('quar','t',1)")
    conn.commit()
    assert {o["condition_id"] for o in ME.baseline_observations(conn)} == {"ok", "quar"}


def test_baseline_observations_and_checkpoint_report(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, up_mid=0.7, winner="Up")
    _seed(conn, "b", 60, up_mid=0.4, winner="Down")
    _seed(conn, "c", 60, up_mid=0.9, winner="Up")
    obs = ME.baseline_observations(conn)
    assert len(obs) == 3
    assert all(o["checkpoint"] == 60 for o in obs)
    rep = ME.checkpoint_efficiency_report(conn)
    assert 60 in rep and rep[60]["n"] == 3
    assert rep[60]["actual_freq"] == 2 / 3                 # 2 of 3 resolved Up


def test_percentile():
    assert ME._percentile([10], 0.5) == 10
    assert ME._percentile([0, 10], 0.5) == 5.0
    assert ME._percentile([0, 5, 10], 0.9) == 9.0
    assert ME._percentile([], 0.5) is None


def _seed_exec(conn, cid, cp, *, up_mid, exec_price, fee_inc, fillable=1.0, winner="Up"):
    feats = {"up_midpoint_probability": up_mid, "down_midpoint_probability": 1 - up_mid}
    for stake in (5, 10, 20):
        feats[f"up_executable_price_{stake}_usdc"] = exec_price
        feats[f"up_fee_inclusive_cost_{stake}_usdc"] = fee_inc
        feats[f"up_fillability_{stake}_usdc"] = fillable
        feats[f"down_executable_price_{stake}_usdc"] = None
        feats[f"down_fee_inclusive_cost_{stake}_usdc"] = None
        feats[f"down_fillability_{stake}_usdc"] = 0.0
    SP.upsert_feature_snapshot(conn, dict(
        condition_id=cid, market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=cp,
        decision_ts=1000 + cp, feature_cutoff_ts=1000 + cp, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable", receive_time_missing=False,
        market_rule_hash="h", features=feats), now_ts=1)
    conn.execute("INSERT OR IGNORE INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES (?,?,?)", (cid, winner, "gamma_only"))
    conn.commit()


def test_executable_hurdle_observation():
    # buying Up at executable price 0.65 when midpoint is 0.61 -> a +0.04 fee-exclusive hurdle
    obs = ME.executable_hurdle_for_features(
        {"up_midpoint_probability": 0.61, "up_executable_price_10_usdc": 0.65,
         "up_fee_inclusive_cost_10_usdc": None, "up_fillability_10_usdc": 1.0}, side="up", stake=10)
    assert abs(obs["executable_hurdle"] - 0.04) < 1e-9
    assert obs["fee_inclusive_hurdle"] is None          # fee unknown -> no fee-inclusive hurdle
    assert obs["fillable"] is True


def test_executable_hurdle_report(tmp_path):
    conn = _conn(tmp_path)
    _seed_exec(conn, "a", 60, up_mid=0.61, exec_price=0.65, fee_inc=0.66)
    _seed_exec(conn, "b", 60, up_mid=0.50, exec_price=0.52, fee_inc=0.53)
    rep = ME.executable_hurdle_report(conn)
    up60 = next(r for r in rep if r["checkpoint"] == 60 and r["side"] == "up" and r["stake"] == 10)
    assert up60["n"] == 2 and up60["fillable_share"] == 1.0
    assert up60["median_executable_hurdle"] is not None
    assert up60["fee_inclusive_available_share"] == 1.0   # fee_inc present -> available


def test_cluster_bootstrap_resamples_windows():
    # one window contributes many checkpoint rows but counts as ONE cluster; the bootstrap resamples
    # windows, so a determinism seed gives a stable CI around the point estimate.
    rows = ([{"condition_id": "w1", "y": 1} for _ in range(12)]
            + [{"condition_id": "w2", "y": 0} for _ in range(12)]
            + [{"condition_id": "w3", "y": 1} for _ in range(12)])
    stat = lambda rs: sum(r["y"] for r in rs) / len(rs)
    res = ME.cluster_bootstrap(rows, statistic=stat, n_boot=200, seed=7)
    assert res["n_clusters"] == 3
    assert abs(res["point"] - 2 / 3) < 1e-9
    assert res["ci_low"] <= res["point"] <= res["ci_high"]
    assert ME.cluster_bootstrap(rows, statistic=stat, n_boot=200, seed=7) == res   # deterministic


def test_temporal_blocks_split_by_time():
    rows = [{"window_start_ts": t, "id": t} for t in (300, 100, 200, 400)]
    blocks = ME.temporal_blocks(rows, n_blocks=2)
    assert [r["id"] for r in blocks[0]] == [100, 200]      # earliest half
    assert [r["id"] for r in blocks[1]] == [300, 400]


def test_temporal_blocks_keep_whole_windows_together():
    rows = [
        {"condition_id": "w1", "window_start_ts": 100, "id": "w1-a"},
        {"condition_id": "w1", "window_start_ts": 100, "id": "w1-b"},
        {"condition_id": "w2", "window_start_ts": 200, "id": "w2-a"},
    ]
    blocks = ME.temporal_blocks(rows, n_blocks=2)
    assert all(
        not ({r["condition_id"] for r in blocks[i]} & {r["condition_id"] for r in blocks[j]})
        for i in range(len(blocks)) for j in range(i + 1, len(blocks))
    )
    assert {r["id"] for r in blocks[0]} == {"w1-a", "w1-b"}
    assert {r["id"] for r in blocks[1]} == {"w2-a"}


def test_baseline_observations_carry_window_start_ts(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, up_mid=0.7, winner="Up")
    obs = ME.baseline_observations(conn)
    assert obs[0]["window_start_ts"] == 1000


def test_solve_linear_system():
    # 2x + y = 5 ; x + 3y = 10  -> x=1, y=3
    sol = ME._solve([[2.0, 1.0], [1.0, 3.0]], [5.0, 10.0])
    assert abs(sol[0] - 1.0) < 1e-9 and abs(sol[1] - 3.0) < 1e-9


def test_fit_logistic_separates():
    # x predicts y; fitted model gives high p for x=1, low for x=0
    X = [[1.0, -1.0]] * 20 + [[1.0, 1.0]] * 20   # [intercept, x]
    y = [0] * 20 + [1] * 20
    w = ME._fit_logistic(X, y)
    assert ME._predict(w, [1.0, 1.0]) > 0.8 and ME._predict(w, [1.0, -1.0]) < 0.2


def _wf_obs(predictive):
    rows = []
    for i in range(80):
        x = i % 2
        rows.append({"condition_id": f"w{i}", "window_start_ts": i, "midpoint": 0.5,
                     "up_won": (x if predictive else (i // 13) % 2),
                     "features": {"x": float(x)}})
    return rows


def test_walk_forward_predictive_feature_beats_midpoint():
    res = ME.walk_forward_comparison(_wf_obs(True), n_blocks=4,
                                     models={"M0": [], "M1": ["x"]})
    assert res["M0"]["brier"] > 0.2                      # midpoint-only ~0.25
    assert res["M1"]["brier"] < res["M0"]["brier"]       # the feature adds OOS info
    assert res["M1"]["delta_brier_vs_m0"] < 0


def test_walk_forward_delta_uses_aligned_midpoint():
    # the delta compares the model to the midpoint over the SAME OOS rows (not a different M0 sample)
    res = ME.walk_forward_comparison(_wf_obs(True), n_blocks=4, models={"M0": [], "M1": ["x"]})
    assert "m0_brier_aligned" in res["M1"]
    assert abs(res["M1"]["delta_brier_vs_m0"]
               - (res["M1"]["brier"] - res["M1"]["m0_brier_aligned"])) < 1e-12


def test_walk_forward_noise_feature_no_real_improvement():
    res = ME.walk_forward_comparison(_wf_obs(False), n_blocks=4,
                                     models={"M0": [], "M1": ["x"]})
    # a feature uncorrelated with the label must not materially beat the midpoint OOS
    assert res["M1"]["delta_brier_vs_m0"] > -0.05


def test_model_observations_reads_features(tmp_path):
    conn = _conn(tmp_path)
    SP.upsert_feature_snapshot(conn, dict(
        condition_id="a", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=60,
        decision_ts=1060, feature_cutoff_ts=1060, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable", receive_time_missing=False,
        market_rule_hash="h", features={"up_midpoint_probability": 0.6, "normalized_distance_to_beat": -1.0,
                                         "seconds_remaining": 240, "vol_per_s": 0.001}), now_ts=1)
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('a','Up','gamma_only')")
    conn.commit()
    obs = ME.model_observations(conn)
    assert obs[0]["midpoint"] == 0.6 and obs[0]["up_won"] == 1
    assert obs[0]["features"]["seconds_remaining"] == 240


def test_shuffle_preserves_block_label_rate():
    obs = _wf_obs(True)
    shuf = ME.shuffle_labels_within_blocks(obs, n_blocks=4, seed=1)
    assert sum(r["up_won"] for r in shuf) == sum(r["up_won"] for r in obs)   # marginal preserved


def test_shuffled_labels_destroy_the_edge():
    # the real feature beats M0 on real labels, but NOT after labels are shuffled within blocks
    real = ME.walk_forward_comparison(_wf_obs(True), n_blocks=4, models={"M0": [], "M1": ["x"]})
    assert real["M1"]["delta_brier_vs_m0"] < 0                       # genuine OOS edge on real labels
    shuf = ME.shuffle_labels_within_blocks(_wf_obs(True), n_blocks=4, seed=2)
    res = ME.walk_forward_comparison(shuf, n_blocks=4, models={"M0": [], "M1": ["x"]})
    assert res["M1"]["delta_brier_vs_m0"] > -0.05                    # edge gone


def test_negative_controls_pass_on_genuine_data():
    nc = ME.negative_controls(_wf_obs(True), n_blocks=4, seed=3, feature_model=("M1", ["x"]))
    assert nc["passed"] is True            # shuffled-label + noise-feature controls show no spurious edge


def test_go_no_go_insufficient_data():
    g = ME._go_no_go(500, {}, True)
    assert g["verdict"] == "insufficient_data"


def test_go_no_go_pause_when_no_improver():
    e2 = {"M0": {"brier": 0.25}, "M1": {"brier": 0.25, "delta_brier_vs_m0": 0.0}}
    assert ME._go_no_go(1500, e2, True)["verdict"] == "pause"


def test_go_no_go_pause_when_controls_fail():
    e2 = {"M0": {"brier": 0.25}, "M1": {"brier": 0.10, "delta_brier_vs_m0": -0.15}}
    assert ME._go_no_go(1500, e2, False)["verdict"] == "pause"


def test_go_no_go_proceed_candidate():
    e2 = {"M0": {"brier": 0.25}, "M1": {"brier": 0.20, "delta_brier_vs_m0": -0.05}}
    assert ME._go_no_go(1500, e2, True)["verdict"] == "proceed_candidate"


def test_efficiency_gate_report_structure(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, up_mid=0.7, winner="Up")
    _seed(conn, "b", 60, up_mid=0.4, winner="Down")
    rep = ME.efficiency_gate_report(conn)
    assert rep["n_resolved_windows"] == 2
    assert rep["milestones"][200] is False and 5000 in rep["milestones"]
    assert "E1_calibration" in rep and "E2_model_ladder" in rep and "E3_executable_hurdle" in rep
    assert rep["go_no_go"]["verdict"] == "insufficient_data"   # only 2 windows


def test_efficiency_report_cli_tells_operator_to_run_replay_first(tmp_path, capsys):
    db = os.path.join(str(tmp_path), "capture_only.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    conn.close()

    assert CLI.main(["--db-path", db]) == 1
    out = capsys.readouterr().out
    assert "run_polymarket_crypto5m_shadow_sim.py" in out


def test_unresolved_windows_excluded_from_baseline(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, "a", 60, up_mid=0.7, winner="Up")
    conn.execute("INSERT INTO crypto5m_settlements (condition_id, winning_outcome, "
                 "reconciliation_status) VALUES ('u', NULL, 'unresolved')")
    SP.upsert_feature_snapshot(conn, dict(
        condition_id="u", market_id="m", window_start_ts=1000, window_end_ts=1300, checkpoint=60,
        decision_ts=1060, feature_cutoff_ts=1060, feature_version=FE.FEATURE_VERSION, feature_status="ok",
        price_to_beat_quality="unavailable", book_integrity_status="usable", receive_time_missing=False,
        market_rule_hash="h", features={"up_midpoint_probability": 0.5}), now_ts=1)
    conn.commit()
    assert len(ME.baseline_observations(conn)) == 1        # only the resolved window
