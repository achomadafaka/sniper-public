"""Phase 2B — calibrated ensemble (log-opinion pool) + uncertainty-shrunk conservative probability.
The conservative prob shrinks toward 0.5 with disagreement and NEVER blows to 0/1 (fixes the v1 bug where
agreement manufactured fake favorite edges)."""
from sniper.algos.polymarket_crypto5m import quant_ensemble as QE


def test_agreement_low_uncertainty_keeps_probability():
    r = QE.ensemble_probability({"a": 0.80, "b": 0.82, "c": 0.78})
    assert 0.78 <= r["q_ensemble"] <= 0.82
    assert r["predictive_uncertainty"] < 0.03
    assert abs(r["q_conservative"] - r["q_ensemble"]) < 0.02   # barely shrunk when models agree
    assert r["n_components"] == 3


def test_disagreement_shrinks_toward_half():
    r = QE.ensemble_probability({"market": 0.06, "structural": 0.50})
    # log-pool of 0.06 and 0.50 sits well below 0.5
    assert r["q_ensemble"] < 0.30
    assert r["predictive_uncertainty"] > 0.15
    # conservative is pulled BACK toward 0.5 (between q_ensemble and 0.5), never to 0/1
    assert r["q_ensemble"] < r["q_conservative"] < 0.5


def test_strong_disagreement_collapses_to_half():
    r = QE.ensemble_probability({"a": 0.10, "b": 0.90}, shrink_k=2.0)
    assert abs(r["q_ensemble"] - 0.5) < 1e-6        # symmetric disagreement -> 0.5
    assert abs(r["q_conservative"] - 0.5) < 1e-6


def test_single_component_passthrough():
    r = QE.ensemble_probability({"only": 0.73})
    assert abs(r["q_ensemble"] - 0.73) < 1e-9 and r["predictive_uncertainty"] == 0.0
    assert abs(r["q_conservative"] - 0.73) < 1e-9


def test_extreme_agreement_does_not_blow_to_one():
    r = QE.ensemble_probability({"a": 0.97, "b": 0.985})
    assert r["q_conservative"] <= r["q_ensemble"] < 1.0
    assert r["q_conservative"] > 0.9                # stays confident but bounded


def test_empty_components():
    r = QE.ensemble_probability({})
    assert r["q_ensemble"] is None and r["q_conservative"] is None and r["n_components"] == 0


def test_weights_respected():
    eq = QE.ensemble_probability({"a": 0.2, "b": 0.8})
    wt = QE.ensemble_probability({"a": 0.2, "b": 0.8}, weights={"a": 3.0, "b": 1.0})
    assert wt["q_ensemble"] < eq["q_ensemble"]      # heavier weight on the 0.2 model pulls it down
