"""Liquidity change flag on feed tickets (seat request 22511). Reported, never a gate: feed and order code do not branch on it.

For each listed ticket: real-quote liquidity at the ticket's emission against the newest PumpSwap tape row of its pool, with both reserves, slots and ages. Both sides are one-sided real quote in USD.
material is True on a fall of MATERIAL_DROP_PCT or more. material False needs both sides known. Any missing side is state unknown, material null: absent is never no change.
The newest row can be old on a quiet pool; current_age_s says how old. Tape rows carry losses the same as any tape read: this does not prove no later swap was lost."""
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

MATERIAL_DROP_PCT = 50.0


def _good(x):
    return type(x) in (int, float) and math.isfinite(x) and x > 0


def _sol(desk_dir, now):
    try:
        beat = json.loads((Path(desk_dir) / 'universe.heartbeat').read_text())
    except (OSError, ValueError):
        return None, None
    sol, age, ts = beat.get('sol_usd'), beat.get('sol_age_s'), beat.get('ts')
    if not _good(sol) or type(age) not in (int, float) or not math.isfinite(age) or not _good(ts):
        return None, None
    return sol, age + max(0.0, now - ts)


def one(ticket, tape, sol, sol_age, now):
    cand = (ticket or {}).get('candidate') or {}
    fields = (cand.get('snapshot') or {}).get('fields') or {}
    was = fields.get('liq_usd') or {}
    state = (fields.get('pool_state') or {}).get('value') or {}
    pool = cand.get('pool')
    out = dict(state='unknown', material=None, basis='real_quote_one_side', drop_threshold_pct=MATERIAL_DROP_PCT, previous_liq_usd=was.get('value') if _good(was.get('value')) else None,
               previous_quote_reserve_atomic=state.get('quote_reserve_atomic'), previous_slot=was.get('slot'), previous_age_s=now - was['known_at'] if _good(was.get('known_at')) else None,
               current_liq_usd=None, current_quote_reserve_atomic=None, current_slot=None, current_age_s=None, sol_age_s=sol_age, change_pct=None)
    if not isinstance(pool, str):
        return dict(out, reason='no_pool')
    if tape is None:
        return dict(out, reason='tape_unreadable')
    try:
        row = tape.execute('SELECT post_quote_reserve_atomic,slot,received_at FROM swaps_ps WHERE pool=? AND received_at<=? ORDER BY received_at DESC LIMIT 1', (pool, now)).fetchone()
    except sqlite3.Error:
        return dict(out, reason='tape_unreadable')
    if row is None:
        return dict(out, reason='no_tape_row')
    if row[0] is None:
        return dict(out, reason='newest_reserve_missing', current_slot=row[1], current_age_s=now - row[2])     # an older row would hide what the newest swap did
    try:
        quote = int(row[0])
        if quote < 0: raise ValueError
    except (TypeError, ValueError):
        return dict(out, reason='reserve_unreadable')
    out.update(current_quote_reserve_atomic=row[0], current_slot=row[1], current_age_s=now - row[2])
    if sol is None:
        return dict(out, reason='sol_unknown')
    current = quote / 1e9 * sol
    out['current_liq_usd'] = current
    if out['previous_liq_usd'] is None:
        return dict(out, reason='no_previous_liquidity')
    change = (current / out['previous_liq_usd'] - 1.0) * 100.0
    return dict(out, state='known', change_pct=change, material=change <= -MATERIAL_DROP_PCT)


def annotate(items, *, get_ticket, desk_dir, now):
    """Adds liquidity_change to each item in place. Total: one ticket's failure stays on that item, and a failed annotation never removes the list."""
    tape = None
    try:
        tape = sqlite3.connect((Path(desk_dir) / 'pumpswap' / 'tape.db').resolve().as_uri() + '?mode=ro', uri=True, timeout=1.0)
    except (sqlite3.Error, OSError):
        tape = None
    sol, sol_age = _sol(desk_dir, now)
    with closing(tape) if tape is not None else _Null():
        for item in items:
            try:
                item['liquidity_change'] = one(get_ticket(item['ticket_id']), tape, sol, sol_age, now)
            except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
                item['liquidity_change'] = dict(state='unknown', material=None, reason='ticket_unreadable')
    return items


def report(feed, *, get_ticket, desk_dir, now):
    """-> {state, basis, drop_threshold_pct, counts, items}. Material and unknown items first, then the rest, each with its ticket id. A failed feed read stays unknown."""
    items = feed.get('items') if isinstance(feed, dict) else None
    if not isinstance(items, list) or feed.get('state') not in ('known', 'partial'):
        return dict(state='unknown', reason=(feed or {}).get('reason', 'feed_unknown') if isinstance(feed, dict) else 'feed_unknown', items=[])
    rows = [dict(ticket_id=i.get('ticket_id'), feed=i.get('feed')) for i in items]
    annotate(rows, get_ticket=get_ticket, desk_dir=desk_dir, now=now)
    keep = []
    for row in rows:
        change = {k: round(v, 3) if isinstance(v, float) else v for k, v in row.pop('liquidity_change').items() if k not in ('basis', 'drop_threshold_pct') and v is not None}
        keep.append(dict(row, **change))
    rank = lambda r: 0 if r.get('material') is True else 1 if r.get('state') == 'unknown' else 2
    keep.sort(key=rank)
    return dict(state=feed['state'], as_of=now, basis='real_quote_one_side', drop_threshold_pct=MATERIAL_DROP_PCT, note='A null field is left out: not known, never zero. Reported only.',
                material=sum(r.get('material') is True for r in keep), unknown=sum(r.get('state') == 'unknown' for r in keep), not_material=sum(r.get('material') is False for r in keep), items=keep)


class _Null:
    def __enter__(self): return None
    def __exit__(self, *a): return False
