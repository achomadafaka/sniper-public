#!/usr/bin/env python3
"""Young-coin units (pre-reg docs/audits/2026-10-23-young-coin-score-preregistration.md). usage: DAY OUT.jsonl"""
import bisect, json, math, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E

LEVELS = (5000, 7500, 10000, 12500, 15000, 17500, 20000, 25000, 30000, 35000, 40000)
N = 300.0


def fill_r(c, i, delay, adverse):
    tf = c.t[i] + delay
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
    t, age = c.t[i], c.t[i] - c.t[0]
    idx = range(0, i + 1)
    buy_sol = sum(c.qa[k] for k in idx if c.buy[k]) / 1e9
    sell_sol = sum(c.qa[k] for k in idx if not c.buy[k]) / 1e9
    wallets, buyers = set(), set()
    net = defaultdict(float)
    for k in idx:
        wallets.add(c.actor[k])
        if c.buy[k]:
            buyers.add(c.actor[k])
        net[c.actor[k]] += (c.qa[k] if c.buy[k] else -c.qa[k]) / 1e9
    a10 = bisect.bisect_right(c.t, t - 10)
    b10 = sum(c.qa[k] for k in range(a10, i + 1) if c.buy[k]) / 1e9
    s10 = sum(c.qa[k] for k in range(a10, i + 1) if not c.buy[k]) / 1e9
    first = c.actor[0]
    bought = sum(c.ta[k] for k in idx if c.actor[k] == first and c.buy[k])
    sold = sum(c.ta[k] for k in idx if c.actor[k] == first and not c.buy[k])
    p, p0 = E.price(c, i), E.price(c, 0)
    k10 = bisect.bisect_right(c.t, t - 10) - 1
    p10 = E.price(c, k10) if k10 >= 0 else p0
    win = [E.price(c, k) for k in range(a10, i + 1)] or [p]
    return {"age_s": age, "ascent": math.log(p / p0) / age, "buyers_per_s": len(buyers) / age, "swaps_per_wallet": (i + 1) / len(wallets),
            "buy_share": buy_sol / (buy_sol + sell_sol) if buy_sol + sell_sol > 0 else None,
            "flow_imb_10": (b10 - s10) / (b10 + s10) if b10 + s10 > 0 else None,
            "top_buyer_share": max(net.values()) / buy_sol if buy_sol > 0 else None,
            "creator_sold": (sold / bought) if bought > 0 else None,
            "ret_10": math.log(p / p10), "range_10": math.log(max(win) / min(win))}


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
            age = t0 - c.t[0]
            if age >= 300:
                stats["not_young"] += 1
                continue
            if age < 5 or i + 1 < 8:
                stats["too_thin"] += 1
                continue
            if t0 + 5 + N + 5 > span[1] or t0 - 300 < span[0]:
                stats["day_edge"] += 1
                continue
            if E.overlaps(silent, t0 - 300, t0 + 5 + N + 5):
                stats["tape_silence"] += 1
                continue
            r = {d: (fill_r(c, i, d, False), fill_r(c, i, d, True)) for d in (5.0, 1.5)}
            if any(v is None for pair in r.values() for v in pair):
                stats["fee_or_completed"] += 1
                continue
            yield {"mint": c.mint, "level": lv, "t0": t0, "r5": r[5.0][0], "r5_adv": r[5.0][1], "r15": r[1.5][0], "r15_adv": r[1.5][1], "f": feats(c, i)}


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
