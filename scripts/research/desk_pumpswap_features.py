#!/usr/bin/env python3
"""Default-OFF descriptive PumpSwap registry. Scores/admission wait for its own frozen F0.

    MEMECOIN_DESK_PUMPSWAP_RANKER_ENABLED=1 python3 scripts/research/desk_pumpswap_features.py \
        --once --at EPOCH --cutoff-slot SLOT --desk-dir PATH
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.services import memecoin_desk_contract as K
from sniper.services.memecoin_desk_safety import verify_u_emission

VERSION = 'pumpswap-features-v1'
_ATOM = re.compile(r'0|[1-9][0-9]{0,19}')
DDL = (
    'CREATE TABLE IF NOT EXISTS first_features(opportunity_id TEXT PRIMARY KEY, venue TEXT NOT NULL, '
    'pool TEXT NOT NULL, mint TEXT NOT NULL, first_rank_at REAL NOT NULL, cutoff_slot INTEGER NOT NULL, '
    'feature_version TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, features_json TEXT, '
    'score REAL, shortlisted INTEGER)',
    'CREATE TABLE IF NOT EXISTS cycle_rows(rank_at REAL NOT NULL, cutoff_slot INTEGER NOT NULL, '
    'opportunity_id TEXT NOT NULL, feature_version TEXT NOT NULL, status TEXT NOT NULL, '
    'reason TEXT, features_json TEXT, score REAL, shortlisted INTEGER, '
    'PRIMARY KEY(rank_at,cutoff_slot,opportunity_id))',
)


def utc_day(at):
    return datetime.fromtimestamp(at, timezone.utc).date().isoformat()


def _stamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'0|[1-9][0-9]*', value):
        return int(value)
    raise ValueError('integer')


def _atom(value, *, positive=False):
    if not isinstance(value, str) or not _ATOM.fullmatch(value):
        raise ValueError('atomic')
    number = int(value)
    if number > 2**64-1 or positive and number == 0:
        raise ValueError('atomic')
    return number


def _unscored(reason):
    return {'status': 'unscored', 'reason': reason, 'features': None, 'score': None, 'shortlisted': None}


def extract(candidate, swaps, gaps, *, cutoff, cutoff_slot, window_s=60):
    """Observed pre-reserve window, strictly before source slot and available on both local clocks.

    This is a descriptive record, not a score or a claim of continuous window coverage.
    """
    if (not _stamp(cutoff) or type(cutoff_slot) is not int or cutoff_slot < 1
            or not _stamp(window_s) or not 0 < window_s <= 3600):
        return _unscored('cutoff_unknown')
    try:
        if candidate.get('schema') != K.PUMPSWAP_SCHEMA:
            return _unscored('venue_mismatch')
        verify_u_emission(candidate, now=cutoff, max_age_s=cutoff + 1)
        field = candidate['snapshot']['fields']['pool_state']
        state = field['value']
        if (candidate['band_entry_slot'] >= cutoff_slot or field['slot'] >= cutoff_slot
                or state['metadata_slot'] >= cutoff_slot):
            return _unscored('future_candidate')
    except (KeyError, TypeError, ValueError, OverflowError):
        return _unscored('metadata_unknown')
    start = max(0, cutoff - window_s)
    for gap in gaps:
        if not isinstance(gap, dict) or not _stamp(gap.get('at')):
            return _unscored('gap_unknown')
        if start <= gap['at'] <= cutoff:
            return _unscored('tape_gap')
    selected, seen = [], {}
    for row in swaps:
        if row.get('pool') != candidate['pool']:
            continue
        try:
            slot = _integer(row['slot'])
        except (KeyError, ValueError, TypeError):
            return _unscored('source_order_unknown')
        if slot >= cutoff_slot:
            continue
        received, processed = row.get('received_at'), row.get('processed_at')
        if not _stamp(received) or not _stamp(processed) or processed < received:
            return _unscored('availability_unknown')
        if not start <= received <= cutoff or processed > cutoff:
            continue
        event_key = (row.get('signature'), row.get('ordinal'))
        if event_key in seen:
            if row != seen[event_key]:
                return _unscored('source_conflict')
            continue
        seen[event_key] = row
        if (row.get('mint') != candidate['mint'] or row.get('quote_mint') != K.WSOL_MINT):
            return _unscored('identity_mismatch')
        if (row.get('map_status') != 'mapped' or not _stamp(row.get('map_known_at'))
                or row['map_known_at'] > processed):
            return _unscored('map_unknown')
        try:
            fees = [_integer(row[k]) for k in ('lp_fee_bps', 'protocol_fee_bps', 'creator_fee_bps')]
            if sum(fees) >= 10000 or _integer(row['cashback_fee_bps']) != 0:
                raise ValueError('fee')
        except (KeyError, ValueError, TypeError):
            return _unscored('fee_unknown')
        if state['metadata_slot'] > slot or state['metadata_known_at'] > received:
            return _unscored('metadata_unknown')
        try:
            base = _atom(row['pre_base_reserve_atomic'], positive=True)
            quote = _atom(row['pre_quote_reserve_atomic'], positive=True)
            paid_quote = _atom(row['quote_atomic'])
            price = ((quote + int(state['virtual_quote_atomic'])) / 1e9
                     / (base / 10**state['base_decimals']))
            if not math.isfinite(price) or price <= 0:
                raise ValueError('reserve')
        except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
            return _unscored('reserves_unknown')
        try:
            is_buy = _integer(row['is_buy'])
            ordinal = _integer(row['ordinal'])
            tx_index = None if row.get('tx_index') is None else _integer(row['tx_index'])
            if is_buy not in (0, 1) or not row.get('signature') or not row.get('actor'):
                raise ValueError('identity')
        except (KeyError, ValueError, TypeError):
            return _unscored('source_order_unknown')
        selected.append({'slot': slot, 'tx_index': tx_index, 'signature': row['signature'],
                         'ordinal': ordinal, 'is_buy': is_buy, 'actor': row['actor'],
                         'quote_sol': paid_quote / 1e9, 'price': price})
    if not selected:
        return _unscored('tape_empty')
    # Receipt order cannot resolve source order between different transactions in one slot.
    for slot in {r['slot'] for r in selected}:
        group = [r for r in selected if r['slot'] == slot]
        if len({r['signature'] for r in group}) > 1:
            indices = [r['tx_index'] for r in group]
            if None in indices or len(set(indices)) != len({r['signature'] for r in group}):
                return _unscored('source_order_unknown')
    selected.sort(key=lambda r: (r['slot'], r['tx_index'] or 0, r['signature'], r['ordinal']))
    features = {'window_s': window_s, 'n_swaps': len(selected),
                'distinct_buyers': len({r['actor'] for r in selected if r['is_buy']}),
                'buy_sol': sum(r['quote_sol'] for r in selected if r['is_buy']),
                'sell_sol': sum(r['quote_sol'] for r in selected if not r['is_buy']),
                'log_return': (math.log(selected[-1]['price'] / selected[0]['price'])
                               if len(selected) > 1 else None),
                'price_basis': 'pre_reserves_plus_proven_virtual_quote'}
    return {'status': 'descriptive', 'reason': None, 'features': features, 'score': None, 'shortlisted': None}


def cycle(desk_dir, *, cutoff, cutoff_slot, window_s=60):
    if (not _stamp(cutoff) or type(cutoff_slot) is not int or cutoff_slot < 1
            or not _stamp(window_s) or not 0 < window_s <= 3600):
        raise ValueError('invalid cutoff or window')
    desk_dir = Path(desk_dir).resolve()
    days = sorted({utc_day(max(0, cutoff-window_s)), utc_day(cutoff)})
    candidates, swaps, gaps = {}, [], []
    tape_missing = gaps_missing = False
    for day in days:
        path = desk_dir/'candidate_emissions_ps'/f'{day}.jsonl'
        if path.exists():
            with path.open() as stream:
                for line in stream:
                    try:
                        c = json.loads(line)
                        verify_u_emission(c, now=cutoff, max_age_s=cutoff+1)
                        if c.get('schema') == K.PUMPSWAP_SCHEMA:
                            candidates.setdefault(c['ticket_id'], c)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        continue
    path = desk_dir/'pumpswap'/'tape.db'
    if not path.exists():
        tape_missing = gaps_missing = True
    else:
        source = None
        try:
            source = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
            source.row_factory = sqlite3.Row
            start = max(0, cutoff-window_s)
            swaps = [dict(row) for row in source.execute(
                'SELECT * FROM swaps_ps WHERE (received_at>=? OR received_at IS NULL) '
                'AND (received_at<=? OR received_at IS NULL) '
                'AND (processed_at<=? OR processed_at IS NULL) AND (slot<? OR slot IS NULL)',
                (start, cutoff, cutoff, cutoff_slot))]
            gaps = [dict(row) for row in source.execute(
                'SELECT * FROM gaps_ps WHERE (at>=? AND at<=?) OR at IS NULL', (start, cutoff))]
        except sqlite3.Error:
            tape_missing = gaps_missing = True
        finally:
            if source is not None:
                source.close()
    by_pool = {}
    for row in swaps:
        by_pool.setdefault(row.get('pool'), []).append(row)
    out = desk_dir/'ranker_ps'/'candidates.db'
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(out, timeout=5)
    try:
        for ddl in DDL:
            conn.execute(ddl)
        summary = {'n_candidates': len(candidates), 'n_descriptive': 0, 'n_unscored': 0}
        for opportunity, candidate in candidates.items():
            got = (_unscored('tape_unavailable') if tape_missing else _unscored('gaps_unavailable') if gaps_missing
                   else extract(candidate, by_pool.get(candidate['pool'], []), gaps, cutoff=cutoff,
                                cutoff_slot=cutoff_slot, window_s=window_s))
            payload = None if got['features'] is None else json.dumps(got['features'], sort_keys=True, allow_nan=False)
            conn.execute('INSERT OR IGNORE INTO first_features VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                         (opportunity, 'pumpswap', candidate['pool'], candidate['mint'], cutoff, cutoff_slot,
                          VERSION, got['status'], got['reason'], payload))
            conn.execute('INSERT OR IGNORE INTO cycle_rows VALUES(?,?,?,?,?,?,?,NULL,NULL)',
                         (cutoff, cutoff_slot, opportunity, VERSION, got['status'], got['reason'], payload))
            summary['n_descriptive' if got['status'] == 'descriptive' else 'n_unscored'] += 1
        conn.commit()
        return summary
    finally:
        conn.close()


def main(argv=None):
    if os.getenv('MEMECOIN_DESK_PUMPSWAP_RANKER_ENABLED', '0').strip() != '1':
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true', required=True)
    parser.add_argument('--at', type=float, required=True)
    parser.add_argument('--cutoff-slot', type=int, required=True)
    parser.add_argument('--window-s', type=float, default=60)
    parser.add_argument('--desk-dir', type=Path, required=True)
    args = parser.parse_args(argv)
    if (not _stamp(args.at) or args.cutoff_slot < 1 or not _stamp(args.window_s) or not 0 < args.window_s <= 3600):
        parser.error('invalid cutoff or window')
    print(json.dumps(cycle(args.desk_dir, cutoff=args.at, cutoff_slot=args.cutoff_slot, window_s=args.window_s),
                     sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
