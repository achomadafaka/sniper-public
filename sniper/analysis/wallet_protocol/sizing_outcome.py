"""Sizing & Outcome surface — `sizing_outcome_surface`.

Spec: docs/superpowers/specs/2026-05-22-sizing-outcome-surface-design.md

Measures whether a wallet's ENTRY sizing is associated with outcome
quality — stratified by category to neutralize the market-mix confound,
leave-one-out to expose one-big-winner artifacts, entry-vs-total cost to
expose late conviction.

HARD WALL — display-only. `compute_sizing_outcome_surface` is read ONLY by
the report renderer. It MUST NEVER feed `overall_verdict`, `gate_result`,
`primary_edge_type`, `copyability_note`, branch/hypothesis logic, or append
to the top-level `data_quality_flags`. Caveats live in the surface's own
`caveats` list. Pure function — no I/O. Reports ASSOCIATION, never "skill":
only thesis-specific simulation can promote an association to skill.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

SURFACE_VERSION = 1
MIN_DATAPOINTS = 20        # below this → underpowered
MIN_STRATUM_N = 8          # category strata below this skip the stratified rho
_PERM_ITERS = 2000
_PERM_SEED = 1
_RHO_BAND = 0.10           # |rho| < this → no_association / robustness trivially holds
_DIVERGENCE_AT = 0.30      # |global - stratified| ≥ this → divergence caveat
_IMBALANCE_AT = 0.70       # one stratum > this share of datapoints → imbalance
_CLUSTER_AT = 0.50         # > this share in 3+-position events → event clustering
_CONCENTRATION_AT = 0.60   # top-5 positions > this share of gross profit
_CLUSTER_MIN = 3           # an event with ≥ this many positions is a cluster

_SEVERITY = {"ok": 0, "proxy_only": 1, "underpowered": 2, "data_unavailable": 3}


@dataclass(frozen=True)
class _DP:
    entry: float
    total: float
    roi: float
    category: str | None
    event: str | None
    realized_pnl: float


def _num(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _cid(x: Any) -> str:
    """Normalize a condition id — lowercased + stripped."""
    return str(x or "").strip().lower()


def _trade_usdc(row: Mapping[str, Any]) -> float:
    """USDC of a trade row. REST rows carry `usdcSize`; subgraph rows carry
    `price` + `size`."""
    amt = _num(row.get("usdcSize"))
    if amt > 0:
        return amt
    return _num(row.get("price")) * _num(row.get("size"))


def _rank(values: list[float]) -> list[float]:
    """Average (fractional) ranks — ties share the mean of their rank span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    return ranks


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:        # no variance on one axis → no correlation
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation. None when < 3 points (not meaningful)."""
    if len(xs) < 3:
        return None
    return _pearson(_rank(xs), _rank(ys))


def _permutation_p(xs: list[float], ys: list[float],
                   observed: float | None) -> float | None:
    """Two-sided permutation p-value. Deterministic — fixed seed; permutes
    the rank vector directly so no re-ranking is needed per iteration."""
    if observed is None or len(xs) < 3:
        return None
    xr = _rank(xs)
    yr = _rank(ys)
    rng = random.Random(_PERM_SEED)
    target = abs(observed)
    hits = 0
    for _ in range(_PERM_ITERS):
        rng.shuffle(yr)
        r = _pearson(xr, yr)
        if r is not None and abs(r) >= target:
            hits += 1
    # +1 correction — the observed arrangement always satisfies the
    # threshold, so a permutation p-value can never be an (improper) 0.0.
    return (hits + 1) / (_PERM_ITERS + 1)


def _category(cid: str, leafs_by_cid: Mapping[str, Any],
              nav_lookup: Mapping[str, Any]) -> tuple[str | None, bool]:
    """Deterministic category for a condition: the nav of the FIRST leaf in
    sorted order that has a nav mapping. `ambiguous` is True when the
    condition's leafs map to more than one distinct nav category."""
    leafs = sorted(str(leaf) for leaf in (leafs_by_cid.get(cid) or []))
    first: str | None = None
    distinct: list[str] = []
    for leaf in leafs:
        nav = nav_lookup.get(leaf)
        if nav:
            if first is None:
                first = str(nav)
            if str(nav) not in distinct:
                distinct.append(str(nav))
    return first, (len(distinct) > 1)


def _robustness(global_rho: float | None,
                loo_rho: float | None) -> bool | None:
    """Whether the global rho survives dropping the largest-entry point."""
    if global_rho is None:
        return None
    if abs(global_rho) < _RHO_BAND:
        return True                      # nothing material to break
    if loo_rho is None:
        return False
    same_sign = (global_rho >= 0) == (loo_rho >= 0)
    return bool(same_sign and abs(loo_rho) >= 0.5 * abs(global_rho))


def _association(state: str, robustness_holds: bool | None,
                 stratified_rho: float | None) -> str:
    if state in ("underpowered", "data_unavailable"):
        return "inconclusive"
    if robustness_holds is False or stratified_rho is None:
        return "inconclusive"
    if abs(stratified_rho) < _RHO_BAND:
        return "no_association"
    return ("larger_entries_better" if stratified_rho >= _RHO_BAND
            else "larger_entries_worse")


def _buckets(datapoints: list[_DP]) -> list[dict]:
    """Entry-size quartiles — n near-equal chunks ascending by entry size."""
    dps = sorted(datapoints, key=lambda d: d.entry)
    n = len(dps)
    out: list[dict] = []
    for q in range(4):
        chunk = dps[q * n // 4:(q + 1) * n // 4]
        if not chunk:
            continue
        wins = sum(1 for d in chunk if d.roi > 0)
        out.append({
            "quartile": q + 1,
            "n": len(chunk),
            "mean_entry_size_usdc": round(
                sum(d.entry for d in chunk) / len(chunk), 2),
            "win_rate": round(wins / len(chunk), 4),
            "mean_roi": round(sum(d.roi for d in chunk) / len(chunk), 6),
        })
    return out


def _event_cluster_share(datapoints: list[_DP]) -> float:
    """Share of datapoints in events with ≥ _CLUSTER_MIN of the wallet's
    positions. Missing-event positions are singletons — never clustered,
    never lumped together (matches independence.py)."""
    counts: Counter[str] = Counter()
    for d in datapoints:
        if d.event:
            counts[d.event] += 1
    clustered = sum(c for c in counts.values() if c >= _CLUSTER_MIN)
    return clustered / len(datapoints) if datapoints else 0.0


def _pnl_concentration(datapoints: list[_DP]) -> float | None:
    """Top-5 winning positions' share of gross profit. None when no wins."""
    wins = sorted((d.realized_pnl for d in datapoints if d.realized_pnl > 0),
                  reverse=True)
    gross = sum(wins)
    if gross <= 0:
        return None
    return sum(wins[:5]) / gross


def _worse(a: str, b: str) -> str:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _empty(data_state: str, reason: str) -> dict:
    return {
        "surface_version": SURFACE_VERSION,
        "data_state": data_state,
        "association": "inconclusive",
        "global_rho": None,
        "global_rho_p": None,
        "stratified_rho": None,
        "n_datapoints": 0,
        "leave_one_out_rho": None,
        "robustness_holds": None,
        "size_buckets": [],
        "by_category": [],
        "total_entry_size_usdc": None,
        "total_cost_basis_usdc": None,
        "late_conviction_ratio": None,
        "added_after_entry_usdc": None,
        "n_positions_dropped": 0,
        "caveats": [reason],
    }


def compute_sizing_outcome_surface(
    closed_positions: list[Mapping[str, Any]],
    activity_rows: list[Mapping[str, Any]],
    leafs_by_cid: Mapping[str, Any] | None,
    nav_lookup: Mapping[str, Any] | None,
) -> dict:
    """Build the Sizing & Outcome surface. Pure, deterministic, display-only."""
    closed = list(closed_positions or [])
    if not closed:
        return _empty("data_unavailable", "no_closed_positions")

    leafs_by_cid = leafs_by_cid or {}
    nav_lookup = nav_lookup or {}
    have_category_data = bool(leafs_by_cid)

    # ---- index BUYs two ways (§4.1 join) --------------------------------
    buys_by_cid_asset: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    buys_by_cid_outcome: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in activity_rows or []:
        if r.get("type") != "TRADE" or r.get("side") != "BUY":
            continue
        cid = _cid(r.get("conditionId"))
        asset = str(r.get("asset") or "")
        outcome = str(r.get("outcome") or "")
        if asset:
            buys_by_cid_asset.setdefault((cid, asset), []).append(r)
        buys_by_cid_outcome.setdefault((cid, outcome), []).append(r)

    # ---- build datapoints (§4.2 guards, §4.3 category) ------------------
    datapoints: list[_DP] = []
    n_dropped = 0
    category_ambiguous = False
    for p in closed:
        cid = _cid(p.get("conditionId"))
        asset = str(p.get("asset") or "")
        outcome = str(p.get("outcome") or "")
        group: list[Mapping[str, Any]] | None = None
        if asset:
            group = buys_by_cid_asset.get((cid, asset))
        if not group:
            # Fall back to the (cid, outcome) bucket. When the closed
            # position HAS an asset, only adopt BUYs that are asset-less or
            # share that asset — a BUY carrying a different non-empty asset
            # is a different position and must never be mis-joined.
            candidates = buys_by_cid_outcome.get((cid, outcome)) or []
            if asset:
                group = [b for b in candidates
                         if str(b.get("asset") or "") in ("", asset)]
            else:
                group = list(candidates)
        if not group:
            n_dropped += 1
            continue
        ordered = sorted(group, key=lambda b: int(b.get("timestamp") or 0))
        entry = _trade_usdc(ordered[0])
        total = sum(_trade_usdc(b) for b in group)
        if entry <= 0 or total <= 0:
            n_dropped += 1
            continue
        cat, amb = _category(cid, leafs_by_cid, nav_lookup)
        if amb:
            category_ambiguous = True
        realized = _num(p.get("realizedPnl"))
        datapoints.append(_DP(
            entry=entry, total=total, roi=realized / total, category=cat,
            event=(p.get("eventSlug") or p.get("event_slug") or None),
            realized_pnl=realized,
        ))

    if not datapoints:
        surf = _empty("data_unavailable", "no_valid_datapoints")
        surf["n_positions_dropped"] = n_dropped
        return surf

    n = len(datapoints)
    entries = [d.entry for d in datapoints]
    rois = [d.roi for d in datapoints]

    # ---- §5.1 global rho ------------------------------------------------
    global_rho = _spearman(entries, rois)
    global_rho_p = _permutation_p(entries, rois, global_rho)

    # ---- §5.2 stratified rho -------------------------------------------
    by_cat_map: dict[str, list[_DP]] = {}
    for d in datapoints:
        if d.category:
            by_cat_map.setdefault(d.category, []).append(d)
    by_category: list[dict] = []
    strat_num = 0.0
    strat_den = 0
    for cat in sorted(by_cat_map):
        dps = by_cat_map[cat]
        if len(dps) < MIN_STRATUM_N:
            continue
        rho_cat = _spearman([d.entry for d in dps], [d.roi for d in dps])
        if rho_cat is None:
            continue
        by_category.append(
            {"category": cat, "n": len(dps), "rho": round(rho_cat, 4)})
        strat_num += len(dps) * rho_cat
        strat_den += len(dps)
    stratified_rho = (round(strat_num / strat_den, 4)
                      if strat_den > 0 else None)

    # ---- §5.3 leave-one-out --------------------------------------------
    loo_rho: float | None = None
    if global_rho is not None and n >= 4:
        idx_max = max(range(n), key=lambda i: entries[i])
        loo_rho = _spearman(
            [entries[i] for i in range(n) if i != idx_max],
            [rois[i] for i in range(n) if i != idx_max],
        )
    robustness_holds = _robustness(global_rho, loo_rho)

    # ---- §5.5 buckets + late conviction --------------------------------
    size_buckets = _buckets(datapoints)
    total_entry = sum(d.entry for d in datapoints)
    total_cost = sum(d.total for d in datapoints)
    late_ratio = (round(total_cost / total_entry, 4)
                  if total_entry > 0 else None)

    # ---- §6 caveats + data_state ---------------------------------------
    caveats: list[str] = []
    proxy = False
    if n_dropped > 0:
        caveats.append(f"MISSING_SIZE_OR_COST: {n_dropped} closed positions "
                       "had no derivable entry size.")
        proxy = True
    if not have_category_data:
        caveats.append("NO_CATEGORY_DATA: leafs_by_cid empty — cannot "
                       "stratify by category.")
        proxy = True
    if stratified_rho is None and have_category_data:
        caveats.append("STRATIFICATION_UNAVAILABLE: no category stratum met "
                       "the sample floor.")
        proxy = True
    if by_cat_map:
        top_share = max(len(v) for v in by_cat_map.values()) / n
        if top_share > _IMBALANCE_AT:
            caveats.append(f"CATEGORY_IMBALANCE: one category holds "
                           f"{top_share:.0%} of datapoints.")
            proxy = True
    clustered = _event_cluster_share(datapoints)
    if clustered > _CLUSTER_AT:
        caveats.append(f"EVENT_CLUSTERING: {clustered:.0%} of datapoints "
                       "share 3+-position events — correlated markets.")
        proxy = True
    conc = _pnl_concentration(datapoints)
    if conc is not None and conc > _CONCENTRATION_AT:
        caveats.append(f"PNL_CONCENTRATION: top-5 positions hold "
                       f"{conc:.0%} of gross profit.")
        proxy = True
    if robustness_holds is False:
        caveats.append("ROBUSTNESS_FAIL: the global association does not "
                       "survive dropping the largest-entry position.")
        proxy = True
    if category_ambiguous:
        caveats.append("CATEGORY_AMBIGUOUS: a condition's leaf tags map to "
                       "multiple nav categories; sorted-first chosen.")
    if (global_rho is not None and stratified_rho is not None
            and abs(global_rho - stratified_rho) >= _DIVERGENCE_AT):
        caveats.append(
            f"GLOBAL_STRATIFIED_DIVERGENCE: global rho {global_rho:.2f} "
            f"reflects category mix; stratified rho {stratified_rho:.2f} is "
            "the honest figure.")

    state = "ok"
    if n < MIN_DATAPOINTS:
        state = _worse(state, "underpowered")
    if proxy:
        state = _worse(state, "proxy_only")

    return {
        "surface_version": SURFACE_VERSION,
        "data_state": state,
        "association": _association(state, robustness_holds, stratified_rho),
        "global_rho": (round(global_rho, 4) if global_rho is not None
                       else None),
        "global_rho_p": (round(global_rho_p, 4) if global_rho_p is not None
                         else None),
        "stratified_rho": stratified_rho,
        "n_datapoints": n,
        "leave_one_out_rho": (round(loo_rho, 4) if loo_rho is not None
                              else None),
        "robustness_holds": robustness_holds,
        "size_buckets": size_buckets,
        "by_category": by_category,
        "total_entry_size_usdc": round(total_entry, 2),
        "total_cost_basis_usdc": round(total_cost, 2),
        "late_conviction_ratio": late_ratio,
        "added_after_entry_usdc": round(total_cost - total_entry, 2),
        "n_positions_dropped": n_dropped,
        "caveats": caveats,
    }
