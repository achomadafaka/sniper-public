#!/usr/bin/env python3
"""Pump-curve progress stages by USD market cap (operator idea 2026-10-26; descriptive first). usage: DAY OUT.jsonl
Per curve-start coin: stages S1 4k->10k, S2 10k->20k, S3 20k->30k, S4 30k->curve completion (~42.8k; beyond it is PumpSwap, not on this tape).
Stage entered at the first swap whose post-swap mcap >= L. Outcome within H = 1800 s of that crossing: U (mcap >= upper) / D (mcap <= 0.7 L) / N (neither, fully observed) / C (censored:
silence, tape end). Entries inside the stage (one per coin per type): E1 at the crossing, E2 first -10% pullback from the running stage high while mcap >= L, E3 first re-cross of L after
a dip below L. Exits: X1 scalp TP +20% / SL -10% / 300 s; X2 stage exit TP when mcap >= upper, SL -15% / 900 s. Exact curve buy of 25 USD at +1.5 s, exact sell, fees, 2.5 pp.
High-start coin = mcap >= 8,000 within the first 5 s of life (fixed before any outcome was read; launch mcap is ~2.9k)."""
import bisect, json, os, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from tape_reader_samples import features, first_silence_after

ROOT = Path(__file__).resolve().parents[2]
STAGES = ((1, 4000.0, 10000.0), (2, 10000.0, 20000.0), (3, 20000.0, 30000.0), (4, 30000.0, 42800.0))
H, LAT, COST, HI_START_MCAP, DIE = 1800.0, 1.5, 0.025, 8000.0, 0.7


def done(c, k):
    return c.rt[k] is not None and c.rt[k] <= 0


def sim(c, m, te, mode, upper, silent, span):
    """exact clip buy at te+LAT, exit by mode; returns (R|None, reason)"""
    j = bisect.bisect_right(c.t, te + LAT) - 1
    if j < 0 or c.fee[j] is None or done(c, j):
        return None, "entry_unavailable"
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j]); tok = c.b[j] * qn / (c.q[j] + qn)
    tf = te + LAT
    hold, tp, sl = (300.0, 0.20, 0.10) if mode == "x1" else (900.0, None, 0.15)
    if tf + hold + 2 > span[1] or E.overlaps(silent, tf, tf + hold + 2):
        return None, "censored_tape"
    trig, why = tf + hold, "time"
    for k in range(j + 1, bisect.bisect_right(c.t, tf + hold)):
        if c.fee[k] is None:
            return None, "fee_missing"
        if done(c, k):
            return None, "completed"
        v = E.value(c, k, tok, qn)
        if (tp is not None and v >= 1 + tp) or (mode == "x2" and m[k] >= upper):
            trig, why = c.t[k], "tp"; break
        if v <= 1 - sl:
            trig, why = c.t[k], "sl"; break
    e = bisect.bisect_right(c.t, trig + LAT) - 1
    if e < j or c.fee[e] is None or done(c, e):
        return None, "exit_unavailable"
    return E.value(c, e, tok, qn) - 1.0 - COST, why


def coin_rows(c, silent, span, day, stats):
    if not (c.first_ok and c.quote_ok) or len(c.t) < 8:
        stats["skipped_nonstandard"] += 1
        return []
    m = [E.mcap_usd(c, k) for k in range(len(c.t))]
    t0 = c.t[0]
    hi = max([m[k] for k in range(len(c.t)) if c.t[k] <= t0 + 5.0] or [0]) >= HI_START_MCAP
    out = [{"kind": "coin", "day": day, "mint": c.mint, "hi_start": hi, "max_mcap": max(m),
            "reached": [any(x >= L for x in m) for _, L, _ in STAGES]}]
    for sid, L, U in STAGES:
        kx = next((k for k in range(len(m)) if m[k] >= L), None)
        if kx is None:
            continue
        tc = c.t[kx]
        row = {"kind": "stage", "day": day, "mint": c.mint, "stage": sid, "hi_start": hi, "tcross": tc, "age": tc - t0, "mcap_cross": m[kx]}
        fs = first_silence_after(silent, tc)
        stop = fs if fs is not None and fs < tc + H + 2 else None
        if tc + H + 2 > span[1]:
            stop = span[1] if stop is None else min(stop, span[1])
        tt = why = None
        end = bisect.bisect_right(c.t, tc + H)
        for k in range(kx + 1, end):
            if m[k] >= U or done(c, k):
                tt, why = c.t[k], "U"; break
            if m[k] <= DIE * L:
                tt, why = c.t[k], "D"; break
        if stop is not None and (tt is None or tt > stop):
            row["out"], row["t_out"] = "C", None
        else:
            row["out"], row["t_out"] = (why, tt - tc) if why else ("N", None)
        try:
            row["feat"] = features(c, kx, tc)
        except Exception:
            row["feat"] = None
        # entries, only while the stage is still open (no touch of U or die yet)
        lim = bisect.bisect_right(c.t, tt if tt is not None else tc + H)
        T = t0 + 5.0                      # the high-start flag uses data up to t0+5 s: no entry may precede it
        hi_p, e2, dipped, e3 = E.price(c, kx), None, False, None
        for k in range(kx + 1, lim):
            p = E.price(c, k)
            if e2 is None and m[k] >= L and p <= 0.90 * hi_p and c.t[k] >= T:
                e2 = c.t[k]
            hi_p = max(hi_p, p)
            if not dipped and m[k] < L:
                dipped = True
            elif dipped and e3 is None and L <= m[k] < U and c.t[k] >= T:
                e3 = c.t[k]
        e1 = max(tc, T)
        if tt is not None and e1 >= tt:   # stage already resolved (U or die) before the first allowed entry
            e1 = None
        for name, te in (("e1", e1), ("e2", e2), ("e3", e3)):
            for mode in ("x1", "x2"):
                if te is None:
                    row[name + mode] = None; row[name + mode + "_why"] = "no_entry"
                else:
                    r, w = sim(c, m, te, mode, U, silent, span)
                    row[name + mode], row[name + mode + "_why"] = (None if r is None else round(r, 4)), w
        out.append(row)
    return out


def run(day, out_path):
    base = ROOT / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats = defaultdict(int)
    cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, "
                     "fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
    mint, coin = None, None
    with open(out_path, "w") as fh:
        def flush(cn):
            if cn is None:
                return
            stats["coins"] += 1
            for r in coin_rows(cn, silent, span, day, stats):
                fh.write(json.dumps(r) + "\n")
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
                if os.environ.get("TR_LIMIT") and stats["coins"] >= int(os.environ["TR_LIMIT"]):
                    coin = None; break
            coin.add(r)
        else:
            flush(coin)
    print(json.dumps({"day": day, **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
