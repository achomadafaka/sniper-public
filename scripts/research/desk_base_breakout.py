#!/usr/bin/env python3
"""Base-breakout prize read (pre-reg docs/audits/2026-10-23-base-breakout-prize-preregistration.md). usage: DAY OUT.json"""
import bisect, json, math, random, sqlite3, sys, time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
import tape_reader_samples as S

MAXAGE = 7200.0       # breakout search window (age); amendment 1
BAND, BASE_S, BASE_RANGE, BRK, MINSW, LAT = (15000, 25000), 600.0, 0.15, 1.03, 10, 1.5
U, D, H, COST = 0.30, 0.10, 600, 0.025


def window_stats(c, lo, hi, upto):
    a, b = bisect.bisect_right(c.t, lo), min(bisect.bisect_right(c.t, hi), upto)
    if b - a < MINSW:
        return None
    ps = [E.price(c, k) for k in range(a, b)]
    return max(ps), min(ps), b - a


def outcome(c, i, t, silent, span):
    """(hit in {1,0,-1,-2}, net R optimistic or None, net R adverse or None) for an entry decided at t (state index i)."""
    if E.overlaps(silent, t - BASE_S, t) or t - BASE_S < span[0]:
        return -2, None, None
    tf = t + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    if j < 0 or c.fee[j] is None:
        return -2, None, None
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    e2 = bisect.bisect_right(c.t, tf + H)
    if any(c.fee[k] is None for k in range(j, e2)):
        return -2, None, None
    fs = S.first_silence_after(silent, t)
    stop = fs if fs is not None and fs < tf + H + 2 else None
    if tf + H + 2 > span[1]:
        stop = span[1] if stop is None else min(stop, span[1])
    vals = [E.value(c, k, tokens, qn) for k in range(j + 1, e2)]
    hit = S.touch(vals, c.slot[j + 1:e2], 1.0, U, D, times=c.t[j + 1:e2], stop=stop, rts=c.rt[j + 1:e2])
    if hit == -2 or hit == -1:
        return hit, None, None
    r = U if hit == 1 else (-D if hit == 0 and any(v <= 1 - D for v in vals) else (vals[-1] - 1 if vals else 0.0))
    return hit, r - COST, r - COST


def coin_units(c, silent, span, stats, brk, ctl):
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return
    n, done = len(c.t), False
    t0 = c.t[0]
    for i in range(n):
        t = c.t[i]
        if t - t0 < BASE_S:
            continue
        if t - t0 > MAXAGE:
            break
        m = E.mcap_usd(c, i)
        if c.rt[i] is not None and c.rt[i] <= 0:
            break
        if not BAND[0] <= m <= BAND[1] or done:
            continue
        w = window_stats(c, t - BASE_S, t, i)
        if w is None:
            continue
        hi, lo, _ = w
        if math.log(hi / lo) <= BASE_RANGE and E.price(c, i) >= BRK * hi:
            o = outcome(c, i, t, silent, span)
            stats["brk_events"] += 1
            if o[0] in (0, 1):
                vol = sum(c.qa[k] for k in range(bisect.bisect_right(c.t, t - 60), i + 1)) / 1e9
                bvol = sum(c.qa[k] for k in range(bisect.bisect_right(c.t, t - 60 - BASE_S), bisect.bisect_right(c.t, t - 60))) / 1e9 / (BASE_S / 60)
                brk.append((c.mint, t, o[0], o[1], vol / bvol if bvol > 0 else None))
            else:
                stats["brk_censored_or_amb"] += 1
            done = True
    s = t0 + BASE_S
    while s - t0 <= MAXAGE and s <= c.t[-1]:
        i = bisect.bisect_right(c.t, s) - 1
        if c.rt[i] is not None and c.rt[i] <= 0:
            break
        if BAND[0] <= E.mcap_usd(c, i) <= BAND[1] and window_stats(c, s - BASE_S, s, i + 1):
            o = outcome(c, i, s, silent, span)
            if o[0] in (0, 1):
                ctl.append((c.mint, s, o[0], o[1], s - t0))
            else:
                stats["ctl_censored_or_amb"] += 1
        s += 30.0


def boot(rows, key, draws=2000):
    byc = defaultdict(list)
    for r in rows:
        byc[r[0]].append(r[key])
    cs = list(byc.values())
    if not cs:
        return None
    rnd = random.Random(0)
    est = []
    for _ in range(draws):
        pick = [cs[rnd.randrange(len(cs))] for _ in cs]
        tot = sum(sum(p) for p in pick); cnt = sum(len(p) for p in pick)
        est.append(tot / cnt)
    est.sort()
    allv = [r[key] for r in rows]
    return {"mean": sum(allv) / len(allv), "lo": est[int(.025 * draws)], "hi": est[int(.975 * draws)]}


def summarize(rows):
    if not rows:
        return {"n": 0}
    return {"n": len(rows), "coins": len({r[0] for r in rows}), "hit_rate": boot(rows, 2), "net_R": boot(rows, 3)}


def run(day, out):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats, brk, ctl = defaultdict(int), [], []
    mint, coin = None, None
    lim = int(__import__("os").environ.get("TR_LIMIT", "0"))

    def flush(cn):
        if cn is not None and len(cn.t) >= 8:
            stats["coins"] += 1
            coin_units(cn, silent, span, stats, brk, ctl)
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
    res = {"day": day, "stats": dict(stats), "breakout": summarize(brk), "control": summarize([r for r in ctl if r[4] <= 1800]), "control_wide_age_600_7200": summarize(ctl),
           "breakout_vol_ge2": summarize([r for r in brk if r[4] is not None and r[4] >= 2]),
           "breakout_vol_lt2": summarize([r for r in brk if r[4] is not None and r[4] < 2])}
    json.dump({**res, "brk_rows": brk, "ctl_rows": ctl}, open(out, "w"))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
