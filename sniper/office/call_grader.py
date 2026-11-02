"""Office per-call grader: one Office paper order -> one descriptive row (direction, entry path, exit) for the lessons loop.
Pure over ledger rows; grade_closed reads a READ-ONLY ledger handle and writes nothing. Descriptive only: no label thresholds, no pass, no verdict input.
Unknown is typed: no priced marks / no peak / zero spend => None for that field, never 0. An order without its entry+exit cashflows is censored, never a loss.
net_usd is the book truth: the sum of the order's two cashflow rows (fill and exit costs already inside). Marks are only the path, so entry-path numbers are partial by design."""
import hashlib
import json
import math

CONTRACT = 'office-call-grade-v1'


def _finite(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _lamports(v):
    return int(v) if isinstance(v, str) and v.isdigit() and int(v) > 0 else None


def grade_call(order, cashflows, marks):
    base = dict(order_id=order['order_id'], contract=CONTRACT)
    if order['state'] == 'censored':
        return dict(base, state='censored', reason=order.get('reason') or 'censored')
    if order['state'] != 'closed':
        return dict(base, state='pending')
    by_kind = {c['kind']: c['usd'] for c in cashflows}
    if len(cashflows) != 2 or set(by_kind) != {'entry', 'exit'} or not all(_finite(v) for v in by_kind.values()):
        return dict(base, state='censored', reason='cashflow_incomplete')
    net = by_kind['entry'] + by_kind['exit']
    spend, peak, proceeds = (_lamports(order.get(k)) for k in ('spend_lamports', 'peak_lamports', 'proceeds_lamports'))
    window = [m for m in marks if _finite(m['at']) and order['filled_at'] <= m['at'] <= order['closed_at']]
    priced = [v for v in (_lamports(m['lamports']) for m in window) if v is not None]
    return dict(base, state='graded', epoch_id=order['epoch_id'], coin_id='%s:%s' % (order['network_id'], order['mint']),
                net_usd=net, direction='win' if net > 0 else 'loss' if net < 0 else 'flat',
                mae_pct=(min(priced) / spend - 1) * 100 if priced and spend else None,
                mfe_pct=(peak / spend - 1) * 100 if peak and spend else None,
                priced_marks=len(priced), unpriced_marks=len(window) - len(priced),
                capture=proceeds / peak if proceeds and peak else None,
                hold_s=order['closed_at'] - order['filled_at'], exit_reason=order.get('exit_reason'),
                exit_by=order.get('exit_requested_by'))


def grade_closed(conn, *, epoch_id, after=None, limit=None):
    """Grade every order of an epoch (open ones come back pending, so the denominator is visible). conn: ledger.open_readonly.
    Cursor: after=(requested_at, order_id) of the last row already read; limit=max rows. Each row carries its own `cursor`."""
    rows = []
    q, args = 'SELECT * FROM orders WHERE epoch_id=?', [epoch_id]
    if after is not None:
        q += ' AND (requested_at, order_id) > (?, ?)'
        args += [after[0], after[1]]
    q += ' ORDER BY requested_at, order_id'
    if limit is not None:
        q += ' LIMIT ?'
        args.append(int(limit))
    for o in conn.execute(q, args).fetchall():
        o = dict(o)
        flows = [dict(r) for r in conn.execute('SELECT kind,usd FROM cashflows WHERE order_id=?', (o['order_id'],))]
        marks = [dict(r) for r in conn.execute('SELECT at,lamports FROM marks WHERE order_id=? ORDER BY at', (o['order_id'],))]
        g = grade_call(o, flows, marks)
        g['cursor'] = [o['requested_at'], o['order_id']]
        g['input_hash'] = hashlib.sha256(json.dumps([o, flows, marks], sort_keys=True, default=str).encode()).hexdigest()
        rows.append(g)
    return rows
