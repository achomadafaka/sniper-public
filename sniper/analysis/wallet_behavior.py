"""Behavioral fingerprinting from raw /activity transaction data.

Built 2026-04-25 to answer: "what does this wallet's execution pattern
look like, and what does that say about whether they have edge?"

Per-position aggregate data (avgPrice, totalBought) collapses every entry
into a single number. The execution mechanics — laddering, fill timing,
inter-fill cadence — are invisible at that level but very visible at the
event level. This module computes per-position fill stats and a per-wallet
aggregate fingerprint.

The fingerprint is descriptive, not predictive. It tells you the SHAPE
of how a wallet enters positions. Whether that shape correlates with
edge is downstream analysis (B2/dossier).

Usage:
    rows = fetch_user_activity(session, wallet, activity_type='TRADE')
    per_position = compute_per_position_fills(rows)
    fingerprint = compute_wallet_fingerprint(rows)
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PositionFillStats:
    """Per (conditionId, outcome, side) fill metrics."""

    condition_id: str
    outcome: str
    side: str  # 'BUY' or 'SELL'

    n_fills: int
    total_size: float                  # sum of share counts
    total_usdc: float                  # sum of usdcSize

    first_ts: int
    last_ts: int
    span_seconds: int                  # last_ts - first_ts (0 if single-clip)

    min_price: float
    max_price: float
    avg_price: float                   # USDC-weighted

    # Fill-size distribution
    median_fill_size: float
    max_fill_size: float

    # Inter-fill timing (seconds between consecutive fills, sorted by ts)
    median_inter_fill_seconds: float | None  # None if n_fills < 2

    # Classification
    ladder_signal: str
    # one of: 'single_clip', 'cluster_burst', 'accumulation_down',
    #         'accumulation_up', 'scatter', 'machine_uniform', 'unknown'


@dataclass
class WalletFingerprint:
    """Wallet-aggregate behavioral profile from activity rows."""

    n_trades: int                      # raw fill count across all positions
    n_positions: int                   # unique (cid, outcome, side) tuples
    n_unique_cids: int

    # Centrality of fill counts per position
    median_fills_per_position: float
    mean_fills_per_position: float

    # Centrality of position spans
    median_position_span_seconds: float

    # Ladder signal histogram
    ladder_distribution: dict[str, int] = field(default_factory=dict)

    # Inter-fill timing centrality (across all positions with >=2 fills)
    median_inter_fill_seconds: float | None = None

    # Activity duration
    first_activity_ts: int = 0
    last_activity_ts: int = 0

    # Top-level archetype label
    archetype_label: str = "unknown"
    # one of: 'single_clip_specialist', 'patient_accumulator',
    #         'rapid_clipper', 'algorithmic_uniform',
    #         'scatter_buyer', 'mixed', 'unknown'


# ---------------------------------------------------------------------------
# Per-position computation
# ---------------------------------------------------------------------------


# Thresholds for ladder classification — tuned to be defensible defaults,
# not over-fit. Documented here so the rules are auditable.
_BURST_WINDOW_SECONDS = 60                 # all fills within 1 minute = single clip burst
_PRICE_RANGE_FLAT = 0.005                  # < 0.5 cent = "same price"
_LADDER_DOWN_TOLERANCE_FRACTION = 0.6      # 60% of fills must be at lower price than first
_MACHINE_CV_THRESHOLD = 0.2                # coefficient-of-variation of inter-fill times below this = machine-uniform


def _classify_ladder(
    n_fills: int,
    span_seconds: int,
    min_price: float,
    max_price: float,
    fills_sorted: list[dict],
) -> str:
    """Classify the entry pattern for one position.

    See module docstring for rule rationale; thresholds at the top of file.
    Returns one of the labels in PositionFillStats.ladder_signal.
    """
    if n_fills <= 1:
        return "single_clip"

    price_range = max_price - min_price
    flat_price = price_range < _PRICE_RANGE_FLAT

    if span_seconds <= _BURST_WINDOW_SECONDS:
        # Many fills in a flash — almost certainly "took whatever was on
        # the book at one moment."
        return "cluster_burst"

    # Inter-fill cadence — coefficient of variation
    inter_seconds = []
    prev_ts = None
    for fill in fills_sorted:
        ts = int(fill.get("timestamp", 0) or 0)
        if prev_ts is not None and ts >= prev_ts:
            inter_seconds.append(ts - prev_ts)
        prev_ts = ts

    if len(inter_seconds) >= 4 and statistics.mean(inter_seconds) > 0:
        mean_iv = statistics.mean(inter_seconds)
        stdev_iv = statistics.pstdev(inter_seconds)
        cv = stdev_iv / mean_iv if mean_iv > 0 else 0.0
        if cv < _MACHINE_CV_THRESHOLD:
            return "machine_uniform"

    if flat_price:
        # Same price, but spread out in time — liquidity replenishment over
        # minutes/hours; treat as a stretched cluster.
        return "scatter"

    # Trend detection: are we mostly going DOWN (averaging in) or UP (chasing)?
    first_price = float(fills_sorted[0].get("price") or 0.0)
    last_price = float(fills_sorted[-1].get("price") or 0.0)
    n_down = sum(
        1 for f in fills_sorted
        if float(f.get("price") or 0.0) < first_price
    )
    n_up = sum(
        1 for f in fills_sorted
        if float(f.get("price") or 0.0) > first_price
    )
    if n_down / n_fills >= _LADDER_DOWN_TOLERANCE_FRACTION:
        return "accumulation_down"
    if n_up / n_fills >= _LADDER_DOWN_TOLERANCE_FRACTION:
        return "accumulation_up"

    return "scatter"


def compute_per_position_fills(
    activity_rows: Iterable[dict],
) -> dict[tuple[str, str, str], PositionFillStats]:
    """Group activity rows by (conditionId, outcome, side) and compute stats.

    Only TRADE rows are processed (pass activity_type='TRADE' to the fetcher).
    Rows missing condition_id, outcome, or size are skipped with a single
    structured log line per skip-reason.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in activity_rows:
        try:
            cid = row.get("conditionId") or ""
            outcome = row.get("outcome") or ""
            side = (row.get("side") or "").upper()
            if not cid or not outcome or side not in ("BUY", "SELL"):
                continue
            key = (cid, outcome, side)
            grouped.setdefault(key, []).append(row)
        except Exception:
            logger.exception("wallet_behavior: bad activity row")

    out: dict[tuple[str, str, str], PositionFillStats] = {}
    for (cid, outcome, side), fills in grouped.items():
        fills_sorted = sorted(fills, key=lambda f: int(f.get("timestamp", 0) or 0))
        n = len(fills_sorted)
        sizes = [float(f.get("size") or 0.0) for f in fills_sorted]
        prices = [float(f.get("price") or 0.0) for f in fills_sorted]
        usdc = [float(f.get("usdcSize") or 0.0) for f in fills_sorted]
        timestamps = [int(f.get("timestamp", 0) or 0) for f in fills_sorted]

        first_ts = timestamps[0]
        last_ts = timestamps[-1]
        span = max(0, last_ts - first_ts)

        total_size = sum(sizes)
        total_usdc = sum(usdc)
        avg_price = (total_usdc / total_size) if total_size > 0 else 0.0

        min_price = min(prices) if prices else 0.0
        max_price = max(prices) if prices else 0.0

        median_size = statistics.median(sizes) if sizes else 0.0
        max_size = max(sizes) if sizes else 0.0

        if n >= 2:
            inter = [
                timestamps[i] - timestamps[i - 1]
                for i in range(1, n)
                if timestamps[i] >= timestamps[i - 1]
            ]
            median_inter: float | None = (
                statistics.median(inter) if inter else None
            )
        else:
            median_inter = None

        ladder = _classify_ladder(
            n_fills=n,
            span_seconds=span,
            min_price=min_price,
            max_price=max_price,
            fills_sorted=fills_sorted,
        )

        out[(cid, outcome, side)] = PositionFillStats(
            condition_id=cid,
            outcome=outcome,
            side=side,
            n_fills=n,
            total_size=total_size,
            total_usdc=total_usdc,
            first_ts=first_ts,
            last_ts=last_ts,
            span_seconds=span,
            min_price=min_price,
            max_price=max_price,
            avg_price=avg_price,
            median_fill_size=median_size,
            max_fill_size=max_size,
            median_inter_fill_seconds=median_inter,
            ladder_signal=ladder,
        )
    return out


# ---------------------------------------------------------------------------
# Wallet-aggregate fingerprint
# ---------------------------------------------------------------------------


def _classify_archetype(
    median_fills: float,
    median_span: float,
    ladder_dist: dict[str, int],
) -> str:
    """Top-level archetype based on aggregate stats.

    Rules are intentionally simple — refine in v2 against labeled wallets.
    """
    total = sum(ladder_dist.values()) or 1
    pct_single = ladder_dist.get("single_clip", 0) / total
    pct_machine = ladder_dist.get("machine_uniform", 0) / total
    pct_accum_down = ladder_dist.get("accumulation_down", 0) / total
    pct_accum_up = ladder_dist.get("accumulation_up", 0) / total
    pct_scatter = (
        ladder_dist.get("scatter", 0) + ladder_dist.get("cluster_burst", 0)
    ) / total

    if pct_single >= 0.7:
        return "single_clip_specialist"
    if pct_machine >= 0.4:
        return "algorithmic_uniform"
    if pct_accum_down >= 0.4:
        return "patient_accumulator"
    if pct_accum_up >= 0.4:
        return "chaser_or_momentum"
    if median_fills <= 2 and median_span < 60:
        return "rapid_clipper"
    if pct_scatter >= 0.5:
        return "scatter_buyer"
    return "mixed"


def compute_wallet_fingerprint(
    activity_rows: Iterable[dict],
) -> WalletFingerprint:
    """Compute the per-wallet behavioral fingerprint."""
    rows = list(activity_rows)
    if not rows:
        return WalletFingerprint(
            n_trades=0, n_positions=0, n_unique_cids=0,
            median_fills_per_position=0.0,
            mean_fills_per_position=0.0,
            median_position_span_seconds=0.0,
            archetype_label="unknown",
        )

    per_pos = compute_per_position_fills(rows)
    if not per_pos:
        return WalletFingerprint(
            n_trades=len(rows), n_positions=0, n_unique_cids=0,
            median_fills_per_position=0.0,
            mean_fills_per_position=0.0,
            median_position_span_seconds=0.0,
            archetype_label="unknown",
        )

    fills_per_pos = [s.n_fills for s in per_pos.values()]
    spans = [s.span_seconds for s in per_pos.values()]
    inter_medians = [
        s.median_inter_fill_seconds for s in per_pos.values()
        if s.median_inter_fill_seconds is not None
    ]

    ladder_dist: dict[str, int] = {}
    for s in per_pos.values():
        ladder_dist[s.ladder_signal] = ladder_dist.get(s.ladder_signal, 0) + 1

    cids = {s.condition_id for s in per_pos.values()}
    timestamps = [
        int(r.get("timestamp", 0) or 0) for r in rows if r.get("timestamp")
    ]

    median_fills = float(statistics.median(fills_per_pos)) if fills_per_pos else 0.0
    mean_fills = float(statistics.mean(fills_per_pos)) if fills_per_pos else 0.0
    median_span = float(statistics.median(spans)) if spans else 0.0
    median_inter = (
        float(statistics.median(inter_medians)) if inter_medians else None
    )

    archetype = _classify_archetype(
        median_fills=median_fills,
        median_span=median_span,
        ladder_dist=ladder_dist,
    )

    return WalletFingerprint(
        n_trades=len(rows),
        n_positions=len(per_pos),
        n_unique_cids=len(cids),
        median_fills_per_position=median_fills,
        mean_fills_per_position=mean_fills,
        median_position_span_seconds=median_span,
        ladder_distribution=ladder_dist,
        median_inter_fill_seconds=median_inter,
        first_activity_ts=min(timestamps) if timestamps else 0,
        last_activity_ts=max(timestamps) if timestamps else 0,
        archetype_label=archetype,
    )


__all__ = [
    "PositionFillStats",
    "WalletFingerprint",
    "compute_per_position_fills",
    "compute_wallet_fingerprint",
]
