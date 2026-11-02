"""Selection gate — transparent green/red/insufficient per signal for one trader.

NOT a score and NOT a verdict. Each signal is a labelled boolean the operator reads
alongside the raw numbers. Windowed signals prefer the ledger when it covers the span,
fall back to the forward snapshot, and say "insufficient" rather than guess. Every
threshold comes from settings/memecoin.py (env-tunable).
"""
from __future__ import annotations

from sniper.settings import memecoin as _cfg

GREEN, RED, INSUFF = "green", "red", "insufficient"


def _defaults():
    return {
        "unrealized_max_share": _cfg.MEMECOIN_GATE_UNREALIZED_MAX_SHARE,
        "fresh_max_age_h": _cfg.MEMECOIN_GATE_FRESH_MAX_AGE_H,
        "fresh_min_active_days": _cfg.MEMECOIN_GATE_FRESH_MIN_ACTIVE_DAYS,
        "snap_min_days_7d": _cfg.MEMECOIN_GATE_SNAP_MIN_DAYS_7D,
        "snap_min_days_30d": _cfg.MEMECOIN_GATE_SNAP_MIN_DAYS_30D,
    }


def _lifetime(row, t):
    pnl = row.get("fomo_total_pnl")
    if pnl is None:
        return {"state": INSUFF, "fomo_total_pnl": None, "unrealized_share": None,
                "note": "not on fomo leaderboard — no lifetime PnL"}
    unreal = row.get("fomo_unrealized_pnl")
    share = (unreal / pnl) if (unreal is not None and pnl > 0) else None
    if pnl <= 0:
        state = RED
    elif share is not None and share >= t["unrealized_max_share"]:
        state = RED   # profitable on paper, but mostly unsold bags — not realized skill
    else:
        state = GREEN
    out = {"state": state, "fomo_total_pnl": pnl, "unrealized_share": share}
    if row.get("fomo_unrealized_partial"):
        # The unrealized sum is only top holdings. It cannot support a lifetime verdict.
        out["state"] = INSUFF
        out["unrealized_partial"] = 1
        out["note"] = "bag-check incomplete (top-holdings-only)"
    return out


def _windowed(row, days, forward, t):
    """Ledger value if the span covered it, else the forward-snapshot tile, else insufficient."""
    covered = row.get(f"realized_{days}d_covered")
    ledger = row.get(f"realized_pnl_{days}d")
    if covered and ledger is not None:
        return {"state": GREEN if ledger > 0 else RED, "value": ledger,
                "source": "ledger", "n_days": None, "saturated": 0}
    min_days = t["snap_min_days_7d"] if days == 7 else t["snap_min_days_30d"]
    if forward and forward.get("n_days", 0) >= min_days:
        val = forward.get("realized")
        return {"state": GREEN if (val or 0) > 0 else RED, "value": val,
                "source": "snapshot", "n_days": forward.get("n_days"),
                "saturated": forward.get("saturated", 0)}
    return {"state": INSUFF, "value": None, "source": None,
            "n_days": (forward or {}).get("n_days", 0), "saturated": 0,
            "note": "span uncovered and forward snapshots not yet mature"}


def _fresh(row, t):
    age = row.get("last_swap_age_h")
    active = row.get("active_days")
    if age is None or active is None:
        return {"state": INSUFF, "last_swap_age_h": age, "active_days": active}
    ok = age <= t["fresh_max_age_h"] and active >= t["fresh_min_active_days"]
    return {"state": GREEN if ok else RED, "last_swap_age_h": age, "active_days": active}


def selection_gate(row, *, w7d=None, w30d=None, thresholds=None):
    """Return per-signal states for one trader. `w7d`/`w30d` are optional
    windowed_realized() results (forward snapshot) used when the ledger span is uncovered."""
    t = _defaults()
    if thresholds:
        t.update(thresholds)
    # NOTE: hold time is deliberately NOT a gate signal — a long holder is fine because the
    # UI lets us sell any position manually regardless of arm. median_hold_s is shown raw.
    signals = {
        "lifetime": _lifetime(row, t),
        "w7d": _windowed(row, 7, w7d, t),
        "w30d": _windowed(row, 30, w30d, t),
        "fresh": _fresh(row, t),
    }
    summary = {GREEN: 0, RED: 0, INSUFF: 0}
    for s in signals.values():
        summary[s["state"]] += 1
    signals["summary"] = summary
    return signals
