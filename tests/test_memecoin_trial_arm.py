"""Trial arm (spec docs/superpowers/specs/2026-10-01-memecoin-trial-arm-design.md, rule
docs/audits/2026-10-01-trial-arm-promotion-preregistration.md, 3a0b141ee).

A trial is one (wallet, chain) armed at a small clip. Its positions carry a marker frozen into the
funded contract; Gamma's rule grades its closes on r = realized_pnl / entry_notional and ends it as
PROMOTE, DEMOTE, INSUFFICIENT or SHADOW, or lets it continue.
"""
import pytest

from sniper.services import memecoin_trial as T

START = "2026-10-01T00:00:00+00:00"


def _closes(rs, *, mints=None, manual=(), shared=()):
    out = []
    for i, r in enumerate(rs):
        out.append({"position_id": 100 + i, "mint": (mints[i] if mints else "M%d" % i),
                    "realized_pnl": 20.0 * r, "entry_notional": 20.0,
                    "closed_at": "2026-10-01T%02d:00:00+00:00" % (1 + i),
                    "exit_reason": "manual" if i in manual else "real_tp",
                    "shared": i in shared})
    return out


def _v(closes, now="2026-10-02T00:00:00+00:00", done=()):
    return T.trial_verdict(closes, START, now, done=set(done))


# --- the rule, branch by branch (r as a fraction) ---

def test_no_checkpoint_before_five_closes():
    assert _v(_closes([0.5] * 4)) is None


def test_early_demote_at_five_when_the_mean_is_below_minus_30_percent():
    v = _v(_closes([-0.31] * 5))
    assert (v["checkpoint"], v["verdict"]) == (5, "DEMOTE")


def test_five_closes_at_exactly_minus_30_percent_continue():
    v = _v(_closes([-0.30] * 5))
    assert (v["checkpoint"], v["verdict"]) == (5, "CONTINUE")


def test_the_five_checkpoint_reads_only_the_first_five_closes():
    # six closes on the first pass: checkpoint 5 is graded on the first five, by close time
    v = _v(_closes([-0.5] * 5 + [5.0]))
    assert (v["checkpoint"], v["verdict"], v["detail"]["n"]) == (5, "DEMOTE", 5)


def test_ten_demote_below_minus_10_percent():
    v = _v(_closes([-0.11] * 10), done={5})
    assert (v["checkpoint"], v["verdict"]) == (10, "DEMOTE")


def test_ten_promote_needs_mean_over_10_without_best_positive_and_best_share_at_most_half():
    rs = [0.3, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.0, -0.05, -0.05]
    v = _v(_closes(rs), done={5})
    assert (v["checkpoint"], v["verdict"]) == (10, "PROMOTE")
    assert v["detail"]["best_share_of_positive"] == pytest.approx(0.3 / 1.2)


def test_ten_one_coin_carrying_the_mean_continues():
    rs = [2.0] + [-0.05] * 9                       # mean +0.155, without best negative
    v = _v(_closes(rs), done={5})
    assert (v["checkpoint"], v["verdict"]) == (10, "CONTINUE")


def test_the_best_coin_is_a_coin_not_a_close():
    # two closes on one coin: together they are the best coin (0.8), share 0.8/1.4 > 0.5
    rs = [0.4, 0.4, 0.2, 0.2, 0.1, 0.1, 0.0, 0.0, 0.0, 0.0]
    mints = ["A", "A", "B", "C", "D", "E", "F", "G", "H", "I"]
    v = _v(_closes(rs, mints=mints), done={5})
    assert v["detail"]["best_mint"] == "A"
    assert v["detail"]["best_share_of_positive"] == pytest.approx(0.8 / 1.4)
    assert v["verdict"] == "CONTINUE"


def test_twenty_promote_over_5_percent_else_demote():
    good = [0.2, 0.1, 0.1, 0.1, 0.05, 0.05, 0.05, 0.05, 0.0, 0.0] * 2
    v = _v(_closes(good), done={5, 10})
    assert (v["checkpoint"], v["verdict"]) == (20, "PROMOTE")
    flat = [0.05, -0.04] * 10                     # mean +0.005
    v = _v(_closes(flat), done={5, 10})
    assert (v["checkpoint"], v["verdict"]) == (20, "DEMOTE")


def test_insufficient_after_14_days_with_fewer_than_10_closes():
    v = _v(_closes([0.1] * 7), now="2026-10-15T00:00:01+00:00", done={5})
    assert v["verdict"] == "INSUFFICIENT" and v["checkpoint"] == "day14"
    assert _v(_closes([0.1] * 7), now="2026-10-14T23:59:59+00:00", done={5}) is None


def test_ten_closes_by_day_14_are_not_insufficient():
    v = _v(_closes([0.0] * 10), now="2026-10-20T00:00:00+00:00", done={5, 10})
    assert v is None


def test_shadow_overrides_promote_only():
    rs = [0.3, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.0, -0.05, -0.05]
    v = _v(_closes(rs, shared=range(5)), done={5})
    assert v["verdict"] == "SHADOW" and v["detail"]["shared_share"] == 0.5
    v = _v(_closes([-0.11] * 10, shared=range(10)), done={5})
    assert v["verdict"] == "DEMOTE"


def test_a_close_without_notional_is_listed_and_excluded_never_zero():
    closes = _closes([-0.31] * 5)
    closes.insert(0, {"position_id": 99, "mint": "Z", "realized_pnl": 3.0, "entry_notional": 0.0,
                      "closed_at": "2026-10-01T00:30:00+00:00", "exit_reason": "real_tp",
                      "shared": False})
    v = _v(closes)
    assert v["verdict"] == "DEMOTE" and v["detail"]["n"] == 5
    assert [x["position_id"] for x in v["detail"]["excluded"]] == [99]


def test_a_checkpoint_already_written_is_not_graded_again():
    assert _v(_closes([0.0] * 5), done={5}) is None


def test_manual_exits_are_marked_and_over_20_percent_reported_both_ways():
    v = _v(_closes([-0.31] * 4 + [0.9], manual={4}))
    assert [c["manual_exit"] for c in v["detail"]["closes"]] == [False] * 4 + [True]
    assert "without_manual" not in v["detail"]               # 1 of 5 = 20%, not more
    v = _v(_closes([-0.5] * 3 + [0.9, 0.9], manual={3, 4}))
    assert v["detail"]["without_manual"]["n"] == 3


def test_bootstrap_ci_by_mint_is_seeded_and_reported():
    rs = [0.3, 0.2, 0.2, 0.2, 0.1, 0.1, 0.1, 0.0, -0.05, -0.05]
    a = _v(_closes(rs), done={5})["detail"]["ci90"]
    b = _v(_closes(rs), done={5})["detail"]["ci90"]
    assert a == b and a[0] < 0.105 < a[1]


def test_ten_closes_between_5_and_10_percent_continue_not_promote():
    rs = [0.2, 0.15, 0.1, 0.1, 0.1, 0.05, 0.05, 0.0, 0.0, -0.05]       # mean +0.07
    v = _v(_closes(rs), done={5})
    assert v["detail"]["mean"] == pytest.approx(0.07) and v["verdict"] == "CONTINUE"


def test_a_best_coin_share_of_exactly_one_half_still_promotes():
    """Rule 3a0b141ee: share <= 50% (Gamma L1 on 92646580c pins the boundary)."""
    rs = [0.75, 0.5, 0.25] + [0.0] * 7               # binary-exact: best 0.75 of positive 1.5
    v = _v(_closes(rs), done={5})
    assert v["detail"]["best_share_of_positive"] == 0.5 and v["verdict"] == "PROMOTE"
