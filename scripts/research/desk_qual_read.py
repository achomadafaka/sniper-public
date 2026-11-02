#!/usr/bin/env python3
"""Greedy qualification stack per docs/audits/2026-10-23-qualification-set-preregistration.md.
usage: desk_qual_read.py D1.jsonl D2.jsonl [-- EVAL.jsonl ...]    (D stack fit; EVAL applies the fixed stack, descriptive or holdout)"""
import json, sys
from collections import defaultdict
from datetime import datetime, timezone
import numpy as np

CANDS = []
for th in (15, 30, 60): CANDS.append(("buyers>=%d" % th, lambda f, th=th: f["buyers"] >= th))
for th in (1.0, 1.5): CANDS.append(("growth>=%.1f" % th, lambda f, th=th: f["buyers_growth"] is not None and f["buyers_growth"] >= th))
for th in (0.55, 0.65, 0.75): CANDS.append(("net_buy>=%.2f" % th, lambda f, th=th: f["net_buy"] is not None and f["net_buy"] >= th))
for th in (0.5, 0.3): CANDS.append(("dust<=%.1f" % th, lambda f, th=th: f["dust"] is not None and f["dust"] <= th))
for th in (0.30, 0.15): CANDS.append(("top_buyer<=%.2f" % th, lambda f, th=th: f["diffuse"] is not None and f["diffuse"] <= th))
for th in (0.0, 0.5): CANDS.append(("creator_sold<=%.1f" % th, lambda f, th=th: f["creator_sold"] is not None and f["creator_sold"] <= th))
for th in (60, 120, 180): CANDS.append(("age>=%d" % th, lambda f, th=th: f["age"] >= th))
for th in (20, 50): CANDS.append(("active60>=%d" % th, lambda f, th=th: f["active"] >= th))
for th in (0.8, 0.9): CANDS.append(("not_dying>=%.1f" % th, lambda f, th=th: f["hi_ratio"] >= th))
for th in (0.25, 0.50): CANDS.append(("ext<=%.2f" % th, lambda f, th=th: f["ext"] <= th))
for th in (0.02, 0.04): CANDS.append(("exit_depth<=%.2f" % th, lambda f, th=th: f["exit_depth"] <= th))


def load(paths):
    return [json.loads(l) for p in paths for l in open(p)]


def stat(rows, mask, key="r5"):
    sel = [r for r, m in zip(rows, mask) if m]
    if not sel:
        return None
    coin_s, coin_n = defaultdict(float), defaultdict(int)
    for r in sel:
        coin_s[r["mint"]] += r[key]; coin_n[r["mint"]] += 1
    N = len(sel); mean = sum(coin_s.values()) / N
    se = (sum((coin_s[c] - mean * coin_n[c]) ** 2 for c in coin_s) ** 0.5) / N
    return {"n": N, "coins": len(coin_s), "mean": mean, "lo90": mean - 1.2816 * se, "lo95": mean - 1.645 * se,
            "pos": float(np.mean([r[key] > 0 for r in sel]))}


def main():
    a = sys.argv[1:]
    k = a.index("--") if "--" in a else len(a)
    D, EV = load(a[:k]), load(a[k + 1:])
    D = [r for r in D if r["f"]]
    masks = {name: np.array([bool(fn(r["f"])) for r in D]) for name, fn in CANDS}
    cur = np.ones(len(D), bool)
    base = stat(D, cur)
    print("D units %d coins %d base P>0 %.3f mean %+.4f" % (base["n"], base["coins"], base["pos"], base["mean"]))
    path, prev = [], base["mean"]
    for step in range(6):
        best = None
        for name, m in masks.items():
            if name in path:
                continue
            s = stat(D, cur & m)
            if s and s["n"] >= 300 and s["coins"] >= 150 and s["mean"] >= prev + 0.02 and (best is None or s["mean"] > best[1]["mean"]):
                best = (name, s)
        if best is None:
            break
        path.append(best[0]); cur &= masks[best[0]]; prev = best[1]["mean"]
        print("step %d + %-18s units %5d coins %4d P>0 %.3f mean %+.4f lo90 %+.4f" % (step + 1, best[0], best[1]["n"], best[1]["coins"], best[1]["pos"], best[1]["mean"], best[1]["lo90"]))
    final = stat(D, cur)
    print("FINAL D stack:", path, "positive mean:", final["mean"] > 0 if final else None)
    adv = stat(D, cur, "r5_adv")
    print("   D adverse mean %+.4f  1.5s mean %+.4f" % (adv["mean"], stat(D, cur, "r15")["mean"]))
    hours = defaultdict(set)
    for r, m in zip(D, cur):
        if m:
            hours[datetime.fromtimestamp(r["t0"], timezone.utc).hour].add(r["mint"])
    print("   qualifying coins by UTC hour (D, 2 days summed):", {h: len(v) for h, v in sorted(hours.items())})
    if EV:
        EV = [r for r in EV if r["f"]]
        em = np.array([all(dict(CANDS)[nm](r["f"]) for nm in path) for r in EV])
        al = stat(EV, np.ones(len(EV), bool)); st = stat(EV, em); sa = stat(EV, em, "r5_adv")
        print("\nEVAL units %d coins %d all-mean %+.4f P>0 %.3f" % (al["n"], al["coins"], al["mean"], al["pos"]))
        if st:
            print("EVAL stack: units %d coins %d P>0 %.3f mean %+.4f lo95 %+.4f | adverse mean %+.4f lo95 %+.4f | 1.5s %+.4f" % (
                st["n"], st["coins"], st["pos"], st["mean"], st["lo95"], sa["mean"], sa["lo95"], stat(EV, em, "r15")["mean"]))
            print("   stack share of units %.2f%%; coins per day ~%.0f" % (100 * st["n"] / al["n"], st["coins"]))


main()
