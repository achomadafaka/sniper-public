"""Phase 2B — unified per-window signal panel: fuse multi-source latent state, structural (realized +
implied vol), microstructure, basis/lead-lag, vol regime, and C0/C1 benchmarks into one dict."""
from sniper.algos.polymarket_crypto5m import quant_signals as QSG

SOURCES = [
    {"source": "binance", "price": 63850.0, "source_age_ms": 400, "stale": False, "gap": False},
    {"source": "coinbase", "price": 63846.0, "source_age_ms": 200, "stale": False, "gap": False},
    {"source": "kraken", "price": 63849.0, "source_age_ms": 240, "stale": False, "gap": False},
    {"source": "deribit", "price": 63852.0, "source_age_ms": 260, "stale": False, "gap": False},
]
UP_BOOK = {"best_bid": 0.58, "best_ask": 0.60, "bids": [[0.58, 500]], "asks": [[0.60, 500]]}
DOWN_BOOK = {"best_bid": 0.38, "best_ask": 0.40, "bids": [[0.38, 300]], "asks": [[0.40, 700]]}


def _panel(**kw):
    base = dict(condition_id="0xabc", sources=SOURCES, up_book=UP_BOOK, down_book=DOWN_BOOK,
                binance_start=63800.0, binance_now=63850.0, realized_vol_per_s=0.00015,
                seconds_remaining=60, implied_vol_per_s=0.00012, hour=18)
    base.update(kw)
    return QSG.build_signal_panel(**base)


def test_panel_core_fields():
    p = _panel()
    assert p["fusion"]["state_quality"] == "ok"
    # q_market from BOTH books: (up_mid 0.59 + (1 - down_mid 0.39)) / 2 = 0.60
    assert abs(p["q_market"] - 0.60) < 1e-9
    assert p["q_struct_realized"] > 0.5            # price rose vs window start -> Up favored
    assert p["q_struct_implied"] is not None
    assert p["c1_side"] == "Up"                    # q_market 0.60 > 0.5 -> favorite Up
    assert p["c0_side"] in ("Up", "Down")
    assert p["session"] == "us"


def test_components_only_include_available():
    p = _panel(implied_vol_per_s=None, q_mc=None, q_residual=None)
    assert set(p["components"]) == {"q_market", "q_struct_realized"}
    p2 = _panel(q_mc=0.7, q_residual=0.66)
    assert {"q_market", "q_struct_realized", "q_struct_implied", "q_mc", "q_residual"} <= set(p2["components"])


def test_ensemble_components_decorrelated():
    # ensemble gets market + ONE structural vote (+residual); implied-vol & MC excluded (correlated)
    p = _panel(q_mc=0.7, q_residual=0.66)
    assert set(p["ensemble_components"]) == {"q_market", "q_structural", "q_residual"}
    assert "q_struct_implied" not in p["ensemble_components"] and "q_mc" not in p["ensemble_components"]
    assert abs(p["ensemble_components"]["q_structural"] - p["q_struct_realized"]) < 1e-6


def test_vol_ratio_and_regime():
    p = _panel(realized_vol_per_s=0.0003, implied_vol_per_s=0.00015)   # realized 2x implied
    assert abs(p["vol_ratio"] - 2.0) < 1e-6
    assert p["vol_regime"] == "realized_hot"


def test_structural_minus_market_and_basis():
    p = _panel()
    assert p["structural_minus_market"] == round(p["q_struct_realized"] - p["q_market"], 6)
    assert p["cross_source_basis_bps"] == p["fusion"]["source_disagreement_bps"]


def test_microstructure():
    p = _panel()
    assert p["down_order_flow_imbalance"] == round((300 - 700) / 1000.0, 6)   # down book bid-heavy? ask-heavy


def test_crossed_book_rejected_not_read_as_half():
    crossed_up = {"best_bid": 0.70, "best_ask": 0.60, "bids": [[0.70, 100]], "asks": [[0.60, 100]]}
    p = _panel(up_book=crossed_up)
    # up book is crossed -> _mid None -> q_market falls back to (1 - down_mid) = 1 - 0.39 = 0.61
    assert abs(p["q_market"] - 0.61) < 1e-9


def test_degraded_ensemble_flagged_when_only_market():
    # no binance path -> no structural -> ensemble is market-only -> degraded
    p = QSG.build_signal_panel(condition_id="0x1", sources=SOURCES, up_book=UP_BOOK, down_book=DOWN_BOOK,
                               binance_start=None, binance_now=None, realized_vol_per_s=None,
                               seconds_remaining=60)
    assert p["n_ensemble_components"] == 1 and p["ensemble_degraded"] is True
    assert set(p["ensemble_components"]) == {"q_market"}


def test_market_coherent_flag():
    # UP_BOOK mid 0.59, DOWN_BOOK mid 0.39 -> sum 0.98, within tolerance -> coherent
    assert _panel()["market_coherent"] is True
    # both books rich (Up and Down both ~0.71) -> sum ~1.42 -> incoherent
    rich = {"best_bid": 0.70, "best_ask": 0.72, "bids": [[0.70, 100]], "asks": [[0.72, 100]]}
    p = QSG.build_signal_panel(condition_id="0x1", sources=SOURCES, up_book=rich, down_book=rich,
                               binance_start=63800.0, binance_now=63850.0, realized_vol_per_s=0.00015,
                               seconds_remaining=60)
    assert p["market_coherent"] is False


def test_c0_deterministic_by_condition():
    a = QSG.build_signal_panel(condition_id="0xfixed", sources=SOURCES, up_book=UP_BOOK,
                               down_book=DOWN_BOOK, binance_start=1.0, binance_now=1.0,
                               realized_vol_per_s=0.0001, seconds_remaining=60)
    b = QSG.build_signal_panel(condition_id="0xfixed", sources=SOURCES, up_book=UP_BOOK,
                               down_book=DOWN_BOOK, binance_start=1.0, binance_now=1.0,
                               realized_vol_per_s=0.0001, seconds_remaining=60)
    assert a["c0_side"] == b["c0_side"]
