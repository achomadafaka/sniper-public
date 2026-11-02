"""TAPE SHAPE: pure functions over a coin's closed 5 s buckets (no I/O, no clock). The shared noise unit and the two pre-move feed
triggers of the promise tracker (feed C 'breakout', feed D 'dump_recovery'). Observe-only: nothing here trades.

A bucket is the tracker's list [idx, o, h, l, c, buy_sol, sell_sol, n_buy, n_sell, max_single_sell_sol, creator_sell_sol]; idx = int(t // 5).
CLOSED buckets only (idx < the open bucket's idx); the caller passes `open_idx`. A bucket with no swap does not exist in the ring: it is a
QUIET bucket and counts 0 in the noise (Gamma's range5 pin, 2026-10-23).

NOISE n5 = median over the trailing NOISE_B closed bucket slots (300 s) of ln(high/low) per bucket, quiet = 0, counted from the coin's first
clean bucket, >= MIN_NOISE_B slots else unknown; a median of 0 is unknown (never a trigger). Every threshold below is a MULTIPLE of n5 or a
count of buckets, so the same rule means the same thing on a 3.9k coin and a 90k coin.

Absent / zero / unknown: unknown noise, a window with too few present buckets, an unknown previous close or no actor identity gives None
(no trigger), never a zero and never a pass.
"""
from __future__ import annotations

import math
import statistics

NOISE_B = 60               # structural: 300 s of 5 s buckets (Gamma's range5 window)
MIN_NOISE_B = 12           # statistical minimum for a median (Gamma's >= 12 rule)
CONS_B = 6                 # structural: the consolidation window is 6 buckets = 30 s (the tracker's TIGHT_S)
MIN_CONS_PRESENT = 3       # a consolidation needs trades in at least half of its slots
BREAK_UNITS = 1.0          # structural: a breakout must clear the range top by one noise unit
HOLD_B = 4                 # buckets the dump low must hold (20 s)
RECOVER_B = 12             # the recovery must be confirmed within 12 buckets (60 s) of the dump, else the dump is stale
LARGE_SHARE = 0.5          # one holder dominates a bucket's selling when it sold at least this share of it
DUMP_K = 3.0               # PROVISIONAL (range5 units): Gamma found no k separation across 2-4 on 17 coins; knob MEMECOIN_DESK_PROMISE_DUMP_K
CONS_SCALE = math.sqrt(CONS_B)   # random-walk scaling: a pure-noise coin's 6-bucket range is about sqrt(6) x its 1-bucket range


def _present(buckets):
    return {b[0]: b for b in buckets}


def range5_detail(buckets, open_idx, first_idx):
    """(n5, why): why = 'ok', 'few_slots' (under MIN_NOISE_B slots since the first clean bucket) or 'zero' (the median is 0: the coin is
    flat/dead over the window, a real observation, not a missing one)."""
    last = open_idx - 1
    start = max(first_idx, last - NOISE_B + 1)
    if last - start + 1 < MIN_NOISE_B:
        return None, "few_slots"
    by = _present(buckets)
    vals = []
    for i in range(start, last + 1):
        b = by.get(i)
        vals.append(math.log(b[2] / b[3]) if b is not None and b[3] > 0 and b[2] >= b[3] else 0.0)
    n5 = statistics.median(vals)
    return (n5, "ok") if n5 > 0 else (None, "zero")


def range5(buckets, open_idx, first_idx):
    """The coin's own noise (see module doc) or None."""
    return range5_detail(buckets, open_idx, first_idx)[0]


def breakout(buckets, open_idx, n5):
    """Feed C. The just-closed bucket (idx open_idx-1, or -2 for tick jitter) closes above the top of a consolidation by more than one noise
    unit with buyers over sellers and the window's biggest buy SOL. None = no trigger or unknown. Returns the block fields on a trigger."""
    if n5 is None or n5 <= 0:
        return None
    by = _present(buckets)
    trig = next((by[i] for i in (open_idx - 1, open_idx - 2) if i in by), None)
    if trig is None:
        return None
    win = [by[i] for i in range(trig[0] - CONS_B, trig[0]) if i in by]
    if len(win) < MIN_CONS_PRESENT:
        return None
    hi, lo = max(b[2] for b in win), min(b[3] for b in win)
    if lo <= 0:
        return None
    rng = math.log(hi / lo)
    if rng > CONS_SCALE * n5:
        return None                                           # not a consolidation: the window itself trended or whipped
    if not (trig[4] > hi * math.exp(BREAK_UNITS * n5) and trig[5] > trig[6] and trig[5] > max(b[5] for b in win)):
        return None
    return {"trigger_bucket": trig[0], "window_buckets": len(win), "window_range": round(rng, 5), "n5": round(n5, 5),
            "break_over_top": round(math.log(trig[4] / hi), 5), "buy_sol": round(trig[5], 4), "sell_sol": round(trig[6], 4),
            "window_max_buy_sol": round(max(b[5] for b in win), 4)}


def dump_recovery(buckets, open_idx, n5, k=DUMP_K):
    """Feed D. A dump bucket (sellers over buyers, a drop of at least k noise units from the previous close to its low, and one large holder
    or the creator behind it), then within RECOVER_B buckets: the dump low held (every close since >= low x (1 - n5), >= HOLD_B buckets
    after it with at least 3 present), buyers over sellers in 2 of the last 3 present buckets, and the last close back over the middle of
    the drop. The most recent qualifying dump wins. None = no trigger or unknown (no actor identity is never a dump)."""
    if n5 is None or n5 <= 0:
        return None
    by = _present(buckets)
    last = open_idx - 1
    for d in range(last - HOLD_B, last - RECOVER_B - 1, -1):
        b = by.get(d)
        if b is None or not b[6] > b[5]:
            continue
        prev = next((by[i] for i in (d - 1, d - 2) if i in by), None)
        if prev is None or prev[4] <= 0 or b[3] <= 0:
            continue
        drop = math.log(prev[4] / b[3])
        if drop < k * n5:
            continue
        big = b[6] > 0 and (b[9] >= LARGE_SHARE * b[6] or b[10] > 0)
        if not big:
            continue
        after = [by[i] for i in range(d + 1, last + 1) if i in by]
        if len(after) < 3 or last < d + HOLD_B:
            continue
        low = b[3]
        if any(x[4] < low * (1 - n5) for x in after):
            continue
        tail = after[-3:]
        if sum(1 for x in tail if x[5] > x[6]) < 2:
            continue
        mid = low + 0.5 * (prev[4] - low)
        if after[-1][4] < mid:
            continue
        return {"dump_bucket": d, "buckets_since_dump": last - d, "drop_in_n5": round(drop / n5, 2), "n5": round(n5, 5),
                "dump_low": low, "prev_close": prev[4], "last_close": after[-1][4], "dump_sell_sol": round(b[6], 4),
                "creator_sold": b[10] > 0, "largest_seller_share": round(b[9] / b[6], 3) if b[6] > 0 else None, "k": k,
                "k_provisional": True}
    return None
