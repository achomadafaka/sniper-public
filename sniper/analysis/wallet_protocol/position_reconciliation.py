"""Position Reconciliation surface — `position_reconciliation_surface`.

Spec: docs/superpowers/specs/2026-05-23-position-reconciliation-surface-design.md

Reconciles `/closed-positions` against CTF Redemption events to detect
API undercounting. Reports the gap ratio, acquisition-rail breakdown, and
sets `downstream_lower_bound` when the gap exceeds the threshold. This
is a honesty flag, not a corrector — it does NOT impute hidden-slice PnL
or adjust any other surface's numbers.

HARD WALL — display-only. `compute_position_reconciliation_surface` is
read ONLY by the report renderer. It MUST NEVER feed `overall_verdict`,
`gate_result`, `primary_edge_type`, `copyability_note`, `trading_mode`,
branch/hypothesis logic, or any other producer's numbers. In particular,
`downstream_lower_bound` is informational — it does NOT mutate any sibling
surface's inputs. No `data_quality_flags` append; caveats live in the
surface's own `caveats` list. Pure function — no I/O. Deterministic.
"""
from __future__ import annotations

SURFACE_VERSION = 1
UNDERCOUNT_PROXY_ONLY_THRESHOLD = 0.05      # gap > 5% → proxy_only
ACQUISITION_GAP_NOTABLE_THRESHOLD = 0.10    # untraced gap > 10% → UNTRACED_ACQUISITION_RAIL

_SEVERITY = {"ok": 0, "proxy_only": 1, "data_unavailable": 2}


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def compute_position_reconciliation_surface(
    closed_positions: list[dict],
    activity_rows: list[dict],
    redemptions: list[dict] | None,
    splits: list[dict] | None,
    merges: list[dict] | None,
    activity_cap_hit: bool,
    n_unmapped_fills: int,
) -> dict:
    """Pure, deterministic. Returns the position_reconciliation_surface dict.

    `redemptions=None` means the fetch failed; `redemptions=[]` means the
    fetch succeeded but the wallet has no Redemption events — the surface
    distinguishes these via the caveat split (spec §6).

    `splits=None` / `merges=None` means that aux-stream fetch failed.
    This is distinct from `splits=[]` (fetch succeeded, no events).
    Per spec §5.1/§5.3: a failed aux stream leaves its rail count as None
    (unknown, not measured-zero), skips its set subtraction in the
    untraced_redemption arithmetic, emits ACQUISITION_RAIL_FETCH_PARTIAL_<rail>,
    and suppresses UNTRACED_ACQUISITION_RAIL + nulls acquisition_rail_gap_share.
    data_state is NOT escalated to data_unavailable for aux-stream failures
    (only redemptions=None escalates).

    `activity_cap_hit` and `n_unmapped_fills` are used only for the
    ORDERBOOK_RAIL_INCOMPLETE caveat — they do not affect set arithmetic.
    """
    # -----------------------------------------------------------------------
    # §6 data_state early-exit: data_unavailable cases
    # -----------------------------------------------------------------------
    if redemptions is None:
        # Fetch failure — we can't measure anything meaningful.
        return {
            "surface_version": SURFACE_VERSION,
            "data_state": "data_unavailable",
            "closed_positions_count": None,
            "closed_condition_count": None,
            "redeemed_condition_count": None,
            "redeemed_not_in_closed_count": None,
            "undercount_ratio": None,
            "redeemed_orderbook_traced_count": None,
            "untraced_redemption_count": None,
            "rails_observed": None,
            "acquisition_rail_gap_share": None,
            "condition_set_summary": None,
            "downstream_lower_bound": False,
            "note": (
                "Redemption event fetch failed. Position reconciliation unavailable."
            ),
            "caveats": ["RECONCILIATION_INPUTS_MISSING"],
        }

    if not closed_positions and not redemptions:
        # Wallet has nothing to reconcile.
        return {
            "surface_version": SURFACE_VERSION,
            "data_state": "data_unavailable",
            "closed_positions_count": 0,
            "closed_condition_count": 0,
            "redeemed_condition_count": 0,
            "redeemed_not_in_closed_count": 0,
            "undercount_ratio": None,
            "redeemed_orderbook_traced_count": 0,
            "untraced_redemption_count": 0,
            "rails_observed": {"orderbook": 0, "redemption": 0, "split": 0, "merge": 0},
            "acquisition_rail_gap_share": None,
            "condition_set_summary": {"closed_only": 0, "redeemed_only": 0, "both": 0},
            "downstream_lower_bound": False,
            "note": (
                "No closed positions or redemption events found. "
                "Nothing to reconcile."
            ),
            "caveats": ["NO_CLOSED_OR_REDEEMED_CONDITIONS"],
        }

    # -----------------------------------------------------------------------
    # §5.1 Build condition sets
    # Track whether aux streams are available (None = fetch failed).
    # The None vs [] distinction is critical: None → rail count unknown;
    # [] → rail count is honestly 0.
    # -----------------------------------------------------------------------
    splits_unavailable = (splits is None)
    merges_unavailable = (merges is None)

    closed_cond: set[str] = {
        p["conditionId"].lower()
        for p in closed_positions
        if p.get("conditionId")
    }
    redeemed_cond: set[str] = {
        e["condition"].lower()
        for e in (redemptions or [])
        if e.get("condition")
    }
    # Build aux-rail sets only when the stream is available.
    # When unavailable, hold None (not an empty set) so downstream
    # code can distinguish "measured-zero" from "unknown".
    split_cond: set[str] | None = (
        None if splits_unavailable
        else {
            e["condition"].lower()
            for e in splits  # type: ignore[union-attr]  # not None here
            if e.get("condition")
        }
    )
    merge_cond: set[str] | None = (
        None if merges_unavailable
        else {
            e["condition"].lower()
            for e in merges  # type: ignore[union-attr]  # not None here
            if e.get("condition")
        }
    )
    orderbook_cond: set[str] = {
        r["conditionId"].lower()
        for r in activity_rows
        if r.get("type") == "TRADE" and r.get("conditionId")
    }

    # -----------------------------------------------------------------------
    # §5.2 Compute counts
    # -----------------------------------------------------------------------
    closed_positions_count = len(closed_positions)
    closed_condition_count = len(closed_cond)
    redeemed_condition_count = len(redeemed_cond)

    redeemed_not_in_closed = redeemed_cond - closed_cond
    redeemed_not_in_closed_count = len(redeemed_not_in_closed)

    redeemed_orderbook_traced = redeemed_not_in_closed & orderbook_cond
    redeemed_orderbook_traced_count = len(redeemed_orderbook_traced)

    # Subtract only the rails we actually have data for.
    # When an aux rail is None (fetch failed), skipping its subtraction
    # means untraced_redemption_count becomes an UPPER BOUND: the missing
    # rail could have traced some of these conditions.
    untraced_redemption = redeemed_not_in_closed - orderbook_cond
    if not splits_unavailable:
        untraced_redemption -= split_cond  # type: ignore[arg-type]  # not None
    if not merges_unavailable:
        untraced_redemption -= merge_cond  # type: ignore[arg-type]  # not None
    untraced_redemption_count = len(untraced_redemption)

    condition_set_summary = {
        "closed_only": len(closed_cond - redeemed_cond),
        "redeemed_only": len(redeemed_cond - closed_cond),
        "both": len(closed_cond & redeemed_cond),
    }

    # -----------------------------------------------------------------------
    # §5.3 Rails observed + partial-stream and orderbook-rail caveats
    # A rail value is None when that stream's fetch failed (unknown, not 0).
    # -----------------------------------------------------------------------
    rails_observed = {
        "orderbook": len(orderbook_cond),
        "redemption": redeemed_condition_count,
        "split": None if splits_unavailable else len(split_cond),  # type: ignore[arg-type]
        "merge": None if merges_unavailable else len(merge_cond),  # type: ignore[arg-type]
    }

    caveats: list[str] = []
    if activity_cap_hit and n_unmapped_fills > 0:
        caveats.append(f"ORDERBOOK_RAIL_INCOMPLETE_n={n_unmapped_fills}")

    # Emit one caveat per failed aux stream (informational; does not change data_state).
    if splits_unavailable:
        caveats.append("ACQUISITION_RAIL_FETCH_PARTIAL_splits")
    if merges_unavailable:
        caveats.append("ACQUISITION_RAIL_FETCH_PARTIAL_merges")

    # -----------------------------------------------------------------------
    # §5.2 Ratios
    # acquisition_rail_gap_share is None when any aux stream is unavailable —
    # the gap is unknowable without all acquisition rails.
    # -----------------------------------------------------------------------
    denom = max(redeemed_condition_count, 1)
    undercount_ratio = redeemed_not_in_closed_count / denom
    any_aux_unavailable = splits_unavailable or merges_unavailable
    acquisition_rail_gap_share = (
        None if any_aux_unavailable
        else untraced_redemption_count / denom
    )

    # -----------------------------------------------------------------------
    # §6 data_state worst-wins
    # -----------------------------------------------------------------------
    data_state = "ok"
    downstream_lower_bound = False

    if undercount_ratio > UNDERCOUNT_PROXY_ONLY_THRESHOLD:
        data_state = _worse(data_state, "proxy_only")
        downstream_lower_bound = True
        pct = int(round(undercount_ratio * 100))
        caveats.append(f"CLOSED_POSITIONS_UNDERCOUNT_{pct}")
        # UNTRACED_ACQUISITION_RAIL is suppressed when any aux stream is None:
        # acquisition_rail_gap_share is None in that case, so the > threshold
        # predicate cannot be evaluated honestly. The ACQUISITION_RAIL_FETCH_PARTIAL_*
        # caveats already name the gap.
        if (
            acquisition_rail_gap_share is not None
            and acquisition_rail_gap_share > ACQUISITION_GAP_NOTABLE_THRESHOLD
        ):
            caveats.append("UNTRACED_ACQUISITION_RAIL")

    # -----------------------------------------------------------------------
    # §5.4 Note rendering
    # -----------------------------------------------------------------------
    if downstream_lower_bound:
        aux_note = ""
        if any_aux_unavailable:
            failed_rails = []
            if splits_unavailable:
                failed_rails.append("splits")
            if merges_unavailable:
                failed_rails.append("merges")
            aux_note = (
                f"; auxiliary rail data for {'/'.join(failed_rails)} is unavailable, "
                f"so untraced-acquisition figures are an upper bound"
            )
        note = (
            f"Closed-positions API undercount detected. "
            f"{redeemed_not_in_closed_count} redeemed conditionIds are not "
            f"surfaced by /closed-positions "
            f"({int(round(undercount_ratio * 100))}%). "
            f"Other surfaces in this report are computed on the visible "
            f"closed book and should be read as lower bounds{aux_note}."
        )
    else:
        note = (
            "Closed-positions API reconciles with redemption events within "
            "the tolerance band. No lower-bound caveat applied to downstream "
            "surfaces."
        )

    return {
        "surface_version": SURFACE_VERSION,
        "data_state": data_state,
        "closed_positions_count": closed_positions_count,
        "closed_condition_count": closed_condition_count,
        "redeemed_condition_count": redeemed_condition_count,
        "redeemed_not_in_closed_count": redeemed_not_in_closed_count,
        "undercount_ratio": undercount_ratio,
        "redeemed_orderbook_traced_count": redeemed_orderbook_traced_count,
        "untraced_redemption_count": untraced_redemption_count,
        "rails_observed": rails_observed,
        "acquisition_rail_gap_share": acquisition_rail_gap_share,
        "condition_set_summary": condition_set_summary,
        "downstream_lower_bound": downstream_lower_bound,
        "note": note,
        "caveats": caveats,
    }
