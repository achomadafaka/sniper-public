#!/usr/bin/env python3
"""Band-drift grading (read-only). Pre-registration: docs/audits/2026-10-23-band-drift-grading-preregistration.md (9e70b8bb9).
usage: DESK_PS_TAPE_SRC=<checkout with memecoin_desk_ps_tape> python3 scripts/research/desk_band_drift.py
"""
import datetime as dt
import glob
import json
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import desk_reentry_probe as P  # noqa: E402

COHORT = 'desk-cohort:U:1d7891%'
COST, HORIZON, BAND_LO = 2.5, 300, 30000.0
_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
ts = lambda s: dt.datetime.fromisoformat(s).timestamp()


def boot_ci(vals_by_mint, fn, draws=2000, seed=23):
    rnd = random.Random(seed)
    mints = list(vals_by_mint)
    out = []
    for _ in range(draws):
        s = [v for m in (rnd.choice(mints) for _ in mints) for v in vals_by_mint[m]]
        out.append(fn(s))
    out.sort()
    return out[int(.05 * draws)], out[int(.95 * draws) - 1]


def grade(mint, t0, dbs, desk_dir, tape_end):
    rows, grad, pool_ok = P.combined(mint, t0 - 5, t0 + HORIZON + 5, dbs, desk_dir)
    series = [(t, p) for t, p, *_ in rows]
    if t0 + HORIZON > tape_end:
        return None
    r = P.run_rails(series, t0, 18, 30, 12, HORIZON)
    if r is None:
        return None
    e = [p for t, p in series if t <= t0][-1]
    post = [p for t, p in series if t0 < t <= t0 + HORIZON]
    return {'ret': r[0] - COST, 'typ': r[1], 'grad': grad, 'pool_ok': pool_ok,
            'hi': (max(post) / e - 1) * 100 if post else 0.0,
            'end': ((post[-1] / e - 1) * 100) if post else None}


def main():
    desk_dir = os.path.join(ROOT, 'data', 'desk')
    paths = sorted(glob.glob(desk_dir + '/swaps/*.db'))
    dbs = [c for c in (_ro(p) for p in paths)
           if 'real_quote' in [r[1] for r in c.execute('PRAGMA table_info(swaps)')]]     # older day files lack the column
    tape_end = max(c.execute('SELECT max(received_at) FROM swaps').fetchone()[0] for c in dbs)
    desk = _ro(os.path.join(ROOT, 'data/memecoin_desk.db'))
    q = ("SELECT c.ticket_id,t.mint,c.claimed_at,c.response_at,c.result_json,v.verdict FROM desk_judge_calls c "
         "JOIN desk_tickets t ON t.ticket_id=c.ticket_id AND t.stage='hard_filter' "
         "JOIN desk_tickets v ON v.ticket_id=c.ticket_id AND v.stage='vet' WHERE t.cohort_id LIKE ? ORDER BY c.claimed_at")
    groups = {'band_below': [], 'model_skip': [], 'enter': []}
    for tid, mint, cl, rs, rj, verdict in desk.execute(q, (COHORT,)):
        res = json.loads(rj or '{}')
        err = res.get('call_error')
        if err == 'band_below':
            g = 'band_below'
        elif err is None and verdict == 'SKIP':
            g = 'model_skip'
        elif err is None and verdict == 'ENTER':
            g = 'enter'
        else:
            continue
        emi = desk.execute("SELECT candidate_json FROM desk_candidate_emissions WHERE ticket_id=?", (tid,)).fetchone()
        m_emit = None
        if emi:
            try:
                m_emit = json.loads(emi[0])['snapshot']['fields']['mcap_usd']['value']
            except (KeyError, TypeError, ValueError):
                pass
        ir = res.get('inline_rank') or {}
        m_claim = ir.get('mcap_usd')
        if g != 'band_below':
            try:
                m_claim = res['asof']['candidate']['snapshot']['fields']['mcap_usd']['value']
            except (KeyError, TypeError):
                pass
        t_c = ts(cl)
        gr = grade(mint, t_c, dbs, desk_dir, tape_end)
        sec = grade(mint, ts(rs), dbs, desk_dir, tape_end) if rs and g != 'band_below' else None
        groups[g].append({'tid': tid, 'mint': mint, 'claim': cl, 'm_emit': m_emit, 'm_claim': m_claim, 'since_pass_s': res.get('since_pass_s'),
                          'g': gr, 'g_resp': sec})
    out = {'tape_end': tape_end, 'groups': {}}
    for g, rs_ in groups.items():
        ok = [r for r in rs_ if r['g']]
        by = {}
        for r in ok:
            by.setdefault(r['mint'], []).append(r['g']['ret'])
        mean = lambda s: sum(s) / len(s)
        rets = [v for vs in by.values() for v in vs]
        st = {'n': len(rs_), 'graded': len(ok), 'censored_or_no_tape': len(rs_) - len(ok)}
        if rets:
            lo, hi = boot_ci(by, mean)
            srt = sorted(rets)
            st.update(mean=round(mean(rets), 2), ci90=[round(lo, 2), round(hi, 2)], median=round(srt[len(srt) // 2], 2),
                      tp=sum(r['g']['typ'] == 'tp' for r in ok), stop=sum(r['g']['typ'] == 'stop' for r in ok),
                      trail=sum(r['g']['typ'] == 'trail' for r in ok), timecap=sum(r['g']['typ'] == 'timecap' for r in ok),
                      end_gt10=sum(1 for r in ok if (r['g']['end'] or -99) > 10), grad=sum(bool(r['g']['grad']) for r in ok))
        out['groups'][g] = st
        out['groups'][g + '_rows'] = [{k: v for k, v in r.items() if k != 'g_resp'} for r in rs_]
    # pre-stated comparison: band_below minus model_skip, mint-cluster bootstrap
    bb = {}
    ms = {}
    for src, dst in ((groups['band_below'], bb), (groups['model_skip'], ms)):
        for r in src:
            if r['g']:
                dst.setdefault(r['mint'], []).append(r['g']['ret'])
    if bb and ms:
        rnd = random.Random(23)
        diffs = []
        for _ in range(2000):
            a = [v for m in (rnd.choice(list(bb)) for _ in bb) for v in bb[m]]
            b = [v for m in (rnd.choice(list(ms)) for _ in ms) for v in ms[m]]
            diffs.append(sum(a) / len(a) - sum(b) / len(b))
        diffs.sort()
        out['diff_bb_minus_ms'] = {'lo90': round(diffs[100], 2), 'hi90': round(diffs[1899], 2),
                                   'point': round(sum(v for vs in bb.values() for v in vs) / sum(len(v) for v in bb.values())
                                                  - sum(v for vs in ms.values() for v in vs) / sum(len(v) for v in ms.values()), 2)}
    # drift descriptives and round-trip share (band_below only)
    bbr = [r for r in groups['band_below'] if r['g']]
    drift = [(r['m_claim'] / r['m_emit'] - 1) * 100 for r in bbr if r['m_emit'] and r['m_claim']]
    out['band_below_drift_pct'] = {'n': len(drift), 'mean': round(sum(drift) / len(drift), 2) if drift else None,
                                   'min': round(min(drift), 2) if drift else None, 'max': round(max(drift), 2) if drift else None}
    out['band_below_since_pass'] = {'lt1s': sum(1 for r in bbr if (r['since_pass_s'] or 0) < 1), 'ge1s': sum(1 for r in bbr if (r['since_pass_s'] or 0) >= 1)}
    out['band_below_reenter_band_300s'] = sum(1 for r in bbr if r['m_claim'] and r['m_claim'] * (1 + r['g']['hi'] / 100) >= BAND_LO)
    print(json.dumps({k: v for k, v in out.items() if not k.endswith('_rows')}, indent=1))
    json.dump(out, open(os.path.join(ROOT, 'data/research/desk-band-drift.json'), 'w'), indent=1, default=str)


if __name__ == '__main__':
    main()
