#!/usr/bin/env python3
"""Gamma's explore-1 report lines, read-only, counts only. Never writes, never opens a funded DB.
Usage: python3 -I scripts/research/explore_gamma_lines.py [--root data/office]
Lines: wakes and tokens per seat and segment, cache read per wake, priced-mark share per order, exit overshoot per rail exit."""
import argparse, collections, json, sqlite3, statistics, sys

def ro(path):
    return sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=5)

def segments(office):
    """[(segment, at)] for release rows, ascending. A wake belongs to the latest release segment at or before its ended_at."""
    return office.execute("SELECT segment,at FROM office_v2_changes WHERE kind='release' ORDER BY at").fetchall()

def seg_of(rel, t):
    cur = None
    for seg, at in rel:
        if at <= t: cur = seg
    return cur

def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else None

def wake_lines(office, quota):
    rel = segments(office)
    roles = dict(office.execute("SELECT actor_id,role FROM office_v2_roster"))
    by = collections.defaultdict(lambda: dict(n=0, known=0, counted=0, cache=0, bad=0, ts=[]))
    for actor, ended, state, pj in quota.execute("SELECT actor_id,ended_at,state,payload_json FROM quota_wakes ORDER BY ended_at"):
        p = json.loads(pj); ct = p.get('counters') or {}
        d = by[(roles.get(actor, actor[:8]), seg_of(rel, ended))]
        d['n'] += 1; d['ts'].append(ended)
        vals = [ct.get(k) for k in ('input_tokens', 'output_tokens', 'cache_creation_input_tokens', 'cache_read_input_tokens')]
        if not all(type(v) in (int, float) for v in vals): d['bad'] += 1; continue
        d['known'] += 1; d['counted'] += vals[0] + vals[1] + vals[2]; d['cache'] += vals[3]
    for (role, seg), d in sorted(by.items(), key=lambda kv: (kv[0][1] or 0, kv[0][0])):
        gaps = [b - a for a, b in zip(d['ts'], d['ts'][1:])]
        k = d['known'] or None
        print('wakes role=%s seg=%s n=%d counters_known=%d unknown=%d counted_per_wake=%s cache_read_per_wake=%s gap_p50_s=%s gap_p10_s=%s' % (
            role, seg, d['n'], d['known'], d['bad'], round(d['counted'] / k) if k else None, round(d['cache'] / k) if k else None,
            round(statistics.median(gaps)) if gaps else None, round(pct(gaps, 0.1)) if gaps else None))

def mark_lines(ledger):
    """Rail-facing priced share = marks at or before the exit request (or all marks while open). Nulls written after the request are the exit FILL waiting for the frontier (fill wait), counted apart. null_n counts watcher ticks, so a collapsed run weighs tick rate, not market time: both rows and ticks are printed. mark_path on null rows is a hardcoded label and is not used."""
    for oid, state, er, ca in ledger.execute("SELECT order_id,state,exit_requested_at,closed_at FROM orders"):
        rows = ledger.execute("SELECT at,lamports,binding_kind,null_n,reason,null_last_at FROM marks WHERE order_id=? ORDER BY at", (oid,)).fetchall()
        rail = [r for r in rows if er is None or r[0] <= er]
        wait = [r for r in rows if er is not None and r[0] > er]
        priced = [r for r in rail if r[1] is not None]
        nulls = [r for r in rail if r[1] is None]
        kinds = {}
        for r in priced: kinds[r[2]] = kinds.get(r[2], 0) + 1
        ticks_null = sum((r[3] or 1) for r in nulls)
        reasons = {}
        for r in nulls: reasons[r[4]] = reasons.get(r[4], 0) + (r[3] or 1)
        # blind seconds: what the rails care about. From fill to exit request (or last mark while open).
        fa = ledger.execute("SELECT filled_at FROM orders WHERE order_id=?", (oid,)).fetchone()[0]
        end = er if er is not None else (rail[-1][0] if rail else None)
        held = round(end - fa, 1) if fa and end else None
        null_run_s = round(sum(max(0.0, (r[5] - r[0])) for r in nulls if r[5] is not None), 1)
        longest_run = round(max([max(0.0, r[5] - r[0]) for r in nulls if r[5] is not None] or [0.0]), 1)
        times = ([fa] if fa else []) + [r[0] for r in priced] + ([end] if end else [])
        gaps = [b - a for a, b in zip(times, times[1:])]
        print('blind order=%s held_s=%s null_run_s=%s longest_null_run_s=%s priced_gap_s_p50=%s p90=%s max=%s (gaps include fill->first priced and last priced->request)' % (
            oid[:8], held, null_run_s, longest_run, round(statistics.median(gaps), 1) if gaps else None, round(pct(gaps, 0.9), 1) if gaps else None, round(max(gaps), 1) if gaps else None))
        print('marks order=%s state=%s rail_rows=%d priced_rows=%d null_rows=%d priced_share_rows=%s null_ticks=%d kinds=%s null_reasons_ticks=%s fill_wait_rows=%d fill_wait_ticks=%d request_to_fill_s=%s' % (
            oid[:8], state, len(rail), len(priced), len(nulls), round(len(priced) / len(rail), 3) if rail else None, ticks_null, kinds, reasons,
            len(wait), sum((r[3] or 1) for r in wait), round(ca - er, 1) if er and ca else None))

def _num(x):
    try: return float(x)
    except (TypeError, ValueError): return None

def overshoot_lines(ledger):
    """Rail exit overshoot = realised exit return minus the rail level's return (negative = filled below the level). Rails from the order's frozen policy_json."""
    for oid, spend, proceeds, peak, reason, pol in ledger.execute("SELECT order_id,spend_lamports,proceeds_lamports,peak_lamports,exit_reason,policy_json FROM orders WHERE state='closed'"):
        spend, proceeds, peak = _num(spend), _num(proceeds), _num(peak)
        if not spend or proceeds is None: print('overshoot order=%s unknown (no spend or proceeds)' % oid[:8]); continue
        p = json.loads(pol) if pol else {}
        level = None
        if reason == 'trail' and peak and _num(p.get('trail_pct')) is not None and peak > spend: level = peak * (1 - p['trail_pct'] / 100)
        elif reason in ('stop', 'sl') and _num(p.get('sl_pct')) is not None: level = spend * (1 - p['sl_pct'] / 100)
        elif reason in ('tp', 'take_profit') and _num(p.get('tp_pct')) is not None: level = spend * (1 + p['tp_pct'] / 100)
        elif reason == 'catastrophe' and _num(p.get('catastrophe_pct')) is not None: level = spend * (1 - p['catastrophe_pct'] / 100)
        ret = proceeds / spend - 1
        if level is None: print('overshoot order=%s exit_reason=%s realised_return=%.4f rail_level=not_a_rail_exit_or_unknown' % (oid[:8], reason, ret)); continue
        print('overshoot order=%s exit_reason=%s realised_return=%.4f rail_level_return=%.4f overshoot=%.4f' % (oid[:8], reason, ret, level / spend - 1, ret - (level / spend - 1)))

def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--root', default='data/office'); a = ap.parse_args()
    office, quota, ledger = ro(a.root + '/office.db'), ro(a.root + '/v2/quota.db'), ro(a.root + '/v2/explore-ledger/ledger.db')
    wake_lines(office, quota); mark_lines(ledger); overshoot_lines(ledger)

if __name__ == '__main__':
    main()
