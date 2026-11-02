#!/usr/bin/env python3
"""ENTER-NOW vs WAIT table (pre-reg docs/audits/2026-10-23-enter-now-vs-wait-table-preregistration.md). usage: OUT.json FIT.jsonl [FIT2.jsonl ...] [-- CHECK.jsonl ...]"""
import json, random, sys
from collections import defaultdict

CUTS = (0.022041227292639198, 0.07272359951628962)
N0, DRAWS = 50, 2000


def b3(x, cuts):
    return None if x is None else sum(x >= c for c in cuts)


def key(u):
    return {"pos300": "flat" if u["pos300"] is None else b3(u["pos300"], (0.5, 0.9)),
            "flow60": "quiet" if u["flow60"] is None else b3(u["flow60"], (0.5, 0.7)),
            "noise": b3(u["noise"], CUTS),
            "fill": "na" if u["curve_fill"] is None else b3(u["curve_fill"], (0.4, 0.8))}


def levels(k):
    return [(k["pos300"], k["flow60"], k["noise"], k["fill"]), (k["pos300"], k["flow60"], k["noise"]), (k["pos300"], k["flow60"]), (k["pos300"],), ()]


def delta(u):
    """R_now (adverse) minus R_wait (optimistic); a MISS waits for nothing: R_wait = 0. None when a leg is unknown."""
    now = u["now"][1]
    wait = 0.0 if u["wait"] is None else u["wait"][0]
    return None if now is None or wait is None else now - wait


def load(paths):
    out = []
    for p in paths:
        for l in open(p):
            u = json.loads(l)
            d = delta(u)
            if d is not None:
                out.append((u["mint"], d, u))
    return out


def boot(vals_by_coin, rnd):
    cs = list(vals_by_coin.values())
    est = []
    for _ in range(DRAWS):
        pick = [cs[rnd.randrange(len(cs))] for _ in cs]
        n = sum(len(p) for p in pick)
        est.append(sum(sum(p) for p in pick) / n)
    est.sort()
    return est[int(.025 * DRAWS)], est[int(.975 * DRAWS)]


def build(rows):
    cells = defaultdict(lambda: defaultdict(list))
    for mint, d, u in rows:
        for lv in levels(key(u)):
            cells[len(lv)][lv].append((mint, d))
    rnd = random.Random(0)
    table = {}
    for ln in sorted(cells, reverse=True):
        for lv, items in cells[ln].items():
            byc = defaultdict(list)
            for m, d in items:
                byc[m].append(d)
            mean = sum(d for _, d in items) / len(items)
            lo, hi = boot(byc, rnd) if len(byc) >= 2 else (None, None)
            table[lv] = {"n": len(items), "coins": len(byc), "mean": mean, "lo": lo, "hi": hi}
    # shrink to the parent (drop the last element), global = ()
    for lv in sorted(table, key=len):
        t = table[lv]
        if lv == ():
            t["shrunk"] = t["mean"]; continue
        w = t["coins"] / (t["coins"] + N0)
        t["shrunk"] = w * t["mean"] + (1 - w) * table[lv[:-1]]["shrunk"]
    return table


def lookup(table, u):
    for lv in levels(key(u)):
        t = table.get(lv)
        if t and t["coins"] >= 30:
            return lv, t
    return (), table[()]


def apply(table, rows):
    g = {"wait": [], "pass": []}
    for mint, d, u in rows:
        lv, t = lookup(table, u)
        hi = t["hi"] if t["hi"] is not None else 1.0
        # WAIT only when the cell is clearly WAIT-better: shrunk delta < 0 and its upper bound < 0
        (g["wait"] if (t["shrunk"] < 0 and hi < 0) else g["pass"]).append((mint, d, u))
    return g


def report(rows, tag, table):
    g = apply(table, rows)
    out = {}
    for name, items in g.items():
        if not items:
            out[name] = {"n": 0}; continue
        byc = defaultdict(list)
        for m, d, _ in items:
            byc[m].append(d)
        lo, hi = boot(byc, random.Random(1)) if len(byc) >= 2 else (None, None)
        nowm = sum(u["now"][1] for _, _, u in items) / len(items)
        waitm = sum((0 if u["wait"] is None else u["wait"][0]) for _, _, u in items) / len(items)
        miss = sum(1 for _, _, u in items if u["wait"] is None) / len(items)
        out[name] = {"n": len(items), "coins": len(byc), "mean_delta_now_minus_wait": sum(d for _, d, _ in items) / len(items), "lo": lo, "hi": hi,
                     "R_now_adverse": nowm, "R_wait_opt_miss0": waitm, "miss_rate": miss}
    return {tag: out}


if __name__ == "__main__":
    out, rest = sys.argv[1], sys.argv[2:]
    fit = rest[:rest.index("--")] if "--" in rest else rest
    chk = rest[rest.index("--") + 1:] if "--" in rest else []
    rows = load(fit)
    table = build(rows)
    res = {"fit_units": len(rows), "global": table[()], **report(rows, "fit", table)}
    if chk:
        res.update(report(load(chk), "check", table))
    json.dump({"result": res, "table": {"|".join(map(str, k)): v for k, v in table.items()}}, open(out, "w"))
    print(json.dumps(res, indent=1))
