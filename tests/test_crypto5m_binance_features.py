"""Phase 1B Deliverable #5 — feature family C (Binance-reframed, known-at-decision, continuous).

Contracts: returns/vol from price (any source); volume/VWAP/flow ONLY when a volume-bearing obs exists
(None for price-only RTDS, never fabricated); strict known-at-time cutoff; trend/reversion are continuous
numbers (the legacy categorical survives ONLY as a diagnostic field).
"""
from sniper.algos.polymarket_crypto5m import binance_features as BFE

DEC = 1000          # decision_ts / cutoff_ts (s); now_ms = 1_000_000


def _obs(ts_ms, price, *, qty=None, quote=None, count=None, side=None, src="rtds_price", recv=None):
    return {"source_event_ts": ts_ms, "receive_ts": recv if recv is not None else ts_ms // 1000,
            "price": price, "quantity": qty, "quote_quantity": quote, "trade_count": count,
            "delivery_source": src, "raw": ({"m": (side == "sell")} if side else {})}


def _price_series(prices_by_age_s, **kw):
    # prices_by_age_s: list of (age_s, price); age 0 == now
    return [_obs((DEC - age) * 1000, p, **kw) for age, p in prices_by_age_s]


def test_returns_positive_on_rising_price():
    obs = _price_series([(120, 100.0), (60, 100.0), (30, 100.0), (15, 100.0), (5, 100.5), (0, 101.0)])
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_price_now"] == 101.0
    assert f["binance_return_15s"] is not None and f["binance_return_15s"] > 0


def test_returns_none_without_enough_history():
    f = BFE.build_binance_features(_price_series([(0, 100.0)]), [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_return_60s"] is None


def test_no_volume_or_vwap_when_price_only():
    obs = _price_series([(i, 100.0 + i * 0.01) for i in range(0, 60)])     # rtds price-only
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_volume_30s"] is None
    assert f["binance_vwap_30s"] is None
    assert f["binance_distance_to_vwap_30s"] is None


def test_volume_and_vwap_present_with_aggtrade():
    obs = _price_series([(i, 100.0, ) for i in range(0, 30)],
                        qty=1.0, quote=100.0, count=1, src="binance_aggtrade")
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_volume_30s"] is not None and f["binance_volume_30s"] > 0
    assert f["binance_vwap_30s"] == 100.0                  # all trades at 100 -> vwap 100
    assert f["binance_trade_count"] is not None


def test_known_at_time_excludes_future_source():
    obs = [_obs(DEC * 1000, 100.0), _obs((DEC + 1) * 1000, 999.0)]   # second sourced after cutoff
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_price_now"] == 100.0                  # 999 did not leak in


def test_excludes_late_received_obs():
    obs = [_obs(DEC * 1000, 100.0, recv=DEC + 50)]          # received after the decision
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_price_now"] is None                   # unknown-at-decision -> excluded


def test_chainlink_realized_vol_from_ticks():
    ticks = [{"source_ts_ms": (DEC - i) * 1000, "recv_ts": DEC - i, "price": 100.0 + (i % 2) * 0.5}
             for i in range(60)]
    f = BFE.build_binance_features([], ticks, cutoff_ts=DEC, decision_ts=DEC)
    assert f["chainlink_realized_vol_30s"] is not None and f["chainlink_realized_vol_30s"] > 0


def test_ema_and_trend_are_continuous_numbers():
    obs = _price_series([(i, 100.0 - i * 0.2) for i in range(0, 60)])    # price rises toward now
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert isinstance(f["binance_ema_spread"], float)
    assert f["binance_ema_spread"] > 0                      # fast(recent, higher) > slow
    assert isinstance(f["binance_trend_strength"], float)


def test_legacy_signal_is_diagnostic_categorical_only():
    obs = _price_series([(i, 100.0 - i * 0.2) for i in range(0, 60)])
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["legacy_spot_signal_diagnostic"] in ("trend_up", "trend_down", "flat")
    # the categorical is NEVER a load-bearing numeric feature — only the diagnostic key carries it
    assert "binance_spot_signal" not in f


def test_signed_volume_from_aggtrade_sides():
    obs = (_price_series([(i, 100.0) for i in range(0, 15)], qty=2.0, count=1,
                         src="binance_aggtrade", side="buy")
           + _price_series([(i, 100.0) for i in range(0, 15)], qty=1.0, count=1,
                           src="binance_aggtrade", side="sell"))
    f = BFE.build_binance_features(obs, [], cutoff_ts=DEC, decision_ts=DEC)
    assert f["binance_signed_volume"] is not None and f["binance_signed_volume"] > 0   # more buy than sell
    assert 0.0 <= f["binance_buy_volume_share"] <= 1.0
