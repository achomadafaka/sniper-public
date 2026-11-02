"""Tests for the honest scorecard math (sniper/hyperlive/copy_scorecard.py)."""
from sniper.hyperlive import copy_scorecard as SC


def test_bootstrap_ci_deterministic_and_spans_zero_for_noisy():
    xs = [200.0, -180.0, 20.0, -30.0, 250.0, -240.0, 15.0, -10.0]
    lo1, hi1 = SC.bootstrap_ci(xs, iters=5000, seed=1)
    lo2, hi2 = SC.bootstrap_ci(xs, iters=5000, seed=1)
    assert (lo1, hi1) == (lo2, hi2)          # deterministic given seed
    assert lo1 < 0 < hi1                     # noisy, mean not distinguishable from 0


def test_bootstrap_ci_excludes_zero_for_clean_positive():
    xs = [40.0, 45.0, 38.0, 42.0, 50.0, 41.0, 39.0, 44.0]
    lo, hi = SC.bootstrap_ci(xs, iters=5000, seed=3)
    assert lo > 0                            # tight positive -> CI above zero


def test_cluster_bootstrap_is_not_narrower_on_correlated_fixture():
    xs = [100.0] * 20 + [-100.0] * 20
    groups = ["winner"] * 20 + ["loser"] * 20
    iid_lo, iid_hi = SC.bootstrap_ci(xs, iters=4000, seed=4)
    cluster_lo, cluster_hi = SC.cluster_bootstrap_ci(xs, groups, iters=4000, seed=4)
    assert cluster_hi - cluster_lo >= iid_hi - iid_lo


def test_winsorized_mean_pulls_in_outlier():
    xs = [10.0, 12.0, 11.0, 9.0, 1000.0]     # one huge outlier
    assert SC.winsorized_mean(xs, p=0.2) < sum(xs) / len(xs)


def test_summary_stats_shape_and_median_vs_mean():
    xs = [769.0, -8.0, -8.0, -8.0, -8.0]     # one winner carries the mean
    s = SC.summary_stats(xs)
    assert s["n"] == 5 and s["mean"] > 0 and s["median"] < 0
    assert set(["ci_lo", "ci_hi", "spans_zero", "winsor_mean", "win_rate"]) <= set(s)


def test_concentration_flags_top_heavy():
    xs = [769.0, 700.0, 300.0, -50.0] + [-20.0] * 40
    c = SC.concentration(xs)
    assert c["top1_share"] is not None and c["top5_share"] > 0.5


def test_per_wallet_groups_and_ranks():
    rows = [{"target": "0xa", "pnl_bps": 50.0}, {"target": "0xa", "pnl_bps": 60.0},
            {"target": "0xb", "pnl_bps": -40.0}, {"target": "0xb", "pnl_bps": -20.0}]
    pw = SC.per_wallet(rows, min_n=2)
    assert pw[0]["wallet"] == "0xa" and pw[0]["mean"] == 55.0
    assert pw[-1]["wallet"] == "0xb" and pw[-1]["mean"] == -30.0


def test_beta_neutral_separates_alpha_from_market():
    gross = [30.0, 40.0, 20.0, 50.0]
    beta = [-20.0, -10.0, -25.0, -30.0]      # market hurt on net
    r = SC.beta_neutral(gross, beta)
    assert r["gross_mean"] == 35.0 and r["beta_mean"] == round(-21.25, 3)
    assert r["alpha"]["mean"] > r["gross_mean"]   # removing negative beta raises alpha


def test_size_decay_degrades_and_marks_infeasible():
    book = {"mid": 100.0,
            "asks": [[100.05, 100.0], [100.5, 50.0]],
            "bids": [[99.95, 100.0], [99.5, 50.0]]}
    trades = [{"pnl_bps": 30.0, "entry_depth": book, "exit_depth": book}]
    rows = SC.size_decay(trades, [1000, 100000])
    small, big = rows[0], rows[1]
    assert small["feasible_pct"] == 100.0 and small["mean_real_bps"] < 30.0
    assert big["feasible"] == 0 and big["censored_depth"] == 1
    assert big["feasible_pct"] is None


def test_total_book_mtm_combines_realized_and_open():
    t = SC.total_book_mtm([100.0, -50.0], [-30.0, -20.0, 40.0])
    assert t["realized_n"] == 2 and t["open_n"] == 3
    assert t["total_bps"] == round(100 - 50 - 30 - 20 + 40, 1)
    assert t["total_mean_bps"] == round(40 / 5, 2)


def test_censoring_reports_hold_gap():
    rows = [{"detect_ts_ms": 0, "exit_ts_ms": int(2 * 3.6e6), "pnl_bps": 50.0},
            {"detect_ts_ms": 0, "exit_ts_ms": int(10 * 3.6e6), "pnl_bps": -50.0}]
    c = SC.censoring(rows)
    assert c["winners_hold_median_h"] == 2.0 and c["losers_hold_median_h"] == 10.0


def test_price_at_steps_and_clamps():
    candles = [{"t": 0, "c": 100.0}, {"t": 1000, "c": 110.0}, {"t": 2000, "c": 120.0}]
    assert SC.price_at(candles, -5) == 100.0        # clamp low
    assert SC.price_at(candles, 5000) == 120.0      # clamp high
    assert SC.price_at(candles, 1500) == 120.0      # step to next boundary (>=1000 -> 110? no, 1500>=1000)
    assert SC.price_at([], 5) is None


def test_build_report_end_to_end():
    import json
    depth = {"mid": 100.0, "asks": [[100.05, 500.0]], "bids": [[99.95, 500.0]]}  # ~$50k/side
    closed = [
        {"target": "0xa", "coin": "ETH", "side": "long", "entry_mark": 2000.0, "exit_mark": 2040.0,
         "pnl_bps": 191.0, "detect_ts_ms": 0, "exit_ts_ms": int(3.6e6),
         "entry_depth_json": json.dumps(depth), "exit_depth_json": json.dumps(depth),
         "funding_bps": 4.0, "entry_spread_bps": 10.0, "lag_bps": 7.0},
        {"target": "0xa", "coin": "ETH", "side": "long", "entry_mark": 2000.0, "exit_mark": 1990.0,
         "pnl_bps": -59.0, "detect_ts_ms": 0, "exit_ts_ms": int(3.6e6),
         "entry_depth_json": json.dumps(depth), "exit_depth_json": json.dumps(depth),
         "funding_bps": 2.0, "entry_spread_bps": 10.0, "lag_bps": 5.0},
    ]
    open_rows = [{"coin": "BTC", "side": "long", "entry_mark": 100.0}]
    marks = {"BTC": 101.0}
    candles = [{"t": 0, "c": 50000.0}, {"t": int(3.6e6), "c": 50500.0}]   # +100 bps market
    rep = SC.build_report(closed, open_rows, marks=marks, market_candles=candles, sizes=[1000, 100000])
    assert rep["realized"]["n"] == 2
    assert rep["episode_realized"]["n"] == 1
    assert rep["total_book"]["open_n"] == 1 and rep["total_book"]["realized_n"] == 2
    assert rep["beta_neutral"]["n"] == 2
    assert rep["haircuts"]["funding_mean_bps"] == 3.0 and rep["haircuts"]["lag_mean_bps"] == 6.0
    sd = {r["size_usd"]: r for r in rep["size_decay"]["rows"]}
    assert sd[1000]["feasible_pct"] == 100.0
    assert sd[100000]["feasible"] == 0


def test_source_latency_only_summarizes_complete_monotonic_matches():
    rows = [
        {"source_latency_status": "matched", "source_event_ts_ms": 1000,
         "source_received_ts_ms": 1100, "decision_ts_ms": 1300,
         "book_request_ts_ms": 1310, "book_received_ts_ms": 1340,
         "mirror_open_ts_ms": 1350},
        {"source_latency_status": "matched", "source_event_ts_ms": 2000,
         "source_received_ts_ms": 1900, "decision_ts_ms": 2300,
         "book_request_ts_ms": 2310, "book_received_ts_ms": 2340,
         "mirror_open_ts_ms": 2350},
        {"source_latency_status": "unmatched"},
        {},
    ]

    latency = SC.source_latency(rows)

    assert latency["provenance"] == {"matched": 2, "unmatched": 1, "legacy_unknown": 1}
    assert latency["complete_monotonic_n"] == 1
    assert latency["invalid_clock_n"] == 1
    assert latency["source_to_receive_ms"]["mean"] == 100.0
    assert latency["receive_to_decision_ms"]["mean"] == 200.0
    assert latency["decision_to_book_ms"]["mean"] == 30.0
    assert latency["book_to_persist_ms"]["mean"] == 10.0
    assert latency["source_to_mirror_ms"]["mean"] == 350.0


def test_size_decay_does_not_pool_legacy_fifteen_level_and_v2_depth():
    import json
    legacy = {"mid": 100.0, "asks": [[100.1, 1.0]] * 15, "bids": [[99.9, 1.0]] * 15}
    current = {
        **legacy, "capture_format_version": 2,
        "depth_scope": "provider_returned_snapshot",
        "returned_bid_levels": 15, "returned_ask_levels": 15,
    }
    rows = [
        {"pnl_bps": -999.0, "entry_depth_json": json.dumps(legacy)},
        {"pnl_bps": 100.0, "entry_depth_json": json.dumps(current)},
    ]
    report = SC.build_report(rows, [], marks={}, sizes=[10.0])
    assert report["size_decay"]["cohort"] == "provider_returned_v2"
    assert report["size_decay"]["provenance"] == {"provider_returned_v2": 1, "legacy_15": 1}
    assert report["size_decay"]["rows"][0]["mean_real_bps"] > 0


def test_shadow_real_deployable_net():
    import json
    depth = {"mid": 100.0, "asks": [[100.05, 500.0]], "bids": [[99.95, 500.0]]}  # ~$50k/side
    closed = [{"pnl_bps": 100.0, "entry_depth_json": json.dumps(depth),
               "exit_depth_json": json.dumps(depth), "funding_bps": 3.0}]
    r = SC.shadow_real(closed, size=1000)
    assert r["feasible"] == 1
    # deployable = 100 paper - round_trip_slippage - 3 funding, < paper
    assert r["deployable"]["n"] == 1 and r["deployable"]["mean"] < 100.0
    # infeasible at huge size
    big = SC.shadow_real(closed, size=1_000_000)
    assert big["feasible"] == 0
    assert big["censored_depth"] == 1 and big["feasible_pct"] is None


def test_shadow_real_does_not_impute_missing_funding_as_zero():
    import json
    depth = {"mid": 100.0, "asks": [[100.05, 500.0]], "bids": [[99.95, 500.0]]}
    row = {"pnl_bps": 100.0, "entry_depth_json": json.dumps(depth),
           "exit_depth_json": json.dumps(depth), "funding_bps": None}
    report = SC.shadow_real([row], size=1000)
    assert report["funding_complete_n"] == 0
    assert report["deployable"]["n"] == 0


def test_shadow_real_prefers_canonical_funding_contribution():
    import json
    depth = {"mid": 100.0, "asks": [[100.05, 500.0]], "bids": [[99.95, 500.0]]}
    row = {
        "pnl_bps": 100.0, "entry_depth_json": json.dumps(depth),
        "exit_depth_json": json.dumps(depth), "funding_bps": 999.0,
        "funding_pnl_bps": 3.0, "funding_provenance_status": "complete",
    }
    report = SC.shadow_real([row], size=1000)
    # Positive canonical funding is a contribution, so deployable is above the no-funding result.
    assert report["deployable"]["mean"] > 90.0


def test_capacity_from_book():
    book = {"mid": 100.0, "asks": [{"px": 100.05, "sz": 1000.0}],
            "bids": [{"px": 99.95, "sz": 1000.0}]}
    assert SC.capacity(book, budget_bps=25.0) > 0
    result = SC.capacity_result(book, budget_bps=25.0)
    assert result["status"] in {"measured", "lower_bound"}
    assert result["capacity_lower_bound_usd"] == SC.capacity(book, budget_bps=25.0)


def test_estimate_beta_recovers_known_slope():
    # asset = 1.5*market exactly -> beta ~ 1.5
    mkt = [0.01, -0.02, 0.03, -0.01, 0.02, -0.015]
    asset = [1.5 * x for x in mkt]
    assert abs(SC.estimate_beta(mkt, asset) - 1.5) < 1e-6
    # zero-variance market -> None
    assert SC.estimate_beta([0.0, 0.0, 0.0], [0.1, 0.2, 0.3]) is None
    # too few -> None
    assert SC.estimate_beta([0.01], [0.02]) is None


def test_aligned_returns_intersects_timestamps():
    mkt = [{"t": 0, "c": 100}, {"t": 1, "c": 101}, {"t": 2, "c": 102}]   # returns at t=1,2
    asset = [{"t": 1, "c": 50}, {"t": 2, "c": 51}, {"t": 3, "c": 52}]    # returns at t=2,3
    m, a = SC.aligned_returns(mkt, asset)
    assert len(m) == len(a) == 1        # only t=2 shared


def test_build_report_uses_estimated_coin_beta():
    market = [{"t": i, "c": p} for i, p in enumerate([100, 101, 99, 102, 100])]
    asset = [{"t": i, "c": p} for i, p in enumerate([100, 102, 98, 104, 100])]
    row = {"target": "w", "coin": "ALT", "side": "long", "entry_mark": 100,
           "exit_mark": 104, "pnl_bps": 391, "detect_ts_ms": 1, "exit_ts_ms": 3}
    report = SC.build_report([row], [], marks={}, market_candles=market,
                             asset_candles={"ALT": asset})
    assert report["beta_neutral"]["method"] == "estimated_per_coin_ols"
    assert report["beta_neutral"]["coin_betas"]["ALT"] > 1.5


def test_build_report_clean_excludes_cold_start():
    base = {"detect_ts_ms": 0, "exit_ts_ms": 1, "target": "0xa", "coin": "ETH",
            "side": "long", "entry_mark": 1.0, "exit_mark": 1.0}
    closed = [{**base, "pnl_bps": 100.0, "is_cold_start": 0},
              {**base, "pnl_bps": -200.0, "is_cold_start": 1}]
    rep = SC.build_report(closed, [], marks={})
    assert rep["realized"]["n"] == 2
    assert rep["clean_realized"]["n"] == 1 and rep["clean_realized"]["mean"] == 100.0
    assert rep["cold_start"]["n"] == 1 and rep["cold_start"]["mean"] == -200.0


def test_total_book_clean_excludes_cold_start_opens_and_closes():
    """total_book blends cold-start rows into the MTM; total_book_clean must mirror
    clean_realized on both the closed AND open legs (cold-start opens can linger for
    weeks and dominate the open drag)."""
    base = {"detect_ts_ms": 0, "exit_ts_ms": 1, "target": "0xa", "side": "long",
            "entry_mark": 100.0, "exit_mark": 100.0}
    closed = [{**base, "coin": "ETH", "pnl_bps": 50.0, "is_cold_start": 0},
              {**base, "coin": "SOL", "pnl_bps": -500.0, "is_cold_start": 1}]
    open_rows = [{"target": "0xa", "coin": "BTC", "side": "long", "entry_mark": 100.0,
                  "is_cold_start": 0},
                 {"target": "0xa", "coin": "DOGE", "side": "long", "entry_mark": 100.0,
                  "is_cold_start": 1}]
    marks = {"BTC": 101.0, "DOGE": 50.0}      # clean open +100 gross, cold open -5000 gross
    rep = SC.build_report(closed, open_rows, marks=marks, fee_bps=0.0)
    assert rep["total_book"]["realized_n"] == 2 and rep["total_book"]["open_n"] == 2
    tbc = rep["total_book_clean"]
    assert tbc["realized_n"] == 1 and tbc["realized_sum_bps"] == 50.0
    assert tbc["open_n"] == 1 and tbc["open_mtm_sum_bps"] == 100.0
    assert tbc["total_bps"] == 150.0
