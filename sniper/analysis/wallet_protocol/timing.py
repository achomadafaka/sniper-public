"""Timing Surface v1 — captured-travel timing classification.

Pure-compute module mirroring `exit_attribution.py`. No I/O, no external
HTTP calls, no `price_history_fn` calls. Display-only; consumers MUST
NOT feed any of these results into verdict, gate, or primary_edge_type
logic.

Spec:  docs/superpowers/specs/2026-05-06-wallet-protocol-timing-surface-v1-design.md
       (commit d598cf2, r2)
Audit: docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md

Hard rules:
- Raw owned-token movement: favorable = later_price - entry_price for
  any outcome (YES, NO, OTHER, categorical). NO asymmetric flip for
  NO/Short. The audit's R1 finding documents the inverse mistake in
  flipper.py FH6.
- Held-bucket captured-travel uses universal payout (resolved token
  redeems at $1 win / $0 loss); coordinate-independent. Avoids the
  audit R2 unresolved coordinate question on closed_positions.curPrice.
- Pre-resolution captured-move uses size-weighted average SELL by
  default. Latest-SELL fallback ONLY when SELL `size` is invalid;
  per-position `sell_price_method` records which path ran.
- `n_unmapped_fills > 0` does NOT alone force `underpowered`. It fires
  the SUBGRAPH_UNMAPPED_FILLS caveat and drops `confidence_band` to
  "low" but the timing classification stands.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, NamedTuple


AttributionCategory = Literal[
    "held_to_resolution_win",
    "held_to_resolution_loss",
    "pre_resolution_profitable_exit",
    "pre_resolution_loss_cut",
    "unresolved_or_unknown",
    "insufficient_data",
]


def held_resolution_travel(
    position: Mapping,
    attribution_category: AttributionCategory,
) -> float:
    """Side-adjustment-free held-bucket captured-travel.

    Universal payout convention: a held token that wins resolves to $1;
    a held token that loses resolves to $0. This is true for YES, NO,
    OTHER, and any categorical outcome label, because resolution
    payouts are universal — not coordinate-dependent.

    + value = wallet captured price travel toward win.
    - value = wallet lost the entry premium.

    Args:
        position: a /closed-positions row (`avgPrice` is the held token's
            entry price per ADR-003).
        attribution_category: must be either "held_to_resolution_win"
            or "held_to_resolution_loss" (the only two held-bucket
            categories from exit_attribution).

    Raises:
        ValueError: when called with a non-held-bucket category.
    """
    entry = float(position.get("avgPrice") or 0.0)
    if attribution_category == "held_to_resolution_win":
        return 1.0 - entry        # token redeemed at $1
    if attribution_category == "held_to_resolution_loss":
        return -entry             # token redeemed at $0
    raise ValueError(
        f"held_resolution_travel called with non-held category: "
        f"{attribution_category!r}"
    )


class SwSellMoveResult(NamedTuple):
    """Per-position size-weighted SELL move outcome.

    `sw_move` is None when no SELL events exist for the (cid, outcome).
    `sell_price_method` is one of:
      - "size_weighted":         all SELLs had size > 0; sw_move uses
                                  the size-weighted average.
      - "latest_sell_fallback":  at least one SELL had missing/invalid
                                  size; sw_move uses the latest SELL.
      - "unavailable":           no SELL events for (cid, outcome).
    """
    sw_move: float | None
    sell_price_method: Literal["size_weighted", "latest_sell_fallback", "unavailable"]
    weighted_sell_price: float | None
    last_sell_price: float | None
    n_sell_fills: int


def _matching_sells(
    position: Mapping,
    activity_rows: Sequence[Mapping],
) -> list[Mapping]:
    """Return SELL rows for this position. Prefer stable token identity
    when both sides expose `asset`; fall back to (conditionId, outcome)
    only when the position has no asset id.

    This guards against the same class of label-based mismatch the
    audit flagged (an outcome string is not a stable identity in the
    way a CLOB token id is). Polymarket maps (cid, outcome) → asset
    1:1 today, but matching by asset is strictly safer when both sides
    carry the field.
    """
    pos_asset = str(position.get("asset") or "").strip()
    if pos_asset:
        return [
            r for r in activity_rows
            if (r.get("side") or "").upper() == "SELL"
            and str(r.get("asset") or "").strip() == pos_asset
        ]
    cid = position.get("conditionId") or ""
    outcome = position.get("outcome") or ""
    return [
        r for r in activity_rows
        if (r.get("side") or "").upper() == "SELL"
        and r.get("conditionId") == cid
        and (r.get("outcome") or "") == outcome
    ]


def sw_sell_move(
    position: Mapping,
    activity_rows: Sequence[Mapping],
) -> SwSellMoveResult:
    """Size-weighted captured move from entry to SELL price.

    Raw owned-token rule: favorable = later_price − entry_price.
    Both prices live in held-token coordinate (`avgPrice` per ADR-003;
    `activity_rows[].price` per fetch.py:120 = usdc_amt / outcome_amt).
    Label-agnostic — applies to YES, NO, OTHER, and categorical outcomes.

    Match path: prefer stable token identity (`asset` token id) when
    both position and activity rows carry it; fall back to
    (conditionId, outcome) only when the position has no asset id.

    Default value path: size-weighted average over ALL SELLs.
    Fallback value path (`latest_sell_fallback`): triggered when ANY
    SELL row has missing/invalid `size`; falls back to the latest SELL
    by timestamp. Ties on timestamp are broken by input order via
    stable max().
    """
    entry = float(position.get("avgPrice") or 0.0)
    sells = _matching_sells(position, activity_rows)
    n_sell_fills = len(sells)
    if n_sell_fills == 0:
        return SwSellMoveResult(
            sw_move=None,
            sell_price_method="unavailable",
            weighted_sell_price=None,
            last_sell_price=None,
            n_sell_fills=0,
        )

    last = max(sells, key=lambda r: int(r.get("timestamp") or 0))
    last_sell_price = float(last.get("price") or 0.0)

    # Size-weighted path requires every SELL to carry positive size.
    sizes = [float(r.get("size") or 0.0) for r in sells]
    if all(s > 0 for s in sizes):
        prices = [float(r.get("price") or 0.0) for r in sells]
        weight_sum = sum(sizes)
        weighted_sell_price = (
            sum(p * s for p, s in zip(prices, sizes)) / weight_sum
        )
        return SwSellMoveResult(
            sw_move=weighted_sell_price - entry,
            sell_price_method="size_weighted",
            weighted_sell_price=weighted_sell_price,
            last_sell_price=last_sell_price,
            n_sell_fills=n_sell_fills,
        )

    # Fallback: at least one SELL had invalid size.
    return SwSellMoveResult(
        sw_move=last_sell_price - entry,
        sell_price_method="latest_sell_fallback",
        weighted_sell_price=None,
        last_sell_price=last_sell_price,
        n_sell_fills=n_sell_fills,
    )


# ---------------------------------------------------------------------------
# Aggregator: compute_timing_surface
# ---------------------------------------------------------------------------

# r2 §5 thresholds — locked.
_N_ATTRIBUTED_FLOOR = 20
_DOMINANT_SHARE_THRESHOLD = 0.70
_MIXED_MIN_PER_BUCKET = 5

ClassificationLabel = Literal[
    "holding_driven",
    "trading_driven",
    "mixed",
    "unclear",
    "underpowered",
]

ConfidenceBand = Literal["high", "low", "underpowered"]


def _classify_one_position(
    position: Mapping,
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
) -> "AttributionCategory":
    """Re-categorize one closed position via exit_attribution.classify_position.

    `has_full_history` MUST OR in `walk_full_history` so the timing
    categorizer agrees with the coverage gate (which suppresses the
    ACTIVITY_TRUNCATED caveat when walk_full_history is True).

    Note: upstream `exit_attribution.compute_attribution_summary` does
    NOT currently OR in walk_full_history (audit followup tracked
    separately as `2026-05-06-exit-attribution-walk-full-history-
    coverage-fix.md`). Timing Surface fixes the derivation locally so
    this surface is correct independent of that upstream patch.
    """
    # Local import to keep timing.py free of cycles at module load.
    from sniper.analysis.wallet_protocol.exit_attribution import (
        _activity_min_ts, classify_position,
    )
    activity_min_ts = _activity_min_ts(activity_rows)
    has_full_history = (
        (not activity_cap_hit)
        or subgraph_fallback_used
        or walk_full_history
    )
    return classify_position(
        position, activity_rows,
        activity_min_ts=activity_min_ts,
        has_full_history=has_full_history,
    )


def compute_timing_surface(
    closed_positions: Sequence[Mapping],
    activity_rows: Sequence[Mapping],
    *,
    activity_cap_hit: bool,
    subgraph_fallback_used: bool,
    walk_full_history: bool,
    n_unmapped_fills: int,
    attribution_coverage_low: bool,
) -> dict | None:
    """Aggregate per-position attribution into a captured-travel timing
    surface. See spec §5–§6 (commit d598cf2, r2).

    Returns None when `closed_positions` is empty (mirrors
    exit_attribution.compute_attribution_summary).

    Display-only — NEVER feeds verdict, gate, or primary_edge_type.
    """
    if not closed_positions:
        return None

    # Per-position categorization + sw_sell_move calculation.
    held_wins: list[Mapping] = []
    held_losses: list[Mapping] = []
    pre_res_winners: list[tuple[Mapping, SwSellMoveResult]] = []
    pre_res_losers: list[tuple[Mapping, SwSellMoveResult]] = []
    n_unresolved = 0
    n_insufficient = 0

    sell_method_counts: dict[str, int] = {
        "size_weighted": 0,
        "latest_sell_fallback": 0,
        "unavailable": 0,
    }

    for pos in closed_positions:
        cat = _classify_one_position(
            pos, activity_rows,
            activity_cap_hit=activity_cap_hit,
            subgraph_fallback_used=subgraph_fallback_used,
            walk_full_history=walk_full_history,
        )
        if cat == "held_to_resolution_win":
            held_wins.append(pos)
        elif cat == "held_to_resolution_loss":
            held_losses.append(pos)
        elif cat == "pre_resolution_profitable_exit":
            sm = sw_sell_move(pos, activity_rows)
            sell_method_counts[sm.sell_price_method] += 1
            pre_res_winners.append((pos, sm))
        elif cat == "pre_resolution_loss_cut":
            sm = sw_sell_move(pos, activity_rows)
            sell_method_counts[sm.sell_price_method] += 1
            pre_res_losers.append((pos, sm))
        elif cat == "unresolved_or_unknown":
            n_unresolved += 1
        elif cat == "insufficient_data":
            n_insufficient += 1

    # Counts.
    held_n = len(held_wins) + len(held_losses)
    preres_n = len(pre_res_winners) + len(pre_res_losers)
    n_total_closed = len(closed_positions)
    # Per spec §5: n_attributed counts positions with meaningful attribution.
    # `insufficient_data` is explicitly EXCLUDED because we cannot reason
    # about timing on positions whose attribution is unreliable.
    n_attributed = held_n + preres_n + n_unresolved

    held_pnl = sum(float(p.get("realizedPnl") or 0.0) for p in held_wins + held_losses)
    preres_pnl = sum(
        float(p.get("realizedPnl") or 0.0)
        for p, _ in pre_res_winners + pre_res_losers
    )
    held_share = (held_n / n_attributed) if n_attributed > 0 else 0.0
    preres_share = (preres_n / n_attributed) if n_attributed > 0 else 0.0

    # r2 §5 — gating + caveats + confidence band split.
    activity_truncated = (
        activity_cap_hit and not (subgraph_fallback_used or walk_full_history)
    )
    force_underpowered = (
        n_attributed < _N_ATTRIBUTED_FLOOR
        or attribution_coverage_low
        or activity_truncated
    )
    coverage_caveats: list[str] = []
    if activity_truncated:
        coverage_caveats.append("ACTIVITY_TRUNCATED")
    if n_unmapped_fills > 0:
        coverage_caveats.append("SUBGRAPH_UNMAPPED_FILLS")
    if attribution_coverage_low:
        coverage_caveats.append("ATTRIBUTION_COVERAGE_LOW")

    if force_underpowered:
        confidence_band: ConfidenceBand = "underpowered"
    elif coverage_caveats:
        confidence_band = "low"
    else:
        confidence_band = "high"
    confidence_downgraded = (confidence_band == "low")

    # Back-compat: True only when force_underpowered AND the cause was a
    # coverage caveat (not just n_attributed < floor).
    coverage_downgraded = force_underpowered and (
        attribution_coverage_low or activity_truncated
    )

    # r2 §5 — classification rule. Dominant rules MUST evaluate before mixed.
    if force_underpowered:
        classification: ClassificationLabel = "underpowered"
        if n_attributed < _N_ATTRIBUTED_FLOOR:
            reason = f"n_attributed={n_attributed} below floor {_N_ATTRIBUTED_FLOOR}"
        else:
            reason = f"coverage caveats: {','.join(coverage_caveats)}"
    elif held_share >= _DOMINANT_SHARE_THRESHOLD and held_pnl > 0:
        classification = "holding_driven"
        reason = f"held_share={held_share:.2f}, held_pnl=${held_pnl:.2f}"
    elif preres_share >= _DOMINANT_SHARE_THRESHOLD and preres_pnl > 0:
        classification = "trading_driven"
        reason = f"preres_share={preres_share:.2f}, preres_pnl=${preres_pnl:.2f}"
    elif (held_pnl > 0 and preres_pnl > 0
          and held_n >= _MIXED_MIN_PER_BUCKET
          and preres_n >= _MIXED_MIN_PER_BUCKET):
        classification = "mixed"
        reason = (
            f"held_n={held_n}, preres_n={preres_n}, both buckets profitable"
        )
    else:
        classification = "unclear"
        reason = "neither bucket dominates and no clear positive concentration"

    # Bucket aggregates.
    held_to_resolution = _build_held_bucket(held_wins, held_losses)
    pre_resolution = _build_preres_bucket(pre_res_winners, pre_res_losers)
    pre_resolution["sell_price_method_counts"] = sell_method_counts

    return {
        "classification": classification,
        "classification_reason": reason,
        "confidence_band": confidence_band,
        "confidence_downgraded": confidence_downgraded,
        "coverage_downgraded": coverage_downgraded,
        "coverage_caveats": coverage_caveats,
        "n_attributed": n_attributed,
        "n_total_closed": n_total_closed,
        "held_to_resolution": held_to_resolution,
        "pre_resolution": pre_resolution,
        # Future strict-repricing reservation (v1.5 / H6). NEVER populated
        # in v1 — see spec §10 + audit followup ticket
        # 2026-05-06-fh6-no-side-flip-fix.md.
        "future_repricing_available": False,
        "future_repricing_signal": None,
        "future_repricing_window": None,
        "price_history_source": None,
    }


def _build_held_bucket(
    held_wins: Sequence[Mapping],
    held_losses: Sequence[Mapping],
) -> dict:
    n = len(held_wins) + len(held_losses)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0,
            "win_rate": None, "total_pnl": 0.0,
            "avg_captured_travel": None,
        }
    travels = (
        [held_resolution_travel(p, "held_to_resolution_win") for p in held_wins]
        + [held_resolution_travel(p, "held_to_resolution_loss") for p in held_losses]
    )
    pnls = [
        float(p.get("realizedPnl") or 0.0)
        for p in list(held_wins) + list(held_losses)
    ]
    return {
        "n": n,
        "wins": len(held_wins),
        "losses": len(held_losses),
        "win_rate": len(held_wins) / n,
        "total_pnl": float(sum(pnls)),
        "avg_captured_travel": sum(travels) / len(travels),
    }


def _build_preres_bucket(
    winners: Sequence[tuple[Mapping, SwSellMoveResult]],
    losers: Sequence[tuple[Mapping, SwSellMoveResult]],
) -> dict:
    n = len(winners) + len(losers)
    if n == 0:
        return {
            "n": 0, "wins": 0, "losses": 0,
            "win_rate": None, "total_pnl": 0.0,
            "avg_sw_sell_move_winners": None,
            "avg_sw_sell_move_losers": None,
        }
    pnls = [
        float(p.get("realizedPnl") or 0.0)
        for p, _ in list(winners) + list(losers)
    ]
    winner_moves = [sm.sw_move for _, sm in winners if sm.sw_move is not None]
    loser_moves = [sm.sw_move for _, sm in losers if sm.sw_move is not None]
    return {
        "n": n,
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / n,
        "total_pnl": float(sum(pnls)),
        "avg_sw_sell_move_winners": (
            sum(winner_moves) / len(winner_moves) if winner_moves else None
        ),
        "avg_sw_sell_move_losers": (
            sum(loser_moves) / len(loser_moves) if loser_moves else None
        ),
    }


__all__ = [
    "AttributionCategory",
    "ClassificationLabel",
    "ConfidenceBand",
    "SwSellMoveResult",
    "held_resolution_travel",
    "sw_sell_move",
    "compute_timing_surface",
]
