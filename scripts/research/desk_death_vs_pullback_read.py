#!/usr/bin/env python3
"""Grade desk_tape_events output per docs/audits/2026-10-23-death-vs-pullback-preregistration.md (sections 4-5).
usage: desk_death_vs_pullback_read.py EVENTS.jsonl [EVENTS2.jsonl ...]   (discovery files, or the holdout once)"""
import json
import sys
from collections import defaultdict

import numpy as np

K_GRID = ("2", "3", "4", "6")
INDS = ("SELL_BURST", "EXODUS", "STRUCT_BREAK", "ONE_SWAP_DROP", "TOP_DUMP")


def load(paths):
    rows = []
    for p in paths:
        rows += [json.loads(l) for l in open(p)]
    return rows


def boot_lo(vals, coins, draws=1000, seed=0):
    """one-sided 95% lower bound of the mean, resampling coins."""
    rng = np.random.default_rng(seed)
    idx = defaultdict(list)
    for i, c in enumerate(coins):
        idx[c].append(i)
    groups = [np.array(v) for v in idx.values()]
    v = np.asarray(vals, float)
    sums = np.array([v[g].sum() for g in groups])
    ns = np.array([len(g) for g in groups])
    out = []
    for _ in range(draws):
        pick = rng.integers(0, len(groups), len(groups))
        out.append(sums[pick].sum() / ns[pick].sum())
    return float(np.percentile(out, 5))


def flags(ev, name, k):
    f = ev["ind"][name][k]
    return bool(f)


def score(evs, flag):
    f = np.array([flag(e) for e in evs])
    F = np.array([e["F"] for e in evs])
    ben = np.where(f, -F, 0.0)
    coins = [e["mint"] for e in evs]
    return {"n": len(evs), "coins_flagged": len({c for c, x in zip(coins, f) if x}), "share": float(f.mean()),
            "benefit": float(ben.mean()), "lo": boot_lo(ben, coins), "F_unflagged": float(F[~f].mean()) if (~f).any() else None,
            "F_all": float(F.mean()), "dB_all_lo": boot_lo(ben - (-F), coins)}


def desk_strata():
    """{(mint): 'ENTER'|'SKIP'} from the desk ledger's vet-stage verdicts (stratum read; descriptive, small n)."""
    import sqlite3
    d = sqlite3.connect("file:data/memecoin_desk.db?mode=ro", uri=True)
    return {m: v for m, v in d.execute("SELECT mint, verdict FROM desk_tickets WHERE stage='vet' AND verdict IN ('ENTER','SKIP')")}


def main(paths):
    rows = load(paths)
    strata = desk_strata()
    for lab, sel in (("PRIMARY (completed-in-window excluded)", lambda e: not e.get("grad")),
                     ("SENSITIVITY incl. completed (F upper bound)", lambda e: True)):
        evs0 = [e for e in rows if e["drop"] == 0.9 and not e["adverse"] and sel(e)]
        print("\n##", lab, "events", len(evs0), "coins", len({e["mint"] for e in evs0}), "completed share %.3f" % np.mean([bool(e.get("grad")) for e in evs0]))
        print("B_all benefit %+.4f  meanF %+.4f  CRASH rate %.3f" % (np.mean([-e["F"] for e in evs0]), np.mean([e["F"] for e in evs0]), np.mean([e["F"] <= -0.3 for e in evs0])))
        for st in ("ENTER", "SKIP"):
            ev = [e for e in evs0 if e["level"] == 30000 and strata.get(e["mint"]) == st]
            if ev:
                print("  stratum %-5s events %3d coins %3d meanF %+.4f CRASH %.3f medianF %+.4f" % (st, len(ev), len({e["mint"] for e in ev}), np.mean([e["F"] for e in ev]), np.mean([e["F"] <= -0.3 for e in ev]), np.median([e["F"] for e in ev])))
    for adverse in (False,):
        evs = [e for e in rows if e["drop"] == 0.9 and e["adverse"] == adverse and not e.get("grad")]
        print("\n== %s ordering, drop 10%%: events %d coins %d; B_all benefit %.4f; mean F %.4f; CRASH(F<=-0.3) rate %.3f" % (
            "adverse" if adverse else "optimistic", len(evs), len({e["mint"] for e in evs}),
            float(np.mean([-e["F"] for e in evs])), float(np.mean([e["F"] for e in evs])),
            float(np.mean([e["F"] <= -0.3 for e in evs]))))
        best = {}
        for name in INDS:
            res = {k: score(evs, lambda e, n=name, kk=k: flags(e, n, kk)) for k in K_GRID}
            for k, r in res.items():
                print("%-14s k=%s flag%% %.1f coins %4d benefit %+.4f lo %+.4f vsB_all lo %+.4f F_unfl %+.4f" % (
                    name, k, 100 * r["share"], r["coins_flagged"], r["benefit"], r["lo"], r["dB_all_lo"], r["F_unflagged"] or 0))
            best[name] = max(K_GRID, key=lambda k: res[k]["benefit"])
        cs = score(evs, lambda e: bool(e["ind"]["CREATOR_SELL"]["0"]))
        print("CREATOR_SELL   flag%% %.1f coins %d benefit %+.4f lo %+.4f" % (100 * cs["share"], cs["coins_flagged"], cs["benefit"], cs["lo"]))
        any_f = lambda e: any(flags(e, n, best[n]) for n in INDS) or bool(e["ind"]["CREATOR_SELL"]["0"])
        two_f = lambda e: (sum(flags(e, n, best[n]) for n in INDS) + bool(e["ind"]["CREATOR_SELL"]["0"])) >= 2
        for lab, fn in (("ANY", any_f), ("TWO_OF", two_f)):
            r = score(evs, fn)
            print("%-14s (best k per indicator %s) flag%% %.1f coins %d benefit %+.4f lo %+.4f vsB_all lo %+.4f F_unfl %+.4f" % (
                lab, best, 100 * r["share"], r["coins_flagged"], r["benefit"], r["lo"], r["dB_all_lo"], r["F_unflagged"] or 0))


if __name__ == "__main__":
    main(sys.argv[1:])
