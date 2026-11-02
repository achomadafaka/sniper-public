from sniper.algos.hl_microstructure import persistence as P
from sniper.algos.hl_microstructure.experts import build_expert_signals
from sniper.algos.hl_microstructure.models import FeatureSnapshot


def _snapshot(ts=10_000, mid=100.0, **overrides):
    features = {
        "mid": mid,
        "spread_bps": 4.0,
        "book_imbalance_5": 0.34,
        "flow_imbalance_5s": 0.60,
        "flow_imbalance_10s": 0.45,
        "flow_imbalance_30s": 0.10,
        "trade_count_5s": 4,
        "trade_count_10s": 8,
        "trade_count_30s": 16,
        "return_5s": 0.0010,
        "return_15s": 0.0015,
        "return_30s": 0.0020,
        "realized_vol_30s": 0.001,
        "funding": -0.00008,
        "premium": -0.002,
    }
    features.update(overrides)
    return FeatureSnapshot("HYPE", ts, "hlm_fv1", features, "ok", [])


def test_expert_signals_cover_multiple_horizons_and_sources():
    signals = build_expert_signals(_snapshot())
    ids = {s.expert_id for s in signals}

    assert "flow_5s" in ids
    assert "flow_10s" in ids
    assert "momentum_15s" in ids
    assert "carry_funding" in ids
    assert all(s.side in ("long", "short") for s in signals)
    assert {s.horizon_ms for s in signals} >= {5_000, 10_000, 15_000, 30_000}


def test_expert_reliability_scales_current_signals(tmp_path):
    conn = P.connect(str(tmp_path / "hlm.sqlite"))
    P.ensure_schema(conn)
    snap = _snapshot()
    signals = build_expert_signals(snap)
    P.record_expert_predictions(conn, snap, signals)
    P.settle_due_expert_predictions(conn, asset="HYPE", decision_ts_ms=45_000, mid=101.0)
    reliability = P.expert_reliability(conn)

    scaled = build_expert_signals(_snapshot(ts=50_000), reliability=reliability)
    flow = next(s for s in scaled if s.expert_id == "flow_5s")

    assert reliability["flow_5s"]["n"] == 1
    assert reliability["flow_5s"]["accuracy"] == 1.0
    assert flow.reliability == 1.0
    assert flow.confidence > 0.55


def test_settled_bad_expert_is_damped(tmp_path):
    conn = P.connect(str(tmp_path / "hlm.sqlite"))
    P.ensure_schema(conn)
    snap = _snapshot()
    signals = build_expert_signals(snap)
    P.record_expert_predictions(conn, snap, signals)
    P.settle_due_expert_predictions(conn, asset="HYPE", decision_ts_ms=45_000, mid=99.0)
    reliability = P.expert_reliability(conn)

    scaled = build_expert_signals(_snapshot(ts=50_000), reliability=reliability)
    flow = next(s for s in scaled if s.expert_id == "flow_5s")

    assert reliability["flow_5s"]["accuracy"] == 0.0
    assert flow.reliability == 0.0
    assert flow.confidence < 0.35
