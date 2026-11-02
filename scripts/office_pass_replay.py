#!/usr/bin/env python3
"""PASS replay (Head request 11318): every hard_filter PASS ticket since a start date, replayed on stored PumpSwap tape under the frozen rails and the listed variants.

Read-only on every tape and database; writes ONE new file (--out), rebuilt from scratch each run, its own table, never outcome_horizons. Reads each ticket's tape once
(longest variant window) and derives every variant from time slices of that read; coverage, gaps and the decoder version are derived per variant from its own window.
  python3 scripts/office_pass_replay.py --out data/office/pass_replay.db [--since 2026-10-27] [--limit N] [--dry]
DESCRIPTIVE AND MODELLED: in-sample; fills modelled; rails fire on the full tape (overstates live); variants are hypotheses; no edge or go-live claim."""
import argparse
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import office_outcome_table as OT                       # noqa: E402  (tape reader and coverage rules live there)
from sniper.office import outcomes as O                 # noqa: E402
from sniper.office import pass_replay as R              # noqa: E402

BASE = dict(sl=30.0, tp=100.0, trail=20.0, cat=60.0)    # frozen policy: stop 30, TP 100, trail 20, catastrophe 60, max hold 3600
# variant -> (entry delay s, rails, mark-blind tick s). Test scenarios, not policy changes.
VARIANTS = {
    'base_d1': (1.0, BASE, None), 'base_d30': (30.0, BASE, None), 'base_d120': (120.0, BASE, None),
    'tp20_d1': (1.0, {**BASE, 'tp': 20.0}, None), 'tp30_d1': (1.0, {**BASE, 'tp': 30.0}, None), 'tp50_d1': (1.0, {**BASE, 'tp': 50.0}, None),
    'stop_off_d1': (1.0, {**BASE, 'sl': 0.0}, None), 'blind15_d1': (1.0, BASE, 15.0),
}
SNAPSHOT_FIELDS = ('age_s', 'mcap_usd', 'liq_usd', 'flow_60s', 'curve_pct', 'price_vs_60s_high_pct', 'price_vs_60s_low_pct', 'creator_prior_launches')
RULE = ('PASS replay. DESCRIPTIVE AND MODELLED: in-sample (PASS set and rails chosen after the crew looked at these coins); fills modelled (entry = last print at or before '
        'pass_at + delay within %d s; cost = sell_probe round trip frozen at PASS plus a fixed %.4f fee, size and landing not modelled); rails fire on the full tape and overstate live '
        'quality (blind15_d1 evaluates them on 15 s ticks); variants are hypotheses, not policy; status counts come first, never a mean over ok rows alone; coin_quiet and '
        'capture_stopped are not outcomes. ok = a covered modelled exit: a rail fired inside the covered span, or the max hold elapsed inside it (in the first file every ok row ended by a rail because coverage rarely reaches 3600 s); a coin with no rail '
        'observed before coverage ended is capture_stopped (what it did after coverage is unknown), so ok rows are conditional on a rail firing or the hold completing inside coverage; print capture_stopped beside any ok figure and never average ok rows alone. Features are as stored at PASS time only.' % (int(R.REF_MAX_AGE_S), R.FEE_FRAC))


def _ts(value):
    try:
        at = datetime.fromisoformat(value)
        return at.timestamp() if at.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def tickets(desk_db, since_epoch):
    """Every hard_filter PASS ticket at or after since: id, time, coin, pool, stored evidence and the emission snapshot fields."""
    conn = OT.ro(desk_db)
    try:
        rows = conn.execute("SELECT t.ticket_id,t.ts,t.network_id,t.mint,t.pool,t.inputs_json FROM desk_tickets t WHERE t.stage='hard_filter' AND t.verdict='PASS' "
                            "AND t.pool IS NOT NULL ORDER BY t.id").fetchall()
        emissions = {}
        out = []
        for tid, ts, net, mint, pool, inputs in rows:
            at = _ts(ts)
            if at is None or at < since_epoch:
                continue
            e = conn.execute("SELECT candidate_json FROM desk_candidate_emissions WHERE ticket_id=? LIMIT 1", (tid,)).fetchone()
            out.append((tid, at, net, mint, pool, inputs, e[0] if e else None))
        return out
    finally:
        conn.close()


def features(inputs_json, candidate_json):
    """Stored-at-PASS values only. Missing stays None and says so; a field is never filled from a later source."""
    f = dict.fromkeys(('probe_round_trip_loss_pct', 'top10_pct', 'creator_pct') + SNAPSHOT_FIELDS)
    try:
        checks = ((json.loads(inputs_json).get('evidence') or {}).get('checks') or {}) if inputs_json else {}
        f['probe_round_trip_loss_pct'] = (checks.get('sell_probe') or {}).get('round_trip_loss_pct')
        f['top10_pct'] = (checks.get('holders') or {}).get('top10_pct')
        f['creator_pct'] = (checks.get('holders') or {}).get('creator_pct')
    except (ValueError, AttributeError, TypeError):
        pass
    try:
        fields = (json.loads(candidate_json).get('snapshot') or {}).get('fields') or {} if candidate_json else {}
        for k in SNAPSHOT_FIELDS:
            row = fields.get(k)
            f[k] = row.get('value') if isinstance(row, dict) and row.get('completeness') in ('complete', 'partial') else None
    except (ValueError, AttributeError, TypeError):
        pass
    return f


def replay_ticket(ps, ticket, versions, create_at, *, now):
    """-> list of row dicts, one per variant. ps: a PumpSwap-like reader (prints(mint, lo, hi), coverage_end(pool, lo, hi))."""
    tid, pass_at, net, mint, pool, inputs, candidate = ticket
    feat = features(inputs, candidate)
    longest = max(d for d, _, _ in VARIANTS.values())
    lo, hi_all = pass_at - R.REF_MAX_AGE_S - 1.0, min(pass_at + longest + R.MAX_HOLD_S + 1.0, math.nextafter(now, math.inf))      # no read reaches past the run's knowledge time
    def read(hi):
        got, end, _c, gaps, gc = ps.prints(mint, lo, hi, start=pass_at)
        return O.clean(got), gaps, gc
    try:
        shared = read(hi_all)
    except OT.TapeUnreadable:
        shared = None          # a corrupt LATER interval must not erase an earlier valid variant: each variant then reads its own window
    rows = []
    for name, (delay, rails, blind) in VARIANTS.items():
        te = pass_at + delay
        hi_v = min(math.nextafter(te + R.MAX_HOLD_S, math.inf), math.nextafter(now, math.inf))
        try:
            prints, gaps_all, gap_check = shared if shared is not None else read(hi_v)
        except OT.TapeUnreadable:
            prints, gaps_all, gap_check = None, [], 'unavailable'
        cov = None
        if prints is None:
            res = dict(status='tape_unreadable', entry_price=None, rail=None, exit_at=None, exit_price=None, gross_ret=None, peak_ret=None, trough_ret=None, last_print_age_s=None)
        else:
            gpoints = [g for g in gaps_all if g <= hi_v] if gap_check == 'checked' else []
            gspans = []
            if gap_check == 'checked' and hasattr(ps, 'window_gaps'):
                gpoints, gspans = ps.window_gaps(pool, te, hi_v)        # whole-window capture stops are intervals, not clipped points
            cov = ps.coverage_end(pool, te, hi_v)           # contiguous from the entry time: rows before it matter only as the reference print
            cov = cov if cov is None else min(cov, now)
            res = R.simulate([p for p in prints if p[0] <= hi_v], te, rails, tape_end=cov,
                             gaps=gpoints, gap_spans=gspans, blind_s=blind, asof=now)
        if res['status'] == 'ok' and gap_check != 'checked': res['status'] = 'ok_unchecked'     # no loss check was possible for this window: a modelled exit, not a clean one
        net_r = R.net_ret(res['gross_ret'], feat['probe_round_trip_loss_pct'])
        rows.append(dict(ticket_id=tid, variant=name, network_id=net, mint=mint, pool=pool, pass_at=pass_at, entry_delay_s=delay, entry_at=te, matured_at=te + R.MAX_HOLD_S + 60.0, coverage_end=cov, asof=now,
                         sl_pct=rails['sl'], tp_pct=rails['tp'], trail_pct=rails['trail'], catastrophe_pct=rails['cat'], mark_blind_s=blind, **res,
                         hold_s=(res['exit_at'] - te) if res['exit_at'] is not None else None, probe_round_trip_loss_pct=feat['probe_round_trip_loss_pct'],
                         fee_frac=R.FEE_FRAC, net_ret=net_r, pair_age_s=(pass_at - create_at) if create_at is not None else None,
                         mcap_usd=feat['mcap_usd'], liq_usd=feat['liq_usd'], top10_pct=feat['top10_pct'], creator_pct=feat['creator_pct'],
                         features_json=json.dumps({k: feat[k] for k in SNAPSHOT_FIELDS}, sort_keys=True, default=str), gap_check=gap_check,
                         tape_decoder_version=O.decoder_version(versions, lo, hi_v)))
    return rows


COLUMNS = ('ticket_id', 'variant', 'network_id', 'mint', 'pool', 'pass_at', 'entry_delay_s', 'entry_at', 'matured_at', 'coverage_end', 'asof', 'sl_pct', 'tp_pct', 'trail_pct', 'catastrophe_pct', 'mark_blind_s',
           'status', 'entry_price', 'rail', 'exit_at', 'exit_price', 'hold_s', 'gross_ret', 'peak_ret', 'trough_ret', 'last_print_age_s', 'probe_round_trip_loss_pct', 'fee_frac',
           'net_ret', 'pair_age_s', 'mcap_usd', 'liq_usd', 'top10_pct', 'creator_pct', 'features_json', 'gap_check', 'tape_decoder_version')


def ensure_schema(conn):
    conn.execute('CREATE TABLE pass_replay(%s, PRIMARY KEY(ticket_id, variant), CHECK(status IN (%s)), CHECK(status IN (\'ok\',\'ok_unchecked\') OR (gross_ret IS NULL AND exit_price IS NULL AND rail IS NULL)))'
                 % (','.join(COLUMNS), ','.join("'%s'" % s for s in R.STATUSES)))
    conn.execute('CREATE TABLE replay_meta(id INTEGER PRIMARY KEY CHECK(id=1), built_at REAL NOT NULL, version TEXT NOT NULL, rule TEXT NOT NULL, since REAL NOT NULL, variants TEXT NOT NULL)')


def pool_creates(tape_db, pools):
    conn = OT.ro(tape_db)
    out = {}
    if conn is None:
        return out
    try:
        for pool in pools:
            r = conn.execute('SELECT MIN(received_at) FROM pools WHERE pool=? AND canonical=1', (pool,)).fetchone()
            if r and r[0] is not None:
                out[pool] = r[0]
    finally:
        conn.close()
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--desk-dir', type=Path, default=ROOT / 'data/desk')
    ap.add_argument('--desk-db', type=Path, default=ROOT / 'data/memecoin_desk.db')
    ap.add_argument('--archive-dir', type=Path, default=ROOT / 'data/archive/pumpswap')
    ap.add_argument('--out', type=Path, required=True)
    ap.add_argument('--scratch', type=Path, default=Path.home() / '.cache/office-replay-scratch')
    ap.add_argument('--decoder-file', type=Path, default=ROOT / 'config/office/tape_decoder_versions.json')
    ap.add_argument('--since', default='2026-10-27')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args(argv)
    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
    now = time.time()
    versions = OT.load_versions(args.decoder_file)
    ts = tickets(args.desk_db, since)
    if args.limit:
        ts = ts[:args.limit]
    pools = {t[3]: t[4] for t in ts}
    args.scratch.mkdir(parents=True, exist_ok=True)
    own = tempfile.mkdtemp(prefix='run-', dir=args.scratch)
    try:
        ps = OT.PumpSwap(args.desk_dir, args.archive_dir, own, pools)
        creates = pool_creates(OT.PumpSwap.T.tape_path(args.desk_dir) if hasattr(OT.PumpSwap, 'T') else ps.T.tape_path(args.desk_dir), set(pools.values()))
        rows = []
        for t in ts:
            rows += replay_ticket(ps, t, versions, creates.get(t[4]), now=now)
        counts = {}
        for r in rows:
            counts.setdefault(r['variant'], {}).setdefault(r['status'], 0)
            counts[r['variant']][r['status']] += 1
        if not args.dry:
            tmp = str(args.out) + '.tmp.%d' % os.getpid()
            conn = sqlite3.connect(tmp)
            try:
                ensure_schema(conn)
                conn.executemany('INSERT INTO pass_replay VALUES(%s)' % ','.join('?' * len(COLUMNS)), [tuple(r[c] for c in COLUMNS) for r in rows])
                conn.execute('INSERT INTO replay_meta VALUES(1,?,?,?,?,?)', (now, 'office-pass-replay-v2', RULE, since, json.dumps({k: [v[0], v[1], v[2]] for k, v in VARIANTS.items()}, sort_keys=True)))
                conn.commit()
            finally:
                conn.close()
            os.replace(tmp, args.out)
        print(json.dumps(dict(tickets=len(ts), rows=len(rows), status_by_variant=counts, asof=now, dry=args.dry), sort_keys=True))
        return 0
    finally:
        import shutil
        shutil.rmtree(own, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
