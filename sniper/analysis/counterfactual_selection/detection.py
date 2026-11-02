"""CSE-F5..F10 — detection battery (operates on the decision matrix).

Decision-matrix row contract:
  {"group_id": str, "picked": 0|1, "features": {name: float|str},
   "resolved": 0|1|None}   # resolved = won-on-her-side, label-only (edge test ONLY)

Every test returns evidence with effect size + a permutation p-value, and the
caller FDR-corrects across the many (test × feature × group) combinations
(guards.benjamini_hochberg). All tests are pure; randomness is seeded.

Spec: sre-f4-detection-battery.md (+ F0 guards mandatory).
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence

from sniper.analysis.counterfactual_selection.guards import standardized_mean_diff

PERM = 1000


def _by_group(rows: Sequence[Mapping[str, Any]]) -> dict[str, list]:
    g: dict[str, list] = defaultdict(list)
    for r in rows:
        g[str(r.get("group_id"))].append(r)
    return g


def _feat_vals(rows, feature, picked):
    out = []
    for r in rows:
        if int(r.get("picked", 0)) == picked:
            v = (r.get("features") or {}).get(feature)
            if v is not None:
                try:
                    out.append(float(v))
                except (TypeError, ValueError):
                    pass
    return out


def within_group_discriminator(rows, feature, *, n_perm: int = PERM, seed: int = 42) -> list[dict]:
    """Per group: does `feature` separate picked from unpicked? SMD + permutation
    p (shuffle picked labels within the group). Caller FDR-corrects across groups."""
    rng = random.Random(seed)
    results = []
    for gid, grp in _by_group(rows).items():
        pk = _feat_vals(grp, feature, 1)
        un = _feat_vals(grp, feature, 0)
        if len(pk) < 3 or len(un) < 3:
            continue
        smd = standardized_mean_diff(pk, un)
        if smd is None:
            continue
        allv = pk + un
        n_pk = len(pk)
        obs = abs(smd)
        ge = 0
        for _ in range(n_perm):
            rng.shuffle(allv)
            s = standardized_mean_diff(allv[:n_pk], allv[n_pk:])
            if s is not None and abs(s) >= obs:
                ge += 1
        p = (ge + 1) / (n_perm + 1)
        results.append({
            "group_id": gid, "feature": feature, "n_picked": n_pk, "n_unpicked": len(un),
            "smd": smd, "direction": "picked_higher" if smd > 0 else "picked_lower",
            "p_value": p,
        })
    return results


def _strat_diff(strata: Sequence[tuple]) -> float:
    """Group-size-weighted mean of per-stratum (picked_wr - unpicked_wr)."""
    num = den = 0.0
    for pk, un in strata:
        w = len(pk) + len(un)
        num += w * ((sum(pk) / len(pk)) - (sum(un) / len(un)))
        den += w
    return num / den if den else 0.0


def _strata_of(rows) -> list[tuple]:
    """Per-group (picked_resolved, unpicked_resolved) lists, groups with both."""
    strata: list[tuple] = []
    for grp in _by_group([r for r in rows if r.get("resolved") in (0, 1)]).values():
        pk = [int(r["resolved"]) for r in grp if int(r.get("picked", 0)) == 1]
        un = [int(r["resolved"]) for r in grp if int(r.get("picked", 0)) == 0]
        if pk and un:
            strata.append((pk, un))
    return strata


def stratified_edge_point(rows) -> float | None:
    """The selection-edge point statistic (stratified weighted within-group diff).
    Shared by selection_edge (obs) and the placebo null so they use the SAME
    statistic under group imbalance."""
    strata = _strata_of(rows)
    return _strat_diff(strata) if strata else None


def selection_edge(rows, *, n_perm: int = PERM, seed: int = 7) -> dict:
    """Do her picks OUT-RESOLVE the matched unselected? STRATIFIED by group_id to
    avoid Simpson's paradox (pooling picked-vs-unpicked across groups with
    different base rates can manufacture or hide an edge). The estimate is the
    group-size-weighted mean of within-group win-rate differences; the permutation
    null shuffles picked labels WITHIN each stratum. `pooled_edge` is reported for
    transparency (the old naive estimate)."""
    rng = random.Random(seed)
    decided = [r for r in rows if r.get("resolved") in (0, 1)]
    strata = _strata_of(decided)
    pk_all = [int(r["resolved"]) for r in decided if int(r.get("picked", 0)) == 1]
    un_all = [int(r["resolved"]) for r in decided if int(r.get("picked", 0)) == 0]
    n_pk, n_un = len(pk_all), len(un_all)
    if n_pk < 5 or n_un < 5 or not strata:
        return {"supported": None, "reason": "underpowered",
                "n_picked": n_pk, "n_unpicked": n_un, "n_strata": len(strata)}
    obs = _strat_diff(strata)
    # persistent per-stratum pooled lists shuffled in place — for a single stratum
    # this is identical to the classic global permutation.
    pooled_lists = [(pk + un, len(pk)) for pk, un in strata]
    ge = 0
    for _ in range(n_perm):
        num = den = 0.0
        for pooled, n_pk_g in pooled_lists:
            rng.shuffle(pooled)
            a, b = pooled[:n_pk_g], pooled[n_pk_g:]
            w = len(pooled)
            num += w * ((sum(a) / len(a)) - (sum(b) / len(b)))
            den += w
        if (num / den if den else 0.0) >= obs:
            ge += 1
    p = (ge + 1) / (n_perm + 1)
    pooled_edge = (sum(pk_all) / n_pk) - (sum(un_all) / n_un)
    return {
        "picked_win_rate": sum(pk_all) / n_pk, "unpicked_win_rate": sum(un_all) / n_un,
        "edge": obs, "pooled_edge": pooled_edge, "stratified": True, "n_strata": len(strata),
        "p_value": p, "n_picked": n_pk, "n_unpicked": n_un,
        "supported": (obs > 0 and p <= 0.05),
    }


def calibration_edge(rows, *, price_field: str = "entry_price", min_n: int = 20) -> dict:
    """Do her picks resolve in her favor MORE OFTEN than the price she paid
    implied? The PnL-relevant selection edge, and INDEPENDENT of the counterfactual
    — it catches skill the matched comparison misses (each pick is a Bernoulli
    whose no-skill probability IS its entry price). One-sided z-test of observed
    wins vs sum of entry prices. Uses picked rows only."""
    obs = []
    for r in rows:
        if int(r.get("picked", 0)) != 1 or r.get("resolved") not in (0, 1):
            continue
        f = r.get("features") or {}
        p = f.get(price_field)
        if p is None:
            p = f.get("price")
        try:
            p = float(p)
        except (TypeError, ValueError):
            continue
        if 0.0 < p < 1.0:
            obs.append((int(r["resolved"]), p))
    n = len(obs)
    if n < min_n:
        return {"supported": None, "reason": "underpowered", "n": n}
    wins = sum(w for w, _ in obs)
    exp = sum(p for _, p in obs)
    var = sum(p * (1 - p) for _, p in obs)
    wr, mean_p = wins / n, exp / n
    edge = wr - mean_p
    z = (wins - exp) / math.sqrt(var) if var > 0 else 0.0
    p_one = 0.5 * math.erfc(z / math.sqrt(2)) if z > 0 else 1.0 - 0.5 * math.erfc(-z / math.sqrt(2))
    return {
        "n": n, "win_rate": wr, "mean_entry_price": mean_p, "edge": edge,
        "z": z, "p_value": p_one, "supported": (edge > 0 and z > 0 and p_one <= 0.05),
    }


def calibration_by_band(rows, *, price_field: str = "entry_price",
                        bands=((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 0.95), (0.95, 1.0))) -> list[dict]:
    """Calibration edge per entry-price band — where the edge lives."""
    out = []
    for lo, hi in bands:
        sub = []
        for r in rows:
            f = r.get("features") or {}
            p = f.get(price_field)
            if p is None:
                p = f.get("price")
            try:
                p = float(p)
            except (TypeError, ValueError):
                continue
            if lo <= p < hi:
                sub.append(r)
        ce = calibration_edge(sub, price_field=price_field, min_n=10)
        out.append({"band": [lo, hi], **{k: ce.get(k) for k in ("n", "win_rate", "mean_entry_price", "edge", "supported")}})
    return out


def coverage(rows, predicate: Callable[[Mapping[str, Any]], bool]) -> dict:
    """Of the candidate markets MATCHING a predicate, what fraction did she enter?
    High → mechanical/exhaustive bot; low/selective → judgment."""
    matching = [r for r in rows if predicate(r.get("features") or {})]
    if not matching:
        return {"n_matching": 0, "coverage": None}
    entered = sum(1 for r in matching if int(r.get("picked", 0)) == 1)
    return {"n_matching": len(matching), "n_entered": entered, "coverage": entered / len(matching)}
