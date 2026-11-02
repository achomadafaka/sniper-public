"""Exit Attribution v1 — separate held-to-resolution prediction skill from
pre-resolution trading skill.

For each closed position, classify into one of:
    held_to_resolution_win       — held the position to market resolution
                                    and resolution went the wallet's way
                                    (curPrice ≈ 0/1 with no pre-res SELL).
    held_to_resolution_loss      — same, but resolution went against.
    pre_resolution_profitable_exit
                                  — exited at least once before resolution
                                    via SELL; total realizedPnl > 0.
    pre_resolution_loss_cut       — pre-res SELL present; realizedPnl ≤ 0.
    unresolved_or_unknown         — closed-positions row exists but curPrice
                                    not at resolution AND no SELL captured;
                                    likely a sale we did not see, or an early
                                    redeem, or partial-fill bookkeeping.
    insufficient_data             — activity history is incomplete relative
                                    to the position's timestamp; cannot
                                    distinguish SELL absence from SELL
                                    truncation. NEVER claim attribution
                                    when this is true.

This module is pure (no DB, no I/O). Display-only; consumers must NEVER
feed any of these labels into verdict, gate, or primary_edge_type logic.

Hard rule: when in doubt, prefer `insufficient_data` over guessing. The
operator-facing report would rather say "we don't know" than mis-attribute.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from sniper.analysis.wallet_protocol.held_analyst import (
    EPSILON_RESOLUTION,
    _has_pre_resolution_sell,
    _realized_pnl,
)


AttributionCategory = Literal[
    "held_to_resolution_win",
    "held_to_resolution_loss",
    "pre_resolution_profitable_exit",
    "pre_resolution_loss_cut",
    "unresolved_or_unknown",
    "insufficient_data",
]

ALL_CATEGORIES: tuple[AttributionCategory, ...] = (
    "held_to_resolution_win",
    "held_to_resolution_loss",
    "pre_resolution_profitable_exit",
    "pre_resolution_loss_cut",
    "unresolved_or_unknown",
    "insufficient_data",
)


def _at_resolution(curprice: float | None) -> bool:
    if curprice is None:
        return False
    cp = float(curprice)
    return cp <= EPSILON_RESOLUTION or cp >= (1.0 - EPSILON_RESOLUTION)


def _activity_min_ts(activity_rows: Sequence[Mapping]) -> int | None:
    """Earliest unix-ts among activity rows. Returns None when no rows."""
    if not activity_rows:
        return None
    timestamps = [int(r.get("timestamp") or 0) for r in activity_rows]
    timestamps = [t for t in timestamps if t > 0]
    return min(timestamps) if timestamps else None


def _position_outside_window(
    position: Mapping,
    activity_min_ts: int | None,
) -> bool:
    """True when the position's timestamp predates activity history.

    `position.timestamp` is the close time on /closed-positions. If close
    time precedes our oldest activity row, any SELL that closed the
    position is outside our window — we cannot reason about pre-res exits.
    """
    if activity_min_ts is None:
        return False  # we'll fall through to insufficient_data via has_history
    pos_ts = int(position.get("timestamp") or 0)
    if pos_ts <= 0:
        return False  # missing timestamp; let downstream classify
    return pos_ts < activity_min_ts


def classify_position(
    position: Mapping,
    activity_rows: Sequence[Mapping],
    *,
    activity_min_ts: int | None = None,
    has_full_history: bool = True,
) -> AttributionCategory:
    """Classify a single closed position.

    Args:
        position: one /closed-positions row.
        activity_rows: full TRADE-only activity sequence for the wallet.
        activity_min_ts: earliest activity timestamp (for window check).
            Pass None when activity_rows is empty.
        has_full_history: when False (e.g. activity_cap_hit AND no subgraph
            fallback), positions with no SELL match are downgraded to
            `insufficient_data` because SELL absence is unreliable.

    Decision tree (in order):
      1. No activity_rows at all → `insufficient_data`.
      2. Position timestamp predates oldest activity row → `insufficient_data`.
      3. SELL captured for (cid, outcome) → `pre_resolution_*` by PnL sign.
      4. curPrice at resolution AND history full → `held_to_resolution_*` by
         PnL sign.
      5. curPrice at resolution BUT history truncated → `insufficient_data`.
         The absent SELL might be inside the missed window, not actually
         absent. A wallet that bought, sold inside a missed REST window, and
         then watched the market resolve in its favor would otherwise be
         falsely tagged held-to-resolution. Conservative move: refuse to
         attribute.
      6. curPrice NOT at resolution + no SELL + history truncated →
         `insufficient_data` (we likely missed a SELL).
      7. curPrice NOT at resolution + no SELL + history full →
         `unresolved_or_unknown` (genuine ambiguity: early redeem, etc.).
    """
    if not activity_rows:
        return "insufficient_data"
    if _position_outside_window(position, activity_min_ts):
        return "insufficient_data"

    cid = position.get("conditionId") or ""
    outcome = position.get("outcome") or ""
    pnl = _realized_pnl(position)

    if cid and _has_pre_resolution_sell(cid, outcome, activity_rows):
        return (
            "pre_resolution_profitable_exit"
            if pnl > 0
            else "pre_resolution_loss_cut"
        )

    if _at_resolution(position.get("curPrice")):
        # Truncated history: cannot distinguish "held through" from
        # "sold inside the missed window then market resolved". Refuse
        # to attribute. Codex review item 1 (PR #9) — see commit
        # message for full rationale.
        if not has_full_history:
            return "insufficient_data"
        return (
            "held_to_resolution_win"
            if pnl > 0
            else "held_to_resolution_loss"
        )

    # curPrice not at resolution AND no SELL match. If history might be
    # incomplete, we cannot rule out a SELL we never saw.
    if not has_full_history:
        return "insufficient_data"
    return "unresolved_or_unknown"


def _empty_summary() -> dict[str, dict]:
    return {cat: {"n": 0, "total_pnl": 0.0} for cat in ALL_CATEGORIES}


def _add_to_summary(
    summary: dict[str, dict],
    category: AttributionCategory,
    pnl: float,
) -> None:
    bucket = summary[category]
    bucket["n"] += 1
    bucket["total_pnl"] = float(bucket["total_pnl"]) + float(pnl)


def compute_attribution_summary(
    closed_positions: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool = False,
) -> dict | None:
    """Compute an attribution summary across a closed-positions sequence.

    Returns None when there are no closed positions. Otherwise returns:
        {
            "summary": {category → {n, total_pnl}},
            "coverage": {
                "activity_cap_hit": bool,
                "subgraph_fallback_used": bool,
                "walk_full_history": bool,
                "activity_min_ts": int | None,
                "n_total": int,
                "n_outside_window": int,
                "history_assumed_full": bool,
                # R10 Option A:
                "n_with_resolved_attribution": int,
            },
        }

    `history_assumed_full` is True when (NOT activity_cap_hit) OR subgraph
    fallback ran OR the REST activity walk fetched full history — i.e. SELL
    absence is reliable evidence of holding.

    `n_with_resolved_attribution` (R10 Option A) is the count of positions
    classified as held_to_resolution_* OR pre_resolution_*. It excludes
    unresolved_or_unknown and insufficient_data. Used by the
    Completeness Surface ATTRIBUTION_COVERAGE_LOW caveat as a reliability
    signal. NOT used as Timing Surface's sample-size floor.
    """
    if not closed_positions:
        return None

    activity_min_ts = _activity_min_ts(activity_rows)
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history
    )
    n_outside = 0

    summary = _empty_summary()
    for pos in closed_positions:
        if _position_outside_window(pos, activity_min_ts):
            n_outside += 1
        cat = classify_position(
            pos, activity_rows,
            activity_min_ts=activity_min_ts,
            has_full_history=has_full_history,
        )
        _add_to_summary(summary, cat, _realized_pnl(pos))

    # R10 Option A: Reliable attribution = positions classified as
    # held_to_resolution_* OR pre_resolution_*. EXCLUDES
    # unresolved_or_unknown (we don't know what happened) and
    # insufficient_data (we couldn't determine). Used for the
    # operator-visible ATTRIBUTION_COVERAGE_LOW caveat (a *reliability*
    # signal); NOT used as Timing Surface's sample-size floor.
    n_with_resolved_attribution = (
        int(summary["held_to_resolution_win"]["n"])
        + int(summary["held_to_resolution_loss"]["n"])
        + int(summary["pre_resolution_profitable_exit"]["n"])
        + int(summary["pre_resolution_loss_cut"]["n"])
    )

    return {
        "summary": summary,
        "coverage": {
            "activity_cap_hit": bool(activity_cap_hit),
            "subgraph_fallback_used": bool(subgraph_fallback_used),
            "walk_full_history": bool(walk_full_history),
            "activity_min_ts": activity_min_ts,
            "n_total": len(closed_positions),
            "n_outside_window": n_outside,
            "history_assumed_full": has_full_history,
            # R10 Option A — see comment block above for semantics.
            "n_with_resolved_attribution": n_with_resolved_attribution,
        },
    }


def attribute_subset(
    rows: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool = False,
) -> dict[str, dict]:
    """Same per-category {n, total_pnl} shape but for a subset of closed
    positions (used by the per-bucket / per-grain attribution slices).
    """
    activity_min_ts = _activity_min_ts(activity_rows)
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history
    )
    summary = _empty_summary()
    for pos in rows:
        cat = classify_position(
            pos, activity_rows,
            activity_min_ts=activity_min_ts,
            has_full_history=has_full_history,
        )
        _add_to_summary(summary, cat, _realized_pnl(pos))
    return summary


__all__ = [
    "AttributionCategory",
    "ALL_CATEGORIES",
    "classify_position",
    "compute_attribution_summary",
    "attribute_subset",
]
