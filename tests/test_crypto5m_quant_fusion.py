"""Phase 2A — multi-source latent-state fusion (pure). Robust weighted-median composite of BTC price
across exchanges, with disagreement + uncertainty + degraded-state handling."""
from sniper.algos.polymarket_crypto5m import quant_fusion as QF


def _src(name, price, age=200, stale=False, gap=False, tracking_error=None):
    return {"source": name, "price": price, "source_age_ms": age, "stale": stale, "gap": gap,
            "tracking_error": tracking_error}


def test_agreeing_sources_low_uncertainty():
    s = [_src("binance", 63850.0), _src("coinbase", 63848.0), _src("kraken", 63852.0)]
    r = QF.fuse_latent_state(s)
    assert r["state_quality"] == "ok"
    assert 63845 <= r["latent_btc_price"] <= 63855
    assert r["source_disagreement_abs"] <= 6.0
    assert r["n_usable"] == 3
    assert abs(sum(r["source_contribution_weights"].values()) - 1.0) < 1e-9


def test_weighted_median_is_robust_to_one_outlier():
    s = [_src("binance", 63850.0), _src("coinbase", 63848.0), _src("kraken", 63851.0),
         _src("badfeed", 70000.0)]               # outlier must not drag the latent up to ~66k
    r = QF.fuse_latent_state(s)
    assert r["latent_btc_price"] < 64200                    # stays in the real cluster
    assert r["source_disagreement_abs"] > 1000              # disagreement flagged
    assert r["state_quality"] == "degraded"


def test_stale_and_gapped_sources_dropped():
    s = [_src("binance", 63850.0, stale=True), _src("coinbase", 63848.0, gap=True),
         _src("kraken", 63851.0)]
    r = QF.fuse_latent_state(s)
    assert r["n_usable"] == 1 and r["state_quality"] == "single_source"
    assert r["latent_btc_price"] == 63851.0
    assert r["source_contribution_weights"] == {"kraken": 1.0}


def test_all_unusable_returns_no_latent():
    s = [_src("binance", 63850.0, stale=True), _src("coinbase", 63848.0, age=99999)]
    r = QF.fuse_latent_state(s, max_age_ms=2000)
    assert r["state_quality"] == "unusable" and r["latent_btc_price"] is None
    assert r["n_usable"] == 0
