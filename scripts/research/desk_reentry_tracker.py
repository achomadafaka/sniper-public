#!/usr/bin/env python3
"""Desk re-entry tracker + the operator's tripwire (read-only). Re-entry = the 2nd+ desk position on one mint.
TRIPWIRE (Alpha/operator 2026-10-23, my definition frozen here): once there are >= 10 re-entries after a LOSING close, if their mean realized USD pnl is
lower than the mean of all FIRST desk entries by more than ONE CLIP-LOSS (= clip x stop pct = 25 x 18% = 4.50 USD), tell Alpha who flips
MEMECOIN_DESK_LOSING_REENTRY_EXEMPT=0. Fewer than 10 = 'insufficient'. Also prints every desk close (descriptive)."""
import collections
import os
import sqlite3
import statistics as st
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLIP_LOSS_USD = 25.0 * 0.18


def main():
    c = sqlite3.connect('file:%s?mode=ro' % os.path.join(ROOT, 'sniper_tracker.db'), uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT id,mint,opened_at,closed_at,entry_price,entry_notional,realized_pnl,exit_reason FROM memecoin_positions "
                     "WHERE signal_source='desk' ORDER BY opened_at").fetchall()
    by = collections.defaultdict(list)
    for r in rows:
        by[r['mint']].append(dict(r))
    firsts = [v[0] for v in by.values() if v[0]['closed_at'] and v[0]['realized_pnl'] is not None]
    print('desk positions %d on %d mints; closed first entries %d (mean realized %.2f USD)' % (
        len(rows), len(by), len(firsts), st.mean(f['realized_pnl'] for f in firsts) if firsts else float('nan')))
    print('\nALL DESK CLOSES (descriptive):')
    for r in rows:
        print('  pos %s %s opened %s closed %s pnl %s exit %s' % (r['id'], r['mint'][:8], str(r['opened_at'])[:19], str(r['closed_at'] or '-')[:19],
              None if r['realized_pnl'] is None else round(r['realized_pnl'], 2), r['exit_reason']))
    re = []
    for mint, ps in by.items():
        for prev, cur in zip(ps, ps[1:]):
            if prev['realized_pnl'] is None:
                continue
            re.append(dict(mint=mint, prior_pnl=prev['realized_pnl'], prior_loss=prev['realized_pnl'] < 0, prior_exit=prev['exit_reason'],
                           entry_vs_prior_entry_pct=(cur['entry_price'] / prev['entry_price'] - 1) * 100 if prev['entry_price'] else None,
                           pnl=cur['realized_pnl'], closed=bool(cur['closed_at']), pos=cur['id']))
    print('\nRE-ENTRIES (2nd+ desk position on a mint): %d' % len(re))
    for x in re:
        print('  pos %s %s after %s close %.2f USD (%s): entry %+.1f%% vs prior entry; result %s' % (
            x['pos'], x['mint'][:8], 'LOSING' if x['prior_loss'] else 'winning/flat', x['prior_pnl'], x['prior_exit'],
            x['entry_vs_prior_entry_pct'] if x['entry_vs_prior_entry_pct'] is not None else float('nan'),
            'open' if not x['closed'] else '%.2f USD' % x['pnl']))
    lost = [x for x in re if x['prior_loss'] and x['closed'] and x['pnl'] is not None]
    print('\nTRIPWIRE: re-entries after a LOSING close, closed: %d (need >= 10)' % len(lost))
    if len(lost) < 10:
        print('  INSUFFICIENT')
    else:
        m_re, m_first = st.mean(x['pnl'] for x in lost), st.mean(f['realized_pnl'] for f in firsts)
        gap = m_first - m_re
        print('  mean re-entry %.2f USD vs mean first entry %.2f USD, shortfall %.2f USD vs one clip-loss %.2f -> %s' % (
            m_re, m_first, gap, CLIP_LOSS_USD, 'TRIPPED: tell Alpha to flip MEMECOIN_DESK_LOSING_REENTRY_EXEMPT=0' if gap > CLIP_LOSS_USD else 'not tripped'))


if __name__ == '__main__':
    main()
