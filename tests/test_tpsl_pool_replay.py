"""TP/SL pool replay (pre-registration docs/audits/2026-09-30-per-wallet-tp-sl-preregistration.md, A1).

Pure rules only, on synthetic paths: the cell replay, the instrument match, the chronological split
and the mint-clustered bootstrap. No ledger is read here.
"""
import pytest

from scripts.research import tpsl_pool_replay as R

COST = 100.0


def path(*pairs):
    """[(t, whole-bag value)] from (t, return %) pairs."""
    return [(float(t), COST * (1 + r / 100.0)) for t, r in pairs]


def test_tp_fires_on_the_first_point_at_or_above_it_and_fills_after_the_lag():
    p = path((0, 0), (2, 10), (4, 26), (6, 30), (8, 28))
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=1.0,
                        actual_reason="vol_death", actual_ret=5.0)
    assert out["status"] == "tp" and out["t_trigger"] == 4.0 and out["t_fill"] == 6.0
    assert out["ret"] == pytest.approx(30.0)


def test_the_stop_needs_d_consecutive_points_and_a_bounce_resets_it():
    p = path((0, 0), (2, -40), (4, -10), (6, -36), (8, -38), (10, -39))
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=2, lag_s=0.0,
                        actual_reason="vol_death", actual_ret=-39.0)
    assert out["status"] == "stop" and out["t_trigger"] == 8.0 and out["t_fill"] == 8.0


def test_an_exit_we_took_for_another_reason_is_carried_into_an_unfired_cell():
    p = path((0, 0), (2, 5), (4, -5))
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=1.0,
                        actual_reason="mirror", actual_ret=-4.0)
    assert out == {"status": "actual", "ret": -4.0, "t_trigger": None, "t_fill": None,
                   "fill_appended": False}


def test_an_unfired_cell_on_a_tp_or_stop_exit_is_censored_never_zero():
    p = path((0, 0), (2, 12), (4, 16))          # our TP at 15 fired; a tp 20 cell never saw 20
    out = R.replay_cell(p, cost=COST, tp=20, stop=35, debounce=1, lag_s=1.0,
                        actual_reason="real_tp", actual_ret=16.0)
    assert out["status"] == "censored" and out["ret"] is None


def test_a_trigger_with_no_point_after_the_lag_is_censored():
    p = path((0, 0), (2, 30))
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=5.0,
                        actual_reason="vol_death", actual_ret=30.0)
    assert out["status"] == "censored"


def test_a_fill_on_the_appended_actual_exit_is_flagged():
    p = path((0, 0), (2, 26), (3, 29))                        # (3, 29) is our actual fill, appended
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=1.0,
                        actual_reason="real_tp", actual_ret=29.0, appended_last=True)
    assert out["status"] == "tp" and out["t_fill"] == 3.0 and out["fill_appended"] is True
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=0.5,
                        actual_reason="real_tp", actual_ret=29.0, appended_last=False)
    assert out["fill_appended"] is False


def test_the_appended_exit_is_a_fill_point_only_never_a_trigger():
    """Gamma: triggers come from marks. A TP visible only on our own exit (say the hot lane fired
    between two marks) must not let the replay trigger on it and grade itself."""
    p = path((0, 0), (2, 20), (3, 26))                       # marks never reach 25; our fill did
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=0.0,
                        actual_reason="real_tp", actual_ret=26.0, appended_last=True)
    assert out["status"] == "censored" and out["t_trigger"] is None


def test_the_instrument_match_is_judged_on_the_trigger_and_on_return_only_for_mark_fills():
    """Gamma's guard: a fill on our own appended exit matches our return by construction."""
    mark = {"status": "tp", "t_trigger": 10.0, "t_fill": 11.0, "ret": 25.5, "fill_appended": False}
    assert R.instrument_match(mark, actual_reason="real_tp", t_decision=12.0, actual_ret=24.0)
    assert not R.instrument_match(mark, actual_reason="b_stop", t_decision=12.0, actual_ret=24.0)
    assert not R.instrument_match(mark, actual_reason="real_tp", t_decision=20.5, actual_ret=24.0)
    assert not R.instrument_match(mark, actual_reason="real_tp", t_decision=12.0, actual_ret=23.4)
    appended = dict(mark, ret=40.0, fill_appended=True)       # return not graded on our own fill
    assert R.instrument_match(appended, actual_reason="real_tp", t_decision=12.0, actual_ret=24.0)
    assert not R.instrument_match({"status": "censored", "t_trigger": None, "t_fill": None, "ret": None,
                                   "fill_appended": False},
                                  actual_reason="real_tp", t_decision=12.0, actual_ret=24.0)


@pytest.mark.parametrize("n, train, test", [(14, 9, 5), (12, 8, 4), (30, 20, 10), (6, 2, 4)])
def test_the_split_is_chronological_two_thirds_with_test_at_least_four(n, train, test):
    units = [{"t": float(100 - i)} for i in range(n)]          # reverse order in
    tr, te = R.chronological_split(units, key="t")
    assert (len(tr), len(te)) == (train, test)
    assert max(u["t"] for u in tr) < min(u["t"] for u in te)


def test_the_bootstrap_resamples_mints_not_units_and_is_seeded():
    diffs = [("a", 2.0), ("a", 2.0), ("b", -1.0), ("c", 3.0)]
    p1 = R.cluster_bootstrap_p(diffs, draws=4000, seed=20260930)
    assert p1 == R.cluster_bootstrap_p(diffs, draws=4000, seed=20260930)
    assert 0.0 <= p1 <= 0.5
    assert R.cluster_bootstrap_p([("a", -1.0), ("b", -2.0)], draws=500, seed=1) == 1.0


def test_tp_fires_at_exactly_the_threshold():
    out = R.replay_cell(path((0, 0), (2, 25)), cost=COST, tp=25, stop=35, debounce=1, lag_s=0.0,
                        actual_reason="vol_death", actual_ret=25.0)
    assert out["status"] == "tp" and out["t_trigger"] == 2.0


def test_a_fill_on_a_mark_is_not_flagged_appended_even_when_an_exit_is_appended():
    p = path((0, 0), (2, 26), (3, 27), (10, 29))              # (10, 29) appended
    out = R.replay_cell(p, cost=COST, tp=25, stop=35, debounce=1, lag_s=0.5,
                        actual_reason="real_tp", actual_ret=29.0, appended_last=True)
    assert out["t_fill"] == 3.0 and out["fill_appended"] is False


def test_the_split_floors_two_thirds():
    tr, te = R.chronological_split([{"t": float(i)} for i in range(16)], key="t")
    assert (len(tr), len(te)) == (10, 6)


def test_the_bootstrap_draws_whole_mints_and_counts_a_zero_mean_as_not_better():
    # Mints a (nine units at +1) and b (one at -100): draws of two mints give +1 (1/4), -9.1 (1/2),
    # -100 (1/4), so p(mean <= 0) = 0.75. Drawing units instead would give about 0.19.
    diffs = [("a", 1.0)] * 9 + [("b", -100.0)]
    assert R.cluster_bootstrap_p(diffs, draws=4000, seed=20260930) == pytest.approx(0.75, abs=0.03)
    # a +1 and b -1: {a,b} has mean exactly 0 and counts as not better, so p = 0.75, not 0.25.
    assert R.cluster_bootstrap_p([("a", 1.0), ("b", -1.0)], draws=4000, seed=7) == pytest.approx(0.75, abs=0.03)


def test_a3_own_replay_fills_on_our_exit_when_no_point_follows_the_lag():
    """A3: the series ends at our exit; the own-contract replay takes our actual fill, never a cell."""
    p = path((0, 0), (2, 26), (2.5, 27))                    # (2.5, 27) is our actual fill, appended
    kw = dict(cost=COST, tp=25, stop=35, debounce=1, lag_s=1.0, actual_reason="real_tp", actual_ret=27.0,
              appended_last=True)
    assert R.replay_cell(p, **kw)["status"] == "censored"
    out = R.replay_cell(p, own=True, **kw)
    assert out["status"] == "tp" and out["fill_appended"] is True and out["t_fill"] == 2.5


def test_bh_step_up_adjusts_and_caps():
    q = R.benjamini_hochberg([0.01, 0.04, 0.03, 0.5])
    assert [round(x, 4) for x in q] == [0.04, 0.0533, 0.0533, 0.5]
