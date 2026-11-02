"""WO-7 — execution tiering primitives: priority fees + depth-aware slippage.

Per the speed roadmap (brain: project-memecoin-execution-speed-roadmap), each execution upgrade gates
on MEASURED fill-delta, never vibes. These are pure, default-OFF building blocks; the runner passes
their output into the existing seam-based executors (which already take `slippage_ladder` as a
parameter). Nothing here is default-ON, and unknown depth fails SAFE to the static schedule.
"""
from __future__ import annotations


def priority_fee_microlamports(enabled, requested, cap):
    """Priority fee to attach to a Solana exit, in micro-lamports. Default-OFF (0 when disabled).
    [bound]: never exceeds `cap`, never negative. Bad inputs -> 0 (never spend an accidental fee)."""
    if not enabled:
        return 0
    try:
        req = int(requested)
        c = int(cap)
    except (TypeError, ValueError):
        return 0
    return max(0, min(req, c))


def depth_impact_estimate(reserve_base, bag_base):
    """Constant-product sell-impact fraction x/(R+x) in base-token atomic units.

    None when depth is unknown/invalid (caller then keeps the static schedule).
    # ponytail: CP heuristic, not a full curve solve — refine to per-AMM math if fills say it's off."""
    try:
        r = float(reserve_base)
        x = float(bag_base)
    except (TypeError, ValueError):
        return None
    if r <= 0 or x < 0:
        return None
    return x / (r + x)


def depth_aware_slippage_ladder(reserve_base, bag_base, base_ladder, *, enabled=False,
                                max_rung=0.25):
    """Size the ladder's FIRST rung from actual pool depth vs bag size instead of a static first rung.

    Disabled, or depth unknown -> the base ladder unchanged (fail-safe: a static schedule is never
    worse than a wrong depth estimate). Enabled + depth known -> the first rung is at least the
    estimated impact, [bound] to `max_rung`; larger base rungs are preserved above it. The ladder
    stays monotonic and never shrinks below the base first rung."""
    base = [float(s) for s in (base_ladder or [])]
    if not enabled:
        return base
    impact = depth_impact_estimate(reserve_base, bag_base)
    if impact is None:
        return base
    base_first = base[0] if base else 0.03
    first = min(float(max_rung), max(base_first, impact))
    rest = [s for s in base if s > first]
    return [first] + rest


def exit_tier_plan(position, base_ladder, *, chain, cfg):
    """Resolve the complete default-OFF tier plan at the executor boundary."""
    depth_on = bool(getattr(cfg, "MEMECOIN_EXIT_DEPTH_AWARE_SLIPPAGE_ENABLED", False))
    ladder = depth_aware_slippage_ladder(
        position.get("_exit_reserve_base"), position.get("_exit_bag_base"), base_ladder,
        enabled=depth_on,
        max_rung=getattr(cfg, "MEMECOIN_EXIT_DEPTH_MAX_RUNG_PCT", 0.25))
    fee = 0
    fee_on = str(chain) == "solana" and bool(
        getattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_ENABLED", False))
    if fee_on:
        fee = priority_fee_microlamports(
            True, getattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS", 0),
            getattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", 0))
    enabled = []
    if fee > 0:
        enabled.append("priority_fee")
    if depth_on and ladder != [float(x) for x in (base_ladder or [])]:
        enabled.append("depth_aware")
    return {"slippage_ladder": ladder, "priority_fee_microlamports": fee,
            "tier_variant": "+".join(enabled) if enabled else "baseline"}
