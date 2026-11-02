#!/usr/bin/env python3
"""Death-vs-pullback event extraction (Gamma, pre-reg docs/audits/2026-10-23-death-vs-pullback-preregistration.md).

Reads ONE closed day of data/desk/swaps/<day>.db (read-only, immutable) and writes one JSON line per pullback event:
hypothetical 25 USD entry at a level crossing, the first 10% / 20% drop of the executable value from its peak, the
forward return F, the coin's own noise, and every indicator at every k of the grid. No outcome is graded here; the
analysis script grades. Run niced: nice -n 19 ionice -c3 python3 scripts/research/desk_tape_events.py DAY OUT.jsonl
"""
from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

FX0 = 104.329875
CLIP_SOL = 25.0 / FX0
LEVELS = (12500, 15000, 17500, 20000, 25000, 30000, 35000)
INIT_Q, INIT_B, INIT_TOL = 30e9, 1.073e15, 0.005
ENTRY_DELAY_S, HOLD_S, LAG_S, FWD_S = 5.0, 900.0, 1.0, 300.0
NOISE_WIN_S, SILENCE_S = 300.0, 10.0
K_GRID = (2, 3, 4, 6)
DROPS = (0.90, 0.80)
SOL_QUOTES = (None, "", "So11111111111111111111111111111111111111112", "1" * 32)


class Coin:
    __slots__ = ("mint", "t", "slot", "q", "b", "buy", "qa", "ta", "actor", "fee", "rt", "quote_ok", "first_ok")

    def __init__(self, mint):
        self.mint = mint
        self.t, self.slot, self.q, self.b, self.buy, self.qa, self.ta, self.actor, self.fee, self.rt = ([] for _ in range(10))
        self.quote_ok = True
        self.first_ok = None

    def add(self, r):
        (_, t, slot, _o, buy, qa, ta, actor, vq, vb, rt, fb, fc, qmint) = r
        if self.first_ok is None:                         # the coin's FIRST swap must show the standard SOL curve start
            pre_q = vq - qa if buy else vq + qa
            pre_b = vb + ta if buy else vb - ta
            self.first_ok = abs(pre_q / INIT_Q - 1) <= INIT_TOL and abs(pre_b / INIT_B - 1) <= INIT_TOL
        if qmint not in SOL_QUOTES:
            self.quote_ok = False
        self.t.append(t); self.slot.append(slot); self.q.append(vq); self.b.append(vb); self.buy.append(buy)
        self.qa.append(qa); self.ta.append(ta); self.actor.append(actor); self.rt.append(rt)
        self.fee.append(None if fb is None or fc is None else (fb + fc) / 1e4)


def price(c, k):
    return c.q[k] / c.b[k]


def mcap_usd(c, k):
    return c.q[k] / c.b[k] * 1e6 * FX0


def value(c, k, tokens, qn):
    """Executable value of `tokens` (clip units) into state k, our own reserve delta applied, sell fee charged."""
    f = c.fee[k]
    if f is None:
        return None
    return tokens * (c.q[k] + qn) / c.b[k] * (1.0 - f) / (CLIP_SOL * 1e9)


def fill_index(c, i, adverse):
    j = bisect.bisect_right(c.t, c.t[i] + ENTRY_DELAY_S) - 1
    if not adverse:
        return j
    best, end = j, bisect.bisect_right(c.slot, c.slot[j] + 2)
    for k in range(j + 1, end):
        if price(c, k) > price(c, best):
            best = k
    return best


def noise5(c, te):
    """median |ln return| of consecutive 5 s closes over the trailing 300 s; None with fewer than 12 returns."""
    first = c.t[0]
    pts = []
    for n in range(61):
        ts = te - NOISE_WIN_S + 5.0 * n
        k = bisect.bisect_right(c.t, ts) - 1
        pts.append(price(c, k) if k >= 0 and ts >= first else None)
    rets = [abs(math.log(b / a)) for a, b in zip(pts, pts[1:]) if a and b]
    return statistics.median(rets) if len(rets) >= 12 else None


def window_sums(c, lo, hi, want_buy):
    a, b = bisect.bisect_right(c.t, lo), bisect.bisect_right(c.t, hi)
    return sum(c.qa[k] for k in range(a, b) if bool(c.buy[k]) == want_buy) / 1e9


def trailing_medians(c, te, width, n, fn):
    """fn over the n windows of `width` seconds ending at te - width * i (i = 1..n), non-empty ones only."""
    vals = [fn(c, te - width * (i + 1), te - width * i) for i in range(1, n + 1)]
    return [v for v in vals if v > 0]


def distinct_buyers(c, lo, hi):
    a, b = bisect.bisect_right(c.t, lo), bisect.bisect_right(c.t, hi)
    return len({c.actor[k] for k in range(a, b) if c.buy[k]})


def indicators(c, te, noise, tokens=None, qn=None):
    """{indicator: {k: bool|None}}; None = unknown inputs (counted, scored false).
    STRUCT_BREAK and ONE_SWAP_DROP are defined on the EXECUTABLE value V of `tokens` bought with `qn` (prereg c74aeaa7b section 3): pass both.
    Without them V falls back to the marginal price, which is NOT the preregistered contract (the fee and our reserve delta are ignored)."""
    out = {}
    vexec = tokens is not None and qn is not None

    def vv(k):
        return value(c, k, tokens, qn) if vexec else price(c, k)
    sells10 = window_sums(c, te - 10, te, False)
    med10 = trailing_medians(c, te, 10, 29, lambda cc, lo, hi: window_sums(cc, lo, hi, False))
    out["SELL_BURST"] = {k: (sells10 >= k * statistics.median(med10)) if len(med10) >= 6 else None for k in K_GRID}
    b30, s30 = distinct_buyers(c, te - 30, te), window_sums(c, te - 30, te, False)
    buy30 = window_sums(c, te - 30, te, True)
    medb = trailing_medians(c, te, 30, 9, lambda cc, lo, hi: float(distinct_buyers(cc, lo, hi)))
    out["EXODUS"] = {k: ((b30 <= statistics.median(medb) / k) and s30 > buy30) if len(medb) >= 4 else None for k in K_GRID}
    ia, ib = bisect.bisect_right(c.t, te - NOISE_WIN_S), bisect.bisect_right(c.t, te - 5)
    k_now = bisect.bisect_right(c.t, te) - 1
    sells15, buys15 = window_sums(c, te - 15, te, False), window_sums(c, te - 15, te, True)
    vlow = [vv(k) for k in range(ia, ib)] if ib > ia else []
    v_now = vv(k_now)
    if vlow and noise and v_now is not None and None not in vlow and v_now > 0:
        drop = math.log(min(vlow) / v_now)
        out["STRUCT_BREAK"] = {k: (drop >= k * noise and sells15 > buys15) for k in K_GRID}
    else:
        out["STRUCT_BREAK"] = {k: None for k in K_GRID}
    a10 = bisect.bisect_right(c.t, te - 10)
    worst, unknown = 0.0, False
    for k in range(max(a10, 1), k_now + 1):
        if not c.buy[k]:
            v0, v1 = vv(k - 1), vv(k)
            if v0 is None or v1 is None or v0 <= 0 or v1 <= 0:
                unknown = True
                continue
            worst = max(worst, math.log(v0 / v1))
    out["ONE_SWAP_DROP"] = {k: (worst >= k * noise) if noise and not unknown else None for k in K_GRID}
    a30 = bisect.bisect_right(c.t, te - 30)
    pos = defaultdict(float)
    for k in range(a30):
        pos[c.actor[k]] += c.ta[k] if c.buy[k] else -c.ta[k]
    if pos:
        top = max(pos, key=pos.get)
        sold = sum(c.ta[k] for k in range(a30, k_now + 1) if not c.buy[k] and c.actor[k] == top)
        out["TOP_DUMP"] = {k: (pos[top] > 0 and sold >= pos[top] / k) for k in K_GRID}
    else:
        out["TOP_DUMP"] = {k: None for k in K_GRID}
    creator = c.actor[0]
    out["CREATOR_SELL"] = {0: any(not c.buy[k] and c.actor[k] == creator for k in range(a30, k_now + 1))}
    return out


def silence_intervals(db):
    """Seconds-with-no-swap runs >= SILENCE_S anywhere on the tape (the capture-gap test)."""
    secs = [r[0] for r in db.execute("SELECT DISTINCT CAST(received_at AS INTEGER) FROM swaps ORDER BY 1")]
    return [(a, b) for a, b in zip(secs, secs[1:]) if b - a >= SILENCE_S], (secs[0], secs[-1])


def overlaps(spans, lo, hi):
    return any(a < hi and b > lo for a, b in spans)


def events_for(c, silent, day_span, stats):
    out = []
    n = len(c.t)
    if not (c.first_ok and c.quote_ok):
        stats["quote_unverified"] += 1
        return out
    crossed = set()
    for i in range(n):
        m = mcap_usd(c, i)
        for lv in LEVELS:
            if lv in crossed or m < lv:
                continue
            crossed.add(lv)
            for adverse in (False, True):
                out += simulate(c, i, lv, adverse, silent, day_span, stats)
    return out


def simulate(c, i, lv, adverse, silent, day_span, stats):
    t0 = c.t[i]
    tf = t0 + ENTRY_DELAY_S
    if tf + HOLD_S + LAG_S + FWD_S > day_span[1] or t0 - NOISE_WIN_S - 900 < day_span[0]:
        stats["day_edge"] += 1
        return []
    j = fill_index(c, i, adverse)
    f = c.fee[j]
    if f is None or any(x is None for x in c.fee[j:bisect.bisect_right(c.t, tf + HOLD_S + FWD_S + LAG_S)]):
        stats["null_fee"] += 1
        return []
    qn = CLIP_SOL * 1e9 * (1.0 - f)
    tokens = c.b[j] * qn / (c.q[j] + qn)
    if overlaps(silent, t0 - 900, tf + HOLD_S + LAG_S + FWD_S):
        stats["tape_silence"] += 1
        return []
    peak, hit, rows = value(c, j, tokens, qn), {}, []
    end = bisect.bisect_right(c.t, tf + HOLD_S)
    for k in range(j + 1, end):
        v = value(c, k, tokens, qn)
        peak = max(peak, v)
        for d in DROPS:
            if d in hit or v > d * peak:
                continue
            hit[d] = (k, peak)
        if len(hit) == len(DROPS):
            break
    for d, (k, pk) in hit.items():
        te = c.t[k]
        xi = bisect.bisect_right(c.t, te + LAG_S) - 1
        hi = bisect.bisect_right(c.t, te + LAG_S + FWD_S) - 1
        if any(x is None for x in c.rt[j:hi + 1]):
            stats["null_real_token"] += 1
            continue
        if c.rt[xi] <= 0:                                # completed before our exit: no curve sell exists
            stats["completed_before_exit"] += 1
            continue
        grad = min(c.rt[j:hi + 1]) <= 0
        if grad:                                         # curve completes inside the window: kept, flagged, F is an UPPER bound
            stats["graduated"] += 1
            hi = max(x for x in range(xi, hi + 1) if c.rt[x] > 0) if any(c.rt[x] > 0 for x in range(xi, hi + 1)) else xi
        noise = noise5(c, te)
        if noise is None or noise <= 0:
            stats["noise_unknown"] += 1
            continue
        vx, vh = value(c, xi, tokens, qn), value(c, hi, tokens, qn)
        ind = indicators(c, te, noise, tokens, qn)
        rows.append({"mint": c.mint, "level": lv, "adverse": adverse, "drop": d, "te": te, "t0": t0,
                     "noise": noise, "depth_k": math.log(pk / value(c, k, tokens, qn)) / noise,
                     "vx": vx, "vh": vh, "F": vh / vx - 1.0, "grad": grad,
                     "ind": {n: {str(kk): fl for kk, fl in dd.items()} for n, dd in ind.items()}})
    return rows


def run(day, out_path):
    base = Path(__file__).resolve().parents[2] / "data" / "desk" / "swaps" / ("%s.db" % day)
    db = sqlite3.connect("file:%s?mode=ro&immutable=1" % base, uri=True, isolation_level=None)
    cols = [r[1] for r in db.execute("PRAGMA table_info(swaps)")]
    txi = "tx_index" if "tx_index" in cols else "slot"
    qm = "quote_mint" if "quote_mint" in cols else "NULL"
    silent, day_span = silence_intervals(db)
    stats = defaultdict(int)
    cur = db.execute("SELECT mint, received_at, slot, ordinal, is_buy, quote_atomic, token_atomic, actor, virtual_quote, "
                     "virtual_token, real_token, fee_bps, creator_fee_bps, %s FROM swaps ORDER BY mint, slot, %s, received_at, ordinal"
                     % (qm, txi))
    n_events, mint, coin = 0, None, None
    with open(out_path, "w") as fh:
        def flush(cn):
            nonlocal n_events
            if cn is None or len(cn.t) < 2:
                return
            stats["coins"] += 1
            for row in events_for(cn, silent, day_span, stats):
                fh.write(json.dumps(row) + "\n")
                n_events += 1
        for r in cur:
            if r[0] != mint:
                flush(coin)
                mint, coin = r[0], Coin(r[0])
            coin.add((r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12], r[13]))
        flush(coin)
    print(json.dumps({"day": day, "events": n_events, "silent_intervals": len(silent), **stats}))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
