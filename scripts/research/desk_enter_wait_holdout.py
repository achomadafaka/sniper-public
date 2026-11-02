#!/usr/bin/env python3
"""ENTER-NOW vs WAIT table HOLDOUT (prereg 2026-10-23-enter-now-vs-wait-table-preregistration.md section 3). Frozen table_v0.json applied unchanged.
usage: UNITS_H.jsonl [TABLE.json]"""
import json, random, sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_enter_wait_table as T

TABLE = Path(__file__).resolve().parents[2] / "data" / "research" / "enter-wait" / "table_v0.json"


def parse(k):
    return tuple(int(x) if x.lstrip("-").isdigit() else x for x in k.split("|")) if k else ()


def lo5(vals_by_coin, seed=1, draws=2000):
    rnd = random.Random(seed)
    cs = list(vals_by_coin.values())
    est = sorted(sum(sum(p) for p in pick) / sum(len(p) for p in pick) for pick in ([cs[rnd.randrange(len(cs))] for _ in cs] for _ in range(draws)))
    return est[int(0.05 * draws)]


def main(units, table_path=TABLE):
    table = {parse(k): v for k, v in json.load(open(table_path))["table"].items()}
    rows = T.load([units])
    g = T.apply(table, rows)
    now_all = sum(u["now"][1] for _, _, u in rows) / len(rows)
    print("units %d coins %d; global R_now(adverse) %+.4f; global mean delta (now-wait) %+.4f" % (len(rows), len({m for m, _, _ in rows}), now_all, sum(d for _, d, _ in rows) / len(rows)))
    res = {}
    for name, items in g.items():
        byc = defaultdict(list)
        for m, d, _ in items:
            byc[m].append(-d)                     # wait advantage = R_wait - R_now
        nowm = sum(u["now"][1] for _, _, u in items) / len(items) if items else None
        adv = sum(sum(v) for v in byc.values()) / len(items) if items else None
        lo = lo5(byc) if len(byc) >= 2 else None
        miss = sum(1 for _, _, u in items if u["wait"] is None) / len(items) if items else None
        res[name] = {"n": len(items), "coins": len(byc), "wait_adv": adv, "wait_adv_lo95": lo, "R_now": nowm, "miss": miss}
        print(name, json.dumps(res[name]))
    w, p = res["wait"], res["pass"]
    c1 = w["coins"] >= 100 and w["wait_adv_lo95"] is not None and w["wait_adv_lo95"] > 0
    c2 = p["R_now"] is not None and p["R_now"] >= now_all
    print("CONFIRM: WAIT-set advantage lb>0 and >=100 coins: %s ; PASS-set R_now >= global NOW mean: %s ; verdict %s" % (c1, c2, "PASS" if c1 and c2 else "FAIL"))


if __name__ == "__main__":
    main(*sys.argv[1:])
