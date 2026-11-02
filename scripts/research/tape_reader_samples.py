#!/usr/bin/env python3
"""Tape reader samples (pre-reg docs/audits/2026-10-23-tape-reader-preregistration.md): features + labels per coin-second.
usage: DAY OUT.npz     (one coin-second every 30 s of life from age 10 s, <= 60 per coin, no mcap gate)"""
import bisect, math, os, sqlite3, statistics, sys, time
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
import desk_pullback_units as PB
import desk_regime_play as RG

TARGETS = ((0.20, 0.10, 60), (0.20, 0.10, 300), (0.10, 0.05, 60), (0.30, 0.15, 300), (0.50, 0.20, 600), (0.20, 0.20, 900))
FEATS = ["age_s", "log_mcap", "ret_10", "ret_30", "ret_60", "ret_300", "noise5",
         "wal_10", "wal_30", "wal_60", "wal_180", "buy_10", "buy_30", "buy_60", "buy_180",
         "bshare_10", "bshare_60", "bshare_180", "net_10", "net_60", "swaps_10", "swaps_60", "maxsell2s_60",
         "drawdown_900", "since_high", "since_low", "top_buyer", "creator_sold", "med_buy", "p90_buy",
         "holders_pos", "growth", "w60max300", "impact", "curve_fill"]
REGS = {"YOUNG": 0, "QUIET": 1, "UNKNOWN": 2, "DYING": 3, "IMPULSE": 4, "RANGE": 5, "TREND": 6, "CONSOLIDATION": 7, "OTHER": 8}
DIR = (0.20, 0.20, 300)      # three-way direction target (prereg v2): 1 up first, 0 down first, 2 neither
INIT_REAL = 7.931e14
STEP, FIRST, MAXAGE = 30.0, 10.0, 1800.0
LAT = 1.5
NAN = float("nan")


def rng(c, lo, hi):
    return range(bisect.bisect_right(c.t, lo), bisect.bisect_right(c.t, hi))


def price_at(c, s):
    k = bisect.bisect_right(c.t, s) - 1
    return E.price(c, max(k, 0))


def features(c, i, t):
    p = E.price(c, i)
    age = t - c.t[0]
    f = {"age_s": age, "log_mcap": math.log(E.mcap_usd(c, i))}
    for k in (10, 30, 60, 300):
        f["ret_%d" % k] = math.log(p / price_at(c, t - k))
    n5 = PB.range5(c, t)
    f["noise5"] = NAN if n5 is None else n5
    for w in (10, 30, 60, 180):
        idx = rng(c, t - w, t)
        f["wal_%d" % w] = len({c.actor[k] for k in idx})
        f["buy_%d" % w] = len({c.actor[k] for k in idx if c.buy[k]})
    for w in (10, 60, 180):
        idx = rng(c, t - w, t)
        b = sum(c.qa[k] for k in idx if c.buy[k]) / 1e9
        s = sum(c.qa[k] for k in idx if not c.buy[k]) / 1e9
        f["bshare_%d" % w] = b / (b + s) if b + s > 0 else NAN
        if w in (10, 60):
            f["net_%d" % w] = b - s
            f["swaps_%d" % w] = len(idx)
    sells = defaultdict(float)
    for k in rng(c, t - 60, t):
        if not c.buy[k]:
            sells[int(c.t[k] // 2)] += c.qa[k] / 1e9
    f["maxsell2s_60"] = max(sells.values()) if sells else 0.0
    w900 = [E.price(c, k) for k in rng(c, t - 900, t)]
    f["drawdown_900"] = p / max(w900) if w900 else NAN
    if w900:
        ks = list(rng(c, t - 900, t))
        hi_k = max(ks, key=lambda k: E.price(c, k)); lo_k = min(ks, key=lambda k: E.price(c, k))
        f["since_high"], f["since_low"] = t - c.t[hi_k], t - c.t[lo_k]
    else:
        f["since_high"] = f["since_low"] = NAN
    net = defaultdict(float)
    buy_sol = 0.0
    for k in range(i + 1):
        s = c.qa[k] / 1e9
        net[c.actor[k]] += s if c.buy[k] else -s
        if c.buy[k]:
            buy_sol += s
    f["top_buyer"] = max(net.values()) / buy_sol if buy_sol > 0 else NAN
    first = c.actor[0]
    bought = sum(c.ta[k] for k in range(i + 1) if c.actor[k] == first and c.buy[k])
    sold = sum(c.ta[k] for k in range(i + 1) if c.actor[k] == first and not c.buy[k])
    f["creator_sold"] = sold / bought if bought > 0 else NAN
    sizes = [c.qa[k] / 1e9 for k in rng(c, t - 180, t) if c.buy[k]]
    f["med_buy"] = statistics.median(sizes) if sizes else NAN
    f["p90_buy"] = float(np.percentile(sizes, 90)) if sizes else NAN
    tok = defaultdict(float)
    for k in range(i + 1):
        tok[c.actor[k]] += c.ta[k] if c.buy[k] else -c.ta[k]
    f["holders_pos"] = sum(1 for v in tok.values() if v > 0)
    last = f["buy_60"]
    prev = len({c.actor[k] for k in rng(c, t - 120, t - 60) if c.buy[k]})
    f["growth"] = last / prev if prev > 0 else NAN
    wm = 0
    for j in range(31):
        s = t - 10 * j
        wm = max(wm, len({c.actor[k] for k in rng(c, s - 60, s)}))
    f["w60max300"] = wm
    fee = c.fee[i] if c.fee[i] is not None else 0.0125
    qn = E.CLIP_SOL * 1e9 * (1 - fee)
    T = c.b[i] * qn / (c.q[i] + qn)
    f["impact"] = T / (c.b[i] + T)
    f["curve_fill"] = 1.0 - c.rt[i] / INIT_REAL if c.rt[i] is not None else NAN
    return [f[n] for n in FEATS]


def touch(prices, slots, p_ref, U, D, times=None, stop=None, rts=None, three=False):
    """first-touch with censor priority (amendment 1): a barrier reached BEFORE a silence/gap (`stop`) or a curve completion
    (rts <= 0) is resolved; otherwise the sample is censored (-2). 1 up first, 0 down first or neither (fully observed), -1 same-slot double touch."""
    up, dn = p_ref * (1 + U), p_ref * (1 - D)
    n, k = len(prices), 0
    while k < n:
        s, j = slots[k], k
        while j < n and slots[j] == s:
            j += 1
        m = j                                    # rows of this slot group that happen before the first censor (silence start, curve completion)
        for r in range(k, j):
            if (stop is not None and times[r] > stop) or (rts is not None and rts[r] is not None and rts[r] <= 0):
                m = r                            # the censoring row itself and later rows of the group are unknown order: not used
                break
        if m > k:
            hi, lo = max(prices[k:m]), min(prices[k:m])
            a, b = hi >= up, lo <= dn
            if a and b:
                return -1
            if a:
                return 1
            if b:
                return 0
        if m < j:
            return -2
        k = j
    return -2 if stop is not None else (2 if three else 0)            # a silence/edge began inside the window and no barrier was seen first


def first_silence_after(silent, t):
    """start of the first tape-silence interval that ends after t (or None)."""
    cands = [a for a, b in silent if b > t]
    return min(cands) if cands else None


def labels(c, i, t, silent, span):
    """(y[6], ye[6]) in {1, 0, -1 ambiguous, -2 censored}; censor priority per amendment 1."""
    y, ye = [], []
    p_ref = E.price(c, i)
    tf = t + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    qn = tokens = None
    if c.fee[j] is not None:
        qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
        tokens = c.b[j] * qn / (c.q[j] + qn)
    pre_gap = E.overlaps(silent, t - 300, t) or t - 300 < span[0]          # feature window must be observed
    fs = first_silence_after(silent, t)
    y3 = -2
    if not pre_gap:
        U, D, H = DIR
        stop = fs if fs is not None and fs < t + H + 2 else None
        if t + H + 2 > span[1]:
            stop = span[1] if stop is None else min(stop, span[1])
        e = bisect.bisect_right(c.t, t + H)
        y3 = touch([E.price(c, k) for k in range(i + 1, e)], c.slot[i + 1:e], p_ref, U, D, times=c.t[i + 1:e], stop=stop, rts=c.rt[i + 1:e], three=True)
    for U, D, H in TARGETS:
        if pre_gap:
            y.append(-2); ye.append(-2); continue
        end_obs = t + H
        stop = None
        if fs is not None and fs < end_obs + 2:
            stop = fs
        if end_obs + 2 > span[1]:
            stop = span[1] if stop is None else min(stop, span[1])
        e = bisect.bisect_right(c.t, t + H)
        y.append(touch([E.price(c, k) for k in range(i + 1, e)], c.slot[i + 1:e], p_ref, U, D,
                       times=c.t[i + 1:e], stop=stop, rts=c.rt[i + 1:e]))
        e2 = bisect.bisect_right(c.t, tf + H)
        if tokens is None:
            ye.append(-2); continue
        fm = next((k for k in range(j + 1, e2) if c.fee[k] is None), None)       # a missing fee is a censor from that row on, not a whole-horizon veto
        xs, xstop = (e2, stop) if fm is None else (fm, c.t[fm] if stop is None else min(stop, c.t[fm]))
        vals = [E.value(c, k, tokens, qn) for k in range(j + 1, xs)]
        ye.append(touch(vals, c.slot[j + 1:xs], 1.0, U, D, times=c.t[j + 1:xs], stop=xstop, rts=c.rt[j + 1:xs]))
    return y, ye, y3


def coin_rows(c, silent, span, stats):
    rows = []
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return rows
    t = c.t[0] + FIRST
    while t - c.t[0] <= MAXAGE and t <= c.t[-1]:
        i = bisect.bisect_right(c.t, t) - 1
        if i + 1 < 8:
            t += STEP; continue
        m = E.mcap_usd(c, i)
        if m > 40000 or (c.rt[i] is not None and c.rt[i] <= 0):
            break
        x = features(c, i, t)
        y, ye, y3 = labels(c, i, t, silent, span)
        if t - c.t[0] >= 300:
            reg, _ = RG.regime(c, t) if not E.overlaps(silent, t - 900, t + 5) else ("UNKNOWN", None)
        else:
            reg = "YOUNG"
        rows.append((x, y, ye, y3, t, REGS.get(reg, 2), m))
        t += STEP
    return rows


def run(day, out):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, span = E.silence_intervals(db)
    stats = defaultdict(int)
    X, Y, YE, Y3, T, R, M, MINT = [], [], [], [], [], [], [], []
    lim = int(os.environ.get("TR_LIMIT", "0"))
    mint, coin = None, None

    def flush(cn):
        if cn is None or len(cn.t) < 8:
            return
        stats["coins"] += 1
        for x, y, ye, y3, t, reg, m in coin_rows(cn, silent, span, stats):
            X.append(x); Y.append(y); YE.append(ye); Y3.append(y3); T.append(t); R.append(reg); M.append(m); MINT.append(cn.mint)
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
    np.savez_compressed(out, X=np.array(X, np.float32), y=np.array(Y, np.int8), ye=np.array(YE, np.int8), y3=np.array(Y3, np.int8), t=np.array(T), reg=np.array(R, np.int8),
                        mcap=np.array(M, np.float32), mint=np.array(MINT), feats=np.array(FEATS))
    print({"day": day, "rows": len(X), **stats})


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
