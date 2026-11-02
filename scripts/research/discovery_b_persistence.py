#!/usr/bin/env python3
"""Discovery B persistence read (Amendment 1 sec. 5). usage: FLIPS.jsonl ACTORS.json   (V = 2026-10-23; later H with the locked intervals)
Selected = the committed list (docs/audits/frozen/2026-10-26-discovery-b-selection-list.jsonl). Primary flips: completed == False. ret = mirror return with the 2.5 pp residual, ret_exact without.
Observed: mean over the selected actors' flips, coin-clustered one-sided 95% lower bound (bootstrap of mints). Null: for each selected actor with n_i >= 1 flips on the day draw, without replacement, a non-selected actor with
flip count within +-1 and in-range share within +-0.1 (day attributes), pool the drawn actors' flips, mean; 1,000 draws seed 0. Pass needs lb > 0 AND observed > null p95 AND >= 60 flips from >= 8 actors."""
import json, sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
LIST = ROOT / "docs" / "audits" / "frozen" / "2026-10-26-discovery-b-selection-list.jsonl"


def lb95(vals, mints, draws=2000, seed=0):
    u, inv = np.unique(mints, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    rng = np.random.default_rng(seed)
    res = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(u), len(u)) for _ in range(draws))]
    return float(s.sum() / n.sum()), float(np.percentile(res, 5)), len(u)


def main(flips, actors):
    sel = [w["actor"] for w in json.load(open(LIST))["wallets"]]
    sset = set(sel)
    att = json.load(open(actors))["actors"]
    per = defaultdict(lambda: {"r": [], "x": [], "m": []})
    n_all = n_comp = 0
    for line in open(flips):
        f = json.loads(line)
        if f.get("completed"):
            n_comp += 1; continue
        p = per[f["actor"]]; p["r"].append(f["ret"]); p["x"].append(f["ret_exact"]); p["m"].append(f["mint"]); n_all += 1
    print("primary flips", n_all, "completed excluded", n_comp, "actors", len(per))
    allr = np.concatenate([np.array(v["r"]) for v in per.values()])
    print("ALL actors: mean ret %.4f, median %.4f" % (allr.mean(), np.median(allr)))
    def block(actors_in, label):
        r = np.concatenate([np.array(per[a]["r"]) for a in actors_in if a in per] or [np.array([])])
        x = np.concatenate([np.array(per[a]["x"]) for a in actors_in if a in per] or [np.array([])])
        m = np.concatenate([np.array(per[a]["m"]) for a in actors_in if a in per] or [np.array([])])
        act = [a for a in actors_in if a in per]
        if len(r) == 0:
            print(label, "no flips"); return None
        mean, lo, nc = lb95(r, m); mx, lox, _ = lb95(x, m)
        print("%s: actors %d, flips %d, coins %d | mean ret %.4f lb95 %.4f | exact (no residual) mean %.4f lb95 %.4f | median %.4f hit %.1f%%" % (
            label, len(act), len(r), nc, mean, lo, mx, lox, np.median(r), 100 * (r > 0).mean()))
        return mean, lo, len(r), len(act)
    obs = block(sel, "SELECTED (25)")
    block([a for a in sel if not a.startswith("4ygk1m3A")], "SELECTED minus 4ygk1m3A (bot suspect)")
    for a in sel:
        if a in per:
            r = np.array(per[a]["r"]); print("   %s flips %3d mean %+.3f median %+.3f" % (a[:8], len(r), r.mean(), np.median(r)))
        else:
            print("   %s no flips on this day" % a[:8])
    # size-matched random-actor null
    pool = {a: (len(v["r"]), (att[a]["in_range"] / att[a]["buys"]) if a in att and att[a]["buys"] else None) for a, v in per.items() if a not in sset}
    keys = list(pool)
    cnt = np.array([pool[a][0] for a in keys]); shr = np.array([pool[a][1] if pool[a][1] is not None else -9 for a in keys])
    rng = np.random.default_rng(0)
    need = [(len(per[a]["r"]), (att[a]["in_range"] / att[a]["buys"]) if a in att and att[a]["buys"] else 0.0) for a in sel if a in per]
    means = []; unmatched = 0
    cands = [np.where((abs(cnt - n) <= 1) & (abs(shr - s) <= 0.1))[0] for n, s in need]
    for _ in range(1000):
        used, tot, k = set(), 0.0, 0
        for c in cands:
            c = [i for i in c if i not in used]
            if not c:
                unmatched += 1; continue
            i = c[rng.integers(0, len(c))]; used.add(i)
            tot += sum(per[keys[i]]["r"]); k += len(per[keys[i]]["r"])
        means.append(tot / k if k else np.nan)
    means = np.array(means)
    print("NULL (size-matched random actors, 1000 draws): mean %.4f p95 %.4f p99 %.4f; unmatched slots total %d" % (np.nanmean(means), np.nanpercentile(means, 95), np.nanpercentile(means, 99), unmatched))
    if obs:
        ok = obs[1] > 0 and obs[0] > np.nanpercentile(means, 95) and obs[2] >= 60 and obs[3] >= 8
        print("PERSISTENCE VERDICT: lb95>0 %s | observed>null p95 %s | >=60 flips/>=8 actors %s -> %s" % (obs[1] > 0, obs[0] > np.nanpercentile(means, 95), obs[2] >= 60 and obs[3] >= 8, "PASS" if ok else "FAIL"))


if __name__ == "__main__":
    main(*sys.argv[1:3])
