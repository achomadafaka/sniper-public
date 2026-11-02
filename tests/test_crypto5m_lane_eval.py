"""Quant lane harness (scripts/crypto5m_lane_eval.py): lane selectors + the bootstrap/FDR stats engine.
Locks the methodology that keeps a ~20-lane grid from manufacturing a fake winner."""
import random

import scripts.crypto5m_lane_eval as LE


def _d(qm, up_ask, down_ask, **kw):
    d = {"q_market": qm, "up_ask": up_ask, "down_ask": down_ask}
    d.update(kw)
    return d


def _rec(cp, feat, win, ts):
    return {cp: feat, "_ts": {cp: ts}, "_win": win}


def test_roi_net_fee_on_entry():
    assert abs(LE._roi_net(0.5, 1) - (1.0 - 0.07 * 0.5)) < 1e-9      # win: (1/.5-1) - fee
    assert abs(LE._roi_net(0.5, 0) - (-1.0 - 0.07 * 0.5)) < 1e-9     # lose: -1 - fee


def test_sig_agreement():
    d = _d(0.62, 0.62, 0.40, q_struct=0.55, q_mc=0.45)
    assert LE._sig_agrees_fav(d, "q_struct") is True                 # both lean Up
    assert LE._sig_agrees_fav(d, "q_mc") is False                    # mc leans Down vs Up favorite
    assert LE._sig_agrees_fav(_d(0.62, 0.62, 0.40), "q_struct") is None  # missing signal


def test_consensus_lane_requires_all_signals():
    cp = 240
    agree = _rec(cp, _d(0.62, 0.62, 0.40, q_trade=0.60, q_struct=0.58, q_mc=0.55), "Up", 0)
    assert LE._fav_consensus_at(cp)(agree) == (cp, "Up", 0.62)       # all 3 agree -> picks favorite
    one_off = _rec(cp, _d(0.62, 0.62, 0.40, q_trade=0.60, q_struct=0.58, q_mc=0.40), "Up", 0)
    assert LE._fav_consensus_at(cp)(one_off) is None                 # mc disagrees -> no pick


def test_underdog_ofi_reversal_lane():
    cp = 240
    # favorite Up (qm .7), underdog Down @ .30 with strong buy pressure on the Down token
    hit = _rec(cp, _d(0.70, 0.72, 0.30, down_ofi=0.5, up_ofi=-0.1), "Down", 0)
    assert LE._dog_ofi_reversal_at(cp)(hit) == (cp, "Down", 0.30)
    no_flow = _rec(cp, _d(0.70, 0.72, 0.30, down_ofi=0.05, up_ofi=0.0), "Down", 0)
    assert LE._dog_ofi_reversal_at(cp)(no_flow) is None              # weak dog flow -> skip


def test_bh_fdr_screens_noise():
    survive = LE.bh_fdr([("real", 0.001), ("noise1", 0.6), ("noise2", 0.9), ("noise3", 0.4)], q=0.10)
    assert "real" in survive and "noise1" not in survive and len(survive) == 1


def test_min_n_gate_blocks_small_samples():
    rng = random.Random(0)
    kept = [("w%d" % i, 0.2, 0.8, 1) for i in range(10)]              # n=10 < MIN_N_OOS
    out = LE.lane_significance("A", kept, [], rng)
    assert out["status"] == "accruing" and out["pval"] is None


def test_family_a_gap_promising_when_skipped_is_worse():
    rng = random.Random(0)
    # kept (lane) wins every window at 0.8; skipped (C1 on dropped windows) loses every window at 0.8.
    kept = [("k%d" % i, LE._roi_net(0.8, 1), 0.8, 1) for i in range(60)]
    skipped = [("s%d" % i, LE._roi_net(0.8, 0), 0.8, 0) for i in range(60)]
    out = LE.lane_significance("A", kept, skipped, rng)
    assert out["n"] == 60 and out["gap_pct"] > 0 and out["gap_ci_lo"] > 0 and out["pval"] < 0.05


def test_family_b_roi_vs_zero():
    rng = random.Random(0)
    losing = [("w%d" % i, LE._roi_net(0.3, 0), 0.3, 0) for i in range(50)]   # underdog always loses
    out = LE.lane_significance("B", losing, [], rng)
    assert out["net_roi_pct"] < 0 and out["gap_ci_hi"] < 0 and out["pval"] > 0.5   # clearly -EV, not promising
