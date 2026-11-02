#!/usr/bin/env python3
"""Read desk_stage_study.py output: stage tables, entry rules, the 30k->completion comparison, desk closes by entry stage. usage: DAY1.jsonl [DAY2.jsonl ...]"""
import json, sqlite3, sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

sys.path.insert(0, str(Path(__file__).parent))
from tape_reader_samples import FEATS

ROOT = Path(__file__).resolve().parents[2]
NAMES = {1: "S1 4k->10k", 2: "S2 10k->20k", 3: "S3 20k->30k", 4: "S4 30k->42.8k(completion)"}


def lo90(vals, coins):
    u, inv = np.unique(coins, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    mean = s.sum() / n.sum()
    se = np.sqrt(((s - mean * n) ** 2).sum()) / n.sum()
    return mean, mean - 1.2816 * se


def main(paths):
    rows = [json.loads(l) for p in paths for l in open(p)]
    coins = [r for r in rows if r["kind"] == "coin"]; st = [r for r in rows if r["kind"] == "stage"]
    print("days", len(paths), "standard-curve coins", len(coins), "high-start", sum(c["hi_start"] for c in coins), "(%.1f%%)" % (100 * np.mean([c["hi_start"] for c in coins])))
    for hs in (False, True):
        cs = [c for c in coins if c["hi_start"] == hs]
        print("\n=== %s coins: n %d ===" % ("HIGH-START" if hs else "curve-start", len(cs)))
        for sid in (1, 2, 3, 4):
            reach = sum(c["reached"][sid - 1] for c in cs)
            rs = [r for r in st if r["stage"] == sid and r["hi_start"] == hs]
            o = defaultdict(int)
            for r in rs:
                o[r["out"]] += 1
            res = len(rs) - o["C"]
            tt = [r["t_out"] for r in rs if r["out"] == "U"]
            print("%s: reach %d (%.1f%% of coins) | U %d D %d N %d C %d | P(U|resolved) %.1f%% P(D|resolved) %.1f%% | median s to U %s" % (
                NAMES[sid], reach, 100 * reach / max(len(cs), 1), o["U"], o["D"], o["N"], o["C"], 100 * o["U"] / max(res, 1), 100 * o["D"] / max(res, 1),
                round(float(np.median(tt))) if tt else None))
            for ent in ("e1", "e2", "e3"):
                line = []
                for mode in ("x1", "x2"):
                    v = [(r[ent + mode], r["mint"]) for r in rs if r.get(ent + mode) is not None]
                    if len(v) >= 20:
                        m, lo = lo90(np.array([x[0] for x in v]), np.array([x[1] for x in v]))
                        line.append("%s n %d mean %+.3f lo90 %+.3f P>0 %.0f%%" % (mode, len(v), m, lo, 100 * np.mean([x[0] > 0 for x in v])))
                    else:
                        line.append("%s n %d" % (mode, len(v)))
                print("    %s  %s" % (ent, " | ".join(line)))
    print("\n=== (d) crossers of 30k (S4 entry), curve-start: continue (U) vs die (D), AUC per feature, one row per coin ===")
    rs = [r for r in st if r["stage"] == 4 and not r["hi_start"] and r["out"] in ("U", "D") and r["feat"]]
    y = np.array([r["out"] == "U" for r in rs]); X = np.array([[np.nan if v is None else v for v in r["feat"]] for r in rs], float)
    print("n %d (U %d, D %d)" % (len(rs), y.sum(), (~y).sum()))
    res = []
    for k, f in enumerate(FEATS):
        ok = ~np.isnan(X[:, k])
        if ok.sum() > 40 and 0 < y[ok].sum() < ok.sum() and np.ptp(X[ok, k]) > 0:
            res.append((roc_auc_score(y[ok], X[ok, k]), f, int(ok.sum()), float(np.median(X[ok & y, k])), float(np.median(X[ok & ~y, k]))))
    for a, f, n, mu, md in sorted(res, key=lambda x: -abs(x[0] - 0.5))[:10]:
        print("  %-14s AUC %.3f n %d median U %.4g vs D %.4g" % (f, a, n, mu, md))
    # desk closes by entry stage (sheet mcap at the vet that preceded the ENTER)
    d = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "memecoin_desk.db"), uri=True); s = sqlite3.connect("file:%s?mode=ro" % (ROOT / "sniper_tracker.db"), uri=True)
    by = defaultdict(list)
    for pid, mint, ts in d.execute("SELECT position_id, mint, ts FROM desk_tickets WHERE verdict='ENTER' AND position_id IS NOT NULL").fetchall():
        v = d.execute("SELECT inputs_json FROM desk_tickets WHERE mint=? AND stage='vet' AND ts<=? ORDER BY ts DESC LIMIT 1", (mint, ts)).fetchone()
        p = s.execute("SELECT realized_pnl, is_open FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
        if not v or not p or p[1] != 0 or p[0] is None:
            continue
        try:
            mc = json.loads(v[0])["sheet"]["mcap_usd"]
        except Exception:
            continue
        by["S1 <10k" if mc < 10000 else "S2 10-20k" if mc < 20000 else "S3 20-30k" if mc < 30000 else "S4 30k+"].append(p[0])
    print("\n=== desk funded closes by sheet-mcap stage (10-22..10-23, n tiny) ===")
    for k in sorted(by):
        v = by[k]; print("  %-10s n %2d  pnl sum %+7.2f  mean %+6.2f  wins %d" % (k, len(v), sum(v), np.mean(v), sum(x > 0 for x in v)))


if __name__ == "__main__":
    main(sys.argv[1:])
