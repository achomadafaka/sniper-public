"""Deterministic exit-policy replay over recorded marks. ONE code path with the live rails
(ExitPolicy). Fills at the NEXT mark after a trigger, minus a p75 realized-slippage haircut; a
missing route is a recorded loss, never a theoretical exit. dedup_to_episode_outcomes enforces the
token-episode unit structurally (Global Constraints)."""
from __future__ import annotations
from sniper.services.memecoin_policy import control_reason


def slippage_haircut(conn, chain_id: str) -> float:
    """p75 realized execution impact from memecoin_exec_latency.impact_pct (the real, verified
    slippage column). exec_latency has NO chain column yet, so chain_id is accepted for the future
    per-chain split but not filtered on today. impact_pct may be stored as a percent or a fraction —
    auto-normalize. Fallback 0.10 when <5 samples (expected until real fills accumulate)."""
    vals = [abs(r[0]) for r in conn.execute(
        "SELECT impact_pct FROM memecoin_exec_latency WHERE impact_pct IS NOT NULL") if r[0] is not None]
    if len(vals) < 5:
        return 0.10
    vals.sort()
    h = vals[int(0.75 * (len(vals) - 1))]
    return h / 100.0 if h > 1.0 else h


def replay_position(policy, entry_price: float, marks: list[float], haircut: float) -> dict:
    """marks = the recorded mid price path (index 0 = entry sweep; callers read the `mid` column of
    memecoin_position_marks). Trigger checked on mark i, fill at mark i+1 (next observation) with the
    p75 realized-slippage `haircut`; if no next mark, fill at the trigger mark. (The marks table's
    `executable_exit_value` column is currently just a copy of `mid` and is not used here.)"""
    if not entry_price or entry_price <= 0 or not marks:
        return {"net_return": 0.0, "exit_reason": "no_data",
                "max_executable_return": 0.0, "max_drawdown": 0.0}
    peak = entry_price
    max_ret, max_dd = 0.0, 0.0
    for i, m in enumerate(marks):
        if m <= 0:
            continue
        peak = max(peak, m)
        max_ret = max(max_ret, m / entry_price - 1.0)
        max_dd = min(max_dd, m / peak - 1.0)
        pnl_pct = (m / entry_price - 1.0) * 100.0
        reason = control_reason(policy, pnl_pct)
        if reason:
            fill_mark = marks[i + 1] if i + 1 < len(marks) else m
            fill = fill_mark * (1.0 - haircut)
            return {"net_return": fill / entry_price - 1.0, "exit_reason": reason,
                    "max_executable_return": max_ret, "max_drawdown": max_dd}
    last = marks[-1] * (1.0 - haircut)
    return {"net_return": last / entry_price - 1.0, "exit_reason": "open_end",
            "max_executable_return": max_ret, "max_drawdown": max_dd}


def dedup_to_episode_outcomes(rows: list[dict]) -> list[dict]:
    """Structural dedup: at most one outcome per (episode_id, arm_id) — first seen wins."""
    seen, out = set(), []
    for r in rows:
        key = (r["episode_id"], r["arm_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
