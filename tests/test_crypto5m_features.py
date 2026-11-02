"""Decision-time feature snapshots — known-at-time, geometry, independence, lineage."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import features as FE

WS, WE = 1000, 1300          # window 1000..1300 (s)
UP = {"best_bid": 0.60, "best_ask": 0.62}
DOWN = {"best_bid": 0.38, "best_ask": 0.40}


def _ticks(prices_by_src_s, *, recv_offset=0):
    return [{"source_ts_ms": s * 1000, "recv_ts": s + recv_offset, "price": p}
            for s, p in prices_by_src_s]


def _base_ticks():
    # 998..1060s, flat 100.0 then 100.5 at the end
    return _ticks([(s, 100.0) for s in range(998, 1060)] + [(1060, 100.5)])


def test_known_at_time_excludes_future_source_and_late_receive():
    ticks = _base_ticks() + [
        {"source_ts_ms": 1100 * 1000, "recv_ts": 1100, "price": 999.0},   # source after cutoff (1060)
        {"source_ts_ms": 1055 * 1000, "recv_ts": 1080, "price": 888.0},   # received after decision (1060)
    ]
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN)
    f = snap["features"]
    assert f["chainlink_price_now"] == 100.5            # neither lookahead tick (999/888) leaked in
    assert f["chainlink_intrawindow_high"] == 100.5


def test_recv_ts_missing_tick_excluded_and_flagged():
    # A tick whose arrival time is unknown (recv_ts=None) cannot be proven known-at-decision -> fail
    # closed: it must NOT influence features, and the snapshot is flagged for the leakage audit.
    ticks = _ticks([(1000, 100.0), (1050, 100.0)]) + [
        {"source_ts_ms": 1055 * 1000, "recv_ts": None, "price": 999.0},   # unknown arrival
    ]
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN)
    assert snap["features"]["chainlink_price_now"] == 100.0   # 999 (recv_ts None) did NOT leak in
    assert snap["receive_time_missing"] is True


def test_recv_ts_present_sets_no_missing_flag():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN)
    assert snap["receive_time_missing"] is False


def test_snapshot_includes_binance_family_c():
    bobs = [{"source_event_ts": (1000 + i) * 1000, "receive_ts": 1000 + i, "price": 100.0 + i * 0.01,
             "quantity": None, "quote_quantity": None, "trade_count": None,
             "delivery_source": "rtds_price", "raw": {}} for i in range(0, 61)]
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN, binance_obs=bobs)
    f = snap["features"]
    assert f["binance_price_now"] is not None
    assert "binance_return_30s" in f
    assert f["binance_volume_30s"] is None                  # price-only -> no fabricated volume
    assert f["legacy_spot_signal_diagnostic"] in ("trend_up", "trend_down", "flat")


def test_snapshot_includes_executable_and_basis_features():
    up = {"best_bid": 0.60, "best_ask": 0.62, "asks": [[0.62, 1000]]}
    down = {"best_bid": 0.38, "best_ask": 0.40, "asks": [[0.40, 1000]]}
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=up, down_book=down, fee={"fee_status": "known",
                                                                      "fee_rate": 0.001})
    f = snap["features"]
    assert "up_executable_price_10_usdc" in f and "down_executable_price_10_usdc" in f
    assert f["up_fee_inclusive_cost_10_usdc"] is not None    # fee known -> populated
    assert "binance_chainlink_basis_abs" in f                # family D present (None w/o binance obs)
    assert f["basis_quality"] == "unavailable"               # no binance obs supplied


def test_geometry_and_normalized_distance():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN)
    f = snap["features"]
    assert abs(f["distance_to_beat"] - 0.5) < 1e-9      # 100.5 - 100.0
    assert f["seconds_remaining"] == 240                # 1300 - 1060
    assert f["price_to_beat"] == 100.0
    assert f["normalized_distance_to_beat"] is not None  # uses price_to_beat + vol + sqrt(t)


def test_midpoints_are_independent_per_token():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN)
    f = snap["features"]
    assert abs(f["up_midpoint_probability"] - 0.61) < 1e-12
    assert abs(f["down_midpoint_probability"] - 0.39) < 1e-12   # from DOWN book, not 1-up


def test_stale_chainlink_reference_is_not_ok():
    ticks = [{"source_ts_ms": WS * 1000, "recv_ts": WS, "price": 100.0}]   # only at window start
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=285, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN)
    assert snap["feature_status"] == "stale_reference"
    assert snap["features"]["chainlink_now_age_ms"] == 285000


def test_present_but_empty_book_is_not_ok():
    # both books present but unpriced (no best_ask / null midpoint) must NOT be feature_status ok,
    # else it becomes model-eligible with no tradable book.
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book={}, down_book={})
    assert snap["feature_status"] == "feature_window_incomplete"


def test_no_chainlink_before_window_is_boundary_unavailable():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=[],
                                     up_book=UP, down_book=DOWN)
    assert snap["feature_status"] == "boundary_unavailable_at_decision"
    assert snap["price_to_beat_quality"] == "unavailable"


def test_missing_book_is_feature_window_incomplete():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=None, down_book=DOWN)
    assert snap["feature_status"] == "feature_window_incomplete"


def test_realized_vol_null_on_insufficient_history():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60,
                                     chainlink_ticks=_ticks([(1000, 100.0)]), up_book=UP, down_book=DOWN)
    assert snap["features"]["vol_per_s"] is None


def test_boundary_rule_is_honored_and_bogus_rule_stays_unavailable():
    # ticks straddling window start: 999s=100.0, 1001s=200.0 (window_start=1000)
    ticks = _ticks([(999, 100.0), (1001, 200.0)] + [(s, 200.0) for s in range(1002, 1061)])
    last = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN, boundary_rule="last_at_or_before")
    first = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                      window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                      up_book=UP, down_book=DOWN, boundary_rule="first_at_or_after")
    assert last["features"]["price_to_beat"] == 100.0      # last tick <= start
    assert first["features"]["price_to_beat"] == 200.0     # first tick >= start (rule honored)
    assert last["price_to_beat_quality"] == "validated_rtds_reconstruction"
    # a bogus / non-candidate rule must NOT promote quality to the activatable level
    bogus = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                      window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                      up_book=UP, down_book=DOWN, boundary_rule="GARBAGE")
    assert bogus["price_to_beat_quality"] == "unavailable"


def test_sub_second_tick_after_cutoff_is_excluded():
    # a tick sourced 900ms AFTER the decision instant must not leak in (no second-floor)
    ticks = _base_ticks() + [{"source_ts_ms": 1060 * 1000 + 900, "recv_ts": 1060, "price": 777.0}]
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN)
    assert snap["features"]["chainlink_price_now"] == 100.5   # 777 (sub-second-late) excluded


def test_intrawindow_range_excludes_pre_window_ticks():
    ticks = _ticks([(990, 9999.0)] + [(s, 100.0) for s in range(1000, 1061)])  # pre-window spike
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=ticks,
                                     up_book=UP, down_book=DOWN)
    assert snap["features"]["chainlink_intrawindow_high"] == 100.0   # the 9999 pre-window spike excluded


def test_per_side_integrity_is_independent():
    up_q = {**UP, "book_integrity_status": "usable"}
    down_q = {**DOWN, "book_integrity_status": "quarantined"}
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=60, chainlink_ticks=_base_ticks(),
                                     up_book=up_q, down_book=down_q)
    assert snap["up_book_integrity_status"] == "usable"
    assert snap["down_book_integrity_status"] == "quarantined"
    assert snap["book_integrity_status"] == "quarantined"    # worst of the two


def test_past_close_checkpoint_is_too_close_to_close():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=310, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN)
    assert snap["features"]["seconds_remaining"] < 0
    assert snap["feature_status"] == "too_close_to_close"


def test_lineage_preserved():
    snap = FE.build_feature_snapshot(condition_id="c", market_id="m", window_start_ts=WS,
                                     window_end_ts=WE, checkpoint=90, chainlink_ticks=_base_ticks(),
                                     up_book=UP, down_book=DOWN, code_commit="deadbeef")
    assert snap["feature_version"] == FE.FEATURE_VERSION
    assert snap["decision_ts"] == 1090 and snap["checkpoint"] == 90
    assert snap["code_commit"] == "deadbeef"


# --- family G: short-horizon flow (depth-flow OFI + CLOB repricing), v5 / fv1 -----------------------

def test_depth_flow_directionality():
    # bids accumulated (50->90) and asks thinned (40->20) -> strong +ve buy pressure
    prior = {"bids": [[0.60, 50]], "asks": [[0.62, 40]]}
    now = {"bids": [[0.60, 90]], "asks": [[0.62, 20]]}
    assert FE._depth_flow(prior, now) == 40.0 - (-20.0)       # +60 (Δbid - Δask)
    # symmetric sell pressure
    assert FE._depth_flow(now, prior) == -60.0
    assert FE._depth_flow(None, now) is None                  # no prior -> None (never fabricated)
    assert FE._depth_flow(prior, None) is None


def test_midpoint_change():
    prior = {"best_bid": 0.50, "best_ask": 0.52}             # mid 0.51
    now = {"best_bid": 0.58, "best_ask": 0.60}               # mid 0.59
    assert FE._midpoint_change(prior, now) == 0.08
    assert FE._midpoint_change(None, now) is None


def test_flow_features_emitted_and_known_at_decision():
    up_prior = {"best_bid": 0.55, "best_ask": 0.57, "bids": [[0.55, 30]], "asks": [[0.57, 30]]}
    up_now = {"best_bid": 0.60, "best_ask": 0.62, "bids": [[0.60, 80]], "asks": [[0.62, 10]]}
    snap = FE.build_feature_snapshot(
        condition_id="c", market_id="m", window_start_ts=WS, window_end_ts=WE, checkpoint=60,
        chainlink_ticks=[], up_book=up_now, down_book=DOWN, up_book_prior=up_prior, down_book_prior=None,
        binance_obs=[], fee={"fee_status": "known", "fee_rate": 0.0})
    f = snap["features"]
    assert f["up_depth_flow_60s"] == (80 - 30) - (10 - 30)   # +70 buy pressure
    assert f["up_midpoint_change_60s"] == round(0.61 - 0.56, 6)
    assert f["down_depth_flow_60s"] is None                   # no down prior -> None
    # always emitted even with no prior at all (so canonical_feature_names is stable)
    snap2 = FE.build_feature_snapshot(
        condition_id="c", market_id="m", window_start_ts=WS, window_end_ts=WE, checkpoint=60,
        chainlink_ticks=[], up_book=up_now, down_book=DOWN, binance_obs=[],
        fee={"fee_status": "known", "fee_rate": 0.0})
    for k in ("up_depth_flow_60s", "down_depth_flow_60s", "up_midpoint_change_60s", "down_midpoint_change_60s"):
        assert k in snap2["features"] and snap2["features"][k] is None


def test_flow_features_registered_decision_safe():
    from sniper.algos.polymarket_crypto5m import feature_registry as FR
    names = FR.canonical_feature_names()
    for k in ("up_depth_flow_60s", "down_depth_flow_60s", "up_midpoint_change_60s", "down_midpoint_change_60s"):
        assert k in names                                    # auto-enumerated from the snapshot
        d = FR.describe_feature(k)
        assert d["decision_safe"] is True and d["required_sources"] == ["polymarket_book"]
