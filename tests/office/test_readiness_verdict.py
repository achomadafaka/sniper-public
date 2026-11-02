"""Readiness verdict (prereg docs/audits/2026-10-28-office-readiness-preregistration.md, Amendment 2)."""
import math

import pytest

from sniper.office.readiness import verdict as V

K_OK = frozenset(range(1, 11))


def coin(i, y, n, day, entered=True, y_cens=False, n_cens=False):
    return V.Coin(f"c{i}", day, y, n, entered, y_cens, n_cens)


def epoch(coins, **kw):
    base = dict(k=1, delta0=1.0, delta1=3.0, clip=100.0, loss_bound=110.0, n_upper=None, cost=0.0,
                exec_bound_present=True, counters={}, calibrated_ks=K_OK, seed=7, min_days=14, n_plan=30,
                draws_cap=None, alpha_scale=1.0, block_days=3, day_method="t_block")
    base.update(kw)
    k = base["k"]
    a = V.alpha_k(k) if 1 <= k <= V.K_MAX else None
    if "proofs" not in kw:
        base["proofs"] = dict(
            metric_registry_hash="r1", calibration_proof_id="cal1", stopping_rule="single_look", cost_rule_id="r",
            exec_bound_evidence_id="b1", alpha_support=[1, 2, 3], n_plan=base["n_plan"],
            design=dict(alpha_scale=base["alpha_scale"], day_method=base["day_method"], block_days=base["block_days"],
                        delta0=base["delta0"], delta1=base["delta1"], n_coins=base["n_plan"], days=base["min_days"],
                        alpha_k=a, draws=V.draws_for(a) if a else None))
    return V.Epoch(coins=tuple(coins), **base)


def clean(effect, n_coins=240, days=16, sd=4.0):
    import random
    r = random.Random(1)
    out = []
    for i in range(n_coins):
        y = effect + r.gauss(0, sd)
        out.append(coin(i, y, r.gauss(0, sd), f"2026-11-{1 + i % days:02d}"))
    return out


def test_alpha_k_spends_below_005_and_caps_at_10():
    assert V.alpha_k(1) == pytest.approx(0.025)
    assert sum(V.alpha_k(k) for k in range(1, 11)) < 0.05
    assert V.alpha_k(10) == pytest.approx(0.05 / 110)


def test_draws_are_tied_to_alpha():
    assert V.draws_for(0.025) == 10000
    assert V.draws_for(V.alpha_k(10)) == math.ceil(200 / V.alpha_k(10))


def test_n_plan_formula():
    a = V.alpha_k(1)
    z = V.z
    assert V.n_plan(sigma=4.0, delta0=1.0, delta1=3.0, k=1) == math.ceil(((z(1 - a) + z(0.80)) * 4.0 / 2.0) ** 2)


def test_k_above_kmax_is_no_verdict():
    r = V.evaluate(epoch(clean(5.0), k=11))
    assert r.status == "no_verdict" and "k_above_kmax" in r.reasons


def test_missing_cost_bound_cost_unknown_and_counters_are_no_verdict():
    assert "no_execution_cost_bound" in V.evaluate(epoch(clean(5.0), exec_bound_present=False)).reasons
    assert "office_cost_unknown" in V.evaluate(epoch(clean(5.0), cost=None)).reasons
    assert "counter:duplicate_fills" in V.evaluate(epoch(clean(5.0), counters={"duplicate_fills": 1})).reasons
    assert "calibration_missing_for_k" in V.evaluate(epoch(clean(5.0), calibrated_ks=frozenset())).reasons


def test_fewer_than_14_days_is_no_verdict():
    r = V.evaluate(epoch(clean(5.0, days=10)))
    assert r.status == "no_verdict" and "fewer_than_min_days" in r.reasons


def test_clear_effect_passes_and_effect_at_delta0_fails():
    assert V.evaluate(epoch(clean(8.0))).status == "pass"
    r = V.evaluate(epoch(clean(1.0)))
    assert r.status == "fail"


def test_censored_entered_coin_is_counted_as_loss_not_dropped():
    coins = clean(8.0, n_coins=60, days=16)
    coins = [coin(i, 0.0, c.n, c.day, True, y_cens=True) if i < 25 else c for i, c in enumerate(coins)]
    r = V.evaluate(epoch(coins, loss_bound=110.0))
    assert r.status == "no_verdict" and "censored_share_above_20pct" in r.reasons
    coins2 = clean(8.0, n_coins=240, days=16)
    coins2 = [coin(i, 0.0, c.n, c.day, True, y_cens=True) if i < 20 else c for i, c in enumerate(coins2)]
    r2 = V.evaluate(epoch(coins2, loss_bound=110.0))
    assert r2.details["office_net_mean_adverse"] < r2.details["office_net_mean_observed_only"] - 4.0


def test_censored_null_without_enforceable_cap_gives_no_paired_lift_verdict():
    coins = clean(8.0)
    coins = [coin(i, c.y, 0.0, c.day, True, n_cens=True) if i < 5 else c for i, c in enumerate(coins)]
    r = V.evaluate(epoch(coins, n_upper=None))
    assert r.status == "no_verdict" and "paired_lift_unbounded" in r.reasons
    r2 = V.evaluate(epoch(coins, n_upper=50.0))
    assert "paired_lift_unbounded" not in r2.reasons


def test_top3_concentration_blocks_a_pass():
    coins = [coin(i, 0.2, 0.0, f"2026-11-{1 + i % 16:02d}") for i in range(240)]
    coins[0] = coin(0, 400.0, 0.0, "2026-11-01")
    coins[1] = coin(1, 400.0, 0.0, "2026-11-02")
    coins[2] = coin(2, 400.0, 0.0, "2026-11-03")
    r = V.evaluate(epoch(coins, delta0=0.0, delta1=1.0))
    assert r.status != "pass" and "top3_share_ge_50pct" in r.reasons


def test_fewer_than_three_positive_days_blocks_a_pass():
    coins = []
    for i in range(240):
        day = f"2026-11-{1 + i % 16:02d}"
        y = 9.0 if i % 16 < 2 else -0.5
        coins.append(coin(i, y, 0.0, day))
    r = V.evaluate(epoch(coins, delta0=-5.0, delta1=1.0))
    assert "fewer_than_3_positive_days" in r.reasons


def test_same_seed_same_bounds():
    a = V.evaluate(epoch(clean(8.0)))
    b = V.evaluate(epoch(clean(8.0)))
    assert a.details["lb_office_net"] == b.details["lb_office_net"]


def test_day_block_bound_is_used_when_days_are_heterogeneous():
    coins = []
    for i in range(240):
        d = i % 16
        coins.append(coin(i, (12.0 if d < 8 else -2.0), 0.0, f"2026-11-{1 + d:02d}"))
    r = V.evaluate(epoch(coins, delta0=0.0, delta1=1.0))
    assert r.details["lb_office_net_day"] <= r.details["lb_office_net_coin"] + 1e-9
    assert r.details["lb_office_net"] == min(r.details["lb_office_net_day"], r.details["lb_office_net_coin"])


def test_block_bootstrap_is_wider_than_iid_day_bootstrap_on_serial_days():
    import numpy as np
    rng = np.random.default_rng(3)
    days = 16
    u = np.zeros(days)
    for d in range(1, days):
        u[d] = 0.8 * u[d - 1] + 0.6 * rng.standard_normal()
    x = np.array([u[i % days] * 3 + rng.standard_normal() for i in range(240)])
    ids = np.array([f"d{i % days:02d}" for i in range(240)])
    wide = [V._lower_bound_day(x, ids, 0.025, 4000, np.random.default_rng(s), 4) for s in range(5)]
    narrow = [V._lower_bound_day(x, ids, 0.025, 4000, np.random.default_rng(s), 1) for s in range(5)]
    assert sum(wide) / 5 < sum(narrow) / 5


def test_unvalidated_fixture_cannot_pass():
    c = clean(8.0)
    assert V.evaluate(epoch(c)).status == "pass"
    for drop in ("metric_registry_hash", "calibration_proof_id", "stopping_rule", "cost_rule_id",
                 "exec_bound_evidence_id"):
        e = epoch(c)
        pr = {k: v for k, v in e.proofs.items() if k != drop}
        r = V.evaluate(V.Epoch(**{**e.__dict__, "proofs": pr}))
        assert r.status == "no_verdict" and f"proof_missing:{drop}" in r.reasons
    e = epoch(c)
    assert "proofs_missing" in V.evaluate(V.Epoch(**{**e.__dict__, "proofs": None})).reasons
    bad = V.Epoch(**{**e.__dict__, "proofs": {**e.proofs, "alpha_support": [2, 3]}})
    assert "proof_alpha_support_lacks_k" in V.evaluate(bad).reasons
    for key, val in (("alpha_scale", 0.1), ("day_method", "boot"), ("block_days", 5), ("delta1", 9.0), ("days", 30),
                     ("n_coins", 99), ("draws", 1), ("alpha_k", 0.01)):
        bad = V.Epoch(**{**e.__dict__, "proofs": {**e.proofs, "design": {**e.proofs["design"], key: val}}})
        assert f"proof_design_mismatch:{key}" in V.evaluate(bad).reasons, key
    assert "proof_missing:design" in V.evaluate(V.Epoch(**{**e.__dict__, "proofs": {k: v for k, v in e.proofs.items() if k != "design"}})).reasons
    assert "draws_cap_forbidden" in V.evaluate(V.Epoch(**{**e.__dict__, "draws_cap": 1})).reasons


def test_flat_cost_is_800_per_month_by_utc_day():
    assert V.flat_office_cost(["2026-11-01"]) == pytest.approx(800 / 30)
    assert V.flat_office_cost(["2026-10-31", "2026-11-01"]) == pytest.approx(800 / 31 + 800 / 30)
    assert V.flat_office_cost([f"2026-11-{d:02d}" for d in range(1, 31)]) == pytest.approx(800.0)
    assert V.flat_office_cost(["2028-02-29"]) == pytest.approx(800 / 29)


def test_epoch_days_are_every_utc_day_open_for_any_part_no_proration():
    import datetime as dt
    ts = lambda y, m, d, h, mi: dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp()
    assert V.epoch_days(ts(2026, 10, 31, 23, 59), ts(2026, 11, 1, 0, 1)) == ["2026-10-31", "2026-11-01"]
    assert V.epoch_days(ts(2026, 11, 3, 6, 0), ts(2026, 11, 3, 18, 0)) == ["2026-11-03"]
    # two consecutive evaluated epochs sharing a day each pay it in full
    a = V.flat_office_cost(V.epoch_days(ts(2026, 11, 3, 1, 0), ts(2026, 11, 3, 12, 0)))
    b = V.flat_office_cost(V.epoch_days(ts(2026, 11, 3, 12, 0), ts(2026, 11, 5, 1, 0)))
    assert a == pytest.approx(800 / 30) and b == pytest.approx(3 * 800 / 30)
    with pytest.raises(ValueError):
        V.epoch_days(10.0, 5.0)


def test_close_at_exact_midnight_does_not_charge_the_new_day():
    import datetime as dt
    ts = lambda y, m, d, h, mi: dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc).timestamp()
    assert V.epoch_days(ts(2026, 11, 3, 6, 0), ts(2026, 11, 4, 0, 0)) == ["2026-11-03"]
    assert V.epoch_days(ts(2026, 11, 3, 6, 0), ts(2026, 11, 4, 0, 1)) == ["2026-11-03", "2026-11-04"]
