"""Phase 2 — FH4 inference path under flip_roi_baseline_fn.

Covers the new code path in `_build_fh4_grain` that runs a percentile
bootstrap of the wallet's flip ROIs against a peer-mean baseline.

Also covers the `build_flip_roi_baseline_fn` /
`build_cached_flip_roi_baseline_fn` helpers in
`sniper.analysis.wallet_protocol.ledger.flip_roi_baseline` which read
peer rows from `wallet_flipper_tag_grain`.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import (
    FH4_MIN_FLIPS_FOR_INFERENCE,
    FH4_MIN_PEERS_FOR_INFERENCE,
    _build_fh4_grain,
    run_flipper_branch,
)
from sniper.analysis.wallet_protocol.ledger.flip_roi_baseline import (
    build_cached_flip_roi_baseline_fn,
    build_flip_roi_baseline_fn,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _flip(idx: int, roi: float = 0.10) -> Flip:
    return Flip(
        condition_id=f"0xcid{idx}",
        outcome="Yes",
        asset_id=f"asset{idx}",
        buy_ts=1700000000 + idx * 60,
        sell_ts=1700000000 + idx * 60 + 3600,
        size=100.0,
        buy_notional=100.0,
        buy_price=0.5,
        sell_price=0.5 * (1 + roi),
        flip_roi=roi,
        hold_duration_seconds=3600,
    )


def _flips_n_roi(n: int, mean_roi: float, noise: float = 0.02) -> list[Flip]:
    """Generate n flips with mean ~= mean_roi and small symmetric noise."""
    return [
        _flip(i, roi=mean_roi + (noise if i % 2 == 0 else -noise))
        for i in range(n)
    ]


def _flat_flips(n: int, mean_roi: float, noise: float, tag: str = "geopolitics"):
    """Build n flips in a single (leaf, tag) bucket with `mean_roi` ± `noise`."""
    flips = _flips_n_roi(n, mean_roi=mean_roi, noise=noise)
    leafs_by_cid = {f.condition_id: [tag] for f in flips}
    nav_lookup = {tag: "geopolitics_nav"}
    return flips, leafs_by_cid, nav_lookup


# ======================================================================
# End-to-end inference via run_flipper_branch — pool-promoted verdicts
# ======================================================================
# These exercise the full path: _build_fh4_grain → BH pool →
# _verdict_after_pool. That's the surface that produces real
# "supported"/"refuted" verdicts; the unit-level builder always returns
# "inconclusive" (with _p_raw set) so the pool can decide.

def test_fh4_supported_when_wallet_well_above_peer_mean():
    """W's mean = +0.20, peer mean = +0.05, n=50, peer_count=30 (ok),
    tight bootstrap → supported after BH pool."""
    flips, leafs_by_cid, nav_lookup = _flat_flips(50, mean_roi=0.20, noise=0.01)
    report = run_flipper_branch(
        flips, wallet="0xself",
        # peer_count = 30 → confidence_flag == "ok"; without that the
        # post-pool guard at flipper.py:737 caps the verdict at inconclusive.
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 30),
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=400, seed=1,
    )
    leaf = next(g for g in report.tag_grains if g.grain == "leaf" and g.tag == "geopolitics")
    assert leaf.verdict == "supported"
    assert leaf.metric_value is not None
    assert leaf.pop_baseline == 0.05
    assert leaf.excess_pp is not None
    assert leaf.excess_pp > 0
    # Sanity: excess_pp is in percentage points per types.py:114.
    # Wallet mean ≈ 0.20, peer mean 0.05, so excess_pp ≈ 15.
    assert 10.0 < leaf.excess_pp < 20.0
    assert leaf.ci_lower is not None
    assert leaf.ci_upper is not None
    assert leaf.peer_count == 30


def test_fh4_refuted_when_wallet_well_below_peer_mean():
    """W's mean well below peers, peer_count=30 (ok) → refuted after pool."""
    flips, leafs_by_cid, nav_lookup = _flat_flips(50, mean_roi=-0.10, noise=0.01)
    report = run_flipper_branch(
        flips, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 30),
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=400, seed=1,
    )
    leaf = next(g for g in report.tag_grains if g.grain == "leaf" and g.tag == "geopolitics")
    assert leaf.verdict == "refuted"
    assert leaf.excess_pp < 0


def test_fh4_inconclusive_when_ci_brackets_peer_mean():
    """High variance around peer mean → inconclusive."""
    flips = [_flip(i, roi=0.05 + (0.50 if i % 2 == 0 else -0.50)) for i in range(50)]
    leafs_by_cid = {f.condition_id: ["geopolitics"] for f in flips}
    nav_lookup = {"geopolitics": "geopolitics_nav"}
    report = run_flipper_branch(
        flips, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 30),
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=400, seed=1,
    )
    leaf = next(g for g in report.tag_grains if g.grain == "leaf" and g.tag == "geopolitics")
    assert leaf.verdict == "inconclusive"


def test_fh4_low_population_caps_verdict_at_inconclusive():
    """Even with strong wallet-vs-peer separation, peer_count in
    [FH4_MIN_PEERS_FOR_INFERENCE, 30) keeps verdict at inconclusive.
    This pins flipper.py:737 — the low-population guard."""
    flips, leafs_by_cid, nav_lookup = _flat_flips(50, mean_roi=0.20, noise=0.01)
    report = run_flipper_branch(
        flips, wallet="0xself",
        # peer_count = 15 → low_population; would have been "supported"
        # without the guard.
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 15),
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=400, seed=1,
    )
    leaf = next(g for g in report.tag_grains if g.grain == "leaf" and g.tag == "geopolitics")
    assert leaf.verdict == "inconclusive"
    assert leaf.confidence_flag == "low_population"


def test_fh4_unit_level_pre_pool_verdict_is_inconclusive_with_p_raw():
    """The unit-level builder always emits inconclusive + _p_raw so the
    BH pool can promote. Direct callers of _build_fh4_grain must not
    short-circuit verdict assignment."""
    flips = _flips_n_roi(50, mean_roi=0.20, noise=0.01)
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=300, seed=1, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 20),
    )
    assert result.verdict == "inconclusive"
    # CI is on the excess (wallet - peer), so positive ci_lower → pool supports.
    assert result.ci_lower is not None and result.ci_upper is not None
    assert result.ci_lower > 0
    p_raw = getattr(result, "_p_raw", None)
    assert p_raw is not None and 0.0 <= p_raw <= 1.0
    # excess_pp must be in percentage points (× 100), matching types.py:114.
    assert result.excess_pp is not None
    assert 10.0 < result.excess_pp < 20.0


def test_fh4_underpowered_when_n_below_floor():
    """Wallet has fewer than FH4_MIN_FLIPS_FOR_INFERENCE flips."""
    n = FH4_MIN_FLIPS_FOR_INFERENCE - 1
    flips = _flips_n_roi(n, mean_roi=0.30)
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(n)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (0.05, 20),
    )
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "flip_count_below_floor"


def test_fh4_underpowered_when_peer_count_below_floor():
    """peer_count < FH4_MIN_PEERS_FOR_INFERENCE → underpowered."""
    n = FH4_MIN_FLIPS_FOR_INFERENCE + 10
    flips = _flips_n_roi(n, mean_roi=0.30)
    low_peers = FH4_MIN_PEERS_FOR_INFERENCE - 1
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(n)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (0.05, low_peers),
    )
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "peer_count_below_floor"


def test_fh4_inference_not_run_when_no_flip_roi_baseline_fn():
    """No fn → inference_not_run + typed reason."""
    flips = _flips_n_roi(50, mean_roi=0.20)
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=None,
    )
    assert result.verdict == "inference_not_run"
    assert result.underpowered_reason == "no_flip_roi_peer_baseline"


def test_fh4_inference_not_run_when_baseline_returns_none():
    """fn returns None (no peer data for this grain) → inference_not_run."""
    flips = _flips_n_roi(50, mean_roi=0.20)
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: None,
    )
    assert result.verdict == "inference_not_run"
    assert result.underpowered_reason == "no_flip_roi_peer_baseline"


def test_fh4_grain_financials_attached_regardless_of_verdict():
    """Edge Surface v1 financials attach in every code path."""
    flips = _flips_n_roi(50, mean_roi=0.10)
    # supported path
    r1 = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=lambda g, t, w: (-0.50, 20),
    )
    assert r1.grain_roi is not None
    assert r1.grain_win_rate is not None
    assert r1.grain_total_pnl is not None

    # inference_not_run path
    r2 = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=None,
    )
    assert r2.grain_roi is not None
    assert r2.grain_total_pnl is not None


def test_fh4_baseline_fn_receives_wallet_arg():
    """The wallet arg must be threaded through so the baseline can
    self-exclude. Verify with a spy."""
    flips = _flips_n_roi(50, mean_roi=0.20)
    captured: list[tuple[str, str, str]] = []

    def _spy(grain: str, tag: str, wallet: str) -> tuple[float, int]:
        captured.append((grain, tag, wallet))
        return (0.05, 20)

    _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xtarget",
        flip_roi_baseline_fn=_spy,
    )
    assert captured == [("leaf", "geopolitics", "0xtarget")]


# ======================================================================
# build_flip_roi_baseline_fn (per-call Supabase reads)
# ======================================================================

def _seed_peer_rows(db: StubSupabaseHttp, rows: list[dict]) -> None:
    table = db.rows_by_table.setdefault("wallet_flipper_tag_grain", [])
    table.extend(rows)


def test_baseline_fn_returns_mean_and_count_excluding_self():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xself", "grain": "leaf", "tag": "x", "grain_roi": 99.0, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xa", "grain": "leaf", "tag": "x", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xb", "grain": "leaf", "tag": "x", "grain_roi": 0.20, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_flip_roi_baseline_fn(db, wallet_protocol_version="v2", min_peer_flips=30)
    result = fn("leaf", "x", "0xself")
    assert result == (pytest.approx(0.15), 2)  # self excluded; only 0xa + 0xb


def test_baseline_fn_returns_none_when_no_peers():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xself", "grain": "leaf", "tag": "x", "grain_roi": 0.5, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_flip_roi_baseline_fn(db, wallet_protocol_version="v2")
    assert fn("leaf", "x", "0xself") is None


def test_baseline_fn_filters_low_peer_flips():
    """A peer wallet with n_at_grain < min_peer_flips is excluded."""
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xa", "grain": "leaf", "tag": "x", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xb", "grain": "leaf", "tag": "x", "grain_roi": 5.0, "n_at_grain": 5,  # too few flips
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_flip_roi_baseline_fn(db, wallet_protocol_version="v2", min_peer_flips=30)
    result = fn("leaf", "x", "0xself")
    # Only 0xa qualifies; 0xb has too few flips
    assert result == (pytest.approx(0.10), 1)


def test_baseline_fn_lowercases_self_exclusion():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xself", "grain": "leaf", "tag": "x", "grain_roi": 99.0, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xa", "grain": "leaf", "tag": "x", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_flip_roi_baseline_fn(db, wallet_protocol_version="v2")
    # Pass uppercase — should still self-exclude
    result = fn("leaf", "x", "0xSELF")
    assert result == (pytest.approx(0.10), 1)


# ======================================================================
# build_cached_flip_roi_baseline_fn (one-shot fetch + dict cache)
# ======================================================================

def test_cached_baseline_fn_serves_multiple_lookups_from_one_fetch():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xa", "grain": "leaf", "tag": "geopolitics", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xb", "grain": "leaf", "tag": "geopolitics", "grain_roi": 0.30, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xa", "grain": "nav", "tag": "sports", "grain_roi": -0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    pre_calls = len(db.calls)
    fn = build_cached_flip_roi_baseline_fn(db, wallet_protocol_version="v2")
    post_construct_calls = len(db.calls)
    # Construction does ONE select call
    assert post_construct_calls - pre_calls == 1

    # Multiple lookups don't add more calls
    _ = fn("leaf", "geopolitics", "0xself")
    _ = fn("nav", "sports", "0xself")
    _ = fn("leaf", "missing", "0xself")
    assert len(db.calls) == post_construct_calls

    geo = fn("leaf", "geopolitics", "0xself")
    assert geo == (pytest.approx(0.20), 2)
    sports = fn("nav", "sports", "0xself")
    assert sports == (-0.10, 1)
    missing = fn("leaf", "missing", "0xself")
    assert missing is None


def test_cached_baseline_fn_filters_low_peer_flips_at_build_time():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xa", "grain": "leaf", "tag": "x", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xb", "grain": "leaf", "tag": "x", "grain_roi": 5.0, "n_at_grain": 5,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_cached_flip_roi_baseline_fn(db, wallet_protocol_version="v2", min_peer_flips=30)
    result = fn("leaf", "x", "0xself")
    assert result == (pytest.approx(0.10), 1)


def test_cached_baseline_fn_self_exclusion_case_insensitive():
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": "0xself", "grain": "leaf", "tag": "x", "grain_roi": 99.0, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
        {"wallet": "0xa", "grain": "leaf", "tag": "x", "grain_roi": 0.10, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True},
    ])
    fn = build_cached_flip_roi_baseline_fn(db, wallet_protocol_version="v2")
    result = fn("leaf", "x", "0xSELF")
    assert result == (pytest.approx(0.10), 1)


# ======================================================================
# End-to-end: build baseline from stub + use it in FH4
# ======================================================================

def test_fh4_end_to_end_with_baseline_fn_from_supabase_stub():
    """Plug the cached baseline fn through `run_flipper_branch` —
    wallet is well above its peer set and should land 'supported'
    once the BH pool runs."""
    db = StubSupabaseHttp()
    # 30 peers in (leaf, geopolitics) with low ROI mean (≈0.05).
    # peer_count must reach PEER_COUNT_OK_THRESHOLD (30) for a supported
    # verdict to survive the post-pool low-population guard.
    _seed_peer_rows(db, [
        {"wallet": f"0xpeer{i:02d}", "grain": "leaf", "tag": "geopolitics",
         "grain_roi": 0.04 + (i % 3) * 0.01, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True}
        for i in range(30)
    ])
    fn = build_cached_flip_roi_baseline_fn(db, wallet_protocol_version="v2")

    flips, leafs_by_cid, nav_lookup = _flat_flips(50, mean_roi=0.30, noise=0.01)
    report = run_flipper_branch(
        flips, wallet="0xself",
        flip_roi_baseline_fn=fn,
        leafs_by_cid=leafs_by_cid, nav_lookup=nav_lookup,
        n_iter=400, seed=1,
    )
    leaf = next(g for g in report.tag_grains if g.grain == "leaf" and g.tag == "geopolitics")
    assert leaf.verdict == "supported"
    assert leaf.peer_count == 30
    assert leaf.metric_value > 0.25  # wallet's mean ROI
    assert leaf.pop_baseline < 0.10  # peer mean ROI


def test_fh4_end_to_end_underpowered_when_population_thin():
    """Mirrors the Phase 1.4 finding: 3 peers in the table → underpowered."""
    db = StubSupabaseHttp()
    _seed_peer_rows(db, [
        {"wallet": f"0xpeer{i}", "grain": "leaf", "tag": "geopolitics",
         "grain_roi": 0.05, "n_at_grain": 50,
         "status": "active", "protocol_version": "v2", "walk_full_history": True}
        for i in range(3)  # peer_count = 3 < FH4_MIN_PEERS_FOR_INFERENCE
    ])
    fn = build_cached_flip_roi_baseline_fn(db, wallet_protocol_version="v2")
    flips = _flips_n_roi(50, mean_roi=0.30)
    result = _build_fh4_grain(
        "leaf", "geopolitics", list(range(50)), flips,
        peer_baseline_fn=None, n_iter=200, seed=1, wallet="0xself",
        flip_roi_baseline_fn=fn,
    )
    assert result.verdict == "underpowered"
    assert result.underpowered_reason == "peer_count_below_floor"
