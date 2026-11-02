#!/usr/bin/env python3
"""Sweep closure measurement (Gamma's frozen selection, read-only on the PumpSwap tape). Descriptive; it changes nothing and exempts nothing.

Population: gaps that contain at least one recorded WSOL-quote sweep event (ps_sweep_events) and have both neighbouring stored prints, ordered by the sweep event's received_at ascending from
the first row the recorder wrote; the first LIMIT distinct (pool, prev, next) gaps are measured. Excluded gaps are listed with counts and reasons and do not count toward the LIMIT.
Gap (slot bracket, A): prev = last stored print with slot < sweep slot, next = first stored print with slot > sweep slot, by chain order (slot, tx_index, ordinal). Closure is exact integer equality:
  next.pre_quote - prev.post_quote  ==  sum(stored swap quote deltas in the gap slots) - sum(recorded sweep amounts of the pool in the gap)
  next.pre_base  - prev.post_base   ==  sum(stored swap base deltas in the gap slots)
Virtual-quote equality across the gap is NOT computable from the tape (the pre-trade virtual quote is not stored): reported as unchecked, never as closed.
Reads one snapshot (BEGIN) and matches pool membership in ps_loss_attribution.pools_json exactly; a NULL discriminator or unreadable scope excludes the gap. The output reports NUMERIC closure only; it never prints anything that reads as an R7 pass (`accounting_closure_500_of_500` is necessary, not sufficient).
Usage: office_sweep_closure.py [--tape PATH] [--limit 500]"""
import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WSOL = 'So11111111111111111111111111111111111111112'
SWEEP_DISC = '82a42461e48287a5'
COLS = 'slot,tx_index,ordinal,pre_base_reserve_atomic,pre_quote_reserve_atomic,post_base_reserve_atomic,post_quote_reserve_atomic,received_at,signature'


def ro(path):
    return sqlite3.connect('file:%s?mode=ro' % path, uri=True, timeout=30)


def _chain_key(r):
    return (r[0], -1 if r[1] is None else r[1], r[2])


def _neighbour(rows, last):
    """The chain-last (or first) of the rows in ONE slot, or None when the order is unknown (more than one print and a NULL tx_index among them)."""
    if len(rows) > 1 and any(r[1] is None for r in rows): return 'order_unknown'
    return (max if last else min)(rows, key=_chain_key)


def _losses(c, pool, lo, hi):
    """Complete-scope losses naming THIS pool (exact JSON membership, never a substring) in slots lo..hi, as (signature, discriminator). None when a scope row cannot be read: the gap is then excluded."""
    out = []
    for sig, disc, pj in c.execute("SELECT signature,discriminator,pools_json FROM ps_loss_attribution WHERE scope='complete' AND slot BETWEEN ? AND ?", (lo, hi)):
        try: pools = json.loads(pj)
        except (TypeError, ValueError): return None
        if not isinstance(pools, list): return None
        if pool in pools: out.append((sig, disc))
    return out


def measure(tape, limit=500):
    c = ro(tape) if not isinstance(tape, sqlite3.Connection) else tape
    c.execute('BEGIN')                                  # one read snapshot: a live tape keeps moving while the gaps are measured
    try:
        return _measure(c, limit)
    finally:
        c.rollback()                                    # release the snapshot on an exception too (a caller-held connection must not stay pinned)


def _measure(c, limit):
    events = c.execute("SELECT signature,ordinal,pool,quote_mint,amount,slot,received_at FROM ps_sweep_events ORDER BY received_at,signature,ordinal").fetchall()
    gaps, seen, excluded, result = [], set(), Counter(), []
    capture_points = [r[0] for r in c.execute("SELECT at FROM gaps_ps WHERE cause IS NULL OR cause NOT IN ('decode_loss','virtual_quote_unproven')")]      # an unknown (NULL) cause is a capture point, not absence
    spans = c.execute("SELECT start_at,end_at FROM ps_gap_spans").fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE name='ps_gap_spans'").fetchone() else []
    sessions = c.execute("SELECT start_at,through_at FROM ps_capture_intervals ORDER BY start_at").fetchall() if c.execute("SELECT 1 FROM sqlite_master WHERE name='ps_capture_intervals'").fetchone() else []
    holes = [(a[1], b[0]) for a, b in zip(sessions, sessions[1:]) if a[1] is not None and b[0] > a[1]]
    for sig, ordinal, pool, qmint, amount, slot, rcv in events:
        if len(gaps) >= limit: break
        if qmint != WSOL: excluded['not_wsol_quote'] += 1; continue
        if slot is None: excluded['sweep_slot_unknown'] += 1; continue
        before = c.execute("SELECT %s FROM swaps_ps WHERE pool=? AND slot=(SELECT max(slot) FROM swaps_ps WHERE pool=? AND slot<?)" % COLS, (pool, pool, slot)).fetchall()
        after = c.execute("SELECT %s FROM swaps_ps WHERE pool=? AND slot=(SELECT min(slot) FROM swaps_ps WHERE pool=? AND slot>?)" % COLS, (pool, pool, slot)).fetchall()
        if not before or not after: excluded['missing_neighbour'] += 1; continue
        prev, nxt = _neighbour(before, True), _neighbour(after, False)
        if prev == 'order_unknown' or nxt == 'order_unknown': excluded['neighbour_slot_order_unknown'] += 1; continue
        key = (pool, prev[0], prev[1], prev[2], nxt[0], nxt[1], nxt[2])
        if key in seen: continue
        seen.add(key)
        lo, hi = prev[0], nxt[0]
        t_lo, t_hi = prev[7], nxt[7]
        if c.execute("SELECT count(*) FROM ps_loss_attribution WHERE scope IS NULL AND slot BETWEEN ? AND ?", (lo, hi)).fetchone()[0]: excluded['loss_scope_null'] += 1; continue      # an unclassified loss is unknown
        pool_losses = _losses(c, pool, lo, hi)
        if pool_losses is None: excluded['loss_scope_unreadable'] += 1; continue
        if any(d is None for _, d in pool_losses): excluded['R7e_unknown_loss_kind'] += 1; continue          # a NULL discriminator is unknown, never "not another kind"
        if any(d != SWEEP_DISC for _, d in pool_losses): excluded['R7e_other_loss_kind'] += 1; continue
        if any(t_lo <= p <= t_hi for p in capture_points) or any(b > t_lo and a <= t_hi for a, b in spans) or any(b > t_lo and a <= t_hi for a, b in holes):
            excluded['R7e_capture_stop_or_point'] += 1; continue
        if prev[5] is None or prev[6] is None: excluded['prev_post_unproven'] += 1; continue
        inside = c.execute("SELECT %s FROM swaps_ps WHERE pool=? AND slot>? AND slot<?" % COLS, (pool, lo, hi)).fetchall()
        if any(r[5] is None or r[6] is None for r in inside): excluded['in_gap_swap_post_unproven'] += 1; continue
        ev = c.execute("SELECT signature,ordinal,amount,slot FROM ps_sweep_events WHERE pool=? AND slot>=? AND slot<=?", (pool, lo, hi)).fetchall()
        if any(e[3] in (lo, hi) for e in ev): excluded['sweep_in_neighbour_slot'] += 1; continue
        sweep_losses = len({sg for sg, d in pool_losses if d == SWEEP_DISC})
        recorded_txs = len({e[0] for e in ev})
        unknown_scope = c.execute("SELECT count(*) FROM ps_loss_attribution WHERE scope!='complete' AND slot BETWEEN ? AND ?", (lo, hi)).fetchone()[0]
        gaps.append(dict(key=key, prev=prev, nxt=nxt, inside=inside, ev=ev, sweep_loss_txs=sweep_losses, recorded_txs=recorded_txs, unknown_scope_losses=unknown_scope))
    closed, residuals, unrecorded = 0, [], 0
    for g in gaps:
        prev, nxt = g['prev'], g['nxt']
        if g['recorded_txs'] < g['sweep_loss_txs']:            # a sweep loss with no recorded event: closure fails by definition
            unrecorded += 1; residuals.append(dict(key=list(g['key']), reason='sweep_event_unrecorded', recorded_txs=g['recorded_txs'], sweep_loss_txs=g['sweep_loss_txs'])); continue
        d_q = sum(int(r[6]) - int(r[4]) for r in g['inside']) - sum(int(e[2]) for e in g['ev'])
        d_b = sum(int(r[5]) - int(r[3]) for r in g['inside'])
        rq, rb = (int(nxt[4]) - int(prev[6])) - d_q, (int(nxt[3]) - int(prev[5])) - d_b
        if rq == 0 and rb == 0: closed += 1
        else: residuals.append(dict(key=list(g['key']), quote_residual=rq, base_residual=rb, in_gap_swaps=len(g['inside']), sweep_events=len(g['ev']), unknown_scope_losses=g['unknown_scope_losses']))
    return dict(selected=len(gaps), limit=limit, closed=closed, residuals=residuals, sweep_event_unrecorded=unrecorded, excluded=dict(excluded),
                accounting_closure_500_of_500=(len(gaps) == 500 and closed == 500 and limit == 500),         # exact quote and base equality over the frozen 500: NECESSARY for R7, not sufficient
                validation_note=('limit_below_500, not a validation' if limit < 500 else 'frozen validation size'),
                virtual_quote_continuity='unchecked, not stored',
                use_gate='a separate reviewed outcome-rule change (with the unchecked label and the completeness conditions) is required; nothing here grants an exemption',
                r7_not_ready_because=['virtual quote continuity unchecked (pre-trade virtual quote not stored)',
                                      'event-set completeness unknown (recorded transactions are counted, the expected number of events per transaction is not stored; a missing event inside a recorded transaction, or offsetting amounts, can still close)',
                                      'the outcome rule (ok_swept) is not built or reviewed'],
                unknown_scope_losses_in_selected_gaps=sum(g['unknown_scope_losses'] for g in gaps))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tape', type=Path, default=ROOT / 'data/desk/pumpswap/tape.db')
    ap.add_argument('--limit', type=int, default=500)
    a = ap.parse_args(argv)
    print(json.dumps(measure(a.tape, a.limit)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
