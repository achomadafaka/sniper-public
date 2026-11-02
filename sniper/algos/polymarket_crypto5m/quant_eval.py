"""Phase 2B — pure performance evaluator for the quant cohort.

Given resolved-window records, computes: overall hit rate (Wilson CI), the residual split (agree vs differ
from the market favorite), per-component Brier + directional accuracy (which signal actually predicts the
Chainlink outcome), paired comparison vs the C1 favorite and C0 random benchmarks, a regime breakdown, and
data-driven recommendations (what to fix). Pure — the script supplies the records from the DB. The
unit of independence is one resolved window; small-n is flagged, never hidden.
"""
from __future__ import annotations

import math

MIN_N = 30


def wilson_ci(k, n, z=1.96):
    if not n:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(centre - half, 4), round(centre + half, 4))


def brier(pairs):
    """pairs of (q_up, y) where y in {0,1}. Lower is better; 0.25 = always-0.5 baseline."""
    return round(sum((q - y) ** 2 for q, y in pairs) / len(pairs), 4) if pairs else None


def leadlag(pairs) -> dict:
    """Lead-lag test (the #1 edge hypothesis). pairs = [(signal_early, market_delta), ...] where
    signal_early = our model-minus-market disagreement at an early checkpoint and market_delta = how the
    market's q moved by a LATER checkpoint. If our signal leads the market, sign(signal) predicts
    sign(market_delta). Reports the sign-agreement rate (Wilson CI) over moves that actually happened.
    >0.5 with a CI clear of 0.5 = the market lags our signal = exploitable (enter before it reprices)."""
    usable = [(s, d) for s, d in pairs if s is not None and d is not None and s != 0 and d != 0]
    n = len(usable)
    hits = sum(1 for s, d in usable if (s > 0) == (d > 0))
    lo, hi = wilson_ci(hits, n)
    return {"n": n, "hits": hits, "agreement_rate": (round(hits / n, 4) if n else None), "ci": [lo, hi]}


def _split(records, predicate):
    n = hits = 0
    for r in records:
        if predicate(r):
            n += 1
            hits += int(r["actionable_side"] == r["winner"])
    lo, hi = wilson_ci(hits, n)
    return {"n": n, "hits": hits, "hit_rate": (round(hits / n, 4) if n else None), "ci": [lo, hi]}


def evaluate_cohort(records) -> dict:
    actionable = [r for r in records if r.get("actionable_side") and r.get("winner") in ("Up", "Down")]
    n = len(actionable)
    hits = sum(int(r["actionable_side"] == r["winner"]) for r in actionable)
    lo, hi = wilson_ci(hits, n)

    agree = _split(actionable, lambda r: r.get("q_market") is not None
                   and r["actionable_side"] == ("Up" if r["q_market"] >= 0.5 else "Down"))
    differ = _split(actionable, lambda r: r.get("q_market") is not None
                    and r["actionable_side"] != ("Up" if r["q_market"] >= 0.5 else "Down"))

    # per-component Brier + directional accuracy against the real outcome (y = 1 if Up won)
    comp_names = set()
    for r in actionable:
        comp_names.update((r.get("components") or {}).keys())
    components = {}
    for name in sorted(comp_names):
        pairs, dir_hits, m = [], 0, 0
        for r in actionable:
            q = (r.get("components") or {}).get(name)
            if q is None:
                continue
            y = 1 if r["winner"] == "Up" else 0
            pairs.append((q, y))
            dir_hits += int((q >= 0.5) == (y == 1))
            m += 1
        components[name] = {"n": m, "brier": brier(pairs),
                            "dir_acc": (round(dir_hits / m, 4) if m else None)}

    # paired benchmarks on the same resolved windows
    c1_hits = sum(int(r.get("c1_side") == r["winner"]) for r in actionable if r.get("c1_side"))
    c1_n = sum(1 for r in actionable if r.get("c1_side"))
    c0_hits = sum(int(r.get("c0_side") == r["winner"]) for r in actionable if r.get("c0_side"))
    c0_n = sum(1 for r in actionable if r.get("c0_side"))

    # regime breakdowns
    def by(field):
        out = {}
        for r in actionable:
            key = r.get(field) or "unknown"
            b = out.setdefault(key, {"n": 0, "hits": 0})
            b["n"] += 1
            b["hits"] += int(r["actionable_side"] == r["winner"])
        for b in out.values():
            b["hit_rate"] = round(b["hits"] / b["n"], 4) if b["n"] else None
        return out

    net_edges = [r["net_edge_f1"] for r in actionable if r.get("net_edge_f1") is not None]
    result = {
        "n": n, "hits": hits, "hit_rate": (round(hits / n, 4) if n else None), "ci": [lo, hi],
        "agree_market": agree, "differ_market": differ,
        "components": components,
        "c1_hits": c1_hits, "c1_n": c1_n,
        "c1_hit_rate": (round(c1_hits / c1_n, 4) if c1_n else None),
        "c0_hits": c0_hits, "c0_n": c0_n,
        "c0_hit_rate": (round(c0_hits / c0_n, 4) if c0_n else None),
        "by_vol_regime": by("vol_regime"), "by_session": by("session"),
        "by_disagreement_regime": by("disagreement_regime"),
        "shadow_net_edge_mean": (round(sum(net_edges) / len(net_edges), 4) if net_edges else None),
        "recommendations": [],
    }
    result["recommendations"] = _recommend(result)
    return result


def _recommend(r) -> list:
    recs = []
    if r["n"] < MIN_N:
        recs.append(f"Insufficient sample (n={r['n']} < {MIN_N}); no conclusion — keep accumulating.")
        return recs
    # ensemble vs best component
    comp_briers = {k: v["brier"] for k, v in r["components"].items() if v["brier"] is not None}
    if comp_briers:
        best = min(comp_briers, key=comp_briers.get)
        recs.append(f"Best single signal by Brier: {best} ({comp_briers[best]}). "
                    f"q_market Brier={comp_briers.get('q_market')}.")
        if "q_market" in comp_briers and comp_briers[best] >= comp_briers["q_market"] - 0.005:
            recs.append("No signal materially beats the market prior — edge unproven; "
                        "structural components are likely re-deriving the price (decorrelate/reweight).")
    # beats C1?
    if r["c1_hit_rate"] is not None and r["hit_rate"] is not None:
        diff = r["hit_rate"] - r["c1_hit_rate"]
        recs.append(f"Quant hit {r['hit_rate']} vs C1 favorite {r['c1_hit_rate']} (Δ={round(diff,4)} on n={r['n']}).")
        if diff <= 0:
            recs.append("Not beating the favorite — no actionable edge yet.")
    # residual bucket
    dm = r["differ_market"]
    if dm["n"] >= MIN_N and dm["ci"][0] is not None and dm["ci"][0] <= 0.5:
        recs.append(f"Differ-from-market bucket hit {dm['hit_rate']} (CI {dm['ci']}) — residual edge not significant.")
    elif dm["n"] < MIN_N:
        recs.append(f"Residual (differ-from-market) bucket still thin (n={dm['n']}).")
    # best regime
    regimes = {k: v["hit_rate"] for k, v in r["by_vol_regime"].items() if v["n"] >= 10 and v["hit_rate"] is not None}
    if regimes:
        best_r = max(regimes, key=regimes.get)
        recs.append(f"Best vol regime: {best_r} (hit {regimes[best_r]}, n={r['by_vol_regime'][best_r]['n']}) "
                    f"— candidate for selective trading.")
    return recs
