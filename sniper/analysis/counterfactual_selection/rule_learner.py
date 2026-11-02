"""CSE-F11 — selection-rule learner (interpretable-first).

Learns the simplest predicate that separates her picks from the matched
unselected candidates, using ONLY leakage-clean (pre/at-engagement) features
(guards.assert_rule_features_clean). Reports precision, coverage, determinism =
min(precision, coverage), and a temporal out-of-sample confirmation. If nothing
clears the bar, returns "no separating predicate" with the residual — which,
combined with the edge test, is how we tell human judgment from an unobserved
signal (F0/G8).

Spec: sre-f5-rule-learner-and-verdict.md.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from sniper.analysis.counterfactual_selection.guards import leakage_violations

MIN_PRECISION = float(os.getenv("CSE_RULE_MIN_PRECISION", "0.60"))
MIN_COVERAGE = float(os.getenv("CSE_RULE_MIN_COVERAGE", "0.30"))


def _vals(rows, feature):
    out = []
    for r in rows:
        v = (r.get("features") or {}).get(feature)
        if v is None:
            continue
        try:
            out.append((float(v), int(r.get("picked", 0))))
        except (TypeError, ValueError):
            pass
    return out


def _score_threshold(vals, thr, op):
    """precision, coverage, F1 for rule `feature op thr` predicting picked."""
    n_picked = sum(1 for _, p in vals if p == 1)
    if n_picked == 0:
        return 0.0, 0.0, 0.0
    match = [(v, p) for v, p in vals if (v <= thr if op == "<=" else v >= thr)]
    if not match:
        return 0.0, 0.0, 0.0
    tp = sum(1 for _, p in match if p == 1)
    precision = tp / len(match)
    cov = tp / n_picked
    f1 = (2 * precision * cov / (precision + cov)) if (precision + cov) else 0.0
    return precision, cov, f1


def learn_threshold_rule(rows: Sequence[Mapping[str, Any]], feature: str) -> dict | None:
    """Best single-threshold rule (`feature <= t` or `>= t`) by F1."""
    vals = _vals(rows, feature)
    if len({p for _, p in vals}) < 2:
        return None
    cands = sorted({v for v, _ in vals})
    best = None
    for thr in cands:
        for op in ("<=", ">="):
            prec, cov, f1 = _score_threshold(vals, thr, op)
            cand = {"feature": feature, "op": op, "threshold": thr,
                    "precision": prec, "coverage": cov, "f1": f1,
                    "determinism": min(prec, cov)}
            if best is None or (f1, cand["determinism"]) > (best["f1"], best["determinism"]):
                best = cand
    return best


def _time_split(rows):
    timed = [r for r in rows if r.get("ts") is not None]
    if len(timed) < 20:
        return None
    timed.sort(key=lambda r: r["ts"])
    split = len(timed) // 2
    train, test = timed[:split], timed[split:]
    if len({int(r.get("picked", 0)) for r in train}) < 2:
        return None
    return train, test


def _oos_confirm(rows, rule) -> bool:
    """Fit-implied rule must still hold on a later time split (rows need `ts`)."""
    test = list(rows)
    vals = _vals(test, rule["feature"])
    if not vals:
        return False
    prec, cov, _ = _score_threshold(vals, rule["threshold"], rule["op"])
    return prec >= MIN_PRECISION and cov >= MIN_COVERAGE


def learn_selection_rules(rows: Sequence[Mapping[str, Any]], features: Sequence[str]) -> dict:
    """Learn + rank interpretable rules over leakage-clean features. Returns the
    best rule (if any clears precision+coverage) and the residual."""
    excluded = leakage_violations(features)
    clean = [f for f in features if f not in excluded]
    split = _time_split(rows)
    rules = []
    for f in clean:
        fit_rows = split[0] if split else rows
        r = learn_threshold_rule(fit_rows, f)
        if r:
            r["oos_confirmed"] = _oos_confirm(split[1], r) if split else False
            rules.append(r)
    rules.sort(key=lambda r: (r["f1"], r["determinism"]), reverse=True)
    qualifying = [r for r in rules if r["precision"] >= MIN_PRECISION and r["coverage"] >= MIN_COVERAGE]
    best = qualifying[0] if qualifying else None
    n_picked = sum(1 for r in rows if int(r.get("picked", 0)) == 1)
    residual = None
    if best and n_picked:
        # picks the best rule does NOT capture = unexplained selection
        captured = sum(1 for r in rows if int(r.get("picked", 0)) == 1 and _matches(r, best))
        residual = 1.0 - captured / n_picked
    return {
        "rules": rules,
        "best": best,
        "rule_found": best is not None,
        "residual_fraction": residual,
        "leakage_excluded": excluded,
    }


def _matches(row, rule) -> bool:
    v = (row.get("features") or {}).get(rule["feature"])
    if v is None:
        return False
    v = float(v)
    return v <= rule["threshold"] if rule["op"] == "<=" else v >= rule["threshold"]
