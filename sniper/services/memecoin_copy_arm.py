"""Funded rail mappings and frozen stall rules.

Legacy paper arms are descriptive only. Selection is disabled until separately
validated research and explicit funded promotion exist; admission uses its
existing band fallback. Frozen positions keep their original contract.
"""
from __future__ import annotations

import math

from sniper.settings import memecoin as cfg


# The fallback when a wallet has no usable arm evidence: the rails the funded leg runs today.
FALLBACK_ARM = "real_shadow"

# Minimum CLOSED copies before a wallet's arm may steer funded rails. `trader_arm_matrix` declares
# min_n=3 and never applies it; selection previously accepted a single close, which is max-of-k
# over eight arms on n=1. 3 is the matrix's own declared floor, honoured rather than reinvented.
MIN_CLOSES_FOR_FUNDED = int(getattr(cfg, "MEMECOIN_COPY_ARM_MIN_CLOSES", 3))

# Arms whose DEFINING behaviour the funded exit engine can actually execute.
#
# `momentum_a` (rollover_sweeps) and `peak_lock_e` (stall_sweeps) exit on "N consecutive sweeps
# with no new high". That counter is advanced on the funded path by the peak-stall rail in
# memecoin_real_runner, against the EXECUTABLE peak — see `_stall_params` for why the funded
# counter cannot simply reuse the paper one.
FUNDED_EXPRESSIBLE = frozenset({
    "control", "alpha_b", "real_shadow", "scalp_c", "scalp_c2", "combo_f", "trail_d",
    "peak_lock_e", "momentum_a",
})


# The paper bracket sweep the stall arms were MEASURED on runs about every 15s
# (scripts/memecoin_runner_loop.py: `if cycle - last_bracket > 15`). The funded fast lane ticks as
# often as every 1s (MEMECOIN_FASTLANE_TICK_S). A stall rule frozen as a SWEEP COUNT therefore means
# ~30s in paper and ~2s funded — a fifteen-fold tightening of the only rule that defines the arm.
# Freeze ELAPSED SECONDS instead, so the funded copy waits what the measured arm waited.
PAPER_SWEEP_S = 15.0


def _stall_params(arm):
    """The frozen no-new-high stall rule for `arm`, or None if it has none.

    `require_profit` is not decoration: the paper arms differ on it. peak_lock_e HARVESTS a stalled
    winner (`peak_gain >= activate_pct and pnl > 0`), while momentum_a ROLLS OUT of anything that
    stops making highs, in profit or not. Collapsing the two would turn one of them into a
    different strategy than the one whose PnL selected it.

    The rule is carried as elapsed SECONDS, not sweeps — see PAPER_SWEEP_S. `stall_sweeps` stays in
    the payload as provenance (which measured count produced the seconds), never as the runtime
    trigger. The funded clock runs against the EXECUTABLE peak, not the nominal one the paper arms
    use: a funded exit sells the real bag, and the nominal mid is explicitly a display surface on
    this path, so stalling on chart movement the position cannot realise would harvest a phantom.
    """
    if not cfg.MEMECOIN_REAL_AUTO_EXITS_ENABLED:
        return None    # operator manual mode: no funded stall harvest
    if arm == "peak_lock_e":
        sweeps = int(cfg.MEMECOIN_PEAK_E_STALL_SWEEPS)
        return {"activate_pct": float(cfg.MEMECOIN_PEAK_E_ACTIVATE_PCT),
                "stall_sweeps": sweeps, "stall_seconds": sweeps * PAPER_SWEEP_S,
                "require_profit": True}
    if arm == "momentum_a":
        sweeps = int(cfg.MEMECOIN_A_ROLLOVER_SWEEPS)
        return {"activate_pct": 0.0,
                "stall_sweeps": sweeps, "stall_seconds": sweeps * PAPER_SWEEP_S,
                "require_profit": False}
    return None


def _rails(arm, network_id, entry_mcap=None):
    """`_policy()` kwargs reproducing one paper arm's rails, from the SAME settings constants
    memecoin_policy uses to build that arm's frozen paper policy. Reading one source keeps the
    funded copy of an arm from drifting away from the paper arm it claims to be."""
    real_max_hold = float(cfg.MEMECOIN_REAL_MAX_HOLD_S)
    table = {
        # Wide TP, plain stop, no trail or de-risk stage.
        "control": dict(
            tp=float(cfg.MEMECOIN_TAKE_PROFIT_PCT), derisk=0.0, trail=0.0, trail_arm=0.0,
            trail_requires_derisk=False, stop=float(cfg.MEMECOIN_STOP_LOSS_PCT),
            max_hold=real_max_hold, mirror_on_sell=True),
        # Hold rails: one stop, a 12h cap, mirror their sell.
        "alpha_b": dict(
            tp=0.0, derisk=0.0, trail=0.0, trail_arm=0.0, trail_requires_derisk=False,
            stop=float(cfg.MEMECOIN_B_STOP_PCT), max_hold=float(cfg.MEMECOIN_B_MAX_HOLD_S),
            mirror_on_sell=True),
        # The rails the funded leg runs today; also the fallback.
        "real_shadow": dict(
            tp=float(cfg.MEMECOIN_REAL_TP_PCT), derisk=float(cfg.MEMECOIN_REAL_DERISK_PCT),
            trail=float(cfg.MEMECOIN_REAL_TRAIL_PCT),
            trail_arm=float(cfg.MEMECOIN_REAL_TRAIL_ARM_PCT), trail_requires_derisk=False,
            stop=float(cfg.real_stop_pct_for_chain(network_id)), max_hold=real_max_hold,
            mirror_on_sell=True),
        # Arm the trail at +30%, give back 25%, stop at 50%, 12h cap.
        "trail_d": dict(
            tp=0.0, derisk=0.0, trail=float(cfg.MEMECOIN_TRAIL_D_TRAIL_PCT),
            trail_arm=float(cfg.MEMECOIN_TRAIL_D_ACTIVATE_PCT), trail_requires_derisk=False,
            stop=float(cfg.MEMECOIN_TRAIL_D_STOP_PCT),
            max_hold=float(cfg.MEMECOIN_TRAIL_D_MAX_HOLD_S), mirror_on_sell=True),
        # Bank half at +20%, trail the rest by 30%, stop at 60%. The paper arm's max_hold of 0
        # means "no cap"; the funded contract requires a positive one, so it takes the funded
        # default week rather than inventing a shorter cap the paper arm never had.
        "combo_f": dict(
            tp=0.0, derisk=float(cfg.MEMECOIN_COMBO_F_TRIGGER_PCT), derisk_mode="fraction",
            derisk_fraction=float(cfg.MEMECOIN_COMBO_F_FRACTION),
            trail=float(cfg.MEMECOIN_COMBO_F_TRAIL_PCT), trail_arm=0.0,
            trail_requires_derisk=True, stop=float(cfg.MEMECOIN_COMBO_F_STOP_PCT),
            max_hold=float(cfg.MEMECOIN_COMBO_F_MAX_HOLD_S) or real_max_hold,
            mirror_on_sell=True),
        # Harvest a stalled winner: arm at +30%, exit after 2 sweeps with no new executable high.
        # The stall itself is frozen in the contract's copy_arm block, not in these rails.
        "peak_lock_e": dict(
            tp=0.0, derisk=0.0, trail=0.0,
            trail_arm=float(cfg.MEMECOIN_PEAK_E_ACTIVATE_PCT), trail_requires_derisk=False,
            stop=float(cfg.MEMECOIN_PEAK_E_STOP_PCT),
            max_hold=float(cfg.MEMECOIN_PEAK_E_MAX_HOLD_S), mirror_on_sell=True),
        # Fast herd-pump ride: trail 15%, stop 30%, roll out after 2 stalled sweeps, 15min cap.
        # mirror_on_sell is False — this arm deliberately does not follow the source wallet out.
        "momentum_a": dict(
            tp=0.0, derisk=0.0, trail=float(cfg.MEMECOIN_A_TRAIL_PCT), trail_arm=0.0,
            trail_requires_derisk=False, stop=float(cfg.MEMECOIN_A_STOP_PCT),
            max_hold=float(cfg.MEMECOIN_A_MAX_HOLD_S), mirror_on_sell=False),
    }
    # scalp_c / scalp_c2 are NOT pure mirrors. Corrected 2026-08-04 after review: an earlier
    # version of this table read their frozen `params` dict — which for scalp arms holds only
    # {mirror_on_sell, paper_max_hold_s, paper_unquotable_s} — and concluded they had no rails.
    # Their real rails live in a separate `exit_policy` key (memecoin_policy.resolve_paper_arm_policy),
    # and `_scalp_c2_policy` is explicit: "otherwise copies CONTROL exactly: wide -60% stop, NO time
    # cap, mirror the trader's sell". The arm changes ONLY the take-profit, banded by ENTRY MCAP.
    # Mapping them as floorless mirrors invented a strategy no paper cohort ever measured.
    #
    # Resolved from the same function the paper arm freezes, so the funded copy cannot drift from
    # the arm whose PnL selected it.
    if arm in ("scalp_c", "scalp_c2"):
        from sniper.services.memecoin_policy import resolve_policy
        resolved, _policy_id = resolve_policy({"entry_mcap": entry_mcap}, arm=arm)
        return dict(
            tp=float(resolved.tp_pct),
            derisk=(float(resolved.derisk_trigger_pct)
                    if resolved.derisk_trigger_pct else 0.0),
            trail=0.0, trail_arm=0.0, trail_requires_derisk=False,
            # ExitPolicy carries the stop as a NEGATIVE percent; the funded contract wants a
            # magnitude. abs() is the same convention memecoin_policy uses building it.
            stop=abs(float(resolved.stop_pct)),
            # A real stop means the catastrophe backstop belongs here exactly like every other
            # armed policy — it was only ever dropped by the floorless-mirror mistake.
            max_hold=(real_max_hold if not math.isfinite(resolved.max_hold_s)
                      else float(resolved.max_hold_s) or real_max_hold),
            mirror_on_sell=(str(resolved.mirror_sell_mode) == "on"),
        )
    rails = table.get(arm)
    if rails is None:
        raise ValueError(f"arm has no funded rail mapping: {arm!r}")
    # FUNDED-ONLY trail kill (2026-08-17 operator, HORNET: momentum_a's entry-armed 15% trail
    # sold a 22-second-old position at -19%). Zeroes the trail on the FUNDED copy of every arm;
    # the paper arms keep their trails, and peak-stall is untouched (the stall block is separate).
    if not cfg.MEMECOIN_REAL_COPY_ARM_TRAIL_ENABLED:
        rails = {**rails, "trail": 0.0, "trail_arm": 0.0, "trail_requires_derisk": False}
    # OPERATOR MANUAL MODE (2026-08-17 "no harvest and no stops"): the funded copy of every arm
    # keeps only mirror-on-sell; tp/derisk/trail/stop all zero. The catastrophe floor and the
    # timecap come from the policy layer and are NOT touched here — a manual book still needs
    # its last-resort backstops. Paper arms keep their full rails.
    if not cfg.MEMECOIN_REAL_AUTO_EXITS_ENABLED:
        rails = {**rails, "tp": 0.0, "derisk": 0.0, "trail": 0.0, "trail_arm": 0.0,
                 "trail_requires_derisk": False, "stop": 0.0}
        rails.pop("derisk_mode", None)
        rails.pop("derisk_fraction", None)
    return rails


def funded_policy_for_arm(arm, *, coin_band, network_id, entry_mcap=None):
    """The immutable funded exit policy reproducing `arm` for a position in `coin_band`."""
    from sniper.services.memecoin_funded_policy import _policy

    if arm not in FUNDED_EXPRESSIBLE:
        raise ValueError(f"arm is not expressible on the funded engine: {arm!r}")
    band = "nano" if coin_band == "assumed_nano" else coin_band
    if band not in ("nano", "micro", "small", "mid"):
        raise ValueError(f"coin band is not reservable: {coin_band!r}")
    return _policy(policy_id=f"copy-arm-{arm}-v1", coin_band=band,
                   **_rails(arm, network_id, entry_mcap))


def best_arm_cell(cells, *, min_closes=1):
    """The winning arm among `{arm: {pnl, closed, ...}}`, or None. THE one ranking rule.

    Shared with the Armed board's `best_arm` so the operator's screen and the money cannot pick
    differently — which they did before this existed: ties were broken by dict iteration order, so
    a 7-way tie could hand `scalp_c2` (no stop, no catastrophe floor) to a wallet whose evidence
    did not distinguish it from `control` (stop 60, floor 80), and show the operator the other one.

    Ties are common and are not a coin-flip to be resolved arbitrarily. Every arm exiting an event
    at the same price — because they all mirrored the source wallet's sell — is the NORMAL case,
    and it means the evidence does not separate them. So a tie resolves toward the arm we would
    have used with no evidence at all (the fallback), then deterministically by name. It never
    resolves toward whichever arm happened to hash first.

    `min_closes` is the EVIDENCE FLOOR. `trader_arm_matrix` declares `min_n=3` and never enforces
    it, so before this the winner could rest on a single closed copy — max-of-k over eight arms on
    n=1 is noise, not a measurement. Cells below the floor are not ranked at all; the caller falls
    back. Pass 0 only for a descriptive read that deliberately wants the unfiltered picture.
    """
    closed = [
        {"arm": arm, **cell} for arm, cell in (cells or {}).items()
        if int(cell.get("closed") or 0) >= max(1, int(min_closes))
    ]
    if not closed:
        return None
    return min(closed, key=lambda c: (-c["pnl"], c["arm"] != FALLBACK_ARM, c["arm"]))


def select_copy_arm(conn, source_user_id, *, chain):
    """Legacy paper paths cannot select policy. Existing band fallback is authoritative.

    Constant-time rejection: no I/O or evidence parsing on the funded entry path.
    Existing frozen contracts and direct operator policy mappings are unchanged.
    """
    return None


def select_from_cells(cells, *, chain):
    """Legacy paper paths cannot select policy. Existing band fallback is authoritative.

    Constant-time rejection: no I/O or evidence parsing on the funded entry path.
    Existing frozen contracts and direct operator policy mappings are unchanged.
    """
    return None


def position_stall_rule(pos):
    """The frozen stall rule for an open funded position, or None.

    Read from the position's authenticated contract, never from cfg — an open position keeps the
    rule it was opened under.
    """
    from sniper.services.memecoin_funded_policy import copy_arm_from_position

    arm = copy_arm_from_position(pos)
    stall = arm.get("stall") if isinstance(arm, dict) else None
    if not isinstance(stall, dict):
        return None
    try:
        sweeps = int(stall["stall_sweeps"])
        activate = float(stall["activate_pct"])
        # Seconds are the runtime trigger. A contract frozen before the cadence fix carries only
        # the sweep count; convert it at the cadence it was MEASURED at rather than running it at
        # the funded loop's much faster tick.
        seconds = float(stall.get("stall_seconds") or sweeps * PAPER_SWEEP_S)
    except (KeyError, TypeError, ValueError):
        return None
    if sweeps < 1 or seconds <= 0 or not (activate >= 0):
        return None
    return {"stall_sweeps": sweeps, "stall_seconds": seconds, "activate_pct": activate,
            "require_profit": bool(stall.get("require_profit"))}


def stall_sweep_count(prior, exact_mid, executable_peak):
    """The new no-new-high sweep count, or None when this sweep must not move it.

    None on a sweep with no usable EXACT price. The rail harvests on "the coin stopped making
    highs"; a provider outage, a blind chain, or a failed route quote is not that, and counting it
    would let infrastructure failure close a funded position. Pure, so the guard is testable
    without a runner.
    """
    try:
        mid = float(exact_mid)
        peak = float(executable_peak)
    except (TypeError, ValueError):
        return None
    if not (mid > 0) or not (peak > 0):
        return None
    return 0 if mid >= peak else int(prior or 0) + 1


def copy_arm_enabled():
    return False  # Publication proof is unavailable; config cannot unlock it.
