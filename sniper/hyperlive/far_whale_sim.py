"""fwlm-v0 paper simulator — §7 exits, all levels FROZEN at signal_ts.

Cost model (pre-registered): pnl_bps = s*(exit_mid-entry_mid)/entry_mid*1e4
- FEE_BPS - spread_bps_at_signal. MFE/MAE on mids, gross. Price path is the
market-state poll series (~180s granularity) — touch detection is
poll-granular, stated honestly in the report (§14).

Exit precedence per step (frozen, non-ambiguous by construction): target and
adverse levels sit on opposite sides of entry_price (target moves TOWARD the
liq level, adverse moves AWAY from it), so a single mid can never satisfy both
trigger conditions in the same bar. Order checked: target_moved (whale moved
the reference liq level — never re-target mid-trade) > target_hit >
adverse_stop > time_stop.

This module is a PURE function library over caller-supplied price paths — no
live network calls, no DB access. `sniper.hyperlive.far_whale_store` persists
what this module computes; the two are never mixed here.

`simulate_path` distinguishes three terminal outcomes so dropped-vs-logged
accounting (Tasks 6-7) stays honest: empty path -> None (no data at all);
non-empty path that never triggers an exit -> a settle()-shaped dict with
exit_reason="unresolved" (truncated coverage, still loggable); non-empty path
that triggers an exit -> the normal resolved dict, exit_reason ∈
{target_moved, target_hit, adverse_stop, time_stop}. See its docstring for
the full contract. `plan_entry`/`simulate_path` both require D>0 (distance
in price terms) and raise ValueError otherwise — D<=0 would collapse
target/adverse to entry_price and resolve trivially on the first tick.
"""
from __future__ import annotations

from sniper.hyperlive.far_whale import (ADVERSE_FRAC, FEE_BPS, HORIZON_MS,
                                        MIGRATE_MIN_FRAC, PAPER_NOTIONAL, TARGET_FRAC)


def _sign(direction: str) -> float:
    return 1.0 if direction == "up" else -1.0


def plan_entry(candidate: dict, signal_ts_ms: int, *,
               paper_notional: float = PAPER_NOTIONAL) -> dict:
    """§7: freeze D = |liq - mid| and the symbolic target/adverse levels at signal_ts.
    Never recomputed from later ticks.

    Precondition: D = |liq - mid| must be > 0. At D<=0 target_price ==
    adverse_stop_price == entry_price, so target_hit fires trivially on the first
    tick — fails loudly instead of silently fabricating a resolved trade."""
    mid = float(candidate["mid_price"])
    liq = float(candidate["liquidation_price"])
    s = _sign(candidate["direction_to_liquidation"])
    d_abs = abs(liq - mid)                     # frozen D in price terms
    if d_abs <= 0:
        raise ValueError(
            f"plan_entry: |liquidation_price - mid_price| must be > 0, got "
            f"liq={liq!r} mid={mid!r} (D<=0 collapses target/adverse to entry_price "
            "and resolves trivially on the first tick)"
        )
    return {
        "asset": candidate["asset"],
        "direction_to_liquidation": candidate["direction_to_liquidation"],
        "entry_ts_ms": int(signal_ts_ms),
        "entry_price": mid,
        "paper_notional": float(paper_notional),
        "d_bps": float(candidate["distance_bps"]),
        "target_price": mid + s * TARGET_FRAC * d_abs,
        "adverse_stop_price": mid - s * ADVERSE_FRAC * d_abs,
        "liquidation_price": liq,
        "spread_bps": float(candidate.get("spread_bps") or 0.0),
        "mfe_bps": 0.0,
        "mae_bps": 0.0,
    }


def step_entry(entry: dict, mid: float, ts_ms: int, *, whale_event: str | None = None,
               horizon_ms: int = HORIZON_MS) -> dict | None:
    """One poll round. Mutates entry["mfe_bps"]/["mae_bps"] in place; returns an exit
    dict or None if still open. Exit priority: target_moved > target_hit >
    adverse_stop > time_stop (see module docstring — target/adverse can't co-trigger)."""
    s = _sign(entry["direction_to_liquidation"])
    excursion_bps = s * (mid - entry["entry_price"]) / entry["entry_price"] * 1e4
    entry["mfe_bps"] = max(entry["mfe_bps"], excursion_bps)
    entry["mae_bps"] = min(entry["mae_bps"], excursion_bps)
    if whale_event == "liq_price_moved":
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "target_moved"}
    if s * (mid - entry["target_price"]) >= 0:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "target_hit"}
    if s * (mid - entry["adverse_stop_price"]) <= 0:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "adverse_stop"}
    if ts_ms - entry["entry_ts_ms"] >= horizon_ms:
        return {"exit_ts_ms": int(ts_ms), "exit_price": float(mid),
                "exit_reason": "time_stop"}
    return None


def settle(entry: dict, exit: dict, *, fee_bps: float = FEE_BPS,
           position_persisted: int | None = None, liq_fired: int | None = None,
           horizon_ms: int = HORIZON_MS) -> dict:
    """§8 outcome labels. Includes fee_assumption = the fee_bps actually applied so
    far_whale_store.save_outcome persists an auditable data fact per entry (spec
    §10/§16), not just a value recoverable via study_version -> FEE_BPS lookup."""
    s = _sign(entry["direction_to_liquidation"])
    gross_bps = s * (exit["exit_price"] - entry["entry_price"]) / entry["entry_price"] * 1e4
    net_bps = gross_bps - fee_bps - entry.get("spread_bps", 0.0)
    liq_excursion_bps = s * (entry["liquidation_price"] - entry["entry_price"]) \
        / entry["entry_price"] * 1e4
    return {
        "exit_ts_ms": exit["exit_ts_ms"],
        "exit_price": exit["exit_price"],
        "exit_reason": exit["exit_reason"],
        "paper_pnl": net_bps / 1e4 * entry["paper_notional"],
        "mfe_bps": entry["mfe_bps"],
        "mae_bps": entry["mae_bps"],
        "migrated_toward_liq": 1 if entry["mfe_bps"] >= MIGRATE_MIN_FRAC * entry["d_bps"] else 0,
        "hit_liq_level": 1 if entry["mfe_bps"] >= liq_excursion_bps else 0,
        "liq_fired": liq_fired,
        "position_persisted": position_persisted,
        "horizon_ms": int(horizon_ms),
        "fee_assumption": float(fee_bps),
    }


def simulate_path(direction: str, d_bps: float, path: list, *, spread_bps: float,
                  fee_bps: float = FEE_BPS, horizon_ms: int = HORIZON_MS) -> dict | None:
    """Pure replay over [(ts_ms, mid), ...]; path[0] is the entry round. Shared by the
    control generator (Tasks 6-7) and horizon diagnostics — the same exit engine as
    live stepping, so controls and treatment run through one identical cost/exit model.

    Return contract (three distinguishable outcomes — callers must not collapse
    "no data" into "ran out of time"):
      - empty path            -> None (no data at all; nothing to log).
      - non-empty, resolves    -> settle()-shaped dict, exit_reason ∈
                                   {target_moved, target_hit, adverse_stop, time_stop}
                                   (UNCHANGED from before this fix).
      - non-empty, never fires -> settle()-shaped dict with exit_reason="unresolved",
                                   exit_price/exit_ts_ms pinned to the last observed
                                   point in the path, carrying whatever partial
                                   mfe_bps/mae_bps accumulated. This is loggable
                                   (truncated coverage before the horizon), distinct
                                   from both a resolved exit and from no data (None).

    Precondition: d_bps must be > 0. At d_bps<=0 the synthesized D collapses
    target_price/adverse_stop_price to entry_price and target_hit would fire
    trivially on the first tick — fails loudly instead of fabricating a resolved
    trade. This guard matters because Tasks 6-7 call this with synthesized d_bps
    outside the upstream 50bps candidate gate."""
    if d_bps <= 0:
        raise ValueError(
            f"simulate_path: d_bps must be > 0, got {d_bps!r} (D<=0 collapses "
            "target/adverse to entry_price and resolves trivially on the first tick)"
        )
    if not path:
        return None
    t0, mid0 = path[0]
    s = _sign(direction)
    pseudo = {
        "asset": "", "direction_to_liquidation": direction, "entry_ts_ms": int(t0),
        "entry_price": float(mid0), "paper_notional": PAPER_NOTIONAL, "d_bps": float(d_bps),
        "target_price": mid0 + s * TARGET_FRAC * (d_bps / 1e4) * mid0,
        "adverse_stop_price": mid0 - s * ADVERSE_FRAC * (d_bps / 1e4) * mid0,
        "liquidation_price": mid0 + s * (d_bps / 1e4) * mid0,
        "spread_bps": float(spread_bps), "mfe_bps": 0.0, "mae_bps": 0.0,
    }
    last_ts, last_mid = t0, mid0
    exit_ = None
    for ts, mid in path[1:]:
        last_ts, last_mid = ts, mid
        exit_ = step_entry(pseudo, mid, ts, horizon_ms=horizon_ms)
        if exit_:
            break
    if exit_ is None:      # path ended before horizon — unresolved but loggable
        exit_ = {"exit_ts_ms": int(last_ts), "exit_price": float(last_mid),
                  "exit_reason": "unresolved"}
    return settle(pseudo, exit_, fee_bps=fee_bps, horizon_ms=horizon_ms)
