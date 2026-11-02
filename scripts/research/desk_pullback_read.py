#!/usr/bin/env python3
"""Grade pullback arms vs NOW per docs/audits/2026-10-23-pullback-trigger-preregistration.md section 4.
usage: desk_pullback_read.py UNITS.jsonl [...]"""
import json, sys
from collections import defaultdict
import numpy as np


def load(paths):
    return [json.loads(l) for p in paths for l in open(p)]


def paired(rows, key, adv, N="300"):
    """per-unit (arm - NOW) with miss = 0 trade; None entries (arm censored) are dropped per arm."""
    out, coins, fired, nowR = [], [], 0, []
    for r in rows:
        now = r["now"][N][adv]
        a = r["arms"][key]
        if now is None:
            continue
        if a is None:
            out.append(0.0 - now); coins.append(r["mint"]); nowR.append((None, now)); continue
        v = a[N][adv]
        if v is None:
            continue
        out.append(v - now); coins.append(r["mint"]); fired += 1; nowR.append((v, now))
    return np.array(out), coins, fired, nowR


def boot(vals, coins, draws=1000, seed=0, q=5):
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for i, c in enumerate(coins):
        by[c].append(i)
    g = [np.array(v) for v in by.values()]
    s = np.array([vals[x].sum() for x in g]); n = np.array([len(x) for x in g])
    res = []
    for _ in range(draws):
        p = rng.integers(0, len(g), len(g))
        res.append(s[p].sum() / n[p].sum())
    return float(np.percentile(res, q))


def main(paths):
    rows = load(paths)
    nowm = np.mean([r["now"]["300"][0] for r in rows if r["now"]["300"][0] is not None])
    print("units", len(rows), "coins", len({r["mint"] for r in rows}), "NOW mean R300 opt %.4f adv %.4f" % (nowm, np.mean([r["now"]["300"][1] for r in rows if r["now"]["300"][1] is not None])))
    keys = sorted(rows[0]["arms"])
    table = []
    for key in keys:
        d0, c0, f0, nr = paired(rows, key, 0)
        d1, c1, f1, _ = paired(rows, key, 1)
        if len(d0) < 50:
            continue
        ent = [v for v, n in nr if v is not None]
        miss_now = [n for v, n in nr if v is None]
        table.append((d0.mean(), key, len(d0), f0 / len(d0), boot(d0, c0), d1.mean(), boot(d1, c1), np.mean(ent) if ent else None,
                      np.mean(miss_now) if miss_now else None))
    table.sort(reverse=True)
    print("%-10s %5s %6s %8s %8s | %8s %8s | entered_R  missed_NOWR" % ("arm", "n", "trig%", "dMean", "lo90", "dAdv", "loAdv"))
    for t in table:
        print("%-10s %5d %6.1f %+8.4f %+8.4f | %+8.4f %+8.4f | %s  %s" % (t[1], t[2], 100 * t[3], t[0], t[4], t[5], t[6],
              "%+.3f" % t[7] if t[7] is not None else "-", "%+.3f" % t[8] if t[8] is not None else "-"))


main(sys.argv[1:])
