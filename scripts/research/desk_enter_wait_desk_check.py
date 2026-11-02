#!/usr/bin/env python3
"""Desk ENTER check of NOW vs WAIT (px_hold shape): for each vet ENTER verdict (10-22/23), modelled NOW vs WAIT from tape at the response time. usage: OUT.json"""
import json, sqlite3, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
import desk_pullback_units as PB
from desk_drift_calibration import load_coin, ts, ROOT


def main(out):
    D = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data/memecoin_desk.db"), uri=True)
    T = sqlite3.connect("file:%s?mode=ro" % (ROOT / "sniper_tracker.db"), uri=True)
    fills = {r[0]: r[1] for r in D.execute("SELECT ticket_id, position_id FROM desk_tickets WHERE stage='fill' AND position_id IS NOT NULL")}
    rows = []
    for tid, mint, resp in D.execute("SELECT ticket_id, mint, response_at FROM desk_tickets WHERE stage='vet' AND verdict='ENTER' AND response_at IS NOT NULL ORDER BY id").fetchall():
        t = ts(resp)
        c = load_coin(mint, [resp[:10]])
        if c is None or not (c.first_ok and c.quote_ok):
            rows.append({"ticket": tid, "censored": "no_tape_or_unverified"}); continue
        i = PB.bisect.bisect_right(c.t, t) - 1
        noise = PB.range5(c, t)
        now = PB.fill_r(c, t, True, 300)
        if now is None or noise is None or noise <= 0:
            rows.append({"ticket": tid, "censored": "now_or_noise_unknown", "noise": noise}); continue
        Tg = PB.trigger(c, i, t, noise, "n", 2, 5, 1)
        wait = None if Tg is None else PB.fill_r(c, Tg, False, 300)
        pid = fills.get(tid)
        pnl = None
        if pid:
            r = T.execute("SELECT realized_pnl, entry_notional FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
            pnl = None if r is None or r[0] is None else r[0] / (r[1] or 25.0)
        rows.append({"ticket": tid, "mint": mint, "t": t, "noise": noise, "now": now, "wait": wait, "wait_after_s": None if Tg is None else Tg - t, "actual_R": pnl})
    json.dump(rows, open(out, "w"))
    ok = [r for r in rows if "now" in r and (r["wait"] is not None or True)]
    tr = [r for r in ok if r["wait"] is not None]
    n = len(ok)
    print({"enters": len(rows), "scored": n, "triggered": len(tr), "miss": n - len(tr),
           "R_now": sum(r["now"] for r in ok) / n, "R_wait_miss0": sum((r["wait"] or 0) for r in ok) / n,
           "trig_R_now": sum(r["now"] for r in tr) / max(1, len(tr)), "trig_R_wait": sum(r["wait"] for r in tr) / max(1, len(tr)),
           "actual_R_mean": (lambda a: sum(a) / len(a) if a else None)([r["actual_R"] for r in ok if r.get("actual_R") is not None])})


if __name__ == "__main__":
    main(sys.argv[1])
