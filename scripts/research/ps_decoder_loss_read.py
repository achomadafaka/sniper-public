#!/usr/bin/env python3
"""PumpSwap decoder-loss read (Gamma). Binds docs/audits/2026-10-28-ps-decoder-loss-read-definition.md
sections 1-3 and Amendments 1, 2, 4. Read-only; run with `nice -n 19`. No outcome column is joined.

Not computed here (stated, not hidden): section 4 stored-sheet share (Amendment 3: no paired before),
pool mix of receipts by pool (only the swap-volume top-10 share is printed).

  nice -n 19 python3 scripts/research/ps_decoder_loss_read.py \
      --boundary-start 1793184602.8783588 --before-from 1793163600 [--later-boundary EPOCH ...]
  python3 scripts/research/ps_decoder_loss_read.py --self-check
"""
import argparse
import bisect
import json
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from sniper.services import memecoin_desk_ps_losses as L  # noqa: E402  (validators are the JUDGE path's own)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ps_tape_reader_census import tape_read  # noqa: E402  (WAL guard and closed per-read connection)

DB = ROOT / 'data/desk/pumpswap/tape.db'
CAUSES = L.DECODE_CAUSES
Q = ('SELECT l.signature,l.cause,l.slot,l.received_at,'
     'a.reason,a.scope,a.pools_json,a.invocations,a.discriminator,a.event_length,a.idl_sha256,a.received_at,a.slot,a.protocol,'
     'e.kind,e.profile,e.bits,e.key_count,e.idl_sha256,e.received_at,e.known_at,e.slot,e.protocol,e.proof_sha256,'
     's.pool_keys_json,s.complete,s.proof,s.decoder_version,s.known_at '
     'FROM ps_decode_losses l '
     'LEFT JOIN ps_loss_attribution a ON a.signature=l.signature AND a.cause=l.cause '
     'LEFT JOIN ps_loss_event_envelopes e ON e.signature=l.signature AND e.cause=l.cause '
     'LEFT JOIN ps_loss_scope s ON s.signature=l.signature AND s.loss_key=l.cause '
     'WHERE l.received_at>=? AND l.received_at<?')


def classify(r):
    """-> (class, reason). S scoped, E envelope, R typed read-error, U unknown. Same validators as the JUDGE path."""
    sig, cause, slot, at = r[0:4]
    a, e, s = r[4:14], r[14:24], r[24:29]
    try:
        scope = L._attribution_scope(a, at, slot) if a[0] is not None else None
        routed = None
        if s[0] is not None:
            orig = (a[6], a[7], a[8], a[9]) if a[0] is not None else None
            routed, _ = L._pool_key_scope(s[0], sig, cause, at, slot, orig, (s[1], s[2], s[3], s[4]))
        if scope is not None and routed is not None and scope != routed:
            raise L.LossUnreadable('loss:conflicting_pool_keys')
        env = None
        if e[0] is not None:
            original = (a[3], a[7], a[8], a[6], a[9]) if a[0] is not None else None
            env = L._validated_envelope(e[:10], original, sig, cause, at, slot)
    except L.LossUnreadable as x:
        return 'R', str(x)
    if scope is not None or routed is not None:
        return 'S', ''
    return ('E', '') if env is not None else ('U', '')


def cover(ivs, lo, hi):
    """Seconds of [lo,hi) covered by the union of (start,through) intervals."""
    tot, cur = 0.0, lo
    for s, t in sorted(ivs):
        s, t = max(s, cur), min(t, hi)
        if t > s:
            tot += t - s
            cur = t
    return tot


def label_for(sid, start, a):
    m = a.session_map.get(sid)
    if m:
        return m
    if start >= a.boundary_start:
        later = [b for b in sorted(a.later_boundary) if start >= b]
        return 'after_g8' if not later else 'after_later%d' % len(later)
    return 'before' if start >= a.before_from else 'legacy'


def read(a):
    # One closed connection per read: no tape.db snapshot outlives a statement (Alpha 2026-10-31, WAL pin).
    ivs = tape_read(DB, 'SELECT session,start_at,through_at FROM ps_capture_intervals ORDER BY start_at')
    lab = {}
    for sid, st, _ in ivs:
        lab.setdefault(sid, label_for(sid, st, a))
    lo_t, hi_t = ivs[0][1], max(t for _, _, t in ivs)
    starts = [s for _, s, _ in ivs]
    meta = {'read_at': time.time(), 'counts': {t: tape_read(DB, 'SELECT count(*) FROM ' + t)[0][0] for t in
            ('ps_decode_losses', 'ps_loss_attribution', 'ps_loss_scope', 'ps_loss_event_envelopes', 'gaps_ps')}}
    swaps = dict(tape_read(DB, 'SELECT hour,sum(swaps) FROM ps_pool_hours GROUP BY hour'))
    mixed_days = {int(b // 86400) for b in a.later_boundary}
    hours = {}
    h0, h1 = int(lo_t // 3600) * 3600, int(hi_t // 3600) * 3600
    for h in range(h0, h1 + 1, 3600):
        t0, t1 = h, h + 3600
        touch = [(sid, s, t) for sid, s, t in ivs if s < t1 and t > t0]
        sess = {sid for sid, _, _ in touch}
        cov = cover([(s, t) for _, s, t in touch], t0, t1) / 3600
        rec = {'hour': h, 'coverage': round(cov, 4), 'sessions': len(sess), 'excluded': None,
               'label': lab[next(iter(sess))] if len(sess) == 1 else None}
        if len(sess) != 1:
            rec['excluded'] = 'session_switch' if sess else 'no_session'
        elif cov < 0.95:
            rec['excluded'] = 'low_coverage'
        elif int(h // 86400) in mixed_days:
            rec['excluded'] = 'mixed_day'
        if rec['excluded'] is None:
            n = Counter()
            amb = out = 0
            reasons = Counter()
            read_started = time.monotonic()
            rows = tape_read(DB, Q, (t0, t1))
            time.sleep(time.monotonic() - read_started)      # at most half duty, so the checkpoint gets gaps
            for r in rows:
                at = r[3]
                i = bisect.bisect_right(starts, at)
                hit = {sid for sid, s, t in ivs[:i] if s <= at <= t}
                if not hit:
                    out += 1
                    continue
                if len(hit) > 1:
                    amb += 1
                    continue
                k, why = classify(r)
                n[(k, r[1])] += 1
                if why:
                    reasons[why] += 1
            tot = sum(n.values()) + amb + out
            if tot and amb / tot > 0.01:
                rec['excluded'] = 'ambiguous_session_gt1pct'
            sw = swaps.get(h, 0)
            rec.update(n={f'{k}:{ca}': v for (k, ca), v in n.items()}, ambiguous=amb, outside_capture=out,
                       reasons=dict(reasons), swaps=sw)
            R = {k: sum(v for (kk, _), v in n.items() if kk == k) for k in 'SERU'}
            rec.update(S=R['S'], E=R['E'], R=R['R'], U=R['U'])
            allr = sum(R.values())
            if sw and not rec['excluded']:
                rec['u_h'] = 1000 * R['U'] / sw
                rec['r_h'] = 1000 * allr / sw
                rec['u_share'] = R['U'] / allr if allr else None
        hours[h] = rec
    gaps = defaultdict(int)
    for (at,) in tape_read(DB, 'SELECT at FROM gaps_ps'):
        gaps[int(at // 3600) * 3600] += 1
    for h, rec in hours.items():
        rec['G'] = gaps.get(h, 0)
    return hours, meta, lab


def side_cells(hours, label, ndays, first):
    days = sorted({h // 86400 for h, r in hours.items() if r['label'] == label and 'u_h' in r})
    days = days[:ndays] if first else days[-ndays:]
    cells = defaultdict(list)
    for h, r in hours.items():
        if r['label'] == label and 'u_h' in r and h // 86400 in days:
            cells[(h // 3600) % 24].append(r)
    return cells, days


def verdict(hours, afters):
    before, bdays = side_cells(hours, 'before', 3, False)
    out = {'before_days': bdays, 'strata': {}}
    for lb in afters:
        after, adays = side_cells(hours, lb, 3, True)
        paired = sorted(set(before) & set(after))
        med = lambda cs, k, x: statistics.median(r[k] for r in cs[x] if r.get(k) is not None)  # noqa: E731
        rows = []
        for x in paired:
            rows.append({'hod': x, 'u_before': med(before, 'u_h', x), 'u_after': med(after, 'u_h', x),
                         'r_before': med(before, 'r_h', x), 'r_after': med(after, 'r_h', x)})
        v = {'after_days': adays, 'paired_cells': len(rows), 'cells': rows}
        if rows:
            ur = [r['u_after'] / r['u_before'] for r in rows if r['u_before'] > 0]
            rr = [r['r_after'] / r['r_before'] for r in rows if r['r_before'] > 0]
            lower = sum(r['u_after'] < r['u_before'] for r in rows) / len(rows)
            sb = [r['u_share'] for xs in before.values() for r in xs if r.get('u_share') is not None]
            sa = [r['u_share'] for xs in after.values() for r in xs if r.get('u_share') is not None]
            v.update(median_u_ratio=statistics.median(ur) if ur else None, share_cells_lower=lower,
                     median_r_ratio=statistics.median(rr) if rr else None,
                     median_u_share=(statistics.median(sb) if sb else None, statistics.median(sa) if sa else None))
            ok = (v['median_u_ratio'] is not None and v['median_u_ratio'] <= 0.5 and lower >= 0.9
                  and v['median_r_ratio'] is not None and v['median_r_ratio'] >= 0.8 and sb and sa
                  and statistics.median(sa) < statistics.median(sb))
            v['call'] = 'reduced' if ok else 'not shown'
            if not ok and v['median_u_ratio'] is not None and v['median_u_ratio'] <= 0.5 and (v['median_r_ratio'] or 0) < 0.8:
                v['call'] = 'not shown (r_h fell: possible silent drop)'
        else:
            v['call'] = 'not shown (no paired cells)'
        out['strata'][lb] = v
    return out


def self_check():
    # cover(): union, not sum
    assert cover([(0, 10), (5, 20), (30, 40)], 0, 100) == 30
    assert cover([(0, 10)], 5, 8) == 3
    # verdict(): halved u with flat r -> reduced; halved u with halved r -> silent drop, not reduced
    def hr(h, lab, u, r, share):
        return {'hour': h, 'label': lab, 'u_h': u, 'r_h': r, 'u_share': share, 'excluded': None}
    base = 86400 * 10
    hs = {}
    for d in range(3):
        for x in range(4):
            hs[base + d * 86400 + x * 3600] = hr(0, 'before', 10.0, 20.0, 0.5)
            hs[base + (5 + d) * 86400 + x * 3600] = hr(0, 'after_g8', 4.0, 20.0, 0.2)
    assert verdict(hs, ['after_g8'])['strata']['after_g8']['call'] == 'reduced'
    for h in hs:
        if hs[h]['label'] == 'after_g8':
            hs[h]['r_h'] = 8.0
    assert verdict(hs, ['after_g8'])['strata']['after_g8']['call'].startswith('not shown (r_h fell')
    # classify(): garbage attribution row is typed R, not U
    row = ('sig', 'decode_loss', 5, 100.0) + ('r', 'complete', '[]', 1, 'ab', None, 'x', 100.0, 5, 1) + (None,) * 10 + (None,) * 5
    assert classify(row)[0] == 'R', classify(row)
    print('self-check ok')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--boundary-start', type=float, default=1793184602.8783588)
    p.add_argument('--before-from', type=float, default=1793163600.0, help='first host second of post-e2270598f decoder sessions (~05:00Z 10-28)')
    p.add_argument('--later-boundary', type=float, action='append', default=[], help='start_at of a later decoder session (Amendment 4)')
    p.add_argument('--session-map', default=None, help='JSON {session_id: label} from Alpha episodes; overrides time rule')
    p.add_argument('--self-check', action='store_true')
    p.add_argument('--data-root', default=str(ROOT), help='checkout holding data/ (a worktree has none)')
    a = p.parse_args()
    global DB
    DB = Path(a.data_root) / 'data/desk/pumpswap/tape.db'
    if a.self_check:
        return self_check()
    a.session_map = json.load(open(a.session_map)) if a.session_map else {}
    hours, meta, lab = read(a)
    print('# ps_decoder_loss_read', json.dumps(meta), 'labels:', json.dumps(dict(Counter(lab.values()))))
    print('# default side labels come from start_at vs --before-from/--boundary-start, NOT Alpha release episodes; pass --session-map to bind')
    print('hour_utc label cov excl S E R U G swaps u_h r_h u_share reasons')
    for h in sorted(hours):
        r = hours[h]
        if 'n' not in r and not r['excluded']:
            continue
        print(time.strftime('%m-%dT%H', time.gmtime(h)), r['label'], r['coverage'], r['excluded'] or '-', r.get('S', ''), r.get('E', ''),
              r.get('R', ''), r.get('U', ''), r['G'], r.get('swaps', ''), *(('%.3f' % r[k]) if r.get(k) is not None else '-' for k in ('u_h', 'r_h', 'u_share')),
              json.dumps(r.get('reasons', {})))
    after = sorted({v for v in lab.values() if v.startswith('after')})
    print(json.dumps(verdict(hours, after), indent=1, default=str))


if __name__ == '__main__':
    main()
