#!/usr/bin/env python3
"""Desk re-entry measurement (read-only). Pre-reg: docs/superpowers/specs/2026-10-23-desk-reentry-preregistration.md (547ae6c1b).
usage: DESK_PS_TAPE_SRC=<checkout with memecoin_desk_ps_tape> python3 scripts/research/desk_reentry_probe.py
"""
import bisect
import collections
import datetime as dt
import glob
import json
import os
import random
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
if os.environ.get('DESK_PS_TAPE_SRC'):
    sys.path.insert(0, os.environ['DESK_PS_TAPE_SRC'])
LAT, HOLD, POST = 8, 300, 600
CAPTURE_START = 1792738622.0
GRAD_MIN = 84.0e9
RT_COST = float(os.environ.get('REENTRY_RT_COST', '2.5'))
_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
ts = lambda s: dt.datetime.fromisoformat(s).timestamp()


def combined(mint, lo, hi, curdbs, desk):
    """[(t, price, is_buy, sol, actor)] curve + (if graduated) priced pool swaps; graduated flag; pool_ok flag."""
    rows = []
    last_rq = 0
    for c in curdbs:
        for t, vq, vt, rq, ib, q, a in c.execute('SELECT received_at,virtual_quote,virtual_token,real_quote,is_buy,quote_atomic,actor FROM swaps WHERE mint=? '
                                                 'AND received_at BETWEEN ? AND ? AND virtual_quote>0 AND virtual_token>0 ORDER BY received_at,slot,ordinal', (mint, lo, hi)):
            rows.append((t, (vq / 1e9) / (vt / 1e6), int(ib), q / 1e9, a))
            last_rq = max(last_rq, rq or 0)
    grad = last_rq >= GRAD_MIN
    pool_ok = None
    if grad:
        from sniper.services import memecoin_desk_ps_tape as PT
        lastc = rows[-1][0] if rows else lo
        pr = PT.load_ps_swaps(desk, mint, lastc - 1, hi, 1.0)          # sol_usd 1.0: 'usd' is SOL
        pp = [(x['received_at'], x['price_sol'], x['is_buy'], x['usd'] or 0.0, x['actor']) for x in pr if x['price_sol'] is not None]
        pool_ok = bool(pp) and pp[0][0] - lastc <= 30.0
        rows = [r for r in rows if r[0] <= lastc] + pp
    rows.sort(key=lambda r: r[0])
    return rows, grad, pool_ok


def run_rails(series, t0, sl, tp, tr, horizon):
    """series [(t, price)]; entry at the last price <= t0. Returns (ret%, type, exit_t, exit_price)."""
    pre = [p for t, p in series if t <= t0]
    if not pre:
        return None
    e = pre[-1]
    peak, last_t, last_p = e, t0, e
    for t, p in series:
        if t <= t0:
            continue
        if t > t0 + horizon:
            break
        last_t, last_p = t, p
        peak = max(peak, p)
        if p <= e * (1 - sl / 100):
            return -float(sl), 'stop', t, p
        if p >= e * (1 + tp / 100):
            return float(tp), 'tp', t, p
        if peak > e and p <= peak * (1 - tr / 100):
            return (p / e - 1) * 100, 'trail', t, p
    return (last_p / e - 1) * 100, 'timecap', last_t, last_p


def signals(rows, exit_t, exit_p, end_t):
    """First R_struct and R_reclaim signal times (None if none)."""
    post = [r for r in rows if exit_t < r[0] <= end_t]
    ex_sec = int(exit_t)
    s_struct = s_reclaim = None
    below = False
    times = [r[0] for r in rows]
    for t, p, ib, sol, a in post:
        # reclaim
        if p < exit_p:
            below = True
        if s_reclaim is None and below and p >= exit_p * 1.02:
            s_reclaim = t
        if s_struct is None and p >= exit_p * 0.97:
            i = bisect.bisect_left(times, t - 60)
            j = bisect.bisect_left(times, t)
            prev = [r[1] for r in rows[i:j] if int(r[0]) != ex_sec]
            if prev and p > max(prev):
                k = bisect.bisect_left(times, t - 10)
                win = rows[k:bisect.bisect_right(times, t)]
                buy = sum(r[3] for r in win if r[2]); sell = sum(r[3] for r in win if not r[2])
                buyers = len({r[4] for r in win if r[2]})
                if buy >= 2 * sell and buyers >= 3:
                    s_struct = t
        if s_struct is not None and s_reclaim is not None:
            break
    return s_struct, s_reclaim


def fill_after(rows, t_sig, end_t):
    for r in rows:
        if r[0] >= t_sig + 2 and r[0] <= end_t:
            return r[0]
    return None


def boot_mean(by_mint, n=2000, seed=23):
    keys = list(by_mint)
    if len(keys) < 3:
        return None
    rnd = random.Random(seed)
    out = sorted(st.mean([v for k in (rnd.choice(keys) for _ in keys) for v in by_mint[k]]) for _ in range(n))
    return round(out[int(.05 * n)], 1), round(out[int(.95 * n) - 1], 1)


def main():
    desk_dir = os.path.join(ROOT, 'data', 'desk')
    labels = [json.loads(l) for l in open(os.path.join(ROOT, 'data/research/desk-pass-labels.jsonl'))]
    dbs = {os.path.basename(p)[:10]: _ro(p) for p in glob.glob(desk_dir + '/swaps/*.db')}
    ends = {d: (c.execute('SELECT MAX(received_at) FROM swaps').fetchone()[0] or 0) for d, c in dbs.items()}
    desk = _ro(os.path.join(ROOT, 'data/memecoin_desk.db'))
    rails_of = {}
    for oid, oj in desk.execute("SELECT opportunity_id,output_json FROM desk_tickets WHERE stage='vet'"):
        try:
            rails_of[oid] = json.loads(oj)['result'].get('rails') or {}
        except Exception:
            pass
    recs, cens = [], collections.Counter()
    rnd = random.Random(23)
    for r in labels:
        t0 = r['ts'] + LAT
        d = dt.datetime.fromtimestamp(t0, dt.timezone.utc).date()
        curdbs = [dbs[k] for k in (d.isoformat(), (d + dt.timedelta(days=1)).isoformat()) if k in dbs]
        if not curdbs:
            cens['no_tape'] += 1
            continue
        tape_end = max(ends[k] for k in (d.isoformat(), (d + dt.timedelta(days=1)).isoformat()) if k in ends)
        if tape_end < t0 + HOLD + POST + 10:
            cens['tape_end'] += 1
            continue
        rows, grad, pool_ok = combined(r['mint'], t0 - 60, t0 + HOLD + POST + 5, curdbs, desk_dir)
        if grad and (r['ts'] < CAPTURE_START or not pool_ok):
            cens['graduated_no_pool_path'] += 1
            continue
        series = [(t, p) for t, p, *_ in rows]
        rl = rails_of.get(r['oid'], {})
        sl, tp, tr = rl.get('sl_pct', 18), rl.get('tp_pct', 30), rl.get('trail_pct', 12)
        first = run_rails(series, t0, sl, tp, tr, HOLD)
        if first is None:
            cens['no_entry_print'] += 1
            continue
        ret1, typ, xt, xp = first
        end_t = xt + POST
        post = [(t, p) for t, p in series if xt < t <= end_t]   # a SILENT coin (no post prints while the tape was live) is flat, not censored
        reach = {X: next((t - xt for t, p in post if p >= xp * (1 + X / 100)), None) for X in (10, 20, 30)}
        # sensitivity (disclosed in the report, not a rule change): the exit print can be a one-print wick; re-base on the first print >= 2 s after
        lat = next(((t, p) for t, p in post if t >= xt + 2), None)
        reach2 = {X: (next((t - lat[0] for t, p in post if t > lat[0] and p >= lat[1] * (1 + X / 100)), None) if lat else None) for X in (10, 20, 30)}
        down = next((t - xt for t, p in post if p <= xp * 0.90), None)
        ss, sr = signals(rows, xt, xp, end_t)
        rec = dict(oid=r['oid'], mint=r['mint'], typ=typ, ret1=ret1, grad=grad, reach=reach, reach2=reach2, down=down, ret_struct=None, ret_reclaim=None, ctrl=None)
        for name, sig in (('ret_struct', ss), ('ret_reclaim', sr)):
            if sig is None:
                continue
            f = fill_after(rows, sig, end_t)
            if f is None:
                continue
            res = run_rails(series, f - 1e-6, sl, tp, tr, min(HOLD, end_t - f))
            if res:
                rec[name] = res[0]
        # random control: same coin, signal replaced by a random second in the window
        cs = []
        for _ in range(100):
            sig = xt + rnd.uniform(1, POST - 20)
            f = fill_after(rows, sig, end_t)
            if f is None:
                continue
            res = run_rails(series, f - 1e-6, sl, tp, tr, min(HOLD, end_t - f))
            if res:
                cs.append(res[0])
        rec['ctrl'] = st.mean(cs) if cs else None
        recs.append(rec)
    print('exits with a complete 600 s post window: %d   censored: %s' % (len(recs), dict(cens)))
    print('RT cost subtracted for net: %.1f pp\n' % RT_COST)
    print('%-8s %-6s %4s | reach +10/+20/+30 within 600 s [latency-rebased +10/+30] | median t(+10) | falls -10%% first | struct sig n  gross  net  CI(net)       win%% | reclaim sig n  gross  net | ctrl' % ('type', 'grad', 'n'))
    strata = collections.defaultdict(list)
    for x in recs:
        strata[(x['typ'], 'grad' if x['grad'] else 'curve')].append(x)
        strata[(x['typ'], 'ALL')].append(x)
    for key in sorted(strata):
        xs = strata[key]
        n = len(xs)
        sh = lambda X: 100 * sum(1 for x in xs if x['reach'][X] is not None) / n
        t10 = [x['reach'][10] for x in xs if x['reach'][10] is not None]
        dn = 100 * sum(1 for x in xs if x['down'] is not None and (x['reach'][10] is None or x['down'] < x['reach'][10])) / n
        s = [x for x in xs if x['ret_struct'] is not None]
        rc = [x for x in xs if x['ret_reclaim'] is not None]
        byc = collections.defaultdict(list)
        for x in s:
            byc[x['mint']].append(x['ret_struct'] - RT_COST)
        ci = boot_mean(byc)
        ctrl = [x['ctrl'] for x in xs if x['ctrl'] is not None]
        sh2 = lambda X: 100 * sum(1 for x in xs if x['reach2'][X] is not None) / n
        print('%-8s %-6s %4d | %3.0f%% / %3.0f%% / %3.0f%% [%3.0f%% / %3.0f%%] | %s | %3.0f%% | %3d %6.1f %6.1f %s %3.0f%% | %3d %6.1f %6.1f | %s' % (
            key[0], key[1], n, sh(10), sh(20), sh(30), sh2(10), sh2(30), ('%.0fs' % st.median(t10)) if t10 else '-', dn,
            len(s), st.mean(x['ret_struct'] for x in s) if s else float('nan'), st.mean(x['ret_struct'] - RT_COST for x in s) if s else float('nan'),
            ci, 100 * sum(1 for x in s if x['ret_struct'] - RT_COST > 0) / len(s) if s else float('nan'),
            len(rc), st.mean(x['ret_reclaim'] for x in rc) if rc else float('nan'), st.mean(x['ret_reclaim'] - RT_COST for x in rc) if rc else float('nan'),
            ('%.1f' % (st.mean(ctrl) - RT_COST)) if ctrl else '-'))
    # first-entry reference
    print('\nfirst-entry mean ret by exit type (gross, same rails):', {k[0]: round(st.mean(x['ret1'] for x in v), 1) for k, v in strata.items() if k[1] == 'ALL'})
    json.dump(recs, open(os.path.join(ROOT, 'data/research/desk-reentry-records.json'), 'w'))


if __name__ == '__main__':
    main()
