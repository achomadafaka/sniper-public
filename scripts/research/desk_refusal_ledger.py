#!/usr/bin/env python3
"""Desk verdict-price refusal ledger (read-only). Spec: Part 3 of docs/superpowers/specs/2026-10-23-desk-decision-quality-preregistration.md.
Every desk attempt refused with 'desk verdict price' is graded FROM THE REFUSED QUOTE under the verdict's rails; a coin that graduates
continues on its canonical pool with the proven virtual quote (memecoin_desk_ps_tape). Spot only (no impact/fees beyond the quote itself).
usage: DESK_PS_TAPE_SRC=<checkout with memecoin_desk_ps_tape> python3 scripts/research/desk_refusal_ledger.py
"""
import ast
import datetime as dt
import glob
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import desk_reentry_probe as P            # combined() / run_rails()  # noqa: E402

_ro = lambda p: sqlite3.connect('file:%s?mode=ro' % p, uri=True, timeout=10)
ts = lambda s: dt.datetime.fromisoformat(s).timestamp()


def payload_of(inputs_json):
    req = json.loads(inputs_json)['request']
    pl = req['payload']
    if isinstance(pl, str):
        try:
            pl = ast.literal_eval(pl)
        except (ValueError, SyntaxError):
            pl = json.loads(pl)
    return pl


def main():
    desk_dir = os.path.join(ROOT, 'data', 'desk')
    dbs = {os.path.basename(p)[:10]: _ro(p) for p in glob.glob(desk_dir + '/swaps/*.db')}
    led = _ro(os.path.join(ROOT, 'sniper_tracker.db'))
    desk = _ro(os.path.join(ROOT, 'data/memecoin_desk.db'))
    out = []
    for oid, mint, qp, qat, reason in led.execute("SELECT origin_event_id,mint,quote_price,quote_observed_at,terminal_reason FROM memecoin_entry_attempts "
                                                  "WHERE signal_source='desk' AND terminal_reason LIKE 'desk verdict price%' ORDER BY decision_started_at"):
        tid = oid[len('desk:'):]
        size = desk.execute("SELECT inputs_json,output_json FROM desk_tickets WHERE ticket_id=? AND stage='size'", (tid,)).fetchone()
        vet = desk.execute("SELECT ts FROM desk_tickets WHERE ticket_id=? AND stage='vet'", (tid,)).fetchone()
        pl = payload_of(size[0])
        vp = pl['desk_metadata']['verdict_price']
        rails = (json.loads(size[1]) or {}).get('rails') or pl.get('rails') or {}
        t_q = ts(qat)
        day = dt.datetime.fromtimestamp(t_q, dt.timezone.utc).date().isoformat()
        cur = dbs[day]
        snap = vp['snapshot_at']
        row = cur.execute('SELECT received_at,virtual_quote,virtual_token,real_quote FROM swaps WHERE mint=? AND received_at<=? AND virtual_quote>0 ORDER BY received_at DESC,slot DESC,ordinal DESC LIMIT 1', (mint, snap)).fetchone()
        rowq = cur.execute('SELECT received_at,virtual_quote,virtual_token,real_quote FROM swaps WHERE mint=? AND received_at<=? AND virtual_quote>0 ORDER BY received_at DESC,slot DESC,ordinal DESC LIMIT 1', (mint, t_q)).fetchone()
        spot = lambda r: (r[1] / 1e9) / (r[2] / 1e6)
        fx = vp['ref_price_usd'] / spot(row)                     # implied USD per SOL from the verdict snapshot itself
        if os.environ.get('LEDGER_FX'):
            fx = float(os.environ['LEDGER_FX'])                    # sensitivity: one FX for the day (the per-ticket implied FX is noisy: tx ordinals are not chain order inside a slot)
        q_sol = qp / fx
        rows, grad, pool_ok = P.combined(mint, snap - 5, t_q + 1900, [cur], desk_dir)
        series = [(t, p) for t, p, *_ in rows]
        res = P.run_rails(series, t_q, rails.get('sl_pct', 18), rails.get('tp_pct', 30), rails.get('trail_pct', 12), 300)
        # run_rails enters at the last print <= t0; here the entry is the REFUSED QUOTE price, so re-run with an explicit entry
        sl, tp, tr = rails.get('sl_pct', 18), rails.get('tp_pct', 30), rails.get('trail_pct', 12)
        peak, ret, typ, xt = q_sol, None, 'timecap', None
        for t, p in series:
            if t <= t_q:
                continue
            if t > t_q + 300:
                break
            peak = max(peak, p)
            if p <= q_sol * (1 - sl / 100):
                ret, typ, xt = -float(sl), 'stop', t; break
            if p >= q_sol * (1 + tp / 100):
                ret, typ, xt = float(tp), 'tp', t; break
            if peak > q_sol and p <= peak * (1 - tr / 100):
                ret, typ, xt = (p / q_sol - 1) * 100, 'trail', t; break
        if ret is None:
            post = [p for t, p in series if t_q < t <= t_q + 300]
            ret = ((post[-1] / q_sol - 1) * 100) if post else None
        at = lambda s: next((p for t, p in reversed(series) if t <= t_q + s), None)
        path = {('%d' % s): (round((at(s) / q_sol - 1) * 100, 1) if at(s) else None) for s in (30, 60, 180, 300, 600, 1800)}
        win = [p for t, p in series if t_q < t <= t_q + 600]
        out.append(dict(ticket=tid, mint=mint, t_quote=qat, snapshot_age_s=round(t_q - snap, 1), judge_to_quote_s=round(t_q - ts(vet[0]), 1),
                        ref_price_usd=vp['ref_price_usd'], quote_price_usd=qp, gap_vs_verdict_pct=round((qp / vp['ref_price_usd'] - 1) * 100, 2),
                        cap_pct=round(vp['cap_pct'], 2), fx_implied=round(fx, 1), spot_move_snapshot_to_quote_pct=round((spot(rowq) / spot(row) - 1) * 100, 1),
                        quote_vs_spot_at_quote_pct=round((q_sol / spot(rowq) - 1) * 100, 1), fullness_at_snapshot=round(row[3] / 85e9, 2),
                        graduated=grad, pool_path_ok=pool_ok, rails=rails, rail_ret_pct=None if ret is None else round(ret, 1), rail_exit=typ,
                        path_vs_quote_pct=path, max_600s_pct=round((max(win) / q_sol - 1) * 100, 1) if win else None,
                        min_600s_pct=round((min(win) / q_sol - 1) * 100, 1) if win else None))
    for o in out:
        print(json.dumps(o))
    rs = [o['rail_ret_pct'] for o in out if o['rail_ret_pct'] is not None]
    print('\nrefusals %d  graded %d  mean rail return from the refused quote %.1f%%  wins %d' % (len(out), len(rs), sum(rs) / len(rs) if rs else float('nan'), sum(r > 0 for r in rs)))
    json.dump(out, open(os.path.join(ROOT, 'data/research/desk-refusal-ledger.json'), 'w'), indent=1)


if __name__ == '__main__':
    main()
