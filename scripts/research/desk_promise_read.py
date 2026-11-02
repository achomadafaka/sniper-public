#!/usr/bin/env python3
"""Promise score fit + backtest per docs/audits/2026-10-23-promise-score-preregistration.md section 6-7.
usage: desk_promise_read.py D1.jsonl D2.jsonl -- TEST.jsonl    (fit on D, report the fixed top-decile cut on TEST)"""
import json, sys
import numpy as np
from collections import defaultdict
from sklearn.linear_model import LogisticRegression

FEATS = ("ret_60", "ret_300", "range_300", "consol_s", "vol_exp", "flow_imb_60", "buyers_60", "buyers_accel", "holders_proxy", "age_s")
BANDS = {"B1 5-10k": (5000, 7500, 10000), "B2 12.5-20k": (12500, 15000, 17500, 20000), "B3 25-35k": (25000, 30000, 35000), "B4 40k": (40000,)}


def load(paths):
    rows = [json.loads(l) for p in paths for l in open(p)]
    return [r for r in rows if r["f"] and all(r["f"].get(k) is not None for k in FEATS)]


def mat(rows):
    return np.array([[r["f"][k] for k in FEATS] for r in rows], float)


def boot_lo(vals, coins, draws=500, seed=0):
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for i, c in enumerate(coins):
        by[c].append(i)
    g = [np.array(v) for v in by.values()]
    v = np.asarray(vals, float)
    s = np.array([v[x].sum() for x in g]); n = np.array([len(x) for x in g])
    res = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(g), len(g)) for _ in range(draws))]
    return float(np.percentile(res, 5))


def main():
    a = sys.argv[1:]
    k = a.index("--")
    D, T = load(a[:k]), load(a[k + 1:])
    print("D scored units %d  TEST scored units %d" % (len(D), len(T)))
    for band, lvls in BANDS.items():
        d = [r for r in D if r["level"] in lvls]; t = [r for r in T if r["level"] in lvls]
        if len(d) < 200 or len(t) < 30:
            print(band, "too few (D %d, T %d)" % (len(d), len(t))); continue
        X = mat(d); mu, sd = X.mean(0), X.std(0) + 1e-12
        y = np.array([r["r300"] > 0 for r in d], int)
        if y.sum() < 20:
            print(band, "too few positives in D"); continue
        m = LogisticRegression(C=1.0, max_iter=500).fit((X - mu) / sd, y)
        cut = np.percentile(m.decision_function((X - mu) / sd), 90)
        Xt = (mat(t) - mu) / sd
        sc = m.decision_function(Xt)
        top = sc >= cut
        rt = np.array([r["r300"] for r in t]); ra = np.array([r["r300_adv"] for r in t])
        coins = [r["mint"] for r in t]
        base = (rt > 0).mean()
        print("\n%s  D n=%d (pos %.1f%%)  TEST n=%d coins=%d  base P(R300>0)=%.3f  mean R300 all %+.3f" % (band, len(d), 100 * y.mean(), len(t), len(set(coins)), base, rt.mean()))
        if top.sum():
            tc = [c for c, f in zip(coins, top) if f]
            print("  top decile (D cut): n=%d coins=%d precision P(R300>0)=%.3f (base %.3f)  meanR %+.3f adv %+.3f  vs all lo90 %+.3f" % (
                top.sum(), len(set(tc)), (rt[top] > 0).mean(), base, rt[top].mean(), ra[top].mean(),
                boot_lo(np.where(top, rt - rt.mean(), 0.0), coins) * len(t) / top.sum()))
        coef = sorted(zip(FEATS, m.coef_[0]), key=lambda x: -abs(x[1]))[:4]
        print("  coef (std units):", ", ".join("%s %+.2f" % x for x in coef))


main()
