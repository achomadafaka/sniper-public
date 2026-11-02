"""FH4 must not consume held-only win rate as a flip ROI baseline.

Phase 2 invariant (post-2026-05-15 flipper-inference roadmap):
  * The legacy held-WR `peer_baseline_fn` argument to `_build_fh4_grain`
    is now IGNORED by FH4 — it is the wrong unit (binary win rate vs
    continuous flip ROI). Only `flip_roi_baseline_fn` drives inference.
  * Without `flip_roi_baseline_fn` (i.e. callers that pass only the
    legacy fn or nothing at all), FH4 returns `inference_not_run` —
    fails closed. This pin survives the Phase 2 rewrite.
"""
from __future__ import annotations

from sniper.analysis.wallet_protocol.flip_reconstruction import Flip
from sniper.analysis.wallet_protocol.flipper import _build_fh4_grain


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


def _wr_baseline_fn(grain: str, tag: str, wallet: str) -> tuple[float, int]:
    # Mirrors db.peer_baseline_for_grain: held-only win rate, not flip ROI.
    # Under Phase 2, this fn is IGNORED by FH4.
    return (0.65, 50)


def test_fh4_grain_returns_inference_not_run_under_held_wr_baseline():
    """Passing only the held-WR baseline (no flip_roi_baseline_fn) keeps
    FH4 in inference_not_run. The fn is ignored under Phase 2."""
    flips = [_flip(i, roi=0.20) for i in range(20)]
    result = _build_fh4_grain(
        "leaf", "politics", list(range(20)), flips,
        _wr_baseline_fn, n_iter=200, seed=1, wallet="0xabc",
    )

    assert result.verdict == "inference_not_run"


def test_fh4_grain_inference_not_run_even_when_peer_count_is_high():
    """High held-WR peer_count doesn't trigger inference. The number
    is from the wrong-unit fn; only flip_roi_baseline_fn matters."""
    flips = [_flip(i, roi=0.30) for i in range(50)]
    result = _build_fh4_grain(
        "nav", "geopolitics", list(range(50)), flips,
        lambda grain, tag, wallet: (0.80, 200),
        n_iter=200, seed=1, wallet="0xabc",
    )

    assert result.verdict == "inference_not_run"


def test_fh4_grain_inference_not_run_with_no_baseline_fn():
    """No baseline fn at all → inference_not_run. Fails closed."""
    flips = [_flip(i) for i in range(15)]
    result = _build_fh4_grain(
        "leaf", "politics", list(range(15)), flips, None,
        n_iter=200, seed=1, wallet="0xabc",
    )

    assert result.verdict == "inference_not_run"
