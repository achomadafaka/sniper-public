"""Calibration gate (prereg section 7): exact one-sided Monte Carlo bounds, min replications, supported generators only."""
import math

import pytest

from sniper.office.readiness import calibration as C
from sniper.office.readiness import verdict as V


def test_cp_upper_with_zero_rejections_is_about_3_over_M():
    assert C.cp_upper(0, 1000) == pytest.approx(1 - 0.05 ** (1 / 1000), rel=1e-9)
    assert C.cp_upper(0, 1000) == pytest.approx(0.002988, abs=2e-5)


def test_cp_lower_matches_known_value_and_zero_case():
    assert C.cp_lower(0, 100) == 0.0
    assert C.cp_lower(80, 100) == pytest.approx(0.7266, abs=5e-3)


def test_min_reps_makes_type_one_bound_attainable():
    for k in (1, 2, 5, 10):
        a = V.alpha_k(k)
        assert C.cp_upper(0, C.min_reps(a)) <= a
        assert C.cp_upper(0, C.min_reps(a) - 5) > a * 0.99


def test_k_beyond_compute_budget_is_unsupported_not_guessed():
    r = C.calibrate(k=10, generator="iid", n_coins=60, days=16, delta0=1.0, delta1=3.0, sigma=4.0,
                    compute_budget_cells=1_000_000, seed=3)
    assert not r.supported and r.reason == "reps_beyond_compute_budget"


def test_a_conservative_enough_method_is_supported_on_listed_generators():
    r = C.calibrate(k=1, generator="iid", n_coins=120, days=16, delta0=1.0, delta1=6.0, sigma=3.0,
                    compute_budget_cells=10**10, seed=11, method="very_conservative_test_only", draws=1500)
    assert r.reps >= C.min_reps(V.alpha_k(1))
    assert r.type1_upper <= V.alpha_k(1)
    assert r.supported == (r.type1_upper <= V.alpha_k(1))


def test_a_broken_method_fails_the_gate_on_day_clustered_data():
    r = C.calibrate(k=1, generator="day_cluster", n_coins=120, days=16, delta0=1.0, delta1=6.0, sigma=3.0,
                    compute_budget_cells=10**10, seed=5, method="coin_only_wrong", draws=1500)
    assert not r.supported and r.reason == "type1_upper_above_alpha"


def test_unlisted_generator_is_refused():
    with pytest.raises(ValueError):
        C.calibrate(k=1, generator="mystery", n_coins=60, days=16, delta0=1.0, delta1=3.0, sigma=3.0,
                    compute_budget_cells=10**9, seed=1)


def test_listed_generators_include_pods_shifts_and_adaptive_selection():
    assert {"iid", "heavy_tail", "day_cluster", "serial", "adaptive_entry", "pods_shifts"} <= set(C.GENERATORS)


def test_proof_needs_every_generator_and_is_content_addressed():
    rs = [C.Calibration(1, g, "production", True, g in ("iid",), 1000, 0.01, 0.9, None) for g in C.GENERATORS]
    p = C.make_proof(rs, params={"days": 30}, code_commit="abc", plan_sha256="def", created_at=1.0)
    assert p["supported_for_k"] and not p["power_claim_for_k"] and len(p["proof_id"]) == 64
    p2 = C.make_proof(rs, params={"days": 31}, code_commit="abc", plan_sha256="def", created_at=1.0)
    assert p["proof_id"] != p2["proof_id"]
    with pytest.raises(ValueError):
        C.make_proof(rs[:-1], params={}, code_commit="a", plan_sha256="b", created_at=1.0)
    bad = rs[:-1] + [C.Calibration(1, "pods_shifts", "production", False, False, 1000, 0.1, 0.1, "type1_upper_above_alpha")]
    assert not C.make_proof(bad, params={}, code_commit="a", plan_sha256="b", created_at=1.0)["supported_for_k"]


def test_generator_days_are_chronological():
    import numpy as np
    rng = np.random.default_rng(1)
    for g in C.GENERATORS:
        _, _, day, _ = C._noise(g, rng, 240, 30, 3.0)
        assert (np.diff(day) >= 0).all() and day.min() == 0 and day.max() == 29
