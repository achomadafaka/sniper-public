#!/usr/bin/env python3
"""Grade a JUDGE candidate replay under Part 2 of docs/superpowers/specs/2026-10-23-desk-decision-quality-preregistration.md.

usage: desk_judge_candidate_grade.py REPLAY.jsonl CANDIDATE_FIELD [NULL_FIELD]
Rows need ticket_id, live_text and the candidate text (str, or dict with 'text'). NULL_FIELD (default 'on') is
the same-settings rerun that gives the noise floor. Labels: data/research/desk-pass-labels.jsonl (Part 1).
Read-only. Verdict letter = JSON key 'v'; unparseable text is a VOID verdict, reported first.
"""
import ast
import json
import math
import os
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LABELS = os.path.join(ROOT, 'data', 'research', 'desk-pass-labels.jsonl')


def text(x):
    if isinstance(x, str):
        try:
            y = ast.literal_eval(x)
            x = y if isinstance(y, dict) else x
        except (ValueError, SyntaxError):
            pass
    return x.get('text') if isinstance(x, dict) else x


def verdict(t, defence=False):
    if defence and isinstance(t, str):
        t = t.strip().strip('`').strip()
        if t.startswith('json\n'):
            t = t[5:]
    try:
        return json.loads(t)['v']
    except Exception:
        return None            # VOID


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(100 * (c - h)), round(100 * (c + h))


def main():
    path, cand = sys.argv[1], sys.argv[2]
    null = sys.argv[3] if len(sys.argv) > 3 else 'on'
    lab = {}
    for l in open(LABELS):
        r = json.loads(l)
        lab[r['oid']] = r
    rows = [json.loads(l) for l in open(path)]
    n = len(rows)
    live = [verdict(r['live_text']) for r in rows]
    c_raw = [verdict(text(r.get(cand))) for r in rows]
    c = [verdict(text(r.get(cand)), defence=True) for r in rows]
    void_raw = sum(x is None for x in c_raw)
    void = sum(x is None for x in c)
    print('n=%d  candidate VOID as strict JSON: %d (%.0f%%); after stripping fences/backticks: %d (%.0f%%)  [as-is the live parser would SKIP the first set]' % (
        n, void_raw, 100 * void_raw / n, void, 100 * void / n))
    has_null = any(null in r for r in rows)
    dis_c = sum(a != b for a, b in zip(live, c))
    if has_null:
        nl = [verdict(text(r.get(null))) for r in rows]
        dis_null = sum(a != b for a, b in zip(live, nl))
        print('NOISE FLOOR  live vs %s rerun: %d/%d = %.1f%%  Wilson95 %s' % (null, dis_null, n, 100 * dis_null / n, wilson(dis_null, n)))
    else:
        dis_null = 5          # fall back on the 40-sheet same-settings measurement (live vs rerun: 5/40)
        print('NOISE FLOOR  field %r absent: using the measured same-settings 5/40 = 12.5%%  Wilson95 %s' % (null, wilson(5, 40)))
    print('CANDIDATE    live vs %s: %d/%d = %.1f%%  Wilson95 %s' % (cand, dis_c, n, 100 * dis_c / n, wilson(dis_c, n)))
    lo_c = wilson(dis_c, n)[0]
    hi_n = wilson(dis_null, 40 if not has_null else n)[1]
    print('  candidate DIFFERENT from noise only if its CI lower bound (%d%%) > noise CI upper bound (%d%%): %s' % (lo_c, hi_n, 'YES' if lo_c > hi_n else 'NO (indistinguishable at this n)'))
    enter_live = {i for i, v in enumerate(live) if v == 'E'}
    enter_c = {i for i, v in enumerate(c) if v == 'E'}
    both = len(enter_live & enter_c)
    print('ENTER: live %d, candidate %d, both %d, ENTER agreement (both/live) %s' % (
        len(enter_live), len(enter_c), both, '%.0f%%' % (100 * both / len(enter_live)) if enter_live else 'n/a'))
    rail = lambda i: lab[rows[i]['ticket_id']]['rail'] if rows[i]['ticket_id'] in lab else None
    groups = {'candidate-only ENTER': enter_c - enter_live, 'live-only ENTER': enter_live - enter_c, 'both ENTER': enter_live & enter_c}
    for k, idx in groups.items():
        x = [rail(i) for i in idx if rail(i) is not None]
        print('  %-21s n=%d  mean rail %s' % (k, len(x), '%.1f' % st.mean(x) if x else '-'))
    for nm, idx in (('candidate ENTER', enter_c), ('live ENTER', enter_live)):
        x = [rail(i) for i in idx if rail(i) is not None]
        print('  %-21s n=%d  mean rail %s' % (nm, len(x), '%.1f' % st.mean(x) if x else '-'))
    print('NOTE: n this small cannot show improvement; promotion needs a NEW cohort and the Part 2 no-harm rule on >=150 sheets.')


if __name__ == '__main__':
    main()
