#!/usr/bin/env python3
"""Desk PASS-pool label table (read-only). Pre-reg: docs/superpowers/specs/2026-10-23-desk-decision-quality-preregistration.md Part 1.

Reads memecoin_desk.db and the universe tape in mode=ro. Writes one JSONL (labels) and prints R1-R3.
"""
import datetime as dt
import glob
import json
import os
import random
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DESK = os.path.join(ROOT, 'data', 'memecoin_desk.db')
TAPES = os.path.join(ROOT, 'data', 'desk', 'swaps')
OUT = os.path.join(ROOT, 'data', 'research', 'desk-pass-labels.jsonl')
LAT_S, SL, TP, TR = 8.0, 18, 30, 12
GRAD_REAL_QUOTE = 84.0e9       # lamports; curve completes near 85 SOL real quote
_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
ts = lambda s: dt.datetime.fromisoformat(s).timestamp()


_END = {}


def tape_end_of(c):
    if id(c) not in _END:
        _END[id(c)] = c.execute('SELECT MAX(received_at) FROM swaps').fetchone()[0] or 0
    return _END[id(c)]


def tape_rows(conns, mint, lo, hi):
    out = []
    for c in conns:
        out += c.execute('SELECT received_at,virtual_quote,virtual_token,real_quote FROM swaps WHERE mint=? AND received_at BETWEEN ? AND ? '
                         'AND virtual_quote>0 AND virtual_token>0 ORDER BY received_at,slot,ordinal', (mint, lo, hi)).fetchall()
    out.sort(key=lambda r: r[0])
    return [(a, (b / 1e9) / (c / 1e6), d) for a, b, c, d in out]


def label(rows, t_pass, sl, tp, tr, tape_end):
    pre = lambda t: [r for r in rows if r[0] <= t]
    e8_t = t_pass + LAT_S
    p8 = pre(e8_t)
    if not p8:
        return None
    e8 = p8[-1][1]
    post = [r for r in rows if r[0] > e8_t]
    graduated = any(r[2] and r[2] >= GRAD_REAL_QUOTE for r in post)
    # a SILENT coin is not censored: censor only when the tape itself stops before PASS+300 s (tape_end)
    if tape_end < t_pass + 300 + LAT_S:
        return {'censored': True}
    if not post:
        post = [(e8_t + 1, e8, 0)]       # no later swap while the tape was live: price unchanged (dead coin)
    at = lambda s: (pre(e8_t + s) or p8)[-1][1]
    peak, out = e8, None
    for t, p, _ in post:
        if t > e8_t + 300:
            break
        peak = max(peak, p)
        if p <= e8 * (1 - sl / 100):
            out = -float(sl); break
        if p >= e8 * (1 + tp / 100):
            out = float(tp); break
        if peak > e8 and p <= peak * (1 - tr / 100):
            out = (p / e8 - 1) * 100; break
    win = [p for t, p, _ in post if t <= e8_t + 300] or [e8]
    r300 = (at(300) / e8 - 1) * 100
    return {'censored': False, 'graduated': graduated, 'e8': e8,
            'ret60': (at(60) / e8 - 1) * 100, 'ret180': (at(180) / e8 - 1) * 100, 'ret300': r300,
            'maxrun': (max(win) / e8 - 1) * 100, 'maxdd': (min(win) / e8 - 1) * 100,
            'rail': out if out is not None else r300}


def boot(xs, f=st.mean, n=2000, seed=23):
    if len(xs) < 3:
        return None
    rnd = random.Random(seed)
    s = sorted(f([rnd.choice(xs) for _ in xs]) for _ in range(n))
    return s[int(.025 * n)], s[int(.975 * n)]


def main():
    d = _ro(DESK)
    tapes = {os.path.basename(p)[:10]: _ro(p) for p in glob.glob(TAPES + '/*.db')}
    emis = {r[0]: json.loads(r[1]) for r in d.execute('SELECT opportunity_id,candidate_json FROM desk_candidate_emissions')}
    vet = {}
    for oid, v, oj in d.execute("SELECT opportunity_id,verdict,output_json FROM desk_tickets WHERE stage='vet'"):
        try:
            rails = json.loads(oj)['result'].get('rails') or {}
        except Exception:
            rails = {}
        vet[oid] = (v, rails)
    rows, cens = [], 0
    for oid, t, inp, coh, mint in d.execute("SELECT opportunity_id,ts,inputs_json,cohort_id,mint FROM desk_tickets WHERE stage='hard_filter' AND verdict='PASS' ORDER BY ts"):
        tp_ = ts(t)
        day = t[:10]
        nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
        conns = [tapes[k] for k in (day, nxt) if k in tapes]
        if not conns:
            cens += 1; continue
        try:
            pj = json.loads(inp)
        except Exception:
            pj = {}
        v, rails = vet.get(oid, (None, {}))
        lab = label(tape_rows(conns, mint, tp_ - 10, tp_ + 330), tp_,
                    rails.get('sl_pct', SL), rails.get('tp_pct', TP), rails.get('trail_pct', TR),
                    max(tape_end_of(c) for c in conns))
        if lab is None or lab.get('censored'):
            cens += 1; continue
        f = ((emis.get(oid) or {}).get('snapshot') or {}).get('fields') or {}
        val = lambda k: (f.get(k) or {}).get('value') if (f.get(k) or {}).get('completeness') == 'complete' else None
        fl = val('flow_60s') or {}
        chk = (pj.get('evidence') or {}).get('checks') or {}
        rows.append(dict(oid=oid, day=day, ts=tp_, mint=mint, cohort=coh[:18], policy=(pj.get('policy_hash') or '')[:8], judge=v or 'not_called',
                         curve_pct=val('curve_pct'), mcap=val('mcap_usd'), liq=val('liq_usd'), buy_n=fl.get('buy_n'), sell_n=fl.get('sell_n'),
                         makers=fl.get('makers'), largest_buy=fl.get('largest_buy'),
                         bs_ratio=(fl['buy_usd'] / fl['sell_usd']) if fl.get('sell_usd') else None,
                         vs_high=val('price_vs_60s_high_pct'), vs_low=val('price_vs_60s_low_pct'),
                         top10=(chk.get('holders') or {}).get('top10_pct'), creator=(chk.get('holders') or {}).get('creator_pct'),
                         rt_loss=(chk.get('sell_probe') or {}).get('round_trip_loss_pct'), **lab))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as fh:
        for r in rows:
            fh.write(json.dumps(r) + '\n')
    n_all = len(rows) + cens
    print('PASS tickets %d, labelled %d, censored/no-tape %d (%.0f%%)  G1 %s' % (n_all, len(rows), cens, 100 * cens / max(1, n_all),
          'PARTIAL (>25%)' if cens / max(1, n_all) > .25 else 'ok'))
    summ = lambda xs: 'n=%d mean %.2f med %.2f win%% %.0f CI %s' % (len(xs), st.mean(xs), st.median(xs), 100 * sum(x > 0 for x in xs) / len(xs),
                                                                 tuple(round(c, 2) for c in boot(xs)) if boot(xs) else None) if xs else 'n=0'
    print('\nR1 base rate (rail return %, entry = spot at PASS+8s)')
    for key in ('day', 'policy'):
        for k in sorted({r[key] for r in rows}):
            print(' ', key, k, summ([r['rail'] for r in rows if r[key] == k]))
    print('  all', summ([r['rail'] for r in rows]), '| ret@300 mean %.1f | graduated %d' % (st.mean(r['ret300'] for r in rows), sum(r['graduated'] for r in rows)))
    print('  not graduated', summ([r['rail'] for r in rows if not r['graduated']]))
    print('  graduated (outcome = last curve spot, LOWER BOUND/upward-biased: curve ends at its top)', summ([r['rail'] for r in rows if r['graduated']]))
    print('\nR2 JUDGE vs pool')
    g, gn = {}, {}
    for r in rows:
        g.setdefault(r['judge'], []).append(r['rail'])
        if not r['graduated']:
            gn.setdefault(r['judge'], []).append(r['rail'])
    for k, xs in sorted(g.items()):
        print(' ', k, summ(xs), '| not-graduated', summ(gn.get(k, [])))
    if g.get('ENTER') and g.get('SKIP'):
        a, b = g['ENTER'], g['SKIP']
        diff = st.mean(a) - st.mean(b)
        rnd = random.Random(23)
        ds = sorted(st.mean([rnd.choice(a) for _ in a]) - st.mean([rnd.choice(b) for _ in b]) for _ in range(2000))
        print('  ENTER-SKIP diff %.2f CI (%.2f, %.2f)' % (diff, ds[50], ds[1949]))
    print('\nR3 features: tercile mean rail return (low/mid/high) and sign agreement across days')
    for feat in ('curve_pct', 'mcap', 'liq', 'buy_n', 'sell_n', 'makers', 'largest_buy', 'bs_ratio', 'vs_high', 'vs_low', 'top10', 'creator', 'rt_loss'):
        have = [r for r in rows if r[feat] is not None]
        miss = [r['rail'] for r in rows if r[feat] is None]
        if len(have) < 30:
            print(' ', feat, 'n=%d too few' % len(have)); continue
        have.sort(key=lambda r: r[feat])
        k = len(have) // 3
        t = [have[:k], have[k:2 * k], have[2 * k:]]
        m = [st.mean(x['rail'] for x in s) for s in t]
        signs = []
        for day in sorted({r['day'] for r in have}):
            dd = sorted([r for r in have if r['day'] == day], key=lambda r: r[feat])
            kk = len(dd) // 3
            if kk >= 5:
                signs.append(st.mean(x['rail'] for x in dd[2 * kk:]) - st.mean(x['rail'] for x in dd[:kk]))
        rep = 'replicates' if len(signs) == 2 and signs[0] * signs[1] > 0 else 'no/one-day'
        print('  %-11s n=%d  %.1f / %.1f / %.1f  (cuts %.3g..%.3g)  top-bottom by day %s  %s | missing n=%d mean %s' % (
            feat, len(have), m[0], m[1], m[2], have[0][feat], have[-1][feat], [round(s, 1) for s in signs], rep, len(miss),
            round(st.mean(miss), 1) if miss else '-'))


if __name__ == '__main__':
    sys.exit(main())
