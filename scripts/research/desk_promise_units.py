#!/usr/bin/env python3
"""Promise-score units (pre-reg docs/audits/2026-10-23-promise-score-preregistration.md + amendments 9, 10).
One row per (coin, level) first upward crossing: 10 trailing features at t0, R300 at delta 5 s (optimistic / adverse).
usage: DAY OUT.jsonl"""
import bisect, json, math, sqlite3, statistics, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

LEVELS = (5000, 7500, 10000, 12500, 15000, 17500, 20000, 25000, 30000, 35000, 40000)
DELAY, N = 5.0, 300.0


def px(c, s):
    k = bisect.bisect_right(c.t, s) - 1
    return E.price(c, k) if k >= 0 else None


def fill_r(c, i, adverse):
    tf = c.t[i] + DELAY
    j = bisect.bisect_right(c.t, tf) - 1
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


def feats(c, i):
    t = c.t[i]
    p = E.price(c, i)
    if t - 300 < c.t[0]:
        return None                                              # younger than 300 s: ret_300 / range_300 undefined
    a300 = bisect.bisect_right(c.t, t - 300)
    ps = [E.price(c, k) for k in range(a300, i + 1)]
    p60, p300 = px(c, t - 60), px(c, t - 300)
    if not p60 or not p300:
        return None
    lo, hi = min(ps), max(ps)
    consol = 0.0
    kk = i
    hh, ll = p, p
    while kk >= 0 and t - c.t[kk] <= 1800:
        hh, ll = max(hh, E.price(c, kk)), min(ll, E.price(c, kk))
        if hh / ll - 1 > 0.15:
            break
        consol = t - c.t[kk]
        kk -= 1
    def vol(lo_t, hi_t):
        a, b = bisect.bisect_right(c.t, lo_t), bisect.bisect_right(c.t, hi_t)
        return sum(c.qa[k] for k in range(a, b)) / 1e9
    mins = [vol(t - 900 + 60 * n, t - 900 + 60 * (n + 1)) for n in range(14)]
    med = statistics.median(mins)
    a60 = bisect.bisect_right(c.t, t - 60)
    buy = sum(c.qa[k] for k in range(a60, i + 1) if c.buy[k]) / 1e9
    sell = sum(c.qa[k] for k in range(a60, i + 1) if not c.buy[k]) / 1e9
    b_now = len({c.actor[k] for k in range(a60, i + 1) if c.buy[k]})
    a120 = bisect.bisect_right(c.t, t - 120)
    b_prev = len({c.actor[k] for k in range(a120, a60) if c.buy[k]})
    net = defaultdict(float)
    for k in range(i + 1):
        net[c.actor[k]] += c.ta[k] if c.buy[k] else -c.ta[k]
    return {"ret_60": math.log(p / p60), "ret_300": math.log(p / p300), "range_300": hi / lo - 1, "consol_s": consol,
            "vol_exp": (vol(t - 60, t) / med) if med > 0 else None,
            "flow_imb_60": ((buy - sell) / (buy + sell)) if buy + sell > 0 else None,
            "buyers_60": b_now, "buyers_accel": b_now - b_prev,
            "holders_proxy": sum(1 for v in net.values() if v > 0), "age_s": t - c.t[0]}


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
            t0 = c.t[i]
            if t0 + DELAY + N + 5 > span[1] or t0 - 900 < span[0]:
                stats["day_edge"] += 1
                continue
            if E.overlaps(silent, t0 - 900, t0 + DELAY + N + 5):
                stats["tape_silence"] += 1
                continue
            r0, r1 = fill_r(c, i, False), fill_r(c, i, True)
            if r0 is None or r1 is None:
                stats["fee_or_completed"] += 1
                continue
            f = feats(c, i)
            yield {"mint": c.mint, "level": lv, "t0": t0, "r300": r0, "r300_adv": r1, "f": f}


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
