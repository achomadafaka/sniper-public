"""Tests for robust wallet selection (sniper/hyperlive/copy_selection.py)."""
from sniper.hyperlive import copy_selection as SEL


def test_sign_stable_true_when_both_halves_positive():
    assert SEL.split_half_sign_stable([10, 20, 15, 30, 12, 25]) is True


def test_sign_stable_false_for_front_loaded_winner():
    # good first half, bleeds second half -> not stable
    assert SEL.split_half_sign_stable([300, 200, 250, -200, -180, -220]) is False


def test_sign_stable_none_when_too_few():
    assert SEL.split_half_sign_stable([10, 20]) is None


def test_wallet_score_uses_median_not_mean():
    s = SEL.wallet_score([769, -8, -8, -8, -8, -8, -8, -8])   # one outlier, mostly negative
    assert s["mean"] > 0 and s["median"] < 0                  # median exposes the truth
    assert s["n"] == 8 and s["eligible"] is True


def test_classify_cull_catastrophic_with_few_closes():
    s = SEL.wallet_score([-200, -300, -250, -400, -180])       # n=5, deeply negative
    assert SEL.classify(s) == "cull"


def test_classify_promote_requires_evidence_and_stability():
    # strong + stable + enough n -> promote
    good = SEL.wallet_score([40, 50, 45, 60, 42, 55, 48, 52])
    assert SEL.classify(good) == "promote"
    # same strength but front-loaded (not sign-stable) -> hold, not promote
    shaky = SEL.wallet_score([300, 250, 280, 260, -10, -20, -5, -15])
    assert SEL.classify(shaky) == "hold"


def test_classify_hold_when_thin():
    # positive but only 4 closes -> not enough to promote (promote_min_n=8), not cull
    assert SEL.classify(SEL.wallet_score([40, 50, 45, 60])) == "hold"


def test_rank_orders_by_median_and_labels_actions():
    data = {
        "0xgood": [40, 50, 45, 60, 42, 55, 48, 52],     # promote
        "0xbad": [-200, -300, -250, -400, -180],         # cull
        "0xmid": [5, -5, 10, -8, 3],                      # hold
    }
    r = SEL.rank(data, min_n=5)
    assert r[0]["wallet"] == "0xgood" and r[0]["action"] == "promote"
    assert r[-1]["wallet"] == "0xbad" and r[-1]["action"] == "cull"
    actions = {d["wallet"]: d["action"] for d in r}
    assert actions["0xmid"] == "hold"


def test_bootstrap_p_positive_low_for_clean_positive():
    p = SEL.bootstrap_p_positive([40, 50, 45, 60, 42, 55], iters=3000, seed=1)
    assert p is not None and p < 0.05
    assert SEL.bootstrap_p_positive([10, -12], iters=1000) is None   # too few


def test_fdr_bh_corrects_multiple_looks():
    # one genuinely small p among many nulls should still pass; many marginal ones should not
    assert SEL.fdr_bh([0.001, 0.5, 0.6, 0.7], q=0.10) == [True, False, False, False]
    # a batch of marginal p's (best of many looks) -> none survive
    assert SEL.fdr_bh([0.06, 0.07, 0.09, 0.11, 0.2], q=0.10) == [False, False, False, False, False]


def test_rank_attaches_fdr_flag():
    data = {"0xa": [40, 50, 45, 60, 42, 55, 48, 52], "0xb": [-40, -50, -45, -60, -42]}
    r = SEL.rank(data, min_n=5)
    for w in r:
        assert "p_positive" in w and "fdr_significant" in w
