"""C-only attention within retained_M, at the validated sheet cut; zero provider calls.

The definition hash covers canonical UTF-8 rule JSON (RULE_BYTES), not this source file.
FX identity is snapshot-derived, not provider-issued or independent quote-time proof.
"""
from __future__ import annotations

import collections
import copy
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from time import time as host_now

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_structure_tape as ST

VERSION = 'desk-ps-trending-v1'
MAX_ROWS = 200000
SQL_BUDGET_S = .5
ACTIVE_S, BUY_WINDOW_S, TOP_N, FX_MAX_AGE_S, HEARTBEAT_MAX_AGE_S = 900, 15, 5, 120, 5
RULE_BYTES = json.dumps({
    'version': VERSION, 'universe': 'retained_M', 'active_s': ACTIVE_S, 'buy_window_s': BUY_WINDOW_S,
    'bounds': '(cut-window,cut]', 'top': TOP_N, 'tie': 'pool_ascending',
    'rank_only': 'buy15>0; no positives => unknown:no_buys',
    'quote_mint': K.WSOL_MINT, 'quote_decimals': 9, 'fx_max_age_s': FX_MAX_AGE_S,
    'heartbeat_max_age_s': HEARTBEAT_MAX_AGE_S,
    'heartbeat_age_basis': 'callback_host_clock; market cut remains immutable',
    'fx_id': 'snapshot_hash:sol_usd; snapshot-derived, not provider-issued',
    'fx_known_at': 'snapshot.emission_at-unrounded_pool_state.sol_age_s',
    'knowledge': 'received_at<=processed_at<=as_of,map_known_at<=min(cut,processed_at),slot<cut_slot',
    'exclusions': ['identity_unknown', 'non_wsol', 'invalid_quote'],
    'losses': ['gap', 'decode_receipt', 'overlapping_batch', 'pending'],
    'max_rows': MAX_ROWS, 'sqlite_progress_budget_s': SQL_BUDGET_S,
}, sort_keys=True, separators=(',', ':')).encode()
_DIGEST = hashlib.sha256(RULE_BYTES).hexdigest()
REASONS = frozenset({'trending_cut_mismatch', 'definition_mismatch', 'fx_unknown', 'no_source',
                     'tape_stale', 'coverage_unknown', 'tape_unreadable', 'read_budget',
                     'no_swaps', 'no_buys', 'unrankable_universe'})
_EXCLUDED = frozenset({'identity_unknown', 'non_wsol', 'invalid_quote'})
_HEAD = {'state', 'version', 'definition_sha256'}
_COUNTS = {'universe', 'universe_n', 'excluded_n', 'excluded_by_reason', 'partial', 'lower_bound'}


def definition():
    return {'version': VERSION, 'definition_sha256': _DIGEST}


def unknown(reason, **counts):
    return {'state': 'unknown', **definition(), 'reason': reason, **counts}


def _number(value, low=0, high=1e15):
    return K._number(value, minimum=low, maximum=high)


def _fold(rows, *, as_of, slot, cut, candidate):
    pools = {}
    for pool,mint,quote,buy,atomic,received,processed,mapped,status,network,venue,program,row_slot,sig,ordinal in rows:
        if _number(processed) and processed > as_of or type(row_slot) is int and row_slot >= slot:
            continue
        rec = pools.setdefault(pool, {'mints': set(), 'bad': set(), 'atomic': 0})
        if (not K._key(pool, 32) or not K._key(mint, 32) or not _number(processed, low=received)
                or not _number(mapped) or mapped > cut or mapped > processed
                or status not in ('mapped', 'mapped_curve_derived', 'mapped_intake')
                or network != 'solana' or venue != 'pumpswap' or program != P.AMM_PROGRAM
                or type(row_slot) is not int or row_slot < 0 or not isinstance(sig, str) or not sig
                or type(ordinal) is not int or ordinal < 0
                or pool == candidate['pool'] and mint != candidate['mint']):
            rec['bad'].add('identity_unknown')
        else:
            rec['mints'].add(mint)
        if quote != K.WSOL_MINT:
            rec['bad'].add('non_wsol')
        if received > cut-BUY_WINDOW_S:
            if buy not in (0, 1):
                rec['bad'].add('invalid_quote')
            elif buy:
                if (not isinstance(atomic, str) or not re.fullmatch('0|[1-9][0-9]{0,19}', atomic)
                        or int(atomic) > 2**64-1):
                    rec['bad'].add('invalid_quote')
                else:
                    rec['atomic'] += int(atomic)
    return pools


def trending_for(candidate, *, desk_dir, as_of, ps_facts, sql_budget_s=None, stream=False):
    try:
        snapshot = candidate['snapshot']
        state = snapshot['fields']['pool_state']['value']
        cut, slot = ps_facts['receipt_cut'], ps_facts['cut_slot']
        if (not _number(as_of) or not _number(cut) or cut > as_of
                or type(slot) is not int or slot <= 0 or slot != state['cut_slot']
                or snapshot['emission_at'] != as_of or ps_facts['as_of'] != as_of
                or ps_facts['snapshot_hash'] != snapshot['hash']
                or not re.fullmatch('[0-9a-f]{64}', snapshot['hash'])
                or candidate.get('venue') != 'pumpswap' or candidate.get('network_id') != 'solana'
                or not K._key(candidate['pool'], 32) or not K._key(candidate['mint'], 32)):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return unknown('trending_cut_mismatch')
    usd, age = state.get('sol_usd'), state.get('sol_age_s')
    if not _number(usd) or usd <= 0 or not _number(age, high=FX_MAX_AGE_S) or as_of-age > cut:
        return unknown('fx_unknown')
    fx = dict(id=snapshot['hash']+':sol_usd', known_at=as_of-age, usd=usd, age_s=age)
    try:
        beat = json.loads((Path(desk_dir)/'universe.heartbeat').read_text())
        ts, through = beat['ts'], beat['ps_committed_through']
        read_at = host_now()
        if (not _number(ts) or not _number(read_at) or not 0 <= read_at-ts <= HEARTBEAT_MAX_AGE_S
                or beat.get('connected') is not True
                or beat.get('pumpswap_capture') is not True):
            return unknown('tape_stale')
        if not _number(through) or not cut <= through <= ts:
            return unknown('coverage_unknown')
        pending = beat['ps_pending_gaps']
        if (not isinstance(pending, dict) or type(beat.get('ps_gap_pending')) is not bool
                or bool(pending) != beat['ps_gap_pending'] or any(not _number(v) for v in pending.values())):
            return unknown('tape_unreadable')
    except (OSError, ValueError, KeyError, TypeError):
        return unknown('tape_stale')
    conn = None
    try:
        deadline = time.monotonic()+(SQL_BUDGET_S if sql_budget_s is None else sql_budget_s)   # the Office passes its own; the definition's value stays the desk's
        conn = ST.ro(Path(desk_dir)/'pumpswap/tape.db')
        if conn is None:
            return unknown('no_source')
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 1000)
        conn.execute('BEGIN')
        if tuple(r[2] for r in conn.execute('PRAGMA index_info(swaps_ps_received)')) != ('received_at',):
            return unknown('tape_unreadable')
        unique = [r[1] for r in conn.execute('PRAGMA index_list(swaps_ps)') if r[2] and not r[4]]
        if not any(tuple(r[0] for r in conn.execute('SELECT name FROM pragma_index_info(?) ORDER BY seqno', (name,)))
                   == ('signature', 'ordinal') for name in unique):
            return unknown('tape_unreadable')
        rows = conn.execute('SELECT pool,mint,quote_mint,is_buy,quote_atomic,received_at,processed_at,'
                            'map_known_at,map_status,network_id,venue,program,slot,signature,ordinal '
                            'FROM swaps_ps INDEXED BY swaps_ps_received '
                            'WHERE received_at>? AND received_at<=? LIMIT ?', (cut-ACTIVE_S, cut, MAX_ROWS+1))
        if stream:   # the Office path: same fold, no list of the whole tape in memory. Its deadline also covers the fold, so it needs a larger budget.
            seen = [0]
            def counted(cursor):
                for row in cursor:
                    seen[0] += 1
                    yield row
            pools = _fold(counted(rows), as_of=as_of, slot=slot, cut=cut, candidate=candidate)
            if seen[0] > MAX_ROWS:
                return unknown('read_budget')
        else:
            rows = rows.fetchall()
            if len(rows) > MAX_ROWS:
                return unknown('read_budget')
        intervals = conn.execute('SELECT start_at,through_at,protocol FROM ps_capture_intervals '
                                 'WHERE start_at<=? AND through_at>? LIMIT ?', (cut, cut-ACTIVE_S, MAX_ROWS+1)).fetchall()
        if len(intervals) > MAX_ROWS:
            return unknown('read_budget')
        if (any(not _number(a) or not _number(b) or a > b or protocol != 1 for a,b,protocol in intervals)
                or not any(a <= cut <= b for a,b,_ in intervals)):
            return unknown('coverage_unknown')
        lost = (conn.execute('SELECT 1 FROM gaps_ps INDEXED BY gaps_ps_at '
                             'WHERE at IS NULL OR (at>? AND at<=?) LIMIT 1', (cut-BUY_WINDOW_S, cut)).fetchone() is not None
                or conn.execute('SELECT 1 FROM ps_decode_losses INDEXED BY ps_decode_losses_received '
                                'WHERE received_at IS NULL OR (received_at>? AND received_at<=?) LIMIT 1',
                                (cut-BUY_WINDOW_S, cut)).fetchone() is not None
                or conn.execute('SELECT 1 FROM ps_loss_batches INDEXED BY ps_loss_batches_through '
                                'WHERE at IS NULL OR through_at IS NULL OR (at<=? AND through_at>?) LIMIT 1',
                                (cut, cut-BUY_WINDOW_S)).fetchone() is not None
                or any(at <= cut for at in pending.values()))
        if time.monotonic() > deadline:
            return unknown('read_budget')
    except (sqlite3.Error, ST.TapeUnreadable, OSError) as exc:
        return unknown('read_budget' if 'interrupt' in str(exc).lower() else 'tape_unreadable')
    finally:
        if conn is not None:
            conn.close()

    pools = pools if stream else _fold(rows, as_of=as_of, slot=slot, cut=cut, candidate=candidate)
    if not pools:
        return unknown('no_swaps')
    excluded, ranked = collections.Counter(), []
    for pool,rec in pools.items():
        if len(rec['mints']) != 1:
            rec['bad'].add('identity_unknown')
        value = rec['atomic']/1e9*usd
        if not _number(value):
            rec['bad'].add('invalid_quote')
        if rec['bad']:
            excluded[sorted(rec['bad'])[0]] += 1
        elif value > 0:
            ranked.append((pool, next(iter(rec['mints'])), value))
    covered_to = cut-ACTIVE_S
    for a,b,_ in sorted(intervals):
        if a <= covered_to:
            covered_to = max(covered_to, b)
    partial = bool(excluded or lost or covered_to < cut)
    counts = dict(universe='retained_M', universe_n=len(pools), excluded_n=sum(excluded.values()),
                  excluded_by_reason=dict(excluded), partial=partial, lower_bound=partial)
    if not ranked:
        return unknown('unrankable_universe' if counts['excluded_n'] == len(pools) else 'no_buys', **counts)
    top = sorted(ranked, key=lambda row: (-row[2], row[0]))[:TOP_N]
    return validate({'state': 'ok', **definition(), 'as_of': as_of, 'receipt_cut': cut, 'cut_slot': slot,
                     'entries': [dict(pool=pool, mint=mint, buy15=value, rank=i+1,
                                      is_candidate=pool == candidate['pool'] and mint == candidate['mint'])
                                 for i,(pool,mint,value) in enumerate(top)], 'fx': fx, **counts})


def validate(block):
    def fail():
        raise ValueError('context')

    if (not isinstance(block, dict) or block.get('version') != VERSION
            or block.get('definition_sha256') != _DIGEST):
        fail()
    if block.get('state') == 'unknown':
        if (not isinstance(block.get('reason'), str) or block['reason'] not in REASONS
                or set(block) not in (_HEAD|{'reason'}, _HEAD|{'reason'}|_COUNTS)):
            fail()
    elif (block.get('state') != 'ok' or set(block) != _HEAD|_COUNTS|{'as_of','receipt_cut','cut_slot','entries','fx'}):
        fail()
    else:
        if (not _number(block['as_of']) or not _number(block['receipt_cut']) or block['receipt_cut'] > block['as_of']
                or type(block['cut_slot']) is not int or block['cut_slot'] <= 0):
            fail()
        fx = block['fx']
        if (not isinstance(fx, dict) or set(fx) != {'id','known_at','usd','age_s'}
                or not isinstance(fx['id'], str) or not re.fullmatch('[0-9a-f]{64}:sol_usd', fx['id'])
                or not _number(fx['known_at']) or fx['known_at'] > block['receipt_cut']
                or not _number(fx['usd']) or fx['usd'] <= 0 or not _number(fx['age_s'], high=FX_MAX_AGE_S)
                or abs(block['as_of']-fx['known_at']-fx['age_s']) > 1e-6):
            fail()
        entries = block['entries']
        if not isinstance(entries, list) or not 1 <= len(entries) <= TOP_N:
            fail()
        for i,row in enumerate(entries):
            if (not isinstance(row, dict) or set(row) != {'pool','mint','buy15','rank','is_candidate'}
                    or not K._key(row['pool'], 32) or not K._key(row['mint'], 32)
                    or not _number(row['buy15']) or row['buy15'] <= 0
                    or type(row['rank']) is not int or row['rank'] != i+1 or type(row['is_candidate']) is not bool):
                fail()
        if (len({r['pool'] for r in entries}) != len(entries) or sum(r['is_candidate'] for r in entries) > 1
                or entries != sorted(entries, key=lambda r: (-r['buy15'], r['pool']))):
            fail()
    if 'universe' in block:
        excluded = block['excluded_by_reason']
        if (block['universe'] != 'retained_M' or type(block['universe_n']) is not int or block['universe_n'] < 0
                or type(block['excluded_n']) is not int or not 0 <= block['excluded_n'] <= block['universe_n']
                or not isinstance(excluded, dict) or not set(excluded) <= _EXCLUDED
                or any(type(n) is not int or n < 0 for n in excluded.values()) or sum(excluded.values()) != block['excluded_n']
                or type(block['partial']) is not bool or type(block['lower_bound']) is not bool
                or block['lower_bound'] != block['partial'] or block['excluded_n'] and not block['partial']):
            fail()
        if block['state'] == 'ok' and len(block['entries']) > min(TOP_N, block['universe_n']-block['excluded_n']):
            fail()
    return copy.deepcopy(block)
