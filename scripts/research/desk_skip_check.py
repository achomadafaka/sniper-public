#!/usr/bin/env python3
"""Descriptive check of desk skips: what did the coin do on our tape after the decision. usage: SINCE_ISO [DESK_DB]
Per ticket: max up / max down vs the decision price in 5/15/60 min, and net R (exact clip buy at +1.5 s, exact sell at +H, fees, 2.5 pp). Windows beyond the tape end are marked partial."""
import bisect, json, sqlite3, sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

ROOT = Path(__file__).resolve().parents[2]
LAT, COST, HS = 1.5, 0.025, (300, 900, 3600)


def epoch(s):
    return datetime.fromisoformat(s).timestamp()


def load_coin(db, mint):
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    c = E.Coin(mint)
    for r in db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, "
                        "fee_bps, creator_fee_bps, %s FROM swaps WHERE mint=? ORDER BY slot, %s, received_at, ordinal" % (qm, txi), (mint,)):
        c.add(r)
    return c


def analyse(c, t, tape_end, silent):
    if not c.t or c.t[0] > t:
        return {"note": "no_tape_before_decision"}
    i = bisect.bisect_right(c.t, t) - 1
    p0, end = E.price(c, i), tape_end
    out = {"mcap0": round(E.mcap_usd(c, i)), "age_s": round(t - c.t[0]), "done_at_t": bool(c.rt[i] is not None and c.rt[i] <= 0), "tape_end_s": round(end - t), "last_swap_s": round(c.t[-1] - t)}
    j = bisect.bisect_right(c.t, t + LAT) - 1
    for H in HS:
        k = bisect.bisect_right(c.t, t + H)
        ps = [E.price(c, x) / p0 for x in range(i + 1, k)]
        part = end < t + H + 2 or E.overlaps(silent, t, t + H + 2)
        d = {"up": round(max(ps) - 1, 3) if ps else None, "dn": round(min(ps) - 1, 3) if ps else None, "partial": part}
        if not part and c.fee[j] is not None:
            qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
            e = bisect.bisect_right(c.t, t + LAT + H) - 1
            if e >= j and c.fee[e] is not None and not any(c.rt[x] is not None and c.rt[x] <= 0 for x in range(j, e + 1)):
                d["R"] = round(E.value(c, e, tok, qn) - 1 - COST, 3)
        out[H] = d
    return out


def touch_r(c, t, pct, tape_end, hold=300.0):
    """first time the price is <= p0*(1+pct/100) after t; R of an exact clip buy 1.5 s later held `hold` s. (touch_s, R|None, partial)"""
    i = bisect.bisect_right(c.t, t) - 1
    lvl = E.price(c, i) * (1 + pct / 100.0)
    for k in range(i + 1, len(c.t)):
        if E.price(c, k) <= lvl:
            tt = c.t[k]; j = bisect.bisect_right(c.t, tt + LAT) - 1
            if c.fee[j] is None or tt + LAT + hold > tape_end:
                return round(tt - t), None, True
            qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
            e = bisect.bisect_right(c.t, tt + LAT + hold) - 1
            if any(c.rt[x] is not None and c.rt[x] <= 0 for x in range(j, e + 1)):
                return round(tt - t), None, False
            return round(tt - t), round(E.value(c, e, tok, qn) - 1 - COST, 3), False
    return None, None, False


def main(since, dbp=ROOT / "data" / "memecoin_desk.db"):
    d = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True)
    day = since[:10]
    t = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)), uri=True, isolation_level=None)
    rows = d.execute("SELECT ts, stage, verdict, reason, mint, network_id, output_json FROM desk_tickets WHERE ts>=? AND ("
                     "(stage='vet' AND verdict IN ('SKIP','WAIT')) OR (stage='hard_filter' AND verdict='SKIP')) ORDER BY ts", (since,)).fetchall()
    silent, span = E.silence_intervals(t)
    cache = {}
    for ts, stage, verdict, reason, mint, net, oj in rows:
        code = None
        try:
            code = json.loads(oj)["result"].get("reason_code")
        except Exception:
            pass
        lab = "%s:%s%s" % (stage, reason, (":c%s" % code) if code is not None else "")
        if net != "solana":
            print(ts[11:19], lab, mint[:8], "non-solana"); continue
        if mint not in cache:
            cache[mint] = load_coin(t, mint)
        r = analyse(cache[mint], epoch(ts), span[1], silent)
        short = "  ".join("%dm up %s dn %s R %s%s" % (H // 60, r[H]["up"], r[H]["dn"], r[H].get("R", "-"), "*" if r[H]["partial"] else "") for H in HS) if HS[0] in r else r.get("note")
        extra = ""
        if verdict == "WAIT":
            try:
                cond = json.loads(oj)["result"]["condition"]
                ts_, r_, pt_ = touch_r(cache[mint], epoch(ts), cond[2], span[1])
                extra = "  | level %s%% touched %s s, R5 after touch %s%s" % (cond[2], ts_, r_, "*" if pt_ else "")
            except Exception as exc:
                extra = " | touch? %s" % exc
        extra = ""
        if verdict == "WAIT":
            try:
                cond = json.loads(oj)["result"]["condition"]
                ts_, r_, pt_ = touch_r(cache[mint], epoch(ts), cond[2], span[1])
                extra = "  | level %s%% touched after %s s, R5 from touch %s%s" % (cond[2], ts_, r_, "*" if pt_ else "")
            except Exception as exc:
                extra = " | touch? %s" % exc
        print(ts[11:19], lab, mint[:8], "mcap", r.get("mcap0"), "age", r.get("age_s"), "done" if r.get("done_at_t") else "", "|", short + extra)


if __name__ == "__main__":
    main(*sys.argv[1:])
