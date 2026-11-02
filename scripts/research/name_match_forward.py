#!/usr/bin/env python3
"""Trump name-match forward count (Gamma). Binds docs/audits/2026-10-31-trump-name-match-forward-preregistration.md.
Read-only, report only, no seat action. Below n=30 usable rows on the match side a horizon prints counts and statuses only.

  nice -n 19 python3 scripts/research/name_match_forward.py
  python3 scripts/research/name_match_forward.py --self-check
"""
import argparse
import json
import re
import sqlite3
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc).timestamp()
SUBSTRINGS = ('trump', 'melania', 'barron', 'ivanka', 'greatagain')   # inside compacted text, so HALLOWTRUMP matches
TOKENS = {'maga', 'mtga', 'djt', 'potus', 'donald', 'jared', 'kushner', 'vance'}
USABLE = ('ok', 'ok_unchecked')
MIN_N = 30


def matches(name, symbol):
    """True or False from the frozen list; None when both texts are absent (name_unknown, never non-matching)."""
    texts = [t for t in (name, symbol) if isinstance(t, str) and t.strip()]
    if not texts:
        return None
    for t in texts:
        low = t.lower()
        compact = re.sub(r'[^a-z0-9]', '', low)
        if any(s in compact for s in SUBSTRINGS) or TOKENS & set(re.findall(r'[a-z0-9]+', low)):
            return True
    return False


def ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)


def collect(root):
    tickets = {}
    with ro(root / 'data/memecoin_desk.db') as d:
        for mint, ts in d.execute("SELECT mint,ts FROM desk_tickets WHERE stage='hard_filter' AND producer='U' ORDER BY id"):
            t = datetime.fromisoformat(ts).timestamp()
            if t >= T0:
                tickets.setdefault(mint, t)
    names = {}
    with ro(root / 'data/desk/intake/pumpfun/first_sightings.db') as f:
        for mint, raw in f.execute('SELECT mint,raw_json FROM first_sightings WHERE observed_at>=?', (T0 - 86400,)):
            if mint in tickets and mint not in names:
                try:
                    r = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                names[mint] = (r.get('name'), r.get('symbol'))
    out = {}
    with ro(root / 'data/office/outcomes.db') as o:
        for mint, h, status, ret, col in o.execute('SELECT mint,horizon_s,status,ret,collapsed FROM outcome_horizons WHERE horizon_s IN (60,300)'):
            if mint in tickets:
                out[(mint, h)] = (status, ret, col)
    return tickets, names, out


def report(tickets, names, out):
    side = {m: matches(*names.get(m, (None, None))) for m in tickets}
    print('tickets since T0', len(tickets), 'by side', dict(Counter(str(v) for v in side.values())))
    hours = {int(tickets[m] // 3600) for m, v in side.items() if v}
    groups = {'match': [m for m, v in side.items() if v],
              'nonmatch_same_hours': [m for m, v in side.items() if v is False and int(tickets[m] // 3600) in hours]}
    for h in (60, 300):
        usable_match = sum(1 for m in groups['match'] if (out.get((m, h)) or ('',))[0] in USABLE)
        for g, ms in groups.items():
            rows = [out.get((m, h)) for m in ms]
            print(h, g, 'n', len(ms), 'status', dict(Counter(r[0] if r else 'immature' for r in rows)))
            use = [r for r in rows if r and r[0] in USABLE]
            if usable_match >= MIN_N and use:
                rets = [r[1] for r in use if r[1] is not None]
                cols = [r[2] for r in use if r[2] is not None]
                print('   usable', len(use), 'median ret', round(statistics.median(rets), 4) if rets else None,
                      'collapsed', sum(cols), 'of', len(cols))
        if usable_match < MIN_N:
            print('   usable match rows', usable_match, 'below', MIN_N, ': counts only')


def self_check():
    assert matches('HALLOWTRUMP', None) is True and matches(None, 'MTGA') is True
    assert matches('Make Toilets Great Again', 'X') is True and matches('Trumpet', None) is True
    assert matches('magazine', 'MAGZ') is False and matches('Dog', 'DOG') is False
    assert matches(None, '  ') is None
    print('self-check ok')


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--self-check', action='store_true')
    a = p.parse_args()
    if a.self_check:
        self_check()
    else:
        report(*collect(ROOT))
