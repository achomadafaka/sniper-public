#!/usr/bin/env python3
"""Desk JUDGE ENTER verdicts (stage vet) by coin age at the verdict: what the coin did on our curve tape afterwards (descriptive).
usage: [SINCE_ISO]   Exact 25 USD clip buy at +1.5 s, exact sell at +1.5 s + h (h = 60, 300 s), fees, 2.5 pp; completion = any swap with real_token <= 0 inside the window; up/dn = max/min price vs the price at the verdict."""
import bisect, sqlite3, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from desk_skip_check import load_coin

ROOT = Path(__file__).resolve().parents[2]
LAT, COST, HS = 1.5, 0.025, (60, 300)


def main(since="2026-10-22T00:00:00"):
    d = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "memecoin_desk.db"), uri=True)
    rows = d.execute("select ts, mint, network_id, cohort_id from desk_tickets where stage='vet' and verdict='ENTER' and ts>=? order by ts", (since,)).fetchall()
    out = []
    dbs, spans = {}, {}
    for ts, mint, net, cohort in rows:
        if net != "solana":
            continue
        t = datetime.fromisoformat(ts).timestamp()
        day = datetime.fromtimestamp(t, timezone.utc).date()
        coin, end = None, None
        for dd in (day, day - timedelta(days=1)):
            p = ROOT / "data" / "desk" / "swaps" / ("%s.db" % dd)
            if not p.exists():
                continue
            if dd not in dbs:
                dbs[dd] = sqlite3.connect("file:%s?mode=ro" % p, uri=True, isolation_level=None)
                spans[dd] = E.silence_intervals(dbs[dd])
            c = load_coin(dbs[dd], mint)
            if c.t:
                coin = c if coin is None or c.t[0] < coin.t[0] else coin
        if coin is None or coin.t[0] > t:
            out.append(("no_tape", ts, mint, cohort, None)); continue
        # tape end of the day db that holds the verdict time
        sil, span = spans.get(day) or spans[day - timedelta(days=1)]
        c = coin
        i = bisect.bisect_right(c.t, t) - 1
        age = t - c.t[0]
        res = {"age": age, "mcap": E.mcap_usd(c, i), "std": bool(c.first_ok and c.quote_ok)}
        j = bisect.bisect_right(c.t, t + LAT) - 1
        for h in HS:
            k = bisect.bisect_right(c.t, t + h)
            comp = any(c.rt[x] is not None and c.rt[x] <= 0 for x in range(i + 1, k))
            part = span[1] < t + h + 2 or E.overlaps(sil, t, t + h + 2)
            ps = [E.price(c, x) / E.price(c, i) for x in range(i + 1, k)]
            r = None
            if not part and c.fee[j] is not None and not comp:
                qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
                e = bisect.bisect_right(c.t, t + LAT + h) - 1
                if e >= j and c.fee[e] is not None:
                    r = E.value(c, e, tok, qn) - 1 - COST
            res[h] = {"comp": comp, "part": part, "up": max(ps) - 1 if ps else None, "dn": min(ps) - 1 if ps else None, "R": r}
        out.append(("ok", ts, mint, cohort, res))
    ok = [o for o in out if o[0] == "ok"]
    print("vet ENTER verdicts (solana) %d, with tape %d, no tape %d" % (len(out), len(ok), len(out) - len(ok)))
    print("%-10s %4s %-34s %5s %6s | %s" % ("time", "age", "mint", "mcap", "std", "60s: comp up dn R | 300s: comp up dn R"))
    for _, ts, m, co, r in ok:
        f = lambda x: "-" if x is None else "%+.2f" % x
        print("%s %5.0f %-34s %6.0f %3s | %s %s %s %s | %s %s %s %s" % (ts[5:19], r["age"], m[:34], r["mcap"], "y" if r["std"] else "n",
              "C" if r[60]["comp"] else ".", f(r[60]["up"]), f(r[60]["dn"]), f(r[60]["R"]), "C" if r[300]["comp"] else ".", f(r[300]["up"]), f(r[300]["dn"]), f(r[300]["R"])))
    print()
    for lab, lo, hi in (("age < 60 s", 0, 60), ("60 <= age < 120 s", 60, 120), ("age >= 120 s", 120, 1e9)):
        g = [r for _, _, _, _, r in ok if lo <= r["age"] < hi]
        for h in HS:
            R = [x[h]["R"] for x in g if x[h]["R"] is not None]
            comp = sum(x[h]["comp"] for x in g)
            print("%-18s h=%3ds n %3d | valued %3d mean R %s median %s R>0 %s | graduated in window %d (%.0f%%) | not valued: completion %d, partial %d" % (
                lab, h, len(g), len(R), "%+.3f" % np.mean(R) if R else "-", "%+.3f" % np.median(R) if R else "-", "%.0f%%" % (100 * np.mean([x > 0 for x in R])) if R else "-",
                comp, 100 * comp / max(len(g), 1), comp, sum(x[h]["part"] for x in g)))
    crash_table(ok)


def crash_table(ok):
    print()
    for lab, lo, hi in (("age < 60 s", 0, 60), ("60 <= age < 120 s", 60, 120), ("age < 120 s", 0, 120), ("age >= 120 s", 120, 1e9)):
        g = [r for _, _, _, _, r in ok if lo <= r["age"] < hi]
        c50 = sum(1 for x in g if x[60]["dn"] is not None and x[60]["dn"] <= -0.5)
        c75 = sum(1 for x in g if x[60]["dn"] is not None and x[60]["dn"] <= -0.75)
        up25 = sum(1 for x in g if x[300]["up"] is not None and x[300]["up"] >= 0.25)
        print("%-18s n %3d | min price within 60 s <= -50%%: %d (%.0f%%), <= -75%%: %d (%.0f%%) | max up within 300 s >= +25%%: %d (%.0f%%) | completes within 300 s: %d (%.0f%%)" % (
            lab, len(g), c50, 100 * c50 / max(len(g), 1), c75, 100 * c75 / max(len(g), 1), up25, 100 * up25 / max(len(g), 1),
            sum(x[300]["comp"] for x in g), 100 * sum(x[300]["comp"] for x in g) / max(len(g), 1)))


if __name__ == "__main__":
    main(*sys.argv[1:])
