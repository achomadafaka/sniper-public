#!/usr/bin/env python3
"""Qualification units (pre-reg docs/audits/2026-10-23-qualification-set-preregistration.md). usage: DAY OUT.jsonl"""
import bisect, json, math, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from desk_young_units import fill_r, LEVELS, N


def feats(c, i):
    t, age = c.t[i], c.t[i] - c.t[0]
    W = min(age, 300.0)
    a = bisect.bisect_right(c.t, t - W) if age > 300 else 0
    idx = range(a, i + 1)
    buys = [k for k in idx if c.buy[k]]
    buy_sol = sum(c.qa[k] for k in buys) / 1e9
    sell_sol = sum(c.qa[k] for k in idx if not c.buy[k]) / 1e9
    gross = buy_sol + sell_sol
    buyers = {c.actor[k] for k in buys}
    net = defaultdict(float)
    for k in idx:
        net[c.actor[k]] += (c.qa[k] if c.buy[k] else -c.qa[k]) / 1e9
    clip = E.CLIP_SOL
    sizes = [c.qa[k] / 1e9 for k in buys]
    growth = None
    if age >= 120:
        a60, a120 = bisect.bisect_right(c.t, t - 60), bisect.bisect_right(c.t, t - 120)
        last = len({c.actor[k] for k in range(a60, i + 1) if c.buy[k]})
        prev = len({c.actor[k] for k in range(a120, a60) if c.buy[k]})
        growth = (last / prev) if prev > 0 else (float("inf") if last > 0 else None)
    first = c.actor[0]
    bought = sum(c.ta[k] for k in range(0, i + 1) if c.actor[k] == first and c.buy[k])
    sold = sum(c.ta[k] for k in range(0, i + 1) if c.actor[k] == first and not c.buy[k])
    a60 = bisect.bisect_right(c.t, t - 60)
    p = E.price(c, i)
    hi = max(E.price(c, k) for k in idx)
    k60 = bisect.bisect_right(c.t, t - 60) - 1
    p60 = E.price(c, k60) if k60 >= 0 else E.price(c, 0)
    f = c.fee[i] or 0.0125
    qn = E.CLIP_SOL * 1e9 * (1 - f)
    T = c.b[i] * qn / (c.q[i] + qn)
    return {"age": age, "buyers": len(buyers), "buyers_growth": growth, "net_buy": (buy_sol / gross) if gross > 0 else None,
            "dust": (sum(1 for s in sizes if s <= 0.25 * clip) / len(sizes)) if sizes else None,
            "diffuse": (max(net.values()) / buy_sol) if buy_sol > 0 else None,
            "creator_sold": (sold / bought) if bought > 0 else None, "active": i + 1 - a60 if a60 <= i else 0,
            "hi_ratio": p / hi, "ext": math.log(p / p60), "exit_depth": T / (c.b[i] + T)}


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
            if t0 - c.t[0] < 10 or i + 1 < 8:
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
            yield {"mint": c.mint, "level": lv, "t0": t0, "r5": r[5.0][0], "r5_adv": r[5.0][1], "r15": r[1.5][0], "f": feats(c, i)}


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
