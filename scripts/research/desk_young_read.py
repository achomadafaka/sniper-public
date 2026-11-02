#!/usr/bin/env python3
"""Young-coin base rates and score per docs/audits/2026-10-23-young-coin-score-preregistration.md.
usage: desk_young_read.py D1.jsonl D2.jsonl -- TEST.jsonl"""
import json, sys
from collections import defaultdict
import numpy as np
from sklearn.linear_model import LogisticRegression

FEATS = ("age_s", "ascent", "buyers_per_s", "swaps_per_wallet", "buy_share", "flow_imb_10", "top_buyer_share", "creator_sold", "ret_10", "range_10")
BANDS = {"B1 5-10k": (5000, 7500, 10000), "B2 12.5-20k": (12500, 15000, 17500, 20000), "B3 25-35k": (25000, 30000, 35000), "B4 40k": (40000,)}
TIERS = ((5, 30), (30, 120), (120, 300))


def load(paths):
    return [json.loads(l) for p in paths for l in open(p)]


def scored(rows):
    return [r for r in rows if all(r["f"].get(k) is not None for k in FEATS)]


def base(rows, name):
    print("\n== base rates: %s (units %d, coins %d)" % (name, len(rows), len({r["mint"] for r in rows})))
    for band, lv in BANDS.items():
        us = [r for r in rows if r["level"] in lv]
        if not us:
            continue
        line = "%-12s n %5d coins %4d" % (band, len(us), len({r["mint"] for r in us}))
        for key in ("r5", "r15"):
            v = np.array([r[key] for r in us]); a = np.array([r[key + "_adv"] for r in us])
            line += " | %s P>0 %.3f mean %+.3f adv %+.3f" % (key, (v > 0).mean(), v.mean(), a.mean())
        print(line)
        for lo, hi in TIERS:
            ut = [r for r in us if lo <= r["f"]["age_s"] < hi]
            if len(ut) >= 20:
                v = np.array([r["r5"] for r in ut])
                print("     age %3d-%3ds n %5d P>0 %.3f mean %+.3f" % (lo, hi, len(ut), (v > 0).mean(), v.mean()))


def boot_lo(vals, coins, draws=500, seed=0, q=2.5):
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for i, c in enumerate(coins):
        by[c].append(i)
    g = [np.array(v) for v in by.values()]
    v = np.asarray(vals, float)
    s = np.array([v[x].sum() for x in g]); n = np.array([len(x) for x in g])
    return float(np.percentile([s[p].sum() / n[p].sum() for p in (rng.integers(0, len(g), len(g)) for _ in range(draws))], q))


def main():
    a = sys.argv[1:]
    k = a.index("--")
    D, T = load(a[:k]), load(a[k + 1:])
    base(D, "discovery 10-21/22"); base(T, "test 10-23 (descriptive)")
    Ds, Ts = scored(D), scored(T)
    print("\nscorable: D %d of %d, T %d of %d" % (len(Ds), len(D), len(Ts), len(T)))
    for band, lv in BANDS.items():
        d = [r for r in Ds if r["level"] in lv]; t = [r for r in Ts if r["level"] in lv]
        if len(d) < 200 or len(t) < 30:
            print(band, "too few (D %d, T %d)" % (len(d), len(t))); continue
        X = np.array([[r["f"][k] for k in FEATS] for r in d]); mu, sd = X.mean(0), X.std(0) + 1e-12
        y = np.array([r["r5"] > 0 for r in d], int)
        m = LogisticRegression(C=1.0, max_iter=1000).fit((X - mu) / sd, y)
        cut = np.percentile(m.decision_function((X - mu) / sd), 90)
        sc = m.decision_function((np.array([[r["f"][k] for k in FEATS] for r in t]) - mu) / sd)
        top = sc >= cut
        rt = np.array([r["r5"] for r in t]); ra = np.array([r["r5_adv"] for r in t]); coins = [r["mint"] for r in t]
        print("\n%s D n=%d pos %.1f%% | TEST n=%d coins %d base P>0 %.3f mean %+.3f" % (band, len(d), 100 * y.mean(), len(t), len(set(coins)), (rt > 0).mean(), rt.mean()))
        if top.sum():
            print("   top decile n=%d coins=%d P>0 %.3f meanR %+.3f adv %+.3f  (cut minus all) lo97.5 %+.3f" % (
                top.sum(), len({c for c, f in zip(coins, top) if f}), (rt[top] > 0).mean(), rt[top].mean(), ra[top].mean(),
                boot_lo(np.where(top, rt - rt.mean(), 0.0), coins) * len(t) / top.sum()))
        print("   coef:", ", ".join("%s %+.2f" % x for x in sorted(zip(FEATS, m.coef_[0]), key=lambda x: -abs(x[1]))[:4]))


main()
