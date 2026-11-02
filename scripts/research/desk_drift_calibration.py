#!/usr/bin/env python3
"""Verdict-to-quote drift vs true-range noise for desk ENTER verdicts (Gamma 2026-10-23, calibration for the fill-drift gate K).
Drift = tape SPOT at the quote/fill time over tape SPOT at the sheet as-of (equals the gate's drift = quote move - fee - impact; v1 of this script subtracted fee+impact twice).
Noise = median 5 s true range pct over the 300 s before the verdict (range vs previous priced close; Lambda 93a830b00 definition).
usage: OUT.json   (read-only; desk db, tracker db, swaps tape)"""
import bisect, json, sqlite3, statistics, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

ROOT = Path(__file__).resolve().parents[2]
FEE_PCT, IMPACT_PCT = 1.25, 0.5
SWAPQ = ("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, "
         "fee_bps, creator_fee_bps, %s FROM swaps WHERE mint=? ORDER BY slot, %s, received_at, ordinal")


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def load_coin(mint, days):
    for day in days:
        db = sqlite3.connect("file:%s?mode=ro" % str(ROOT / "data/desk/swaps" / ("%s.db" % day)), uri=True)
        cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
        txi = "tx_index" if "tx_index" in cols else "slot"
        qm = "quote_mint" if "quote_mint" in cols else "NULL"
        c = E.Coin(mint)
        for r in db.execute(SWAPQ % (qm, txi), (mint,)):
            c.add(r)
        if len(c.t) >= 5:
            return c
    return None


def px(c, s):
    k = bisect.bisect_right(c.t, s) - 1
    return E.price(c, k) if k >= 0 else None


def true_range_noise_pct(c, t):
    prev, trs = None, []
    for n in range(60):
        lo_t, hi_t = t - 300 + 5 * n, t - 300 + 5 * (n + 1)
        a, b = bisect.bisect_right(c.t, lo_t), bisect.bisect_right(c.t, hi_t)
        if b > a:
            ps = [E.price(c, k) for k in range(a, b)]
            hi, lo = max(ps), min(ps)
            if prev is not None:
                hi, lo = max(hi, prev), min(lo, prev)
            trs.append((hi / lo - 1) * 100)
            prev = E.price(c, b - 1)
    return statistics.median(trs) if len(trs) >= 6 else None


def main(out):
    D = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data/memecoin_desk.db"), uri=True)
    T = sqlite3.connect("file:%s?mode=ro" % (ROOT / "sniper_tracker.db"), uri=True)
    vets = D.execute("SELECT ticket_id, mint, response_at, json_extract(inputs_json,'$.asof.meta.as_of') FROM desk_tickets WHERE stage='vet' AND verdict='ENTER' AND response_at IS NOT NULL ORDER BY id").fetchall()
    fills = {r[0]: r[1] for r in D.execute("SELECT ticket_id, position_id FROM desk_tickets WHERE stage='fill' AND position_id IS NOT NULL")}
    rows = []
    for tid, mint, resp, asof in vets:
        t = float(asof) if asof is not None else ts(resp)       # the sheet's as-of time = the verdict price time
        day = resp[:10]
        c = load_coin(mint, [day])
        if c is None:
            rows.append({"ticket": tid, "censored": "no_tape"}); continue
        p0, noise = px(c, t), true_range_noise_pct(c, t)
        row = {"ticket": tid, "mint": mint, "t": t, "noise_pct": noise}
        pid = fills.get(tid)
        if pid:
            r = T.execute("SELECT quote_observed_at, opened_at, realized_pnl, entry_notional, exit_reason, closed_at FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
            tq = ts(r[0]) if r[0] else ts(r[1])
            row.update(pid=pid, delay_s=tq - t, realized_pnl=r[2], notional=r[3], exit_reason=r[4])
            p1 = px(c, tq)
        else:
            p1 = px(c, ts(resp) + 5.7)                # unfilled: response + typical quote delay
            row["delay_s"] = ts(resp) + 5.7 - t
        if p0 and p1:
            row["drift_pct"] = (p1 / p0 - 1) * 100          # spot to spot: the gate nets fee+impact off the QUOTE move, which equals this
            row["raw_move_pct"] = (p1 / p0 - 1) * 100
        rows.append(row)
    json.dump(rows, open(out, "w"))
    ok = [r for r in rows if r.get("drift_pct") is not None and r.get("noise_pct")]
    print({"vet_enters": len(vets), "scored": len(ok), "filled": sum(1 for r in rows if r.get("pid"))})


if __name__ == "__main__":
    main(sys.argv[1])
