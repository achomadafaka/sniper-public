"""Phase 2A — Q2 jump/regime Monte Carlo structural probability + Q3 residual model anchored on the
market prior. Both pure / deterministic (seeded)."""
import math

from sniper.algos.polymarket_crypto5m import quant_probability as QP


# ---- Q2 Monte Carlo ----
def test_mc_at_the_money_is_half():
    r = QP.monte_carlo_up_probability(current_price=100.0, price_to_beat=100.0, vol_per_s=0.0005,
                                      seconds_remaining=60, n_paths=40000, seed=1)
    assert abs(r["q_structural_up"] - 0.5) < 0.02
    assert r["simulation_standard_error"] > 0


def test_mc_far_above_threshold_near_certain_up():
    # current price well above the line with little time/vol left -> Up almost certain
    r = QP.monte_carlo_up_probability(current_price=101.0, price_to_beat=100.0, vol_per_s=0.0002,
                                      seconds_remaining=30, n_paths=40000, seed=2)
    assert r["q_structural_up"] > 0.95


def test_mc_matches_driftless_diffusion_without_jumps():
    # with no jumps, MC should track the closed-form lognormal probability used by ThresholdDiffusionV0
    cur, p2b, vol, secs = 100.5, 100.0, 0.0006, 90
    z = math.log(cur / p2b) / (vol * math.sqrt(secs))
    closed = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    r = QP.monte_carlo_up_probability(current_price=cur, price_to_beat=p2b, vol_per_s=vol,
                                      seconds_remaining=secs, n_paths=60000, seed=3)
    assert abs(r["q_structural_up"] - closed) < 0.02


def test_mc_jumps_widen_distribution():
    base = QP.monte_carlo_up_probability(current_price=101.0, price_to_beat=100.0, vol_per_s=0.0002,
                                         seconds_remaining=30, n_paths=40000, seed=4)
    jumpy = QP.monte_carlo_up_probability(current_price=101.0, price_to_beat=100.0, vol_per_s=0.0002,
                                          seconds_remaining=30, n_paths=40000, seed=4,
                                          jump_intensity_per_s=0.05, jump_sigma=0.01)
    # jumps add downside mass to a near-certain Up -> probability drops toward 0.5
    assert jumpy["q_structural_up"] < base["q_structural_up"]


def test_mc_deterministic_with_seed():
    a = QP.monte_carlo_up_probability(current_price=100.2, price_to_beat=100.0, vol_per_s=0.0005,
                                      seconds_remaining=60, n_paths=20000, seed=7)
    b = QP.monte_carlo_up_probability(current_price=100.2, price_to_beat=100.0, vol_per_s=0.0005,
                                      seconds_remaining=60, n_paths=20000, seed=7)
    assert a["q_structural_up"] == b["q_structural_up"]


# ---- Q3 residual model (anchored on logit(q_market)) ----
def _logit(p):
    return math.log(p / (1 - p))


def test_residual_learns_signal_beyond_market_prior():
    # Build data where the market prior is a coin-flip (0.5) but a feature perfectly predicts the label.
    # The residual model should learn a positive weight on that feature and beat the prior.
    rows = []
    for i in range(400):
        x = 1.0 if i % 2 == 0 else -1.0
        y = 1 if x > 0 else 0
        rows.append({"q_market": 0.5, "x": x, "y": y})
    w = QP.fit_residual_logistic(rows, feature_names=["x"], l2=0.01, iters=600, lr=0.3)
    up = QP.residual_predict(w, {"x": 1.0}, q_market=0.5)
    dn = QP.residual_predict(w, {"x": -1.0}, q_market=0.5)
    assert up > 0.8 and dn < 0.2                       # residual moved probability the right way


def test_residual_shrinks_to_prior_when_no_extra_signal():
    # Label already matches the market prior; feature is pure noise-free zero -> residual ~ prior.
    rows = [{"q_market": 0.7, "x": 0.0, "y": 1 if i < 70 else 0} for i in range(100)]
    w = QP.fit_residual_logistic(rows, feature_names=["x"], l2=1.0, iters=300, lr=0.1)
    q = QP.residual_predict(w, {"x": 0.0}, q_market=0.7)
    assert abs(q - 0.7) < 0.08                          # stays anchored near the market prior
