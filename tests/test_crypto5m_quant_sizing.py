"""Phase 2A — paper sizing tracks S0 fixed, S1 frozen edge-buckets, S2 capped fractional Kelly. Pure."""
from sniper.algos.polymarket_crypto5m import quant_sizing as QS


def test_s0_fixed():
    assert QS.fixed_stake(stake=10.0) == 10.0


def test_s1_edge_buckets():
    buckets = [(0.0, 0.0), (0.02, 5.0), (0.05, 10.0), (0.10, 20.0)]
    assert QS.bucket_stake(0.005, buckets=buckets) == 0.0
    assert QS.bucket_stake(0.03, buckets=buckets) == 5.0
    assert QS.bucket_stake(0.07, buckets=buckets) == 10.0
    assert QS.bucket_stake(0.20, buckets=buckets) == 20.0


def test_s2_kelly_positive_edge_scales_with_fraction():
    # cost per share ~0.50, conservative prob 0.65 -> positive edge
    full = QS.fractional_kelly_stake(q_conservative=0.65, cost_per_share=0.50, bankroll=400.0,
                                     fraction=0.25, max_fraction_per_market=1.0,
                                     max_open_exposure=1e9, current_open_exposure=0.0)
    half = QS.fractional_kelly_stake(q_conservative=0.65, cost_per_share=0.50, bankroll=400.0,
                                     fraction=0.10, max_fraction_per_market=1.0,
                                     max_open_exposure=1e9, current_open_exposure=0.0)
    assert full["stake"] > half["stake"] > 0


def test_s2_no_edge_zero_stake():
    r = QS.fractional_kelly_stake(q_conservative=0.45, cost_per_share=0.50, bankroll=400.0,
                                  fraction=0.25, max_fraction_per_market=1.0,
                                  max_open_exposure=1e9, current_open_exposure=0.0)
    assert r["stake"] == 0.0 and r["capped_by"] == "no_edge"


def test_s2_per_market_cap_binds():
    r = QS.fractional_kelly_stake(q_conservative=0.95, cost_per_share=0.50, bankroll=400.0,
                                  fraction=1.0, max_fraction_per_market=0.05,
                                  max_open_exposure=1e9, current_open_exposure=0.0)
    assert r["stake"] == 400.0 * 0.05 and r["capped_by"] == "per_market_cap"


def test_s2_open_exposure_cap_binds():
    r = QS.fractional_kelly_stake(q_conservative=0.95, cost_per_share=0.50, bankroll=400.0,
                                  fraction=1.0, max_fraction_per_market=1.0,
                                  max_open_exposure=100.0, current_open_exposure=95.0)
    assert r["stake"] == 5.0 and r["capped_by"] == "open_exposure_cap"
