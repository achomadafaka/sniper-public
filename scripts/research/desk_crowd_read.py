#!/usr/bin/env python3
"""Crowd-burst abstain grading per docs/audits/2026-10-23-crowd-burst-abstain-preregistration.md.
usage: desk_crowd_read.py D1.jsonl D2.jsonl [-- EVAL.jsonl]"""
import json, sqlite3, sys
from collections import defaultdict
import numpy as np


def load(paths):
    return [r for p in paths for l in open(p) for r in [json.loads(l)] if r["f"]]


def cluster_stat(vals, coins):
    s, n = defaultdict(float), defaultdict(int)
    for v, c in zip(vals, coins):
        s[c] += v; n[c] += 1
    N = len(vals); m = sum(s.values()) / N
    se = (sum((s[c] - m * n[c]) ** 2 for c in s) ** 0.5) / N
    return m, se


def flag_abs(r, N, T):
    return r["f"]["wmax%d" % T] >= N


def flag_hist(r, m):
    h = r["f"]["hist_med"]
    return h is not None and h > 0 and r["f"]["w60"] >= m * h


def grade(rows, flag, label, key="r5"):
    f = np.array([bool(flag(r)) for r in rows])
    R = np.array([r[key] for r in rows]); coins = [r["mint"] for r in rows]
    org = R >= 0.20
    if f.sum() == 0:
        return None
    ben = -(R * f)                                       # skipping a flagged unit removes its R from the book
    m, se = cluster_stat(ben, coins)
    return {"label": label, "share": f.mean(), "coins": len({c for c, x in zip(coins, f) if x}), "flag_meanR": R[f].mean(),
            "unflag_meanR": R[~f].mean(), "false_abstain": f[org].mean() if org.any() else None, "benefit": m, "ben_lo95": m - 1.645 * se,
            "organic_n": int(org.sum())}


def main():
    a = sys.argv[1:]
    k = a.index("--") if "--" in a else len(a)
    D, EV = load(a[:k]), load(a[k + 1:])
    print("D units %d coins %d mean R300 %+.3f" % (len(D), len({r["mint"] for r in D}), np.mean([r["r5"] for r in D])))
    cands = [(("abs", N, T), lambda r, N=N, T=T: flag_abs(r, N, T)) for N in (50, 100, 150, 250) for T in (60, 180, 300)]
    cands += [(("hist", m, 0), lambda r, m=m: flag_hist(r, m)) for m in (3, 5)]
    print("%-12s %6s %5s %9s %9s %7s %8s %8s" % ("rule", "share", "coins", "flag_R", "unfl_R", "falseAb", "benefit", "lo95"))
    table = []
    for key, fn in cands:
        g = grade(D, fn, key)
        if g:
            table.append((key, fn, g))
            print("%-12s %5.1f%% %5d %+9.3f %+9.3f %6.1f%% %+8.4f %+8.4f" % ("/".join(map(str, key)), 100 * g["share"], g["coins"], g["flag_meanR"], g["unflag_meanR"], 100 * (g["false_abstain"] or 0), g["benefit"], g["ben_lo95"]))
    ok = [t for t in table if t[2]["false_abstain"] is not None and t[2]["false_abstain"] <= 0.15 and t[2]["share"] <= 0.25 and t[0][0] == "abs"]
    if not ok:
        print("no absolute rule meets false-abstain <= 15% and flagged share <= 25% on D"); return
    key, fn, g = max(ok, key=lambda t: t[2]["benefit"])
    print("CHOSEN (abs):", key, "benefit %+.4f" % g["benefit"])
    j = sqlite3.connect("file:data/memecoin_desk.db?mode=ro", uri=True)
    from datetime import datetime
    ev = defaultdict(list)                                # mint -> [(emitted epoch, verdict)] at the judged moment
    for m, em, v in j.execute("SELECT e.mint, e.emitted_at, t.verdict FROM desk_candidate_emissions e JOIN desk_tickets t ON t.ticket_id=e.ticket_id "
                              "WHERE e.is_primary=1 AND t.stage='vet' AND t.verdict IN ('ENTER','SKIP')"):
        ev[m].append((datetime.fromisoformat(em).timestamp(), v))
    def verdict(r):
        for ts, v in ev.get(r["mint"], ()):
            if abs(r["t0"] - ts) <= 5:                     # the unit AT the judged moment, never a later-run coin's earlier levels
                return v
        return None
    for lab, rows in (("D", D), ("EVAL", EV)):
        if not rows:
            continue
        for ky in ("r5", "r5_adv"):
            gg = grade(rows, fn, lab, ky)
            if gg:
                print("%s %-6s share %5.1f%% coins %d flagR %+.3f unflagR %+.3f falseAb %.1f%% benefit %+.4f lo95 %+.4f" % (lab, ky, 100 * gg["share"], gg["coins"], gg["flag_meanR"], gg["unflag_meanR"], 100 * (gg["false_abstain"] or 0), gg["benefit"], gg["ben_lo95"]))
        for st in ("ENTER", "SKIP"):
            us = [r for r in rows if verdict(r) == st]
            fl = [r for r in us if fn(r)]
            if us:
                print("   %s %s stratum: units %d (flagged %d) meanR all %+.3f flagged %s" % (lab, st, len(us), len(fl), np.mean([r["r5"] for r in us]), "%+.3f" % np.mean([r["r5"] for r in fl]) if fl else "-"))


main()
