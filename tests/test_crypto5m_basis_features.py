"""Phase 1B Deliverable #6 — feature family D (Binance↔Chainlink basis).

Basis = binance_price - chainlink_price, formed ONLY when the two source timestamps are sufficiently
aligned (skew <= limit). On excessive skew the basis fields are None and basis_quality is
'stale_or_misaligned' — never silently interpolated. Strict known-at-time on both inputs.
"""
from sniper.algos.polymarket_crypto5m import basis_features as DF

DEC = 1000


def _bobs(ts_ms, price, recv=None):
    return {"source_event_ts": ts_ms, "receive_ts": recv if recv is not None else ts_ms // 1000,
            "price": price, "quantity": None, "quote_quantity": None, "trade_count": None,
            "delivery_source": "rtds_price", "raw": {}}


def _tick(ts_ms, price, recv=None):
    return {"source_ts_ms": ts_ms, "recv_ts": recv if recv is not None else ts_ms // 1000,
            "price": price}


def test_basis_abs_and_bps_when_aligned():
    f = DF.build_basis_features([_bobs(DEC * 1000, 100.5)], [_tick(DEC * 1000, 100.0)],
                                cutoff_ts=DEC, decision_ts=DEC, max_skew_ms=1000)
    assert f["basis_quality"] == "ok"
    assert abs(f["binance_chainlink_basis_abs"] - 0.5) < 1e-9
    assert f["binance_chainlink_basis_bps"] > 0
    assert f["basis_observation_skew_ms"] == 0


def test_stale_or_misaligned_when_skew_exceeds_limit():
    f = DF.build_basis_features([_bobs(DEC * 1000, 100.5)], [_tick((DEC - 5) * 1000, 100.0)],
                                cutoff_ts=DEC, decision_ts=DEC, max_skew_ms=1000)
    assert f["basis_quality"] == "stale_or_misaligned"
    assert f["binance_chainlink_basis_abs"] is None        # not interpolated
    assert f["binance_chainlink_basis_bps"] is None
    assert f["basis_observation_skew_ms"] == 5000


def test_unavailable_when_a_side_is_missing():
    f = DF.build_basis_features([], [_tick(DEC * 1000, 100.0)], cutoff_ts=DEC, decision_ts=DEC)
    assert f["basis_quality"] == "unavailable"
    assert f["binance_chainlink_basis_abs"] is None


def test_source_ages_and_skew_recorded():
    f = DF.build_basis_features([_bobs((DEC - 2) * 1000, 100.5)], [_tick((DEC - 1) * 1000, 100.0)],
                                cutoff_ts=DEC, decision_ts=DEC, max_skew_ms=2000)
    assert f["binance_source_age_ms"] == 2000
    assert f["chainlink_source_age_ms"] == 1000
    assert f["basis_observation_skew_ms"] == 1000
    assert f["basis_quality"] == "ok"


def test_basis_change_and_volatility_over_series():
    bobs = [_bobs((DEC - i) * 1000, 100.0 + (0.5 if i < 30 else 0.2)) for i in range(60)]
    ticks = [_tick((DEC - i) * 1000, 100.0) for i in range(60)]
    f = DF.build_basis_features(bobs, ticks, cutoff_ts=DEC, decision_ts=DEC)
    assert f["basis_change_30s"] is not None and f["basis_change_30s"] > 0   # 0.2 -> 0.5
    assert f["basis_volatility_30s"] is not None


def test_point_basis_consistent_with_series_pairing():
    # newest binance (110 @ now) is newer than the latest chainlink (@ DEC-1); the point basis must pair
    # the latest chainlink to its NEAREST binance (100 @ DEC-1), not blindly the newest binance.
    bobs = [_bobs((DEC - 1) * 1000, 100.0), _bobs(DEC * 1000, 110.0)]
    ticks = [_tick((DEC - 1) * 1000, 100.0)]
    f = DF.build_basis_features(bobs, ticks, cutoff_ts=DEC, decision_ts=DEC, max_skew_ms=2000)
    assert f["binance_chainlink_basis_abs"] == 0.0     # 100-100, NOT 110-100


def test_basis_stale_when_sources_old_vs_decision():
    # aligned (skew 0) but both sides 120s old vs the decision -> stale, basis not formed
    bobs = [_bobs((DEC - 120) * 1000, 100.5)]
    ticks = [_tick((DEC - 120) * 1000, 100.0)]
    f = DF.build_basis_features(bobs, ticks, cutoff_ts=DEC, decision_ts=DEC, max_skew_ms=1000)
    assert f["basis_quality"] == "stale_or_misaligned"
    assert f["binance_chainlink_basis_abs"] is None


def test_known_at_time_excludes_future_binance():
    bobs = [_bobs(DEC * 1000, 100.5), _bobs((DEC + 1) * 1000, 999.0)]    # second after cutoff
    f = DF.build_basis_features(bobs, [_tick(DEC * 1000, 100.0)], cutoff_ts=DEC, decision_ts=DEC)
    assert abs(f["binance_chainlink_basis_abs"] - 0.5) < 1e-9            # 999 didn't leak in
