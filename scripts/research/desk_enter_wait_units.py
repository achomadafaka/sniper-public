#!/usr/bin/env python3
"""ENTER-NOW vs WAIT units (pre-reg docs/audits/2026-10-23-enter-now-vs-wait-table-preregistration.md). usage: DAY OUT.jsonl
One unit per coin-second sampled every 30 s (age 60..1800 s, mcap 3.96k..100k, curve incomplete): state variables, R_now, R_wait (px_hold shape), miss flag."""
import bisect, json, math, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
import desk_pullback_units as PB

STEP, FIRST, MAXAGE = 30.0, 60.0, 1800.0
BAND = (3960.0, 100000.0)
INIT_REAL = 7.931e14
HORIZON = PB.WAIT + PB.LAT + 900 + 5


def rngi(c, lo, hi):
    return range(bisect.bisect_right(c.t, lo), bisect.bisect_right(c.t, hi))


def state(c, i, t):
    """trailing-only state variables as of the sample second t (state index i)."""
    p = E.price(c, i)
    ks = list(rngi(c, t - 300, t))
    if len(ks) < 2:
        return None
    ps = [E.price(c, k) for k in ks]
    hi_k = max(ks, key=lambda k: E.price(c, k))
    hi, lo = max(ps), min(ps)
    pos300 = (p - lo) / (hi - lo) if hi > lo else None
    b = sum(c.qa[k] for k in rngi(c, t - 60, t) if c.buy[k]) / 1e9
    s = sum(c.qa[k] for k in rngi(c, t - 60, t) if not c.buy[k]) / 1e9
    flow60 = b / (b + s) if b + s > 0 else None
    t_hi = c.t[hi_k]
    # base_s: longest span before the 300 s high within which ln(high/low) <= 0.15 (>= 10 swaps), capped 1800 s
    base_s, last_ok = 0.0, None
    for x in range(60, 1801, 60):
        a = bisect.bisect_right(c.t, t_hi - x)
        e = bisect.bisect_right(c.t, t_hi - 1e-9)
        if e - a < 10:
            continue
        w = [E.price(c, k) for k in range(a, e)]
        if math.log(max(w) / min(w)) <= 0.15:
            base_s = float(x)
        else:
            break
    vol60 = b + s
    bvol = None
    if base_s >= 120:
        a, e = bisect.bisect_right(c.t, t_hi - base_s), bisect.bisect_right(c.t, t_hi - 1e-9)
        bvol = (sum(c.qa[k] for k in range(a, e)) / 1e9) / (base_s / 60.0)
    return {"pos300": pos300, "flow60": flow60, "breakout_age": t - t_hi, "base_s": base_s,
            "breakout_vol": (vol60 / bvol) if bvol else None,
            "curve_fill": 1.0 - c.rt[i] / INIT_REAL if c.rt[i] is not None else None}


def units(c, silent, span, stats):
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return
    t = c.t[0] + FIRST
    while t - c.t[0] <= MAXAGE and t <= c.t[-1]:
        tc = t
        t += STEP
        i = bisect.bisect_right(c.t, tc) - 1
        if i + 1 < 8:
            continue
        m = E.mcap_usd(c, i)
        if (c.rt[i] is not None and c.rt[i] <= 0) or m > BAND[1]:
            break
        if m < BAND[0]:
            continue
        if tc + HORIZON > span[1] or tc - 400 < span[0] or E.overlaps(silent, tc - 300, tc + HORIZON):
            stats["censored_tape"] += 1
            continue
        noise = PB.range5(c, tc)
        if noise is None or noise <= 0:
            stats["noise_unknown"] += 1
            continue
        st = state(c, i, tc)
        if st is None:
            continue
        now = (PB.fill_r(c, tc, False, 300), PB.fill_r(c, tc, True, 300))
        if now[0] is None or now[1] is None:
            stats["now_censored"] += 1
            continue
        T = PB.trigger(c, i, tc, noise, "n", 2, 5, 1)
        if T is None:
            wait = None
        else:
            wait = (PB.fill_r(c, T, False, 300), PB.fill_r(c, T, True, 300))
            if wait[0] is None or wait[1] is None:
                stats["wait_censored"] += 1
                continue
        yield {"mint": c.mint, "t": tc, "age": tc - c.t[0], "mcap": m, "noise": noise, **st, "now": now,
               "wait": wait, "wait_after_s": None if T is None else T - tc}


def run(day, out):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats, n, mint, coin = defaultdict(int), 0, None, None
    lim = int(__import__("os").environ.get("TR_LIMIT", "0"))
    with open(out, "w") as fh:
        def flush(cn):
            nonlocal n
            if cn is None or len(cn.t) < 8:
                return
            stats["coins"] += 1
            for row in units(cn, silent, span, stats):
                fh.write(json.dumps(row) + "\n"); n += 1
        cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                         "virtual_token, real_token, fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
                if lim and stats["coins"] >= lim:
                    coin = None
                    break
            coin.add(r)
        else:
            flush(coin)
    print(json.dumps({"day": day, "units": n, **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
