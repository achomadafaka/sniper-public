#!/usr/bin/env python3
"""Desk attention dev-day rates (read-only). Protocol: docs/audits/2026-10-22-desk-attention-signals-preregistration.md section 3.

Population: the Part 1 label table (PASS-pool coins, 10-22 + 10-23), a 300 s hold window from PASS+8 s as the proxy for a desk
position (desk holds are minutes). Signals are evaluated on a 1 s grid from the U swap tape only; holder_dump needs an entry holder
snapshot the tape does not carry (unknown, not evaluated); quote_collapse is evaluated on tape SPOT as a PROXY for the runner's exact
quote (the real one is stricter/blinder). Outcome-free choice rule: lowest threshold with fires in <=5% of position-minutes;
precision P(spot falls >= D within 60 s | fire episode) vs the base P(same | any second in the same windows), mint-cluster bootstrap.
"""
import bisect
import collections
import glob
import json
import os
import random
import sqlite3
import statistics as st

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABELS = os.path.join(ROOT, 'data', 'research', 'desk-pass-labels.jsonl')
LAT, HOLD, FWD, D = 8, 300, 60, 18.0          # D = the desk stop magnitude (percent)
_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)


def load():
    tapes = {os.path.basename(p)[:10]: _ro(p) for p in glob.glob(ROOT + '/data/desk/swaps/*.db')}
    launches = {}
    for p in glob.glob(ROOT + '/data/desk/launches/*.db'):
        c = _ro(p)
        launches.update(dict(c.execute('SELECT mint,creator FROM launches')))
    coins = []
    for l in open(LABELS):
        r = json.loads(l)
        t0 = r['ts'] + LAT
        day = __import__('datetime').datetime.fromtimestamp(r['ts'], __import__('datetime').timezone.utc).date()
        rows = []
        for d in (day, day + __import__('datetime').timedelta(days=1)):
            c = tapes.get(d.isoformat())
            if c:
                rows += c.execute('SELECT received_at,is_buy,quote_atomic,actor,virtual_quote,virtual_token FROM swaps WHERE mint=? AND '
                                  'received_at BETWEEN ? AND ? AND virtual_quote>0 AND virtual_token>0 ORDER BY received_at,slot,ordinal',
                                  (r['mint'], t0 - 90, t0 + HOLD + FWD + 2)).fetchall()
        if rows and r.get('graduated') is False:        # non-graduated: the curve tape covers the whole window
            coins.append((r['mint'], t0, launches.get(r['mint']), rows))
    return coins


def grid(rows, t0):
    """Per-second series over [t0, t0+HOLD+FWD]: spot, plus per-second buy/sell SOL and actor sets."""
    n = HOLD + FWD + 1
    ts = [r[0] for r in rows]
    spot = []
    for s in range(-60, n):
        i = bisect.bisect_right(ts, t0 + s) - 1
        spot.append(None if i < 0 else (rows[i][4] / 1e9) / (rows[i][5] / 1e6))
    return spot            # index s+60


def events(rows, t0):
    out = collections.defaultdict(list)
    for t, b, q, a, *_ in rows:
        out[int(t - t0)].append((b, q / 1e9, a))
    return out


def signal_fires(name, par, spot, ev, creator, first_buyers):
    """List of fire seconds s in [0, HOLD) for this coin."""
    fires = []
    if name == 'creator_sell':
        for s in range(0, HOLD):
            if any((not b) and a == creator for b, _q, a in ev.get(s, ())):
                fires.append(s); break
    elif name == 'sell_burst':
        R, k = par
        for s in range(0, HOLD):
            buy = sell = 0.0
            sellers = set()
            for u in range(s - 9, s + 1):
                for b, q, a in ev.get(u, ()):
                    if b:
                        buy += q
                    else:
                        sell += q
                        if a != creator:
                            sellers.add(a)
            if sell > 0 and len(sellers) >= k and (buy == 0 or sell / buy >= R):
                fires.append(s)
    elif name == 'buyer_exodus':
        W, = par
        last_buy, recent = None, collections.deque()
        for s in range(-60, HOLD):
            nb = sum(1 for b, *_ in ev.get(s, ()) if b)
            if nb:
                last_buy = s
                recent.append(s)
            while recent and recent[0] < s - 30:
                recent.popleft()
            if s >= 0 and last_buy is not None and s - last_buy == W and len(recent) >= 3:
                fires.append(s)
    elif name == 'volume_spike':
        V, = par
        vol = {}
        for s in range(-70, HOLD + 1):
            vol[s] = sum(q for b, q, a in ev.get(s, ()))
        for s in range(0, HOLD, 1):
            cur = sum(vol[u] for u in range(s - 9, s + 1))
            bins = [sum(vol[u] for u in range(e - 9, e + 1)) for e in range(s - 60, s - 9, 10)]
            med = st.median(bins) if bins else 0
            if med > 0 and cur / med >= V:
                fires.append(s)
    elif name == 'quote_collapse_proxy':
        Q, N = par
        for s in range(0, HOLD):
            a, b = spot[s + 60], spot[s + 60 - N]
            if a and b and (a / b - 1) * 100 <= -Q:
                fires.append(s)
    return fires


def episodes(fires, gap=10):
    out, last = [], None
    for s in fires:
        if last is None or s - last > gap:
            out.append(s)
        last = s
    return out


def falls(spot, s):
    base = spot[s + 60]
    if not base:
        return None, None
    lo = min(x for x in spot[s + 60:s + 60 + FWD + 1] if x)
    dd = (lo / base - 1) * 100
    return dd <= -D, dd


def tighten_eval(prep):
    """Auto-tighten pair (creator_sell AND sell_burst within 10 s) and quote_collapse>=Q2: for each fire, simulate the new stop at
    fire_price*(1-S%) for 120 s. exit_vs_fire = exit price/fire price - 1; hold_vs_fire = spot at fire+60/fire - 1;
    false_tighten = stopped out but spot regains the fire price within 120 s of the fire."""
    print('AUTO-TIGHTEN replay (spot proxy; no sell impact, which favours holding => conservative for the tighten)')
    print('%-30s %5s %6s %10s %10s %9s %8s' % ('trigger', 'S%', 'n', 'exit-fire', 'hold60-fire', 'false-tgt', 'stopped'))
    trig = {}
    for mint, spot, ev, creator in prep:
        cs = signal_fires('creator_sell', (), spot, ev, creator, None)
        for R, k in ((5, 5), (10, 5), (20, 5), (10, 3)):
            sb = signal_fires('sell_burst', (R, k), spot, ev, creator, None)
            fs = [s for s in sb if cs and abs(s - cs[0]) <= 10 or (cs and s > cs[0] and s - cs[0] <= 10)]
            if fs:
                trig.setdefault('creator_sell&sell_burst(%d,%d)' % (R, k), []).append((mint, spot, min(fs)))
        for Q in (20, 25, 30, 40):
            qc = signal_fires('quote_collapse_proxy', (Q, 10), spot, ev, creator, None)
            if qc:
                trig.setdefault('quote_collapse>=%d(N=10)' % Q, []).append((mint, spot, qc[0]))
    for name, lst in sorted(trig.items()):
        for S in (3, 5, 8, 12):
            ex, ho, false, stopped, n = [], [], 0, 0, 0
            for mint, spot, s in lst:
                base = spot[s + 60]
                if not base:
                    continue
                n += 1
                stop = base * (1 - S / 100)
                path = [x for x in spot[s + 60 + 1:s + 60 + 121] if x]
                hit = next((i for i, x in enumerate(path) if x <= stop), None)
                h60 = spot[s + 60 + 60]
                ho.append((h60 / base - 1) * 100 if h60 else 0.0)
                if hit is None:
                    ex.append(ho[-1])
                else:
                    stopped += 1
                    ex.append((path[hit] / base - 1) * 100)
                    if any(x >= base for x in path[hit:]):
                        false += 1
            if n:
                print('%-30s %5d %6d %9.1f%% %9.1f%% %8.0f%% %7.0f%%' % (name, S, n, st.mean(ex), st.mean(ho), 100 * false / max(1, stopped), 100 * stopped / n))


def main():
    coins = load()
    random.seed(23)
    print('coins (non-graduated PASS, tape-covered): %d  position-hours %.1f' % (len(coins), len(coins) * HOLD / 3600))
    prep = []
    for mint, t0, creator, rows in coins:
        spot = grid(rows, t0)
        prep.append((mint, spot, events(rows, t0), creator))
    base_hits = [(m, falls(sp, s)[0]) for m, sp, ev, c in prep for s in range(0, HOLD, 5) if falls(sp, s)[0] is not None]
    base = sum(h for _, h in base_hits) / len(base_hits)
    print('BASE P(spot falls >= %.0f%% within %d s | any 5th second in window) = %.1f%%  (n=%d)\n' % (D, FWD, 100 * base, len(base_hits)))
    import sys
    ext = len(sys.argv) > 1 and sys.argv[1] == 'ext'
    if ext:
        configs = [('sell_burst', (R, k)) for R in (10, 20, 50) for k in (5, 8, 12)]
        configs += [('volume_spike', (V,)) for V in (20, 40, 80)]
        configs += [('quote_collapse_proxy', (Q, N)) for Q in (25, 30, 40) for N in (5, 10)]
    else:
        configs = [('creator_sell', ())]
        configs += [('sell_burst', (R, k)) for R in (2, 3, 5, 10) for k in (2, 3, 5)]
        configs += [('buyer_exodus', (W,)) for W in (10, 15, 20, 30)]
        configs += [('volume_spike', (V,)) for V in (3, 5, 10)]
        configs += [('quote_collapse_proxy', (Q, N)) for Q in (5, 8, 10, 15, 20) for N in (5, 10, 20)]
    if ext:
        tighten_eval(prep)
        print()
    print('%-22s %-8s %7s %9s %9s %7s %8s %8s %9s %s' % ('signal', 'par', 'fires/h', '%pos-min', '%pos>=1', 'n_ep', 'P(fall)', 'lift', 'medDD60', 'boot90 lift'))
    for name, par in configs:
        eps, fire_min, pos_hit = [], set(), 0
        by_mint = collections.defaultdict(list)
        for mint, spot, ev, creator in prep:
            fs = signal_fires(name, par, spot, ev, creator, None)
            if fs:
                pos_hit += 1
            for s in fs:
                fire_min.add((mint, s // 60))
            for s in episodes(fs):
                h, dd = falls(spot, s)
                if h is not None:
                    eps.append((mint, h, dd)); by_mint[mint].append(h)
        n_ep = len(eps)
        if not n_ep:
            print('%-22s %-8s no fires' % (name, par)); continue
        p = sum(h for _, h, _ in eps) / n_ep
        share_min = 100 * len(fire_min) / (len(prep) * HOLD / 60)
        # mint-cluster bootstrap of the lift (fire precision - base)
        mints = list(by_mint)
        lifts = []
        for _ in range(400):
            pick = [random.choice(mints) for _ in mints]
            xs = [h for m in pick for h in by_mint[m]]
            lifts.append(100 * (sum(xs) / len(xs) - base))
        lifts.sort()
        print('%-22s %-8s %7.1f %8.1f%% %8.0f%% %7d %7.1f%% %7.1fpp %8.1f%% (%.1f, %.1f)' % (
            name, ','.join(map(str, par)), n_ep / (len(prep) * HOLD / 3600), share_min, 100 * pos_hit / len(prep), n_ep, 100 * p,
            100 * (p - base), st.median(d for _, _, d in eps), lifts[20], lifts[379]))


if __name__ == '__main__':
    main()
