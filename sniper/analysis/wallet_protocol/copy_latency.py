"""Copy-Latency Decay — capture grid helper + `copy_latency_surface`.

Spec: docs/superpowers/specs/2026-05-22-copy-latency-surface-design.md

Measures how much of a wallet's entry-price advantage survives to a copier
who enters 5 / 15 / 60 minutes after the wallet's trade. For a resolved
binary position, ROI is pure arithmetic: bought at price `p`, resolves to
1 (win) or 0 (loss), so `roi = (1-p)/p` if the side won, `-1` if it lost.
The copier pays the spot price `h` minutes later; the decay is the ROI the
copier loses to that latency.

`copy_latency_grid_pairs` (capture-side, imported by the snapshot script)
derives the post-entry `(asset, ts)` pairs the capture must resolve.
`compute_copy_latency_surface` (the display surface) is a HARD-WALLED,
display-only, pure function — read ONLY by the report renderer. It MUST
NEVER feed `overall_verdict`, `gate_result`, `primary_edge_type`,
`copyability_note` (the surface's own `copyability` key is display text,
not that field), branch/hypothesis logic, the Strategy Pattern Audit, or
append to `data_quality_flags`. No I/O in the surface.
"""
from __future__ import annotations

from typing import Any, Mapping

SURFACE_VERSION = 1
HORIZONS_MIN = (5, 15, 60)         # post-entry latency horizons, minutes
PRIMARY_HORIZON = 15               # drives the headline copyability label
MIN_ELIGIBLE = 20                  # below this → underpowered
MIN_PER_HORIZON = 10               # primary-horizon n below this → underpowered
COVERAGE_FLOOR = 0.50              # primary-horizon n / n_eligible → proxy_only
EDGE_RETAINED_COPYABLE = 0.50      # edge_retained >= this → copyable
RESOLVED_WIN = 0.97
RESOLVED_LOSS = 0.03
PRICE_BAND_LO = 0.02
PRICE_BAND_HI = 0.98

_SEVERITY = {"ok": 0, "proxy_only": 1, "underpowered": 2, "data_unavailable": 3}


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _cid(x: Any) -> str:
    return str(x or "").strip().lower()


def _roi(price: float, side_won: bool) -> float:
    """Hold-to-resolution ROI of a $1 bet on a side at `price` (price > 0)."""
    return (1.0 - price) / price if side_won else -1.0


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _earliest_buys(activity_rows) -> dict[tuple[str, str], int]:
    """(conditionId, asset) -> earliest BUY timestamp."""
    earliest: dict[tuple[str, str], int] = {}
    for r in activity_rows or []:
        if r.get("type") != "TRADE" or r.get("side") != "BUY":
            continue
        asset = str(r.get("asset") or "")
        if not asset:
            continue
        ts = _int(r.get("timestamp"))
        if ts is None:
            continue
        key = (_cid(r.get("conditionId")), asset)
        if key not in earliest or ts < earliest[key]:
            earliest[key] = ts
    return earliest


def copy_latency_grid_pairs(
    closed_positions: list[Mapping[str, Any]],
    activity_rows: list[Mapping[str, Any]],
) -> set[tuple[str, int]]:
    """Post-entry price-grid `(asset, ts)` pairs the capture must resolve:
    per closed position, the first-BUY `(asset, entry_ts)` plus
    `(asset, entry_ts + h*60)` for each horizon. Pure, deterministic."""
    earliest = _earliest_buys(activity_rows)
    pairs: set[tuple[str, int]] = set()
    for p in closed_positions or []:
        asset = str(p.get("asset") or "")
        if not asset:
            continue
        ts = earliest.get((_cid(p.get("conditionId")), asset))
        if ts is None:
            continue
        pairs.add((asset, ts))
        for h in HORIZONS_MIN:
            pairs.add((asset, ts + h * 60))
    return pairs


def _empty(data_state: str, n_total: int, reason: str) -> dict:
    return {
        "surface_version": SURFACE_VERSION,
        "data_state": data_state,
        "copyability": "inconclusive",
        "primary_horizon_min": PRIMARY_HORIZON,
        "n_eligible": 0,
        "n_total_closed": n_total,
        "horizons": [
            {"horizon_min": h, "n": 0, "mean_drift": None,
             "entry_ew_roi": None, "copier_ew_roi": None,
             "mean_decay": None, "edge_retained": None}
            for h in HORIZONS_MIN
        ],
        "caveats": [reason],
    }


def _out_of_range_horizon_caveat(n: int) -> str:
    return (
        f"OUT_OF_RANGE_HORIZON_PRICE: {n} horizon spot prices fell outside "
        "(0, 1] and were dropped (likely negative-risk / synthetic-short "
        "markets)."
    )


def compute_copy_latency_surface(
    closed_positions: list[Mapping[str, Any]],
    activity_rows: list[Mapping[str, Any]],
    copy_latency_prices: Mapping[str, Any] | None,
) -> dict:
    """Build the Copy-Latency Decay surface. Pure, deterministic,
    display-only. `copy_latency_prices` is the fidelity=1 post-entry price
    grid (keyed "<asset>|<ts>"), captured separately from
    price_history_lookups so the two fidelities never share a key."""
    closed = list(closed_positions or [])
    n_total = len(closed)
    if n_total == 0:
        return _empty("data_unavailable", 0, "no_closed_positions")

    earliest = _earliest_buys(activity_rows)
    clp = copy_latency_prices or {}

    # ---- §5.1 eligibility + §5.2 per-position datapoints ----------------
    n_horizon_out_of_range = 0
    datapoints: list[dict] = []
    for p in closed:
        asset = str(p.get("asset") or "")
        if not asset:
            continue
        ts = earliest.get((_cid(p.get("conditionId")), asset))
        if ts is None:                       # no derivable entry
            continue
        avg = _num(p.get("avgPrice"))
        cur = _num(p.get("curPrice"))
        if avg is None or cur is None:
            continue
        if not (PRICE_BAND_LO < avg < PRICE_BAND_HI):
            continue
        if not (cur >= RESOLVED_WIN or cur <= RESOLVED_LOSS):
            continue
        p_entry = _num(clp.get(f"{asset}|{ts}"))
        # The entry spot must be present AND in the binary band — a
        # near-certain entry spot (≈0 or ≈1) makes roi_entry tiny and
        # edge_retained noisy, so such positions are not eligible.
        if p_entry is None or not (PRICE_BAND_LO < p_entry < PRICE_BAND_HI):
            continue
        won = cur >= RESOLVED_WIN
        roi_entry = _roi(p_entry, won)
        horizons: dict[int, dict] = {}
        for h in HORIZONS_MIN:
            p_h = _num(clp.get(f"{asset}|{ts + h * 60}"))
            if p_h is None:
                continue
            if p_h <= 0 or p_h > 1.0:
                # Out-of-(0,1] spot — negative-risk / synthetic-short
                # market weirdness; would feed nonsense into _roi. Drop it.
                n_horizon_out_of_range += 1
                continue
            horizons[h] = {
                "drift": p_h - p_entry,
                "roi_entry": roi_entry,
                "roi_h": _roi(p_h, won),
            }
        datapoints.append({"roi_entry": roi_entry, "horizons": horizons})

    n_eligible = len(datapoints)
    if n_eligible == 0:
        return _empty("data_unavailable", n_total, "no_eligible_positions")

    # ---- §5.3 per-horizon aggregation over the matched set -------------
    horizon_rows: list[dict] = []
    primary_n = 0
    for h in HORIZONS_MIN:
        matched = [d["horizons"][h] for d in datapoints if h in d["horizons"]]
        n_h = len(matched)
        if h == PRIMARY_HORIZON:
            primary_n = n_h
        if n_h == 0:
            horizon_rows.append({
                "horizon_min": h, "n": 0, "mean_drift": None,
                "entry_ew_roi": None, "copier_ew_roi": None,
                "mean_decay": None, "edge_retained": None})
            continue
        mean_drift = sum(m["drift"] for m in matched) / n_h
        entry_ew = sum(m["roi_entry"] for m in matched) / n_h
        copier_ew = sum(m["roi_h"] for m in matched) / n_h
        edge_retained = (copier_ew / entry_ew) if entry_ew > 0 else None
        horizon_rows.append({
            "horizon_min": h,
            "n": n_h,
            "mean_drift": round(mean_drift, 6),
            "entry_ew_roi": round(entry_ew, 6),
            "copier_ew_roi": round(copier_ew, 6),
            "mean_decay": round(entry_ew - copier_ew, 6),
            "edge_retained": (round(edge_retained, 4)
                              if edge_retained is not None else None),
        })

    has_any_horizon = any(row["n"] > 0 for row in horizon_rows)
    if not has_any_horizon:
        # Entry spots captured but no usable post-entry horizon. Distinguish
        # the dormant/pre-grid case from a grid that exists but was rejected.
        reason = (
            "all_horizon_prices_out_of_range"
            if n_horizon_out_of_range > 0
            else "no_post_entry_grid_captured"
        )
        surf = _empty("data_unavailable", n_total,
                      reason)
        surf["n_eligible"] = n_eligible
        if n_horizon_out_of_range > 0:
            surf["caveats"].append(
                _out_of_range_horizon_caveat(n_horizon_out_of_range)
            )
        return surf

    # ---- §7 data_state + caveats ---------------------------------------
    caveats: list[str] = []
    if n_horizon_out_of_range > 0:
        caveats.append(_out_of_range_horizon_caveat(n_horizon_out_of_range))
    state = "ok"
    if n_eligible < MIN_ELIGIBLE or primary_n < MIN_PER_HORIZON:
        state = _worse(state, "underpowered")
    if primary_n < COVERAGE_FLOOR * n_eligible:
        caveats.append(
            f"PARTIAL_HORIZON_COVERAGE: the +{PRIMARY_HORIZON}m horizon "
            f"resolved for only {primary_n} of {n_eligible} eligible "
            "positions — the decay figure sees a subset.")
        state = _worse(state, "proxy_only")

    # ---- §5.4 copyability label ----------------------------------------
    primary_row = next(r for r in horizon_rows
                       if r["horizon_min"] == PRIMARY_HORIZON)
    retained = primary_row["edge_retained"]
    if state != "ok" or retained is None:
        copyability = "inconclusive"
    elif retained >= EDGE_RETAINED_COPYABLE:
        copyability = "copyable"
    else:
        copyability = "edge_decays"

    return {
        "surface_version": SURFACE_VERSION,
        "data_state": state,
        "copyability": copyability,
        "primary_horizon_min": PRIMARY_HORIZON,
        "n_eligible": n_eligible,
        "n_total_closed": n_total,
        "horizons": horizon_rows,
        "caveats": caveats,
    }
