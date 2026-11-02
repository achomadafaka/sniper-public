#!/usr/bin/env python3
"""Farm signature grading per docs/audits/2026-10-23-farm-signature-preregistration.md section 5-6.
usage: desk_farm_read.py D1.jsonl D2.jsonl   (discovery)   |   --apply CUTS.json H.jsonl   (fixed rule)"""
import json
import sys
from collections import defaultdict

import numpy as np
from scipy.stats import rankdata

FEATS = ("net_ratio", "swaps_per_wallet", "one_shot_share", "retained", "round_trip_share", "buy_cv", "buy_mode_share", "move_per_sol",
         "dust_share", "burst_share", "top3_size_share", "top_buyer_share", "age_s")


def load(paths):
    return [json.loads(l) for p in paths for l in open(p)]


def auc(x, y):
    x, y = np.asarray(x, float), np.asarray(y, int)
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(x)
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def boot(rows, fn, draws=400, seed=0):
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for i, r in enumerate(rows):
        by[r["mint"]].append(i)
    groups = [np.array(v) for v in by.values()]
    out = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        idx = np.concatenate([groups[p] for p in pick])
        v = fn(idx)
        if v is not None:
            out.append(v)
    return np.percentile(out, [1, 99]) if out else (None, None)


def stats(rows, flag, lab="crash40"):
    f = np.array([flag(r) for r in rows])
    y = np.array([r[lab] for r in rows])
    r3 = np.array([r["r300"] for r in rows])
    org = (y == 0) & (r3 >= 0.20)
    return {"units": len(rows), "flag_share": f.mean(), "crash_flag": y[f].mean() if f.any() else None,
            "crash_unflag": y[~f].mean(), "r300_flag": r3[f].mean() if f.any() else None, "r300_unflag": r3[~f].mean(),
            "organic_n": int(org.sum()), "fp_organic": f[org].mean() if org.any() else None,
            "coins_flagged": len({r["mint"] for r, ff in zip(rows, f) if ff}), "crash_rate": y.mean()}


def main():
    if sys.argv[1] == "--apply":
        cuts = json.load(open(sys.argv[2]))
        rows = [r for r in load(sys.argv[3:]) if all(r.get(f) is not None for f in cuts["feats"])]
        flag = lambda r: sum((r[f] >= c) if d > 0 else (r[f] <= c) for f, (c, d) in cuts["feats"].items()) >= cuts["need"]
        print(json.dumps(stats(rows, flag), default=float, indent=1))
        return
    rows = load(sys.argv[1:])
    print("units", len(rows), "coins", len({r["mint"] for r in rows}), "CRASH40 rate %.4f" % np.mean([r["crash40"] for r in rows]),
          "CRASH30 %.4f CRASH50 %.4f" % (np.mean([r["crash30"] for r in rows]), np.mean([r["crash50"] for r in rows])))
    adv = []
    for f in FEATS:
        sub = [r for r in rows if r.get(f) is not None]
        y = np.array([r["crash40"] for r in sub]); x = np.array([r[f] for r in sub])
        a = auc(x, y)
        lo, hi = boot(sub, lambda idx: auc(x[idx], y[idx]))
        print("%-18s n %5d AUC %.3f  99%% [%.3f, %.3f]" % (f, len(sub), a, lo, hi))
        if lo is not None and (lo > 0.55 or hi < 0.45):
            adv.append((abs(a - 0.5), f, 1 if a > 0.5 else -1))
    adv = sorted(adv, reverse=True)[:3]
    print("advancing:", [(f, d) for _, f, d in adv])
    if not adv:
        print("no feature advances: no pre-entry signature found on D"); return
    sub = [r for r in rows if all(r.get(f) is not None for _, f, _ in adv)]
    best = {}
    for _, f, d in adv:
        vals = np.array([r[f] for r in sub])
        best[f] = {p: float(np.percentile(vals, p if d > 0 else 100 - p)) for p in (80, 90, 95)}
    import itertools
    need = len(adv) if len(adv) == 1 else 2
    top = None
    for combo in itertools.product((80, 90, 95), repeat=len(adv)):
        cuts = {f: (best[f][p], d) for p, (_, f, d) in zip(combo, adv)}
        flag = lambda r: sum((r[f] >= c) if d > 0 else (r[f] <= c) for f, (c, d) in cuts.items()) >= need
        s = stats(sub, flag)
        if s["flag_share"] <= 0.10 and s["crash_flag"] is not None and (top is None or s["crash_flag"] > top[0]):
            top = (s["crash_flag"], combo, cuts, s)
    if top is None:
        print("no cutoff set meets flag share <= 10%"); return
    print("chosen percentiles", top[1], "cuts", top[2])
    print(json.dumps(top[3], default=float, indent=1))
    json.dump({"feats": {f: list(v) for f, v in top[2].items()}, "need": need}, open("data/research/farm/cuts.json", "w"))


main()
