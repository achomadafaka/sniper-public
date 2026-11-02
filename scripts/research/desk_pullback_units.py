#!/usr/bin/env python3
"""Pullback-hold entry arms after an impulse at a band cross (pre-reg docs/audits/2026-10-23-pullback-trigger-preregistration.md).
usage: DAY OUT.jsonl"""
import bisect, itertools, json, math, sqlite3, statistics, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

LEVELS = (20000, 25000, 30000)
LAT = 1.5
WAIT = 60.0
D_NOISE, D_FIXED, HOLDS, FLOWS = (1, 2, 4), (0.05, 0.10, 0.15), (5, 15, 30), (0, 1)
NS = (120, 300, 900)


def px(c, s):
    k = bisect.bisect_right(c.t, s) - 1
    return E.price(c, k) if k >= 0 else None


def range5(c, tc):
    """median over the trailing 300 s of ln(high/low) per 5 s bucket (quiet = 0); None with < 12 buckets after launch."""
    vals, first = [], c.t[0]
    for n in range(60):
        lo_t, hi_t = tc - 300 + 5 * n, tc - 300 + 5 * (n + 1)
        if hi_t <= first:
            continue
        a, b = bisect.bisect_right(c.t, lo_t), bisect.bisect_right(c.t, hi_t)
        if b > a:
            ps = [E.price(c, k) for k in range(a, b)]
            vals.append(math.log(max(ps) / min(ps)))
        else:
            vals.append(0.0)
    return statistics.median(vals) if len(vals) >= 12 else None


def fill_r(c, trig, adverse, N):
    """net R of an entry triggered at `trig`: fill at trig + LAT, exit at fill + N; None when unknowable."""
    tf = trig + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    if j < 0:
        return None
    if adverse:
        best, end = j, bisect.bisect_right(c.slot, c.slot[j] + 2)
        for k in range(j + 1, end):
            if E.price(c, k) > E.price(c, best):
                best = k
        j = best
    e = bisect.bisect_right(c.t, tf + N) - 1
    if e < j or c.fee[j] is None or c.fee[e] is None or any(x is None for x in c.rt[j:e + 1]) or min(c.rt[j:e + 1]) <= 0:
        return None
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    return E.value(c, e, tokens, qn) - 1.0 - 0.025


def trigger(c, i, tc, noise, kind, d, h, f):
    """first second T in (tc, tc + WAIT] the pullback has held, or None (miss)."""
    P = E.price(c, i)
    L = t_low = None
    k = i + 1
    n = len(c.t)
    s = tc
    while s < tc + WAIT:
        s += 1.0
        while k < n and c.t[k] <= s:                                  # states inside (s-1, s]
            p = E.price(c, k)
            if L is None:
                P = max(P, p)
                retr = math.log(P / p) >= d * noise if kind == "n" else p <= (1 - d) * P
                if retr:
                    L, t_low = p, c.t[k]
            elif p < L:
                L, t_low = p, c.t[k]
            k += 1
        if L is not None and s - t_low >= h:
            if f:
                a, b = bisect.bisect_right(c.t, s - h), bisect.bisect_right(c.t, s)
                net = sum((c.qa[x] if c.buy[x] else -c.qa[x]) for x in range(a, b))
                if net < 0:
                    continue
            return s
    return None


def unit_rows(c, silent, span, stats):
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return
    crossed = set()
    for i in range(len(c.t)):
        m = E.mcap_usd(c, i)
        for lv in LEVELS:
            if lv in crossed or m < lv:
                continue
            crossed.add(lv)
            tc = c.t[i]
            if tc - 60 < c.t[0]:
                stats["too_young"] += 1
                continue
            if tc + WAIT + LAT + 900 + 5 > span[1] or tc - 400 < span[0]:
                stats["day_edge"] += 1
                continue
            p60 = px(c, tc - 60)
            if p60 is None or math.log(E.price(c, i) / p60) < 0.20:
                stats["no_impulse"] += 1
                continue
            if E.overlaps(silent, tc - 300, tc + WAIT + LAT + 900 + 5):
                stats["tape_silence"] += 1
                continue
            noise = range5(c, tc)
            if noise is None or noise <= 0:
                stats["noise_unknown"] += 1
                continue
            now = {N: (fill_r(c, tc, False, N), fill_r(c, tc, True, N)) for N in NS}
            if now[300][0] is None or now[300][1] is None:
                stats["now_censored"] += 1
                continue
            arms = {}
            for kind, dset in (("n", D_NOISE), ("f", D_FIXED)):
                for d, h, f in itertools.product(dset, HOLDS, FLOWS):
                    T = trigger(c, i, tc, noise, kind, d, h, f)
                    key = "%s|%s|%d|%d" % (kind, d, h, f)
                    if T is None:
                        arms[key] = None
                    else:
                        arms[key] = {"T": T - tc, **{str(N): [fill_r(c, T, False, N), fill_r(c, T, True, N)] for N in NS}}
            yield {"mint": c.mint, "level": lv, "tc": tc, "noise": noise, "now": {str(N): list(v) for N, v in now.items()}, "arms": arms}


def run(day, out):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats, n, mint, coin = defaultdict(int), 0, None, None
    with open(out, "w") as fh:
        def flush(cn):
            nonlocal n
            if cn is None or len(cn.t) < 2:
                return
            stats["coins"] += 1
            for row in unit_rows(cn, silent, span, stats):
                fh.write(json.dumps(row) + "\n"); n += 1
        cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                         "virtual_token, real_token, fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal" % (qm, txi))
        for r in cur:
            if r[0] != mint:
                flush(coin); mint, coin = r[0], E.Coin(r[0])
            coin.add(r)
        flush(coin)
    print(json.dumps({"day": day, "units": n, **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
