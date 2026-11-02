"""`profile_matched_v1`: the builder is deterministic, thin history falls back, and the arm cannot
read past its own fit window.

Every one of these fails QUIETLY if broken. A non-deterministic builder still produces artifacts; a
fallback that picks the wrong default still produces policy; an arm that reads past its window still
produces a plausible take-profit — and that last one is the in-sample error that made a roster
shadow read 1.43x when the honest number was 1.12x.
"""
import json
import math

import pytest

from sniper.services import memecoin_profile_fit as pf


def _obs(rows):
    """rows = [(peak, censored, outcome)]"""
    return [pf.Observation(i + 1, p, c, o) for i, (p, c, o) in enumerate(rows)]


def _artifact(observations, *, uid="w", chain="solana", ratio=5.2, cohort=0.1,
              end="2026-08-29T00:00:00+00:00"):
    return pf.build_artifact(
        source_user_id=uid, chain=chain, fit_window_end=end, observations=observations,
        cohort_mean_log=cohort, ratio=ratio, ratio_estimated=True, stop_at_fit=35.0,
        script_sha256="deadbeef")


# ---------------------------------------------------------------- determinism (A10)

def test_the_same_inputs_produce_a_byte_identical_artifact():
    rows = _obs([(0.4, False, 0.3), (0.1, True, 0.1), (0.9, False, -0.2), (0.25, False, 0.25)])
    a = json.dumps(_artifact(rows), sort_keys=True)
    b = json.dumps(_artifact(list(reversed(rows))), sort_keys=True)
    assert a == b, "artifact must not depend on row order"


def test_the_dataset_hash_covers_the_inputs_and_not_their_order():
    rows = _obs([(0.4, False, 0.3), (0.1, True, 0.1)])
    assert pf.dataset_sha256(rows) == pf.dataset_sha256(list(reversed(rows)))


def test_the_dataset_hash_changes_when_an_input_value_changes():
    """A hash that ignored a field the fit consumes would call two different fits identical."""
    base = _obs([(0.4, False, 0.3), (0.1, True, 0.1)])
    moved = _obs([(0.4, False, 0.3), (0.1, True, 0.9)])
    assert pf.dataset_sha256(base) != pf.dataset_sha256(moved)


# ---------------------------------------------------------------- censoring (A1)

def test_a_censored_row_is_a_lower_bound_not_a_zero_and_not_a_drop():
    """A position that exited up 40% with no recorded peak reached AT LEAST 40%. Recording it as
    zero drags the fit down as hard as dropping it drags the fit up — dropping overstates the
    cohort median peak by 11.9 points. The row enters the log-peak mean at its bound."""
    art = _artifact(_obs([(0.0, True, 0.40), (0.5, False, 0.5)]))
    assert art["n_censored"] == 1 and art["n_observed"] == 1
    assert pf.mean_log_peak(_obs([(0.40, True, 0.40)])) == pytest.approx(math.log1p(0.40))


def test_the_outcome_never_enters_the_fit_at_all():
    """The withdrawn EV rule consumed realised outcomes, and half the fitting rows exited on the
    very rails being varied — a take-profit exit returns about +25% BY CONSTRUCTION, so feeding it
    back evaluated the current policy against itself. The offset rule reads peaks only, and this
    asserts the contamination cannot re-enter: two wallets with identical peaks and wildly
    different outcomes must fit identically."""
    peaks = [(0.4, False), (0.2, False), (0.9, True)]
    a = _artifact(_obs([(p, c, +9.0) for p, c in peaks]))
    b = _artifact(_obs([(p, c, -9.0) for p, c in peaks]))
    assert a["tp_pct"] == b["tp_pct"]


def test_a_wallet_at_the_cohort_mean_gets_exactly_the_funded_level():
    """No evidence of difference means no difference. The anchor is given, never estimated."""
    obs = _obs([(0.3, False, 0.3), (0.3, False, 0.3), (0.3, False, 0.3)])
    art = _artifact(obs, cohort=pf.mean_log_peak(obs))
    assert art["tp_pct"] == pytest.approx(pf.TP_ANCHOR * 100.0)


def test_only_the_deviation_is_fitted_so_a_shifted_cohort_moves_nothing():
    """THE PROPERTY THAT REMOVES THE CIRCULARITY. The LEVEL is contaminated — the peak histogram's
    mode sits on the current target — so a rule that read it would re-derive what funded already
    runs. Raising every wallet's peaks AND the cohort mean together must leave the fit untouched,
    because the wallet's position relative to its peers has not changed."""
    obs = _obs([(0.2, False, 0.2), (0.6, False, 0.6)])
    lifted = _obs([(1.4, False, 0.2), (3.2, False, 0.6)])
    base = _artifact(obs, cohort=pf.mean_log_peak(obs) - 0.15)
    up = _artifact(lifted, cohort=pf.mean_log_peak(lifted) - 0.15)
    assert base["tp_pct"] == pytest.approx(up["tp_pct"], abs=0.01)


# ---------------------------------------------------------------- shrinkage (A3)

def test_shrinkage_weights_match_the_registered_table():
    for n, expected in ((3, 0.37), (5, 0.49), (10, 0.66), (20, 0.79), (50, 0.91)):
        assert n / (n + 5.2) == pytest.approx(expected, abs=0.005), n


def test_a_thin_wallet_is_pulled_toward_the_cohort_and_a_thick_one_is_not():
    """The protection shrinkage provides instead of a threshold: low evidence is weighted at what
    it is worth rather than forbidden by a clamp."""
    wild = [(3.0, False, 3.0)]
    thin = _artifact(_obs(wild * 3), cohort=0.0)
    thick = _artifact(_obs(wild * 60), cohort=0.0)
    assert thin["shrinkage_weight"] < thick["shrinkage_weight"]
    assert thin["shrinkage_weight"] == pytest.approx(3 / (3 + 5.2), abs=1e-6)


def test_the_shrinkage_ratio_is_estimated_and_flagged_when_it_cannot_be():
    ratio, estimated = pf.shrinkage_ratio([_obs([(0.2, False, 0.2)])])
    assert ratio == pf.DEFAULT_SHRINK_RATIO and estimated is False, "one group is not estimable"
    ratio, estimated = pf.shrinkage_ratio(
        [_obs([(0.2, False, 0.2), (0.4, False, 0.4)]),
         _obs([(0.9, False, 0.9), (1.4, False, 1.4)])])
    assert estimated is True and ratio > 0


# ---------------------------------------------------------------- fallback (A7)

def test_no_prior_closes_falls_back_and_says_why():
    art = _artifact([])
    assert art["fallback"] is True
    assert art["tp_pct"] is None
    assert "no prior measurable closes" in art["fallback_reason"]


def test_derisk_and_trail_are_null_and_marked_inherited_never_zero():
    """A4. A null that reads as a zero would silently disable the funded de-risk."""
    art = _artifact(_obs([(0.4, False, 0.4), (0.2, False, 0.2), (0.6, False, 0.6)]))
    assert art["derisk_fraction"] is None and art["trail_pct"] is None
    assert art["inherited"] is True


def test_the_take_profit_is_clamped_to_the_derived_bounds():
    """+15% is the round-trip cost floor, +60% the observed cohort p90. Neither is a round number
    someone liked. Reached only by a wallet far below or far above its cohort, with enough
    evidence that shrinkage does not pull it back."""
    low = _artifact(_obs([(0.01, False, 0.01)] * 80), cohort=math.log1p(3.0))
    high = _artifact(_obs([(6.0, False, 6.0)] * 80), cohort=math.log1p(0.02))
    assert low["tp_pct"] == pytest.approx(15.0)
    assert high["tp_pct"] == pytest.approx(60.0)


def test_the_clamp_is_a_backstop_and_not_the_primary_control():
    """A3 forbids the clamp being what decides. Under the withdrawn EV rule two thirds of real
    wallet-chains landed on a bound, and that is how the error surfaced. On a realistic spread of
    wallets around their cohort, every fit must land strictly inside."""
    cohort = math.log1p(0.30)
    fits = [_artifact(_obs([(p, False, 0.0)] * 8), cohort=cohort)["tp_pct"]
            for p in (0.05, 0.12, 0.20, 0.30, 0.45, 0.70, 1.10)]
    assert all(15.0 < t < 60.0 for t in fits), fits


# ---------------------------------------------------------------- the fit-window guard (A6)

def test_the_arm_cannot_price_a_position_the_profile_was_fitted_on(tmp_path):
    """THE GUARD. A profile fitted on events before the cutoff must never price a position that
    opened before it — that grades the profile on data that helped build it, which is exactly the
    in-sample error that read 1.43x where the honest number was 1.12x. It RAISES rather than
    falling back, because a quiet fallback would hide the violation it exists to catch.
    """
    path = tmp_path / "profile_matched_v1.json"
    path.write_text(json.dumps({
        "fit_window_end": "2026-08-29T00:00:00+00:00",
        "artifacts": [{"source_user_id": "w", "chain": "solana", "tp_pct": 30.0,
                       "fallback": False, "fit_window_end": "2026-08-29T00:00:00+00:00",
                       "peak_space": "executable", "n_observed": 5, "n_censored": 1,
                       "shrinkage_weight": 0.5, "shrinkage_ratio": 5.2,
                       "dataset_sha256": "x", "stop_at_fit": 35.0}]}))
    pf._CACHE.clear()
    assert pf.profile_for("w", "solana", opened_at="2026-08-28T23:59:59+00:00", path=path) is None
    assert pf.profile_for("w", "solana", opened_at="2026-08-29T00:00:01+00:00", path=path) is None


def test_a_missing_artifact_file_falls_back_rather_than_raising(tmp_path):
    pf._CACHE.clear()
    assert pf.profile_for("w", "solana", opened_at="2026-08-29T01:00:00+00:00",
                          path=tmp_path / "absent.json") is None


def test_an_unknown_wallet_falls_back(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"fit_window_end": "2026-08-29T00:00:00+00:00", "artifacts": []}))
    pf._CACHE.clear()
    assert pf.profile_for("nobody", "solana", opened_at="2026-08-29T01:00:00+00:00",
                          path=path) is None
