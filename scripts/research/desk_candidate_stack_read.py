#!/usr/bin/env python3
"""DISCOVERY: JUDGE candidate emissions graded by the three qualification-stack conditions (growth >= 1.5, creator_sold <= 0.5, net_buy >= 0.65)
from tape as-of features at emission, outcome R300 at 5 s delay (desk_young fill_r). Not pre-registered as a claim; descriptive. usage: OUT.json"""
import bisect, json, random, sqlite3, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from desk_young_units import fill_r, N
from desk_qual_units import feats
from desk_drift_calibration import load_coin, ROOT


def stack(f):
    return (f["buyers_growth"] is not None and f["buyers_growth"] >= 1.5 and f["creator_sold"] is not None and f["creator_sold"] <= 0.5
            and f["net_buy"] is not None and f["net_buy"] >= 0.65)


def boot(by_coin, draws=2000):
    cs = list(by_coin.values())
    rnd = random.Random(0)
    est = sorted(sum(sum(p) for p in (cs[rnd.randrange(len(cs))] for _ in cs)) / max(1, sum(len(p) for p in (cs[rnd.randrange(len(cs))] for _ in cs))) for _ in range(draws))
    return est[int(.05 * draws)], est[int(.95 * draws)]


def main(out):
    D = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data/memecoin_desk.db"), uri=True)
    verdict = {}
    for tid, v in D.execute("SELECT ticket_id, verdict FROM desk_tickets WHERE stage='vet'"):
        verdict[tid] = v
    rows, cens = [], defaultdict(int)
    for oid, tid, mint, em in D.execute("SELECT opportunity_id, ticket_id, mint, emitted_at FROM desk_candidate_emissions WHERE is_primary=1 ORDER BY emitted_at"):
        t = datetime.fromisoformat(em).timestamp()
        c = load_coin(mint, [em[:10]])
        if c is None or not (c.first_ok and c.quote_ok):
            cens["no_tape_or_unverified"] += 1; continue
        i = bisect.bisect_right(c.t, t) - 1
        if i + 1 < 8:
            cens["thin"] += 1; continue
        r = fill_r(c, i, 5.0, False)
        if r is None:
            cens["outcome_unknown"] += 1; continue
        f = feats(c, i)
        rows.append({"mint": mint, "t": t, "r": r, "stack": stack(f), "verdict": verdict.get(tid, "none"), "f": f})
    res = {"emissions": len(rows) + sum(cens.values()), "scored": len(rows), "censored": dict(cens)}
    for name, sel in (("all", lambda x: True), ("stack", lambda x: x["stack"]), ("not_stack", lambda x: not x["stack"]),
                      ("ENTER", lambda x: x["verdict"] == "ENTER"), ("ENTER&stack", lambda x: x["verdict"] == "ENTER" and x["stack"]),
                      ("ENTER&not_stack", lambda x: x["verdict"] == "ENTER" and not x["stack"]),
                      ("SKIP", lambda x: x["verdict"] == "SKIP"), ("SKIP&stack", lambda x: x["verdict"] == "SKIP" and x["stack"])):
        s = [x for x in rows if sel(x)]
        if not s:
            res[name] = {"n": 0}; continue
        by = defaultdict(list)
        for x in s:
            by[x["mint"]].append(x["r"])
        lo, hi = boot(by) if len(by) > 1 else (None, None)
        res[name] = {"n": len(s), "coins": len(by), "mean_R300": sum(x["r"] for x in s) / len(s), "win": sum(1 for x in s if x["r"] > 0) / len(s), "lo90": lo, "hi90": hi}
    json.dump({"result": res, "rows": rows}, open(out, "w"))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(sys.argv[1])
