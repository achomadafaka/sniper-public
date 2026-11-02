"""Tests for §6 held-analyst branch (H1–H7)."""

import time

import numpy as np
import pytest

from sniper.analysis.wallet_protocol.held_analyst import (
    EPSILON_RESOLUTION,
    H2_MIN_N,
    H3_MIN_N,
    H5_MIN_N_PER_GRAIN,
    SUB_080_ENTRY_FILTER,
    _entry_notional,
    _is_win,
    filter_held_to_resolution,
    run_h1_net_pnl,
    run_h2_excess_wr,
    run_h3_size_conviction,
    run_h4_entry_timing,
    run_h6_exit_quality,
    run_h7_stationarity,
    run_held_branch,
)
from sniper.analysis.wallet_protocol.types import TagGrainResult, WalletData


def test_tag_grain_result_accepts_details_kwarg():
    """Codex 2026-05-27 audit: held_analyst calls TagGrainResult with
    details={...} but the dataclass lacked the field, raising
    TypeError at runtime. Fix added details: dict (mirrors
    HypothesisResult.details)."""
    r = TagGrainResult(
        grain="leaf", tag="nfl",
        peer_count=10, confidence_flag="ok",
        n_at_grain=5, metric_value=0.6, pop_baseline=0.5, excess_pp=10.0,
        details={"p_value_raw": 0.04, "iters": 1000},
    )
    assert r.details == {"p_value_raw": 0.04, "iters": 1000}


def test_tag_grain_result_default_details_empty_dict():
    """details defaults to an empty dict per HypothesisResult convention."""
    r = TagGrainResult(
        grain="nav", tag="sports",
        peer_count=20, confidence_flag="ok",
        n_at_grain=8, metric_value=None, pop_baseline=None, excess_pp=None,
    )
    assert r.details == {}


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _now() -> int:
    return int(time.time())


def _pos(
    cid="cid_a", outcome="Yes", curPrice=1.0, avgPrice=0.50, size=100,
    realizedPnl=50.0, ts=None, eventSlug=None,
):
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts if ts is not None else _now(),
        "eventSlug": eventSlug or f"slug-{cid}",
    }


def _wallet_data(closed=None, activity=None, archetype=None, profile=None):
    return WalletData(
        wallet="0xabc",
        closed_positions=closed or [],
        open_positions=[],
        activity_rows=activity or [],
        activity_cap_hit=False,
        archetype_layer1=archetype,
        execution_profile=profile,
        fetched_at=_now(),
    )


# ----------------------------------------------------------------------------
# filter_held_to_resolution
# ----------------------------------------------------------------------------


def test_filter_excludes_position_with_pre_resolution_sell():
    """SELL on (cid, outcome) → excluded from held subset (§6.1 H2 rule)."""
    closed = [_pos(cid="cid1", outcome="Yes", curPrice=1.0, ts=2000)]
    activity = [{
        "side": "SELL", "conditionId": "cid1", "outcome": "Yes",
        "size": 50, "price": 0.6, "timestamp": 1500,
    }]
    held = filter_held_to_resolution(_wallet_data(closed=closed, activity=activity))
    assert held == []


def test_filter_keeps_position_no_sell():
    closed = [_pos(cid="cid1", outcome="Yes", curPrice=1.0)]
    activity = []
    held = filter_held_to_resolution(_wallet_data(closed=closed, activity=activity))
    assert len(held) == 1


def test_filter_excludes_unresolved_position():
    """curPrice in (ε, 1-ε) → not resolved → excluded."""
    closed = [_pos(curPrice=0.5)]
    held = filter_held_to_resolution(_wallet_data(closed=closed))
    assert held == []


def test_filter_uses_epsilon_threshold():
    """curPrice within ε=0.01 of {0, 1} qualifies."""
    assert EPSILON_RESOLUTION == 0.01
    closed = [_pos(curPrice=0.99), _pos(cid="cid2", curPrice=0.005)]
    held = filter_held_to_resolution(_wallet_data(closed=closed))
    assert len(held) == 2


def test_filter_handles_null_cur_price():
    closed = [_pos(curPrice=None)]
    held = filter_held_to_resolution(_wallet_data(closed=closed))
    assert held == []


# ----------------------------------------------------------------------------
# _is_win
# ----------------------------------------------------------------------------


def test_is_win_yes_resolved_to_one():
    assert _is_win({"outcome": "Yes", "curPrice": 1.0})


def test_is_win_yes_resolved_to_zero():
    assert not _is_win({"outcome": "Yes", "curPrice": 0.0})


def test_is_win_no_resolved_at_one_is_a_win():
    """A NO position wins when its NO token redeems at $1.

    Per ADR-003 + R2 empirical resolution (commit cba241e):
    curPrice is the held-token price. The held NO token redeems at $1
    iff NO won; iff curPrice ≈ 1.0.
    """
    assert _is_win({"outcome": "No", "curPrice": 1.0})


def test_is_win_no_resolved_at_zero_is_a_loss():
    """A NO position loses when its NO token redeems at $0 (YES won).

    Locks audit-R2 fix: pre-fix code treated NO + curPrice=0.0 as a
    WIN (incorrectly assuming curPrice was YES probability). Post-fix
    must classify it as a loss.
    """
    assert not _is_win({"outcome": "No", "curPrice": 0.0})


# ----------------------------------------------------------------------------
# H1 — net P&L gate
# ----------------------------------------------------------------------------


def test_h1_winsorized_recovers_central_mean_under_fat_tail():
    """v3 amendment A: H1 verdict driven by winsorized bootstrap.

    50 positions at +$10 each (real edge) plus 1 outlier at +$100K. Raw
    sum-bootstrap CI is wide and may include zero; winsorized bootstrap
    clips the outlier and recovers the central tendency.
    """
    closed = []
    for _ in range(50):
        closed.append(_pos(realizedPnl=10.0))
    closed.append(_pos(realizedPnl=100_000.0))
    h1 = run_h1_net_pnl(closed, n_iter=500)
    # Winsorized verdict on per-position mean: tight CI > 0 around $10
    assert h1.verdict == "supported"
    # Raw sum preserved in details
    assert "raw_pnl_sum" in h1.details
    assert h1.details["raw_pnl_sum"] == 100_500.0
    assert "raw_sum_ci" in h1.details
    assert "winsorize_clip_frac" in h1.details


def test_h1_sub_top3_absent_when_no_indices_passed():
    """H1_sub_top3 diagnostic only fires when caller passes top3_indices."""
    closed = [_pos(realizedPnl=50.0) for _ in range(40)]
    h1 = run_h1_net_pnl(closed, n_iter=200)
    assert "sub_top3" not in h1.details


def test_h1_sub_top3_present_and_excludes_indices():
    """When top3_indices passed, exclusion subset's CI is reported in details."""
    closed = [_pos(realizedPnl=10.0) for _ in range(50)]
    closed.append(_pos(realizedPnl=1_000_000.0))   # idx 50: massive outlier
    closed.append(_pos(realizedPnl=500_000.0))     # idx 51
    closed.append(_pos(realizedPnl=200_000.0))     # idx 52
    h1 = run_h1_net_pnl(
        closed, n_iter=300, top3_indices=[50, 51, 52],
    )
    assert "sub_top3" in h1.details
    sub = h1.details["sub_top3"]
    assert sub["n"] == 50  # 53 total minus 3 excluded
    # Sub-top3 CI should be tight around 10 (no outliers left)
    assert sub.get("ci_lower") is not None
    assert sub["ci_lower"] > 0
    assert sub["ci_upper"] < 50  # well below the dominant position values


def test_h1_supported_when_clearly_positive():
    closed = [_pos(realizedPnl=100.0) for _ in range(50)]
    h1 = run_h1_net_pnl(closed, n_iter=500)
    assert h1.verdict == "supported"
    assert h1.ci_lower is not None and h1.ci_lower > 0


def test_h1_refuted_when_clearly_negative():
    closed = [_pos(realizedPnl=-100.0) for _ in range(50)]
    h1 = run_h1_net_pnl(closed, n_iter=500)
    assert h1.verdict == "refuted"


def test_h1_inconclusive_when_zero_book():
    closed = [_pos(realizedPnl=0.0) for _ in range(20)]
    h1 = run_h1_net_pnl(closed, n_iter=500)
    assert h1.verdict in ("inconclusive", "refuted")


def test_h1_underpowered_when_empty():
    h1 = run_h1_net_pnl([], n_iter=100)
    assert h1.verdict == "underpowered"


# ----------------------------------------------------------------------------
# H2 — excess WR
# ----------------------------------------------------------------------------


def test_h2_underpowered_below_min_n():
    held = [_pos(curPrice=1.0, avgPrice=0.5) for _ in range(H2_MIN_N - 1)]
    h2 = run_h2_excess_wr(held, n_iter=200)
    assert h2.verdict == "underpowered"


def test_h2_supported_when_wins_above_implied():
    """Wallet bought at 0.30 and won 80% of the time → strong excess."""
    held = []
    for _ in range(40):
        held.append(_pos(outcome="Yes", curPrice=1.0, avgPrice=0.30))
    for _ in range(10):
        held.append(_pos(outcome="Yes", curPrice=0.0, avgPrice=0.30))
    h2 = run_h2_excess_wr(held, n_iter=500)
    assert h2.verdict == "supported"


def test_h2_sub_080_filter_drops_certainty_buys():
    """Positions entered >= 0.80 are dropped from the sub-test."""
    assert SUB_080_ENTRY_FILTER == 0.80
    held = [_pos(curPrice=1.0, avgPrice=0.95) for _ in range(50)]
    h2_full = run_h2_excess_wr(held, sub_080_only=False, n_iter=200)
    h2_sub = run_h2_excess_wr(held, sub_080_only=True, n_iter=200)
    # Sub-080 filter strips all of them → underpowered
    assert h2_sub.verdict == "underpowered"
    # Full version may or may not be supported; either way it's not underpowered
    assert h2_full.verdict != "underpowered"


# ----------------------------------------------------------------------------
# H3 — size conviction
# ----------------------------------------------------------------------------


def test_h3_skipped_for_hedge_archetype():
    held = [_pos(curPrice=1.0) for _ in range(50)]
    h3 = run_h3_size_conviction(held, archetype_layer1="paired_outcome_arb")
    assert h3.verdict == "n/a"


def test_verdict_after_pool_preserves_permutation_refuted_when_rejected():
    """Regression: permutation tests (H3) carry direction in `base` and have
    no CIs. Pre-fix, `_verdict_after_pool` silently demoted refuted→inconclusive
    because both CI checks fell through to the trailing `return "inconclusive"`.
    """
    from sniper.analysis.wallet_protocol.held_analyst import _verdict_after_pool

    assert _verdict_after_pool(
        "refuted", rejected=True, ci_lower=None, ci_upper=None,
    ) == "refuted"


def test_verdict_after_pool_preserves_permutation_supported_when_rejected():
    from sniper.analysis.wallet_protocol.held_analyst import _verdict_after_pool

    assert _verdict_after_pool(
        "supported", rejected=True, ci_lower=None, ci_upper=None,
    ) == "supported"


def test_verdict_after_pool_demotes_permutation_when_not_rejected():
    """When BH does not reject, even a directional permutation result
    demotes to inconclusive (the effect did not survive multiple-comparison
    correction)."""
    from sniper.analysis.wallet_protocol.held_analyst import _verdict_after_pool

    assert _verdict_after_pool(
        "refuted", rejected=False, ci_lower=None, ci_upper=None,
    ) == "inconclusive"
    assert _verdict_after_pool(
        "supported", rejected=False, ci_lower=None, ci_upper=None,
    ) == "inconclusive"


def test_verdict_after_pool_bootstrap_path_unchanged():
    """Bootstrap-CI tests still resolve via CI-direction logic (no behavior
    change)."""
    from sniper.analysis.wallet_protocol.held_analyst import _verdict_after_pool

    # rejected + ci_lower > 0 → supported
    assert _verdict_after_pool(
        "inconclusive", rejected=True, ci_lower=0.05, ci_upper=0.20,
    ) == "supported"
    # rejected + ci_upper < 0 → refuted
    assert _verdict_after_pool(
        "inconclusive", rejected=True, ci_lower=-0.20, ci_upper=-0.05,
    ) == "refuted"
    # rejected + CI straddles zero → inconclusive
    assert _verdict_after_pool(
        "inconclusive", rejected=True, ci_lower=-0.05, ci_upper=0.05,
    ) == "inconclusive"


def test_h3_underpowered_below_30():
    held = [_pos(curPrice=1.0, size=100, avgPrice=0.5) for _ in range(H3_MIN_N - 1)]
    h3 = run_h3_size_conviction(held)
    assert h3.verdict == "underpowered"


def test_h3_detects_positive_size_win_correlation():
    """Larger positions win, smaller positions lose → supported."""
    rng = np.random.default_rng(50)
    held = []
    for _ in range(40):
        # Big position → win
        held.append(_pos(curPrice=1.0, size=1000, avgPrice=0.5))
    for _ in range(40):
        # Small position → loss
        held.append(_pos(curPrice=0.0, size=10, avgPrice=0.5))
    h3 = run_h3_size_conviction(held, n_iter=500)
    assert h3.verdict == "supported"
    assert h3.test_statistic > 0  # positive Spearman ρ


# ----------------------------------------------------------------------------
# M7 — _entry_notional fall-back when /closed-positions omits `size`
# ----------------------------------------------------------------------------


def test_entry_notional_uses_avgprice_times_size_when_size_present():
    """Open-position path: avgPrice * size (per ADR-004)."""
    pos = {"avgPrice": 0.4, "size": 250, "totalBought": 9999}
    assert _entry_notional(pos) == pytest.approx(0.4 * 250)


def test_entry_notional_falls_back_to_total_bought_when_size_absent():
    """Closed-position path (M7): /closed-positions omits `size`.

    Must NOT silently return avgPrice * 0 = 0 — that's the M7 bug
    (mechanical ρ=0/p=1.0 for every wallet on real data).
    """
    pos = {"avgPrice": 0.4, "totalBought": 100.0}  # no `size`
    assert _entry_notional(pos) == pytest.approx(100.0)
    # Must not be the broken-product result.
    assert _entry_notional(pos) != 0.0


def test_entry_notional_falls_back_to_total_bought_when_size_is_zero():
    """`size: 0` (not just missing) → still use totalBought."""
    pos = {"avgPrice": 0.4, "size": 0, "totalBought": 100.0}
    assert _entry_notional(pos) == pytest.approx(100.0)


def test_entry_notional_returns_zero_when_both_size_and_total_bought_absent():
    pos = {"avgPrice": 0.4}  # neither size nor totalBought
    assert _entry_notional(pos) == 0.0


def test_h3_uses_total_bought_on_closed_positions_without_size():
    """Integration (M7): closed positions WITHOUT `size` but WITH `totalBought`
    must produce a non-trivial Spearman ρ — i.e. notionals have variance.

    Pre-fix: every notional collapsed to 0 → ρ=0, p=1.0 every time.
    Post-fix: totalBought spread carries the conviction signal.
    """
    held = []
    # 40 wins with large totalBought.
    for _ in range(40):
        held.append({
            "conditionId": "cid_w",
            "outcome": "Yes",
            "curPrice": 1.0,
            "avgPrice": 0.5,
            # NOTE: no `size` field (mimicking /closed-positions payload)
            "totalBought": 1000.0,
            "realizedPnl": 500.0,
        })
    # 40 losses with small totalBought.
    for _ in range(40):
        held.append({
            "conditionId": "cid_l",
            "outcome": "Yes",
            "curPrice": 0.0,
            "avgPrice": 0.5,
            "totalBought": 10.0,
            "realizedPnl": -5.0,
        })
    h3 = run_h3_size_conviction(held, n_iter=500)
    assert h3.n == 80
    # If the fall-back works, notionals split into {1000, 10} aligned with
    # wins/losses → ρ pegged near +1, p near 0.
    assert h3.test_statistic is not None
    assert h3.test_statistic > 0.5
    assert h3.verdict == "supported"


# ----------------------------------------------------------------------------
# H4 — entry timing
# ----------------------------------------------------------------------------


def test_h4_underpowered_when_no_profile():
    h4 = run_h4_entry_timing(None)
    assert h4.verdict == "underpowered"


def test_h4_underpowered_when_status_not_populated():
    h4 = run_h4_entry_timing({"status": "pending"})
    assert h4.verdict == "underpowered"


def test_h4_supported_with_positive_drift():
    profile = {
        "status": "populated",
        "sample_tier": "gold",
        "drifts_6h": [0.02] * 30,
    }
    h4 = run_h4_entry_timing(profile, n_iter=300)
    assert h4.verdict == "supported"


def test_h4_inconclusive_with_zero_drift():
    profile = {
        "status": "populated",
        "sample_tier": "silver",
        "drifts_6h": [0.0] * 30,
    }
    h4 = run_h4_entry_timing(profile, n_iter=300)
    assert h4.verdict in ("inconclusive", "refuted")


def test_h4_uses_fill_bootstrap_when_clusters_absent():
    """Back-compat: callsites that don't supply cluster ids keep working."""
    profile = {
        "status": "populated",
        "sample_tier": "silver",
        "drifts_6h": [0.02] * 30,
    }
    h4 = run_h4_entry_timing(profile, n_iter=300)
    assert h4.details["bootstrap_method"] == "fill"
    assert "n_distinct_clusters" not in h4.details


def test_h4_uses_cluster_bootstrap_when_clusters_present():
    """M6 closure: drifts_6h_clusters → cluster-resampled CI."""
    drifts_6h = [0.035] * 38 + [0.075] * 35
    clusters = ["mktA"] * 38 + ["mktB"] * 35
    profile = {
        "status": "populated",
        "sample_tier": "silver",
        "drifts_6h": drifts_6h,
        "drifts_6h_clusters": clusters,
    }
    h4 = run_h4_entry_timing(profile, n_iter=2000, seed=42)
    assert h4.details["bootstrap_method"] == "cluster"
    assert h4.details["n_distinct_clusters"] == 2


def test_h4_cluster_ci_wider_than_fill_ci_on_clustered_data():
    """eatpraylove-shape: cluster CI must be ≥3× wider than fill CI per
    spec verification bar (M6)."""
    drifts_6h = [0.035] * 38 + [0.075] * 35
    clusters = ["mktA"] * 38 + ["mktB"] * 35

    profile_fill = {
        "status": "populated", "sample_tier": "silver",
        "drifts_6h": drifts_6h,
    }
    profile_cluster = {
        "status": "populated", "sample_tier": "silver",
        "drifts_6h": drifts_6h,
        "drifts_6h_clusters": clusters,
    }
    h4_fill = run_h4_entry_timing(profile_fill, n_iter=2000, seed=42)
    h4_cluster = run_h4_entry_timing(profile_cluster, n_iter=2000, seed=42)

    width_fill = h4_fill.ci_upper - h4_fill.ci_lower
    width_cluster = h4_cluster.ci_upper - h4_cluster.ci_lower
    assert width_cluster >= 3.0 * width_fill


def test_h4_falls_back_to_fill_on_length_mismatch():
    """Defensive: malformed envelope (clusters length != drifts) → fill path."""
    profile = {
        "status": "populated",
        "sample_tier": "silver",
        "drifts_6h": [0.02] * 30,
        "drifts_6h_clusters": ["x"] * 5,  # wrong length
    }
    h4 = run_h4_entry_timing(profile, n_iter=300)
    assert h4.details["bootstrap_method"] == "fill"


# ----------------------------------------------------------------------------
# H6 — exit quality
# ----------------------------------------------------------------------------


def test_h6_underpowered_no_price_history_fn():
    activity = [{"side": "SELL", "price": 0.6, "timestamp": _now(),
                 "conditionId": "c1", "asset": "a1"}] * 30
    h6 = run_h6_exit_quality(activity, None)
    assert h6.verdict == "underpowered"


def test_h6_supported_when_sells_consistently_correct():
    """Wallet sells at 0.7, market drops to 0.5 24h later → correct exit, all 30 times."""
    activity = [
        {"side": "SELL", "price": 0.7, "timestamp": _now() + i,
         "conditionId": f"c{i}", "asset": f"a{i}"}
        for i in range(30)
    ]
    def _fake_price(market_id, ts):
        return 0.5  # Market price 24h later is lower → correct exit
    h6 = run_h6_exit_quality(activity, _fake_price)
    assert h6.verdict == "supported"
    assert h6.test_statistic == 1.0
    # C5: details must expose coverage so the verification bar can read it.
    assert h6.details["coverage"] == 1.0
    assert h6.details["examined"] == 30


def test_h6_underpowered_when_coverage_low():
    activity = [
        {"side": "SELL", "price": 0.7, "timestamp": _now() + i,
         "conditionId": f"c{i}", "asset": f"a{i}"}
        for i in range(30)
    ]
    def _no_history(market_id, ts):
        return None
    h6 = run_h6_exit_quality(activity, _no_history)
    assert h6.verdict == "underpowered"
    # Coverage is 0 here; details still populated so callers can introspect.
    assert h6.details["coverage"] == 0.0
    assert h6.details["n_sells"] == 30


# ----------------------------------------------------------------------------
# H7 — CUSUM stationarity pre-pass
# ----------------------------------------------------------------------------


def test_h7_no_signal_on_stationary_pnl():
    rng = np.random.default_rng(60)
    closed = [
        _pos(realizedPnl=float(rng.normal(0, 1)), ts=1000 + i)
        for i in range(150)
    ]
    s = run_h7_stationarity(closed)
    assert not s.signaled
    assert s.inference_basis == "full_history"


def test_h7_handles_short_history():
    closed = [_pos(realizedPnl=1.0, ts=1000)]
    s = run_h7_stationarity(closed)
    assert not s.signaled


# ----------------------------------------------------------------------------
# run_held_branch — end-to-end smoke
# ----------------------------------------------------------------------------


def test_held_branch_smoke_end_to_end():
    """50 held positions, supported H1, no execution_profile, no peer_baseline_fn."""
    closed = [
        _pos(
            cid=f"cid_{i}", outcome="Yes",
            curPrice=1.0 if i % 3 != 0 else 0.0,
            avgPrice=0.40, size=100,
            realizedPnl=60.0 if i % 3 != 0 else -40.0,
            ts=1000 + i,
        )
        for i in range(50)
    ]
    data = _wallet_data(closed=closed)
    report = run_held_branch(data, n_iter=500, seed=42)
    assert report.branch == "held_analyst"
    assert report.is_active
    # H1, H2, H2_sub080, H3, H4, H6 all populated
    names = [h.name for h in report.hypotheses]
    assert "H1" in names
    assert "H2" in names
    assert "H2_sub080" in names
    assert "H3" in names
    assert "H4" in names
    assert "H6" in names
    # H4/H6 should be underpowered (no execution_profile, no price_history_fn)
    h4 = next(h for h in report.hypotheses if h.name == "H4")
    assert h4.verdict == "underpowered"
    h6 = next(h for h in report.hypotheses if h.name == "H6")
    assert h6.verdict == "underpowered"
    # Stationarity ran
    assert report.stationarity is not None
    # Independence check ran on held positions
    assert report.independence_check is not None


def test_held_branch_h5_grains_with_peer_baseline_fn():
    """H5 fires per-grain when leafs/nav/peer_baseline_fn all provided."""
    closed = [
        _pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0, avgPrice=0.40, ts=1000 + i)
        for i in range(50)
    ]
    leafs_by_cid = {f"cid_{i}": ["nba"] for i in range(50)}
    nav_lookup = {"nba": "sports"}
    seen_wallets: list[str] = []

    def _peer_fn(grain, tag, wallet):
        # Peer baseline: 30 peers with WR 0.45 (we win way more at 1.0)
        seen_wallets.append(wallet)
        return (0.45, 30)
    report = run_held_branch(
        _wallet_data(closed=closed),
        peer_baseline_fn=_peer_fn,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=500,
    )
    # Expect at least one TagGrainResult with verdict 'supported' (we always win)
    assert len(report.tag_grains) >= 1
    leaf_grain = next(g for g in report.tag_grains if g.grain == "leaf")
    assert leaf_grain.tag == "nba"
    assert leaf_grain.peer_count == 30
    # Wallet's WR at 1.0 vs baseline 0.45 → supported after BH
    assert leaf_grain.verdict in ("supported", "inconclusive")  # depends on n
    # The analyzed wallet must be forwarded to the peer baseline lookup so
    # concrete implementations can self-exclude (gap C3 follow-up).
    assert seen_wallets, "peer_baseline_fn was never invoked"
    assert all(w == "0xabc" for w in seen_wallets), seen_wallets


def test_held_branch_h5_low_population_inference_not_run():
    """peer_count < 10 → inference_not_run."""
    closed = [
        _pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0, ts=1000 + i)
        for i in range(20)
    ]
    leafs_by_cid = {f"cid_{i}": ["niche-tag"] for i in range(20)}
    nav_lookup = {"niche-tag": "other"}
    def _peer_fn(grain, tag, wallet):
        return (0.5, 3)  # only 3 peers
    report = run_held_branch(
        _wallet_data(closed=closed),
        peer_baseline_fn=_peer_fn,
        leafs_by_cid=leafs_by_cid,
        nav_lookup=nav_lookup,
        n_iter=300,
    )
    for g in report.tag_grains:
        assert g.confidence_flag == "low_population"
        assert g.verdict == "inference_not_run"


def test_held_branch_h3_skipped_for_hedge_archetype():
    closed = [_pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0) for i in range(40)]
    data = _wallet_data(closed=closed, archetype="paired_outcome_arb")
    report = run_held_branch(data, n_iter=200)
    h3 = next(h for h in report.hypotheses if h.name == "H3")
    assert h3.verdict == "n/a"


def test_held_branch_independence_check_concentrated():
    """Many positions on same eventSlug → concentrated flag."""
    closed = [
        _pos(cid=f"cid_{i}", outcome="Yes", curPrice=1.0, eventSlug="event-X")
        for i in range(30)
    ]
    report = run_held_branch(_wallet_data(closed=closed), n_iter=200)
    assert report.independence_check is not None
    assert report.independence_check.flag == "concentrated"
