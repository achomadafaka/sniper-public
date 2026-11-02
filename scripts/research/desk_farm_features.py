#!/usr/bin/env python3
"""Farm / wash-trade signature features from the curve tape (Gamma, docs/audits/2026-10-23-farm-signature-preregistration.md).
Trailing window only (nothing after t). Reuses the Coin loader of desk_tape_events."""
from __future__ import annotations

import bisect
import importlib.util
import math
import statistics
from collections import defaultdict
from pathlib import Path

_spec = importlib.util.spec_from_file_location("dte", Path(__file__).with_name("desk_tape_events.py"))
E = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(E)
W_S = 180.0


def features(c, k_now, w=W_S):
    """dict of ratio features at state k_now over (t - w, t]; None when the window has too little to say."""
    t = c.t[k_now]
    a = bisect.bisect_right(c.t, t - w)
    idx = range(a, k_now + 1)
    if len(idx) < 20:
        return None
    buy_sol = sum(c.qa[k] for k in idx if c.buy[k]) / 1e9
    sell_sol = sum(c.qa[k] for k in idx if not c.buy[k]) / 1e9
    gross = buy_sol + sell_sol
    if gross <= 0:
        return None
    swaps = defaultdict(int)
    net_tok = defaultdict(float)
    bought, sold = set(), set()
    for k in idx:
        swaps[c.actor[k]] += 1
        net_tok[c.actor[k]] += c.ta[k] if c.buy[k] else -c.ta[k]
        (bought if c.buy[k] else sold).add(c.actor[k])
    sizes = [c.qa[k] / 1e9 for k in idx if c.buy[k]]
    med = statistics.median(sizes) if sizes else 0.0
    p0, p1 = E.price(c, max(a - 1, 0)), E.price(c, k_now)
    sol = lambda k: c.qa[k] / 1e9
    clip = E.CLIP_SOL
    dust = sum(1 for s_ in sizes if s_ <= 0.25 * clip) / len(sizes) if sizes else None
    by_slot = defaultdict(set)
    for k in idx:
        if c.buy[k]:
            by_slot[c.slot[k]].add(c.actor[k])
    burst = (sum(1 for v in by_slot.values() if len(v) >= 5) / len(by_slot)) if by_slot else None
    common = defaultdict(int)
    for s_ in sizes:
        common[round(s_, 3)] += 1
    top3 = (sum(sorted(common.values(), reverse=True)[:3]) / len(sizes)) if sizes else None
    net_sol = defaultdict(float)
    for k in idx:
        net_sol[c.actor[k]] += sol(k) if c.buy[k] else -sol(k)
    top_buyer = (max(net_sol.values()) / buy_sol) if buy_sol > 0 else None
    age = (t - c.t[0]) if c.first_ok else None
    return {
        "dust_share": dust, "burst_share": burst, "top3_size_share": top3, "top_buyer_share": top_buyer, "age_s": age,
        "n_swaps": len(idx), "n_wallets": len(swaps), "gross_sol": gross,
        "net_ratio": (buy_sol - sell_sol) / gross,                                     # ~0 = wash, high = one-sided demand
        "swaps_per_wallet": len(idx) / len(swaps),
        "one_shot_share": sum(1 for v in swaps.values() if v == 1) / len(swaps),
        "retained": (sum(1 for x in bought if net_tok[x] > 0) / len(bought)) if bought else None,   # buyers still holding
        "round_trip_share": len(bought & sold) / len(bought | sold),
        "buy_cv": (statistics.pstdev(sizes) / statistics.mean(sizes)) if len(sizes) >= 5 and statistics.mean(sizes) > 0 else None,
        "buy_mode_share": (sum(1 for s in sizes if abs(s / med - 1) <= 0.10) / len(sizes)) if med > 0 and sizes else None,
        "move_per_sol": math.log(p1 / p0) / gross,                                       # log price move per SOL of gross volume
    }


def slot_end_prices(c, lo, hi):
    """{slot: price after the slot's LAST swap}. Within a slot the tape order is not reliable, so the last swap is the one
    whose post-swap reserves are no other swap's pre-swap reserves (reserve chain); a slot where that is not unique
    gets None (unknown, never a guess)."""
    out, k = {}, lo
    while k < hi:
        s0, j = c.slot[k], k
        while j < hi and c.slot[j] == s0:
            j += 1
        pres = set()
        for x in range(k, j):
            pq = c.q[x] - c.qa[x] if c.buy[x] else c.q[x] + c.qa[x]
            pb = c.b[x] + c.ta[x] if c.buy[x] else c.b[x] - c.ta[x]
            pres.add((pq, pb))
        ends = [x for x in range(k, j) if (c.q[x], c.b[x]) not in pres]
        out[s0] = E.price(c, ends[0]) if len(ends) == 1 else None
        k = j
    return out


def crash_label(c, k_now, horizon_s=600.0, drop=0.40):
    """1 if within (t, t + horizon] the slot-end price falls >= `drop` across two consecutive slots (slot s-1 end to slot s+1
    end, or to slot s end when s+1 has no swap); None when an involved slot has no unique end state."""
    t = c.t[k_now]
    end = bisect.bisect_right(c.t, t + horizon_s)
    ends = slot_end_prices(c, k_now, end)
    slots = sorted(ends)
    prev = E.price(c, k_now)
    unknown = False
    for n, s0 in enumerate(slots):
        nxt = ends.get(s0 + 1, ends[s0]) if (s0 + 1 in ends or True) else ends[s0]
        e0, e1 = ends[s0], ends.get(s0 + 1, ends[s0])
        if e0 is None or e1 is None:
            unknown = True
        else:
            if min(e0, e1) <= (1 - drop) * prev:
                return 1
        if e0 is not None:
            prev = e0
    return None if unknown else 0
