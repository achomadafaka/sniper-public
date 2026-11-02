"""Phase 2B — pure performance evaluator: Wilson CI, Brier, per-component scoring, residual split,
paired-vs-C1, regime breakdown, and data-driven recommendations."""
from sniper.algos.polymarket_crypto5m import quant_eval as QEV


def test_wilson_ci():
    lo, hi = QEV.wilson_ci(8, 10)
    assert 0 < lo < 0.8 < hi < 1.0
    assert QEV.wilson_ci(0, 0) == (None, None)


def test_brier():
    assert QEV.brier([(1.0, 1), (0.0, 0)]) == 0.0
    assert QEV.brier([(0.5, 1), (0.5, 0)]) == 0.25
    assert QEV.brier([]) is None


def _rec(side, winner, qmkt, comps, won=None, vol_regime="normal", net=0.02):
    return {"actionable_side": side, "c1_side": ("Up" if qmkt >= 0.5 else "Down"),
            "c0_side": "Up", "winner": winner, "q_market": qmkt, "q_trade": comps.get("q_market"),
            "components": comps, "vol_regime": vol_regime, "session": "us",
            "disagreement_regime": "calm", "net_edge_f1": net}


def test_evaluate_cohort_overall_and_residual_split():
    recs = [
        _rec("Up", "Up", 0.7, {"q_market": 0.7, "q_struct_realized": 0.8}),    # agree, hit
        _rec("Up", "Up", 0.4, {"q_market": 0.4, "q_struct_realized": 0.9}),    # differ, hit (residual)
        _rec("Down", "Up", 0.3, {"q_market": 0.3, "q_struct_realized": 0.2}),  # agree, miss
    ]
    r = QEV.evaluate_cohort(recs)
    assert r["n"] == 3 and r["hits"] == 2
    assert r["agree_market"]["n"] == 2 and r["agree_market"]["hits"] == 1
    assert r["differ_market"]["n"] == 1 and r["differ_market"]["hits"] == 1
    # paired vs C1: c1 favorite sides are Up,Down,Down ; winner Up,Up,Up -> c1 hits = 1
    assert r["c1_hits"] == 1 and r["c1_n"] == 3


def test_evaluate_cohort_per_component_brier():
    # q_struct_realized perfectly predicts; q_market is anti-correlated -> struct lower brier
    recs = [
        _rec("Up", "Up", 0.2, {"q_market": 0.2, "q_struct_realized": 0.95}),
        _rec("Up", "Up", 0.1, {"q_market": 0.1, "q_struct_realized": 0.90}),
        _rec("Down", "Down", 0.8, {"q_market": 0.8, "q_struct_realized": 0.05}),
    ]
    r = QEV.evaluate_cohort(recs)
    comp = r["components"]
    assert comp["q_struct_realized"]["brier"] < comp["q_market"]["brier"]
    assert comp["q_struct_realized"]["n"] == 3


def test_recommendations_flag_small_sample():
    r = QEV.evaluate_cohort([_rec("Up", "Up", 0.7, {"q_market": 0.7})])
    assert any("insufficient" in rec.lower() for rec in r["recommendations"])


def test_evaluate_empty():
    r = QEV.evaluate_cohort([])
    assert r["n"] == 0 and r["recommendations"]


def test_leadlag_signal_leads_market():
    # signal sign perfectly predicts the market's next move -> agreement 1.0
    pairs = [(0.1, 0.05), (-0.2, -0.08), (0.15, 0.03), (-0.05, -0.01)]
    r = QEV.leadlag(pairs)
    assert r["n"] == 4 and r["agreement_rate"] == 1.0


def test_leadlag_no_relationship():
    pairs = [(0.1, -0.05), (-0.2, 0.08), (0.15, -0.03), (-0.05, 0.01)]
    assert QEV.leadlag(pairs)["agreement_rate"] == 0.0   # signal anti-predicts here


def test_leadlag_skips_zero_moves():
    pairs = [(0.1, 0.0), (0.0, 0.05), (0.2, 0.04)]       # only the last is usable
    r = QEV.leadlag(pairs)
    assert r["n"] == 1 and r["agreement_rate"] == 1.0


def test_leadlag_empty():
    assert QEV.leadlag([])["n"] == 0
