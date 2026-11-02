#!/usr/bin/env python3
"""Decision test (pre-reg amendment 1 section 2): top-2% score seconds, first per episode, capacity 5 / one per coin / 900 s cooldown,
scalp TP +20% / SL -10% / 300 s on the exact sell value, costs 2.5 pp. usage: DAY SAMPLES.npz [THRESH_FROM.npz]   (threshold fixed on THRESH_FROM, default = SAMPLES)"""
import bisect, json, sqlite3, sys, time
from pathlib import Path

import joblib
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
import desk_tape_events as E
from tape_reader_samples import FEATS

OUT = Path(__file__).resolve().parents[2] / "data" / "models" / "tape_reader" / "v1"
TP, SL, HOLD, LAT, COST = 0.20, 0.10, 300.0, 1.5, 0.025
TOP, GAP, MAXOPEN, COOL, TARGET = 0.02, 120.0, 5, 900.0, 1


def score(path):
    d = np.load(path, allow_pickle=False)
    art = joblib.load(OUT / ("target_%d.joblib" % TARGET))
    C = [FEATS.index(f) for f in art["feats"]]
    p = art["iso"].predict(art["model"].predict_proba(d["X"][:, C])[:, 1])
    return d, p


def load_coin(db, mint):
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    c = E.Coin(mint)
    for r in db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, real_token, "
                        "fee_bps, creator_fee_bps, %s FROM swaps WHERE mint=? ORDER BY slot, %s, received_at, ordinal" % (qm, txi), (mint,)):
        c.add(r)
    return c


def trade(c, t, adverse):
    i = bisect.bisect_right(c.t, t) - 1
    tf = t + LAT
    j = bisect.bisect_right(c.t, tf) - 1
    if adverse:
        best, end = j, bisect.bisect_right(c.slot, c.slot[j] + 2)
        for k in range(j + 1, end):
            if E.price(c, k) > E.price(c, best):
                best = k
        j = best
    if c.fee[j] is None:
        return None
    qn = E.CLIP_SOL * 1e9 * (1 - c.fee[j])
    tokens = c.b[j] * qn / (c.q[j] + qn)
    reason, trig = "time", tf + HOLD
    for k in range(j + 1, bisect.bisect_right(c.t, tf + HOLD)):
        if c.fee[k] is None or (c.rt[k] is not None and c.rt[k] <= 0):
            return None
        v = E.value(c, k, tokens, qn)
        if v >= 1 + TP:
            reason, trig = "tp", c.t[k]; break
        if v <= 1 - SL:
            reason, trig = "sl", c.t[k]; break
    e = bisect.bisect_right(c.t, trig + LAT) - 1
    if e < j or c.fee[e] is None or (c.rt[e] is not None and c.rt[e] <= 0):
        return None
    return E.value(c, e, tokens, qn) - 1.0 - COST, reason, trig + LAT


def ci_lo(vals, coins):
    rng = np.random.default_rng(0)
    u, inv = np.unique(coins, return_inverse=True)
    s = np.bincount(inv, weights=vals); n = np.bincount(inv)
    r = [s[p].sum() / n[p].sum() for p in (rng.integers(0, len(u), len(u)) for _ in range(500))]
    return float(np.percentile(r, 5)), float(np.percentile(r, 2.5))


def main():
    day, samples = sys.argv[1], sys.argv[2]
    thr_src = sys.argv[3] if len(sys.argv) > 3 else samples
    _, pt = score(thr_src)
    thr = float(np.quantile(pt, 1 - TOP))
    d, p = score(samples)
    elig = np.where(p >= thr)[0]
    print("threshold %.4f (top %.0f%% of %s); eligible rows %d of %d" % (thr, 100 * TOP, Path(thr_src).name, len(elig), len(p)))
    by = {}
    for r in elig:
        by.setdefault(str(d["mint"][r]), []).append(r)
    entries, hyp_rows = [], list(elig)
    for m, rows in by.items():
        rows.sort(key=lambda r: d["t"][r])
        last = -1e18
        for r in rows:
            if d["t"][r] - last >= GAP:
                entries.append((float(d["t"][r]), m))
            last = d["t"][r]
    entries.sort()
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    imm = "&immutable=1" if day < time.strftime("%Y-%m-%d", time.gmtime()) else ""
    db = sqlite3.connect("file:%s?mode=ro%s" % (base, imm), uri=True, isolation_level=None)
    for adverse in (False, True):
        open_until, cool, rec, skipped = [], {}, [], 0
        coins_cache = {}
        for t, m in entries:
            open_until = [x for x in open_until if x > t]
            if len(open_until) >= MAXOPEN or cool.get(m, -1) > t:
                skipped += 1; continue
            if m not in coins_cache:
                coins_cache[m] = load_coin(db, m)
            res = trade(coins_cache[m], t, adverse)
            if res is None:
                skipped += 1; continue
            R, why, tx = res
            rec.append((R, why, m)); open_until.append(tx); cool[m] = tx + COOL
        R = np.array([r[0] for r in rec]); coins = np.array([r[2] for r in rec])
        lo90, lo95 = ci_lo(R, coins)
        ex = {k: sum(1 for r in rec if r[1] == k) for k in ("tp", "sl", "time")}
        print("%s: entries %d (skipped %d) trades %d coins %d mean R %+.4f lo90 %+.4f lo97.5 %+.4f win %.1f%% exits %s" % (
            "adverse" if adverse else "optimistic", len(entries), skipped, len(R), len(set(coins)), R.mean() if len(R) else float("nan"), lo90, lo95,
            100 * (R > 0).mean() if len(R) else 0, ex))
    # row-wise hypothetical (all eligible rows, no capacity) on a sample for speed
    rng = np.random.default_rng(0)
    pick = rng.choice(hyp_rows, size=min(3000, len(hyp_rows)), replace=False)
    hr = []
    for r in pick:
        m = str(d["mint"][r])
        if m not in coins_cache:
            coins_cache[m] = load_coin(db, m)
        res = trade(coins_cache[m], float(d["t"][r]), False)
        if res:
            hr.append(res[0])
    print("row-wise HYPOTHETICAL (no capacity, sample %d of %d rows): mean R %+.4f" % (len(hr), len(hyp_rows), np.mean(hr)))


main()
