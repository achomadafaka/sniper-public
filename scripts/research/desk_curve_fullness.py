#!/usr/bin/env python3
"""Curve-fullness test (read-only). Pre-reg: docs/superpowers/specs/2026-10-23-curve-fullness-preregistration.md (0e4759ee2).

usage: desk_curve_fullness.py [YYYY-MM-DD]   (default 2026-10-23)
Frozen: bins, 85 SOL threshold, E8 = PASS+8 s, rails 30/18/12 or the verdict's, 300 s hold, censor-never-fill after graduation.
"""
import bisect
import collections
import datetime as dt
import json
import os
import random
import sqlite3
import statistics as st
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if os.environ.get('DESK_PS_TAPE_SRC'):          # until memecoin_desk_ps_tape is on main: a checkout that has it (read-only)
    sys.path.insert(0, os.environ['DESK_PS_TAPE_SRC'])

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DAY = sys.argv[1] if len(sys.argv) > 1 else '2026-10-23'
LAT, HOLD = 8, 300
THRESH, GRAD_MIN = 85.0e9, 84.0e9
CAPTURE_START = 1792738622.0         # 2026-10-23T06:57:02Z
FIRST_GAP, GAP_MAX = 30.0, 60.0
BINS = [('B1 <50%', 0.0, 0.50), ('B2 50-65%', 0.50, 0.65), ('B3 65-75%', 0.65, 0.75), ('B4 75-85%', 0.75, 0.85), ('B5 >=85%', 0.85, 9.9)]
_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
ts = lambda s: dt.datetime.fromisoformat(s).timestamp()


def rails_sim(series, t0, sl, tp, tr):
    """series: [(t, price)] sorted, includes the entry state at/before t0. Returns (ret%, resolved_t, path_end_price)."""
    pre = [p for t, p in series if t <= t0]
    if not pre:
        return None
    e = pre[-1]
    peak, last = e, e
    for t, p in series:
        if t <= t0:
            continue
        if t > t0 + HOLD:
            break
        last = p
        peak = max(peak, p)
        if p <= e * (1 - sl / 100):
            return -float(sl), t
        if p >= e * (1 + tp / 100):
            return float(tp), t
        if peak > e and p <= peak * (1 - tr / 100):
            return (p / e - 1) * 100, t
    return (last / e - 1) * 100, None


def boot(vals_by_mint, f, n=2000, seed=23):
    keys = list(vals_by_mint)
    if len(keys) < 3:
        return None
    rnd = random.Random(seed)
    out = []
    for _ in range(n):
        xs = [x for k in (rnd.choice(keys) for _ in keys) for x in vals_by_mint[k]]
        if xs:
            out.append(f(xs))
    out.sort()
    return out[int(.05 * len(out))], out[int(.95 * len(out)) - 1]


def main():
    from sniper.services.memecoin_slot_graduation import canonical_pool
    labels = [json.loads(l) for l in open(os.path.join(ROOT, 'data/research/desk-pass-labels.jsonl'))]
    labels = [r for r in labels if r['day'] == DAY]
    desk = _ro(os.path.join(ROOT, 'data/memecoin_desk.db'))
    rails_of = {}
    for oid, oj in desk.execute("SELECT opportunity_id,output_json FROM desk_tickets WHERE stage='vet'"):
        try:
            rails_of[oid] = json.loads(oj)['result'].get('rails') or {}
        except Exception:
            pass
    cur_db = _ro(os.path.join(ROOT, 'data/desk/swaps/%s.db' % DAY))
    ps = _ro(os.path.join(ROOT, 'data/desk/pumpswap/tape.db'))
    ps_end = ps.execute('SELECT MAX(received_at) FROM swaps_ps').fetchone()[0]
    cur_end = cur_db.execute('SELECT MAX(received_at) FROM swaps').fetchone()[0]
    ex = collections.Counter()
    rows = []
    for r in labels:
        if r['ts'] < CAPTURE_START:
            ex['no_pool_tape (before capture start)'] += 1
            continue
        t0 = r['ts'] + LAT
        if cur_end < t0 + HOLD + 8 or ps_end < t0 + HOLD + 8:
            ex['tape_not_yet_complete'] += 1
            continue
        cur = cur_db.execute('SELECT received_at,virtual_quote,virtual_token,real_quote FROM swaps WHERE mint=? AND received_at BETWEEN ? AND ? '
                             'AND virtual_quote>0 AND virtual_token>0 ORDER BY received_at,slot,ordinal', (r['mint'], t0 - 60, t0 + HOLD + 5)).fetchall()
        pre = [x for x in cur if x[0] <= t0]
        if not pre:
            ex['no_curve_state'] += 1
            continue
        F = (pre[-1][3] or 0) / THRESH
        series = [(x[0], (x[1] / 1e9) / (x[2] / 1e6)) for x in cur]
        last = cur[-1]
        grad = bool(last[3] and last[3] >= GRAD_MIN)
        status = 'ok'
        if grad:
            # pool prices come from the desk's PumpSwap reader (memecoin_desk_ps_tape, Beta f0e7d63fe): price = (post_quote +
            # PROVEN virtual quote) / post_base per swap, unproven = None. Dropping the virtual quote understated pool prices by
            # 17-77% on 2026-10-23 (Gamma correction 2026-10-23), so the raw reserve ratio is NOT used here.
            from sniper.services import memecoin_desk_ps_tape as PT
            prow = PT.load_ps_swaps(os.path.join(ROOT, 'data', 'desk'), r['mint'], last[0] - 1, t0 + HOLD + 5, 100.0)
            pp = [(x['received_at'], x['price_sol']) for x in prow if x['price_sol'] is not None]
            if not pp or pp[0][0] - last[0] > FIRST_GAP:
                status = 'censored:no_priced_pool_swap_within_30s'
            else:
                series = [s for s in series if s[0] <= last[0]] + pp
                # a priced gap > 60 s before the rails resolve
                res = rails_sim(series, t0, 18, 30, 12)
                ts_ = [t for t, _ in series]
                end_t = res[1] if res and res[1] else t0 + HOLD
                after = [t for t in ts_ if last[0] <= t <= end_t]
                gaps = [b - a for a, b in zip(after, after[1:])] + ([end_t - after[-1]] if after else [])
                if gaps and max(gaps) > GAP_MAX and not (res and res[1]):
                    status = 'censored:priced_gap_gt_60s'
        rl = rails_of.get(r['oid'], {})
        sim = None if status != 'ok' else rails_sim(series, t0, rl.get('sl_pct', 18), rl.get('tp_pct', 30), rl.get('trail_pct', 12))
        rows.append(dict(oid=r['oid'], mint=r['mint'], F=F, grad=grad, status=status, ret=None if sim is None else sim[0], judge=r['judge']))
    print('DAY %s  labelled tickets %d  graded-or-censored %d  excluded %s' % (DAY, len(labels), len(rows), dict(ex)))

    def table(sel, title):
        ok = [x for x in sel if x['status'] == 'ok' and x['ret'] is not None]
        cens = [x for x in sel if x['status'] != 'ok']
        if not sel:
            print('%-14s n=0' % title)
            return
        byc = collections.defaultdict(list)
        for x in ok:
            byc[x['mint']].append(x['ret'])
        ci = boot(byc, st.mean) if ok else None
        g = [x['ret'] for x in ok if x['grad']]
        ng = [x['ret'] for x in ok if not x['grad']]
        print('%-14s n=%3d graded %3d cens %2d | mean %6.2f CI %s | med %6.2f win %3.0f%% | P(grad) %3.0f%% | grad mean %s (n=%d)  non-grad mean %s (n=%d)' % (
            title, len(sel), len(ok), len(cens), st.mean(x['ret'] for x in ok) if ok else float('nan'),
            tuple(round(c, 1) for c in ci) if ci else None, st.median(x['ret'] for x in ok) if ok else float('nan'),
            100 * sum(x['ret'] > 0 for x in ok) / len(ok) if ok else float('nan'), 100 * sum(x['grad'] for x in sel) / len(sel),
            round(st.mean(g), 1) if g else '-', len(g), round(st.mean(ng), 1) if ng else '-', len(ng)))
    for name, lo, hi in BINS:
        table([x for x in rows if lo <= x['F'] < hi], name)
    print()
    low = [x for x in rows if x['F'] < 0.75]
    high = [x for x in rows if x['F'] >= 0.75]
    table(low, 'F<0.75 (B1-B3)')
    table(high, 'F>=0.75 (B4+B5)')
    okl = [x for x in low if x['status'] == 'ok']
    okh = [x for x in high if x['status'] == 'ok']
    if okl and okh:
        bl, bh = collections.defaultdict(list), collections.defaultdict(list)
        for x in okl:
            bl[x['mint']].append(x['ret'])
        for x in okh:
            bh[x['mint']].append(x['ret'])
        rnd = random.Random(23)
        ml, mh = list(bl), list(bh)
        d = []
        for _ in range(2000):
            a = [v for k in (rnd.choice(ml) for _ in ml) for v in bl[k]]
            b = [v for k in (rnd.choice(mh) for _ in mh) for v in bh[k]]
            d.append(st.mean(b) - st.mean(a))
        d.sort()
        print('(B4+B5) minus (B1-B3): %.2f  90%% CI (%.2f, %.2f)' % (st.mean(x['ret'] for x in okh) - st.mean(x['ret'] for x in okl), d[100], d[1899]))
    print('\nJUDGE inside F>=0.75:')
    for j in ('ENTER', 'SKIP', 'WAIT', 'not_called'):
        table([x for x in high if x['judge'] == j], '  ' + j)
    print('JUDGE inside F<0.75:')
    for j in ('ENTER', 'SKIP'):
        table([x for x in low if x['judge'] == j], '  ' + j)
    print('\ncensor reasons:', dict(collections.Counter(x['status'] for x in rows if x['status'] != 'ok')))


if __name__ == '__main__':
    main()
