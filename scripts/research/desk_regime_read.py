#!/usr/bin/env python3
"""Regime frequency/duration (part a) and play grading (part b) per docs/audits/2026-10-23-regime-play-preregistration.md.
usage: desk_regime_read.py UNITS1[,UNITS2..] GRID1[,GRID2..]"""
import json, sys
from collections import Counter, defaultdict
import numpy as np


def boot_lo(vals, coins, q=5, draws=1000, seed=0):
    rng = np.random.default_rng(seed)
    by = defaultdict(list)
    for i, c in enumerate(coins):
        by[c].append(i)
    g = [np.array(v) for v in by.values()]
    v = np.asarray(vals, float)
    s = np.array([v[x].sum() for x in g]); n = np.array([len(x) for x in g])
    res = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(g), len(g)) for _ in range(draws))]
    return float(np.percentile(res, q))


def part_a(grid_paths):
    rows = [json.loads(l) for p in grid_paths for l in open(p)]
    cnt = Counter(r[2] for r in rows if r[2] not in ("OUT", "UNKNOWN", "QUIET"))
    tot = sum(cnt.values())
    print("== (a) grid times in band with a regime: %d  QUIET (noise 0) %d  UNKNOWN %d  OUT %d" % (tot, sum(1 for r in rows if r[2] == "QUIET"), sum(1 for r in rows if r[2] == "UNKNOWN"), sum(1 for r in rows if r[2] == "OUT")))
    runs, trans, coins_with = defaultdict(list), Counter(), defaultdict(set)
    prev_m, prev_r, run = None, None, 0
    for m, t, r in rows + [("", 0, "END")]:
        if m != prev_m or r != prev_r:
            if prev_r not in (None, "OUT", "UNKNOWN", "QUIET", "END"):
                runs[prev_r].append(run * 30)
                if m == prev_m and r not in ("OUT", "UNKNOWN", "QUIET", "END"):
                    trans[(prev_r, r)] += 1
            prev_m, prev_r, run = m, r, 0
        run += 1
        if r not in ("OUT", "UNKNOWN", "QUIET", "END"):
            coins_with[r].add(m)
    for rg in ("DYING", "IMPULSE", "RANGE", "TREND", "CONSOLIDATION", "OTHER"):
        d = runs.get(rg, [])
        if d:
            print("%-14s share %5.1f%%  episodes %5d  median %4ds p90 %5ds  coins %d" % (rg, 100 * cnt[rg] / tot, len(d), np.median(d), np.percentile(d, 90), len(coins_with[rg])))
    top = sorted(trans.items(), key=lambda x: -x[1])[:8]
    print("top transitions:", ", ".join("%s>%s %d" % (a, b, n) for (a, b), n in top))


def R(v, adv):
    return None if v is None else v[adv]


def part_b(unit_paths):
    units = [json.loads(l) for p in unit_paths for l in open(p)]
    print("\n== (b) units %d, coins %d" % (len(units), len({u["mint"] for u in units})))
    for rg in ("DYING", "IMPULSE", "RANGE", "TREND", "CONSOLIDATION", "OTHER"):
        us = [u for u in units if u["regime"] == rg]
        if len(us) < 30:
            continue
        print("\n%s  units %d coins %d" % (rg, len(us), len({u["mint"] for u in us})))
        for play in ("NOW", "PULLBACK", "BREAKOUT", "DUMP_RECOVERY"):
            if play not in us[0]["plays"] and not any(play in u["plays"] for u in us):
                continue
            for adv in (0, 1):
                vals, now_, coins, fired = [], [], [], 0
                for u in us:
                    nv = u["plays"]["NOW"]["300"][adv]
                    p = u["plays"].get(play)
                    if play == "NOW":
                        v = nv
                    elif p is None:
                        v = 0.0
                    else:
                        v = p["R"][adv]
                        if v is None:
                            continue
                        fired += 1
                    vals.append(v); now_.append(nv); coins.append(u["mint"])
                a = np.array(vals); n_ = np.array(now_)
                lo_abs = boot_lo(a, coins)
                lo_d = boot_lo(a - n_, coins) if play != "NOW" else float("nan")
                print("  %-13s %s n %4d trig %5.1f%% mean R %+.3f lo90 %+.3f  vsNOW %+.3f lo90 %+.3f" % (
                    play, "adv" if adv else "opt", len(a), 100 * fired / len(a) if play != "NOW" else 100.0, a.mean(), lo_abs, (a - n_).mean(), lo_d))
        if rg == "RANGE":
            for adv in (0, 1):
                cyc = [c for u in us for c in u["plays"]["SCALP"][adv]]
                per_unit = [sum(c["R"] for c in u["plays"]["SCALP"][adv]) for u in us]
                if cyc:
                    wn = np.array([c["width_n"] for c in cyc]); wp = np.array([c["width_pct"] for c in cyc]); r = np.array([c["R"] for c in cyc])
                    print("  SCALP %s cycles %d units-with-cycle %d mean R/cycle %+.3f; per-unit sum mean %+.3f; exits %s; median width %.1f n / %.1f%%; median impact %.2f%%" % (
                        "adv" if adv else "opt", len(cyc), sum(1 for u in us if u["plays"]["SCALP"][adv]), r.mean(), np.mean(per_unit),
                        dict(Counter(c["exit"] for c in cyc)), np.median(wn), 100 * np.median(wp), 100 * np.median([c["impact"] for c in cyc])))
                    for lo, hi in ((0, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 9)):
                        m = (wp >= lo) & (wp < hi)
                        if m.sum() >= 5:
                            print("     width %2d-%3d%%: cycles %3d meanR %+.3f win %.0f%%" % (100 * lo, 100 * min(hi, 9), m.sum(), r[m].mean(), 100 * (r[m] > 0).mean()))


if __name__ == "__main__":
    part_b(sys.argv[1].split(","))
    part_a(sys.argv[2].split(","))
