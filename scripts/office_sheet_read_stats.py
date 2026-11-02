#!/usr/bin/env python3
"""Sheet reads by state, per distinct ticket (first read) with the ticket's age at the read in bins. Read-only on office.db and the desk db.

Needs the ticket id on the tool_call event (recorded from the seat_tools change that adds refs.ticket_id / refs.sheet_state); older events lack it and are counted as `no_ticket_id`,
never attributed. Age = read time (corrected_at) minus the ticket's emitted_at; the two clocks differ by the host clock offset, so ages are approximate to that offset.
Usage: office_sheet_read_stats.py [--office PATH] [--desk PATH] [--since EPOCH]"""
import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINS = ((60, 'under_60s'), (300, '1_5min'), (3600, '5_60min'), (float('inf'), 'over_1h'))


def _bin(age):
    if age is None: return 'age_unknown'
    return next(name for limit, name in BINS if age < limit)


def stats(office, desk, since=0.0):
    per_state, no_id, ambiguous, first = Counter(), 0, 0, {}
    pending = defaultdict(list)              # actor -> [[call time, ticket id, overlapped, call id, seq]] in call order
    for seq, actor, at, kind, refs in office.execute("SELECT seq,actor_id,corrected_at,kind,refs_json FROM office_v2_events WHERE kind IN ('tool_call','tool_result') AND corrected_at>=? ORDER BY seq", (since,)):
        try: r = json.loads(refs) if refs else {}
        except ValueError: continue
        if r.get('tool') != 'sheet_read': continue
        if kind == 'tool_call': pending[actor].append([at, r.get('ticket_id'), False, r.get('call_id'), seq]); continue
        state = r.get('sheet_state', 'unreadable')
        per_state[state] += 1
        calls, entry = pending[actor], None
        if r.get('call_id') is not None:     # exact pair by the unique call id stamped on both events
            i = next((k for k, c in enumerate(calls) if c[3] == r['call_id']), None)
            entry = calls.pop(i) if i is not None else None
        else:                                # legacy results: by ticket id when it names exactly one pending call, else by the single pending call, else explicitly ambiguous
            tid = r.get('ticket_id')
            same = [k for k, c in enumerate(calls) if tid is not None and c[1] == tid]
            if len(same) == 1 and not calls[same[0]][2]: entry = calls.pop(same[0])
            elif tid is None and len(calls) == 1 and not calls[0][2]: entry = calls.pop()
            else:
                ambiguous += 1
                for c in calls: c[2] = True      # every call that overlapped stays unattributable, also when it is the last one left
                if same: calls.pop(same[0])
                elif calls: calls.pop(0)
                continue
        if entry is None or entry[1] is None: no_id += 1; continue
        tid = entry[1]
        key = (entry[0], entry[4])           # the FIRST read of a ticket is the earliest CALL (time, then sequence), not the first to complete
        if tid not in first or key < first[tid][3]: first[tid] = (entry[0], state, r.get('sheet_class') or r.get('sheet_reason'), key)
    ages = defaultdict(Counter); why = Counter()
    for tid, (at, state, reason, _key) in first.items():
        row = desk.execute("SELECT min(emitted_at) FROM desk_tickets WHERE ticket_id=?", (tid,)).fetchone() if desk is not None else None
        try: emitted = datetime.fromisoformat(row[0]).timestamp() if row and row[0] else None
        except ValueError: emitted = None
        ages[_bin(at - emitted if emitted is not None else None)][state] += 1
        if state != 'known': why[(state, reason)] += 1
    return dict(reads_by_state=dict(per_state), reads_without_ticket_id=no_id, ambiguous_reads=ambiguous, calls_without_result=sum(len(v) for v in pending.values()), distinct_tickets=len(first),
                first_read_by_age_bin={b: dict(c) for b, c in ages.items()}, first_read_failures_by_reason={'%s:%s' % k: v for k, v in why.most_common()})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--office', type=Path, default=ROOT / 'data/office/office.db')
    ap.add_argument('--desk', type=Path, default=ROOT / 'data/memecoin_desk.db')
    ap.add_argument('--since', type=float, default=0.0)
    a = ap.parse_args(argv)
    o = sqlite3.connect('file:%s?mode=ro' % a.office, uri=True, timeout=30)
    d = sqlite3.connect('file:%s?mode=ro' % a.desk, uri=True, timeout=30)
    print(json.dumps(stats(o, d, a.since)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
