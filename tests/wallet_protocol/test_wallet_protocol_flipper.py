"""Tests for §7 flipper branch (FH1–FH6)."""

import numpy as np
import pytest

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import (
    FH1_MIN_N,
    FH2_MIN_N,
    FH3_MATCHED_PAIRS_FLOOR,
    FH4_MIN_N_PER_GRAIN,
    FH6_MIN_N,
    FH6_PRICE_HISTORY_COVERAGE,
    compute_fh5_hold_duration,
    run_fh1_mean_flip_roi,
    run_fh2_size_conviction,
    run_fh3_spread_capture,
    run_fh6_directional_skill,
    run_flipper_branch,
    run_flipper_stationarity,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _flip(
    cid="cid1", outcome="Yes", buy_ts=1000, sell_ts=2000,
    buy_price=0.50, sell_price=0.60, size=100, flip_roi=None,
    asset_id="",
):
    if flip_roi is None:
        flip_roi = (sell_price - buy_price) / buy_price if buy_price > 0 else 0.0
    return Flip(
        condition_id=cid,
        outcome=outcome,
        buy_ts=buy_ts,
        sell_ts=sell_ts,
        buy_price=buy_price,
        sell_price=sell_price,
        size=size,
        buy_notional=buy_price * size,
        flip_roi=flip_roi,
        hold_duration_seconds=sell_ts - buy_ts,
        asset_id=asset_id,
    )


# ----------------------------------------------------------------------------
# FH1 — mean flip ROI
# ----------------------------------------------------------------------------


def test_fh1_underpowered_below_30():
    flips = [_flip() for _ in range(FH1_MIN_N - 1)]
    result = run_fh1_mean_flip_roi(flips, n_iter=200)
    assert result.verdict == "underpowered"


def test_fh1_supported_with_clear_positive_roi():
    flips = [_flip(buy_price=0.30, sell_price=0.40) for _ in range(50)]
    result = run_fh1_mean_flip_roi(flips, n_iter=500)
    assert result.verdict == "supported"
    assert result.test_statistic > 0


def test_fh1_refuted_with_negative_roi():
    flips = [_flip(buy_price=0.50, sell_price=0.30) for _ in range(50)]
    result = run_fh1_mean_flip_roi(flips, n_iter=500)
    assert result.verdict == "refuted"


# ----------------------------------------------------------------------------
# FH2 — size conviction
# ----------------------------------------------------------------------------


def test_fh2_underpowered_below_30():
    flips = [_flip() for _ in range(FH2_MIN_N - 1)]
    result = run_fh2_size_conviction(flips, n_iter=200)
    assert result.verdict == "underpowered"


def test_fh2_detects_positive_size_correlation():
    """Bigger flips win more, smaller flips lose."""
    flips = []
    for _ in range(40):
        flips.append(_flip(size=1000, buy_price=0.5, sell_price=0.6))
    for _ in range(40):
        flips.append(_flip(size=10, buy_price=0.5, sell_price=0.4))
    result = run_fh2_size_conviction(flips, n_iter=500)
    assert result.verdict == "supported"
    assert result.test_statistic > 0


# ----------------------------------------------------------------------------
# FH3 — spread capture
# ----------------------------------------------------------------------------


def test_fh3_underpowered_when_no_profile_cost_surface():
    """Bug 3: no execution_profile means no spread-cost surface."""
    flips = [_flip(buy_price=0.50, sell_price=0.55) for _ in range(50)]
    result = run_fh3_spread_capture(flips, execution_profile=None, n_iter=300)
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "no_execution_profile"


def test_fh3_low_spread_estimate_flag_when_few_pairs():
    """matched_pairs < 20 → confidence=low_spread_estimate flag."""
    profile = {"tape_midpoint_pairs": [
        {"fill_price": 0.50, "tape_midpoint": 0.495} for _ in range(5)
    ]}
    flips = [_flip(buy_price=0.50, sell_price=0.60) for _ in range(50)]
    result = run_fh3_spread_capture(flips, profile, n_iter=300)
    assert any("low_spread_estimate" in n for n in result.notes)


def test_fh3_refuted_when_roi_below_round_trip_cost():
    """ROI = 1pp, round_trip_cost = 5pp → refuted spread-or-worse."""
    profile = {"round_trip_cost_estimate": 0.05}
    flips = [_flip(buy_price=0.50, sell_price=0.505) for _ in range(50)]
    result = run_fh3_spread_capture(flips, profile, n_iter=300)
    assert result.verdict == "refuted"
    assert any("spread-or-worse" in n for n in result.notes)


# ----------------------------------------------------------------------------
# FH5 — hold duration regime
# ----------------------------------------------------------------------------


def test_fh5_scalper_label_at_short_median():
    flips = [_flip(buy_ts=0, sell_ts=600) for _ in range(20)]  # 10 min hold
    stats = compute_fh5_hold_duration(flips)
    assert stats.regime_label == "scalper"
    assert stats.median_seconds == 600


def test_fh5_news_arb_label_at_medium_median():
    flips = [_flip(buy_ts=0, sell_ts=3 * 3600) for _ in range(20)]  # 3h
    stats = compute_fh5_hold_duration(flips)
    assert stats.regime_label == "news_arb"


def test_fh5_slow_flipper_label_at_long_median():
    flips = [_flip(buy_ts=0, sell_ts=2 * 86400) for _ in range(20)]  # 2 days
    stats = compute_fh5_hold_duration(flips)
    assert stats.regime_label == "slow_flipper"


def test_fh5_mixed_label_when_p25_short_p75_long():
    flips = (
        [_flip(buy_ts=0, sell_ts=600) for _ in range(15)]   # 10min
        + [_flip(buy_ts=0, sell_ts=10 * 86400) for _ in range(15)]  # 10 days
    )
    stats = compute_fh5_hold_duration(flips)
    assert stats.regime_label == "mixed"


def test_fh5_handles_empty():
    stats = compute_fh5_hold_duration([])
    assert stats.regime_label == "n/a"
    assert stats.n == 0


# ----------------------------------------------------------------------------
# FH6 — directional skill
# ----------------------------------------------------------------------------


def test_fh6_underpowered_below_min_n():
    flips = [_flip() for _ in range(FH6_MIN_N - 1)]
    result = run_fh6_directional_skill(flips, lambda c, t: 0.5)
    assert result.verdict == "underpowered"


def test_fh6_underpowered_no_price_history_fn():
    flips = [_flip() for _ in range(30)]
    result = run_fh6_directional_skill(flips, None)
    assert result.verdict == "underpowered"


def test_fh6_underpowered_below_coverage_threshold():
    """price_history coverage < 0.80 → underpowered with explicit reason."""
    assert FH6_PRICE_HISTORY_COVERAGE == 0.80
    flips = [_flip(cid=f"c{i}", asset_id=f"asset_{i}") for i in range(50)]
    counter = {"calls": 0}
    def _spotty(asset_id, ts):
        counter["calls"] += 1
        return None  # always missing → 0% coverage
    result = run_fh6_directional_skill(flips, _spotty)
    assert result.verdict == "underpowered"
    assert any("insufficient_price_history_coverage" in n for n in result.notes)
    assert counter["calls"] > 0  # confirm fn actually invoked


def test_fh6_calls_price_history_with_asset_id_not_condition_id():
    """C5 follow-on: FH6 must query the CLOB by asset/token id."""
    flips = [
        _flip(cid="cidX", asset_id=f"tok_{i}",
              buy_ts=1000 + i, sell_ts=2000 + i,
              buy_price=0.5, sell_price=0.55)
        for i in range(30)
    ]
    seen_ids = []
    def _fn(market_id, ts):
        seen_ids.append(market_id)
        return 0.55  # constant — exit price = buy_ts price; not the test focus
    run_fh6_directional_skill(flips, _fn, n_iter=200)
    # Must see only asset ids, never the conditionId.
    assert all(s.startswith("tok_") for s in seen_ids)
    assert "cidX" not in seen_ids


def test_fh6_underpowered_when_all_flips_missing_asset_id():
    """asset_id="" on every flip → none queryable → underpowered via coverage gate."""
    flips = [_flip(asset_id="") for _ in range(30)]
    def _fn(market_id, ts):  # never called, but defined to skip the no-fn gate
        return 0.55
    result = run_fh6_directional_skill(flips, _fn, n_iter=100)
    assert result.verdict == "underpowered"
    assert result.details.get("asset_id_missing") == 30


def test_fh6_runs_with_valid_asset_ids_and_directional_signal():
    """End-to-end: 30 flips with valid asset_ids + price-history fn → FH6 runs."""
    flips = []
    for i in range(30):
        # Half win + half lose; market direction matches outcome on wins.
        is_win = i % 2 == 0
        bp = 0.50
        sp = 0.60 if is_win else 0.45
        flips.append(_flip(
            cid=f"c{i}", asset_id=f"a{i}",
            buy_ts=1000 + i, sell_ts=2000 + i,
            buy_price=bp, sell_price=sp,
        ))
    def _fn(market_id, ts):
        # Roughly track buy → sell price drift on the same asset.
        idx = int(market_id[1:])
        if ts <= 1500 + idx:
            return 0.50
        return 0.60 if idx % 2 == 0 else 0.45
    result = run_fh6_directional_skill(flips, _fn, n_iter=200)
    # Method ran; not gated by asset / coverage.
    assert result.verdict in ("supported", "refuted", "inconclusive")
    assert result.details["coverage"] >= 0.80
    assert result.details.get("asset_id_missing") == 0


# ----------------------------------------------------------------------------
# Stationarity
# ----------------------------------------------------------------------------


def test_flipper_stationarity_no_signal_on_uniform_flips():
    flips = [_flip(buy_price=0.5, sell_price=0.55, buy_ts=i, sell_ts=i + 100)
             for i in range(50)]
    s = run_flipper_stationarity(flips)
    assert not s.signaled


# ----------------------------------------------------------------------------
# run_flipper_branch — end-to-end
# ----------------------------------------------------------------------------


def test_flipper_branch_smoke_end_to_end():
    flips = [_flip(buy_price=0.50, sell_price=0.55, buy_ts=i, sell_ts=i + 1000)
             for i in range(50)]
    report = run_flipper_branch(flips, n_iter=300)
    assert report.branch == "flipper"
    names = [h.name for h in report.hypotheses]
    assert set(names) == {"FH1", "FH2", "FH3", "FH5", "FH6"}
    fh1 = next(h for h in report.hypotheses if h.name == "FH1")
    # Strong positive ROI supports FH1; FH3 is separate spread-cost evidence.
    assert fh1.verdict == "supported"
    fh5 = next(h for h in report.hypotheses if h.name == "FH5")
    assert fh5.verdict == "descriptive_only"


def test_flipper_branch_fh4_grain_with_peer_baseline_fn():
    """Held-WR peer_baseline_fn is IGNORED by FH4 under Phase 2.

    Pre-2026-05-15: `peer_baseline_fn` was the only baseline; FH4 was
    suppressed by `_build_fh4_grain` because the fn returned the wrong
    unit (held-only win rate vs continuous flip-ROI). The old test
    asserted `peer_count == 30` from this wrong-unit fn.

    Post-Phase 2: `peer_baseline_fn` is still accepted as a positional
    arg for backward-compat but it never reaches FH4 verdict logic.
    Only `flip_roi_baseline_fn` drives FH4. With no flip_roi fn passed,
    verdict stays `inference_not_run` AND peer_count is 0 (from the
    absent flip_roi fn, not from the held-WR fn).
    """
    flips = [
        _flip(cid=f"cid_{i}", buy_price=0.50, sell_price=0.65,
              buy_ts=i, sell_ts=i + 100)
        for i in range(40)
    ]
    leafs_by_cid = {f"cid_{i}": ["nba"] for i in range(40)}
    nav_lookup = {"nba": "sports"}

    def _peer(grain, tag, wallet):
        return (0.0, 30)  # wrong unit; should be ignored by FH4

    report = run_flipper_branch(
        flips, wallet="0xabc", peer_baseline_fn=_peer,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=300,
    )
    leaf_grain = next(g for g in report.tag_grains if g.grain == "leaf")
    assert leaf_grain.tag == "nba"
    # Phase 2: peer_count comes from flip_roi_baseline_fn (absent here),
    # NOT from the wrong-unit peer_baseline_fn. So peer_count = 0.
    assert leaf_grain.peer_count == 0
    assert leaf_grain.verdict == "inference_not_run"
    assert leaf_grain.underpowered_reason == "no_flip_roi_peer_baseline"


def test_flipper_branch_fh4_grain_threads_wallet_to_flip_roi_baseline_fn():
    """The flip_roi_baseline_fn must receive the analyzed wallet so it
    can self-exclude (held-side C3 follow-up generalized to flipper)."""
    flips = [
        _flip(cid=f"cid_{i}", buy_price=0.50, sell_price=0.65,
              buy_ts=i, sell_ts=i + 100)
        for i in range(40)
    ]
    leafs_by_cid = {f"cid_{i}": ["nba"] for i in range(40)}
    nav_lookup = {"nba": "sports"}
    seen_wallets: list[str] = []

    def _flip_roi(grain, tag, wallet):
        seen_wallets.append(wallet)
        return None  # forces inference_not_run; we only care about the wallet arg

    run_flipper_branch(
        flips, wallet="0xabc",
        flip_roi_baseline_fn=_flip_roi,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=300,
    )
    assert seen_wallets, "flip_roi_baseline_fn should have been called"
    assert all(w == "0xabc" for w in seen_wallets), seen_wallets


def test_flipper_branch_fh4_low_population_inference_not_run():
    flips = [
        _flip(cid=f"cid_{i}", buy_ts=i, sell_ts=i + 100)
        for i in range(35)
    ]
    leafs_by_cid = {f"cid_{i}": ["niche"] for i in range(35)}
    nav_lookup = {"niche": "other"}
    def _peer(grain, tag, wallet):
        return (0.0, 5)  # 5 peers — below floor
    report = run_flipper_branch(
        flips, peer_baseline_fn=_peer,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=300,
    )
    for g in report.tag_grains:
        assert g.confidence_flag == "low_population"
        assert g.verdict == "inference_not_run"


def test_flipper_branch_fh5_excluded_from_bh_pool():
    """FH5 verdict stays 'descriptive_only' and never gets BH-adjusted."""
    flips = [_flip(buy_ts=i, sell_ts=i + 100) for i in range(50)]
    report = run_flipper_branch(flips, n_iter=300)
    fh5 = next(h for h in report.hypotheses if h.name == "FH5")
    assert fh5.verdict == "descriptive_only"
    assert fh5.p_value_bh_adjusted is None


# ----------------------------------------------------------------------------
# FH6 — audit-R1 regression: NO-side flip must NOT invert market_dir
# ----------------------------------------------------------------------------


def test_fh6_does_not_invert_no_side_market_direction():
    """Lock the audit-R1 fix.

    A winning NO flip whose held-token (NO) market appreciated must be
    counted as `with_market`, not `against_market`. Pre-fix code applied
    `market_dir = -market_dir` on outcome=No, which inverted the routing.

    20-flip fixture (FH6_MIN_N=20): 10 with_market wins + 10 against_market
    losses — all NO-outcome so the pre-fix inversion would corrupt both
    buckets simultaneously.
    """
    # Flips 0-9: winning NO flips, NO token appreciated → with_market.
    # Flips 10-19: losing NO flips, NO token fell → against_market.
    # Every flip uses a unique asset_id so price_history_fn can distinguish them.
    flips = []
    rising_ids: set[str] = set()
    falling_ids: set[str] = set()
    for i in range(10):
        aid = f"tok_rising_{i}"
        rising_ids.add(aid)
        flips.append(Flip(
            condition_id=f"cid_r{i}", outcome="No",
            buy_ts=1000 + i, sell_ts=2000 + i,
            buy_price=0.40, sell_price=0.65, size=100.0,
            buy_notional=40.0,
            flip_roi=(0.65 - 0.40) / 0.40,   # +0.625
            hold_duration_seconds=1000,
            asset_id=aid,
        ))
    for i in range(10):
        aid = f"tok_falling_{i}"
        falling_ids.add(aid)
        flips.append(Flip(
            condition_id=f"cid_f{i}", outcome="No",
            buy_ts=3000 + i, sell_ts=4000 + i,
            buy_price=0.60, sell_price=0.40, size=100.0,
            buy_notional=60.0,
            flip_roi=(0.40 - 0.60) / 0.60,   # -0.333
            hold_duration_seconds=1000,
            asset_id=aid,
        ))

    # price_history_fn returns NO-token prices (held-token coordinate per ADR-003).
    # Rising group: market moved 0.42→0.62, market_dir = +0.20 > 0.
    # Falling group: market moved 0.62→0.42, market_dir = -0.20 < 0.
    def price_history_fn(asset_id: str, ts: int) -> float | None:
        if asset_id in rising_ids:
            # buy_ts in [1000,1009], sell_ts in [2000,2009]
            return 0.42 if ts < 2000 else 0.62
        if asset_id in falling_ids:
            # buy_ts in [3000,3009], sell_ts in [4000,4009]
            return 0.62 if ts < 4000 else 0.42
        return None

    result = run_fh6_directional_skill(
        flips,
        price_history_fn=price_history_fn,
        n_iter=200,
        seed=42,
    )
    # Pre-fix bug: market_dir = -market_dir on outcome=No swapped every flip.
    # Rising group would become market_dir < 0 → against_market (wrong).
    # Falling group would become market_dir > 0 → with_market (wrong).
    # Result: wr_with_market=0.0, wr_against_market=1.0.
    #
    # Post-fix: no inversion. Coordinate already aligned (ADR-003).
    # Rising wins → with_market: wr=1.0. Falling losses → against_market: wr=0.0.
    assert result.details.get("wr_with_market") == 1.0, (
        f"Expected wr_with_market=1.0 (all rising NO wins), got {result.details}"
    )
    assert result.details.get("wr_against_market") == 0.0, (
        f"Expected wr_against_market=0.0 (all falling NO losses), got {result.details}"
    )
