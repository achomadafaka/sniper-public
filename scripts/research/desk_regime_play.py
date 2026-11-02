#!/usr/bin/env python3
"""Regime + play extractor (pre-reg docs/audits/2026-10-23-regime-play-preregistration.md).
Writes one JSON line per regime UNIT (episode second grid time) with every play's net R, plus a grid-level regime log for
frequency/duration. usage: DAY OUT_UNITS.jsonl OUT_GRID.jsonl"""
import bisect, json, math, sqlite3, statistics, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
import desk_pullback_units as PB

LAT = 1.5
GRID_S = 30.0
MCAP_LO, MCAP_HI = 5000.0, 40000.0
HORIZON = 300.0


def price_at(c, s):
    k = bisect.bisect_right(c.t, s) - 1
    return E.price(c, k) if k >= 0 else None


def states(c, lo, hi):
    return range(bisect.bisect_right(c.t, lo), bisect.bisect_right(c.t, hi))


def flows(c, lo, hi):
    b = sum(c.qa[k] for k in states(c, lo, hi) if c.buy[k]) / 1e9
    s = sum(c.qa[k] for k in states(c, lo, hi) if not c.buy[k]) / 1e9
    return b, s


def zone_touches(c, lo_t, hi_t, level, below, gap=15.0):
    n, last = 0, -1e18
    for k in states(c, lo_t, hi_t):
        p = E.price(c, k)
        inside = (p <= level) if below else (p >= level)
        if inside and c.t[k] - last >= gap:
            n += 1
        if inside:
            last = c.t[k]
    return n


def regime(c, t):
    """(regime, noise) at grid time t; ('UNKNOWN', None) when an input is unknown."""
    n = PB.range5(c, t)
    if n is None:
        return "UNKNOWN", None
    if n <= 0:
        return "QUIET", None                      # median 5 s range is 0: no usable noise scale (dead or flat), kept apart
    s = n * math.sqrt(12)
    p, p60 = price_at(c, t), price_at(c, t - 60)
    if p is None or p60 is None:
        return "UNKNOWN", n
    b60, s60 = flows(c, t - 60, t)
    imb = ((b60 - s60) / (b60 + s60)) if b60 + s60 > 0 else 0.0
    hi900 = max([E.price(c, k) for k in states(c, t - 900, t)] or [p])
    if p <= 0.60 * hi900 and imb < 0:
        return "DYING", n
    if math.log(p / p60) >= 3 * s:
        return "IMPULSE", n
    idx = list(states(c, t - 300, t))
    if len(idx) >= 4:
        ps = [E.price(c, k) for k in idx]
        lo, hi = min(ps), max(ps)
        w = math.log(hi / lo)
        if 4 * n <= w <= 8 * s:
            lowz, highz = lo * (hi / lo) ** 0.2, lo * (hi / lo) ** 0.8
            if zone_touches(c, t - 300, t, lowz, True) >= 2 and zone_touches(c, t - 300, t, highz, False) >= 2:
                bb, ss = flows(c, t - 300, t)
                g = bb + ss
                if g > 0 and bb / g >= 0.30 and ss / g >= 0.30:
                    return "RANGE", n
    hs, ls, prev = [], [], p60
    for blk in range(5):
        lo_t, hi_t = t - 150 + 30 * blk, t - 150 + 30 * (blk + 1)
        pp = [E.price(c, k) for k in states(c, lo_t, hi_t)]
        hs.append(max(pp) if pp else prev); ls.append(min(pp) if pp else prev)
        prev = pp[-1] if pp else prev
    if sum(hs[i + 1] > hs[i] for i in range(4)) >= 3 and sum(ls[i + 1] > ls[i] for i in range(4)) >= 3 and imb > 0:
        return "TREND", n
    prior = [PB.range5(c, t - 150 * m) for m in range(1, 7)]
    prior = [x for x in prior if x is not None]
    vols = [sum(c.qa[k] for k in states(c, t - 60 * m - 60, t - 60 * m)) / 1e9 for m in range(1, 16)]
    if prior and n <= 0.5 * statistics.median(prior) and (b60 + s60) <= 0.5 * statistics.median(vols):
        return "CONSOLIDATION", n
    return "OTHER", n


def play_R(c, trig, adverse, N=HORIZON):
    return PB.fill_r(c, trig, adverse, N) if trig is not None else None


def breakout_trigger(c, t, n):
    h300 = max([E.price(c, k) for k in states(c, t - 300, t)] or [0])
    s = t
    while s < t + 60:
        s += 1.0
        p = price_at(c, s)
        if p is not None and h300 > 0 and p >= h300 * math.exp(n):
            return s
    return None


def dump_trigger(c, t):
    hi900 = max([E.price(c, k) for k in states(c, t - 900, t)] or [0])
    s = t
    while s < t + 120:
        s += 1.0
        idx = list(states(c, s - 900, s))
        if not idx:
            continue
        ps = [E.price(c, k) for k in idx]
        low = min(ps)
        k_low = max(k for k in idx if E.price(c, k) == low)
        p = price_at(c, s)
        b, sl = flows(c, s - 15, s)
        if s - c.t[k_low] >= 15 and p >= 0.5 * hi900 and b >= sl:
            return s
    return None


def scalp(c, t, n, adverse):
    """up to 3 buy-low / sell-high cycles inside 900 s of t; band fixed from the last 300 s."""
    ps = [E.price(c, k) for k in states(c, t - 300, t)]
    lo, hi = min(ps), max(ps)
    lowz, highz, brk = lo * (hi / lo) ** 0.2, lo * (hi / lo) ** 0.8, lo * math.exp(-n)
    out, s, end = [], t, t + 900
    while len(out) < 3 and s < end:
        s += 1.0
        p = price_at(c, s)
        if p is None or p > lowz:
            continue
        r = fill_cycle(c, s, adverse, highz, brk, n)
        if r is None:
            break
        out.append({**r, "width_n": math.log(hi / lo) / n, "width_pct": hi / lo - 1})
        s = r["exit_t"]
    return out


def fill_cycle(c, trig, adverse, highz, brk, n):
    tf = trig + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    if adverse:
        best, e2 = j, bisect.bisect_right(c.slot, c.slot[j] + 2)
        for k in range(j + 1, e2):
            if E.price(c, k) > E.price(c, best):
                best = k
        j = best
    if c.fee[j] is None:
        return None
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    impact = ((c.q[j] + qn) / (c.b[j] - tokens)) / (c.q[j] / c.b[j]) - 1
    s, deadline = tf, tf + HORIZON
    reason = "time"
    while s < deadline:
        s += 1.0
        p = price_at(c, s)
        if p >= highz:
            reason = "target"; break
        if p < brk:
            reason = "break"; break
    xt = s + LAT
    e = bisect.bisect_right(c.t, xt) - 1
    if e < j or c.fee[e] is None or any(x is None for x in c.rt[j:e + 1]) or min(c.rt[j:e + 1]) <= 0:
        return None
    r = E.value(c, e, tokens, qn) - 1.0 - 0.025
    return {"R": r, "exit": reason, "exit_t": xt, "impact": impact}


def unit_rows(c, silent, span, stats, grid_out):
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return
    t = c.t[0] + 300.0
    seq = []
    last_t = c.t[-1]
    while t <= last_t:
        k = bisect.bisect_right(c.t, t) - 1
        if k >= 0 and MCAP_LO <= E.mcap_usd(c, k) <= MCAP_HI and c.rt[k] and c.rt[k] > 0:
            if E.overlaps(silent, t - 900, t + 1000) or t + 1000 > span[1] or t - 900 < span[0]:
                seq.append((t, "UNKNOWN", None))
            else:
                r, n = regime(c, t)
                seq.append((t, r, n))
        else:
            seq.append((t, "OUT", None))
        t += GRID_S
    for a, (tt, rg, n) in enumerate(seq):
        grid_out.append((c.mint, tt, rg))
        if rg in ("OUT", "UNKNOWN", "QUIET") or n is None or a == 0:
            continue
        # unit = SECOND grid time of an episode (regime confirmed by two looks)
        if seq[a - 1][1] != rg or (a >= 2 and seq[a - 2][1] == rg):
            continue
        run = 2
        while a - 1 + run < len(seq) and seq[a - 1 + run][1] == rg:
            run += 1
        if E.overlaps(silent, tt - 300, tt + 1000):
            continue
        now = {}
        for N in (120, 300, 900):
            now[str(N)] = [PB.fill_r(c, tt, False, N), PB.fill_r(c, tt, True, N)]
        if now["300"][0] is None or now["300"][1] is None:
            stats["now_censored"] += 1
            continue
        i = bisect.bisect_right(c.t, tt) - 1
        plays = {"NOW": now}
        trig = PB.trigger(c, i, tt, n, "n", 2, 5, 1)
        plays["PULLBACK"] = None if trig is None else {"T": trig - tt, "R": [play_R(c, trig, False), play_R(c, trig, True)]}
        trig = breakout_trigger(c, tt, n)
        plays["BREAKOUT"] = None if trig is None else {"T": trig - tt, "R": [play_R(c, trig, False), play_R(c, trig, True)]}
        if rg == "DYING":
            trig = dump_trigger(c, tt)
            plays["DUMP_RECOVERY"] = None if trig is None else {"T": trig - tt, "R": [play_R(c, trig, False), play_R(c, trig, True)]}
        if rg == "RANGE":
            plays["SCALP"] = [scalp(c, tt, n, False), scalp(c, tt, n, True)]
        yield {"mint": c.mint, "regime": rg, "t": tt, "run_steps_at_unit": run, "noise": n,
               "level": E.mcap_usd(c, i), "plays": plays}


def run(day, out_units, out_grid):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats, n, mint, coin = defaultdict(int), 0, None, None
    with open(out_units, "w") as fu, open(out_grid, "w") as fg:
        def flush(cn):
            nonlocal n
            if cn is None or len(cn.t) < 20:
                return
            stats["coins"] += 1
            grid = []
            for row in unit_rows(cn, silent, span, stats, grid):
                fu.write(json.dumps(row) + "\n"); n += 1
            for g in grid:
                fg.write(json.dumps(g) + "\n")
        cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                         "virtual_token, real_token, fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
        import os
        lim = int(os.environ.get("REGIME_LIMIT", "0"))
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
                if lim and stats["coins"] >= lim:
                    break
            coin.add(r)
        else:
            flush(coin)
    print(json.dumps({"day": day, "units": n, **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3])
