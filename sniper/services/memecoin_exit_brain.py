"""Candidate exit decision engine — V5-only, PURE. Proposes only, never submits.

`propose_v5(contract, state, cfg) -> CandidateDecision`. No DB, no network, no rail call. It cannot
nominate, size, sign, approve, or broadcast; the shadow adapter merely records what it said.

V2/V3 executable policies are RETIRED (2026-07-25 v5-only retirement). Only V5 is installed;
`exit_engine()` accepts "v5" and fails closed on anything else. Historical V2/V3 evidence rows are
preserved in the DB but no V2/V3 code path runs.

Every rule here is FROZEN in docs/superpowers/plans/2026-07-24-memecoin-exit-engine-v3-prereg.md §2
(carried forward unchanged into V5; V5 differs only in registered partial-size and band-horizon
values). Changing a rule or constant requires a new dated prereg + a version bump, not an edit here.

Doctrine enforced structurally, not by discipline:
- Every rule shortens the hold or protects the tail; none adds size, widens a stop, raises a TP,
  resets a peak, or re-enters (MONOTONIC RISK-REDUCTION ONLY).
- SOURCE_UNTRUSTED is a global veto; every other rule declares and checks only the feature families
  it actually consumes.
- An EXIT maps onto an EXISTING funded reason; the engine never invents an exit route.
"""
from __future__ import annotations

from dataclasses import dataclass

ENGINE_VERSION_V5 = "exit-brain-v5"

DEFER = "DEFER_TO_LEGACY"

# a structure-break exit lands on the existing trail reason (memecoin_real_runner._EXECUTABLE_PRICE_REASONS)
_EXIT_REASON = "real_trail"

RIDE = "RIDE"                 # positive assertion: the tail is alive. NOT the same as DEFER.
DERISK = "DERISK"
TAKE_PROFIT = "TAKE_PROFIT"
CUT = "CUT"


@dataclass(frozen=True)
class CandidateDecision:
    action: str
    reason_codes: tuple = ()
    no_action_reason: str | None = None
    exit_reason: str | None = None        # set only when action == EXIT / CUT
    proposed_fraction: float | None = None    # set only when action == DERISK / TAKE_PROFIT


def _vol_blowoff(realized_vol, trail_pct):
    """True when one window's realized vol has reached the live trail distance — i.e. the move is
    outside its own noise. Parameter-free by construction (prereg §2.2): vol is compared to the
    LIVE trail, never to a new constant."""
    if realized_vol is None:
        return False
    try:
        t = float(trail_pct)
        v = float(realized_vol)
    except (TypeError, ValueError):
        return False
    return t > 0 and v >= (t / 100.0)


def _net_flow(state):
    try:
        return int(state.net_flow_quote) if state.net_flow_quote is not None else None
    except (TypeError, ValueError):
        return None


# WO-1 per-rule evidence contracts — the exact feature families each rule needs provenance-`trusted`
# before it may act. Subsets of {price, flow, vol, depth}; "structure" is carried by `price` (pivots
# off the exec-price path) and "arrival" by `flow` (rates off the same swap source). LP CUT needs
# ONLY depth so a decoded burn cuts through quiet-tape stale swaps (the missed-rug fix). `()` = a
# position/ops-only rule with no market-data family; only the actionable-label gate applies.
_FAM_LP_CUT = ("depth",)
_FAM_UNSELLABLE = ()
_FAM_STRUCTURE = ("price", "flow")
_FAM_TAKE_PROFIT = ("price", "vol", "flow")
_FAM_VOL_DERISK = ("price", "vol")
_FAM_TIME_DERISK = ()
_FAM_RIDE = ("price", "flow")

# WO-3 LP-removal severity ------------------------------------------------------------------------
from sniper.analysis.memecoin_lp_removal import (  # noqa: E402
    LP_CATASTROPHIC, LP_MATERIAL, LP_MINOR, classify_lp_removal,
)


def _lp_severity_bounds(cfg, chain_id):
    bounds = getattr(cfg, "MEMECOIN_LP_SEVERITY_BOUNDS", {}) or {}
    value = bounds.get(str(chain_id or "").strip().lower())
    if not isinstance(value, dict):
        return None, None
    try:
        minor = float(value["minor"])
        catastrophic = float(value["catastrophic"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not (0.0 < minor < catastrophic <= 1.0):
        return None, None
    return minor, catastrophic


def _propose_quant(contract, state, cfg, *, tp_fraction, vol_derisk_fraction, lp_fraction,
                   time_fraction, edge_duration_s, route_degraded, route_gone,
                   executable_no_progress, structure_cut_ready, no_signal_reason):
    """The V5 quant rule tree. Policy values (partial size, band horizon) enter through arguments.
    WO-1: each rule is gated by its OWN required families (above), not one profile-wide gate, so a
    fresh family can act while an unrelated stale one cannot veto it.

    WO-5B: each partial action is sized by its OWN function; a None fraction means the required
    sizing evidence is missing, so the rule proposes NO partial (defers) rather than a fabricated
    one. `route_degraded`/`route_gone`/`executable_no_progress` arrive pre-resolved from the
    controller-state feed (propose_v5). One partial per position: TP/DERISK are suppressed once a
    partial has been banked (derisked_at) — a full CUT stays available (it outranks them here)."""
    # SOURCE_UNTRUSTED is the only global veto. Explicit per-rule contracts intentionally remain
    # eligible through profile WARMUP/staleness/insufficiency; their own family gates below decide
    # whether the evidence that rule actually reads is trusted.
    if not state.candidate_capable(()) and state.state.value == "source_untrusted":
        return CandidateDecision(
            action=DEFER, reason_codes=("not_candidate_capable", f"state:{state.state.value}"),
            no_action_reason=f"state {state.state.value} is not candidate-capable")

    get = contract.get if hasattr(contract, "get") else (lambda *_a, **_k: None)
    trail_pct = get("trail_pct")
    held_s = get("held_s")
    misses = get("exact_quote_misses") or 0
    unsellable_n = int(getattr(cfg, "MEMECOIN_REAL_UNSELLABLE_SWEEPS", 0) or 0)
    net = _net_flow(state)
    # One partial per position (prereg §2.3, WO-5B escalation): any prior banked partial suppresses
    # every later PARTIAL action. Full CUTs are unaffected — they outrank the partials below.
    already_derisked = get("derisked_at") not in (None, "")

    # --- 2.1 CUT, highest precedence -------------------------------------------------
    # WO-3: a decoded LP removal is graded by severity — catastrophic CUTs, material partial-derisks,
    # minor falls through (the executor refreshes the route). DEGENERATE until WO-7 registers bounds:
    # every burn is catastrophic -> CUT, unchanged.
    if state.depth_pull_flag is True and state.candidate_capable(_FAM_LP_CUT):
        minor_frac, catastrophic_frac = _lp_severity_bounds(cfg, state.chain_id)
        # Fraction thresholds alone are not enough to soften a rug exit. Until the funded adapter
        # supplies an exact pre/post route-health comparison, stay in conservative degenerate mode.
        if get("lp_route_health_complete") is not True:
            minor_frac, catastrophic_frac = None, None
        severity = classify_lp_removal(
            getattr(state, "liquidity_removed_fraction", None),
            minor_frac=minor_frac, catastrophic_frac=catastrophic_frac,
            route_degraded=bool(route_degraded), route_gone=bool(route_gone))
        if severity == LP_CATASTROPHIC:
            return CandidateDecision(
                action=CUT, exit_reason=_EXIT_REASON,
                reason_codes=("cut", "lp_pull", "depth_pull_flag", f"lp_severity:{severity}"))
        if severity == LP_MATERIAL and not already_derisked:
            return CandidateDecision(
                action=DERISK, proposed_fraction=lp_fraction,
                reason_codes=("derisk", "lp_removal", "depth_pull_flag", f"lp_severity:{severity}"))
        if (severity == LP_MATERIAL and already_derisked
                and get("partial_lp_escalation") == "escalate_to_cut"):
            # WO-13 preregistered alternative. Runtime omits this contract field until a complete
            # policy revision passes its one held-out read, so today's `suppress` behavior is
            # byte-for-byte unchanged. Replay can evaluate both full-policy inventories now.
            return CandidateDecision(
                action=CUT, exit_reason=_EXIT_REASON,
                reason_codes=("cut", "lp_removal", "material_after_partial",
                              f"lp_severity:{severity}"))
        # minor -> no LP action; fall through to the other rules on this state.
    if (unsellable_n > 0 and int(misses) >= unsellable_n
            and state.candidate_capable(_FAM_UNSELLABLE)):
        return CandidateDecision(action=CUT, exit_reason=_EXIT_REASON,
                                 reason_codes=("cut", "unsellable", f"misses:{int(misses)}"))
    price, prior_low = state.nominal_price, state.prior_low
    if (price is not None and prior_low is not None and price < prior_low
            and state.candidate_capable(_FAM_STRUCTURE)):
        if net is None:
            return CandidateDecision(
                action=DEFER, reason_codes=("break_nominated", "flow_unreadable"),
                no_action_reason="structure break nominated but net flow is unreadable")
        if net < 0:
            if structure_cut_ready is False:
                return CandidateDecision(
                    action=DEFER,
                    reason_codes=("break_nominated", "structure_hysteresis_pending"),
                    no_action_reason="structure break has fewer than two decoded confirmations")
            return CandidateDecision(
                action=CUT, exit_reason=_EXIT_REASON,
                reason_codes=("cut", "break_confirmed", "structure_break_below_prior_low",
                              "flow_negative"))
        return CandidateDecision(
            action=DEFER, reason_codes=("break_nominated", "flow_not_confirming"),
            no_action_reason="structure break nominated but flow did not confirm (net >= 0)")

    blowoff = _vol_blowoff(state.realized_vol, trail_pct)

    # --- 2.2 TAKE_PROFIT: sell into strength, never at a fixed +X% -------------------
    # WO-5B: sized by size_take_profit (None -> no size -> no partial); suppressed once a partial has
    # been banked (one partial per position). A climax with an intact higher-low then RIDEs.
    if (state.arrival_accel is not None and state.arrival_accel > 0
            and blowoff and state.higher_low_state is True and not already_derisked
            and tp_fraction is not None and state.candidate_capable(_FAM_TAKE_PROFIT)):
        return CandidateDecision(
            action=TAKE_PROFIT, proposed_fraction=tp_fraction,
            reason_codes=("take_profit", "climax", "arrival_accel_up", "vol_blowoff"))

    # --- 2.3 DERISK: bank half, keep the tail ---------------------------------------
    # Once per position (prereg §2.3). Without this the engine re-proposed DERISK on EVERY pass and
    # would have bled the position out in halves — P1, Codex 2026-07-24. `already_derisked` (above)
    # fails closed: any non-empty / unreadable `derisked_at` counts as done.
    if (not already_derisked and state.lower_high_state is True and blowoff
            and vol_derisk_fraction is not None and state.candidate_capable(_FAM_VOL_DERISK)):
        return CandidateDecision(action=DERISK, proposed_fraction=vol_derisk_fraction,
                                 reason_codes=("derisk", "lower_high", "vol_expansion"))
    try:
        # Age alone is not evidence of staleness. The executable no-progress window comes from the
        # WO-5A controller peak (executable_no_progress, pre-resolved by propose_v5); until WO-7
        # registers that window it is False, so this rule has zero authority on age alone.
        if (not already_derisked and executable_no_progress is True
                and held_s is not None and float(held_s) >= edge_duration_s
                and state.candidate_capable(_FAM_TIME_DERISK)):
            return CandidateDecision(
                action=DERISK, proposed_fraction=time_fraction,
                reason_codes=("derisk", "edge_duration_elapsed", f"held_s:{int(float(held_s))}"))
    except (TypeError, ValueError):
        pass

    # --- 2.4 RIDE: the trend is intact; do NOT cap the winner ------------------------
    if (state.higher_low_state is True and net is not None and net >= 0
            and state.candidate_capable(_FAM_RIDE)):
        return CandidateDecision(action=RIDE,
                                 reason_codes=("ride", "higher_low", "flow_non_negative"))

    # --- 2.5 nothing to say ----------------------------------------------------------
    return CandidateDecision(action=DEFER, reason_codes=("no_signal",),
                             no_action_reason=no_signal_reason)


def propose_v5(contract, state, cfg=None) -> CandidateDecision:
    """The registered V5 policy: the quant precedence with flexible partial size and band horizon.
    Pure; proposes only. This is the single installed decision entry point."""
    from sniper.analysis.exit_controller import (
        executable_no_progress as _no_progress, structure_cut_confirmed)
    from sniper.analysis.exit_flex_v5 import (
        band_edge_duration_s, clamp_partial_fraction, route_health, size_lp_derisk,
        size_take_profit, size_time_derisk, size_vol_derisk, vol_reference_ratio)

    get = contract.get if hasattr(contract, "get") else (lambda *_a, **_k: None)
    strength = vol_reference_ratio(getattr(state, "realized_vol", None), get("trail_pct"))
    edge_duration = band_edge_duration_s(get("coin_band") or "unknown")

    # WO-5B route-degradation + no-progress feed: the WO-5A controller state (carried on the contract
    # when the builder supplies it) resolves route_degraded/route_gone and executable_no_progress. An
    # explicit contract value wins (existing callers/tests); absent everything, the feed is
    # conservative (empty state + unregistered windows -> False), so runtime is unchanged until WO-7.
    ctrl = get("controller_state")
    route_degraded, route_gone = bool(get("route_degraded")), bool(get("route_gone"))
    executable_no_progress = get("executable_no_progress")
    structure_cut_ready = None
    if ctrl is not None:
        fed_degraded, fed_gone = route_health(
            ctrl, gone_streak=getattr(cfg, "MEMECOIN_ROUTE_GONE_STREAK", None))
        route_degraded, route_gone = route_degraded or fed_degraded, route_gone or fed_gone
        if executable_no_progress is None:
            executable_no_progress = _no_progress(
                ctrl, now=(get("now_iso") or state.observed_time or state.computed_time),
                no_progress_window_s=getattr(cfg, "MEMECOIN_NO_PROGRESS_WINDOW_S", None),
                quote_fresh=bool(get("quote_fresh")))
        structure_cut_ready = structure_cut_confirmed(
            ctrl, now=(get("now_iso") or state.observed_time or state.computed_time),
            persistence_window_s=getattr(cfg, "MEMECOIN_STRUCTURE_BREAK_WINDOW_S", None),
            min_break_events=2)

    remaining_budget = 0.0 if get("derisked_at") not in (None, "") else 1.0
    slippage_cap_max = get("max_fraction_inside_slippage_cap")
    tp_fraction = clamp_partial_fraction(
        size_take_profit(strength_ratio=strength),
        slippage_cap_max=slippage_cap_max, remaining_budget=remaining_budget)
    vol_fraction = clamp_partial_fraction(
        size_vol_derisk(strength_ratio=strength),
        slippage_cap_max=slippage_cap_max, remaining_budget=remaining_budget)
    lp_fraction = clamp_partial_fraction(
        size_lp_derisk(), slippage_cap_max=slippage_cap_max, remaining_budget=remaining_budget)
    time_fraction = clamp_partial_fraction(
        size_time_derisk(), slippage_cap_max=slippage_cap_max, remaining_budget=remaining_budget)

    return _propose_quant(
        contract, state, cfg,
        tp_fraction=tp_fraction, vol_derisk_fraction=vol_fraction,
        lp_fraction=lp_fraction, time_fraction=time_fraction,
        edge_duration_s=edge_duration, route_degraded=route_degraded, route_gone=route_gone,
        executable_no_progress=(executable_no_progress is True),
        structure_cut_ready=structure_cut_ready,
        no_signal_reason="no v5 trigger met")


def exit_engine(version):
    """V5-only decision-engine factory. Returns (identity, proposer) for "v5"; every other value —
    including retired "v2"/"v3", None, and blanks — fails closed. This is the enforcement point that
    refuses to run a frozen contract whose decision version is not v5 (never a silent upgrade)."""
    if str(version or "").strip().lower() == "v5":
        return ENGINE_VERSION_V5, propose_v5
    raise ValueError("exit engine is v5-only (v2/v3 retired); got %r" % (version,))
